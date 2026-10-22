# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Did every recall call in this turn come back empty, or run without a leg?
Read off the trace.

The verdict is mechanical on purpose. Asking the model to self-assess whether
what it recalled was sufficient (the L2 sufficiency router) was kill-tested and
killed on 2026-07-24: the prompt changed its behavior without improving the
answers. So the hint fires from tool results, never from the model's judgment.
The same holds for the degraded line (ADR-0094): it names the missing leg and
where to fix it, and tells nobody what to conclude.
"""
from __future__ import annotations

import json

from silica.agent.events import ToolCompleteEvent
from silica.agent.loop import _is_tool_failure
from silica.kernel.write.provenance import number_tokens, unfounded_numbers

THIN_COVERAGE_HINT = "vault coverage was thin: /web to search the web"

# The degraded line, one per leg: `<prefix>: the <role> <phrase><fix>` (ADR-0094
# point 5). The prefix says what the answer lost, not which server failed: the
# reader judges the answer, and "lexical only" is what changes how to read it.
_LEG = {"embed": ("lexical only", "embedder", "SILICA_EMBEDDING_TIMEOUT", "embedding_timeout"),
        "rerank": ("not reranked", "reranker", "SILICA_RERANK_TIMEOUT", "rerank_timeout")}
_PHRASE = {"unreachable": "is unreachable", "loading": "is still loading",
           "rejected": "refused the model name", "failed": "failed"}
# Where to fix it, per surface. The GUI names its own panels; the REPL has none,
# so both Settings pointers become the doctor; a tool result can be read by
# either, or by an MCP client, so it names both. Loading has no fix: it ends.
_FIX = {
    "gui": {"unreachable": "start it in Settings, Endpoints",
            "diagnose": "Settings, Diagnostics names the cause"},
    "repl": {"unreachable": "run silica doctor", "diagnose": "run silica doctor"},
    "tool": {"unreachable": "start it in Settings, Endpoints, or run silica doctor",
             "diagnose": "Settings, Diagnostics names the cause, or run silica doctor"},
}


def degraded_lines(degraded: dict, surface: str = "gui") -> list[str]:
    """One line per leg this call or turn ran without, the embedder first."""
    from silica.config import CONFIG

    lines = []
    for leg in ("embed", "rerank"):
        cause = degraded.get(leg)
        if cause is None:
            continue
        prefix, role, knob, timeout_attr = _LEG[leg]
        if cause == "slow":
            phrase, fix = f"did not answer in {getattr(CONFIG, timeout_attr):g} s", f"raise {knob}"
        else:
            phrase = _PHRASE.get(cause, "failed")
            fix = {"unreachable": _FIX[surface]["unreachable"],
                   "loading": ""}.get(cause, _FIX[surface]["diagnose"])
        lines.append(f"{prefix}: the {role} {phrase}" + (f" · {fix}" if fix else ""))
    return lines

# Per-tool emptiness. `silica_related` is excluded on purpose: it takes an
# existing note, so an empty result means "no neighbors", not "no coverage".
# `silica_search` (title match only) is too weak a signal to escalate on.
#
# ponytail: hard miss only — every recall call returned zero. "Few or low-scored
# hits" needs a scored threshold nobody has benched; revisit with an eval.
_MISS = {
    "silica_recall": lambda d: not d.get("notes"),
    "silica_semantic_search": lambda d: not d.get("results"),
    "silica_search_context": lambda d: not d.get("notes_matched"),
}


def found_nothing(name: str | None, result: object) -> bool:
    """A vault search that matched nothing: the only call a `final` web answer
    may stand beside and still end the turn (loop.py).

    Kept beside _MISS because _MISS is the one list of what an empty retrieval
    looks like. `silica_search` counts here though not there: too weak a signal
    to send the user to the web on, but a title search that matched nothing put
    nothing in front of the chat model that the web answer would leave out. An
    error is not nothing: the chat model may owe the user a word about it.
    """
    predicate = (lambda d: not d.get("paths")) if name == "silica_search" else _MISS.get(name or "")
    if predicate is None:
        return False
    try:
        data = json.loads(result)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and "error" not in data and bool(predicate(data))


class RecallWatch:
    """Wraps a turn's tool_progress_callback; `.thin` is the verdict afterwards.

    Forwards every event untouched — the renderer downstream never knows it is
    being watched.
    """

    def __init__(self, inner=None, surface: str = "gui") -> None:
        self._inner = inner
        # Which surface ran the turn: the stored note keeps the fix it named.
        self.surface = surface
        self.calls = 0
        self.misses = 0
        # Leg to cause over every retrieval call this turn, first cause per leg.
        self.degraded: dict[str, str] = {}
        # Did this turn go to the web through silica_web_answer? The UI reapplies
        # the citation block when it did (web_research.relay_sources).
        self.web_answer = False
        # Every number the turn's results showed, from the event and not from the
        # tool message, which the loop may already have stubbed (ADR-0095).
        self.numbers: set[str] = set()

    def __call__(self, event) -> None:
        if isinstance(event, ToolCompleteEvent):
            if event.name == "silica_web_answer":
                self.web_answer = True
            self.numbers |= result_numbers(event.result, event.args)
            self._inspect(event)
        if self._inner is not None:
            self._inner(event)

    def _inspect(self, event: ToolCompleteEvent) -> None:
        predicate = _MISS.get(event.name)
        if predicate is None:
            return
        try:
            data = json.loads(event.result)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        # Before the error check: a call with no leg left answers with an error,
        # and the error alone points at rebuilding an index that is fine.
        degraded = data.get("degraded")
        for leg, cause in (degraded.items() if isinstance(degraded, dict) else ()):
            self.degraded.setdefault(leg, cause)
        if "error" in data:
            # A degraded index or an offline embedder is not thin coverage:
            # sending the user to the web here would mask the real problem.
            return
        self.calls += 1
        if predicate(data):
            self.misses += 1

    @property
    def thin(self) -> bool:
        """True when the turn searched the vault and every search missed.

        Never when a search ran without the embedder: that turn did not search
        by meaning, so its misses, silica_search_context's included, are not a
        finding about coverage and the web is the wrong fix. A turn that lost
        only the reranker keeps the rule: a reorder cannot change membership.
        """
        if "embed" in self.degraded:
            return False
        return self.calls > 0 and self.calls == self.misses

    def notes(self) -> list[dict]:
        """This turn's `retrieval` notes for the end-of-turn channel (turn_notes):
        the degraded legs first, the cause before the miss it may explain."""
        lines = degraded_lines(self.degraded, self.surface)
        # Not after the turn already went to the web: "/web to search the web"
        # under a web answer sends the user where they just were.
        if self.thin and not self.web_answer:
            lines.append(THIN_COVERAGE_HINT)
        return [{"kind": "retrieval", "text": line} for line in lines]


# --- ADR-0095: the numbers an answer states that its session never read -----

NUMBERS_NOTE_MAX = 5  # named in the note; the rest are counted


def _leaves(obj, out: list[str]) -> None:
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        out.append(str(obj))
    elif isinstance(obj, dict):
        for value in obj.values():
            _leaves(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _leaves(value, out)


def result_text(result: str, args: dict | None = None) -> str:
    """One successful tool result as the session read it, "" for a failed one.

    A JSON result is read by its string and number leaves: raw JSON doubles
    every backslash and writes a newline as `\\n`, which glues the next digits
    to a letter and hides them, and a number field (a count, a table statistic)
    is evidence as it stands. A write tool's result counts too: it reports what
    happened ("notes created: 20" was flagged when writes were left out, probe
    of 2026-10-11, session 87c32572c511).

    A string leaf equal to one of the call's own string *args* is dropped: the
    argument is the model's text, and a result that echoes it ("query") let the
    model ground the dates it had guessed by searching for them (GUI check,
    2026-10-11, "Giordano Bruno 1548 1600"). Exact leaves only, and strings
    only: a count that happens to equal `k` is still a count.
    """
    if _is_tool_failure(result):
        return ""
    try:
        obj = json.loads(result)
    except (ValueError, TypeError):
        return str(result)
    out: list[str] = []
    _leaves(obj, out)
    if args:
        echoed = _string_leaves(args)
        out = [leaf for leaf in out if leaf not in echoed]
    return "\n".join(out)


def _string_leaves(obj) -> set[str]:
    if isinstance(obj, str):
        return {obj}
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        return set().union(*(_string_leaves(v) for v in obj)) if obj else set()
    return set()


def result_numbers(result: str, args: dict | None = None) -> set[str]:
    """The number tokens of one tool result, raw (`result_text`)."""
    return number_tokens(result_text(result, args))


def numbers_from_beats(beats) -> set[str]:
    """The session set rebuilt from a narration: every successful `tool` beat,
    whose payload keeps `ToolCompleteEvent.result` whole. The same events fed
    the live watch, so a reopened chat checks against what the live one did."""
    out: set[str] = set()
    args: dict[str, dict] = {}  # the span's opening beat holds the call's arguments
    for beat in beats:
        if beat.get("kind") != "tool":
            continue
        payload = beat.get("payload") or {}
        if beat.get("status") == "running":
            args[beat.get("id")] = payload.get("args") or {}
        elif beat.get("status") == "done" and isinstance(payload.get("result"), str):
            out |= result_numbers(payload["result"], args.get(beat.get("id")))
    return out


def _text_of(content) -> str:
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def turn_evidence(turn_numbers: set[str], messages: list[dict],
                  session_numbers: set[str]) -> set[str]:
    """What a turn's answer may state: this turn's results, the earlier turns'
    results, the seed and the user's messages.

    The seed and the user's messages are read from *messages* here, because
    compaction never touches them. Tool messages are not read: compaction and a
    relevance verdict stub them, and the raw sets stand in for them. The model's
    own messages and every tool call's arguments are never evidence, so a number
    the model invented in one turn cannot ground itself in the next.
    """
    evidence = turn_numbers | session_numbers
    for m in messages:
        if m.get("role") in ("system", "user"):
            evidence |= number_tokens(_text_of(m.get("content")))
    return evidence


def unfounded_in_turn(answer: str, turn_numbers: set[str], messages: list[dict],
                      session_numbers: set[str]) -> list[str]:
    """The numbers of *answer* that `turn_evidence` does not hold."""
    return unfounded_numbers(answer, turn_evidence(turn_numbers, messages, session_numbers))


def numbers_notes(flagged: list[str]) -> list[dict]:
    """The turn's `numbers` note for the end-of-turn channel (turn_notes), or
    none when nothing was flagged: no line ever says the numbers are fine,
    because the rule cannot know that."""
    if not flagged:
        return []
    # " · " and not ", ": "95, 1,067" reads as three numbers, and a grouped
    # number is how a model writes a count (seen on the first GUI check,
    # 2026-10-11). The app's other inline lists already use it ("730 notes ·
    # 1,385 links").
    shown = " · ".join(flagged[:NUMBERS_NOTE_MAX])
    rest = len(flagged) - NUMBERS_NOTE_MAX
    more = f" and {rest} more" if rest > 0 else ""
    return [{"kind": "numbers", "text": f"numbers not in what this turn read: {shown}{more}"}]


# The tools that hand the model a note's own words, and where each names the
# note: in its arguments, its result, or both (shapes read off stored sessions,
# 2026-10-22). A title search is left out: it lists candidates, it reads none.
_READS_ARGS = {"silica_read_note": ("name",), "silica_outline": ("name",),
               "silica_read": ("path",), "silica_recall": ("notes",)}
_READS_RESULT = {"silica_recall": "notes", "silica_read": "path", "silica_search_context": "hits"}
READS_KEY = "silica_reads"   # the read notes stored on the answer, for a reopened chat


def read_refs(messages: list[dict], end: int | None = None) -> list[str]:
    """The notes the turn ending at `messages[end - 1]` read, as the calls named
    them (a title or a path), for linking its answer (ADR-0127).

    The turn starts after the last user message. A tool result is read only
    while it is whole: compaction stubs old ones, which is why the answer keeps
    the resolved list under READS_KEY once it is drawn.
    """
    msgs = messages[:end] if end is not None else messages
    start = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1) + 1
    calls: dict[str, str] = {}
    out: list[str] = []
    for m in msgs[start:]:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls[tc.get("id")] = name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                continue   # a malformed call read nothing
            for key in _READS_ARGS.get(name, ()):
                v = args.get(key) if isinstance(args, dict) else None
                out += [v] if isinstance(v, str) else [x for x in v or () if isinstance(x, str)]
        if m.get("role") != "tool" or (key := _READS_RESULT.get(calls.get(m.get("tool_call_id"), ""))) is None:
            continue
        try:
            v = json.loads(m.get("content") or "").get(key)
        except (ValueError, TypeError, AttributeError):
            continue   # a stub or a failure names no note
        items = v if isinstance(v, list) else [v]
        out += [x if isinstance(x, str) else str(x.get("path") or "") for x in items
                if isinstance(x, (str, dict))]
    return [r for r in dict.fromkeys(out) if r]


def numbers_of_session(sid: str | None) -> set[str]:
    """`numbers_from_beats` over the narration of session *sid*: what
    /session/load and /resume rebuild the session set from. Empty for no
    session, and for a legacy snapshot, which kept no raw results."""
    from silica.agent import narration

    if not sid:
        return set()
    return numbers_from_beats(narration.read_beats(narration.narration_dir() / f"{sid}.jsonl"))
