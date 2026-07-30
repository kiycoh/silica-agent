# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Where does gold sit in the ranking? — the payload lever, measured LLM-free.

Throwaway instrument, NOT product code. `silica_recall` costs 8-10k tokens per
call on a real vault, and the cost is `DEFAULT_K=15` x `WINDOW_CHARS=3000`
(perception.py). Cutting k is only free if the notes it drops never carried the
answer. An end-to-end A/B cannot tell you that: the LME accuracy metric sits at
a 0.9823 ceiling with the gate at 100%, and identical runs have varied 9-vs-34
tool calls, so one run decides nothing. Rank position does tell you, exactly,
with no answer model and no judge.

Four measures, one of them a vacuity check:

  1. Sessions per question vault — capping at N is trivially free for any vault
     that holds <= N notes. If the haystack is small the whole probe is vacuous.
  2. Rank histogram of the gold sessions at production k.
  3. cap@N curve — recall@N and the rendered context cost for N = 1..k, hence
     the free-cut point: the largest N whose recall still equals recall@k.
  4. --verify-k — the same measure through a real `perceive(k=N)`.

Measures 3 and 4 are NOT the same lever and the report says so:

  * cap@N  = retrieve k, rerank k, RENDER N. One run gives the whole curve.
  * k=N    = retrieve N, rerank N. `facade_retrieve` truncates the fused
             first stage to k BEFORE the cross-encoder sees it, so a smaller k
             hands the reranker a smaller candidate pool and can surface a
             document that k=15 never scored. cap@8 != k=8 in general.

So the curve decides a RENDER cap safely; a real k change needs --verify-k.

  uv run python -m evals.probe_recall_rank \\
      --data longmemeval_oracle.json --run-root bench/rank_probe --limit 20
  # frozen corpus, no distiller re-roll, indexes reused:
  uv run python -m evals.probe_recall_rank --data ... --run-root bench/lme18_std \\
      --distill --reuse-vaults --verify-k 8

Requires an embedder + reranker for the default arms (--no-embed / --no-rerank
degrade to the co-occurrence leg). No LLM: nothing here answers or judges.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import Counter
from pathlib import Path

from evals import _shared
from evals.longmemeval.runner import (
    _ABS,
    bind_vault,
    build_indexes,
    load_question_vault,
    question_vault,
)


def sample(data: list[dict], n: int, seed: str) -> list[dict]:
    """`n` questions stratified by question_type, proportional, deterministic.

    longmemeval_s is GROUPED BY TYPE on disk: a head slice of 100 answerable
    questions returns only single-session-user + multi-session, 2 of the 6
    types, and the types differ in exactly what this probe measures (a
    multi-session question carries up to 6 gold sessions, a single-session one
    carries 1). An unstratified --limit would answer the rank question on the
    easy half of the benchmark and never say so.
    """
    by_type: dict[str, list[dict]] = {}
    for q in sorted(data, key=lambda q: q["question_id"]):
        by_type.setdefault(q["question_type"], []).append(q)
    rng = random.Random(seed)
    out: list[dict] = []
    for qtype in sorted(by_type):
        share = round(n * len(by_type[qtype]) / len(data))
        out.extend(rng.sample(by_type[qtype], min(share, len(by_type[qtype]))))
    # Rounding can leave the draw short: top up from what is left, seeded.
    taken = {q["question_id"] for q in out}
    remaining = [q for q in data if q["question_id"] not in taken]
    rng.shuffle(remaining)
    out.extend(remaining[: max(0, n - len(out))])
    return sorted(out[:n], key=lambda q: q["question_id"])


def _retrieve(inst: dict, *, k: int, use_embedder: bool, use_rerank: bool,
              win_kw: dict) -> tuple[list[str], list[int]]:
    """(gold-or-None session id per rank, cumulative rendered chars per rank).

    Assumes the vault is already bound and indexed. `with_facts=False`: the
    episodic block is a fixed per-call cost independent of k, and the verbatim
    arms never carry one (runner.py gates it on --distill).
    """
    from silica.kernel.recall.perception import Perception, perceive

    p = perceive(inst["question"], now=inst.get("question_date", ""), k=k,
                 use_embedder=use_embedder, use_rerank=use_rerank,
                 with_facts=False, **win_kw)
    # Cost via the product renderer, not a re-implementation of the header
    # format — the number has to be the bytes the tool would actually return.
    chars = [len(Perception(query=inst["question"], blocks=p.blocks[:n]).render())
             for n in range(1, len(p.blocks) + 1)]
    return [b.path for b in p.blocks], chars


