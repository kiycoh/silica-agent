# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`/web-search` — agentic web-research loop → cited findings note in the Inbox.

ADR-0015 staged acquisition: Silica may *fetch* on request but never *decides*
what enters the vault. The loop is constrained to `web_search` (find pages) and
`web_fetch` (read one); it physically cannot write to the vault. Its output is
one findings note in the Inbox, with sources cited. The note enters the vault
only via /nucleate.

Both tools are `sensitive=True` (ADR-0009): the main agent's default toolset
excludes them, so they are reachable only where named explicitly in
AgentConstraints — here, and in fetch_to_inbox() for `/fetch`.

`web_search` needs no key: the default backend scrapes DuckDuckGo's HTML
endpoint with httpx + stdlib html.parser (same posture as web_fetch — no
vendor sees the query). Setting SILICA_TAVILY_API_KEY switches it to Tavily.
"""
from __future__ import annotations

import datetime
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from silica.agent.constraints import AgentConstraints
from silica.agent.events import ToolCompleteEvent
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
_MAX_RESULTS = 5            # ponytail: module constant; per-query result cap
_HTTP_TIMEOUT = 30
# Fetches spend iterations too, so the budget covers both calls. The flag is
# still --max-searches: renaming a user-facing flag buys nothing.
_DEFAULT_MAX_SEARCHES = 16

_RESEARCH_SYSTEM_PROMPT = """You are a focused web-research agent. Given a \
concept, research it on the web and write a findings note.

Method (iterative deepening):
1. Decompose the concept into what you need to know.
2. Call `web_search(query)` for the most important sub-question.
3. When a result looks like it actually answers the question, call \
`web_fetch(url)` and read the page. A search snippet is not the article. One \
fetch of a good source beats three more searches.
4. Identify gaps and adjacent areas of knowledge.
5. Search again only where a gap remains. STOP when you have enough — one \
search if the concept is trivial, up to ~8-10 if it is genuinely complex. Do \
not pad with redundant searches.
6. When done, reply with NO tool call — your final message IS the note body.

The note body must be markdown prose synthesising what you found, with inline \
citations like [1], [2] tied to specific sources, and end with a section:

## Sources
1. <title> — <url>

Do not write YAML frontmatter; it is added for you. Write only the prose and \
the Sources section."""


class WebSearchArgs(BaseModel):
    query: str


@tool(WebSearchArgs, cls="atomic", sensitive=True)
def web_search(query: str) -> str:
    """Search the web for a single query. Returns a JSON list of
    {title, url, content} results. Use iteratively to research a concept."""
    key = (CONFIG.tavily_api_key or "").strip()
    compact = _tavily_search(query, key) if key else _ddg_search(query)
    return json.dumps(compact, ensure_ascii=False)


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
    """Keyless default: scrape DuckDuckGo's HTML endpoint, stdlib parser only.
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
            "challenged); retry later or set SILICA_TAVILY_API_KEY to use Tavily."
        )
    parser = _DDGParser()
    parser.feed(resp.text)
    return [
        {"title": r["title"].strip(), "url": r["url"], "content": r["content"].strip()}
        for r in parser.results
        if r["url"]
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
        {"role": "system", "content": _RESEARCH_SYSTEM_PROMPT},
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

    def _record(event) -> None:
        if isinstance(event, ToolCompleteEvent) and isinstance(event.result, str):
            trace[event.call_id] = event.result
        if tool_progress_callback is not None:
            tool_progress_callback(event)

    body = run_agent(
        messages,
        model=CONFIG.model,
        tool_progress_callback=_record,
        constraints=AgentConstraints(
            tools=("web_search", "web_fetch"), max_iterations=max_searches
        ),
    )

    if not body or body.startswith("(silica:"):
        raise ValueError(
            f"web-research produced no findings for {concept!r} "
            "(loop hit its limit, was cancelled, or all searches failed)."
        )

    results = list(trace.values())
    note = _build_note(concept, body, _collect_sources(results))
    note_rel = _unique_inbox_path(concept)
    from silica.driver import DRIVER

    DRIVER.create(note_rel, note)
    _write_leaf(note_rel, results)
    return note_rel


# The last consented /web turn, waiting for /keep: (question, prose, sources,
# raw trace). Per-process, one deep — a second /web overwrites it, /keep clears
# it. Deliberately no history: a queue of unkept answers is a second inbox.
_LAST_WEB_TURN: tuple[str, str, list[tuple[str, str]], list[str]] | None = None


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

    def __call__(self, event) -> None:
        if isinstance(event, ToolCompleteEvent) and isinstance(event.result, str):
            self._trace[event.call_id] = event.result
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
        """
        global _LAST_WEB_TURN

        if not answer or answer.startswith("(silica:"):
            return answer  # cancelled or capped: nothing to cite, nothing to keep
        raw = list(self._trace.values())
        sources = _collect_sources(raw)
        _LAST_WEB_TURN = (self.question, answer, sources, raw)
        lines = [f"{i}. {title} — {url}" for i, (url, title) in enumerate(sources, 1)]
        block = "## Sources (web)\n" + ("\n".join(lines) or "(no sources captured)")
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
    question, body, sources, raw = _LAST_WEB_TURN
    note = _build_note(question, body, sources, source="web", force_sources=True)
    note_rel = _unique_inbox_path(question, fallback="web")
    from silica.driver import DRIVER

    DRIVER.create(note_rel, note)
    _write_leaf(note_rel, raw)
    _LAST_WEB_TURN = None
    return note_rel


def _write_leaf(note_rel: str, results: list[str]) -> None:
    """Verbatim source leaf beside the findings note (spec-harness-promotion §2).

    web_research bypasses the FSM, so it writes its own leaf: the raw
    web_search and web_fetch tool results the findings were written from, in
    call order. Named after the inbox note's basename, so a later /nucleate of
    that note finds the leaf and links the distilled notes to it at CLEANUP.
    Retrieval-invisible like every sources/ file. Best-effort: a leaf failure
    never loses the note.
    """
    try:
        from silica.kernel.recall.paths import SOURCES_DIR

        raw = "\n\n".join(r for r in results if r)
        if not raw:
            return
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
) -> str:
    """Deterministic frontmatter + body + guaranteed ## Sources.

    The date is set here, never trusted to the model. If the body already has a
    ## Sources section it is kept as-is; otherwise we append one from the
    collected trace (ADR-0015: sources are mandatory, not a courtesy).

    `force_sources` appends ours regardless. For /web-search the body is the
    model's own prose and its ## Sources is the one it was asked to write; for
    /fetch the body is the verbatim page, where a ## Sources heading is the
    *page author's* (every markdown README has one) and must not be able to
    stand in for Silica's."""
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
