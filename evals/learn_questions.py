# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learn's question quality on real notes (ADR-0065): generate what a note
session would ask, with the configured model, and count what a reader would
see wrong. No session is opened and nothing reaches the ledger.

  probe   one note: its points, the asks, the raw reply, what generate() keeps
  run     every note of a set file, one folder each, then the stats
  stats   counts over a run folder: items kept of asked, questions with two
          asks, pointers to a text the reader cannot see, questions over 25
          words, CRITERIA left in English
  grade   hand-written answers against one probed note's items, real grader

Usage:
  uv run python -m evals.learn_questions probe VAULT NOTE [--objectives recall,explain] [--model M] [--out DIR]
  uv run python -m evals.learn_questions run --tag v12 [--set FILE] [--model M]
  uv run python -m evals.learn_questions stats DIR
  uv run python -m evals.learn_questions grade DIR/NAME CASES.json

A set file is TSV, one note per line: vault path, vault-relative note path,
a short name. A cases file is a JSON list of {point, label, production}.
Both default under ~/.silica/evals/, and runs land in
~/.silica/evals/learn_questions/<tag>/: they quote the notes, which stay out
of the repo. The baseline measured for ADR-0065 is the run tagged
2026-10-06-shipped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HOME = Path("~/.silica/evals").expanduser()
DEFAULT_SET = HOME / "learn_questions.tsv"
RUNS = HOME / "learn_questions"

# A second ask hung on the first ("... e cosa non fanno?"), or two question
# marks. English and Italian only, like the strip in items.py.
COMPOUND = re.compile(
    r",?\s(?:e|ed|and)\s(?:(?:su|di|a|in|per|con|da|tra|on|of|to|for|with|by)\s)?"
    r"(?:come|cosa|che cosa|qual\w*|chi|perch[eé]|quando|dove|quant\w*|in che modo|"
    r"how|what|which|who|why|when|where)\b|\?.*\S.*\?", re.I)
TEXT_REF = re.compile(r"\b(?:la nota|il testo|nel testo|nella nota|secondo il punto|definizione data|"
                      r"elencat\w+ nella|the note|the text|the passage)\b", re.I)
ENGLISH = re.compile(r"\b(?:the|must|answer|should)\b", re.I)


def probe(vault: str, note: str, objectives: list[str], out: Path, model: str | None = None) -> dict:
    """Generate one note session's items into *out*; returns the run record."""
    from silica.config import CONFIG
    from silica.kernel.learn import items

    CONFIG.vault_path = str(Path(vault).expanduser())
    if model:
        CONFIG.model = model
    out.mkdir(parents=True, exist_ok=True)
    text = items.read_note(note)
    if not text:
        raise SystemExit(f"no note at {note!r} in {CONFIG.vault_path}")
    points = items.key_points(note, text)
    asks = items.ask_plan(points, objectives, len(items.relations_of(note, text)))
    row = {"path": note, "kind": "explain", "level": 3, "asks": asks, "objective": objectives[0]}
    prompt = items.GENERATE_PROMPT.format(notes=items.note_block(1, row))
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    t0 = time.monotonic()
    reply, finish = items._llm(prompt, items.GENERATE_MAX_TOKENS)
    seconds = round(time.monotonic() - t0, 1)
    (out / "reply.txt").write_text(reply, encoding="utf-8")
    # generate() on the reply just written: the product's join, fallback and
    # downgrade, without paying for the generation call twice
    real = items._llm
    items._llm = lambda p, n: (reply, finish) if p.startswith(items.GENERATE_TAG) else real(p, n)
    try:
        made = items.generate([row]).get(note, [])
    finally:
        items._llm = real
    (out / "made.json").write_text(json.dumps(made, ensure_ascii=False, indent=1), encoding="utf-8")
    run = {"vault": CONFIG.vault_path, "note": note, "model": CONFIG.model, "objectives": objectives,
           "points": len(points), "asks": len(asks), "kept": len(made), "reply_chars": len(reply),
           "finish": finish, "seconds": seconds}
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    return run


