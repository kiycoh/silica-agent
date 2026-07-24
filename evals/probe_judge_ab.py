"""Verify the content-match clause added to _JUDGE_BASE actually recovers the
measured judge false-negatives WITHOUT over-inflating.

Re-judges every answerable-wrong question of conv-26 / conv-47 through the
PRODUCT judge() (new rubric), and reports flips-to-correct per type. Calibration
check: recovered should land near the independent re-grade probe's counts
(conv-26 39, conv-47 7). Materially MORE recovered than that => the clause is
looser than intended (over-inflation); FEWER => it did not land in the product
judge. Regression of previously-CORRECT answers is not re-checked: the clause is
leniency-only (it can only turn a 'no' into 'yes'), so a correct answer cannot
flip to wrong. ponytail: skip the correct-sample re-judge, add it if the clause
ever grows a 'no' policy.

Run: PYTHONPATH=. uv run python evals/probe_judge_ab.py
"""
import collections
import json
import os
import sys

from evals.longmemeval.runner import judge

MODEL = "openrouter/deepseek/deepseek-v4-flash"
RUNS = [("conv-26", "bench/ab_c26_verbatim_agent.metrics.json", 39),
        ("conv-47", "bench/v1_c47_verbatim.agent.metrics.json", 7)]


def main() -> int:
    qa_by = {c["sample_id"]: c["qa"] for c in json.load(open("bench/locomo10.json"))}
    out = {}
    for conv, f, expect in RUNS:
        qa = qa_by[conv]
        qs = json.load(open(f))["questions"]
        wrong = [q for q in qs if q.get("correct") is False
                 and not q.get("abstention")]
        recovered = 0
        errs = 0
        by_type = collections.Counter()
        for q in wrong:
            qi = int(q["question_id"].rsplit("_q", 1)[1])
            e = qa[qi]
            v = judge(MODEL, q.get("question_type", ""), e.get("question", ""),
                      str(e.get("answer", "")), q.get("response", ""))
            if v is None:
                errs += 1
            elif v:
                recovered += 1
                by_type[q.get("question_type")] += 1
            print(f"[{conv}] {q['question_id']:<12} {q.get('question_type'):<11} "
                  f"{'RECOVER' if v else ('no' if v is False else 'ERR')}", flush=True)
        out[conv] = {"wrong": len(wrong), "recovered": recovered, "errs": errs,
                     "expected_fn": expect,
                     "by_type": dict(by_type)}
        print(f"[{conv}] recovered {recovered}/{len(wrong)} (probe expected ~{expect}, "
              f"errs {errs}) by_type={dict(by_type)}\n", flush=True)
    p = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "tmp",
                     "judge_ab.json")
    json.dump(out, open(p, "w"), indent=2)
    print("written:", p, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
