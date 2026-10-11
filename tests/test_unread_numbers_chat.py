# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0095 at the two callers: the `numbers` turn note, live and replayed, and
the session set that grounds a number a past turn read."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from silica.agent import turn_notes
from silica.agent.events import ToolCompleteEvent
from silica.agent.recall_watch import THIN_COVERAGE_HINT
from silica.config import CONFIG

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

THIN = {"kind": "retrieval", "text": THIN_COVERAGE_HINT}


def _note(*numbers: str) -> dict:
    return {"kind": "numbers", "text": "numbers not in what this turn read: " + ", ".join(numbers)}


def _read_sse(response) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines()
            if line.startswith("data: ")]


def _event(name: str, result: dict, call_id: str = "c1") -> ToolCompleteEvent:
    return ToolCompleteEvent(name=name, args={}, call_id=call_id, result=json.dumps(result),
                             duration_s=0.0, iteration=1)


def _turn(answer: str, *events: ToolCompleteEvent):
    """A fake run_agent: each event reaches the turn's wrapper and the narration,
    as the loop's _emit sends it to both, then the answer lands as the final
    assistant message, the way the loop leaves it."""
    def run(messages, model=None, tool_progress_callback=None, cancel_token=None, **kw):
        from silica.agent import narration

        for ev in events:
            tool_progress_callback(ev)
            narration.NARRATOR.on_render_event(ev)
        messages.append({"role": "assistant", "content": answer})
        return answer
    return run


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    server._reset_session()
    yield TestClient(server.app), server
    server._reset_session()


READ_242 = _event("silica_read_note", {"path": "Mail.md", "body": "the sensor table has 242 columns"})


class TestGui:
    def test_an_unread_number_gets_its_note_live_and_on_the_answer(self, client, monkeypatch):
        tc, server = client
        monkeypatch.setattr(server, "run_agent", _turn("It has 243+ variables, as 242 columns.", READ_242))
        done = _read_sse(tc.post("/chat", json={"text": "how many variables?"}))[-1]
        assert done["notes"] == [_note("243")]
        assert server.messages[-1][turn_notes.KEY] == [_note("243")]
        assert [m["notes"] for m in tc.get("/messages").json() if m["role"] == "assistant"] == [
            [_note("243")]]

    def test_numbers_come_after_retrieval_in_the_channel(self, client, monkeypatch):
        tc, server = client
        miss = _event("silica_recall", {"query": "x", "notes": []})
        monkeypatch.setattr(server, "run_agent", _turn("Roughly 55 to 60% of it.", miss))
        done = _read_sse(tc.post("/chat", json={"text": "how much?"}))[-1]
        assert done["notes"] == [THIN, _note("55", "60")]

    def test_nothing_unread_means_no_note(self, client, monkeypatch):
        tc, server = client
        monkeypatch.setattr(server, "run_agent", _turn("It has 242 columns.", READ_242))
        done = _read_sse(tc.post("/chat", json={"text": "how many columns?"}))[-1]
        assert "notes" not in done

    def test_the_answer_is_checked_before_attribution_adds_to_it(self, client, monkeypatch):
        """relay_sources and place_figures add text the model did not write."""
        tc, server = client
        import silica.sources.web_research as wr

        web = _event("silica_web_answer", {"answer": "x"})
        monkeypatch.setattr(wr, "relay_sources",
                            lambda answer, messages: answer + "\n\n## Sources\n1. page 4471")
        monkeypatch.setattr(server, "run_agent", _turn("It has 242 columns.", READ_242, web))
        done = _read_sse(tc.post("/chat", json={"text": "how many columns?"}))[-1]
        assert "4471" in done["answer"]
        assert "notes" not in done

    def test_a_stopped_turn_is_not_checked(self, client, monkeypatch):
        tc, server = client

        def stopped(messages, model=None, tool_progress_callback=None, cancel_token=None, **kw):
            cancel_token.set()
            return "(silica: cancelled) 9999"

        monkeypatch.setattr(server, "run_agent", stopped)
        done = _read_sse(tc.post("/chat", json={"text": "q"}))[-1]
        assert done["stopped"] is True
        assert "notes" not in done

    def test_the_session_set_is_kept_across_turns_and_emptied_by_a_new_chat(self, client, monkeypatch):
        tc, server = client
        monkeypatch.setattr(server, "run_agent", _turn("Read it.", READ_242))
        tc.post("/chat", json={"text": "read the mail"})
        # turn two reads nothing: 242 was read by turn one
        monkeypatch.setattr(server, "run_agent", _turn("It has 242 columns."))
        assert "notes" not in _read_sse(tc.post("/chat", json={"text": "how many?"}))[-1]
        tc.post("/reset")
        done = _read_sse(tc.post("/chat", json={"text": "how many?"}))[-1]
        assert done["notes"] == [_note("242")]

    def test_session_load_rebuilds_the_set_the_live_chat_had(self, client, monkeypatch):
        tc, server = client
        events = (READ_242, _event("silica_search", {"hits": ["a"], "total": 1067}, "c2"),
                  _event("silica_read_note", {"error": "no note 404"}, "c3"))
        monkeypatch.setattr(server, "run_agent", _turn("Read it.", *events))
        tc.post("/chat", json={"text": "read the mail"})
        live = set(server._seen_numbers)
        assert {"242", "1067"} <= live and "404" not in live
        sid = server.current_session_id
        tc.post("/reset")
        assert server._seen_numbers == set()
        assert tc.post("/session/load", json={"id": sid}).status_code == 200
        assert server._seen_numbers == live
        monkeypatch.setattr(server, "run_agent", _turn("242 columns, 1,067 hits, 404 errors."))
        done = _read_sse(tc.post("/chat", json={"text": "and so?"}))[-1]
        assert done["notes"] == [_note("404")]

    def test_a_reopened_chat_draws_the_note_in_channel_order(self, client, monkeypatch):
        tc, server = client
        miss = _event("silica_recall", {"query": "x", "notes": []})
        monkeypatch.setattr(server, "run_agent", _turn("About 70 more passages.", miss))
        tc.post("/chat", json={"text": "how many?"})
        sid = server.current_session_id
        tc.post("/reset")
        assert tc.post("/session/load", json={"id": sid}).status_code == 200
        replayed = [m["notes"] for m in tc.get("/messages").json() if m["role"] == "assistant"]
        assert replayed == [[THIN, _note("70")]]

    def test_the_seed_grounds_this_year_and_the_user_grounds_their_numbers(self, client, monkeypatch):
        import datetime

        tc, server = client
        year = str(datetime.date.today().year)
        monkeypatch.setattr(server, "run_agent", _turn(f"In {year}, 45 minutes a day, 3010 left."))
        done = _read_sse(tc.post("/chat", json={"text": "plan 45 minutes a day"}))[-1]
        assert done["notes"] == [_note("3010")]

    def test_a_web_turns_results_ground_later_turns(self, client, monkeypatch):
        """/web turns are not checked, but the session read their pages: the
        rebuilt set holds them, so the live one must too."""
        tc, server = client
        page = _event("web_fetch", {"url": "https://x.test", "text": "population 8,336,817"})
        monkeypatch.setattr(server, "run_agent", _turn("New York has 9,999,999 people.", page))
        done = _read_sse(tc.post("/chat", json={"text": "/web population of New York"}))[-1]
        assert not any(n["kind"] == "numbers" for n in done.get("notes", []))
        assert "8336817" in server._seen_numbers


