"""ADR-0134 point 7, step 3: `/check` on a student's rewrite of course notes,
measured from eight angles before the written reading relies on it.

The set (cards and the expected verdicts) is written by a separate agent
before the run, under `<gate>/set/` and `<gate>/expected.jsonl`; the run never
reads the expected file, only `--score` does, afterwards.

Each card is checked alone: written into the vault as `Gate-0134/<file>`,
checked through the product's `check_note` with the GUI's own search and
dense legs, then deleted with its cache, so no card is another's source and
no arm reads another's verdicts. Arms differ only in the judge:
  H   Jev, then the hosted chat model (deepseek-v4-flash-0731) when Jev
      answers nothing: the pilot's judge;
  L   Jev, then the owner's local chat model (Qwen3.5-9B on :1237);
  L0  the local chat model alone, Jev off: a laptop with no OpenRouter key.
Jev's answers are memoised by their exact request, so H and L pay Jev once;
`--repeat` asks Jev again for the same text, for angle 5.

Run (from the repo, the GUI's env):
  SILICA_VAULT=<copy> uv run --extra all python -m evals.probe_reading_check_gate \
      --gate <gate> --arm H
  ... --arm H --repeat         (the faithful cards of n01-n10, Jev asked again)
  ... --edit                   (angle 8: a ranged re-check after one edit)
  ... --score                  (all arms found under <gate>/runs/)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

GATE_DIR = "Gate-0134"
HOSTED = "openrouter/deepseek/deepseek-v4-flash-0731"
LOCAL = "custom/qwen3.5-9b"
JUDGED = ("F", "P", "M", "F2")
ERROR_KINDS = {"contradicted", "overstated", "misquoted"}

_lock = threading.Lock()
COUNT: Counter = Counter()


def _chat(model: str):
    from silica.agent.llm import call_llm

    def chat(prompt: str) -> str:
        with _lock:
            COUNT[f"chat:{model}"] += 1
        # reasoning off and 300 tokens, as cli._check_judge asks
        resp = call_llm(
            model,
            [{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.0,
            reasoning=False,
        )
        return resp.text or ""

    return chat


def _decide(memo_path: Path, fresh: bool):
    """Jev, memoised by its exact request across arms; `fresh` asks again."""
    from silica.agent.decide import decide

    memo: dict[str, object] = {}
    if memo_path.is_file():
        for line in memo_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            memo[row["k"]] = row["v"]

    def call(state, questions):
        k = hashlib.sha1(
            json.dumps([state, questions], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if not fresh and k in memo:
            with _lock:
                COUNT["jev:memo"] += 1
            return memo[k]
        with _lock:
            COUNT["jev:call"] += 1
        v = decide(state, questions)
        if v is not None and not fresh:
            memo[k] = v
            with memo_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"k": k, "v": v}, ensure_ascii=False) + "\n")
        return v

    return call


def _judge(arm: str, gate: Path, fresh: bool = False):
    from silica.kernel.check import judge as jd

    if arm == "L0":
        decide = lambda s, q: None  # noqa: E731
    else:
        decide = _decide(gate / "runs" / "jev_memo.jsonl", fresh)
    chat = _chat(HOSTED if arm == "H" else LOCAL)
    return lambda text, passages: jd.judge(text, passages, decide=decide, chat=chat)


def _legs():
    from silica.cli import _check_legs

    search, dense, floor = _check_legs()

    def only_notes(q: str, k: int) -> list[dict]:
        # a card under test is never its own or another card's passage
        return [
            h
            for h in search(q, k)
            if not str(h.get("path", "")).startswith(GATE_DIR + "/")
        ]

    return only_notes, dense, floor


def _paragraphs(text: str) -> list[tuple[int, int]]:
    """[(first, last)] 1-based lines of each paragraph after the heading."""
    lines = text.split("\n")
    fm_end = lines.index("---", 1) + 1 if lines and lines[0] == "---" else 0
    out, start = [], None
    for i, ln in enumerate(lines, 1):
        if i <= fm_end:
            continue
        if ln.strip() and not ln.startswith("#"):
            start = start or i
        elif start:
            out.append((start, i - 1))
            start = None
    if start:
        out.append((start, len(lines)))
    return out


def _vault_text(card: str) -> str:
    return "---\ntype: reading\nform: own\n---\n\n" + card.strip() + "\n"


def check_one(vault: Path, name: str, card: str, judge, lines=None) -> dict:
    from silica.kernel.check import run

    search, dense, floor = _legs()
    rel = f"{GATE_DIR}/{name}"
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    text = _vault_text(card)
    full.write_text(text, encoding="utf-8")
    calls_before = sum(
        v for k, v in COUNT.items() if k.startswith(("chat:", "jev:call"))
    )
    t0 = time.time()
    try:
        rep = run.check_note(
            rel,
            str(vault),
            write_dir="",
            search=search,
            dense=dense,
            dense_floor=floor,
            judge=judge,
            lines=lines,
        )
    finally:
        took = time.time() - t0
    calls = (
        sum(v for k, v in COUNT.items() if k.startswith(("chat:", "jev:call")))
        - calls_before
    )
    paras = _paragraphs(text)

    def para_of(line: int) -> int | None:
        return next((i for i, (a, z) in enumerate(paras, 1) if a <= line <= z), None)

    blocks: dict[int, dict] = {}
    for s in rep["states"]:
        blocks.setdefault(
            s["line"], {"para": para_of(s["line"]), "states": [], "findings": []}
        )["states"].append({"state": s["state"], "say": s.get("say", "")[:200]})
    for f in rep["findings"]:
        blocks.setdefault(
            f["line"], {"para": para_of(f["line"]), "states": [], "findings": []}
        )["findings"].append(
            {
                "grade": f["grade"],
                "kind": f["kind"],
                "model": bool(f.get("model")),
                "by": f.get("by"),
                "p": f.get("p"),
                "at": f.get("at", ""),
                "say": f.get("say", "")[:240],
            }
        )
    return {
        "file": name,
        "blocks": {str(k): v for k, v in sorted(blocks.items())},
        "n_blocks": rep["blocks"],
        "counts": rep["counts"],
        "seconds": round(took, 2),
        "calls": calls,
        "paras": paras,
    }


def _clean(vault: Path, name: str) -> None:
    from silica.kernel.check import run

    (vault / GATE_DIR / name).unlink(missing_ok=True)
    run._cache_path(str(vault), f"{GATE_DIR}/{name}").unlink(missing_ok=True)


def run_arm(gate: Path, vault: Path, arm: str, repeat: bool) -> None:
    judge = _judge(arm, gate, fresh=repeat)
    files = sorted(
        p for p in (gate / "set").glob("*.md") if p.stem.split("-", 1)[1] in JUDGED
    )
    if repeat:
        files = [p for p in files if p.stem.endswith("-F") and int(p.stem[1:3]) <= 10]
    out = gate / "runs" / f"{arm}{'-repeat' if repeat else ''}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = (
        {json.loads(x)["file"] for x in out.read_text(encoding="utf-8").splitlines()}
        if out.is_file()
        else set()
    )
    for p in files:
        if p.name in done:
            continue
        try:
            row = check_one(vault, p.name, p.read_text(encoding="utf-8"), judge)
        finally:
            _clean(vault, p.name)
        row["arm"] = arm
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"[{arm}] {p.name:<10} blocks {row['n_blocks']} calls {row['calls']} {row['seconds']}s "
            f"{dict(row['counts'])}",
            flush=True,
        )
    print(f"[{arm}] counters {dict(COUNT)}", flush=True)


def run_edit(gate: Path, vault: Path) -> None:
    """Angle 8: a card checked whole, one paragraph edited, then the card
    checked again by its line range: the blocks judged the second time."""
    from silica.kernel.check import run

    judge = _judge("H", gate)
    rows = []
    for p in sorted((gate / "set").glob("n0[1-5]-F.md")):
        card = p.read_text(encoding="utf-8")
        name = p.name
        try:
            first = check_one(vault, name, card, judge)
            paras = first["paras"]
            # one sentence more at the end of the last paragraph: one block changed
            edited = card.rstrip() + " Lo riporta la nota.\n"
            text = _vault_text(edited)
            (vault / GATE_DIR / name).write_text(text, encoding="utf-8")
            calls_before = sum(
                v for k, v in COUNT.items() if k.startswith(("chat:", "jev:call"))
            )
            t0 = time.time()
            search, dense, floor = _legs()
            rep = run.check_note(
                f"{GATE_DIR}/{name}",
                str(vault),
                write_dir="",
                search=search,
                dense=dense,
                dense_floor=floor,
                judge=judge,
                lines=(paras[0][0], paras[-1][1]),
            )
            again = (
                sum(v for k, v in COUNT.items() if k.startswith(("chat:", "jev:call")))
                - calls_before
            )
            rows.append(
                {
                    "file": name,
                    "first_calls": first["calls"],
                    "first_s": first["seconds"],
                    "first_blocks": first["n_blocks"],
                    "recheck_calls": again,
                    "recheck_s": round(time.time() - t0, 2),
                    "recheck_cached": rep["cached"],
                }
            )
            print(rows[-1], flush=True)
        finally:
            _clean(vault, name)
    (gate / "runs" / "edit.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )


def run_code_checks(gate: Path, vault: Path) -> None:
    """Angle 6: the copy and central-claim checks, by code, on every card."""
    from silica.kernel.learn import reading

    expected_titles = {}
    rows = []
    for p in sorted((gate / "set").glob("*.md")):
        card = p.read_text(encoding="utf-8")
        title = re.search(r"\[\[([^\]|#]+)", card)
        title = title.group(1) if title else ""
        if title not in expected_titles:
            hits = list((vault / "Cultura").rglob(f"{title}.md"))
            expected_titles[title] = (
                hits[0].relative_to(vault).as_posix() if hits else ""
            )
        note = expected_titles[title]
        if not note:
            continue
        note_text = (vault / note).read_text(encoding="utf-8")
        body = card.split("\n", 2)[-1]
        found, of = reading.copy_share(body, note_text)
        lede = reading.note_lede(note_text, is_text=True)
        rows.append(
            {
                "file": p.name,
                "copy_found": found,
                "copy_of": of,
                "is_copy": reading.is_copy(body, note_text),
                "holds_lede": reading.holds_lede(body, lede),
                "lede": lede[:160],
            }
        )
    (gate / "runs").mkdir(parents=True, exist_ok=True)
    (gate / "runs" / "code.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"code checks on {len(rows)} cards", flush=True)


# ------------------------------------------------------------------ scoring


def _grade(block: dict) -> str:
    gs = [f["grade"] for f in block["findings"]]
    return (
        "error"
        if "error" in gs
        else "warning"
        if "warning" in gs
        else "nit"
        if "nit" in gs
        else (block["states"][0]["state"] if block["states"] else "none")
    )


def _verdict(block: dict) -> str:
    """The block's one-word outcome: an error kind, a warning kind, or its state."""
    for g in ("error", "warning"):
        ks = [f["kind"] for f in block["findings"] if f["grade"] == g]
        if ks:
            return ks[0]
    return block["states"][0]["state"] if block["states"] else "no finding"


