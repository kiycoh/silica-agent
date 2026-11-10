# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0110, amendment of 2026-11-06: a selection is checked where it stands,
against the vault and against the web. Every leg is handed in, so nothing here
reaches a network or a model."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from silica.kernel.check import GRADES, blocks, judge as jd, run, web
from silica.kernel.text.text import squash

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def _web_open(monkeypatch):
    """The web entry is off by default since its gate failed (web.is_open): what
    these tests hold is the path a pass on a new set would open."""
    monkeypatch.setenv("SILICA_CHECK_WEB", "1")


PAGE_30 = ("Consumer rights. The time limit for exercising the short-term right to reject is the end of 30 days "
           "beginning with the first day after ownership has passed to the consumer. Other remedies follow.")
PAGE_OFF = "A page about something else entirely, with no word of the claim in it at all."
CLAIM = "The short-term right to reject lasts 14 days from delivery of the goods."


def page(text):
    return SimpleNamespace(text=f"Source: https://x.example/a\n\n{text}", title="t")


def lanes(*urls, lane="mojeek"):
    return lambda q: ([{"title": "t", "url": u, "content": ""} for u in urls], lane)


def verdict(kind, at="https://a.example/1:0", p=0.9):
    return lambda text, passages: {"kind": kind, "p": p, "by": "jev", "at": at}


# -- the selection, against the vault (points 4 and 5) -------------------------

NOTE = """---
title: x
---

# Heading of six plain ordinary words here

First line of a paragraph that runs
over two lines of the file, with words enough.

Too short here.

Second paragraph stands alone on its own line of the note.
"""


def test_a_selection_inside_a_paragraph_of_several_lines_touches_it():
    every, _ = blocks.split(NOTE)
    first = every[0]
    assert (first.line, first.end) == (7, 8)
    assert run.overlapping(every, (8, 8)) == [first]
    assert run.overlapping(every, (7, 7)) == [first]
    assert run.overlapping(every, (7, 8)) == [first]
    assert [b.line for b in run.overlapping(every, (8, 12))] == [7, 12]
    assert run.overlapping(every, (5, 5)) == [] and run.overlapping(every, (10, 10)) == []


def test_a_block_with_no_end_ends_at_its_line():
    b = blocks.Block(4, "paragraph", "x")
    assert b.end == 0
    assert run.overlapping([b], (4, 4)) == [b] and run.overlapping([b], (5, 9)) == []


def test_a_ranged_check_finds_the_paragraph_a_selection_sits_in(tmp_path):
    (tmp_path / "Draft.md").write_text(NOTE, encoding="utf-8")
    out = run.check_note("Draft.md", str(tmp_path), lines=(8, 8))
    assert out["blocks"] == 1


def test_supported_says_what_supported_it_and_keeps_its_say():
    _f, states = run._verdict_rows({"kind": "supported", "p": 0.9, "by": "jev", "at": "Notes/A.md:12"}, 3)
    assert states == [{"state": "supported", "line": 3, "say": "Jev, p 0.90", "at": "Notes/A.md:12"}]
    # a verdict cached before the field existed reads as it did
    assert "at" not in run._verdict_rows({"kind": "supported", "p": 0.9, "by": "jev"}, 3)[1][0]


def test_jev_names_the_passage_a_supported_verdict_rests_on():
    two = [{"path": "a.md", "line": 3, "text": "x"}, {"path": "b.md", "line": 4, "text": "y"}]

    def decide(state, questions):
        if "verdict" in questions:
            return {"verdict": {"choice": "supported", "probabilities": {"supported": 0.9}}}
        return {"rests_on": {"choice": "2"}}

    assert jd.by_jev("t", two, decide)["at"] == "b.md:4"
    assert jd.by_jev("t", two[:1], decide)["at"] == "a.md:3"       # one passage: no second call


def test_web_disagrees_is_a_warning_and_never_an_error():
    assert GRADES["web disagrees"] == "warning"


