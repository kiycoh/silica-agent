# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Learn tab (learn-suite §5, §6.1): every route through TestClient, and
the five shell edits plus the two new assets pinned by slicing app.js/app.css."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_learn_items import LEAF, NOTE  # noqa: E402
from tests.webassets import WEB, app_css, app_js  # noqa: E402

DAY = 86400.0
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    import os

    import silica.kernel.report.quiz as quiz
    from silica.kernel.learn import grade, items
    from silica.ui.web import learn_routes, server

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    monkeypatch.setattr(grade, "_llm", lambda *a, **k: (
        "GRADE: partial\nPRECISE: no\nRUBRIC: the bound is missing\nFEEDBACK: Name the bound.\n"
        "PARAPHRASE: yes\nMISCONCEPTION: none\nNOTE_DEFECT: none\n", "stop"))
    old = time.time() - 400 * DAY
    p = tmp_vault.note("ML/Margine geometrico.md", NOTE.replace("AI: true\n", "date: 2025-01-01\n"))
    os.utime(p, (old, old))
    tmp_vault.note("sources/Lezione 11.md", LEAF)
    q = tmp_vault.note("ML/Iperpiano.md", "---\ndate: 2025-01-01\n---\n# Iperpiano\n\nUn piano.[^Lezione-11]\n\n## Sources\n[^Lezione-11]: [[sources/Lezione 11]]\n")
    os.utime(q, (old, old))
    tmp_vault.note("Plans/ML.md", "---\ntype: plan\nscope: folder\ntarget: ML/\nreach: relational\ncreated: 2026-01-01\n---\n# ML plan\n\n## Tree\n- ML\n\n## Sessions\n")
    server._reset_session()
    learn_routes._reset()
    return TestClient(server.app), server


def _start(c, **body):
    r = c.post("/learn/session", json={"n": 2, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_get_learn_reports_the_readings_and_a_preview(client):
    c, _ = client
    d = c.get("/learn").json()
    assert d["total"] >= 2 and d["due"] >= 1 and "measured" in d
    assert len(d["horizon"]) == 7 and d["areas"] and d["plans"][0]["name"] == "ML plan"
    assert d["calibration"]["enough"] is False and d["preview"]["items"]
    assert d["preview"]["items"][0]["reason"] in ("Suggested for review", "Not practised yet", "Changed since last practice",
                                                   "Compare these notes", "Check a disagreement")
    row = next(r for r in d["preview"]["items"] if r["path"] == "ML/Margine geometrico.md")
    assert row["points"] >= 1 and row["questions"] >= 1 and row["minutes"] >= 1 and row["also_in"][0]["name"] == "ML plan"
    assert d["preview"]["counts"]["review"] + d["preview"]["counts"]["first"] >= 1
    assert d["plans"][0]["unit"] == "notes practised" and d["plans"][0]["objectives"] == ["explain"]   # legacy reach: relational
    assert d["live"] is None and d["live_info"] is None


def test_session_payload_carries_no_answer_key_and_one_session_is_live_at_a_time(client):
    c, _ = client
    s = _start(c)
    payload = json.dumps(s, ensure_ascii=False)
    assert s["id"] and s["items"] and "answer" not in payload and "claim" not in payload
    assert "rubric" not in payload and "criteria" not in payload and not re.search(r"limita l'errore atteso", payload)   # G5
    assert c.post("/learn/session", json={"n": 2}).status_code == 409
    c.post("/learn/close", json={"id": s["id"]})
    assert c.post("/learn/session", json={"n": 2}).status_code == 200


def test_answer_rate_close_writes_one_line_per_item_and_a_receipt(client, tmp_path):
    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    assert s["plan"] == "Plans/ML.md"
    it = s["items"][0]
    a = c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "il margine", "conf": 70, "hints": 1, "told": False}).json()
    # the plan's objective (explain) picks the item: a relation first, the footnoted line otherwise
    assert a["grader"] in ("self", "model") and a["claim"] and a["objective"] == "explain"
    assert a["span"]["leaf"] == "sources/Lezione 11.md" or a["kind"] == "structure"
    if a["grader"] == "model":
        assert a["grade"] == "partial" and a["rating"] == 2 and a["feedback_line"]
    r = c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 2}).json()
    assert r["recorded"] is True and r["next_due"]
    again = c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 3}).json()
    assert again["recorded"] is True and again["duplicate"] is True     # a retry never records twice
    lines = [json.loads(l) for l in (tmp_path / "quiz.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["session"] == s["id"] and lines[0]["plan"] == "Plans/ML.md"
    assert lines[0]["rating"] == 2 and lines[0]["conf"] == 70 and lines[0]["hints"] == 1
    assert lines[0]["objective"] == "explain" and lines[0]["plan"] == "Plans/ML.md"
    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    assert rc["recorded"] == 1 and rc["items"][0]["rating"] == 2 and "revisit" in rc and rc["plan_line"]
    from silica.kernel.learn import plans

    assert f"session: {s['id']}" in plans._read("Plans/ML.md")
    live = c.get("/learn/receipt", params={"id": s["id"]}).json()
    assert live["recorded"] == 1 and live["from_ledger"] is False      # still in memory: the full one
    from silica.ui.web import learn_routes

    learn_routes._reset()                                              # a restart: the ledger's own
    rebuilt = c.get("/learn/receipt", params={"id": s["id"]}).json()
    assert rebuilt["recorded"] == 1 and rebuilt["from_ledger"] is True and rebuilt["items"][0]["rating"] == 2


def test_told_and_self_grade_paths_and_the_mismatch_writes_nothing(client, tmp_path, monkeypatch):
    c, _ = client
    from silica.kernel.learn import grade

    s = _start(c)
    it = s["items"][0]
    told = c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "", "conf": None, "hints": 0, "told": True}).json()
    assert told["grader"] == "told" and told["rating"] == 2
    monkeypatch.setattr(grade, "_llm", lambda *a, **k: ("GRADE: lapsed\nRUBRIC: x\nFEEDBACK: y\nNOTE_DEFECT: the span says otherwise\n", "stop"))
    if len(s["items"]) > 1:
        it2 = s["items"][1]
        mm = c.post("/learn/answer", json={"id": s["id"], "i": it2["i"], "production": "x", "conf": 90, "hints": 0, "told": False, "force_model": True}).json()
        if mm["grader"] == "model":
            assert mm["skip"] is True and mm["note_defect"]
            assert c.post("/learn/rate", json={"id": s["id"], "i": it2["i"], "rating": 3}).status_code == 400
    assert not (tmp_path / "quiz.jsonl").exists()


