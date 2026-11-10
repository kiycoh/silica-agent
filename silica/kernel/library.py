# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A vault's files as a library (ADR-0156): what Silica holds of each file,
a book's chapters, the editions a set of photographs reproduces, and the
files as a layer of Explore's graph.

The laptop's only. On the server a tenant reaches a file through the note
that names it (ADR-0085), and its embedder is remote (consent), so every
entry point here answers empty there.
"""
from __future__ import annotations

import colorsys
import logging
import math
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# --- what Silica holds of a file (the rail's rule, ADR-0129 amended 2026-11-08)


def file_states(vault: Path, raws: dict[str, list[str]]) -> dict[str, str]:
    """{vault path: read | unread | unindexed} for every file that is neither
    a note nor a raw a note names: `read` when the passage index has its text
    or a `.md` beside it is its note, `unread` when the index found no text in
    it or never reads its kind, `unindexed` when the index has not reached it.
    Read off the index's own stamps, so the rail, Explore and silica_files
    never disagree about one file. Private names of the engine and not its
    silica_files, which reads every note's head: 1.33 s on 3,202 files against
    two walks here (2026-11-04)."""
    from silica.config import hosted
    from silica.tools.atomic import _engine, _vault_files_under

    if hosted():
        return {}
    try:
        core = _engine()
        meta, live, known = core._load_meta(), core._notes(), True
    except Exception:
        # An engine that cannot load cannot say what it read: every file is
        # listed as not indexed, which is all that is true of it then.
        logger.warning("library: the passage index could not be read", exc_info=True)
        meta, live, known = {}, {}, False
    stamps, skipped = meta.get("stamps", {}), meta.get("no_text", {})
    out: dict[str, str] = {}
    for rel in _vault_files_under(""):
        if rel.endswith(".md") or rel in raws:
            continue
        if rel in live:  # a document the index reads: a PDF, a text file, code
            mt = live[rel]
            out[rel] = ("read" if stamps.get(rel) == mt
                        else "unread" if skipped.get(rel, {}).get("mtime") == mt
                        else "unindexed")
        elif (vault / rel).with_suffix(".md").is_file():
            out[rel] = "read"  # converted beside itself: the `.md` is its note
        else:
            out[rel] = "unread" if known else "unindexed"
    return out


def file_act(rel: str) -> str:
    """What the drawer offers an unread file: `ocr` for a PDF or a picture,
    whose words are pixels; `convert` for a document convert() reads (an
    e-book, an office file, a recording); nothing for a kind it does not."""
    from silica.sources.convert import DOC_EXTS, IMG_EXTS

    suffix = Path(rel).suffix.lower()
    return "ocr" if suffix == ".pdf" or suffix in IMG_EXTS else "convert" if suffix in DOC_EXTS else ""


# --- a book's chapters (ADR-0156 point 6) -----------------------------------

# The front of a book the outline names and a reader does not study.
_FRONT_RE = re.compile(r"^(title|copyright|contents|table of contents|cover|front|half.?title|"
                       r"dedication|frontespizio|indice|sommario|colophon)\b", re.I)
# A page that opens a chapter, by its first line: measured 2026-11-08 on the
# 101 library books with no outline, 36 have five or more such pages.
_CHAPTER_RE = re.compile(r"^(chapter|chap\.|book|part|lecture|section|canto|capitolo|cap\.|libro|parte|"
                         r"kapitel|buch|teil|lettera)\b[\s\w.:'’-]{0,60}$|^[IVXLC]{1,6}\.?$", re.I)
CHAPTER_PAGES_MIN = 5   # fewer chapter pages than this is no structure
MERGE_UNDER = 8         # a step under this many pages joins the next
SPLIT_OVER = 30         # a step over this many pages is cut ...
RUN = 15                # ... into runs of this many, the steps of a book with nothing to cut at
# 8, 30 and 15 are starting values, not measured (ADR-0156, Not decided).

_chapters_memo: dict[str, tuple[tuple, dict]] = {}


def _outline(full: Path) -> tuple[int, list[tuple[str, int]]]:
    """(pages, [(title, 1-based page)]) of the outline's top entries."""
    import pypdfium2 as pdfium

    from silica.sources import pdf_extract

    with pdf_extract._PDFIUM:  # one thread in PDFium at a time (test_pdf_hostile)
        pdf = pdfium.PdfDocument(os.fspath(full))
        try:
            n = len(pdf)
            entries = []
            for b in pdf.get_toc(max_depth=1):
                dest = b.get_dest()
                index = dest.get_index() if dest is not None else None
                title = " ".join((b.get_title() or "").split())
                if title and index is not None and 0 <= index < n:
                    entries.append((title, index + 1))
        finally:
            pdf.close()
    return n, entries


