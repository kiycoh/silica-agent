"""Does the content-match judge tighten the ~4pp single-run noise floor?

Isolate the JUDGE's contribution to the floor: hold the responses FIXED (the
stored conv-26 answerable answers, no answer re-run) and re-judge K times with
the OLD rubric vs the NEW content-match rubric, unpinned provider. The floor
that hurts A/B resolvability is the run-to-run swing of reported accuracy; with
fixed responses that swing is entirely the judge. If NEW's spread < OLD's, the
content-match rubric tightens the judge-driven floor.

ponytail: bounded to a deterministic strided sample of conv-26 answerable (the
floor-sensitive conv), K=3. Full 152x2x3 is ~900 slow calls (~$130) for a
confirmation; run the full set only if this sample's OLD/NEW gap is marginal.

Run: PYTHONPATH=. uv run python evals/probe_judge_floor.py
"""
import json
import os
import sys
import time

from silica.agent.llm import call_llm

MODEL = "openrouter/deepseek/deepseek-v4-flash"
METRICS = "bench/ab_c26_verbatim_agent.metrics.json"
K = 3
SAMPLE = 48

# OLD = _JUDGE_BASE before the content-match clause (kept verbatim for the A/B).
OLD_BASE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, answer yes. If the "
    "response only contains a subset of the information required by the answer, "
    "answer no."
)
NEW_BASE = OLD_BASE + (
    " Judge on CONTENT, not wording or length: answer yes when the response "
    "asserts the correct answer even if it is more verbose, reformatted (bullets "
    "or prose), or paraphrased, and even if it adds other correct detail; extra "
    "correct information never makes a response wrong. Answer no only when the "
    "response omits the required fact, states a contradictory fact, or hedges "
    "without committing to it."
)


def judge_once(base: str, q: str, gold: str, resp: str):
    prompt = (f"{base}\n\nQuestion: {q}\nCorrect Answer: {gold}\n"
              f"Model Response: {resp}\n\nIs the model response correct? "
              f"Answer yes or no only.")
    for attempt in range(3):
        r = call_llm(MODEL, [{"role": "user", "content": prompt}],
                     max_tokens=256, temperature=0.0)
        t = (r.text or "").strip().lower()
        if t:
            return "yes" in t
        time.sleep(1.0 * (attempt + 1))
    return None


def spread(base, label, items):
    accs, flips = [], 0
    verdicts = {i: [] for i in range(len(items))}
    for k in range(K):
        correct = 0
        for i, (q, gold, resp) in enumerate(items):
            v = judge_once(base, q, gold, resp)
            verdicts[i].append(v)
            if v:
                correct += 1
        acc = correct / len(items)
        accs.append(round(acc, 4))
        print(f"  [{label}] pass {k+1}: acc {acc:.4f}", flush=True)
    flips = sum(1 for i in verdicts if len(set(verdicts[i])) > 1)
    sp = round(max(accs) - min(accs), 4)
    print(f"  [{label}] accs {accs} spread {sp} | flipped {flips}/{len(items)}",
          flush=True)
    return {"accs": accs, "spread": sp, "flips": flips, "n": len(items)}


def main() -> int:
    qa = {c["sample_id"]: c["qa"] for c in
          json.load(open("bench/locomo10.json"))}["conv-26"]
    qs = [q for q in json.load(open(METRICS))["questions"]
          if not q.get("abstention")]
    qs.sort(key=lambda q: int(q["question_id"].rsplit("_q", 1)[1]))
    stride = max(1, len(qs) // SAMPLE)
    sample = qs[::stride][:SAMPLE]
    items = []
    for q in sample:
        e = qa[int(q["question_id"].rsplit("_q", 1)[1])]
        items.append((e.get("question", ""), str(e.get("answer", "")),
                      q.get("response", "")))
    print(f"conv-26 answerable sample: {len(items)} (stride {stride}), K={K}",
          flush=True)
    out = {"old": spread(OLD_BASE, "OLD", items),
           "new": spread(NEW_BASE, "NEW", items)}
    out["_verdict"] = ("NEW tighter" if out["new"]["spread"] < out["old"]["spread"]
                       else "NEW not tighter")
    print(f"\nOLD spread {out['old']['spread']} vs NEW spread {out['new']['spread']} "
          f"-> {out['_verdict']}", flush=True)
    p = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "/tmp"), "tmp",
                     "judge_floor.json")
    json.dump(out, open(p, "w"), indent=2)
    print("written:", p, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
