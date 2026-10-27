# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A miss at confidence 90 is a reason to escalate, logged and never shown
until 20 sessions show it earns its place (ADR-0135 point 9)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from silica.kernel.learn import offer  # noqa: E402
from tests.test_learn_panel import _start, client  # noqa: E402,F401


def _e(ts, correct, conf=70, miss=None):
    e = {"ts": f"2026-10-2{ts}T10:00:00+00:00", "path": "ML/x.md", "correct": correct, "conf": conf}
    if miss:
        e["miss"] = miss
    return e


def test_the_reason_fires_on_the_last_answer_only_and_the_old_reasons_win(monkeypatch):
    assert offer.escalation("ML/x.md", "", "", [], [_e(1, True), _e(2, False, 90)], records=[]) == offer.CONFIDENT_REASON
    assert offer.escalation("ML/x.md", "", "", [], [_e(1, False, 90), _e(2, True)], records=[]) == ""
    three = [_e(1, False), _e(2, False), _e(3, False, 90)]
    assert offer.escalation("ML/x.md", "", "", [], three, records=[]) == "3 misses in a row"
    assert offer.shadowed(offer.CONFIDENT_REASON) and not offer.shadowed("3 misses in a row")
    # the structural class waits behind the picker's gate
    structural = [_e(1, True), _e(2, False, 50, "structural")]
    assert offer.escalation("ML/x.md", "", "", [], structural, records=[]) == ""
    from silica.kernel.learn import pick

    monkeypatch.setattr(pick, "MISS_CLASS_READ", True)
    assert offer.escalation("ML/x.md", "", "", [], structural, records=[]) == offer.STRUCTURAL_REASON


def test_a_confident_miss_in_a_session_is_logged_not_shown_and_keeps_the_notes_aid(client):
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    it = s["items"][0]
    c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "sbagliato", "conf": 90, "self": True})
    r = c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 1}).json()
    assert r["escalate"] is None
    recs = [x for x in offer._records() if x.get("why") == offer.CONFIDENT_REASON]
    assert len(recs) == 1 and recs[0]["displayed"] is False and recs[0]["session"] == s["id"]
    assert it["path"] not in (learn_routes._LIVE.get("escalated") or set())
    from silica.kernel.report import quiz

    out = offer.confident_readout(quiz.entries())
    assert out["fires"] == 1 and out["followed_by_miss"] == 0 and out["sessions"] == 1
