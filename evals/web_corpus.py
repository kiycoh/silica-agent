# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Frozen-web corpus: serve recorded pages back through the live web tools.

The L5 lever (docs/specs/web-research-frozen-corpus.md, from the
OpenResearcher teardown): a gate recording's trace already contains every
page the live web served, so serving those pages back through the same two
tools gives a run the agent cannot distinguish from a live one — no content
drift between arms, no search-time contamination, no lane flakiness, and n
grows without touching the web. Backend output parity is the whole design
(OpenResearcher's local corpus and Serper emit byte-identical result pages;
that is what let them synthesize 97K trajectories offline for free).

The corpus freezes the WEB, not the model: the loop's LLM stays live, which
is DR3-Eval's sandbox posture — the system under measurement keeps its
variance, the environment loses all of it.
"""
from __future__ import annotations

import json
import math
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_K1, _B = 1.5, 0.75
_MAX_RESULTS = 5    # parity: silica.sources.web_research._MAX_RESULTS
_SNIPPET_LEN = 180  # parity with OpenResearcher's MAX_SNIPPET_LEN: a SERP
#                     entry costs ~180 chars, the only context budget it has
_TITLE_LEN = 120


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class Corpus:
    """url -> the exact recorded web_fetch result ("Source: <url>\\n\\n<text>").

    fetch serves the recorded string whole — Source header, truncation marker
    and all — and search renders the same {title, url, content} triples
    _ddg_search does. Byte parity is the point: re-extracting or re-truncating
    would make the frozen web observably different from the live one.
    """

    def __init__(self, pages: dict[str, str], conflicts: int = 0):
        self.pages = pages
        self.conflicts = conflicts
        self._tf: dict[str, dict[str, int]] = {}
        df: dict[str, int] = {}
        for url, page in pages.items():
            counts: dict[str, int] = {}
            for t in _tokens(page):
                counts[t] = counts.get(t, 0) + 1
            self._tf[url] = counts
            for t in counts:
                df[t] = df.get(t, 0) + 1
        n = len(pages)
        self._idf = {
            t: math.log((n - d + 0.5) / (d + 0.5) + 1.0) for t, d in df.items()
        }
        total = sum(sum(c.values()) for c in self._tf.values())
        self._avgdl = total / n if n else 0.0

    def search(self, query: str) -> list[dict[str, str]]:
        """Okapi BM25 over the recorded pages, top 5 as _ddg_search triples.

        No overlap -> empty list, exactly what a live lane returns for a query
        the web has nothing for; the model reformulates, it does not crash.
        """
        q = set(_tokens(query))
        scored: list[tuple[float, str]] = []
        for url, counts in self._tf.items():
            dl = sum(counts.values())
            s = 0.0
            for t in q:
                f = counts.get(t)
                if f:
                    s += (self._idf[t] * f * (_K1 + 1)
                          / (f + _K1 * (1 - _B + _B * dl / self._avgdl)))
            if s > 0:
                scored.append((s, url))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self._result(url, q) for _s, url in scored[:_MAX_RESULTS]]

    def _result(self, url: str, q: set[str]) -> dict[str, str]:
        body = self.pages[url].split("\n", 1)[1] if "\n" in self.pages[url] else ""
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        title = (lines[0] if lines else url)[:_TITLE_LEN]
        # The snippet is the line with the most distinct query terms, not a
        # highlighter passage; the corpus is frozen, so this is settled with it.
        pool = lines[1:] or lines
        best = max(pool, key=lambda ln: len(q & set(_tokens(ln))), default="")
        return {"title": title, "url": url, "content": best[:_SNIPPET_LEN]}

    def fetch(self, url: str) -> str:
        page = self.pages.get(url.strip())
        if page is None:
            raise ValueError(
                f"{url} is outside this run's frozen corpus — fetch only URLs "
                "that web_search returned."
            )
        return page


def load(paths: Iterable[str | Path]) -> Corpus:
    """Recording files/dirs -> Corpus. Every `Source:`-headed tool result in a
    trace is a page; search-result JSON and error strings fall through the
    prefix test. First fetch of a URL wins: a later recording carrying
    different text for the same URL is live-web drift between recordings,
    counted in .conflicts so the caller can say so out loud.
    """
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted(p.glob("*.json")) if p.is_dir() else [p]
    pages: dict[str, str] = {}
    conflicts = 0
    for f in files:
        rec = json.loads(f.read_text(encoding="utf-8"))
        for result in rec.get("trace", {}).values():
            if not isinstance(result, str) or not result.startswith("Source: "):
                continue
            url = result.split("\n", 1)[0][len("Source: "):].strip()
            if not url:
                continue
            if url in pages:
                conflicts += pages[url] != result
                continue
            pages[url] = result
    return Corpus(pages, conflicts)


@contextmanager
def install(corpus: Corpus):
    """Swap the two web tools' executors for corpus-backed ones; restore on exit.

    TOOLS[name].fn is the one point every caller passes (loop dispatch, /web,
    record_run). Name, schema and description stay live, so _harvest_page
    still recognises web_fetch events and the remember guardian reads the
    recorded pages exactly as it reads live ones. find_in_page needs no swap:
    it works off _PAGES, which the swapped fetch feeds.
    """
    from silica.tools import TOOLS

    def corpus_search(query: str) -> str:
        return json.dumps(corpus.search(query), ensure_ascii=False)

    real = {name: TOOLS[name].fn for name in ("web_search", "web_fetch")}
    TOOLS["web_search"].fn = corpus_search
    TOOLS["web_fetch"].fn = corpus.fetch
    try:
        yield corpus
    finally:
        for name, fn in real.items():
            TOOLS[name].fn = fn
