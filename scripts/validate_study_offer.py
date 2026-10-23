#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Whether the study offer's rule fires before real misses, on a real ledger.

The rule under a hard chat answer (silica/kernel/learn/offer.py) follows the
research report difficulty-detection-and-study-aids, whose evidence comes from
other people's students. This checks it on answers: at every graded answer,
would the rule have fired on what came before, and was the answer then wrong?
A rule worth its line fires before misses more often than it stays silent
before them. Then how often a shown offer was acted on, and the escalate
rung's own reading: how often the answer after three misses in a row is wrong.

    uv run python scripts/validate_study_offer.py [VAULT]
    uv run python scripts/validate_study_offer.py --assistments skill_builder_data_corrected.csv

VAULT defaults to SILICA_VAULT. --assistments reads the public ASSISTments
2009-2010 skill builder log (thousands of real students, one row per answer;
the report's own section 3 cites it; the CSV inside
http://base.ustc.edu.cn/data/ASSISTment/2009_skill_builder_data_corrected.zip,
9.1 MB, the mirror the EduData library downloads from), the same rule run per
student and skill:
`correct` is a first attempt right without a hint, `bottom_hint` an answer
shown, which is the ledger's `told`. It holds no confidence, so the blind-spot
branch never fires there. Reopen the thresholds at 200 graded answers after a
firing or 2027-01-31, whichever comes first (MISSES, RECENT and the confidence
bands are the report's proposals).
"""
from __future__ import annotations

import json
import math
import os
import sys


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """The 95% interval of a rate k/n, which stays inside 0..1 at small n."""
    if not n:
        return None
    p = k / n
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    mid = (p + z * z / (2 * n)) / (1 + z * z / n)
    return round(mid - half, 3), round(mid + half, 3)


def assistments(csv_path: str) -> dict:
    """The offer rule and the escalate rung over the ASSISTments log, each
    student's answers on each skill a ledger of its own, in order_id order."""
    import csv

    from silica.kernel.learn import offer

    seqs: dict[tuple[str, str], list[tuple[int, bool, bool]]] = {}
    with open(csv_path, encoding="ISO-8859-15", newline="") as fh:
        for r in csv.DictReader(fh):
            # main problems only: a scaffolding question is the tutor's help,
            # not the student's attempt at the skill
            if r.get("original") != "1" or not r.get("skill_id"):
                continue
            seqs.setdefault((r["user_id"], r["skill_id"]), []).append(
                (int(r["order_id"]), r["correct"] == "1", r.get("bottom_hint") == "1"))
    fired = {"wrong": 0, "right": 0}
    silent = {"wrong": 0, "right": 0}
    after3 = {"wrong": 0, "right": 0}
    for seq in seqs.values():
        seq.sort()
        evs = [{"path": "skill.md", "ts": f"{o:012d}", "correct": c, "told": t} for o, c, t in seq]
        for i in range(1, len(evs)):
            # the rule reads the last RECENT answers only: the window is the
            # whole of what it would have seen, and keeps this linear
            hit = bool(offer.struggles(["skill.md"], evs[max(0, i - offer.RECENT):i]))
            outcome = "right" if evs[i]["correct"] else "wrong"
            (fired if hit else silent)[outcome] += 1
            if i >= offer.ESCALATE_MISSES and not any(e["correct"] for e in evs[i - offer.ESCALATE_MISSES:i]):
                after3[outcome] += 1

    def rate(b: dict) -> dict:
        n = b["wrong"] + b["right"]
        return {"n": n, "miss_rate": round(b["wrong"] / n, 3) if n else None, "ci95": wilson(b["wrong"], n)}

    return {"students": len({u for u, _s in seqs}), "skills": len({s for _u, s in seqs}),
            "answers": sum(len(s) for s in seqs.values()), "fired": rate(fired), "silent": rate(silent),
            "after_3_misses": rate(after3)}


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--assistments"]:
        result = assistments(args[1])
        print(json.dumps(result, indent=1))
        f, s = result["fired"], result["silent"]
        if f["ci95"] and s["ci95"]:
            apart = f["ci95"][0] > s["ci95"][1]
            print(f"\nNext answer wrong: {f['miss_rate']} after a firing, {s['miss_rate']} after silence "
                  f"({'the intervals do not overlap' if apart else 'the intervals overlap: revisit the rule'}).")
        return 0
    if args:
        os.environ["SILICA_VAULT"] = args[0]
    from silica.kernel.learn import offer
    from silica.kernel.report import quiz

    entries = quiz.entries()
    result = offer.replay(entries)
    result["graded_answers"] = len(entries)
    print(json.dumps(result, indent=1))
    fired = result["fired"]["wrong"] + result["fired"]["right"]
    if fired < 30:
        print(f"\n{fired} answers came after a firing: too few to read a rate (want 30 or more).")
    elif (result["miss_rate_when_fired"] or 0) <= (result["miss_rate_when_silent"] or 0):
        print("\nThe rule fires no more before misses than it stays silent before them: revisit it.")
    else:
        print("\nThe rule fires before misses more often than it stays silent before them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
