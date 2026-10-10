# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""G0's harness (study-mode §10): the review sheet the maintainer marks, read
back into the gate's numbers."""
from __future__ import annotations

import pytest

from evals import study_g0

LEAF_RAW = "# Continuità\n\nUna funzione è continua.\n\n## Esempio\n\nx^2 è continua.\n"


def _inv(claims, dropped=()):
    return {"leaf": "sources/L.md", "claims": claims, "dropped": list(dropped),
            "sections": [{"section": "Continuità", "sha256": "a"}, {"section": "Esempio", "sha256": "b"}]}


CLAIMS = [
    {"id": "1", "text": "Una funzione è continua", "section": "Continuità", "kind": "definition", "role": "core"},
    {"id": "2", "text": "x^2 è continua", "section": "Esempio", "kind": "example", "role": "illustrative"},
    {"id": "3", "text": "Esempio applies Continuità", "section": "Esempio", "kind": "relation", "role": "supporting",
     "span": None},
    {"id": "4", "text": "Una funzione continua ha un grafico", "section": "Continuità", "kind": "claim", "role": "core"},
]


def test_the_sheet_shows_each_section_with_its_claims_and_a_missing_slot():
    text = study_g0.sheet(_inv(CLAIMS), LEAF_RAW, {"calls": 3, "seconds": 12.0})
    assert text.startswith("# G0 review: sources/L.md\n")
    assert "## Continuità\n" in text and "> Una funzione è continua." in text
    assert "- core · definition · Una funzione è continua\n" in text
    assert text.count("missing:\n") == 2
    assert "- [ ] Esempio applies Continuità\n" in text


def test_a_cut_section_is_marked_on_its_heading():
    raw = "# Lungo\n\n" + "\n\n".join(f"{i} " + "parola " * 140 for i in range(9))
    inv = {"leaf": "sources/L.md", "claims": [], "dropped": [],
           "sections": [{"section": "Lungo", "sha256": "a"}, {"section": "Lungo (2)", "sha256": "b"}]}
    text = study_g0.sheet(inv, raw, {"calls": 1, "seconds": 1.0})
    assert "## Lungo · cut\n" in text and "## Lungo (2) · cut\n" in text


def test_a_marked_sheet_scores_missing_share_cut_recall_edges_and_span_drops():
    text = study_g0.sheet(_inv(CLAIMS, dropped=[{"text": "t", "reason": "span not in leaf", "section": "Esempio"},
                                                {"text": "h", "reason": "header apparatus", "section": "Esempio"}]),
                          LEAF_RAW, {"calls": 3, "seconds": 12.0})
    # the maintainer names one missing core concept under Esempio and confirms the edge
    marked = text.replace("- [ ] Esempio applies", "- [x] Esempio applies").replace("reviewed: no", "reviewed: yes")
    head, tail = marked.split("## Esempio\n", 1)
    tail = tail.replace("missing:\n", "missing:\n- il prodotto di continue è continua\n", 1)
    got = study_g0.score([study_g0.read_sheet(head + "## Esempio\n" + tail)],
                         [_inv(CLAIMS, dropped=[{"text": "t", "reason": "span not in leaf", "section": "Esempio"},
                                                {"text": "h", "reason": "header apparatus", "section": "Esempio"}])])
    assert (got["core"], got["missing"]) == (2, 1)
    assert got["missing_share"] == pytest.approx(1 / 3)
    assert (got["edges"], got["edges_correct"]) == (1, 1)
    # 3 claims proposed by the model and kept (the relation is rendered), 2 dropped
    assert got["span_drop_rate"] == pytest.approx(1 / 5)
    assert got["recall_cut"] is None
    assert got["verdict"]["missing"] == "reopen the decomposition prompt"


def test_an_empty_missing_slot_reads_as_nothing_missing():
    text = study_g0.sheet(_inv(CLAIMS), LEAF_RAW, {"calls": 3, "seconds": 12.0})
    got = study_g0.read_sheet(text)
    assert [s["missing"] for s in got["sections"]] == [0, 0]
    assert got["edges"] == [False]


def test_an_unreviewed_sheet_never_scores_as_a_pass():
    # an unmarked sheet has no missing line and no checked edge: read as
    # marked it would pass the missing gate before anyone looked at it
    text = study_g0.sheet(_inv(CLAIMS), LEAF_RAW, {"calls": 3, "seconds": 12.0})
    assert "reviewed: no\n" in text
    got = study_g0.score([study_g0.read_sheet(text)], [_inv(CLAIMS)])
    assert got["reviewed"] == 0
    assert got["verdict"]["missing"] == "no reviewed sheet"
    assert got["verdict"]["edges"] == "no reviewed sheet"
    reviewed = study_g0.read_sheet(text.replace("reviewed: no\n", "reviewed: yes\n"))
    assert study_g0.score([reviewed], [_inv(CLAIMS)])["verdict"]["missing"] == "pass"
