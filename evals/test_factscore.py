# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Mechanics of the FActScore harness — parsing, scoring math, pair mapping.
No network: the _llm seam is monkeypatched."""
from __future__ import annotations

from evals import factscore


def _fake_llm(decompose_reply, judge_reply):
    def fake(model, prompt, max_tokens):
        return decompose_reply if "atomic facts" in prompt else judge_reply
    return fake


def test_score_note_math(monkeypatch):
    monkeypatch.setattr(factscore, "_llm", _fake_llm(
        "- Alice moved to Rome\n- Alice has a dog\nnoise line\n- Bob paints",
        "1: yes\n2: no\n3: yes"))
    row = factscore.score_note("m", "body", "src")
    assert row["facts"] == 3 and row["judged"] == 3
    assert row["supported"] == 2 and row["score"] == 2 / 3
    assert row["unsupported"] == ["Alice has a dog"]
    assert row["judge_failures"] == 0 and row["error"] is None


def test_verdict_gaps_are_judge_failures():
    v = factscore._parse_verdicts("1: yes\n3: No\n9: yes", 3)
    assert v == [True, None, False]


def test_decompose_failure_excluded(monkeypatch):
    monkeypatch.setattr(factscore, "_llm", lambda *a: "")
    row = factscore.score_note("m", "body", "src")
    assert row["error"] == "decompose_failed" and row["score"] is None
    assert factscore.aggregate([row])["micro_factscore"] is None
    assert factscore.aggregate([row])["notes_error"] == 1


def test_judge_chunking(monkeypatch):
    calls = []

    def fake(model, prompt, max_tokens):
        calls.append(prompt)
        return "\n".join(f"{i + 1}: yes" for i in range(25))

    monkeypatch.setattr(factscore, "_llm", fake)
    verdicts = factscore.judge_facts("m", [f"f{i}" for i in range(60)], "src")
    assert len(calls) == 3 and len(verdicts) == 60
    assert sum(v is True for v in verdicts) == 25 + 25 + 10


def test_aggregate_micro_vs_macro():
    rows = [{"judged": 10, "supported": 10, "score": 1.0, "facts": 10,
             "judge_failures": 0, "error": None},
            {"judged": 2, "supported": 1, "score": 0.5, "facts": 2,
             "judge_failures": 0, "error": None}]
    m = factscore.aggregate(rows)
    assert m["micro_factscore"] == 11 / 12 and m["macro_factscore"] == 0.75


def test_locomo_note_pairs(tmp_path):
    vault = tmp_path / "vault"
    (vault / "sessions").mkdir(parents=True)
    (vault / "sources").mkdir()
    (vault / ".silica").mkdir()
    (vault / "sessions" / "s0000.md").write_text(
        "---\nsession_id: \"session_1\"\ndate: \"2023-05-08\"\n---\n\n"
        "Distilled body.\n\n## Sources\n[[session_1]]\n", encoding="utf-8")
    (vault / "sources" / "session_1.md").write_text("verbatim leaf", encoding="utf-8")
    (vault / ".silica" / "internal.md").write_text("internal", encoding="utf-8")
    # FSM vault residue: archived + leftover inbox transcripts carry the same
    # session_id frontmatter and would self-score 1.0 — must be skipped.
    for d in ("done", "inbox"):
        (vault / d).mkdir()
        (vault / d / "session_1.md").write_text(
            "---\nsession_id: \"session_1\"\n---\nAlice: hi\n", encoding="utf-8")
    # No session_id: an entity/merged note, judged against the full conversation.
    (vault / "entity.md").write_text("no attribution\n", encoding="utf-8")
    (vault / "empty.md").write_text("---\nx: 1\n---\n", encoding="utf-8")  # bodyless
    inst = {"conversation": {
        "session_1": [{"speaker": "Alice", "text": "hi"}],
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_2": [{"speaker": "Bob", "text": "bye"}],
        "session_2_date_time": "2:00 pm on 9 May, 2023"}}
    pairs, unmapped = factscore.locomo_note_pairs(vault, inst)
    by = {p["rel"]: p for p in pairs}
    # flat note keeps strict 1:1 attribution to its own session
    assert by["sessions/s0000"]["sessions"] == ["session_1"]
    assert by["sessions/s0000"]["body"] == "Distilled body."  # frontmatter + Sources stripped
    assert by["sessions/s0000"]["source"] == "Alice: hi"
    # entity note -> full conversation, in numeric session order, not dropped
    assert by["entity"]["sessions"] == [factscore._FULL_CONV]
    assert by["entity"]["source"] == "Alice: hi\n\nBob: bye"
    assert unmapped == ["empty"]  # only bodyless notes are unmapped now
