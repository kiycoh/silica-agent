# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ViDoRe V3 (ACL 2026, arXiv 2601.08620) through the passage index: the
8 public sets, enterprise PDFs in English and French, every query written or
checked by people, the pages that answer it judged by people, each query
also in Italian (translated by the authors with Qwen3-235B).

Owner, 2026-11-08: "Vai con ViDoRe ma metti embedder su GPU". It checks the
page-vector choice of the same day (evals/probe_pdf_vs_md.py) on questions
nobody wrote from one page, and the Italian-to-English gap that bench found
(hit@5 0.16 words only, 0.66 with vectors).

Arms, one engine (silica-core as the agent runs it, `_engine()`):

  L_P  words only, over the PDFs as the agent reads them (`pdf_extract`)
  H_P  words and vectors (jina v5 small retrieval), over the same pages
  L_O  words only, over the set's own OCR markdown, one file a page
  H_O  words and vectors, over that markdown

P is the product; O is the text the published text pipelines rank, so an
O number can be set beside theirs and P against O says what the PDF's own
text layer costs.

Per query: NDCG@10 on the graded judgments (gain = grade, as pytrec_eval),
recall@10, hit@5; for P also hit@8 as the chat searches
(`silica_search_context`: k=8, silica-core's two sections a document), the
passages a turn actually reads. The question goes in as written: in chat the
model rewrites it into the vault's words first, which this does not measure.
Queries: the set's native language and Italian, paired by the question they
translate.

Usage:
  uvx --with pyarrow python -I evals/probe_vidore_v3.py prep --data DIR --out DIR
  uv run --no-sync python -m evals.probe_vidore_v3 run --out DIR
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from pathlib import Path

SETS = ["hr", "computer_science", "finance_en", "finance_fr", "pharmaceuticals", "energy", "physics", "industrial"]
# The two private sets (Nuclear, Telecom) are not downloadable.
LANGS = ("native", "italian")


def prep(data: Path, out: Path, sets: list[str]) -> None:
    """Parquet into JSON and the O arm's pages. Runs outside the project venv
    (it has no pyarrow), so stdlib and pyarrow only."""
    import pyarrow.parquet as pq

    def rows(s: str, part: str, cols=None) -> list[dict]:
        # a large corpus comes in shards (test-00000-of-00003, ...)
        return pq.read_table(sorted((data / s / part).glob("test-*.parquet")), columns=cols).to_pylist()

    for s in sets:
        docs = rows(s, "documents_metadata")
        file_of = {d["doc_id"]: d["file_name"] for d in docs}
        corpus = rows(s, "corpus", ["corpus_id", "doc_id", "page_number_in_doc", "markdown"])
        queries = rows(s, "queries", ["query_id", "query", "language", "query_types", "content_type", "answer"])
        qrels: dict[int, dict[int, int]] = {}
        for r in rows(s, "qrels", ["query_id", "corpus_id", "score"]):
            qrels.setdefault(r["query_id"], {})[r["corpus_id"]] = r["score"]
        langs = {d["doc_language"] for d in docs}
        assert len(langs) == 1, (s, langs)
        native = langs.pop()
        # The i-th query of a language, by id, translates the i-th of every
        # other (checked on hr: 318 of 318 share their judged pages, and the
        # texts read as one question). The answers are translated too, so they
        # cannot be the key. A pair whose judged pages differ is left out.
        ids = {lang: sorted(q["query_id"] for q in queries if q["language"] == lang) for lang in (native, "italian")}
        assert len(ids[native]) == len(ids["italian"]), (s, {k: len(v) for k, v in ids.items()})
        base = {}
        for i, (a, b) in enumerate(zip(ids[native], ids["italian"])):
            if qrels[a] != qrels[b]:
                print(f"{s}: queries {a} and {b} differ in judged pages, left out", file=sys.stderr)
                continue
            base[a] = base[b] = i
        keep = [dict(q, base=base[q["query_id"]], lang=("native" if q["language"] == native else q["language"]))
                for q in queries if q["query_id"] in base and q["language"] in (native, "italian")]
        o_dir = out / "O" / s
        o_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        for c in corpus:
            stem = Path(file_of[c["doc_id"]]).stem
            name = f"{stem} p. {c['page_number_in_doc'] + 1}.md"
            (o_dir / name).write_text(c["markdown"] or "", encoding="utf-8")
            pages.append({"corpus_id": c["corpus_id"], "file": file_of[c["doc_id"]],
                          "page": c["page_number_in_doc"] + 1, "o_path": name})
        dest = out / "prep" / s
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "set.json").write_text(json.dumps({
            "native": native, "pages": pages, "queries": keep,
            "qrels": {str(k): {str(c): g for c, g in v.items()} for k, v in qrels.items()},
            "licenses": sorted({d["license"] for d in docs})}, ensure_ascii=False))
        print(s, native, len(docs), "docs", len(pages), "pages", len(keep), "queries", flush=True)


