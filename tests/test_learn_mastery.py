# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Mastery readings (learn-suite D25) and calibration (D26): pure functions
over the ledger, never a composite score."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from silica.kernel.learn import mastery

DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _e(path, days_ago, **kw):
    base = {"ts": _iso(time.time() - days_ago * DAY), "path": path, "correct": True, "rating": 3,
            "kind": "recall", "level": 1, "grader": "model", "hints": 0, "told": False, "conf": 70}
    base.update(kw)
    return base


def test_depth_is_the_highest_level_met_without_help_and_a_miss_never_erases_it():
    entries = [
        _e("A.md", 10, level=3, kind="explain"),                       # held Mechanisms, unassisted
        _e("A.md", 8, level=4, kind="transfer", correct=True, hints=1),  # assisted: no credit
        _e("A.md", 5, level=3, kind="explain", correct=False, rating=1),  # later miss: an observation
        _e("B.md", 5, level=2, kind="recall", grader="self"),          # self-graded: labeled as such
        _e("C.md", 5, level=3, kind="explain", correct=True, told=True),  # told: no credit
    ]
    d = mastery.depth_by_path(["A.md", "B.md", "C.md", "D.md"], entries)
    assert d["A.md"]["level"] == 3 and d["A.md"]["label"] == "Mechanisms"
    assert any(o["level"] == 3 and o["correct"] is False for o in d["A.md"]["observations"])
    assert d["B.md"]["level"] == 2 and d["B.md"]["self"] is True
    assert d["C.md"]["level"] is None
    assert d["D.md"]["level"] is None and d["D.md"]["measured"] is False


def test_next_ask_repeats_a_failed_demand_and_climbs_after_success_capped_at_reach():
    assert mastery.next_ask([], reach=4) == 1                                  # nothing held: Facts
    held3 = [_e("A.md", 10, level=3, kind="explain")]
    assert mastery.next_ask(held3, reach=4) == 4                              # one above the last success
    assert mastery.next_ask(held3, reach=3) == 3                              # capped at reach
    failed = held3 + [_e("A.md", 5, level=4, kind="transfer", correct=False, rating=1)]
    assert mastery.next_ask(failed, reach=4) == 4                             # repeat the failed demand
    fresh = [_e("A.md", 10, level=2, kind="recall")]
    assert mastery.next_ask(fresh, reach=4) == 3
    jump = [_e("A.md", 10, level=2, kind="recall"), _e("A.md", 9, level=2, kind="recall")]
    assert mastery.next_ask(jump, reach=4) < 4                                # level 4 never before 3 held


def test_transfer_reads_applied_and_transferred_under_the_unassisted_criterion():
    entries = [_e("A.md", 5, level=3, kind="apply"), _e("B.md", 5, level=4, kind="transfer", hints=2)]
    d = mastery.depth_by_path(["A.md", "B.md"], entries)
    assert d["A.md"]["applied"] is True and d["A.md"]["transferred"] is False
    assert d["B.md"]["transferred"] is False       # assisted


def test_integration_counts_a_relation_only_when_the_item_named_both_endpoints():
    entries = [
        _e("A.md", 5, level=3, kind="structure", q="How does [[B]] relate to A?", endpoints=["A", "B"]),
        _e("A.md", 4, level=3, kind="structure", q="What holds here?"),           # no second endpoint
        _e("C.md", 4, level=3, kind="structure", q="How does [[D]] relate to C?", endpoints=["C", "D"], hints=1),
    ]
    rel = [("A.md", "B.md"), ("C.md", "D.md"), ("A.md", "D.md")]
    out = mastery.integration(["A.md", "B.md", "C.md", "D.md"], entries, relations=rel)
    assert out["held"] == 1 and out["total"] == 3
    assert mastery.integration(["A.md"], entries, relations=[])["total"] == 0
    assert mastery.integration(["A.md"], entries, relations=[])["label"] == "No relations available"


def test_misconception_observations_stay_after_a_later_success():
    entries = [_e("A.md", 5, level=3, kind="explain", correct=False, rating=1,
                  q="Why?", misconception="thinks the gradient points downhill"),
               _e("A.md", 1, level=3, kind="explain")]
    obs = mastery.misconceptions(["A.md"], entries)
    assert len(obs) == 1 and obs[0]["misconception"].startswith("thinks") and obs[0]["q"] == "Why?"


