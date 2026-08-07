# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`/web-search` — agentic web-research loop → cited findings note in the Inbox.

ADR-0015 staged acquisition: Silica may *fetch* on request but never *decides*
what enters the vault. The loop is constrained to `web_search` (find pages),
`web_fetch` (read one) and `remember` (bank a verbatim quote from a fetched
page); it physically cannot write to the vault. Its output is one findings note
in the Inbox, with sources cited. The note enters the vault only via /nucleate.

Citations are guaranteed mechanically at two grains (spec-web-research-memory-bank):
the *sources* come from the fetch trace, so a URL the model never opened cannot
be cited; the *quotes* go through remember's verbatim guardian, so a sentence
the page does not contain cannot be banked, and `_bind_citations` removes any
inline marker that names no banked quote. Fabrication is structurally
impossible at both levels, not merely discouraged by the prompt.

When a run banked quotes, the note body is recomposed from them (spec
§3.3-3.4): one tool-less call outlines sections from the bank index, one per
section writes prose from only that section's quotes — so a deep run's note is
written from evidence still in view, not from a history compaction has already
stubbed. Any composition failure falls back to the loop's one-shot final
message (§3.6). /web-search only; /web answers in direct prose (§5).

Both tools are `sensitive=True` (ADR-0009): the main agent's default toolset
excludes them, so they are reachable only where named explicitly in
AgentConstraints — here, and in fetch_to_inbox() for `/fetch`.

`web_search` needs no key: the primary backend scrapes DuckDuckGo's HTML
endpoint with httpx + stdlib html.parser (same posture as web_fetch — no
vendor sees the query). DDG starts challenging after a couple of queries, so
Mojeek (its own index, also keyless), then Tavily (when SILICA_TAVILY_API_KEY
is set), then a Wikipedia search keep a capped loop moving. A key is a
backstop, not a switch: DDG still runs first, and both keyless lanes run ahead
of it. Which lanes answered is named on the note (`_lane_line`), so a turn that
never reached the open web says so instead of reading as a thin answer.
"""
from __future__ import annotations

import datetime
import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from silica.agent.constraints import AgentConstraints
from silica.agent.events import ToolCompleteEvent
from silica.agent.llm import call_llm
from silica.agent.loop import run_agent
from silica.config import CONFIG
from silica.kernel.write.templates import slugify
from silica.tools import tool

# Importing the module runs its @tool decorator, which is what puts web_fetch in
# TOOLS for the AgentConstraints below to name. Bound as a module, not as the
# function, so fetch_to_inbox() resolves the attribute at call time.
from silica.sources import web_fetch as _web_fetch  # noqa: F401
from pydantic import BaseModel

_TAVILY_URL = "https://api.tavily.com/search"
_DDG_URL = "https://html.duckduckgo.com/html/"
_MOJEEK_URL = "https://www.mojeek.com/search"
_WP_SITE = "https://en.wikipedia.org"
_WP_TAG_RE = re.compile(r"<[^>]+>")
_MAX_RESULTS = 5            # ponytail: per-query cap; promote to CONFIG if a real query needs more
_HTTP_TIMEOUT = 30
# Fetches spend iterations too, so the budget covers both calls. The flag is
# still --max-searches: renaming a user-facing flag buys nothing.
#
# A ceiling, not a target: the loop stops when the prompt below says to stop, and
# a run that converges in six steps still costs six. 16 was cutting off runs that
# had more to do (interaction depth is the third scaling axis after parameters and
# context window — MiroThinker measures 8-10 points from it alone), and the loop
# already lands gracefully at the wall: run_agent spends one last tool-less turn
# asking for the findings back, so hitting the cap yields a note rather than a
# ValueError. What makes a deep run survivable is silica/agent/compaction.py,
# which past 60% of the context window rewrites every old web_fetch body to a
# stub while keeping the last turns and the whole reasoning trace verbatim.
# Citations are unaffected: they are rebuilt from `trace`, which never lived in
# the message history.
#
# ponytail: 48 because the search lanes ration us before this number does — DDG
# challenges from the third consecutive query and the backstops are one crawler
# and one encyclopedia, so a run that actually spends 48 steps says so in its
# lane line. Raise it when a lane can carry it.
_DEFAULT_MAX_SEARCHES = 48

_RESEARCH_SYSTEM_PROMPT = """You are a focused web-research agent. Given a \
concept, research it on the web and write a findings note.

Method (iterative deepening):
1. Decompose the concept into what you need to know.
2. Call `web_search(query)` for the most important sub-question.
3. When a result looks like it actually answers the question, call \
`web_fetch(url)` and read the page. A search snippet is not the article. One \
fetch of a good source beats three more searches.
4. Bank the evidence while the page is in front of you: call \
`remember(url, quote, why)` for each sentence or figure you expect to write \
from, copying the quote exactly as the fetched text shows it. It returns an ID \
like Q3; cite that inline as [Q3]. A claim you could not bank a quote for is a \
claim you cannot make.
5. Identify gaps and adjacent areas of knowledge.
6. Search again where a gap remains. Stop when another search would no longer \
change what you are about to write — not when you have merely enough to write \
something. One or two steps for a trivial concept; twenty to thirty is normal \
for a genuinely complex one. Do not pad with redundant searches, and do not \
stop on a question you have only answered from snippets.
7. When done, reply with NO tool call — your final message IS the note body.

