# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Workspaces in the web app (ADR-0112): the drawer's endpoints, and a chat
opened in a workspace that searches only its members, reads the note's body
as standing instructions, lists the notes it writes and keeps its scope."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# the write gate registers on import
import silica.tools.notes  # noqa: E402,F401
from silica.agent.events import ToolCompleteEvent  # noqa: E402

WS = """---
type: workspace
notes:
- '[[Dataset README]]'
folders:
- Courses/Econometrics
where: []
---
# Thesis

Answer in Italian and cite the page.
"""


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    tmp_vault.note("Courses/Econometrics/Panel data.md", "# Panel data\n\nFixed effects.\n")
    tmp_vault.note("Courses/Statistics/Bayes.md", "# Bayes\n\nPriors.\n")
    tmp_vault.note("Inbox/Dataset README.md", "# Dataset\n\nColumns.\n")
    tmp_vault.note("Workspaces/Thesis.md", WS)
    server._reset_session()
    return TestClient(server.app), server


def test_the_drawer_lists_workspaces_with_members_and_reasons(client):
    tc, _ = client
    body = tc.get("/workspaces").json()
    assert body["current"] is None
    (ws,) = body["workspaces"]
    assert ws["name"] == "Thesis" and ws["count"] == 2 and ws["has_instructions"] is True
    reasons = {m["path"]: [r["kind"] for r in m["reasons"]] for m in ws["members"]}
    assert reasons == {"Courses/Econometrics/Panel data.md": ["folder"], "Inbox/Dataset README.md": ["listed"]}


def test_create_add_remove_and_membership(client):
    tc, _ = client
    made = tc.post("/workspaces", json={"name": "Exam", "folders": ["Courses/Statistics"]}).json()
    assert made["path"].endswith("Workspaces/Exam.md") and made["count"] == 1
    assert tc.post("/workspaces", json={"name": "exam"}).status_code == 400
    out = tc.post("/workspaces/line", json={"workspace": "Thesis", "key": "notes", "value": "Bayes"}).json()
    assert out["count"] == 3
    rows = {r["name"]: r for r in tc.get("/workspaces/membership", params={"path": "Courses/Statistics/Bayes.md"}).json()["workspaces"]}
    assert rows["Thesis"]["listed"] is True
    assert rows["Exam"]["listed"] is False and rows["Exam"]["reasons"][0]["kind"] == "folder"
    out = tc.post("/workspaces/line", json={"workspace": "Thesis", "key": "notes", "value": "Bayes", "on": False}).json()
    assert out["count"] == 2
    assert tc.post("/workspaces/line", json={"workspace": "Nope", "key": "notes", "value": "x"}).status_code == 404


def test_a_chat_opened_in_a_workspace_is_scoped_instructed_and_lists_what_it_writes(client, monkeypatch, tmp_vault):
    tc, server = client
    from silica.kernel import workspaces

    seen: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        seen["allow"] = workspaces.allow_keys()
        seen["system"] = [m["content"] for m in messages if m.get("origin") == "workspace"]
        tool_progress_callback(ToolCompleteEvent("silica_write_note", {"path": "Notes/Summary.md"}, "c1",
                                                 json.dumps({"op": "write", "success": True, "path": "Notes/Summary.md"}),
                                                 0.0, 0))
        messages.append({"role": "assistant", "content": "Fatto."})
        return "Fatto."

    tmp_vault.note("Notes/Summary.md", "# Summary\n")
    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    resp = tc.post("/chat", json={"text": "riassumi", "workspace": "Workspaces/Thesis.md"})
    assert resp.status_code == 200
    assert seen["allow"] == frozenset({"Courses/Econometrics/Panel data", "Inbox/Dataset README"})
    (line,) = seen["system"]
    assert '"Thesis" (2 notes)' in line and "Answer in Italian and cite the page." in line
    user = next(m for m in server.messages if m["role"] == "user")
    assert user["silica_workspace"] == "Workspaces/Thesis.md"
    assert "Summary" in workspaces.find_workspace("Thesis")["notes"]     # the written note joined
    assert workspaces.allow_keys() is None                                # the scope ends with the turn
    assert tc.get("/workspaces").json()["current"] == "Workspaces/Thesis.md"


def test_closing_the_scope_widens_the_next_turn_and_edits_the_line_in_place(client, monkeypatch):
    tc, server = client
    from silica.kernel import workspaces

    allows = []

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        allows.append(workspaces.allow_keys())
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    tc.post("/chat", json={"text": "one", "workspace": "Thesis"})
    n = len([m for m in server.messages if m.get("origin") == "workspace"])
    tc.post("/chat", json={"text": "two"})
    lines = [m["content"] for m in server.messages if m.get("origin") == "workspace"]
    assert n == 1 and len(lines) == 1 and "no longer limited" in lines[0]
    assert allows[0] is not None and allows[1] is None
    users = [m for m in server.messages if m["role"] == "user"]
    assert "silica_workspace" in users[0] and "silica_workspace" not in users[1]


def test_a_reopened_chat_keeps_the_scope_of_its_last_turn(client, monkeypatch):
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    tc.post("/chat", json={"text": "one", "workspace": "Thesis"})
    sid = server.current_session_id
    tc.post("/reset")
    assert tc.get("/workspaces").json()["current"] is None
    assert tc.post("/session/load", json={"id": sid}).status_code == 200
    assert tc.get("/workspaces").json()["current"] == "Workspaces/Thesis.md"
    assert any(m.get("origin") == "workspace" for m in server.messages)


def test_the_internal_key_never_reaches_the_provider():
    from silica.agent.providers import INTERNAL_KEYS

    assert "silica_workspace" in INTERNAL_KEYS
