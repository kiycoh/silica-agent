# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chat's web door spends as few model calls as the question needs.

Hosted t06, 2026-10-21: "chi è deborah ann woll" took 96 s for 2 s of web I/O,
six sequential model calls and a floor of sleeps. These pin the changes that
came out of it: the first search and its top pages are opened before the
model's first call (_seed), the lane runs without `remember`, a `final` web
answer ends the turn when nothing else of the turn found anything, and the
Sources block names the pages opened.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import silica.cli  # noqa: F401  registers every chat tool, silica_web_answer included
from silica.agent.constraints import AgentConstraints, web_answer_constraints
from silica.agent.events import ToolCompleteEvent
from silica.agent.loop import run_agent
from silica.agent.recall_watch import found_nothing
from silica.sources import web_research as wr
from silica.tools import TOOLS

_HITS = [{"title": "Woll - Wikipedia", "url": "https://it.w.test/Woll", "content": "attrice"},
         {"title": "Woll - en", "url": "https://en.w.test/Woll", "content": "actress"},
         {"title": "Unread", "url": "https://u.test", "content": "x"}]


@pytest.fixture
def lanes(monkeypatch):
    """The two web tools the seed calls, offline. Returns the URLs fetched."""
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        if "en." in url:
            raise ValueError("403 Forbidden")
        return f"Source: {url}\n\nDeborah Ann Woll è un'attrice."

    monkeypatch.setattr(wr, "web_search", lambda query: json.dumps(_HITS))
    monkeypatch.setattr(wr._web_fetch, "web_fetch", fetch)
    return fetched


def test_the_seed_searches_the_question_and_opens_the_top_pages(lanes):
    turn = wr.WebTurn("chi è deborah ann woll")

    seeded = wr._seed("chi è deborah ann woll", turn)

    assert sorted(lanes) == ["https://en.w.test/Woll", "https://it.w.test/Woll"]
    call, *results = seeded
    # The page that would not open is left out, so the model fetches it itself
    # if it wants it; every call left in the history has its result.
    assert [tc["function"]["name"] for tc in call["tool_calls"]] == ["web_search", "web_fetch"]
    assert [r["tool_call_id"] for r in results] == [tc["id"] for tc in call["tool_calls"]]
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {
        "query": "chi è deborah ann woll"}
    # Through the trace, like the model's own calls: the opened page is the source.
    out = turn.attribute("Risposta.", [])
    assert "Woll - Wikipedia — https://it.w.test/Woll" in out
    assert "u.test" not in out
    assert "https://it.w.test/Woll" in wr._PAGES   # find_in_page can mine it


def test_a_seed_with_every_lane_down_leaves_the_model_to_search(monkeypatch):
    def down(query):
        raise ValueError("every lane refused")

    monkeypatch.setattr(wr, "web_search", down)
    assert wr._seed("q", wr.WebTurn("q")) == []


def test_the_door_runs_its_own_lane_on_top_of_the_seed(monkeypatch, lanes):
    seen = {}

    def fake_run_agent(messages, model=None, tool_progress_callback=None,
                       constraints=None, cancel_token=None):
        seen["constraints"] = constraints
        seen["roles"] = [m["role"] for m in messages]
        messages.append({"role": "assistant", "content": "Risposta."})
        return "Risposta."

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)

    out = wr.silica_web_answer("chi è deborah ann woll")

    c = seen["constraints"]
    assert c == web_answer_constraints()
    assert "remember" not in c.tools and c.max_iterations == 8
    assert seen["roles"] == ["system", "user", "assistant", "tool", "tool"]
    assert "## Sources (web)" in out


# --- the loop: a `final` web answer is the reply ---------------------------------

def _resp(calls=(), text=""):
    tool_calls = [SimpleNamespace(id=cid, name=name, args=args) for cid, name, args in calls]
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = [{"id": t.id, "type": "function",
                              "function": {"name": t.name, "arguments": json.dumps(t.args)}}
                             for t in tool_calls]
    return SimpleNamespace(assistant_message=msg, tool_calls=tool_calls, text=text,
                           reasoning=None, usage={})


