"""ADR-0097 point 4: Settings lists every skill and installs one SKILL.md into
the vault, after a preview, by the loader's own rules."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

FLASHCARDS = """---
name: flashcards
description: Turn a note into question and answer cards.
argument-hint: <note> [--n=12]
---
<!-- one fact per card -->
Read $0 with silica_read_note before writing a card.
"""

EXPLAIN = FLASHCARDS.replace("flashcards", "explain")


@pytest.fixture
def tc(tmp_vault, tmp_path, monkeypatch):
    from silica_core.kernel.recall import paths

    from silica.ui.web import server

    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path / "home")
    server._reset_session()
    return TestClient(server.app)


def _post(tc, text: str, confirm: bool = False, name: str = "SKILL.md"):
    return tc.post("/skills", files={"file": (name, text.encode(), "text/markdown")},
                   data={"confirm": "true" if confirm else "false"})


def _vault_dir():
    from silica.config import CONFIG

    from pathlib import Path

    return Path(CONFIG.vault_path) / ".silica" / "skills"


def test_the_preview_writes_nothing_and_the_confirm_writes_the_file(tc):
    r = _post(tc, FLASHCARDS)
    assert r.status_code == 200, r.text
    skill = r.json()["skill"]
    assert r.json()["installed"] is False and not _vault_dir().exists()
    assert skill["name"] == "flashcards" and skill["hint"] == "<note> [--n=12]"
    assert skill["path"] == ".silica/skills/flashcards/SKILL.md"
    assert "one fact per card" not in skill["body"] and skill["stripped"] is True
    assert skill["replaces"] is False and skill["shadows"] == ""

    r = _post(tc, FLASHCARDS, confirm=True)
    assert r.json()["installed"] is True
    assert (_vault_dir() / "flashcards" / "SKILL.md").read_text() == FLASHCARDS
    assert "/flashcards" in [c["name"] for c in tc.get("/commands").json()]
    assert _post(tc, FLASHCARDS).json()["skill"]["replaces"] is True


def test_a_vault_skill_replaces_the_built_in_and_the_list_says_so(tc):
    assert _post(tc, EXPLAIN).json()["skill"]["shadows"] == "builtin"
    _post(tc, EXPLAIN, confirm=True)
    groups = {g["source"]: g["skills"] for g in tc.get("/skills").json()["groups"]}
    mine = next(s for s in groups["vault"] if s["name"] == "explain")
    builtin = next(s for s in groups["builtin"] if s["name"] == "explain")
    assert mine["state"] == "active" and mine["shadows"] == ["builtin"]
    assert builtin["state"] == "replaced" and builtin["by"] == "vault"


def test_a_skill_placed_by_hand_under_a_command_name_is_listed_inactive(tc):
    folder = _vault_dir() / "quiz"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(FLASHCARDS.replace("flashcards", "quiz"))
    groups = {g["source"]: g["skills"] for g in tc.get("/skills").json()["groups"]}
    assert [s["state"] for s in groups["vault"] if s["name"] == "quiz"] == ["inactive"]


@pytest.mark.parametrize("text, words", [
    ("# Flashcards\n\nno frontmatter\n", "no frontmatter"),
    ("---\ndescription: x\n---\nbody\n", "no name:"),
    ("---\nname: Exam Prep\n---\nbody\n", "is not a skill name"),
    ("---\nname: ../../etc\n---\nbody\n", "is not a skill name"),
    ("---\nname: quiz\n---\nbody\n", "built-in command"),
    ("---\nname: [x\n---\nbody\n", "not valid YAML"),
    ("---\nname: big\n---\n" + "x" * (33 * 1024), "over 32 KiB"),
])
def test_a_refusal_names_the_check_and_writes_nothing(tc, text, words):
    r = _post(tc, text, confirm=True)
    assert r.status_code in (400, 413) and words in r.json()["detail"]
    assert not _vault_dir().exists()


def test_remove_deletes_the_skill_and_keeps_its_authors_other_files(tc):
    _post(tc, FLASHCARDS, confirm=True)
    (_vault_dir() / "flashcards" / "notes.txt").write_text("mine")
    r = tc.delete("/skills/flashcards")
    assert r.json() == {"ok": True, "kept": ["notes.txt"]}
    assert not (_vault_dir() / "flashcards" / "SKILL.md").exists()
    assert "/flashcards" not in [c["name"] for c in tc.get("/commands").json()]

    _post(tc, EXPLAIN, confirm=True)
    assert tc.delete("/skills/explain").json()["kept"] == []
    assert not (_vault_dir() / "explain").exists()
    assert tc.delete("/skills/explain").status_code == 404
    assert tc.delete("/skills/%2E%2E").status_code == 404
    assert tc.delete("/skills/Not_a_name").status_code == 404


def test_a_skills_folder_linked_outside_the_vault_is_not_written_through(tc, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (_vault_dir().parent).mkdir(parents=True)
    _vault_dir().symlink_to(outside)
    r = _post(tc, FLASHCARDS, confirm=True)
    assert r.status_code == 400 and "outside the vault" in r.json()["detail"]
    assert list(outside.iterdir()) == []