def ndcg10(ranked: list[int], rel: dict[int, int]) -> float:
    dcg = sum(rel.get(c, 0) / math.log2(i + 2) for i, c in enumerate(ranked[:10]))
    ideal = sum(g / math.log2(i + 2) for i, g in enumerate(sorted(rel.values(), reverse=True)[:10]))
    return dcg / ideal if ideal else 0.0


def ranked_pages(core, query: str, k: int, per_doc: int, page_of, dense: bool) -> list[int]:
    r = core.search(query=query, k=k, per_doc=per_doc)
    if dense and (r.get("dense") or {}).get("state") != "ready":
        raise SystemExit(f"dense leg not ready: {r.get('dense')}")
    out: list[int] = []
    for hit in r.get("hits", []):
        cid = page_of(hit)
        if cid is not None and cid not in out:
            out.append(cid)
    return out


def run_set(out: Path, s: str, timing: dict) -> list[dict]:
    from evals.probe_pdf_vs_md import build, engine

    meta = json.loads((out / "prep" / s / "set.json").read_text())
    qrels = {int(q): {int(c): g for c, g in v.items()} for q, v in meta["qrels"].items()}
    by_pdf = {(p["file"], p["page"]): p["corpus_id"] for p in meta["pages"]}
    by_o = {p["o_path"]: p["corpus_id"] for p in meta["pages"]}
    p_root, o_root = out / "P" / s, out / "O" / s
    if not (p_root / "pdfs").exists():
        (p_root / "pdfs").mkdir(parents=True)
        for f in sorted({p["file"] for p in meta["pages"]}):
            # a hard link, not a symlink: the engine refuses a path that
            # resolves outside its root (probe_pdf_vs_md, 2026-11-08)
            os.link(out / "data" / s / "pdfs" / f, p_root / "pdfs" / f)

    def p_page(hit):
        sec = hit.get("section") or ""
        if not sec.startswith("p. "):
            return None
        return by_pdf.get((Path(hit["path"]).name, int(sec[3:])))

    def o_page(hit):
        return by_o.get(hit["path"])

    rows: list[dict] = []
    for unit, root, page_of, per_doc in (("P", p_root, p_page, 10), ("O", o_root, o_page, 1)):
        done = (root / ".built").exists()
        timing[f"{s}/{unit}"] = build(root, rebuild=not done)
        (root / ".built").touch()
        print(s, unit, "built", timing[f"{s}/{unit}"], flush=True)
        for dense in (False, True):
            arm = ("H_" if dense else "L_") + unit
            core = engine(root, dense)
            t = time.time()
            for q in meta["queries"]:
                rel = qrels[q["query_id"]]
                # per_doc 10 on a PDF lets ten pages of one report rank, as the
                # judged pages often sit in one; an O file is one page, so 1
                # keeps a page's heading sections from filling the ten
                ranked = ranked_pages(core, q["query"], 10, per_doc, page_of, dense)
                row = {"set": s, "arm": arm, "query_id": q["query_id"], "base": q["base"], "lang": q["lang"],
                       "types": q["query_types"], "ndcg10": round(ndcg10(ranked, rel), 4),
                       "recall10": round(len(set(ranked[:10]) & set(rel)) / len(rel), 4),
                       "hit5": bool(set(ranked[:5]) & set(rel))}
                if unit == "P":
                    chat = ranked_pages(core, q["query"], 8, 2, page_of, dense)
                    row["hit8_chat"] = bool(set(chat) & set(rel))
                rows.append(row)
            timing[f"{s}/{arm}/search_s"] = round(time.time() - t, 1)
            sub = [r for r in rows if r["arm"] == arm and r["set"] == s]
            print(s, arm, "ndcg10", round(sum(r["ndcg10"] for r in sub) / len(sub), 3),
                  f"({len(sub)} queries, {timing[f'{s}/{arm}/search_s']} s)", flush=True)
    return rows


