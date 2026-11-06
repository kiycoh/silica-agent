# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A PDF's tables as tables (ADR-0087 amendment 2026-11-06), local only.

PDFium's text keeps a table's words and loses its rows and columns. RAGFlow's
`layout.onnx` (the [tables] extra) boxes each table on a page, and the chat
model rewrites the characters inside a box as a table: its own text, so any
chat model reads it, and a page's other lines stay PDFium's. On the server this
never runs: the tenant's shared cores do not carry a detector (the amendment of
2026-11-01), and there PDFium itself only runs in `pdf_extract`'s child.

Three phases, so that PDFium is used on its lock alone and the lock is free
while the slow parts run: the page is drawn and its characters read under
`pdf_extract._PDFIUM`, the detector and the model run without it, and the lines
are put back together from what was collected.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import numpy as np

from silica.sources import pdf_extract

logger = logging.getLogger(__name__)

# The revision is pinned and the hash checked: these weights run on the
# owner's machine, and a moved branch must not change what a note says.
_WEIGHTS_URL = ("https://huggingface.co/InfiniFlow/deepdoc/resolve/"
                "9c7aa2c730d7a242d7f04cf6109a6a239aefc717/layout.onnx")
_WEIGHTS_SHA256 = "de401c03ee30b1c120416dc06f0705237f0c36d3cdb692c9bfefe8a8f98a4b70"

_SIZE = 1024
_TABLE_CLASS = 5
# DocLayout-YOLO's own README value; DeepDoc's 0.08 feeds a tagger, not a model
# that is paid to read what the box holds.
_MIN_SCORE = 0.2
_MAX_IOU = 0.45
# A reading of a page gets the same (`convert._PAGE_MAX_TOKENS`); a table past
# it stops at `finish_reason == "length"` and keeps its PDFium text.
_MAX_TOKENS = 2500
# Which pages reach the detector, measured 2026-11-06 on the bench and on 866 real
# pages: 30 to 45% of pages with text, 98% of the 157 real pages with a table box
# kept. End to end on the dev sets it costs -1.8 on tables [-6.8, +2.0], nothing
# outside them, for a third less time. Three rules and not one: a page's header or footer draws one,
# and a thin vertical rule is rarer, so two of those say more. A page past
# _MAX_OBJECTS is a plot, whose paths stop saying anything about tables.
_MIN_ACROSS = 3
_MIN_DOWN = 2
_THIN = 2.5
_MAX_OBJECTS = 4000
_CAPTION = re.compile(r"(?:table|tabella|tab\.|tabla|tableau|tabelle)\s*[0-9ivxlc]+\b", re.I)

# No "plain text" here: in the bench's crop prompt those words made Qwen answer
# tab-separated, olmOCR's tables 80.9 to 72.0. `_accepted` takes those that still do.
_PROMPT = (
    "Below is the text of one table from one page of a document, one printed line per line, "
    "with spaces keeping every word at its printed position. Write the table alone as a "
    "markdown table in pipe syntax (| cell | cell |, then | --- | --- | under the header): the "
    "header row first, one row per line, every cell verbatim as printed and in its own "
    "language, without LaTeX or dollar signs, numbers exactly as printed, an empty cell left "
    "empty, and a cell printed on several lines joined into one cell. Leave out any caption, "
    "title or note printed above or below the table. If a cell spans several rows or columns, "
    "write the whole table as an HTML <table> with rowspan and colspan instead. Reply with the "
    "table alone: no preamble, no code fence. If this text is not a table (running text, a "
    "list), reply with the two words NOT TABLE and nothing else.")


def enabled() -> bool:
    """`SILICA_PDF_TABLES=1`, spelled "1" alone and read per call as
    `pdf_extract.every_page()` is. A setting and not onnxruntime being importable:
    [stt] brings onnxruntime through faster-whisper, and a dictation install must
    not start paying a model per table, or fetching 76 MB, on its next PDF."""
    return os.environ.get("SILICA_PDF_TABLES", "").strip() == "1"


