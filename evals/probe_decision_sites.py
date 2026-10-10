# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed decisions at three sites, Jev (hosted) against Kev (local), 2026-09-27.

One question per site, the same `{state, questions}` body to both backends
(Kev serves TypeSafe's System One contract, so `decide._validate` checks both):

  sniff   choice  the form of a file head; items and labels are
                  `probe_sniff.build_items()` (63 heads, labels of 2026-09-19)
  orphan  noul    per (orphan, candidate) pair: should the candidate link to the
                  orphan? State and question are the production ones
                  (`orphan.typed_state`, `LINK_QUESTION`). Candidates are `curate.link_candidates` on the test
                  vault; a pair is positive when the vault already links the two
                  notes (either direction), else the label is hand-set in the
                  pairs file (`labeller` says whose). Baseline arm `llm`: the
                  production `orphan._decide_links` call, one per orphan.
  effort  score   a-priori effort of a LoCoMo question from its text alone,
                  against the category (single-hop < multi-hop < temporal /
                  open-domain) and against whether one retrieval put the gold
                  evidence in context (the retrieval-only run given to build).

Run:
  uv run python -m evals.probe_decision_sites build-orphan [--n 40]
  uv run python -m evals.probe_decision_sites build-effort --retrieval <metrics.json> [--per-cat 60]
  uv run python -m evals.probe_decision_sites run <site> <backend>   # backend: jev | kev | llm (orphan only)
  uv run python -m evals.probe_decision_sites report
Out: bench/decision_sites/ (raw, untracked).
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench/decision_sites"
VAULT = Path(os.environ.get("DECISION_VAULT", "~/Documents/Obsidian/test")).expanduser()
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"
KEV_URL = os.environ.get("KEV_URL", "http://127.0.0.1:8009/v1/systemone")
KEV_MODEL = os.environ.get("KEV_MODEL", "kev-4b")
HEAD = 2000

_LINK = re.compile(r"\[\[([^\]|#^]+)")

EFFORT_LEVELS = [
    "one fact, stated in one place",
    "two or more facts from different places, combined",
    "facts combined with date arithmetic or with inference beyond what is stated",
]
EFFORT_Q = {
    "effort": {
        "type": "score",
        "instructions": ("How much retrieval and reasoning will answering this question from a "
                         "personal memory of past conversations need?"),
        "criteria": EFFORT_LEVELS,
    },
}
# LoCoMo categories -> effort level expected by the question's own shape.
CAT_EFFORT = {4: 0, 1: 1, 2: 2, 3: 2}


# --- backends ----------------------------------------------------------------

def call(backend: str, state, questions: dict, timeout: float = 30.0) -> dict:
    import httpx

    import silica.config  # noqa: F401  loads ~/.silica/.env, where OPENROUTER_API_KEY lives
    from silica.agent.decide import _validate

    if backend == "jev":
        url, model = JEV_URL, JEV_MODEL
        headers = {"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}"}
    else:
        url, model = KEV_URL, KEV_MODEL
        headers = {}
    t0 = time.time()
    try:
        r = httpx.post(url, json={"model": model, "state": state, "questions": questions},
                       headers=headers, timeout=timeout)
        body = r.json() if r.status_code == 200 else {}
        err = "" if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:  # a failed call is a row with err, never a dead run
        body, err = {}, f"{type(e).__name__}: {e}"
    answers = body.get("answers") if isinstance(body, dict) else None
    if not err:
        err = _validate(answers, questions)
    return {"answers": answers if not err else None, "err": err,
            "s": round(time.time() - t0, 3), "served": body.get("model") if isinstance(body, dict) else None}


# --- datasets ----------------------------------------------------------------

def _notes() -> dict[str, str]:
    out = {}
    for p in VAULT.rglob("*.md"):
        rel = p.relative_to(VAULT).as_posix()
        if rel.startswith((".silica/", ".obsidian/")):
            continue
        out[rel] = p.read_text(encoding="utf-8", errors="replace")
    return out


def _strip_fm(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def _stem(rel: str) -> str:
    return rel.rsplit("/", 1)[-1].removesuffix(".md")


def build_orphan(n: int) -> None:
    from silica.tools.curate import link_candidates

    notes = _notes()
    by_stem = {_stem(r).lower(): r for r in notes}
    out_links: dict[str, set[str]] = {}
    for rel, text in notes.items():
        out_links[rel] = {by_stem[t.strip().lower()] for t in _LINK.findall(text)
                          if t.strip().lower() in by_stem} - {rel}
    indeg = {r: 0 for r in notes}
    for rel, tgts in out_links.items():
        for t in tgts:
            indeg[t] += 1
    pool = sorted(r for r in notes if len(_strip_fm(notes[r])) >= 300)
    rng = random.Random(7)
    linked = [r for r in pool if indeg[r] > 0]
    lonely = [r for r in pool if indeg[r] == 0]
    picks = rng.sample(linked, min(n * 3 // 4, len(linked))) + rng.sample(lonely, min(n - n * 3 // 4, len(lonely)))
    rows = []
    for x in picks:
        cands = link_candidates(x, k=5)
        for c in cands:
            cp = c["path"] if c["path"].endswith(".md") else c["path"] + ".md"
            if cp not in notes or cp == x:
                continue
            c2x, x2c = x in out_links.get(cp, ()), cp in out_links.get(x, ())
            rows.append({
                "orphan": x, "candidate": cp, "candidate_name": c["name"],
                "real_orphan": indeg[x] == 0,
                "human_link": "both" if c2x and x2c else "c->x" if c2x else "x->c" if x2c else None,
                "label": True if (c2x or x2c) else None,
                "labeller": "vault link" if (c2x or x2c) else None,
            })
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "orphan_pairs.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"{len(picks)} orphans, {len(rows)} pairs, {sum(r['label'] is None for r in rows)} to label")


def orphan_state(row: dict, notes: dict[str, str], *, names_only: bool = False) -> dict:
    """The production state (`orphan.typed_state`); names_only drops the
    candidate's opening, leaving the production LLM's input (names only)."""
    from silica.capabilities.orphan import typed_state

    state = typed_state(row["orphan"], notes[row["orphan"]], row["candidate"], notes[row["candidate"]])
    if names_only:
        state["candidate_note"].pop("opening")
    return state


def build_effort(retrieval: Path, per_cat: int) -> None:
    """Rows of a LoCoMo --retrieval-only run joined back to their question text
    (`conv-26_q0` = sample conv-26, qa 0); adversarial items are left out, they
    have no gold evidence to miss."""
    doc = json.loads(retrieval.read_text())
    data = {d["sample_id"]: d for d in json.loads((REPO / "bench/locomo10.json").read_text())}
    names = {"multi-hop": 1, "temporal": 2, "open-domain": 3, "single-hop": 4}
    by_cat: dict[int, list] = {}
    for r in doc["questions"]:
        cat = names.get(r.get("question_type"))
        if cat is None or r.get("gold_in_context") is None:
            continue
        sid, qi = r["question_id"].rsplit("_q", 1)
        qa = data[sid]["qa"][int(qi)]
        by_cat.setdefault(cat, []).append({"qid": r["question_id"], "question": qa["question"], "category": cat,
                                           "gold_in_context": r["gold_in_context"],
                                           "session_recall": r.get("session_recall")})
    rng = random.Random(7)
    items = [it for cat, rs in sorted(by_cat.items()) for it in rng.sample(rs, min(per_cat, len(rs)))]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "effort_items.json").write_text(json.dumps(items, indent=1, ensure_ascii=False))
    print({c: len(v) for c, v in by_cat.items()}, "->", len(items), "items,",
          sum(not it["gold_in_context"] for it in items), "retrieval misses")


# --- runs --------------------------------------------------------------------

def run(site: str, backend: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{site}.{backend}.json"
    if site == "sniff":
        from evals.probe_sniff import build_items
        from silica.kernel.forms import _FORM_CRITERIA, _SNIFF_INSTRUCTIONS

        q = {"form": {"type": "choice", "instructions": _SNIFF_INSTRUCTIONS, "criteria": _FORM_CRITERIA}}
        items = [(it.id, {"file_content": it.text[:HEAD]}, {"label": it.label, "lang": it.lang,
                                                            "synthetic": it.synthetic}) for it in build_items()]
    elif site in ("orphan", "orphan_names"):
        notes = _notes()
        pairs = json.loads((OUT / "orphan_pairs.json").read_text())
        if backend == "llm":
            return _run_orphan_llm(pairs, notes, out)
        from silica.capabilities.orphan import LINK_QUESTION as q
        items = [(f"{p['orphan']}|{p['candidate']}",
                  orphan_state(p, notes, names_only=site == "orphan_names"), p) for p in pairs]
    elif site == "effort":
        q = EFFORT_Q
        items = [(str(it["qid"]), {"question": it["question"]}, it)
                 for it in json.loads((OUT / "effort_items.json").read_text())]
    else:
        raise SystemExit(f"unknown site {site}")
    workers = 4 if backend == "jev" else 1

    def one(item):
        iid, state, meta = item
        return {"id": iid, **meta, **call(backend, state, q)}

    with ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(one, items))
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    errs = [r for r in rows if r["err"]]
    print(f"{site}/{backend}: {len(rows)} rows, {len(errs)} errors -> {out}")
    for r in errs[:3]:
        print("  ", r["err"])


def _run_orphan_llm(pairs: list[dict], notes: dict[str, str], out: Path) -> None:
    from silica.capabilities.orphan import _decide_links
    from silica.config import CONFIG

    groups: dict[str, list[dict]] = {}
    for p in pairs:
        groups.setdefault(p["orphan"], []).append(p)

    def one(x):
        cands = [{"name": p["candidate_name"], "path": p["candidate"]} for p in groups[x]]
        t0 = time.time()
        try:
            d = _decide_links(CONFIG, x, _strip_fm(notes[x])[:8000], cands)
            chosen, err = set(d.links), ""
        except Exception as e:
            chosen, err = set(), f"{type(e).__name__}: {e}"
        s = round(time.time() - t0, 3)
        return [{"id": f"{x}|{p['candidate']}", **p, "chosen": p["candidate_name"] in chosen,
                 "err": err, "s": s} for p in groups[x]]

    with ThreadPoolExecutor(4) as ex:
        rows = [r for rs in ex.map(one, list(groups)) for r in rs]
    out.write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"orphan/llm: {len(rows)} rows over {len(groups)} orphans -> {out}")


# --- report ------------------------------------------------------------------

def _auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    s = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(s / (len(pos) * len(neg)), 3)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(v):
        order = sorted(range(len(v)), key=v.__getitem__)
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(cov / den, 3) if den else None


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 3) if xs else None


def _mcnemar(a: list[bool], b: list[bool]) -> float:
    """Exact two-sided McNemar p on paired correctness."""
    n01 = sum(1 for x, y in zip(a, b) if x and not y)
    n10 = sum(1 for x, y in zip(a, b) if y and not x)
    n = n01 + n10
    if n == 0:
        return 1.0
    k = min(n01, n10)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * p), 4)


