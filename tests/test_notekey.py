"""One note, several keys: each spelling pinned, so a store's keys never move by accident.

The rows come from the copies this module replaced (cooccur_key, quiz.key,
graph_diff.normalize_path, provenance.note_key), run on the same paths.
"""
from __future__ import annotations

import pytest

from silica.kernel.base.notekey import gate_key, ledger_key, note_key, note_ref, stem, store_key


@pytest.mark.parametrize("path, store, ledger, gate, name", [
    ("notes/Foo.md",        "notes/Foo",     "notes/foo",      "notes/foo",   "Foo"),
    ("notes/Foo",           "notes/Foo",     "notes/foo",      "notes/foo",   "Foo"),
    ("notes\\Foo.md",       "notes/Foo",     "notes/foo",      "notes/foo",   "Foo"),
    ("notes/Foo.MD",        "notes/Foo.MD",  "notes/foo.md",   "notes/foo",   "Foo.MD"),
    ("Node.js.md",          "Node.js",       "node.js",        "node.js",     "Node.js"),
    ("notes/Node.js",       "notes/Node.js", "notes/node.js",  "notes/node.js", "Node.js"),
    ("Straße.md",           "Straße",        "straße",         "straße",      "Straße"),
])
def test_each_key_spells_the_rule_of_its_store(path, store, ledger, gate, name):
    assert store_key(path) == store
    assert ledger_key(path) == ledger
    assert gate_key(path) == gate
    assert stem(path) == name


def test_store_key_is_idempotent_and_takes_none():
    assert store_key(store_key("a/b.md")) == "a/b"
    assert store_key(None) == "" and ledger_key(None) == ""


def test_the_folds_part_on_a_german_s():
    # lower() keeps ß, casefold() does not: the two must not be swapped silently.
    assert ledger_key("Straße.md") == "straße"
    assert note_key("Straße.md") == "strasse"


def test_note_ref_degrades_to_a_plain_strip_outside_the_vault(tmp_path, monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path), raising=False)
    assert note_ref(f"{tmp_path}/A/B.md") == "A/B"
    assert note_ref("/elsewhere/B.md") == "elsewhere/B"
