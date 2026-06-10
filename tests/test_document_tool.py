import json
import subprocess
from pathlib import Path

import pytest

from silica.config import CONFIG


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    f = path / "src" / "m.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("def hi():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


@pytest.fixture
def repo_vault(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    vault = tmp_path / "docs"
    vault.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(CONFIG, "inbox_dir", "Inbox")
    # DRIVER is a singleton bound to CONFIG.vault_path lazily; force fs backend on this vault
    from silica.driver import fs_backend
    import silica.driver as driver_mod
    monkeypatch.setattr(driver_mod, "DRIVER", fs_backend.ObsidianFSBackend(str(vault)))
    # the tool imports DRIVER from silica.driver at call time
    return tmp_path, vault


def test_document_writes_to_inbox_with_frontmatter(repo_vault):
    from silica.tools.codedocs_tool import silica_document
    root, vault = repo_vault
    result = silica_document(path="src/m.py")
    data = json.loads(result) if isinstance(result, str) else result
    assert data["status"] == "ok"
    inbox_file = Path(data["note_path"])
    written = (vault / inbox_file).read_text(encoding="utf-8") if not inbox_file.is_absolute() else inbox_file.read_text(encoding="utf-8")
    assert "Inbox/" in data["note_path"]
    assert "documents:" in written
    assert "src/m.py" in written
    assert "def hi()" in written  # source content captured


def test_document_rejects_path_outside_repo(repo_vault):
    from silica.tools.codedocs_tool import silica_document
    result = silica_document(path="../../etc/passwd")
    data = json.loads(result) if isinstance(result, str) else result
    assert data["status"] == "error"
