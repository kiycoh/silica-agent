# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_remember: the model's one way to write user memory (ADR-0076), and
the gate every such write passes.

The model saves on its own and says what it saved; the user checks it after
the fact (Undo on the saved line, Settings > Memory). Until 2026-10-23 a second
gate refused any save the user's current message did not ask for, which did
not stop the model from wanting one: that day qwen3.5-9b tried to keep a course
plan's exam date unasked, was refused, and told the user "Ho salvato il
contesto" (narration 6aebe4c1e7f8). The owner removed the gate the same day
(ADR-0076, amendment 2026-10-23): a save is not something to ask leave for.

What stays is a property of the turn, not a line in the prompt:

- untainted: no tool that brings outside content into the conversation has
  run in it, on this turn or any earlier one. A page read on turn N is still
  in context on turn N+1: Trojan Hippo (arXiv 2605.01970, §6.2) found
  filtering on the user message unsound for tool-call memories and
  per-session no-untrusted-write sound. With unasked saves a page no longer
  needs the user to say "remember", so this gate is the whole defence.
- a chat turn the user started: a batch, a worker loop or the MCP server
  writes nothing.

`/remember <text>` and the Settings editor never come through here: those are
the user's own keystrokes (silica/kernel/user_memory.py).
"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field

from silica.tools import tool

# Tools whose result carries content from outside the user's own words and
# notes. One set, read by both halves of the taint gate (history at turn
# start, results inside the turn).
INGRESS = frozenset({
    "silica_web_answer",    # an answer composed from fetched web pages
    "silica_run_injector",  # an ingested file's text, distilled into the turn
    "silica_read",          # any file under the root (PDFs, downloads), not only notes the user wrote
    "web_fetch",            # /web: a page's text
    "web_search",           # /web: result titles and snippets
    "find_in_page",         # /web: lines grepped out of a fetched page
    "remember",             # /web: a verbatim quote banked from a page (not this tool)
    # Earlier answers, which may restate a page an earlier chat fetched: the
    # taint of that chat ends with it, its words do not.
    "silica_session_search",
})

# An MCP connector (ADR-0091) brings a third party's text in, whatever the
# server is: its tools are `mcp__<key>`, one per configured server, so the
# set above cannot name them and the prefix does.
_INGRESS_PREFIX = "mcp__"


def _ingress(name: str) -> bool:
    return name in INGRESS or name.startswith(_INGRESS_PREFIX)

# The bare `/remember` expands to this (cli._SHORTCUTS).
REMEMBER_DIRECTIVE = "/remember: the user asks you to keep something from this conversation."

# "Nothing was saved" first and in those words: refused, qwen3.5-9b once told
# the user "Ho salvato l'informazione" in the same breath as the alternative.
_SUGGEST = ("Tell the user that nothing was saved, and that they can save it themselves "
            "with /remember <text> (when this result has a `suggest`, that is the exact "
            "command to give them) or in Settings > Memory.")


@dataclass
class _Turn:
    request: str            # the current user request, the last user message
    directive: bool         # that message is a harness directive (origin cli)
    chat: bool              # an interactive chat turn, not a batch or worker loop
    tainted_by: str = ""    # the first ingress tool this conversation ran
    # Vault paths the running tool's result holds, handed over by the tool
    # itself (returned_notes) and checked by after_tool (ADR-0090 point 8).
    returned: list[str] = field(default_factory=list)


# The key a tool message carries once its call tainted the conversation by a
# path, which no tool name in the history records. Stripped from the wire
# (providers.INTERNAL_KEYS) and kept by the narration, so a reopened chat stays
# tainted. Compaction rewrites a message's content, never its other keys.
TAINT_KEY = "silica_taint"


# Set by run_agent at entry, like read_scope.REQUESTS: tools run on the loop's
# thread and see it; a call from anywhere else (the MCP server, a test) sees
# None and is refused, because nothing shows that a user asked.
_TURN: ContextVar[_Turn | None] = ContextVar("user_memory_turn", default=None)