def test_teach_runs_four_turns_then_a_summary(client, monkeypatch):
    c, _ = client
    from silica.kernel.learn import grade

    replies = iter(["Che cos'e' un margine?", "E perche' conta?", "Come si misura?", "Quando fallisce?",
                    "GRADE: recalled\nPRECISE: yes\nRUBRIC: connected\nFEEDBACK: The mechanism is there.\n"])
    monkeypatch.setattr(grade, "_llm", lambda *a, **k: (next(replies), "stop"))
    s = _start(c)
    it = s["items"][0]
    t = c.post("/learn/teach", json={"id": s["id"], "i": it["i"]}).json()
    assert t["turn"] == 1 and t["question"].startswith("Che")
    for k in range(2, 5):
        t = c.post("/learn/teach", json={"id": s["id"], "i": it["i"], "reply": "spiegazione " + str(k)}).json()
        assert t["turn"] == k
    fin = c.post("/learn/teach", json={"id": s["id"], "i": it["i"], "reply": "ultima"}).json()
    assert fin["summary"] and fin["rating"] == 3 and fin["grade"] == "recalled"


def test_plans_routes_read_write_revisit_and_close(client):
    c, _ = client
    lst = c.get("/learn/plans").json()
    assert lst[0]["path"] == "Plans/ML.md" and "frontier" in lst[0] and "coverage" in lst[0]
    rep = c.get("/learn/plan", params={"path": "Plans/ML.md"}).json()
    assert rep["tree"] and rep["readings"]["coverage"][1] >= 2 and rep["calibration"]["enough"] is False
    prev = c.post("/learn/plan", json={"name": "Esame", "scope": "steps", "target": "[[Iperpiano]]\nNesterov", "reach": "relational", "preview": True}).json()
    assert prev["gaps"] == ["Nesterov"] and prev["paths"] == ["ML/Iperpiano.md"] and prev.get("path") is None
    w = c.post("/learn/plan", json={"name": "Esame", "scope": "steps", "target": "[[Iperpiano]]\nNesterov", "reach": "relational"}).json()
    assert w["path"].endswith("Esame.md") and w["gaps"] == ["Nesterov"]
    q = c.post("/learn/revisit", json={"plan": "Plans/ML.md", "paths": ["ML/Iperpiano.md"]}).json()
    assert q["queued"] == ["ML/Iperpiano.md"]
    assert c.get("/learn/plan", params={"path": "Plans/ML.md"}).json()["revisit"] == ["ML/Iperpiano.md"]
    cl = c.post("/learn/plan/close", json={"path": "Plans/ML.md"}).json()
    assert cl["status"] == "active"           # no evidence, no completion
    assert c.post("/learn/plan/close", json={"path": "Plans/ML.md", "reopen": True}).json()["status"] == "active"


