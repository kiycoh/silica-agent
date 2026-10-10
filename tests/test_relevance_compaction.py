"""Turn-start compaction by relevance: a typed decision per earlier tool result
says whether the current request still needs it; a confident no becomes a
stub that names the call to re-issue, and every non-answer keeps the body."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from silica.agent import relevance
from silica.agent.loop import run_agent

BIG = "lezione " * 400  # 3.2k chars, over MIN_CHARS
TOOLS = {"silica_read_note": SimpleNamespace(collapse="lazy"),
         "silica_patch_note": SimpleNamespace(collapse="eager")}


def _call(cid: str, name: str, args: dict) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _history(current: str = "E la Lezione 30?") -> list[dict]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Riassumi la Lezione 32"},
        _call("a", "silica_read_note", {"name": "Lezione 32"}),
        {"role": "tool", "tool_call_id": "a", "content": BIG},
        {"role": "assistant", "content": "La Lezione 32 tratta knn e alberi."},
        {"role": "user", "content": current},
    ]


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(relevance, "ENABLED", True)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev-1.13")


def test_no_decision_model_asks_nothing(monkeypatch):
    monkeypatch.setattr(relevance, "ENABLED", True)  # conftest pins the model to ""
    msgs = _history()
    with patch.object(relevance, "decide") as d:
        assert relevance.compact_by_relevance(msgs, TOOLS) == []
    d.assert_not_called()


def _answers(p):
    return None if p is None else {"needed": {"type": "noul", "noul": p}}


def test_disabled_asks_nothing(monkeypatch):
    monkeypatch.setattr(relevance, "ENABLED", False)
    msgs = _history()
    with patch.object(relevance, "decide") as d:
        assert relevance.compact_by_relevance(msgs, TOOLS) == []
    d.assert_not_called()
    assert msgs[3]["content"] == BIG


@pytest.mark.parametrize("p,kept", [(0.05, False), (0.9, True), (None, True),
                                    (relevance.DROP_BELOW, True)])
def test_only_a_confident_no_is_stubbed(on, p, kept):
    msgs = _history()
    with patch.object(relevance, "decide", return_value=_answers(p)):
        dropped = relevance.compact_by_relevance(msgs, TOOLS)
    assert (msgs[3]["content"] == BIG) is kept
    assert dropped == ([] if kept else [3])
    if not kept:
        assert "silica_read_note" in msgs[3]["content"] and "Lezione 32" in msgs[3]["content"]
        assert "not needed for the current request" in msgs[3]["content"]


def test_state_carries_the_line_of_requests_and_the_previous_answer(on):
    msgs = _history()
    with patch.object(relevance, "decide", return_value=_answers(0.9)) as d:
        relevance.compact_by_relevance(msgs, TOOLS)
    state, question = d.call_args.args
    assert state["current_request"] == "E la Lezione 30?"
    assert state["earlier_requests"] == ["Riassumi la Lezione 32"]
    assert state["previous_answer"].startswith("La Lezione 32")
    assert state["tool_result"]["tool"] == "silica_read_note"
    assert "Lezione 32" in state["tool_result"]["arguments"]
    assert len(state["tool_result"]["head"]) <= relevance._RESULT_HEAD
    assert question["needed"]["type"] == "noul"


def test_skips_small_eager_stubbed_and_current_turn_results(on):
    msgs = _history()
    msgs[3:3] = []
    msgs.insert(4, _call("b", "silica_patch_note", {"name": "X"}))
    msgs.insert(5, {"role": "tool", "tool_call_id": "b", "content": BIG})       # eager tool
    msgs.insert(6, _call("c", "silica_read_note", {"name": "Small"}))
    msgs.insert(7, {"role": "tool", "tool_call_id": "c", "content": "short"})   # small
    msgs.insert(8, _call("d", "silica_read_note", {"name": "Old"}))
    msgs.insert(9, {"role": "tool", "tool_call_id": "d",
                    "content": "⟪silica: result elided — x⟫" + BIG})           # already a stub
    msgs += [_call("e", "silica_read_note", {"name": "Now"}),
             {"role": "tool", "tool_call_id": "e", "content": BIG}]             # current turn
    with patch.object(relevance, "decide", return_value=_answers(0.0)) as d:
        dropped = relevance.compact_by_relevance(msgs, TOOLS)
    assert dropped == [3] and d.call_count == 1


def test_first_turn_has_nothing_to_judge(on):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    with patch.object(relevance, "decide") as d:
        assert relevance.compact_by_relevance(msgs, TOOLS) == []
    d.assert_not_called()


def test_run_agent_judges_on_entry_of_an_interactive_turn():
    seen = []
    final = SimpleNamespace(assistant_message={"role": "assistant", "content": "ok"},
                            tool_calls=[], text="ok", reasoning=None, usage={})
    with patch("silica.agent.loop.compact_by_relevance",
               side_effect=lambda m, t: seen.append(len(m)) or []), \
         patch("silica.agent.loop.call_llm", return_value=final):
        run_agent(messages=_history(), model="m")
    assert seen == [6]