def test_the_selection_widens_under_six_words_and_refuses_past_600_characters():
    claim, hit = web.aim(NOTE, (12, 12), "stands alone")
    assert claim == hit[0].text and claim.startswith("Second paragraph")
    claim, hit = web.aim(NOTE, (12, 12), "Second paragraph stands alone on its own")
    assert claim == "Second paragraph stands alone on its own" and hit[0].line == 12
    with pytest.raises(ValueError, match="select one statement"):
        web.aim(NOTE, (12, 12), "x " * 301)
    long = "---\na: b\n---\n\n" + "word " * 200 + "\n"
    with pytest.raises(ValueError, match="select one statement"):
        web.aim(long, (5, 5), "word word")        # widened past the limit


def test_a_heading_code_or_short_block_has_nothing_to_check():
    assert web.aim(NOTE, (5, 5), "Heading of six plain ordinary words here") == ("", [])
    assert web.aim(NOTE, (10, 10), "Too short here.") == ("", [])


# -- the window and the quote (points 6 and 15) --------------------------------

def test_the_window_holds_most_of_the_claims_words_from_its_sentence():
    filler = "Nothing to see in this sentence at all. " * 200
    text = filler + "The right to reject lasts thirty days. " + filler
    got = web.window(text, "right to reject lasts", 80)
    assert got.startswith("The right to reject lasts thirty days.") and len(got) <= 80


def test_a_page_with_none_of_the_claims_words_is_read_from_its_head():
    assert web.window("Il termine è di trenta giorni.", "The period is thirty days", 100) == "Il termine è di trenta giorni."


def test_a_window_never_outruns_its_width():
    assert len(web.window("word " * 5000, "word", 4000)) <= 4000


def test_the_quote_is_the_pages_own_words_and_says_when_it_is_cut():
    text = "A first sentence about nothing. " * 30 + "Rejection runs\n  for 30 days from delivery.\n" + "Tail. " * 100
    q = web.quote(text, "Rejection runs for 30 days")
    assert "Rejection runs for 30 days from delivery." in q and q.endswith("…")
    assert squash(q.rstrip("…")).casefold() in squash(text).casefold()
    assert web.quote("Short page.", "Short page") == "Short page."


# -- a press (points 6, 7 and 14) ----------------------------------------------

def test_the_steps_run_in_order_and_the_lanes_get_the_selection():
    seen = {}

    def search(q):
        seen["query"] = q
        return [{"url": "https://a.example/1"}, {"url": "https://b.example/2"}], "mojeek"

    def judge(text, passages):
        seen["passages"] = passages
        return {"kind": "contradicted", "p": 0.9, "by": "jev", "at": "https://b.example/2:0"}

    steps = []
    out = web.check_claim(CLAIM, search=search, fetch=lambda u: page(PAGE_30), judge=judge, step=steps.append)
    assert seen["query"] == CLAIM
    assert steps == [{"step": "asking"}, {"step": "reading", "pages": 2}]
    assert [(p["path"], p["line"]) for p in seen["passages"]] == [("https://a.example/1", 0), ("https://b.example/2", 0)]
    assert "Source:" not in seen["passages"][0]["text"]
    assert out["web"] == "disagrees" and out["url"] == "https://b.example/2" and out["lane"] == "mojeek"
    assert "30 days" in out["passage"]


def test_three_pages_are_read_past_one_that_refuses():
    seen = []

    def fetch(url):
        if url.endswith("2"):
            raise ValueError("HTTP 403")
        return page(PAGE_30)

    def judge(text, passages):
        seen.extend(p["path"] for p in passages)
        return {"kind": "supported", "p": 0.9, "by": "jev", "at": passages[0]["path"] + ":0"}

    urls = [f"https://a.example/{n}" for n in range(1, 6)]
    web.check_claim(CLAIM, search=lanes(*urls), fetch=fetch, judge=judge)
    assert seen == [urls[0], urls[2], urls[3]]


