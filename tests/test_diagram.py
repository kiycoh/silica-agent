# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/diagram, the reader skill that draws a note, folder, or topic in Mermaid
(silica_core/builtin_skills/diagram, ADR-0063)."""
from __future__ import annotations

import pytest

from silica.cli import _expand_workflow_shortcut as expand


@pytest.fixture(autouse=True)
def _hosted_model(monkeypatch):
    """The skill's own wording is what these pin; on a `custom/` route
    /diagram answers with the table instead (ADR-0082, test_skill_commands)."""
    from silica.config import CONFIG
    monkeypatch.setattr(CONFIG, "model", "openrouter/anthropic/claude-opus-5-5")


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


def test_diagram_save_is_read_off_the_arguments():
    """The skill carries both clauses and the model reads --save off the
    arguments; the Python that used to pick one clause is gone (ADR-0063)."""
    out = expand('/diagram "the ingest pipeline" --save=Concepts/AI/ingest-diagram.md')
    assert "--save=Concepts/AI/ingest-diagram.md" in out
    assert "unless the arguments carry --save=<path>" in out
    assert "silica_write_note" in out
