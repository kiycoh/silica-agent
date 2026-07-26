import io
import subprocess
from pathlib import Path

import pytest

from silica.cli import _activate_repo_mode, default_user_vault, resolve_repo_mode_vault
from silica.config import CONFIG


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _obsidian(path: Path) -> None:
    (path / ".obsidian").mkdir(parents=True, exist_ok=True)


def test_explicit_vault_env_code_repo_adopts_root_and_declares_write_dir(tmp_path):
    # SILICA_VAULT at a source tree: the vault is the repo itself (reads see the
    # whole tree), and the write boundary is declared rather than guessed.
    _init_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    orig = CONFIG.vault_path
    try:
        CONFIG.vault_path = str(tmp_path)
        _activate_repo_mode()
        assert Path(CONFIG.vault_path).resolve() == tmp_path.resolve()
        assert (tmp_path / "vault.yaml").read_text(encoding="utf-8") == "write_dir: docs/silica\n"
    finally:
        CONFIG.vault_path = orig


def test_explicit_vault_env_prose_folder_stays_in_place(tmp_path):
    # A folder of notes is written in place: no subfolder, and no manifest at all
    # (in-place IS the default, so there is nothing to declare).
    _init_repo(tmp_path)
    (tmp_path / "nota.md").write_text("# nota")
    orig = CONFIG.vault_path
    try:
        CONFIG.vault_path = str(tmp_path)
        _activate_repo_mode()
        assert Path(CONFIG.vault_path).resolve() == tmp_path.resolve()
        assert not (tmp_path / "vault.yaml").exists()
        assert not (tmp_path / "docs").exists()
    finally:
        CONFIG.vault_path = orig


def test_explicit_vault_env_file_is_refused(tmp_path):
    # Pointing SILICA_VAULT at a file used to crash with NotADirectoryError while
    # trying to mkdir <file>/docs/silica.
    target = tmp_path / "note.md"
    target.write_text("# not a vault")
    orig = CONFIG.vault_path
    try:
        CONFIG.vault_path = str(target)
        _activate_repo_mode()
        assert CONFIG.vault_path == str(target)  # left alone, not mangled
    finally:
        CONFIG.vault_path = orig


def test_explicit_vault_env_obsidian_vault_verbatim(tmp_path):
    # An Obsidian vault (even git-tracked) is adopted exactly — no docs/silica.
    _init_repo(tmp_path)
    _obsidian(tmp_path)
    orig = CONFIG.vault_path
    try:
        CONFIG.vault_path = str(tmp_path)
        _activate_repo_mode()
        assert Path(CONFIG.vault_path).resolve() == tmp_path.resolve()
        assert not (tmp_path / "docs" / "silica").exists()
    finally:
        CONFIG.vault_path = orig


def test_repo_mode_picks_docs_silica_when_present(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    result = resolve_repo_mode_vault(cwd=tmp_path, vault_env="", adopt_ok=True)
    assert Path(result).resolve() == (tmp_path / "docs" / "silica").resolve()


def test_repo_mode_obsidian_root_is_verbatim(tmp_path):
    # .obsidian at the repo root → adopt the root itself, even without docs_ok.
    _init_repo(tmp_path)
    _obsidian(tmp_path)
    result = resolve_repo_mode_vault(cwd=tmp_path, vault_env="", adopt_ok=False)
    assert Path(result).resolve() == tmp_path.resolve()


def test_repo_mode_skipped_when_vault_env_set(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    result = resolve_repo_mode_vault(cwd=tmp_path, vault_env="/explicit/vault", adopt_ok=True)
    assert result is None


def test_repo_mode_none_outside_repo(tmp_path):
    result = resolve_repo_mode_vault(cwd=tmp_path, vault_env="", adopt_ok=True)
    assert result is None


def test_repo_mode_none_when_repo_not_yet_adopted(tmp_path):
    # A repo the user never adopted is not silently taken over just because the
    # shell happens to sit in it.
    _init_repo(tmp_path)
    result = resolve_repo_mode_vault(cwd=tmp_path, vault_env="", adopt_ok=False)
    assert result is None


def test_repo_mode_adopts_root_carrying_a_manifest(tmp_path):
    # Declared vault at the root: adopted without asking, and NOT overridden by
    # the docs/silica it writes into.
    _init_repo(tmp_path)
    (tmp_path / "vault.yaml").write_text("write_dir: docs/silica\n", encoding="utf-8")
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    result = resolve_repo_mode_vault(cwd=tmp_path, vault_env="", adopt_ok=False)
    assert Path(result).resolve() == tmp_path.resolve()


def test_repo_mode_skipped_for_silica_own_repo(tmp_path):
    # Running inside Silica's *own* source repo is dev mode, not a vault.
    _init_repo(tmp_path)
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    result = resolve_repo_mode_vault(
        cwd=tmp_path, vault_env="", adopt_ok=True, self_repo=tmp_path.resolve()
    )
    assert result is None


def test_repo_mode_unaffected_when_self_repo_differs(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    result = resolve_repo_mode_vault(
        cwd=tmp_path, vault_env="", adopt_ok=True, self_repo=tmp_path / "elsewhere"
    )
    assert Path(result).resolve() == (tmp_path / "docs" / "silica").resolve()


def test_unadopted_repo_never_prompts_without_a_terminal(tmp_path, monkeypatch):
    # `silica mcp` runs with stdin bound to the MCP client: a prompt there would
    # eat the first JSON-RPC message and then die on EOF. No tty ⇒ home vault.
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO())  # StringIO.isatty() is False
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted without a tty"))
    orig = CONFIG.vault_path
    try:
        CONFIG.vault_path = ""
        _activate_repo_mode()
        assert Path(CONFIG.vault_path).resolve() == default_user_vault().resolve()
    finally:
        CONFIG.vault_path = orig


def test_default_user_vault_under_home(tmp_path):
    assert default_user_vault(home=tmp_path) == tmp_path / ".silica" / "vault"
