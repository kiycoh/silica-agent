# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""probe_web_gate: record/replay harness for the L2 gate (spec §6).

No network and no LLM: record is tested against a faked research loop, replay
against faked composition and judge calls. What is asserted is the harness's
own mechanics — the frozen run file, the metric arithmetic, and that the two
arms are built from the same frozen retrieval.
"""
from __future__ import annotations

import json

import pytest

from evals import probe_web_gate as pwg
from silica.sources import web_research as wr

_PAGE = (
    "Source: https://s.test/a\n\nAlpha Title\n\n"
    "Graphs beat lists for this workload."
)


@pytest.fixture(autouse=True)
def _fresh_turn_state():
    wr._reset_turn()
    yield
    wr._reset_turn()


def test_effective_citations_counts_distinct_sources():
    assert pwg.effective_citations("x [1] y [2, 3] z [1]. plain [notanum]") == 3
    assert pwg.effective_citations("no markers") == 0


def test_bank_validity_checks_quotes_against_their_pages():
    bank = {
        "Q1": wr._Quote("https://s.test/a", "Graphs beat lists", "w"),
        "Q2": wr._Quote("https://s.test/a", "never on the page", "w"),
        "Q3": wr._Quote("https://gone.test", "page never fetched", "w"),
    }

    assert pwg.bank_validity(bank, [_PAGE]) == pytest.approx(1 / 3)
    assert pwg.bank_validity({}, [_PAGE]) is None


def test_record_run_freezes_trace_bank_and_oneshot_body(tmp_vault, monkeypatch, tmp_path):
    """The frozen file carries everything replay needs, and the recorded note
    is arm A: composition is disabled for the duration of the run."""
    from silica.agent.events import ToolCompleteEvent

    def fake_run_agent(messages, model, tool_progress_callback=None,
                       constraints=None, **kw):
        tool_progress_callback(ToolCompleteEvent(
            name="web_fetch", args={"url": "https://s.test/a"}, call_id="f1",
            result=_PAGE, duration_s=0.0, iteration=1,
        ))
        wr.remember("https://s.test/a", "Graphs beat lists for this workload.", "core")
        return "One-shot body [Q1]."

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        wr, "call_llm",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("record must not compose")
        ),
    )

    path = pwg.record_run("graph workloads", tmp_path, max_searches=48)
    rec = json.loads(path.read_text(encoding="utf-8"))

    assert rec["concept"] == "graph workloads"
    assert rec["oneshot_body"] == "One-shot body [Q1]."
    assert rec["bank"]["Q1"] == [
        "https://s.test/a", "Graphs beat lists for this workload.", "core",
    ]
    assert list(rec["trace"].values()) == [_PAGE]
    assert rec["note_rel"]  # the arm-A note really was written


def test_replay_scores_both_arms_from_the_same_frozen_run(monkeypatch):
    """Arm A is the frozen one-shot body; arm B recomposes from the frozen
    bank; both bind against the frozen trace and are judged fact by fact."""
    from types import SimpleNamespace

    rec = {
        "concept": "graph workloads",
        "oneshot_body": "One-shot body [Q1].",
        "bank": {"Q1": ["https://s.test/a",
                        "Graphs beat lists for this workload.", "core"]},
        "trace": {"f1": _PAGE},
    }
    replies = iter(["## The claim [Q1]", "Composed prose [Q1]."])
    monkeypatch.setattr(
        wr, "call_llm",
        lambda model, messages, **kw: SimpleNamespace(
            text=next(replies), usage={"prompt_tokens": 10, "completion_tokens": 5},
        ),
    )
    monkeypatch.setattr(pwg, "decompose", lambda model, text: ["a fact"])
    monkeypatch.setattr(
        pwg, "judge_facts", lambda model, facts, source: [True] * len(facts)
    )

    row = pwg.replay_run(rec, judge_model="judge/x")

    assert row["quote_validity"] == 1.0
    a, b = row["arms"]["A"], row["arms"]["B"]
    assert a["body"].startswith("One-shot body [1].")
    assert b["body"].startswith("## The claim\n\nComposed prose [1].")
    assert a["factscore"] == 1.0 and b["factscore"] == 1.0
    assert a["factscore_detail"] == {
        "facts": 1, "judged": 1, "supported": 1, "score": 1.0,
    }
    assert a["effective_citations"] == 1 and b["effective_citations"] == 1
    assert b["extra_tokens"] == 30  # outline + one section, prompt+completion


def test_replay_summary_counts_unjudged_arms(monkeypatch, tmp_path, capsys):
    """Measured live: a transient judge failure leaves factscore None, and a
    mean that silently skips it reads as a clean 1.0. The summary must say
    how many runs each mean is actually standing on."""
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    rows = iter([
        {"concept": "a", "quote_validity": 1.0, "arms": {
            "A": {"factscore": 0.9, "body": "", "effective_citations": 1,
                  "phantom_audit": ""},
            "B": {"factscore": None, "body": "", "effective_citations": 1,
                  "phantom_audit": "", "extra_tokens": 5}}},
        {"concept": "b", "quote_validity": 1.0, "arms": {
            "A": {"factscore": 1.0, "body": "", "effective_citations": 1,
                  "phantom_audit": ""},
            "B": {"factscore": 1.0, "body": "", "effective_citations": 1,
                  "phantom_audit": "", "extra_tokens": 5}}},
    ])
    monkeypatch.setattr(pwg, "replay_run", lambda rec, judge: next(rows))

    rc = pwg.main(["replay", "--runs", str(tmp_path), "--judge-model", "j/x"])

    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{"):])
    assert summary["unjudged_A"] == 0
    assert summary["unjudged_B"] == 1
    assert summary["mean_factscore_B"] == 1.0  # over ONE run, and it says so


def test_replay_reports_a_failed_composition_instead_of_faking_arm_b(monkeypatch):
    monkeypatch.setattr(
        wr, "call_llm",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    monkeypatch.setattr(pwg, "decompose", lambda model, text: ["a fact"])
    monkeypatch.setattr(
        pwg, "judge_facts", lambda model, facts, source: [True] * len(facts)
    )
    rec = {
        "concept": "q",
        "oneshot_body": "Body [Q1].",
        "bank": {"Q1": ["https://s.test/a",
                        "Graphs beat lists for this workload.", "core"]},
        "trace": {"f1": _PAGE},
    }

    row = pwg.replay_run(rec, judge_model="judge/x")

    assert row["arms"]["B"] is None
    assert row["arms"]["A"]["factscore"] == 1.0


def test_record_batch_survives_a_concept_with_no_findings(monkeypatch, tmp_path, capsys):
    """Measured on the first live smoke: a 12-step run whose final turn yields
    nothing raises ValueError, and one such concept must not kill the batch."""
    concepts = tmp_path / "concepts.txt"
    concepts.write_text("good one\nbad one\nworse one\nlast one\n", encoding="utf-8")
    recorded = []

    def fake_record_run(concept, out_dir, max_searches, arm="B", tag=None,
                        corpus_stamp=None):
        if concept == "bad one":
            raise ValueError("web-research produced no findings")
        if concept == "worse one":
            # Measured live: run_agent can surface RuntimeError and httpx can
            # surface anything; no single concept may kill the batch.
            raise RuntimeError("tool failed 3 consecutive times")
        recorded.append(concept)
        path = out_dir / f"{concept}.json"
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"trace": {}, "bank": {}}), encoding="utf-8")
        return path

    monkeypatch.setattr(pwg, "record_run", fake_record_run)
    # main() assigns CONFIG.vault_path; registering it restores it after.
    monkeypatch.setattr(pwg.CONFIG, "vault_path", pwg.CONFIG.vault_path)

    rc = pwg.main([
        "record", "--concepts", str(concepts),
        "--out", str(tmp_path / "runs"), "--vault", str(tmp_path / "vault"),
    ])

    assert rc == 0
    assert recorded == ["good one", "last one"]
    out = capsys.readouterr().out
    assert "bad one" in out and "worse one" in out


def test_factscore_any_page_supports_a_fact_from_any_page(monkeypatch):
    """A fact is supported when any fetched page supports it; a fact no page
    supports counts against the score; an unjudgeable fact is excluded."""
    monkeypatch.setattr(
        pwg, "decompose", lambda model, text: ["on page two", "nowhere", "skip"]
    )

    def judge(model, facts, source):
        return [
            None if f == "skip"
            else (f == "on page two" if "TWO" in source else False)
            for f in facts
        ]

    monkeypatch.setattr(pwg, "judge_facts", judge)

    out = pwg.factscore_any_page("judge/x", "body", ["page ONE", "page TWO"])

    assert out == {"facts": 3, "judged": 2, "supported": 1, "score": 0.5}


# --- L3 steering gate (spec-web-research-plan-steering §6) ------------------


def test_acquisition_metrics_come_straight_off_the_recording():
    rec = {
        "arm": "B",
        "bank": {
            "Q1": ["https://a.test/x", "quote one", "w"],
            "Q2": ["https://a.test/x", "quote two", "w"],
            "Q3": ["https://b.test/y", "quote three", "w"],
        },
        "trace": {
            "c1": "Source: https://a.test/x\nbody",
            "c2": "Source: https://b.test/y\nbody",
            "c3": "5 results for 'q'",
        },
    }
    assert pwg.acquisition(rec) == {
        "arm": "B",
        "urls_with_quotes": 2,
        "quotes": 3,
        "fetches": 2,
        "yield_per_fetch": 1.5,
        "steps": 3,
    }


def test_acquisition_handles_no_fetches_and_missing_arm():
    acq = pwg.acquisition({"bank": {}, "trace": {}})
    assert acq["arm"] == "A"          # L2-era recordings had no arm key
    assert acq["yield_per_fetch"] is None


def test_record_stamps_corpus_provenance(monkeypatch, tmp_path):
    """A frozen-corpus recording must say so — live and offline acquisition
    would otherwise be indistinguishable files."""
    monkeypatch.setattr(
        wr, "web_research",
        lambda concept, max_searches=None, tool_progress_callback=None: "Inbox/x.md",
    )
    stamp = {"pages": 7, "sources": ["docs/gate-l3-2026-08-06/runs"]}
    path = pwg.record_run("crdt", tmp_path, 4, arm="A", corpus_stamp=stamp)
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["corpus"] == stamp
    live = pwg.record_run("crdt2", tmp_path, 4, arm="A")
    assert json.loads(live.read_text(encoding="utf-8"))["corpus"] is None


def test_record_arm_a_flips_steering_off_and_restores_it(monkeypatch, tmp_path):
    seen = {}

    def fake_web_research(concept, max_searches=None, tool_progress_callback=None):
        seen["steering"] = wr._STEERING
        return "Inbox/x.md"

    monkeypatch.setattr(wr, "web_research", fake_web_research)
    path = pwg.record_run("crdt", tmp_path, 4, arm="A")
    assert seen["steering"] is False
    assert wr._STEERING is True
    assert path.name == "crdt-A.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["arm"] == "A"
    assert rec["plan"] == ""
    assert rec["tokens"] == 0     # faked loop makes no LLM calls


def test_record_tag_names_the_aa_run(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wr, "web_research",
        lambda concept, max_searches=None, tool_progress_callback=None: "Inbox/x.md",
    )
    path = pwg.record_run("crdt", tmp_path, 4, arm="A", tag="A2")
    assert path.name == "crdt-A2.json"
