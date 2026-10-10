# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""G0, inventory recall (docs/specs/study-mode.md §10): build the inventories
of real lessons with the configured worker model, then score the review
sheets the maintainer marks.

  build   the inventory of each given leaf, its calls and wall time (§8), and
          one review sheet per leaf: every section's source text with its
          claims and a `missing:` slot, then a checkbox per relation claim
  score   the gate's numbers from the marked sheets: the share of core
          concepts missing (at most 5%), the span-drop rate (at most 20%),
          the share of relation edges marked correct (relations become core
          at 95%) and the recall of sections cut at 6,000 characters against
          the others (reopen at a gap over 5 points)

Usage:
  uv run python -m evals.study_g0 build "sources/Analisi 1 - Lezione 01.md" ... [--fresh]
  uv run python -m evals.study_g0 score

Sheets and runs land in <index_dir>/g0/ unless --out names another folder:
they quote the course material, which stays out of the repo.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

MISSING_MAX = 0.05
SPAN_DROP_MAX = 0.20
EDGES_CORE_AT = 0.95
CUT_GAP_MAX = 0.05
LESSONS = 5

_REL_HEADER = "# Relations"  # H1, so no section heading (always H2 here) can read as it
_EDGE = re.compile(r"^- \[([ xX])\] ")


def sheet(inv: dict, leaf_raw: str, run: dict) -> str:
    """The review sheet of one inventory."""
    from silica.kernel.learn import inventory
    secs = {s["section"]: s for s in inventory.sections(leaf_raw, untitled=Path(inv["leaf"]).stem)}
    claims = [c for c in inv["claims"] if c["kind"] != "relation"]
    rels = [c for c in inv["claims"] if c["kind"] == "relation"]
    span = sum(d["reason"] == "span not in leaf" for d in inv["dropped"])
    proposed = len(claims) + sum(bool(d.get("section")) for d in inv["dropped"])
    lines = [f"# G0 review: {inv['leaf']}", "",
             f"{run['calls']} calls, {run['seconds']:.0f} s; {len(claims)} claims "
             f"({sum(c['role'] == 'core' for c in claims)} core), {len(rels)} relations; "
             f"span drops {span} of {proposed}", "",
             "Under each section, after `missing:`, write one line `- ...` for every core concept "
             "of the source that no claim states. Mark a relation [x] when the source supports it. "
             "Set `reviewed: yes` when the sheet is marked: score reads only reviewed sheets.", "",
             "reviewed: no", ""]
    for s in inv["sections"]:
        name = s["section"]
        sec = secs.get(name, {})
        lines += [f"## {name}" + (" · cut" if sec.get("cut") else ""), ""]
        own = inventory._OWN_TEXT.sub("", sec.get("text", ""), count=1).strip()
        lines += [f"> {line}" if line.strip() else ">" for line in own.splitlines()] + [""]
        lines += [f"- {c['role']} · {c['kind']} · {c['text']}" for c in claims if c["section"] == name]
        lines += ["", "missing:", ""]
    lines += [_REL_HEADER, ""] + [f"- [ ] {c['text']}" for c in rels]
    return "\n".join(lines) + "\n"


def read_sheet(text: str) -> dict:
    """A marked sheet -> {"leaf", "sections": [{"section", "cut", "core", "missing"}], "edges": [bool]}."""
    out: dict = {"leaf": "", "reviewed": False, "sections": [], "edges": []}
    cur: dict | None = None
    in_missing = in_rel = False
    for line in text.splitlines():
        if line.startswith("# G0 review: "):
            out["leaf"] = line.removeprefix("# G0 review: ").strip()
        elif line.strip() == "reviewed: yes":
            out["reviewed"] = True
        elif line.strip() == _REL_HEADER:
            in_rel, cur = True, None
        elif line.startswith("## ") and not in_rel:
            name = line[3:].strip()
            cur = {"section": name.removesuffix(" · cut"), "cut": name.endswith(" · cut"), "core": 0, "missing": 0}
            out["sections"].append(cur)
            in_missing = False
        elif in_rel:
            if m := _EDGE.match(line):
                out["edges"].append(m.group(1) != " ")
        elif cur is not None and line.strip() == "missing:":
            in_missing = True
        elif cur is not None and line.startswith("- "):
            if in_missing:
                cur["missing"] += bool(line[2:].strip())
            elif line.startswith("- core · "):
                cur["core"] += 1
    return out


def _share(num: int, den: int) -> float | None:
    return num / den if den else None


