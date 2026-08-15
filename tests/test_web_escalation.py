# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`/web` consent turn, trace-built citations, `/keep`, thin-coverage hint.

No network and no LLM: `call_llm` and `run_agent` are faked. What is pinned here
is the escalation contract — the web turn sees only the three web tools, the
citations come from the tool trace rather than from the model's prose, and the
hint fires on a mechanical hard miss only.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from silica.agent.events import ToolCompleteEvent
from silica.agent.recall_watch import RecallWatch
from silica.cli import _expand_web_turn
from silica.config import CONFIG
from silica.sources import web_research as wr


# --- 1. the toolset of a /web turn -------------------------------------------

def test_web_turn_exposes_exactly_the_three_web_tools():
    """The consent turn is the only chat path to web_search/web_fetch/remember,
    and it must not carry the vault toolset along with them."""
    from silica.agent.constraints import web_turn_constraints
    from silica.agent.loop import run_agent

    captured = {}

    def fake_call_llm(model, messages, tools=None, cancel=None):
        captured["tools"] = tools
        return SimpleNamespace(
            assistant_message={"role": "assistant", "content": "ok"},
            tool_calls=[], text="ok", reasoning=None, usage={},
        )

    with patch("silica.agent.loop.call_llm", fake_call_llm):
        run_agent(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            constraints=web_turn_constraints(),
        )

    names = {t["function"]["name"] for t in (captured["tools"] or [])}
    assert names == {"web_search", "web_fetch", "remember", "find_in_page"}


def test_web_turn_iteration_cap_matches_web_research():
    from silica.agent.constraints import web_turn_constraints

    assert web_turn_constraints().max_iterations == wr._DEFAULT_MAX_SEARCHES


# --- 2. dispatch ------------------------------------------------------------

def test_keywords_land_in_the_instruction():
    question, instruction = _expand_web_turn("/web graph rewiring", [])
    assert question == "graph rewiring"
    assert "graph rewiring" in instruction
    assert "web_search" in instruction and "web_fetch" in instruction


def test_bare_web_escalates_the_previous_user_question():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "what is graph rewiring?"},
        {"role": "assistant", "content": "I have nothing on that."},
    ]
    question, instruction = _expand_web_turn("/web", messages)
    assert question == "what is graph rewiring?"
    assert "what is graph rewiring?" in instruction


def test_bare_web_ignores_a_cli_expanded_directive():
    """`origin: cli` marks a harness instruction, not a human question — bare /web
    must not escalate the expansion of an earlier /summarize."""
    messages = [
        {"role": "user", "content": "what is graph rewiring?"},
        {"role": "user", "content": "Read Concepts/RAG.md and...", "origin": "cli"},
    ]
    question, _ = _expand_web_turn("/web", messages)
    assert question == "what is graph rewiring?"


def test_bare_web_on_a_fresh_session_is_a_usage_error():
    with pytest.raises(ValueError, match="Usage: /web"):
        _expand_web_turn("/web", [{"role": "system", "content": "sys"}])


def test_web_search_is_not_matched_as_a_web_turn():
    assert _expand_web_turn("/web-search 'x'", []) is None
    assert _expand_web_turn("what about /web", []) is None


# --- 3./4. attribution: built from the trace, appended to answer AND history --

def _turn_with_trace(question="q", results=None):
    """A WebTurn fed the events the real loop emits, ready to attribute."""
    turn = wr.WebTurn(question)
    for i, payload in enumerate(results or []):
        turn(ToolCompleteEvent(
            name="web_search", args={"query": "q"}, call_id=f"c{i}",
            result=payload if isinstance(payload, str) else json.dumps(payload),
            duration_s=0.0, iteration=i + 1,
        ))
    return turn


_HITS = [{"title": "Rewiring", "url": "https://a.test/rw", "content": "c"}]


def test_sources_come_from_the_trace_not_from_the_prose():
    turn = _turn_with_trace(results=[_HITS])
    messages = [{"role": "assistant", "content": "Answer."}]

    out = turn.attribute("Answer.", messages)

    assert "## Sources (web)" in out
    assert "1. Rewiring — https://a.test/rw" in out


def test_a_url_the_model_invented_never_reaches_the_sources_block():
    turn = _turn_with_trace(results=[_HITS])
    prose = "Answer, see https://invented.test/paper for details."

    out = turn.attribute(prose, [])

    block = out.split("## Sources (web)", 1)[1]
    assert "invented.test" not in block
    assert "a.test/rw" in block


