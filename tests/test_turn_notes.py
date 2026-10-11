# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The end-of-turn channel (ADR-0094 point 8).

Every line drawn under a chat answer is a turn note on the final assistant
message, under the internal key `silica_turn_notes`. These tests hold the
channel's contract on every path a note must survive: the live `done` frame,
`/messages`, both compactions, `/session/load`, the landing turn the loop runs
at its cap, the REPL's print order, and the wire, which must never carry it.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent import turn_notes
from silica.agent.events import ToolCompleteEvent
from silica.agent.recall_watch import THIN_COVERAGE_HINT

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

THIN = {"kind": "retrieval", "text": THIN_COVERAGE_HINT}
ANSWER = "I have nothing on that."


def _read_sse(response) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines()
            if line.startswith("data: ")]


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    server._reset_session()
    yield TestClient(server.app), server
    server._reset_session()


def _thin_turn(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
    """A turn whose one recall came back empty: the thin hint is its note."""
    tool_progress_callback(ToolCompleteEvent(
        name="silica_recall", args={}, call_id="c1",
        result=json.dumps({"query": "x", "notes": []}), duration_s=0.0, iteration=1,
    ))
    messages.append({"role": "assistant", "content": ANSWER})
    return ANSWER


def _assistant_notes(tc) -> list[list[dict]]:
    return [m["notes"] for m in tc.get("/messages").json() if m["role"] == "assistant"]


class TestAttach:
    def test_kinds_come_back_in_channel_order(self):
        msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        notes = turn_notes.attach(msgs, [
            {"kind": "graph", "text": "g"},
            {"kind": "numbers", "text": "n"},
            {"kind": "retrieval", "text": "r"},
        ])
        assert [n["kind"] for n in notes] == ["retrieval", "numbers", "graph"]
        assert msgs[-1][turn_notes.KEY] == notes

    def test_two_notes_of_one_kind_keep_the_order_they_were_given(self):
        msgs = [{"role": "assistant", "content": "a"}]
        notes = turn_notes.attach(msgs, [{"kind": "graph", "text": "g"},
                                         {"kind": "retrieval", "text": "first"},
                                         {"kind": "retrieval", "text": "second"}])
        assert [n["text"] for n in notes] == ["first", "second", "g"]

    def test_only_the_final_answer_carries_them(self):
        """A tool-calling step is not the answer, and the system instruction a
        landing turn appends is not either."""
        step = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}
        system = {"role": "system", "content": "no tools are available now"}
        for last in (step, system):
            turn_notes.attach([last], [THIN])
            assert turn_notes.KEY not in last

    def test_a_turn_with_no_notes_sets_no_key(self):
        msgs = [{"role": "assistant", "content": "a"}]
        assert turn_notes.attach(msgs, []) == []
        assert turn_notes.KEY not in msgs[-1]

    def test_an_unknown_kind_is_refused(self):
        """A sibling ADR adds its kind to KINDS, where its place is decided."""
        with pytest.raises(ValueError):
            turn_notes.attach([{"role": "assistant", "content": "a"}],
                              [{"kind": "mood", "text": "x"}])


class TestNeverSentToTheModel:
    def test_the_wire_drops_the_key(self):
        from silica.agent.providers import _to_wire

        wire = _to_wire({"role": "assistant", "content": "a", turn_notes.KEY: [THIN]})
        assert wire == {"role": "assistant", "content": "a"}

    def test_the_next_request_carries_no_key(self, client, monkeypatch):
        """Turn one stores a note; turn two goes through the real loop and the
        real call_llm, and litellm receives the history without it."""
        from silica.agent.constraints import AgentConstraints
        from silica.agent.loop import run_agent
        from tests.llm_mocks import litellm_mock_response

        tc, server = client
        monkeypatch.setattr(server, "run_agent", _thin_turn)
        tc.post("/chat", json={"text": "what is graph rewiring?"})
        assert server.messages[-1][turn_notes.KEY] == [THIN]

        def real_turn(messages, model, cb, cancel_token=None, **kw):
            return run_agent(messages, "test_model", cb, cancel_token=cancel_token,
                             constraints=AgentConstraints(tools=(), max_iterations=1))

        monkeypatch.setattr(server, "run_agent", real_turn)
        with patch("litellm.completion",
                   return_value=litellm_mock_response("second answer")) as completion:
            tc.post("/chat", json={"text": "and now?"})
        sent = completion.call_args.kwargs["messages"]
        assert any(m.get("content") == ANSWER for m in sent)  # the history did go
        assert not any(turn_notes.KEY in m for m in sent)