@pytest.mark.parametrize("kind, web_says", [
    ("supported", "supported"), ("contradicted", "disagrees"), ("overstated", "disagrees"),
    ("off-point", ""), ("not in these passages", ""), ("not a statement of fact", ""),
])
def test_what_the_judge_says_becomes(kind, web_says):
    out = web.check_claim(CLAIM, search=lanes("https://a.example/1"), fetch=lambda u: page(PAGE_30), judge=verdict(kind))
    assert out["web"] == web_says
    if not web_says:
        assert out["say"] == "no page spoke to this"


@pytest.mark.parametrize("v", [
    None,
    {"state": "not checked", "kind": "contradicted", "p": 0.2, "by": "jev"},     # the kind it was unsure of
    {"state": "not checked", "by": "chat", "why": "no verdict in the reply"},
    {"kind": "contradicted", "p": 0.9, "by": "jev"},                             # names no page
    {"kind": "supported", "p": 0.9, "by": "jev", "at": "https://elsewhere.example/9:0"},
])
def test_a_verdict_that_stands_on_no_page_is_not_checked(v):
    out = web.check_claim(CLAIM, search=lanes("https://a.example/1"), fetch=lambda u: page(PAGE_30), judge=lambda t, p: v)
    assert out["web"] == "" and out["say"] == web.NO_PAGE


def test_a_judge_that_breaks_is_not_checked_and_never_raises():
    def judge(text, passages):
        raise KeyError("rests_on")

    out = web.check_claim(CLAIM, search=lanes("https://a.example/1"), fetch=lambda u: page(PAGE_30), judge=judge)
    assert out["web"] == "" and out["say"] == web.NO_PAGE


def test_no_network_is_the_web_did_not_answer():
    def down(q):
        raise ConnectionError("no route to host")

    def refused(url):
        raise ValueError("HTTP 403")

    assert web.check_claim(CLAIM, search=down, fetch=page, judge=verdict("supported"))["say"] == web.NO_ANSWER
    out = web.check_claim(CLAIM, search=lanes("https://a.example/1"), fetch=refused, judge=verdict("supported"))
    assert out["say"] == web.NO_ANSWER
    assert web.check_claim(CLAIM, search=lambda q: ([], "mojeek"), fetch=page, judge=verdict("supported"))["say"] == web.NO_PAGE


def test_a_cancelled_press_stops_before_the_next_paid_call():
    def judge(text, passages):
        raise AssertionError("cancelled: no judge")

    def fetch(url):
        raise AssertionError("cancelled: no page")

    def search(q):
        raise AssertionError("cancelled: no search")

    out = web.check_claim(CLAIM, search=search, fetch=fetch, judge=judge, cancelled=lambda: True)
    assert out == {"web": "", "say": web.CANCELLED}


def test_a_check_leaves_a_chat_turns_web_state_alone(monkeypatch):
    from silica.sources import web_research as wr

    monkeypatch.setattr(wr, "ddg_search", lambda q: [{"url": "https://a.example/1"}])
    wr._reset_turn()
    wr._LANES.append("mojeek")
    wr._DEAD = 2
    hits, lane = wr.search_lanes("anything")
    assert (hits, lane) == ([{"url": "https://a.example/1"}], "duckduckgo")
    assert wr._LANES == ["mojeek"] and wr._DEAD == 2
    wr._reset_turn()


