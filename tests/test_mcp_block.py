"""Settings › Connectors (ADR-0089 point 8) and the MCP client's block in it
(ADR-0091, "GUI and TUI"): one row per mcpServers entry, its state, and the
actions the row offers."""
from __future__ import annotations

import json
import sys

import pytest

from tests.mcp_helpers import FIXTURE, configure


@pytest.fixture(autouse=True)
def _clean():
    yield
    from silica.connectors import mcp_client, mcp_tools

    mcp_tools.unregister()
    mcp_client.CLIENTS.close_all()


def _block():
    from silica.connectors import find

    return find("mcp")


def _row(key):
    return next(r for r in _block().status()["rows"] if r["key"] == key)


def test_the_section_lists_the_mcp_block_with_the_five_fields():
    from silica.connectors import connectors

    names = [c.name for c in connectors()]
    assert "mcp" in names
    st = _block().status()
    assert {"name", "title", "state", "account", "detail", "actions"} <= set(st)
    assert st["state"] == "off" and st["rows"] == []


def test_a_row_names_its_key_its_kind_and_a_state():
    from silica.connectors import mcp_client

    configure(mail={}, remote={"type": "http", "url": "https://mcp.example.org/mcp", "command": None,
                               "args": None})
    rows = {r["key"]: r for r in _block().status()["rows"]}
    assert rows["mail"]["kind"] == "stdio" and rows["remote"]["kind"] == "http"
    mcp_client.CLIENTS.list_tools("mail")
    assert _row("mail")["state"] == f"connected · {len(mcp_client.cached_tools('mail'))} tools"


