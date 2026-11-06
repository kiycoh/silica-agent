"""A note's text stays out of the process when nothing needs it there.

t20's 97 converted books, 49 MiB of text, held 102 MiB in the driver's body
cache and 102 MiB more in Learn's parsed memo before anyone opened the app
(tracemalloc on a copy of the tenant, 2026-11-06).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.driver import fs_backend
from silica.driver.fs_backend import ObsidianFSBackend
from silica.kernel.learn import learner


def test_a_long_note_reads_whole_and_only_a_short_one_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_backend, "_BODY_CACHE_MAX_CHARS", 10)
    book, short = tmp_path / "Book.md", tmp_path / "Short.md"
    book.write_text("x" * 50)
    short.write_text("short")
    b = ObsidianFSBackend(vault_path=str(tmp_path))

    assert b._read_cached(book) == "x" * 50
    assert b._read_cached(book) == "x" * 50
    assert b._read_cached(short) == "short"

    assert b._body_cache[str(book)][1] is None
    assert b._body_cache[str(short)][1] == "short"


def test_a_long_crlf_note_keeps_its_terminator_when_written(tmp_path, monkeypatch):
    """_crlf reads the cache entry: a long note with no entry would turn LF."""
    monkeypatch.setattr(fs_backend, "_BODY_CACHE_MAX_CHARS", 10)
    book = tmp_path / "Book.md"
    book.write_bytes(b"line one\r\nline two\r\nline three\r\n")
    b = ObsidianFSBackend(vault_path=str(tmp_path))

    b.overwrite("Book.md", "line one\nline two\nline three\nline four\n")

    data = book.read_bytes()
    assert b"line four" in data
    assert data.count(b"\r\n") == data.count(b"\n")


def test_the_parsed_memo_keeps_the_frontmatter_and_not_the_body(tmp_path):
    (tmp_path / "N.md").write_text("---\ntype: plan\n---\nThe body.\n")

    first = learner.parsed_note(tmp_path, "N.md")
    second = learner.parsed_note(tmp_path, "N.md")

    assert first == second == ({"type": "plan"}, "The body.\n")
    assert learner._parsed_memo[str(tmp_path / "N.md")][1:] == ({"type": "plan"},)


def test_a_note_with_no_frontmatter_gives_its_whole_text_on_a_memo_hit(tmp_path):
    (tmp_path / "N.md").write_text("Only a body.\n")

    assert learner.parsed_note(tmp_path, "N.md") == (None, "Only a body.\n")
    assert learner.parsed_note(tmp_path, "N.md") == (None, "Only a body.\n")


def test_a_frontmatter_read_on_a_memo_hit_opens_no_file(tmp_path, monkeypatch):
    """The plan list asks this of every note, and a 2,166-note vault pays for it."""
    (tmp_path / "N.md").write_text("---\ntype: plan\n---\nThe body.\n")
    learner.parsed_note(tmp_path, "N.md")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("opened the note"))

    assert learner.parsed_note(tmp_path, "N.md", body=False) == ({"type": "plan"}, "")


def test_a_note_edited_after_the_memo_is_parsed_again(tmp_path):
    import os

    note = tmp_path / "N.md"
    note.write_text("---\ntype: plan\n---\nOld.\n")
    learner.parsed_note(tmp_path, "N.md")
    note.write_text("---\ntype: note\n---\nNew.\n")
    os.utime(note, (note.stat().st_atime, note.stat().st_mtime + 5))

    assert learner.parsed_note(tmp_path, "N.md") == ({"type": "note"}, "New.\n")