def test_the_models_own_sources_section_is_kept_and_ours_appended_after_it():
    turn = _turn_with_trace(results=[_HITS])

    out = turn.attribute("Answer.\n\n## Sources\n1. whatever the model wrote", [])

    assert out.index("## Sources\n") < out.index("## Sources (web)")


def test_an_empty_trace_says_so_rather_than_citing_nothing():
    out = _turn_with_trace().attribute("Answer.", [])
    assert "(no sources captured)" in out


def test_a_fallback_lane_is_named_under_the_sources_block(monkeypatch):
    """/web is where a challenged primary lane is felt first, so the answer the
    user reads carries the same lane line the kept note does."""
    turn = _turn_with_trace(results=[_HITS])
    monkeypatch.setattr(wr, "_LANES", ["mojeek", "wikipedia", "wikipedia"])

    out = turn.attribute("Answer.", [])

    block = out.split("## Sources (web)", 1)[1]
    assert "Search lanes: mojeek 1, wikipedia 2." in block


def test_a_healthy_turn_carries_no_lane_line(monkeypatch):
    turn = _turn_with_trace(results=[_HITS])
    monkeypatch.setattr(wr, "_LANES", ["duckduckgo", "duckduckgo"])

    assert "Search lanes" not in turn.attribute("Answer.", [])


def test_the_block_lands_in_the_history_too():
    """History must carry what the user saw: the next turn reads the citations."""
    turn = _turn_with_trace(results=[_HITS])
    messages = [
        {"role": "user", "content": "instruction"},
        {"role": "assistant", "content": "Answer."},
    ]

    out = turn.attribute("Answer.", messages)

    assert messages[-1]["content"] == out


def test_a_cancelled_turn_is_neither_cited_nor_kept(monkeypatch):
    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)
    turn = _turn_with_trace(results=[_HITS])

    out = turn.attribute("(silica: cancelled)", [])

    assert out == "(silica: cancelled)"
    assert wr._LAST_WEB_TURN is None


# --- 5. /keep ---------------------------------------------------------------

@pytest.fixture
def kept(monkeypatch):
    """A /web turn waiting in the slot, cleared again after the test."""
    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)
    turn = _turn_with_trace(question="graph rewiring", results=[_HITS])
    turn.attribute("Rewiring is a local edge swap.", [])
    yield
    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)


def test_keep_writes_a_cited_inbox_note_and_its_leaf(tmp_vault, kept):
    note_rel = wr.keep_last()

    vault = Path(CONFIG.vault_path)
    body = (vault / note_rel).read_text(encoding="utf-8")
    assert note_rel == "Inbox/graph rewiring.md"
    assert "source: web" in body
    assert f"fetched: {datetime.date.today().isoformat()}" in body
    assert "https://a.test/rw" in body
    assert body.count("## Sources") == 1, "the stored prose must not carry a block already"

    from silica.kernel.recall.paths import SOURCES_DIR

    leaf = vault / SOURCES_DIR / "graph rewiring.md"
    assert leaf.is_file() and "a.test/rw" in leaf.read_text(encoding="utf-8")


def test_keep_clears_the_slot_so_a_second_one_is_a_clean_error(tmp_vault, kept):
    wr.keep_last()
    with pytest.raises(ValueError, match="nothing to keep"):
        wr.keep_last()


def test_keep_without_a_web_turn_is_a_clean_error(tmp_vault, monkeypatch):
    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)
    with pytest.raises(ValueError, match="nothing to keep: run /web first"):
        wr.keep_last()


def test_a_second_web_turn_overwrites_the_slot(tmp_vault, kept):
    _turn_with_trace(
        question="something else",
        results=[[{"title": "Other", "url": "https://b.test/x", "content": "c"}]],
    ).attribute("Other answer.", [])

    note_rel = wr.keep_last()
    assert note_rel == "Inbox/something else.md"


# --- 6. the thin-coverage hint ---------------------------------------------

def _complete(name: str, payload) -> ToolCompleteEvent:
    return ToolCompleteEvent(
        name=name, args={}, call_id="c1",
        result=payload if isinstance(payload, str) else json.dumps(payload),
        duration_s=0.0, iteration=1,
    )


