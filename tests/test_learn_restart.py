# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The live Learn session survives a restart (ADR-0135 point 1): it is a
file beside the answer ledger, written whole after every change and read
back when memory has none. `learn_routes._reset()` is the restart."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from tests.test_learn_panel import _start, client  # noqa: E402,F401


def _file():
    from silica.ui.web import learn_routes

    return learn_routes._live_file()


def test_a_session_comes_back_after_a_restart_where_it_was(client):
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    first = s["items"][0]
    c.post("/learn/answer", json={"id": s["id"], "i": first["i"], "production": "il margine", "conf": 70})
    assert c.post("/learn/rate", json={"id": s["id"], "i": first["i"], "rating": 2}).json()["recorded"]
    assert _file().is_file()

    learn_routes._reset()
    back = c.get("/learn/session", params={"id": s["id"]})
    assert back.status_code == 200
    d = back.json()
    assert d["done"] == [first["i"]] and d["results"][str(first["i"])]["rating"] == 2
    # a second start is still refused: the session is live again
    assert c.post("/learn/session", json={"n": 2}).status_code == 409
    # the rating already recorded is not recorded twice after the restart
    assert c.post("/learn/rate", json={"id": s["id"], "i": first["i"], "rating": 3}).json()["duplicate"] is True

    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    assert rc["recorded"] == 1 and not _file().exists()
    learn_routes._reset()
    assert c.get("/learn/session", params={"id": s["id"]}).status_code == 404


def test_the_file_keeps_ints_and_sets_and_a_damaged_one_starts_empty(client):
    from silica.ui.web import learn_routes

    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    c.post("/learn/skip", json={"id": s["id"], "i": s["items"][0]["i"]})
    doc = json.loads(_file().read_text(encoding="utf-8"))
    assert doc["skipped"] == [s["items"][0]["i"]] and doc["vault"]
    learn_routes._reset()
    assert c.get("/learn/session", params={"id": s["id"]}).json()["skipped"] == [s["items"][0]["i"]]
    assert isinstance(learn_routes._LIVE["skipped"], set)

    _file().write_text("{torn", encoding="utf-8")
    learn_routes._reset()
    assert c.get("/learn/session", params={"id": s["id"]}).status_code == 404
    assert c.post("/learn/session", json={"n": 2}).status_code == 200   # and the next start overwrites it
