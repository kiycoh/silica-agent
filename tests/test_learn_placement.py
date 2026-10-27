# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A placement before a plan starts (ADR-0135 point 3): two candidates a
step, the walk in code, the answers as ordinary ledger lines, "placed"
derived on read and never the same as passed."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from silica.kernel.learn import mastery, placement, plans  # noqa: E402
from tests.test_learn_reading import ORDER, client, step_vault  # noqa: E402,F401

TWO = ("---\ntype: plan\nscope: steps\ntarget: Programma\nreach: relational\ncreated: '2026-10-20'\n---\n"
       "# Due\n\n## Steps\n- [ ] Neuroni: [[" + ORDER[0] + "]], [[" + ORDER[1] + "]] · 2 h\n"
       "- [ ] Ottimizzazione: [[" + ORDER[2] + "]], [[" + ORDER[3] + "]] · 2 h\n\n## Sessions\n")


@pytest.fixture
def two(step_vault):
    step_vault.note("Plans/Due.md", TWO)
    return plans.find_plan("Plans/Due.md")


def test_two_candidates_a_step_deepest_first(two, monkeypatch):
    from silica.kernel.report import learner

    # the second note of each step needs the first: the deepest is the second
    monkeypatch.setattr(learner, "prerequisites_map", lambda: {
        f"Course/{ORDER[1]}.md": [f"Course/{ORDER[0]}.md"], f"Course/{ORDER[3]}.md": [f"Course/{ORDER[2]}.md"]})
    rows = placement.candidates(two)
    got = [(r["step"], r["rank"], r["path"].split("/")[-1][:-3]) for r in rows]
    assert got == [(0, 0, ORDER[1]), (0, 1, ORDER[0]), (1, 0, ORDER[3]), (1, 1, ORDER[2])]
    assert {r["kind"] for r in rows} == {"explain"}          # no apply objective on this plan
    assert placement.PER_STEP * placement.MAX_STEPS <= 24     # within the session's cap


def _answer_and_rate(c, sid, i, rating):
    c.post("/learn/answer", json={"id": sid, "i": i, "production": "una risposta", "conf": 70, "self": True})
    return c.post("/learn/rate", json={"id": sid, "i": i, "rating": rating}).json()


def test_the_walk_drops_the_second_question_of_a_placed_step_and_placed_is_derived(two, client, tmp_path):
    c = client
    s = c.post("/learn/session", json={"plan": "Plans/Due.md", "placement": True}).json()
    assert s["origin"]["label"] == "Due · placement"
    items = s["items"]
    assert len(items) == 4
    first, second, third = items[0], items[1], items[2]

    met = _answer_and_rate(c, s["id"], first["i"], 3)           # step 1's deepest note, met without help
    assert met["drop"] == [second["i"]]
    back = c.get("/learn/session", params={"id": s["id"]}).json()
    assert [it["i"] for it in back["items"]] == [first["i"], third["i"], items[3]["i"]]

    missed = _answer_and_rate(c, s["id"], third["i"], 1)        # step 2's deepest note, missed
    assert "drop" not in missed

    lines = [json.loads(ln) for ln in (tmp_path / "quiz.jsonl").read_text().splitlines()]
    assert [(e["origin"], e["step"], e["rank"]) for e in lines] == [("placement", 0, 0), ("placement", 1, 0)]
    assert not any(mastery.eligible(e) for e in lines)           # a pretest: out of calibration
    rep = c.get("/learn/plan", params={"path": "Plans/Due.md"}).json()
    assert rep["placed"]["steps"] == [0] and rep["placed"]["notes"] == 2
    assert rep["placed"]["say"].startswith("1 step placed by what you already know (Neuroni, 2 notes)")
    assert rep["placement"]["questions"] == 4 and rep["placement"]["last"]
    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    assert len(rc["items"]) == 3                                 # the dropped question was never asked

    # placed is not passed: a plan session still asks the placed step, after the rest
    from silica.ui.web import learn_routes

    assert learn_routes._placed_notes(two) == {f"Course/{ORDER[0]}.md", f"Course/{ORDER[1]}.md"}


def test_a_hint_or_just_tell_me_never_places_a_step(two):
    entries = [{"origin": "placement", "plan": "Plans/Due.md", "step": 0, "rank": 0, "path": f"Course/{ORDER[1]}.md",
                "correct": True, "hints": 1, "told": False},
               {"origin": "placement", "plan": "Plans/Due.md", "step": 1, "rank": 1, "path": f"Course/{ORDER[2]}.md",
                "correct": True, "hints": 0, "told": False}]
    assert placement.placed_steps(two, entries) == []           # helped, and a middle note never places
    entries[0]["hints"] = 0
    assert placement.placed_steps(two, entries) == [0]