def _called(messages: list[dict]) -> list[str]:
    # Assistant tool calls, never tool results: compaction rewrites results
    # into stubs but leaves the calls alone, so the record survives a sweep.
    return [str((tc.get("function") or {}).get("name") or "")
            for m in messages if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or ()]


def begin_turn(messages: list[dict], *, chat: bool) -> _Turn:
    request, directive = "", False
    for m in reversed(messages):
        if m.get("role") == "user":
            request, directive = str(m.get("content") or ""), m.get("origin") == "cli"
            break
    turn = _Turn(request, directive, chat,
                 next((n for n in _called(messages) if _ingress(n)), "")
                 or next((str(m[TAINT_KEY]) for m in messages
                          if m.get("role") == "tool" and m.get(TAINT_KEY)), ""))
    _TURN.set(turn)
    return turn


def returned_notes(paths: Iterable[str]) -> None:
    """A note-reading tool hands the gate the vault paths its result holds,
    from its own result objects: recall strips frontmatter from the text the
    model sees, so the mail stamp could never be read off it (ADR-0090
    point 8). Outside a turn there is nothing to taint."""
    turn = _TURN.get()
    if turn is not None:
        turn.returned.extend(str(p) for p in paths if p)


def after_tool(turn: _Turn, name: str) -> str:
    """Taint the turn on what the call brought in; returns the taint this call
    added, "" when it added none, for the loop to mark the tool message with."""
    before = turn.tainted_by
    if _ingress(name) and not turn.tainted_by:
        turn.tainted_by = name
    returned, turn.returned = turn.returned, []
    if returned and not turn.tainted_by:
        from silica.sources.mail import mail_note_among

        try:
            hit = mail_note_among(returned)
        except (OSError, ValueError):
            # The import record cannot be read, so no returned note can be
            # told apart from a mail note: refusing memory for this
            # conversation is the failure the gate can afford (/remember stays
            # open), a turn that crashes on every note read is not.
            turn.tainted_by = "an unreadable mail import record"
        else:
            if hit:
                turn.tainted_by = f"the mail note {hit}"
    # silica_web_answer runs a run_agent of its own on this thread, which
    # opened its own turn; without this the rest of the outer turn would be
    # judged against the inner one's request.
    _TURN.set(turn)
    return turn.tainted_by if turn.tainted_by != before else ""


# "Dimentica che preferisco risposte brevi" became a replace with "prefers long
# answers" on 3 of 3 final qwen3.5-9b runs and 2 of 3 qwen3.5-4b ones
# (evals/probe_user_memory.py, 2026-10-08): a preference nobody stated. A
# request that only asks to forget may only forget. The negated asks ("non
# dimenticare", "don't forget") are keeps, so they are removed first.
# ponytail: Italian and English, by pattern. In any other language a request
# that only asks to forget is not recognised, and a replace can write the
# opposite preference again. Upgrade path, once the first hosted user
# (ADR-0071) writes in a third language: a typed decision on the current
# request (silica/agent/decide.py, as read_scope.py asks one) with these
# patterns kept as the offline fallback.
_NOT_FORGET = re.compile(r"\b(?:non\s+(?:ti\s+)?(?:dimentic|scord)\w*|(?:don['’]?t|do\s+not|never)\s+forget)",
                         re.IGNORECASE)
_FORGET = re.compile(r"\b(?:dimentic\w*|scord\w*|forget\w*|(?:cancella|elimina|rimuovi|togli|leva)\w*"
                     r"\b[^.!?\n]{0,40}?\bmemoria|(?:remove|delete|erase|drop|clear)\b[^.!?\n]{0,40}?\bmemory)",
                     re.IGNORECASE)
_KEEP = re.compile(r"\b(?:ricord\w*|memorizz\w*|tien\w*\s+(?:bene\s+)?(?:a\s+mente|presente)|segna\w*|annota\w*|"
                   r"salva\w*|aggiung\w*|aggiorna\w*|sostitui\w*|remember|keep\b|memori[sz]e|note|save|"
                   r"add|update|replace|store)", re.IGNORECASE)


