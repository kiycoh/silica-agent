# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Reranker A/B over the fused ranking (opt-in, informational).

The gated fusion probe itself lives in ``silica.kernel.health.fusion_probe``
(also served by the silica_health tool); the runner calls it directly. This
module keeps only the reranker A/B — it depends on an HTTP provider, so it is
neither deterministic nor free: never gated, never in the baseline.
"""
from __future__ import annotations

from silica.kernel.health import K, eligible_pairs, wikilink_graph


def _score_pairs(eligible: list[tuple[str, str]], topk: dict[str, list[str]]) -> tuple[float, float, set[tuple[str, str]]]:
    """(recall, mrr, recovered-pair set) for one arm's per-endpoint top-k."""
    hits = 0
    rr_sum = 0.0
    recovered: set[tuple[str, str]] = set()
    for a, b in eligible:
        ranks = []
        if b in topk[a]:
            ranks.append(topk[a].index(b) + 1)
        if a in topk[b]:
            ranks.append(topk[b].index(a) + 1)
        if ranks:
            hits += 1
            rr_sum += 1.0 / min(ranks)
            recovered.add((a, b))
    n = len(eligible)
    return round(hits / n, 4), round(rr_sum / n, 4), recovered


def run_rerank_ab(vault, store, *, embed_store=None, reranker=None,
                  k: int = K, pool: int = 20, verbose: bool = False) -> dict:
    """A/B on the same masked pairs: the gated fused top-k (arm A) vs the
    production rerank path (arm B: pool of `pool` → cross-encoder → top-k).

    Arm B mirrors the production call sites (coordinator/_orphan_candidates,
    curate): pool = max(k, 20), query text = note_document(key).

    ``empty_docs`` counts endpoints whose query text could not be read — for
    those, rerank_related no-ops on the empty query and arm B silently
    degenerates to the unreranked pool. A high count means the A/B measured
    nothing, not that the reranker is neutral.
    """
    from silica.kernel import correlate
    from silica.kernel.relatedness import related_notes
    from silica.kernel.rerank import note_document, rerank_related

    empty = {
        "pairs_evaluated": 0, "endpoints": 0, "empty_docs": 0,
        "base_recall": 0.0, "base_mrr": 0.0,
        "rerank_recall": 0.0, "rerank_mrr": 0.0,
        "pairs_won": 0, "pairs_lost": 0,
    }
    if len(store) == 0:
        return empty
    correlate.recompute_all_edges(store)
    eligible = eligible_pairs(wikilink_graph(vault, store))
    if not eligible:
        return empty

    es = embed_store if (embed_store is not None and len(embed_store)) else None
    endpoints = sorted({e for pr in eligible for e in pr})
    if verbose:
        print(f"\nrerank A/B: {len(endpoints)} endpoints, pool {max(k, pool)} → top-{k} …")

    base_topk: dict[str, list[str]] = {}
    rr_topk: dict[str, list[str]] = {}
    empty_docs = 0
    for i, key in enumerate(endpoints):
        # Arm A re-derived at k (NOT pool[:k]): the facade's internal per-leg
        # pool scales with k, so pool[:k] can order differently than the gated
        # probe — the arms must differ only by the rerank pass.
        base_topk[key] = [r.path for r in related_notes(
            key, embed_store=es, cooccur_store=store, k=k)]
        pool_results = related_notes(
            key, embed_store=es, cooccur_store=store, k=max(k, pool))
        doc = note_document(key)
        if not doc:
            empty_docs += 1
        rr_topk[key] = [r.path for r in rerank_related(reranker, doc, pool_results, k=k)]
        if verbose and (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(endpoints)}")

    base_recall, base_mrr, base_rec = _score_pairs(eligible, base_topk)
    rr_recall, rr_mrr, rr_rec = _score_pairs(eligible, rr_topk)

    res = {
        "pairs_evaluated": len(eligible),
        "endpoints": len(endpoints),
        "empty_docs": empty_docs,
        "base_recall": base_recall, "base_mrr": base_mrr,
        "rerank_recall": rr_recall, "rerank_mrr": rr_mrr,
        "pairs_won": len(rr_rec - base_rec),
        "pairs_lost": len(base_rec - rr_rec),
    }
    if verbose:
        print(f"  fused    recall@{k} {base_recall:.1%}  mrr {base_mrr:.3f}")
        print(f"  reranked recall@{k} {rr_recall:.1%}  mrr {rr_mrr:.3f}  "
              f"(won +{res['pairs_won']} / lost -{res['pairs_lost']}, "
              f"empty docs {empty_docs}/{len(endpoints)})")
    return res
