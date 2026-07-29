"""web_search tool + web_research orchestrator (ADR-0015 staged acquisition).

No real network (httpx.post is monkeypatched) and no real LLM (run_agent is
monkeypatched). Asserts: Tavily request shape, compact result mapping, missing
key error, sensitivity, and (later tasks) the inbox findings note.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.sources import web_research as wr
from silica.tools import TOOLS


# --- web_search tool --------------------------------------------------------

def test_web_search_registered_and_sensitive():
    assert "web_search" in TOOLS
    assert TOOLS["web_search"].sensitive is True


def test_web_search_missing_key_raises(monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    with pytest.raises(ValueError, match="TAVILY"):
        wr.web_search("anything")


def test_web_search_posts_and_returns_compact_results(monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k-123")
    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return self

        def json(self):
            return {
                "results": [
                    {"title": "T1", "url": "https://a.test", "content": "c1", "score": 0.9},
                    {"title": "T2", "url": "https://b.test", "content": "c2"},
                ]
            }

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["body"] = json
        seen["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(wr.httpx, "post", fake_post)

    out = wr.web_search("graph theory")
    items = json.loads(out)

    assert seen["url"] == wr._TAVILY_URL
    assert seen["body"]["api_key"] == "k-123"
    assert seen["body"]["query"] == "graph theory"
    assert seen["body"]["max_results"] == wr._MAX_RESULTS
    assert items == [
        {"title": "T1", "url": "https://a.test", "content": "c1"},
        {"title": "T2", "url": "https://b.test", "content": "c2"},
    ]


# --- web_research orchestrator ----------------------------------------------

def _patch_run_agent(monkeypatch, body, tool_results=None):
    """Fake run_agent: replay a web_search trace the way the real loop does —
    a ToolCompleteEvent per call *and* the same payload appended to `messages`
    — then return the body."""
    from silica.agent.events import ToolCompleteEvent

    captured = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, constraints=None, **kw):
        captured["constraints"] = constraints
        captured["model"] = model
        for i, items in enumerate(tool_results or []):
            call_id = f"c{i}"
            payload = json.dumps(items)
            if tool_progress_callback is not None:
                tool_progress_callback(ToolCompleteEvent(
                    name="web_search", args={"query": "q"}, call_id=call_id,
                    result=payload, duration_s=0.0, iteration=i + 1,
                ))
            messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": payload}
            )
        return body

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)
    return captured


def test_web_research_writes_inbox_note_with_deterministic_frontmatter(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(
        monkeypatch,
        body="Findings about graph theory [1][2].",
        tool_results=[[
            {"title": "T1", "url": "https://a.test", "content": "c1"},
            {"title": "T2", "url": "https://b.test", "content": "c2"},
        ]],
    )

    note_rel = wr.web_research("graph theory")

    assert note_rel.startswith(f"{CONFIG.inbox_dir}/")
    body = (Path(CONFIG.vault_path) / note_rel).read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    assert 'title: "graph theory"' in body
    assert "source: web-research" in body
    assert f"fetched: {today}" in body
    assert "tags: [inbox, web-research]" in body
    assert "Findings about graph theory" in body


def test_web_research_appends_sources_when_model_omits_them(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(
        monkeypatch,
        body="Findings with no sources section.",  # model forgot ## Sources
        tool_results=[[
            {"title": "T1", "url": "https://a.test", "content": "c1"},
        ]],
    )

    body = (Path(CONFIG.vault_path) / wr.web_research("x")).read_text(encoding="utf-8")
    assert body.count("## Sources") == 1
    assert "https://a.test" in body


def test_web_research_keeps_model_sources_section(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(
        monkeypatch,
        body="Findings [1].\n\n## Sources\n1. T1 — https://a.test",
        tool_results=[[{"title": "T1", "url": "https://a.test", "content": "c1"}]],
    )

    body = (Path(CONFIG.vault_path) / wr.web_research("x")).read_text(encoding="utf-8")
    assert body.count("## Sources") == 1  # not doubled


def test_web_research_constrains_loop_to_search_and_fetch(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    captured = _patch_run_agent(
        monkeypatch,
        body="Findings.",
        tool_results=[[{"title": "T1", "url": "https://a.test", "content": "c"}]],
    )

    wr.web_research("x", max_searches=7)

    assert captured["constraints"].tools == ("web_search", "web_fetch")
    assert captured["constraints"].max_iterations == 7


def test_web_research_prompt_tells_the_model_to_fetch():
    assert "web_fetch" in wr._RESEARCH_SYSTEM_PROMPT


def test_collect_sources_picks_up_a_fetched_url():
    """web_fetch returns prose, not JSON; its Source: line is the citation."""
    results = [
        json.dumps([{"title": "T1", "url": "https://a.test", "content": "c"}]),
        "Source: https://b.test/article\n\nBody text.",
    ]
    assert wr._collect_sources(results) == [
        ("https://a.test", "T1"),
        ("https://b.test/article", "https://b.test/article"),
    ]


def test_collect_sources_ignores_prose_without_a_source_line():
    assert wr._collect_sources(["just some text\nno header"]) == []


# --- compaction cannot reach the trace the leaf and the citations are built from


def _patch_run_agent_then_compact(monkeypatch, body, fetches):
    """run_agent double that behaves like the real loop on a long research run.

    It emits a ToolCompleteEvent per tool call (as silica/agent/loop.py does,
    before anything can rewrite the message), appends the same result to
    `messages`, and then lets the *real* compaction sweep run over that history.
    Past the recency floor the sweep replaces each fat web_fetch result with an
    elision stub in place, which is exactly what a caller reading `messages`
    after run_agent returns would find.
    """
    from silica.agent.compaction import COMPACT_FLOOR_TURNS, compact_read_history
    from silica.agent.events import ToolCompleteEvent
    from silica.tools import TOOLS

    def fake_run_agent(messages, model, tool_progress_callback=None, constraints=None, **kw):
        for i, (url, text) in enumerate(fetches):
            call_id = f"call-{i}"
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "web_fetch",
                                 "arguments": json.dumps({"url": url})},
                }],
            })
            if tool_progress_callback is not None:
                tool_progress_callback(ToolCompleteEvent(
                    name="web_fetch", args={"url": url}, call_id=call_id,
                    result=text, duration_s=0.1, iteration=i + 1,
                ))
            messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
        messages.append({"role": "assistant", "content": body})
        compact_read_history(
            messages, set(), prompt_tokens=10**9, budget=0,
            floor_turns=COMPACT_FLOOR_TURNS, tools=TOOLS,
        )
        return body

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)


_PAGES = [
    (f"https://p{i}.test/article", f"Source: https://p{i}.test/article\n\nPage {i} title\n\n"
     + f"body of page {i}. " * 40)
    for i in range(5)
]


def test_web_research_leaf_survives_context_compaction(tmp_vault, monkeypatch):
    """web_fetch is `collapse="lazy"` and ~7.5k tokens a call, so a handful of
    fetches trips run_agent's compaction sweep, which rewrites the old tool
    results in `messages` to elision stubs *in place*. A leaf built by reading
    `messages` after the loop returns is then a list of stubs, not the pages."""
    from silica.kernel.recall.paths import SOURCES_DIR

    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent_then_compact(monkeypatch, body="Findings.", fetches=_PAGES)

    note_rel = wr.web_research("deep topic")
    leaf = (Path(CONFIG.vault_path) / SOURCES_DIR / note_rel.rsplit("/", 1)[-1]
            ).read_text(encoding="utf-8")

    assert "result elided" not in leaf
    for i in range(len(_PAGES)):
        assert f"body of page {i}." in leaf


def test_web_research_citations_survive_context_compaction(tmp_vault, monkeypatch):
    """Same sweep, other casualty: the ADR-0015 ## Sources fallback is built
    from the same trace, so the elided fetches lose their URLs entirely."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent_then_compact(monkeypatch, body="Findings.", fetches=_PAGES)

    body = (Path(CONFIG.vault_path) / wr.web_research("deep topic")).read_text(
        encoding="utf-8")

    assert body.count("## Sources") == 1
    for url, _ in _PAGES:
        assert url in body