def test_a_missing_secret_reads_needs_and_its_field_writes_the_keyring():
    from silica.connectors import credentials

    configure(gh={"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    row = _row("gh")
    assert row["state"] == "needs GITHUB_TOKEN" and row["needs"] == ["GITHUB_TOKEN"]
    out = _block().act("secret", {"key": "gh", "name": "GITHUB_TOKEN", "value": "ghp_x"})
    assert out["ok"] and credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp_x"
    assert _row("gh")["state"] != "needs GITHUB_TOKEN"
    assert "ghp_x" not in json.dumps(_block().status())


def test_an_inline_secret_offers_move_to_keyring():
    configure(a={"env": {"API_KEY": "sk-live"}})
    assert _row("a")["state"] == "inline secret in env.API_KEY"
    assert _block().act("secure", {"key": "a"})["ok"]
    assert _row("a")["inline"] == []


def test_a_server_that_failed_says_why_scrubbed():
    from silica.connectors import mcp_client

    configure(bad={"command": "/nonexistent/mcp?token=SECRET9", "args": []})
    with pytest.raises(mcp_client.CallFailed):
        mcp_client.CLIENTS.list_tools("bad")
    state = _row("bad")["state"]
    assert state.startswith("failed: ") and "SECRET9" not in state


def test_add_merges_a_pasted_object_and_registers_its_tool():
    from silica.tools import TOOLS

    out = _block().act("add", {"json": json.dumps({"mcpServers": {"mail": {
        "command": sys.executable, "args": [FIXTURE]}}})})
    assert out["ok"] and out["added"] == ["mail"]
    assert "mcp__mail" in TOOLS
    assert "search_threads" in TOOLS["mcp__mail"].description  # listed once, before registering


def test_add_reports_what_it_refused():
    out = _block().act("add", {"json": "{nope"})
    assert not out["ok"] and "JSON" in out["error"]


def test_remove_drops_the_row_the_tool_and_the_secrets():
    from silica.connectors import credentials, mcp_tools
    from silica.tools import TOOLS

    _block().act("add", {"json": json.dumps({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp"}}})})
    mcp_tools.reload()
    assert _block().act("remove", {"key": "gh"})["ok"]
    assert "mcp__gh" not in TOOLS and _block().status()["rows"] == []
    assert credentials.get("mcp/gh", "GITHUB_TOKEN") is None


def test_reload_rereads_the_file():
    from silica.tools import TOOLS

    configure(mail={})
    assert _block().act("reload", {})["ok"]
    assert "mcp__mail" in TOOLS


def test_expanding_a_row_lists_its_tools_and_why_one_is_not_offered():
    from silica.connectors import mcp_client

    configure(mail={})
    mcp_client.CLIENTS.list_tools("mail")
    tools = {t["name"]: t["note"] for t in _row("mail")["tools"]}
    assert tools["search_threads"] == ""
    assert "nested $ref" in tools["nested_defs"]


def test_no_keyring_the_row_offers_the_file_and_then_says_so():
    import keyring
    import keyring.backends.fail

    keyring.set_keyring(keyring.backends.fail.Keyring())
    configure(gh={"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    assert _row("gh")["store"] == ""
    out = _block().act("secret", {"key": "gh", "name": "GITHUB_TOKEN", "value": "ghp"})
    assert not out["ok"] and "opt into" in out["error"]
    assert _block().act("store_in_file", {"key": "gh"})["ok"]
    assert _row("gh")["store"] == "file"
    assert _block().act("secret", {"key": "gh", "name": "GITHUB_TOKEN", "value": "ghp"})["ok"]


def test_hosted_the_block_is_local_only_and_refuses_every_action(monkeypatch):
    configure(mail={})
    monkeypatch.setenv("SILICA_HOSTED", "1")
    st = _block().status()
    assert st["state"] == "local-only" and st["rows"] == []
    out = _block().act("add", {"json": "{}"})
    assert not out["ok"] and "local only" in out["error"]


def test_an_unknown_action_is_refused():
    out = _block().act("explode", {})
    assert out == {"ok": False, "error": "unknown action: explode"}


# --- the routes --------------------------------------------------------------

@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def test_get_connectors_lists_every_block(client):
    names = [b["name"] for b in client.get("/connectors").json()]
    assert "mcp" in names


def test_post_runs_one_action_and_404s_an_unknown_block(client):
    configure(mail={})
    assert client.post("/connectors/mcp/reload", json={}).json()["ok"] is True
    assert client.post("/connectors/nope/reload", json={}).status_code == 404


def test_post_is_behind_the_same_origin_guard(client):
    r = client.post("/connectors/mcp/reload", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_approve_route_answers_a_pending_card(client):
    import threading

    from silica.connectors import approval

    seen = []
    verdicts = []
    t = threading.Thread(target=lambda: verdicts.append(approval.ask(
        server="mail", tool="send_message", args={"to": "a@b"}, progress=seen.append,
        cancel=None, budget=approval.Budget(5))))
    t.start()
    while not seen:
        pass
    r = client.post("/connectors/mcp/approve", json={"id": seen[0].id, "run": True})
    t.join(5)
    assert r.json()["ok"] is True and verdicts == ["run"]
    assert client.post("/connectors/mcp/approve", json={"id": seen[0].id, "run": True}).json()["ok"] is False


def test_stopping_the_turn_answers_every_open_card_no(client):
    import threading

    from silica.connectors import approval
    from silica.ui.web import server

    seen, verdicts = [], []
    cancel = threading.Event()
    server.current_cancel = cancel
    t = threading.Thread(target=lambda: verdicts.append(approval.ask(
        server="mail", tool="send_message", args={}, progress=seen.append,
        cancel=cancel, budget=approval.Budget(5))))
    t.start()
    while not seen:
        pass
    client.post("/stop")
    t.join(5)
    assert verdicts == ["no answer"]


def test_add_can_opt_into_the_file_for_the_pasted_servers():
    import keyring
    import keyring.backends.fail

    from silica.connectors import credentials

    keyring.set_keyring(keyring.backends.fail.Keyring())
    paste = json.dumps({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp"}}})
    refused = _block().act("add", {"json": paste})
    assert not refused["ok"] and refused.get("no_store") is True
    out = _block().act("add", {"json": paste, "store_in_file": True})
    assert out["ok"] and credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp"


def test_add_returns_each_refused_entry_beside_the_added_ones():
    out = _block().act("add", {"json": json.dumps({"mcpServers": {
        "ok": {"command": "x"}, "w": {"type": "ws", "url": "wss://x/ws"}}})})
    assert out["ok"] and out["added"] == ["ok"]
    assert len(out["refused"]) == 1 and "'w'" in out["refused"][0]
