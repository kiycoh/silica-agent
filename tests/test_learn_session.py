# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The session engine with no HTTP in the way: a whole session through
`silica.kernel.learn.session`, its ledger line, its `## Sessions` line and the
one start the tool shares."""
# ruff: noqa: F811  the client fixture is imported, so every test that takes it reads as a redefinition
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from silica.kernel.learn import pick, plans, quiz, session  # noqa: E402
from tests.test_learn_panel import client  # noqa: E402,F401


def test_a_whole_session_runs_on_the_engine_alone(client):
    s = session.start(n=2, plan="Plans/ML.md")
    assert s["origin"] == {"kind": "plan", "path": "Plans/ML.md", "label": "ML plan"}
    first = s["items"][0]["i"]
    session.answer(s["id"], first, production="il margine", conf=70)
    assert session.rate(s["id"], first, 2)["recorded"] is True
    assert session.rate(s["id"], first, 4)["duplicate"] is True
    assert session.reload(s["id"])["done"] == [first]
    rc = session.close(s["id"])
    assert rc["recorded"] == 1 and rc["plan_line"] and session.live() is None
    assert session.receipt(s["id"]) is rc
    assert plans.read_plan("Plans/ML.md")["sessions"][0]["session"] == s["id"]


@pytest.mark.parametrize("call, why, text", [
    (lambda: session.start(plan="Plans/none.md"), session.MISSING, "no plan at 'Plans/none.md'"),
    (lambda: session.start(note="no/such.md"), session.MISSING, "no note at 'no/such.md'"),
    (lambda: session.reload("nope"), session.MISSING, "no live session with that id"),
    (lambda: session.close("nope"), session.MISSING, "no live session with that id"),
    (lambda: session.receipt("nope"), session.MISSING, "no recorded events for that session"),
])
def test_what_is_not_there_is_refused_by_name(client, call, why, text):
    with pytest.raises(session.Refused) as got:
        call()
    assert got.value.why == why and str(got.value) == text


def test_one_session_at_a_time_and_a_rating_needs_an_answer(client):
    s = session.start(n=2)
    with pytest.raises(session.Refused) as busy:
        session.start(n=2)
    assert busy.value.why == session.BUSY
    i = s["items"][0]["i"]
    with pytest.raises(session.Refused) as early:
        session.rate(s["id"], i, 3)
    assert early.value.why == session.INVALID and str(early.value) == "answer first"
    with pytest.raises(session.Refused) as gone:
        session.answer(s["id"], 99)
    assert gone.value.why == session.MISSING


def test_every_field_the_entry_builds_reaches_the_ledger_but_the_one_it_names(client):
    """`quiz.record` keeps only the fields it knows and says nothing of the rest,
    which is how `axis` came to be built for a line that never held it."""
    it = {"i": 1, "path": "ML/Iperpiano.md", "kind": "structure", "level": 3, "q": "q", "anchor": "h", "objective": "explain",
          "source": {"leaf": "sources/Lezione 11.md", "line": 3}, "point": {"i": 2}, "endpoints": ["a", "b"],
          "placement": {"step": 1, "rank": 0}, "retest": True, "rungs": ["H1", "H2"]}
    v = {"conf": 90, "hints": 1, "told": True, "paraphrase": False, "misconception": "m", "miss": "structural", "modality": "voice"}
    s = {"id": "abc", "plan": "Plans/ML.md"}
    entry = {**session.ledger_entry(s, it, v, False, 2, "model", 3), "concepts": ["c"]}
    assert quiz.record([entry]) == 1
    line = json.loads(quiz.log_path().read_text(encoding="utf-8").splitlines()[-1])
    assert set(entry) - set(line) == set(session.NOT_IN_LEDGER)


def test_the_sessions_line_is_composed_and_read_by_one_definition(client):
    rc = {"recorded": 3, "tally": {"Good": 1, "Easy": 1, "Hard": 1, "Again": 0}, "next": "Next session draws from what is due.",
          "by_objective": [{"objective": "explain", "met": 2}]}
    sid = "0123456789abcdef"
    line = session.compose_line(rc, sid)
    assert line.endswith(f"ratings: 2 Good/Easy · 1 Hard · 0 Again · met: explain 2 · Next session draws from what is due · session: {sid}")
    assert session.line_id(line) == sid and session.has_line(f"- {line}\n", sid)
    got = session.parse_lines(["", "text", f"- {line}"])
    assert len(got) == 1 and got[0]["items"] == 3 and got[0]["met"] == "explain 2" and got[0]["session"] == sid
    assert got[0]["date"] == line[:10] and got[0]["line"] == line


def test_a_line_from_before_the_id_reads_with_none(client):
    assert session.parse_lines(["- 2026-01-02 · 1 item · ratings"]) == [
        {"date": "2026-01-02", "items": 1, "line": "2026-01-02 · 1 item · ratings", "session": "", "met": ""}]


def test_the_agent_draws_without_the_plan_s_date_or_its_placement(client, monkeypatch):
    """`silica_learn_items` shares the start and never had a deadline or later
    notes: a dated plan reads the same to it as an undated one."""
    seen = []
    real = pick.session
    monkeypatch.setattr(pick, "session", lambda **kw: (seen.append(kw), real(**kw))[1])
    monkeypatch.setattr(session, "_placed_notes", lambda plan: {"ML/Iperpiano.md"})
    plan = dict(plans.find_plan("Plans/ML.md"), by="2099-01-01")
    session.draw(2, plan=plan, paced=False)
    assert seen[-1]["deadline"] is None and not seen[-1]["later"]
    session.draw(2, plan=plan)
    assert seen[-1]["deadline"] == "2099-01-01" and seen[-1]["later"] == {"ML/Iperpiano.md"}
    assert session.live() is None     # drawing keeps no session


def test_a_prefix_and_a_workspace_bound_the_draw(client, monkeypatch):
    seen = []
    real = pick.session
    monkeypatch.setattr(pick, "session", lambda **kw: (seen.append(kw["scope"]), real(**kw))[1])
    session.draw(1, scope="ml/")
    assert sorted(seen[-1]) == ["ML/Iperpiano.md", "ML/Margine geometrico.md"]
    session.draw(1, scope="ML/", within={"ML/Iperpiano.md", "elsewhere.md"})
    assert seen[-1] == ["ML/Iperpiano.md"]
    session.draw(1, within={"ML/Iperpiano.md"})
    assert seen[-1] == ["ML/Iperpiano.md"]


def test_a_build_outside_a_session_leaves_the_turn_s_own_meter_alone(client, monkeypatch):
    """`meter_scope(None)` counts nowhere, so an agent's draw entering it would
    take the turn's calls off the turn's meter."""
    from silica.kernel.learn import items
    from silica.model import llm

    scopes = []
    real = items.build_session
    monkeypatch.setattr(items, "build_session", lambda *a, **k: (scopes.append(llm._SCOPE.get()), real(*a, **k))[1])
    mine = llm.new_meter()
    with llm.meter_scope(mine):
        session.draw(1)
    assert scopes[-1] is mine
