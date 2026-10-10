# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/relate, the reader skill that maps a note's typed relationships
(silica_core/builtin_skills/relate, ADR-0063)."""
from __future__ import annotations

from silica.cli import _expand_workflow_shortcut as expand


def test_relate_requires_a_note():
    assert "Error" in expand("/relate")


def test_relate_names_note_and_is_read_only():
    out = expand("/relate Concepts/AI/RAG.md")
    assert "Concepts/AI/RAG.md" in out
    assert "top 8" in out  # default neighbor count
    assert "READ-ONLY" in out


def test_relate_passes_n_and_quoted_paths_as_typed():
    out = expand('/relate "With Spaces.md" --n=3')
    assert '"With Spaces.md" --n=3' in out
    assert "or as many as the arguments ask for with --n=" in out
