# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A selection checked against the web (ADR-0110, amendment of 2026-11-06).

A fixed path and not an agent: the lanes, the first pages, from each page the
window holding most of the claim's words, then `judge()` as it stands. A page is
hostile (ADR-0009), and the judge holds no tools and chooses from a closed list,
so the worst a page can do is a wrong verdict, which point 9 caps at a warning.

The selection is the query. Point 6's step 2, one chat call writing a neutral
question first, lost its own gate on 2026-11-06 (point 15, ORACLE.md): it flagged
10 of 16 F against the selection's 12, at 4.29 s a press against 3.15. A press now
asks no chat model unless Jev answers nothing.

The legs come in as arguments, as `check_note`'s do: `search(query)` is
web_research.search_lanes and `fetch(url)` web_fetch.fetch_page. So this module
holds no turn state, reaches no network of its own, and a test hands in lists.
"""
from __future__ import annotations

import calendar
import datetime
import logging
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from silica.config import live_env
from silica.kernel.check import GRADES
from silica.kernel.check import blocks as blk
from silica.kernel.check.judge import PASSAGE_CHARS
from silica.kernel.check.run import PASSAGE_QUOTE, Judge, overlapping


def is_open() -> bool:
    """Whether the web entry is offered. shortcut: off by default, because its
    gate failed on 2026-11-06 (amendment point 15, ORACLE.md): on B the web backed
    4 of 8 real values where 6 were asked, with the controls, the false sources, the
    quotes and the time all passing. SILICA_CHECK_WEB=1 opens it for a rerun. Reopen
    by a pass on a set that run has not seen, and not by moving the 0.75 on this one."""
    return live_env("SILICA_CHECK_WEB")


logger = logging.getLogger(__name__)

# Point 5: past this a selection is several statements, and one verdict over
# several claims would hide which one failed.
CLAIM_CHARS = 600
TOO_LONG = "select one statement"
# Point 6. A web check of a 40-block opinion would be 40 searches, which is why
# the claim is one selection, and three pages is what one press reads of it.
PAGES = 3
NOTHING_TO_CHECK = "a heading, code or a block under 6 words states nothing to check"
# Without "not checked:", which the box prints from the state before these.
NO_ANSWER = "the web did not answer"
NO_PAGE = "no page spoke to this"
CANCELLED = "the check was cancelled"

# Point 8's table, keyed (web, vault). The vault's own grade never moves: these
# are the lines that stand beside it. Any pair not named takes the plain line.
_SAY = {
    ("disagrees", "supported"): "the vault backs this and the web does not; the source may be wrong or out of date",
    ("supported", "contradicted"): "the web backs your wording, so the source may be the one out of date",
    ("supported", "not found"): "the web backs this and the vault has no source for it",
}
_PLAIN = {"disagrees": "the web does not back this", "supported": "the web backs this"}

Search = Callable[[str], "tuple[list[dict], str]"]
Fetch = Callable[[str], Any]

_WORD = re.compile(r"[^\W_]+")


def aim(text: str, lines: tuple[int, int], claim: str) -> tuple[str, list[blk.Block]]:
    """(the claim, the blocks it stands in), or ("", []) when the range touches
    nothing checkable. Under MIN_WORDS it widens to its block, because a
    fragment states nothing (point 5). ValueError past CLAIM_CHARS, widened or
    not, and that is the route's 400."""
    claim = claim.strip()
    if len(claim) > CLAIM_CHARS:
        raise ValueError(TOO_LONG)
    hit = overlapping(blk.split(text)[0], lines)
    if not hit:
        return "", []
    if blk.Block(0, "paragraph", claim).words < blk.MIN_WORDS:
        claim = hit[0].text
        if len(claim) > CLAIM_CHARS:
            raise ValueError(TOO_LONG)
    return claim, hit


def _words(text: str) -> set[str]:
    """What a page is searched for: words long enough to say something, and
    every number, which is where these claims disagree with a page."""
    return {w for w in map(str.casefold, _WORD.findall(text)) if len(w) >= 4 or w.isdigit()}