def _row(inst: dict, run_root: Path, *, k: int, use_embedder: bool,
         use_rerank: bool, distill: bool, reuse: bool, win_kw: dict,
         verify_ks: list[int]) -> dict:
    qid = inst["question_id"]
    vault = question_vault(run_root, qid)
    vault.mkdir(parents=True, exist_ok=True)
    bind_vault(vault)
    index = load_question_vault(vault, inst, distill=distill, reuse=reuse)
    build_indexes(embed=use_embedder, force=not reuse)

    sid_of = {rel: meta.get("session_id", "") for rel, meta in index.items()}
    gold = set(inst.get("answer_session_ids") or [])

    rels, chars = _retrieve(inst, k=k, use_embedder=use_embedder,
                            use_rerank=use_rerank, win_kw=win_kw)
    ranked = [sid_of.get(r, "") for r in rels]
    gold_ranks = [i for i, sid in enumerate(ranked, 1) if sid in gold]

    verified = {}
    for vk in verify_ks:
        v_rels, _ = _retrieve(inst, k=vk, use_embedder=use_embedder,
                              use_rerank=use_rerank, win_kw=win_kw)
        v_sids = {sid_of.get(r, "") for r in v_rels}
        verified[str(vk)] = len(gold & v_sids) / len(gold) if gold else None
    return {
        "question_id": qid,
        "question_type": inst["question_type"],
        "sessions": len(index),
        "retrieved": len(rels),
        "gold_n": len(gold),
        "gold_ranks": gold_ranks,          # [] = gold never retrieved at this k
        "cum_chars": chars,
        "verified_recall": verified,
    }


def _curve(rows: list[dict], k: int) -> list[dict]:
    """recall@N + mean rendered cost, N = 1..k. recall@N is the mean over
    questions of the fraction of that question's gold sessions inside rank N."""
    out = []
    for n in range(1, k + 1):
        recalls = [sum(1 for r in row["gold_ranks"] if r <= n) / row["gold_n"]
                   for row in rows if row["gold_n"]]
        # A question that returned fewer than n blocks costs what it returned.
        costs = [row["cum_chars"][min(n, len(row["cum_chars"])) - 1]
                 for row in rows if row["cum_chars"]]
        out.append({
            "n": n,
            "recall": round(statistics.mean(recalls), 4) if recalls else None,
            "mean_chars": round(statistics.mean(costs)) if costs else 0,
            "mean_tokens": round(statistics.mean(costs) / 4) if costs else 0,
        })
    return out


def _report(rows: list[dict], curve: list[dict], k: int, verify_ks: list[int]) -> dict:
    sessions = [r["sessions"] for r in rows]
    hist = Counter(rank for r in rows for rank in r["gold_ranks"])
    at_k = curve[-1]["recall"] if curve else None
    free_cut = k
    if at_k is not None:
        for c in curve:
            if c["recall"] is not None and c["recall"] >= at_k:
                free_cut = c["n"]
                break
    notes = []
    if sessions and min(sessions) <= k:
        thin = sum(1 for s in sessions if s <= k)
        notes.append(f"VACUITY: {thin}/{len(sessions)} vaults hold <= k={k} "
                     "sessions, so k does not bind on them at all.")
    if curve and curve[0]["recall"] == at_k:
        notes.append("VACUITY: recall@1 already equals recall@k — gold is always "
                     "rank 1 here, so this corpus cannot discriminate a cap.")
    if at_k is not None and at_k < 1.0:
        notes.append(f"recall@k={at_k}: the residual miss is a retrieval failure "
                     "no choice of N recovers.")
    missing = sum(1 for r in rows if r["gold_n"] and not r["gold_ranks"])
    if missing:
        notes.append(f"{missing}/{len(rows)} questions never retrieved any gold "
                     "session at this k.")
    if verify_ks:
        for vk in verify_ks:
            vals = [r["verified_recall"].get(str(vk)) for r in rows
                    if r["verified_recall"].get(str(vk)) is not None]
            cap = next((c["recall"] for c in curve if c["n"] == vk), None)
            true_k = round(statistics.mean(vals), 4) if vals else None
            notes.append(f"k={vk}: true recall {true_k} vs cap@{vk} {cap} — "
                         "a gap is the stage-1 truncation, not noise.")
    else:
        notes.append("no --verify-k: the curve decides a RENDER cap only, not a "
                     "change to DEFAULT_K (stage 1 truncates before the rerank).")
    return {
        "n_questions": len(rows),
        # Sample composition is part of the result: a type-skewed draw answers
        # the rank question on whichever half of the benchmark it happened to hit.
        "types": dict(sorted(Counter(r["question_type"] for r in rows).items())),
        "gold_sessions_total": sum(r["gold_n"] for r in rows),
        "sessions_per_vault": {
            "mean": round(statistics.mean(sessions), 1) if sessions else 0,
            "min": min(sessions) if sessions else 0,
            "max": max(sessions) if sessions else 0,
        },
        "k": k,
        "gold_rank_histogram": {str(r): hist[r] for r in sorted(hist)},
        "curve": curve,
        "recall_at_k": at_k,
        "free_cut": free_cut,
        "free_cut_tokens_saved": (curve[-1]["mean_tokens"]
                                  - next(c["mean_tokens"] for c in curve
                                         if c["n"] == free_cut)) if curve else 0,
        "notes": notes,
    }


