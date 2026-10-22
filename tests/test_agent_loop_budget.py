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


def test_turn_prompt_budget_lands_the_turn_instead_of_spiralling(monkeypatch):
    """Twelve of 26 live runs on 2026-09-22 spent 300k+ prompt tokens re-reading
    one note and were killed by the probe's cap; the product had only the
    iteration cap. Past TURN_PROMPT_BUDGET the loop lands with the tool-less
    final turn, the same landing as the iteration cap, so the user gets an
    answer built from what was already read."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from silica.agent import loop as loop_mod

    class _Tool:
        sensitive = False; internal = False; summarize = None; collapse = "lazy"; name = "fake_read"
        def json_schema(self):
            return {"type": "function", "function": {"name": "fake_read", "description": "", "parameters": {"type": "object", "properties": {}}}}
        def run(self, _cancel_token=None, **kw):
            return "x" * 50

    calls = []

    def fake_call_llm(model, messages, **kw):
        calls.append(kw.get("tools"))
        if kw.get("tools") is None:  # the landing call
            return SimpleNamespace(assistant_message={"role": "assistant", "content": "done"},
                                   tool_calls=[], text="done", reasoning=None, usage={"prompt_tokens": 1})
        cid = f"c{len(calls)}"
        return SimpleNamespace(
            assistant_message={"role": "assistant", "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": "fake_read", "arguments": "{}"}}]},
            tool_calls=[SimpleNamespace(id=cid, name="fake_read", args={})],
            text="", reasoning=None, usage={"prompt_tokens": 250_000})

    monkeypatch.setattr(loop_mod, "TURN_PROMPT_BUDGET", 400_000)
    with patch.dict("silica.tools.TOOLS", {"fake_read": _Tool()}, clear=True), \
         patch("silica.agent.loop.call_llm", fake_call_llm):
        out = run_agent(messages=[{"role": "user", "content": "q"}], model="m")
    assert out == "done"
    assert calls[-1] is None and len(calls) == 3  # 250k, 500k > budget, then the landing


# --- 2026-10-22, hosted t06: the token budget and a reasoning-only landing ---

class TestTheTokenBudget:
    def test_the_notice_names_the_nearer_wall(self):
        assert _budget_notice(1, 48) is None
        assert "1 tool step(s) remain" in _budget_notice(1, 48, token_steps=1)["content"]
        assert "2 tool step(s) remain" in _budget_notice(46, 48, token_steps=30)["content"]

    def test_the_model_is_told_before_the_token_budget_stops_it(self):
        from silica.agent.loop import TURN_PROMPT_BUDGET

        seen: list[list[dict]] = []

        def fake(model, messages, tools=None, cancel=None):
            seen.append([dict(m) for m in messages])
            r = _resp(tool_calls=[_tc(f"c{len(seen)}")]) if tools is not None else _resp(text="final")
            r.usage = {"prompt_tokens": TURN_PROMPT_BUDGET // 4}
            return r

        _run(fake, max_iterations=48)
        # Four calls of a quarter each cross the budget; the third is told first.
        told = [i for i, call in enumerate(seen)
                if any("Budget:" in str(m.get("content")) for m in call if m.get("role") == "system")]
        assert told and told[0] < len(seen) - 1


class TestAReasoningOnlyLanding:
    def test_it_is_asked_once_more_with_reasoning_off(self):
        asked: list = []

        def fake(model, messages, tools=None, cancel=None, reasoning=None):
            if tools is not None:
                return _resp(tool_calls=[_tc("c1")])
            asked.append(reasoning)
            if reasoning is False:
                return _resp(text="Wrote Inbox/R.md.")
            r = _resp(text="")
            r.reasoning = "Ho completato il lavoro: Inbox/R.md."
            return r

        assert _run(fake, max_iterations=1) == "Wrote Inbox/R.md."
        assert asked == [None, False]

    def test_an_empty_landing_with_no_reasoning_is_not_retried(self):
        asked: list = []

        def fake(model, messages, tools=None, cancel=None, reasoning=None):
            if tools is not None:
                return _resp(tool_calls=[_tc("c1")])
            asked.append(reasoning)
            return _resp(text="")

        assert _run(fake, max_iterations=1) == "(silica: maximum iterations reached)"
        assert asked == [None]
