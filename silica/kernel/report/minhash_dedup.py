# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Embedder-free near-duplicate detection — the STABLE dedup leg.

MinHash over character k-shingles, deterministic and dependency-free (stdlib
hashlib only). The twin of the co-occurrence relatedness leg: when the embedding
index is empty or the embedder is down, this still catches near-duplicate
concepts so COLLISION does not silently let duplicates land in the vault.

MinHash idea ported from Graphify (github.com/safishamsi/graphify, MIT,
Copyright (c) 2026 Safi Shamsi). Their `_minhash.py` is a vectorised
datasketch-compatible drop-in with band-LSH for codebase-scale all-pairs dedup.
Two slices live here: the one-query-vs-vault lookup (`near_duplicates`, O(n)
scan, signatures memoized per run) and the all-pairs maintenance sweep
(`banded_duplicate_pairs`), which buckets by signature bands so only pairs
sharing a band are verified — the all-pairs loop it replaced was interpreted
O(n^2) and took /curate to minutes at a few thousand notes.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from random import Random

_MERSENNE = (1 << 61) - 1   # prime modulus for the (a·h + b) hash family
_MASK32 = (1 << 32) - 1
_K = 3                      # char-shingle width (short labels survive; mirrors Graphify)

Signature = tuple[int, ...]


@lru_cache(maxsize=None)
def _coeffs(num_perm: int) -> tuple[tuple[int, int], ...]:
    """Fixed-seed (a, b) permutation coefficients — same across calls/processes."""
    rng = Random(1)
    return tuple((rng.randint(1, _MERSENNE), rng.randint(0, _MERSENNE)) for _ in range(num_perm))


def _shingles(text: str, k: int = _K) -> set[str]:
    """Character k-grams of the normalised text."""
    s = " ".join(text.lower().split())
    if not s:
        return set()
    if len(s) < k:
        return {s}
    return {s[i : i + k] for i in range(len(s) - k + 1)}


def minhash_signature(text: str, *, num_perm: int = 64) -> Signature:
    """Return the MinHash signature of text. Empty text → empty signature."""
    shingles = _shingles(text)
    if not shingles:
        return ()
    hashed = [
        int.from_bytes(hashlib.sha1(sh.encode("utf-8")).digest()[:4], "little")
        for sh in shingles
    ]
    coeffs = _coeffs(num_perm)
    return tuple(
        min(((a * h + b) % _MERSENNE) & _MASK32 for h in hashed)
        for a, b in coeffs
    )


def estimate_jaccard(sig_a: Signature, sig_b: Signature) -> float:
    """Estimated Jaccard similarity = fraction of matching signature slots.

    An empty signature (empty text) is similar to nothing, so → 0.0.
    """
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return 0.0
    matches = sum(1 for x, y in zip(sig_a, sig_b) if x == y)
    return matches / len(sig_a)


def near_duplicates(
    query: str,
    corpus: dict[str, str],
    *,
    threshold: float = 0.7,
    num_perm: int = 64,
    sig_cache: dict[str, tuple[int, Signature]] | None = None,
) -> list[tuple[str, float]]:
    """Keys in corpus whose text is a near-duplicate of query, best first.

    Args:
        query:     the incoming concept text (name + excerpt).
        corpus:    {key: text} of existing notes to compare against.
        threshold: minimum estimated Jaccard to count as a near-duplicate.
        sig_cache: optional {key: (content_fingerprint, signature)} memo so the
                   caller can reuse corpus signatures across calls. Entries whose
                   fingerprint no longer matches the text are recomputed, so a
                   changed note is never matched against its stale signature.

    Returns [(key, score)] sorted by score descending; empty query → [].
    """
    q_sig = minhash_signature(query, num_perm=num_perm)
    if not q_sig:
        return []

    def corpus_sig(key: str, text: str) -> Signature:
        if sig_cache is None:
            return minhash_signature(text, num_perm=num_perm)
        fp = hash(text)
        hit = sig_cache.get(key)
        if hit is not None and hit[0] == fp:
            return hit[1]
        sig = minhash_signature(text, num_perm=num_perm)
        sig_cache[key] = (fp, sig)
        return sig

    hits = [
        (key, score)
        for key, text in corpus.items()
        if (score := estimate_jaccard(q_sig, corpus_sig(key, text))) >= threshold
    ]
    hits.sort(key=lambda kv: kv[1], reverse=True)
    return hits


