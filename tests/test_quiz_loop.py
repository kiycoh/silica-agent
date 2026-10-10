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


def test_record_carries_concepts_question_and_anchor(log):
    """New optional fields land in the log; absent fields keep the legacy line shape."""
    quiz.record([
        {"path": "Math/Linear Algebra.md", "correct": True,
         "concepts": ["eigenvector", "basis"], "q": "What is an eigenvector?",
         "anchor": "#Definitions"},
        {"path": "Math/Calculus.md", "correct": False},  # legacy-shaped entry
    ])
    lines = [__import__("json").loads(l) for l in log.read_text().splitlines()]
    assert lines[0]["concepts"] == ["eigenvector", "basis"]
    assert lines[0]["q"] == "What is an eigenvector?"
    assert lines[0]["anchor"] == "#Definitions"
    assert set(lines[1]) == {"ts", "path", "correct"}  # no empty keys smuggled in
    # stats() must not choke on the richer lines
    assert quiz.stats()["math/linear algebra"]["correct"] == 1


def test_entries_returns_ordered_events_and_survives_damage(log):
    quiz.record([{"path": "A.md", "correct": False, "concepts": ["rag"]}])
    with log.open("a", encoding="utf-8") as f:
        f.write('{"ts": "2026-01-01T00:00:00+00:00", "path": "B.md", "corr\n')  # torn
    quiz.record([{"path": "A.md", "correct": True, "q": "second round"}])
    ev = quiz.entries()
    assert [e["correct"] for e in ev] == [False, True]  # oldest first, torn line skipped
    assert ev[0]["concepts"] == ["rag"] and ev[1]["q"] == "second round"
    assert quiz.entries() == sorted(ev, key=lambda e: e["ts"])


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


def _vault_note(rel: str) -> None:
    from pathlib import Path

    from silica.config import CONFIG

    p = Path(CONFIG.vault_path) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ndate: 2024-01-01\n---\nbody\n", encoding="utf-8")


def test_digest_surfaces_weak_notes(log, tmp_path):
    """A missed note reaches the human through the run digest, not only /graph."""
    import silica.kernel.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = _mod.ProgressLedger.new(mode="chat", inputs={})
    assert "WEAK RECALL" not in p.digest()  # nothing graded yet: nothing to say

    _vault_note("Concepts/RAG.md")
    quiz.record([{"path": "Concepts/RAG.md", "correct": False}])
    d = p.digest()
    assert "WEAK RECALL" in d and "Concepts/RAG.md" in d


def test_digest_retires_a_note_recalled_since_the_miss(log, tmp_path):
    """One old miss must not haunt the digest after the reader re-learns the note."""
    import silica.kernel.progress as _mod
    _mod._RUNS_DIR = tmp_path

    _vault_note("Relearned.md")
    _vault_note("Struggling.md")
    quiz.record([{"path": "Relearned.md", "correct": False}])
    quiz.record([{"path": "Relearned.md", "correct": True}] * 3)  # stability recovered
    quiz.record([{"path": "Struggling.md", "correct": False}])

    d = _mod.ProgressLedger.new(mode="chat", inputs={}).digest()
    assert "Struggling.md" in d
    assert "Relearned.md" not in d  # retention re-measured high: nothing to nag about


def test_record_carries_the_v2_fields_and_keeps_legacy_lines(log):
    """learn-suite D11: rating, kind, level, conf, source, grader, hints,
    told, paraphrase, misconception, session, plan ride on the same line;
    `correct` is the rubric and is never recomputed from `rating`."""
    import json

    quiz.record([
        {"path": "ML/Softmax.md", "correct": True, "rating": 2, "kind": "explain",
         "level": 3, "conf": 70, "source": {"leaf": "sources/Lezione 4.md", "line": 42},
         "grader": "model", "grader_rating": 3, "hints": 1, "told": False,
         "paraphrase": True, "misconception": "", "session": "abc", "plan": "Plans/ML.md",
         "q": "Why?", "anchor": "#Mechanism", "concepts": ["softmax"]},
        {"path": "ML/Loss.md", "correct": False, "rating": 9, "conf": 33},  # invalid values dropped
        {"path": "ML/Told.md", "correct": False, "told": True, "conf": None, "grader": "told"},
    ])
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    v2 = lines[0]
    assert v2["rating"] == 2 and v2["correct"] is True      # correct assisted answer, Hard
    assert v2["kind"] == "explain" and v2["level"] == 3 and v2["conf"] == 70
    assert v2["source"] == {"leaf": "sources/Lezione 4.md", "line": 42}
    assert v2["grader"] == "model" and v2["grader_rating"] == 3
    assert v2["hints"] == 1 and v2["told"] is False and v2["paraphrase"] is True
    assert "misconception" not in v2                        # empty observation omitted
    assert v2["session"] == "abc" and v2["plan"] == "Plans/ML.md"
    assert "rating" not in lines[1] and "conf" not in lines[1]
    assert lines[2]["told"] is True and lines[2]["conf"] is None and lines[2]["grader"] == "told"
    # entries() hands the richer lines back untouched, and stats() still counts them
    assert quiz.entries()[0]["source"]["line"] == 42
    assert quiz.stats()["ml/softmax"]["correct"] == 1


def test_replay_over_a_mixed_ledger_is_byte_identical_and_drops_nothing(log):
    """G1: legacy and v2 lines replay together; two reads give the same view."""
    import json
    import time

    from silica.kernel.report import learner

    quiz.record([{"path": "A.md", "correct": True}])
    quiz.record([{"path": "A.md", "correct": False, "rating": 1, "kind": "recall", "conf": 90}])
    quiz.record([{"path": "B.md", "correct": True, "rating": 4, "kind": "cloze", "grader": "self"}])
    assert len(quiz.entries()) == 3
    now = time.time()
    notes = {"A.md": {"created": now - 100 * 86400, "ai": False},
             "B.md": {"created": now - 100 * 86400, "ai": True}}
    one = json.dumps(learner.view(now_ts=now, _notes_override=notes), sort_keys=True)
    two = json.dumps(learner.view(now_ts=now, _notes_override=notes), sort_keys=True)
    assert one == two
    v = learner.view(now_ts=now, _notes_override=notes)
    assert v["a"]["attempts"] == 2 and v["b"]["attempts"] == 1
