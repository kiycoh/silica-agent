# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Embedding-based PROPOSED signals: duplicate pairs.

Every function degrades to [] when the embedding index is empty or unreadable.
They read stored vectors only and never construct an embedder, so a provider
that is down does not suppress the proposals: this module reaches nothing
under silica.agent, which is what lets the P2/DKB contract cover it without
an exemption.
"""
from __future__ import annotations

import logging

from silica.kernel.report.graph_report.models import DuplicatePair, VaultReport
from silica.kernel.recall.paths import in_folder

logger = logging.getLogger(__name__)




def _pair_key(d: DuplicatePair) -> tuple[float, str, str]:
    """Deterministic pair ordering — best score first, then path. Byte-stable output."""
    return (-d.score, d.source, d.target)


def _minhash_duplicate_pairs(report: VaultReport) -> list[DuplicatePair]:
    """Embedder-free near-duplicate pairs — the STABLE leg of the maintenance dedup.

    COLLISION already falls back to MinHash when the embedder is down
    (router/states/collision.py), so INGEST keeps protecting itself against
    duplicates. The maintenance path did not: `/dedup` and `/curate` read
    `duplicate_pairs`, which is pure cosine, so an embedder-less vault got an
    empty plan and reported itself coherent. Same signature scheme, same
    threshold as the collision leg.

    Pairs land in the BORDERLINE band only, never `confirmed`: an estimated
    Jaccard over character shingles is a surface-form signal on a different
    scale from cosine, so it must not be compared against the cosine taus. The
    curator feeds both bands to the ternary dedup judge, which reads the two
    bodies — so these still get judged, never mechanically merged.

    Band-LSH after one O(n) signing pass: only pairs sharing a signature band
    are verified (the all-pairs loop this replaced was interpreted O(n^2) and
    took /curate to minutes at a few thousand notes). The sweep is now
    probabilistic — pairs sitting exactly at the threshold have ~11% miss odds
    (see banded_duplicate_pairs) — which the judge-not-merge contract absorbs.
    """
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter
    from silica.kernel.report.minhash_dedup import banded_duplicate_pairs, minhash_signature

    threshold = getattr(CONFIG, "minhash_dup_threshold", 0.6)
    sigs: dict[str, tuple[int, ...]] = {}
    for nid in report.pagerank_map:  # every real node, always populated
        if not in_folder(nid, report.scope):
            continue
        try:
            content = DRIVER.read_note(nid).content
        except Exception:
            continue
        if not content:
            continue
        _data, _raw, body = frontmatter.split(content)
        # Title + body, mirroring the collision leg's "name + excerpt" query: two
        # notes on the same concept usually differ in surface title, and body
        # alone loses that signal.
        name = nid.rsplit("/", 1)[-1].removesuffix(".md")
        sig = minhash_signature(f"{name}\n{body}")
        if sig:
            sigs[nid] = sig

    out = [DuplicatePair(source=a, target=b, score=round(score, 4))
           for a, b, score in banded_duplicate_pairs(sigs, threshold=threshold)]
    out.sort(key=_pair_key)
    return out


def _compute_duplicate_pairs(
    report: VaultReport,
) -> tuple[list[DuplicatePair], list[DuplicatePair]]:
    """Find cosine-close note pairs (PROPOSED — not authoritative).

    Returns ``(borderline, confirmed)``:
      - borderline (τ_low < score < τ_high): topically related but distinct —
        the "link, don't merge" band. This is what the old report mislabelled
        as "possible duplicates"; it scales with vault size and domain coherence.
      - confirmed (score ≥ τ_high): bodies cover the same topic → likely true
        duplicates and genuine merge candidates.

    One cosine pass over the vault feeds both bands; a pair lands in exactly one.
    """
    store = None
    try:
        from silica.kernel.recall.embed import get_store

        store = get_store()
        if len(store) == 0:
            store = None
    except Exception as exc:
        logger.debug("graph_report: embeddings unavailable for dedup (%s)", exc)

    if store is None:
        # Degrade to the embedder-free leg instead of reporting zero duplicates:
        # an empty list here reads as "the vault is clean" and silently disarms
        # /dedup and /curate. Mirrors the abstention contract in relatedness.
        try:
            return _minhash_duplicate_pairs(report), []
        except Exception as exc:
            logger.debug("graph_report: minhash dedup leg also unavailable (%s)", exc)
            return [], []

    from silica.config import CONFIG

    tau_high = getattr(CONFIG, "sim_threshold_high", 0.85)
    tau_low = getattr(CONFIG, "sim_threshold_low", 0.75)

    borderline: list[DuplicatePair] = []
    confirmed: list[DuplicatePair] = []
    seen: set[tuple[str, str]] = set()

    scope = [p for p in store.paths() if in_folder(p, report.scope)]
    # One blocked matmul for the whole scope instead of one matvec per note — the
    # loop below only ever wants each note's single nearest neighbour, but paid a
    # full pass over the index for each. Same top-1 (see cosine_top_k_batch).
    try:
        neighbours = store.cosine_top_k_batch(scope, k=1)
    except Exception as exc:
        # Same contract as the missing-store branch above: [] would read as "the
        # vault is clean" and silently disarm /dedup and /curate.
        logger.debug("graph_report: cosine dedup leg failed (%s)", exc)
        return _minhash_duplicate_pairs(report), []

    for p in scope:
        candidates = neighbours.get(p)
        if not candidates:
            continue

        cand = candidates[0]
        tgt = cand["path"]
        score = cand.get("score", 0.0)

        if score <= tau_low:
            continue
        key = (min(p, tgt), max(p, tgt))
        if key in seen:
            continue
        seen.add(key)
        pair = DuplicatePair(source=p, target=tgt, score=round(score, 4))
        (confirmed if score >= tau_high else borderline).append(pair)

    borderline.sort(key=_pair_key)
    confirmed.sort(key=_pair_key)
    return borderline, confirmed
