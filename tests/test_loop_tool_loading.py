"""A chat turn is offered the core; a deferred tool joins it when loaded (ADR-0050).

The offered block was 52 schemas, ~11k tokens of a ~13k floor on every
iteration, while GUI and TUI sessions used 22 tools. A deferred tool must
still be one step away, and joining must only ever append: the tool block is
the head of the prompt prefix a local server caches.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent.constraints import AgentConstraints
from silica.agent.llm import ToolCall
from silica.agent.loop import run_agent
from silica.tools import TOOLS, Tool


class _NoArgs(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    import silica.tools.loader  # noqa: F401  registers silica_load_tools
    for name in ("silica_recall", "silica_orphans", "silica_delete"):
        monkeypatch.setitem(TOOLS, name, Tool(lambda: '{"ok": true}', name, "stub", _NoArgs, "atomic"))


_C = AgentConstraints(tools=("silica_recall", "silica_load_tools"), deferred=("silica_orphans",))


def _call(name, args=None, cid="c1"):
    tc = ToolCall(id=cid, name=name, args=args or {})
    msg = {"role": "assistant", "tool_calls": [{"id": cid, "type": "function",
           "function": {"name": name, "arguments": json.dumps(args or {})}}]}
    return SimpleNamespace(assistant_message=msg, tool_calls=[tc], text=None, reasoning=None, usage={})


def _text(t):
    return SimpleNamespace(assistant_message={"role": "assistant", "content": t}, tool_calls=[],
                           text=t, reasoning=None, usage={})


def _run(*responses):
    offered, queue = [], list(responses)

    def fake(model, messages, **kw):
        offered.append([s["function"]["name"] for s in kw.get("tools") or []])
        return queue.pop(0)
    msgs = [{"role": "user", "content": "q"}]
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent(msgs, "test", constraints=_C)
    return out, offered, msgs


def test_a_loaded_tool_is_offered_from_the_next_step_at_the_end():
    _, offered, _ = _run(_call("silica_load_tools", {"names": ["silica_orphans"]}), _text("done"))
    assert offered[0] == ["silica_recall", "silica_load_tools"]
    assert offered[1] == ["silica_recall", "silica_load_tools", "silica_orphans"]


def test_a_direct_call_to_a_deferred_tool_runs_instead_of_being_refused():
    _, offered, msgs = _run(_call("silica_orphans"), _text("done"))
    result = next(m for m in msgs if m.get("role") == "tool")
    assert "forbidden" not in result["content"]
    assert offered[1][-1] == "silica_orphans"


def test_a_tool_outside_the_deferred_set_stays_forbidden():
    _, offered, msgs = _run(_call("silica_delete"), _text("done"))
    result = next(m for m in msgs if m.get("role") == "tool")
    assert "forbidden" in result["content"]
    assert "silica_delete" not in offered[1]


def test_chat_tools_keeps_what_an_earlier_turn_loaded():
    import silica.cli  # noqa: F401  registers the whole core
    from silica.agent.constraints import CHAT_CORE, chat_deferred, chat_tools

    history = [{"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {
        "name": "silica_load_tools", "arguments": json.dumps({"names": ["silica_orphans"]})}}]}]
    tools = chat_tools(history)
    assert tools[:len(CHAT_CORE)] == CHAT_CORE and tools[-1] == "silica_orphans"
    assert "silica_orphans" not in chat_deferred(history)


def test_a_ledger_task_loads_the_tool_it_names(monkeypatch):
    """prompt-rules probe, 2026-09-28: the ledger handed `silica_backlink`, a
    loadable tool, and on llama.cpp the grammar only admits offered names, so
    the model called `silica_backlinks` (a read) instead. The plan names the
    tools to run: the loop offers them."""
    task = {"task_id": "t1", "capability": "silica_orphans", "payload": {}}
    monkeypatch.setitem(TOOLS, "silica_ledger_next", Tool(
        lambda: json.dumps({"tasks": [task], "remaining": 0}), "silica_ledger_next", "stub", _NoArgs, "atomic"))
    c = AgentConstraints(tools=("silica_recall", "silica_ledger_next"), deferred=("silica_orphans",))
    offered, queue = [], [_call("silica_ledger_next"), _text("done")]

    def fake(model, messages, **kw):
        offered.append([s["function"]["name"] for s in kw.get("tools") or []])
        return queue.pop(0)
    with patch("silica.agent.loop.call_llm", fake):
        run_agent([{"role": "user", "content": "yes"}], "test", constraints=c)
    assert "silica_orphans" not in offered[0] and offered[1][-1] == "silica_orphans"


def test_loading_a_core_tool_says_it_is_already_offered():
    from silica.tools.loader import silica_load_tools
    out = silica_load_tools(["silica_rework", "silica_no_such_tool"])
    assert out["already_offered"] == ["silica_rework"]
    assert out["unknown"] == ["silica_no_such_tool"]
