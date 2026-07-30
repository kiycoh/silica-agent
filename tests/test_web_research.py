"""web_search tool + web_research orchestrator (ADR-0015 staged acquisition).

No real network (the _no_network fixture below fails any unstubbed httpx call)
and no real LLM (run_agent is monkeypatched). Asserts: the DuckDuckGo primary
lane, the Mojeek, Tavily and Wikipedia backstops behind it, the lane line that
names a fallback on the note, compact result mapping, sensitivity, and the inbox
findings note.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import httpx
import pytest

from silica.config import CONFIG
from silica.sources import web_research as wr
from silica.tools import TOOLS


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every lane is a stub or an error. Mojeek scrapes with httpx.get, so a
    test that stubs only httpx.post would reach the real Mojeek from a
    challenged DDG — the fixture turns that into a failure, not a slow pass."""

    def boom(url, *a, **kw):
        raise AssertionError(f"test reached the network: {url}")

    monkeypatch.setattr(wr.httpx, "get", boom)
    monkeypatch.setattr(wr.httpx, "post", boom)


# --- web_search tool --------------------------------------------------------

def test_web_search_registered_and_sensitive():
    assert "web_search" in TOOLS
    assert TOOLS["web_search"].sensitive is True


_DDG_HTML = """
<div class="result results_links web-result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.test%2Fpage&amp;rut=abc">Title <b>One</b></a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Snippet <b>text</b> one.</a>
</div>
<div class="result result--ad">
  <a rel="nofollow" class="result__a"
     href="https://duckduckgo.com/y.js?ad_domain=x&amp;u3=enc">Ad title</a>
  <a class="result__snippet">Buy stuff.</a>
</div>
"""


class _FakeDDGResp:
    status_code = 200
    text = _DDG_HTML

    def raise_for_status(self):
        return self


def test_web_search_without_key_uses_duckduckgo(monkeypatch):
    """No key is not an error: the default backend scrapes DDG's HTML endpoint,
    unwraps the redirect hrefs, and drops the ad (whose href has no uddg)."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["url"] = url
        seen["data"] = data
        seen["headers"] = headers
        return _FakeDDGResp()

    monkeypatch.setattr(wr.httpx, "post", fake_post)

    items = json.loads(wr.web_search("graph theory"))

    assert seen["url"] == wr._DDG_URL
    assert seen["data"]["q"] == "graph theory"
    assert "Mozilla" in seen["headers"]["User-Agent"]  # browser UA, not httpx's
    assert items == [
        {"title": "Title One", "url": "https://a.test/page", "content": "Snippet text one."}
    ]


class _Challenged(_FakeDDGResp):
    """DDG's rate-limit answer: 202, which raise_for_status waves through as
    success, with a bare JavaScript shell for a body."""

    status_code = 202
    text = "prove you are human"


# Mojeek's results list, plus a chrome list carrying an <h2> anchor of its own:
# the parser is anchored on ul.results-standard, so the chrome must not become a
# hit. Selectors per searxng's mojeek engine (see _mojeek_search).
_MOJEEK_HTML = """
<ul class="nav-standard">
  <li><h2><a href="https://www.mojeek.com/about/">About Mojeek</a></h2></li>
</ul>
<ul class="results-standard">
  <li>
    <h2><a href="https://m1.test/page">Mojeek <b>One</b></a></h2>
    <a class="ob" href="https://m1.test/page">m1.test/page</a>
    <p class="s">Snippet one &amp; a bit.</p>
  </li>
  <li>
    <h2><a href="https://m2.test/">Mojeek Two</a></h2>
    <a class="ob" href="https://m2.test/">m2.test</a>
    <p class="s">Snippet two.</p>
  </li>
