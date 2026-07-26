# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/schematize — the reader shortcut that tables a note, folder, or topic."""
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


def test_schematize_save_flag_swaps_read_only_for_a_write():
    out = expand('/schematize "the ingest pipeline" --save=Concepts/AI/ingest.md')
    assert "silica_write_note" in out
    assert "Concepts/AI/ingest.md" in out
    assert "READ-ONLY" not in out
    assert "--save" not in out  # the flag is consumed, not part of the target
