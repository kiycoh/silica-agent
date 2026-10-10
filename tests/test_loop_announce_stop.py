"""A first step that announces a tool and stops gets one nudge to make the call.

Measured 2026-09-27 on custom/qwen3.5-9b (GUI session 79d52abf8395): reasoning
"devo usare silica_recall", text "Cerco di capire come il vault affronta...",
no tool call, finish=stop. The loop returned the preamble as the answer. The same
request replayed called a tool 110/110, so the model is not broken: the loop took
a rare stop for a final answer.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent.constraints import AgentConstraints
from silica.agent.llm import ToolCall
from silica.agent.loop import run_agent
from silica.tools import TOOLS, Tool

_TOOLS = AgentConstraints(tools=("silica_recall",))


class _NoArgs(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _recall_offered(monkeypatch):
    # The registry is filled by importing the tool modules; a stub keeps this
    # test off the vault and the embedder.
    monkeypatch.setitem(TOOLS, "silica_recall",
                        Tool(lambda: "{}", "silica_recall", "stub", _NoArgs, "atomic"))


def _resp(text=None, reasoning=None, tool_calls=()):
    msg = {"role": "assistant"}
    if text:
        msg["content"] = text
    if tool_calls:
        msg["tool_calls"] = [{"id": tc.id, "type": "function",
                              "function": {"name": tc.name, "arguments": "{}"}}
                             for tc in tool_calls]
    return SimpleNamespace(assistant_message=msg, tool_calls=list(tool_calls),
                           text=text, reasoning=reasoning, usage={})


def _scripted(*responses):
    seen: list[list[dict]] = []
    queue = list(responses)

    def fake(model, messages, **kw):
        seen.append([dict(m) for m in messages])
        return queue.pop(0)
    return fake, seen


_ANNOUNCE = _resp(text="Cerco di capire come il vault affronta il tema.",
                  reasoning="devo usare silica_recall per recuperare il contesto")


def test_announced_tool_without_call_gets_one_nudge():
    fake, seen = _scripted(_ANNOUNCE, _resp(text="La risposta vera."))
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent([{"role": "user", "content": "ML?"}], "test", constraints=_TOOLS)
    assert out == "La risposta vera."
    assert len(seen) == 2
    nudge = seen[1][-1]
    assert nudge["role"] == "system" and "silica_recall" in nudge["content"]


def test_nudge_fires_at_most_once():
    fake, seen = _scripted(_ANNOUNCE, _ANNOUNCE)
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent([{"role": "user", "content": "ML?"}], "test", constraints=_TOOLS)
    assert len(seen) == 2
    assert out.startswith("Cerco di capire")


def test_answer_whose_reasoning_names_no_tool_is_final():
    fake, seen = _scripted(_resp(text="Ciao!", reasoning="the user greets me"))
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent([{"role": "user", "content": "ciao"}], "test", constraints=_TOOLS)
    assert out == "Ciao!"
    assert len(seen) == 1


def test_no_nudge_once_a_tool_has_run():
    # After a tool result the model routinely names the tool it used while
    # answering; only the step before any tool can be an unkept announcement.
    call = ToolCall(id="c1", name="no_such_tool", args={})
    fake, seen = _scripted(
        _resp(tool_calls=[call]),
        _resp(text="Trovato.", reasoning="silica_recall returned the notes"),
    )
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent([{"role": "user", "content": "ML?"}], "test", constraints=_TOOLS)
    assert out == "Trovato."
    assert len(seen) == 2


def test_no_nudge_on_the_last_step():
    # The landing turn after the cap has no tools, so a nudge to call one there
    # would ask for the impossible.
    fake, seen = _scripted(_ANNOUNCE, _resp(text="unused"))
    one_step = AgentConstraints(tools=("silica_recall",), max_iterations=1)
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent([{"role": "user", "content": "ML?"}], "test", constraints=one_step)
    assert len(seen) == 1
    assert out.startswith("Cerco di capire")