The note body must be markdown prose synthesising what you found, citing \
banked quotes inline as [Q1], [Q2] exactly as remember named them. Do not \
renumber them and do not write a Sources section: sources are appended \
mechanically from the pages you actually opened, and every [Qn] marker is \
checked against the bank — one that names no banked quote is removed.

Do not write YAML frontmatter; it is added for you. Write only the prose."""

# Appended to the system prompt only when steering is on, so the prompt step
# and the tool appear and disappear together.
_STEER_STEP = """

Throughout, keep a live plan of the note you will write: call \
`plan(outline)` with `## ` section headings as soon as the shape of the \
answer is clear, ending each heading with the banked IDs it will draw on, \
like `## Where they fail [Q2, Q9]`. A heading without IDs is a gap: research \
it next. Update the plan whenever remember returns new IDs; the reply names \
the sections still without evidence. You are saturated when no section that \
matters is still empty."""

# ponytail: gate seam. evals/probe_web_gate.py flips this to run gate arm A
# (the exact pre-steering loop) live; product code never touches it.
_STEERING = True


class WebSearchArgs(BaseModel):
    query: str


# Which lane answered each web_search call of the current turn, in call order.
# Per-process and per-turn: web_research() and WebTurn() clear it before the loop
# starts, and _lane_line() reads it after. Kept beside the tool rather than
# threaded through its return value: the payload shape is the model's contract,
# and a lane is the *user's* business, not the model's.
_LANES: list[str] = []

# Consecutive web_search calls that exhausted every lane. Cleared beside _LANES.
#
# A search that reaches the `else` below has paid up to four HTTP timeouts, so a
# stack that is wholly down costs ~2 minutes per call and the loop keeps buying
# them: the convergence guard in run_agent keys on identical arguments, and a
# research loop never repeats a query. That is survivable at a ceiling of 16 and
# is not at 48, so past _DEAD_LANES_LIMIT the tool stops dialling and fails on
# the spot. The model still sees an error and can still write the note from what
# it has — the loop lands, it just lands in seconds instead of in half an hour.
_DEAD = 0
_DEAD_LANES_LIMIT = 3


class _Quote(NamedTuple):
    """One banked quote: the evidence unit behind an inline [Qn] citation."""

    url: str
    quote: str
    why: str


# The turn's evidence bank: "Q1", "Q2", ... -> _Quote, in remember order.
# Same per-process, per-turn life as _LANES. The note builders snapshot it
# (dict(_BANK)) before the next turn's _reset_turn can clear it.
_BANK: dict[str, _Quote] = {}

# url -> fetched page text, fed by _harvest_page from the turn's tool events.
# remember's guardian checks quotes against these bodies: a page the loop never
# fetched cannot back a quote.
_PAGES: dict[str, str] = {}

# The live research plan (spec-web-research-plan-steering §3.1): markdown
# `## ` headings, optionally ending with banked IDs like [Q1, Q3]. Steering
# state only: composition never reads it (§2 invariant).
_PLAN: str = ""


def _reset_turn() -> None:
    """Clear the per-turn search state. Both callers of the loop run this first."""
    global _DEAD, _PLAN

    _LANES.clear()
    _BANK.clear()
    _PAGES.clear()
    _DEAD = 0
    _PLAN = ""


def _harvest_page(event) -> None:
    """Feed the remember guardian: keep each fetched page under its final URL.

    Both loop recorders call this per event, so the page is on file before the
    model can even see it, let alone quote it. The key is the result's own
    `Source:` head — the post-redirect URL, the same one _collect_sources
    cites — so the guardian and the citation can never disagree about a page's
    name.
    """
    if (
        isinstance(event, ToolCompleteEvent)
        and event.name == "web_fetch"
        and isinstance(event.result, str)
    ):
        head, _, _ = event.result.partition("\n")
        url = head[len("Source: "):].strip() if head.startswith("Source: ") else ""
        if url:
            _PAGES[url] = event.result


# Typography a model cannot be asked to reproduce: pages render apostrophes and
# dashes with the curly/long forms, models copy them back as ASCII, and the
# guardian then reads a correct quote as a paraphrase. This was the whole
# "remember paraphrase" spiral — on docs.kernel.org ("the states it’s") a run
# burned 18 rejections and died with no findings, and the one arm that got a
# quote past the guardian did it by cutting the quote off at the character
# before the apostrophe. Folding these does NOT weaken the guarantee: every
# word must still appear verbatim, only quote and dash STYLE is forgiven.
_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "…": "...",
})


def _squash(text: str) -> str:
    """Whitespace- and quote-style-insensitive form for verbatim comparison."""
    return " ".join(text.translate(_TYPOGRAPHY).split())


@tool(WebSearchArgs, cls="atomic", sensitive=True)
def web_search(query: str) -> str:
    """Search the web for a single query. Returns a JSON list of
    {title, url, content} results. Use iteratively to research a concept."""
    global _DEAD

    if _DEAD >= _DEAD_LANES_LIMIT:
        raise ValueError(
            f"web search is unavailable: {_DEAD} consecutive queries exhausted "
            "every lane (DuckDuckGo, Mojeek, Wikipedia and Tavily where "
            "configured). Not retrying. Write up what you already have, or "
            "retry later / set SILICA_TAVILY_API_KEY."
        )
    try:
        compact = _ddg_search(query)
        _LANES.append("duckduckgo")
        _DEAD = 0
    except ValueError as ddg_err:
        # DDG is the primary lane and runs first even when a key is set: it is
        # the whole web, and no vendor sees the query. The backstops only run
        # when it challenges us, best first — Mojeek ahead of a set key for the
        # same reason DDG leads (its own crawl, keyless, no vendor account), and
        # the encyclopedia last because one encyclopedia is not the web.
        key = (CONFIG.tavily_api_key or "").strip()
        backstops = [(_mojeek_search, "mojeek"), (_wikipedia_search, "wikipedia")]
        if key:
            backstops.insert(1, (lambda q: _tavily_search(q, key), "tavily"))
        for backstop, lane in backstops:
            try:
                compact = backstop(query)
            except Exception:
                continue
            _LANES.append(lane)
            _DEAD = 0
            break
        else:
            # The DDG message is the one that names the escape hatch, so a
            # total failure surfaces that one rather than a backstop's.
            _DEAD += 1
            raise ddg_err from None
    return json.dumps(compact, ensure_ascii=False)


def _lane_line() -> str:
    """One line naming the lanes that answered, empty while DDG answered alone.

    The loud half of the fallback. A note whose citations are five wikipedia.org
    URLs looks like a thin answer; this says it was a challenged primary lane,
    and the per-lane counts say how much of the note came from where. Silent
    otherwise: a banner on every healthy note is noise nobody reads.
    """
    if not _LANES or set(_LANES) == {"duckduckgo"}:
        return ""
    counts: dict[str, int] = {}
    for lane in _LANES:
        counts[lane] = counts.get(lane, 0) + 1
    named = ", ".join(f"{lane} {n}" for lane, n in counts.items())
    return f"Search lanes: {named}. DuckDuckGo was challenged; these answered instead."


class RememberArgs(BaseModel):
    url: str
    quote: str
    why: str


@tool(RememberArgs, cls="atomic", sensitive=True)
def remember(url: str, quote: str, why: str) -> str:
    """Bank one verbatim quote from a page already read with web_fetch, to cite
    in the findings. Copy the quote exactly as the fetched text shows it; `why`
    is one line on what it will support. Returns an ID like Q3 — cite it inline
    as [Q3]."""
    # The guardian (spec §3.2): a quote is accepted only when it appears
    # verbatim (up to whitespace) in the page fetched from that URL this turn.
    # String containment, no model in the loop — which is what makes the
    # rejection trustworthy: it cannot be sweet-talked.
    url = url.strip()
    page = _PAGES.get(url)
    if page is None:
        fetched = "\n".join(f"  {u}" for u in _PAGES) or "  (none yet)"
        raise ValueError(
            f"no page fetched from {url!r} this turn — quote only pages you "
            "have read with web_fetch, using the URL on the page's own "
            f"'Source:' line. Fetched so far:\n{fetched}"
        )
    squashed = _squash(quote)
    if not squashed:
        raise ValueError("empty quote — copy the exact sentence you will cite.")
    if squashed not in _squash(page):
        raise ValueError(
            "quote not found verbatim on that page — copy it exactly as the "
            "fetched text shows it (whitespace differences are fine, "
            "paraphrase is not)."
        )
    for qid, banked in _BANK.items():
        if banked.url == url and _squash(banked.quote) == squashed:
            return f"already banked as [{qid}]"
    qid = f"Q{len(_BANK) + 1}"
    _BANK[qid] = _Quote(url=url, quote=quote.strip(), why=why.strip())
    return f"banked [{qid}] — cite it inline as [{qid}]"


class PlanArgs(BaseModel):
    outline: str


@tool(PlanArgs, cls="atomic", sensitive=True)
def plan(outline: str) -> str:
    """Save or replace your working plan for the findings note: markdown
    `## ` section headings, each optionally ending with the banked quote IDs
    it will draw on, like `## Where they fail [Q2, Q9]`. A heading without
    IDs marks a section still to research. Each call replaces the whole
    plan; the reply names the sections still without evidence."""
    # The guardian (spec-web-research-plan-steering §3.2): structure and ID
    # validity are checked mechanically; coverage is what the return value
    # reports, not what it enforces. The bank only grows within a turn, so a
    # plan accepted here cannot lose ID validity later.
    global _PLAN

    text = outline.strip()
    covered = 0
    gaps: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m is None:
            continue
        if _OUTLINE_RE.match(line):
            covered += 1
        else:
            gaps.append(m.group(1))
    if covered + len(gaps) == 0:
        raise ValueError(
            "no `## ` section headings found. Write the plan as markdown "
            "`## ` headings, each optionally ending with banked IDs like "
            "[Q1, Q3]; a heading without IDs marks a section still to "
            "research."
        )
    cited = {
        qid
        for m in _QREF_RE.finditer(text)
        for qid in re.split(r"\s*,\s*", m.group(2))
    }
    unknown = sorted(cited - _BANK.keys(), key=lambda q: int(q[1:]))
    if unknown:
        known = ", ".join(_BANK) or "(none yet)"
        raise ValueError(
            f"unknown quote ID(s): {', '.join(unknown)}. The bank holds: "
            f"{known}. Cite only IDs that remember returned; a heading "
            "without IDs is fine for a section still to research."
        )
    _PLAN = text
    n = covered + len(gaps)
    head = f"saved: {n} section{'s' if n != 1 else ''}, "
    if gaps:
        return head + f"{covered} with evidence; gaps: " + "; ".join(gaps)
    return head + "all with evidence"


def _tavily_search(query: str, key: str) -> list[dict[str, str]]:
    # ponytail: direct REST, no tavily-python SDK until their API changes.
    resp = httpx.post(
        _TAVILY_URL,
        json={
            "api_key": key,
            "query": query,
            "search_depth": "advanced",
            "max_results": _MAX_RESULTS,
            "include_answer": False,
        },
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in results
        if r.get("url")
    ][:_MAX_RESULTS]


def _unwrap_ddg(href: str) -> str:
    """DDG result hrefs are redirect-wrapped (`//duckduckgo.com/l/?uddg=<url>`);
    ad links go through y.js and carry no uddg, so they unwrap to nothing."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com"):
        target = parse_qs(parts.query).get("uddg", [""])[0]
        return target if target.startswith("http") else ""
    return href if href.startswith("http") else ""


