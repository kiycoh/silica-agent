"""`/connect` (ADR-0091, "GUI and TUI"): the MCP block's actions from the
REPL. No secret is typed on the REPL line: a missing one is asked in a hidden
prompt (ADR-0089 point 7)."""
from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from tests.mcp_helpers import configure


@pytest.fixture(autouse=True)
def _clean():
    yield
    from silica.connectors import mcp_client, mcp_tools

    mcp_tools.unregister()
    mcp_client.CLIENTS.close_all()


def _run(line, **kw) -> str:
    from silica.connectors import connect_cli

    buf = io.StringIO()
    connect_cli.run(line, Console(file=buf, width=200, color_system=None), **kw)
    return buf.getvalue()


def test_the_command_is_registered_as_the_adr_writes_it():
    from silica.ui.commands import COMMANDS

    cmd = next(c for c in COMMANDS if c.name == "/connect")
    assert cmd.group == "direct"
    assert cmd.usage == "[list|add <json>|secure <key>|login <key>|logout <key>|remove <key>|reload]"
    assert cmd.examples == ("/connect", "/connect secure github", "/connect login gmail")


def test_the_repl_dispatches_it_without_a_model_round_trip(monkeypatch):
    from silica import cli
    from silica.connectors import connect_cli

    seen = []
    monkeypatch.setattr(connect_cli, "run", lambda line, console, **kw: seen.append(line))
    assert cli._handle_direct_shortcut("/connect secure github", []) is True
    assert seen == ["/connect secure github"]