def _page_texts(rel: str, full: Path) -> list[str]:
    """Each page's text as the passage index holds it, "" for a page with none."""
    from silica.tools.atomic import _engine, _pdf_text

    _engine()  # the engine's root is this vault before its cache is asked
    text, pages, _reason = _pdf_text(rel, full)
    bounds = [p["offset"] for p in pages] + [len(text)]
    by_page = {p["page"]: text[p["offset"]:bounds[i + 1]] for i, p in enumerate(pages)}
    return [by_page.get(k, "") for k in range(1, max(by_page, default=0) + 1)]


def _first_line(page: str) -> str:
    return next((line.strip() for line in page.splitlines() if line.strip()), "")


def ladder(n: int, outline: list[tuple[str, int]], openings: list[tuple[str, int]]) -> tuple[str, list[dict]]:
    """(source, steps): the outline where it names two body entries or more,
    else the chapter pages where there are CHAPTER_PAGES_MIN or more, else
    runs of RUN pages; then a step under MERGE_UNDER pages joins the next and
    one over SPLIT_OVER is cut into runs of RUN. Each step is {title, from, to},
    pages 1-based and inclusive. Pure, for the tests."""
    body = sorted({p: t for t, p in outline if not _FRONT_RE.match(t)}.items())
    if len(body) >= 2:
        source, starts = "outline", body
    elif len(openings) >= CHAPTER_PAGES_MIN:
        source, starts = "chapters", sorted({p: t for t, p in openings}.items())
    else:
        source, starts = "pages", []
    steps: list[dict]
    if not starts:
        steps = [{"title": "", "from": a, "to": min(n, a + RUN - 1)} for a in range(1, n + 1, RUN)]
    else:
        # The pages before the first chapter are the front (title, contents,
        # copyright), which no step holds: a plan starts where the book does.
        steps = [{"title": t, "from": p, "to": (starts[i + 1][0] - 1 if i + 1 < len(starts) else n)}
                 for i, (p, t) in enumerate(starts)]
    # A step that takes in the next keeps that one's title in `parts`: the
    # Book of Enoch's "Ch. 1 Blessing of Enoch" (3 pages) vanished into
    # "About the Book of Enoch, pp. 6 to 13" before it did (2026-11-08).
    def join(into: dict, step: dict) -> None:
        into["to"] = step["to"]
        into["parts"] = into.get("parts", []) + [t for t in [step["title"], *step.get("parts", [])] if t]

    merged: list[dict] = []
    for s in steps:
        if merged and merged[-1]["to"] - merged[-1]["from"] + 1 < MERGE_UNDER:
            join(merged[-1], s)
        else:
            merged.append(dict(s))
    if len(merged) > 1 and merged[-1]["to"] - merged[-1]["from"] + 1 < MERGE_UNDER:
        tail = merged.pop()  # popped first: `merged[-2]` read after a pop is the step before
        join(merged[-1], tail)
    out: list[dict] = []
    for s in merged:
        size = s["to"] - s["from"] + 1
        if size <= SPLIT_OVER:
            out.append(s)
            continue
        runs = math.ceil(size / RUN)
        for i in range(runs):
            a = s["from"] + i * RUN
            run = {"title": f"{s['title']} ({i + 1}/{runs})" if s["title"] else "",
                   "from": a, "to": min(s["to"], a + RUN - 1)}
            if i == 0 and s.get("parts"):
                run["parts"] = s["parts"]  # the titles it took in, named once, where it starts
            out.append(run)
    for s in out:
        s["title"] = s["title"] or (f"pages {s['from']} to {s['to']}" if s["to"] > s["from"] else f"page {s['from']}")
    return source, out


