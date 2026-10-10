# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The HEAD-keyed stale snapshot: one recompute per HEAD move, peek never pays."""
import json
import subprocess

from silica.kernel.code import codedocs, gitstate


def _repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path, msg="c"):
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


def _fixture(tmp_path):
    """Repo with one documented source that moved past the note's code_ref."""
    _repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    ref0 = _commit(tmp_path)
    vault = tmp_path / "docs"
    vault.mkdir()
    (vault / "m.md").write_text(
        f"---\ndocuments:\n  - src/m.py\ncode_ref: {ref0}\n---\n\nbody\n",
        encoding="utf-8")
    (tmp_path / "src" / "m.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _commit(tmp_path, "structural change")
    return vault


def test_snapshot_second_call_skips_the_git_walk(tmp_path, monkeypatch):
    vault = _fixture(tmp_path)
    first = codedocs.snapshot(vault, repo_root=tmp_path)
    assert [d.note_path for d in first] == ["m.md"]

    calls = {"n": 0}
    orig = gitstate.paths_touched_since

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(gitstate, "paths_touched_since", counting)
    second = codedocs.snapshot(vault, repo_root=tmp_path)
    assert calls["n"] == 0                      # cache hit: no history walk
    assert second == first                      # roundtrip is field-for-field


def test_head_move_recomputes(tmp_path):
    vault = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)
    (tmp_path / "src" / "m.py").write_text("def f(x, y):\n    return x\n",
                                           encoding="utf-8")
    _commit(tmp_path, "moves HEAD again")
    fresh = codedocs.snapshot(vault, repo_root=tmp_path)
    assert [c.subject for c in fresh[0].intervening] == [
        "moves HEAD again", "structural change"]


def test_peek_reads_the_warm_cache(tmp_path):
    vault = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)
    assert codedocs.peek(vault, repo_root=tmp_path) == {"m.md": "structural"}


def test_peek_structural_wins_regardless_of_order(tmp_path):
    """A note with paths at both levels reports structural, in either order."""
    vault = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)   # warms the cache with a real HEAD key
    f = codedocs._snapshot_path(vault)
    raw = json.loads(f.read_text(encoding="utf-8"))
    cosmetic = {"note_path": "m.md", "change_level": "cosmetic"}
    structural = {"note_path": "m.md", "change_level": "structural"}

    raw["docs"] = [cosmetic, structural]
    f.write_text(json.dumps(raw), encoding="utf-8")
    assert codedocs.peek(vault, repo_root=tmp_path) == {"m.md": "structural"}

    raw["docs"] = [structural, cosmetic]
    f.write_text(json.dumps(raw), encoding="utf-8")
    assert codedocs.peek(vault, repo_root=tmp_path) == {"m.md": "structural"}


def test_peek_never_recomputes(tmp_path):
    vault = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)
    f = codedocs._snapshot_path(vault)
    raw = json.loads(f.read_text(encoding="utf-8"))
    raw["head"] = "0" * 40                      # stored key no longer matches HEAD
    f.write_text(json.dumps(raw), encoding="utf-8")
    before = f.read_text(encoding="utf-8")
    assert codedocs.peek(vault, repo_root=tmp_path) == {}
    assert f.read_text(encoding="utf-8") == before   # file untouched


def test_peek_cold_cache_is_empty(tmp_path):
    vault = _fixture(tmp_path)
    assert codedocs.peek(vault, repo_root=tmp_path) == {}


def test_corrupt_cache_recovers(tmp_path):
    vault = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)
    f = codedocs._snapshot_path(vault)
    f.write_text("{not json", encoding="utf-8")
    assert codedocs.peek(vault, repo_root=tmp_path) == {}
    fresh = codedocs.snapshot(vault, repo_root=tmp_path)   # warming call recovers
    assert [d.note_path for d in fresh] == ["m.md"]
    assert json.loads(f.read_text(encoding="utf-8"))["docs"]


def test_invalidate_unlinks(tmp_path):
    vault = _fixture(tmp_path)
    codedocs.snapshot(vault, repo_root=tmp_path)
    f = codedocs._snapshot_path(vault)
    assert f.exists()
    codedocs.invalidate_snapshot(vault)
    assert not f.exists()
    codedocs.invalidate_snapshot(vault)         # second unlink is a no-op


def test_no_git_degrades_soft(tmp_path):
    vault = tmp_path / "docs"
    vault.mkdir()
    assert codedocs.snapshot(vault) == []
    assert codedocs.peek(vault) == {}


def test_peek_level_tolerates_store_keyspace_paths():
    m = {"wiki/m.md": "cosmetic"}
    assert codedocs.peek_level(m, "wiki/m") == "cosmetic"
    assert codedocs.peek_level(m, "wiki/m.md") == "cosmetic"
    assert codedocs.peek_level(m, "other") is None
