# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Which notes of the active vault cover which papers of the Zotero library
(ADR-0066 decisions 4 and 6).

A note covers a paper when it is among the paper's K_NEAREST nearest notes and
passes the collection test for one of the paper's collections: the papers of
that collection meet the note more often than the rest of the library does,
beyond chance (hypergeometric tail, Bonferroni over every (collection, note)
pair with a hit). Nearest alone was measured and refused: 6 of 12 checked
papers land on a hub first, and one note is near 144 of 271 papers.

The test needs the whole library as its null, so one collection costs the
same pass as all of them: one search per paper, 0.47 s each on 2026-10-06. It
runs on demand in a thread and is kept in the vault's index namespace under
the library's version and the vault's version, never in the vault.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from math import comb
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The measured setting (ADR-0066 "Consequences"): changing either changes the
# verdicts, so each is named once, here.
K_NEAREST = 5
QUERY_CHARS = 600
ALPHA = 0.05   # family-wise, split over every (collection, note) pair with a hit

_lock = threading.Lock()
_run: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": "", "vault": "", "unreranked": 0}


def query_of(title: str, abstract: str) -> str:
    """The paper as a search: its title, then its abstract, cut at QUERY_CHARS
    as a whole. The cut is on the joined string, which is what was measured."""
    return f"{title}. {abstract or ''}"[:QUERY_CHARS]


def tail(k: int, K: int, n: int, N: int) -> float:
    """P(X >= k) for X ~ Hypergeometric(N, K, n): the chance that n papers
    drawn from the N searched, K of which have the note among their nearest,
    hit it k times or more. Exact, because N is a library and not a corpus:
    comb() on 271 papers is microseconds."""
    return sum(comb(K, i) * comb(N - K, n - i) for i in range(k, min(K, n) + 1)) / comb(N, n)


def verdicts(hits: dict[str, list[str]], members: dict[str, list[str]],
             names: dict[str, str] | None = None) -> dict:
    """{paper: its nearest notes} and {collection: its papers} -> the covers.

    Pure. N is the papers searched, which are the papers in at least one
    collection, as measured. `covers` maps a paper to [note, collection name,
    k, K] for each pair that passed: k of the collection's papers, of the K
    library papers that have the note among their nearest. A note passing
    for two of a paper's collections is listed under both, because each k/K
    is its own evidence.
    """
    names = names or {}
    N = len(hits)
    everywhere = Counter(x for h in hits.values() for x in h)
    per: dict[str, tuple[int, Counter]] = {}
    for col, papers in members.items():
        inside = [p for p in papers if p in hits]
        per[col] = (len(inside), Counter(x for p in inside for x in hits[p]))
    tests = sum(len(cc) for _, cc in per.values())
    alpha = ALPHA / tests if tests else 0.0
    passed = {(col, x): (k, everywhere[x])
              for col, (n, cc) in per.items() for x, k in cc.items()
              if tail(k, everywhere[x], n, N) < alpha}
    cols_of: dict[str, list[str]] = {}
    for col in sorted(members, key=lambda c: names.get(c, c)):
        for p in members[col]:
            cols_of.setdefault(p, []).append(col)
    covers = {}
    for p, near in hits.items():
        rows = [[x, names.get(col, col), *passed[(col, x)]]
                for x in near for col in cols_of.get(p, []) if (col, x) in passed]
        if rows:
            covers[p] = rows
    return {"covers": covers, "papers": N, "tests": tests, "alpha": alpha}


def library(zot) -> dict:
    """Every paper of the library with the names of its collections, and the
    library's version, read live: membership is never copied into the vault
    (ADR-0055 decision 10). The version is read first, so a library that moves
    during the read is stamped older than its content and reads stale."""
    from silica.kernel.recall.citations import _label_base
    from silica.kernel.text.sanitize import strip_degenerate_runs
    from silica.sources.zotero import _authors_of, _is_paper

    version = int(zot.get("/items/top", limit=1, format="versions")
                  .headers.get("Last-Modified-Version") or 0)
    names = {c["key"]: c["data"]["name"] for c in zot.paged("/collections")}
    papers = []
    for item in zot.paged("/items/top"):
        if not _is_paper(item):
            continue
        data = item.get("data", {})
        # The same cleaning paper_of applies, because the measured queries
        # were built from what paper_of returned.
        title = strip_degenerate_runs(" ".join((data.get("title") or "").split()))
        year = (item.get("meta", {}).get("parsedDate") or "")[:4]
        keys = [c for c in data.get("collections", []) if c in names]
        papers.append({
            "key": item.get("key") or data.get("key", ""),
            "title": title,
            "abstract": strip_degenerate_runs((data.get("abstractNote") or "").strip()),
            "label": _label_base({"authors": _authors_of(data), "year": year, "title": title}),
            "col_keys": keys,
            # Plain code-point order, the order the picker's `.sort()` gives,
            # because the rim's arcs follow the menu (decision 3).
            "collections": sorted({names[c] for c in keys}),
        })
    return {"version": version, "names": names, "papers": papers}


def _cache_path(vault: str) -> Path:
    from silica.kernel.recall.paths import index_dir_for

    return index_dir_for(str(vault)) / "zotero_covers.json"


