# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/quiz picker migration and /learn v2 — the study-loop prompt contracts
(learn-suite D20, §4), pinned by substring."""
from __future__ import annotations

from silica.cli import _expand_workflow_shortcut as expand

CONF_LABELS = ("90", "70", "50", "25")


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


def test_quiz_on_a_passage_is_a_short_round_on_the_note():
    """The drawer's selection bar (ADR-0059): the passage narrows the questions,
    the note stays the subject, so the ledger line still carries its path."""
    out = expand('/quiz "ML/Trees.md" --focus="A tree is a connected\nacyclic graph."')
    assert out.startswith("Run a 3-question")
    assert "`ML/Trees.md`" in out and "> A tree is a connected\n> acyclic graph." in out
    assert "silica_record_quiz" in out and "path" in out
    assert expand('/quiz "ML/Trees.md" --focus="x" --n=5').startswith("Run a 5-question")
    assert expand('/quiz "ML/Trees.md"').startswith("Run a 10-question")


# ------------------------------------------------------------ the session

def test_learn_free_session_calls_the_engine_and_asks_one_item_at_a_time():
    out = expand("/learn")
    assert "silica_learn_items" in out
    assert "ONE item" in out and "STOP" in out
    for label in CONF_LABELS:
        assert label in out                      # the four fixed confidence bands
    assert "Skip" in out                         # null when skipped
    for field in ("rating", "conf", "source", "grader", "hints", "told", "session"):
        assert field in out                      # the v2 ledger line
    assert "silica_record_quiz" in out
    assert "silica_read_note" in out             # the span is quoted from the leaf
    assert "answer" in out.lower() and "never" in out.lower()  # no key in the cue message
    assert "Just tell me" in out and "told" in out
    assert "n=8" in out or "n=8" in out.replace(" ", "")


def test_learn_session_size_flag_and_plan_target():
    out = expand("/learn --n=12")
    assert "n=12" in out.replace(" ", "")
    out = expand("/learn Machine Learning")
    assert "Machine Learning" in out
    # a plan by that name continues; none yet writes one over that scope
    assert "silica_learn_items" in out or '"type": "plan"' in out


def test_learn_plan_writer_trusts_the_queue_order_and_carries_a_because_clause():
    out = expand("/learn --plan ML --scope=ML/")
    assert "silica_review_queue" in out and "target=" in out
    assert "topological" in out or "already in prerequisite order" in out
    assert "## Steps" in out                     # a folder scope orders itself: no steps
    assert '"type": "plan"' in out and '"scope": "folder"' in out and '"target": "ML/"' in out
    assert "silica_write_note" in out and "props=" in out
    assert "the graph stores none" not in out    # the v1 lie is gone
    assert "ask whether to begin" in out


def test_learn_steps_plan_keeps_gaps_and_restricts_to_a_scope():
    out = expand('/learn --plan Esame --steps="Programma esame" --scope=ML/')
    assert '"scope": "steps"' in out and "Programma esame" in out
    assert "not in the vault" in out            # a step with no covering note is a gap, kept
    assert "ML/" in out


def test_learn_v1_target_form_writes_a_steps_plan():
    out = expand("/learn Concepts/ML")
    assert "Concepts/ML" in out
    assert '"scope": "steps"' in out and '"type": "plan"' in out
    assert "because" in out and "silica_review_queue" in out


def test_learn_teach_back_hides_the_source_from_the_child():
    out = expand("/learn --teach Softmax")
    assert "Softmax" in out
    assert "4 turns" in out or "four turns" in out
    assert "title and" in out and "never" in out    # the child sees the title and concepts only
    assert "summary" in out and "silica_record_quiz" in out
    assert '"kind": "teach"' in out or "kind: teach" in out or "teach" in out


def test_learn_status_relays_the_readings_line(monkeypatch):
    from silica.kernel.learn import mastery

    monkeypatch.setattr(mastery, "status_line", lambda plan="": "12 measured · 3 due · calibration: not enough evidence")
    out = expand("/learn --status")
    assert "12 measured" in out


def test_review_docstring_names_the_deferred_store():
    from silica.cli import _dc_review

    assert "deferred" in _dc_review.__doc__.lower()
    assert "learner" not in _dc_review.__doc__.lower()


# --------------------------------------------------------------- the hook

def test_session_start_hook_counts_due_notes_only(tmp_path, monkeypatch):
    """D18: one line, a count and a minute estimate, never a path."""
    import json
    import os
    import time

    from silica import hook as hook_mod
    from silica.kernel.report import quiz

    vault = tmp_path / "v"
    vault.mkdir()
    (vault / "vault.yaml").write_text("name: v\n")
    (vault / ".silica").mkdir()
    old = time.time() - 400 * 86400
    for i in range(3):
        p = vault / f"n{i}.md"
        p.write_text("---\ndate: 2024-01-01\n---\n# n\n\nx\n", encoding="utf-8")
        os.utime(p, (old, old))
    log = tmp_path / "quiz.jsonl"
    monkeypatch.setattr(quiz, "log_path", lambda: log)
    from silica.config import CONFIG
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    out = hook_mod.session_start(json.dumps({"cwd": str(vault)}))
    assert "due for review" not in out            # nothing measured yet
    quiz.record([{"path": "n0.md", "correct": False, "rating": 1}, {"path": "n1.md", "correct": False, "rating": 1}])
    out = hook_mod.session_start(json.dumps({"cwd": str(vault)}))
    assert "2 notes due for review · ~1 min · /learn" in out
    assert "n0.md" not in out
