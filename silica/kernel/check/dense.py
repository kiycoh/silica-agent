# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The dense leg of the check's absence floor (ADR-0110 point 6): one block's
best cosine against the notes of its pool, in the vault's own embed store.

Not a relatedness ranking, so not through the fusion facade: the check needs
one number to compare with a floor, and the passage search it would fuse with
carries no vectors on this index (`dense: no_vectors`, 2026-10-24).
"""
from __future__ import annotations

from typing import Any


def leg(embedder: Any):
    """`dense(text, pool) -> best cosine or None`, or None when the vault has
    no embed index. A failed embed call answers None for that block, which
    then waits for the model instead of being called absent."""
    from silica.kernel.recall.embed import get_store

    store = get_store()
    if not len(store):
        return None

    def dense(text: str, pool: set[str]) -> float | None:
        try:
            vec = embedder.embed([text], query=True)[0]
        except Exception:
            return None  # embedder down mid-check: this block's absence waits for the model
        top = store.cosine_top_k(vec, k=1, allow={p.removesuffix(".md") for p in pool})
        return float(top[0]["score"]) if top else None

    return dense
