# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Explore's reads: the graph, one note's context, concepts, comparisons and the tree."""
from __future__ import annotations

import html as _html
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from silica.config import CONFIG
from silica.kernel.graph.mindmap import note_resolver
from silica.kernel.report import brief, note_context
from silica.kernel.text import note_parts
from silica.ui.web import file_tree, render
from silica.ui.web.failures import explain as _failure

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/graph")
def graph(request: Request):
    """The explore iframe's whole document, force-graph bundles inlined.

    That inlining is what makes this route ~6 MB, and it used to ship
    uncompressed with no validator at all, so every visit to the explore tab
    refetched every byte. Two cheap fixes, in this order:

      * an ETag over the payload, so a revisit costs a 304 and nothing else.
        Content-addressed rather than time-based, because the document changes
        when the vault does and no clock knows when that was.
      * gzip when the client asks for it, which measured 6,229,645 -> 766,371
        bytes on a 1,199-note vault. Done here rather than with GZipMiddleware,
        which would also wrap the SSE turn stream and stall it.

    no-cache, not no-store: the browser must revalidate (the vault changes under
    it) but is allowed to keep the bytes and take the 304.
    """
    import gzip as _gzip
    import hashlib
    import tempfile

    from silica.tools.graph import graph_export

    # Per-request directory, not a fixed name in /tmp: the route is a sync def,
    # so two overlapping requests ran in the threadpool and shared one file — and
    # a world-writable path anyone can pre-create as a symlink is not somewhere
    # to write a document we then serve back.
    try:
        with tempfile.TemporaryDirectory(prefix="silica-graph-") as tmp:
            out = Path(tmp) / "graph.html"
            graph_export(output_path=str(out), folder="")
            body = out.read_text(encoding="utf-8").encode("utf-8")
    except Exception as exc:
        return HTMLResponse(
            f"<p style='font-family:monospace'>graph unavailable: {_html.escape(_failure(exc)['error'])}"
            f"<br><small>{_html.escape(_failure(exc)['detail'])}</small></p>"
        )

    etag = '"' + hashlib.blake2b(body, digest_size=8).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        body = _gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(body, media_type="text/html; charset=utf-8", headers=headers)


# A note lights its degree over the layers on show: 86 at most on the 730-note
# test vault. The bound is for a body no frame would send, since the placement
# checks each note against every one placed before it.
_MAP_LAYOUT_MAX = 2000


@router.post("/map/layout")
def map_layout(payload: dict):
    """The graph frame's lit set, arranged as the Map (ADR-0088).

    The frame sends what it lit and the community each dot is painted with, so
    the set is the graph's and a wedge cannot disagree with its dots; only the
    geometry is computed here, by the builder the .canvas export uses. POST
    because a hub lights dozens of paths; guarded like a write because it reads
    the vault on another page's say-so.
    """
    from silica.kernel.graph.mindmap import arrange_in_vault

    root, ids, groups = payload.get("root"), payload.get("ids"), payload.get("groups") or {}
    if not isinstance(root, str) or not isinstance(ids, list) or not isinstance(groups, dict):
        raise HTTPException(400, "expected {root: str, ids: [str], groups: {id: int}}")
    community_of = {k: v for k, v in groups.items()
                    if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}
    lit = [i for i in ids if isinstance(i, str)][:_MAP_LAYOUT_MAX]
    return arrange_in_vault(root, lit, community_of)


@router.get("/link")
def link_written(request: Request):
    """Where a clicked link is written (ADR-0152): the note, the line, its words.

    A wikilink edge is one per pair, so the line may be in either end. Only
    keys in the vault index resolve, which keeps a path out of the vault out of
    the read, as /note does. Asked per click and not shipped on the export:
    1,263 lines on Cultura would ride every graph for one look.
    """
    from silica.kernel.graph.mindmap import note_resolver

    resolve = note_resolver()
    a, b = (resolve(request.query_params.get(k, "")) for k in ("from", "to"))
    if not a or not b:
        return {"path": "", "line": 0, "text": ""}
    for src, target in ((a, b), (b, a)):
        line, text = note_context.link_line(src, target)
        if line:
            return {"path": src, "line": line, "text": text}
    return {"path": "", "line": 0, "text": ""}


