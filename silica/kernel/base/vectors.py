# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Vector maths on plain lists, and a document's thematic centroid. No store, no model of its own."""
from __future__ import annotations

from typing import Any

import numpy as np


# Maximum characters of note content to embed (title + body prefix).
# Keeps embedding calls fast without losing most of the signal.
MAX_CHARS = 1200


# Theme-vector sampling budget: at most this many MAX_CHARS blocks are
# embedded per document, equispaced across the body (document_theme_vector).
_THEME_MAX_SEGMENTS = 8


def cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity in [−1, 1] between two vectors.

    Returns 0.0 if either vector is the zero vector (degenerate case).
    """
    if len(a) != len(b):
        return 0.0
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(va @ vb) / denom


def centroid(vectors: list[list[float]]) -> list[float]:
    """Component-wise mean of a list of vectors. Returns [] if empty or ragged."""
    if not vectors:
        return []
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        return []
    return np.mean(np.asarray(vectors, dtype=np.float64), axis=0).tolist()


# Theme vectors are requested twice per inbox file (RECON rerank + SALIENCE
# gate) with an identical cleaned body — cache by content so the second call
# is free. ponytail: crude clear-at-cap bound, fine for per-run lifetimes.
# (model, body sha1, segment_chars, max_segments) -> centroid.
_theme_cache: dict[tuple[str, str, int, int], list[float]] = {}


_THEME_CACHE_MAX = 64


def document_theme_vector(
    embedder: Any, body: str, *, segment_chars: int = MAX_CHARS,
    max_segments: int = _THEME_MAX_SEGMENTS,
) -> list[float]:
    """Thematic centroid of a document: embed a bounded sample of body segments,
    then average.

    Robust on long notes. Returns [] if embedder fails or body is empty.
    Cached per (model, body-hash, segment_chars, max_segments) — see
    _theme_cache above.

    Bodies over ``max_segments`` blocks are sampled equispaced rather than
    embedded in full: both consumers (keyphrase MMR, SALIENCE at τ=0.35) use
    this vector as a direction, not a faithful centroid, and walking a book
    segment block-by-block made RECON's cost linear in the note size (34
    embed inputs per 40k-char segment). ``max_segments=0`` disables sampling.
    """
    if not body.strip():
        return []
    import hashlib
    key = (
        getattr(embedder, "model", ""),
        hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest(),
        segment_chars,
        max_segments,
    )
    cached = _theme_cache.get(key)
    if cached is not None:
        return cached
    segs = [body[i:i + segment_chars] for i in range(0, len(body), segment_chars)] or [body]
    if max_segments and len(segs) > max_segments:
        step = len(segs) / max_segments
        segs = [segs[int(i * step)] for i in range(max_segments)]
    try:
        vecs = embedder.embed(segs)
    except Exception:
        return []
    vec = centroid(vecs)
    if vec:
        if len(_theme_cache) >= _THEME_CACHE_MAX:
            _theme_cache.clear()
        _theme_cache[key] = vec
    return vec
