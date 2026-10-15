"""A step without a tool call is final, whatever its reasoning names.

The loop used to nudge a first step whose reasoning named an offered tool. It
retracted a finished /quiz on hosted t01 (2026-10-14, qwen3.7-flash): the prompt
names silica_record_quiz for the turn after the reader answers, and the
reasoning restated that plan. Removed 2026-10-15 (loop.py, at the no-tool-calls
return).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent.constraints import AgentConstraints
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


def _resp(text=None, reasoning=None):
    msg = {"role": "assistant"}
    if text:
        msg["content"] = text
    return SimpleNamespace(assistant_message=msg, tool_calls=[],
                           text=text, reasoning=reasoning, usage={})


def _scripted(*responses):
    seen: list[list[dict]] = []
    queue = list(responses)

    def fake(model, messages, **kw):
        seen.append([dict(m) for m in messages])
        return queue.pop(0)
    return fake, seen


@pytest.mark.parametrize("text, reasoning", [
    # The 2026-10-14 misfire: a full answer whose reasoning plans a later call.
    ("Ecco le tue 3 domande:\n\n1. Recall?\n2. Comprehension?\n3. Application?",
     "When the reader replies, grade each answer, call silica_recall."),
    # The 2026-09-27 shape the nudge was for: now accepted as the answer.
    ("Cerco di capire come il vault affronta il tema.",
     "devo usare silica_recall per recuperare il contesto"),
])
def test_a_step_without_a_tool_call_is_final(text, reasoning):
    fake, seen = _scripted(_resp(text=text, reasoning=reasoning), _resp(text="unused"))
    with patch("silica.agent.loop.call_llm", fake):
        out = run_agent([{"role": "user", "content": "q"}], "test", constraints=_TOOLS)
    assert out == text
    assert len(seen) == 1
