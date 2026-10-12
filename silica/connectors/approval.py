# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The approval rule for calls that leave the vault (ADR-0091 point 6).

Every MCP `tools/call` is asked, on every call: its arguments leave the
machine. What is asked is the server, the tool and the full arguments. There
is no "always allow", and annotations decide nothing: the schema calls them
hints from an untrusted server, and an honest `destructiveHint: false`
describes sending a mail.

One card waits at most 120 seconds and one turn at most 180 for all its cards
together; after that every further call of the turn is refused without
asking, so a turn holds the GUI's turn gate for at most three minutes of
waiting, not the sixteen that eight steps of 120 s would allow. No answer, a
cancelled turn, or a surface that cannot ask means no.

The card travels as an ApprovalRequestEvent through the frontend callback the
loop already hands every tool. The GUI's worker thread waits here while the
card crosses the SSE bridge and POST /connectors/mcp/approve answers it; the
TUI renderer answers in place, inside the callback, before this waits at all.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
import weakref
from typing import Any, Callable

from silica.agent.events import ApprovalRequestEvent

logger = logging.getLogger(__name__)

CARD_TIMEOUT_S = 120.0
TURN_BUDGET_S = 180.0

_PENDING: dict[str, ApprovalRequestEvent] = {}
_LOCK = threading.Lock()


class Budget:
    """The seconds of waiting a turn has left."""

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds


# Keyed by the turn's own frontend callback: GUI and TUI build a new one per
# turn (the RecallWatch or WebTurn wrapping the renderer), so it names the turn
# without a turn id the loop does not have. Weak, so a finished turn's budget
# goes with it.
_BUDGETS: "weakref.WeakKeyDictionary[Any, Budget]" = weakref.WeakKeyDictionary()


def budget_for(progress: Callable | None) -> Budget:
    if progress is None:
        return Budget(0.0)
    try:
        budget = _BUDGETS.get(progress)
        if budget is None:
            budget = _BUDGETS[progress] = Budget(TURN_BUDGET_S)
        return budget
    except TypeError:
        # An object that takes no weak reference: the budget then spans this
        # one connector call, still bounded, never unbounded.
        return Budget(TURN_BUDGET_S)


def ask(*, server: str, tool: str, args: dict[str, Any], progress: Callable | None,
        cancel: threading.Event | None, budget: Budget) -> str:
    """Ask before one call. Returns "run", "deny", "no answer", "no surface"
    or "out of time"; only "run" lets the call go."""
    if progress is None:
        verdict = "no surface"
    elif budget.remaining <= 0:
        verdict = "out of time"
    elif cancel is not None and cancel.is_set():
        verdict = "no answer"
    else:
        event = ApprovalRequestEvent(id=uuid.uuid4().hex[:12], server=server, tool=tool,
                                     args=dict(args), timeout_s=min(CARD_TIMEOUT_S, budget.remaining))
        with _LOCK:
            _PENDING[event.id] = event
        t0 = time.monotonic()
        try:
            progress(event)
            while not event.decided.wait(0.1):
                if cancel is not None and cancel.is_set():
                    break
                if time.monotonic() - t0 >= event.timeout_s:
                    break
        except Exception as exc:
            # A surface that failed to draw the card could not have been
            # answered; that is a no, and the turn goes on without the call.
            logger.warning("approval card for %s/%s not shown: %s", server, tool, exc)
        finally:
            with _LOCK:
                _PENDING.pop(event.id, None)
            budget.remaining -= time.monotonic() - t0
        verdict = ("run" if event.approved is True and event.decided.is_set()
                   else "deny" if event.approved is False else "no answer")
    _narrate(server, tool, verdict)
    return verdict


def answer(card_id: str, run: bool) -> bool:
    """The surface's answer to one pending card; False when it is gone (it
    timed out, or its turn ended)."""
    with _LOCK:
        event = _PENDING.get(card_id)
    if event is None:
        return False
    event.approved = bool(run)
    event.decided.set()
    return True


def pending() -> list[ApprovalRequestEvent]:
    with _LOCK:
        return list(_PENDING.values())


def _narrate(server: str, tool: str, verdict: str) -> None:
    """One beat per card, for Gate 5 (approvals per use, read off the
    narration)."""
    from silica.agent.narration import NARRATOR

    NARRATOR.narrate("approval", "done", f"{server} › {tool}: {verdict}",
                     {"server": server, "tool": tool, "verdict": verdict})
