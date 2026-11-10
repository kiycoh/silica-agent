# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The GUI's one session (localhost, one user, no auth) and the slot its turns take.

`SESSION` is the single instance. What a chat remembers is a `Conversation`, and
the browser and the phone each hold one; the gate, the seed a new chat starts
from and the running server sit beside them, because both conversations share
those. Routers read this module and it reads none of them.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from silica.config import CONFIG
from silica.kernel import system_lines
from silica.session import meter
from silica.session.conversation import Conversation

logger = logging.getLogger(__name__)


@dataclass
class Session:
    chat: Conversation = field(default_factory=Conversation)
    # Turns from the operator's Telegram bot (silica-hosted) run in a conversation
    # of their own (ADR-0089 point 5, amendment 2026-10-14): their own list,
    # narration session and tool block, so a phone turn never re-prefills the
    # browser's chat with a different block. The gate stays shared, one model and
    # one turn at a time, which also makes the release drain (GET /jobs/running)
    # wait for a phone turn as for any other.
    phone: Conversation = field(default_factory=Conversation)
    phone_running: bool = False  # a phone turn holds the gate; read by reset()
    busy: bool = False  # one turn at a time; a second /chat is refused with 409
    cancel: threading.Event | None = None  # cancel token of the in-flight turn
    task: asyncio.Task | None = None  # in-flight worker; owns the busy-gate release
    # Fresh-session seed, precomputed so /reset ("new chat") is instant instead of
    # rebuilding the vault map + token count on the click path (~seconds on a real
    # vault). Built at startup, refreshed in the background after each turn (the
    # turn may have written notes). (messages, their token count).
    seed: tuple[list[dict], int] | None = None
    # The vault the cached seed was built from. A prewarm started before a vault
    # switch can end after it and leave the old vault's map for the next chat; in
    # the suite one test inherited another test's seed, whose numbers then read as
    # evidence the turn had seen (test_unread_numbers_chat, 2026-10-31).
    seed_vault: str | None = None
    # The GUI's own uvicorn server, so an endless response can ask whether the
    # process is leaving. None under TestClient and in the suite, where nothing is
    # shutting down and every stream is read to its end by its caller.
    server: Any = None

    def claim(self) -> bool:
        """Claim the single-turn slot. Sync with no `await` between the test and the
        set, so two racing POSTs can't both pass. Returns False if one's in flight."""
        if self.busy:
            return False
        self.busy = True
        return True

    def release(self) -> None:
        """Release the turn slot. Idempotent (a completed turn and its worker's
        done-callback may both call it)."""
        self.busy = False
        self.cancel = None
        self.task = None

    def sweep_if_orphaned(self) -> None:
        """Free a gate claimed for a turn whose `run_turn` never ran: the client
        dropped between POST and the SSE body's first `__anext__`, so nothing else
        releases it. Runs after the response closes; a no-op once a worker exists."""
        if self.busy and (self.task is None or self.task.done()):
            self.release()

    def reset(self) -> None:
        from silica.kernel import user_memory
        from silica.obs import narration as _narr

        # A phone turn (ADR-0089 amendment) holds the gate and the narrator while
        # the browser cannot see it, so its New chat must not free either: the
        # browser's chat starts over here, and the phone turn hands the narrator
        # to whatever chat is current when it ends (_narrating_phone).
        phone = self.phone_running
        if not phone:
            _narr.NARRATOR.close()   # release the flock; next turn opens a fresh sid
        # Incognito is per chat in the GUI (ADR-0076 §8): a new chat is back on
        # the record, so the seed below gets its memory message again.
        user_memory.set_incognito(False)
        # In place: an abandoned turn's worker still holds this list.
        self.chat.messages[:], CONFIG.context_tokens = seeded()
        self.chat = Conversation(messages=self.chat.messages)
        if not phone:
            self.release()

    def reset_phone(self) -> None:
        self.phone = Conversation()


SESSION = Session()


