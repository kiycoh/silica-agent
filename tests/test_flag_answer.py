"""ADR-0100 point 6: a hosted student flags one answer as wrong.

The flag is a pointer, never a copy: the session id and the narration seq of
the user beat that opened the turn. These tests hold the route to that shape,
to hosted mode, and to the ids it accepts, and the page to drawing the control
only where the server named a turn.
"""
from __future__ import annotations

import json

import pytest

from tests.webassets import WEB, app_js

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_vault, _fresh_narrator, monkeypatch):
    from silica.ui.web import server

    server._reset_session()

    def answers(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "forty-two"})
        return "forty-two"

    monkeypatch.setattr(server, "run_agent", answers)
    return TestClient(server.app), server


def _frames(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def _flags() -> list[dict]:
    from silica.kernel.recall.paths import _SILICA_HOME

    path = _SILICA_HOME / "flags.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_hosted_turn_names_its_turn_and_a_flag_points_at_it(client, monkeypatch):
    tc, server = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    tc.post("/chat", json={"text": "first"})
    done = _frames(tc.post("/chat", json={"text": "what is six times seven?"}))[-1]
    turn = done["turn"]
    assert turn["session"] == server.current_session_id
    # the pointer resolves to this turn's own user beat, not the first turn's
    beats = server._session_beats(turn["session"])
    opened = next(b for b in beats if b["seq"] == turn["seq"])
    assert opened["payload"]["message"]["content"] == "what is six times seven?"

    r = tc.post("/flag", json={**turn, "note": "it is 42, but the passage was wrong"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    (line,) = _flags()
    assert set(line) == {"ts", "session", "answer", "note"}
    assert line["session"] == turn["session"] and line["answer"] == turn["seq"]
    assert line["ts"].endswith("Z") and line["note"] == "it is 42, but the passage was wrong"
    # one flag per answer: a second click is acknowledged, not appended
    assert tc.post("/flag", json=turn).json() == {"ok": True, "already": True}
    assert len(_flags()) == 1

    # a reopened chat names each user row's turn and says which is flagged
    rows = [m for m in tc.get("/messages").json() if m["role"] == "user"]
    assert [r["turn"]["flagged"] for r in rows] == [False, True]
    assert rows[1]["turn"]["seq"] == turn["seq"]


def test_a_local_server_has_no_flag(client, monkeypatch):
    tc, _ = client
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    done = _frames(tc.post("/chat", json={"text": "hi"}))[-1]
    assert "turn" not in done
    assert all("turn" not in m for m in tc.get("/messages").json())
    assert tc.post("/flag", json={"session": "abc", "seq": 1}).status_code == 404
    assert not _flags()


def test_the_flag_takes_only_a_plain_id_a_real_answer_and_a_short_note(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    turn = _frames(tc.post("/chat", json={"text": "hi"}))[-1]["turn"]
    for bad in ("../flags", "a/b", "", "é1"):
        assert tc.post("/flag", json={"session": bad, "seq": turn["seq"]}).status_code == 400
    for bad in ("3", None, True, 1.5):
        assert tc.post("/flag", json={"session": turn["session"], "seq": bad}).status_code == 400
    # a seq that is not a user beat of this chat (the session's own opening beat)
    assert tc.post("/flag", json={"session": turn["session"], "seq": 1}).status_code == 404
    assert tc.post("/flag", json={**turn, "note": "x" * 301}).status_code == 400
    assert not _flags()
    assert tc.post("/flag", json={**turn, "note": "x" * 300}).status_code == 200


def test_the_page_draws_the_control_only_where_the_server_named_a_turn():
    js = app_js()
    live = js[js.index("async function runTurn("):js.index("function send(text")]
    assert "flagTurn = ev.turn || null;" in live
    assert "if (answer && flagTurn) flow.appendChild(flagControl(flagTurn));" in live
    replay = js[js.index("async function loadVault()"):js.index("// --- the composer hint")]
    assert "flagTurn = m.turn || null;" in replay
    assert "turn.flag.flagged ? mkFlagDone() : flagControl(turn.flag)" in replay
    assert "if (closeFlagSheet()) return;" in js and '"#flag-sheet", "#st-sheet"' in js
    sheet = (WEB / "index.html").read_text(encoding="utf-8")
    sheet = sheet[sheet.index('<div id="flag-sheet"'):sheet.index('<div id="sr-status"')]
    assert 'maxlength="300"' in sheet and 'href="/privacy"' in sheet
    assert "90 days" in sheet and "this one answer" in sheet