def score(gate: Path) -> None:
    exp = {
        json.loads(x)["file"]: json.loads(x)
        for x in (gate / "expected.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    }
    runs = gate / "runs"
    report: dict = {}
    for arm in ("H", "L", "L0"):
        f = runs / f"{arm}.jsonl"
        if not f.is_file():
            continue
        rows = {
            json.loads(x)["file"]: json.loads(x)
            for x in f.read_text(encoding="utf-8").splitlines()
        }
        by_para = lambda r, k: [b for b in r["blocks"].values() if b["para"] == k]  # noqa: E731
        a: dict = {}
        # 1. recall on prose errors
        hits, miss = Counter(), []
        for name, e in exp.items():
            if e["variant"] != "P" or name not in rows:
                continue
            bl = by_para(rows[name], e["error_para"])
            caught = any(any(x["grade"] == "error" for x in b["findings"]) for b in bl)
            hits[(e["error_kind"], caught)] += 1
            hits[("para", e["error_para"], caught)] += 1
            if not caught:
                miss.append(
                    {
                        "file": name,
                        "kind": e["error_kind"],
                        "para": e["error_para"],
                        "got": [_verdict(b) for b in bl],
                        "says": [x["say"] for b in bl for x in b["findings"]][:2],
                    }
                )
        n_p = sum(1 for e in exp.values() if e["variant"] == "P" and e["file"] in rows)
        a["1_prose_errors_at_error"] = (
            f"{sum(v for k, v in hits.items() if len(k) == 2 and k[1])} of {n_p}"
        )
        a["1_by_kind"] = {
            k: f"{hits[(k, True)]} of {hits[(k, True)] + hits[(k, False)]}"
            for k in ("contradicted", "overstated", "relation")
        }
        a["1_by_para"] = {
            p: f"{hits[('para', p, True)]} of {hits[('para', p, True)] + hits[('para', p, False)]}"
            for p in (1, 2, 3)
        }
        a["1_misses"] = miss
        # 2. false errors (and the warnings a faithful card would be asked about)
        fe, fw, nb, false_rows = 0, Counter(), 0, []
        for name, e in exp.items():
            if e["variant"] not in ("F", "F2") or name not in rows:
                continue
            for b in rows[name]["blocks"].values():
                nb += 1
                if any(x["grade"] == "error" for x in b["findings"]):
                    fe += 1
                    false_rows.append(
                        {
                            "file": name,
                            "para": b["para"],
                            "got": _verdict(b),
                            "says": [x["say"] for x in b["findings"]][:2],
                        }
                    )
                for x in b["findings"]:
                    if x["grade"] == "warning":
                        fw[x["kind"]] += 1
        a["2_faithful_blocks_at_error"] = f"{fe} of {nb}"
        a["2_false_errors"] = false_rows
        a["2_faithful_warnings"] = dict(fw)
        # 3. formulas
        fh = Counter()
        for name, e in exp.items():
            if e["variant"] != "M" or name not in rows:
                continue
            caught = any(
                any(x["grade"] == "error" for x in b["findings"])
                for b in rows[name]["blocks"].values()
                if b["para"] == e["error_para"] or e["error_para"] is None
            )
            fh[("display" if e.get("formula_display") else "inline", caught)] += 1
        a["3_formula_errors_at_error"] = {
            k: f"{fh[(k, True)]} of {fh[(k, True)] + fh[(k, False)]}"
            for k in ("inline", "display")
        }
        # 4. linked first paragraph against unlinked later ones, on faithful cards
        st = defaultdict(Counter)
        for name, e in exp.items():
            if e["variant"] != "F" or name not in rows:
                continue
            for b in rows[name]["blocks"].values():
                st["linked" if b["para"] == 1 else "unlinked"][_verdict(b)] += 1
        a["4_verdicts_faithful_by_link"] = {k: dict(v) for k, v in st.items()}
        # 5. two faithful wordings of the same claims
        same = diff = 0
        pairs = []
        for i in range(1, 11):
            f1, f2 = rows.get(f"n{i:02d}-F.md"), rows.get(f"n{i:02d}-F2.md")
            if not f1 or not f2:
                continue
            for k in (1, 2, 3):
                g1 = [_grade(b) for b in by_para(f1, k)]
                g2 = [_grade(b) for b in by_para(f2, k)]
                if g1 == g2:
                    same += 1
                else:
                    diff += 1
                    pairs.append({"note": f"n{i:02d}", "para": k, "F": g1, "F2": g2})
        a["5_wordings_same_grade"] = f"{same} of {same + diff}"
        # what the loop acts on: whether the paragraph is at error at all
        a["5_wordings_same_error_outcome"] = (
            f"{same + sum(1 for x in pairs if ('error' in x['F']) == ('error' in x['F2']))} of {same + diff}"
        )
        a["5_wordings_differ"] = pairs
        rep_f = runs / f"{arm}-repeat.jsonl"
        if rep_f.is_file():
            again = {
                json.loads(x)["file"]: json.loads(x)
                for x in rep_f.read_text(encoding="utf-8").splitlines()
            }
            s2 = n2 = e2 = 0
            for name, r in again.items():
                for line, b in r["blocks"].items():
                    b0 = rows.get(name, {}).get("blocks", {}).get(line)
                    if b0 is None:
                        continue
                    n2 += 1
                    s2 += _verdict(b0) == _verdict(b)
                    e2 += (_grade(b0) == "error") == (_grade(b) == "error")
            a["5_same_text_same_verdict"] = f"{s2} of {n2}"
            a["5_same_text_same_error_outcome"] = f"{e2} of {n2}"
        # 8. cost and time
        secs = [r["seconds"] for r in rows.values()]
        calls = [r["calls"] for r in rows.values()]
        a["8_per_card"] = {
            "cards": len(rows),
            "median_s": sorted(secs)[len(secs) // 2] if secs else None,
            "max_s": max(secs) if secs else None,
            "calls_total": sum(calls),
        }
        report[arm] = a
    code = runs / "code.jsonl"
    if code.is_file():
        rows6 = [
            json.loads(x)
            for x in code.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
        c = Counter()
        for r in rows6:
            v = exp.get(r["file"], {}).get("variant")
            c[(v, "copy", r["is_copy"])] += 1
            c[(v, "lede", r["holds_lede"])] += 1
        report["6_code_checks"] = {
            "copies_flagged (C)": f"{c[('C', 'copy', True)]} of {c[('C', 'copy', True)] + c[('C', 'copy', False)]}",
            "faithful flagged as copy (F, F2)": f"{c[('F', 'copy', True)] + c[('F2', 'copy', True)]} of "
            f"{sum(c[(v, 'copy', b)] for v in ('F', 'F2') for b in (True, False))}",
            "lede left out caught (N)": f"{c[('N', 'lede', False)]} of {c[('N', 'lede', True)] + c[('N', 'lede', False)]}",
            "faithful missing the lede (F, F2)": f"{c[('F', 'lede', False)] + c[('F2', 'lede', False)]} of "
            f"{sum(c[(v, 'lede', b)] for v in ('F', 'F2') for b in (True, False))}",
            "faithful flagged as copy, files": [
                r["file"]
                for r in rows6
                if r["is_copy"] and exp.get(r["file"], {}).get("variant") in ("F", "F2")
            ],
            "faithful missing the lede, files": [
                r["file"]
                for r in rows6
                if not r["holds_lede"]
                and exp.get(r["file"], {}).get("variant") in ("F", "F2")
            ],
        }
    edit = runs / "edit.json"
    if edit.is_file():
        report["8_recheck_after_one_edit"] = json.loads(
            edit.read_text(encoding="utf-8")
        )
    (runs / "score.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", required=True, type=Path)
    ap.add_argument("--arm", choices=("H", "L", "L0"))
    ap.add_argument("--repeat", action="store_true")
    ap.add_argument("--edit", action="store_true")
    ap.add_argument("--code", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.score:
        score(a.gate)
        return 0
    from silica.config import CONFIG

    vault = Path(CONFIG.vault_path)
    if not (vault / "Plans").is_dir() or "scratch" not in str(vault):
        sys.exit(
            f"refusing to write test cards into {vault}: run on a vault copy under scratch/"
        )
    if a.code:
        # reads the set and the notes only, so it may run beside an arm
        run_code_checks(a.gate, vault)
        return 0
    if a.edit:
        run_edit(a.gate, vault)
    elif a.arm:
        run_arm(a.gate, vault, a.arm, a.repeat)
    shutil.rmtree(vault / GATE_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
