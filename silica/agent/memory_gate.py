# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The taint gate: what a conversation has brought in, for the one write the
model makes on its own, silica_remember (ADR-0076, ADR-0090).

Loop policy and not the tool's: run_agent opens a turn, marks it after every
call and carries the mark on the tool message.

A conversation is tainted once a tool that brings outside content into it has
run, on this turn or any earlier one. A page read on turn N is still in context
on turn N+1: Trojan Hippo (arXiv 2605.01970, §6.2) found filtering on the user
message unsound for tool-call memories and per-session no-untrusted-write sound.
With unasked saves a page no longer needs the user to say "remember", so this
gate is the whole defence. Which tools those are is Tool.ingress, declared at
each tool, and a test refuses a tool that never said.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace

from silica.tools import TOOLS

# An MCP connector (ADR-0091) brings a third party's text in, whatever the
# server is, and a stored conversation can name a server that is configured no
# longer: the prefix answers for it where TOOLS cannot.
_INGRESS_PREFIX = "mcp__"


def ingress(name: str) -> bool:
    return bool(getattr(TOOLS.get(name), "ingress", False)) or name.startswith(_INGRESS_PREFIX)


def _no_mail_check(paths: list[str]) -> str:
    raise OSError("no mail check was handed over")


# The first mail note of some paths, "" when none is. The import record sits in
# silica/sources, above this layer, so tools/user_memory.py hands the check over
# when it is imported. Until then a returned path reads as an unreadable record,
# which refuses memory: a gate that cannot look must not pass.
mail_check: Callable[[list[str]], str] = _no_mail_check


@dataclass
class Turn:
    request: str            # the current user request, the last user message
    directive: bool         # that message is a harness directive (origin cli)
    chat: bool              # an interactive chat turn, not a batch or worker loop
    tainted_by: str = ""    # the first ingress tool this conversation ran
    # Vault paths the running tool's result holds, handed over by the tool
    # itself (returned_notes) and checked by after_tool (ADR-0090 point 8).
    returned: list[str] = field(default_factory=list)
    # Every path the turn's tools returned, in order: a save names the last
    # few as where its entry came from (survey card C7).
    read: list[str] = field(default_factory=list)


# The key a tool message carries once its call tainted the conversation by a
# path, which no tool name in the history records. Stripped from the wire
# (wire.WIRE_KEYS) and kept by the narration, so a reopened chat stays
# tainted. Compaction rewrites a message's content, never its other keys.
TAINT_KEY = "silica_taint"


# Set by run_agent at entry, like read_scope.REQUESTS: tools run on the loop's
# thread and see it; a call from anywhere else (a batch, a test) sees
# None and is refused, because nothing shows that a user asked.
TURN: ContextVar[Turn | None] = ContextVar("user_memory_turn", default=None)


def _called(messages: list[dict]) -> list[str]:
    # Assistant tool calls, never tool results: compaction rewrites results
    # into stubs but leaves the calls alone, so the record survives a sweep.
    return [str((tc.get("function") or {}).get("name") or "")
            for m in messages if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or ()]


def begin_turn(messages: list[dict], *, chat: bool) -> Turn:
    request, directive = "", False
    for m in reversed(messages):
        if m.get("role") == "user":
            request, directive = str(m.get("content") or ""), m.get("origin") == "cli"
            break
    turn = Turn(request, directive, chat,
                 next((n for n in _called(messages) if ingress(n)), "")
                 or next((str(m[TAINT_KEY]) for m in messages
                          if m.get("role") == "tool" and m.get(TAINT_KEY)), ""))
    TURN.set(turn)
    return turn


def returned_notes(paths: Iterable[str]) -> None:
    """A note-reading tool hands the gate the vault paths its result holds,
    from its own result objects: recall strips frontmatter from the text the
    model sees, so the mail stamp could never be read off it (ADR-0090
    point 8). Outside a turn there is nothing to taint."""
    turn = TURN.get()
    if turn is not None:
        turn.returned.extend(str(p) for p in paths if p)


def own_returns() -> list[str]:
    """Give the calling context a turn of its own whose `returned` starts
    empty, and hand back that list. A read running beside others of its block
    (ADR-0150) collects its paths there, and the loop credits them to its own
    call before `after_tool`: on the shared list, the first call's taint mark
    would carry what its siblings returned."""
    turn = TURN.get()
    if turn is None:
        return []
    mine = replace(turn, returned=[])
    TURN.set(mine)
    return mine.returned


def after_tool(turn: Turn, name: str) -> str:
    """Taint the turn on what the call brought in; returns the taint this call
    added, "" when it added none, for the loop to mark the tool message with."""
    before = turn.tainted_by
    if ingress(name) and not turn.tainted_by:
        turn.tainted_by = name
    returned, turn.returned = turn.returned, []
    turn.read.extend(p for p in returned if p not in turn.read)
    if returned and not turn.tainted_by:
        try:
            hit = mail_check(returned)
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
    TURN.set(turn)
    return turn.tainted_by if turn.tainted_by != before else ""