class TurnSlot:
    """The slot `turn_slot` holds. `hand_off` is for the one case the block does
    not end the holding: a turn's stream, whose own `finally` frees it."""

    handed_off = False

    def hand_off(self) -> None:
        self.handed_off = True


@contextmanager
def turn_slot(busy: str = "a turn is already in progress") -> Iterator[TurnSlot]:
    """Hold the one slot for the block, or refuse with a 409 saying `busy`.

    It is also the vault's write lock: the one serialisation of vault writes
    the server has, which is why an import or a tree move takes it too. The
    block's end frees it however the block ends, a cancelled request included.
    A block that returns a turn's stream hands it off (`sse.turn_response`
    does), and the release is then the turn's: `run_turn`'s finally, its
    worker's done-callback, or `sweep_if_orphaned` when the body never ran."""
    if not SESSION.claim():
        # Here and not at the top: the bridge reads this module without the [gui] extra.
        from fastapi import HTTPException

        raise HTTPException(409, busy)
    slot = TurnSlot()
    try:
        yield slot
    finally:
        if not slot.handed_off:
            SESSION.release()


def build_seed() -> tuple[list[dict], int]:
    """Compute the fresh-session seed. Never touches the live session state:
    uses the pure token counter so a background rebuild can't clobber the
    context meter of the conversation in progress."""
    from silica.session import seed
    from silica.ui.web.app_help import app_message

    vault = CONFIG.vault_path  # before the build: a switch during it must not label the old map as the new vault's

    # The same builder the TUI seeds from, math=True for the MathML renderer,
    # app=True for the help panel the chat answers from (ADR-0115).
    msgs = seed.seed_messages(math=True, app=app_message())
    # Tools included for the same reason `update_context_tokens` includes them:
    # counted without the block, a fresh window read 2.1k and jumped to 9.3k on
    # the first turn, so the meter's opening number disagreed with its next one.
    SESSION.seed = (msgs, meter.count_context_tokens(msgs, meter.chat_tool_schemas(msgs)))
    # After the seed: a reader between the two sees a vault that does not match and builds again.
    SESSION.seed_vault = vault
    return SESSION.seed


def prewarm_seed() -> None:
    """Refresh the seed off the request path; failures only cost freshness."""

    def work():
        try:
            build_seed()
        except Exception:
            logger.exception("seed prewarm failed")

    threading.Thread(target=work, daemon=True).start()


def seeded() -> tuple[list[dict], int]:
    """A copy of the fresh-session seed, with user memory as it is NOW.

    The seed is rebuilt only when a turn ends, and memory also changes outside
    a turn (Settings, Undo, /incognito), so the two small files are re-read
    here; the count is redone only when that changed the list.
    """
    from silica.kernel import user_memory

    cached, built_for = SESSION.seed, SESSION.seed_vault  # read once: /instructions can clear the seed meanwhile
    seed_msgs, seed_tokens = cached if cached is not None and built_for == CONFIG.vault_path else build_seed()
    msgs = [dict(m) for m in seed_msgs]  # per-message copy; the seed's own dicts stay untouched
    if user_memory.refresh_snapshot(msgs):
        seed_tokens = meter.count_context_tokens(msgs, meter.chat_tool_schemas(msgs))
    return msgs, seed_tokens


WS_ORIGIN = "workspace"


def apply_workspace(ws: dict | None) -> None:
    """The system line of a chat opened in a workspace (ADR-0112 point 4,
    Open 3): what the scope does, then the workspace note's body as the
    user's standing instructions. Written once and edited in place, never
    removed: compaction counts messages by index (`collapsed`), so a removal
    or a late insert would shift every one it recorded."""
    from silica.kernel import workspaces

    messages = SESSION.chat.messages
    i = system_lines.index_of(messages, WS_ORIGIN)
    if ws is None:
        if i is not None:
            messages[i]["content"] = ("This conversation is no longer limited to a workspace: searches "
                                      "cover the whole vault and no workspace instructions apply.")
        return
    text = workspaces.instructions_message(ws, len(workspaces.members(ws)))
    if i is not None:
        messages[i]["content"] = text
        return
    if SESSION.chat.collapsed:
        # ponytail: a chat compacted before it was first opened in a workspace
        # gets the scope without the line; the UI opens a workspace chat only
        # as a new chat, so this needs an API caller. Rebase `collapsed` if
        # that ever shows up in the pilot's logs.
        logger.info("workspace line skipped: the chat is already compacted")
        return
    system_lines.insert(messages, {"role": "system", "content": text, "origin": WS_ORIGIN})