def forget_only(text: str) -> bool:
    """Whether a request asks to forget and nothing else."""
    rest = _NOT_FORGET.sub(" ", text)
    return bool(_FORGET.search(rest)) and not _KEEP.search(_FORGET.sub(" ", rest))


_WEEKDAYS = {
    **dict.fromkeys(("lunedì", "lunedi", "monday", "mon"), 0),
    **dict.fromkeys(("martedì", "martedi", "tuesday", "tue"), 1),
    **dict.fromkeys(("mercoledì", "mercoledi", "wednesday", "wed"), 2),
    **dict.fromkeys(("giovedì", "giovedi", "thursday", "thu"), 3),
    **dict.fromkeys(("venerdì", "venerdi", "friday", "fri"), 4),
    **dict.fromkeys(("sabato", "saturday", "sat"), 5),
    **dict.fromkeys(("domenica", "sunday", "sun"), 6),
}
_MONTHS = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")
_MONTHS_EN = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAY_DATE = re.compile(r"\b(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b\W+"
                       r"(\d{1,2})\s+([a-z]{3})[a-z]*(?:\s+(\d{4}))?", re.IGNORECASE)


def _date_mismatch(text: str, today) -> str:
    """The first weekday-and-date pair in `text` that disagree, as the sentence
    the model relays; "" when there is none. Asked to keep "giovedì" on
    Wednesday 2026-10-07, qwen3.5-9b stored "giovedì 07 ottobre 2026" and
    "giovedì 12 ottobre 2026" on 3 of 3 live runs, with the instruction to
    keep the user's words in the schema: the instruction did not hold, a
    calendar does. ponytail: Italian and English, day-month-year order only
    ("12/10" and "October 12" pass unchecked); extend with the intent
    patterns' languages."""
    import datetime as dt

    for m in _DAY_DATE.finditer(text):
        mon = m.group(3).lower()
        month = (_MONTHS.index(mon) if mon in _MONTHS else
                 _MONTHS_EN.index(mon) if mon in _MONTHS_EN else -1) + 1
        if not month:
            continue
        year = int(m.group(4)) if m.group(4) else today.year
        try:
            date = dt.date(year, month, int(m.group(2)))
        except ValueError:
            return f"Nothing was saved: {m.group(0)} is not a date. Keep the user's own words."
        if date.weekday() != _WEEKDAYS[m.group(1).lower()]:
            return (f"Nothing was saved: {date.isoformat()} is a {date:%A}, not {m.group(1)}. "
                    "Write the day as the user said it, without a computed date, and save again.")
    return ""


def _gate() -> str:
    """The first gate that refuses, as the sentence the model relays; "" when
    the write may proceed."""
    from silica.kernel import user_memory as um

    if why := um.disabled_reason():
        return why
    turn = _TURN.get()
    if turn is None or not turn.chat:
        return f"Nothing was saved: memory is written only from a chat turn the user started. {_SUGGEST}"
    if turn.tainted_by:
        # The last sentence because saves are unasked: a model that retried on
        # each later turn of the chat would draw one more "Not saved" line
        # under every answer.
        return (f"Nothing was saved: this conversation brought in outside content ({turn.tainted_by}), "
                "which could carry instructions written by someone else, so memory is not "
                f"written from it. {_SUGGEST} Do not try to save again in this conversation "
                "unless the user asks you to.")
    return ""


