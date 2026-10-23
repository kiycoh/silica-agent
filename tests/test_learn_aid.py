# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The escalate rung inside Learn (silica/kernel/learn/offer.py, research
report difficulty-detection-and-study-aids section 8): the rating that makes
three misses in a row on a note makes the next move the aid itself."""
# ruff: noqa: F811  the client fixture is imported, so every test that takes it reads as a redefinition
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from silica.kernel.learn import offer  # noqa: E402
from tests.test_learn_panel import client  # noqa: E402,F401

NOTE = "ML/Margine geometrico.md"


class _Reply:
    text = "1. Si misura la distanza dal piano.\n2. Si prende la minima.\n\n**Quale punto la fissa?**"
    finish_reason = "stop"


def _miss_twice():
    from silica.kernel.report import quiz

    quiz.record([{"path": NOTE, "correct": False, "rating": 1, "kind": "explain", "level": 3, "q": "?"}] * 2)


def _third_miss(c):
    s = c.post("/learn/session", json={"note": NOTE}).json()
    it = s["items"][0]
    c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "la distanza media", "conf": 50})
    return s, it, c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 1}).json()


def test_the_third_miss_in_a_row_writes_the_aid_from_the_note(client, tmp_path, monkeypatch):
    import silica.agent.llm as llm

    c, _ = client
    monkeypatch.setattr(offer, "_log_path", lambda: tmp_path / "offers.jsonl")
    prompts = []
    monkeypatch.setattr(llm, "call_llm", lambda model, msgs, **kw: (prompts.append(msgs[0]["content"]), _Reply())[1])
    _miss_twice()
    s, it, r = _third_miss(c)
    assert r["escalate"]["why"] == "3 misses in a row" and r["escalate"]["form"] == "worked"
    aid = c.post("/learn/aid", json={"id": s["id"], "i": it["i"]}).json()
    assert "<ol>" in aid["html"] and "<strong>Quale punto la fissa?</strong>" in aid["html"]
    # grounded on the note's points and on what the reader wrote
    assert "la distanza media" in prompts[0] and "worked example" in prompts[0]
    assert c.post("/learn/aid", json={"id": s["id"], "i": it["i"]}).json() == aid and len(prompts) == 1  # kept
    rec = [json.loads(x) for x in (tmp_path / "offers.jsonl").read_text().splitlines()]
    assert rec[0]["aid"] == "learn" and rec[0]["displayed"] and "shown" not in rec[0]
    assert offer.allowed("any chat", NOTE, records=rec)   # an aid is not an offer the reader ignored


def test_a_miss_that_does_not_escalate_asks_for_no_aid(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setattr(offer, "_log_path", lambda: tmp_path / "offers.jsonl")
    s, it, r = _third_miss(c)      # the first miss on the note
    assert r["escalate"] is None
    assert c.post("/learn/aid", json={"id": s["id"], "i": it["i"]}).status_code == 400


def test_shadow_mode_logs_the_aid_and_shows_none(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setattr(offer, "_log_path", lambda: tmp_path / "offers.jsonl")
    monkeypatch.setenv("SILICA_STUDY_OFFER", "shadow")
    _miss_twice()
    _s, _it, r = _third_miss(c)
    assert r["escalate"] is None
    rec = json.loads((tmp_path / "offers.jsonl").read_text().splitlines()[0])
    assert rec["why"] == "3 misses in a row" and rec["displayed"] is False


def test_the_prerequisite_attached_is_the_one_held_least(monkeypatch):
    from silica.kernel.report import learner

    monkeypatch.setattr(learner, "prerequisites_map", lambda: {learner.key_of(NOTE): ["ML/A.md", "ML/B.md", "ML/C.md"]})
    rows = {learner.key_of("ML/A.md"): {"attempts": 3, "R": 0.95}, learner.key_of("ML/B.md"): {"attempts": 2, "R": 0.6},
            learner.key_of("ML/C.md"): {"attempts": 0, "R": None}}
    monkeypatch.setattr(learner, "view", lambda *a, **k: rows)
    assert offer.weakest_prerequisite(NOTE) == "ML/C.md"           # never practised first
    rows[learner.key_of("ML/C.md")] = {"attempts": 4, "R": 0.99}
    assert offer.weakest_prerequisite(NOTE) == "ML/B.md"           # then the lowest recall


def test_a_note_carries_its_study_tools_and_a_session_its_aid():
    """NotebookLM's Studio row in Learn: each form of the note one button that
    writes its command into the chat; the aid box asks for the aid once."""
    from tests.webassets import app_js

    js = app_js()
    assert "lrStudyTools(n.path, n.title)" in js and '"/learn/aid"' in js
    for cmd in ("/flashcards", "/slides", "/map", "/schematize", "/diagram", "/explain"):
        assert f"`{cmd} ${{shellQuote(" in js


def test_the_formulas_in_learn_come_back_as_mathml(client):
    """A course's questions carry LaTeX; Learn draws it as the chat does, and a
    formula that does not convert comes back as written."""
    c, _ = client
    r = c.post("/learn/math", json={"tex": ["y_i f(x) > \\gamma", "\\frac{1}{2}\\|w\\|^2", "\\notacommand{"],
                                    "display": [False, True]}).json()["mathml"]
    assert r[0].startswith("<math") and 'display="inline"' in r[0]
    assert 'display="block"' in r[1]
    assert len(r) == 3


def test_learn_parks_formulas_before_it_escapes():
    from tests.webassets import app_js

    js = app_js()
    assert "LR_MATH_RE" in js and '"/learn/math"' in js and "lrMathSpan(...math[i])" in js




def test_a_hold_too_small_to_read_says_no_milestone():
    """S from 0.1 to 0.3 days doubled and read "~0 days, was ~0" (2026-10-23)."""
    from silica.ui.web.learn_routes import _momentum

    assert _momentum({"S": 0.1}, {"S": 0.3}, False) is None
    assert _momentum({"S": 2.0}, {"S": 4.5}, False) == "Estimated hold ~4 days, was ~2."
    assert _momentum(None, {"S": 0.2}, True) == "First hold on this note: estimated under a day."
    assert _momentum(None, {"S": 2.3}, True) == "First hold on this note: estimated ~2 days."