def dense_only(out: Path, sets: list[str]) -> None:
    """D_P and D_O: the vector leg alone, ranked by section cosine, no fusion.
    Added 2026-11-08 when HR and Computer Science showed Italian 0.18 NDCG
    under native with vectors on, where the paper's best retriever loses
    0.017 between English and Italian: the gap is either the embedder's or
    the lexical leg's, which RRF fuses at equal weight. Reuses the vectors
    `run` built, so it needs the same embedder up for the queries only."""
    from evals.probe_pdf_vs_md import engine
    from silica_core import embeddings

    rows_file = out / "rows.jsonl"
    have = {(r["set"], r["arm"]) for r in map(json.loads, rows_file.open())}
    for s in sets:
        meta = json.loads((out / "prep" / s / "set.json").read_text())
        qrels = {int(q): {int(c): g for c, g in v.items()} for q, v in meta["qrels"].items()}
        by_pdf = {(p["file"], p["page"]): p["corpus_id"] for p in meta["pages"]}
        by_o = {p["o_path"]: p["corpus_id"] for p in meta["pages"]}
        rows: list[dict] = []
        for unit in ("P", "O"):
            arm = "D_" + unit
            if (s, arm) in have:
                continue
            root = out / unit / s
            core = engine(root, dense=True)
            live, _unextracted = core._inventory()
            page_at: dict[tuple[str, int], int] = {}
            if unit == "P":
                for rel in live:
                    _text, pages, _reason = core._pdf_text(rel, root / rel)
                    for p in pages:
                        cid = by_pdf.get((Path(rel).name, p["page"]))
                        if cid is not None:
                            page_at[(rel, p["offset"])] = cid
            t = time.time()
            for q in meta["queries"]:
                rel_q = qrels[q["query_id"]]
                _docs, secs = embeddings.rank(q["query"], live)
                ranked: list[int] = []
                for (rel, off), _c in sorted(secs.items(), key=lambda kv: (-kv[1], kv[0])):
                    cid = page_at.get((rel, off)) if unit == "P" else by_o.get(rel)
                    if cid is not None and cid not in ranked:
                        ranked.append(cid)
                        if len(ranked) == 10:
                            break
                rows.append({"set": s, "arm": arm, "query_id": q["query_id"], "base": q["base"], "lang": q["lang"],
                             "types": q["query_types"], "ndcg10": round(ndcg10(ranked, rel_q), 4),
                             "recall10": round(len(set(ranked) & set(rel_q)) / len(rel_q), 4),
                             "hit5": bool(set(ranked[:5]) & set(rel_q))})
            sub = [r for r in rows if r["arm"] == arm]
            print(s, arm, "ndcg10", round(sum(r["ndcg10"] for r in sub) / len(sub), 3),
                  f"({len(sub)} queries, {round(time.time() - t, 1)} s)", flush=True)
        with rows_file.open("a") as f:
            f.writelines(json.dumps(x) + "\n" for x in rows)


def mean(xs) -> float:
    xs = list(xs)
    return round(sum(xs) / len(xs), 4) if xs else float("nan")