@pytest.mark.parametrize("events, thin", [
    # every recall-family call missed → armed
    ([("silica_recall", {"notes": [], "facts": 0})], True),
    ([("silica_semantic_search", {"results": []})], True),
    ([("silica_search_context", {"hits": [], "notes_matched": 0})], True),
    ([("silica_recall", {"notes": []}), ("silica_semantic_search", {"results": []})], True),
    # any hit anywhere → silent
    ([("silica_recall", {"notes": ["a.md"]})], False),
    ([("silica_recall", {"notes": []}), ("silica_semantic_search", {"results": [{"n": 1}]})], False),
    # no recall call at all → silent
    ([], False),
    ([("silica_read_note", "some prose")], False),
    # a degraded leg is not thin coverage: escalating would mask the real problem
    ([("silica_semantic_search", {"error": "embedder offline"})], False),
    # silica_related takes an existing note: no neighbors != no coverage
    ([("silica_related", {"results": []})], False),
    # title match is too weak a signal to escalate on
    ([("silica_search", {"paths": [], "matched": 0})], False),
])
def test_hint_truth_table(events, thin):
    watch = RecallWatch()
    for name, payload in events:
        watch(_complete(name, payload))
    assert watch.thin is thin


def test_recall_watch_forwards_every_event_untouched():
    seen = []
    watch = RecallWatch(seen.append)
    ev = _complete("silica_recall", {"notes": []})
    watch(ev)
    assert seen == [ev]


# --- 8. the ADR-0009 boundary still holds -----------------------------------

def test_the_default_chat_toolset_still_excludes_the_web_tools():
    """/web is the only chat path to them: the toolset must not have widened."""
    from silica.agent.constraints import chat_tools

    assert "web_search" not in chat_tools()
    assert "web_fetch" not in chat_tools()


def test_the_default_chat_toolset_excludes_remember_too():
    from silica.agent.constraints import chat_tools

    assert "remember" not in chat_tools()


# --- 9. the evidence bank on the /web path (spec §3.2/§3.5) ------------------

_RW_PAGE = "Source: https://a.test/rw\n\nRewiring\n\nEdges move locally."


def _turn_with_banked_quote(question="q"):
    """A WebTurn that fetched a page and banked one quote from it."""
    turn = _turn_with_trace(question=question, results=[_HITS])
    turn(ToolCompleteEvent(
        name="web_fetch", args={"url": "https://a.test/rw"}, call_id="f1",
        result=_RW_PAGE, duration_s=0.0, iteration=2,
    ))
    wr.remember("https://a.test/rw", "Edges move locally.", "definition")
    return turn


def test_web_answer_binds_bank_markers_to_the_sources_block():
    """[Qk] in the chat answer becomes [n] pointing at the block below it, and
    a marker with no banked quote is stripped and audited — the same guarantee
    the batch note gets, on the answer the user actually reads."""
    turn = _turn_with_banked_quote()

    out = turn.attribute("Rewiring works [Q1], and fails [Q9].", [])

    assert "Rewiring works [1], and fails." in out
    assert "[Q1]" not in out and "[Q9]" not in out
    assert "1. Rewiring — https://a.test/rw" in out
    assert "Citation audit: 1 marker(s)" in out


def test_keep_carries_the_bank_into_the_leaf(tmp_vault, monkeypatch):
    """The stash snapshots the bank at attribute time: a later turn resetting
    the module state must not strip the kept note's leaf of its quotes."""
    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)
    turn = _turn_with_banked_quote(question="rewiring")
    turn.attribute("Claim [Q1].", [])
    wr._reset_turn()  # a later turn moved on; the stash must not care

    note_rel = wr.keep_last()

    from silica.kernel.recall.paths import SOURCES_DIR

    leaf = (
        Path(CONFIG.vault_path) / SOURCES_DIR / Path(note_rel).name
    ).read_text(encoding="utf-8")
    assert "## Evidence bank" in leaf
    assert "> Edges move locally." in leaf


# --- 10. the in-chat door (silica_web_answer) --------------------------------

def test_the_chat_toolset_carries_the_door_not_the_tools_behind_it():
    """The chat model can decide to go to the web, but only through the
    sub-loop tool: the four web tools stay out of a turn that holds the write
    tools, which is the whole point of ADR-0009 here."""
    from silica.agent.constraints import chat_tools

    assert "silica_web_answer" in chat_tools()