@tool(cls="atomic")
def silica_remember(
    action: Annotated[Literal["save", "replace", "forget"], Field(
        description="save adds text; replace swaps the entry containing old for text; "
                    "forget removes the entry containing old")],
    # Required, not defaulted to vault: with a default, qwen3.5-9b omitted it
    # and "Mi chiamo Giulia" landed in the vault file on 2 of 3 live runs
    # (evals/probe_user_memory.py, 2026-10-07).
    scope: Annotated[Literal["user", "vault"], Field(
        description="user: the person, true in every vault (name, preferences, how they want "
                    "help); vault: the work in this vault (project, exams, deadlines, decisions)")],
    # Dates in the user's words: asked to keep "giovedì" on Wednesday
    # 2026-10-07, qwen3.5-9b stored "12 ottobre" once and "07 ottobre" twice.
    text: Annotated[str, Field(description="The entry: one short self-contained line. Keep dates "
                                           "and weekdays in the user's own words, never computed")] = "",
    old: Annotated[str, Field(description="replace/forget: the entry's [id] as memory shows it, "
                                          "or words copied from it")] = "",
) -> dict[str, Any]:
    """Keep, correct or forget what a later chat needs about the user and their work:
    who they are, preferences, goals, exams and deadlines, decisions, where the work
    stands. Save it yourself as soon as it comes up and never ask whether to save;
    then say in one line what you saved, so the user can check it and undo or edit it.
    Subject matter belongs in notes, not here. What is saved shows from the next chat
    on. If it refuses, tell the user what it says they can do."""
    from silica.kernel import user_memory as um

    if why := _gate():
        out = {"error": why}
        if action == "save" and text.strip():
            # The exact command, so the model relays it instead of composing
            # one (qwen3.5-9b wrote "/reremember" once in three refusals), and
            # the GUI can offer it as one click: the user sees the text and
            # sends it, which is the consent the gate could not infer.
            out["suggest"] = f"/remember {' '.join(text.split())}"
        return out
    import datetime as dt

    turn = _TURN.get()
    if action != "forget" and turn and not turn.directive and forget_only(turn.request):
        return {"error": "Nothing was saved: the user asked only to forget. Call forget with "
                         "the entry's [id] and write no new entry; if they want something "
                         "else kept, they will say so."}
    if action != "forget" and (why := _date_mismatch(text, dt.date.today())):
        return {"error": why}
    if action == "save":
        out = um.save(scope, text)
    else:
        # qwen3.5-9b put the entry to forget in `text` on 3 of 3 forget runs
        # (probe_user_memory, 2026-10-07), and refused, saved the opposite
        # preference instead.
        needle = old or (text if action == "forget" else "")
        out = _addressed(um, action, scope, needle, text)
    if not out.get("ok"):
        out["error"] = out.get("error", "not saved")
        del out["ok"]   # the loop reads a failure off "error"; ok=False beside it says it twice
        out.pop("miss", None)
    return out


def _addressed(um, action: str, scope: str, needle: str, text: str) -> dict:
    """replace/forget in the named file, then in the other one: the model
    names the default scope for an entry the snapshot shows under the other
    (the same 3 of 3 forget runs). A miss in both shows both files' entries,
    so the next call can quote one."""
    def edit(s: str) -> dict:
        return um.replace(s, needle, text) if action == "replace" else um.forget(s, needle)

    first = edit(scope)
    if not first.get("miss"):
        return first
    other = "user" if scope == "vault" else "vault"
    second = edit(other)
    # Found there, or ambiguous there (its refusal lists the candidates). A
    # refusal without entries (no vault open) says nothing about the entry.
    if second.get("ok") or ("entries" in second and not second.get("miss")):
        return second
    first["entries"] = {scope: first.get("entries", []), other: second.get("entries", [])}
    return first


def suggestion(result: Any) -> str:
    """A refused save's `/remember <text>`, for the GUI's one-click line; ""
    for anything else."""
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return ""   # not JSON: not a result this tool returned
    return str(data.get("suggest") or "") if isinstance(data, dict) and "error" in data else ""


def parse_result(result: Any) -> dict | None:
    """A tool result's write, for the GUI's Saved line; None for a refusal."""
    from silica.kernel.user_memory import undo_payload

    try:
        data = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return None   # not JSON: not a result this tool returned, so nothing to draw
    return undo_payload(data) if isinstance(data, dict) else None
