# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Does the answer text survive a narrower window? — the WINDOW_CHARS lever.

Throwaway instrument, NOT product code. probe_recall_rank settled k (the tail
carries gold, k stays); the remaining payload lever is the per-block render
window (perception WINDOW_CHARS=3000). Session rank cannot evaluate it: rank
says which notes arrived, not whether the answer span survived windowing. The
right metric is gold_in_context — substring/token presence of the gold answer
in the rendered context — which is LLM-free (runner._gold_in_context).

The sweep is nearly free because windowing is POST-retrieval: best_windows is
a pure function of (body, query, width, n). One perceive() per question yields
full bodies; every grid cell is then recomputed offline from the same blocks.
Retrieval cost is paid once, not once per cell.

Grid: uniform widths (3000 baseline .. 750), cost-equal multi-window arms
(2x1500, 3x1000 vs 1x3000), and a rank-decayed schedule (3000/1500/750 over
bands 1-3/4-8/9+) — motivated by the rank probe's cost/value asymmetry: ranks
1-3 carry 82.6% of gold at 21% of payload, ranks 9-15 carry 3.2% at 45%.

Honesty caveats, printed with the numbers:
  * gic is substring/token-based: honest on VERBATIM content, returns None on
    derived golds ("7 days" is computed, not quoted), and false-negatives on
    paraphrased distill notes. Run the verbatim arm (HEAD ingest).
  * Block-level gold survival is conditioned on gold_in_body=True — a body
    that never contained the extractable answer cannot lose it to a window.
  * Per-block evidence strings are recorded in the JSON so the score-gap
    adaptive-N question is analyzable offline later; not analyzed here.

  uv run python -m evals.probe_window_sweep --data data/longmemeval_s.json \\
      --run-root bench/rank_probe --reuse-vaults --limit 150 \\
      --out bench/window_sweep_150.json
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from evals import _shared
from evals.longmemeval.runner import (
    _ABS,
    _gold_in_context,
    bind_vault,
    build_indexes,
    load_question_vault,
    question_vault,
)
from evals.probe_recall_rank import sample

# (max_rank, width) bands; max_rank=None is the open tail. Uniform = one band.
CELLS = [
    {"name": "1x3000", "n": 1, "bands": [[None, 3000]]},  # production baseline
    {"name": "1x2250", "n": 1, "bands": [[None, 2250]]},
    {"name": "1x1500", "n": 1, "bands": [[None, 1500]]},
    {"name": "1x1000", "n": 1, "bands": [[None, 1000]]},
    {"name": "1x750", "n": 1, "bands": [[None, 750]]},
    {"name": "2x1500", "n": 2, "bands": [[None, 1500]]},  # cost-equal to 1x3000
    {"name": "3x1000", "n": 3, "bands": [[None, 1000]]},  # cost-equal to 1x3000
    {"name": "decay-3000/1500/750", "n": 1,
     "bands": [[3, 3000], [8, 1500], [None, 750]]},
]
BASELINE = "1x3000"


def width_for(rank: int, bands: list[list]) -> int:
    for max_rank, width in bands:
        if max_rank is None or rank <= max_rank:
            return width
    return bands[-1][1]


def _band_of(rank: int) -> str:
    return "1-3" if rank <= 3 else ("4-8" if rank <= 8 else "9+")


