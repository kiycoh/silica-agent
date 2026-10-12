"""~/.silica/mcp.json, in Claude Code's shape (ADR-0091 points 2, 3 and 8).

One key, `mcpServers`; stdio and HTTP entries; `${VAR}` expansion in the five
fields Claude Code expands; every secret in an entry is Class B and lives in
the keyring, never in os.environ."""
from __future__ import annotations

import json

import pytest


def _write(cfg) -> None:
    from silica.connectors import mcp_config

    mcp_config.path().parent.mkdir(parents=True, exist_ok=True)
    mcp_config.path().write_text(json.dumps(cfg), encoding="utf-8")


def _saved() -> dict:
    from silica.connectors import mcp_config

    return json.loads(mcp_config.path().read_text(encoding="utf-8"))["mcpServers"]


def test_the_file_lives_in_silica_home_not_in_a_vault():
    from silica.connectors import mcp_config
    from silica.kernel.recall import paths

    assert mcp_config.path() == paths._SILICA_HOME / "mcp.json"


def test_a_pasted_claude_code_file_loads_as_is():
    from silica.connectors import mcp_config

    _write({"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "server-fs", "${HOME}/docs"]},
        "remote": {"type": "http", "url": "https://mcp.example.org/mcp"},
        "typed": {"type": "stdio", "command": "uvx", "args": ["x"]},
    }})
    entries = mcp_config.load()
    assert {k: e.kind for k, e in entries.items()} == {"fs": "stdio", "remote": "http", "typed": "stdio"}
    assert all(not e.problem for e in entries.values())


@pytest.mark.parametrize("kind", ["sse", "ws"])
def test_sse_and_ws_entries_are_refused_by_name(kind):
    from silica.connectors import mcp_config

    _write({"mcpServers": {"old": {"type": kind, "url": "https://x.example/sse"}}})
    entry = mcp_config.load()["old"]
    assert "old" in entry.problem and kind in entry.problem


def test_a_url_without_a_type_is_a_configuration_error():
    from silica.connectors import mcp_config

    _write({"mcpServers": {"r": {"url": "https://x.example/mcp"}}})
    assert "type" in mcp_config.load()["r"].problem


@pytest.mark.parametrize("name,secret", [
    ("GITHUB_TOKEN", True), ("API_KEY", True), ("OPENAI_APIKEY", True), ("DB_PASSWORD", True),
    ("Authorization_Bearer", True), ("SESSION_ID", True), ("MY_COOKIE", True),
    ("HOME", False), ("KEYBOARD_LAYOUT", False), ("PATH", False), ("LOG_LEVEL", False),
])
def test_a_secret_is_known_by_the_words_scrub_credentials_treats_as_secret(name, secret):
    from silica.connectors import mcp_config

    assert mcp_config.is_secret_name(name) is secret


def test_plain_references_expand_from_the_environment_as_in_claude_code(monkeypatch):
    from silica.connectors import mcp_config

    monkeypatch.setenv("DOCS_DIR", "/srv/docs")
    _write({"mcpServers": {"fs": {"command": "server-fs", "args": ["${DOCS_DIR}", "${MISSING:-/tmp}"],
                                  "env": {"LOG_LEVEL": "${LEVEL:-info}"}}}})
    spec = mcp_config.resolve(mcp_config.load()["fs"])
    assert spec["args"] == ["/srv/docs", "/tmp"]
    assert spec["env"] == {"LOG_LEVEL": "info"}