</ul>
"""


class _FakeMojeekResp:
    status_code = 200
    text = _MOJEEK_HTML


class _MojeekCaptcha:
    """Mojeek's challenge: HTTP *200* with a captcha page, so the status code
    cannot be the guard (measured 2026-07-30, every UA tried)."""

    status_code = 200
    text = '<html><head><title>Captcha</title></head><body>...</body></html>'


def _fake_get(resp):
    def fake_get(url, params=None, headers=None, timeout=None, follow_redirects=None):
        assert url == wr._MOJEEK_URL
        assert params == {"q": "graph theory"}
        return resp

    return fake_get


def test_web_search_falls_back_to_mojeek_when_ddg_challenges(monkeypatch):
    """The keyless default keeps the open web when DDG challenges: Mojeek is its
    own crawl with its own rate limits, so it is tried before the encyclopedia."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _Challenged())
    monkeypatch.setattr(wr.httpx, "get", _fake_get(_FakeMojeekResp()))
    monkeypatch.setattr(wr._web_fetch, "_fetch", _wikipedia_must_not_run)

    items = json.loads(wr.web_search("graph theory"))

    assert items == [
        {"title": "Mojeek One", "url": "https://m1.test/page",
         "content": "Snippet one & a bit."},
        {"title": "Mojeek Two", "url": "https://m2.test/", "content": "Snippet two."},
    ]


def test_mojeek_runs_ahead_of_a_set_key(monkeypatch):
    """Same posture as DDG-first: a keyless lane on its own index beats billing a
    vendor, so a healthy Mojeek means Tavily is never posted to."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k-123")
    posted = []

    def fake_post(url, **kw):
        posted.append(url)
        return _Challenged()  # only DDG should be posted to at all

    monkeypatch.setattr(wr.httpx, "post", fake_post)
    monkeypatch.setattr(wr.httpx, "get", _fake_get(_FakeMojeekResp()))

    items = json.loads(wr.web_search("graph theory"))

    assert posted == [wr._DDG_URL]
    assert [i["url"] for i in items] == ["https://m1.test/page", "https://m2.test/"]


def test_mojeek_parse_does_not_depend_on_anchor_order(monkeypatch):
    """searxng's selectors say the title anchor is a sibling of the url anchor
    but not which comes first, and that is not verifiable from a captcha'd IP —
    so the parser takes the url from whichever anchor leads and the title only
    from the one inside the <h2>, and holds either way round."""
    swapped = _MOJEEK_HTML.replace(
        '<h2><a href="https://m1.test/page">Mojeek <b>One</b></a></h2>\n'
        '    <a class="ob" href="https://m1.test/page">m1.test/page</a>',
        '<a class="ob" href="https://m1.test/page">m1.test/page</a>\n'
        '    <h2><a href="https://m1.test/page">Mojeek <b>One</b></a></h2>',
    )
    assert swapped != _MOJEEK_HTML  # the replace matched

    class _Swapped(_FakeMojeekResp):
        text = swapped

    monkeypatch.setattr(wr.httpx, "get", _fake_get(_Swapped()))

    assert wr._mojeek_search("graph theory")[0] == {
        "title": "Mojeek One",
        "url": "https://m1.test/page",
        "content": "Snippet one & a bit.",
    }


def test_mojeek_captcha_and_empty_page_both_raise(monkeypatch):
    """A 200 captcha and a 200 whose markup no longer parses must both raise: a
    silent [] would spend the loop's whole budget on a lane that stopped
    answering and never reach the ones that still do."""
    monkeypatch.setattr(wr.httpx, "get", _fake_get(_MojeekCaptcha()))
    with pytest.raises(ValueError, match="challenged"):
        wr._mojeek_search("graph theory")

    class _Renamed(_FakeMojeekResp):
        text = _MOJEEK_HTML.replace("results-standard", "results-v2")

    monkeypatch.setattr(wr.httpx, "get", _fake_get(_Renamed()))
    with pytest.raises(ValueError, match="no parseable results"):
        wr._mojeek_search("graph theory")


class _FakeWPResp:
    def json(self):
        return {
            "query": {
                "search": [
                    {"title": "Graph theory",
                     "snippet": 'a <span class="searchmatch">graph</span> is a &amp; b'},
                    {"title": "PageRank", "snippet": "link analysis"},
                ]
            }
        }


def test_web_search_falls_back_to_wikipedia_when_ddg_challenges(monkeypatch):
    """Measured: DDG 202s from the third consecutive query and 20s of backoff
    does not clear it, while the loop is budgeted for 8-10. A challenge must
    degrade the lane, not end the research turn. Keyless with Mojeek challenged
    too, the encyclopedia is what is left."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _Challenged())
    monkeypatch.setattr(wr.httpx, "get", lambda *a, **k: _MojeekCaptcha())
    seen = []

    def fake_fetch(url, headers=None):
        seen.append((url, headers))
        return _FakeWPResp(), url

    monkeypatch.setattr(wr._web_fetch, "_fetch", fake_fetch)

    items = json.loads(wr.web_search("graph theory"))

    assert seen[0][0].startswith("https://en.wikipedia.org/w/api.php?")
    assert "list=search" in seen[0][0] and "srsearch=graph+theory" in seen[0][0]
    assert "silica-agent" in seen[0][1]["User-Agent"]  # Wikimedia UA policy
    assert items == [
        {"title": "Graph theory",
         "url": "https://en.wikipedia.org/wiki/Graph_theory",
         "content": "a graph is a & b"},
        {"title": "PageRank",
         "url": "https://en.wikipedia.org/wiki/PageRank",
         "content": "link analysis"},
    ]