def sweep_question(query: str, answer: str, blocks: list, gold_sids: set[str],
                   sid_of: dict[str, str]) -> dict:
    """All grid cells for one already-retrieved question. Pure: no I/O.

    `blocks` are perceive()'s NoteBlocks (full bodies). Returns per-cell
    {gic, chars} plus per-gold-block survival rows and block metadata.
    """
    from silica.kernel.recall.perception import NoteBlock, Perception
    from silica.kernel.recall.rerank import best_windows

    meta = []
    for rank, b in enumerate(blocks, 1):
        sid = sid_of.get(b.path, "")
        meta.append({"rank": rank, "session_id": sid, "gold": sid in gold_sids,
                     "evidence": b.evidence, "body_chars": len(b.body),
                     "gold_in_body": _gold_in_context(answer, b.body)
                     if sid in gold_sids else None})

    cells, survival = {}, []
    for cell in CELLS:
        nbs = []
        for rank, b in enumerate(blocks, 1):
            w = width_for(rank, cell["bands"])
            ex = "\n[…]\n".join(best_windows(b.body, query, w, cell["n"]))
            if not ex.strip():
                continue
            m = meta[rank - 1]
            if m["gold"] and m["gold_in_body"]:
                # Survival is per gold block, conditioned on the body ceiling.
                survival.append({"cell": cell["name"], "band": _band_of(rank),
                                 "survived": _gold_in_context(answer, ex) is True})
            nbs.append(NoteBlock(path=b.path, date=b.date, evidence=b.evidence,
                                 body=b.body, excerpt=ex, contested=b.contested))
        ctx = Perception(query=query, blocks=nbs).render()
        cells[cell["name"]] = {"gic": _gold_in_context(answer, ctx),
                               "chars": len(ctx)}
    return {"cells": cells, "survival": survival, "blocks": meta}


def aggregate(rows: list[dict]) -> dict:
    """Per-cell gic/cost + per-type breakdown + band survival. The rank probe
    showed a mean hides concentrated damage (-20pp on one type behind -3.4pp
    overall), so the per-type worst delta is a first-class column."""
    def gic_mean(sub: list[dict], cell: str) -> float | None:
        vals = [r["cells"][cell]["gic"] for r in sub
                if r["cells"][cell]["gic"] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r["question_type"]].append(r)

    out = {}
    base_types = {t: gic_mean(sub, BASELINE) for t, sub in by_type.items()}
    for cell in (c["name"] for c in CELLS):
        chars = [r["cells"][cell]["chars"] for r in rows]
        types = {t: gic_mean(sub, cell) for t, sub in sorted(by_type.items())}
        deltas = {t: round(types[t] - base_types[t], 4) for t in types
                  if types[t] is not None and base_types[t] is not None}
        surv: dict[str, list[bool]] = defaultdict(list)
        for r in rows:
            for s in r["survival"]:
                if s["cell"] == cell:
                    surv[s["band"]].append(s["survived"])
        out[cell] = {
            "gic": gic_mean(rows, cell),
            "mean_chars": round(statistics.mean(chars)) if chars else 0,
            "mean_tokens": round(statistics.mean(chars) / 4) if chars else 0,
            "by_type": types,
            "worst_type_delta": min(deltas.values()) if deltas else None,
            "worst_type": min(deltas, key=deltas.get) if deltas else None,
            "band_survival": {band: {"n": len(v), "rate": round(sum(v) / len(v), 4)}
                              for band, v in sorted(surv.items())},
        }
    return out


def _print(agg: dict, notes: list[str]) -> None:
    base = agg[BASELINE]
    print(f"\n{'cell':<22} {'gic':>7} {'Δgic':>8} {'tok':>6} {'Δtok%':>7}  worst-type Δ")
    for name, c in agg.items():
        dg = (f"{c['gic'] - base['gic']:+.4f}"
              if c["gic"] is not None and base["gic"] is not None else "n/a")
        dt = (f"{100 * (c['mean_tokens'] - base['mean_tokens']) / base['mean_tokens']:+.0f}%"
              if base["mean_tokens"] else "n/a")
        wt = (f"{c['worst_type_delta']:+.4f} ({c['worst_type']})"
              if c["worst_type_delta"] is not None else "")
        print(f"{name:<22} {str(c['gic']):>7} {dg:>8} {c['mean_tokens']:>6} {dt:>7}  {wt}")
    print("\ngold-block survival by rank band (conditioned on gold_in_body):")
    for name, c in agg.items():
        bands = "  ".join(f"{b}: {v['rate']} (n={v['n']})"
                          for b, v in c["band_survival"].items())
        print(f"  {name:<22} {bands}")
    for n in notes:
        print(f"  ! {n}")


