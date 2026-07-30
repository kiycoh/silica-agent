# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_search_context must not hand the whole vault to the model.

One Hit per matching LINE means a short query returns everything: measured at
529 hits / 170k chars for "OSI" on a 719-note vault. The tool groups by note,
ranks by hit density, and keeps a top slice — and says so when it truncates.
"""
from silica.driver.base import Hit, NoteRef
from silica.tools import atomic


def _hits(spec):
    """spec: {note_name: n_matching_lines} -> flat Hit list, driver ordering."""
    out = []
    for name, n in spec.items():
        ref = NoteRef(name=name, path=f"Folder/{name}.md")
        out.extend(Hit(ref=ref, line=i + 1, snippet=f"line {i + 1} of {name}") for i in range(n))
    return out


def _patch(monkeypatch, spec):
    monkeypatch.setattr(atomic.DRIVER, "search_context", lambda q: _hits(spec), raising=False)


def test_caps_notes_and_lines_and_says_so(monkeypatch):
    _patch(monkeypatch, {f"Note {i}": 10 for i in range(20)})
    out = atomic.silica_search_context("whatever")
    assert out["notes_matched"] == 20
    assert len(out["hits"]) == atomic._CONTEXT_MAX_NOTES * atomic._CONTEXT_LINES_PER_NOTE
    assert "20 notes matched" in out["truncated"]


def test_densest_note_wins_and_title_match_breaks_the_tie(monkeypatch):
    _patch(monkeypatch, {"Sparse": 1, "Dense": 9, "OSI model": 3, "Elsewhere": 3})
    out = atomic.silica_search_context("osi")
    order = list(dict.fromkeys(h["name"] for h in out["hits"]))
    assert order == ["Dense", "OSI model", "Elsewhere", "Sparse"]


def test_small_result_is_untruncated_and_complete(monkeypatch):
    _patch(monkeypatch, {"Alpha": 2, "Beta": 1})
    out = atomic.silica_search_context("alpha")
    assert "truncated" not in out
    assert len(out["hits"]) == 3
    assert out["hits"][0]["line"] == 1 and out["hits"][0]["path"] == "Folder/Alpha.md"


def test_no_match_returns_an_empty_answer(monkeypatch):
    _patch(monkeypatch, {})
    out = atomic.silica_search_context("nothing")
    assert out == {"hits": [], "notes_matched": 0}