def test_the_lanes_fall_back_and_web_search_alone_counts_them(monkeypatch):
    from silica.sources import web_research as wr

    def challenged(q):
        raise ValueError("DuckDuckGo answered HTTP 202")

    monkeypatch.setattr(wr, "ddg_search", challenged)
    monkeypatch.setattr(wr, "mojeek_search", lambda q: [{"url": "https://m.example/1"}])
    wr._reset_turn()
    assert wr.search_lanes("q")[1] == "mojeek" and wr._LANES == []
    wr.web_search("q")
    assert wr._LANES == ["mojeek"] and wr._DEAD == 0

    def down(q):
        raise ValueError("lane down")

    monkeypatch.setattr(wr, "mojeek_search", down)
    monkeypatch.setattr(wr, "_wikipedia_search", down)
    with pytest.raises(ValueError, match="DuckDuckGo"):
        wr.search_lanes("q")
    assert wr._DEAD == 0
    with pytest.raises(ValueError, match="DuckDuckGo"):
        wr.web_search("q")
    assert wr._DEAD == 1
    wr._reset_turn()


# -- what it says (point 8) ----------------------------------------------------

HIT = {"url": "https://www.legislation.gov.uk/ukpga/2015/15/section/22", "passage": "the end of 30 days"}
DAY = date(2026, 11, 6)


@pytest.mark.parametrize("web_says, vault, say", [
    ("disagrees", "supported", "the vault backs this and the web does not; the source may be wrong or out of date"),
    ("supported", "contradicted", "the web backs your wording, so the source may be the one out of date"),
    ("supported", "not found", "the web backs this and the vault has no source for it"),
    ("disagrees", "not found", "the web does not back this"),
    ("supported", "supported", "the web backs this"),
    ("disagrees", "contradicted", "the web does not back this"),
])
def test_each_row_of_the_table(web_says, vault, say):
    findings, states = web.rows({"web": web_says, **HIT}, CLAIM, vault, 7, 8, DAY)
    row = (findings or states)[0]
    assert row["say"] == say
    assert (row["line"], row["end"], row["host"], row["read"]) == (7, 8, "legislation.gov.uk", "6 Nov")
    assert row["url"] == HIT["url"] and row["passage"] == HIT["passage"]


def test_web_disagrees_is_a_finding_and_supported_a_state_with_no_mark():
    findings, states = web.rows({"web": "disagrees", **HIT}, CLAIM, "supported", 7, 8, DAY)
    assert not states
    f = findings[0]
    assert (f["grade"], f["kind"], f["at"], f["mark"], f["model"]) == ("warning", "web disagrees", "", CLAIM, True)
    findings, states = web.rows({"web": "supported", **HIT}, CLAIM, "supported", 7, 8, DAY)
    assert not findings and states[0]["state"] == "web"


def test_nothing_from_the_web_is_not_checked():
    assert web.rows({"web": "", "say": web.NO_PAGE}, CLAIM, "supported", 7, 8) == (
        [], [{"state": "not checked", "line": 7, "end": 8, "say": web.NO_PAGE}])


@pytest.mark.parametrize("report, says", [
    ({"findings": [{"kind": "contradicted"}], "states": []}, "contradicted"),
    ({"findings": [{"kind": "overstated"}, {"kind": "not found"}], "states": []}, "contradicted"),
    ({"findings": [{"kind": "not found"}], "states": [{"state": "supported"}]}, "not found"),
    ({"findings": [{"kind": "off-point"}], "states": []}, "not found"),
    ({"findings": [], "states": [{"state": "supported"}]}, "supported"),
    ({"findings": [{"kind": "date not found"}], "states": [{"state": "not checked"}]}, ""),
])
def test_the_vaults_verdict_on_the_range(report, says):
    assert web.vault_says(report) == says


# -- the route (points 10, 14) -------------------------------------------------

DRAFT = "---\nmatter: x\n---\n\nThe short-term right to reject lasts 14 days from delivery.\n\nA second block of ordinary words sits here unasked.\n"


