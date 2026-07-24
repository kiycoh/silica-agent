"""Quantify judge false-negatives on temporal LoCoMo questions.

The base LLM judge marks a temporal answer wrong when the agent gives a correct
ABSOLUTE date but the gold is phrased RELATIVE ("the week before <DATE>"). This
re-grades the temporal wrong-answerable answers with a temporal-aware prompt
that resolves both sides to a calendar date/range before deciding. Read-only
over stored metrics + dataset (no answer re-run); one cheap LLM call per item.

Run: PYTHONPATH=. uv run python evals/probe_temporal_regrade.py
"""
import json
import sys

from silica.agent.llm import call_llm

MODEL = "openrouter/deepseek/deepseek-v4-flash"
RUNS = [("conv-47", "bench/v1_c47_verbatim.agent.metrics.json"),
        ("conv-26", "bench/ab_c26_verbatim_agent.metrics.json")]

REGRADE = (
    "Re-grade one temporal question. The gold answer is a date, often phrased "
    "RELATIVE to a reference date. The candidate answer may express the SAME "
    "date absolutely or in prose. Resolve BOTH to a calendar date/range, then "
    "decide if they match.\n"
    "Resolution rules:\n"
    "- 'the week before D' = the 7 days ending the day before D.\n"
    "- 'the <weekday> before D' = that weekday in the 7 days before D.\n"
    "- 'two weekends before D' = the weekend about 14 days before D.\n"
    "- a bare month/year (e.g. 'June 2023') matches any date in it.\n"
    "MATCH only if the candidate ASSERTS a date that falls on/within the gold's "
    "resolved date/range. If the candidate abstains ('I do not have that "
    "information'), hedges without committing, or asserts a clearly different "
    "date, it is NOMATCH.\n\n"
    "Question: {q}\nGold: {gold}\nCandidate: {resp}\n\n"
    "First line: exactly 'match' or 'nomatch'. Second line: one short reason."
)


def regrade(q: str, gold: str, resp: str):
    prompt = REGRADE.format(q=q, gold=gold, resp=resp)
    for _ in range(3):  # deepseek reasoning routing can drop an empty first reply
        r = call_llm(MODEL, [{"role": "user", "content": prompt}],
                     max_tokens=256, temperature=0.0)
        t = (r.text or "").strip()
        if t:
            first = t.splitlines()[0].strip().lower()
            reason = t.splitlines()[1].strip() if len(t.splitlines()) > 1 else ""
            return (first.startswith("match") and "nomatch" not in first), reason
    return None, "(judge empty x3)"


def main() -> int:
    data = json.load(open("bench/locomo10.json"))
    qa_by = {c["sample_id"]: c["qa"] for c in data}
    grand_fn = grand_n = 0
    for conv, f in RUNS:
        d = json.load(open(f))
        qs = d["questions"]
        qa = qa_by[conv]
        tw = [q for q in qs if not q.get("abstention")
              and q.get("correct") is False
              and q.get("question_type") == "temporal"]
        ans = [q for q in qs if not q.get("abstention")]
        cur_correct = sum(1 for q in ans if q.get("correct"))
        print(f"\n=== {conv}: {len(tw)} temporal wrong-answerable ===", flush=True)
        fn = 0
        for q in tw:
            qi = int(q["question_id"].rsplit("_q", 1)[1])
            e = qa[qi]
            m, reason = regrade(e.get("question", ""), str(e.get("answer", "")),
                                q.get("response", ""))
            rflag = "1" if (q.get("session_recall") or 0) >= 1.0 else "<1"
            verdict = "JUDGE-FN" if m else ("wrong" if m is False else "ERR")
            if m:
                fn += 1
            print(f"  [{q['question_id']:<12}] recall={rflag:<2} {verdict:<8} | "
                  f"gold={str(e.get('answer',''))[:38]!r} | {reason[:70]}", flush=True)
        corr = (cur_correct + fn) / len(ans) if ans else 0
        base = cur_correct / len(ans) if ans else 0
        print(f"  {conv}: judge-FN {fn}/{len(tw)} temporal | answerable acc "
              f"{base:.3f} -> {corr:.3f} (+{fn} recovered)", flush=True)
        grand_fn += fn
        grand_n += len(tw)
    print(f"\nTOTAL temporal judge-FN: {grand_fn}/{grand_n} "
          f"({grand_fn/grand_n*100:.0f}% of temporal wrong were actually correct)",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
