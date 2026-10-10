# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learner-model derived view, v2: the ledger replayed through FSRS-6 (spec
learn-suite D10). The state is still a pure function of creation dates,
authorship and quiz.jsonl; only the decay curve and the rating scale moved."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from silica.kernel.report import learner

DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ---------------------------------------------------------------- state math

def test_reader_prior_is_due_ninety_days_after_writing():
    """Writing a note seeds a Review card with S=90: R(90d) is the desired
    retention by construction, and one day later the note enters the pool."""
    now = time.time()
    st = learner.note_state(created_ts=now - 90 * DAY, is_ai=False, events=[], now_ts=now)
    assert st["S"] == 90.0
    assert math.isclose(st["R"], 0.9, abs_tol=1e-6)
    assert st["R"] >= learner.DUE_R
    later = learner.note_state(now - 91 * DAY, False, [], now)
    assert later["R"] < learner.DUE_R
    fresh = learner.note_state(now - DAY, False, [], now)
    assert fresh["R"] > st["R"]
    assert st["last"] == now - 90 * DAY and st["state"] == "review"


def test_ai_note_is_unknown_until_first_rating():
    now = time.time()
    st = learner.note_state(created_ts=now - 400 * DAY, is_ai=True, events=[], now_ts=now)
    assert st["R"] is None and st["S"] is None and st["state"] == "new"
    st = learner.note_state(now - 400 * DAY, True, [(now - 30 * DAY, 3, True)], now)
    assert st["S"] is not None and st["S"] > 0
    assert st["last"] == now - 30 * DAY
    assert 0 < st["R"] < 1


def test_ratings_order_stability_and_replay_is_deterministic():
    now = time.time()
    by_rating = {
        r: learner.note_state(now - 100 * DAY, False, [(now - 10 * DAY, r, r >= 3)], now)
        for r in (2, 3, 4)
    }
    assert by_rating[2]["S"] < by_rating[3]["S"] < by_rating[4]["S"]
    again = learner.note_state(now - 100 * DAY, False, [(now - 10 * DAY, 3, True)], now)
    twice = learner.note_state(now - 100 * DAY, False, [(now - 10 * DAY, 3, True)], now)
    assert again == twice  # fuzz off: the view is identical on every surface


def test_a_miss_never_moves_the_clock():
    """A miss measures NOT knowing: stability shrinks and R falls, but the
    last-review clock stays on the last success, so a note missed a second ago
    never reads as fresh."""
    now = time.time()
    held = learner.note_state(now - 100 * DAY, False, [(now - 10 * DAY, 3, True)], now)
    missed = learner.note_state(
        now - 100 * DAY, False, [(now - 10 * DAY, 3, True), (now - 5 * DAY, 1, False)], now
    )
    assert missed["last"] == now - 10 * DAY
    assert missed["S"] < held["S"]
    assert missed["R"] < held["R"] and missed["R"] < learner.DUE_R
    assert missed["misses"] == 1 and missed["correct"] == 1
    # an AI note with only misses is measured unknown, not unexplored
    st = learner.note_state(now - 400 * DAY, True, [(now, 1, False)], now)
    assert st["R"] == 0.0
    # and a later rating lifts it
    st = learner.note_state(now - 400 * DAY, True, [(now - DAY, 1, False), (now, 3, True)], now)
    assert st["R"] > 0.9


def test_weak_and_due_thresholds_split():
    """DUE_R (0.9) decides the session pool; WEAK_R (0.6) keeps the digest's
    WEAK RECALL line quiet about a merely aging vault."""
    assert learner.WEAK_R == 0.6 and learner.DUE_R == 0.9
    now = time.time()
    st = learner.note_state(now - 730 * DAY, False, [], now)
    assert learner.WEAK_R < st["R"] < learner.DUE_R


# --------------------------------------------------------------- the ledger

def test_legacy_lines_map_to_again_and_good():
    now = time.time()
    notes = {
        "Legacy Ok.md": {"created": now - 100 * DAY, "ai": False},
        "Rated Ok.md": {"created": now - 100 * DAY, "ai": False},
        "Legacy Miss.md": {"created": now - 100 * DAY, "ai": False},
        "Rated Miss.md": {"created": now - 100 * DAY, "ai": False},
    }
    ts = _iso(now - 10 * DAY)
    entries = [
        {"ts": ts, "path": "Legacy Ok.md", "correct": True},
        {"ts": ts, "path": "Rated Ok.md", "correct": True, "rating": 3},
        {"ts": ts, "path": "Legacy Miss.md", "correct": False},
        {"ts": ts, "path": "Rated Miss.md", "correct": False, "rating": 1},
    ]
    v = learner.view(_notes_override=notes, _entries_override=entries, now_ts=now)
    k = learner.key_of
    assert v[k("Legacy Ok.md")]["S"] == v[k("Rated Ok.md")]["S"]
    assert v[k("Legacy Miss.md")]["S"] == v[k("Rated Miss.md")]["S"]
    assert v[k("Legacy Ok.md")]["S"] > v[k("Legacy Miss.md")]["S"]


def test_rating_overrides_correct_for_scheduling():
    """A complete assisted answer is correct and receives Hard (D11): the
    schedule reads `rating`, the correctness counters read `correct`."""
    now = time.time()
    notes = {"A.md": {"created": now - 100 * DAY, "ai": False},
             "B.md": {"created": now - 100 * DAY, "ai": False}}
    ts = _iso(now - 10 * DAY)
    entries = [{"ts": ts, "path": "A.md", "correct": True, "rating": 2},
               {"ts": ts, "path": "B.md", "correct": True, "rating": 3}]
    v = learner.view(_notes_override=notes, _entries_override=entries, now_ts=now)
    a, b = v[learner.key_of("A.md")], v[learner.key_of("B.md")]
    assert a["S"] < b["S"]
    assert a["correct"] == 1 and a["misses"] == 0


def test_view_rows_carry_due_and_state():
    now = time.time()
    notes = {"A.md": {"created": now - 10 * DAY, "ai": False},
             "Bot.md": {"created": now - 10 * DAY, "ai": True}}
    v = learner.view(_notes_override=notes, _entries_override=[], now_ts=now)
    a = v[learner.key_of("A.md")]
    assert a["due"] is not None and math.isclose(a["due"], now - 10 * DAY + 90 * DAY, abs_tol=DAY)
    bot = v[learner.key_of("Bot.md")]
    assert bot["due"] is None and bot["state"] == "new" and bot["attempts"] == 0


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
    user = v[learner.key_of("User.md")]
    assert learner.WEAK_R < user["R"] < learner.DUE_R          # months old: in the pool, not the digest
    assert v[learner.key_of("Robot.md")]["R"] is None        # AI: true, never quizzed
    assert v[learner.key_of("Undated.md")]["R"] > 0.9        # mtime fallback: just written
