# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Workspaces (ADR-0112): a `type: workspace` note's lines resolved by a walk,
one hop over `sources:`, dead lines named, writes that never touch a member."""
from __future__ import annotations

from pathlib import Path

import pytest

# the write gate registers on import
import silica.tools.notes  # noqa: F401
from silica.kernel import workspaces as wsk

WS = """---
type: workspace
notes:
- '[[Lecture 05 regression]]'
folders:
- Courses/Statistics
where:
- 'course: statistics'
---
# Statistics exam

Answer as my exam tutor: the professor wants chapters 1 to 5.
"""


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("Courses/Statistics/Bayes theorem.md", "---\ncourse: statistics\n---\n# Bayes\n")
    tmp_vault.note("Courses/Statistics/Deep/Sampling.md", "# Sampling\n")
    tmp_vault.note("Courses/Econometrics/Panel data.md", "# Panel\n")
    tmp_vault.note("Inbox/Prof handout week 3.md", "---\ncourse: [statistics, exam]\n---\n# Handout\n")
    tmp_vault.note("Inbox/Lecture 05 regression.md", "# Lecture 05\n")
    tmp_vault.note("Concepts/Residuals.md", "---\nsources:\n- Lecture 05 regression\n---\n# Residuals\n")
    tmp_vault.note("Concepts/Unrelated.md", "---\nsources:\n- Other lecture\n---\n# Unrelated\n")
    tmp_vault.note("Plans/Exam plan.md", "---\ntype: plan\ncourse: statistics\n---\n# Exam plan\n")
    tmp_vault.note("Workspaces/Statistics exam.md", WS)
    return tmp_vault


def test_members_are_the_union_of_the_three_lines_plus_one_hop_over_sources(vault):
    ws = wsk.find_workspace("[[Statistics exam]]")
    m = wsk.members(ws)
    assert set(m) == {
        "Courses/Statistics/Bayes theorem.md",       # folder and where
        "Courses/Statistics/Deep/Sampling.md",       # folder, at any depth
        "Inbox/Prof handout week 3.md",              # where, a list value matching one element
        "Inbox/Lecture 05 regression.md",            # listed
        "Concepts/Residuals.md",                     # derived from a member
    }
    assert [r["kind"] for r in m["Courses/Statistics/Bayes theorem.md"]] == ["folder", "where"]
    assert m["Concepts/Residuals.md"] == [{"kind": "derived", "via": "Lecture 05 regression"}]


def test_a_plan_or_a_workspace_note_is_never_a_member(vault):
    m = wsk.members(wsk.find_workspace("Statistics exam"))
    assert "Plans/Exam plan.md" not in m           # matches where:, but a plan is the card
    assert "Workspaces/Statistics exam.md" not in m


def test_the_body_is_read_as_instructions_and_the_h1_is_the_name(vault):
    ws = wsk.find_workspace("statistics EXAM")
    assert ws["name"] == "Statistics exam"
    assert ws["instructions"] == "Answer as my exam tutor: the professor wants chapters 1 to 5."


def test_a_line_pointing_at_nothing_is_named_not_dropped(vault):
    path = "Workspaces/Statistics exam.md"
    wsk.add_line(path, "folders", "Courses/Gone")
    wsk.add_line(path, "notes", "No such note")
    dead = wsk.dead_lines(wsk.find_workspace(path))
    assert dead == {"notes": ["No such note"], "folders": ["Courses/Gone"]}


def test_adding_and_removing_a_line_never_touches_the_member(vault):
    path = "Workspaces/Statistics exam.md"
    assert wsk.add_line(path, "notes", "Panel data") is True
    assert wsk.add_line(path, "notes", "[[panel data]]") is False      # already there, casefolded
    from silica.config import CONFIG

    root = Path(CONFIG.vault_path)
    text = (root / path).read_text(encoding="utf-8")
    assert "'[[Panel data]]'" in text                                    # a wikilink, quoted YAML
    assert "Courses/Econometrics/Panel data.md" in wsk.members(wsk.find_workspace(path))
    assert wsk.remove_line(path, "notes", "Panel data") is True
    assert "Courses/Econometrics/Panel data.md" not in wsk.members(wsk.find_workspace(path))
    assert (root / "Courses/Econometrics/Panel data.md").exists()


def test_create_writes_a_workspace_note_and_refuses_a_duplicate(vault):
    path = wsk.create_workspace("Thesis", notes=["Panel data"], folders=["Courses/Econometrics/"])
    assert path.endswith("Workspaces/Thesis.md")
    ws = wsk.find_workspace("Thesis")
    assert ws["notes"] == ["Panel data"] and ws["folders"] == ["Courses/Econometrics"]
    with pytest.raises(ValueError):
        wsk.create_workspace("thesis")
    with pytest.raises(ValueError):
        wsk.create_workspace("a/b")


def test_membership_of_marks_listed_and_other_reasons(vault):
    rows = {r["name"]: r for r in wsk.membership_of("Inbox/Lecture 05 regression.md")}
    assert rows["Statistics exam"]["listed"] is True
    rows = {r["name"]: r for r in wsk.membership_of("Concepts/Residuals.md")}
    assert rows["Statistics exam"]["listed"] is False
    assert rows["Statistics exam"]["reasons"][0]["kind"] == "derived"
    rows = {r["name"]: r for r in wsk.membership_of("Courses/Statistics/Deep", is_folder=True)}
    assert rows["Statistics exam"]["reasons"] == [{"kind": "folder", "via": "Courses/Statistics"}]


def test_a_plan_scoped_to_a_workspace_follows_its_members_live(vault):
    from silica.kernel.learn import plans

    out = plans.write_plan("Exam prep", "workspace", "Statistics exam", objectives=["explain"])
    plan = plans.read_plan(out["path"])
    assert plan["scope"] == "workspace" and plan["target"] == "[[Workspaces/Statistics exam]]"
    before = set(plans.scope_of(plan)["paths"])
    assert "Concepts/Residuals.md" in before and "Plans/Exam plan.md" not in before
    wsk.add_line("Workspaces/Statistics exam.md", "notes", "Panel data")
    assert "Courses/Econometrics/Panel data.md" in plans.scope_of(plans.read_plan(out["path"]))["paths"]
    assert plans.resolve_scope("workspace", "Nope")["gaps"] == ["no workspace named Nope"]


# --- retrieval inside a scope (point 4) -----------------------------------------

def test_the_embedding_leg_fills_k_from_members_only(tmp_path):
    from silica.kernel.recall.embed import EmbedStore

    s = EmbedStore(path=tmp_path / "e.json")
    for i, name in enumerate(["a", "b", "c", "d"]):
        s.upsert(f"F/{name}", name, [1.0, 0.1 * i, 0.0])
    top = [r["path"] for r in s.cosine_top_k([1.0, 0.0, 0.0], k=2, allow=frozenset({"F/c", "F/d"}))]
    assert top == ["F/c", "F/d"]
    assert len(s.cosine_top_k([1.0, 0.0, 0.0], k=4)) == 4          # unscoped: unchanged


def test_title_search_finds_only_members_inside_a_scope(vault):
    from silica.tools.atomic import silica_search

    ws = wsk.find_workspace("Statistics exam")
    token = wsk.open_scope(ws)
    try:
        assert silica_search("panel")["paths"] == []
        assert silica_search("bayes")["paths"] == ["Courses/Statistics/Bayes theorem.md"]
    finally:
        wsk.close_scope(token)
    assert silica_search("panel")["paths"] == ["Courses/Econometrics/Panel data.md"]


def test_bm25_is_asked_once_per_member_folder_and_cut_to_members(vault):
    from silica.tools.atomic import _scoped_search

    calls = []

    class Engine:
        def search(self, query, folder="", k=8):
            calls.append(folder)
            pool = {"Courses/Statistics": [("Courses/Statistics/Bayes theorem.md", 3.0)],
                    "Inbox": [("Inbox/Prof handout week 3.md", 5.0), ("Inbox/Other.md", 9.0)],
                    "Concepts": [("Concepts/Residuals.md", 4.0), ("Concepts/Unrelated.md", 8.0)]}
            hits = [{"path": p, "line": 1, "score": sc} for p, sc in pool.get(folder, [])]
            return {"hits": hits, "candidates": len(hits), "terms_absent": [], "index": {}}

    token = wsk.open_scope(wsk.find_workspace("Statistics exam"))
    try:
        r = _scoped_search(Engine(), "q", 8, "")
    finally:
        wsk.close_scope(token)
    assert sorted(calls) == ["Concepts", "Courses/Statistics", "Inbox"]
    assert [h["path"] for h in r["hits"]] == ["Inbox/Prof handout week 3.md", "Concepts/Residuals.md",
                                               "Courses/Statistics/Bayes theorem.md"]


def test_a_note_is_listed_by_name_unless_another_note_shares_it(vault):
    path = "Workspaces/Statistics exam.md"
    vault.note("Other/Panel data.md", "# Another panel\n")
    wsk.add_line(path, "notes", "Inbox/Prof handout week 3.md")
    wsk.add_line(path, "notes", "Courses/Econometrics/Panel data.md")
    ws = wsk.find_workspace(path)
    assert "Prof handout week 3" in ws["notes"]
    assert "Courses/Econometrics/Panel data" in ws["notes"]
    assert "Courses/Econometrics/Panel data.md" in wsk.members(ws)
    assert wsk.remove_line(path, "notes", "Courses/Econometrics/Panel data.md") is True


def test_the_file_listing_is_the_members_inside_a_scope(vault):
    from silica.tools.atomic import silica_files

    token = wsk.open_scope(wsk.find_workspace("Statistics exam"))
    try:
        out = silica_files("")
    finally:
        wsk.close_scope(token)
    assert out["workspace"] == "Statistics exam"
    assert "Courses/Econometrics/Panel data.md" not in out["files"]
    assert "Concepts/Residuals.md" in out["files"] and out["total"] == 5
    assert "Courses/Econometrics/Panel data.md" in silica_files("")["files"]
