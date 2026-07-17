"""Run-scoped title-index cache: one list_files() per run, append-on-write."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from silica.router.states.linking import _run_title_refs


def _ref(name, path):
    return SimpleNamespace(name=name, path=path)


def test_list_files_called_once_and_cached():
    fsm = SimpleNamespace()
    driver = MagicMock()
    driver.list_files.return_value = [_ref("A", "A.md"), _ref("B", "B.md")]
    with patch("silica.router.orchestrator.DRIVER", driver):
        refs1 = _run_title_refs(fsm)
        refs2 = _run_title_refs(fsm)
    assert refs1 is refs2
    assert driver.list_files.call_count == 1
    assert [r.name for r in refs1] == ["A", "B"]


def test_write_appended_stems_visible_to_later_chunks():
    from silica.kernel.autolink import build_title_index
    fsm = SimpleNamespace()
    driver = MagicMock()
    driver.list_files.return_value = [_ref("A", "A.md")]
    with patch("silica.router.orchestrator.DRIVER", driver):
        refs = _run_title_refs(fsm)
    # simulate the WRITE-side append for a note created mid-run
    from silica.driver.base import NoteRef
    refs.append(NoteRef(name="New Note", path="Notes/New Note.md"))
    idx = build_title_index(_run_title_refs(fsm))
    assert "New Note" in idx and "A" in idx


def test_duplicate_basename_still_disambiguated():
    # build_title_index drops conflicting basenames; the cache must preserve
    # that by re-running it over cached refs, not by caching the index itself.
    from silica.kernel.autolink import build_title_index
    fsm = SimpleNamespace()
    driver = MagicMock()
    driver.list_files.return_value = [_ref("Foo", "x/Foo.md")]
    with patch("silica.router.orchestrator.DRIVER", driver):
        refs = _run_title_refs(fsm)
    from silica.driver.base import NoteRef
    refs.append(NoteRef(name="Foo", path="y/Foo.md"))
    assert "Foo" not in build_title_index(refs)
