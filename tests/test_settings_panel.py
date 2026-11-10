"""Settings panel — the seams that fail silently if they break.

Ponytail: one check per contract from docs/specs/settings-panel.md §11. The
panel's own rendering is not tested here (that is a browser's job); what is
tested is everything that decides WHERE a value lands, whether it is allowed to
land at all, and whether a secret can escape.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from silica.config import CONFIG  # noqa: E402
from silica.kernel.base.user_env import merge_env, resolve_env_path  # noqa: E402
from silica.onboarding import settings_ops
from silica.ui.web import settings as st  # noqa: E402


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point every write at a throwaway .env — never the developer's own."""
    path = tmp_path / ".env"
    monkeypatch.setattr("silica.kernel.base.user_env.resolve_env_path", lambda: path)
    return path


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    monkeypatch.setattr(SESSION, "busy", False)
    return TestClient(server.app), server


def test_merge_env_writes_many_keys_and_keeps_the_rest(env_file):
    existing = "# a comment\nSILICA_MODEL=old\nUNRELATED=keepme\n"
    out = merge_env(existing, {"SILICA_MODEL": "new", "SILICA_PROVIDER": "openrouter"})
    assert "# a comment" in out
    assert "UNRELATED=keepme" in out
    assert "SILICA_MODEL=new" in out
    assert "SILICA_MODEL=old" not in out
    assert "SILICA_PROVIDER=openrouter" in out


def test_the_env_holds_keys_so_only_its_owner_reads_it(env_file):
    """A .env the write creates, and one an older release left group-readable,
    both end 0600: the file holds every API key the user has."""
    import os

    from silica.kernel.base.user_env import write_env

    write_env({"SILICA_MODEL": "a"})
    assert os.stat(env_file).st_mode & 0o777 == 0o600
    os.chmod(env_file, 0o664)
    write_env({"SILICA_MODEL": "b"})
    assert os.stat(env_file).st_mode & 0o777 == 0o600


def test_env_path_is_the_user_file_even_inside_a_repo(tmp_path, monkeypatch):
    """The file a write lands in is the one that would win at the next boot, and
    config.py layers only the user-level file. A .env in the working directory
    or at the repo root must not attract the write: nothing reads it back."""
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    user_env = tmp_path / "home" / ".silica" / ".env"
    monkeypatch.setattr("silica.kernel.base.user_env.USER_ENV", user_env)
    monkeypatch.chdir(sub)
    (repo / ".env").write_text("X=1")
    (sub / ".env").write_text("X=2")

    assert resolve_env_path() == user_env


def test_a_write_is_refused_while_a_turn_runs(client, env_file, monkeypatch):
    from silica.ui.web.session import SESSION
    c, server = client
    monkeypatch.setattr(SESSION, "busy", True)
    r = c.post("/settings", json={"key": "SILICA_INBOX_DIR", "value": "Nope"})
    assert r.status_code == 409
    assert not env_file.exists()  # refused means nothing was written


def test_an_exported_key_is_locked_and_unwritable(client, env_file, monkeypatch):
    c, _server = client
    monkeypatch.setattr(st, "SHELL_ENV", frozenset({"SILICA_INBOX_DIR"}))
    rows = {
        row["key"]: row
        for section in st.read_sections(probe=False)
        for row in section["rows"]
    }
    assert rows["SILICA_INBOX_DIR"]["locked"] is True
    assert rows["SILICA_INBOX_DIR"]["origin"] == "env"
    assert rows["SILICA_GIT_COMMIT"]["locked"] is False

    r = c.post("/settings", json={"key": "SILICA_INBOX_DIR", "value": "Nope"})
    assert r.status_code == 409
    assert not env_file.exists()


_KEY = "sk-fake-S3-key-0123456789"


def test_get_settings_never_carries_a_key(client, env_file, monkeypatch):
    """Audit 2026-10-09, S3: only the browser masked a key, so the key itself
    went to any page that could read GET /settings."""
    c, _server = client
    monkeypatch.setattr(CONFIG, "provider_api_key", _KEY)
    body = c.get("/settings").text
    assert _KEY not in body
    assert "6789" in body  # the last four still say which key is set


