"""Residue-round ROI judge (step 2 of the residue-lane audit, 2026-08-16).

HISTORICAL: reads the `residue_rounds` instrument of runs made BEFORE the
2026-08-17 reconstruction (verification-based residue, no rounds). Its
verdict — declared residue 100% false positives, round refuted — is what
motivated that reconstruction. New runs record `inputs["residue"]` instead.

Reads a run's persisted `residue_rounds` instrument (uncensored fact lists,
written by _residue_gate since the same audit) and judges every fact with the
narrow FActScore question against the file's FINAL notes:

- recovery rate: share of the steer facts (check[:12], what the round was
  explicitly told to add) now stated in the notes. ~0 refutes the round.
- check false-positive rate: share of the declared (post-round) facts that
  are in fact already stated in the notes. High = the check invents residue,
  the defect sits upstream of the round.

Notes are read from the vault as they are NOW, so run this before further
runs rewrite the same notes. Judge failures follow the factscore convention:
excluded from the denominator.

Usage:
    uv run python -m evals.residue_roi [run_id_prefix]   # default: latest run
"""
from __future__ import annotations

import json
import os
import sys


def _notes_for(vault: str, manifest_entries: list[dict], basename: str) -> str:
    seen: set[str] = set()
    bodies: list[str] = []
    for e in manifest_entries:
        if e.get("source_basename") != basename or e.get("path") in seen:
            continue
        seen.add(e.get("path"))
        for cand in (e["path"], f"{e['path']}.md"):
            p = os.path.join(vault, cand)
            if os.path.isfile(p):
                bodies.append(f"### {e.get('title', '')}\n"
                              + open(p, encoding="utf-8").read())
                break
    return "\n\n".join(bodies)


def _tally(facts: list[str], verdicts: list[bool | None]) -> dict:
    judged = [v for v in verdicts if v is not None]
    return {"supported": sum(judged), "judged": len(judged),
            "failures": verdicts.count(None)}


def roi_report(run_dir: str, judge=None) -> dict:
    """Judge one run's residue_rounds against the vault's current notes.

    ``judge(facts, source) -> list[bool | None]`` — injected for tests;
    the CLI wires evals.factscore.judge_facts.
    """
    led = json.load(open(os.path.join(run_dir, "ledger.json"), encoding="utf-8"))
    manifest = json.load(open(os.path.join(run_dir, "manifest.json"), encoding="utf-8"))
    vault = led["vault"]
    inbox_files = led.get("inputs", {}).get("inbox_files", [])
    rounds = led.get("inputs", {}).get("residue_rounds", {})

    files: dict[str, dict] = {}
    rec_sup = rec_jud = fp_sup = fp_jud = 0
    for key, entry in sorted(rounds.items()):
        fi = int(key[1:])
        basename = os.path.basename(inbox_files[fi]) if fi < len(inbox_files) else ""
        out: dict = {"source": basename}
        files[key] = out
        # recovery + false-positive are only meaningful when a round ran,
        # which is exactly when the post-round check recorded "declared".
        if "declared" not in entry:
            continue
        notes = _notes_for(vault, manifest.get("entries", []), basename)
        if not notes:
            out["error"] = "no notes found for source"
            continue
        steer = entry.get("check", {}).get("facts", [])[:12]
        if steer:
            t = _tally(steer, judge(steer, notes))
            out["recovery"] = t
            rec_sup += t["supported"]; rec_jud += t["judged"]
        declared = entry.get("declared", {}).get("facts", [])
        if declared:
            t = _tally(declared, judge(declared, notes))
            out["false_positive"] = t
            fp_sup += t["supported"]; fp_jud += t["judged"]

    return {
        "files": files,
        "totals": {
            "recovery_rate": rec_sup / rec_jud if rec_jud else None,
            "fp_rate": fp_sup / fp_jud if fp_jud else None,
            "recovered": rec_sup, "recovery_judged": rec_jud,
            "false_positives": fp_sup, "fp_judged": fp_jud,
        },
    }


def _latest_run(runs_root: str, prefix: str | None) -> str:
    dirs = [d for d in os.listdir(runs_root)
            if not prefix or d.startswith(prefix)]
    if not dirs:
        raise SystemExit(f"no run matching {prefix!r} under {runs_root}")
    best = max(dirs, key=lambda d: os.path.getmtime(os.path.join(runs_root, d)))
    return os.path.join(runs_root, best)


def main() -> None:
    from silica.config import CONFIG
    from evals.factscore import judge_facts

    run_dir = _latest_run(os.path.expanduser("~/.silica/runs"),
                          sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"run: {os.path.basename(run_dir)[:8]}  judge model: {CONFIG.model}")
    rep = roi_report(run_dir, judge=lambda facts, src: judge_facts(CONFIG.model, facts, src))
    if not rep["files"]:
        raise SystemExit("this run has no residue_rounds instrument "
                         "(recorded only by runs after the 2026-08-16 audit)")
    for key, f in rep["files"].items():
        line = f"  {key} {f.get('source', '')}: "
        if "recovery" in f:
            r = f["recovery"]
            line += f"recovered {r['supported']}/{r['judged']}"
            if r["failures"]:
                line += f" ({r['failures']} judge-fail)"
        if "false_positive" in f:
            p = f["false_positive"]
            line += f" | declared-already-present {p['supported']}/{p['judged']}"
        if "error" in f:
            line += f["error"]
        if "recovery" not in f and "false_positive" not in f and "error" not in f:
            line += "no round (check only)"
        print(line)
    t = rep["totals"]
    rr = f"{t['recovery_rate']:.2f}" if t["recovery_rate"] is not None else "n/a"
    fp = f"{t['fp_rate']:.2f}" if t["fp_rate"] is not None else "n/a"
    print(f"totals: recovery {t['recovered']}/{t['recovery_judged']} ({rr}) | "
          f"check false-positive {t['false_positives']}/{t['fp_judged']} ({fp})")


if __name__ == "__main__":
    main()