def cached(vault: str) -> dict | None:
    try:
        return json.loads(_cache_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # no pass yet, or a torn write: either way the next scope runs one


def freshness(cache: dict | None, library_version: int, vault_version: str) -> str:
    """none, fresh or stale. Stale is still drawn while it is redone
    (decision 6): an edit between sessions does not blank the rim."""
    if not cache:
        return "none"
    same = (cache.get("library_version") == library_version
            and cache.get("vault_version") == vault_version
            and cache.get("k") == K_NEAREST and cache.get("chars") == QUERY_CHARS)
    return "fresh" if same else "stale"


def status() -> dict:
    with _lock:
        return dict(_run)


def start(vault: str) -> dict:
    """Start the pass for `vault` unless one is running. Returns status()."""
    with _lock:
        if not _run["running"]:
            _run.update(running=True, done=0, total=0, error="", vault=str(vault), unreranked=0)
            threading.Thread(target=_pass, args=(str(vault),), name="zotero-covers",
                             daemon=True).start()
        return dict(_run)


def _search(query: str, k: int) -> list[str]:
    """The paths of the k nearest notes. This vault only: the memory lane is
    another vault, and the relation drawn is this one's notes (decision 4).

    The embedder is asked first, because the search hides its absence: with
    the dense leg down it answers from co-occurrence alone. On 2026-10-06 the
    embedder stopped 18 papers into a pass, and the 253 lexical searches after
    it cut the covered papers from 86 to 27 with nothing to say so. A verdict
    from another search is not this one, so the pass stops instead. A pool the
    cross-encoder abstained on is counted, not refused: its gate fires on one
    pool of long notes as well as on a reranker that is down."""
    from silica.agent.providers import get_embedder
    from silica.config import CONFIG
    from silica.tools import TOOLS

    try:
        get_embedder(CONFIG).embed([query], query=True)
    except Exception as exc:
        raise RuntimeError(f"the embedder stopped answering ({exc}); a pass on co-occurrence "
                           "alone is not the measured search") from exc
    res = json.loads(TOOLS["silica_semantic_search"].run(query=query, k=k, memory=False))
    if "error" in res:
        raise RuntimeError(res["error"])
    if not res.get("reranked"):
        with _lock:
            _run["unreranked"] += 1
    return [r.get("path") or "" for r in res.get("results", [])]


def _nearest(query: str, resolve) -> list[str]:
    """The K_NEAREST nearest notes, as graph ids. A paper leaf is a paper, not
    one of "my notes", so leaves are dropped, and so is the pipeline's staging:
    an archived segment of a nucleated paper covered 33 of a collection's 37
    papers, under its segment file name, beside the 16 its note covered
    (2026-10-26). A vault with either among the first hits asks once more for
    four times as many, and keeps the first K_NEAREST notes left. The first
    call is the measured k=5 one, so a vault without them runs exactly the
    measured search."""
    from silica.kernel.recall.paths import is_source_leaf, is_staging_path

    for k in (K_NEAREST, 4 * K_NEAREST):
        found = _search(query, k)
        notes: list[str] = []
        for path in found:
            gid = resolve(path) or path
            if gid and not is_source_leaf(gid) and not is_staging_path(gid) and gid not in notes:
                notes.append(gid)
        if len(notes) >= K_NEAREST or len(notes) == len(found):
            break
    return notes[:K_NEAREST]


def _pass(vault: str) -> None:
    from silica.config import CONFIG
    from silica.kernel.recall.mindmap import note_resolver
    from silica.kernel.recall.sync import vault_version
    from silica.sources.zotero import client

    try:
        zot = client()
        lib = library(zot)
        papers = [p for p in lib["papers"] if p["col_keys"]]
        with _lock:
            _run["total"] = len(papers)
        stamp = vault_version()   # before the searches: an edit during them reads stale
        resolve = note_resolver()
        hits: dict[str, list[str]] = {}
        for p in papers:
            hits[p["key"]] = _nearest(query_of(p["title"], p["abstract"]), resolve)
            with _lock:
                _run["done"] += 1
        if (CONFIG.vault_path or "").strip() != vault:
            raise RuntimeError("the vault changed during the pass")
        with _lock:
            unreranked = _run["unreranked"]
        members: dict[str, list[str]] = {}
        for p in papers:
            for ck in p["col_keys"]:
                members.setdefault(ck, []).append(p["key"])
        out = verdicts(hits, members, lib["names"])
        out.update(library_version=lib["version"], vault_version=stamp, k=K_NEAREST,
                   chars=QUERY_CHARS, hits=hits, unreranked=unreranked,
                   at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        path = _cache_path(vault)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out), encoding="utf-8")
        tmp.replace(path)   # a reader never sees half a pass
    except Exception as exc:
        # The pass runs on a thread no request waits on, so a raise would land
        # nowhere: Zotero closed, the embedder down or the vault switched is
        # reported through status(), which the graph's chip reads.
        logger.warning("zotero covers pass failed: %s", exc)
        with _lock:
            done = _run["done"]
            _run["error"] = f"stopped after {done} of {_run['total']} papers: {exc}" \
                if _run["total"] else str(exc) or type(exc).__name__
    finally:
        with _lock:
            _run["running"] = False