class _DDGParser(HTMLParser):
    """Scraper for html.duckduckgo.com results: a `result__a` anchor per hit
    (title + wrapped href) followed by a `result__snippet` anchor. Ads keep the
    same classes but their href unwraps to "", so they drop in the url filter
    downstream; their snippet lands in their own dict, not a neighbour's."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._field: str | None = None  # "title" | "content" while inside its <a>

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        a = dict(attrs)
        cls = a.get("class") or ""
        if "result__a" in cls:
            self.results.append(
                {"title": "", "url": _unwrap_ddg(a.get("href") or ""), "content": ""}
            )
            self._field = "title"
        elif "result__snippet" in cls and self.results:
            self._field = "content"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            self.results[-1][self._field] += data


def _ddg_search(query: str) -> list[dict[str, str]]:
    """Primary lane: scrape DuckDuckGo's HTML endpoint, stdlib parser only.
    Same local-first trade as web_fetch — no third party sees the query, and in
    exchange rate limits are ours to surface, not a vendor's to hide."""
    resp = httpx.post(
        _DDG_URL,
        data={"q": query},
        headers=_web_fetch._HEADERS,  # browser UA; a bare httpx UA gets challenged
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:  # DDG answers challenges with 202, not an error
        raise ValueError(
            f"DuckDuckGo answered HTTP {resp.status_code} (rate-limited or "
            "challenged); retry later or set SILICA_TAVILY_API_KEY so Tavily "
            "can take over when this happens."
        )
    parser = _DDGParser()
    parser.feed(resp.text)
    return [
        {"title": r["title"].strip(), "url": r["url"], "content": r["content"].strip()}
        for r in parser.results
        if r["url"]
    ][:_MAX_RESULTS]


class _MojeekParser(HTMLParser):
    """Scraper for mojeek.com/search: hits live in `<ul class="results-standard">`,
    one `<li>` each, with `<h2><a href>Title</a></h2>`, a second anchor to the
    same target and a `<p class="s">` snippet.

    Anchored on the results list, so a renamed class yields zero results rather
    than harvesting the chrome — `_mojeek_search` turns that emptiness into a
    raise, and the next lane answers.
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._depth = 0  # nesting inside the results <ul>; 0 means outside
        self._in_h2 = False
        self._field: str | None = None  # "title" | "content" while inside its tag

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "ul":
            if "results-standard" in cls:
                self._depth = 1
            elif self._depth:
                self._depth += 1  # a nested list inside a hit, not a new one
        elif not self._depth:
            return
        elif tag == "li" and self._depth == 1:
            self.results.append({"title": "", "url": "", "content": ""})
        elif not self.results:
            return
        elif tag == "h2":
            self._in_h2 = True
        elif tag == "a":
            href = a.get("href") or ""
            if href.startswith("http"):
                # Both anchors of a hit point at the target; the title is only
                # ever the one inside the <h2> (the other renders the bare URL).
                self.results[-1]["url"] = self.results[-1]["url"] or href
                if self._in_h2:
                    self._field = "title"
        elif tag == "p" and "s" in cls:
            self._field = "content"

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul" and self._depth:
            self._depth -= 1
        elif tag == "h2":
            self._in_h2 = False
            self._field = None
        elif tag in ("a", "p"):
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field and self.results:
            self.results[-1][self._field] += data


def _mojeek_search(query: str) -> list[dict[str, str]]:
    """Second keyless lane: Mojeek's own crawl, scraped like DDG.

    Keeps a challenged keyless default on the open web instead of dropping
    straight to one encyclopedia. Independent index and independent rate limits,
    which is the whole point of putting it here rather than a second DDG
    endpoint (measured 2026-07-30: html.duckduckgo.com 202s while
    lite.duckduckgo.com answers, but both are DDG's to challenge at once).

    Mojeek challenges with a *200* whose title is Captcha, so the status code
    cannot be the guard, and an unparseable page raises rather than returning []:
    a silent empty list would burn the loop's whole budget on a lane that stopped
    working, never reaching the ones that still do.

    ponytail: selectors mirror searxng's mojeek engine (AGPL, scrapes this same
    markup in production) because this IP is captcha'd on every query, UA and
    cookie jar regardless, so they are not verified against a live answer here.
    Re-check them the first time this lane returns nothing on a working IP.
    """
    resp = httpx.get(
        _MOJEEK_URL,
        params={"q": query},
        headers=_web_fetch._HEADERS,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
    )
    if resp.status_code != 200 or "<title>Captcha" in resp.text:
        raise ValueError(f"Mojeek answered HTTP {resp.status_code} (challenged).")
    parser = _MojeekParser()
    parser.feed(resp.text)
    hits = [
        {"title": r["title"].strip(), "url": r["url"], "content": r["content"].strip()}
        for r in parser.results
        if r["url"]
    ][:_MAX_RESULTS]
    if not hits:
        raise ValueError("Mojeek returned a page with no parseable results.")
    return hits


def _wikipedia_search(query: str) -> list[dict[str, str]]:
    """Last-resort lane for when DuckDuckGo challenges us and Tavily is absent
    or down.

    Measured 2026-07-30: DDG answers 202 from the third consecutive query and
    stays there: 3s, 8s and 20s of backoff all came back 202, and
    lite.duckduckgo.com was challenged on the same IP, while the loop above is
    budgeted for 8 to 10 searches. Without a fallback the keyless default dies
    partway through its own default workload, and the 202 body is a bare
    JavaScript shell with no challenge to answer.

    ponytail: one encyclopedia is not the web, and en.wikipedia.org is
    hardcoded rather than following the vault's language. This is a lane that
    keeps a capped loop moving, not a second search engine — that is what the
    Mojeek lane above it is for. Every URL it returns says wikipedia.org and
    `_lane_line` counts the calls it answered, so a note that leaned on it says
    so twice over.
    """
    q = urlencode(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "search",
            "srsearch": query,
            "srlimit": _MAX_RESULTS,
        }
    )
    resp, _ = _web_fetch._fetch(
        f"{_WP_SITE}{_web_fetch._WP_API_PATH}?{q}", headers=_web_fetch._WP_HEADERS
    )
    return [
        {
            "title": h["title"],
            "url": f"{_WP_SITE}/wiki/{quote(h['title'].replace(' ', '_'))}",
            # snippets are HTML: <span class="searchmatch"> around each hit
            "content": unescape(_WP_TAG_RE.sub("", h.get("snippet", ""))).strip(),
        }
        for h in resp.json().get("query", {}).get("search", [])
        if h.get("title")
    ][:_MAX_RESULTS]


def web_research(
    concept: str,
    max_searches: int = _DEFAULT_MAX_SEARCHES,
    tool_progress_callback=None,
) -> str:
    """Run the constrained web-research loop and write one findings note to the
    Inbox. Returns the note's vault-relative path.

    Raises ValueError if the loop produced no findings (sentinel return — no
    note is written).
    """
    messages = [
        {
            "role": "system",
            "content": _RESEARCH_SYSTEM_PROMPT + (_STEER_STEP if _STEERING else ""),
        },
        {"role": "user", "content": concept},
    ]

    # The trace is recorded as it happens, never read back off `messages`.
    # run_agent compacts its own history mid-loop (silica/agent/compaction.py):
    # past the recency floor every `collapse="lazy"` tool result is rewritten
    # *in place* to an elision stub, and web_fetch is lazy and ~7.5k tokens a
    # call, so a handful of fetches is enough to gut what this function reads
    # afterwards — a leaf of stubs and citations with no URLs. ToolCompleteEvent
    # carries the untruncated result and a stable call id, and loop.py emits it
    # before the eager projection and before any later sweep can touch it.
    trace: dict[str, str] = {}
    _reset_turn()  # lanes and the dead-lane counter are per turn

    def _record(event) -> None:
        if isinstance(event, ToolCompleteEvent) and isinstance(event.result, str):
            trace[event.call_id] = event.result
        _harvest_page(event)
        if tool_progress_callback is not None:
            tool_progress_callback(event)

    body = run_agent(
        messages,
        model=CONFIG.model,
        tool_progress_callback=_record,
        constraints=AgentConstraints(
            tools=(
                ("web_search", "web_fetch", "remember", "plan")
                if _STEERING
                else ("web_search", "web_fetch", "remember")
            ),
            max_iterations=max_searches,
        ),
    )

    if not body or body.startswith("(silica:"):
        raise ValueError(
            f"web-research produced no findings for {concept!r} "
            "(loop hit its limit, was cancelled, or all searches failed)."
        )

    if _BANK:
        # Deep runs write past their own context: compaction has stubbed most
        # fetched pages by the time the loop's final message is written. The
        # sectioned rewrite (spec §3.3-3.4) recomposes the note from the banked
        # quotes instead; the one-shot body stays as the §3.6 fallback. Only
        # here — /web answers in direct prose (§5).
        body = _compose_findings(concept, dict(_BANK)) or body

    results = list(trace.values())
    bound, sources, audit = _bind_citations(body, _collect_sources(results), _BANK)
    if _BANK and "## Sources" in bound:
        # The prompt forbids a model-written Sources section when quotes were
        # banked: _bind_citations owns the numbering, and _build_note would keep
        # a hand-written section over the mechanical one. Cut from the heading
        # down; the block below regenerates what the cut removed.
        bound = re.split(r"(?m)^## Sources.*$", bound, maxsplit=1)[0].rstrip()
    provenance = "\n".join(line for line in (_lane_line(), audit) if line)
    note = _build_note(concept, bound, sources, lanes=provenance)
    note_rel = _unique_inbox_path(concept)
    from silica.driver import DRIVER

    DRIVER.create(note_rel, note)
    _write_leaf(note_rel, results, bank=dict(_BANK))
    return note_rel


# The last consented /web turn, waiting for /keep: (question, prose, sources,
# raw trace, provenance line, bank snapshot). Per-process, one deep — a second
# /web overwrites it, /keep clears it. Deliberately no history: a queue of
# unkept answers is a second inbox. Lanes and bank are stashed with the rest
# because _LANES and _BANK have moved on by the time /keep runs.
_LAST_WEB_TURN: (
    tuple[str, str, list[tuple[str, str]], list[str], str, dict[str, _Quote]] | None
) = None


class WebTurn:
    """Trace recorder + mechanical attribution for one `/web` turn.

    Same `_record` pattern as web_research(): ToolCompleteEvent carries the
    untruncated result and a stable call id, and loop.py emits it before
    compaction can rewrite the message it came from — so the citations survive a
    long turn that elides its own history.
    """

    def __init__(self, question: str, inner=None) -> None:
        self.question = question
        self._inner = inner
        self._trace: dict[str, str] = {}
        _reset_turn()  # lanes and the dead-lane counter are per turn

    def __call__(self, event) -> None:
        if isinstance(event, ToolCompleteEvent) and isinstance(event.result, str):
            self._trace[event.call_id] = event.result
        _harvest_page(event)
        if self._inner is not None:
            self._inner(event)

    def attribute(self, answer: str, messages: list[dict]) -> str:
        """Append the trace-built Sources block and stash the turn for /keep.

        The block goes on the returned answer AND on `messages[-1]`, one helper
        doing both so the two can never diverge — the history carries what the
        user saw. Appended regardless of what the model wrote, so a citation can
        be neither forgotten (it does not depend on the model remembering) nor
        fabricated (a URL absent from the trace cannot appear). A `## Sources`
        the model wrote itself is left in place above ours, same posture as
        `force_sources` in _build_note.

        [Qk] markers go through the same _bind_citations pass as the batch
        path, against this block's own ordering — so the numbers the user reads
        point at the lines below them, and a marker with no banked quote is
        removed here too.
        """
        global _LAST_WEB_TURN

        if not answer or answer.startswith("(silica:"):
            return answer  # cancelled or capped: nothing to cite, nothing to keep
        raw = list(self._trace.values())
        answer, sources, audit = _bind_citations(answer, _collect_sources(raw), _BANK)
        lanes = "\n".join(line for line in (_lane_line(), audit) if line)
        _LAST_WEB_TURN = (self.question, answer, sources, raw, lanes, dict(_BANK))
        lines = [f"{i}. {title} — {url}" for i, (url, title) in enumerate(sources, 1)]
        block = "## Sources (web)\n" + ("\n".join(lines) or "(no sources captured)")
        if lanes:
            block = f"{block}\n\n{lanes}"
        out = f"{answer.rstrip()}\n\n{block}"
        if messages and messages[-1].get("role") == "assistant":
            messages[-1]["content"] = out
        return out


def keep_last() -> str:
    """`/keep` — materialize the last `/web` turn as one Inbox note.

    Returns the note's vault-relative path. Raises ValueError when no web turn
    is waiting. `_build_note(force_sources=True)` regenerates the Sources block
    from the stored pairs, which is why the slot holds the model's prose without
    the block appended by WebTurn.attribute — otherwise the note would carry two.

    /nucleate remains the only path from the Inbox into the vault (ADR-0015).
    """
    global _LAST_WEB_TURN

    if _LAST_WEB_TURN is None:
        raise ValueError("nothing to keep: run /web first")
    question, body, sources, raw, lanes, bank = _LAST_WEB_TURN
    note = _build_note(
        question, body, sources, source="web", force_sources=True, lanes=lanes
    )
    note_rel = _unique_inbox_path(question, fallback="web")
    from silica.driver import DRIVER

    DRIVER.create(note_rel, note)
    _write_leaf(note_rel, raw, bank=bank)
    _LAST_WEB_TURN = None
    return note_rel


def _write_leaf(
    note_rel: str, results: list[str], bank: dict[str, _Quote] | None = None
) -> None:
    """Verbatim source leaf beside the findings note (spec-harness-promotion §2).

    web_research bypasses the FSM, so it writes its own leaf: the raw
    web_search and web_fetch tool results the findings were written from, in
    call order. Named after the inbox note's basename, so a later /nucleate of
    that note finds the leaf and links the distilled notes to it at CLEANUP.
    Retrieval-invisible like every sources/ file. Best-effort: a leaf failure
    never loses the note.

    The bank rides ahead of the raw trace: after /nucleate distils the note,
    the [n] markers in its prose remain resolvable to the exact quote and URL
    without re-reading megabytes of page text (spec §3.5).
    """
    try:
        from silica.kernel.recall.paths import SOURCES_DIR

        raw = "\n\n".join(r for r in results if r)
        if not raw:
            return
        if bank:
            lines = ["## Evidence bank", ""]
            for qid, q in bank.items():
                lines += [f"[{qid}] {q.url}", f"> {q.quote}", f"why: {q.why}", ""]
            raw = "\n".join(lines) + "\n" + raw
        from silica.driver import DRIVER

        basename = note_rel.rsplit("/", 1)[-1]
        today = datetime.date.today().isoformat()
        DRIVER.create(
            f"{SOURCES_DIR}/{basename}",
            f"---\ndate: {today}\nsource_id: {basename}\n---\n\n{raw}\n",
        )
    except Exception:
        import logging

        logging.getLogger(__name__).debug(
            "web_research: leaf write skipped (non-fatal)", exc_info=True
        )


# One outline section: a `## ` heading naming the bank IDs it will draw on
# (spec §3.3). Headings without an ID list are skipped rather than failing the
# parse: a section the writer would get no evidence for is not worth a call.
_OUTLINE_RE = re.compile(r"(?m)^##\s+(.+?)\s*\[(Q\d+(?:\s*,\s*Q\d+)*)\]\s*$")


def _parse_outline(text: str) -> list[tuple[str, list[str]]]:
    """(section title, [Qk, ...]) per parseable heading, in outline order."""
    return [
        (m.group(1), re.split(r"\s*,\s*", m.group(2)))
        for m in _OUTLINE_RE.finditer(text)
    ]


_OUTLINE_SYSTEM_PROMPT = """You are organising web-research findings into an \
outline.

You get a research question and an index of banked evidence quotes \
(ID | url | why). Reply with a markdown outline answering the question: one \
`## ` heading per section, each ending with the IDs that section will draw \
on, like:

## How they are trained [Q3, Q7, Q11]
## Where they fail [Q2, Q9]

Use every ID that earned a place; leave out only what proved irrelevant. \
Reply with the outline only — no prose before, between or after the headings."""

_SECTION_SYSTEM_PROMPT = """You are writing one section of a web-research \
findings note.

You get the research question, the full outline for context, the section to \
write now, and the verbatim quotes banked for it. Reply with the section's \
markdown prose only — its heading is added for you. Ground every claim in the \
quotes and cite them inline as [Q3] exactly as named; do not cite IDs you were \
not given, do not write a Sources section, and do not repeat what the outline \
assigns to other sections."""


def _compose_findings(concept: str, bank: dict[str, _Quote]) -> str | None:
    """Outline + per-section writer (spec §3.3-3.4). None means: fall back.

    One tool-less call plans sections from the bank *index* (no quote texts),
    then one tool-less call per section writes prose from only that section's
    quotes — so each section is written inside a small window full of nothing
    but pertinent evidence, instead of the loop's big window full of compaction
    stubs. Costs N+1 LLM calls per run, N = sections. Any failure — unparsable
    outline, a section call raising or coming back empty — returns None and the
    caller keeps the loop's one-shot body (§3.6): no regression possible.
    """
    index = "\n".join(f"{qid} | {q.url} | {q.why}" for qid, q in bank.items())
    try:
        outline_md = (
            call_llm(
                CONFIG.model,
                [
                    {"role": "system", "content": _OUTLINE_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Question: {concept}\n\nEvidence bank:\n{index}",
                    },
                ],
            ).text
            or ""
        )
        sections = [
            (title, known)
            for title, ids in _parse_outline(outline_md)
            if (known := [qid for qid in ids if qid in bank])
        ]
        if not sections:
            return None
        parts = []
        for title, ids in sections:
            quotes = "\n\n".join(
                f"[{qid}] {bank[qid].url}\n> {bank[qid].quote}\nwhy: {bank[qid].why}"
                for qid in ids
            )
            prose = (
                call_llm(
                    CONFIG.model,
                    [
                        {"role": "system", "content": _SECTION_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Question: {concept}\n\nOutline:\n{outline_md}\n\n"
                                f"Write section: {title}\n\nQuotes:\n{quotes}"
                            ),
                        },
                    ],
                ).text
                or ""
            ).strip()
            # The writer sometimes opens by echoing the section heading despite
            # the prompt (measured live), and ours is added mechanically below —
            # drop the echo, keep any other leading heading as prose.
            first, _, rest = prose.partition("\n")
            if re.fullmatch(rf"#+\s+{re.escape(title)}\s*", first):
                prose = rest.strip()
            if not prose:
                return None
            parts.append(f"## {title}\n\n{prose}")
        return "\n\n".join(parts)
    except Exception:
        import logging

        logging.getLogger(__name__).debug(
            "web_research: sectioned composition failed, keeping the one-shot "
            "body (non-fatal)",
            exc_info=True,
        )
        return None


