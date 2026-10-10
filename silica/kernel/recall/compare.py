# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Two parts of the vault side by side: what characterises each, what puts
them in touch (ADR-0045, fourth pass).

Terms come from the co-occurrence store as RATES (notes carrying the stem over
notes in the set) and are scored against the other side, rate_a * log(rate_a /
rate_b), with a floor and a ceiling. The class-based TF-IDF the area labels use
was measured on the 728-note test vault (2026-09-22) and put "dati", "valore"
and "funzione" on both sides of Matematica vs Informatica: with two or three
classes its idf barely separates. The ratio put statistica, distribuzione,
varianza on one side and programmazione, sistema, linguaggio on the other.

No shared terms: measured generic twice (pairs of notes, then pairs of folders:
"dati", "definizione", "esempio", "può"). Contact is the wikilinks and the k-NN
pairs that cross, which a reader can open.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


def _pair(x: str, y: str) -> tuple[str, str]:
    return (x, y) if x < y else (y, x)


def compare_sets(
    a: set[str], b: set[str], *, store: Any, vault_size: int,
    nodes: list[dict], edges: list[dict], sim: list[dict],
    k: int = 8, floor: float = 0.05, ceiling: float = 0.25,
) -> dict:
    """`a`, `b`: note ids (graph keys). `store`: the co-occurrence store, or
    None for no terms. `vault_size`: notes in the whole vault, the ceiling's
    denominator. `edges`: the wikilink layer; `sim`: the k-NN layer."""
    from silica.kernel.recall.cooccurrence import cooccur_key

    def rates(ids: set[str]) -> dict[str, float]:
        keys = {cooccur_key(p) for p in ids}
        out: dict[str, float] = {}
        if not store or not keys:
            return out
        for stem, notes in store.stem_postings().items():
            c = sum(1 for p in notes if p in keys)
            if c:
                out[stem] = c / len(keys)
        return out

    ra, rb = rates(a), rates(b)
    eps = 1.0 / max(len(a), len(b), 1)
    # A stem carried by more than `ceiling` of the notes OUTSIDE both sets
    # characterises neither: it is the vault's own vocabulary ("dati" sits in
    # 38% of the test vault). Measured against the rest and not the whole, or a
    # term owned by one big side would be its own ceiling.
    common: set[str] = set()
    both = {cooccur_key(p) for p in a | b}
    rest_n = max(vault_size - len(both), 0)
    if store and rest_n:
        for stem, notes in store.stem_postings().items():
            outside = sum(1 for p in notes if p not in both)
            if outside / rest_n > ceiling:
                common.add(stem)

    def side(rx: dict[str, float], ry: dict[str, float]) -> list[str]:
        sc = {t: rx[t] * math.log((rx[t] + eps) / (ry.get(t, 0.0) + eps))
              for t in rx if rx[t] >= floor and t not in common}
        top = sorted(sc.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [store.node_label(t) for t, s in top if s > 0]

    def crossing(layer: Iterable[dict], kind: str) -> list[tuple[str, str]]:
        seen: set[tuple[str, str]] = set()
        for e in layer:
            if e.get("type") != kind:
                continue
            x, y = e.get("from"), e.get("to")
            if (x in a and y in b) or (x in b and y in a):
                seen.add((x, y) if x in a else (y, x))
        return sorted(seen)

    return {
        "a": {"size": len(a), "terms": side(ra, rb)},
        "b": {"size": len(b), "terms": side(rb, ra)},
        "links": crossing(edges, "EXTRACTED"),
        "knn": crossing(sim, "SIMILAR"),
    }
