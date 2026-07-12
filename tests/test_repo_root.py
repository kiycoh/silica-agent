"""ADR-0019 collateral: code-lane repo root resolved once and validated.

The invariant "the vault lives inside the repo it documents" is now checked
at one choke point (paths.repo_root_for) instead of being silently assumed by
7 call sites. An Obsidian vault nested in a FOREIGN git repo disables the
code lane instead of grounding it on the wrong repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from silica.kernel import paths


def _init_repo(p: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)


def setup_function(_fn):
    paths.clear_repo_root_cache()


def test_repo_mode_vault_grounds_on_its_repo(tmp_path):
    _init_repo(tmp_path)
    vault = tmp_path / "docs" / "silica"
    vault.mkdir(parents=True)
    assert paths.repo_root_for(vault) == tmp_path.resolve()
    assert paths.repo_root_warning(vault) is None


def test_plain_dir_vault_inside_repo_grounds_on_repo(tmp_path):
    # Non-Obsidian vault anywhere under the repo: git discovers the target
    # (today's invariant, relied on by the code/notebook adapters and tests).
    _init_repo(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    assert paths.repo_root_for(vault) == tmp_path.resolve()


def test_obsidian_vault_in_foreign_repo_disables_code_lane(tmp_path):
    _init_repo(tmp_path)
    vault = tmp_path / "notes"
    (vault / ".obsidian").mkdir(parents=True)
    assert paths.repo_root_for(vault) is None
    warn = paths.repo_root_warning(vault)
    assert warn is not None and "code lane disabled" in warn


def test_obsidian_vault_that_is_its_own_repo_keeps_root(tmp_path):
    vault = tmp_path / "notes"
    (vault / ".obsidian").mkdir(parents=True)
    _init_repo(vault)
    assert paths.repo_root_for(vault) == vault.resolve()
    assert paths.repo_root_warning(vault) is None


def test_no_repo_result_is_not_cached_so_git_init_is_picked_up(tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    assert paths.repo_root_for(vault) is None
    _init_repo(tmp_path)
    assert paths.repo_root_for(vault) == tmp_path.resolve()


def test_valid_resolution_is_cached(tmp_path):
    _init_repo(tmp_path)
    vault = tmp_path / "docs" / "silica"
    vault.mkdir(parents=True)
    assert paths.repo_root_for(vault) == tmp_path.resolve()
    key = str(vault.resolve())
    assert key in paths._REPO_ROOT_CACHE


def test_empty_vault_resolves_to_none():
    assert paths.repo_root_for("") is None
    assert paths.repo_root_warning("") is None