def test_mutating_learn_routes_refuse_cross_origin(client):
    c, _ = client
    bad = {"origin": "http://evil.example", "host": "127.0.0.1:8765"}
    for route, body in (("/learn/session", {"n": 2}), ("/learn/rate", {"id": "x", "i": 1, "rating": 3}),
                        ("/learn/plan", {"name": "x", "scope": "folder", "target": "ML/"}),
                        ("/learn/revisit", {"plan": "Plans/ML.md", "paths": []})):
        assert c.post(route, json=body, headers=bad).status_code == 403


# ------------------------------------------------------------------ the shell

def test_index_carries_the_learn_view_after_explore_and_the_section():
    # The views are rows of the drawer since ADR-0084, and the rail repeats them
    # folded; chat is no row (a chat is a row of Chats). Learn still follows
    # Explore: do, see, train, when, measure.
    views = INDEX[INDEX.index('<nav id="views"'):]
    views = views[:views.index("</nav>")]
    assert re.findall(r'data-tab="(\w+)"', views) == ["graph", "learn", "calendar", "metrics"]
    rail = INDEX[INDEX.index('<aside id="railmini"'):INDEX.index('<aside id="sidebar">')]
    assert re.findall(r'data-tab="(\w+)"', rail) == ["graph", "learn", "calendar", "metrics"]
    assert 'id="view-learn"' in INDEX and 'id="learn-bar"' in INDEX
    assert 'id="learn-crumb"' in INDEX and 'id="learn-back"' in INDEX and 'id="learn-end"' in INDEX
    # practice left the note's menu (ADR-0075): it starts from Learn's own rows
    assert 'id="note-learn"' not in INDEX
    scripts = re.findall(r'<script src="/static/(app-[\w-]+\.js)">', INDEX)
    assert scripts.index("app-learn.js") == scripts.index("app-metrics.js") + 1
    assert scripts.index("app-learn.js") < scripts.index("app-settings.js")
    css = re.findall(r'href="/static/(app-[\w-]+\.css)"', INDEX)
    assert css.index("app-learn.css") == css.index("app-metrics.css") + 1


def test_shell_switches_loads_and_deep_links_the_learn_view():
    js = app_js()
    assert '$("#view-learn").classList.toggle("active", tab === "learn")' in js
    assert 'if (tab === "learn") loadLearn()' in js
    assert '["chat", "graph", "learn", "calendar", "metrics"].includes(tab)' in js
    assert '["graph", "learn"].includes(tab)' in js
    assert "async function loadLearn(" in js
    assert '#note-learn' not in js and "learnStartSession({ note: n.path" in js
    # sub-states in the hash survive the shell (refinement §7): the shell reads
    # the first segment and never flattens a learn hash; the boot tab knows learn
    assert '.split("/")[0]' in js[js.index("function tabFromHash"):]
    # the one writer of the hash (syncUrl, audit P7) keeps learn's own
    assert 'activeTab === "learn" && location.hash.startsWith("#learn")' in js
    assert '["graph", "learn", "calendar", "metrics"].includes(bootTab)' in js
    for h in ('"#learn/plan/"', '"#learn/note/"', '"#learn/plans"', '"#learn/map"', '"#learn/new"', '"#learn/edit/"',
              '"#learn/session/"', '"#learn/receipt/"'):
        assert h in js
    assert 'v === "suggest"' in js                       # a bookmark from before ADR-0081 still lands on the note
    assert "history[replace ? \"replaceState\" : \"pushState\"]" in js
    assert "/learn/session?id=" in js                    # a reload re-attaches to the live session
    assert "/learn/skip" in js and "Skip question" in js and "End session" in js
    assert "lrEscArmed" not in js                         # the double-Esc stop is gone (refinement §9)
    css = app_css()
    assert "body.note-open .view" in css                  # every view ends before the note (one rule)
    assert "#learn-bar" in css and ".lr-conf" in css and "aria-live" in js
    # ADR-0081 §1: a row's action shows on hover, on focus and on the selected row
    act = css[css.index(".lr-sg .act {"):css.index(".lr-more {")]
    assert "opacity: 0" in act and ".lr-sg:focus-within .act" in act and ".lr-sg.on .act" in act
    keys = js[js.index("function learnKeys"):]
    for key in ('"Enter"', '"h"', '"n"', '"Escape"'):
        assert key in keys


