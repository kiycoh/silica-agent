# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_search_context hits say when the file changed, who wrote the note and
what state it is in, and carry no `score` (survey card C2, owner 2026-11-04).

A superseded decision denser in the query's words than its successor ranked
first with nothing on the hit to say it was replaced.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

import silica.tools.notes  # noqa: F401  (the write gate registers on import)
from silica.config import CONFIG
from silica.tools import atomic


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("Decisions/print vendor 2025.md",
                   "---\nsuperseded_by: '[[print vendor 2026]]'\n---\n"
                   "# Print vendor\n\nThe print vendor is Harbour Print. Print vendor Harbour, "
                   "print runs at Harbour.\n")
    tmp_vault.note("Decisions/print vendor 2026.md", "# Print vendor 2026\n\nThe print vendor is now Fjord Press.\n")
    tmp_vault.note("Notes/vendor summary.md",
                   "---\nAI: true\n---\n# Vendor summary\n\nThe print vendor list was summarised.\n")
    tmp_vault.note("sources/vendor letter.md", "---\nAI: true\n---\nThe print vendor letter, verbatim.\n")
    return tmp_vault


def _by_path(out: dict) -> dict:
    return {h["path"]: h for h in out["hits"]}


def test_a_superseded_note_names_its_successor(vault):
    hit = _by_path(atomic.silica_search_context("print vendor"))["Decisions/print vendor 2025.md"]
    assert hit["lifecycle"] == "superseded"
    assert hit["superseded_by"] == "[[print vendor 2026]]"
    assert "trust" not in hit  # a person's note: the default is not spelled out


def test_a_persons_active_note_carries_only_its_date(vault):
    path = "Decisions/print vendor 2026.md"
    os.utime(Path(CONFIG.vault_path) / path, (1_767_225_600, 1_767_225_600))  # 2026-01-01
    hit = _by_path(atomic.silica_search_context("print vendor"))[path]
    assert hit["modified"] == date.fromtimestamp(1_767_225_600).isoformat()
    assert not {"trust", "lifecycle", "superseded_by", "score"} & set(hit)


def test_an_agent_note_with_no_source_says_distilled(vault):
    hit = _by_path(atomic.silica_search_context("print vendor"))["Notes/vendor summary.md"]
    assert hit["trust"] == "distilled"


def test_a_sources_copy_gets_its_date_and_no_tier(vault):
    hit = _by_path(atomic.silica_search_context("print vendor letter"))["sources/vendor letter.md"]
    assert "modified" in hit and "trust" not in hit


def test_the_literal_scan_carries_the_same_fields(vault):
    hit = _by_path(atomic.silica_search_context("Harbour Print", literal=True))["Decisions/print vendor 2025.md"]
    assert hit["lifecycle"] == "superseded" and "modified" in hit
