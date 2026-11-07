# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Ctrl+C in the TUI (ADR-0004): during a tool the first press sets the turn's
cancel token and lets the tool drain; elsewhere, and on the second press, it
raises as it always did."""
import signal
from unittest.mock import MagicMock

import pytest

from silica import cli
from silica.obs.events import ToolCompleteEvent, ToolErrorEvent, ToolStartEvent


def _start(name="silica_dedup"):
    return ToolStartEvent(name=name, args={}, call_id="c1", iteration=1)


def _done(name="silica_dedup"):
    return ToolCompleteEvent(name=name, args={}, call_id="c1", result="{}", duration_s=0.1, iteration=1)


def test_outside_a_tool_the_first_press_raises_and_sets_the_token():
    turn = cli._TurnInterrupt(lambda e: None)
    with pytest.raises(KeyboardInterrupt):
        turn.on_sigint(2, None)
    assert turn.token.is_set()


def test_during_a_tool_the_first_press_only_sets_the_token():
    seen = []
    turn = cli._TurnInterrupt(seen.append)
    turn(_start())
    turn.on_sigint(2, None)  # no raise: the tool's pools poll the token and drain
    assert turn.token.is_set()
    assert seen == [_start()]  # events still reach the renderer untouched


def test_the_second_press_raises_even_during_a_tool():
    turn = cli._TurnInterrupt(lambda e: None)
    turn(_start())
    turn.on_sigint(2, None)
    with pytest.raises(KeyboardInterrupt):
        turn.on_sigint(2, None)


def test_a_finished_or_failed_tool_is_no_longer_running():
    turn = cli._TurnInterrupt(lambda e: None)
    turn(_start())
    turn(_done())
    with pytest.raises(KeyboardInterrupt):
        turn.on_sigint(2, None)
    turn = cli._TurnInterrupt(lambda e: None)
    turn(_start())
    turn(ToolErrorEvent(name="silica_dedup", call_id="c1", error="x", iteration=1))
    with pytest.raises(KeyboardInterrupt):
        turn.on_sigint(2, None)


def test_a_nested_tool_keeps_the_outer_one_running():
    # A recipe tool runs a loop of its own, whose events reach the same callback.
    turn = cli._TurnInterrupt(lambda e: None)
    turn(_start("silica_inject"))
    turn(_start("silica_read"))
    turn(_done("silica_read"))
    turn.on_sigint(2, None)
    assert turn.token.is_set()


def test_the_repl_hands_its_token_to_run_agent(monkeypatch):
    """The token a Ctrl+C sets is the one the model run polls, while the turn
    runs: the handler is in place around the call and gone after it."""
    from silica.session import turn as session_turn
    from silica.session.conversation import Conversation

    before = signal.getsignal(signal.SIGINT)
    seen = {}

    def run_agent(messages, model, tool_progress_callback=None, cancel_token=None, constraints=None):
        seen["token"], seen["handler"] = cancel_token, signal.getsignal(signal.SIGINT)
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr("silica.session.turn.run_agent", run_agent)
    cli._agent_turn(session_turn.Turn(Conversation(messages=[{"role": "user", "content": "hi"}]), "hi"),
                    MagicMock())
    assert seen["handler"].__self__.token is seen["token"]
    assert signal.getsignal(signal.SIGINT) is before