def report() -> dict:
    rep: dict = {}
    load = lambda name: json.loads((OUT / name).read_text()) if (OUT / name).is_file() else None  # noqa: E731

    # sniff: accuracy of the argmax, and of the production rule (form and p > 0.6).
    s = {b: load(f"sniff.{b}.json") for b in ("jev", "kev")}
    if all(s.values()):
        both = [(j, k) for j, k in zip(s["jev"], s["kev"]) if not j["err"] and not k["err"]]
        acc = {}
        for b, idx in (("jev", 0), ("kev", 1)):
            rows = [pair[idx] for pair in both]
            ok = [r["answers"]["form"]["choice"] == r["label"] for r in rows]
            p = [r["answers"]["form"]["probabilities"].get(r["answers"]["form"]["choice"], 0) for r in rows]
            took = [pp > 0.6 and r["answers"]["form"]["choice"] != "unsure" for pp, r in zip(p, rows)]
            acc[b] = {"n": len(rows), "accuracy": round(sum(ok) / len(rows), 3),
                      "taken_at_0.6": sum(took), "taken_right": sum(o for o, t in zip(ok, took) if t),
                      "auc_p_right_vs_wrong": _auc([pp for pp, o in zip(p, ok) if o], [pp for pp, o in zip(p, ok) if not o]),
                      "italian_acc": round(sum(o for o, r in zip(ok, rows) if r["lang"] == "it") /
                                           max(1, sum(r["lang"] == "it" for r in rows)), 3),
                      "p50_s": _pct([r["s"] for r in rows], .5), "p95_s": _pct([r["s"] for r in rows], .95)}
            acc[b]["_ok"] = ok
        acc["mcnemar_p"] = _mcnemar(acc["jev"].pop("_ok"), acc["kev"].pop("_ok"))
        acc["errors"] = {b: sum(1 for r in s[b] if r["err"]) for b in s}
        rep["sniff"] = acc

    # orphan: per pair, noul p vs label; decision rule p > tau, at most 3 per orphan.
    o = {b: load(f"orphan.{b}.json") for b in ("jev", "kev", "llm")}
    o.update({f"{b}_names": load(f"orphan_names.{b}.json") for b in ("jev", "kev")})
    labelled = lambda rows: [r for r in rows if r.get("label") is not None and not r.get("err")]  # noqa: E731
    orep = {}
    for b in ("jev", "kev", "jev_names", "kev_names"):
        if not o[b]:
            continue
        rows = labelled(o[b])
        p = [r["answers"]["link"]["noul"] for r in rows]
        y = [bool(r["label"]) for r in rows]
        best = None
        for tau in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
            pred = [pp > tau for pp in p]
            tp = sum(a and b_ for a, b_ in zip(pred, y))
            fp = sum(a and not b_ for a, b_ in zip(pred, y))
            fn = sum(b_ and not a for a, b_ in zip(pred, y))
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            orep.setdefault(b, {}).setdefault("by_tau", {})[tau] = {
                "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3), "links": tp + fp}
            if best is None or f1 > best[1]:
                best = (tau, f1)
        # The production rule caps an orphan at 3 links ("fewer is better"):
        # the same cap here, the top 3 by p among those over tau.
        for tau in (0.5, 0.6):
            keep = set()
            for x in {r["orphan"] for r in rows}:
                mine = sorted((r for r in rows if r["orphan"] == x and r["answers"]["link"]["noul"] > tau),
                              key=lambda r: -r["answers"]["link"]["noul"])[:3]
                keep.update(r["id"] for r in mine)
            pred = [r["id"] in keep for r in rows]
            tp = sum(a and b_ for a, b_ in zip(pred, y))
            fp = sum(a and not b_ for a, b_ in zip(pred, y))
            fn = sum(b_ and not a for a, b_ in zip(pred, y))
            prec, rec = (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)
            orep[b].setdefault("cap3", {})[tau] = {"precision": round(prec, 3), "recall": round(rec, 3),
                                                  "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0,
                                                  "links": tp + fp}
        vault_pos = [pp for pp, r in zip(p, rows) if r["labeller"] == "vault link"]
        mine_pos = [pp for pp, r in zip(p, rows) if r["label"] and r["labeller"] != "vault link"]
        neg = [pp for pp, yy in zip(p, y) if not yy]
        orep[b]["auc_vault_link_pos"] = _auc(vault_pos, neg)
        orep[b]["auc_hand_pos"] = _auc(mine_pos, neg)
        orep[b].update({"n": len(rows), "positives": sum(y),
                        "auc": _auc([pp for pp, yy in zip(p, y) if yy], [pp for pp, yy in zip(p, y) if not yy]),
                        "best_tau": best[0], "p50_s": _pct([r["s"] for r in rows], .5),
                        "errors": sum(1 for r in o[b] if r["err"])})
    if o["jev"] and o["llm"]:
        # The production rule, offline: Jev decides every orphan it answered
        # in full (at most 3 over LINK_ACCEPT), the LLM call the rest.
        from silica.capabilities.orphan import LINK_ACCEPT

        llm_by = {r["id"]: r for r in o["llm"]}
        by: dict[str, list] = {}
        for r in o["jev"]:
            by.setdefault(r["orphan"], []).append(r)
        pred, to_llm = {}, 0
        for x, rs in by.items():
            if any(r["err"] for r in rs):
                to_llm += 1
                pred.update({r["id"]: llm_by[r["id"]]["chosen"] for r in rs})
                continue
            top = sorted((r for r in rs if r["answers"]["link"]["noul"] > LINK_ACCEPT),
                         key=lambda r: -r["answers"]["link"]["noul"])[:3]
            pred.update({r["id"]: r in top for r in rs})
        rows = [r for r in o["jev"] if r.get("label") is not None]
        tp = sum(pred[r["id"]] and r["label"] for r in rows)
        fp = sum(pred[r["id"]] and not r["label"] for r in rows)
        fn = sum(r["label"] and not pred[r["id"]] for r in rows)
        orep["production_rule"] = {"tau": LINK_ACCEPT, "orphans_to_llm": to_llm,
                                   "precision": round(tp / (tp + fp), 3) if tp + fp else 0.0,
                                   "recall": round(tp / (tp + fn), 3) if tp + fn else 0.0,
                                   "right": sum(pred[r["id"]] == r["label"] for r in rows), "n": len(rows)}
    if o["llm"]:
        rows = labelled(o["llm"])
        pred, y = [r["chosen"] for r in rows], [bool(r["label"]) for r in rows]
        tp = sum(a and b_ for a, b_ in zip(pred, y))
        fp = sum(a and not b_ for a, b_ in zip(pred, y))
        fn = sum(b_ and not a for a, b_ in zip(pred, y))
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        orep["llm"] = {"n": len(rows), "precision": round(prec, 3), "recall": round(rec, 3),
                       "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0, "links": tp + fp,
                       "p50_s_per_orphan": _pct([r["s"] for r in o["llm"]], .5)}
    if orep:
        rep["orphan"] = orep

    # effort: expected level vs category effort, and vs a retrieval miss.
    e = {b: load(f"effort.{b}.json") for b in ("jev", "kev")}
    erep = {}
    for b, rows in e.items():
        if not rows:
            continue
        rows = [r for r in rows if not r["err"]]
        sc = [r["answers"]["effort"]["score"] for r in rows]
        cat = [CAT_EFFORT[r["category"]] for r in rows]
        miss = [not r["gold_in_context"] for r in rows]
        # gold_in_context is a string match on the answer (strict for paraphrased
        # temporal/multi-hop answers); a gold session left out is the cleaner miss.
        smiss = [(r.get("session_recall") or 0) < 1.0 for r in rows]
        erep[b] = {"n": len(rows),
                   "auc_score_predicts_session_miss": _auc([s_ for s_, m in zip(sc, smiss) if m],
                                                           [s_ for s_, m in zip(sc, smiss) if not m]),
                   "session_misses": sum(smiss),
                   "spearman_vs_category": _spearman(sc, cat),
                   "auc_score_predicts_miss": _auc([s_ for s_, m in zip(sc, miss) if m], [s_ for s_, m in zip(sc, miss) if not m]),
                   "mean_score_by_category": {c: round(sum(s_ for s_, r in zip(sc, rows) if r["category"] == c) /
                                                     max(1, sum(r["category"] == c for r in rows)), 3) for c in (4, 1, 2, 3)},
                   "p50_s": _pct([r["s"] for r in rows], .5)}
    if e.get("jev") or e.get("kev"):
        rows = next(r for r in e.values() if r)
        rows = [r for r in rows if not r["err"]]
        ln = [len(r["question"].split()) for r in rows]
        erep["baseline_length"] = {
            "spearman_vs_category": _spearman(ln, [CAT_EFFORT[r["category"]] for r in rows]),
            "auc_length_predicts_session_miss": _auc(
                [l_ for l_, r in zip(ln, rows) if (r.get("session_recall") or 0) < 1.0],
                [l_ for l_, r in zip(ln, rows) if (r.get("session_recall") or 0) >= 1.0]),
            "auc_length_predicts_miss": _auc([l_ for l_, r in zip(ln, rows) if not r["gold_in_context"]],
                                             [l_ for l_, r in zip(ln, rows) if r["gold_in_context"]])}
    if erep:
        rep["effort"] = erep
    (OUT / "report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(rep, indent=1))
    return rep


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    if a[0] == "build-orphan":
        build_orphan(int(a[a.index("--n") + 1]) if "--n" in a else 40)
    elif a[0] == "build-effort":
        build_effort(Path(a[a.index("--retrieval") + 1]),
                     int(a[a.index("--per-cat") + 1]) if "--per-cat" in a else 60)
    elif a[0] == "run":
        run(a[1], a[2])
    elif a[0] == "report":
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