def _print(rep: dict) -> None:
    s = rep["sessions_per_vault"]
    print(f"\nquestions {rep['n_questions']} | gold sessions {rep['gold_sessions_total']} "
          f"| sessions/vault mean {s['mean']} min {s['min']} max {s['max']} | k={rep['k']}")
    print(f"types: {rep['types']}")
    print(f"gold rank histogram: {rep['gold_rank_histogram']}")
    print(f"\n{'N':>3}  {'recall@N':>9}  {'mean chars':>11}  {'~tokens':>8}")
    for c in rep["curve"]:
        print(f"{c['n']:>3}  {str(c['recall']):>9}  {c['mean_chars']:>11}  {c['mean_tokens']:>8}")
    print(f"\nrecall@k = {rep['recall_at_k']} | free cut at N={rep['free_cut']} "
          f"(saves ~{rep['free_cut_tokens_saved']} tokens/call)")
    for n in rep["notes"]:
        print(f"  ! {n}")


def main(argv=None) -> int:
    from silica.config import CONFIG

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="longmemeval_{oracle,s,m}.json")
    ap.add_argument("--run-root", required=True, help="dir for the per-question vaults")
    ap.add_argument("--k", type=int, default=15,
                    help="production k under measurement (perception DEFAULT_K)")
    ap.add_argument("--verify-k", type=int, action="append", default=[],
                    help="also run a REAL perceive(k=N); repeatable")
    ap.add_argument("--distill", action="store_true", help="distiller ingest arm")
    ap.add_argument("--reuse-vaults", action="store_true",
                    help="adopt populated vaults as-is (frozen corpus)")
    ap.add_argument("--windows", type=int)
    ap.add_argument("--window-chars", type=int)
    ap.add_argument("--no-embed", action="store_true", help="cooccur leg only")
    ap.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder")
    ap.add_argument("--limit", type=int,
                    help="sample size, STRATIFIED by question_type (the corpus "
                         "is grouped by type on disk, so a head slice is biased)")
    ap.add_argument("--seed", default="0", help="stratified-sample seed")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    # Abstention golds are synthetic markers absent from the haystack: a rank is
    # undefined for them, not a miss (same rule as runner.aggregate).
    data = [d for d in data if not d["question_id"].endswith(_ABS)
            and (d.get("answer_session_ids") or [])]
    if args.limit:
        data = sample(data, args.limit, args.seed)
    if not data:
        raise SystemExit("no answerable questions with answer_session_ids in --data")

    win_kw = {}
    if args.windows is not None:
        win_kw["windows"] = args.windows
    if args.window_chars is not None:
        win_kw["window_chars"] = args.window_chars

    run_root = Path(args.run_root).expanduser()
    run_root.mkdir(parents=True, exist_ok=True)
    if not args.no_rerank:
        # Bind one vault first: the guard resolves the reranker through CONFIG.
        bind_vault(question_vault(run_root, data[0]["question_id"]))
        _shared.assert_reranker_live(CONFIG)

    rows = []
    for i, inst in enumerate(data, 1):
        rows.append(_row(inst, run_root, k=args.k, use_embedder=not args.no_embed,
                         use_rerank=not args.no_rerank, distill=args.distill,
                         reuse=args.reuse_vaults, win_kw=win_kw,
                         verify_ks=args.verify_k))
        print(f"[{i}/{len(data)}] {inst['question_id']} "
              f"gold_ranks={rows[-1]['gold_ranks']}", flush=True)

    rep = _report(rows, _curve(rows, args.k), args.k, args.verify_k)
    doc = {"probe": "recall_rank",
           "provenance": _shared.provenance(args.data),
           "config": {"k": args.k, "verify_k": args.verify_k, "seed": args.seed,
                      "distill": args.distill, "reuse": args.reuse_vaults,
                      "embedder": not args.no_embed,
                      "reranker": not args.no_rerank, **win_kw},
           "report": rep, "questions": rows}
    _print(rep)
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
