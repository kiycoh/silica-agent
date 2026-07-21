"""In-memory body cache on ObsidianFSBackend — mtime-keyed, invalidated on write.

Pins root fix 1 of docs/audits/2026-07-21-perf-audit-hot-paths.md: reads route
through a process-lifetime cache keyed by mtime, and every backend write method
keeps it coherent with what just landed on disk.
"""
import os

import pytest

from silica.driver.fs_backend import ObsidianFSBackend


def _make_backend(tmp_path, files: dict[str, str]) -> ObsidianFSBackend:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    backend = ObsidianFSBackend(str(tmp_path))
    backend._rebuild_index()
    return backend


def test_cache_hit_avoids_reread_mtime_guard(tmp_path):
    """Reading twice without an mtime change must serve the cached body, not
    whatever bytes are currently on disk (proves the mtime guard is load-bearing,
    not just a cache that always happens to match)."""
    backend = _make_backend(tmp_path, {"note.md": "original content"})
    path = tmp_path / "note.md"

    first = backend.read_note("note.md")
    assert first.content == "original content"

    # Mutate bytes on disk WITHOUT changing mtime -> cache must still serve OLD content.
    st = os.stat(path)
    path.write_text("mutated bytes, same mtime", encoding="utf-8")
    os.utime(path, (st.st_atime, st.st_mtime))

    stale = backend.read_note("note.md")
    assert stale.content == "original content"

    # Now bump mtime -> cache must detect the change and serve NEW content.
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    fresh = backend.read_note("note.md")
    assert fresh.content == "mutated bytes, same mtime"


def test_overwrite_invalidates_cache(tmp_path):
    backend = _make_backend(tmp_path, {"note.md": "v1"})
    assert backend.read_note("note.md").content == "v1"
    backend.overwrite("note.md", "v2")
    assert backend.read_note("note.md").content == "v2"


def test_create_then_read_sees_new_content(tmp_path):
    backend = _make_backend(tmp_path, {})
    backend.create("fresh.md", "brand new")
    assert backend.read_note("fresh.md").content == "brand new"


def test_set_prop_invalidates_cache(tmp_path):
    backend = _make_backend(tmp_path, {"note.md": "body text"})
    assert backend.read_note("note.md").content == "body text"
    backend.set_prop("note.md", "status", "done")
    content = backend.read_note("note.md").content
    assert content != "body text"
    assert "status: done" in content


def test_append_invalidates_cache(tmp_path):
    backend = _make_backend(tmp_path, {"note.md": "hello"})
    assert backend.read_note("note.md").content == "hello"
    backend.append("note.md", " world")
    assert backend.read_note("note.md").content == "hello world"


def test_delete_invalidates_cache(tmp_path):
    backend = _make_backend(tmp_path, {"note.md": "to be deleted"})
    assert backend.read_note("note.md").content == "to be deleted"
    backend.delete("note.md")
    with pytest.raises(RuntimeError):
        backend.read_note("note.md")


def test_move_invalidates_old_new_and_referrer(tmp_path):
    """Move must invalidate the old (now-gone) path, the new path, AND every
    referrer whose link text got rewritten on disk directly (not through
    overwrite()) -- otherwise a referrer read before the move would keep
    serving its pre-rewrite body."""
    backend = _make_backend(
        tmp_path, {"old.md": "moved content", "ref.md": "see [[old]]"}
    )
    assert backend.read_note("old.md").content == "moved content"
    assert backend.read_note("ref.md").content == "see [[old]]"

    backend.move("old.md", "new.md")

    with pytest.raises(RuntimeError):
        backend.read_note("old.md")
    assert backend.read_note("new.md").content == "moved content"
    assert "[[new]]" in backend.read_note("ref.md").content


def test_read_note_missing_raises_runtime_error(tmp_path):
    backend = _make_backend(tmp_path, {})
    with pytest.raises(RuntimeError):
        backend.read_note("nope.md")


def test_search_context_batch_empty_queries(tmp_path):
    backend = _make_backend(tmp_path, {"a.md": "hello"})
    assert backend.search_context_batch([]) == {}


def test_search_context_batch_matches_per_query(tmp_path):
    backend = _make_backend(
        tmp_path,
        {
            "a.md": "alpha line one\nbeta line two\nalpha again here",
            "b.md": "no match here\nanother alpha mention",
            "c.md": "totally unrelated content",
        },
    )
    queries = ["alpha", "beta", "zzz-nomatch", "line"]
    expected = {q: backend.search_context(q) for q in queries}
    assert backend.search_context_batch(queries) == expected


def test_search_context_batch_dedupes_repeated_query(tmp_path):
    """A repeated query string must not double its hits: the reference
    ``{q: search_context(q) for q in queries}`` collapses duplicate keys to
    one dict entry (last write wins, but every write is identical), so the
    batch output must match that shape exactly, not append twice."""
    backend = _make_backend(
        tmp_path,
        {
            "a.md": "alpha line one\nsomething else",
            "b.md": "alpha appears again here",
        },
    )
    queries = ["alpha", "alpha", "beta"]
    expected = {q: backend.search_context(q) for q in queries}
    assert backend.search_context_batch(queries) == expected