@pytest.fixture
def desk(tmp_vault, monkeypatch):
    from silica.config import CONFIG
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    tmp_vault.note("Drafts/memo.md", DRAFT)
    calls: dict = {"judged": [], "searched": []}

    def vault_judge(text, passages):
        calls["judged"].append(text)
        return {"kind": "supported", "p": 0.9, "by": "jev", "at": passages[0]["path"] + ":1"} if passages else None

    def legs():
        def search(q):
            calls["searched"].append(q)
            return [{"url": "https://www.example.org/rights"}], "mojeek"

        return search, lambda u: page(PAGE_30)

    monkeypatch.setattr("silica.session.check.judge", lambda **_: vault_judge)
    monkeypatch.setattr("silica.session.check.legs", lambda: (None, None, None))
    monkeypatch.setattr("silica.session.check.web_legs", legs)
    SESSION.reset()
    yield TestClient(server.app, headers={"Origin": "http://testserver"}), server, Path(CONFIG.vault_path), calls, monkeypatch
    SESSION.release()


def press(tc, **over):
    body = {"path": "Drafts/memo", "lines": [5, 5], "web": True, "claim": CLAIM, **over}
    return [json.loads(x) for x in tc.post("/note/check", json=body).text.splitlines()]


def judged_web(monkeypatch, kind="contradicted", **kw):
    """The judge the route gets: the vault's leg passes through, the web's answers `kind`."""
    def judge(text, passages):
        if passages and passages[0]["line"] == 0:
            return {"kind": kind, "p": 0.9, "by": "jev", "at": passages[0]["path"] + ":0"}
        return None

    monkeypatch.setattr("silica.session.check.judge", lambda **_: judge)


def test_a_press_streams_the_vault_then_the_web_then_done(desk):
    tc, _, _, calls, mp = desk
    judged_web(mp)
    out = press(tc)
    assert [x["event"] for x in out] == ["rules", "block", "web", "web", "web_verdict", "done"]
    assert out[2] == {"event": "web", "step": "asking"}
    assert out[3] == {"event": "web", "step": "reading", "pages": 1}
    v = out[4]
    assert v["states"] == []
    (f,) = v["findings"]
    assert f["grade"] == "warning" and f["kind"] == "web disagrees" and f["at"] == ""
    assert (f["line"], f["end"]) == (5, 5) and f["mark"] == CLAIM
    assert f["host"] == "example.org" and f["url"] == "https://www.example.org/rights"
    assert f["read"] and "30 days" in f["passage"]
    assert calls["searched"] == [CLAIM]


def test_the_web_never_touches_the_vaults_report(desk):
    tc, _, _, _, mp = desk
    judged_web(mp)
    done = press(tc)[-1]
    assert done["event"] == "done" and not [f for f in done["findings"] if f["kind"] == "web disagrees"]


def test_a_press_refuses_what_is_not_one_statement_before_it_streams(desk):
    tc, _, _, calls, _ = desk
    r = tc.post("/note/check", json={"path": "Drafts/memo", "lines": [5, 5], "web": True, "claim": "x " * 301})
    assert r.status_code == 400 and r.json()["detail"] == "select one statement"
    assert tc.post("/note/check", json={"path": "Drafts/memo", "web": True, "claim": CLAIM}).status_code == 400
    assert not calls["searched"] and not calls["judged"]


def test_a_selection_under_six_words_goes_out_as_its_block(desk):
    tc, _, _, calls, mp = desk
    judged_web(mp)
    press(tc, claim="14 days")
    assert calls["searched"] == ["The short-term right to reject lasts 14 days from delivery."]


def test_a_range_with_nothing_to_check_asks_no_model(desk):
    tc, _, _, calls, _ = desk
    out = press(tc, lines=[1, 3], claim="matter: x")
    assert [x["event"] for x in out] == ["rules", "web_verdict", "done"]
    assert out[1]["states"] == [{"state": "not checked", "line": 1, "end": 3, "say": web.NOTHING_TO_CHECK}]
    assert not calls["searched"]


def test_no_network_keeps_the_vaults_verdict_and_says_so(desk):
    tc, _, _, _, mp = desk

    def down(q):
        raise ConnectionError("no route")

    mp.setattr("silica.session.check.web_legs", lambda: (down, page))
    out = press(tc)
    assert [x["event"] for x in out][-2:] == ["web_verdict", "done"]
    assert out[-2]["states"] == [{"state": "not checked", "line": 5, "end": 5, "say": "the web did not answer"}]