# One [Qn] or [Qn, Qm, ...] inline marker, with the whitespace that leads it —
# captured so a surviving marker keeps its spacing and a removed one takes its
# spacing with it (no "claim ." left behind).
_QREF_RE = re.compile(r"(\s*)\[(Q\d+(?:\s*,\s*Q\d+)*)\]")


def _bind_citations(
    body: str,
    collected: list[tuple[str, str]],
    bank: dict[str, _Quote],
) -> tuple[str, list[tuple[str, str]], str]:
    """Resolve the body's [Qk] markers against the bank (spec §3.5).

    Returns (bound body, ordered sources, audit line). Surviving markers become
    [n], where n is the source's 1-based position in the returned list: cited
    pages first in first-citation order, then the rest of `collected` — the
    ADR-0015 guarantee still lists every page the run opened, the numbering
    just puts the cited ones where the numbers point. Two quotes from one page
    share one number, because citations name sources, not bank rows.

    A marker naming no banked quote is removed and counted on the audit line
    ("" when clean). This is the other half of remember's guarantee: the
    guardian stops fabricated quotes from entering the bank, this stops
    citations of quotes that never did. Pure function — the callers pass a
    snapshot, so /keep can re-render long after _BANK has moved on.
    """
    order: list[str] = []  # cited urls, first-citation order
    phantoms = 0

    def _sub(match: re.Match) -> str:
        nonlocal phantoms
        lead, ids = match.groups()
        numbers: list[int] = []
        for qid in re.split(r"\s*,\s*", ids):
            banked = bank.get(qid)
            if banked is None:
                phantoms += 1
                continue
            if banked.url not in order:
                order.append(banked.url)
            n = order.index(banked.url) + 1
            if n not in numbers:
                numbers.append(n)
        if not numbers:
            return ""
        return f"{lead}[{', '.join(str(n) for n in numbers)}]"

    bound = _QREF_RE.sub(_sub, body)
    # A writer citing the same quote twice in a row ([Q3][Q3], measured live)
    # binds to the same number twice; adjacent duplicates are one citation.
    bound = re.sub(r"(\[\d+(?:, \d+)*\])(?:\s*\1)+", r"\1", bound)
    titles = dict(collected)
    sources = [(u, titles.get(u, u)) for u in order]
    sources += [(u, t) for u, t in collected if u not in order]
    audit = (
        f"Citation audit: {phantoms} marker(s) named no banked quote "
        "and were removed."
        if phantoms
        else ""
    )
    return bound, sources, audit


