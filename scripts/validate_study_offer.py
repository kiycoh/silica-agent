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

    uv run python scripts/validate_study_offer.py --eedi <unzipped NeurIPS 2020 dir>
    uv run python scripts/validate_study_offer.py --experiments processed_experiment_data.csv

--eedi checks the confidence branch on the Eedi NeurIPS 2020 Education
Challenge log (task 3/4), the one public log of real students' answers with a
confidence beside each. --experiments reads the aids' effect off randomized
experiments: Prihar et al., "Exploring Common Trends in Online Educational
Experiments", data at https://osf.io/59shv. The ASSISTments data comes under
terms of use: cite it, no commercial use, outcomes public, no redistribution;
this script ships no data.
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


def _band(conf: float) -> int:
    """Eedi's 0-100 slider (0, 25, 50, 75, 100) on Learn's four bands."""
    return 90 if conf >= 87.5 else 70 if conf >= 62.5 else 50 if conf >= 37.5 else 25


def eedi(data_dir: str) -> dict:
    """The confidence branch on the Eedi NeurIPS 2020 log (task 3/4: real
    students' answers, some with a confidence 0-100). Mirrors offer.struggles
    with calibration counted over each student's whole ledger, as the live
    rule counts it over the reader's: the misses rule alone, the blind-spot
    branch where it adds a firing, and a right answer given unsure, which the
    rule now counts (the owner's call of 2026-10-23, on this measurement)."""
    import csv
    from pathlib import Path

    from silica.kernel.learn.mastery import BLIND_SPOT_CONF, CALIBRATION_MIN, FRAGILE_CONF
    from silica.kernel.learn.offer import MISSES, RECENT

    base = Path(data_dir) / "data"
    level = {}
    with open(base / "metadata" / "subject_metadata.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            level[r["SubjectId"]] = int(r["Level"])
    subject = {}
    with open(base / "metadata" / "question_metadata_task_3_4.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            ids = [s.strip() for s in r["SubjectId"].strip("[]").split(",") if s.strip()]
            # the most specific subject the question is filed under: the skill
            subject[r["QuestionId"]] = max(ids, key=lambda s: level.get(s, 0)) if ids else ""
    meta = {}
    with open(base / "metadata" / "answer_metadata_task_3_4.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            meta[r["AnswerId"]] = (r["DateAnswered"], _band(float(r["Confidence"])) if r["Confidence"] else None)
    by_user: dict[str, list] = {}
    with open(base / "train_data" / "train_task_3_4.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            date, conf = meta.get(r["AnswerId"], ("", None))
            by_user.setdefault(r["UserId"], []).append(
                (date, int(r["AnswerId"]), subject.get(r["QuestionId"], ""), r["IsCorrect"] == "1", conf))
    keys = ("misses_fired", "silent", "blind_only", "silent_not_blind", "fragile", "sure_right")
    out = {k: {"wrong": 0, "right": 0} for k in keys}
    for answers in by_user.values():
        answers.sort()
        seen: dict[str, list] = {}
        with_conf = 0
        for _date, _aid, subj, correct, conf in answers:
            prev = seen.setdefault(subj, [])
            if prev:
                window = prev[-RECENT:]
                last = window[-1]
                outcome = "right" if correct else "wrong"
                misses = sum(1 for _c, ok in window if not ok) >= MISSES
                blind = (with_conf >= CALIBRATION_MIN and last[0] is not None and last[0] >= BLIND_SPOT_CONF
                         and not last[1])
                if misses:
                    out["misses_fired"][outcome] += 1
                else:
                    out["silent"][outcome] += 1
                    out["blind_only" if blind else "silent_not_blind"][outcome] += 1
                    if last[1] and last[0] is not None:
                        if last[0] <= FRAGILE_CONF:
                            out["fragile"][outcome] += 1
                        elif last[0] >= BLIND_SPOT_CONF:
                            out["sure_right"][outcome] += 1
            prev.append((conf, correct))
            with_conf += conf is not None

    def rate(b: dict) -> dict:
        n = b["wrong"] + b["right"]
        return {"n": n, "miss_rate": round(b["wrong"] / n, 3) if n else None, "ci95": wilson(b["wrong"], n)}

    return {"students": len(by_user), "answers": sum(len(a) for a in by_user.values()),
            "with_confidence": sum(1 for a in by_user.values() for x in a if x[4] is not None),
            **{k: rate(v) for k, v in out.items()}}


# The randomized comparisons of an aid against less help in the OSF data of
# Prihar et al., "Exploring Common Trends in Online Educational Experiments"
# (EDM 2022): condition 1 is the second name of the pair, the aid.
AID_PAIRS = ("Answer Only vs. Example", "Answer Only vs. Explanation", "Answer Only vs. Hint",
             "Answer Only vs. Scaffolding", "Answer Only vs. Video Example", "Answer Only vs. Video Scaffolding",
             "No Feedback vs. Common Wrong Answer Feedback")


def experiments(csv_path: str) -> dict:
    """Each aid's effect on normalized learning (Cohen's d, z-scored within
    each experiment by the authors, an incomplete assignment at the floor),
    pooled over its experiments by inverse variance, overall and for students
    below and above the average prior correctness."""
    import csv

    cells: dict[tuple, list[float]] = {}
    prior: list[float] = []
    rows = []
    with open(csv_path, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["independent_measure_pair"] not in AID_PAIRS:
                continue
            rows.append(r)
            if r["student_prior_average_correctness"]:
                prior.append(float(r["student_prior_average_correctness"]))
    cut = sum(prior) / len(prior) if prior else 0.0
    for r in rows:
        y = float(r["normalized_student_learning"])
        group = ("all",)
        if r["student_prior_average_correctness"]:
            group += ("below" if float(r["student_prior_average_correctness"]) < cut else "above",)
        for g in group:
            cells.setdefault((r["independent_measure_pair"], g, r["experiment_id"], r["condition"]), []).append(y)

    def d_of(a: list[float], b: list[float]) -> tuple[float, float] | None:
        if len(a) < 2 or len(b) < 2:
            return None
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
        vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
        sd = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2)) or 1.0
        d = (mb - ma) / sd
        return d, (len(a) + len(b)) / (len(a) * len(b)) + d * d / (2 * (len(a) + len(b)))

    out: dict = {}
    for pair in AID_PAIRS:
        for g in ("all", "below", "above"):
            ds = []
            for exp in {k[2] for k in cells if k[0] == pair and k[1] == g}:
                got = d_of(cells.get((pair, g, exp, "0"), []), cells.get((pair, g, exp, "1"), []))
                if got:
                    ds.append(got)
            if not ds:
                continue
            w = [1 / v for _d, v in ds]
            d = sum(wi * di for wi, (di, _v) in zip(w, ds)) / sum(w)
            half = 1.96 / math.sqrt(sum(w))
            n = sum(len(v) for k, v in cells.items() if k[0] == pair and k[1] == g)
            out.setdefault(pair, {})[g] = {"experiments": len(ds), "students": n, "d": round(d, 3),
                                           "ci95": (round(d - half, 3), round(d + half, 3))}
    return out


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--eedi"]:
        print(json.dumps(eedi(args[1]), indent=1))
        return 0
    if args[:1] == ["--experiments"]:
        print(json.dumps(experiments(args[1]), indent=1))
        return 0
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