@router.get("/path")
def prereq_path(note: str = ""):
    """The prerequisite ladder around one note (V2, RefD): read-order space.

    Flow reads this (ADR-0152), because the other surfaces cannot show
    DIRECTION: the graph and the Map lay notes out by how they connect, Clusters
    by area, Folders by where they are filed. All are undirected, and "what do
    I read before this" has no answer in any of them.

    Empty `note` returns the landing instead of an error, like /map: the notes
    that root the biggest ladders, which is the one list that cannot be got
    from the file tree.
    """
    from silica.kernel.graph.mindmap import note_resolver
    from silica.kernel.report.structure import ladder, structure_map

    try:
        m = structure_map()
    except Exception as exc:
        logger.warning("path: structure map failed (%s)", exc)
        return _failure(exc)
    if not m.prereq:
        return {
            "picks": [],
            # The one thing that leaves Flow empty, said by Flow itself rather
            # than as a bare graph: RefD needs the co-occurrence
            # index, and a vault that never built one has no reading order to
            # show, not a reading order of length zero.
            "hint": "No reading order yet: the prerequisite direction is derived from the "
                    "co-occurrence index. Run /report to build it.",
        }

    if not note.strip():
        # Ranked by how much of a ladder each note actually roots. Computing the
        # real ladder for every candidate would be O(V) walks, so the rank is
        # the cheap proxy (own prerequisites + own dependents) and the top slice
        # is then measured for real, which is what the count on the row says.
        rough = sorted(
            m.prereq.keys() | m.unlocks.keys(),
            key=lambda k: (-(len(m.prereq.get(k, ())) + len(m.unlocks.get(k, ()))), k),
        )[:40]
        picks: list[dict[str, Any]] = []
        for k in rough:
            l = ladder(k + ".md")
            if len(l["nodes"]) < 2:
                continue
            picks.append({
                "name": note_parts.clean_name(k), "path": k + ".md",
                "notes": len(l["nodes"]),
                "before": sum(1 for n in l["nodes"] if n["depth"] < 0),
                "after": sum(1 for n in l["nodes"] if n["depth"] > 0),
            })
        # A note with notes on BOTH sides is the instructive root: it is the one
        # whose ladder shows an order rather than a fan. Ranking by size alone
        # put the vault's biggest hub first every time, and its ladder is one
        # rung of 59 dependents, which is a list with a title.
        picks.sort(key=lambda r: (-min(r["before"], r["after"]), -r["notes"], r["name"]))
        return {"picks": picks[:12], "hint": ""}

    # `ladder` resolves its own root and hands the graph id back, so the two
    # keyspaces (graph ids carry `.md`, RefD works without it) are bridged once,
    # inside the kernel, and never here.
    resolve = note_resolver()
    l = ladder(resolve(note) or note)
    canon = l["root"]
    if not canon:
        return {"error": f"'{note}' not found in vault."}
    if not l["nodes"]:
        return {
            "root": note_context.row(canon), "levels": [], "edges": [], "cycles": 0, "truncated": False,
            "hint": "nothing reads before or after this note: RefD found no direction "
                    "between it and its neighbours.",
        }
    by_depth: dict[int, list[dict]] = {}
    for n in l["nodes"]:
        by_depth.setdefault(n["depth"], []).append(
            {**note_context.row(n["path"]), "cyclic": n["cyclic"], "root": n["path"] == canon}
        )
    levels = [
        {"depth": d, "notes": sorted(by_depth[d], key=lambda r: r["name"])}
        for d in sorted(by_depth)
    ]
    return {
        "root": note_context.row(canon), "levels": levels, "edges": l["edges"],
        "cycles": l["cycles"], "truncated": l["truncated"], "hint": "",
    }


