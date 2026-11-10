# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A PDF's pages against its markdown, as the passage index searches them:
does a vector per page find the answer, and does it find it as well as the
same book converted to `.md` would (owner, 2026-11-08: "L'embedding può essere
fatto direttamente sul .pdf? ... valutiamo la resa contro il suo formato .md").

Four arms over the same books, one engine (silica-core, as the chat's
silica_search_context runs it, k=5, two sections a document):

  L_P  words only, over the PDFs' pages (what the laptop has today)
  H_P  words and vectors, over the pages (option B)
  L_M  words only, over the books converted by PDFium with no model reading
       a page, so the unit (a page against a heading section) and the
       cleaning are the only differences
  H_M  words and vectors, over that markdown

Questions: DeepSeek V4 Flash 0731 through OpenRouter (the production pin,
2026-11-06) reads one page and writes one question it answers, half in
Italian (the owner asks in Italian of English books), half in the page's own
language, with the shortest verbatim span that answers it. A hit is a
returned section holding that span, compared on letters and digits only, so
a hyphen or a line break the conversion joined does not count against it.

Known bias: a question written from one page favours an arm whose unit is a
page. A span the conversion changed is reported apart (`lost_in_md`).

Usage:
  uv run python -m evals.probe_pdf_vs_md --out /run/media/kiycoh/Data/silica-bench/pdf-vs-md-2026-11-08
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

LIBRARY = Path("/run/media/kiycoh/Data/lost-books-library")
MODEL = "openrouter/deepseek/deepseek-v4-flash-0731"
PROMPT = """Here is one page of the book "{book}".

{page}

Write ONE question, in {language}, that this page answers and that a reader
could ask without having seen it: do not quote the page, and do not name the
book or say "this page". Then copy from the page, verbatim, the shortest span
of 8 to 30 words that answers the question.
Reply with JSON only: {{"question": "...", "evidence": "..."}}"""


def norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def pick_books(library: Path, n: int, rng: random.Random) -> list[Path]:
    """One book a top folder while there are folders, 80 to 600 pages, text on
    at least 7 of 8 pages sampled across it: books the index reads, of a
    size ten of them embed in minutes."""
    import pypdfium2 as pdfium

    by_folder: dict[str, list[Path]] = {}
    for pdf in sorted(library.rglob("*.pdf")):
        doc = pdfium.PdfDocument(str(pdf))
        pages = len(doc)
        if 80 <= pages <= 600:
            sample = [int(pages * k / 8) for k in range(8)]
            with_text = sum(len(doc[i].get_textpage().get_text_range().strip()) > 200 for i in sample)
            if with_text >= 7:
                by_folder.setdefault(pdf.relative_to(library).parts[1], []).append(pdf)
        doc.close()
    folders = sorted(by_folder)
    rng.shuffle(folders)
    picked: list[Path] = []
    while len(picked) < n and any(by_folder.values()):
        for f in folders:
            if by_folder[f] and len(picked) < n:
                picked.append(by_folder[f].pop(rng.randrange(len(by_folder[f]))))
    return picked


def engine(root: Path, dense: bool):
    """The engine over `root`, its dense leg on or off; the vector cache is
    keyed by the file's stamp alone, so it is dropped between roots."""
    from evals._shared import adopt_bench_vault
    from silica.config import CONFIG
    from silica.kernel.recall.engine import engine as recall_engine
    from silica.tools import atomic  # noqa: F401  hands the engine its PDF reader
    from silica_core import embeddings
    from silica_core.config import CONFIG as CORE

    adopt_bench_vault(root)
    core = recall_engine()
    embeddings._CACHE = None
    CORE.embedding_base_url = CONFIG.embedding_base_url if dense else ""
    return core


def build(root: Path, rebuild: bool = True) -> dict:
    """The index with its vectors, or the run stops: the engine returns a
    failed embedding as a reply, and a run that read past it searched an
    arm with no vectors (2026-11-08: H_M stopped on `no_vectors` after an
    embedder 500, "Context size has been exceeded")."""
    t = time.time()
    r = engine(root, dense=True).build_index(rebuild=rebuild, embed=True)
    if "error" in r:
        raise SystemExit(f"build failed on {root}: {r['error']}")
    return {"docs": r["docs"], "failed": len(r.get("failed") or {}), "seconds": round(time.time() - t, 1),
            "embeddings": r.get("embeddings")}


def convert_books(books: list[Path], m_root: Path) -> dict[str, list[str]]:
    """Each book into markdown through PDFium, no page sent to a model: the
    pages without text and the formula pages stay as the text layer has them,
    which is what L_P and H_P search too."""
    from evals._shared import adopt_bench_vault
    from silica.config import CONFIG
    from silica.sources import convert as conv

    def no_reading(data, workdir, draw=None):
        data.pop("images", None)
        data.update(readings={}, read=[], math_read=[], unread=list(data.get("no_text", [])))

    conv._read_pages = no_reading
    (m_root / "src").mkdir(parents=True, exist_ok=True)
    adopt_bench_vault(m_root)
    CONFIG.pdf_provider = "pdfium"
    out: dict[str, list[str]] = {}
    for pdf in books:
        link = m_root / "src" / pdf.name
        if not link.exists():
            link.symlink_to(pdf)
        out[pdf.name] = conv.convert(f"src/{pdf.name}")
    shutil.rmtree(m_root / "src")  # the markdown alone is the M arms' corpus
    return out


def _parts_on_disk(m_root: Path) -> dict[str, list[str]]:
    """{book's file name: its notes}, from the `source_file` each note names."""
    import yaml

    out: dict[str, list[str]] = {}
    for md in sorted(m_root.rglob("*.md")):
        head = md.read_text(encoding="utf-8").split("---\n")
        fm = yaml.safe_load(head[1]) if len(head) > 2 else {}
        src = (fm or {}).get("source_file")
        if src:
            out.setdefault(Path(str(src)).name, []).append(md.relative_to(m_root).as_posix())
    return out


def ask(page: str, book: str, language: str) -> tuple[dict | None, dict]:
    import litellm

    r = litellm.completion(model=MODEL, messages=[{"role": "user", "content": PROMPT.format(
        book=book, page=page[:6000], language=language)}], max_tokens=400, temperature=0.3,
        response_format={"type": "json_object"})
    usage = {"in": r.usage.prompt_tokens, "out": r.usage.completion_tokens}
    try:
        got = json.loads(r.choices[0].message.content)
    except (TypeError, ValueError):
        return None, usage  # unparsed: the caller draws another page
    return got if isinstance(got, dict) else None, usage


def make_questions(p_root: Path, books: list[Path], per_book: int, rng: random.Random) -> tuple[list[dict], dict]:
    from silica.kernel.recall import engine as recall_engine

    engine(p_root, dense=False)
    questions, tokens = [], {"in": 0, "out": 0, "calls": 0}
    for pdf in books:
        rel = f"books/{pdf.name}"
        text, pages, _ = recall_engine.pdf_text(rel, p_root / rel)
        bounds = [p["offset"] for p in pages] + [len(text)]
        body = {p["page"]: text[p["offset"]:bounds[i + 1]] for i, p in enumerate(pages)}
        n = len(body)
        pool = [k for k in range(int(n * 0.1) + 1, int(n * 0.9) + 1) if len(body.get(k, "").strip()) >= 800]
        rng.shuffle(pool)
        made = 0
        for page_no in pool:
            if made >= per_book:
                break
            language = "Italian" if made % 2 == 0 else "the language the page is written in"
            got, usage = ask(body[page_no], pdf.stem, language)
            tokens["in"] += usage["in"]; tokens["out"] += usage["out"]; tokens["calls"] += 1
            if not got or not got.get("question") or len(norm(got.get("evidence", ""))) < 25 \
                    or norm(got["evidence"]) not in norm(body[page_no]):
                continue  # a span not on the page grades nothing
            questions.append({"id": f"{pdf.stem}:p{page_no}", "book": pdf.name, "page": page_no,
                              "italian": language == "Italian", "question": got["question"],
                              "evidence": got["evidence"]})
            made += 1
    return questions, tokens


def run_arm(root: Path, dense: bool, questions: list[dict], book_of: dict[str, str]) -> list[dict]:
    core = engine(root, dense)
    rows = []
    for q in questions:
        r = core.search(query=q["question"], k=5)
        if dense and (r.get("dense") or {}).get("state") != "ready":
            raise SystemExit(f"dense leg not ready on {root}: {r.get('dense')}")
        want, rank, doc_rank = norm(q["evidence"]), None, None
        for i, hit in enumerate(r.get("hits", []), 1):
            if doc_rank is None and book_of.get(hit["path"]) == q["book"]:
                doc_rank = i
            if rank is None:
                section = core.read(hit["path"], section=hit.get("section") or "", max_chars=40000)
                if want in norm(section.get("text", "")):
                    rank = i
        rows.append({"question_id": q["id"], "correct": rank is not None, "rank": rank, "doc_rank": doc_rank,
                     "italian": q["italian"]})
    return rows


def summary(rows: list[dict]) -> dict:
    n = len(rows)
    return {"n": n, "hit@1": round(sum(r["rank"] == 1 for r in rows) / n, 3),
            "hit@5": round(sum(r["correct"] for r in rows) / n, 3),
            "book@5": round(sum(r["doc_rank"] is not None for r in rows) / n, 3),
            "hit@5_italian": round(sum(r["correct"] for r in rows if r["italian"]) / max(1, sum(r["italian"] for r in rows)), 3),
            "hit@5_native": round(sum(r["correct"] for r in rows if not r["italian"]) / max(1, sum(not r["italian"] for r in rows)), 3)}


def main(argv=None) -> int:
    from evals.paired_stats import paired

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--books", type=int, default=10)
    ap.add_argument("--per-book", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261108)
    ap.add_argument("--resume", action="store_true",
                    help="reuse the arms built and the questions written by an earlier run in --out")
    args = ap.parse_args(argv)
    if not os.environ.get("OPENROUTER_API_KEY"):
        from silica.config import CONFIG  # noqa: F401  loads ~/.silica/.env
    rng = random.Random(args.seed)
    out, p_root, m_root = args.out, args.out / "P", args.out / "M"
    if args.resume:
        books = sorted((p_root / "books").iterdir())
        parts = _parts_on_disk(m_root)
        timing = {"P": build(p_root, rebuild=False), "M": build(m_root, rebuild=False)}
        questions, tokens = json.loads((out / "questions.json").read_text()), {"resumed": True}
        print("resumed:", timing, len(questions), "questions", flush=True)
    else:
        for d in (p_root, m_root):
            if d.exists():
                shutil.rmtree(d)
        (p_root / "books").mkdir(parents=True)
        books = pick_books(LIBRARY, args.books, rng)
        for pdf in books:
            # a hard link and not a symlink: silica_read refuses a path that
            # resolves outside the root, so a symlinked page could be found and
            # never read (the smoke run of 2026-11-08 scored 0 that way)
            os.link(pdf, p_root / "books" / pdf.name)
        print("books:", [b.name for b in books], flush=True)
        timing = {"P": build(p_root)}
        print("P built:", timing["P"], flush=True)
        parts = convert_books(books, m_root)
        timing["M"] = build(m_root)
        print("M built:", timing["M"], flush=True)
        questions, tokens = make_questions(p_root, books, args.per_book, rng)
        (out / "questions.json").write_text(json.dumps(questions, indent=1, ensure_ascii=False))
        print(f"questions: {len(questions)}, tokens {tokens}", flush=True)

    book_of_p = {f"books/{b.name}": b.name for b in books}
    book_of_m = {p: name for name, ps in parts.items() for p in ps}
    md_text = {name: norm("".join((m_root / p).read_text(encoding="utf-8") for p in ps if (m_root / p).is_file()))
               for name, ps in parts.items()}
    lost = [q["id"] for q in questions if norm(q["evidence"]) not in md_text.get(q["book"], "")]

    arms = {"L_P": run_arm(p_root, False, questions, book_of_p), "H_P": run_arm(p_root, True, questions, book_of_p),
            "L_M": run_arm(m_root, False, questions, book_of_m), "H_M": run_arm(m_root, True, questions, book_of_m)}
    as_doc = {k: {"questions": v} for k, v in arms.items()}
    kept = set(q["id"] for q in questions) - set(lost)
    on_kept = {k: {"questions": [r for r in v if r["question_id"] in kept]} for k, v in arms.items()}
    result = {
        "books": [{"name": b.name, "parts": len(parts[b.name])} for b in books],
        "timing": timing, "tokens": tokens, "lost_in_md": lost,
        "arms": {k: summary(v) for k, v in arms.items()},
        "paired": {"H_P vs L_P": paired(as_doc["H_P"], as_doc["L_P"]),
                   "H_P vs H_M": paired(as_doc["H_P"], as_doc["H_M"]),
                   "H_P vs H_M, spans kept in md": paired(on_kept["H_P"], on_kept["H_M"]),
                   "H_M vs L_M": paired(as_doc["H_M"], as_doc["L_M"])},
        "rows": arms,
    }
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps({k: result[k] for k in ("timing", "tokens", "arms", "paired")}, indent=1))
    print("lost in md:", len(lost))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
