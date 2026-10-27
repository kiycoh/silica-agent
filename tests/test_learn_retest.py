# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A miss is asked again before the session ends, in another kind (ADR-0135
point 4); its line is kept apart, so FSRS and the objectives never read a
note met minutes after its miss as recalled."""
from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("fastapi")

from tests.test_learn_panel import _start, client  # noqa: E402,F401


def _item(kind, path="ML/Margine geometrico.md"):
    return {"path": path, "kind": kind, "level": 3, "title": "Margine", "q": "?", "grader": "self",
            "claim": "c", "span": "s", "source": {}, "answer": "a", "criteria": "k", "objective": "explain"}


def _miss(c, s, i):
    c.post("/learn/answer", json={"id": s["id"], "i": i, "production": "non so", "conf": 25, "self": True})
    return c.post("/learn/rate", json={"id": s["id"], "i": i, "rating": 1}).json()


def test_a_model_kind_miss_is_asked_again_now_in_a_mechanical_kind(client, monkeypatch, tmp_path):
    from silica.kernel.learn import items
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    live = learn_routes._LIVE
    live["items"][0].update(kind="explain")               # as if the model had written it
    calls = []

    def fake_build(rows, backfill=None, n=None):
        calls.append(rows[0]["kind"])
        return [dict(_item("cloze", rows[0]["path"]), i=1)]

    monkeypatch.setattr(items, "build_session", fake_build)
    r = _miss(c, s, live["items"][0]["i"])
    assert calls == ["cloze"] and r["added"][0]["kind"] == "cloze"
    added = r["added"][0]["i"]
    assert added == len(live["items"]) and live["items"][-1]["retest"] is True

    # once a note, and the retest's own miss is not retested again
    c.post("/learn/answer", json={"id": s["id"], "i": added, "production": "x", "conf": 25, "self": True})
    again = c.post("/learn/rate", json={"id": s["id"], "i": added, "rating": 3}).json()
    assert "added" not in again

    lines = [json.loads(ln) for ln in (tmp_path / "quiz.jsonl").read_text().splitlines()]
    assert [ln.get("retest", False) for ln in lines] == [False, True]
    from silica.kernel.learn import mastery, schedule

    assert len(schedule.events_by_key(lines)["ml/margine geometrico"]) == 1     # the miss, not the retest
    assert len(mastery.by_path(lines, ["ML/Margine geometrico.md"])["ML/Margine geometrico.md"]) == 1
    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    assert rc["retests"] == {"asked": 1, "met": 1}


def test_a_mechanical_miss_gets_a_generated_retest_in_the_background(client, monkeypatch):
    from silica.kernel.learn import items
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    live = learn_routes._LIVE
    live["items"][0].update(kind="cloze")
    monkeypatch.setattr(items, "build_session", lambda rows, backfill=None, n=None: [dict(_item(rows[0]["kind"]), i=1)])
    r = _miss(c, s, live["items"][0]["i"])
    assert "added" not in r                                  # not ready now: generated in a thread
    for _ in range(100):
        if live["items"][-1].get("retest"):
            break
        time.sleep(0.02)
    assert live["items"][-1]["kind"] == "explain" and live["items"][-1]["retest"] is True
    # the tab that reaches the end reads the list once more and finds it
    back = c.get("/learn/session", params={"id": s["id"]}).json()
    assert back["items"][-1]["i"] == live["items"][-1]["i"]


def test_no_retest_for_a_met_answer_a_placement_or_past_the_cap(client, monkeypatch):
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    live = learn_routes._LIVE
    it = dict(live["items"][0], kind="explain")
    assert learn_routes._retest(live, it, {"correct": True}) is None
    assert learn_routes._retest(live, {**it, "placement": {"step": 0, "rank": 0}}, {"correct": False}) is None
    live["retested"] = {f"n{k}" for k in range(learn_routes.RETEST_MAX)}
    assert learn_routes._retest(live, it, {"correct": False}) is None
