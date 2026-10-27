# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A ledger line says which hints the reader saw, not only how many
(ADR-0135 point 5); the caps of ADR-0040 do not change with it."""
# ruff: noqa: F811  the client fixture is imported, so every test that takes it reads as a redefinition
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from tests.test_learn_panel import _start, client  # noqa: E402,F401


def test_the_rungs_of_an_item_follow_its_hints_and_the_line_keeps_those_shown(client, tmp_path):
    from silica.kernel.learn import grade, items
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    it = learn_routes._LIVE["items"][0]
    assert len(it["rungs"]) == len(it["hints"]) and set(it["rungs"]) <= {"H1", "H2", "H3"}
    assert [r for r, _t in items.hint_rungs(it["path"])] == [r for r in it["rungs"] if r != "H1"]
    if not it["hints"]:
        pytest.skip("the fixture note offers no hint")
    c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "x", "conf": 70, "hints": 1, "self": True})
    c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 3})
    line = json.loads((tmp_path / "quiz.jsonl").read_text().splitlines()[-1])
    assert line["hints"] == 1 and line["rungs"] == it["rungs"][:1]
    assert grade.rating_for("recalled", hints=1, told=False, conf=90, precise=True) == grade.HARD   # caps unchanged


def test_a_model_item_puts_its_own_hint_first_as_h1(monkeypatch):
    from silica.kernel.learn import items

    monkeypatch.setattr(items, "generate", lambda rows: {rows[0]["path"]: [{"path": rows[0]["path"], "kind": "explain",
                                                                            "level": 3, "hints": ["a cue"]}]})
    monkeypatch.setattr(items, "hint_rungs", lambda path: [("H3", "Outline: A · B")])
    built = items.build_session([{"path": "N.md", "kind": "explain", "level": 3}], [], n=1)
    assert built[0]["hints"] == ["a cue", "Outline: A · B"] and built[0]["rungs"] == ["H1", "H3"]