# ------------------------------------------------------------------ 2026-09-07: objectives, points, origin

def test_note_detail_sizes_a_session_on_key_points_without_revealing_a_claim(client):
    c, _ = client
    d = c.get("/learn/note", params={"path": "ML/Margine geometrico.md"}).json()
    assert d["questions"] >= 2 and d["points"][0]["kind"] == "definition" and d["points"][0]["objective"] == "recall"
    assert d["also_in"] == [{"path": "Plans/ML.md", "name": "ML plan"}] and d["objectives"] == ["explain"]   # the plan's
    assert "limita l'errore" not in json.dumps(d, ensure_ascii=False)                                    # G5
    assert c.get("/learn/note", params={"path": "Nowhere.md"}).status_code == 404


def test_a_note_session_asks_every_point_records_objective_and_point_and_skips_without_writing(client, tmp_path):
    c, _ = client
    s = c.post("/learn/session", json={"note": "ML/Margine geometrico.md"}).json()
    assert s["origin"] == {"kind": "note", "path": "ML/Margine geometrico.md", "label": "Margine geometrico"}
    assert s["n"] == len(s["items"]) >= 2 and s["items"][0]["point"]["i"] == 1 and s["items"][0]["objective"]
    assert s["objectives"] == ["explain"]                                    # inherited from the plan that includes the note
    sk = c.post("/learn/skip", json={"id": s["id"], "i": 1}).json()
    assert sk["skipped"] == [1]
    live = c.get("/learn/session", params={"id": s["id"]}).json()
    assert live["skipped"] == [1] and live["done"] == [] and live["items"][0]["q"]
    it = s["items"][1]
    c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "il bound", "conf": 70, "hints": 0, "told": False})
    r = c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 3}).json()
    assert r["objective"] == it["objective"]
    line = json.loads((tmp_path / "quiz.jsonl").read_text().splitlines()[-1])
    assert line["objective"] == it["objective"] and line["point"] == it["point"]["i"]
    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    assert rc["origin"]["kind"] == "note" and rc["tally3"]["skipped"] == 1 and rc["items"][0]["status"] == "skipped"
    assert rc["points"][0]["status"] == "skipped" and rc["followup"]["points"] == [1] and rc["empty"] is False
    assert any(m["what"] == "explain" for m in rc["moved"]) or rc["by_objective"]


def test_closing_with_nothing_answered_says_empty_and_a_reload_reattaches(client):
    c, _ = client
    s = _start(c, plan="Plans/ML.md")
    assert c.get("/learn").json()["live_info"]["cur"] == 1
    assert c.get("/learn/plan", params={"path": "Plans/ML.md"}).json()["open_session"] is True
    assert c.post("/learn/session", json={"n": 2}).status_code == 409
    live = c.get("/learn/session", params={"id": s["id"]}).json()
    assert live["id"] == s["id"] and live["origin"]["kind"] == "plan" and "answer" not in json.dumps(live)
    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    assert rc["empty"] is True and rc["recorded"] == 0 and rc["plan_line"] is None
    assert c.get("/learn/session", params={"id": s["id"]}).status_code == 404