def boot_delta(a: list[float], b: list[float], iters: int = 10000, seed: int = 42) -> dict:
    """Paired bootstrap of mean(a - b): NDCG is graded, McNemar does not fit."""
    d = [x - y for x, y in zip(a, b)]
    rng = random.Random(seed)
    n = len(d)
    boot = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return {"n": n, "delta": round(sum(d) / n, 4),
            "ci95": [round(boot[int(0.025 * iters)], 4), round(boot[int(0.975 * iters)], 4)]}


def report(rows: list[dict]) -> dict:
    from evals.paired_stats import paired

    arms = sorted({r["arm"] for r in rows})
    sets = [s for s in SETS if any(r["set"] == s for r in rows)]
    table = {}
    for arm in arms:
        for lang in LANGS:
            sub = [r for r in rows if r["arm"] == arm and r["lang"] == lang]
            table[f"{arm}/{lang}"] = {
                "ndcg10_macro": mean(mean(r["ndcg10"] for r in sub if r["set"] == s) for s in sets),
                "ndcg10_micro": mean(r["ndcg10"] for r in sub), "recall10": mean(r["recall10"] for r in sub),
                "hit5": mean(r["hit5"] for r in sub),
                **({"hit8_chat": mean(r["hit8_chat"] for r in sub)} if sub and all("hit8_chat" in r for r in sub) else {}),
                "per_set": {s: mean(r["ndcg10"] for r in sub if r["set"] == s) for s in sets}}

    def key(r):
        return (r["set"], r["query_id"])

    def pair(a_arm, a_lang, b_arm, b_lang, by="query"):
        # same query across arms, or the same question across languages
        k = key if by == "query" else (lambda r: (r["set"], r["base"]))
        a = {k(r): r for r in rows if r["arm"] == a_arm and r["lang"] == a_lang}
        b = {k(r): r for r in rows if r["arm"] == b_arm and r["lang"] == b_lang}
        common = sorted(set(a) & set(b))
        as_doc = lambda d: {"questions": [{"question_id": f"{c[0]}:{c[1]}", "correct": d[c]["hit5"]} for c in common]}
        return {"ndcg10": boot_delta([a[c]["ndcg10"] for c in common], [b[c]["ndcg10"] for c in common]),
                "hit5": paired(as_doc(a), as_doc(b))}

    comparisons = {}
    for lang in LANGS:
        comparisons[f"H_P vs L_P ({lang})"] = pair("H_P", lang, "L_P", lang)
        comparisons[f"H_O vs L_O ({lang})"] = pair("H_O", lang, "L_O", lang)
        comparisons[f"H_P vs H_O ({lang})"] = pair("H_P", lang, "H_O", lang)
        if "D_P" in arms:
            comparisons[f"H_P vs D_P ({lang})"] = pair("H_P", lang, "D_P", lang)
    for arm in arms:
        comparisons[f"{arm}: native vs italian"] = pair(arm, "native", arm, "italian", by="base")
    return {"arms": table, "comparisons": comparisons}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--sets", default=",".join(SETS))
    r = sub.add_parser("run")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--sets", default=",".join(SETS))
    d = sub.add_parser("dense-only")
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--sets", default=",".join(SETS))
    args = ap.parse_args(argv)
    if args.cmd == "prep":
        prep(args.data, args.out, args.sets.split(","))
        return 0
    if args.cmd == "dense-only":
        dense_only(args.out, args.sets.split(","))
        return 0
    rows_file = args.out / "rows.jsonl"
    done = {json.loads(line)["set"] for line in rows_file.open()} if rows_file.exists() else set()
    timing: dict = {}
    for s in args.sets.split(","):
        if s in done:
            continue  # a set's rows land whole, so a rerun resumes at the next set
        rows = run_set(args.out, s, timing)
        with rows_file.open("a") as f:
            f.writelines(json.dumps(x) + "\n" for x in rows)
        with (args.out / "timing.jsonl").open("a") as f:
            f.write(json.dumps(timing) + "\n")
    rows = [json.loads(line) for line in rows_file.open()]
    result = report(rows)
    (args.out / "results.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result["arms"], indent=1))
    print(json.dumps(result["comparisons"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
