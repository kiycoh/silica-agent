# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note derived from source warns the reader when the source moved.

`code_ref`/`documents:` always carried the signal; until the read gate only the
`/stale` report consumed it, so an agent reading a wiki note after a refactor
was told nothing while the note named files that no longer existed.
"""
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


def test_moved_source_warns(tmp_path):
    _repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "old.py").write_text("x = 1\n", encoding="utf-8")
    ref = _commit(tmp_path)
    # the refactor: pkg/old.py becomes pkg/lane/old.py, note keeps the old ref
    (tmp_path / "pkg" / "lane").mkdir()
    (tmp_path / "pkg" / "old.py").rename(tmp_path / "pkg" / "lane" / "old.py")
    _commit(tmp_path, "split into lanes")

    data = {"documents": ["pkg/old.py"], "code_ref": ref}
    warning = codedocs.read_warning(tmp_path, data, repo_root=tmp_path)
    assert warning.startswith("[stale]")
    assert "pkg/old.py" in warning


def test_fresh_note_is_silent(tmp_path):
    _repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    ref = _commit(tmp_path)

    data = {"documents": ["pkg/m.py"], "code_ref": ref}
    assert codedocs.read_warning(tmp_path, data, repo_root=tmp_path) == ""


def test_note_without_documents_is_silent(tmp_path):
    _repo(tmp_path)
    (tmp_path / "a.md").write_text("hi\n", encoding="utf-8")
    _commit(tmp_path)
    assert codedocs.read_warning(tmp_path, {"tags": ["x"]}, repo_root=tmp_path) == ""


def test_read_tool_prefixes_the_banner(tmp_path, monkeypatch):
    """The gate is on the tool an agent actually calls, not just the kernel."""
    from silica.tools import atomic

    monkeypatch.setattr(codedocs, "read_warning", lambda *a, **k: "[stale] moved")
    content = "---\ndocuments:\n  - \"pkg/old.py\"\ncode_ref: deadbeef\n---\n\n# note\n"
    out = atomic._with_stale_banner(content)
    assert out.startswith("> [stale] moved")
    assert content in out


def test_read_tool_leaves_plain_notes_untouched():
    from silica.tools import atomic

    content = "# just a note\n\nno frontmatter here\n"
    assert atomic._with_stale_banner(content) == content
