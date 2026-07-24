"""Symmetric content-match re-grade of open-domain + multi-hop wrong-answerable
for conv-26 and conv-47, to finish decomposing the 0.592-vs-0.840 gap.

Single-hop (c26 19 FN / c47 0 FN) and temporal (c26 4 / c47 3) are already
done; this fills the remaining answerable buckets so a TRUE corrected accuracy
per conv can be computed symmetrically. Reuses the lenient content-match judge
from probe_single_hop_regrade (asserts-gold-fact => match; omit/contradict/
abstain => nomatch). Writes a JSON summary; slow (deepseek reasoning routing),
run in background.

Run: PYTHONPATH=. uv run python evals/probe_regrade_sweep.py
"""
import collections
import json
import os
import sys

from evals.probe_single_hop_regrade import regrade

RUNS = [("conv-26", "bench/ab_c26_verbatim_agent.metrics.json"),
        ("conv-47", "bench/v1_c47_verbatim.agent.metrics.json")]
TYPES = ["open-domain", "multi-hop"]
# already-measured judge-FN from prior probes, to fold into the corrected number
PRIOR_FN = {"conv-26": {"single-hop": 19, "temporal": 4},
            "conv-47": {"single-hop": 0, "temporal": 3}}


def main() -> int:
    qa_by = {c["sample_id"]: c["qa"] for c in json.load(open("bench/locomo10.json"))}
    out = {}
    for conv, f in RUNS:
        qa = qa_by[conv]
        qs = json.load(open(f))["questions"]
        ans = [q for q in qs if not q.get("abstention")]
        cur_correct = sum(1 for q in ans if q.get("correct"))
        base = cur_correct / len(ans) if ans else 0
        sweep_fn = 0
        per_type = {}
        for t in TYPES:
            wrong = [q for q in qs if q.get("question_type") == t
                     and q.get("correct") is False]
            fn = 0
            tags = collections.Counter()
            for q in wrong:
                qi = int(q["question_id"].rsplit("_q", 1)[1])
                e = qa[qi]
                m, tag = regrade(e.get("question", ""), str(e.get("answer", "")),
                                 q.get("response", ""))
                verdict = "JUDGE-FN" if m else ("wrong" if m is False else "ERR")
                if m:
                    fn += 1
                tags[(verdict, tag)] += 1
                print(f"[{conv}] {q['question_id']:<12} {t:<11} {verdict:<8} "
                      f"{tag:<12} gold={str(e.get('answer',''))[:34]!r}", flush=True)
            per_type[t] = {"wrong": len(wrong), "judge_fn": fn,
                           "tags": {f"{v}:{tg}": n for (v, tg), n in tags.items()}}
            sweep_fn += fn
            print(f"[{conv}] {t}: judge-FN {fn}/{len(wrong)}", flush=True)
        prior = sum(PRIOR_FN.get(conv, {}).values())
        total_fn = sweep_fn + prior
        corr = (cur_correct + total_fn) / len(ans) if ans else 0
        out[conv] = {"answerable": len(ans), "base_acc": round(base, 4),
                     "sweep_judge_fn": sweep_fn, "prior_judge_fn": prior,
                     "total_judge_fn": total_fn, "corrected_acc": round(corr, 4),
                     "per_type": per_type}
        print(f"[{conv}] base {base:.4f} -> corrected {corr:.4f} "
              f"(sweep +{sweep_fn}, prior +{prior}, total +{total_fn})\n", flush=True)

    c26, c47 = out["conv-26"], out["conv-47"]
    out["_gap"] = {
        "raw": round(c47["base_acc"] - c26["base_acc"], 4),
        "corrected": round(c47["corrected_acc"] - c26["corrected_acc"], 4),
    }
    print("GAP raw:", out["_gap"]["raw"], "-> corrected:", out["_gap"]["corrected"],
          flush=True)
    p = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "tmp",
                     "regrade_sweep.json")
    json.dump(out, open(p, "w"), indent=2)
    print("written:", p, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