def _integrate(f, lo: float, hi: float) -> float:
    """Midpoint rule, 10 slices — the collision-probability curves are smooth.
    Ports graphify's `_lsh_integrate` (their reason for hand-rolling: importing
    scipy.integrate for this costs a full scipy load at import time)."""
    n = 10
    w = (hi - lo) / n
    return sum(f(lo + (i + 0.5) * w) for i in range(n)) * w


@lru_cache(maxsize=None)
def _optimal_bands(num_perm: int, threshold: float) -> tuple[int, int]:
    """(bands, rows) minimizing false positives + false negatives at threshold.

    The datasketch parameter search: for each split of the signature into
    b bands of r rows, a pair with true Jaccard s shares a band with
    probability 1-(1-s^r)^b; integrate the miss mass above the threshold and
    the hit mass below it, keep the split with the least total error. Fixed
    (b, r) instead would under-recall right at the threshold, which for a
    dedup sweep is exactly where the interesting pairs sit.
    """
    best, best_err = (num_perm, 1), float("inf")
    for b in range(1, num_perm + 1):
        r = num_perm // b
        fp = _integrate(lambda s: 1.0 - (1.0 - s**r) ** b, 0.0, threshold)
        fn = _integrate(lambda s: (1.0 - s**r) ** b, threshold, 1.0)
        if (err := fp + fn) < best_err:
            best, best_err = (b, r), err
    return best


def banded_duplicate_pairs(
    sigs: dict[str, Signature],
    *,
    threshold: float = 0.7,
) -> list[tuple[str, str, float]]:
    """All near-duplicate pairs above threshold, via band-LSH.

    Bucket every signature by its bands, verify with estimate_jaccard only the
    pairs that share at least one bucket. Work is O(n·bands) + verification of
    the candidate pairs, instead of the n^2/2 all-pairs scan. Degenerate case
    (everything near-identical) verifies O(n^2) pairs — but then the OUTPUT is
    O(n^2), so no algorithm does better.

    PROBABILISTIC, unlike the all-pairs scan it replaced: a pair whose true
    similarity sits exactly at the threshold is caught with probability
    1-(1-s^r)^b (~89% at num_perm=64, t=0.6), rising steeply above it. The
    misses concentrate AT the threshold — acceptable here because every pair
    feeds a ternary judge, not a mechanical merge, and a missed borderline
    pair resurfaces on a later sweep with fresh signatures.

    Returns [(a, b, score)] with a < b, sorted (-score, a, b).
    """
    keys = [k for k in sorted(sigs) if sigs[k]]
    if len(keys) < 2:
        return []
    num_perm = len(sigs[keys[0]])
    bands, rows = _optimal_bands(num_perm, round(threshold, 4))

    buckets: dict[tuple[int, Signature], list[str]] = {}
    for k in keys:
        sig = sigs[k]
        if len(sig) != num_perm:
            continue  # a mixed-width signature can never match slot-for-slot
        for i in range(bands):
            band = sig[i * rows: (i + 1) * rows]
            buckets.setdefault((i, band), []).append(k)

    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, float]] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                pair = (a, b) if a < b else (b, a)
                if pair in seen:
                    continue
                seen.add(pair)
                score = estimate_jaccard(sigs[pair[0]], sigs[pair[1]])
                if score >= threshold:
                    out.append((pair[0], pair[1], score))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out