def test_a_rebound_host_is_refused_before_routing(client, env_file):
    """A DNS-rebinding page sets Origin and Host to its own name, so the origin
    check passes it; the Host allow-list is what refuses it (S3)."""
    _c, server = client
    rebound = TestClient(server.app, base_url="http://attacker.example:8765")
    h = {"Origin": "http://attacker.example:8765"}
    assert rebound.get("/settings", headers=h).status_code == 403
    post = rebound.post("/settings", headers=h, json={
        "key": "SILICA_PROVIDER_BASE_URL", "value": "http://attacker.example:9/v1"})
    assert post.status_code == 403
    assert not env_file.exists()


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_loopback_names_are_served(client, host):
    c, _server = client
    assert c.get("/health", headers={"host": host}).status_code == 200


def test_changing_the_provider_writes_its_whole_group_at_once(env_file, monkeypatch):
    """No intermediate state where the provider is new and the model is old:
    both move in one merge_env, so there is no instant to observe them apart."""
    from silica.kernel.base import user_env

    writes = []
    real_write = user_env.write_env
    monkeypatch.setattr(user_env, "write_env", lambda u: (writes.append(dict(u)), real_write(u))[1])
    monkeypatch.setattr(CONFIG, "model", "qwen3-32b")
    monkeypatch.setattr(CONFIG, "_provider", "lmstudio")
    # apply() makes the keys live by writing os.environ; declare them here so
    # monkeypatch restores the real environment after the test.
    for key in ("SILICA_MODEL", "SILICA_PROVIDER", "SILICA_PROVIDER_BASE_URL"):
        monkeypatch.setenv(key, "")

    result = st.apply("SILICA_PROVIDER", "openrouter")

    assert result["ok"], result
    assert len(writes) == 1, "the group must be one write, not three"
    assert set(writes[0]) == {"SILICA_MODEL", "SILICA_PROVIDER", "SILICA_PROVIDER_BASE_URL"}
    assert CONFIG.provider == "openrouter"
    assert CONFIG.model.startswith("openrouter/")
    text = env_file.read_text()
    assert "SILICA_PROVIDER=openrouter" in text
    assert "SILICA_MODEL=openrouter/" in text


def test_a_live_value_is_the_one_the_next_boot_reads(env_file, monkeypatch):
    """The panel sets a field through its settings row's type: a name typed
    with spaces was set with them, and stamped on the page, until a restart
    read it through str.strip."""
    from silica.config import SETTINGS

    monkeypatch.setattr(CONFIG, "user_name", CONFIG.user_name)
    monkeypatch.setattr(CONFIG, "max_context_tokens", CONFIG.max_context_tokens)
    monkeypatch.setenv("SILICA_USER_NAME", "")   # apply() writes os.environ: restored after
    monkeypatch.setenv("SILICA_MAX_CONTEXT", "")

    assert st.apply("SILICA_USER_NAME", "  Ada  ")["ok"]
    assert CONFIG.user_name == "Ada" == SETTINGS["SILICA_USER_NAME"].read()
    assert st.apply("SILICA_MAX_CONTEXT", " 42000 ")["ok"]
    assert CONFIG.max_context_tokens == 42000


def test_adopt_vault_rebuilds_the_driver_on_the_new_path(tmp_vault, tmp_path):
    """The nine steps as one call: a live process holds a driver, an overlay, a
    manifest and vault-scoped caches, and they all still answer for the old
    folder until they are reset."""
    from silica.onboarding.adopt import adopt_vault
    from silica.driver import get_driver

    other = tmp_path / "other-vault"
    other.mkdir()
    get_driver()  # build one against the old vault first

    result = adopt_vault(str(other))

    assert result.error is None
    assert result.vault == str(other.resolve())
    assert CONFIG.vault_path == str(other.resolve())
    assert get_driver().vault_path == other.resolve()


