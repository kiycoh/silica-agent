# tests/test_vault_codebase_switch.py
"""A vault path is adopted as-is: Silica reads the folder the user named, never a
subfolder it invents. Where it may *write* inside that folder is the separate
`write_dir` axis, declared in vault.yaml at adoption time (docs/silica for a
source tree, in place for prose). Pre-`write_dir` layouts still resolve to
<target>/docs/silica so existing vaults do not move."""
import subprocess

import silica.driver as driver_pkg
from silica.cli import resolve_vault_switch, _handle_direct_shortcut
from silica.config import CONFIG


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _obsidian(path):
    (path / ".obsidian").mkdir(parents=True, exist_ok=True)


def _code_repo(path):
    _git_init(path)
    (path / "pyproject.toml").write_text("[project]\nname='x'\n")


def test_obsidian_vault_is_verbatim(tmp_path):
    _obsidian(tmp_path)

    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())  # notes in the root
    assert target.created is False


def test_plain_dir_is_adopted_as_is(tmp_path):
    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())  # no docs/silica invented
    assert target.created is False


def test_code_repo_is_adopted_as_is(tmp_path):
    # The repo root becomes the vault; only the write boundary differs, and that
    # lives in vault.yaml, not in the resolved path.
    _code_repo(tmp_path)

    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())
    assert not (tmp_path / "docs" / "silica").exists()


def test_git_tracked_obsidian_vault_stays_verbatim(tmp_path):
    # The original bug: a git-tracked Obsidian vault must not nest.
    _git_init(tmp_path)
    _obsidian(tmp_path)

    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())
    assert target.created is False


def test_a_legacy_docs_silica_does_not_capture_the_switch(tmp_path):
    # /vault <path> opens <path>. What it holds cannot redirect it elsewhere.
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    (tmp_path / "docs" / "silica" / "nota.md").write_text("# nota", encoding="utf-8")

    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())
    assert target.created is False


def test_root_manifest_beats_legacy_docs_silica(tmp_path):
    # A declared vault at the root owns the docs/silica it writes into: adopting
    # the subfolder instead would silently halve the vault.
    (tmp_path / "vault.yaml").write_text("write_dir: docs/silica\n", encoding="utf-8")
    (tmp_path / "docs" / "silica").mkdir(parents=True)

    target = resolve_vault_switch(str(tmp_path))

    assert target.vault == str(tmp_path.resolve())


def test_nonexistent_path_is_created_as_the_vault(tmp_path):
    missing = tmp_path / "missing"

    target = resolve_vault_switch(str(missing))

    assert target.vault == str(missing.resolve())
    assert target.created is True


def test_file_path_is_refused(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# not a vault")

    target = resolve_vault_switch(str(note))

    assert target.error and "not a directory" in target.error


def test_handler_on_code_repo_adopts_root_and_confines_writes(tmp_path, monkeypatch, capsys):
    _code_repo(tmp_path)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "old"))
    monkeypatch.setattr(driver_pkg, "_driver", object())  # sentinel to observe reset

    handled = _handle_direct_shortcut(f"/vault {tmp_path}", [])

    assert handled is True
    assert CONFIG.vault_path == str(tmp_path.resolve())  # the repo IS the vault
    assert (tmp_path / "vault.yaml").read_text(encoding="utf-8") == "write_dir: docs/silica\n"
    assert "docs/silica" in capsys.readouterr().out
    assert driver_pkg._driver is None  # reset so the next read uses the new vault


def test_handler_on_file_refuses_without_switching(tmp_path, monkeypatch):
    note = tmp_path / "note.md"
    note.write_text("# not a vault")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "old"))
    sentinel = object()
    monkeypatch.setattr(driver_pkg, "_driver", sentinel)

    handled = _handle_direct_shortcut(f"/vault {note}", [])

    assert handled is True
    assert CONFIG.vault_path == str(tmp_path / "old")  # unchanged
    assert driver_pkg._driver is sentinel  # never reset


def test_handler_on_obsidian_vault_is_verbatim(tmp_path, monkeypatch):
    _obsidian(tmp_path)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "old"))
    monkeypatch.setattr(driver_pkg, "_driver", object())

    handled = _handle_direct_shortcut(f"/vault {tmp_path}", [])

    assert handled is True
    assert CONFIG.vault_path == str(tmp_path.resolve())  # root, no docs/silica
    assert not (tmp_path / "docs" / "silica").exists()
    # The vault is still adopted verbatim; what the switch declares is where
    # writes may land, and safe mode is the default for someone's own notes.
    assert (tmp_path / "vault.yaml").read_text(encoding="utf-8") == "write_dir: silica\n"