def _collect_sources(results: list[str]) -> list[tuple[str, str]]:
    """Pull (url, title) pairs from the tool-result trace, deduped, first-seen
    order. These back the ADR-0015 Sources guarantee."""
    seen: dict[str, str] = {}
    for content in results:
        if not content:
            continue
        try:
            items = json.loads(content)
        except (ValueError, TypeError):
            # web_fetch returns prose, not JSON. Its first line names the final
            # URL after redirects, which is what a citation should point at.
            head = content.split("\n", 1)[0]
            if head.startswith("Source: ") and head[8:].strip():
                url = head[8:].strip()
                seen.setdefault(url, url)
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("url"):
                seen.setdefault(it["url"], it.get("title") or it["url"])
    return list(seen.items())


def _build_note(
    concept: str,
    body: str,
    sources: list[tuple[str, str]],
    source: str = "web-research",
    force_sources: bool = False,
    lanes: str = "",
) -> str:
    """Deterministic frontmatter + body + guaranteed ## Sources.

    The date is set here, never trusted to the model. If the body already has a
    ## Sources section it is kept as-is; otherwise we append one from the
    collected trace (ADR-0015: sources are mandatory, not a courtesy).

    `force_sources` appends ours regardless. For /web-search the body is the
    model's own prose — asked NOT to write a Sources section since the bank
    landed, and stripped of one when it disobeys with quotes banked, so the
    kept-if-present branch is a legacy posture there; for /fetch the body is
    the verbatim page, where a ## Sources heading is the *page author's* (every
    markdown README has one) and must not be able to stand in for Silica's."""
    today = datetime.date.today().isoformat()
    front = (
        "---\n"
        f"title: {json.dumps(concept)}\n"
        f"source: {source}\n"
        f"fetched: {today}\n"
        f"tags: [inbox, {source}]\n"
        "---\n"
    )
    out = body.strip()
    if force_sources or "## Sources" not in out:
        lines = [f"{i}. {title} — {url}" for i, (url, title) in enumerate(sources, 1)]
        sources_block = "\n".join(lines) or "(no sources captured)"
        out = f"{out}\n\n## Sources\n{sources_block}"
    if lanes:
        # Under the citations, whether the model wrote them or we appended them:
        # the line qualifies the sources, so it belongs with them.
        out = f"{out}\n\n{lanes}"
    return f"{front}\n{out}\n"


