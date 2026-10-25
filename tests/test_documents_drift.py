# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0038: the stale lane prunes like every other vault walk, and the
cached snapshot never resurrects what the walk excludes. (Detection at
session open left with the harness hook, ADR-0130.)"""
import json
import subprocess

from silica.kernel.code import codedocs


def _repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path, msg="c"):
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


def _note(path, ref):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ndocuments:\n  - src/m.py\ncode_ref: {ref}\n---\n\nbody\n",
                    encoding="utf-8")


def test_documenting_walk_prunes_hidden_and_ignored_dirs(tmp_path):
    # Measured 2026-09-04 on the repo vault: rglob entered .silica/ (242
    # residue notes of a killed probe) and .venv/, 17.8 s for 4 real notes.
    vault = tmp_path
    _note(vault / "docs" / "real.md", "abc")
    _note(vault / ".silica" / "probe" / "ghost.md", "abc")
    _note(vault / ".venv" / "pkg" / "readme.md", "abc")
    _note(vault / "node_modules" / "x" / "doc.md", "abc")
    (vault / ".silicaignore").write_text("bench/\n", encoding="utf-8")
    _note(vault / "bench" / "fixture.md", "abc")
    assert [p for p, _, _ in codedocs.iter_documenting_notes(vault)] == ["docs/real.md"]


def _fixture(tmp_path):
    """Repo with two documented sources; the note on m.py is stale, n.py's is not."""
    _repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "src" / "n.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    ref0 = _commit(tmp_path)
    vault = tmp_path / "docs"
    _note(vault / "m.md", ref0)
    (vault / "n.md").write_text(
        f"---\ndocuments:\n  - src/n.py\ncode_ref: {ref0}\n---\n\nbody\n", encoding="utf-8")
    (tmp_path / "src" / "m.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    ref1 = _commit(tmp_path, "structural change")
    return vault, ref0, ref1


def test_peek_ignores_cached_entries_under_hidden_dirs(tmp_path):
    # A snapshot written before the walk was pruned can still hold residue
    # under .silica/; a read of the cache must not resurrect it (seen live
    # 2026-09-04).
    vault, _, _ = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)
    cache = codedocs._snapshot_path(vault)
    raw = json.loads(cache.read_text(encoding="utf-8"))
    raw["docs"].append(dict(raw["docs"][0], note_path=".silica/probe/ghost.md"))
    cache.write_text(json.dumps(raw), encoding="utf-8")
    assert codedocs.peek(vault, repo_root=tmp_path) == {"m.md": codedocs.CHANGE_STRUCTURAL}
