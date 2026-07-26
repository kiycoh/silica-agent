# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/diagram — the reader shortcut that draws a note, folder, or topic in Mermaid."""
from __future__ import annotations

from silica.cli import _expand_workflow_shortcut as expand


def test_diagram_requires_a_target():
    out = expand("/diagram")
    assert "Error" in out
    assert "Usage: /diagram <note|folder|topic> [--save=<path>]" in out


def test_diagram_names_target_and_is_read_only():
    out = expand("/diagram kernel/codegraph.py")
    assert "kernel/codegraph.py" in out
    assert "mermaid" in out.lower()
    assert "READ-ONLY" in out


def test_diagram_leaves_the_type_to_the_agent():
    out = expand("/diagram Concepts/ML")
    for kind in ("flowchart", "mindmap", "sequence", "timeline"):
        assert kind in out


def test_diagram_quoted_multi_word_target():
    out = expand('/diagram "the ingest pipeline"')
    assert "the ingest pipeline" in out


def test_diagram_save_flag_swaps_read_only_for_a_write():
    out = expand('/diagram "the ingest pipeline" --save=Concepts/AI/ingest-diagram.md')
    assert "silica_write_note" in out
    assert "Concepts/AI/ingest-diagram.md" in out
    assert "READ-ONLY" not in out
    assert "--save" not in out
