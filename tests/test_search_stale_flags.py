# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Recall payload stale flags (spec-stale-triggers §3): peek-only, zero bytes
when nothing is stale, and a peek failure never fails the search."""
from silica.driver.base import Hit, NoteRef
from silica.kernel.code import codedocs
from silica.tools import atomic


def _refs(monkeypatch, names):
    refs = [NoteRef(name=n, path=f"F/{n}.md") for n in names]
    monkeypatch.setattr(atomic.DRIVER, "search_names", lambda q: refs, raising=False)


def _hits(monkeypatch, pairs):
    hits = [Hit(ref=NoteRef(name=n, path=p), line=1, snippet="s") for n, p in pairs]
    monkeypatch.setattr(atomic.DRIVER, "search_context", lambda q: hits, raising=False)


def test_search_carries_a_stale_map(monkeypatch):
    _refs(monkeypatch, ["Alpha", "Beta"])
    monkeypatch.setattr(codedocs, "peek", lambda v: {"F/Alpha.md": "structural"})
    out = atomic.silica_search("a")
    assert out["stale"] == {"F/Alpha.md": "structural"}


def test_search_fresh_vault_has_no_stale_key(monkeypatch):
    _refs(monkeypatch, ["Alpha"])
    monkeypatch.setattr(codedocs, "peek", lambda v: {})
    assert "stale" not in atomic.silica_search("a")


def test_search_map_lists_only_returned_paths(monkeypatch):
    _refs(monkeypatch, ["Alpha"])
    monkeypatch.setattr(codedocs, "peek",
                        lambda v: {"F/Alpha.md": "cosmetic", "F/Other.md": "structural"})
    assert atomic.silica_search("a")["stale"] == {"F/Alpha.md": "cosmetic"}


def test_search_context_flags_stale_hits(monkeypatch):
    _hits(monkeypatch, [("A", "F/A.md"), ("B", "F/B.md")])
    monkeypatch.setattr(codedocs, "peek", lambda v: {"F/A.md": "cosmetic"})
    by_path = {h["path"]: h for h in atomic.silica_search_context("s")["hits"]}
    assert by_path["F/A.md"]["stale"] == "cosmetic"
    assert "stale" not in by_path["F/B.md"]


def test_peek_failure_never_fails_the_search(monkeypatch):
    _refs(monkeypatch, ["Alpha"])
    _hits(monkeypatch, [("A", "F/A.md")])

    def boom(v):
        raise RuntimeError("boom")

    monkeypatch.setattr(codedocs, "peek", boom)
    assert atomic.silica_search("a")["paths"] == ["F/Alpha.md"]
    assert atomic.silica_search_context("s")["hits"]
