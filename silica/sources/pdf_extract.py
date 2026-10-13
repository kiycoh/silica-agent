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
"""
from __future__ import annotations

import json
import os
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
_OUT_BYTES = 40 * 1024 * 1024
_ERR_BYTES = 64 * 1024
# A PDF reader accepts the header anywhere in the first 1,024 bytes (Acrobat's
# tolerance, which files in the wild rely on), so the check looks that far.
HEAD_BYTES = 1024
# Under 1 pt is unreadable on any screen or page: text set that small is there
# for a reader other than the student (ADR-0098 H6).
TINY_POINTS = 1.0
# Text render modes 3 (neither fill nor stroke) and 7 (clip only) draw nothing.
_INVISIBLE_MODES = (3, 7)

TOO_LARGE = "This PDF is too large or malformed to read."
PASSWORD = "This PDF is password-protected: remove the password and upload it again."
NOT_PDF = "This file is not a PDF: it has no %PDF- header in its first 1,024 bytes."
_MESSAGES = {"password": PASSWORD, "malformed": TOO_LARGE, "pages": TOO_LARGE, "output": TOO_LARGE}


class PdfUnreadable(ValueError):
    """A PDF that cannot be read, with the sentence the student is shown.

    A ValueError because every caller that stages files already catches one per
    file (`_stage_uploads`); `PdfiumError` is a RuntimeError, which ended a
    whole drop with a 500 (ADR-0098 H4)."""


def require_header(path: str | os.PathLike) -> None:
    with open(path, "rb") as fh:
        if b"%PDF-" not in fh.read(HEAD_BYTES):
            raise PdfUnreadable(NOT_PDF)


# --- the read, on either side of the process boundary -------------------------

def extract(source, max_pages: int | None = None, max_chars: int | None = None) -> dict:
    """Read `source` (a path or a binary file object) into plain data.

    Hidden text is left out of a page that also has visible text, and counted
    under `hidden` by page index (ADR-0098 H6). A page whose text is all
    hidden is an OCR layer, which Tesseract and ocrmypdf draw in render mode 3
    over the scan, and is kept whole.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument(source)
    try:
        n = len(pdf)
        # len() reads the page count without parsing a page: 0.05 s on a
        # 200,000-page tree (ADR-0087), so the cap costs nothing before the work.
        if max_pages is not None and n > max_pages:
            raise _Refused("pages")
        meta = {key: " ".join((pdf.get_metadata_value(key) or "").split())
                for key in ("Title", "Author", "CreationDate")}
        toc: list[list] = []
        for bookmark in pdf.get_toc():
            title = " ".join((bookmark.get_title() or "").split())
            dest = bookmark.get_dest()
            index = dest.get_index() if dest is not None else None
            if title and index is not None and index >= 0:
                toc.append([bookmark.level, title, index])
        pages: list[list] = []
        hidden: dict[int, int] = {}
        chars = 0
        for i in range(n):
            page = pdf[i]
            textpage = page.get_textpage()
            try:
                lines, dropped = _page_lines(page, textpage, raw)
            finally:
                textpage.close()
                page.close()
            if dropped:
                hidden[i] = dropped
            chars += sum(len(t) for t, _ in lines)
            if max_chars is not None and chars > max_chars:
                raise _Refused("output")
            pages.append(lines)
    finally:
        pdf.close()
    return {"pages": pages, "toc": toc, "meta": meta, "n_pages": n, "hidden": hidden}


class _Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _page_lines(page, textpage, raw) -> tuple[list[list], int]:
    """[text, font size] per non-blank line of a page, and how many hidden
    characters were left out of it.

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
    return lines, dropped


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
    stack = [(raw.FPDFPage_CountObjects(page), lambda j, p=page: raw.FPDFPage_GetObject(p, j))]
    while stack:
        count, get = stack.pop()
        for j in range(count):
            obj = get(j)
            kind = raw.FPDFPageObj_GetType(obj)
            if kind == raw.FPDF_PAGEOBJ_TEXT and _object_hidden(obj, raw, size, matrix):
                return True
            if kind == raw.FPDF_PAGEOBJ_FORM:
                stack.append((raw.FPDFFormObj_CountObjects(obj),
                              lambda j, f=obj: raw.FPDFFormObj_GetObject(f, j)))
    return False


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

    Cached by path, size and mtime: one conversion asks for the text, the date
    and the citation, and before this each of those opened the hostile file
    again (ADR-0087 Context, three opens)."""
    from silica.config import hosted

    key = _key(path)
    if key in _CACHE:
        return _CACHE[key]
    # Before PDFium sees it: a renamed HTML file reached it as a PdfiumError,
    # which ended a whole drop with a 500 (ADR-0098 H4).
    require_header(path)
    if hosted():
        data = _read_in_child(path)
    else:
        try:
            data = extract(os.fspath(path))
        except _Refused as exc:
            raise PdfUnreadable(_MESSAGES[exc.reason]) from None
        except Exception as exc:
            # pypdfium2 raises PdfiumError (a RuntimeError) for every load
            # failure, and names the password case only in its message.
            if type(exc).__name__ != "PdfiumError":
                raise
            raise PdfUnreadable(PASSWORD if "password" in str(exc).lower() else TOO_LARGE) from None
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
        return {k: " ".join((pdf.get_metadata_value(k) or "").split())
                for k in ("Title", "Author", "CreationDate")}
    finally:
        pdf.close()


def cached(path: str | os.PathLike) -> dict | None:
    """The last read of `path`, for a caller that reports on it after converting."""
    return _CACHE.get(_key(path))


def _key(path: str | os.PathLike) -> tuple:
    st = os.stat(path)
    return (os.path.realpath(path), st.st_size, st.st_mtime_ns)


def _read_in_child(path: str | os.PathLike) -> dict:
    """Run `_child` on an already-open descriptor of `path` and parse its JSON.

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
            raise PdfUnreadable(TOO_LARGE)
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", os.path.abspath(__file__), str(fd)],
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
                proc.kill()
                proc.wait()
                raise PdfUnreadable(TOO_LARGE) from None
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
    # A signal is a limit that fired (SIGABRT under RLIMIT_AS, SIGXCPU under
    # RLIMIT_CPU), or a crash in PDFium: the same sentence either way.
    if proc.returncode != 0:
        raise PdfUnreadable(TOO_LARGE)
    try:
        data = json.loads(out)
    except ValueError:
        raise PdfUnreadable(TOO_LARGE) from None
    if "error" in data:
        raise PdfUnreadable(_MESSAGES.get(data["error"], TOO_LARGE))
    data["hidden"] = {int(k): v for k, v in data.get("hidden", {}).items()}
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


def _child(fd: int) -> int:
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
    try:
        with os.fdopen(fd, "rb") as fh:
            data = extract(fh, max_pages=MAX_PAGES, max_chars=MAX_CHARS)
    except _Refused as exc:
        data = {"error": exc.reason}
    except Exception as exc:
        data = {"error": "password" if "password" in str(exc).lower() else "malformed"}
    sys.stdout.write(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(_child(int(sys.argv[1])))