def stitched(src: Path, data: dict) -> list:
    """`data["pages"]` with the tables of its pages rewritten, for `_pdf_markdown`:
    the same list when nothing changed. `data` is `pdf_extract.read`'s cached
    dict, so a page rewritten is a new list beside it and never an edit of it.

    A no-op on the server, unless `enabled()`, without onnxruntime, and without
    the weights. Only
    a page whose PDFium text stands is looked at: one without text has no
    characters to rewrite, and a formula page read whole keeps only its
    headings. A table's characters leave the text when that table has a
    reading, and it goes where the first of them was."""
    from silica.config import hosted

    pages = data["pages"]
    if hosted() or not enabled():
        return pages
    skip = {*data["no_text"], *data["math_read"]}
    candidates = [i for i in range(len(pages)) if i not in skip]
    session = _session() if candidates else None
    if session is None:
        return pages
    from silica.sources import convert

    # Here, on the calling thread: the job's ContextVar is not set in a pool's.
    report = convert.PAGE_PROGRESS.get() or (lambda done, total: None)
    plans, grids = _plan(src, pages, candidates, session, report)
    readings = _read_all(grids, report) if grids else {}
    if not readings:
        return pages
    out = list(pages)
    for i, lines, owner in plans:
        out[i] = _assembled(pages[i], lines, owner, readings)
    return out


# --- the detector -------------------------------------------------------------

def _weights() -> Path | None:
    """The weights' path, fetched on first use; None when they cannot be had."""
    # Read per call: a test or a tenant (ADR-0071) moves it.
    from silica.kernel.recall import paths

    target = paths._SILICA_HOME / "models" / "layout.onnx"
    if target.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Beside the target, so the rename cannot cross a filesystem, and of its
        # own, so two conversions fetching at once do not write into one file.
        with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
            part = Path(tmp) / target.name
            digest = hashlib.sha256()
            # HuggingFace answers `resolve` with a redirect to its CDN.
            with part.open("wb") as out, httpx.stream("GET", _WEIGHTS_URL, follow_redirects=True,
                                                      timeout=60.0) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != _WEIGHTS_SHA256:
                logger.warning("pdf tables off: layout.onnx did not match its hash, discarded")
                return None
            part.replace(target)
    except (httpx.HTTPError, OSError) as exc:
        # Offline, or no room on the disk: the file converts as it always did,
        # with PDFium's text, and the next conversion tries again.
        logger.warning("pdf tables off: layout.onnx not fetched", exc_info=exc)
        return None
    return target


def _session():
    """The detector; None without onnxruntime or usable weights. Per conversion
    and not kept: 0.16 s to load against 1.06 s a page (2026-11-06), and kept it
    would hold its memory in a server that may not see another PDF."""
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    path = _weights()
    if path is None:
        return None
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 2, 1
    options.enable_cpu_mem_arena = False
    try:
        return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
    except Exception as exc:
        # A file damaged on the disk since it passed its hash, or an onnxruntime
        # that refuses it: tables are an improvement, the conversion stands
        # without them.
        logger.warning("pdf tables off: layout.onnx did not load", exc_info=exc)
        return None


def _nms(rows: list[list[float]]) -> list[list[float]]:
    """`rows` (x0, y0, x1, y1, score, ...) without those a better one overlaps."""
    keep: list[list[float]] = []
    for r in sorted(rows, key=lambda r: -r[4]):
        for k in keep:
            iw = min(r[2], k[2]) - max(r[0], k[0])
            ih = min(r[3], k[3]) - max(r[1], k[1])
            inter = max(iw, 0) * max(ih, 0)
            union = (r[2] - r[0]) * (r[3] - r[1]) + (k[2] - k[0]) * (k[3] - k[1]) - inter
            if union > 0 and inter / union > _MAX_IOU:
                break
        else:
            keep.append(r)
    return keep


def _signs_of_table(doc, i: int, prod: list) -> bool:
    """Whether a page is worth a detection: a caption in its lines, or thin rules
    drawn on it. Milliseconds against a second a page for the detector, and the
    pages it drops read as they always did. A borderless table with no caption is
    the case it gives up: 8 of 103 boxed pages on the bench."""
    import pypdfium2.raw as raw

    if any(_CAPTION.match(text) for text, _ in prod):
        return True
    page = doc[i]
    try:
        width, height = page.get_size()
        across = down = 0
        for seen, obj in enumerate(page.get_objects(max_depth=2)):
            if seen >= _MAX_OBJECTS:
                break
            if obj.type != raw.FPDF_PAGEOBJ_PATH:
                continue
            left, bottom, right, top = obj.get_bounds()
            across += top - bottom <= _THIN and right - left >= 0.04 * width
            down += right - left <= _THIN and top - bottom >= 0.008 * height
            if across >= _MIN_ACROSS or down >= _MIN_DOWN:
                return True
        return False
    finally:
        page.close()


