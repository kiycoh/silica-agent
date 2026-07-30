# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Stale flags on the fused-retrieval payloads (spec-stale-triggers §3).

Store-keyspace paths carry no .md; the peek map's keys do. peek_level bridges
the two, and memory-lane results are never flagged (another vault)."""
from types import SimpleNamespace

from silica.kernel.code import codedocs
from silica.tools import graph


def _r(path, origin="vault", score=0.9):
    return SimpleNamespace(path=path, name=path.rsplit("/", 1)[-1],
                           score=score, origin=origin)


def _patch_retrieve(monkeypatch, results):
    import silica.kernel.recall.perception as perception_mod
    monkeypatch.setattr(perception_mod, "facade_retrieve",
                        lambda text, k: (results, None), raising=False)


def test_stale_entry_flags_vault_results():
    m = {"wiki/m.md": "structural"}
    assert graph._stale_entry(m, _r("wiki/m")) == {"stale": "structural"}
    assert graph._stale_entry(m, _r("other/n")) == {}


def test_stale_entry_never_flags_memory_lane():
    m = {"wiki/m.md": "structural"}
    assert graph._stale_entry(m, _r("wiki/m", origin="memory")) == {}


def test_semantic_search_flags_stale_results(monkeypatch):
    _patch_retrieve(monkeypatch, [_r("wiki/m"), _r("other/n")])
    monkeypatch.setattr(codedocs, "peek", lambda v: {"wiki/m.md": "structural"})
    out = graph.silica_semantic_search("q")
    assert out["results"][0]["stale"] == "structural"
    assert "stale" not in out["results"][1]


def test_semantic_search_fresh_vault_payload_unchanged(monkeypatch):
    _patch_retrieve(monkeypatch, [_r("wiki/m")])
    monkeypatch.setattr(codedocs, "peek", lambda v: {})
    assert set(graph.silica_semantic_search("q")["results"][0]) == {
        "path", "name", "score"}


def test_peek_failure_never_fails_the_search(monkeypatch):
    _patch_retrieve(monkeypatch, [_r("wiki/m")])

    def boom(v):
        raise RuntimeError("boom")

    monkeypatch.setattr(codedocs, "peek", boom)
    assert graph.silica_semantic_search("q")["results"]
