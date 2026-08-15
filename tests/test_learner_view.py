# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learner-model derived view: R = exp(-dt/S) over creation dates, authorship
and the graded-quiz ledger (docs/specs/learner-model.md)."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from silica.kernel.report import learner

DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ---------------------------------------------------------------- state math

def test_user_note_decays_from_its_creation_date():
    now = time.time()
    st = learner.note_state(created_ts=now - 90 * DAY, is_ai=False, events=[], now_ts=now)
    assert math.isclose(st["R"], math.exp(-1), rel_tol=1e-6)  # dt = S0 = 90d
    fresh = learner.note_state(created_ts=now - DAY, is_ai=False, events=[], now_ts=now)
    assert fresh["R"] > 0.9


def test_ai_note_is_unknown_until_first_graded_answer():
    now = time.time()
    st = learner.note_state(created_ts=now - 400 * DAY, is_ai=True, events=[], now_ts=now)
    assert st["R"] is None  # no creation credit: unknown, not forgotten
    st = learner.note_state(
        created_ts=now - 400 * DAY, is_ai=True, events=[(now - 30 * DAY, True)], now_ts=now
    )
    assert st["S"] == 30.0  # first correct grants S0_AI, dated at the answer
    assert math.isclose(st["R"], math.exp(-1), rel_tol=1e-6)


def test_correct_doubles_stability_and_a_miss_quarters_it():
    now = time.time()
    st = learner.note_state(now - 100 * DAY, False, [(now - 10 * DAY, True)], now)
    assert st["S"] == 180.0  # 90 * 2
    assert math.isclose(st["R"], math.exp(-10 / 180), rel_tol=1e-6)
    st = learner.note_state(
        now - 100 * DAY, False, [(now - 10 * DAY, True), (now - 5 * DAY, False)], now
    )
    assert st["S"] == 45.0  # 180 / 4
    assert st["R"] == 0.3   # trailing miss caps the estimate below the due bar


def test_a_miss_never_makes_a_note_look_fresh():
    """A miss measures NOT knowing: it must not reset the decay clock."""
    now = time.time()
    st = learner.note_state(now - DAY, False, [(now, False)], now)
    assert st["R"] < learner.DUE_R  # missed a second ago: due, however fresh the note
    st = learner.note_state(now - 400 * DAY, True, [(now, False)], now)
    assert st["R"] == 0.0  # AI note, only misses: measured unknown, not unexplored
    # and a later correct answer lifts it back out
    st = learner.note_state(now - 400 * DAY, True, [(now - DAY, False), (now, True)], now)
    assert st["R"] > 0.9


# ------------------------------------------------------------ view and pools

def test_review_queue_mixes_due_and_unexplored_pools():
    now = time.time()
    notes = {
        "Old User.md": {"created": now - 400 * DAY, "ai": False},
        "Fresh User.md": {"created": now - DAY, "ai": False},
        "AI Note.md": {"created": now - DAY, "ai": True},
        "Known.md": {"created": now - 400 * DAY, "ai": False},
    }
    entries = [{"ts": _iso(now - DAY), "path": "Known.md", "correct": True}]
    rows = learner.review_queue(
        limit=4, _notes_override=notes, _entries_override=entries, now_ts=now
    )
    whys = {r["path"]: r["why"] for r in rows}
    assert whys["Old User.md"] == "due"            # prior decayed below threshold
    assert whys["AI Note.md"] == "unexplored"      # zero evidence, no prior
    assert whys["Fresh User.md"] == "unexplored"   # unvalidated prior = probe target
    assert "Known.md" not in whys                  # recalled yesterday: not surfaced
    unexplored = [r["path"] for r in rows if r["why"] == "unexplored"]
    assert unexplored[0] == "AI Note.md"           # unknown beats unvalidated prior


def test_review_queue_target_mode_reports_every_note_in_scope():
    now = time.time()
    notes = {
        "Area/Old.md": {"created": now - 400 * DAY, "ai": False},
        "Area/Fresh.md": {"created": now - DAY, "ai": False},
        "Elsewhere/Note.md": {"created": now - 400 * DAY, "ai": False},
    }
    rows = learner.review_queue(
        target="Area/", _notes_override=notes, _entries_override=[], now_ts=now
    )
    assert {r["path"] for r in rows} == {"Area/Old.md", "Area/Fresh.md"}
    fresh = next(r for r in rows if r["path"] == "Area/Fresh.md")
    assert fresh["why"] == "unexplored" and fresh["R"] > 0.9  # syllabus calibration data


def test_ledger_joins_no_matter_the_path_spelling():
    """Entries logged with wikilink case or missing .md still hit the note."""
    now = time.time()
    notes = {"Concepts/RAG.md": {"created": now - 400 * DAY, "ai": False}}
    entries = [{"ts": _iso(now - DAY), "path": "concepts/rag", "correct": True}]
    v = learner.view(_notes_override=notes, _entries_override=entries, now_ts=now)
    row = v[learner.key_of("Concepts/RAG.md")]
    assert row["correct"] == 1 and row["R"] > 0.9


def test_unexplored_ranking_prefers_unmeasured_central_concepts():
    now = time.time()

    class FakeStore:
        lang = "en"

        def note_nodes(self, path):
            return {"central": {"eigenvector": 5.0}, "peripheral": {"trivia": 1.0}}[path.split("/")[0]]

        def adjacency(self):
            return {"eigenvector": {"basis": 9.0}, "trivia": {"misc": 0.5}}

    notes = {
        "peripheral/a.md": {"created": now - DAY, "ai": False},
        "central/b.md": {"created": now - DAY, "ai": False},
    }
    rows = learner.review_queue(
        limit=2, _notes_override=notes, _entries_override=[], now_ts=now, _store=FakeStore()
    )
    assert [r["path"] for r in rows] == ["central/b.md", "peripheral/a.md"]


def test_view_reads_frontmatter_from_the_vault(tmp_vault, monkeypatch):
    import silica.kernel.report.quiz as quiz
    monkeypatch.setattr(quiz, "log_path", lambda: __import__("pathlib").Path("/nonexistent/quiz.jsonl"))
    tmp_vault.note("User.md", "---\ndate: 2026-01-01\n---\nwritten by hand\n")
    tmp_vault.note("Robot.md", "---\nAI: true\n---\nnucleated\n")
    tmp_vault.note("Undated.md", "no frontmatter at all\n")
    v = learner.view()
    assert 0 < v[learner.key_of("User.md")]["R"] < 0.6      # months old: decayed
    assert v[learner.key_of("Robot.md")]["R"] is None        # AI: true, never quizzed
    assert v[learner.key_of("Undated.md")]["R"] > 0.9        # mtime fallback: just written