def chapters(rel: str) -> dict:
    """{source, pages, steps} of a vault PDF, {} for any other file or one that
    cannot be read. Cached on the file's mtime and size."""
    from silica.config import CONFIG

    full = Path(CONFIG.vault_path or "") / rel
    if full.suffix.lower() != ".pdf":
        return {}
    try:
        st = full.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    hit = _chapters_memo.get(str(full))
    if hit and hit[0] == key:
        return hit[1]
    try:
        n, outline = _outline(full)
    except Exception:
        # A file PDFium refuses (a renamed HTML page, a password) has no
        # chapters to list; the drawer still shows it, and says nothing here.
        logger.info("library: no outline read from %s", rel, exc_info=True)
        return {}
    openings: list[tuple[str, int]] = []
    if len([t for t, _ in outline if not _FRONT_RE.match(t)]) < 2:
        openings = [(_first_line(page), i + 1) for i, page in enumerate(_page_texts(rel, full))
                    if _CHAPTER_RE.match(_first_line(page))]
    source, steps = ladder(n, outline, openings)
    out = {"source": source, "pages": n, "steps": steps}
    _chapters_memo[str(full)] = (key, out)
    return out


# --- photo sets and the editions they reproduce (ADR-0156 point 4) ----------

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".jp2")
_YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20[0-4]\d)(?!\d)")


def editions(sets: list[str]) -> dict[str, list[str]]:
    """{edition label: its sets}: the sets whose folder names carry one year
    are one edition of that year, a set with no year is its own.
    shortcut: the year alone, so two different books printed the same year
    join; split by the words beside the year when a library shows one."""
    out: dict[str, list[str]] = {}
    for s in sorted(sets):
        years = _YEAR_RE.findall(s.rsplit("/", 1)[-1])
        out.setdefault(f"{years[0]} edition" if years else s.rsplit("/", 1)[-1], []).append(s)
    return out


# --- the files as a layer of Explore (ADR-0156 points 1 to 3) ---------------

KNN = 3  # a book's nearest books drawn; a starting value, not measured
_APPARATUS_RE = re.compile(r"^(bibliography|bibliografia|references|works cited|index|indice|"
                           r"contents|table of contents|sommario|general index|major titles)\b", re.I)
_EDGE_SHARE = 0.02  # the first and last pages of a book, its front and its back
_layer_memo: dict[str, tuple[tuple, dict]] = {}
_layer_lock = threading.Lock()
_STOP = {"the", "of", "and", "in", "a", "to", "on", "its", "with", "for", "di", "e", "il", "la",
         "le", "gli", "dei", "del", "delle", "da"}


def _title_phrase(stem: str) -> str:
    s = re.sub(r"^\d+[_-]+", "", stem)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\b(1[5-9]\d\d|20\d\d|v\d+|t\d+|complete|searchable|ita)\b", " ", s, flags=re.I)
    words = [w for w in re.split(r"\W+", s.lower()) if w]
    return " ".join(words) if len([w for w in words if w not in _STOP]) >= 2 else ""


def _area_of(rels: list[str]) -> dict[str, str]:
    """A file's area is its folder under the folders every file shares: a
    library kept under one `inbox/` has its themes as areas, not one area."""
    folders = [r.split("/")[:-1] for r in rels]
    common: list[str] = []
    for parts in zip(*folders) if folders else ():
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    depth = len(common)
    return {r: (f[depth] if len(f) > depth else (f[-1] if f else "(vault)")) for r, f in zip(rels, folders)}


