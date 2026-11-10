# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Graph & relatedness tools — indexes, search, linking, and the vault audit.

Embedding and co-occurrence index refresh, semantic search, autolink/backlink
passes, the vis.js graph export, and the structural vault report.
"""
from __future__ import annotations

from typing import Annotated

import logging
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from silica.agent.compaction import dedup_recall_excerpts
from silica.driver import DRIVER
from silica.kernel.recall import refresh
from silica.kernel.recall import search as recall_search
from silica.kernel.report import related as report_related
from silica.tools import Reads, tool

logger = logging.getLogger(__name__)



@tool(cls="composed", ingress=False)
def silica_graph_export(output_path: Annotated[str, Field(description="Vault-relative path for the output HTML file (e.g. 'graph.html')")] = "graph.html", folder: Annotated[str, Field(description='Vault-relative folder to restrict scope (empty = entire vault)')] = "",
                        title: Annotated[str, Field(description='Title shown in the visualization header')] = "Vault Graph", knn_k: Annotated[int, Field(description='semantic overlay: nearest neighbours per note (SIMILAR edge density)')] = 6) -> dict[str, Any]:
    """Self-contained interactive HTML graph of the vault: the wikilink graph
    (Louvain-clustered; ghost nodes mark unresolved links) plus a toggleable
    embedding k-NN overlay that places link-orphans next to their semantic
    neighbours. Opens in any browser. Visualization only — for an actionable
    structural audit use silica_vault_report.
    """
    from silica.config import CONFIG
    from silica.kernel.base.layout import contain_in_vault

    # The model names this path (audit 2026-10-09, S1). It used to write any
    # file, and a title carrying newlines turned `~/.silica/.env` into a
    # SILICA_*_SERVE_CMD that serve.py runs with shell=True. In the vault and
    # .html only: a vault opened at $HOME still holds ~/.silica/.env, and a
    # note overwritten with HTML is data loss.
    if not output_path.strip().lower().endswith(".html"):
        raise ValueError(f"output_path must be an .html file: {output_path!r}")
    vault = Path(CONFIG.vault_path or ".")
    out = vault / contain_in_vault(output_path, vault)
    return graph_export(str(out), folder=folder, title=" ".join(title.split()), knn_k=knn_k)


def graph_export(output_path: str, folder: str = "", title: str = "Vault Graph",
                 knn_k: int = 6) -> dict[str, Any]:
    """The export behind silica_graph_export, for a path the USER chose (the
    /graph command, the GUI's temp file), which may lie outside the vault."""
    from silica.tools.graph_view import export_graph

    # Best-effort: refresh the co-occurrence index so clusters get named labels
    # (incremental — skips already-indexed notes). Naming degrades to "Cluster N"
    # if this fails; the graph still renders. ponytail: full-vault refresh, scope
    # to changed notes only if it gets slow on big vaults.
    try:
        silica_cooccurrence_refresh(folder=folder)
    except Exception as exc:
        logger.warning("silica_graph_export: cooccurrence refresh skipped (%s)", exc)
    # The daily notes' block vectors, by the same rule (ADR-0062 Decision 6):
    # only the blocks whose text is new are embedded. With the embedder down
    # the days keep their place in the graph and draw no relation.
    try:
        from pathlib import Path

        from silica.model.providers import get_embedder
        from silica.config import CONFIG
        from silica.kernel.graph import daily_layer

        daily_layer.refresh_blocks(get_embedder(CONFIG), Path(CONFIG.vault_path or "."))
    except Exception as exc:
        logger.warning("silica_graph_export: daily blocks refresh skipped (%s)", exc)

    return export_graph(output_path=output_path, folder=folder, title=title, knn_k=knn_k)


@tool(cls="composed", ingress=False, note_args=("note_path",))
def silica_mindmap(note_path: Annotated[str, Field(description='Vault-relative path of the note to root the map on')], force: Annotated[bool, Field(description='Overwrite an existing maps/<stem>.canvas (defaults to no-clobber)')] = False) -> dict[str, Any]:
    """Radial mind-map rooted on one note, written to maps/<stem>.canvas
    (editable in Obsidian). Deterministic: BFS over wikilinks plus the latent
    relatedness leg, radial wedges by community. No-clobber: an existing map
    needs force=True. Flat whole-vault network: silica_graph_export.
    """
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.graph.mindmap import build_mapview, gather_materials, mapview_to_canvas
    from silica.kernel.recall.resolve import note_resolver

    # Accept a path OR a title (the GUI input and casual CLI use give titles).
    root = note_resolver()(note_path)
    if root is None:
        return {"error": f"'{note_path}' not found in the vault graph."}

    stem = Path(root).stem
    vault = CONFIG.vault_path or "."
    out = Path(vault) / "maps" / f"{stem}.canvas"

    # No-clobber: exists + not force → refuse. Diffing the generated map
    # against a user-rearranged one is a feature for the day someone asks;
    # here we simply never clobber.
    if out.exists() and not force:
        return {"skipped": str(out), "reason": "exists", "hint": "re-run with force=True to regenerate"}

    materials = gather_materials(root, latent_k=CONFIG.mindmap_latent_k)
    mv = build_mapview(
        root, materials, max_nodes=CONFIG.mindmap_max_nodes, hops=CONFIG.mindmap_hops
    )
    if len(mv.nodes) <= 1:
        return {"error": f"'{root}' has no neighbours to map (isolated in the graph)."}

    import orjson
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(mapview_to_canvas(mv), option=orjson.OPT_INDENT_2))
    logger.info("silica_mindmap: wrote %s — %d nodes, %d edges", out, len(mv.nodes), len(mv.edges))
    return {"path": str(out), "nodes": len(mv.nodes), "edges": len(mv.edges)}