def test_web_research_still_forwards_progress_events_to_its_caller(tmp_vault, monkeypatch):
    """The recorder wraps the caller's callback; it must not swallow it."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent_then_compact(monkeypatch, body="Findings.", fetches=_PAGES[:1])

    seen = []
    wr.web_research("x", tool_progress_callback=seen.append)
    assert [e.call_id for e in seen] == ["call-0"]


def test_main_agent_default_toolset_excludes_web_fetch():
    from unittest.mock import patch
    from types import SimpleNamespace
    from silica.agent.loop import run_agent

    # "web_fetch" in TOOLS alone doesn't pin edit 3a: tests/test_web_fetch.py
    # imports the module at collection time too, so TOOLS would be populated
    # even without wr's own import. Assert the attribute wr._web_fetch itself
    # carries, which only holds if web_research.py did the import (edit 3a).
    assert wr._web_fetch.web_fetch.__name__ == "web_fetch"

    captured = {}

    def fake_call_llm(model, messages, tools=None, cancel=None):
        captured["tools"] = tools
        return SimpleNamespace(
            assistant_message={"role": "assistant", "content": "ok"},
            tool_calls=[], text="ok", reasoning=None, usage={},
        )

    with patch("silica.agent.loop.call_llm", fake_call_llm):
        run_agent(messages=[{"role": "user", "content": "hi"}], model="m")

    names = {t["function"]["name"] for t in (captured["tools"] or [])}
    assert "web_fetch" not in names


def test_web_research_no_findings_raises_and_writes_nothing(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(monkeypatch, body="(silica: maximum iterations reached)")

    with pytest.raises(ValueError, match="no findings"):
        wr.web_research("x")
    inbox = Path(CONFIG.vault_path) / CONFIG.inbox_dir
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_web_research_missing_key_raises_before_loop(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")

    called = {"run": False}

    def fake_run_agent(*a, **k):
        called["run"] = True
        return "x"

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)
    with pytest.raises(ValueError, match="TAVILY"):
        wr.web_research("x")
    assert called["run"] is False  # fail fast, no loop, no note


def test_web_research_empty_body_raises_and_writes_nothing(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(monkeypatch, body="")

    with pytest.raises(ValueError, match="no findings"):
        wr.web_research("x")
    inbox = Path(CONFIG.vault_path) / CONFIG.inbox_dir
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_web_research_sources_section_nonempty_when_no_sources(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(monkeypatch, body="Findings with no sources and no trace.", tool_results=[])

    body = (Path(CONFIG.vault_path) / wr.web_research("x")).read_text(encoding="utf-8")
    assert body.count("## Sources") == 1
    assert "(no sources captured)" in body


def test_web_research_title_with_colon_is_valid_yaml(tmp_vault, monkeypatch):
    """A concept containing a colon must produce parseable YAML frontmatter."""
    import yaml

    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")
    _patch_run_agent(
        monkeypatch,
        body="Findings about RAG.",
        tool_results=[[{"title": "T1", "url": "https://a.test", "content": "c1"}]],
    )

    note_rel = wr.web_research("RAG: a survey")
    body = (Path(CONFIG.vault_path) / note_rel).read_text(encoding="utf-8")

    # Extract the frontmatter block between the first two --- delimiters
    parts = body.split("---\n", 2)
    assert len(parts) >= 3, "frontmatter delimiters not found"
    fm_block = parts[1]
    fm = yaml.safe_load(fm_block)
    assert fm["title"] == "RAG: a survey"
    # Ensure the malformed bare form is not present
    assert "title: RAG: a survey\n" not in body


# --- ADR-0015 / ADR-0009 boundary, as wired in production --------------------

def test_main_agent_default_toolset_excludes_web_search():
    """With web_search registered (module imported), run_agent without
    constraints must NOT expose it to the main agent."""
    from unittest.mock import patch
    from types import SimpleNamespace
    from silica.agent.loop import run_agent

    assert "web_search" in TOOLS  # registered by importing this module's target

    captured = {}

    def fake_call_llm(model, messages, tools=None, cancel=None):
        captured["tools"] = tools
        return SimpleNamespace(
            assistant_message={"role": "assistant", "content": "ok"},
            tool_calls=[], text="ok", reasoning=None, usage={},
        )

    with patch("silica.agent.loop.call_llm", fake_call_llm):
        run_agent(messages=[{"role": "user", "content": "hi"}], model="m")

    names = {t["function"]["name"] for t in (captured["tools"] or [])}
    assert "web_search" not in names


# --- /fetch ------------------------------------------------------------------

def _patch_web_fetch(monkeypatch, text):
    import silica.sources.web_fetch as wf
    monkeypatch.setattr(wf, "web_fetch", lambda url: text)


def test_fetch_to_inbox_writes_a_note_titled_after_the_page(tmp_vault, monkeypatch):
    _patch_web_fetch(
        monkeypatch,
        "Source: https://a.test/post\n\nOn Graph Theory\n\nBody prose here.",
    )

    note_rel = wr.fetch_to_inbox("https://a.test/post")

    assert note_rel.startswith(f"{CONFIG.inbox_dir}/")
    body = (Path(CONFIG.vault_path) / note_rel).read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    assert 'title: "On Graph Theory"' in body
    assert "source: web-fetch" in body
    assert "tags: [inbox, web-fetch]" in body
    assert f"fetched: {today}" in body
    assert "Body prose here." in body
    # the ADR-0015 sources guarantee: a ## Sources block naming this URL, not
    # merely the URL appearing somewhere in the fetched text we echo verbatim
    assert "## Sources" in body
    assert "1. On Graph Theory — https://a.test/post" in body


def test_fetch_to_inbox_writes_a_source_leaf(tmp_vault, monkeypatch):
    from silica.kernel.recall.paths import SOURCES_DIR

    _patch_web_fetch(monkeypatch, "Source: https://a.test/post\n\nTitle\n\nBody.")
    note_rel = wr.fetch_to_inbox("https://a.test/post")

    leaf = Path(CONFIG.vault_path) / SOURCES_DIR / note_rel.rsplit("/", 1)[-1]
    assert leaf.exists()
    assert "Body." in leaf.read_text(encoding="utf-8")


def test_fetch_to_inbox_rejects_a_page_with_no_body(tmp_vault, monkeypatch):
    """A login wall can extract down to nothing but our own Source: header."""
    _patch_web_fetch(monkeypatch, "Source: https://a.test/post\n\n")
    with pytest.raises(ValueError, match="nothing readable"):
        wr.fetch_to_inbox("https://a.test/post")
    inbox = Path(CONFIG.vault_path) / CONFIG.inbox_dir
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_fetch_to_inbox_propagates_the_fetch_error(tmp_vault, monkeypatch):
    import silica.sources.web_fetch as wf

    def boom(url):
        raise ValueError("403 at https://a.test: bot wall")

    monkeypatch.setattr(wf, "web_fetch", boom)
    with pytest.raises(ValueError, match="403"):
        wr.fetch_to_inbox("https://a.test/post")
    inbox = Path(CONFIG.vault_path) / CONFIG.inbox_dir
    assert not inbox.exists() or not list(inbox.glob("*.md"))


def test_fetch_to_inbox_does_not_attach_a_stale_leaf_after_nucleate(tmp_vault, monkeypatch):
    """A note's sources/ leaf outlives /nucleate consuming the note itself
    (by design). A later /fetch that happens to produce the same title must
    not inherit that unrelated, stale leaf for its own new note."""
    from silica.kernel.recall.paths import SOURCES_DIR

    _patch_web_fetch(monkeypatch, "Source: https://a.test/first\n\nSame Title\n\nPage A body.")
    note_a = wr.fetch_to_inbox("https://a.test/first")
    (Path(CONFIG.vault_path) / note_a).unlink()  # simulate /nucleate consuming the note

    _patch_web_fetch(monkeypatch, "Source: https://b.test/second\n\nSame Title\n\nPage B body.")
    note_b = wr.fetch_to_inbox("https://b.test/second")

    leaf = Path(CONFIG.vault_path) / SOURCES_DIR / note_b.rsplit("/", 1)[-1]
    content = leaf.read_text(encoding="utf-8")
    assert "Page B body." in content
    assert "Page A body." not in content