@router.get("/find")
def find(q: str = "", k: int = 5):
    """Direct semantic-search panel: calls the tool straight, same pattern as /graph and /map."""
    # The core and not the tool, whose payload drops `score` for the model
    # (graph._without_score): this panel shows it to the person.
    from silica.kernel.recall.search import facade_search

    q = q.strip()
    if not q:
        return HTMLResponse("<p style='font-family:monospace;color:#8a93a3'>usage: /find &lt;query&gt; [--k=N]</p>")
    try:
        parsed = facade_search(q, k=k)
    except Exception as exc:
        return HTMLResponse(
            f"<p style='font-family:monospace'>find unavailable: {_html.escape(_failure(exc)['error'])}"
            f"<br><small>{_html.escape(_failure(exc)['detail'])}</small></p>"
        )
    if "error" in parsed:
        return HTMLResponse(f"<p style='font-family:monospace;color:#8a93a3'>{_html.escape(parsed['error'])}</p>")
    results = parsed.get("results", [])
    if not results:
        return HTMLResponse(f"<p style='font-family:monospace;color:#8a93a3'>no results for '{_html.escape(q)}'.</p>")
    rows = []
    for r in results:
        p = r.get("path") or r.get("name") or "?"
        rows.append(
            f'<div class="find-result">{render.anchor(p, note_parts.clean_name(p))}'
            f'<span class="find-score">{r.get("score", 0.0):.3f}</span></div>'
        )
    return HTMLResponse("".join(rows))


def _note_concepts(target: str, k: int = 18) -> list[dict]:
    from silica.tools.graph import silica_concepts

    try:
        return silica_concepts(note=target, k=k).get("concepts") or []
    except Exception:
        logger.debug("context: concepts failed for %s", target, exc_info=True)
        return []


def _ghost_context(name: str) -> dict:
    """An unresolved wikilink as a subject of its own.

    Today a ghost node carries path "" (graph_export.py), so clicking one used to
    post an empty path and open nothing. It has no body to read and no reader
    mode — what it does have is a name, the notes that invoke it, and their
    merged concepts, which is exactly the material for deciding whether to write
    it.
    """
    from collections import Counter

    stem = note_parts.clean_name(name).lower()
    invokers = sorted({
        link.source.path for link in note_context.unresolved_links()
        if note_parts.clean_name(link.target).lower() == stem and link.source.path
    })
    merged: Counter = Counter()
    for src in invokers[:12]:  # decided cap: a 12-note cloud is already dense
        for c in _note_concepts(src, k=12):
            merged[c["concept"]] += c.get("weight", 1)
    return {
        "title": note_parts.clean_name(name),
        "path": "",
        "ghost": True,
        "snippets": [],
        "concepts": [{"concept": c, "weight": w} for c, w in merged.most_common(18)],
        "related": {"frontmatter": [], "outgoing": [], "backlinks": [note_context.row(p) for p in invokers]},
        "suggested": [],
        "hint": "",
    }


# --- context explorer (GET /context) -----------------------------------------
# One blocking call, all of it deterministic and LLM-free. Measured on a
# 718-note vault, warm: related 0.01s, concepts(note=) 0.06s, outline/links/
# unresolved 0.00s. The first call in a fresh process pays ~0.9s to load the
# co-occurrence store — once, in a long-lived server. So: no progressive fill,
# no client-side hybrid, one endpoint that returns the whole drawer.