@tool(cls="composed", collapse="eager", ingress=False, note_args=("note_path",))
def silica_autolink(note_paths: Annotated[list[str] | None, Field(description='List of vault-relative paths to autolink')] = None, note_path: Annotated[str, Field(description='Vault-relative path of the note to autolink (legacy single-file)')] = "", use_candidates: Annotated[bool, Field(description='Use embedding candidates to focus autolinking (requires index)')] = True) -> dict[str, Any]:
    """Scan the given notes for mentions of existing vault titles and wrap them
    as wikilinks. Skips frontmatter, code, math, headings, already-linked text;
    only links titles that exist (graph-safe).

    ONE call consumes the ENTIRE `note_paths` list; `notes_linked` below
    `notes_scanned` is normal — there is no rest, do NOT re-call to finish.
    Reverse direction (links TO new notes from older neighbours):
    silica_backlink. Vault-wide pass that finds its own candidates:
    silica_curate.
    """
    from silica.kernel.link.autolink import build_title_index

    paths = note_paths or []
    if note_path and note_path not in paths:
        paths.append(note_path)

    if not paths:
        return {"error": "No note paths provided."}

    try:
        all_refs = DRIVER.list_files()
    except Exception as e:
        return {"error": f"Failed to list vault files: {e}"}

    title_index = build_title_index(all_refs)
    
    store = None
    embedder = None
    cooccur_store = None
    if use_candidates:
        try:
            from silica.model.providers import get_embedder
            from silica.config import CONFIG
            from silica.kernel.recall.embed import get_store
            store = get_store()
            if len(store) > 0:
                embedder = get_embedder(CONFIG)
        except Exception:
            pass
        # The co-occurrence leg is embedder-free: load it independently so
        # candidates survive (focused) even when the embedder is down.
        try:
            from silica.config import CONFIG
            from silica.kernel.recall.cooccurrence import get_cooccur_store
            cooccur_store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
            if len(cooccur_store) == 0:
                cooccur_store = None
        except Exception:
            cooccur_store = None

    total_added = 0
    linked = 0
    skipped: list[str] = []
    write_errors: list[str] = []

    for path in paths:
        try:
            nc = DRIVER.read_note(path)
        except Exception as e:
            skipped.append(f"{path}: unreadable ({type(e).__name__})")
            continue

        body = nc.content or ""
        if not body.strip():
            skipped.append(f"{path}: empty")
            continue

        candidates: list[str] | None = None
        if use_candidates and (cooccur_store is not None or (store is not None and embedder is not None)):
            query_vec = None
            if store is not None and embedder is not None:
                try:
                    query_vec = embedder.embed([body[:800]])[0]
                except Exception:
                    query_vec = None
            try:
                from silica.kernel.recall.relatedness import related_notes_for_query
                related = related_notes_for_query(
                    query_vec=query_vec,
                    query_text=body,
                    embed_store=store,
                    cooccur_store=cooccur_store,
                    k=20,
                )
                # Only narrow to candidates when the facade actually proposed
                # some; an empty list would suppress linking, so leave it None
                # to fall back to the full title_index scan.
                if related:
                    candidates = [r.name for r in related]
            except Exception:
                pass  # fall back to full title_index scan

        try:
            added = DRIVER.autolink_note(
                path,
                candidates=candidates if candidates is not None else title_index,
                title_index=title_index,  # already built above; else rebuilt per note
            )
            if added:
                total_added += len(added)
                linked += 1
        except Exception as e:
            write_errors.append(f"{path}: {e}")

    # `notes_scanned` is the anti-relaunch signal: the whole list is consumed in
    # one call, so a low `notes_linked` means "nothing left to link", not
    # "call me again with the rest".
    result: dict[str, Any] = {
        "notes_scanned": len(paths),
        "notes_linked": linked,
        "total_links_added": total_added,
    }
    if skipped:
        result["skipped"] = skipped
    if write_errors:
        result["write_errors"] = write_errors
    return result


@tool(cls="composed", collapse="eager", ingress=False)
def silica_backlink(new_titles: Annotated[list[str], Field(description='Titles of notes just created in this run')], neighbourhood: Annotated[list[str], Field(description='Vault-relative paths of candidate notes to scan')]) -> dict[str, Any]:
    """Inject wikilinks to newly-created notes into pre-existing neighbouring notes.

    For each note in `neighbourhood`, wraps mentions of any title in `new_titles`
    with a wikilink — the reverse of silica_autolink. Skips frontmatter, code,
    math, and already-linked spans. Returns {path: [titles_added]}.
    """
    from silica.kernel.link.autolink import backlink_pass, build_title_index

    try:
        all_refs = DRIVER.list_files()
    except Exception as e:
        return {"error": f"Failed to list vault files: {e}"}

    title_index = build_title_index(all_refs)
    added_map = backlink_pass(new_titles, title_index=title_index, neighbourhood=neighbourhood)
    total = sum(len(v) for v in added_map.values())
    return {"added": total, "notes_modified": len(added_map), "details": added_map}


