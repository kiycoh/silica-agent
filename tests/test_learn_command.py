# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/quiz picker migration and /learn — the study-loop prompt contracts."""
from __future__ import annotations

from silica.cli import _expand_workflow_shortcut as expand


def test_quiz_untargeted_draws_from_both_picker_pools():
    out = expand("/quiz")
    assert "silica_review_queue" in out
    assert "silica_weak_notes" not in out
    assert "'due'" in out and "'unexplored'" in out


def test_quiz_grading_logs_the_full_evidence_schema():
    out = expand("/quiz")
    for field in ("concepts", "anchor"):
        assert field in out
    assert "follow-up round" in out


def test_learn_requires_a_target():
    assert "Error" in expand("/learn")


def test_learn_generates_calibrated_or_resumes():
    out = expand("/learn Concepts/ML")
    assert "Concepts/ML" in out
    assert "type: syllabus" in out            # the plan is a discoverable vault note
    assert "silica_review_queue" in out        # calibration read before generating
    assert "first unchecked step" in out       # resume path
    assert "ask whether to begin" in out       # generate-then-ask, never generate-and-go


def test_learn_teaching_discipline_matches_the_quiz_rule():
    out = expand("/learn Area/")
    assert "ONE logical step" in out
    assert "STOP" in out                       # gate questions never ship an answer key
    assert "silica_record_quiz" in out         # gates are quizzes, same ledger
    assert "silica_patch_note" in out          # a passed gate ticks the checkbox
    assert "mermaid" in out


def test_learn_names_the_props_mechanism():
    """The old contract demanded `type: syllabus` frontmatter while no tool
    could write it — the model apologized and shipped `type: Note`, so every
    /learn rebuilt the plan from scratch."""
    out = expand("/learn Concepts/ML")
    assert "props=" in out
    assert '"type": "syllabus"' in out
