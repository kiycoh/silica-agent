"""The one check behind `int_flag`, the parser the five `--flag=N` sites share."""
from __future__ import annotations

from silica.session.words import int_flag


class TestIntFlag:
    def test_parses_value(self):
        assert int_flag(["query", "--k=7"], "--k=", 5) == 7

    def test_absent_keeps_default(self):
        assert int_flag(["query"], "--k=", 5) == 5

    def test_garbage_keeps_default(self):
        assert int_flag(["--k=abc"], "--k=", 5) == 5
        assert int_flag(["--k="], "--k=", 5) == 5

    def test_longer_flag_is_not_a_prefix_match(self):
        # /report reads --top-k=; /find reads --k=. Neither may eat the other.
        assert int_flag(["--top-k=15"], "--k=", 5) == 5
        assert int_flag(["--top-k=15"], "--top-k=", 10) == 15


def test_each_index_command_builds_its_own_index(monkeypatch):
    """The three commands call `kernel.recall.refresh` itself, not a tool by its
    registry name: each reaches its own index, with the folder and --force."""
    from silica.commands import dispatch
    from silica.kernel.recall import refresh

    called = []
    for name in ("embed", "cooccurrence", "lexical"):
        monkeypatch.setattr(refresh, name, lambda folder, force, ask=None, _n=name: (
            called.append((_n, folder, force)), {"indexed": 1, "total_notes": 1})[1])
    for line in ("/embed A --force", "/cooccur B", "/lexical --folder=C"):
        dispatch(line, [])
    assert called == [("embed", "A", True), ("cooccurrence", "B", False), ("lexical", "C", False)]


def test_the_command_word_is_cut_one_way():
    """A leading space must not get a command past a gate that reads the word
    (the demo's door, review 2026-10-31), and no word at all is not a crash."""
    from silica.session.words import command_word, first_word

    assert command_word(" /Convert x") == "/convert"
    assert first_word(" /Convert x") == "/Convert"
    assert command_word("") == command_word("   ") == ""