class TestLive:
    def test_the_done_frame_carries_the_notes_in_place_of_hint(self, client, monkeypatch):
        tc, server = client
        monkeypatch.setattr(server, "run_agent", _thin_turn)
        done = _read_sse(tc.post("/chat", json={"text": "what is graph rewiring?"}))[-1]
        assert done["notes"] == [THIN]
        assert "hint" not in done
        assert server.messages[-1][turn_notes.KEY] == [THIN]

    def test_a_turn_with_nothing_to_say_has_no_notes(self, client, monkeypatch):
        tc, server = client

        def found(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
            tool_progress_callback(ToolCompleteEvent(
                name="silica_recall", args={}, call_id="c1",
                result=json.dumps({"notes": ["Concepts/RAG.md"]}), duration_s=0.0, iteration=1,
            ))
            messages.append({"role": "assistant", "content": "You wrote about it."})
            return "You wrote about it."

        monkeypatch.setattr(server, "run_agent", found)
        done = _read_sse(tc.post("/chat", json={"text": "what about RAG?"}))[-1]
        assert "notes" not in done
        assert turn_notes.KEY not in server.messages[-1]

    def test_a_stopped_turn_draws_its_notes_live_only(self, client, monkeypatch):
        """No final answer to carry them: the frame has them, the history not."""
        tc, server = client

        def stopped(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
            _thin_turn([], model, tool_progress_callback)
            cancel_token.set()
            return "(silica: cancelled)"

        monkeypatch.setattr(server, "run_agent", stopped)
        done = _read_sse(tc.post("/chat", json={"text": "q"}))[-1]
        assert done["stopped"] is True
        assert done["notes"] == [THIN]
        assert not any(turn_notes.KEY in m for m in server.messages)


class TestReplay:
    def test_messages_gives_the_assistant_entry_its_notes(self, client, monkeypatch):
        tc, server = client
        monkeypatch.setattr(server, "run_agent", _thin_turn)
        tc.post("/chat", json={"text": "what is graph rewiring?"})
        entries = tc.get("/messages").json()
        assert [m["notes"] for m in entries if m["role"] == "assistant"][-1] == [THIN]
        assert all(m["notes"] == [] for m in entries if m["role"] == "user")

    def test_both_compactions_leave_the_key(self, client, monkeypatch):
        """Relevance and budget compaction rewrite tool results, never the
        assistant answer: forced over a history whose tool results all qualify."""
        from silica.agent import relevance
        from silica.agent.compaction import compact_read_history
        from silica.tools import TOOLS

        tc, server = client
        big = json.dumps({"query": "x", "notes": [], "context": "y" * 6000})

        def reads_then_misses(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
            # One note per turn: a second read of the same note is stubbed as
            # redundant by the between-turns sweep, leaving nothing to force.
            n = sum(m.get("role") == "user" for m in messages)
            args = json.dumps({"path": f"n{n}.md"})
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"r{n}", "type": "function",
                 "function": {"name": "silica_read_note", "arguments": args}}]})
            messages.append({"role": "tool", "tool_call_id": f"r{n}", "content": big})
            return _thin_turn(messages, model, tool_progress_callback)

        monkeypatch.setattr(server, "run_agent", reads_then_misses)
        for q in ("first", "second", "third"):
            tc.post("/chat", json={"text": q})
        before = _assistant_notes(tc)
        assert before.count([THIN]) == 3

        monkeypatch.setattr(relevance, "ENABLED", True)
        monkeypatch.setattr(server.CONFIG, "decision_model", "stub/model")
        monkeypatch.setattr(relevance, "decide",
                            lambda state, q: {"needed": {"noul": 0.0}})
        dropped = relevance.compact_by_relevance(server.messages, TOOLS)
        collapsed = compact_read_history(server.messages, set(), prompt_tokens=10**6,
                                         budget=1, floor_turns=1, tools=TOOLS)
        assert dropped and collapsed  # both passes did rewrite something
        assert _assistant_notes(tc) == before

    def test_session_load_gives_them_back(self, client, monkeypatch):
        tc, server = client
        monkeypatch.setattr(server, "run_agent", _thin_turn)
        tc.post("/chat", json={"text": "what is graph rewiring?"})
        sid = server.current_session_id
        tc.post("/reset")
        assert tc.post("/session/load", json={"id": sid}).status_code == 200
        assert _assistant_notes(tc)[-1] == [THIN]


# The landing turn: the loop hits its cap and asks for one closing answer with
# no tools. That answer used to be narrated as a detached dict and never entered
# `messages`, so a note had nowhere to live and /messages never showed it.

class _EmptyArgs(BaseModel):
    pass


def _resp(text="", tool_calls=None):
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = [{"id": t.id, "type": "function",
                              "function": {"name": t.name, "arguments": "{}"}}
                             for t in tool_calls]
    return SimpleNamespace(assistant_message=msg, tool_calls=tool_calls or [],
                           text=text, reasoning=None, usage={})


def _landing_llm(model, messages, tools=None, **kw):
    if tools is None:  # the closing call: no tools offered
        return _resp(text="Closing answer from what I found.")
    return _resp(tool_calls=[SimpleNamespace(name="silica_recall", args={}, id="c1")])


