# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/schematize, the reader skill that tables a note, folder, or topic
(silica_core/builtin_skills/schematize, ADR-0063)."""
from __future__ import annotations

from silica.cli import _expand_workflow_shortcut as expand


def test_schematize_requires_a_target():
    out = expand("/schematize")
    assert "Error" in out
    assert "Usage: /schematize <note|folder|topic> [--save=<path>]" in out


def test_schematize_names_target_and_is_read_only():
    out = expand("/schematize Concepts/ML")
    assert "Concepts/ML" in out
    assert "table" in out.lower()
    assert "READ-ONLY" in out


def test_schematize_quoted_multi_word_target():
    out = expand('/schematize "the ingest pipeline"')
    assert "the ingest pipeline" in out


def test_schematize_save_is_read_off_the_arguments():
    out = expand('/schematize "the ingest pipeline" --save=Concepts/AI/ingest.md')
    assert "--save=Concepts/AI/ingest.md" in out
    assert "unless the arguments carry --save=<path>" in out
    assert "silica_write_note" in out
