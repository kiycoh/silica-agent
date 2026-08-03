"""`silica setup <client>`: merge, never clobber, and stay idempotent."""
from __future__ import annotations

import json
import tomllib

from silica.onboarding import setup_client


def test_codex_appends_block(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-5"\n', encoding="utf-8")
    assert setup_client.run_setup(["codex", "--config", str(cfg)]) == 0
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5"  # existing content survives
    assert parsed["mcp_servers"]["silica"]["command"] == "uvx"
    # The [mcp] extra is the point of the block, and rich markup eats it if the
    # payload is ever printed or written through a markup-enabled path.
    assert parsed["mcp_servers"]["silica"]["args"] == ["--from", "silica-agent[mcp]", "silica", "mcp"]


def test_no_client_gets_a_pinned_vault(tmp_path):
    """The generated config must not carry SILICA_VAULT.

    The server resolves the vault from the working directory its client spawns
    it in, so a pin written once at setup time would serve that one vault to
    every project the user ever opens.
    """
    toml_cfg = tmp_path / "config.toml"
    setup_client.run_setup(["codex", "--config", str(toml_cfg)])
    assert "SILICA_VAULT" not in toml_cfg.read_text(encoding="utf-8")

    json_cfg = tmp_path / "opencode.json"
    setup_client.run_setup(["opencode", "--config", str(json_cfg)])
    assert "SILICA_VAULT" not in json_cfg.read_text(encoding="utf-8")

    assert "SILICA_VAULT" not in _printed(["claude", "--dry-run"])


def test_codex_is_idempotent(tmp_path):
    cfg = tmp_path / "config.toml"
    assert setup_client.run_setup(["codex", "--config", str(cfg)]) == 0
    once = cfg.read_text(encoding="utf-8")
    assert setup_client.run_setup(["codex", "--config", str(cfg)]) == 0
    assert cfg.read_text(encoding="utf-8") == once


def test_codex_refuses_broken_toml(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is [not toml\n", encoding="utf-8")
    assert setup_client.run_setup(["codex", "--config", str(cfg)]) == 1
    assert cfg.read_text(encoding="utf-8") == "this is [not toml\n"


def test_opencode_merges_into_existing_json(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"theme": "dark", "mcp": {"other": {}}}), encoding="utf-8")
    assert setup_client.run_setup(["opencode", "--config", str(cfg)]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert "other" in data["mcp"]
    assert data["mcp"]["silica"]["command"][0] == "uvx"


def test_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "opencode.json"
    assert setup_client.run_setup(["opencode", "--config", str(cfg), "--dry-run"]) == 0
    assert not cfg.exists()


def test_backup_taken_before_write(tmp_path):
    cfg = tmp_path / "opencode.json"
    cfg.write_text('{"theme": "dark"}', encoding="utf-8")
    setup_client.run_setup(["opencode", "--config", str(cfg)])
    backups = list(tmp_path.glob("opencode.json.bak.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"theme": "dark"}


def _printed(args: list[str]) -> str:
    """What the user actually sees, newlines folded so rich's wrapping at the
    console width cannot be mistaken for a dropped token."""
    from silica.ui.console import CONSOLE
    with CONSOLE.capture() as cap:
        setup_client.run_setup(args)
    return " ".join(cap.get().split())


def test_previews_survive_rich_markup(tmp_path):
    """Every bracketed token rich could read as a style tag has to reach the
    terminal intact: a preview that drops `[mcp]` prints a command which
    installs the wrong package, and one that drops the TOML headers shows a
    block the writer never produces."""
    out = _printed(["codex", "--config", str(tmp_path / "config.toml"), "--dry-run"])
    assert "silica-agent[mcp]" in out
    assert "[mcp_servers.silica]" in out

    out = _printed(["opencode", "--config", str(tmp_path / "opencode.json"), "--dry-run"])
    assert "silica-agent[mcp]" in out

    assert "silica-agent[mcp]" in _printed(["claude", "--dry-run"])
    assert "[--dry-run]" in _printed(["nonsense"])


def test_claude_command_is_one_pastable_line():
    """A wrapped command pastes as fragments, so it must not be folded."""
    from silica.ui.console import CONSOLE
    with CONSOLE.capture() as cap:
        setup_client.run_setup(["claude", "--dry-run"])
    assert cap.get().strip().count("\n") == 0


def test_unknown_client_is_an_error(tmp_path):
    assert setup_client.run_setup(["cursor"]) == 1
    assert setup_client.run_setup([]) == 1
