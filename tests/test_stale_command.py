import json
import subprocess
from pathlib import Path

from silica.cli import _handle_direct_shortcut
from silica.config import CONFIG


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path: Path, rel: str, text: str, msg: str) -> str:
    f = path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", rel], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg, "--", rel], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True
    ).stdout.strip()


def test_stale_command_handled_and_reports(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "def f():\n    return 1\n", "c1")
    vault = tmp_path / "docs"
    vault.mkdir()
    (vault / "m.md").write_text(
        f"---\ndocuments:\n  - src/m.py\ncode_ref: {ref0}\n---\n\nbody\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "src/m.py", "def f(x):\n    return 1\n", "c2")  # structural: shown by default

    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    handled = _handle_direct_shortcut("/stale", [])
    assert handled is True
    out = capsys.readouterr().out
    assert "m.md" in out
    assert "src/m.py" in out


def test_stale_command_refreshes_the_snapshot(tmp_path, monkeypatch, capsys):
    """/stale is both the inspection tool and the manual refresh valve: it
    always recomputes and rewrites the cache, never trusts it."""
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "def f():\n    return 1\n", "c1")
    vault = tmp_path / "docs"
    vault.mkdir()
    (vault / "m.md").write_text(
        f"---\ndocuments:\n  - src/m.py\ncode_ref: {ref0}\n---\n\nbody\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "src/m.py", "def f(x):\n    return 1\n", "c2")

    from silica.kernel.code import codedocs
    cache = codedocs._snapshot_path(vault)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"head": "poison", "docs": []}', encoding="utf-8")

    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    assert _handle_direct_shortcut("/stale", []) is True
    assert "m.md" in capsys.readouterr().out          # reported despite the poison
    raw = json.loads(cache.read_text(encoding="utf-8"))
    assert raw["head"] != "poison" and raw["docs"]    # cache rewritten fresh