def test_bare_connect_lists_each_server_with_its_state():
    assert "no MCP server configured" in _run("/connect")
    configure(mail={}, gh={"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    out = _run("/connect")
    assert "mail" in out and "stdio" in out and "not connected yet" in out
    assert "gh" in out and "needs GITHUB_TOKEN" in out


def test_add_takes_the_rest_of_the_line_as_json():
    from silica.connectors import mcp_config

    out = _run('/connect add {"remote": {"type": "http", "url": "https://mcp.example.org/mcp"}}')
    assert "added remote" in out
    assert "remote" in mcp_config.load()


def test_add_says_what_it_refused():
    assert "not valid JSON" in _run("/connect add {nope")


def test_secure_moves_inline_secrets_and_asks_for_missing_ones_hidden():
    from silica.connectors import credentials

    configure(gh={"env": {"API_KEY": "sk-1", "GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    asked = []
    out = _run("/connect secure gh", ask_secret=lambda prompt: asked.append(prompt) or "ghp_typed")
    assert "moved env.API_KEY" in out
    assert asked == ["GITHUB_TOKEN for gh: "]
    assert credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp_typed"
    assert "ghp_typed" not in out


def test_without_a_terminal_a_secret_is_never_asked_on_the_line():
    configure(gh={"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    out = _run("/connect secure gh")
    assert "Settings › Connectors" in out


def test_no_keyring_secure_offers_the_file_first():
    import keyring
    import keyring.backends.fail

    from silica.connectors import credentials

    keyring.set_keyring(keyring.backends.fail.Keyring())
    configure(gh={"env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}})
    answers = iter(["y", "ghp_file"])
    out = _run("/connect secure gh", ask_secret=lambda prompt: next(answers),
               ask_yes=lambda prompt: next(answers) == "y")
    assert "stored in a file" in out
    assert credentials.where("mcp/gh") == "file"
    assert credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp_file"


def test_remove_and_reload():
    from silica.connectors import mcp_config
    from silica.tools import TOOLS

    configure(mail={}, other={})
    assert "mcp__mail" in _run("/connect reload")
    assert "mcp__mail" in TOOLS
    assert "removed other" in _run("/connect remove other")
    assert "other" not in mcp_config.load()


def test_login_refuses_a_stdio_server():
    configure(mail={})
    assert "only http servers log in" in _run("/connect login mail")


def test_login_prints_the_authorization_url_for_a_terminal_without_a_browser(monkeypatch):
    from silica.connectors import mcp_oauth

    monkeypatch.setattr(mcp_oauth, "login", lambda key, **kw: {"url": "https://auth.example/authorize?x=1",
                                                               "connected": False})
    states = iter([{"done": False}, {"done": True, "error": ""}])
    monkeypatch.setattr(mcp_oauth, "login_state", lambda key: next(states))
    configure(remote={"type": "http", "url": "https://mcp.example.org/mcp", "command": None, "args": None})
    out = _run("/connect login remote", poll_s=0)
    assert "https://auth.example/authorize?x=1" in out
    assert "connected" in out


def test_unknown_subcommand_shows_the_usage():
    assert "usage: /connect" in _run("/connect frobnicate")


def test_hosted_answers_local_only(monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert "connectors are local only" in _run("/connect")
    assert "connectors are local only" in _run('/connect add {"a": {"command": "x"}}')


def test_list_never_prints_a_secret():
    from silica.connectors import find, mcp_config

    configure(gh={"env": {"GITHUB_TOKEN": "${keyring:GITHUB_TOKEN}"}})
    mcp_config.set_secret("gh", "GITHUB_TOKEN", "ghp_never_shown")
    assert "ghp_never_shown" not in _run("/connect")
    assert "ghp_never_shown" not in json.dumps(find("mcp").status())


def test_no_keyring_add_offers_the_file_and_then_moves_the_pasted_secret():
    """Without a keyring, Add refused and Secure needs the entry Add refused
    to write: the opt-in has to be offered where the secret arrives."""
    import keyring
    import keyring.backends.fail

    from silica.connectors import credentials, mcp_config

    keyring.set_keyring(keyring.backends.fail.Keyring())
    out = _run('/connect add {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_pasted"}}}',
               ask_yes=lambda prompt: True)
    assert "added gh" in out and "ghp_pasted" not in out
    assert credentials.where("mcp/gh") == "file"
    assert credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp_pasted"
    assert mcp_config.read_raw()["gh"]["env"] == {"GITHUB_TOKEN": "${keyring:GITHUB_TOKEN}"}


def test_no_keyring_add_declined_writes_nothing():
    import keyring
    import keyring.backends.fail

    from silica.connectors import mcp_config

    keyring.set_keyring(keyring.backends.fail.Keyring())
    out = _run('/connect add {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp"}}}',
               ask_yes=lambda prompt: False)
    assert "nothing added" in out
    assert "gh" not in mcp_config.load()


def test_login_that_needs_a_client_id_asks_for_one_and_retries(monkeypatch):
    from silica.connectors import mcp_client, mcp_config, mcp_oauth

    calls = []

    def login(key, **kw):
        calls.append(key)
        if len(calls) == 1:
            raise mcp_client.CallFailed("needs a client ID: Registration failed: 404 Not Found")
        return {"url": "", "connected": True}
    monkeypatch.setattr(mcp_oauth, "login", login)
    configure(remote={"type": "http", "url": "https://mcp.example.org/mcp", "command": None, "args": None})
    out = _run("/connect login remote", ask_line=lambda prompt: "hand-registered-7", poll_s=0)
    assert "needs a client ID" in out and "remote: connected" in out
    assert mcp_config.read_raw()["remote"]["oauth"]["clientId"] == "hand-registered-7"
    assert len(calls) == 2


def test_login_without_a_store_offers_the_file_and_retries(monkeypatch):
    import keyring
    import keyring.backends.fail

    from silica.connectors import credentials, mcp_client, mcp_oauth

    keyring.set_keyring(keyring.backends.fail.Keyring())
    calls = []

    def login(key, **kw):
        calls.append(key)
        if len(calls) == 1:
            raise mcp_client.CallFailed("needs a credentials store: no keyring backend on this machine")
        return {"url": "", "connected": True}
    monkeypatch.setattr(mcp_oauth, "login", login)
    configure(remote={"type": "http", "url": "https://mcp.example.org/mcp", "command": None, "args": None})
    out = _run("/connect login remote", ask_yes=lambda prompt: True, poll_s=0)
    assert credentials.where("mcp/remote") == "file" and "remote: connected" in out


def test_add_takes_the_good_entries_and_names_each_refused_one():
    """One `sse` entry beside a good one refused the whole paste (tester,
    2026-10-12): the good one is added, the refused one named with why."""
    from silica.connectors import mcp_config

    out = _run('/connect add {"ok": {"command": "x"}, "old": {"type": "sse", "url": "https://x/sse"}}')
    assert "added ok" in out
    assert "not added: entry 'old'" in out and "sse" in out
    assert set(mcp_config.load()) == {"ok"}
