# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note's neighbours, with what the graph says about each.

Above recall and beside the graph lane because it reads both: the fused ranking
from kernel.recall.relatedness, the clusters and hops from kernel.graph. Below
the tools, because the orphan pass and the Coordinator ask for link targets too.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def related(note: str, k: int = 5, memory: bool = True) -> dict[str, Any]:
    """silica_related's payload with the fused `score` and `reranked`, for the
    callers that are not the model: the reranker's contract is tested here."""
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.recall.cooccurrence import (
        CooccurStore, store_key, get_cooccur_store,
    )
    from silica.kernel.recall.embed import get_store
    from silica.kernel.recall.relatedness import related_notes
    from silica.kernel.recall.search import code_stale_map, stale_entry

    # Resolve name-or-path to the canonical vault path (any backend), then reduce to
    # the store keyspace via store_key (strip .md, posix, CASE-PRESERVED). This is
    # the single source of truth for both index keyspaces: it makes the query hit the
    # stored vectors/nodes AND lets related_notes exclude the query itself (blocking
    # a raw ".md" path would let the note resurface among its own results). Never
    # ledger_key here — its lowercasing misses the case-preserving stored keys.
    try:
        query_path = DRIVER.read_note(note).ref.path
        resolved = bool(query_path)  # a backend returning '' must not pass as resolved
    except Exception:
        resolved = False
    if not resolved:
        query_path = note  # unresolved: treat the input itself as a path
    note_path = query_path if resolved else ""  # pre-store_key form, for file_refs_of
    query_path = store_key(query_path)

    embed_store = get_store()
    cooccur_store: CooccurStore | None
    try:
        cooccur_store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        if len(cooccur_store) == 0:
            cooccur_store = None
    except Exception:
        cooccur_store = None
    if len(embed_store) == 0 and cooccur_store is None:
        return {"note": note, "error": "No index available. Run silica_embed_refresh or silica_cooccurrence_refresh first."}

    from silica.kernel.workspaces import allow_keys

    allow = allow_keys()  # a workspace's turn ranks its members only (ADR-0112 point 4)
    mem_embed = mem_cooccur = None
    if memory and allow is None:
        from silica.kernel.recall.memory_lane import memory_stores

        mem_embed, mem_cooccur = memory_stores()  # ADR-0019 second recall lane
    results = related_notes(
        query_path,
        embed_store=embed_store,
        cooccur_store=cooccur_store,
        memory_embed_store=mem_embed,
        memory_cooccur_store=mem_cooccur,
        k=k if allow is None else max(k, len(allow)),
    )
    if allow is not None:
        results = [r for r in results if r.path in allow][:k]
    # Same score contract as silica_semantic_search (ADR-0032): reorder-only
    # within the fused top-k (membership stays first-stage, retrieval-gates 2a),
    # query = the seed's bare title — the one measured rerank query shape. The
    # raw RRF term 1/(60+rank) the payload used to expose as `score` cannot
    # separate candidates by construction (measured 2026-08-25: six results
    # inside [0.0159, 0.0164]).
    rr: dict[str, Any] = {"reranked": False}
    if results:
        from silica.model.providers import get_reranker
        from silica.kernel.recall.rerank import link_query, rerank_related

        reranker = get_reranker(CONFIG)
        if reranker:
            results = rerank_related(reranker, link_query(query_path), results,
                                     k=k, stats=rr)
    # Cluster membership from the cached ctx (last Louvain run; {} when cold):
    # tells the caller whether a candidate sits in the query's own knowledge
    # area or across a cluster boundary. Memory-lane notes are another vault —
    # never annotated.
    from silica.kernel.graph.graph_export import cluster_hub_of, graph_distances, load_cluster_ctx

    gctx_map = (load_cluster_ctx() or {}).get("ctx") or {}
    # Structural distance: wikilink hops from the query to each result — the
    # per-pair coherence read. High fused score + null (unreachable) or large
    # distance = a missing link worth creating; distance 1 = already linked.
    # Omitted entirely when the wikilink graph is unavailable.
    dists = graph_distances(query_path)
    stale_map = code_stale_map()
    out: dict[str, Any] = {
        "note": note,
        "reranked": bool(rr.get("reranked", False)),
        # Which legs actually ranked: an absent leg (cold index, lane off) is
        # otherwise indistinguishable from unanimous agreement — 2026-08-25 the
        # embed index was empty and six cooccur-only results read as consensus.
        "legs": {
            "embed": len(embed_store) > 0,
            "cooccur": cooccur_store is not None,
            "memory": mem_embed is not None or mem_cooccur is not None,
        },
        "results": [
            {
                "path": r.path,
                "name": r.name,
                "score": round(r.score, 4),
                "evidence": r.evidence,
                **({"embed": round(r.embed_score, 3)}
                   if r.embed_score is not None else {}),
                **({"cooccur": int(round(r.cooccur_weight))}
                   if r.cooccur_weight is not None else {}),
                **(
                    {"cluster": hub}
                    if r.origin != "memory" and (hub := cluster_hub_of(gctx_map, r.path))
                    else {}
                ),
                **(
                    {"distance": dists.get(r.path)}
                    if dists is not None and r.origin != "memory"
                    else {}
                ),
                **({"origin": "memory"} if r.origin == "memory" else {}),
                **stale_entry(stale_map, r),
            }
            for r in results
        ],
    }
    if note_path:
        try:
            fr = DRIVER.file_refs_of(note_path)
            files = {kind: v for kind, v in fr.items() if v and kind != "unresolved"}
            if files:
                # Structural section, not a retrieval leg: what the seed note
                # references on disk (embeds, documents:). Never enters fusion
                # (ADR-0018 boundary); omitted when empty to save tokens.
                out["files"] = files
        except Exception:
            pass  # a backend without file refs must not fail the related call
    if not results:
        # Empty is ambiguous — say why so the caller acts instead of guessing.
        if not resolved:
            out["hint"] = f"note '{note}' did not resolve to a vault note — check the name/path."
        elif len(embed_store) == 0:
            out["hint"] = "embedding index empty — co-occurrence only. Run silica_embed_refresh for semantic neighbors."
    return out