def test_a_press_stays_out_of_the_chats_lock(desk):
    from silica.ui.web.session import SESSION
    tc, server, _, _, mp = desk
    judged_web(mp)
    assert SESSION.claim()
    assert press(tc)[-1]["event"] == "done"


def test_the_usage_line_says_web_and_no_content(desk):
    tc, server, _, _, mp = desk
    judged_web(mp)
    lines = []
    mp.setattr("silica.metrics.emit", lambda ev, **kw: lines.append((ev, kw)))
    press(tc)
    tc.post("/note/check", json={"path": "Drafts/memo"})
    (ev, kw), (_, plain) = lines
    assert ev == "check" and kw["web"] is True and "web" not in plain
    assert set(kw) == {"outcome", "ms", "blocks", "cached", "error", "warning", "nit", "web"}
    assert all(isinstance(v, (int, float, bool)) or v == "ok" for v in kw.values())


# -- what stays (point 13) -----------------------------------------------------

def cached(vault):
    return run.read_cache(str(vault), "Drafts/memo.md")


def test_the_verdict_stays_with_its_block_and_carries_its_url_quote_and_day(desk):
    tc, _, vault, _, mp = desk
    judged_web(mp)
    press(tc)
    (row,) = cached(vault)["web"].values()
    assert row["kind"] == "web disagrees" and row["url"] and row["passage"] and row["read"]
    assert "line" not in row


def test_a_rerun_from_the_cache_brings_it_back_where_its_block_now_stands(desk):
    tc, _, vault, _, mp = desk
    judged_web(mp)
    press(tc)
    (vault / "Drafts/memo.md").write_text(DRAFT.replace("---\n\nThe", "---\n\n# A heading\n\nThe"), encoding="utf-8")
    out = [json.loads(x) for x in tc.post("/note/check", json={"path": "Drafts/memo", "rules": True}).text.splitlines()]
    done = out[-1]
    (f,) = [f for f in done["findings"] if f["kind"] == "web disagrees"]
    assert (f["line"], f["end"]) == (7, 7) and f["model"] is True


def test_a_whole_note_check_does_not_drop_it(desk):
    tc, _, vault, _, mp = desk
    judged_web(mp)
    press(tc)
    out = [json.loads(x) for x in tc.post("/note/check", json={"path": "Drafts/memo"}).text.splitlines()]
    # on the rules event, which is what the drawer draws its marks from
    assert [f for f in out[0]["findings"] if f["kind"] == "web disagrees"]
    assert len(cached(vault)["web"]) == 1


def test_a_state_comes_back_as_a_state(desk):
    tc, _, _, _, mp = desk
    judged_web(mp, kind="supported")
    press(tc)
    done = [json.loads(x) for x in tc.post("/note/check", json={"path": "Drafts/memo", "rules": True}).text.splitlines()][-1]
    assert [s for s in done["states"] if s["state"] == "web"][0]["host"] == "example.org"
    assert not done["counts"]["warning"]


def test_an_edit_to_the_block_lets_it_go(desk):
    tc, _, vault, _, mp = desk
    judged_web(mp)
    press(tc)
    (vault / "Drafts/memo.md").write_text(DRAFT.replace("14 days", "15 days"), encoding="utf-8")
    done = [json.loads(x) for x in tc.post("/note/check", json={"path": "Drafts/memo", "rules": True}).text.splitlines()][-1]
    assert not [f for f in done["findings"] if f["kind"] == "web disagrees"]
    assert cached(vault)["web"] == {}