def _muted(hex_colour: str) -> str:
    """The area's colour with most of its saturation and some of its light
    gone: an unread file is filled, never hollow (owner, 2026-11-08)."""
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, l * 0.55, s * 0.25)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


_read_memo: dict[str, tuple[tuple, list[str], frozenset]] = {}


def _read_book(rel: str, full: Path, phrases: dict[str, str]) -> tuple[list[str], frozenset]:
    """(each page's first line, the files whose title phrase the book's text
    holds): one read of the book's text a mtime, since a library's 129 books
    are about 10^8 characters and Explore asks on every open."""
    st = full.stat()
    key = (st.st_mtime_ns, st.st_size, hash(frozenset(phrases.items())))
    hit = _read_memo.get(str(full))
    if hit and hit[0] == key:
        return hit[1], hit[2]
    pages = _page_texts(rel, full)
    body = " " + " ".join(re.split(r"\W+", " ".join(pages).lower())) + " "
    names = frozenset(dst for dst, phrase in phrases.items() if dst != rel and f" {phrase} " in body)
    out = ([_first_line(p) for p in pages], names)
    _read_memo[str(full)] = (key, *out)
    return out


def _book_vectors(core, docs: list[str], first_lines: dict[str, list[str]]) -> dict[str, list[float]]:
    """{PDF: the mean of its page vectors}, apparatus pages and the first and
    last _EDGE_SHARE of its pages left out: page vectors match a page's kind,
    and the closest pages of two unrelated books were their bibliographies
    (2026-11-08). shortcut: a page is apparatus by its first line alone;
    read its share of citations when a library's edges still pair indexes."""
    import numpy as np
    from silica_core import embeddings

    loaded = embeddings.load()
    if loaded is None:
        return {}
    meta, rows = loaded
    codes = np.frombuffer(rows.codes, dtype=np.int8).reshape(-1, rows.dim)
    scales = np.asarray(rows.scales, dtype=np.float32)
    out: dict[str, list[float]] = {}
    for rel in docs:
        d = meta["docs"].get(rel)
        if not d:
            continue
        lines = first_lines.get(rel, [])
        n, keep = len(d["offsets"]), []
        edge = max(1, round(n * _EDGE_SHARE))
        for i in range(n):
            if i < edge or i >= n - edge or (i < len(lines) and _APPARATUS_RE.match(lines[i])):
                continue
            keep.append(d["row"] + i)
        if not keep:
            continue
        v = (codes[keep].astype(np.float32) * scales[keep][:, None]).mean(axis=0)
        norm = float(np.linalg.norm(v))
        if norm:
            out[rel] = (v / norm).tolist()
    return out


