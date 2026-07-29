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
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

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
    if not key:
        raise ValueError(
            "web_search requires a TAVILY API key "
            "(set SILICA_TAVILY_API_KEY or TAVILY_API_KEY)."
        )
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
    compact = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        for r in results
        if r.get("url")
    ]
    return json.dumps(compact, ensure_ascii=False)


def web_research(
    concept: str,
    max_searches: int = _DEFAULT_MAX_SEARCHES,
    tool_progress_callback=None,
) -> str:
    """Run the constrained web-research loop and write one findings note to the
    Inbox. Returns the note's vault-relative path.

    Raises ValueError if no TAVILY key is configured (fail fast, no loop) or if
    the loop produced no findings (sentinel return — no note is written).
    """
    if not (CONFIG.tavily_api_key or "").strip():
        raise ValueError(
            "web-search requires a TAVILY API key "
            "(set SILICA_TAVILY_API_KEY or TAVILY_API_KEY)."
        )

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
) -> str:
    """Deterministic frontmatter + model body + guaranteed ## Sources.

    The date is set here, never trusted to the model. If the model already
    wrote a ## Sources section it is kept as-is; otherwise we append one from
    the collected trace (ADR-0015: sources are mandatory, not a courtesy)."""
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
    if "## Sources" not in out:
        lines = [f"{i}. {title} — {url}" for i, (url, title) in enumerate(sources, 1)]
        sources_block = "\n".join(lines) or "(no sources captured)"
        out = f"{out}\n\n## Sources\n{sources_block}"
    return f"{front}\n{out}\n"


def _unique_inbox_path(concept: str) -> str:
    """`<inbox>/<slug>.md`, with a numeric suffix on collision in the Inbox OR
    in sources/. _write_leaf names the leaf after this same basename, so a
    basename that is merely free in the Inbox is not enough: a leaf can
    outlive its note past /nucleate, and reusing that basename would silently
    attach a stale, unrelated leaf to the new note (DRIVER.create raises
    FileExistsError there, which _write_leaf swallows as best-effort). Check
    both up front so the note and its future leaf are always in lockstep."""
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import active_inbox_dir

    inbox = active_inbox_dir() or "Inbox"
    slug = slugify(concept) or "web-research"
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
    text = _web_fetch.web_fetch(url).strip()
    title = _title_of(text)
    if not title:
        raise ValueError(f"nothing readable at {url}")

    note = _build_note(title, text, [(url, title)], source="web-fetch")
    note_rel = _unique_inbox_path(title)
    from silica.driver import DRIVER

    DRIVER.create(note_rel, note)
    _write_leaf(note_rel, [text])
    return note_rel
