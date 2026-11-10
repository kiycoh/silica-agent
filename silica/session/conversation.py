# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What one chat remembers, on whichever surface holds it, and how a chat starts
and reopens: once here, since the REPL and the browser each wrote both and the
copies had come to differ."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Conversation:
    """What one chat remembers. A field added here starts clean with each new
    chat by being here: `fresh` builds a new one and names none of them."""

    messages: list[dict] = field(default_factory=list)
    collapsed: set[int] = field(default_factory=set)  # message indices elided by compaction, across turns
    # Number tokens of every result this session read, raw (ADR-0095): compaction
    # and a relevance verdict stub the tool messages, and must not decide what grounds.
    seen_numbers: set[str] = field(default_factory=set)
    # The narration sid. The store moved to ~/.silica/narration (docs/specs/narration/spec.md §5);
    # legacy web_sessions/*.json are read forever through silica.obs.narration.
    sid: str | None = None
    workspace: str | None = None  # vault path of the workspace this chat is opened in (ADR-0112)


def fresh(chat: Conversation, seed: list[dict], sid: str | None = None) -> Conversation:
    """A new chat in place of `chat`, holding `seed`. The message list is the
    same list, refilled: an abandoned turn's worker still holds it."""
    chat.messages[:] = seed
    return Conversation(messages=chat.messages, sid=sid)


def reopen(chat: Conversation, sid: str, *, driver: str, seed: Callable[[], list[dict]],
           scope: Callable[[dict], None] | None = None) -> list[dict] | None:
    """The saved conversation `sid`, reopened into `chat`; its replayed messages,
    None when no store holds it. Raises `SessionBusy` when another process
    narrates it.

    A narration session this vault lists goes on in place; a legacy snapshot,
    or one another vault or incognito holds, goes on as a new session seeded
    with its turns (emit new, recognise legacy: ticket 05). The narration
    records turns and never the seed, so `seed()` goes in front; `scope` writes
    what the surface keeps beside it from the last user message (the browser's
    workspace line) before the persona's, since both insert before the seed's
    last system line. Then the persona that message wore (ADR-0127 point 3),
    the numbers the session read (ADR-0095) and the meter."""
    from silica.agent.recall_watch import numbers_of_session
    from silica.config import CONFIG
    from silica.kernel import personas
    from silica.obs import narration
    from silica.session import meter

    vault = CONFIG.vault_path or ""
    replayed = narration.load_session_messages(sid, vault)
    if replayed is None:
        return None
    if narration.listed(sid, vault):
        narration.NARRATOR.resume(sid)  # appending continues the same account
        chat.sid = sid
    else:
        narration.NARRATOR.close()
        chat.sid = narration.NARRATOR.ensure_session(driver=driver)
        for m in replayed:
            narration.NARRATOR.turn(m)
    chat.messages[:] = seed() + replayed
    last: dict = next((m for m in reversed(replayed) if m.get("role") == "user"), {})
    if scope is not None:
        scope(last)
    # Read from the file as it is now; one removed since leaves the chat
    # without it rather than failing to open.
    if (worn := personas.available(vault or None).get(str(last.get(personas.TURN_KEY) or ""))):
        personas.apply(chat.messages, worn)
    chat.seen_numbers.clear()
    chat.seen_numbers.update(numbers_of_session(chat.sid))
    meter.update_context_tokens(chat.messages)
    return replayed
