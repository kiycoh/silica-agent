# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_search_context(literal=True) finds one exact string (ADR-0149).

BM25 splits `2022-009` into `2022` and `009`, so a note holding the two apart
ranked as a match. The literal scan reads note bodies the turn may see and
answers in the ranked reply's shape, so recall_watch keeps one miss signal.
"""
from __future__ import annotations

import json
import os

import pytest

import silica.tools.notes  # noqa: F401  (the write gate registers on import)
from silica.tools import found_nothing
from silica.kernel import workspaces as wsk
from silica.tools import atomic


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("Contracts/Northwind.md", "# Northwind\n\nOrder Northwind 2022-009, delivery June 2022.\n")
    tmp_vault.note("Contracts/Other.md", "# Other\n\nIn 2022 we shipped order 009 and order 2022-0091.\n")
    tmp_vault.note("Notes/Mail.md", "# Mail\n\nWrite to Marta@Example.org about NORTHWIND 2022-009.\n")
    tmp_vault.note(".obsidian/cache.md", "2022-009\n")
    tmp_vault.note("sources/raw.md", "verbatim: northwind 2022-009 clause 4\n")
    return tmp_vault


def _lit(q, **kw):
    return atomic.silica_search_context(q, literal=True, **kw)


def test_the_exact_string_and_only_it(vault):
    out = _lit("2022-009 ")  # a trailing space from the model is not part of the string
    assert out["literal"] is True
    # Other.md holds "2022-0091": a substring match, and that is what exact means here.
    assert {h["path"] for h in out["hits"]} == {"Contracts/Northwind.md", "Notes/Mail.md",
                                                "sources/raw.md", "Contracts/Other.md"}
    assert out["notes_matched"] == 4 and out["terms_absent"] == []
    assert not any(h["path"].startswith(".obsidian") for h in out["hits"])


def test_case_is_ignored_and_the_line_is_the_one_silica_read_numbers(vault):
    out = _lit("marta@example.org")
    [hit] = out["hits"]
    assert hit["path"] == "Notes/Mail.md" and hit["line"] == 3
    read = atomic.silica_read(hit["path"], start=hit["line"], end=hit["line"])
    assert "Marta@Example.org" in json.dumps(read)
    # The version silica_read computes, so it can be carried as expect_version.
    assert hit["version"] == read["version"]
    assert "error" not in atomic.silica_read(hit["path"], expect_version=hit["version"])


def test_a_formula_bm25_has_no_token_for_is_found(vault):
    from silica_core.core import _tokens

    vault.note("Math/RNN.md", "# RNN\n\nThe state is h_t = tanh(W x_{t-1} + b).\n")
    assert _tokens("x_{t-1}") == []  # why the ranked search cannot look for it
    assert [h["path"] for h in _lit("x_{t-1}")["hits"]] == ["Math/RNN.md"]


def test_a_miss_is_the_miss_recall_watch_already_reads(vault):
    out = _lit("2023-417")
    assert out["hits"] == [] and out["notes_matched"] == 0
    assert out["terms_absent"] == ["2023-417"]
    assert found_nothing("silica_search_context", json.dumps(out))


def test_folder_and_workspace_cut_the_scan(vault):
    assert {h["path"] for h in _lit("2022-009", folder="Notes")["hits"]} == {"Notes/Mail.md"}
    vault.note("Workspaces/Deals.md", "---\ntype: workspace\nnotes: []\nfolders:\n- Contracts\nwhere: []\n---\n# Deals\n")
    tok = wsk.open_scope(wsk.find_workspace("Deals"))
    try:
        out = _lit("2022-009")
    finally:
        wsk.close_scope(tok)
    assert {h["path"] for h in out["hits"]} == {"Contracts/Northwind.md", "Contracts/Other.md"}


def test_a_link_out_of_the_vault_is_not_read(vault, tmp_path):
    secret = tmp_path / "outside.md"
    secret.write_text("password 2022-009\n", encoding="utf-8")
    from silica.config import CONFIG

    os.symlink(secret, os.path.join(CONFIG.vault_path, "Contracts", "link.md"))
    assert "Contracts/link.md" not in {h["path"] for h in _lit("2022-009")["hits"]}


def test_more_occurrences_rank_first_and_k_caps_the_lines(vault):
    vault.note("Contracts/Log.md", "# Log\n\n2022-009 a\n2022-009 b\n2022-009 c\n2022-009 d\n")
    out = _lit("2022-009", k=2)
    assert [h["path"] for h in out["hits"]] == ["Contracts/Log.md", "Contracts/Log.md"]
    assert "5 notes hold the string" in out["truncated"]
    # Three lines per note at most, whatever k allows.
    assert len([h for h in _lit("2022-009", k=50)["hits"] if h["path"] == "Contracts/Log.md"]) == 3


@pytest.mark.parametrize("q", ["", "x", "y" * 201])
def test_a_string_out_of_bounds_is_refused(vault, q):
    assert _lit(q)["error"]["code"] == "bad_query"


def test_the_clock_says_what_it_did_not_scan(vault, monkeypatch):
    monkeypatch.setattr(atomic, "_LITERAL_BUDGET_S", -1.0)
    out = _lit("2022-009")
    assert out["hits"] == [] and "not; narrow with folder" in out["scan_cut"]


def test_ranked_search_is_unchanged_by_default(vault, monkeypatch):
    called = []
    monkeypatch.setattr(atomic, "_literal_search", lambda *a: called.append(a) or {})
    atomic.silica_search_context("northwind order")
    assert called == []
