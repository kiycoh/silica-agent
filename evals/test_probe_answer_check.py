# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""probe_answer_check: the ADR-0095 gate harness, on synthetic narration.

No model and no real vault: sessions are written as narration beats, the way
the GUI and the REPL record them, and the harness's own mechanics are asserted
(the evidence it builds, set R's cut, set P's draw, the label round trip and
the four ship conditions).
"""
from __future__ import annotations

import datetime
import json
import random
from pathlib import Path

import pytest

from evals import probe_answer_check as pac

DAY = datetime.datetime(2026, 9, 22, 10, 0).timestamp()
LATER = datetime.datetime(2026, 10, 20, 10, 0).timestamp()


def _session(path: Path, driver: str, ts: float, turns: list[tuple]) -> None:
    """turns: (question, [(tool, result)], answer). Beats as the narrator writes them."""
    seq = 0
    lines = []

    def beat(kind, payload, status="done", id=None):
        nonlocal seq
        seq += 1
        lines.append(json.dumps({"seq": seq, "ts": ts + seq, "kind": kind, "status": status,
                                 "id": id, "parent": None, "payload": payload}))

    beat("session", {"driver": driver, "vault": "/v"})
    beat("call", {"model": "custom/qwen3.5-9b"})
    for q, results, answer in turns:
        beat("turn", {"message": {"role": "user", "content": q}})
        for i, (name, result) in enumerate(results):
            beat("turn", {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "function": {"name": name, "arguments": "{\"x\": 9876}"}}]}})
            seq_id = f"c{seq}"
            beat("tool", {"name": name, "args": {"x": "9876", "query": "born 1548"}},
                 status="running", id=seq_id)
            beat("tool", {"name": name, "result": result}, id=seq_id)
        beat("turn", {"message": {"role": "assistant", "content": answer}})
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def narration(tmp_path):
    d = tmp_path / "narration-src"
    d.mkdir()
    mail = json.dumps({"path": "Mail.md", "body": "the sensor table\n242 columns",
                       "query": "born 1548"})
    _session(d / "aaa111.jsonl", "gui", DAY, [
        ("how many variables?", [("silica_read_note", mail)], "It has 243+ variables, born 1548."),
        # turn two reads nothing: 242 came from turn one, 9876 only from a call's
        # arguments, 243 only from the model's own earlier answer
        ("and columns?", [], "242 columns, 243 again, 9876 args."),
    ])
    _session(d / "bbb222.jsonl", "tui", DAY, [
        ("plan 45 minutes", [("silica_read_note", json.dumps({"error": "no note 31"}))],
         "45 minutes, 31 notes."),
    ])
    _session(d / "ccc333.jsonl", "mcp", DAY, [("q", [], "99 problems")])
    _session(d / "ddd444.jsonl", "gui", LATER, [("q", [], "after the cut: 77")])
    return d


@pytest.fixture
def evaluation(tmp_path):
    root = tmp_path / "evaluation"
    case_dir = root / "prompt-rules" / "qwen3.5-9b-A"
    case_dir.mkdir(parents=True)
    for n, answer in enumerate(["It says 12 and 13 and 14.", "Read 12 only.", "Nothing: 55, 56."]):
        stem = f"case-{n}"
        _session(case_dir / f"{stem}.narration.jsonl", "probe", DAY, [
            ("question one", [("silica_search", json.dumps({"hits": 12}))], "")])
        (case_dir / f"{stem}.json").write_text(json.dumps({
            "model": "custom/qwen3.5-9b", "case": stem,
            "turns": [{"user": "question one", "answer": answer}]}), encoding="utf-8")
    return root


def test_the_evidence_is_the_products(narration, tmp_path):
    real = pac.load_real(pac.copy_narration(narration, tmp_path / "scratch"))
    by_id = {a.id: a for a in real}
    # 1548 came back only as the echo of the call's own argument
    assert by_id["aaa111#0"].flagged == ["243", "1548"]
    assert by_id["aaa111#1"].flagged == ["243", "9876"]
    # a failed result is not evidence; the user's message is
    assert by_id["bbb222#0"].flagged == ["31"]
    assert "ccc333#0" not in by_id  # an MCP session is not a chat


def test_the_harness_reads_a_copy(narration, tmp_path):
    copy = pac.copy_narration(narration, tmp_path / "scratch")
    assert copy != narration
    assert sorted(p.name for p in copy.glob("*.jsonl")) == sorted(
        p.name for p in narration.glob("*.jsonl"))


def test_set_r_is_the_recorded_answers_then_the_next_ones(narration, tmp_path):
    real = pac.load_real(pac.copy_narration(narration, tmp_path / "scratch"))
    r, recorded = pac.set_r(real)
    assert recorded == 3
    assert [a.id for a in r][-1] == "ddd444#0"


def test_set_p_draws_one_number_per_answer_before_any_second(evaluation, monkeypatch):
    probes = pac.load_probes(evaluation)
    assert [a.flagged for a in probes] == [["13", "14"], [], ["55", "56"]]
    monkeypatch.setattr(pac, "P_SIZE", 3)
    picks = pac.set_p(probes)
    assert len({a.id for a, _n in picks[:2]}) == 2
    assert len(picks) == 3
    assert pac.set_p(probes) == picks  # seeded


def _label(path: Path, labels: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for rid, label in labels.items():
        head = text.index(f"## {rid} · ")
        at = text.index("- label:", head) + len("- label:")
        text = text[:at] + " " + label + text[at:]
    path.write_text(text, encoding="utf-8")


def test_build_then_score(narration, evaluation, tmp_path, monkeypatch):
    monkeypatch.setattr(pac, "R_SIZE", 4)
    out = tmp_path / "out"
    gate = pac.build(narration, evaluation, tmp_path / "scratch", out)
    assert gate["R"]["answers"] == 4
    assert gate["rates"]["R"]["warned"] == 4  # 243 + 1548; 243 + 9876; 31; 77
    assert gate["rates"]["probes"] == {"answers": 3, "warned": 2, "rate": 2 / 3, "numbers": 4}
    r_rows, p_rows = gate["R"]["rows"], gate["P"]["rows"]
    assert len(r_rows) == 6 and len(p_rows) == 4
    labels_r = (out / "labels-R.md").read_text(encoding="utf-8")
    assert "- label:\n" in labels_r and "242 columns" in labels_r

    assert pac.score(out)["verdict"] == "CLOSE"  # 2 of 3 probe answers warned: over 20%

    monkeypatch.setattr(pac, "WARN_MAX", 1.0)
    assert pac.score(out)["verdict"] == "PENDING"
    _label(out / "labels-R.md", {rid: "wrong" for rid in r_rows})
    _label(out / "labels-P.md", {rid: "outside" for rid in p_rows})
    result = pac.score(out)
    assert result["conditions"]["1_R_holds_3_wrong"] is True
    assert result["conditions"]["2_3_in_10_warned_hold_wrong"] is False  # P: 0 in 10
    assert result["verdict"] == "CLOSE"


def test_an_unknown_label_is_refused(narration, evaluation, tmp_path):
    out = tmp_path / "out"
    gate = pac.build(narration, evaluation, tmp_path / "scratch", out)
    _label(out / "labels-R.md", {next(iter(gate["R"]["rows"])): "wronng"})
    with pytest.raises(ValueError):
        pac.score(out)


def test_seeded_recall_catches_a_changed_value_only_when_it_was_never_read(tmp_path):
    a = pac.Answer(src="real", session="s", turn=0, ts=DAY, driver="gui", model="m",
                   question="q", text="242 columns, 0.025 level", web=False)
    a.evidence = {"242", "0.025", "243"}
    out = pac.seeded_recall([a], random.Random(95))
    # 243 is in the evidence, so 242 changed to 243 escapes; 0.026 is caught
    assert out["last_digit_plus_one_every"] == {"caught": 1, "of": 2, "rate": 0.5}
    assert out["decimal_point_moved"]["of"] == 2