def test_plan_preview_proposes_objectives_and_write_keeps_the_edited_cards(client):
    c, _ = client
    # ADR-0081 §16: the preview is model-free; the cards are their own call, run in the background
    pv = c.post("/learn/plan", json={"scope": "folder", "target": "ML/", "preview": True, "objectives": ["explain", "apply"]}).json()
    assert pv["cards"] == [] and pv["objectives"] == ["explain", "apply"] and pv["parts"] == 1
    prev = c.post("/learn/plan", json={"scope": "folder", "target": "ML/", "cards_only": True, "objectives": ["explain", "apply"]}).json()
    assert [x["id"] for x in c.get("/learn/objectives").json()] == ["recall", "explain", "apply", "compare", "evaluate"]
    assert len(prev["cards"]) == 2 and prev["cards"][0]["source"] == "chosen" and prev["model"] is False
    auto = c.post("/learn/plan", json={"scope": "folder", "target": "ML/", "cards_only": True, "objectives": []}).json()
    assert auto["cards"][0]["source"] == "proposed"
    cards = prev["cards"]
    cards[0]["title"] = "Explain why the margin bounds the error."
    w = c.post("/learn/plan", json={"name": "Margini", "scope": "folder", "target": "ML/", "objectives": ["explain", "apply"], "cards": cards}).json()
    rep = c.get("/learn/plan", params={"path": w["path"]}).json()
    assert rep["objectives"] == ["explain", "apply"] and rep["cards"][0]["title"] == "Explain why the margin bounds the error."
    row = next(p for p in c.get("/learn/plans").json() if p["path"] == w["path"])
    assert row["objective_labels"] == ["Explain how and why", "Apply to a case"] and row["progress"][1] == 2


def test_a_spoken_answer_is_graded_as_speech_and_recorded_as_voice(client, tmp_path, monkeypatch):
    """ADR-0049: the grader is told the production was transcribed, and the
    ledger keeps the modality so agreement can be read per modality. A typed
    line writes no key at all: absence has always meant typed."""
    from silica.kernel.learn import grade

    c, _ = client
    seen = []
    real = grade.grade
    monkeypatch.setattr(grade, "grade", lambda it, **kw: (seen.append(kw.get("spoken")), real(it, **kw))[1])
    s = c.post("/learn/session", json={"note": "ML/Margine geometrico.md"}).json()
    for it, modality in zip(s["items"][:2], ("voice", None)):
        body = {"id": s["id"], "i": it["i"], "production": "il margine", "conf": 70, "hints": 0,
                "told": False, "force_model": True}
        if modality:
            body["modality"] = modality
        c.post("/learn/answer", json=body)
        assert c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 3}).status_code == 200
    assert seen == [True, False]
    lines = [json.loads(l) for l in (tmp_path / "quiz.jsonl").read_text().splitlines()]
    assert [l.get("modality") for l in lines] == ["voice", None]


def test_the_answer_box_takes_dictation_and_submit_names_the_modality():
    """ADR-0049, pinned on the page: the mic joins the Learn answer box with
    the longer take, a submit mid-take is refused, and both answer posts say
    whether the words were spoken."""
    js = app_js()
    assert "attachMic(ta, b, {" in js and "maxMs: LR_MIC_MAX_MS" in js
    assert 'micBusy("lr-answer")' in js
    assert js.count('"voice" : "typed"') == 2
    # read aloud asks the probe without loading the transcriber
    assert 'fetch("/stt?warm=0")' in js


# ------------------------------------------------------------------ 2026-10-06: ADR-0061, each fact once


def test_learn_is_on_the_shell_scale_and_states_are_marks():
    """ADR-0061: nothing in Learn under the 12px floor or in tracked uppercase;
    a learning state is a mark, never the diff's --add, and caution alone is
    amber; a question's markdown is rendered with a cloze's blank as a gap."""
    css = (WEB / "app-learn.css").read_text(encoding="utf-8")
    sizes = [float(x) for x in re.findall(r"font-size:\s*([\d.]+)px", css)]
    assert sizes and min(sizes) >= 12
    assert "uppercase" not in css and "var(--add)" not in css
    assert ".lr-mk.full {" in css and ".lr-mk.half {" in css and ".lr-mk.warn {" in css
    # the segments and the checklist draw no browser outset border (seen 2026-10-06)
    assert ".lr-seg button," in css[:css.index(".lr-back {")]
    learn = (WEB / "app-learn.js").read_text(encoding="utf-8")
    assert "function lrMd(" in learn and 'class="lr-gap"' in learn
    # the intentions are toggle chips with a real pressed state (ADR-0081 §14), never a div posing as one
    goals = learn[learn.index("function lrGoalsSection"):learn.index("function lrBySection")]
    assert 'setAttribute("aria-pressed"' in goals and '"role", "checkbox"' not in learn
    # one fact once: no tooltip glossary, no explanations in the flow
    assert "mcard-sub" not in learn and '"mnote"' not in learn
    keys = learn[learn.index("function learnKeys"):]
    assert '"ArrowDown"' in keys and '"ArrowUp"' in keys and "replace: true" in keys