def test_a_second_press_replaces_and_a_failed_one_leaves_the_verdict_paid_for(desk):
    tc, _, vault, _, mp = desk
    judged_web(mp)
    assert not [f for f in press(tc)[-1]["findings"] if f["kind"] == "web disagrees"]
    # a press answers for itself: its report carries no earlier verdict to draw twice
    assert not [f for f in press(tc)[-1]["findings"] if f["kind"] == "web disagrees"]

    def down(q):
        raise ConnectionError("no route")

    mp.setattr("silica.session.check.web_legs", lambda: (down, page))
    press(tc)
    assert len(cached(vault)["web"]) == 1


def test_a_torn_cache_costs_a_press_and_never_a_wrong_verdict(desk):
    tc, _, vault, _, mp = desk
    judged_web(mp)
    press(tc)
    run._cache_path(str(vault), "Drafts/memo.md").write_text("{torn", encoding="utf-8")
    press(tc)
    assert len(cached(vault)["web"]) == 1


# -- keep in Inbox (point 13) --------------------------------------------------

def keep(tc, **over):
    return tc.post("/note/check/keep", json={"path": "Drafts/memo", "url": "https://www.example.org/rights",
                                             "quote": "the end of\n 30 days", "claim": CLAIM, **over})


def test_keep_writes_one_cited_note_in_the_inbox_with_no_web_turn_behind_it(desk):
    from silica.sources import web_research as wr

    tc, _, vault, *_ = desk
    wr._LAST_WEB_TURN = None
    body = keep(tc).json()
    assert body["ok"] is True and body["path"].startswith("Inbox/")
    text = (vault / body["path"]).read_text(encoding="utf-8")
    assert "> the end of 30 days" in text and "[[memo]]" in text and CLAIM in text
    assert "## Sources\n1. www.example.org " in text and "https://www.example.org/rights" in text
    assert "source: web\n" in text and f"fetched: {date.today().isoformat()}" in text


def test_a_long_claim_still_fits_a_files_name(desk):
    tc, _, vault, *_ = desk
    claim = "è una frase piuttosto lunga " * 21
    body = keep(tc, claim=claim).json()
    assert len(body["path"].rsplit("/", 1)[-1].encode()) <= 200 and (vault / body["path"]).is_file()
    assert claim.strip() in (vault / body["path"]).read_text(encoding="utf-8")


def test_keep_goes_nowhere_but_the_inbox_and_a_second_is_a_second_note(desk):
    tc, _, vault, *_ = desk
    first, second = keep(tc).json()["path"], keep(tc).json()["path"]
    assert first != second and {p.rsplit("/", 1)[0] for p in (first, second)} == {"Inbox"}


@pytest.mark.parametrize("over", [
    {"url": "javascript:alert(1)"}, {"url": ""}, {"quote": ""}, {"claim": "  "}, {"url": "file:///etc/passwd"},
])
def test_keep_refuses_what_is_not_a_page_its_words_and_a_claim(desk, over):
    tc, *_ = desk
    assert keep(tc, **over).status_code == 400


def test_keep_needs_a_note_a_free_turn_and_a_same_origin(desk):
    from silica.ui.web.session import SESSION
    tc, server, vault, *_ = desk
    assert keep(tc, path="Nowhere/Nothing").status_code == 404
    assert SESSION.claim()
    assert keep(tc).status_code == 409
    SESSION.release()
    assert tc.post("/note/check/keep", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert not (vault / "Inbox").exists()


def test_a_check_that_fails_says_a_sentence_and_never_the_failures_text(desk):
    """B3: the error frame's `say` is a toast, and its text was the exception's, which
    hosted can be a provider's carrying a fragment of the platform's key."""
    tc, _, _, _, mp = desk

    def leaky(*_a, **_k):
        raise RuntimeError("OpenrouterException - bad request for sk-or-v1-0123456789abcdef")

    mp.setattr("silica.session.check.run", leaky)
    out = [json.loads(x) for x in tc.post("/note/check", json={"path": "Drafts/memo"}).text.splitlines()]
    assert out == [{"event": "error", "say": "the check failed"}]