def _repl(inputs, turns, monkeypatch):
    """Drive the REPL through `inputs`; each agent turn plays the next of `turns`.
    Returns the notes each turn printed."""
    printed: list[list[dict]] = []
    session = MagicMock()
    pending = list(inputs)
    session.prompt.side_effect = lambda *a, **k: (pending.pop(0)() if callable(pending[0])
                                                 else pending.pop(0))
    plays = iter(turns)

    def run_agent(messages, model=None, tool_progress_callback=None, **kw):
        return next(plays)(messages, model, tool_progress_callback)

    monkeypatch.setattr(CONFIG, "capture_sessions", False)
    with patch("silica.cli.build_session", MagicMock(return_value=session)), \
         patch("silica.cli.run_agent", run_agent), \
         patch("silica.cli._model_configured", lambda: True), \
         patch("silica.cli.CONSOLE"), \
         patch("silica.cli.print_home"), \
         patch("silica.cli._setup_logging"), \
         patch("silica.cli._update_context_tokens"), \
         patch("silica.cli._ensure_servers"), \
         patch("silica.cli._announce_code_lane"), \
         patch("silica.cli._activate_repo_mode"), \
         patch("silica.cli._print_turn_notes", printed.append), \
         patch("sys.argv", ["silica"]):
        from silica.cli import main
        main()
    return printed


class TestRepl:
    def test_kept_across_turns_emptied_by_clear_rebuilt_by_resume(self, tmp_vault, monkeypatch):
        from silica.agent import narration as narr

        first: list[str] = []

        def read_turn(messages, model, cb):
            first.append(narr.NARRATOR.sid)
            return _turn("It has 243 variables.", READ_242)(messages, model, cb)

        printed = _repl(
            ["read the mail", "how many?", "/clear", "how many?",
             lambda: f"/resume {first[0]}", "how many?", "/exit"],
            [read_turn, _turn("It has 242 columns."), _turn("It has 242 columns."),
             _turn("It has 242 columns.")],
            monkeypatch,
        )
        assert printed == [[_note("243")], [], [_note("242")], []]