def window(text: str, claim: str, width: int) -> str:
    """The `width` characters of `text` holding most of the claim's distinct
    words, from the start of the sentence the first of them stands in. The head
    of the page when none of them is there: an Italian claim shares no word with
    an English page, and the judge reads across languages."""
    want = _words(claim)
    hits = [(m.start(), w) for m in _WORD.finditer(text) if (w := m.group().casefold()) in want]
    held: Counter[str] = Counter()
    best = start = first = 0
    for at, word in hits:
        held[word] += 1
        # Three quarters: the last quarter is what the walk back to the
        # sentence's start may spend, and it must not push the words back out.
        while at - hits[first][0] >= width * 3 // 4:
            gone = hits[first][1]
            first += 1
            held[gone] -= 1
            if not held[gone]:
                del held[gone]
        if len(held) > best:
            best, start = len(held), hits[first][0]
    floor = max(0, start - width // 4)
    cut = max(text.rfind("\n", floor, start), text.rfind(". ", floor, start))
    return text[cut + 1 if cut >= 0 else start:][:width].strip()


def quote(passage: str, claim: str) -> str:
    """The words a row shows: the page's own, whitespace folded, cut at a word.
    Nothing is rewritten, so what the box quotes is on the page (point 15): the
    judge names a passage, and the words come from it mechanically."""
    flat = " ".join(window(passage, claim, PASSAGE_QUOTE).split())
    if len(" ".join(passage.split())) <= len(flat):
        return flat
    return flat[:PASSAGE_QUOTE - 1].rsplit(" ", 1)[0] + "…"


def _page_text(page: Any) -> str:
    """A fetched page's words, past the `Source:` line fetch_page puts on top:
    its address holds the claim's words as often as its body does."""
    return re.sub(r"\ASource: \S*\s*", "", page.text)


def _read(hits: list[dict], fetch: Fetch, claim: str) -> list[dict]:
    """The first PAGES of `hits` that open, each as one passage. All of them are
    fetched at once, so a 403 at rank 2 does not leave the judge with two pages
    while a fourth was there: 89 of the 480 pages the gate's presses asked for
    refused (2026-11-06). A failed fetch is one page fewer, never a failed check.
    shortcut: a press waits for its slowest page, at most fetch_page's 30 s, or
    120 s for a YouTube hit on a laptop; none of the gate's 391 fetches passed 8 s,
    so add a deadline when a press's p90 passes 20 s."""
    def one(url: str) -> dict | None:
        try:
            text = _page_text(fetch(url))
        except Exception as exc:
            logger.debug("check: %s not read: %s", url, type(exc).__name__)
            return None
        return {"path": url, "line": 0, "text": window(text, claim, PASSAGE_CHARS)} if text.strip() else None

    urls = list(dict.fromkeys(h["url"] for h in hits if h.get("url")))
    if not urls:
        return []
    with ThreadPoolExecutor(len(urls), thread_name_prefix="check-web") as pool:
        return [p for p in pool.map(one, urls) if p][:PAGES]


def check_claim(claim: str, *, search: Search, fetch: Fetch, judge: Judge,
                step: Callable[[dict], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> dict:
    """One press. {"web": "supported" | "disagrees", "url", "passage", "lane", ...},
    or {"web": "", "say": why not}; never raises, since the vault's verdict
    stands whatever the web does (point 14). `step` gets the two events the box
    shows while it waits; `cancelled` is polled before each paid or slow call."""
    def stop() -> bool:
        return cancelled is not None and cancelled()

    tell = step or (lambda ev: None)
    tell({"step": "asking"})
    if stop():
        return {"web": "", "say": CANCELLED}
    try:
        hits, lane = search(claim)
    except Exception as exc:
        # Every lane down, whatever it raised (httpx's own on no network, a
        # ValueError on a challenge): the vault's verdict stands.
        logger.info("check: no lane answered: %s", type(exc).__name__)
        return {"web": "", "say": NO_ANSWER}
    tell({"step": "reading", "pages": min(PAGES, len(hits))})
    if not hits:
        return {"web": "", "say": NO_PAGE, "lane": lane}
    passages = _read(hits, fetch, claim)
    if not passages:
        return {"web": "", "say": NO_ANSWER, "lane": lane}
    if stop():
        return {"web": "", "say": CANCELLED, "lane": lane}
    try:
        verdict = judge(blk.Block(0, "paragraph", claim).clean, passages) or {}
    except Exception as exc:
        # A reply the judge's own parsing did not hold: one more block not
        # checked, and the vault's verdict, already on screen, is not lost to it.
        logger.warning("check: the judge failed on the web's passages: %s", type(exc).__name__)
        verdict = {}
    # by_jev's "not checked" keeps the kind it was unsure of, so the state is
    # read first. A verdict naming no page has nothing to show, and every row
    # carries its host (point 10).
    said = "" if verdict.get("state") else verdict.get("kind", "")
    web = {"supported": "supported", "contradicted": "disagrees", "overstated": "disagrees"}.get(said, "")
    rest = str(verdict.get("at") or "").rsplit(":", 1)[0]
    page = next((p for p in passages if p["path"] == rest), None)
    if not web or page is None:
        return {"web": "", "say": NO_PAGE, "lane": lane, "pages": [p["path"] for p in passages]}
    return {"web": web, "url": page["path"], "passage": quote(page["text"], claim), "lane": lane,
            "pages": [p["path"] for p in passages]}


def vault_says(report: dict) -> str:
    """The vault's verdict on the range, as point 8's table reads it: "contradicted",
    "not found", "supported" or "". The worst block's, when a selection crosses two."""
    kinds = {f["kind"] for f in report["findings"]}
    if kinds & {"contradicted", "overstated"}:
        return "contradicted"
    if kinds & {"not found", "off-point"}:
        return "not found"
    return "supported" if any(s["state"] == "supported" for s in report["states"]) else ""


def rows(result: dict, claim: str, vault: str, line: int, end: int,
         today: datetime.date | None = None) -> tuple[list[dict], list[dict]]:
    """One press as (findings, states), for the `web_verdict` event. `model` as
    a judge's finding has it: the drawer turns the mark hollow on an edit."""
    web = result["web"]
    if not web:
        return [], [{"state": "not checked", "line": line, "end": end, "say": result["say"]}]
    d = today or datetime.date.today()
    row = {"line": line, "end": end, "say": _SAY.get((web, vault), _PLAIN[web]), "passage": result["passage"],
           "host": (urlsplit(result["url"]).hostname or "").removeprefix("www."),
           "read": f"{d.day} {calendar.month_abbr[d.month]}", "url": result["url"]}
    if web == "supported":
        return [], [{"state": "web", **row}]
    return [{"grade": GRADES["web disagrees"], "kind": "web disagrees", "at": "", "mark": claim, "model": True,
             **row}], []