def resolve_workspace(ref: str) -> dict | None:
    """The workspace a turn names, or None; sets this chat's current one."""
    from silica.kernel import workspaces

    ws = workspaces.find_workspace(ref) if ref else None
    SESSION.chat.workspace = ws["path"] if ws else None
    return ws


def ctx_meter() -> dict:
    """What the composer's context ring reads, in one shape.

    The parts are counted, not apportioned: `context_breakdown` charges the
    chat envelope once so they sum to the total beside them, and a meter that
    invites you to add up its own segments has to survive that. Three sites emit
    this (the two `done` yields and the transcript headers) and they must not
    drift, since the ring is the one place the user is told how much room is
    left before the history starts being collapsed.
    """
    from silica.agent.compaction import COMPACT_FRACTION

    return {"context_tokens": CONFIG.context_tokens,
            "max_context_tokens": CONFIG.max_context_tokens,
            "context_parts": meter.context_breakdown(SESSION.chat.messages),
            # The one fill level that means anything: past it the loop starts
            # collapsing old read results. Sent rather than mirrored in the
            # client, so the ring cannot go on reassuring at 0.6 after someone
            # moves the threshold in compaction.py.
            "compact_at": COMPACT_FRACTION}


def capture_own_session() -> None:
    """Flush the live conversation to the capture WAL, if capture is on.

    The server owns the session, so the two moments it can see a conversation
    end are a new chat and its own shutdown. A closed tab is neither; the next
    one of these catches its content (accepted ceiling, spec §10).
    `capture_session` is opt-in and fail-open in itself — a capture bug can
    never break the GUI from here. An incognito chat is never flushed.
    """
    from silica.sources.capture import capture_session
    from silica.kernel import user_memory

    if user_memory.incognito():
        return
    capture_session(SESSION.chat.messages, session_id=SESSION.chat.sid or uuid.uuid4().hex[:12],
                    driver="gui")


def open_session() -> str | None:
    """The live chat's session, opened now when its first turn has not come
    yet; idempotent, as the turn's own `ensure_session` is."""
    from silica.obs import narration as _narr
    try:
        SESSION.chat.sid = _narr.NARRATOR.ensure_session(driver="gui", sid=SESSION.chat.sid)
    except _narr.SessionBusy as e:   # another process owns it: the turn will say so
        logger.warning("narration unavailable for this drop: %s", e)
    return SESSION.chat.sid


def narrate_user_turn(msg: dict) -> None:
    """Session born at the first user turn (spec §5), then the turn beat.
    Runs on the worker thread; ensure_session is idempotent per turn."""
    from silica.obs import narration as _narr
    try:
        SESSION.chat.sid = _narr.NARRATOR.ensure_session(
            driver="gui", sid=SESSION.chat.sid)
        _narr.NARRATOR.turn(msg)
    except _narr.SessionBusy as e:   # another process owns it: keep chatting unsaved
        logger.warning("narration unavailable for this turn: %s", e)


def list_sessions() -> list[dict]:
    """Saved conversations for the current vault, newest first, both stores.
    _save_session is gone with the snapshot model: appending turn beats IS
    the save (spec §5), so a refresh/close can no longer lose history."""
    from silica.obs import narration as _narr
    return _narr.list_sessions(CONFIG.vault_path or "")


def stopping() -> bool:
    return SESSION.server is not None and bool(SESSION.server.should_exit)
