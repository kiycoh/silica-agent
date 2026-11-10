# tests/test_undo_command.py
"""`/undo <path>` takes the rest of the line, not the first whitespace token.

`parts` is a plain `.split()`, so `/undo silica/Verdetto reranker.md` used to look
up "silica/Verdetto" and report nothing to undo. Note names with spaces are the
common case in a vault, and the web GUI's per-note revert button sends exactly
this string.
"""
from silica.driver import NoteNotFound
from tests.run_command import direct


class _Store:
    """Records the path it was asked to undo; returns content for any path."""

    def __init__(self):
        self.asked = None

    def most_recent_path(self):
        return "fallback.md"

    def most_recent(self):
        return ("fallback.md", 0.0)

    def undo(self, path, current=None):
        self.asked = path
        return "restored body"

    def changed_since(self, path, current):
        return None

    def settle(self, path, landed):
        pass

    def depth(self, path):
        return 1

    def absent_floor(self, path):
        return False


def _run(monkeypatch, line):
    store = _Store()
    written = {}
    monkeypatch.setattr("silica.kernel.write.checkpoints.get_checkpoint_store", lambda: store)

    class _Journal:  # no chat deletes or moves: these tests are about the checkpoint path
        def last_delete(self, vault, path=None):
            return None

        def last_chat_move(self, vault, ref=None):
            return None

    monkeypatch.setattr("silica.kernel.write.undo_journal.get_undo_journal", lambda: _Journal())

    class _Driver:
        def read_note(self, ref):
            raise NoteNotFound(ref)

        def overwrite(self, path, content, *, loss=None, free=()):
            written["path"] = path

    monkeypatch.setattr("silica.driver.DRIVER", _Driver())
    assert direct(line, []) is True
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


def test_undo_takes_a_quoted_path_without_its_quotes(monkeypatch):
    store, written = _run(monkeypatch, '/undo "notes/a b.md"')
    assert store.asked == "notes/a b.md" and written["path"] == "notes/a b.md"


def test_direct_args_group_a_double_quoted_name_and_leave_apostrophes_and_tags_alone():
    from silica.session.words import direct_args

    assert direct_args('/check "Drafts/Opinion on limitation"') == ["/check", "Drafts/Opinion on limitation"]
    assert direct_args('/verify "Stoicism" --as "J. Smith"') == ["/verify", "Stoicism", "--as", "J. Smith"]
    assert direct_args("/find l'entropia #tag") == ["/find", "l'entropia", "#tag"]
    assert direct_args(r"/map C:\notes\a.md") == ["/map", r"C:\notes\a.md"]
    assert direct_args('/find "unbalanced') == ["/find", '"unbalanced']  # as typed, as before


def test_map_resolves_a_quoted_title_with_spaces(monkeypatch):
    seen = {}

    def mindmap(note_path, force=False):
        seen["note"] = note_path
        return {"skipped": "maps/x.canvas", "reason": "exists"}

    monkeypatch.setattr("silica.tools.graph.silica_mindmap", mindmap)
    assert direct('/map "Galileo Galilei"', []) is True
    assert seen["note"] == "Galileo Galilei"