@tool(cls="composed", ingress=False, parallel_safe=True, found_nothing="results")
def silica_semantic_search(query: Annotated[str, Field(description='Free-form query text to embed and search against the vault index')], k: Annotated[int, Field(description='Number of results to return')] = 5, memory: Annotated[bool, Field(description='Include the personal-memory lane (ADR-0019). Pass false for questions scoped to THIS vault — repo and code questions — so an unrelated personal vault cannot occupy result slots.')] = True, folder: Annotated[str, Field(description="Only notes under this vault folder (e.g. 'docs/adr'). Empty = whole vault.")] = "") -> dict[str, Any]:
    """Find vault notes by MEANING (embeddings + co-occurrence fused, reranked).

    Use for "what do I have about X" when the exact wording is unknown;
    `query` can be a phrase or a whole paragraph. A leg that is down (empty
    index, embedder offline) degrades to the survivor. For literal text use
    silica_search_context; when the text IS an existing note, prefer
    silica_related. Returns at most k results, best first; verify with
    silica_read_note before acting. The last result is not a cut-off: to
    decide whether a note exists, use silica_exists or silica_search.
    """
    return _without_score({"query": query, **recall_search.facade_search(query, k=k, memory=memory, folder=folder)})


def _without_score(out: dict[str, Any]) -> dict[str, Any]:
    """The payload the model gets: no fused `score`, and no `reranked`, which
    only said which of two scales that score was on. Seven of nine models in
    the 2026-11-04 survey refuse a number with no basis beside it, and the
    owner took it out (docs plans/llm-km-survey-cards.md, C2); the order is
    the ranking, and `evidence`, `embed` and `cooccur` keep the numbers that
    name their leg. /find, in the CLI and the web panel, reads
    facade_search and keeps the score for the person."""
    out = {key: v for key, v in out.items() if key != "reranked"}
    if isinstance(out.get("results"), list):
        out["results"] = [{key: v for key, v in r.items() if key != "score"} for r in out["results"]]
    return out


@tool(cls="composed", ingress=False, parallel_safe=True, once_per_block=True,
      reads=Reads(args=("notes",), result="notes"), found_nothing="notes",
      history_view=dedup_recall_excerpts)