def stats(run_dir: Path) -> dict:
    """The counts over every probed note under *run_dir*."""
    asks = kept = 0
    qs: list[dict] = []
    for run_file in sorted(run_dir.glob("*/run.json")):
        run = json.loads(run_file.read_text(encoding="utf-8"))
        asks += run["asks"]
        kept += run["kept"]
        qs += json.loads((run_file.parent / "made.json").read_text(encoding="utf-8"))
    return {
        "notes": len(list(run_dir.glob("*/run.json"))), "asks": asks, "kept": kept,
        "compound": [q["q"] for q in qs if COMPOUND.search(q["q"])],
        "text_ref": [q["q"] for q in qs if TEXT_REF.search(q["q"])],
        "over_25_words": [q["q"] for q in qs if len(q["q"].split()) > 25],
        "english_criteria": [q["q"] for q in qs if ENGLISH.search(q.get("criteria", ""))],
    }


def _print_run(name: str, run: dict, made: list[dict]) -> None:
    print(f"== {name}: kept {run['kept']} of {run['asks']} asks, reply {run['reply_chars']} chars, "
          f"finish={run['finish']}, {run['seconds']} s")
    for it in made:
        print(f"  P{it['point']} {it['objective']}/{it['kind']} | {it['q']}")
        if it.get("hints"):
            print(f"      H1: {it['hints'][0][:160]}")


def _read_set(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            vault, note, name = line.split("\t")
            rows.append((vault, note, name))
    return rows


def _grade(item_dir: Path, cases_file: Path) -> int:
    from silica.config import CONFIG
    from silica.kernel.learn import grade

    run = json.loads((item_dir / "run.json").read_text(encoding="utf-8"))
    CONFIG.vault_path = run["vault"]
    made = {it["point"]: it for it in json.loads((item_dir / "made.json").read_text(encoding="utf-8"))}
    for case in json.loads(cases_file.read_text(encoding="utf-8")):
        it = made.get(case["point"])
        if it is None:
            print(f"P{case['point']}: no item on that point in {item_dir}", file=sys.stderr)
            continue
        t0 = time.monotonic()
        v = grade.grade(it, production=case["production"], conf=70, hints=0, told=False)
        print(f"P{case['point']} {case['label']:8s} -> {v.get('grade')!s:9s} {time.monotonic() - t0:4.1f} s | "
              f"{v.get('feedback_line', '')[:170]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("vault")
    p.add_argument("note")
    p.add_argument("--objectives", default="recall,explain")
    p.add_argument("--model", help="a model id instead of the configured one, e.g. openrouter/z-ai/glm-5.3-flash")
    p.add_argument("--out")
    r = sub.add_parser("run")
    r.add_argument("--tag", required=True)
    r.add_argument("--set", default=str(DEFAULT_SET))
    r.add_argument("--objectives", default="recall,explain")
    r.add_argument("--model")
    s = sub.add_parser("stats")
    s.add_argument("dir")
    g = sub.add_parser("grade")
    g.add_argument("item_dir")
    g.add_argument("cases")
    args = ap.parse_args(argv)
    if args.cmd == "stats":
        print(json.dumps(stats(Path(args.dir).expanduser()), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "grade":
        return _grade(Path(args.item_dir).expanduser(), Path(args.cases).expanduser())
    objectives = args.objectives.split(",")
    if args.cmd == "probe":
        out = Path(args.out).expanduser() if args.out else RUNS / "probe" / Path(args.note).stem
        run = probe(args.vault, args.note, objectives, out, args.model)
        _print_run(Path(args.note).stem, run, json.loads((out / "made.json").read_text(encoding="utf-8")))
        return 0
    run_dir = RUNS / args.tag
    for vault, note, name in _read_set(Path(args.set).expanduser()):
        run = probe(vault, note, objectives, run_dir / name, args.model)
        _print_run(name, run, json.loads((run_dir / name / "made.json").read_text(encoding="utf-8")))
    st = stats(run_dir)
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in st.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