def _draw(doc, i: int) -> tuple:
    """Page `i` on the detector's canvas, and what maps its pixels back. Under
    the lock: drawn straight at the letterbox size, so no resize and no Pillow.
    The bitmap is closed here because its finalizer would run off the lock, and
    the canvas is a copy because `to_numpy` is a view of what that frees."""
    page = doc[i]
    try:
        width, height = page.get_size()
        scale = _SIZE / max(width, height)
        bitmap = page.render(scale=scale, rev_byteorder=True)
        try:
            image = bitmap.to_numpy()[:, :, :3]
            ih, iw = image.shape[:2]
            dy, dx = (_SIZE - ih) // 2, (_SIZE - iw) // 2
            canvas = np.full((_SIZE, _SIZE, 3), 114, np.uint8)
            canvas[dy:dy + ih, dx:dx + iw] = image
        finally:
            bitmap.close()
    finally:
        page.close()
    return canvas, scale, dx, dy, height


def _detect(session, canvas, scale: float, dx: int, dy: int, height: float) -> list[tuple]:
    """The tables on a canvas as (left, bottom, right, top) in PDF points, the
    bottom-left origin PDFium's character boxes use."""
    x = canvas.transpose(2, 0, 1)[None].astype(np.float32) / 255
    rows = session.run(None, {"images": x})[0][0]
    tables = [list(map(float, r)) for r in rows if r[4] >= _MIN_SCORE and int(r[5]) == _TABLE_CLASS]
    return [((r[0] - dx) / scale, height - (r[3] - dy) / scale,
             (r[2] - dx) / scale, height - (r[1] - dy) / scale) for r in _nms(tables)]


# --- the characters -----------------------------------------------------------

def _boxes(tp, n: int, raw) -> list[tuple | None]:
    rect = raw.FS_RECTF()
    out: list[tuple | None] = []
    for i in range(n):
        if raw.FPDFText_GetLooseCharBox(tp, i, rect) and rect.right > rect.left:
            out.append((rect.left, rect.bottom, rect.right, rect.top))
        else:
            out.append(None)
    return out


def _lines(text: str) -> list[list[tuple[str, int]]]:
    """`pdf_extract._page_lines`' split of a page's text, each line as stripped
    [(character, index)]. Only the split: a page it drops hidden text from, or
    whose letters `_unalpha` restored, comes out different and is left alone."""
    out = []
    pos = 0
    for line_raw in text.split("\n"):
        line = line_raw.rstrip("\r")
        chars = [(ch, pos + k) for k, ch in enumerate(line) if not pdf_extract._BAD_RE.match(ch)]
        while chars and chars[0][0].isspace():
            chars.pop(0)
        while chars and chars[-1][0].isspace():
            chars.pop()
        if chars:
            out.append(chars)
        pos += len(line_raw) + 1
    return out


def _chars(doc, i: int, prod: list) -> tuple | None:
    """Page `i`'s rebuilt lines and character boxes, under the lock; None when
    those lines are not the page's: an index would then point at another word."""
    import pypdfium2.raw as raw

    page = doc[i]
    tp = page.get_textpage()
    try:
        lines = _lines(tp.get_text_range())
        if ["".join(ch for ch, _ in line) for line in lines] != [t for t, _ in prod]:
            return None
        return lines, _boxes(tp, tp.count_chars(), raw)
    finally:
        tp.close()
        page.close()


def _owner(boxes: list[tuple | None], tables: list[tuple]) -> dict[int, int]:
    """Character index to the table holding its centre; the likelier box wins
    a character two boxes hold, `tables` being in the detector's order."""
    got: dict[int, int] = {}
    for k, (left, bottom, right, top) in enumerate(tables):
        for i, bx in enumerate(boxes):
            if bx and i not in got and left <= (bx[0] + bx[2]) / 2 <= right \
                    and bottom <= (bx[1] + bx[3]) / 2 <= top:
                got[i] = k
    return got