def test_adopt_vault_refuses_a_path_that_is_not_a_directory(tmp_vault, tmp_path):
    from silica.onboarding.adopt import adopt_vault

    file_path = tmp_path / "notes.md"
    file_path.write_text("x")
    before = CONFIG.vault_path

    result = adopt_vault(str(file_path))

    assert result.error and "not a directory" in result.error
    assert CONFIG.vault_path == before  # nothing moved


def test_safe_mode_round_trips_through_vault_yaml_not_the_env(tmp_vault, env_file):
    """The one row whose store is the manifest. Off then on re-derives the
    boundary from the vault's content, so there is no remembered value to rot."""
    from pathlib import Path

    from silica.kernel.base.manifest import reset_manifest_cache
    from silica.ui.web import settings as st

    root = Path(CONFIG.vault_path)
    (root / "vault.yaml").write_text("write_dir: silica\n", encoding="utf-8")
    reset_manifest_cache()
    row = next(r for rows in st.sections().values() for r in rows
               if r.key == settings_ops.SAFE_MODE_KEY)
    assert st._value(row) == "true"

    assert st.apply(settings_ops.SAFE_MODE_KEY, "false")["ok"] is True
    assert st._value(row) == "false"
    assert 'write_dir: ""' in (root / "vault.yaml").read_text(encoding="utf-8")

    result = st.apply(settings_ops.SAFE_MODE_KEY, "true")
    assert result["values"] == {"safe_mode": "true", "write_dir": "silica"}
    assert st._value(row) == "true"
    assert not env_file.exists()  # never an env var


def test_the_bug_payload_carries_no_api_key(tmp_vault, monkeypatch):
    """The report is built server-side precisely so that nothing which can see a
    key ever builds it — a public issue is the wrong place for one."""
    secret = "sk-or-v1-THIS-MUST-NEVER-APPEAR"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.setattr(CONFIG, "provider_api_key", secret)
    monkeypatch.setattr(CONFIG, "tavily_api_key", secret)

    payload = settings_ops.bug_report(st.reportable())["payload"]

    assert secret not in payload
    assert "silica" in payload  # it still says something useful


def test_vault_switch_is_live_only_and_never_persists(client, env_file, tmp_path):
    """A SILICA_VAULT line in the .env is ignored at boot (config.load_user_env),
    so persisting the switch would write a value nothing reads back and the
    panel would show a "file" origin for a vault the next launch will not use."""
    from silica.ui.web import settings_routes
    _, server = client
    other = tmp_path / "other-vault"
    other.mkdir()
    out = settings_routes._apply_vault_switch(str(other))
    assert out["ok"] and out["vault"] == str(other.resolve())
    assert CONFIG.vault_path == str(other.resolve())
    assert not env_file.exists() or "SILICA_VAULT" not in env_file.read_text()


def test_a_vault_switch_keeps_the_changes_of_the_vault_it_leaves(client, env_file, tmp_path):
    """The ledger is keyed by vault so a switch needs nothing from it; clearing it on
    a switch emptied A's Changes list after A, then B, then A, which /vault in the
    REPL never did."""
    from silica.kernel.write import session_changes
    from silica.ui.web import settings_routes

    first = CONFIG.vault_path
    session_changes.touched("Kept.md", "before")
    other = tmp_path / "other-vault"
    other.mkdir()
    assert settings_routes._apply_vault_switch(str(other))["ok"]
    assert session_changes.snapshot() == {}
    assert settings_routes._apply_vault_switch(first)["ok"]
    assert list(session_changes.snapshot()) == ["Kept.md"]


def test_vault_origin_is_never_the_file():
    assert st._origin(st.VAULT_KEY, {st.VAULT_KEY}) == "derived"