def test_a_secret_named_reference_never_reads_the_environment(monkeypatch):
    """A pasted Claude Code file says ${GITHUB_TOKEN}; the shell may well hold
    one, and taking it would put a Class B secret through os.environ."""
    from silica.connectors import mcp_config

    monkeypatch.setenv("GITHUB_TOKEN", "from-the-shell")
    _write({"mcpServers": {"gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}})
    entry = mcp_config.load()["gh"]
    assert entry.needs == ["GITHUB_TOKEN"] and not entry.problem
    with pytest.raises(mcp_config.Unresolved, match="needs GITHUB_TOKEN"):
        mcp_config.resolve(entry)
    mcp_config.set_secret("gh", "GITHUB_TOKEN", "from-the-keyring")
    entry = mcp_config.load()["gh"]
    assert entry.needs == []
    assert mcp_config.resolve(entry)["env"] == {"GITHUB_TOKEN": "from-the-keyring"}


def test_a_reference_inside_a_header_is_a_secret_whatever_its_name(monkeypatch):
    from silica.connectors import mcp_config

    monkeypatch.setenv("ACME", "from-the-shell")
    _write({"mcpServers": {"r": {"type": "http", "url": "https://x.example/mcp",
                                 "headers": {"X-Acme": "${ACME}"}}}})
    assert mcp_config.load()["r"].needs == ["ACME"]


def test_an_inline_secret_found_at_load_keeps_the_server_from_starting():
    from silica.connectors import mcp_config

    _write({"mcpServers": {
        "a": {"command": "x", "env": {"API_KEY": "sk-live-123", "LOG_LEVEL": "debug"}},
        "b": {"type": "http", "url": "https://x.example/mcp",
              "headers": {"Authorization": "Bearer abc"}},
    }})
    entries = mcp_config.load()
    assert entries["a"].inline == ["env.API_KEY"]
    assert entries["b"].inline == ["headers.Authorization"]
    with pytest.raises(mcp_config.Unresolved, match="inline secret in env.API_KEY"):
        mcp_config.resolve(entries["a"])


def test_add_moves_a_pasted_literal_to_the_keyring_and_keeps_a_reference(_no_real_keyring):
    from silica.connectors import credentials, mcp_config

    added, _refused = mcp_config.add(json.dumps({"mcpServers": {
        "gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp_live", "LOG_LEVEL": "info"}},
        "r": {"type": "http", "url": "https://x.example/mcp", "headers": {"Authorization": "Bearer abc"}},
    }}))
    assert added == ["gh", "r"]
    saved = _saved()
    assert saved["gh"]["env"] == {"GITHUB_TOKEN": "${keyring:GITHUB_TOKEN}", "LOG_LEVEL": "info"}
    assert saved["r"]["headers"] == {"Authorization": "${keyring:Authorization}"}
    assert "ghp_live" not in mcp_config.path().read_text() and "abc" not in mcp_config.path().read_text()
    assert credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp_live"
    assert mcp_config.resolve(mcp_config.load()["r"])["headers"] == {"Authorization": "Bearer abc"}


def test_add_takes_one_named_entry_and_merges_into_the_file():
    from silica.connectors import mcp_config

    mcp_config.add(json.dumps({"mcpServers": {"a": {"command": "a"}}}))
    mcp_config.add(json.dumps({"b": {"command": "b", "args": ["--x"]}}))
    assert set(_saved()) == {"a", "b"}


def test_add_refuses_what_it_cannot_take():
    from silica.connectors import mcp_config

    for bad, why in [("{not json", "JSON"), (json.dumps({"command": "x"}), "name"),
                     (json.dumps({"s": {"type": "sse", "url": "https://x/sse"}}), "sse"),
                     (json.dumps({"s": {"url": "https://x/mcp"}}), "type")]:
        with pytest.raises(mcp_config.ConfigError, match=why):
            mcp_config.add(bad)
    assert not mcp_config.path().exists()


def test_without_a_store_add_refuses_and_writes_nothing(monkeypatch):
    import keyring
    import keyring.backends.fail

    from silica.connectors import credentials, mcp_config

    keyring.set_keyring(keyring.backends.fail.Keyring())
    with pytest.raises(credentials.NoStore):
        mcp_config.add(json.dumps({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp"}}}))
    assert not mcp_config.path().exists()


def test_secure_moves_a_hand_edited_literal_to_the_keyring():
    from silica.connectors import credentials, mcp_config

    _write({"mcpServers": {"a": {"command": "x", "env": {"API_KEY": "sk-1"}}}})
    assert mcp_config.secure("a") == ["env.API_KEY"]
    assert _saved()["a"]["env"] == {"API_KEY": "${keyring:API_KEY}"}
    assert credentials.get("mcp/a", "API_KEY") == "sk-1"
    assert mcp_config.load()["a"].inline == []


def test_remove_deletes_the_entry_and_its_keyring_secrets():
    from silica.connectors import credentials, mcp_config

    mcp_config.add(json.dumps({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp", "X": "${OTHER_TOKEN}"}}}))
    mcp_config.set_secret("gh", "OTHER_TOKEN", "o")
    credentials.put("mcp/gh", "oauth_tokens", "{}")
    mcp_config.remove("gh")
    assert "gh" not in _saved()
    for field in ("GITHUB_TOKEN", "OTHER_TOKEN", "oauth_tokens"):
        assert credentials.get("mcp/gh", field) is None


def test_the_tool_name_is_safe_for_every_provider():
    from silica.connectors import mcp_config

    assert mcp_config.tool_name("github") == "mcp__github"
    assert mcp_config.tool_name("my server/1") == "mcp__my_server_1"
    assert len(mcp_config.tool_name("k" * 200)) == 64


def test_hosted_reads_no_file_at_all(monkeypatch):
    from silica.connectors import mcp_config

    mcp_config.path().parent.mkdir(parents=True, exist_ok=True)
    mcp_config.path().write_text("{not even json", encoding="utf-8")
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert mcp_config.load() == {}
    with pytest.raises(mcp_config.ConfigError, match="local only"):
        mcp_config.add(json.dumps({"a": {"command": "x"}}))


# --- the e2e tester's defects (2026-10-12) -----------------------------------

def _fields(keyring_backend, key: str) -> set[str]:
    """Every field the in-memory keyring holds under one server's scope."""
    prefix = f"mcp/{key}/"
    return {u[len(prefix):] for (s, u) in keyring_backend.data if s == "silica" and u.startswith(prefix)}


def test_re_adding_a_key_drops_the_secrets_its_old_entry_kept(_no_real_keyring):
    """Add fx2 with API_KEY, re-add fx2 with OTHER_TOKEN, remove it: before
    the fix mcp/fx2/API_KEY stayed in the keyring with nothing naming it."""
    from silica.connectors import mcp_config

    mcp_config.add(json.dumps({"fx2": {"command": "x", "env": {"API_KEY": "first-secret-111"}}}))
    mcp_config.add(json.dumps({"fx2": {"command": "x", "env": {"OTHER_TOKEN": "second-222"}}}))
    assert _fields(_no_real_keyring, "fx2") == {"OTHER_TOKEN"}
    mcp_config.remove("fx2")
    assert _fields(_no_real_keyring, "fx2") == set()


def test_re_adding_a_key_keeps_a_secret_the_new_entry_still_reads(_no_real_keyring):
    from silica.connectors import credentials, mcp_config

    mcp_config.add(json.dumps({"gh": {"command": "x", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}))
    mcp_config.set_secret("gh", "GITHUB_TOKEN", "ghp-kept")
    mcp_config.add(json.dumps({"gh": {"command": "y", "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}}}))
    assert credentials.get("mcp/gh", "GITHUB_TOKEN") == "ghp-kept"


def test_re_adding_an_http_key_at_another_url_drops_its_login(_no_real_keyring):
    """A grant is for the server that issued it: presented to a new URL it
    would hand that server a bearer token for the old one."""
    from silica.connectors import credentials, mcp_config

    mcp_config.add(json.dumps({"r": {"type": "http", "url": "https://a.example/mcp"}}))
    credentials.put("mcp/r", "oauth_tokens", "{}")
    credentials.put("mcp/r", "oauth_client", "{}")
    mcp_config.add(json.dumps({"r": {"type": "http", "url": "https://a.example/mcp", "headers": {}}}))
    assert credentials.get("mcp/r", "oauth_tokens") == "{}"  # same server: still logged in
    mcp_config.add(json.dumps({"r": {"type": "http", "url": "https://b.example/mcp"}}))
    assert _fields(_no_real_keyring, "r") == set()


def test_one_sse_or_ws_entry_does_not_refuse_the_whole_paste():
    from silica.connectors import mcp_config

    added, refused = mcp_config.add(json.dumps({"mcpServers": {
        "ok": {"command": "x"},
        "old": {"type": "sse", "url": "https://x.example/sse"},
        "w": {"type": "ws", "url": "wss://x.example/ws"},
    }}))
    assert added == ["ok"]
    assert set(_saved()) == {"ok"}
    assert len(refused) == 2
    assert "'old'" in refused[0] and "sse" in refused[0]
    assert "'w'" in refused[1] and "ws" in refused[1]
    assert mcp_config.paste_keys(json.dumps({"ok": {"command": "x"},
                                             "old": {"type": "sse", "url": "u"}})) == ["ok"]


def test_a_paste_with_no_usable_entry_names_every_refusal():
    from silica.connectors import mcp_config

    with pytest.raises(mcp_config.ConfigError) as err:
        mcp_config.add(json.dumps({"a": {"type": "sse", "url": "u"}, "b": {"type": "ws", "url": "u"}}))
    assert "'a'" in str(err.value) and "'b'" in str(err.value)
    assert not mcp_config.path().exists()


def test_a_defaulted_header_variable_with_a_plain_name_expands_as_in_claude_code(monkeypatch):
    """`"X-Trace": "${TRACE_ID:-none}"` read "needs TRACE_ID" before the fix."""
    from silica.connectors import mcp_config

    monkeypatch.delenv("TRACE_ID", raising=False)
    _write({"mcpServers": {"r": {"type": "http", "url": "https://x.example/mcp",
                                 "headers": {"X-Trace": "${TRACE_ID:-none}"}}}})
    entry = mcp_config.load()["r"]
    assert entry.needs == [] and not entry.problem
    assert mcp_config.resolve(entry)["headers"] == {"X-Trace": "none"}
    monkeypatch.setenv("TRACE_ID", "t-42")
    assert mcp_config.resolve(mcp_config.load()["r"])["headers"] == {"X-Trace": "t-42"}
    assert mcp_config.secret_fields("r", _saved()["r"]) == []


def test_a_defaulted_header_variable_with_a_secret_name_still_reads_the_keyring(monkeypatch):
    """ADR-0091 point 3's name rule holds with a default too: a secret-shaped
    name never reads os.environ, whatever follows the `:-`."""
    from silica.connectors import mcp_config

    monkeypatch.setenv("API_TOKEN", "from-the-shell")
    _write({"mcpServers": {"r": {"type": "http", "url": "https://x.example/mcp",
                                 "headers": {"Authorization": "Bearer ${API_TOKEN:-none}"}}}})
    assert mcp_config.load()["r"].needs == ["API_TOKEN"]