# ------------------------------------------------------------------ 2026-10-08: ADR-0081, one queue that says why

def _ledger(tmp_path, *lines):
    with (tmp_path / "quiz.jsonl").open("a", encoding="utf-8") as f:
        for e in lines:
            f.write(json.dumps(e) + "\n")


def test_next_groups_say_why_from_the_ledger_and_the_note_carries_its_history(client, tmp_path):
    c, _ = client
    ts = datetime.now(timezone.utc).isoformat()
    m = "ML/Margine geometrico.md"
    _ledger(tmp_path, {"ts": ts, "path": m, "correct": False, "q": "Cos'è il margine?", "kind": "recall",
                       "session": "s1", "rating": 1, "conf": 50, "hints": 0, "told": False, "source": {"line": 5}},
            {"ts": ts, "path": m, "correct": False, "q": "Cosa limita?", "kind": "recall",
             "session": "s1", "rating": 1, "conf": 90, "hints": 0, "told": False})
    d = c.get("/learn").json()
    n = d["next"]
    # the plan resumes on Iperpiano (first in its order, not tested yet)
    assert n["plans"][0]["name"] == "ML plan" and n["plans"][0]["why"] == "Iperpiano · first in order, not tested yet"
    missed = n["missed"][0]
    assert missed["path"] == m and missed["title"] == "Margine geometrico" and missed["why"].startswith("sure, but wrong")
    assert "in ML plan" in missed["why"] and missed["questions"] == 2
    assert all(r["path"] != m for g in ("due", "never") for r in n[g])     # one group per note
    assert d["session"]["questions"] >= 1 and d["stamp"]["waiting"] == d["due"]
    note = c.get("/learn/note", params={"path": m}).json()
    assert note["group"] == "missed" and sorted(h["word"] for h in note["history"]) == ["missed", "sure, but wrong"]
    assert note["history"][0]["day"] and "A miss schedules nothing" in note["sentence"]
    # met without help afterwards: the note leaves Missed lately
    _ledger(tmp_path, {"ts": datetime.now(timezone.utc).isoformat(), "path": m, "correct": True, "q": "x",
                       "kind": "recall", "session": "s2", "rating": 3, "conf": 70, "hints": 0, "told": False})
    assert not c.get("/learn").json()["next"]["missed"]
    # the plan's next note missed: its row says the miss, the more useful reason
    _ledger(tmp_path, {"ts": datetime.now(timezone.utc).isoformat(), "path": "ML/Iperpiano.md", "correct": False, "q": "y",
                       "kind": "recall", "session": "s3", "rating": 1, "conf": 50, "hints": 0, "told": False})
    assert c.get("/learn").json()["next"]["plans"][0]["why"].startswith("Iperpiano · missed")


def test_the_map_marks_each_note_by_its_last_answer_and_filters_by_plan(client, tmp_path):
    c, _ = client
    ts = datetime.now(timezone.utc).isoformat()
    _ledger(tmp_path, {"ts": ts, "path": "ML/Iperpiano.md", "correct": True, "q": "x", "kind": "recall", "session": "s1",
                       "rating": 2, "hints": 1, "told": False})
    m = c.get("/learn/map").json()
    ml = next(f for f in m["folders"] if f["folder"] == "ML")
    marks = {x["title"]: x["mark"] for x in ml["notes"]}
    assert marks == {"Iperpiano": "half", "Margine geometrico": "never"} and ml["plans"] == ["Plans/ML.md"]
    assert m["practised"] == 1 and m["plans"] == [{"path": "Plans/ML.md", "name": "ML plan"}]
    assert all(x["path"] != "Plans/ML.md" for f in m["folders"] for x in f["notes"])       # a plan is not material