def test_web_search_double_failure_names_the_tavily_escape_hatch(monkeypatch):
    """When the fallback is down too, the surfaced error is DDG's, the
    one that tells the user their way out, and Wikipedia's would bury it."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _Challenged())
    monkeypatch.setattr(wr.httpx, "get", lambda *a, **k: _MojeekCaptcha())

    def boom(*a, **k):
        raise ValueError("cannot resolve 'en.wikipedia.org'")

    monkeypatch.setattr(wr._web_fetch, "_fetch", boom)
    with pytest.raises(ValueError, match="TAVILY"):
        wr.web_search("anything")


class _FakeTavilyResp:
    def raise_for_status(self):
        return self

    def json(self):
        return {
            "results": [
                {"title": "T1", "url": "https://a.test", "content": "c1", "score": 0.9},
                {"title": "T2", "url": "https://b.test", "content": "c2"},
            ]
        }


def test_web_search_prefers_ddg_over_tavily_when_a_key_is_set(monkeypatch):
    """A key is a backstop, not a switch: DDG is the primary lane, so a healthy
    DDG answers and Tavily is never billed."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k-123")
    posted = []

    def fake_post(url, **kw):
        posted.append(url)
        return _FakeDDGResp()

    monkeypatch.setattr(wr.httpx, "post", fake_post)

    items = json.loads(wr.web_search("graph theory"))

    assert posted == [wr._DDG_URL]
    assert [i["url"] for i in items] == ["https://a.test/page"]


def test_web_search_posts_to_tavily_when_ddg_challenges(monkeypatch):
    """With a key, Tavily takes over from a challenged DDG once the keyless
    Mojeek lane is challenged too, and still ahead of the Wikipedia lane, which
    stays the last resort."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k-123")
    monkeypatch.setattr(wr.httpx, "get", lambda *a, **k: _MojeekCaptcha())
    seen = {}

    def fake_post(url, json=None, data=None, headers=None, timeout=None):
        if url == wr._DDG_URL:
            return _Challenged()
        seen["url"] = url
        seen["body"] = json
        seen["timeout"] = timeout
        return _FakeTavilyResp()

    monkeypatch.setattr(wr.httpx, "post", fake_post)
    monkeypatch.setattr(wr._web_fetch, "_fetch", _wikipedia_must_not_run)

    items = json.loads(wr.web_search("graph theory"))

    assert seen["url"] == wr._TAVILY_URL
    assert seen["body"]["api_key"] == "k-123"
    assert seen["body"]["query"] == "graph theory"
    assert seen["body"]["max_results"] == wr._MAX_RESULTS
    assert items == [
        {"title": "T1", "url": "https://a.test", "content": "c1"},
        {"title": "T2", "url": "https://b.test", "content": "c2"},
    ]


def test_web_search_falls_through_tavily_to_wikipedia(monkeypatch):
    """A key that is expired or a Tavily outage must not end the turn: the
    keyless lane is still there behind it."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k-123")
    monkeypatch.setattr(wr.httpx, "get", lambda *a, **k: _MojeekCaptcha())

    def fake_post(url, **kw):
        if url == wr._DDG_URL:
            return _Challenged()
        raise httpx.HTTPError("tavily 401")

    monkeypatch.setattr(wr.httpx, "post", fake_post)
    monkeypatch.setattr(wr._web_fetch, "_fetch", lambda url, headers=None: (_FakeWPResp(), url))

    items = json.loads(wr.web_search("graph theory"))

    assert [i["url"] for i in items] == [
        "https://en.wikipedia.org/wiki/Graph_theory",
        "https://en.wikipedia.org/wiki/PageRank",
    ]


