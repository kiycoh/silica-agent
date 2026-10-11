# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""run_agent opens ADR-0093's chat turn: a create at a path the chat deleted
since the turn began is judged against the deleted body. A batch sub-loop
opens none, so it cannot move the start of the chat turn it runs inside."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from silica.agent.loop import run_agent
from silica.kernel.write import preserve


def _answer(*a, **k):
    return SimpleNamespace(assistant_message={"role": "assistant", "content": "ok"},
                           tool_calls=[], text="ok", reasoning=None, usage={})


def test_a_chat_turn_opens_the_window(monkeypatch):
    monkeypatch.setattr(preserve, "_TURN_STARTED", preserve.ContextVar("t", default=None))
    with patch("silica.agent.loop.call_llm", _answer):
        run_agent(messages=[{"role": "user", "content": "hi"}], model="test")
    assert preserve._TURN_STARTED.get() is not None


def test_a_batch_loop_opens_none(monkeypatch):
    from silica.agent.constraints import AgentConstraints

    monkeypatch.setattr(preserve, "_TURN_STARTED", preserve.ContextVar("t", default=None))
    with patch("silica.agent.loop.call_llm", _answer):
        run_agent(messages=[{"role": "user", "content": "hi"}], model="test",
                  constraints=AgentConstraints(tools=(), interactive=False))
    assert preserve._TURN_STARTED.get() is None
