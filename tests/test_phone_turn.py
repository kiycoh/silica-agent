# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0089 point 5 and its 2026-10-14 amendment (H5): a turn from Telegram
runs in the phone's own conversation, on a read-only toolset, behind the one
turn gate, and its usage line says it came from the phone."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from silica.obs.events import ToolStartEvent
from tests.test_pilot_metrics import journal  # noqa: F401  the hosted journal fixture

WRITES = {"silica_write_note", "silica_patch_note", "silica_move", "silica_flag_note",
          "silica_remember", "silica_ledger_update", "silica_run_injector",
          "silica_web_answer", "silica_load_tools"}


@pytest.fixture
def phone(tmp_vault, monkeypatch):
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    SESSION.reset()
    SESSION.reset_phone()
    seen: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None,
                       constraints=None, **kw):
        seen.update(messages=messages, constraints=constraints)
        tool_progress_callback(ToolStartEvent("silica_search", {}, "c1", 0))
        messages.append({"role": "assistant", "content": "From your notes: 42."})
        return "From your notes: 42."

    monkeypatch.setattr("silica.session.turn.run_agent", fake_run_agent)
    yield TestClient(server.app), server, seen
    SESSION.reset_phone()


def test_phone_tools_only_read():
    import silica.cli  # noqa: F401  registers the whole core
    from silica.agent.constraints import CHAT_CORE, PHONE_TOOLS, _eligible

    assert set(PHONE_TOOLS) <= set(CHAT_CORE)
    assert not set(PHONE_TOOLS) & WRITES
    assert set(PHONE_TOOLS) <= set(_eligible())


def test_a_phone_turn_runs_on_its_own_conversation_with_phone_tools(phone):
    from silica.ui.web.session import SESSION
    from silica.agent.constraints import PHONE_TOOLS

    tc, server, seen = phone
    SESSION.chat.messages.append({"role": "user", "content": "browser question"})
    browser = [dict(m) for m in SESSION.chat.messages]

    r = tc.post("/phone/turn", json={"text": "what did I write about entropy?"})

    assert r.status_code == 200 and r.json() == {"answer": "From your notes: 42."}
    c = seen["constraints"]
    assert set(c.tools) <= set(PHONE_TOOLS) and c.deferred == ()
    assert seen["messages"] is SESSION.phone.messages
    assert SESSION.chat.messages == browser
    user = [m for m in SESSION.phone.messages if m.get("role") == "user"]
    assert user == [{"role": "user", "content": "what did I write about entropy?", "origin": "telegram"}]
    assert any("on Telegram" in str(m.get("content")) for m in SESSION.phone.messages
               if m.get("role") == "system")
    assert not SESSION.busy


def test_a_phone_turn_is_refused_while_another_holds_the_gate(phone):
    from silica.ui.web.session import SESSION
    tc, server, seen = phone
    assert SESSION.claim()
    try:
        r = tc.post("/phone/turn", json={"text": "hello"})
    finally:
        SESSION.release()
    assert r.status_code == 409 and "messages" not in seen


def test_the_phone_narrates_its_own_session_and_gives_the_browsers_back(phone):
    from silica.ui.web import session
    from silica.ui.web.session import SESSION
    from silica.obs import narration
    from silica.config import CONFIG

    tc, server, _ = phone
    browser_sid = session.open_session()
    CONFIG.context_tokens = 1234

    tc.post("/phone/turn", json={"text": "first"})

    assert narration.NARRATOR.sid == browser_sid
    assert CONFIG.context_tokens == 1234
    phone_sid = SESSION.phone.sid
    assert phone_sid and phone_sid != browser_sid
    rows = {r["id"]: r for r in narration.list_sessions(CONFIG.vault_path)}
    assert rows[phone_sid]["title"] == "Telegram"
    beats = list(narration.read_beats(narration.narration_dir() / f"{phone_sid}.jsonl"))
    said = [b["payload"]["message"] for b in beats if b["kind"] == "turn"]
    assert [m["role"] for m in said] == ["user", "assistant"]
    assert said[0]["origin"] == "telegram"

    tc.post("/phone/turn", json={"text": "second"})
    assert SESSION.phone.sid == phone_sid  # one conversation across turns