def _wikipedia_must_not_run(url, headers=None):
    raise AssertionError(f"Wikipedia lane ran while Tavily was available: {url}")


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


def _patch_run_agent_calling_web_search(monkeypatch, calls):
    """Fake run_agent that goes through the real web_search, so the lanes on the
    note are the ones that actually answered rather than a hand-set list."""

    def fake_run_agent(messages, model, tool_progress_callback=None, constraints=None, **kw):
        for _ in range(calls):
            wr.web_search("graph theory")
        return "Findings [1]."

    monkeypatch.setattr(wr, "run_agent", fake_run_agent)


def test_web_research_note_names_the_lanes_that_answered(tmp_vault, monkeypatch):
    """The loud half: a note whose sources came from a fallback says which lane
    and how many calls, so a thin answer is legible as a challenged primary lane
    rather than as a thin web."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _Challenged())
    monkeypatch.setattr(wr.httpx, "get", lambda *a, **k: _FakeMojeekResp())
    _patch_run_agent_calling_web_search(monkeypatch, calls=2)

    body = (Path(CONFIG.vault_path) / wr.web_research("graph theory")).read_text(
        encoding="utf-8"
    )

    assert "Search lanes: mojeek 2. DuckDuckGo was challenged" in body


def test_web_research_note_stays_quiet_when_ddg_answered(tmp_vault, monkeypatch):
    """No banner on a healthy note: the line is the fallback's, not a status
    report on every turn."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _FakeDDGResp())
    _patch_run_agent_calling_web_search(monkeypatch, calls=2)

    body = (Path(CONFIG.vault_path) / wr.web_research("graph theory")).read_text(
        encoding="utf-8"
    )

    assert "Search lanes" not in body


def test_lane_line_is_per_turn_not_cumulative(tmp_vault, monkeypatch):
    """A second turn must not inherit the first one's lanes: web_research clears
    the record before the loop, so a recovered DDG stops reporting a fallback."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _Challenged())
    monkeypatch.setattr(wr.httpx, "get", lambda *a, **k: _FakeMojeekResp())
    _patch_run_agent_calling_web_search(monkeypatch, calls=1)
    wr.web_research("graph theory")

    monkeypatch.setattr(wr.httpx, "post", lambda *a, **k: _FakeDDGResp())
    body = (Path(CONFIG.vault_path) / wr.web_research("second turn")).read_text(
        encoding="utf-8"
    )

    assert "Search lanes" not in body


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


def test_web_research_runs_without_a_key(tmp_vault, monkeypatch):
    """No Tavily key no longer fails fast: web_search falls back to DDG."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "")
    _patch_run_agent(
        monkeypatch,
        body="Findings.",
        tool_results=[[{"title": "T", "url": "https://a.test", "content": "c"}]],
    )

    note_rel = wr.web_research("x")
    assert (Path(CONFIG.vault_path) / note_rel).exists()


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