def file_layer(first_group: int) -> dict:
    """The vault's files as Explore nodes, edges and areas: {nodes, edges,
    areas}, areas as (group, label, size). Files a note names stay out, their
    notes are the nodes; a folder of pictures is a set, and sets join the
    edition they photograph. Cached on the index's stamps, the vectors' file
    and the vault's files, so a vector build landing redraws the edges."""
    from silica.config import CONFIG, hosted
    from silica.kernel import raws as raw_index
    from silica.kernel.recall.graph_export import _community_color, similar_edge
    from silica.sources.convert import DOC_EXTS
    from silica.tools.atomic import _engine

    if hosted() or not CONFIG.vault_path:
        return {"nodes": [], "edges": [], "areas": []}
    vault = Path(CONFIG.vault_path)
    named = raw_index.index(vault)
    states = file_states(vault, named)
    core = _engine()
    stamp = lambda p: p.stat().st_mtime_ns if p.is_file() else 0  # noqa: E731
    index_dir = core._paths.index_dir()
    key = (first_group, stamp(index_dir / "stamps.json"), stamp(index_dir / "embed.json"),
           tuple(sorted(states.items())))
    with _layer_lock:
        hit = _layer_memo.get(str(vault))
        if hit and hit[0] == key:
            return hit[1]

    docs = sorted(r for r in states if Path(r).suffix.lower() in DOC_EXTS
                  and Path(r).suffix.lower() not in _IMAGE_EXTS)
    sets: dict[str, list[str]] = {}
    for r in states:
        if Path(r).suffix.lower() in _IMAGE_EXTS:
            sets.setdefault(r.rsplit("/", 1)[0] if "/" in r else "", []).append(r)
    by_edition = editions([s for s in sets if s])
    # a set is a folder: placed as a file inside it, so it takes the area its
    # pictures would
    area = _area_of(docs + [s + "/x" for s in sets])
    names = sorted(set(area.values()), key=str.lower)
    group = {a: first_group + i for i, a in enumerate(names)}

    def colour(g: int, read: bool) -> dict:
        c, paper = _community_color(g), _community_color(g, on_paper=True)
        if not read:
            c, paper = _muted(c), _muted(paper)
        return {"background": c, "border": c, "background_paper": paper,
                "highlight": {"background": c, "border": "#F4F5F7"}}

    nodes: list[dict] = []
    pages_of: dict[str, int] = {}
    for rel in docs:
        info = chapters(rel) if rel.lower().endswith(".pdf") else {}
        pages = info.get("pages", 0)
        pages_of[rel] = pages
        g = group[area[rel]]
        nodes.append({"id": f"file:{rel}", "type": "file", "path": rel, "label": Path(rel).stem,
                      "group": g, "color": colour(g, states[rel] == "read"), "state": states[rel],
                      "act": file_act(rel) if states[rel] == "unread" else "", "pages": pages, "size": round(12 + 6 * math.log2(1 + pages / 40), 1)})
    for label, members in by_edition.items():
        photos = sorted(p for s in members for p in sets[s])
        counts: dict[str, int] = {}
        for s in members:
            counts[area[s + "/x"]] = counts.get(area[s + "/x"], 0) + len(sets[s])
        g = group[max(counts, key=lambda a: counts[a])]
        nodes.append({"id": f"edition:{label}", "type": "edition", "path": photos[0], "label": label,
                      "group": g, "color": colour(g, False), "state": "unread", "act": "ocr", "sets": members,
                      "pages": len(photos), "size": round(12 + 6 * math.log2(1 + len(photos) / 40), 1)})

    edges: list[dict] = []
    read_pdfs = [r for r in docs if states[r] == "read" and r.lower().endswith(".pdf")]
    phrases = {r: p for r in docs if (p := _title_phrase(Path(r).stem))}
    first_lines, named_by = {}, {}
    for r in read_pdfs:
        first_lines[r], named_by[r] = _read_book(r, vault / r, phrases)
    vectors = _book_vectors(core, read_pdfs, first_lines)
    if len(vectors) > 1:
        import numpy as np

        ids = sorted(vectors)
        m = np.array([vectors[r] for r in ids], dtype=np.float32)
        sim = m @ m.T
        np.fill_diagonal(sim, -1.0)
        seen: set[tuple[str, str]] = set()
        for i, a in enumerate(ids):
            for j in np.argsort(-sim[i])[:KNN]:
                pair = tuple(sorted((a, ids[j])))
                if pair not in seen:
                    seen.add(pair)
                    edges.append(similar_edge(f"fs{len(edges)}", f"file:{pair[0]}", f"file:{pair[1]}",
                                              float(sim[i, j])))
    for src in read_pdfs:
        for dst in sorted(named_by[src]):
            edges.append({"id": f"fc{len(edges)}", "from": f"file:{src}", "to": f"file:{dst}",
                          "type": "CITES", "arrows": {"to": {"enabled": True, "scaleFactor": 0.5}},
                          "width": 1.0, "names": phrases[dst]})
    areas = [(group[a], a, sum(1 for n in nodes if n["group"] == group[a])) for a in names]
    out = {"nodes": nodes, "edges": edges, "areas": areas, "vectors": len(vectors)}
    with _layer_lock:
        _layer_memo[str(vault)] = (key, out)
    return out