def test_the_phone_moves_on_when_the_browser_opens_its_session(phone):
    from silica.ui.web.session import SESSION
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    taken = SESSION.phone.sid
    SESSION.chat.sid = taken

    tc.post("/phone/turn", json={"text": "second"})

    assert SESSION.phone.sid != taken
    assert [m["content"] for m in SESSION.phone.messages if m.get("role") == "user"] == ["second"]


def test_new_starts_the_phone_over(phone):
    from silica.ui.web.session import SESSION
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    assert tc.post("/phone/new").json() == {"ok": True}
    assert SESSION.phone.messages == [] and SESSION.phone.sid is None


def test_the_turn_line_names_the_channel_and_a_failure_answers_its_sentence(phone, journal,  # noqa: F811
                                                                             monkeypatch):
    from silica.ui.web.session import SESSION
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "student-wrote-this"})

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("silica.session.turn.run_agent", boom)
    r = tc.post("/phone/turn", json={"text": "again"})

    assert r.status_code == 200 and set(r.json()) == {"error"}
    ok, failed = journal.events("turn")
    assert "student-wrote-this" not in json.dumps(ok)
    assert (ok["channel"], ok["outcome"], ok["tools"]) == ("telegram", "done", {"silica_search": 1})
    assert (failed["channel"], failed["outcome"]) == ("telegram", "error")
    assert not SESSION.busy


def test_an_empty_text_is_refused(phone):
    tc, _, seen = phone
    assert tc.post("/phone/turn", json={"text": "  "}).status_code == 422
    assert "messages" not in seen


def test_new_chat_during_a_phone_turn_keeps_the_gate_and_the_old_chat_clean(phone, monkeypatch):
    """The browser cannot see a phone turn, so its New chat can land in the
    middle of one: the gate stays the phone's, and the narrator ends on the
    new chat, not on the file the student left."""
    from silica.ui.web import session
    from silica.ui.web.session import SESSION
    from silica.obs import narration

    tc, server, _ = phone
    old = session.open_session()
    seen = {}

    def agent(messages, model, cb=None, cancel_token=None, constraints=None, **kw):
        from silica.ui.web.session import SESSION
        SESSION.reset()          # New chat, mid-turn
        SESSION.sweep_if_orphaned()      # a /chat stream closing in the same instant
        seen["busy"] = SESSION.busy
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr("silica.session.turn.run_agent", agent)
    assert tc.post("/phone/turn", json={"text": "hi"}).json() == {"answer": "ok"}

    assert seen["busy"] is True and not SESSION.busy
    assert SESSION.chat.sid is None and narration.NARRATOR.sid is None
    beats = list(narration.read_beats(narration.narration_dir() / f"{old}.jsonl"))
    assert not [b for b in beats if b["kind"] == "turn"]
    said = list(narration.read_beats(narration.narration_dir() / f"{SESSION.phone.sid}.jsonl"))
    assert [b["payload"]["message"]["role"] for b in said if b["kind"] == "turn"] == ["user", "assistant"]


def test_a_deleted_phone_chat_starts_over(phone):
    from silica.ui.web.session import SESSION
    from silica.obs import narration

    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    gone = SESSION.phone.sid
    (narration.narration_dir() / f"{gone}.jsonl").unlink()

    tc.post("/phone/turn", json={"text": "second"})

    assert SESSION.phone.sid != gone
    assert [m["content"] for m in SESSION.phone.messages if m.get("role") == "user"] == ["second"]


def test_the_phone_beats_stay_off_the_browser_tab(phone):
    from silica.ui.web import chat_routes
    from silica.ui.web.session import SESSION
    tc, server, _ = phone
    tc.post("/phone/turn", json={"text": "first"})
    phone_sid = SESSION.phone.sid
    assert not chat_routes._beat_for_tab({"sid": phone_sid})
    assert chat_routes._beat_for_tab({"sid": "browser"})
    SESSION.chat.sid = phone_sid   # the browser opened the phone's chat
    assert chat_routes._beat_for_tab({"sid": phone_sid})
