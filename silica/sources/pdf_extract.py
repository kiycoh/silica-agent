# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A PDF's text layer as plain data: pages of (line, font size), the outline,
the metadata. The markdown is the parent's (`convert._pdf_markdown`); this is
the one place a hostile file is opened.

On the server the read runs in a child with limits (ADR-0087 point 2,
ADR-0098 H1): the bomb that aborts a process under `RLIMIT_AS` has no Python
exception to catch, so the limit needs a process of its own. Locally it runs
in-process, as it always has.

The child half (`_child`, run as this file's `__main__`) imports pypdfium2 and
the standard library and nothing from `silica`: it starts with `python -I -B`
on this file's path, so neither the package nor the working directory is on
its import path.

A DOCX on the server goes through the same child with the same limits
(`docx_html`, ADR-0087 point 5's plan, built by ADR-0120): mammoth reads a
member whole with no cap of its own, and its memory grows 25 MB a MB of XML.

A page without text (ADR-0087 point 3) is rendered here too, grayscale, and
handed back as PNG for the parent to send to the chat model (the amendment of
2026-10-13), and so is a page drawing a math font, whose formulas the text
layer loses (the amendment of 2026-10-14); the reading itself is the
parent's (`convert._read_pages`), because the child has no network.
"""
from __future__ import annotations

import json
import os
import re
import sys

# ADR-0087 point 2's table, measured there: the library's largest PDF is 20 MB
# and its longest paper 135 pages; the 803-page book takes 3.8 s, 364 MB and
# 3.1 million characters; the 1 GiB Flate bomb needs 2.07 GB and dies.
MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 1000
AS_BYTES = 1024 ** 3
CPU_SECONDS = 60
WALL_SECONDS = 120
MAX_CHARS = 8_000_000
# The JSON around 8 million characters: per-line arrays and font sizes, and
# \uXXXX escapes for non-ASCII text, which can triple a line.
_TEXT_OUT_BYTES = 40 * 1024 * 1024
_ERR_BYTES = 64 * 1024

# ADR-0087 point 3: a page under 20 non-space characters has no text layer.
# Over the library's 5,915 pages, 3 sit under it (two figures, one blank) and
# the next real page has 45.
MIN_PAGE_CHARS = 20
# Point 4, measured on 255 papers: the median page's share of bad characters
# fired on the 2 true garbage documents, and the nearest clean one sat at
# 0.083. The function-word share only warns until it is measured on 20 UNIPA
# lecture PDFs, because terse Italian slides may sit near it.
BAD_SHARE = 0.10
FUNCTION_WORD_SHARE = 0.05
# U+FFFD, C0 but newline and tab, C1, and the three private use areas: what
# glyph codes with no Unicode map come out as. Never a letter a student wrote.
_BAD_RE = re.compile("[\x00-\x08\x0b-\x1f\x80-\x9f\ufffd\ue000-\uf8ff"
                     "\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
# English and Italian closed-class words: running prose of either language is
# a quarter to a half of these, glyph-code garbage none.
_FUNCTION_WORDS = frozenset("""
    the of and to a in is that for it as with was on be by this are or at from an not which
    but have has had were been their its they we our can these such than also into more
    il lo la i gli le di da con su per tra fra e che non un una uno del della dei delle degli
    al alla ai alle nel nella nei nelle sul sulla sono come anche si ma o ed se questo questa
""".split())

# The amendment's render: grayscale, long side at most 1600 px, about 1,500
# image tokens a page in on glm-5.3-flash by its estimate. A page without text
# past `vision_pages()` is named instead; a formula page past it keeps PDFium's
# text.
VISION_PAGES = 60
LONG_SIDE = 1600
# A page drawing a glyph in one of these fonts has formulas PDFium's text layer
# loses (ADR-0087 amendment of 2026-10-14): Cambria Math is PowerPoint's
# equation editor, Symbol the old one's Greek and operators (it extracts as
# private-use characters, or as ASCII with the layout gone: "( ) ( ) | =+ |
# h W x b"), CMMI to MSAM LaTeX's math fonts. Over the 706 pages of 32 lecture
# decks it fired on 440, and none of the 358 fired by Symbol alone drew only
# bullets. A name may carry a subset prefix ("BCDNEE+CambriaMath").
# ponytail: a deck whose bullets are set in Symbol has every bulleted page
# read, which costs a reading and loses nothing; refine to the glyphs drawn
# (FPDFText_GetFontInfo per character) when a tenant's journal shows a deck
# with more than 80% of its pages read and no formula in the readings.
_MATH_FONT = re.compile(r"Math|Symbol|(?:^|\+)(?:CMMI|CMSY|CMEX|MSBM|MSAM)")
# The child has no network and RLIMIT_FSIZE 0, so pictures come back on stdout
# and the stdout cap has to hold them. 16 MiB of PNG for one file: measured
# 2026-10-13 at 16 gray levels, a born-digital page is 108 KiB median and 141
# KiB at most (the first 3 pages of 12 papers) and one page with noise added
# as a scanner's 152 to 187 KiB, so the default 60 pages need about 11 MiB and
# fit with room. A page that would pass the budget is not sent, and it and the
# rest are named.
_IMAGE_BYTES = 16 * 1024 * 1024
# The text, the pictures as base64 (4 bytes for every 3), and a line of JSON
# framing per picture: 40 + 21.4 MiB, so the bound stays a number.
_OUT_BYTES = _TEXT_OUT_BYTES + _IMAGE_BYTES * 4 // 3 + 1024 * 1024
# 16 gray levels, not 256: a scan's grain is most of an 8-bit PNG (857 KiB to
# 1.3 MiB for that noisy page, against 152 to 187 KiB quantised), and 16
# levels keep every edge of a letter.
_LEVELS = bytes((v >> 4) * 17 for v in range(256))
# A rendered page with no pixel darker than 0xF0 draws nothing worth reading.
_LIGHT = bytes(range(0xF0, 0x100))
# A PDF reader accepts the header anywhere in the first 1,024 bytes (Acrobat's
# tolerance, which files in the wild rely on), so the check looks that far.
HEAD_BYTES = 1024
# Under 1 pt is unreadable on any screen or page: text set that small is there
# for a reader other than the student (ADR-0098 H6).
TINY_POINTS = 1.0
# Text render modes 3 (neither fill nor stroke) and 7 (clip only) draw nothing.
_INVISIBLE_MODES = (3, 7)

TOO_LARGE = "This PDF is too large or malformed to read."
DOC_TOO_LARGE = "This document is too large or malformed to read: export it as PDF and add that."
PASSWORD = "This PDF is password-protected: remove the password and upload it again."
NOT_PDF = "This file is not a PDF: it has no %PDF- header in its first 1,024 bytes."
NO_TEXT = ("This PDF has no selectable text (a scan or images). "
           "Silica reads text only: upload a PDF exported from the original file.")
GARBAGE = ("This PDF's text cannot be read: its fonts do not map to letters. "
           "Upload a PDF exported from the original file.")
# Each refusal's sentence and its ADR-0100 `ingest` outcome. `garbage` is not
# in ADR-0100's list; it is counted on its own because ADR-0087 reopens a
# second reader when garbage refusals pass 2% of uploads.
_REFUSALS = {"password": (PASSWORD, "password"), "malformed": (TOO_LARGE, "malformed"),
             "pages": (TOO_LARGE, "too_large"), "output": (TOO_LARGE, "too_large"),
             "garbage": (GARBAGE, "garbage")}


class PdfUnreadable(ValueError):
    """A PDF that cannot be read, with the sentence the student is shown and
    the `ingest` outcome word ADR-0100 counts it under.

    A ValueError because every caller that stages files already catches one per
    file (`_stage_uploads`); `PdfiumError` is a RuntimeError, which ended a
    whole drop with a 500 (ADR-0098 H4). The outcome is an attribute and not a
    second argument, so `args[0]` stays the sentence the routes show."""

    def __init__(self, message: str, outcome: str = "malformed"):
        super().__init__(message)
        self.outcome = outcome


def _refusal(reason: str) -> PdfUnreadable:
    return PdfUnreadable(*_REFUSALS.get(reason, (TOO_LARGE, "too_large")))


def require_header(path: str | os.PathLike) -> None:
    with open(path, "rb") as fh:
        if b"%PDF-" not in fh.read(HEAD_BYTES):
            raise PdfUnreadable(NOT_PDF)


def vision_pages() -> int:
    """How many pages, without text first and then with formulas, one file may
    have read by the chat model: `SILICA_PDF_VISION_PAGES`, set at deploy, 0
    to turn the reading off.

    Read per call, as `hosted()` reads its flag: the child gets an empty
    environment, so the number goes to it as an argument, and a test or a
    tenant's restart changes it without a reload."""
    raw = os.environ.get("SILICA_PDF_VISION_PAGES", "").strip()
    try:
        return max(0, int(raw)) if raw else VISION_PAGES
    except ValueError:
        return VISION_PAGES


# --- the read, on either side of the process boundary -------------------------

def extract(source, max_pages: int | None = None, max_chars: int | None = None,
            vision_pages: int = 0, emit=None) -> dict:
    """Read `source` (a path or a binary file object) into plain data.

    Hidden text is left out of a page that also has visible text, and counted
    under `hidden` by page index (ADR-0098 H6). A page whose text is all
    hidden is an OCR layer, which Tesseract and ocrmypdf draw in render mode 3
    over the scan, and is kept whole.

    Pages under `MIN_PAGE_CHARS` are listed in `no_text`, pages with text and
    a `_MATH_FONT` glyph in `math`, and the first `vision_pages` of the two,
    in that order, are rendered (`render` lists which): `images` maps
    each to its PNG, or to None when nothing is on it; a page missing from it
    could not be drawn or would pass `_IMAGE_BYTES`. With `emit`, the text
    goes to it the moment it is read and then each page as it is rendered,
    instead of into `images`: the child writes each out as it comes, because a
    render decodes a page's images, which reading text never did, and a bomb
    there must cost the pages after it and not the text before.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(source)
    try:
        data = _text(pdf, max_pages, max_chars)
        # A page without text has nothing else to stand for it; a formula page
        # left out keeps PDFium's text, so it goes after them under the cap.
        data["render"] = (data["no_text"] + data["math"])[:vision_pages]
        if emit is None:
            data["images"] = dict(_render(pdf, data["render"]))
        else:
            emit(data)
            for item in _render(pdf, data["render"]):
                emit(item)
    finally:
        pdf.close()
    return data


def _text(pdf, max_pages: int | None, max_chars: int | None) -> dict:
    import pypdfium2.raw as raw

    n = len(pdf)
    # len() reads the page count without parsing a page: 0.05 s on a
    # 200,000-page tree (ADR-0087), so the cap costs nothing before the work.
    if max_pages is not None and n > max_pages:
        raise _Refused("pages")
    meta = {key: _clean(" ".join((pdf.get_metadata_value(key) or "").split()))
            for key in ("Title", "Author", "CreationDate")}
    toc: list[list] = []
    for bookmark in pdf.get_toc():
        title = _clean(" ".join((bookmark.get_title() or "").split()))
        dest = bookmark.get_dest()
        index = dest.get_index() if dest is not None else None
        if title and index is not None and index >= 0:
            toc.append([bookmark.level, title, index])
    pages: list[list] = []
    hidden: dict[int, int] = {}
    no_text: list[int] = []
    math: list[int] = []
    bad_shares: list[float] = []
    word_shares: list[float] = []
    chars = 0
    for i in range(n):
        page = pdf[i]
        textpage = page.get_textpage()
        try:
            lines, dropped, bad = _page_lines(page, textpage, raw)
            has_math = _page_has_math(page, raw)
        finally:
            textpage.close()
            page.close()
        if dropped:
            hidden[i] = dropped
        chars += sum(len(t) for t, _ in lines)
        if max_chars is not None and chars > max_chars:
            raise _Refused("output")
        pages.append(lines)
        good = sum(len("".join(t.split())) for t, _ in lines)
        if good < MIN_PAGE_CHARS:
            no_text.append(i)
        elif has_math:
            math.append(i)
        if good + bad >= MIN_PAGE_CHARS:
            bad_shares.append(bad / (good + bad))
        if good >= MIN_PAGE_CHARS:
            words = re.findall(r"[^\W\d_]+", " ".join(t for t, _ in lines).lower())
            word_shares.append(sum(w in _FUNCTION_WORDS for w in words) / max(1, len(words)))
    # Judged on the median page, never page by page: tables tripped a page
    # rule on 79 pages of 18 clean documents (ADR-0087 Context).
    bad_median = _median(bad_shares)
    if bad_median > BAD_SHARE:
        raise _Refused("garbage")
    return {"pages": pages, "toc": toc, "meta": meta, "n_pages": n, "hidden": hidden,
            "no_text": no_text, "math": math,
            "function_words": _median(word_shares) if word_shares else None}


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _clean(text: str) -> str:
    return _BAD_RE.sub("", text)


def _render(pdf, indices: list[int]):
    """(index, PNG) for each page of `indices`, (index, None) for one with
    nothing on it, until `_IMAGE_BYTES` would be passed. A page left out is
    one the parent names."""
    import pypdfium2.raw as raw

    spent = 0
    for i in indices:
        page = pdf[i]
        try:
            width, height = page.get_size()
            # render() rounds each side up, so a hair under keeps 1600 at most.
            bitmap = page.render(scale=LONG_SIDE / max(width, height, 1.0) * (1 - 1e-9), grayscale=True)
            try:
                w, h, stride = bitmap.width, bitmap.height, bitmap.stride
                gray = bytes(bitmap.buffer)
            finally:
                bitmap.close()
            # Each row behind PNG's filter byte 0 (none): the per-byte filters
            # compress better and cost a Python loop over 1.8 million pixels.
            rows = b"".join(b"\x00" + gray[y * stride:y * stride + w] for y in range(h))
            white = len(rows.translate(None, _LIGHT)) == h  # only the filter bytes are dark
            # White with a picture on it is a picture PDFium would not draw: a
            # 1 GiB image rendered as nothing in 0.1 s (2026-10-13). Not blank,
            # so it is named rather than passed over in silence.
            blank = white and not any(kind == raw.FPDF_PAGEOBJ_IMAGE for _, kind in _objects(page, raw))
        except Exception:
            # A page that raises while drawn (a MediaBox of zero size is a
            # ValueError, and the bitmap is a ctypes buffer, so the address
            # space limit surfaces as MemoryError): that page goes unsent and
            # is named, and the text read before it stands.
            continue
        finally:
            page.close()
        if white:
            if blank:
                yield i, None
            continue
        png = png_gray(rows.translate(_LEVELS), w, h)
        spent += len(png)
        if spent > _IMAGE_BYTES:
            return
        yield i, png


def png_gray(rows: bytes, width: int, height: int) -> bytes:
    """An 8-bit grayscale PNG of `rows` (each a filter byte, then `width` pixels).

    Over zlib and struct, because Pillow is not a dependency of the hosted
    install and the child imports nothing else (ADR-0087 amendment point 1)."""
    import struct
    import zlib

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


class _Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _page_lines(page, textpage, raw) -> tuple[list[list], int, int]:
    """[text, font size] per non-blank line of a page, how many hidden
    characters were left out of it, and how many bad ones (`_BAD_RE`) were
    stripped, which ADR-0087 point 4 removes always and judges per document.

    One font-size call per line, at the line's first glyph: PDFium's text index
    is one unit per character, so the offset of a line in the page string IS
    its character index (a surrogate pair shifts it by one glyph, which lands on
    a neighbouring character of the same line). The per-character look at
    hidden text runs only on a page whose objects hold some: measured over
    5,915 pages of papers, a check of every character cost 10.6 ms a page and
    the object pre-scan 0.9 ms, flagging 142 pages.
    """
    text = textpage.get_text_range()
    n = textpage.count_chars()
    hide = _hidden_chars(textpage, n, raw) if _page_has_hidden(page, raw) else None
    if hide is not None:
        seen = sum(1 for i in range(min(n, len(text))) if not text[i].isspace() and not hide[i])
        if not seen:
            hide = None  # all hidden: an OCR layer, kept whole
    lines: list[list] = []
    dropped = 0
    bad = 0
    pos = 0
    for line_raw in text.split("\n"):
        line = line_raw.rstrip("\r")
        if hide is not None:
            kept = []
            for k, ch in enumerate(line):
                if pos + k < n and hide[pos + k]:
                    dropped += not ch.isspace()
                else:
                    kept.append(ch)
            shown = "".join(kept)
        else:
            shown = line
        shown, n_bad = _BAD_RE.subn("", shown)
        bad += n_bad
        stripped = shown.strip()
        if stripped:
            lead = len(line) - len(line.lstrip())
            first = pos + lead
            if hide is not None:
                first = next((pos + k for k in range(lead, len(line))
                              if pos + k < n and not hide[pos + k] and not line[k].isspace()), first)
            size = float(raw.FPDFText_GetFontSize(textpage, first)) if first < n else 0.0
            lines.append([stripped, size])
        pos += len(line_raw) + 1
    return lines, dropped, bad


def _object_hidden(obj, raw, size, matrix) -> bool:
    """A text object that may hide text: it draws nothing, or its font size
    times its matrix (which carries the page's `cm`) is under `TINY_POINTS`,
    so `12 Tf` under a 0.04 matrix counts like `0.5 Tf`. Only a trigger for
    `_hidden_chars`, which decides per character.
    ponytail: a form XObject's own matrix is not multiplied in, so text shrunk
    by its form passes; the upgrade is carrying the product down the walk,
    due when a probe or an upload shows it.
    """
    import math

    if raw.FPDFTextObj_GetTextRenderMode(obj) in _INVISIBLE_MODES:
        return True
    if not raw.FPDFTextObj_GetFontSize(obj, size) or not raw.FPDFPageObj_GetMatrix(obj, matrix):
        return False
    scale = math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c))
    return size.value * scale < TINY_POINTS


def _page_has_hidden(page, raw) -> bool:
    import ctypes

    size, matrix = ctypes.c_float(), raw.FS_MATRIX()
    return any(kind == raw.FPDF_PAGEOBJ_TEXT and _object_hidden(obj, raw, size, matrix)
               for obj, kind in _objects(page, raw))


def _page_has_math(page, raw) -> bool:
    """A text object on the page draws in a `_MATH_FONT`. Per object and not
    per character: 1.5 ms a page over 706 lecture pages."""
    import ctypes

    name = ctypes.create_string_buffer(128)
    for obj, kind in _objects(page, raw):
        if kind != raw.FPDF_PAGEOBJ_TEXT:
            continue
        font = raw.FPDFTextObj_GetFont(obj)
        size = raw.FPDFFont_GetBaseFontName(font, name, len(name)) if font else 0
        # Past the buffer PDFium leaves it as it was, the previous object's name.
        if 0 < size <= len(name) and _MATH_FONT.search(name.value.decode("latin-1")):
            return True
    return False


def _objects(page, raw):
    """(object, type) for everything a page draws, inside form XObjects too."""
    stack = [(raw.FPDFPage_CountObjects(page), lambda j, p=page: raw.FPDFPage_GetObject(p, j))]
    while stack:
        count, get = stack.pop()
        for j in range(count):
            obj = get(j)
            kind = raw.FPDFPageObj_GetType(obj)
            yield obj, kind
            if kind == raw.FPDF_PAGEOBJ_FORM:
                stack.append((raw.FPDFFormObj_CountObjects(obj),
                              lambda j, f=obj: raw.FPDFFormObj_GetObject(f, j)))


def _hidden_chars(textpage, n: int, raw) -> list[bool]:
    """Per character: drawn invisible, or drawn under `TINY_POINTS` tall.

    The height is the glyph's loose box on the page, which carries every
    matrix down to the page. The object's font size times its matrix, which
    the pre-scan uses, is a trigger and not a verdict: a Type 3 font sets
    `1 Tf` and scales in its own matrix, and on one paper that read 5,000
    characters a page of normal-sized text as tiny; their boxes are 15.6 pt.
    """
    import ctypes

    rect = raw.FS_RECTF()
    invisible: dict[int, bool] = {}
    out = []
    for i in range(n):
        obj = raw.FPDFText_GetTextObject(textpage, i)
        key = ctypes.cast(obj, ctypes.c_void_p).value if obj else None
        if key is None:
            out.append(False)  # a generated space or line break: no object behind it
            continue
        if key not in invisible:
            invisible[key] = raw.FPDFTextObj_GetTextRenderMode(obj) in _INVISIBLE_MODES
        if invisible[key]:
            out.append(True)
            continue
        out.append(bool(raw.FPDFText_GetLooseCharBox(textpage, i, rect))
                   and rect.top - rect.bottom < TINY_POINTS)
    return out


# --- the parent side ----------------------------------------------------------

_CACHE: dict[tuple, dict] = {}
_NETWORK_NOTE_LOGGED = False


def read(path: str | os.PathLike) -> dict:
    """`extract(path)`, in a limited child on the server and in-process locally.

    Cached by path, size, mtime and the vision cap: one conversion asks for the
    text, the date and the citation, and before this each of those opened the
    hostile file again (ADR-0087 Context, three opens)."""
    import logging

    from silica.config import hosted

    key = _key(path)
    if key in _CACHE:
        return _CACHE[key]
    # Before PDFium sees it: a renamed HTML file reached it as a PdfiumError,
    # which ended a whole drop with a 500 (ADR-0098 H4).
    require_header(path)
    if hosted():
        data = _read_in_child(path, key[-1])
    else:
        try:
            data = extract(os.fspath(path), vision_pages=key[-1])
        except _Refused as exc:
            raise _refusal(exc.reason) from None
        except Exception as exc:
            # pypdfium2 raises PdfiumError (a RuntimeError) for every load
            # failure, and names the password case only in its message.
            if type(exc).__name__ != "PdfiumError":
                raise
            raise _refusal("password" if "password" in str(exc).lower() else "malformed") from None
    words = data.get("function_words")
    if words is not None and words < FUNCTION_WORD_SHARE:
        # Point 4's second rule warns and keeps the text: terse Italian slides
        # may sit near 0.05, and no lecture PDF has been measured yet.
        logging.getLogger(__name__).warning(
            "pdf: the median page is %.3f function words, under %.2f; kept", words, FUNCTION_WORD_SHARE)
    if len(_CACHE) >= 8:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = data
    return data


def metadata(path: str | os.PathLike) -> dict:
    """Title, Author and CreationDate, without a full read where one is cheap.

    The provider ran first when pdfium converted the file, so the cache answers.
    With mineru as the provider locally, a full text read here would cost a
    book's worth of work for three strings, so the metadata alone is opened.
    On the server every open goes through the child, and so through `read`."""
    from silica.config import hosted

    key = _key(path)
    if key in _CACHE:
        return _CACHE[key]["meta"]
    if hosted():
        return read(path)["meta"]
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(os.fspath(path))
    try:
        return {k: _clean(" ".join((pdf.get_metadata_value(k) or "").split()))
                for k in ("Title", "Author", "CreationDate")}
    finally:
        pdf.close()


def cached(path: str | os.PathLike) -> dict | None:
    """The last read of `path`, for a caller that reports on it after converting.

    After a conversion it also holds what became of the pages without text
    (`read` and `unread`) and of the formula pages (`math_read`), set by
    `convert._read_pages`."""
    return _CACHE.get(_key(path))


def report(path: str | os.PathLike) -> tuple[dict, str]:
    """ADR-0100's page fields for the last conversion of `path`, and the
    sentence that names its pages left unread ("" when none were).

    Nulls for a file that was not read as a PDF here: counting it a second
    time in the GUI process would double the untrusted work."""
    try:
        seen = cached(path)
    except OSError:
        # The file is gone (the server deletes a PDF once its note exists or
        # it failed): there is nothing left to count, and nulls say so.
        seen = None
    if not seen:
        return {"pages": None, "pages_without_text": None, "pages_read": None}, ""
    return ({"pages": seen["n_pages"], "pages_without_text": len(seen["no_text"]),
             "pages_read": len(seen.get("read", ()))},
            unread_sentence([i + 1 for i in seen.get("unread", ())]))


def unread_sentence(pages: list[int]) -> str:
    """"pages 2, 4 and 7 to 9 have no selectable text and were not read", for
    the reply and the import row; "" for none."""
    if not pages:
        return ""
    runs: list[list[int]] = []
    for n in sorted(pages):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    # A run of three or more is a range; two side by side read better as two.
    named = [part for a, b in runs
             for part in ([f"{a} to {b}"] if b - a >= 2 else [str(n) for n in range(a, b + 1)])]
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    if len(pages) == 1:
        return f"page {listed} has no selectable text and was not read"
    return f"pages {listed} have no selectable text and were not read"


def _key(path: str | os.PathLike) -> tuple:
    st = os.stat(path)
    return (os.path.realpath(path), st.st_size, st.st_mtime_ns, vision_pages())


def docx_html_here(path: str | os.PathLike) -> str:
    """mammoth's HTML of a DOCX, in this process: the laptop's path."""
    import mammoth

    with open(path, "rb") as fh:
        return mammoth.convert_to_html(fh).value


def docx_html(path: str | os.PathLike) -> str:
    """mammoth's HTML of a DOCX, read in the limited child (the server's path).

    ponytail: figures stay behind in the child, which writes the HTML on one
    line under the text's share of the pipe; a PDF on the server carries no
    figures either. Send them as later lines, as rendered pages are, when a
    student's DOCX figure is what the note is missing."""
    out = _run_child(path, ["docx"], DOC_TOO_LARGE)
    end = out.find(b"\n")
    try:
        data = json.loads(out[:end]) if 0 <= end <= _TEXT_OUT_BYTES else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("html"), str):
        raise PdfUnreadable(DOC_TOO_LARGE, "too_large")
    return data["html"]


def _read_in_child(path: str | os.PathLike, vision: int = 0) -> dict:
    """Run `_child` on `path` and parse what it writes."""
    return _parse_child(_run_child(path, [str(vision)], TOO_LARGE))


def _run_child(path: str | os.PathLike, args: list[str], too_large: str) -> bytearray:
    """Run `_child` on an already-open descriptor of `path`; its stdout, capped.

    The descriptor, not the path: the child never resolves a name, so a link
    swapped in after this open cannot point it elsewhere (ADR-0098 H1)."""
    import logging
    import selectors
    import subprocess
    import tempfile
    import time

    global _NETWORK_NOTE_LOGGED
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        if os.fstat(fd).st_size > MAX_BYTES:
            raise PdfUnreadable(too_large, "too_large")
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", os.path.abspath(__file__), str(fd), *args],
                pass_fds=(fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={}, cwd=cwd,
            )
            assert proc.stdout is not None and proc.stderr is not None  # both are PIPE above
            stdout, stderr = proc.stdout, proc.stderr
            out, err = bytearray(), bytearray()
            sel = selectors.DefaultSelector()
            sel.register(stdout, selectors.EVENT_READ, out)
            sel.register(stderr, selectors.EVENT_READ, err)
            deadline = time.monotonic() + WALL_SECONDS
            try:
                # Read with a cap rather than communicate(): a child whose parse
                # was taken over can write without end, and communicate() would
                # hold all of it in the tenant's memory.
                while sel.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(proc.args, WALL_SECONDS)
                    for key, _ in sel.select(left):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            sel.unregister(key.fileobj)
                            continue
                        key.data.extend(chunk)
                        if len(out) > _OUT_BYTES or len(err) > _ERR_BYTES:
                            raise subprocess.TimeoutExpired(proc.args, WALL_SECONDS)
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # Stopped, not refused yet: a child past a limit while it
                # rendered has already written the text (`_parse_child`).
                proc.kill()
                proc.wait()
            finally:
                sel.close()
                stdout.close()
                stderr.close()
    finally:
        os.close(fd)
    note = err.decode("utf-8", "replace").strip()
    if note and not _NETWORK_NOTE_LOGGED:
        _NETWORK_NOTE_LOGGED = True
        logging.getLogger(__name__).warning("pdf child: %s", note[:500])
    return out


def _parse_child(out: bytearray) -> dict:
    """The child's lines: the text first, then one `[index, base64 PNG or
    null]` a rendered page.

    The text counts only when its line is whole, and then whatever ended the
    child: a signal or a limit while it rendered (a page's images are decoded
    there, which text extraction never did) costs the pages not yet sent,
    which the student sees named, and not the document. A child that ended
    before its first line is a limit that fired (SIGABRT under RLIMIT_AS,
    SIGXCPU under RLIMIT_CPU) or a crash in PDFium: the same sentence either way."""
    import base64

    end = out.find(b"\n")
    # The text has its own share of the cap: past it the child is not the one
    # this file wrote (MAX_CHARS stops an honest one well before).
    if end < 0 or end > _TEXT_OUT_BYTES:
        raise PdfUnreadable(TOO_LARGE, "too_large")
    try:
        data = json.loads(out[:end])
    except ValueError:
        raise PdfUnreadable(TOO_LARGE, "too_large") from None
    if not isinstance(data, dict):
        raise PdfUnreadable(TOO_LARGE, "too_large")
    if "error" in data:
        raise _refusal(str(data["error"]))
    data["hidden"] = {int(k): v for k, v in data.get("hidden", {}).items()}
    planned = set(data.get("render", ()))
    images: dict[int, bytes | None] = {}
    start = end + 1
    while (end := out.find(b"\n", start)) >= 0:
        try:
            item = json.loads(out[start:end])
            if not isinstance(item, list) or len(item) != 2:
                break  # the child's own error line after the text: no page follows it
            images[int(item[0])] = base64.b64decode(item[1], validate=True) if item[1] is not None else None
        except (ValueError, TypeError):
            break  # a line a kill cut short, which is the last one anyway
        start = end + 1
    data["images"] = {i: png for i, png in images.items() if i in planned}
    return data


# --- the child ----------------------------------------------------------------

def _drop_network() -> str:
    """Leave the network before the file is opened; "" when it worked.

    A new user namespace makes the new network namespace possible without
    privilege, and an empty network namespace has no route anywhere: a PDFium
    bug taken over in here cannot open a socket (ADR-0087's ponytail, closed
    by ADR-0098 H1). The call needs unprivileged user namespaces on the host;
    where they are off, the child runs with ADR-0087's limits alone, which stay
    the floor, and the parent logs the note once."""
    flags = 0x10000000 | 0x40000000  # CLONE_NEWUSER | CLONE_NEWNET
    try:
        if hasattr(os, "unshare"):
            os.unshare(flags)
        else:  # Python 3.11
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            if libc.unshare(flags) != 0:
                raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    except (OSError, AttributeError) as exc:
        return f"network kept, unshare failed: {exc}"
    return ""


def _child(fd: int, vision: int, kind: str = "pdf") -> int:
    import resource

    # Limits first, before pypdfium2 is even imported: set here and not through
    # preexec_fn, which is unsafe in the threaded GUI that spawns this.
    # RLIMIT_FSIZE 0 because the child writes no file, only its stdout pipe,
    # which the limit does not count; -B keeps an import from writing a .pyc,
    # which the limit would kill with SIGXFSZ.
    for limit, value in ((resource.RLIMIT_AS, AS_BYTES), (resource.RLIMIT_CPU, CPU_SECONDS),
                         (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NOFILE, 16)):
        resource.setrlimit(limit, (value, value))
    note = _drop_network()
    if note:
        sys.stderr.write(note + "\n")
    out = sys.stdout.buffer

    def emit(item) -> None:
        import base64

        if not isinstance(item, dict):
            index, png = item
            item = [index, base64.b64encode(png).decode("ascii") if png is not None else None]
        out.write(json.dumps(item).encode("ascii") + b"\n")
        # Flushed a line at a time: a SIGABRT in the next render loses
        # Python's buffer, and the text must already be in the pipe.
        out.flush()

    if kind == "docx":
        try:
            import io

            import mammoth

            # Copied, not handed over: mammoth takes `fileobj.name` for the
            # folder of a linked image, and an fdopen'd file's name is an int.
            # The parent already held the file to MAX_BYTES.
            with os.fdopen(fd, "rb") as fh:
                docx = io.BytesIO(fh.read())
            # An <img> with no src: `_html_to_md` keeps its alt text and
            # writes no file, which the child could not write anyway.
            html = mammoth.convert_to_html(
                docx, convert_image=mammoth.images.img_element(lambda image: {})).value
            emit({"html": html[:MAX_CHARS]})
        except Exception:
            # Any failure in here is the file's: the parent names it with
            # DOC_TOO_LARGE, and the traceback stays in the child.
            emit({"error": "malformed"})
        return 0
    try:
        with os.fdopen(fd, "rb") as fh:
            extract(fh, max_pages=MAX_PAGES, max_chars=MAX_CHARS, vision_pages=vision, emit=emit)
    except _Refused as exc:
        emit({"error": exc.reason})
    except Exception as exc:
        emit({"error": "password" if "password" in str(exc).lower() else "malformed"})
    return 0


if __name__ == "__main__":
    if sys.argv[2:3] == ["docx"]:
        sys.exit(_child(int(sys.argv[1]), 0, "docx"))
    sys.exit(_child(int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 0))
