# tests/test_undo_command.py
"""`/undo <path>` takes the rest of the line, not the first whitespace token.

`parts` is a plain `.split()`, so `/undo silica/Verdetto reranker.md` used to look
up "silica/Verdetto" and report nothing to undo. Note names with spaces are the
common case in a vault, and the web GUI's per-note revert button sends exactly
this string.
"""
from silica.cli import _handle_direct_shortcut


class _Store:
    """Records the path it was asked to undo; returns content for any path."""

    def __init__(self):
        self.asked = None

    def most_recent_path(self):
        return "fallback.md"

    def undo(self, path):
        self.asked = path
        return "restored body"

    def depth(self, path):
        return 1


def _run(monkeypatch, line):
    store = _Store()
    written = {}
    monkeypatch.setattr("silica.kernel.write.checkpoints.get_checkpoint_store", lambda: store)

    class _Driver:
        def overwrite(self, path, content):
            written["path"] = path

    monkeypatch.setattr("silica.driver.DRIVER", _Driver())
    assert _handle_direct_shortcut(line, []) is True
    return store, written


def test_undo_keeps_a_path_that_contains_spaces(monkeypatch):
    store, written = _run(monkeypatch, "/undo silica/Verdetto reranker.md")
    assert store.asked == "silica/Verdetto reranker.md"
    assert written["path"] == "silica/Verdetto reranker.md"


def test_undo_without_an_argument_falls_back_to_the_last_checkpoint(monkeypatch):
    store, _ = _run(monkeypatch, "/undo")
    assert store.asked == "fallback.md"


def test_undo_strips_surrounding_whitespace(monkeypatch):
    store, _ = _run(monkeypatch, "/undo   notes/a b.md  ")
    assert store.asked == "notes/a b.md"
