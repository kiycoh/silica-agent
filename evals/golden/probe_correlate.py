# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""probe_correlate — masked-pair recovery lift (CORRELATE / ADR-0013).

Masked signal: note-to-note wikilinks. For each human body-link pair (A, B),
mask the link and ask whether the AUTOLINK machinery would still propose it —
comparing the expanded-only ranking (the pre-2026-07 baseline, kept only here)
against the direct note_edges UNION expanded population. The LIFT (union − expanded) is the number
that justifies the CORRELATE spec.

Embedder-free (cheap tier): no cosine band, no embeddings. Candidate populations
are text-derived and independent of wikilinks; the only wikilink-dependent step
is the >2-hop eligibility (a pair still reachable in ≤2 hops after masking — via
a shared hub — is not recoverable, so it is excluded, matching the real filter).

Informational, not gated: the harness's baseline-then-delta pattern records the
lift; the fusion regression is gated by classify.agreement / links.recall
(unchanged legs). Proxy precision (fraction of edges already wikilinked) is
reported but unknowable in absolute terms — humans under-link, so an unlinked
edge is the product, not an error.
"""
from __future__ import annotations

import contextlib

from silica.kernel.link.health import pair as _pair
from silica.kernel.link.health import wikilink_graph as _wikilink_graph

# Per-note expanded ranking depth — matches graph_report's _cooccur_ranking(k=10).
EXPANDED_K = 10

# The associative expansion lives here and not in relatedness.py: no production
# caller ever enabled it, and enabled it measured -7.86pp recall at 48x the
# cost (docs/specs/ppr-propagation.md:455; S4 of
# docs/audits/2026-09-17-paper-backed-simplifications.md, moved 2026-09-19).
# This probe keeps it as the arm its lift is measured against, and
# probe_ppr_phase2 swaps `expanded_profile` in as its negative control.
# Neighbour edges are associative, not direct membership: the discount keeps
# notes literally sharing concepts ahead of the ones reached by association.
EXPANSION_DISCOUNT = 0.25


def expanded_profile(cooccur_store, seeds, *, scope) -> dict[str, float]:
    """Seed profile plus each seed's co-occurrence neighbours at a discount.

    Same signature as `relatedness._profile_from_seeds`, so it can be rebound
    in its place.
    """
    if not seeds:
        return {}
    profile: dict[str, float] = dict(seeds)
    adj = cooccur_store.adjacency(scope=scope)
    for stem, weight in list(profile.items()):
        for neighbour, edge_weight in adj.get(stem, {}).items():
            profile[neighbour] = (
                profile.get(neighbour, 0.0) + weight * edge_weight * EXPANSION_DISCOUNT
            )
    return profile


@contextlib.contextmanager
def expansion_enabled():
    """Rebind the production profile seam to `expanded_profile` for the block.

    For the probes that still want the associative arm through the facade
    (evals/musique/runner.py --expand) without a parameter on the product.
    """
    import silica.kernel.recall.relatedness as R

    saved = R._profile_from_seeds
    R._profile_from_seeds = expanded_profile
    try:
        yield
    finally:
        R._profile_from_seeds = saved


def expanded_ranking(store, key: str, *, k: int) -> list[tuple[str, float]] | None:
    """`_cooccur_ranking` over the expanded profile of an indexed note."""
    from silica.kernel.recall.relatedness import _rank_cooccur_from_profile

    profile = expanded_profile(store, store.note_nodes(key), scope=None)
    return _rank_cooccur_from_profile(store, profile, k=k, blocked={key}, scope=None)

_EMPTY = {
    "pairs_evaluated": 0,
    "recall_expanded": 0.0,
    "recall_union": 0.0,
    "lift": 0.0,
    "lift_pairs": 0,
    "edges": 0,
    "edges_wikilinked_frac": 0.0,
}


def run(vault, store, *, verbose: bool = False) -> dict:
    if len(store) == 0:
        return dict(_EMPTY)

    # note_edges derive from the current contributions on read (self-contained:
    # the measurement never depends on whether the persisted store was reindexed).

    # Candidate populations — text-derived, no hop filter (applied per pair below).
    expanded_pairs: set[tuple[str, str]] = set()
    direct_pairs: set[tuple[str, str]] = set()
    for key in store.paths():
        for tgt, _w in (expanded_ranking(store, key, k=EXPANDED_K) or []):
            expanded_pairs.add(_pair(key, tgt))
        for tgt in store.note_edges_for(key):
            direct_pairs.add(_pair(key, tgt))

    adj = _wikilink_graph(vault, store)
    human_pairs: set[tuple[str, str]] = set()
    for a, nbrs in adj.items():
        for b in nbrs:
            human_pairs.add(_pair(a, b))

    evaluated = exp_rec = uni_rec = dir_rec = 0
    for a, b in human_pairs:
        # >2-hop eligibility: after masking a-b, a shared neighbour leaves a
        # 2-hop path (via hub) -> not recoverable -> excluded, per the real filter.
        if (adj.get(a, set()) - {b}) & (adj.get(b, set()) - {a}):
            continue
        evaluated += 1
        in_exp = (a, b) in expanded_pairs
        in_dir = (a, b) in direct_pairs
        exp_rec += in_exp
        dir_rec += in_dir
        uni_rec += in_exp or in_dir

    recall_expanded = round(exp_rec / evaluated, 4) if evaluated else 0.0
    recall_union = round(uni_rec / evaluated, 4) if evaluated else 0.0
    edges = len(direct_pairs)
    edges_wikilinked_frac = round(len(direct_pairs & human_pairs) / edges, 4) if edges else 0.0

    if verbose:
        print(f"\ncorrelate: recall expanded {exp_rec}/{evaluated} = {recall_expanded:.1%}, "
              f"union {uni_rec}/{evaluated} = {recall_union:.1%}, "
              f"LIFT +{uni_rec - exp_rec} ({recall_union - recall_expanded:+.1%})")
        print(f"  edges {edges}, direct-only recovered {dir_rec}, "
              f"already-wikilinked {edges_wikilinked_frac:.1%}")

    return {
        "pairs_evaluated": evaluated,
        "recall_expanded": recall_expanded,
        "recall_union": recall_union,
        "lift": round(recall_union - recall_expanded, 4),
        "lift_pairs": uni_rec - exp_rec,
        "edges": edges,
        "edges_wikilinked_frac": edges_wikilinked_frac,
    }
