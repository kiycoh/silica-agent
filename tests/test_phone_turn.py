# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0089 point 5 and its 2026-10-14 amendment (H5): a turn from Telegram
runs in the phone's own conversation, on a read-only toolset, behind the one
turn gate, and its usage line says it came from the phone."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from silica.agent.events import ToolStartEvent
from tests.test_pilot_metrics import journal  # noqa: F401  the hosted journal fixture

WRITES = {"silica_write_note", "silica_patch_note", "silica_move", "silica_flag_note",
          "silica_remember", "silica_ledger_update", "silica_run_injector",
          "silica_web_answer", "silica_load_tools"}


@pytest.fixture
def phone(tmp_vault, monkeypatch):
    from silica.ui.web import server

    server._reset_session()
    server._reset_phone()
    seen: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None,
                       constraints=None, **kw):
        seen.update(messages=messages, constraints=constraints)
        tool_progress_callback(ToolStartEvent("silica_search", {}, "c1", 0))
        messages.append({"role": "assistant", "content": "From your notes: 42."})
        return "From your notes: 42."

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    yield TestClient(server.app), server, seen
    server._reset_phone()


def test_phone_tools_only_read():
    import silica.cli  # noqa: F401  registers the whole core
    from silica.agent.constraints import CHAT_CORE, PHONE_TOOLS, _eligible

    assert set(PHONE_TOOLS) <= set(CHAT_CORE)
    assert not set(PHONE_TOOLS) & WRITES
    assert set(PHONE_TOOLS) <= set(_eligible())


def test_a_phone_turn_runs_on_its_own_conversation_with_phone_tools(phone):
    from silica.agent.constraints import PHONE_TOOLS

    tc, server, seen = phone
    server.messages.append({"role": "user", "content": "browser question"})
    browser = [dict(m) for m in server.messages]

    r = tc.post("/phone/turn", json={"text": "what did I write about entropy?"})

    assert r.status_code == 200 and r.json() == {"answer": "From your notes: 42."}
    c = seen["constraints"]
    assert set(c.tools) <= set(PHONE_TOOLS) and c.deferred == ()
    assert seen["messages"] is server._phone_messages
    assert server.messages == browser
    user = [m for m in server._phone_messages if m.get("role") == "user"]
    assert user == [{"role": "user", "content": "what did I write about entropy?", "origin": "telegram"}]
    assert any("on Telegram" in str(m.get("content")) for m in server._phone_messages
               if m.get("role") == "system")
    assert not server._busy


def test_a_phone_turn_is_refused_while_another_holds_the_gate(phone):
    tc, server, seen = phone
    assert server._begin_turn()
    try:
        r = tc.post("/phone/turn", json={"text": "hello"})
    finally:
        server._end_turn()
    assert r.status_code == 409 and "messages" not in seen


def test_the_phone_narrates_its_own_session_and_gives_the_browsers_back(phone):
    from silica.agent import narration
    from silica.config import CONFIG

    tc, server, _ = phone
    browser_sid = server._open_session()
    CONFIG.context_tokens = 1234

    tc.post("/phone/turn", json={"text": "first"})

    assert narration.NARRATOR.sid == browser_sid
    assert CONFIG.context_tokens == 1234
    phone_sid = server._phone_sid
    assert phone_sid and phone_sid != browser_sid
    rows = {r["id"]: r for r in narration.list_sessions(CONFIG.vault_path)}
    assert rows[phone_sid]["title"] == "Telegram"
    beats = list(narration.read_beats(narration.narration_dir() / f"{phone_sid}.jsonl"))
    said = [b["payload"]["message"] for b in beats if b["kind"] == "turn"]
    assert [m["role"] for m in said] == ["user", "assistant"]
    assert said[0]["origin"] == "telegram"

    tc.post("/phone/turn", json={"text": "second"})
    assert server._phone_sid == phone_sid  # one conversation across turns


def test_the_phone_moves_on_when_the_browser_opens_its_session(phone):
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    taken = server._phone_sid
    server.current_session_id = taken

    tc.post("/phone/turn", json={"text": "second"})

    assert server._phone_sid != taken
    assert [m["content"] for m in server._phone_messages if m.get("role") == "user"] == ["second"]


def test_new_starts_the_phone_over(phone):
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    assert tc.post("/phone/new").json() == {"ok": True}
    assert server._phone_messages == [] and server._phone_sid is None


def test_the_turn_line_names_the_channel_and_a_failure_answers_its_sentence(phone, journal,  # noqa: F811
                                                                             monkeypatch):
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "student-wrote-this"})

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(server, "run_agent", boom)
    r = tc.post("/phone/turn", json={"text": "again"})

    assert r.status_code == 200 and set(r.json()) == {"error"}
    ok, failed = journal.events("turn")
    assert "student-wrote-this" not in json.dumps(ok)
    assert (ok["channel"], ok["outcome"], ok["tools"]) == ("telegram", "done", {"silica_search": 1})
    assert (failed["channel"], failed["outcome"]) == ("telegram", "error")
    assert not server._busy


def test_an_empty_text_is_refused(phone):
    tc, _, seen = phone
    assert tc.post("/phone/turn", json={"text": "  "}).status_code == 422
    assert "messages" not in seen


def test_new_chat_during_a_phone_turn_keeps_the_gate_and_the_old_chat_clean(phone, monkeypatch):
    """The browser cannot see a phone turn, so its New chat can land in the
    middle of one: the gate stays the phone's, and the narrator ends on the
    new chat, not on the file the student left."""
    from silica.agent import narration

    tc, server, _ = phone
    old = server._open_session()
    seen = {}

    def agent(messages, model, cb=None, cancel_token=None, constraints=None, **kw):
        server._reset_session()          # New chat, mid-turn
        server._sweep_if_orphaned()      # a /chat stream closing in the same instant
        seen["busy"] = server._busy
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", agent)
    assert tc.post("/phone/turn", json={"text": "hi"}).json() == {"answer": "ok"}

    assert seen["busy"] is True and not server._busy
    assert server.current_session_id is None and narration.NARRATOR.sid is None
    beats = list(narration.read_beats(narration.narration_dir() / f"{old}.jsonl"))
    assert not [b for b in beats if b["kind"] == "turn"]
    said = list(narration.read_beats(narration.narration_dir() / f"{server._phone_sid}.jsonl"))
    assert [b["payload"]["message"]["role"] for b in said if b["kind"] == "turn"] == ["user", "assistant"]


def test_a_deleted_phone_chat_starts_over(phone):
    from silica.agent import narration

    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    gone = server._phone_sid
    (narration.narration_dir() / f"{gone}.jsonl").unlink()

    tc.post("/phone/turn", json={"text": "second"})

    assert server._phone_sid != gone
    assert [m["content"] for m in server._phone_messages if m.get("role") == "user"] == ["second"]


def test_the_phone_beats_stay_off_the_browser_tab(phone):
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    phone_sid = server._phone_sid
    assert not server._beat_for_tab({"sid": phone_sid})
    assert server._beat_for_tab({"sid": "browser"})
    server.current_session_id = phone_sid   # the browser opened the phone's chat
    assert server._beat_for_tab({"sid": phone_sid})