def _turn(monkeypatch, *, final, titles, then=None):
    """One chat turn whose first call runs a title search and the web door in
    the same block. Returns (answer, model calls made, messages)."""
    monkeypatch.setattr(TOOLS["silica_search"], "fn", lambda query: {
        "paths": titles, "matched": len(titles)})
    monkeypatch.setattr(TOOLS["silica_web_answer"], "fn",
                        lambda question, final=False: "Dal web: è un'attrice.\n\n## Sources (web)\n1. x")
    replies = iter([
        _resp([("c1", "silica_search", {"query": "woll"}),
               ("c2", "silica_web_answer", {"question": "chi è Woll", "final": final})]),
        then or _resp(text="Riscritta."),
    ])
    calls = []

    def fake_call_llm(model, messages, **kw):
        calls.append(kw)
        return next(replies)

    messages = [{"role": "user", "content": "chi è deborah ann woll"}]
    with patch("silica.agent.loop.call_llm", fake_call_llm):
        answer = run_agent(messages, model="test", constraints=AgentConstraints(
            tools=("silica_search", "silica_web_answer"), interactive=True))
    return answer, calls, messages


def test_a_final_web_answer_beside_an_empty_vault_search_ends_the_turn(monkeypatch):
    answer, calls, messages = _turn(monkeypatch, final=True, titles=[])

    assert len(calls) == 1
    assert answer.startswith("Dal web: è un'attrice.")
    assert messages[-1] == {"role": "assistant", "content": answer}
    # The history carries the answer once, not in the tool message as well.
    door = next(m for m in messages if m.get("tool_call_id") == "c2")
    assert "attrice" not in door["content"]


def test_a_vault_search_that_found_a_note_hands_the_answer_back(monkeypatch):
    answer, calls, _ = _turn(monkeypatch, final=True, titles=["Woll.md"])
    assert len(calls) == 2 and answer == "Riscritta."


def test_without_final_the_chat_model_still_writes_the_reply(monkeypatch):
    answer, calls, _ = _turn(monkeypatch, final=False, titles=[])
    assert len(calls) == 2 and answer == "Riscritta."


def test_a_turn_that_went_to_the_web_does_not_point_the_user_at_web():
    from silica.agent.recall_watch import RecallWatch

    watch = RecallWatch()
    watch(ToolCompleteEvent(name="silica_recall", args={"query": "woll"}, call_id="r1",
                            result=json.dumps({"notes": []}), duration_s=0.0, iteration=1))
    assert watch.thin and watch.notes()      # a miss alone still suggests /web
    watch(ToolCompleteEvent(name="silica_web_answer", args={"question": "q"}, call_id="w1",
                            result="Dal web.", duration_s=0.0, iteration=1))
    assert watch.notes() == []


@pytest.mark.parametrize("name, result, nothing", [
    ("silica_search", {"paths": [], "matched": 0}, True),
    ("silica_search", {"paths": ["a.md"], "matched": 1}, False),
    ("silica_recall", {"notes": []}, True),
    ("silica_recall", {"notes": [{"path": "a.md"}]}, False),
    ("silica_recall", {"error": "index missing"}, False),
    ("silica_read_note", {"content": ""}, False),
])
def test_found_nothing_is_an_empty_vault_search_and_only_that(name, result, nothing):
    assert found_nothing(name, json.dumps(result)) is nothing


def test_the_door_still_reports_its_searches_to_the_outer_ui(monkeypatch, lanes):
    """The seed's calls draw the same live rows the model's own calls do."""
    monkeypatch.setattr(wr, "run_agent", lambda messages, **kw: "ok")
    seen = []
    TOOLS["silica_web_answer"].run(_progress=seen.append, question="q")
    done = [(e.name, e.call_id) for e in seen if isinstance(e, ToolCompleteEvent)]
    assert ("web_search", "web:seed-search") in done


def test_the_lane_is_told_the_users_own_words(monkeypatch, lanes):
    """The chat model rewrote "chi è deborah ann woll" into English twice on
    2026-10-21; a `final` answer must still reach the user in Italian."""
    from silica.agent import read_scope

    seen = []
    monkeypatch.setattr(wr, "run_agent",
                        lambda messages, **kw: seen.append(messages[1]["content"]) or "ok")
    token = read_scope.REQUESTS.set(("prima domanda", "chi è deborah ann woll"))
    try:
        wr.silica_web_answer("Who is Deborah Ann Woll?")
        read_scope.REQUESTS.set(("x" * (wr._ASKED_CHARS + 1),))
        wr.silica_web_answer("Who is Deborah Ann Woll?")
    finally:
        read_scope.REQUESTS.reset(token)
    assert "«chi è deborah ann woll»" in seen[0] and "prima domanda" not in seen[0]
    assert "«" not in seen[1]