def silica_recall(query: Annotated[str, Field(description='The question or topic to recall memory for')], k: Annotated[int, Field(description='Maximum number of notes contributing to the context')] = 15, memory: Annotated[bool, Field(description='Include the personal-memory lane and its facts (ADR-0019). Pass false for questions scoped to THIS vault — repo and code questions — so an unrelated personal vault cannot occupy context slots.')] = True, vault: Annotated[str, Field(description="Peek: path of another Silica vault to answer from<< (silica_vaults, loadable, lists them)>>. Read-only; the session's vault does not change.")] = "",
                  folder: Annotated[str, Field(description="Only notes under this vault folder (e.g. 'docs/adr', 'silica'). Empty = whole vault. Use it when a subtree of unrelated notes (research on other products, fixtures) keeps taking the slots.")] = "",
                  min_trust: Annotated[str, Field(description="Trust floor: 'human' (a person wrote or vouched for it), 'grounded' (agent note with its verbatim source leaf), 'distilled' (agent note without one), 'kept' (a /keep of a web answer). Empty = no floor. A floor drops notes after retrieval, so `filtered` says how many you did not see.")] = "",
                  lifecycle: Annotated[str, Field(description="Only notes in this state: 'active', 'review' (gate soft flag), 'contested' (unresolved contradiction), 'superseded'. Empty = every state; the header names the non-active ones anyway.")] = "",
                  notes: Annotated[list[str] | None, Field(description="Notes the question names (names or paths): the context is those notes, in this order, and `missing` lists the ones that do not exist. Empty = search the vault.")] = None,
                  since: Annotated[str, Field(description="Only notes dated on/after YYYY[-MM[-DD]]. Undated notes drop out: on an empty result, retry without.")] = "",
                  until: Annotated[str, Field(description="Only notes dated on/before YYYY[-MM[-DD]]; '2026-03' keeps all of March.")] = "") -> dict[str, Any]:
    """Assemble an answer-ready memory context for a question: fused retrieval,
    each note's query-densest window under a rank/evidence/date header,
    recalled personal facts first. Use when ANSWERING from vault memory
    INSTEAD of stitching searches and reads yourself. When the question names
    notes ("according to Lecture 32", "summarise A and B"), pass them as
    `notes`: one call replaces the search, exists and read chain, and
    `missing` says which named notes are not there. Answer from `context`;
    re-read only the notes named in `partial`, the rest arrived whole. Paths
    under `memory` live in the personal-memory vault; re-read them, and a
    peek's `partial`, with silica_read_note(name, vault=<memory_vault> or the
    same `vault`).
    """
    import datetime

    from silica.kernel.recall.perception import perceive

    from silica.kernel.code import codedocs

    from silica.kernel.write.contested import LIFECYCLES, TRUST_ORDER

    min_trust, lifecycle = min_trust.strip().lower(), lifecycle.strip().lower()
    if min_trust and min_trust not in TRUST_ORDER:
        return {"query": query, "error": f"unknown min_trust '{min_trust}'; one of "
                + ", ".join(sorted(TRUST_ORDER, key=lambda k: TRUST_ORDER[k], reverse=True)),
                "context": "", "notes": [], "partial": [], "facts": 0}
    if lifecycle and lifecycle not in LIFECYCLES:
        return {"query": query, "error": f"unknown lifecycle '{lifecycle}'; one of "
                + ", ".join(LIFECYCLES), "context": "", "notes": [], "partial": [], "facts": 0}
    from silica.kernel.calendar.timeline import DATE_PREFIX

    since, until = since.strip(), until.strip()
    bad = next((b for b in (since, until) if b and not DATE_PREFIX.fullmatch(b)), None)
    if bad is not None:
        return {"query": query, "error": f"'{bad}' is not a date bound; since and until take "
                "YYYY, YYYY-MM or YYYY-MM-DD", "context": "", "notes": [], "partial": [], "facts": 0}
    peek = _peek_target(vault)
    if isinstance(peek, dict):  # a refusal, already shaped as the reply
        return {"query": query, **peek}
    if (since or until) and (notes or peek):
        # A peek's legs take no allow set (perception.facade_retrieve): the window would
        # be dropped there without a word (ADR-0148 point 5).
        return {"query": query, "error": "since/until narrow a search of this vault: named notes "
                "are read whatever their date, and a peek takes no window",
                "context": "", "notes": [], "partial": [], "facts": 0}
    if notes and peek:
        # perceive() assembles given paths from the active vault only.
        return {"query": query, "error": "notes= names notes of the active vault; for another "
                "vault's note use silica_read_note(name, vault=...)",
                "context": "", "notes": [], "partial": [], "facts": 0}
    named, missing = _resolve_named(notes) if notes else (None, [])
    from silica.kernel.workspaces import allow_keys

    # A workspace's turn searches its members only (ADR-0112 point 4); named
    # notes are the user's act and a peek is another vault, so neither is cut.
    allow = allow_keys() if named is None and not peek else None
    window: dict[str, Any] | None = None
    if since or until:
        from silica.config import CONFIG
        from silica.kernel.workspaces import in_scope
        from silica.kernel.calendar.timeline import dated_window

        # The window is an allow set, applied inside each leg like a
        # workspace's and intersected with it (ADR-0148 point 2).
        keys, undated = dated_window(Path(CONFIG.vault_path or ".").expanduser(),
                                     since, until, keep=in_scope if allow is not None else None)
        allow = keys if allow is None else allow & keys
        window = {"since": since, "until": until, "dated": len(allow), "undated": undated}
        if not allow:
            return {"query": query, "window": window, "context": "", "notes": [], "partial": [],
                    "facts": 0, "hint": "no dated note falls in this window; retry without "
                    "since/until"}
    if allow is not None:
        memory = False
    rr: dict[str, Any] = {"reranked": False}
    # Named notes are the whole context: the memory lane's facts would lead it
    # with material the question did not ask about.
    p = perceive(query, now=datetime.date.today().isoformat(), k=k,
                 paths=named,
                 use_memory=memory and named is None, vault=peek, leg_stats=rr,
                 folder=folder.strip() or None,
                 min_trust=min_trust or None, lifecycle=lifecycle or None,
                 mark_used=True, allow=allow)
    # Staleness is the ACTIVE vault's code lane; a peeked vault's paths would
    # only ever match it by coincidence.
    stale_map = {} if peek else recall_search.code_stale_map()
    if not peek:
        from silica.agent.memory_gate import returned_notes

        # From the blocks, not `context`: the rendered text carries no
        # frontmatter, so a mail note's stamp is not in it (ADR-0090 point 8).
        # Memory-lane paths resolve in another vault, whose imports are not
        # this vault's record.
        returned_notes(b.path for b in p.blocks if b.origin != "memory")
    flagged = {b.path: lvl for b in p.blocks
               if (lvl := codedocs.peek_level(stale_map, b.path))}
    # render(windowed=True) emits b.excerpt, so a note whose window IS its body
    # was delivered complete — measured: 3 of 9 calls in a chat were re-reads of
    # notes recall had already handed over whole.
    out = {"query": query, **recall_search.degraded_fields(rr),
           "context": p.render(stale=stale_map or None),
           "notes": [b.path for b in p.blocks],
           # Active-vault and peeked notes: the caller can re-read both (the
           # latter with vault=). Memory-lane notes stay out (ADR-0032) — they
           # are listed under `memory` with the vault to read them from, and
           # "recall names notes that read_note denies" is how the doctor's
           # worst report was born.
           "partial": [b.path for b in p.blocks if b.origin != "memory" and not b.whole],
           "facts": len(p.fact_hits)}
    if missing:
        out["missing"] = missing
    if window is not None:
        out["window"] = window
    if p.filtered:
        out["filtered"] = p.filtered  # the model must know it saw less than retrieval found
    if p.all_non_active:
        out["all_non_active"] = True  # the context opens with the abstain rule (M3)
    mem_paths = [b.path for b in p.blocks if b.origin == "memory"]
    if mem_paths:
        out["memory"] = mem_paths
        from silica.kernel.recall.memory_lane import memory_vault

        mv = memory_vault()
        if mv is not None:
            out["memory_vault"] = str(mv)  # silica_read_note(name, vault=this) opens them
    if flagged:
        out["stale"] = flagged
    if peek:
        out["vault"] = peek
        from silica.kernel.recall.vault_registry import coverage

        cov = coverage(Path(peek))
        if cov["level"] != "indexed":
            # An empty answer from a cold index reads as "that vault knows
            # nothing about this"; what it means is that nobody indexed it.
            out["coverage"] = cov["level"]
            cov_hint = (
                "no recall index yet: open the vault once (SILICA_VAULT=<path> silica) "
                "and run /embed and /cooccur"
                if cov["level"] == "cold" else
                "only the lexical index exists (silica_vaults can score it); run /embed "
                "and /cooccur in that vault for recall to see it")
            # Appended: it used to overwrite the degraded line, the one hint
            # that says the answer above it was made without a leg.
            out["hint"] = f"{out['hint']} · {cov_hint}" if out.get("hint") else cov_hint
    return out


