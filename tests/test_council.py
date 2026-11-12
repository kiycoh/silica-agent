# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0170: one search, several members on their own models, a failing one
named and skipped, a cited synthesis with every answer folded under it."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from silica.agent import turn_notes
from silica.commands import council as council_mod
from silica.config import CONFIG

MODEL = "openrouter/deepseek/deepseek-v4-flash-0731"


@pytest.fixture
def council(tmp_vault, monkeypatch):
    """Three seats of one model, a recall that counts its calls, and a fake
    call_llm that answers per model and records how it was called."""
    monkeypatch.setattr(CONFIG, "council_models", ",".join([MODEL] * 3))
    monkeypatch.setattr(CONFIG, "model", "openrouter/chat-model")
    recalls: list[str] = []

    def recall(query, **kw):
        recalls.append(query)
        return {"query": query, "context": "[[Econ/Inflation.md]] money supply grows", "notes": ["Econ/Inflation.md"]}

    calls: list[dict] = []
    lock = threading.Lock()

    def call_llm(model, messages, **kw):
        with lock:
            calls.append({"model": model, "messages": messages, **kw})
        if model == CONFIG.model:
            return SimpleNamespace(text="## Agree\nMoney supply [[Econ/Inflation.md]].",
                                   usage={"prompt_tokens": 900, "completion_tokens": 100})
        return SimpleNamespace(text="Money supply grows [[Econ/Inflation.md]].",
                               usage={"prompt_tokens": 1000, "completion_tokens": 234})

    monkeypatch.setattr("silica.tools.graph.silica_recall", recall)
    monkeypatch.setattr("silica.model.llm.call_llm", call_llm)
    return SimpleNamespace(recalls=recalls, calls=calls)


def _run(line: str):
    from silica.commands import dispatch

    out = dispatch(line, [])
    assert out is not None
    return out


def test_one_search_the_same_passages_to_every_member_and_one_synthesis(council):
    out = _run('/council "what causes inflation?"')
    assert out.error is None and council.recalls == ["what causes inflation?"]
    members = [c for c in council.calls if c["model"] == MODEL]
    assert len(members) == 3 and all(c["fallback"] is False for c in members)
    assert all(c["messages"] == members[0]["messages"] for c in members)
    assert "money supply grows" in members[0]["messages"][1]["content"]
    synthesis = [c for c in council.calls if c["model"] == CONFIG.model]
    assert len(synthesis) == 1 and "Member A · deepseek-v4-flash-0731" in synthesis[0]["messages"][1]["content"]
    assert out.answer.startswith("## Agree")
    assert out.answer.count("<details>") == 3 and "Member C · deepseek-v4-flash-0731" in out.answer
    assert {"kind": "council", "text": "member A · deepseek-v4-flash-0731: 1 call, 1,234 tokens"} in out.notes
    assert out.notes[-1] == {"kind": "council", "text": "synthesis: 1 call, 1,000 tokens"}


def test_a_member_that_fails_or_runs_out_of_time_is_named_and_skipped(council, monkeypatch):
    from silica.model import llm

    monkeypatch.setattr(CONFIG, "council_models", f"{MODEL},bad/model,slow/model")
    monkeypatch.setattr(council_mod, "MEMBER_TIMEOUT_S", 0.2)
    fake = llm.call_llm  # the fixture's

    def call_llm(model, messages, **kw):
        if model == "bad/model":
            raise ConnectionError("refused")
        if model == "slow/model":
            time.sleep(1)
        return fake(model, messages, **kw)

    monkeypatch.setattr("silica.model.llm.call_llm", call_llm)
    out = _run('/council "what causes inflation?"')
    texts = [n["text"] for n in out.notes]
    assert "member B · model: skipped, ConnectionError" in texts
    assert "member C · model: skipped, no answer in 0 s" in texts
    assert out.answer.count("<details>") == 1


@pytest.mark.parametrize("models, said", [
    ("", "SILICA_COUNCIL_MODELS"), (MODEL, "2 to 5 members"), (",".join([MODEL] * 6), "names 6")])
def test_no_members_or_a_wrong_count_says_how_to_set_them(council, monkeypatch, models, said):
    monkeypatch.setattr(CONFIG, "council_models", models)
    assert said in _run('/council "q"').error
    assert council.calls == [] and council.recalls == []


def test_a_question_with_an_apostrophe_is_asked_quoted_or_not(council):
    """The composer's + prefills `/council ` and the person types a question:
    "What's" was refused as unbalanced quotes, with advice about paths."""
    for line in ("/council What's the role of light?", "/council \"What's the role of light?\""):
        out = _run(line)
        assert out.error is None, line
    assert council.recalls == ["What's the role of light?"] * 2


def test_the_server_and_the_demo_refuse_it(council, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert "not on the server" in _run('/council "q"').error
    assert council.calls == []


def test_a_member_is_never_answered_by_a_fallback(monkeypatch):
    from silica.model import llm

    tried: list[str] = []

    def down(used, *a, **kw):
        tried.append(used)
        raise llm._litellm().APIConnectionError(message="down", llm_provider="openrouter", model=used)

    monkeypatch.setattr(CONFIG, "fallback_models", "openrouter/other")
    monkeypatch.setattr(llm, "_call_llm", down)
    with pytest.raises(Exception):
        llm.call_llm(MODEL, [{"role": "user", "content": "q"}], fallback=False)
    assert tried == [MODEL]


def test_the_folded_answers_draw_as_their_own_markdown(tmp_vault):
    from silica.ui.web import render

    html = render.answer_html(council_mod._drawn("## Agree\nyes", [
        council_mod._Heard("A", MODEL, text="**bold** claim")]), [])
    assert "<details>" in html and "<summary>Member A · deepseek-v4-flash-0731</summary>" in html
    assert "<strong>bold</strong>" in html


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def test_the_meter_rides_the_answer_and_stays_on_it(council):
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    SESSION.reset()
    try:
        r = TestClient(server.app).post("/chat", json={"text": '/council "what causes inflation?"'})
        done = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")][-1]
        assert done["type"] == "done" and "<details>" in done["html"]
        assert sum(n["kind"] == "council" for n in done["notes"]) == 4
        assert SESSION.chat.messages[-1][turn_notes.KEY] == done["notes"]
    finally:
        SESSION.reset()


def test_a_recall_with_no_index_falls_back_to_the_body_search(council, monkeypatch):
    monkeypatch.setattr("silica.tools.graph.silica_recall", lambda q, **kw: {"query": q, "context": "", "notes": []})
    monkeypatch.setattr("silica.tools.atomic.silica_search_context", lambda q, **kw: {"hits": [
        {"path": "Science/Galileo.md", "section": "Trial", "line": 9, "snippet": "tried in 1633"}]})
    _run('/council "why was galileo tried?"')
    member = next(c for c in council.calls if c["model"] == MODEL)
    assert "[[Science/Galileo.md]], Trial:\ntried in 1633" in member["messages"][1]["content"]
