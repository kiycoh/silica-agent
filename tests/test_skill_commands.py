# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A SKILL.md in the vault or the home dir is a command (ADR-0063): listed
next to the Python ones and expanded by the same dispatch."""
from __future__ import annotations

from pathlib import Path

from silica.cli import _expand_workflow_shortcut as expand


def _skill(root: Path, name: str, body: str, hint: str = "") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: weekly review of the inbox\n"
        f"argument-hint: \"{hint}\"\n---\n{body}\n", encoding="utf-8")


def test_a_vault_skill_is_listed_and_expanded():
    from silica.config import CONFIG
    from silica.ui.commands import commands

    _skill(Path(CONFIG.vault_path) / ".silica" / "skills", "weekly", "Review $ARGUMENTS.", "<folder>")
    row = next(c for c in commands() if c.name == "/weekly")
    assert (row.group, row.usage) == ("workflow", "<folder>")
    assert row.summary == "weekly review of the inbox (vault skill)"
    assert expand("/weekly Inbox/") == "Review Inbox/."
    assert expand("/weekly").startswith("Error: /weekly needs 1 argument")


def test_a_skill_never_shadows_a_python_command():
    """Dispatch asks _SHORTCUTS first, so a skill named /quiz would be listed
    and never run: it is left out of the list instead."""
    from silica.kernel.recall import paths
    from silica.ui.commands import commands

    _skill(paths._SILICA_HOME / "skills", "quiz", "home quiz")
    assert [c.name for c in commands()].count("/quiz") == 1
    assert "home quiz" not in expand("/quiz Concepts/ML")


def test_an_apostrophe_reaches_a_skill_as_prose():
    out = expand("/explain dell'entropia")
    assert "dell'entropia" in out and "unbalanced quotes" not in out


def test_an_unknown_command_still_goes_to_the_agent():
    assert expand("/nosuchcommand foo") is None


def test_diagram_on_a_local_model_writes_the_table(monkeypatch):
    """ADR-0082 gate 2: the 9B's diagrams carried arrows their notes do not
    hold, so on a `custom/` route /diagram answers with /schematize's table."""
    from silica.config import CONFIG
    monkeypatch.setattr(CONFIG, "model", "custom/qwen3.5-9b")
    assert "Markdown table" in expand("/diagram Morale")
    monkeypatch.setattr(CONFIG, "model", "openrouter/anthropic/claude-opus-5-5")
    assert "```mermaid" in expand("/diagram Morale")