def _resolve_named(notes: list[str]) -> tuple[list[str], list[str]]:
    """(store paths of the named notes that resolve, names that do not).

    The chain this replaces is what the model ran instead of recall on every
    question that names a note: search the title, check it exists, read it
    (8 of 185 tool calls were recall on the 2026-09-23 prompt A/B; GUI and TUI
    sessions, 14 of 445). Resolution is the reader's own wikilink rule, so a
    name recall accepts is a name silica_read_note opens.
    """
    from silica.driver import DRIVER

    named: list[str] = []
    missing: list[str] = []
    for ref in notes:
        ref = (ref or "").strip()
        if not ref:
            continue
        try:
            path = DRIVER.read_note(ref).ref.path or ""
        except (RuntimeError, ValueError):
            # The reader's "not found" (RuntimeError) or "outside the vault"
            # (ValueError) is the answer the caller asked for. Anything else is
            # a broken reader, and reporting every named note as missing would
            # be the wrong answer to a question the tool could not ask.
            missing.append(ref)
            continue
        key = path[:-3] if path.endswith(".md") else path
        if key and key not in named:
            named.append(key)
    return named, missing


def _peek_target(vault: str) -> str | dict | None:
    """Resolve `vault=`: None for a plain call (empty, or the active vault
    itself), the resolved path for a peek, or the error reply for a folder
    not on this machine's list (`resolve_known`)."""
    if not (vault or "").strip():
        return None
    from silica.config import CONFIG
    from silica.kernel.recall.vault_registry import resolve_known

    try:
        target = resolve_known(vault)
    except ValueError as e:
        return {"error": str(e), "hint": "silica_vaults lists the vaults this machine knows",
                "context": "", "notes": [], "partial": [], "facts": 0}
    active = (CONFIG.vault_path or "").strip()
    if active and Path(active).resolve() == target:
        return None
    return str(target)


@tool(cls="atomic", ingress=False, parallel_safe=True)
def silica_vaults(query: Annotated[str, Field(description='Question to score each vault against; empty = the plain list')] = "", k: Annotated[int, Field(description='Top titles per vault')] = 3) -> dict[str, Any]:
    """The Silica vaults this machine knows (active, personal memory, adopted
    Obsidian vaults): name, path, brief, write_dir, coverage. With `query`,
    each row adds `top` titles and `rerank`, to tell WHICH vault to read with
    silica_recall(query, vault=<path>). `home` lists the vaults that hold the
    answer ([] = none of them; null = no calibrated reranker, judge by `top`).
    Rows are in relevance order only when `ranked` is true. `coverage: "cold"`
    = never indexed, not "no hits". Never switches or fuses vaults.
    """
    from silica.config import CONFIG
    from silica.kernel.recall import vault_registry

    out = vault_registry.route(query, k=k)
    active = next((r["path"] for r in out["vaults"] if r["active"]),
                  (CONFIG.vault_path or ""))
    return {"query": query, "active": active, **out}


@tool(cls="composed", ingress=False, parallel_safe=True)
def silica_timeline(start: Annotated[str, Field(description='Inclusive ISO start date (empty = unbounded)')] = "", end: Annotated[str, Field(description='Inclusive ISO end date (empty = unbounded)')] = "", limit: Annotated[int, Field(description='Maximum rows; on overflow the most recent are kept')] = 50) -> dict[str, Any]:
    """Chronological index of the vault's dated notes, oldest first — for
    ORDERING and TIME questions ("when did X happen", "most recent Z"). Reads
    `date` frontmatter; undated notes are excluded. Order here, then
    silica_read_note the stem for detail. Content-based recall: silica_recall.
    """
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.calendar.timeline import timeline

    vault = Path(CONFIG.vault_path or "").expanduser()
    if not vault.is_dir():
        return {"error": "No vault configured."}
    from silica.kernel.workspaces import active_scope, in_scope

    # A workspace's turn lists and counts its members only (ADR-0112 point 4).
    scoped = active_scope() is not None
    t = timeline(vault, start=start, end=end, limit=limit, keep=in_scope if scoped else None)
    lines = [f"{date}  -> {label} ({stem}.md)" for date, label, stem in t["rows"]]
    if t["dropped"]:
        lines.append(f"...and {t['dropped']} more dated notes before this range")
    return {"timeline": "\n".join(lines),
            "total_dated": t["total_dated"], "dropped": t["dropped"]}