def test_the_plan_detail_carries_lately_and_after_this(client, tmp_path):
    c, _ = client
    _ledger(tmp_path, {"ts": datetime.now(timezone.utc).isoformat(), "path": "ML/Iperpiano.md", "correct": False, "q": "x",
                       "kind": "recall", "session": "s1", "rating": 1, "hints": 0, "told": True, "misconception": "nessuna"})
    rep = c.get("/learn/plan", params={"path": "Plans/ML.md"}).json()
    assert rep["lately"] == [{"path": "ML/Iperpiano.md", "title": "Iperpiano", "questions": 1,
                              "why": rep["lately"][0]["why"], "misconception": ""}]
    assert rep["lately"][0]["why"].startswith("told the answer")
    assert [a["path"] for a in rep["after"]] == ["ML/Margine geometrico.md"]


def test_scopes_typeahead_and_the_reading_route(client, monkeypatch):
    from silica.kernel.learn import items

    c, _ = client
    opts = c.get("/learn/scopes", params={"q": "iper"}).json()
    assert {"kind": "notes", "target": "Iperpiano", "label": "Iperpiano", "count": 1, "path": "ML/Iperpiano.md"} in opts
    assert c.get("/learn/scopes").json()[0] == {"kind": "folder", "target": "ML/", "label": "ML/", "count": 2}
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("KIND: folder\nTARGET: ML/\nONLY_IN: none\nNAME: Esame ML", "stop"))
    r = c.post("/learn/plan/read", json={"sentence": "Esame di ML il 30 novembre 2099, devo saperlo spiegare"}).json()
    assert (r["scope"], r["target"], r["name"], r["by"], r["objectives"]) == ("folder", "ML/", "Esame ML", "2099-11-30", ["explain"])


def test_a_plan_is_created_with_template_cards_while_they_are_written_and_edited_in_place(client):
    c, _ = client
    w = c.post("/learn/plan", json={"name": "Margini", "scope": "folder", "target": "ML/", "objectives": ["recall"],
                                    "templates": True}).json()
    rep = c.get("/learn/plan", params={"path": w["path"]}).json()
    assert len(rep["cards"]) == 1 and rep["cards"][0]["intention"] == "recall"
    e = c.post("/learn/plan", json={"path": w["path"], "name": "Margini, da capo", "scope": "notes", "target": ["Iperpiano"],
                                    "objectives": ["explain"], "templates": True}).json()
    assert e["path"] == w["path"] and e["paths"] == ["ML/Iperpiano.md"]
    rep = c.get("/learn/plan", params={"path": w["path"]}).json()
    assert rep["name"] == "Margini, da capo" and rep["scope"] == "notes" and rep["objectives"] == ["explain"]


def test_a_plan_row_says_queued_only_for_the_note_the_reader_queued(client):
    c, _ = client
    c.post("/learn/revisit", json={"plan": "Plans/ML.md", "paths": ["ML/Margine geometrico.md"]})
    row = c.get("/learn").json()["next"]["plans"][0]
    assert row["why"] == "Margine geometrico · queued by you" and row["next"]["path"] == "ML/Margine geometrico.md"


def test_a_note_gone_from_the_vault_is_not_offered_in_missed_lately(client, tmp_path):
    c, _ = client
    _ledger(tmp_path, {"ts": datetime.now(timezone.utc).isoformat(), "path": "ML/Renamed.md", "correct": False, "q": "x",
                       "kind": "recall", "session": "s1", "rating": 1, "hints": 0, "told": False})
    assert not c.get("/learn").json()["next"]["missed"]


def test_a_refusal_by_line_text_holds_in_the_preview_and_through_reword(client, tmp_vault):
    c, _ = client
    tmp_vault.note("ML/Teoria della probabilita.md", "# Teoria della probabilita\n\nEventi.\n")
    body = {"scope": "steps", "target": "Iperpiano\nTeoria della mente", "objectives": ["explain"]}
    pv = c.post("/learn/plan", json={**body, "preview": True, "refused": ["Teoria della mente"]}).json()
    assert pv["tree"][1]["how"] == "refused" and pv["paths"] == ["ML/Iperpiano.md"]
    w = c.post("/learn/plan", json={**body, "name": "Esame", "refused": [1], "templates": True}).json()
    c.post("/learn/plan", json={**body, "path": w["path"], "name": "Esame", "templates": True})     # Reword: no refused
    rep = c.get("/learn/plan", params={"path": w["path"]}).json()
    assert rep["refused"] == ["Teoria della mente"] and rep["paths"] == ["ML/Iperpiano.md"]
