# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The gate on the grader's class of a miss (ADR-0135 point 6): the real
grader, on labelled wrong answers, with one model at a time. The picker reads
the class only when at least 30 of 40 agree on the local AND on the hosted
model, reported apart.

The ledger keeps no answer text, so the cases are written and labelled by a
separate agent before the run, on the owner's course notes, as the /check
gate of ADR-0134 was: a JSON list of {id, path, level, q, criteria, claim,
span, production, expected}. They quote the notes, so they stay out of the
repo, under ~/.silica/evals/learn_miss_class/.

Usage:
  uv run python -m evals.learn_miss_class run --model custom/qwen3.5-9b --tag local
  uv run python -m evals.learn_miss_class run --model openrouter/deepseek/deepseek-v4-flash-0731 --tag hosted
  uv run python -m evals.learn_miss_class score TAG
  --cases FILE on both reads another set (a fresh one, to measure a prompt
  changed after the first run: cases.json, then cases2.json).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

HOME = Path("~/.silica/evals/learn_miss_class").expanduser()
CASES = HOME / "cases.json"
GATE = 30        # of 40, on each model (ADR-0135 point 6)


def run(model: str, tag: str, cases_path: Path = CASES) -> Path:
    from silica.config import CONFIG
    from silica.kernel.learn import grade

    CONFIG.model, CONFIG.judge_model = model, None
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    out = HOME / tag
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in cases:
        item = {"path": c["path"], "kind": "explain", "level": c["level"], "q": c["q"], "criteria": c["criteria"],
                "claim": c["claim"], "span": c["span"], "source": {}}
        t0 = time.monotonic()
        v = grade.grade(item, production=c["production"], conf=None, hints=0, told=False)
        rows.append({"id": c["id"], "grader": v.get("grader"), "grade": v.get("grade"), "miss": v.get("miss"),
                     "seconds": round(time.monotonic() - t0, 1)})
        print(f"{c['id']}: {v.get('grade')} {v.get('miss')}", flush=True)
    path = out / "results.json"
    path.write_text(json.dumps({"model": model, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def score(tag: str, cases_path: Path = CASES) -> dict:
    cases = {c["id"]: c for c in json.loads(cases_path.read_text(encoding="utf-8"))}
    res = json.loads((HOME / tag / "results.json").read_text(encoding="utf-8"))
    agree = sum(1 for r in res["rows"] if r["miss"] == cases[r["id"]]["expected"])
    confusion: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in res["rows"]:
        confusion[cases[r["id"]]["expected"]][r["miss"] or ("met" if r["grade"] == "recalled" else "none")] += 1
    return {"model": res["model"], "agree": agree, "of": len(res["rows"]), "gate": agree >= GATE,
            "self_graded": sum(1 for r in res["rows"] if r["grader"] != "model"),
            "confusion": {k: dict(v) for k, v in sorted(confusion.items())}}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="learn_miss_class")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True)
    r.add_argument("--tag", required=True)
    r.add_argument("--cases", type=Path, default=CASES)
    s = sub.add_parser("score")
    s.add_argument("tag")
    s.add_argument("--cases", type=Path, default=CASES)
    a = p.parse_args(argv)
    if a.cmd == "run":
        print(run(a.model, a.tag, a.cases))
    else:
        print(json.dumps(score(a.tag, a.cases), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
