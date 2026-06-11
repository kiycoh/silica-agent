# tests/test_vault_codebase_switch.py
"""Codebase-aware /vault switching: when the target is a git repo, the vault
is the repo's .silica/ (created on demand) rather than the repo root itself."""
import subprocess

import silica.driver as driver_pkg
from silica.cli import resolve_vault_switch, _handle_direct_shortcut
from silica.config import CONFIG


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_codebase_missing_silica_is_created(tmp_path):
    _git_init(tmp_path)

    target = resolve_vault_switch(str(tmp_path))

    assert target.error is None
    assert target.vault == str((tmp_path / ".silica").resolve())
    assert target.created is True


def test_codebase_existing_silica_is_adopted(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".silica").mkdir()

    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str((tmp_path / ".silica").resolve())
    assert target.created is False
    assert target.error is None


def test_plain_directory_is_literal(tmp_path):
    # Not a git repo → keep the historical "use the path verbatim" behaviour.
    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())
    assert target.created is False
    assert target.error is None


def test_explicit_silica_dir_is_not_nested(tmp_path):
    _git_init(tmp_path)
    silica = tmp_path / ".silica"
    silica.mkdir()

    target = resolve_vault_switch(str(silica))

    assert target.vault == str(silica.resolve())  # not <silica>/.silica
    assert target.created is False


def test_nonexistent_path_is_error(tmp_path):
    target = resolve_vault_switch(str(tmp_path / "missing"))

    assert target.vault is None
    assert target.created is False
    assert "directory" in target.error.lower()


def test_handler_on_codebase_creates_silica_and_switches(tmp_path, monkeypatch):
    _git_init(tmp_path)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "old"))
    monkeypatch.setattr(driver_pkg, "_driver", object())  # sentinel to observe reset

    handled = _handle_direct_shortcut(f"/vault {tmp_path}", [])

    assert handled is True
    silica = tmp_path / ".silica"
    assert silica.is_dir()  # created on demand
    assert CONFIG.vault_path == str(silica.resolve())  # not the repo root
    assert driver_pkg._driver is None  # reset so next read uses .silica
