# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0119: Learn opens on the next session as its list, a test is a plan
only when it holds something to keep, a reference bounds a syllabus by the
notes studied, and Explain it runs the teach-back the server already had."""
# ruff: noqa: F811  the client and vault fixtures are imported, so every test that takes them reads as a redefinition
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("fastapi")

from silica.kernel.learn import plans  # noqa: E402
from tests.test_learn_panel import client  # noqa: E402,F401
from tests.test_learn_plans import vault  # noqa: E402,F401
from tests.webassets import WEB, app_css, app_js  # noqa: E402

INDEX = (WEB / "index.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the server

def test_a_reference_bounded_by_two_notes_resolves_only_inside_them(vault):
    res = plans.resolve_scope("steps", "Chain rule\nSoftmax\nResNet", restrict="ML/Chain rule.md | ML/Softmax.md")
    assert res["paths"] == ["ML/Chain rule.md", "ML/Softmax.md"]
    assert res["gaps"] == ["ResNet"]                     # in the vault, outside what is studied
    # one folder, as before
    assert plans.resolve_scope("steps", "Chain rule\nResNet", restrict="ML/Deep/")["paths"] == ["ML/Deep/ResNet.md"]


def test_a_plan_written_with_a_reference_keeps_both_prefixes(vault):
    out = plans.write_plan("Two notes", "steps", "Chain rule\nSoftmax", restrict="ML/Chain rule.md | ML/Softmax.md")
    p = plans.read_plan(out["path"])
    assert p["scope"] == "steps" and p["restrict"] == "ML/Chain rule.md | ML/Softmax.md"
    assert plans.scope_of(p)["paths"] == ["ML/Chain rule.md", "ML/Softmax.md"]


def test_a_study_of_several_folders_and_notes_resolves_their_union(vault):
    res = plans.resolve_scope("folder", "ML/Deep/ | Other/Note.md | Softmax.md")
    # a note by its path or by its name, each a part of its own after the folder's
    assert res["paths"] == ["ML/Deep/ResNet.md", "Other/Note.md", "ML/Softmax.md"]
    assert [n["node"] for n in res["tree"]] == ["ML/Deep", "Note", "Softmax"]
    # a note under a folder already chosen is counted once
    assert plans.resolve_scope("folder", "ML/ | ML/Softmax.md")["paths"].count("ML/Softmax.md") == 1
    out = plans.write_plan("Mixed", "folder", "ML/Deep/ | Softmax.md")
    assert plans.scope_of(plans.read_plan(out["path"]))["paths"] == ["ML/Deep/ResNet.md", "ML/Softmax.md"]


def test_the_ask_box_and_the_study_gather_notes_and_folders():
    """A note or a folder joins the ones already chosen, in the home's box and
    in the dialog's Study, and the field stays open for the next (ADR-0119,
    amendment 2026-10-17)."""
    js = app_js()
    for fn in ("function lrAskPick(", "function lrPickScope("):
        body = js[js.index(fn):]
        body = body[:body.index("\n}\n")]
        assert "lrJoins(o) &&" in body and "every(lrJoins)" in body
    assert "if (lrAsk.picks.every(lrJoins))" in js and "if (items.every(lrJoins)) row.appendChild(lrStudyInput(" in js


def test_learn_lists_the_next_session_with_a_reason_per_note(client):
    c, _ = client
    d = c.get("/learn").json()
    rows = d["session_rows"]
    assert [r["path"] for r in rows] == [it["path"] for it in d["preview"]["items"]]
    assert all(r["why"] and r["title"] for r in rows)
    # the reason is ADR-0081's clause, not the picker's pool label
    assert not any(r["why"] in ("Suggested for review", "Not practised yet") for r in rows)


def test_learn_offers_the_notes_changed_this_week_and_no_plan(client, tmp_vault):
    c, _ = client
    tmp_vault.note("ML/Kernel.md", "---\ndate: 2026-10-01\n---\n# Kernel\n\nUn kernel.\n")
    old = time.time() - 30 * 86400
    p = tmp_vault.note("ML/Vecchia.md", "---\ndate: 2026-01-01\n---\n# Vecchia\n\nVecchia.\n")
    os.utime(p, (old, old))
    recent = [r["path"] for r in c.get("/learn").json()["recent"]]
    assert "ML/Kernel.md" in recent and "ML/Vecchia.md" not in recent
    assert not any(x.startswith("Plans/") for x in recent)   # a plan is never material
    assert len(recent) <= 4


def test_a_note_never_tested_says_so_in_plain_words(client):
    c, _ = client
    n = c.get("/learn").json()["next"]
    assert n["plans"][0]["why"] == "Iperpiano · first in order, not tested yet"
    whys = [r["why"] for g in ("due", "never") for r in n[g]] + [r["why"] for r in c.get("/learn").json()["session_rows"]]
    assert not any("never practised" in w for w in whys)


def test_a_teach_session_keeps_one_item_and_a_reload_finds_the_transcript(client):
    c, _ = client
    s = c.post("/learn/session", json={"note": "ML/Margine geometrico.md", "teach": True}).json()
    assert s["mode"] == "teach" and len(s["items"]) == 1 and s["n"] == 1
    t = c.post("/learn/teach", json={"id": s["id"], "i": 1, "reply": "Il margine misura la distanza dal piano."}).json()
    assert t["done"] is False and t["turn"] == 1 and t["of"] == 4 and t["question"]
    live = c.get("/learn/session", params={"id": s["id"]}).json()
    assert live["mode"] == "teach"
    said = live["teach"]["1"]
    assert said["turn"] == 1 and [x["role"] for x in said["transcript"]] == ["reader", "student"]
    # a plain session says nothing of the sort
    c.post("/learn/close", json={"id": s["id"]})
    plain = c.post("/learn/session", json={"n": 2}).json()
    assert plain["mode"] == "questions"


# ------------------------------------------------------------------ the page

def test_the_home_is_one_column_that_asks_what_you_would_like_to_learn():
    js = app_js()
    assert "What would you like to learn?" in js
    assert "Test yourself on your notes, then see what you missed." in js
    assert '"Form"' in js and '"Test me"' in js
    # the segments of ADR-0081 are gone from the bar
    assert '["next", "Next", { view: "home" }]' not in js
    # the session is its list, and Drop edits it (ADR-0119 Gate)
    assert "session_rows" in js and 'from: "edited"' in js
    for word in ('"Fading"', '"Not tested yet"', '"Missed"', '"What you know"', '"Changed this week"'):
        assert word in js, word


def test_a_test_is_written_as_a_plan_only_when_it_holds_something_to_keep():
    js = app_js()
    keep = js[js.index("function lrKeeps("):]
    keep = keep[:keep.index("\n}\n")]
    for field in ("f.ref", "f.by", "f.objectives"):
        assert field in keep, field
    assert '"Save only"' in js and "Kept as a plan" in js
    # the dialog sits over the home; Reference is a field of its own
    assert "lr-dialog" in js and '"Reference"' in js and "lr-dialog" in app_css()


def test_explain_it_and_test_me_open_from_the_note_menu():
    # ADR-0119 reopens the note menu to practice (ADR-0075 had moved it to
    # Learn's rows, which no longer list every note)
    assert 'id="note-test"' in INDEX and 'id="note-teach"' in INDEX
    js = app_js()
    assert '$("#note-test")' in js and '$("#note-teach")' in js
    assert 'teach: true' in js and "/learn/teach" in js


def test_the_receipt_opens_on_what_was_missed():
    js = app_js()
    row = js[js.index("function lrMissedRow("):]
    row = row[:row.index("\n}\n")]
    assert "Read it in the note" in row and "openNote(file, span?.line)" in row
    rc = js[js.index("function lrRenderReceipt("):]
    assert rc.index("lrMissedList(") < rc.index('"The figures"')


def test_a_reference_is_a_syllabus_bounded_by_what_is_studied(client):
    c, _ = client
    body = {"scope": "folder", "target": "ML/", "reference": "Iperpiano\nTeoria della mente"}
    pv = c.post("/learn/plan", json={**body, "preview": True}).json()
    assert [n["node"] for n in pv["tree"]] == ["Iperpiano", "Teoria della mente"]
    assert pv["paths"] == ["ML/Iperpiano.md"] and pv["gaps"] == ["Teoria della mente"]
    w = c.post("/learn/plan", json={**body, "name": "Esame ML", "templates": True}).json()
    rep = c.get("/learn/plan", params={"path": w["path"]}).json()
    assert rep["scope"] == "steps" and rep["restrict"] == "ML/"
    # a note studied bounds it by its path; no Study, the whole vault
    # pasted text ends in a line break, or one line equal to a title would be read as that note
    one = c.post("/learn/plan", json={"scope": "notes", "target": ["Iperpiano"], "reference": "Iperpiano\n", "preview": True}).json()
    assert one["paths"] == ["ML/Iperpiano.md"]
    assert c.post("/learn/plan", json={"scope": "", "reference": "Iperpiano\n", "preview": True}).json()["paths"] == ["ML/Iperpiano.md"]
    # a folder and a note named by its title: the bound is the note's path
    mixed = c.post("/learn/plan", json={"scope": "folder", "target": "sources/ | Iperpiano.md", "reference": "Iperpiano\n", "preview": True}).json()
    assert mixed["paths"] == ["ML/Iperpiano.md"]


def test_the_teach_summary_speaks_the_explanations_language(monkeypatch):
    from silica.kernel.learn import grade

    seen = []
    monkeypatch.setattr(grade, "_llm", lambda prompt, *a, **k: (seen.append(prompt), ("GRADE: partial\nPRECISE: no\nRUBRIC: manca\nFEEDBACK: Hai detto.\nMISCONCEPTION: none\n", "stop"))[1])
    said = [{"role": "reader", "text": "L'apprendimento è un cambiamento stabile del comportamento dovuto all'esperienza e alle sue conseguenze."},
            {"role": "student", "text": "E il condizionamento classico?"}]
    v = grade.teach_summary({"title": "L'apprendimento", "span": "", "path": "P/L'apprendimento.md", "kind": "explain", "level": 3}, said, ["Una claim."])
    assert "LANGUAGE: Italian" in seen[0] and 'as "you"' in seen[0]
    assert v["summary"] == "manca"


def test_two_ratings_at_once_write_one_ledger_line(client, tmp_path, monkeypatch):
    """Enter pressed while the first rating was in flight wrote two lines
    (2026-10-16): the route is sync, so the two posts ran on two threads."""
    import json
    import threading

    from silica.kernel.report import learner

    c, _ = client
    s = c.post("/learn/session", json={"note": "ML/Margine geometrico.md"}).json()
    it = s["items"][0]
    c.post("/learn/answer", json={"id": s["id"], "i": it["i"], "production": "il margine", "conf": 70, "hints": 0, "told": False})
    real = learner.view
    monkeypatch.setattr(learner, "view", lambda *a, **k: (time.sleep(0.3), real(*a, **k))[1])
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(c.post("/learn/rate", json={"id": s["id"], "i": it["i"], "rating": 3}).status_code)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes == [200, 200]
    lines = [json.loads(x) for x in (tmp_path / "quiz.jsonl").read_text().splitlines()]
    assert len([x for x in lines if x.get("session") == s["id"]]) == 1


def test_a_one_line_pasted_reference_reads_back_as_text(vault):
    out = plans.write_plan("One line", "steps", "Softmax\n", restrict="ML/")
    p = plans.read_plan(out["path"])
    assert p["target"] == "Softmax\n"                      # still text: Edit plan offers it as pasted
    assert plans.scope_of(p)["paths"] == ["ML/Softmax.md"]
    # a note's name stays a name
    vault.note("ML/Programma.md", "# Programma\n\n- Softmax\n- Chain rule\n")
    q = plans.read_plan(plans.write_plan("By note", "steps", "Programma", restrict="ML/")["path"])
    assert q["target"] == "Programma" and plans.scope_of(q)["paths"] == ["ML/Softmax.md", "ML/Chain rule.md"]


def test_the_session_list_names_no_earlier_session_before_any_answer():
    """A fresh vault of six papers opened Learn on "8 questions waiting from
    earlier sessions", none ever run (2026-10-26)."""
    js = app_js()
    body = js[js.index("function lrSessionList(d)"):]
    body = body[:body.index("\n}\n")]
    assert 'd.measured ? "waiting from earlier sessions" : "for your first session"' in body


def test_a_pair_never_answered_is_not_counted_as_fading():
    from silica.ui.web.learn_routes import _mix

    groups = {"missed": [], "due": [{"path": "a.md"}], "never": []}   # the pair sits in the "due" list
    items = [{"path": "a.md", "pool": "contrast", "attempts": 0},
             {"path": "b.md", "pool": "unexplored", "attempts": 0},
             {"path": "c.md", "pool": "due", "attempts": 0}]       # a card from writing it (D10)
    assert _mix(items, groups) == "1 fading, 2 not tested yet"