def test_blind_spot_and_fragile_come_from_conf_and_correct_never_rating():
    entries = [
        _e("A.md", 5, conf=90, correct=False, rating=2),      # high confidence, criteria not met
        _e("B.md", 5, conf=25, correct=True, rating=4),       # correct with low confidence
        _e("C.md", 5, conf=90, correct=False, grader="self"), # self-assessed: not eligible
        _e("D.md", 5, conf=90, correct=False, told=True),     # told: not eligible
        _e("E.md", 5, conf=90, correct=False, kind="predict"),  # pretest: not eligible
        _e("F.md", 5, conf=None, correct=False),              # skipped confidence: not eligible
    ]
    s = mastery.suggestions(entries, ["A.md", "B.md", "C.md", "D.md", "E.md", "F.md"])
    assert [x["path"] for x in s["blind_spots"]] == ["A.md"]
    assert s["blind_spots"][0]["say"] == "High confidence; criteria not fully met"
    assert [x["path"] for x in s["fragile"]] == ["B.md"]
    assert s["fragile"][0]["say"] == "Correct with low confidence"


def test_calibration_needs_ten_eligible_events():
    nine = [_e(f"N{i}.md", 5, conf=90, correct=(i % 2 == 0)) for i in range(9)]
    c = mastery.calibration(nine)
    assert c["n"] == 9 and c["enough"] is False and "brier" not in c
    ten = nine + [_e("N9.md", 5, conf=25, correct=True, rating=1)]   # rating must not matter
    c = mastery.calibration(ten)
    assert c["n"] == 10 and c["enough"] is True
    assert 0 <= c["brier"] <= 1 and -1 <= c["bias"] <= 1
    assert c["bands"]["90"]["n"] == 9 and c["bands"]["50"] == {"n": 0, "label": "Not enough evidence"}
    assert c["bias"] > 0 and "above observed" in c["bias_say"]


def test_node_readings_aggregate_distinct_leaves_and_overlapping_plans_agree():
    now = time.time()
    view = {"a": {"path": "A.md", "R": 0.5, "S": 3.0, "attempts": 2, "due": now - DAY},
            "b": {"path": "B.md", "R": 0.95, "S": 40.0, "attempts": 1, "due": now + 30 * DAY},
            "c": {"path": "C.md", "R": None, "S": None, "attempts": 0, "due": None}}
    entries = [_e("A.md", 5, level=3, kind="explain"), _e("A.md", 4, level=1, correct=False, rating=1),
               _e("B.md", 5, level=1)]
    node = mastery.node_readings(["A.md", "B.md", "C.md", "A.md"], view, entries)
    assert node["coverage"] == [2, 3] and node["due"] == 1
    assert abs(node["R_mean"] - 0.725) < 1e-9
    assert node["depth_profile"] == {"3": 1, "1": 1} and node["not_measured"] == 1
    empty = mastery.node_readings(["C.md"], view, entries)
    assert empty["R_mean"] is None and empty["coverage"] == [0, 1]
    # the same leaf under two scopes reads the same
    p1 = mastery.node_readings(["A.md", "B.md"], view, entries)
    p2 = mastery.node_readings(["A.md", "C.md"], view, entries)
    assert p1["leaves"]["A.md"] == p2["leaves"]["A.md"]


def test_grader_agreement_splits_spoken_from_typed_once_there_is_speech():
    """The ADR-0049 gate: if the reader overrides the grader more often on
    spoken answers, the transcription is costing grades."""
    ev = lambda **kw: {"path": "ML/A.md", "correct": True, "grader": "model", "rating": 3, "ts": 1.0, **kw}
    typed_only = [ev(), ev(grader_rating=2)]
    out = mastery.grader_agreement(typed_only)
    assert out["n"] == 2 and out["kept"] == 1 and "voice" not in out
    mixed = typed_only + [ev(modality="voice", grader_rating=1), ev(modality="voice"), ev(modality="voice", grader_rating=4)]
    out = mastery.grader_agreement(mixed)
    assert out["n"] == 5 and out["kept"] == 2
    assert out["voice"] == {"n": 3, "kept": 1, "share": 1 / 3}
    assert out["typed"] == {"n": 2, "kept": 1, "share": 0.5}
