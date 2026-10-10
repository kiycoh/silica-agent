"""The typed decision in front of a narrowed read (read_scope.py): when the
model writes a `query` on a long note, the user's request decides whole or
part, and anything but a confident "part" reads the whole note."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from silica.agent import read_scope
from silica.driver.base import NoteContent, NoteRef
from silica.kernel.recall import rerank
from silica.kernel.write import provenance
from silica.tools import atomic

LONG = ("# Lezione 32\n\n" + "\n".join(f"riga {i} sul metodo knn" for i in range(400))
        + "\n\n## Boosting\n\nIl boosting combina classificatori deboli.\n")


@pytest.fixture
def note(monkeypatch):
    monkeypatch.setattr(atomic, "DRIVER", SimpleNamespace(
        read_note=lambda name: NoteContent(ref=NoteRef(name=name, path="F/L32.md"), content=LONG)))
    monkeypatch.setattr(provenance, "drift_map", lambda **k: {})
    monkeypatch.setattr(rerank, "window_weights", lambda q: {})
    monkeypatch.setattr(read_scope, "ENABLED", True)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "jev")
    read_scope.REQUESTS.set(("Cos'è il boosting secondo la Lezione 32?",))
    asked = []

    def verdict(choice, p):
        def fake(state, questions, **kw):
            asked.append(state)
            return {"scope": {"type": "choice", "choice": choice, "probabilities": {choice: p}}}
        monkeypatch.setattr(read_scope, "decide", fake)
    return SimpleNamespace(verdict=verdict, asked=asked)


def test_a_confident_part_narrows(note):
    note.verdict("part", 0.9)
    out = atomic.silica_read_note("Lezione 32", query="boosting")
    assert "Il boosting combina" in out and len(out) < len(LONG)
    assert note.asked[0]["current_request"] == "Cos'è il boosting secondo la Lezione 32?"
    assert note.asked[0]["query"] == "boosting"


def test_a_whole_verdict_reads_the_whole_note(note):
    note.verdict("whole", 0.8)
    assert atomic.silica_read_note("Lezione 32", query="riassunto") == LONG


def test_an_unsure_part_reads_the_whole_note(note):
    note.verdict("part", 0.5)
    assert atomic.silica_read_note("Lezione 32", query="boosting") == LONG


def test_no_decision_reads_the_whole_note(note, monkeypatch):
    monkeypatch.setattr(read_scope, "decide", lambda *a, **k: None)
    assert atomic.silica_read_note("Lezione 32", query="boosting") == LONG


@pytest.mark.parametrize("change", ["disabled", "no_query", "no_request"])
def test_nothing_to_decide_asks_nothing(note, monkeypatch, change):
    note.verdict("part", 0.9)
    query = "boosting"
    if change == "disabled":
        monkeypatch.setattr(read_scope, "ENABLED", False)
    elif change == "no_query":
        query = ""
    else:
        read_scope.REQUESTS.set(())
    atomic.silica_read_note("Lezione 32", query=query)
    assert note.asked == []


def test_run_agent_hands_the_turns_requests_to_the_tools(monkeypatch):
    from silica.agent.loop import run_agent

    seen = []

    class Probe:
        name, collapse, sensitive, internal, summarize = "probe", "lazy", False, False, None

        def json_schema(self):
            return {"type": "function", "function": {"name": "probe", "description": "",
                                                     "parameters": {"type": "object", "properties": {}}}}

        def run(self, _cancel_token=None, _progress=None, **kw):
            seen.append(read_scope.REQUESTS.get())
            return "ok"

    calls = [0]

    def llm(*a, **k):
        calls[0] += 1
        if calls[0] == 1:
            return SimpleNamespace(
                assistant_message={"role": "assistant", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
                tool_calls=[SimpleNamespace(id="c1", name="probe", args={})],
                text="", reasoning=None, usage={})
        return SimpleNamespace(assistant_message={"role": "assistant", "content": "done"},
                               tool_calls=[], text="done", reasoning=None, usage={})

    from unittest.mock import patch
    messages = [{"role": "user", "content": "prima"}, {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "riassumi la Lezione 32"}]
    with patch.dict("silica.agent.loop.TOOLS", {"probe": Probe()}, clear=True), \
         patch("silica.agent.loop.call_llm", llm):
        run_agent(messages, model="test")
    assert seen == [("prima", "riassumi la Lezione 32")]