def _unique_inbox_path(concept: str, fallback: str = "web-research") -> str:
    """`<inbox>/<slug>.md`, with a numeric suffix on collision in the Inbox OR
    in sources/. _write_leaf names the leaf after this same basename, so a
    basename that is merely free in the Inbox is not enough: a leaf can
    outlive its note past /nucleate, and reusing that basename would silently
    attach a stale, unrelated leaf to the new note (DRIVER.create raises
    FileExistsError there, which _write_leaf swallows as best-effort). Check
    both up front so the note and its future leaf are always in lockstep.

    `fallback` names the note when the title slugifies to nothing (CJK, emoji,
    pure punctuation), so each command squats only its own namespace."""
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import active_inbox_dir

    inbox = active_inbox_dir() or "Inbox"
    slug = slugify(concept) or fallback
    vault = Path(CONFIG.vault_path)
    basename = f"{slug}.md"
    n = 2
    while (vault / inbox / basename).exists() or (vault / SOURCES_DIR / basename).exists():
        basename = f"{slug} {n}.md"
        n += 1
    return f"{inbox}/{basename}"


def _title_of(text: str) -> str:
    """First real line of the fetched text, skipping our own Source: header.

    On a well-formed page that is the <title>, which html.parser emits first.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("Source: "):
            return line[:120]
    return ""


def fetch_to_inbox(url: str) -> str:
    """`/fetch <url>` — read one page and drop it in the Inbox, verbatim.

    No loop and no model: the fetched text *is* the note body. Same frontmatter,
    same sources/ leaf and the same ADR-0015 boundary as web_research, so
    /nucleate remains the only way into the vault.

    Returns the note's vault-relative path. Raises ValueError when the fetch
    fails or the page yielded nothing readable; no note is written either way.
    """
    if "://" not in url:
        # Humans type bare domains. Inferred here at the user-facing seam only:
        # web_fetch's guard still validates the https form, and agent-issued
        # calls (which always carry a scheme) stay strict.
        url = f"https://{url}"
    text = _web_fetch.web_fetch(url).strip()
    title = _title_of(text)
    if not title:
        raise ValueError(f"nothing readable at {url}")

    note = _build_note(
        title, text, [(url, title)], source="web-fetch", force_sources=True
    )
    note_rel = _unique_inbox_path(title, fallback="web-fetch")
    from silica.driver import DRIVER

    DRIVER.create(note_rel, note)
    _write_leaf(note_rel, [text])
    return note_rel