def test_web_answer_runs_the_consented_sub_loop_and_cites_its_trace(monkeypatch):
    """One question in, cited prose out — and the sub-loop it spends is the
    same constrained turn `/web` spends, not the caller's toolset."""
    from silica.agent.constraints import web_turn_constraints

    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)
    seen = {}

    def fake_run_agent(messages, model=None, tool_progress_callback=None,
                       constraints=None, cancel_token=None):
        seen["tools"] = constraints.tools
        seen["question"] = messages[-1]["content"]
        tool_progress_callback(ToolCompleteEvent(
            name="web_search", args={"query": "q"}, call_id="s1",
            result=json.dumps(_HITS), duration_s=0.0, iteration=1,
        ))
        messages.append({"role": "assistant", "content": "An answer."})
        return "An answer."

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)

    out = wr.silica_web_answer("what is rewiring")

    assert seen["tools"] == web_turn_constraints().tools
    assert "what is rewiring" in seen["question"]
    assert "1. Rewiring — https://a.test/rw" in out
    # and the answer is keepable, exactly like a /web turn's
    assert wr._LAST_WEB_TURN is not None


def test_the_sub_loops_searches_reach_the_outer_ui(monkeypatch):
    """The chat used to show one opaque `web answer` row for the whole lane.

    Driven through Tool.run rather than the function, because the injection seam
    is half of what is pinned here: the loop hands the frontend callback to any
    tool declaring `progress`, and only searches and fetches come back out —
    banked quotes and the sub-loop's own stream deltas would render into the
    chat bubble as if the chat model had written them.

    The ids come back namespaced: they are not unique across loops (the provider
    mints them per request), and the web UI indexes its rows by id, so a bare
    inner id can close an outer row.
    """
    from silica.agent.events import LLMStreamEvent, ToolStartEvent
    from silica.tools import TOOLS

    monkeypatch.setattr(wr, "_LAST_WEB_TURN", None)

    def fake_run_agent(messages, model=None, tool_progress_callback=None,
                       constraints=None, cancel_token=None):
        for ev in (
            ToolStartEvent(name="web_search", args={"query": "q"}, call_id="c1", iteration=1),
            ToolCompleteEvent(name="web_search", args={"query": "q"}, call_id="c1",
                              result=json.dumps(_HITS), duration_s=0.0, iteration=1),
            ToolStartEvent(name="remember", args={"url": "https://a.test/rw"},
                           call_id="c2", iteration=2),
            LLMStreamEvent(chunk_type="text", content="half an answer", iteration=2),
            ToolStartEvent(name="web_fetch", args={"url": "https://a.test/rw"},
                           call_id="c3", iteration=3),
            ToolCompleteEvent(name="web_fetch", args={"url": "https://a.test/rw"},
                              call_id="c3", result="Source: https://a.test/rw\n\nbody",
                              duration_s=0.0, iteration=3),
        ):
            tool_progress_callback(ev)
        messages.append({"role": "assistant", "content": "An answer."})
        return "An answer."

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)

    seen = []
    TOOLS["silica_web_answer"].run(_progress=seen.append, question="what is rewiring")

    assert [(e.name, e.call_id) for e in seen] == [
        ("web_search", "web:c1"),
        ("web_search", "web:c1"),
        ("web_fetch", "web:c3"),
        ("web_fetch", "web:c3"),
    ]


def test_a_relayed_answer_that_dropped_the_block_gets_it_back():
    """The chat model stands between the web lane and the user, and a relay
    paraphrases: the citations must survive it."""
    turn = _turn_with_trace(question="rewiring", results=[_HITS])
    turn.attribute("Answer.", [])  # the door stashed the turn

    messages = [{"role": "assistant", "content": "Fonte: un sito a caso."}]
    out = wr.relay_sources("Fonte: un sito a caso.", messages)

    assert "1. Rewiring — https://a.test/rw" in out
    assert messages[-1]["content"] == out  # history carries what the user saw


def test_a_faithful_relay_is_not_cited_twice():
    turn = _turn_with_trace(question="rewiring", results=[_HITS])
    relayed = turn.attribute("Answer.", [])

    assert wr.relay_sources(relayed, []) == relayed


def test_the_watch_flags_the_turn_that_used_the_door():
    """The UI only reapplies the block on a turn that actually went to the web."""
    watch = RecallWatch()
    assert watch.web_answer is False

    watch(ToolCompleteEvent(
        name="silica_web_answer", args={"question": "q"}, call_id="w1",
        result="prose", duration_s=0.0, iteration=1,
    ))

    assert watch.web_answer is True