def _layout_text(cells: list[tuple[str, tuple]]) -> str:
    """A table's characters as `pdftotext -layout` prints them: rows by height, each
    character at the column its x gives, so the model sees the columns without an image."""
    cells = [c for c in cells if c[0].strip() and c[1]]
    if not cells:
        return ""
    h = sorted(b[3] - b[1] for _, b in cells)[len(cells) // 2] or 1.0
    w = sorted(b[2] - b[0] for _, b in cells)[len(cells) // 2] or 1.0
    left = min(b[0] for _, b in cells)
    rows: list[list] = []
    for ch, b in sorted(cells, key=lambda c: -(c[1][1] + c[1][3]) / 2):
        y = (b[1] + b[3]) / 2
        if rows and abs(rows[-1][0] - y) <= 0.5 * h:
            rows[-1][1].append((ch, b))
        else:
            rows.append([y, [(ch, b)]])
    out = []
    for _, row in rows:
        line, last = "", None
        for ch, b in sorted(row, key=lambda c: c[1][0]):
            gap = b[0] - last if last is not None else 1e9
            # A column only at a real gap: a proportional font's wide letter puts the
            # next one past its median-width column, and padding there broke words
            # ("Occu rrence", 2026-11-06), which the scorer's exact cells then failed.
            if gap > 1.0 * h:
                line = line.ljust(max(int((b[0] - left) / w), len(line) + 2))
            elif gap > 0.12 * h:
                line += " "
            line += ch
            last = max(last or b[2], b[2])
        out.append(line.rstrip())
    return "\n".join(out)


def _collect(doc, i: int, prod: list, tables: list[tuple], grids: list[str]) -> tuple | None:
    """Page `i`'s (index, lines, character to table), its tables' text added to
    `grids`; a table's number is its place there. None with nothing to read."""
    with pdf_extract._PDFIUM:
        found = _chars(doc, i, prod)
    if found is None:
        return None
    lines, boxes = found
    owner = _owner(boxes, tables)
    cells: dict[int, list] = {}
    # From the lines and not the page's text: a character production dropped
    # (`_BAD_RE`) must not reach the model.
    for line in lines:
        for ch, idx in line:
            if idx in owner:
                cells.setdefault(owner[idx], []).append((ch, boxes[idx]))
    number: dict[int, int] = {}
    for k, table in cells.items():
        grid = _layout_text(table)
        if grid:
            number[k] = len(grids)
            grids.append(grid)
    owner = {idx: number[k] for idx, k in owner.items() if k in number}
    return (i, lines, owner) if owner else None


def _plan(src: Path, pages: list, candidates: list[int], session,
          report: Callable[[int, int], None]) -> tuple[list[tuple], list[str]]:
    """Phase one: what `_collect` finds on each page, and the tables' text.

    The lock is taken per page and not for the file: a detection is 1.06 s a
    page on two cores (2026-11-06), and held across a book it would stop every
    chat turn that reads a PDF for minutes. An open document nobody is calling
    is not a thread inside PDFium, which is what the lock is for."""
    import pypdfium2 as pdfium

    plans: list[tuple] = []
    grids: list[str] = []
    try:
        with pdf_extract._PDFIUM:
            doc = pdfium.PdfDocument(os.fspath(src))
    except Exception as exc:
        # The file went while `_read_pages` spent minutes on it (the server
        # deletes an import that failed, a person moves it): the pages it read
        # are paid for, and PDFium's text is what this step found them with.
        logger.warning("pdf tables not looked for, the file did not open again", exc_info=exc)
        return plans, grids
    try:
        report(0, len(candidates))
        for done, i in enumerate(candidates, 1):
            try:
                with pdf_extract._PDFIUM:
                    drawn = _draw(doc, i) if _signs_of_table(doc, i, pages[i]) else None
                tables = _detect(session, *drawn) if drawn else []
                plan = _collect(doc, i, pages[i], tables, grids) if tables else None
                if plan:
                    plans.append(plan)
            except Exception as exc:
                # A page that will not draw (a MediaBox of zero size) or a run
                # that fails on it: that page keeps PDFium's text, which is what
                # it had before this step, and the next page is another page.
                logger.warning("pdf page %d: tables not looked for", i + 1, exc_info=exc)
            report(done, len(candidates))
    finally:
        with pdf_extract._PDFIUM:
            doc.close()
    return plans, grids


# --- the model ----------------------------------------------------------------

def _accepted(text: str | None, finish_reason: str | None, grid: str) -> str | None:
    """The table a reply holds, or None to keep PDFium's text: a reply cut at the
    token limit, the model's veto, or a digit the table does not print."""
    if finish_reason == "length":
        return None
    from silica.sources import convert

    md = convert._FENCE_RE.sub("", (text or "").strip()).strip()
    lines = md.splitlines()
    rows: list = [r for r in lines if r.count("|") >= 3]
    if len(rows) < 2 and "<table" not in md and sum("\t" in r for r in lines) >= 2:
        # Tab-separated rows: the table the prompt asked for, in another dress.
        rows = [r.split("\t") for r in lines if r.strip()]
        width = max(len(c) for c in rows)
        rows = [c + [""] * (width - len(c)) for c in rows]
        md = "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
                       + ["| " + " | ".join(c) + " |" for c in rows[1:]])
    # A box the layout model took for a table (a column of prose, a list) is
    # refused in words, or answered with something that has no rows.
    if "NOT TABLE" in md[:40] or (len(rows) < 2 and "<table" not in md):
        return None
    # Silica keeps the source's words, and a number is where a slip costs most.
    # Tags out first: the rowspan="2" the prompt asks for prints nowhere. Whole
    # runs and not substrings: "20" read off a cell printing "2024" is the slip.
    printed = re.sub(r"<[^>]*>", " ", md)
    if set(re.findall(r"\d+", printed)) - set(re.findall(r"\d+", grid)):
        return None
    return md


def _read_one(grid: str) -> str | None:
    from silica.agent.providers import get_provider
    from silica.config import CONFIG

    # The router and not the vision role: this is text, so any chat model reads
    # it. temperature 0 and reasoning=False for `vision.ask_chat_model`'s reasons.
    resp = get_provider(CONFIG).call_llm(
        messages=[{"role": "user", "content": f"{_PROMPT}\n\n{grid}"}],
        max_tokens=_MAX_TOKENS, temperature=0.0, reasoning=False)
    return _accepted(resp.text, resp.finish_reason, grid)


def _read_all(grids: list[str], report: Callable[[int, int], None]) -> dict[int, str]:
    """Phase two: each table's reading by its number, those with one."""
    from concurrent.futures import ThreadPoolExecutor

    from silica.sources import convert

    def guarded(grid: str) -> str | None:
        try:
            return _read_one(grid)
        except Exception as exc:
            # One table the model will not take (a time out, a content filter)
            # keeps its PDFium text, and the tables already read are paid for.
            logger.warning("pdf table not read", exc_info=exc)
            return None

    report(0, len(grids))
    try:
        results = [_read_one(grids[0])]
    except Exception as exc:
        # The first call alone decides whether the others go out, as in
        # `convert._read_pages`: no model at all fails every table the same
        # way, and each refusal is a round trip that buys nothing.
        logger.info("pdf tables not read, the chat model refused the first: %s", type(exc).__name__)
        return {}
    report(1, len(grids))
    with ThreadPoolExecutor(max_workers=convert._PAGE_READERS, thread_name_prefix="pdf-table") as pool:
        # In order, on this thread: see `stitched`.
        for md in pool.map(guarded, grids[1:]):
            results.append(md)
            report(len(results), len(grids))
    return {k: md for k, md in enumerate(results) if md}


# --- the page -----------------------------------------------------------------

def _assembled(prod: list, lines: list, owner: dict[int, int], readings: dict[int, str]) -> list:
    """Phase three, no PDFium: the page's lines with each read table where its
    first character was. A line no reading touches stays as production has it."""
    placed: set[int] = set()
    out = []
    for entry, line in zip(prod, lines):
        if not any(owner.get(idx) in readings for _, idx in line):
            out.append(entry)
            continue
        size = entry[1]
        parts = []
        for ch, idx in line:
            t = owner.get(idx)
            if t is None or t not in readings:
                parts.append(ch)
            elif t not in placed:
                placed.add(t)
                # Blank lines around it, kept by `strip(" ")` below: the line
                # after a table with none between is read as one more row.
                parts.append(f"\n{readings[t]}\n")
                # `_heading_level` would take a short table at a title's size
                # for a title, and `_body_font_size` skips a size of 0.
                size = 0.0
        text = re.sub(r"[ \t]+", " ", "".join(parts)).strip(" ")
        if text.strip():
            out.append([text, size])
    return out
