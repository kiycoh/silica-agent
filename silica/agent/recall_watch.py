# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Did every recall call in this turn come back empty? Read off the trace.

The verdict is mechanical on purpose. Asking the model to self-assess whether
what it recalled was sufficient (the L2 sufficiency router) was kill-tested and
killed on 2026-07-24: the prompt changed its behavior without improving the
answers. So the hint fires from tool results, never from the model's judgment.
"""
from __future__ import annotations

import json

from silica.agent.events import ToolCompleteEvent
from silica.agent.loop import _is_tool_failure
from silica.kernel.write.provenance import number_tokens, unfounded_numbers

THIN_COVERAGE_HINT = "vault coverage was thin: /web to search the web"

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


class RecallWatch:
    """Wraps a turn's tool_progress_callback; `.thin` is the verdict afterwards.

    Forwards every event untouched — the renderer downstream never knows it is
    being watched.
    """

    def __init__(self, inner=None) -> None:
        self._inner = inner
        self.calls = 0
        self.misses = 0
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
        if not isinstance(data, dict) or "error" in data:
            # A degraded index or an offline embedder is not thin coverage:
            # sending the user to the web here would mask the real problem.
            return
        self.calls += 1
        if predicate(data):
            self.misses += 1

    @property
    def thin(self) -> bool:
        """True when the turn searched the vault and every search missed."""
        return self.calls > 0 and self.calls == self.misses

    def notes(self) -> list[dict]:
        """This turn's `retrieval` notes for the end-of-turn channel (turn_notes)."""
        return [{"kind": "retrieval", "text": THIN_COVERAGE_HINT}] if self.thin else []


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


def numbers_of_session(sid: str | None) -> set[str]:
    """`numbers_from_beats` over the narration of session *sid*: what
    /session/load and /resume rebuild the session set from. Empty for no
    session, and for a legacy snapshot, which kept no raw results."""
    from silica.agent import narration

    if not sid:
        return set()
    return numbers_from_beats(narration.read_beats(narration.narration_dir() / f"{sid}.jsonl"))
