# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The loop tells the model how much budget is left, and spends a final turn.

Exhaustion used to log a warning and return a fixed sentence, discarding
everything the turn had accomplished: a note committed on iteration 3 of 4 was
reported to the user as "maximum iterations reached", i.e. a completed write
read as a failure. And the model was never told a cap existed, so it could
spend its last step opening work it had no room to close.

Two changes, both cheap: a graduated notice near the wall, and one last call
with the tools removed rather than merely discouraged — a model that can still
see a tool schema asks for a step it no longer has.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent.constraints import AgentConstraints
from silica.agent.loop import BUDGET_NOTICE_AT, _budget_notice, run_agent
from silica.tools import TOOLS, Tool


class _EmptyArgs(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _tool():
    TOOLS["budget_tool"] = Tool(lambda: "ok", "budget_tool", "test tool",
                                _EmptyArgs, "atomic")
    yield
    TOOLS.pop("budget_tool", None)


def _resp(tool_calls=None, text="done"):
    return SimpleNamespace(
        assistant_message={"role": "assistant", "content": text},
        tool_calls=tool_calls or [], text=text, reasoning=None, usage={})


def _tc(call_id):
    return SimpleNamespace(name="budget_tool", args={}, id=call_id)


def _run(fake, max_iterations):
    with patch("silica.agent.loop.call_llm", fake):
        return run_agent(
            messages=[{"role": "user", "content": "hi"}], model="router",
            constraints=AgentConstraints(tools=("budget_tool",), model="worker",
                                         max_iterations=max_iterations))


class TestTheNotice:
    def test_it_is_silent_while_there_is_room(self):
        """A line on every iteration is noise the model learns to skip."""
        assert _budget_notice(iteration=1, max_iterations=20) is None

    def test_it_speaks_near_the_wall(self):
        notice = _budget_notice(iteration=20 - BUDGET_NOTICE_AT, max_iterations=20)
        assert notice is not None
        assert notice["role"] == "system"
        assert f"{BUDGET_NOTICE_AT} tool step(s) remain" in notice["content"]

    def test_it_counts_down(self):
        assert "1 tool step(s) remain" in _budget_notice(19, 20)["content"]
        assert "0 tool step(s) remain" in _budget_notice(20, 20)["content"]

    def test_the_model_sees_it_before_the_last_call(self):
        seen: list[list[dict]] = []

        def fake(model, messages, tools=None, cancel=None):
            seen.append([dict(m) for m in messages])
            return _resp(tool_calls=[_tc(f"c{len(seen)}")])

        _run(fake, max_iterations=3)

        last_call = seen[-1]
        assert any(m.get("role") == "system" and "Budget:" in str(m.get("content"))
                   for m in last_call)


class TestTheFinalTurn:
    def test_exhaustion_answers_instead_of_discarding_the_turn(self):
        calls = {"n": 0}

        def fake(model, messages, tools=None, cancel=None):
            calls["n"] += 1
            if tools is None:                      # the final turn
                return _resp(text="Wrote Life/Rex.md with four attributes.")
            return _resp(tool_calls=[_tc(f"c{calls['n']}")])

        result = _run(fake, max_iterations=2)

        assert result == "Wrote Life/Rex.md with four attributes."
        assert calls["n"] == 3                     # 2 loop passes + 1 final

    def test_the_tools_are_removed_not_merely_discouraged(self):
        """A model that can still see a schema asks for a step it cannot take."""
        seen_tools: list = []

        def fake(model, messages, tools=None, cancel=None):
            seen_tools.append(tools)
            if tools is None:
                return _resp(text="final")
            return _resp(tool_calls=[_tc(f"c{len(seen_tools)}")])

        _run(fake, max_iterations=2)

        assert seen_tools[-1] is None
        assert seen_tools[0] is not None

    def test_the_final_turn_is_told_the_phase_is_over(self):
        seen: list[list[dict]] = []

        def fake(model, messages, tools=None, cancel=None):
            seen.append([dict(m) for m in messages])
            if tools is None:
                return _resp(text="final")
            return _resp(tool_calls=[_tc(f"c{len(seen)}")])

        _run(fake, max_iterations=2)

        assert any("no tools are available now" in str(m.get("content"))
                   for m in seen[-1])

    def test_a_failing_final_turn_falls_back_to_the_old_sentence(self):
        """The recovery must not turn an exhausted turn into a crashed one."""
        def fake(model, messages, tools=None, cancel=None):
            if tools is None:
                raise RuntimeError("upstream down")
            return _resp(tool_calls=[_tc("c1")])

        assert _run(fake, max_iterations=1) == "(silica: maximum iterations reached)"

    def test_an_empty_final_answer_falls_back_too(self):
        """An empty string would render as a successful turn that said nothing."""
        def fake(model, messages, tools=None, cancel=None):
            if tools is None:
                return _resp(text="   ")
            return _resp(tool_calls=[_tc("c1")])

        assert _run(fake, max_iterations=1) == "(silica: maximum iterations reached)"

    def test_a_turn_that_ends_on_its_own_pays_for_no_extra_call(self):
        """The final turn is exhaustion recovery, not a step every turn takes."""
        calls = {"n": 0}

        def fake(model, messages, tools=None, cancel=None):
            calls["n"] += 1
            return _resp(text="answered directly")

        assert _run(fake, max_iterations=5) == "answered directly"
        assert calls["n"] == 1