def link_candidates(path: str, k: int = 5) -> list[dict]:
    """Link targets to offer for an orphan note: ``[{"name", "path"}, ...]``.

    Fuses embeddings + co-occurrence (RRF): pure candidate generation, no cosine
    thresholding, so it still produces targets when the embedding index is empty
    (the co-occurrence leg carries the routing on its own). A reranker, when one
    is configured, tightens a wider pool down to `k`.

    Any failure degrades to `[]`, which every caller treats as a safe no-op:
    the orphan worker only links among the candidates it was offered.
    """
    try:
        from silica.config import CONFIG
        from silica.kernel.recall.cooccurrence import get_cooccur_store
        from silica.kernel.base.notekey import store_key
        from silica.kernel.recall.embed import get_store
        from silica.kernel.base.layout import is_staging_path
        from silica.kernel.recall.relatedness import related_notes

        from silica.model.providers import get_reranker
        from silica.kernel.recall.rerank import link_query, rerank_related

        # store_key (case-PRESERVED, .md-stripped) is the store keyspace; ledger_key
        # would lowercase and miss the case-preserving stored keys -> empty results.
        key = store_key(path)
        reranker = get_reranker(CONFIG)
        pool = max(k, 20) if reranker else k
        embed_store = get_store()
        cooccur_store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        # Staging (inbox, done/) is never a link target: the orphan pass writes
        # INTO the chosen candidate, and on 2026-09-27 the owner ruled both out
        # (inbox: 28 of 200 gate candidates; done/: 2 of 16 field links went
        # into archived source chapters). Excluded inside each leg, before the
        # pool is cut, so k survivors remain.
        staging = {p for store in (embed_store, cooccur_store) if store is not None
                   for p in store.paths() if is_staging_path(p)}
        results = related_notes(
            key,
            embed_store=embed_store,
            cooccur_store=cooccur_store,
            k=pool,
            exclude=staging,
        )
        if reranker:
            results = rerank_related(reranker, link_query(key), results, k=k)
        return [{"name": r.name, "path": r.path} for r in results]
    except Exception as e:
        logger.debug("link candidate lookup failed for %r (non-fatal): %s", path, e)
        return []