def score(sheets: list[dict], invs: list[dict]) -> dict:
    """The gate's numbers. What the maintainer marks is read from reviewed
    sheets only: an unmarked sheet has no missing line and no checked edge,
    and read as marked it would pass the missing gate unseen. The span-drop
    rate needs no review and is read over every inventory."""
    marked = [sh for sh in sheets if sh["reviewed"]]
    secs = [s for sh in marked for s in sh["sections"]]

    def recall(group: list[dict]) -> float | None:
        core = sum(s["core"] for s in group)
        return _share(core, core + sum(s["missing"] for s in group))

    core, missing = sum(s["core"] for s in secs), sum(s["missing"] for s in secs)
    edges = [e for sh in marked for e in sh["edges"]]
    span = sum(d["reason"] == "span not in leaf" for inv in invs for d in inv["dropped"])
    # proposed = what the decomposition calls returned: the claims kept plus
    # every claim dropped from a section; relation claims are rendered, never proposed
    proposed = sum(c["kind"] != "relation" for inv in invs for c in inv["claims"]) \
        + sum(bool(d.get("section")) for inv in invs for d in inv["dropped"])
    out = {"leaves": len(sheets), "reviewed": len(marked), "core": core, "missing": missing, "missing_share": _share(missing, core + missing),
           "recall_cut": recall([s for s in secs if s["cut"]]), "recall_other": recall([s for s in secs if not s["cut"]]),
           "edges": len(edges), "edges_correct": sum(edges), "edges_share": _share(sum(edges), len(edges)),
           "span_drops": span, "proposed": proposed, "span_drop_rate": _share(span, proposed)}

    def gate(value: float | None, ok: bool, fail: str) -> str:
        return "no data" if value is None else ("pass" if ok else fail)

    unseen = "no reviewed sheet"

    ms, sd, es = out["missing_share"], out["span_drop_rate"], out["edges_share"]
    rc, ro = out["recall_cut"], out["recall_other"]
    out["verdict"] = {
        "missing": unseen if not marked else gate(ms, ms is not None and ms <= MISSING_MAX,
                                                  "reopen the decomposition prompt"),
        "span_drops": gate(sd, sd is not None and sd <= SPAN_DROP_MAX, "reopen the decomposition prompt"),
        "edges": unseen if not marked else "no data" if es is None else (
            "relations may become core" if es >= EDGES_CORE_AT else "relations stay supporting"),
        "cut": unseen if not marked else "no cut section" if rc is None or ro is None else (
            "pass" if ro - rc <= CUT_GAP_MAX else "reopen the 6,000-character cut"),
        "lessons": "pass" if len(marked) >= LESSONS else f"G0 asks for {LESSONS} reviewed lessons, {len(marked)} reviewed",
    }
    return out


def _out_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    from silica.config import CONFIG
    from silica.kernel.recall import paths
    return paths.index_dir_for(CONFIG.vault_path) / "g0"


def _build(leaves: list[str], out: Path, *, fresh: bool, force: bool) -> int:
    from silica.config import CONFIG
    from silica.kernel import outline as ol
    from silica.kernel.learn import inventory
    out.mkdir(parents=True, exist_ok=True)
    for leaf in leaves:
        calls: list[str] = []

        def counting(system: str, user: str, **kw) -> dict:
            calls.append(system[:40])  # list.append is atomic: the sections run in a pool
            return ol._default_ask(system, user, **kw)

        if fresh:
            inventory.path_for(leaf).unlink(missing_ok=True)
        t0 = time.monotonic()
        inv = inventory.build(leaf, ask=counting)
        run = {"leaf": leaf, "calls": len(calls), "seconds": round(time.monotonic() - t0, 1), "model": inv["model"]}
        stem = Path(leaf).stem
        (out / f"{stem}.run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
        target = out / f"{stem}.md"
        if target.exists() and not force:
            # A sheet the maintainer marked is the gate's evidence: never overwritten by default.
            print(f"{leaf}: {run['calls']} calls, {run['seconds']} s; sheet kept ({target} exists, --force rewrites it)")
            continue
        leaf_raw = (Path(CONFIG.vault_path) / leaf).read_text(encoding="utf-8")
        target.write_text(sheet(inv, leaf_raw, run), encoding="utf-8")
        print(f"{leaf}: {run['calls']} calls, {run['seconds']} s -> {target}")
    return 0


def _score(out: Path) -> int:
    from silica.kernel.learn import inventory
    sheets, invs, runs = [], [], []
    for p in sorted(out.glob("*.md")):
        sh = read_sheet(p.read_text(encoding="utf-8"))
        inv = inventory.load(sh["leaf"]) if sh["leaf"] else None
        if inv is None:
            print(f"skipped {p.name}: no inventory for {sh['leaf']!r}", file=sys.stderr)
            continue
        sheets.append(sh)
        invs.append(inv)
        run = p.with_suffix(".run.json")
        if run.exists():
            runs.append(json.loads(run.read_text(encoding="utf-8")))
    print(json.dumps({**score(sheets, invs), "runs": runs}, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("leaves", nargs="+", help="vault-relative leaf paths, e.g. 'sources/Analisi 1 - Lezione 01.md'")
    b.add_argument("--fresh", action="store_true", help="delete each inventory first, so calls and time are a full build's")
    b.add_argument("--force", action="store_true", help="rewrite an existing review sheet")
    b.add_argument("--out")
    s = sub.add_parser("score")
    s.add_argument("--out")
    args = ap.parse_args(argv)
    out = _out_dir(args.out)
    if args.cmd == "build":
        return _build(args.leaves, out, fresh=args.fresh, force=args.force)
    return _score(out)


if __name__ == "__main__":
    sys.exit(main())