# --- studying a file (ADR-0156 point 7) ---------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def write_chapters(rel: str) -> dict:
    """A vault PDF's chapters, written beside it as notes for Memorize to plan
    over: `<folder>/<stem>/<stem>-NN-<title>.md`, one a step of the ladder,
    from the text layer and with no model call, under ADR-0129's keys, so the
    drawer opens each on its PDF at its pages; and `<folder>/<stem>.md`, the
    document's list of its parts, flagged as convert's is so /nucleate and the
    plan skip it as content. One undo run for all of them, under the person's
    own source: a person asked for it. A file studied before is not written
    again: its folder answers. Raises ValueError for a file with no chapters
    to read."""
    import hashlib
    import json

    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.preserve import Loss
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE, get_undo_journal

    vault = Path(CONFIG.vault_path or "")
    full = vault / rel
    info = chapters(rel)
    steps = info.get("steps") or []
    pages = _page_texts(rel, full) if steps else []
    if not steps or not any(p.strip() for p in pages):
        raise ValueError(f"{full.name} has no text to study: OCR it first")
    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
    stem = full.stem
    base = f"{folder}/{stem}" if folder else stem
    parent = f"{base}.md"
    if (vault / parent).is_file():
        return {"folder": base, "parent": parent, "parts": sorted(
            p.relative_to(vault).as_posix() for p in (vault / base).glob("*.md")), "written": 0}
    with open(full, "rb") as fh:
        sha = hashlib.file_digest(fh, "sha256").hexdigest()

    def head(extra: str) -> str:
        # json.dumps quotes for YAML too: a title holding ": " left unquoted
        # empties the whole frontmatter
        return (f"---\nsource_file: {json.dumps(rel, ensure_ascii=False)}\nsource_sha256: {sha}\n"
                f"source_bytes: {full.stat().st_size}\nsource_pages: {info['pages']}\n{extra}---\n\n")

    width = len(str(len(steps)))
    notes: list[tuple[str, str]] = []
    for i, s in enumerate(steps, 1):
        slug = _SLUG_RE.sub("-", s["title"].lower()).strip("-")[:60] or f"pages-{s['from']}"
        body = "\n\n".join(p.strip() for p in pages[s["from"] - 1:s["to"]] if p.strip())
        also = f"Also in these pages: {'; '.join(s['parts'])}.\n\n" if s.get("parts") else ""
        notes.append((f"{base}/{stem}-{i:0{width}d}-{slug}.md",
                      head(f"segment_title: {json.dumps(s['title'], ensure_ascii=False)}\n"
                           f"source_page: {s['from']}\nsource_page_end: {s['to']}\n")
                      + f"# {s['title']}\n\n{also}{body}\n"))
    rows = "\n".join(f"- [[{Path(p).stem}]] (pages {s['from']} to {s['to']})" for (p, _t), s in zip(notes, steps))
    notes.append((parent, head("document: true\n")
                   + f"The {len(steps)} chapters of {full.name}, in order.\n\n{rows}\n"))

    journal = get_undo_journal()
    run_id = journal.start_run(source=USER_WRITE_SOURCE, vault=str(vault))
    for path, text in notes:
        journal.record(run_id, InverseOp(kind=InverseOpKind.trash_created, path=path), post_hash=None)
        try:
            DRIVER.create(path, text, loss=Loss.PERSON)
        except Exception:
            # the notes already written stay, each with its way back in this
            # run; the one that failed has nothing to take back
            journal.drop_inverse(run_id, path)
            raise
    return {"folder": base, "parent": parent, "parts": [p for p, _t in notes[:-1]], "written": len(notes),
            "run": run_id}