def main(argv=None) -> int:
    from silica.config import CONFIG

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="longmemeval_{oracle,s,m}.json")
    ap.add_argument("--run-root", required=True, help="dir for the per-question vaults")
    ap.add_argument("--k", type=int, default=15, help="production retrieval k")
    ap.add_argument("--distill", action="store_true",
                    help="distiller arm (WARNING: gic false-negatives on paraphrase)")
    ap.add_argument("--reuse-vaults", action="store_true",
                    help="adopt populated vaults as-is (e.g. the rank probe's)")
    ap.add_argument("--no-embed", action="store_true", help="cooccur leg only")
    ap.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder")
    ap.add_argument("--limit", type=int, help="stratified sample size (see rank probe)")
    ap.add_argument("--seed", default="0", help="stratified-sample seed")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    data = [d for d in data if not d["question_id"].endswith(_ABS)
            and (d.get("answer_session_ids") or [])]
    if args.limit:
        data = sample(data, args.limit, args.seed)
    if not data:
        raise SystemExit("no answerable questions with answer_session_ids in --data")

    run_root = Path(args.run_root).expanduser()
    run_root.mkdir(parents=True, exist_ok=True)
    if not args.no_rerank:
        bind_vault(question_vault(run_root, data[0]["question_id"]))
        _shared.assert_reranker_live(CONFIG)

    from silica.kernel.recall.perception import perceive

    rows = []
    for i, inst in enumerate(data, 1):
        vault = question_vault(run_root, inst["question_id"])
        vault.mkdir(parents=True, exist_ok=True)
        bind_vault(vault)
        index = load_question_vault(vault, inst, distill=args.distill,
                                    reuse=args.reuse_vaults)
        build_indexes(embed=not args.no_embed, force=not args.reuse_vaults)
        # ONE retrieval; every cell recomputes windows from these bodies.
        p = perceive(inst["question"], now=inst.get("question_date", ""),
                     k=args.k, use_embedder=not args.no_embed,
                     use_rerank=not args.no_rerank, with_facts=False)
        row = sweep_question(inst["question"], str(inst["answer"]), p.blocks,
                             set(inst.get("answer_session_ids") or []),
                             {rel: m.get("session_id", "") for rel, m in index.items()})
        row.update(question_id=inst["question_id"], question_type=inst["question_type"])
        rows.append(row)
        print(f"[{i}/{len(data)}] {inst['question_id']} "
              f"base_gic={row['cells'][BASELINE]['gic']}", flush=True)

    agg = aggregate(rows)
    none_n = sum(1 for r in rows if r["cells"][BASELINE]["gic"] is None)
    notes = [f"{none_n}/{len(rows)} questions have derived golds (gic=None, excluded)."]
    if args.distill:
        notes.append("DISTILL ARM: paraphrased notes false-negative gic; deltas "
                     "are trustworthy only against this arm's own baseline.")
    tail = agg[BASELINE]["band_survival"].get("9+", {"n": 0})
    if tail["n"] == 0:
        notes.append("VACUITY: no measurable gold block in band 9+ — the decay "
                     "tail width is untested by this sample.")
    notes.append("per-block evidence recorded in the JSON: the score-gap "
                 "adaptive-N question is analyzable offline from it.")
    _print(agg, notes)
    doc = {"probe": "window_sweep",
           "provenance": _shared.provenance(args.data),
           "config": {"k": args.k, "seed": args.seed, "distill": args.distill,
                      "reuse": args.reuse_vaults, "embedder": not args.no_embed,
                      "reranker": not args.no_rerank,
                      "cells": CELLS, "baseline": BASELINE},
           "report": agg, "notes": notes, "questions": rows}
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