# --- hosted (ADR-0104) ---------------------------------------------------------
# Spelled out here rather than read off HOSTED_ROWS, so a key added to the
# tuple by accident fails a test instead of reaching every tenant.
_HOSTED_TABLE = {
    "Session": ["SILICA_MODEL", "context", "SILICA_SHOW_THINKING"],
    "Language": ["SILICA_STT_LANG", "SILICA_STT_BROWSER"],
    # the name the new chat greets with is the tenant's own to set
    "Display": ["SILICA_USER_NAME", "SILICA_THEME", "SILICA_SHOW_THINKING",
                "SILICA_GRAPH_PARTICLES", "SILICA_GRAPH_SHADING"],
    "Memory": ["SILICA_MEMORY"],
}


@pytest.fixture
def hosted_client(client, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    # Nothing exported: the refusals below must come from the table, not from
    # a lock that a host.env missing the line would not give.
    monkeypatch.setattr(st, "SHELL_ENV", frozenset())
    return client


def test_hosted_get_lists_exactly_the_table(hosted_client, env_file):
    c, _server = hosted_client
    body = c.get("/settings").json()
    assert body["hosted"] is True and body["behind"] == 0
    assert {s["name"]: [r["key"] for r in s["rows"]] for s in body["sections"]} == _HOSTED_TABLE
    rows = {r["key"]: r for s in body["sections"] for r in s["rows"]}
    assert rows["SILICA_MODEL"]["kind"] == "readonly"
    assert rows["SILICA_STT_BROWSER"]["options"] == ["off", "on-device"]
    assert rows["context"]["value"] == f"{CONFIG.max_context_tokens:,} tokens"


def test_hosted_refuses_every_write_outside_the_table(hosted_client, env_file):
    c, _server = hosted_client
    for key, value in (("SILICA_PROVIDER_API_KEY", "sk-x"),
                       ("SILICA_STT_BROWSER", "cloud"),
                       ("SILICA_MODEL", "openai/gpt-5"),
                       ("SILICA_INBOX_DIR", "elsewhere")):
        assert c.post("/settings", json={"key": key, "value": value}).status_code == 400, key
    confirm = c.post("/settings/confirm", json={"key": "SILICA_VAULT", "value": "/"})
    assert confirm.status_code == 404
    assert not env_file.exists()


def test_hosted_writes_a_table_row_to_the_tenant_env(hosted_client, env_file, monkeypatch):
    c, _server = hosted_client
    # The write is live too: both are put back after the test.
    monkeypatch.setenv("SILICA_STT_LANG", "auto")
    monkeypatch.setattr(CONFIG, "stt_lang", CONFIG.stt_lang)
    assert c.post("/settings", json={"key": "SILICA_STT_LANG", "value": "it"}).status_code == 200
    assert "SILICA_STT_LANG=it" in env_file.read_text()


def test_a_model_changed_in_settings_brings_its_window(client, env_file, monkeypatch):
    """The window was sized at boot alone, so the ring and the compaction budget
    went on reading the last model's until a restart. Under a pin, from the pin:
    the last model may have cut the field below it."""
    from silica.config import CONFIG

    tc, _ = client
    monkeypatch.setattr(CONFIG, "model", "openrouter/x/narrow")
    monkeypatch.setattr(CONFIG, "_provider", "openrouter")
    monkeypatch.setattr(CONFIG, "max_context_tokens", 60000)
    monkeypatch.setattr("silica.model.limits.model_limits", lambda provider, model: (131072, 0))
    monkeypatch.setenv("SILICA_MODEL", "")   # apply() writes os.environ: monkeypatch restores what it declared
    monkeypatch.delenv("SILICA_MAX_CONTEXT", raising=False)
    assert tc.post("/settings", json={"key": "SILICA_MODEL", "value": "openrouter/x/wide"}).status_code == 200
    assert CONFIG.max_context_tokens == 131072

    monkeypatch.setenv("SILICA_MAX_CONTEXT", "65536")
    monkeypatch.setattr(CONFIG, "max_context_tokens", 8192)   # what a narrow model left
    assert tc.post("/settings", json={"key": "SILICA_MODEL", "value": "openrouter/x/other"}).status_code == 200
    assert CONFIG.max_context_tokens == 65536
