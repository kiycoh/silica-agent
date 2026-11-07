# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One failure shape and one reader of it: `error` is what the person reads,
whichever way the failure reached the wire, and status codes stay what the page
branches on."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from silica.ui.web import failures, server
from silica.ui.web.session import SESSION
from tests.webassets import WEB, core_js

ROUTES = Path(server.__file__).parent


@pytest.fixture
def tc(tmp_vault):
    SESSION.reset()
    return TestClient(server.app)


def test_a_session_that_is_gone_says_so_by_code_and_not_by_its_sentence(tc):
    """The page starts over on this one 404, and it used to know by matching the
    words a route wrote for the reader."""
    r = tc.post("/learn/close", json={"id": "nope"})
    assert r.status_code == 404
    assert r.json() == {"error": "no live session with that id", "detail": "no live session with that id",
                        "code": "session_gone"}


def test_any_other_learn_refusal_carries_no_code(tc):
    r = tc.post("/learn/session", json={"plan": "Plans/Nowhere.md"})
    assert r.status_code == 404 and "code" not in r.json()


def test_a_routes_own_sentence_takes_the_shape_with_its_status(tc):
    r = tc.post("/jobs/nope/dismiss")
    assert r.status_code == 409
    assert r.json() == {"error": "there is no such conversion", "detail": "there is no such conversion"}


def test_a_chat_another_process_holds_takes_the_shape(tc, monkeypatch):
    from silica.obs import narration

    def held(sid, title, vault):
        raise narration.SessionBusy(sid, {"driver": "tui", "pid": 7})

    monkeypatch.setattr(narration, "rename_session", held)
    r = tc.post("/session/rename", json={"id": "abc123", "title": "x"})
    assert r.status_code == 409
    assert r.json() == {"error": "session abc123 in use by tui, pid 7", "detail": "session abc123 in use by tui, pid 7"}


def test_refused_keeps_a_routes_words_where_explain_would_rewrite_them():
    """`explain()`'s table is for raw text. These words match its classes, and a
    route that wrote them for the person must not have them replaced."""
    said = "no connection to that note"
    assert failures.sentence(said) != said
    assert failures.refusal(said)["error"] == said


def test_a_status_with_nothing_to_say_keeps_fastapis_body(tc):
    """A withheld route answers exactly as a missing one does (ADR-0140 point 4),
    so neither can grow a key the other lacks."""
    assert tc.get("/no/such/route").json() == {"detail": "Not Found"}


def test_fastapis_own_refusal_of_a_body_is_left_alone(tc):
    r = tc.post("/learn/close", content="not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list) and "error" not in r.json()


def test_no_route_serialises_an_exception_by_hand():
    """`raise Refused(400, exc)` says the route read those words; `str(exc)` in an
    HTTPException says nothing of the kind."""
    written = {p.name: re.findall(r"HTTPException\([^\n]*str\(\w+\)", p.read_text(encoding="utf-8"))
               for p in ROUTES.glob("*_routes.py")}
    assert not {name: hits for name, hits in written.items() if hits}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app-core.js")
def test_the_one_reader_takes_each_body_the_wire_can_carry(tmp_path):
    cases = [
        [{"error": "said", "detail": "raw"}, "fallback", "said"],
        [{"error": "said", "detail": ""}, "fallback", "said"],
        [{"detail": "a guard's"}, "fallback", "a guard's"],
        [{"ok": False, "error": "not saved"}, "fallback", "not saved"],
        [{"detail": [{"loc": ["body"], "msg": "x"}]}, "fallback", "fallback"],
        [{}, "fallback", "fallback"],
        [None, "fallback", "fallback"],
    ]
    script = tmp_path / "said.js"
    script.write_text(core_js() + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[2]).map("
                      "([b, f]) => failureSaid(b, f))));\n")
    out = subprocess.run(["node", str(script), json.dumps(cases)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [c[2] for c in cases]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app-limits.js")
def test_a_limit_reads_as_the_server_wrote_it_with_the_readers_clock(tmp_path):
    """The page used to swap `when` into the sentence by exact substring. It writes
    the sentence from the table now, and must land on the same words."""
    src = (WEB / "app-limits.js").read_text(encoding="utf-8")
    block = src[src.index("function limWhen("):src.index("// The Usage section of Settings")]
    at = "2026-11-02T14:20:00+00:00"
    frames = [{**failures.explain(f"usage limit reached: {layer}, resets {at}"), "layer": layer}
              for layer in failures.LIMIT_SENTENCE]
    script = tmp_path / "limit.js"
    script.write_text(core_js() + block + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[2]).map("
                      "(ev) => [limitSentence(ev), limWhen(ev.limit, ev.resets_at)])));\n")
    out = subprocess.run(["node", str(script), json.dumps(frames)], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "TZ": "Asia/Tokyo"})
    assert out.returncode == 0, out.stderr
    for frame, (said, local) in zip(frames, json.loads(out.stdout), strict=True):
        assert said == frame["error"].replace(frame["when"], local) and local and "UTC" not in said