@router.get("/context")
def context(path: str = "", name: str = "", ghost: bool = False):
    """Everything deterministic the vault knows about one note, in one call.

    Sections: key snippets (what it says), concepts (what it is about), related
    (how it IS connected), suggested (how it SHOULD be). Zero LLM calls — every
    number here is index lookup, so the drawer is a read, not a turn. Graceful
    on miss, like /note: never 500.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef
    from silica.tools.graph import silica_related

    if ghost or (not path and name):
        return _ghost_context(name or path)

    resolve = note_resolver()
    canon = resolve(path)
    if not canon:
        return {"title": path, "path": path, "ghost": False, "error": "note not found in vault."}

    driver = get_driver()
    try:
        content = driver.read_note(NoteRef(name=note_parts.clean_name(canon), path=canon)).content
    except Exception:
        content = ""  # the drawer opens on what the graph knows, without the body
    props, body = note_parts.split_frontmatter(content)

    def _refs(fn) -> list[dict]:
        # Resolved only. DRIVER.links() also returns a synthesised ref for every
        # UNRESOLVED wikilink (path "<Target>.md", a file that does not exist),
        # and listing those here would put a dead row under "how it IS
        # connected" — they belong under suggested, as links worth writing.
        try:
            return [note_context.row(p) for r in fn(canon) if (p := resolve(r.path or ""))]
        except Exception:
            logger.debug("context: %s failed for %s", fn.__name__, canon, exc_info=True)
            return []

    outgoing = _refs(driver.links)
    backlinks = _refs(driver.backlinks)

    # frontmatter `related:` is a hand-written claim, so it is shown as written
    # and resolved only for the click target; an unresolvable entry still lists.
    # The hand writing it is usually Obsidian's, which writes `- "[[B]]"` — and
    # neither resolve() nor clean_name() sees through the brackets, so every
    # such entry used to come back named "[[B]]" with an empty path: a dead row
    # under a heading that promises a connection. fm_ref splits the two jobs.
    fm_raw = (props or {}).get("related") or []
    if not isinstance(fm_raw, (list, tuple)):
        fm_raw = [fm_raw]
    frontmatter = []
    for v in fm_raw:
        if not v:
            continue
        target, display = note_context.fm_ref(str(v))
        frontmatter.append({"name": display, "path": resolve(target) or ""})

    try:
        rel_out = silica_related(note=canon, k=12)
    except Exception:
        logger.debug("context: related failed for %s", canon, exc_info=True)
        rel_out = {}
    rel = rel_out.get("results") or []

    linked = {r["path"] for r in outgoing} | {r["path"] for r in backlinks}
    # Whether the semantic leg contributed is READ OFF the ranking's own
    # provenance — every result names the metric that proposed it (embed:0.83,
    # cooccur:w9, edge:0.57). Asking the embed store directly would reach past
    # the relatedness facade for a fact the facade already reports.
    embed_ran = any(
        str(e).startswith("embed:") for r in rel for e in (r.get("evidence") or [])
    )

    # A Zotero paper's leaf carries its identity (ADR-0055): the panel heads it
    # with the paper's title, not the citekey stem, and adds its citations.
    paper = None
    if props and props.get("zotero_key"):
        from silica.ui.web.zotero_web import paper_context
        try:
            paper = paper_context(canon, props, backlinks)
        except Exception:
            logger.debug("context: paper block failed for %s", canon, exc_info=True)
    return {
        **({"paper": paper} if paper else {}),
        "title": str(props.get("title") or "") if paper and props and props.get("title") else note_parts.clean_name(canon),
        "path": canon,
        "ghost": False,
        # A paper's text before its first section is the byline, not a claim.
        "snippets": [s for s in note_context.key_snippets(body) if s["heading"] or not paper],
        "concepts": _note_concepts(canon),
        "related": {"frontmatter": frontmatter, "outgoing": outgoing, "backlinks": backlinks},
        "suggested": note_context.suggested(canon, rel, linked, resolve),
        "structure": note_context.note_structure(canon),
        "ladder": note_context.note_ladder(canon),
        # Without embeddings `related` ranks on co-occurrence alone, and the
        # section looks thin for a reason the reader cannot see from here.
        # silica_related's own hint wins when it has one — it knows more about
        # why it came back empty than an inference from the evidence can.
        "hint": rel_out.get("hint") or ("" if embed_ran else
                "no embedding index — relatedness is co-occurrence only "
                "(run /embed to add the semantic half)"),
    }


@router.get("/concept")
def concept(term: str = "", k: int = 20):
    """The notes that carry one concept, with paths resolved back to graph keys
    so the ids match the nodes a viewer holds. The page stopped asking this when
    the drawer moved to /concepts; the route stays while a path is a contract."""
    from silica.tools.graph import silica_concepts

    resolve = note_resolver()
    try:
        res = silica_concepts(term=term, k=k)
    except Exception as exc:
        logger.debug("concept: lookup failed for %s", term, exc_info=True)
        return {"term": term, "notes": [], **_failure(exc)}
    return {
        "term": term,
        "concept": res.get("concept") or term,
        "notes": [
            p for n in (res.get("notes") or [])
            if (p := resolve(n.get("path", "")) or n.get("path", ""))
        ],
    }


@router.get("/concepts")
def concepts(terms: str = ""):
    """The notes that carry EVERY listed concept, with the lines that carry each.

    The co-occurrence store knows which notes hold a stem and nothing about
    where; the driver's search knows lines and nothing about stems. Joined
    here and not in the store, because positions would triple its size for
    one reader. Each term is searched as typed and as its canonical label,
    which covers the two spellings a pill can show and misses a third
    ("feedback" and "retroazione" are one stem and two searches).
    """
    from silica.driver import get_driver
    from silica.tools.graph import silica_concepts

    words = [t.strip() for t in terms.split(",") if t.strip()][:6]
    resolve = note_resolver()
    sets: list[set[str]] = []
    label: dict[str, str] = {}
    for w in words:
        try:
            # k bounds the notes returned; the intersection needs all of them.
            res = silica_concepts(term=w, k=100_000)
        except Exception as exc:
            logger.debug("concepts: lookup failed for %s", w, exc_info=True)
            return {"terms": [], "notes": [], **_failure(exc)}
        label[w] = res.get("concept") or w
        sets.append({p for n in (res.get("notes") or [])
                     if (p := resolve(n.get("path", "")) or n.get("path", ""))})
    common = set.intersection(*sets) if sets else set()
    passages: dict[str, list[dict]] = {}
    if common:
        queries = {q: w for w in words for q in (w, label[w])}
        try:
            hits = get_driver().search_context_batch(sorted(queries))
        except Exception:
            # No lines is a poorer answer, not a wrong one: the notes still light.
            logger.debug("concepts: search failed", exc_info=True)
            hits = {}
        # One entry per line, naming every term it carries: the line that holds
        # two of them together is the one the reader came for, not a duplicate.
        by_line: dict[tuple[str, int], dict] = {}
        for q, hs in hits.items():
            for h in hs:
                path = resolve(h.ref.path) or h.ref.path
                text = note_context.plain(note_context.LIST_OR_QUOTE.sub("", h.snippet))[:240]
                # A one-token line is a tag or a frontmatter entry, not a sentence
                # ("- classificazione-statistica" carried half the hits on a
                # statistics note): the note still counts, the line is not quoted.
                if path not in common or " " not in text.strip():
                    continue
                row = by_line.setdefault((path, h.line), {"line": h.line, "text": text, "terms": []})
                if queries[q] not in row["terms"]:
                    row["terms"].append(queries[q])
        for (path, _line), row in by_line.items():
            passages.setdefault(path, []).append(row)
    notes = [{"path": p, "name": note_parts.clean_name(p),
              "passages": sorted(passages.get(p, []), key=lambda x: x["line"])[:8]}
             for p in common]
    notes.sort(key=lambda n: (-len(n["passages"]), n["name"]))
    return {"terms": [{"term": w, "concept": label[w], "notes": len(st)} for w, st in zip(words, sets)],
            "notes": notes}


def _select(nodes: list[dict], sel: str, edges: list | None = None
            ) -> tuple[set[str], str] | None:
    """`folder:<prefix>` | `area:<id>` | `collection:<name>` |
    `collections:<json list of names>` -> (note ids, label), or None.
    `collections:` with no list is every collection (ADR-0066 decision 5)."""
    kind, _, rest = sel.partition(":")
    real = [n for n in nodes if n.get("type") != "ghost"]
    if kind == "collections":
        try:
            names = set(json.loads(rest)) if rest else None
        except ValueError:
            return None
        papers = {n["id"] for n in real
                  if any(names is None or c in names
                         for c in ((n.get("paper") or {}).get("collections") or []))}
        notes = {e["from"] for e in (edges or [])
                 if e.get("type") == "EXTRACTED" and e.get("to") in papers}
        return papers | notes, ("all collections" if names is None else f"{len(names)} collections")
    if kind == "collection" and rest:
        # A Zotero collection (ADR-0055) is its paper leaves plus the notes that
        # cite them: a leaf links to nothing, so leaves alone could never meet.
        papers = {n["id"] for n in real
                  if rest in ((n.get("paper") or {}).get("collections") or [])}
        notes = {e["from"] for e in (edges or [])
                 if e.get("type") == "EXTRACTED" and e.get("to") in papers}
        return papers | notes, rest
    if kind == "folder" and rest:
        pre = rest if rest.endswith("/") else rest + "/"
        return {n["id"] for n in real if n["id"].startswith(pre)}, pre
    if kind == "area" and rest.lstrip("-").isdigit():
        from silica.kernel.graph.graph_export import area_hubs, area_tally

        gid = int(rest)
        ids = {n["id"] for n in real if n.get("group", -1) == gid}
        hub = area_hubs(area_tally(real, [])).get(gid, "")
        return ids, note_parts.clean_name(hub) if hub else f"area {gid}"
    return None


@router.get("/compare")
def compare(a: str = "", b: str = ""):
    """Two parts of the vault side by side (compare_sets): what characterises
    each, and the wikilinks and k-NN pairs that put them in touch, each link
    with the sentence that carries it. Selectors are the scope picker's:
    folder:<prefix>, area:<id>, collection:<name>."""
    from silica.config import CONFIG
    from silica.kernel.recall.compare import compare_sets
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.graph.graph_export import build_graph_data, detect_communities, knn_edges

    try:
        nodes, edges = build_graph_data(folder="")
        detect_communities(nodes, edges)
    except Exception as exc:
        return _failure(exc)
    sim: list[dict] = []
    try:
        sim = knn_edges(nodes, k=6)
    except Exception:
        logger.debug("compare: semantic layer unavailable", exc_info=True)
    if a.startswith("collection") or b.startswith("collection"):
        from silica.sources.citations import paper_layer
        from silica.ui.web.graph_view import _stamp_collections

        paper_layer(nodes)
        _stamp_collections(nodes)
    A, B = _select(nodes, a, edges), _select(nodes, b, edges)
    if not A or not B:
        return {"error": "selectors are folder:<prefix>, area:<id> or collection:<name>",
                "a": a, "b": b}
    store = None
    try:
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        if len(store) == 0:
            store = None
    except Exception:
        logger.debug("compare: co-occurrence store unavailable", exc_info=True)
    real_n = sum(1 for n in nodes if n.get("type") != "ghost")
    out = compare_sets(A[0], B[0], store=store, vault_size=real_n, nodes=nodes, edges=edges, sim=sim)
    end = lambda p: {"name": note_parts.clean_name(p), "path": p}  # noqa: E731

    def scaffold(x: str, y: str) -> bool:
        return any(e.get("scaffold") and {e.get("from"), e.get("to")} == {x, y} for e in edges)

    return {
        "a": {"selector": a, "label": A[1], "size": out["a"]["size"], "terms": out["a"]["terms"], "paths": sorted(A[0])},
        "b": {"selector": b, "label": B[1], "size": out["b"]["size"], "terms": out["b"]["terms"], "paths": sorted(B[0])},
        "links": [{"a": end(x), "b": end(y), "passage": "" if scaffold(x, y) else note_context.bridge_passage(x, y) or note_context.bridge_passage(y, x)}
                  for x, y in out["links"]],
        "knn": [{"a": end(x), "b": end(y)} for x, y in out["knn"]],
    }


def _tree_raws() -> dict[str, list[str]]:
    """The raws the rail lists where they sit (ADR-0129, amended 2026-11-04),
    each with the notes naming it, from the notes' `source_file` and never
    from the disk: an embedded image or a figure Silica extracted is no raw."""
    from silica.kernel import raws
    from silica.kernel.base.formats import DOC_EXTS

    if not CONFIG.vault_path:
        return {}
    return {r: n for r, n in raws.index(Path(CONFIG.vault_path)).items()
            if Path(r).suffix.lower() in DOC_EXTS}


@router.get("/vault_info")
def vault_info():
    """Sidebar data: vault stats + file tree, from the same builders as the
    graph view so the numbers can't disagree between the two surfaces."""
    from silica.kernel.check.web import is_open as check_web_open
    from silica.kernel.publish import never_dirs
    from silica.kernel.graph.graph_export import build_graph_data

    try:
        nodes, edges = build_graph_data(folder="")
    except Exception as exc:
        return _failure(exc)
    return {
        # `path` so the header label follows a `/vault <dir>` switch mid-session:
        # this endpoint already re-runs after every turn, the boot-time header
        # never did.
        "path": CONFIG.vault_path or "",
        "unresolved": sum(1 for n in nodes if n.get("type") == "ghost"),
        # actions=True: the rail's tree carries a pin per note. The graph
        # frame renders the same tree without one, because a pin there would
        # point at a rail that document does not have.
        "tree": file_tree.rail_tree(nodes, _tree_raws()),
        # Silica's own folders: no folder in view and no "Write a note" (ADR-0155
        # points 4, 5). The page asks here because it has the tree from here.
        "never": list(never_dirs()),
        "check_web": check_web_open(),
        # The chat landing's ways to start (ADR-0048, amendment 2026-10-22):
        # the notes the vault links most. The counts, the area count and the
        # topic labels the landing stated before the greeting went with it,
        # and with them the community pass this endpoint ran after every turn.
        "hubs": brief.top_hubs(nodes, edges),
    }