@tool(cls="composed", ingress=False, parallel_safe=True, note_args=("note",))
def silica_related(note: Annotated[str, Field(description='Note name (wikilink-style) or vault-relative path to find related notes for')], k: Annotated[int, Field(description='Number of results to return')] = 5, memory: Annotated[bool, Field(description='Include the personal-memory lane (ADR-0019). Pass false for questions scoped to THIS vault — repo and code questions — so an unrelated personal vault cannot occupy result slots.')] = True) -> dict[str, Any]:
    """Given an EXISTING note (by name or path), the notes most related to it —
    embeddings + co-occurrence fused into one ranked shortlist, then reranked.

    Use for "what's related to note X" INSTEAD of reading X and
    keyword-searching; for free-form text use silica_semantic_search. Each
    result carries `evidence` (which metric proposed it, with native scores),
    `cluster`, and `distance` (wikilink hops; null = unreachable). A top
    result at null/large distance = a missing link worth creating; distance
    1 = already linked. Verify with silica_read_note before acting. `legs`
    names which retrieval legs actually ranked; an absent leg is not
    agreement.
    """
    return _without_score(report_related.related(note, k, memory))


@tool(cls="composed", ingress=False, parallel_safe=True, note_args=("note",))
def silica_concepts(term: Annotated[str, Field(description="A single word/concept to look up in the vault's co-occurrence graph")] = "", note: Annotated[str, Field(description="An existing note (name or path): return ITS concepts instead of a term's neighbourhood")] = "", k: Annotated[int, Field(description='Number of neighbouring concepts and containing notes to return')] = 10) -> dict[str, Any]:
    """The vault's concepts (deterministic co-occurrence graph, embedder-free).

    `term=`: canonical label, centrality, top co-occurring concepts, top notes
    — for terminology decisions and picking wikilink targets BEFORE coining a
    synonym. Single words only; for a phrase query its most distinctive word.
    `note=`: that note's top concepts by weight — "what is this about" without
    reading the body. For related NOTES use silica_related or
    silica_semantic_search.
    """
    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.text.text import stem_word

    try:
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
    except Exception as e:
        return {"error": f"co-occurrence store unavailable ({e}) — run silica_cooccurrence_refresh"}
    if len(store) == 0:
        return {"error": "co-occurrence index empty. Run silica_cooccurrence_refresh first."}

    if note:
        # The note's own concept vector is already stored per note by the index —
        # no extraction, no LLM. note_nodes() applies store_key to the path itself.
        from silica.driver import DRIVER

        try:
            path = DRIVER.read_note(note).ref.path or note
        except Exception:
            path = note  # unresolved: the index may still know the reader's spelling
        nodes = store.note_nodes(path)
        top = sorted(nodes.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        out: dict[str, Any] = {
            "note": note,
            "concepts": [{"concept": store.node_label(s), "weight": c} for s, c in top],
        }
        if not top:
            out["hint"] = (
                f"'{note}' has no entry in the co-occurrence index — check the name/path, "
                "or run silica_cooccurrence_refresh if the note is new."
            )
        return out

    if not term.strip():
        return {"error": "pass either term= (concept neighbourhood) or note= (a note's concepts)."}

    stem = stem_word(term.strip().lower(), lang=store.lang)
    neighbors = store.neighbors(term, k=k)

    # Concept -> notes inverted lookup, ranked by contribution count. Read the
    # cached stem postings directly (O(df log df)) instead of an O(N) vault scan.
    from silica.kernel.workspaces import in_scope

    posting = {p: c for p, c in store.stem_postings().get(stem, {}).items() if in_scope(p)}
    notes = sorted(posting.items(), key=lambda kv: (-kv[1], kv[0]))[:k]

    out = {
        "term": term,
        "concept": store.node_label(stem),
        "centrality": round(sum(store.adjacency().get(stem, {}).values()), 1),
        "neighbors": neighbors,
        "notes": [{"path": p, "count": c} for p, c in notes],
    }
    if not neighbors and not notes:
        out["hint"] = (
            f"'{term}' is not a concept node in the co-occurrence graph — "
            "concepts are single content words; try another word, or run "
            "silica_cooccurrence_refresh if the index is stale."
        )
    return out


@tool(cls="composed", collapse="eager", ingress=False)
def silica_embed_refresh(folder: Annotated[str, Field(description='Vault-relative folder to restrict indexing (empty = entire vault)')] = "", force: Annotated[bool, Field(description='Re-embed all notes, even if already indexed')] = False) -> dict[str, Any]:
    """Build or refresh the vault embedding index.

    Powers silica_semantic_search, silica_related, and silica_dedup — run it
    first if those report an empty index. Incremental: skips notes already
    indexed (unless force=True). Call after bulk writes to keep it fresh.
    """
    from silica.agent.vision import ask_chat_model

    return refresh.embed(folder, force, ask=ask_chat_model)


@tool(cls="composed", collapse="eager", ingress=False)
def silica_cooccurrence_refresh(folder: Annotated[str, Field(description='Vault-relative folder to restrict indexing (empty = entire vault)')] = "", force: Annotated[bool, Field(description='Re-process all notes, even if already indexed')] = False) -> dict[str, Any]:
    """Build or refresh the co-occurrence index: a deterministic concept graph
    from note text, works without the embedder. Powers cluster naming and
    silica_vault_report signals. Incremental (force=True to redo). Seed once
    on an existing vault; writes keep it fresh afterwards.
    """
    return refresh.cooccurrence(folder, force)


@tool(cls="composed", collapse="eager", ingress=False)
def silica_lexical_refresh(folder: Annotated[str, Field(description='Vault-relative folder to restrict indexing (empty = entire vault)')] = "", force: Annotated[bool, Field(description="Rebuild the scanned folder's slice from empty (the whole index when folder is empty)")] = False) -> dict[str, Any]:
    """Build or refresh the lexical (BM25/fuzzy) index over note title+body —
    strong on rare tokens, proper nouns, and dates. The perception window
    selector uses its term weights; run once on an existing vault, writes keep it fresh
    afterwards.
    """
    return refresh.lexical(folder, force)


def _covering_stem(path: Path) -> str:
    """Stem of the note that would cover `path`, "" when only its own can.

    A tabular profile is named for its shard FAMILY, so the 12 members of a
    converted family each read as unconverted under a plain stem match. This
    is the exact inverse of `convert._family_stem` (strip the trailing
    counter), not a fuzzy match — and it is restricted to data files because
    families exist nowhere else, which bounds a wrong answer here to
    suppressing one hint about one CSV.
    """
    from silica.kernel.base.formats import TABULAR_EXTS

    if not path.suffix.lower().endswith(TABULAR_EXTS):
        return ""
    stem = re.sub(r"[\s_.\-0-9]+$", "", path.stem)
    return stem if len(stem) >= 3 and stem != path.stem else ""


# The review protocol used to be ~450 tokens of BASE, paid on every turn of
# every chat to govern the one turn that runs a review. It now arrives in the
# result, at the moment it applies; BASE keeps only the consent rule. Each
# string must fit compaction.STUB_FIELD_CHARS: the budget sweep keeps short
# fields of an old read verbatim, and the "yes" turn is exactly the turn that
# comes after the sweep may have stubbed this result.
_REPORT_NEXT = (
    "Step 1 of 2. Summarise `digest` in plain words, point to GRAPH_REPORT.md, "
    "name every fix in `plan_preview` (capability and reason), and report "
    "`inbox_pending` / `unconverted` if present. Then stop and ask whether to go "
    "ahead. Apply nothing and do not call silica_ledger_next in this turn. After "
    "the user's clear yes, call silica_ledger_next(run_id). Issues are the "
    "user's to decide one by one."
)


@tool(cls="composed", ingress=False)
def silica_vault_report(
    folder: Annotated[str, Field(description='Vault-relative folder to scope (empty = whole vault)')] = "",
    top_k: Annotated[int, Field(description='How many god-nodes / bridges to surface')] = 10,
    with_embeddings: Annotated[bool, Field(description='Also propose missing links via the embedding index')] = False,
    with_cooccurrence: Annotated[bool, Field(description='Also compute the co-occurrence vs wikilink delta (autolink candidates, stale links, missing hubs) — embedder-free')] = False,
) -> dict[str, Any]:
    """Deterministic structural audit — the entry point for /graph and vault
    health. Computes god-nodes, surprising cross-cluster connections, orphans,
    dangling links, clusters; writes GRAPH_REPORT.md and seeds a remediation
    run for silica_ledger_next. Nothing runs until the
    user approves the run; then tiers: auto (no per-task confirmation),
    propose (ask first), escalate (IssueCards, human judgment).<< Loadable
    follow-ups: silica_graph_export for a visual graph, silica_curate straight
    to executable maintenance.>>
    """
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.report.graph_report import compute_report, to_digest, to_facts, write_report
    from silica.kernel.analyst_plan import build_task_plan
    from silica.kernel.progress import IssueCard, Run

    # 1. Build report (on-demand /graph: full analytics — god_nodes/bridges/cohesion)
    report = compute_report(
        folder=folder, top_k=top_k, analytics=True,
        with_embeddings=with_embeddings, with_cooccurrence=with_cooccurrence,
    )

    # Warm the cluster-ctx cache so silica_related/build_substrate can annotate
    # candidates with their community without a nucleate run having happened.
    # Whole-vault only: a folder-scoped map would clobber the global one.
    if not folder:
        try:
            from silica.kernel.graph.graph_export import ctx_from_report, save_cluster_ctx

            save_cluster_ctx(
                [report.totals.get("notes", 0), report.totals.get("links", 0)],
                ctx_from_report(report),
            )
        except Exception as exc:
            logger.debug("silica_vault_report: cluster ctx warm skipped (%s)", exc)

    # 2. Determine output path
    vault_path = CONFIG.vault_path or ""
    if vault_path:
        report_path = str(Path(vault_path) / "GRAPH_REPORT.md")
    else:
        report_path = "GRAPH_REPORT.md"

    paths = write_report(report, report_path)

    result: dict[str, Any] = {
        "digest": to_digest(report),
        "report_md": paths["path_md"],
    }

    # Material waiting in the inbox is part of the vault's state: an audit of
    # an "empty" vault that holds four PDFs must say so, or the caller reports
    # "nothing to do" over a folder full of work (observed 2026-08-15).
    try:
        from silica.kernel.recall.listing import unconverted_under
        from silica.kernel.base.manifest import active_inbox_dir

        inbox = active_inbox_dir()
        pending = unconverted_under(inbox) if inbox else []
        if pending:
            result["inbox_pending"] = {
                "total": len(pending),
                "files": pending[:20],
                "hint": "not yet in the vault — suggest /nucleate <path> (or the folder) to bring them in",
            }
    except Exception as exc:
        logger.debug("silica_vault_report: inbox pending scan skipped (%s)", exc)

    # The inbox is not the only place unread material sits: a student's
    # slide/*.pdf never entered Inbox/, and the audit read as "nothing to do"
    # over a folder of unconverted documents (observed 2026-08-15). Documents
    # whose converted .md already exists (inbox or done/) are not re-reported.
    try:
        if vault_path:
            from silica.kernel.base.manifest import active_done_dir
            from silica.kernel.base.formats import CONVERTIBLE_DOC_EXTS
            from silica.kernel.base.layout import files_with

            root = Path(vault_path)
            # rglob, not glob: the archive mirrors the inbox tree since
            # 2026-08-23, so a flat scan saw only what had been archived from
            # the inbox root and re-reported every nested book as unread.
            converted_stems = {
                p.stem for p in (root / active_done_dir()).rglob("*.md")
            }
            try:
                from silica.driver import DRIVER as _drv

                converted_stems |= {
                    Path(r.path).stem for r in _drv.list_inbox_files()
                    if r.path.endswith(".md")
                }
            except Exception:
                pass
            docs = [
                p.relative_to(root).as_posix()
                for p in files_with(root, CONVERTIBLE_DOC_EXTS)
                if p.stem not in converted_stems
                and (_covering_stem(p) or p.stem) not in converted_stems
            ]
            if docs:
                result["unconverted"] = {
                    "total": len(docs),
                    "files": docs[:20],
                    "hint": "not readable by the index — suggest /nucleate <path> "
                            "(or /convert) to bring them in",
                }
    except Exception as exc:
        logger.debug("silica_vault_report: unconverted scan skipped (%s)", exc)

    # Always seeded. `seed_ledger` was a parameter until 2026-09-28, and no
    # caller in the tree ever passed False; qwen3.5-9b did, in 3 of 3 review
    # runs on the chat core (against 0 of 3 on the full surface), and on the
    # user's "yes" it had no plan to apply and wrote 26 notes of its own
    # (docs/evaluation/prompt-rules-2026-09-28.md).
    # 3. Build plan and seed ledger
    plan = build_task_plan(report)

    # Name what was queued: the chat layer asks consent on these tasks, and a
    # bare count ("2 proposed fixes") forces a blind yes/no (observed 2026-08-15).
    result["plan_preview"] = [
        {"tier": c.tier, "capability": c.capability_name, "reason": c.reason}
        for c in plan.auto + plan.propose
    ]

    run = Run.new(
        mode="analyst",
        user_request=f"audit {folder or 'vault'}",
        checkpoints=plan.checkpoints,
        inputs={"scope": folder or "vault"},
        facts=to_facts(report),
    )

    # Seed tasks from auto + propose (propose carries needs_confirmation flag)
    for candidate in plan.auto + plan.propose:
        run.add_payload_task(candidate.capability_name, candidate.payload, candidate.reason,
                             needs_confirmation=candidate.tier == "propose")

    for i, candidate in enumerate(plan.escalate):
        card = IssueCard(
            task_id=f"issue_{i}",
            question=candidate.reason,
            # Per-candidate when the rule knows what it is asking; the dangling-link
            # wording is only the fallback, not every escalation's question.
            options=candidate.options or [
                {"label": "create_note", "description": "Create a new note with this title"},
                {"label": "rename_existing", "description": "Rename an existing note to match"},
                {"label": "ignore", "description": "Leave the broken link as-is"},
            ],
        )
        run.progress.issues.append(card)

    run.save()

    result["run_id"] = run.run_id
    result["auto"] = len(plan.auto)
    result["propose"] = len(plan.propose)
    result["issues"] = len(plan.escalate)
    result["next"] = _REPORT_NEXT

    return result


@tool(cls="composed", ingress=False)
def silica_health() -> dict[str, Any]:
    """Retrieval + write-path health check, live: `fusion` (masked-wikilink
    recovery — low recall or embed_coverage < 1.0 means related/semantic
    search is degraded; refresh with silica_embed_refresh /
    silica_cooccurrence_refresh and re-run) and `integrity` (differential lint
    across the write-path transforms — under 1.0 the pipeline CORRUPTS note
    bodies and writes should stop; null means the vault held no notes and
    nothing was measured). Full-vault sweep, on demand, not a
    per-write gate. Structural audit of content: silica_vault_report.
    """
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.recall.embed import get_store
    from silica.kernel.report.health import fusion_probe, integrity_probe

    vault = Path(CONFIG.vault_path or "").expanduser()
    if not vault.is_dir():
        return {"error": "No vault configured."}

    try:
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
    except Exception as e:
        store = None
        fusion: dict[str, Any] = {"error": f"co-occurrence store unavailable ({e}) — run silica_cooccurrence_refresh"}
    if store is not None:
        fusion = fusion_probe(vault, store, embed_store=get_store())

    return {"fusion": fusion, "integrity": integrity_probe(vault)}