@pytest.fixture
def empty_recall(monkeypatch):
    from silica.tools import TOOLS, Tool

    monkeypatch.setitem(TOOLS, "silica_recall", Tool(
        lambda: json.dumps({"query": "x", "notes": []}), "silica_recall",
        "test recall", _EmptyArgs, "composed"))


class TestLandingTurn:
    def test_the_closing_answer_enters_the_history(self, empty_recall):
        from silica.agent.constraints import AgentConstraints
        from silica.agent.loop import run_agent

        messages = [{"role": "user", "content": "q"}]
        with patch("silica.agent.loop.call_llm", _landing_llm):
            text = run_agent(messages, "router", constraints=AgentConstraints(
                tools=("silica_recall",), max_iterations=1))
        assert text == "Closing answer from what I found."
        assert messages[-1] == {"role": "assistant", "content": text}

    def test_a_landing_turn_keeps_its_notes_live_and_on_both_replays(
            self, client, monkeypatch, empty_recall):
        from silica.agent import narration as narr
        from silica.agent.loop import run_agent

        tc, server = client

        def capped(messages, model, cb, cancel_token=None, constraints=None):
            return run_agent(messages, model, cb, cancel_token=cancel_token,
                             constraints=dataclasses.replace(constraints, max_iterations=1))

        monkeypatch.setattr(server, "run_agent", capped)
        with patch("silica.agent.loop.call_llm", _landing_llm):
            done = _read_sse(tc.post("/chat", json={"text": "what is graph rewiring?"}))[-1]
        assert done["answer"] == "Closing answer from what I found."
        assert done["notes"] == [THIN]
        assert _assistant_notes(tc)[-1] == [THIN]

        sid = server.current_session_id
        beats = narr.read_beats(narr.narration_dir() / f"{sid}.jsonl")
        answers = [b for b in narr.messages_from_beats(list(beats))
                   if b.get("content") == done["answer"]]
        assert len(answers) == 1  # narrated once, by the caller, with its notes
        tc.post("/reset")
        assert tc.post("/session/load", json={"id": sid}).status_code == 200
        assert _assistant_notes(tc)[-1] == [THIN]


class TestRepl:
    def test_the_repl_prints_them_in_channel_order(self):
        from silica.cli import _print_turn_notes
        from silica.ui.console import CONSOLE

        msgs = [{"role": "assistant", "content": "a"}]
        notes = turn_notes.attach(msgs, [{"kind": "graph", "text": "graph line"},
                                         {"kind": "numbers", "text": "numbers line"},
                                         {"kind": "retrieval", "text": "[retrieval] line"}])
        with CONSOLE.capture() as cap:
            _print_turn_notes(notes)
        out = cap.get()
        assert out.index("[retrieval] line") < out.index("numbers line") < out.index("graph line")


# The GUI draws the same block live (app-chat.js, the `done` frame) and on
# reload (app-boot.js, /messages): one function, so the two cannot drift.

def _turn_notes_js() -> str:
    import re

    from tests.webassets import app_js

    m = re.search(r"^function turnNotes\(notes\) \{.*?^\}", app_js(), re.S | re.M)
    assert m, "turnNotes() not found in app.js"
    return m.group(0)


_DOM_STUB = """
const document = { createElement: (tag) => ({ tag, className: "", textContent: "",
  children: [], appendChild(c) { this.children.push(c); return c; } }) };
"""


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node to run app.js")
def test_the_gui_draws_one_muted_line_per_note_in_order(tmp_path):
    import subprocess

    script = tmp_path / "notes.js"
    script.write_text(_DOM_STUB + _turn_notes_js() + """
const b = turnNotes(JSON.parse(process.argv[2]));
console.log(JSON.stringify({cls: b.className, lines: b.children.map((c) => [c.className, c.textContent])}));
console.log(JSON.stringify([turnNotes([]), turnNotes(undefined)]));
""")
    notes = [{"kind": "retrieval", "text": "lexical only: x"}, {"kind": "numbers", "text": "n"}]
    out = subprocess.run(["node", str(script), json.dumps(notes)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    drawn, empty = (json.loads(line) for line in out.stdout.splitlines())
    assert drawn == {"cls": "turn-notes",
                     "lines": [["turn-note retrieval", "lexical only: x"],
                               ["turn-note numbers", "n"]]}
    assert empty == [None, None]  # nothing drawn for a turn with no notes


def test_live_and_replay_both_draw_through_turn_notes():
    from tests.webassets import WEB, app_css

    chat = (WEB / "app-chat.js").read_text(encoding="utf-8")
    boot = (WEB / "app-boot.js").read_text(encoding="utf-8")
    assert chat.count("turnNotes(ev.notes)") == 2  # an answer and a stopped turn
    assert "turnNotes(m.notes)" in boot
    assert "turn-hint" not in chat and "ev.hint" not in chat
    assert ".turn-notes" in app_css() and ".turn-hint" not in app_css()
