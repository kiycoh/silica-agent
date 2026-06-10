import subprocess
from pathlib import Path

import pytest

from silica.kernel import gitstate


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path: Path, rel: str, text: str, msg: str) -> None:
    f = path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", rel], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg, "--", rel], cwd=path, check=True)


def test_find_repo_root_returns_toplevel(tmp_path):
    _init_repo(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert gitstate.find_repo_root(sub) == tmp_path.resolve()


def test_find_repo_root_none_outside_repo(tmp_path):
    assert gitstate.find_repo_root(tmp_path) is None


def test_head_ref_none_on_empty_repo(tmp_path):
    _init_repo(tmp_path)
    assert gitstate.head_ref(tmp_path) is None


def test_head_ref_returns_sha_after_commit(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "f.py", "x=1\n", "init")
    ref = gitstate.head_ref(tmp_path)
    assert isinstance(ref, str) and len(ref) == 40
