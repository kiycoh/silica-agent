# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The study loop: graded answers land in the log and re-rank the attention list."""
from __future__ import annotations

import time

import pytest

from silica.kernel.report import quiz
from silica.kernel.report.graph_report import compute_report


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "quiz.jsonl"
    monkeypatch.setattr(quiz, "log_path", lambda: p)
    return p


def test_record_then_stats_counts_both_sides(log):
    assert quiz.stats() == {}  # no log yet: no measurement, not a clean slate
    quiz.record([
        {"path": "Concepts/RAG.md", "correct": False},
        {"path": "Concepts/RAG.md", "correct": True},
        {"path": "Concepts/BM25.md", "correct": False},
        {"path": "", "correct": False},  # no source note → no recall signal
    ])
    s = quiz.stats()
    assert s["concepts/rag"] == {
        "path": "Concepts/RAG.md", "misses": 1, "correct": 1, "last": s["concepts/rag"]["last"]
    }
    assert s["concepts/bm25"]["misses"] == 1
    assert len(s) == 2


def test_appends_and_survives_a_torn_line(log):
    quiz.record([{"path": "A.md", "correct": False}])
    with log.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-01-01T00:00:00+00:00", "path": "B.md", "corr\n')  # torn write
    quiz.record([{"path": "A.md", "correct": False}])
    assert quiz.stats()["a"]["misses"] == 2  # the tail is readable past the damage


def test_weakest_is_worst_first_and_ignores_the_notes_you_know(log):
    quiz.record([{"path": "Hard.md", "correct": False}] * 3)
    quiz.record([{"path": "Medium.md", "correct": False}])
    quiz.record([{"path": "Known.md", "correct": True}] * 5)
    assert [r["path"] for r in quiz.weakest()] == ["Hard.md", "Medium.md"]


def test_misses_outrank_idleness_in_the_attention_list():
    """A note answered wrong today beats a note nobody has touched for 100 days."""
    nodes = [
        {"id": "missed.md", "label": "Missed", "group": 0, "type": "note"},
        {"id": "idle.md", "label": "Idle", "group": 0, "type": "note"},
        {"id": "known.md", "label": "Known", "group": 0, "type": "note"},
    ]
    edges = [{"id": "e0", "from": "missed.md", "to": "idle.md", "type": "EXTRACTED"}]
    now = time.time()
    r = compute_report(
        _nodes_edges_override=(nodes, edges),
        analytics=True,
        _mtimes_override={
            "missed.md": now,
            "idle.md": now - 100 * 86400,
            "known.md": now - 100 * 86400,
        },
        _quiz_override={
            "missed": {"path": "missed.md", "misses": 3, "correct": 0, "last": ""},
            "known": {"path": "known.md", "misses": 0, "correct": 4, "last": ""},
        },
    )
    ranked = [a.path for a in r.attention_candidates]
    assert ranked[0] == "missed.md"                      # measured failure wins
    assert ranked.index("idle.md") < ranked.index("known.md")  # answering right sinks a note
    missed = r.attention_candidates[0]
    assert (missed.misses, missed.attempts) == (3, 3)


def test_recalling_a_note_enough_times_retires_it_from_the_failing_tier():
    nodes = [
        {"id": "learned.md", "label": "Learned", "group": 0, "type": "note"},
        {"id": "idle.md", "label": "Idle", "group": 0, "type": "note"},
    ]
    now = time.time()
    r = compute_report(
        _nodes_edges_override=(nodes, []),
        analytics=True,
        _mtimes_override={"learned.md": now, "idle.md": now - 100 * 86400},
        # missed once, then recalled twice: no longer failing more than recalling
        _quiz_override={"learned": {"path": "learned.md", "misses": 1, "correct": 2, "last": ""}},
    )
    assert [a.path for a in r.attention_candidates][0] == "idle.md"


def test_unquizzed_vault_scores_exactly_as_before():
    """No quiz history ⇒ the score stays (days_idle+1)/(1+degree), to the digit."""
    nodes = [{"id": "a.md", "label": "A", "group": 0, "type": "note"}]
    r = compute_report(
        _nodes_edges_override=(nodes, []),
        analytics=True,
        _mtimes_override={"a.md": time.time() - 9 * 86400},
        _quiz_override={},
    )
    assert r.attention_candidates[0].score == 10.0  # (9+1)/(1+0)


def test_digest_surfaces_weak_notes(log, tmp_path):
    """A missed note reaches the human through the run digest, not only /graph."""
    import silica.kernel.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = _mod.ProgressLedger.new(mode="chat", inputs={})
    assert "WEAK RECALL" not in p.digest()  # nothing graded yet: nothing to say

    quiz.record([{"path": "Concepts/RAG.md", "correct": False}])
    d = p.digest()
    assert "WEAK RECALL" in d and "Concepts/RAG.md" in d
