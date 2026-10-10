# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Frozen-web corpus: build from recordings, DDG-shape parity, registry swap."""
from __future__ import annotations

import json

import pytest

import silica.sources.web_research  # noqa: F401 — registers the web tools
from evals import web_corpus

PAGE_RUST = (
    "Source: https://a.test/rust\n\n"
    "Rust borrow checker\n"
    "The borrow checker enforces aliasing rules at compile time.\n"
)
PAGE_GC = (
    "Source: https://b.test/gc\n\n"
    "Garbage collection\n"
    "Tracing collectors reclaim unreachable memory at runtime.\n"
)


def _rec(tmp_path, name, results):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(
        {"concept": name, "trace": {f"c{i}": r for i, r in enumerate(results)}}
    ), encoding="utf-8")
    return p


def test_load_extracts_pages_first_fetch_wins_drift_counted(tmp_path):
    _rec(tmp_path, "one", [PAGE_RUST, json.dumps([{"title": "a serp"}])])
    _rec(tmp_path, "two", [PAGE_RUST.replace("compile", "run"), PAGE_GC])
    c = web_corpus.load([tmp_path])
    assert set(c.pages) == {"https://a.test/rust", "https://b.test/gc"}
    assert c.pages["https://a.test/rust"] == PAGE_RUST
    assert c.conflicts == 1


def test_search_ranks_on_topic_first_in_ddg_shape():
    c = web_corpus.Corpus({
        "https://a.test/rust": PAGE_RUST, "https://b.test/gc": PAGE_GC,
    })
    hits = c.search("borrow checker aliasing")
    assert hits[0]["url"] == "https://a.test/rust"
    assert set(hits[0]) == {"title", "url", "content"}  # _ddg_search parity
    assert hits[0]["title"] == "Rust borrow checker"
    assert "aliasing" in hits[0]["content"]
    assert len(hits[0]["content"]) <= 180
    assert c.search("zebra quantum") == []  # no overlap = empty, not an error


def test_fetch_serves_the_recording_verbatim_and_fences_the_rest():
    c = web_corpus.Corpus({"https://a.test/rust": PAGE_RUST})
    assert c.fetch("https://a.test/rust") == PAGE_RUST
    with pytest.raises(ValueError) as err:
        c.fetch("https://c.test/other")
    assert "frozen corpus" in str(err.value)


def test_install_swaps_the_registry_and_restores_it():
    from silica.tools import TOOLS

    c = web_corpus.Corpus({"https://a.test/rust": PAGE_RUST})
    real_search, real_fetch = TOOLS["web_search"].fn, TOOLS["web_fetch"].fn
    with web_corpus.install(c):
        hits = json.loads(TOOLS["web_search"].run(query="borrow checker"))
        assert hits and hits[0]["url"] == "https://a.test/rust"
        assert TOOLS["web_fetch"].run(url="https://a.test/rust") == PAGE_RUST
    assert TOOLS["web_search"].fn is real_search
    assert TOOLS["web_fetch"].fn is real_fetch
