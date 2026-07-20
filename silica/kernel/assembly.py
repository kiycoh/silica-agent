# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Read-time assembly (IWE-informed): reconstitute atomic notes into assembled
context at recall time. Pure module — no I/O, no LLM. Callers inject edge and
body readers so the logic is unit-testable without a vault.

Three ported ideas (spec 2026-07-21): directional expansion (1.1), squash by
hub (1.3), breadcrumb (2.3). Ranking is NOT touched here: seeds arrive already
ranked/reranked; neighbours enter as periphery and are never re-ranked.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Unit:
    """One assemblable piece of text: a ranked seed or a periphery neighbour."""
    path: str
    text: str
    is_seed: bool
    rank: int


@dataclass
class Truncation:
    """What the budget kept vs dropped (IWE Truncation style)."""
    kept: int = 0
    dropped: list[str] = field(default_factory=list)


def fill_budget(
    seeds: list[Unit], periphery: list[Unit], *, budget: int
) -> tuple[list[Unit], Truncation]:
    """Seeds first (never trimmed), then periphery by ascending rank until the
    char ceiling. Metering is by len(text) — chars, no tokenizer (spec Dec. 4).
    """
    kept: list[Unit] = list(seeds)
    used = sum(len(u.text) for u in kept)
    dropped: list[str] = []
    for u in sorted(periphery, key=lambda x: x.rank):
        if used + len(u.text) <= budget:
            kept.append(u)
            used += len(u.text)
        else:
            dropped.append(u.path)
    return kept, Truncation(kept=len(kept), dropped=dropped)
