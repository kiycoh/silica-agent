"""Quantify judge false-negatives on single-hop LoCoMo answers (conv-26).

Step 2 already proved all conv-26 single-hop wrong-answerable had the evidence
turn in the read text (0 retrieval misses). So the whole bucket is "fact was
delivered, graded wrong". This re-grades each with a content-match prompt that
ignores verbosity/format/paraphrase, splitting the bucket into JUDGE-FN (the
candidate asserts the gold fact, base judge over-penalized) vs GENUINE-WRONG
(fact omitted, contradicted, or abstained). The second reason line names WHY,
which points at the lever. Read-only; one cheap LLM call per item.

Run: PYTHONPATH=. uv run python evals/probe_single_hop_regrade.py
"""
import collections
import json
import sys

from silica.agent.llm import call_llm

MODEL = "openrouter/deepseek/deepseek-v4-flash"
CONV, METRICS = "conv-26", "bench/ab_c26_verbatim_agent.metrics.json"

REGRADE = (
    "Re-grade one factual (single-hop) question. The gold is a short reference "
    "answer stating one fact. The candidate may express the SAME fact verbosely, "
    "with extra correct detail, bullets, or paraphrase.\n"
    "MATCH if the candidate ASSERTS the gold fact (same core content), even if "
    "much longer, reformatted, or paraphrased, and even if it adds other correct "
    "detail. NOMATCH only if the candidate OMITS the gold fact, asserts something "
    "CONTRADICTORY, hedges without committing, or abstains ('I do not have that "
    "information'). Extra correct information never makes it NOMATCH.\n\n"
    "Question: {q}\nGold: {gold}\nCandidate: {resp}\n\n"
    "First line: exactly 'match' or 'nomatch'. Second line, one short tag for "
    "WHY: on match one of [verbose|reformatted|paraphrase|extra-detail]; on "
    "nomatch one of [omitted|contradicted|hedged|abstained]."
)


def regrade(q: str, gold: str, resp: str):
    prompt = REGRADE.format(q=q, gold=gold, resp=resp)
    for _ in range(3):  # deepseek reasoning routing can drop an empty first reply
        r = call_llm(MODEL, [{"role": "user", "content": prompt}],
                     max_tokens=256, temperature=0.0)
        t = (r.text or "").strip()
        if t:
            lines = t.splitlines()
            first = lines[0].strip().lower()
            tag = lines[1].strip().lower() if len(lines) > 1 else ""
            return (first.startswith("match") and "nomatch" not in first), tag
    return None, "(judge empty x3)"


def main() -> int:
    qa = {c["sample_id"]: c["qa"] for c in json.load(open("bench/locomo10.json"))}[CONV]
    qs = json.load(open(METRICS))["questions"]
    sh_wrong = [q for q in qs if q.get("question_type") == "single-hop"
                and q.get("correct") is False]
    ans = [q for q in qs if not q.get("abstention")]
    cur_correct = sum(1 for q in ans if q.get("correct"))
    print(f"=== {CONV}: {len(sh_wrong)} single-hop wrong-answerable ===", flush=True)
    fn = 0
    tags = collections.Counter()
    for q in sh_wrong:
        qi = int(q["question_id"].rsplit("_q", 1)[1])
        e = qa[qi]
        m, tag = regrade(e.get("question", ""), str(e.get("answer", "")),
                         q.get("response", ""))
        verdict = "JUDGE-FN" if m else ("wrong" if m is False else "ERR")
        if m:
            fn += 1
        tags[(verdict, tag)] += 1
        print(f"  [{q['question_id']:<12}] {verdict:<8} {tag:<12} | "
              f"gold={str(e.get('answer',''))[:42]!r}", flush=True)
    base = cur_correct / len(ans) if ans else 0
    corr = (cur_correct + fn) / len(ans) if ans else 0
    print(f"\n  judge-FN {fn}/{len(sh_wrong)} single-hop | answerable acc "
          f"{base:.3f} -> {corr:.3f} (+{fn} recovered)", flush=True)
    print("  tag breakdown:", flush=True)
    for (v, tag), n in tags.most_common():
        print(f"    {n:2d}  {v:<8} {tag}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
