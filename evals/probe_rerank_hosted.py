# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Hosted reranker A/B: a metered cross-encoder on OpenRouter against no rerank
and against the local reference, on one shared first stage.

Why this exists: the hosted CPU host runs no reranker (host.env.example, "No
reranker on a CPU host"), and the cheapest way to give it one is a metered
endpoint, which speaks the same /rerank protocol `Reranker` already does. The
golden `--rerank-ab` measures one arm per run and keeps no abstention count, so
a 429 from the metered endpoint silently scores as the fused order (seen on the
first run, 2026-10-14). This runs every arm on the same pool, retries an
abstention with backoff, counts what still abstains, and keeps per-pair ranks
for the head-to-head.

Protocol is probe_fusion.run_rerank_ab's: masked wikilink pairs, query =
link_query(endpoint), reorder-only top-K, so recall is invariant and MRR and
per-pair rank wins carry the result. The query is a note TITLE (the link path),
not a chat question: this measures the cross-encoder, not search_context.

Usage:
  OPENROUTER_API_KEY=... uv run python -m evals.probe_rerank_hosted VAULT OUT.json
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

from evals.golden.probe_fusion import _pair_ranks, _score_pairs
from evals.golden.runner import _open_stores
from silica.agent.providers import Reranker
from silica.kernel.link.health import K, eligible_pairs, wikilink_graph
from silica.kernel.recall.relatedness import related_notes
from silica.kernel.recall.rerank import link_query, rerank_related

HOSTED = ("https://openrouter.ai/api/v1", "voyageai/rerank-3-lite")
LOCAL = ("http://127.0.0.1:1235/v1", "local")  # whatever rerank_serve loaded


class Counted:
    """Retries an abstention with backoff and counts calls that still abstain."""

    def __init__(self, inner: Reranker, tries: int = 6):
        self.inner, self.tries = inner, tries
        self.failure: str | None = None
        self.calls = self.retries = self.abstained = 0
        self.ms: list[float] = []

    def scores(self, query: str, docs: list[str]) -> list[float] | None:
        self.calls += 1
        t = time.time()
        for attempt in range(self.tries):
            s = self.inner.scores(query, docs)
            if s is not None:
                self.failure = None
                self.ms.append((time.time() - t) * 1000)
                return s
            self.retries += 1
            time.sleep(min(2 ** attempt, 20))
        self.abstained += 1
        self.failure = self.inner.failure
        return None


def sign_p(won: int, lost: int) -> float:
    """Two-sided exact sign test over the non-tied pairs."""
    n = won + lost
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(won, lost) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def main(argv: list[str]) -> int:
    vault, out = Path(argv[0]).expanduser(), Path(argv[1])
    arms = {
        "hosted": Counted(Reranker(*HOSTED, api_key=os.environ["OPENROUTER_API_KEY"], timeout=15)),
        "local": Counted(Reranker(*LOCAL, api_key="none", timeout=15)),
    }
    store, es = _open_stores(vault)
    eligible = eligible_pairs(wikilink_graph(vault, store))
    endpoints = sorted({e for pr in eligible for e in pr})
    topk: dict[str, dict[str, list[str]]] = {"fused": {}, **{a: {} for a in arms}}
    for i, key in enumerate(endpoints):
        results = related_notes(key, embed_store=es, cooccur_store=store, k=K)
        topk["fused"][key] = [r.path for r in results]
        q = link_query(key)
        for name, rr in arms.items():
            topk[name][key] = [r.path for r in rerank_related(rr, q, list(results), k=K)]
        if (i + 1) % 100 == 0:
            print(f"{i + 1}/{len(endpoints)}", flush=True)

    ranks = {a: _pair_ranks(eligible, t) for a, t in topk.items()}
    worst = float("inf")

    def h2h(a: str, b: str) -> dict:
        won = sum(1 for p in eligible if (ranks[a][p] or worst) < (ranks[b][p] or worst))
        lost = sum(1 for p in eligible if (ranks[a][p] or worst) > (ranks[b][p] or worst))
        return {"won": won, "lost": lost, "ties": len(eligible) - won - lost, "p": sign_p(won, lost)}

    doc: dict = {"vault": str(vault), "pairs": len(eligible), "endpoints": len(endpoints),
                 "k": K, "hosted_model": HOSTED[1], "arms": {}}
    for a, t in topk.items():
        rec, mrr, _ = _score_pairs(eligible, t)
        row: dict = {"recall": rec, "mrr": mrr}
        if a in arms:
            c = arms[a]
            ms = sorted(c.ms)
            row |= {"vs_fused": h2h(a, "fused"), "calls": c.calls, "retries": c.retries,
                    "abstained": c.abstained, "median_ms": round(ms[len(ms) // 2]) if ms else None}
        doc["arms"][a] = row
    doc["hosted_vs_local"] = h2h("hosted", "local")
    out.write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