def test_fetch_to_inbox_assumes_https_for_a_bare_domain(tmp_vault, monkeypatch):
    """`/fetch en.wikipedia.org/wiki/X` is how humans type URLs. The scheme is
    inferred here at the user-facing seam, so the strict guard in web_fetch
    still validates the full https form (and agent calls stay strict)."""
    import silica.sources.web_fetch as wf
    seen = {}

    def fake(url):
        seen["url"] = url
        return "Source: https://a.test/post\n\nOn Graph Theory\n\nBody."

    monkeypatch.setattr(wf, "web_fetch", fake)

    body = (Path(CONFIG.vault_path) / wr.fetch_to_inbox("a.test/post")).read_text(
        encoding="utf-8"
    )

    assert seen["url"] == "https://a.test/post"
    # the citation carries the URL actually fetched, not the schemeless input
    assert "1. On Graph Theory — https://a.test/post" in body


def test_fetch_to_inbox_sources_block_cannot_be_spoofed_by_the_page(tmp_vault, monkeypatch):
    """For /fetch the note body IS the fetched page, so a page that happens to
    contain its own `## Sources` heading (any markdown README does) would
    otherwise suppress ours and leave a reviewer looking at an attacker-authored
    Sources section. ADR-0015 makes sources mandatory, not content-dependent."""
    _patch_web_fetch(
        monkeypatch,
        "Source: https://raw.example.test/README.md\n\nAwesome Thing\n\n"
        "Prose.\n\n## Sources\n1. Somebody else — https://evil.test/theirs\n",
    )

    body = (Path(CONFIG.vault_path) / wr.fetch_to_inbox(
        "https://raw.example.test/README.md")).read_text(encoding="utf-8")

    assert "1. Awesome Thing — https://raw.example.test/README.md" in body


def test_fetch_to_inbox_falls_back_to_its_own_namespace(tmp_vault, monkeypatch):
    """A title that slugifies to nothing must not land on web-research.md and
    push the other command's next note to `web-research 2.md`."""
    _patch_web_fetch(monkeypatch, "Source: https://a.test/p\n\n***\n\nBody.")

    assert wr.fetch_to_inbox("https://a.test/p") == f"{CONFIG.inbox_dir}/web-fetch.md"


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


# --- the CLI lines, which interpolate untrusted text into Rich markup --------


def _run_cli(cmd: str) -> str:
    """Dispatch one REPL command, returning what the user would see."""
    from silica import cli
    from silica.ui.console import CONSOLE

    with CONSOLE.capture() as capture:
        assert cli._expand_workflow_shortcut(cmd) == ""  # handled inline
    return capture.get()


def test_fetch_failure_line_survives_a_url_that_looks_like_rich_markup(
    tmp_vault, monkeypatch
):
    """A URL carrying `[/x]` used to raise MarkupError out of the very except
    that exists to report the failure, so the user got a traceback."""
    import silica.sources.web_fetch as wf

    def boom(url):
        raise ValueError(f"cannot resolve {url!r}: nope")

    monkeypatch.setattr(wf, "web_fetch", boom)

    out = _run_cli("/fetch https://a.test/[/x]")
    assert "fetch failed" in out
    assert "[/x]" in out  # shown verbatim, not swallowed as a closing tag


def test_fetch_success_line_shows_the_real_path(tmp_vault, monkeypatch):
    """slugify strips `\\ / : * ? " < > |` but not brackets, so a page titled
    `[bold red]Foo` yields a note_rel whose markup Rich would silently eat,
    telling the user a path that is not the file's name."""
    _patch_web_fetch(monkeypatch, "Source: https://a.test/p\n\n[bold red]Foo\n\nBody.")

    out = _run_cli("/fetch https://a.test/p")
    assert "[bold red]Foo.md" in out


def test_web_search_failure_line_survives_rich_markup(tmp_vault, monkeypatch):
    """The identical defect two lines away in the sibling command."""
    monkeypatch.setattr(CONFIG, "tavily_api_key", "k")

    def boom(*a, **kw):
        raise ValueError("no findings for 'x [/y] z'")

    monkeypatch.setattr(wr, "web_research", boom)

    out = _run_cli('/web-search "x"')
    assert "web-search failed" in out
    assert "[/y]" in out
