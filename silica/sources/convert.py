# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Non-`.md` → `.md` conversion — ingress frontier (ADR-0009).

A plain function, not a `SourceAdapter`: `/convert` exposes it and `/nucleate`
calls it as the fallback when no source adapter claims a file. Dispatch is by
extension: PDF plus every other format MuPDF opens (`DOC_EXTS` — DOCX, EPUB,
XPS, MOBI, FB2).

For PDF the converter is selectable via `CONFIG.pdf_provider` (ADR-0011):
`pymupdf` default (pymupdf4llm, ~60 MB installed, no torch and no JVM, but no
OCR), `mineru` (heavyweight CLI, best fidelity and the only OCR path, downloads
models on first run), `docling` (MIT but pulls torch + CUDA), `opendataloader`
(Apache-2.0, strong on complex tables and multi-column reading order, needs a
JVM). Only `pymupdf` opens the non-PDF formats, so those bypass the seam.
`pymupdf4llm` is a base dependency; the alternatives install via the
`silica-agent[pdf]` extra or by hand.

`pymupdf4llm` is pinned `<1` on purpose: from 1.27.2 it hard-depends on
`pymupdf-layout`, which is Polyform Noncommercial and cannot ship as a
dependency of an AGPL package.

Every provider returns `(markdown, images_dir)`; the rest of the pipeline
(sanitize → copy images flat into the vault → rewrite image links to Obsidian
embeds → write the note to the inbox) is shared and provider-agnostic.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from glob import glob
from pathlib import Path

from silica.config import CONFIG
from silica.kernel.text.sanitize import strip_degenerate_runs

logger = logging.getLogger(__name__)

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# Book segmentation — a converted book is one giant markdown, but RECON caps
# concepts PER FILE (keyphrase.MAX_CONCEPTS=40), so a whole book in one inbox
# note loses almost everything. Split on chapter headings, then size-cap each
# section so RECON sees book-sized units. ~40k chars ≈ 10k tokens ≈ ~15 pages:
# raise for fewer/larger files, lower for more granular notes.
_MAX_SEGMENT_CHARS = 40_000
_HEADING_RE = re.compile(r"^#{1,2} \S")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# MinerU knobs — ponytail: module constants. First run downloads models, so the
# timeout is generous; switch to a VLM/hybrid backend or raise the timeout here.
# Measured ~0.9 s/page on CPU (80-page probe): 600s died on an 800-page book.
_MINERU_BACKEND = "pipeline"
_MINERU_TIMEOUT_S = 3600
# Maximum-precision non-generative pins (today's upstream defaults, pinned
# against drift — the upstream default backend already drifted to a VLM):
# -m auto (parse method), -f true (formula parsing), -t true (table parsing).
# No -l: mineru 3.4.4 has no latin-script choice (ch|ch_server|korean|...) and
# the default `ch` OCR models cover latin script.
_MINERU_ARGS = ["-m", "auto", "-f", "true", "-t", "true"]

# stderr triage (see _mineru_error): noise = loguru INFO/DEBUG, uvicorn banner
# lines, tqdm progress bars; error-ish = a line naming an error/exception.
_MINERU_NOISE_RE = re.compile(
    r"\|\s*(?:INFO|DEBUG)\s*\||^(?:INFO|DEBUG|WARNING):|it/s|\d+%\|", re.IGNORECASE
)
_MINERU_ERR_RE = re.compile(r"error|exception|traceback", re.IGNORECASE)


# Formats MuPDF opens beyond PDF. `.txt`/`.md` are absent on purpose: ProseAdapter
# already claims them, and round-tripping plain text through a page renderer would
# hard-wrap it at the page width.
_PYMUPDF_ONLY_EXTS = (".docx", ".epub", ".xps", ".mobi", ".fb2")
DOC_EXTS = (".pdf", *_PYMUPDF_ONLY_EXTS)


def convert(target: str, dest_dir: str = "") -> list[str]:
    """Convert a non-`.md` document into one or more `.md` notes in the inbox.

    Returns the list of created note paths. A small document is a single note; a
    book-sized one is split into chapter/size-bounded segments (see
    ``split_markdown``) so RECON — which caps concepts PER FILE — sees book
    units, not the whole book collapsed into one note. Dispatch by extension
    over ``DOC_EXTS``; anything else → ``ValueError``. Side artifacts (extracted
    figures) go to ``<dest_dir>/Images`` when given, else ``<inbox>/Images``.
    """
    # Strip first: a quoted path with a stray trailing space ("…book.pdf ") has
    # suffix ".pdf " — not in DOC_EXTS — and the rejection then prints ".pdf",
    # a message the user cannot tell from a real unsupported type.
    target = target.strip()
    if Path(target).suffix.lower() not in DOC_EXTS:
        raise ValueError(f"no converter for {Path(target).suffix.lower() or 'this file type'}")
    return _doc_to_md(target, dest_dir)


def _split_on_headings(md: str) -> list[str]:
    """Split markdown at level-1/2 headings (fence-aware). Always ≥1 segment.

    Content before the first heading stays attached to it (no empty lead
    segment). A ``#``/``##`` inside a fenced code block is not a boundary.
    """
    segs: list[str] = []
    cur: list[str] = []
    in_fence = False
    for line in md.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and _HEADING_RE.match(line) and "".join(cur).strip():
            segs.append("".join(cur))
            cur = []
        cur.append(line)
    if "".join(cur).strip():
        segs.append("".join(cur))
    return segs or [md]


def _split_by_size(text: str, max_chars: int) -> list[str]:
    """Greedy split on blank-line (paragraph) boundaries, ≤ max_chars per part.

    A single paragraph larger than max_chars is left whole (its own oversized
    part) rather than cut mid-sentence — vanishingly rare in prose.
    """
    segs: list[str] = []
    cur = ""
    for part in re.split(r"(\n[ \t]*\n)", text):
        if cur and len(cur) + len(part) > max_chars:
            segs.append(cur)
            cur = ""
        cur += part
    if cur.strip():
        segs.append(cur)
    return segs or [text]


def split_markdown(md: str, max_chars: int = _MAX_SEGMENT_CHARS) -> list[str]:
    """Book-sized markdown → RECON-sized segments: heading-split, packed to size.

    Headings (``#``/``##``) are the cut points; any section still over
    ``max_chars`` is further split on paragraph boundaries — the same
    dimensional fallback that carries a heading-less scan. Adjacent pieces are
    then greedily packed up to ``max_chars``: real converters flatten every
    section to ``##`` and emit lone ``## Chapter N`` lines (verified on an
    80-page docling probe: 53 raw segments, some 14 chars), so raw sections
    over-fragment — packing restores chapter-sized units and absorbs the
    micro-segments. A document smaller than ``max_chars`` packs to a single
    segment. Always returns ≥1 segment.
    """
    pieces: list[str] = []
    for section in _split_on_headings(md):
        if len(section) <= max_chars:
            pieces.append(section)
        else:
            pieces.extend(_split_by_size(section, max_chars))

    out: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) > max_chars:
            out.append(cur)
            cur = ""
        cur += p
    if cur.strip():
        out.append(cur)
    return out or [md]


def _segment_slug(segment: str, fallback: str) -> str:
    """Filename slug from the segment's first heading; ``fallback`` if none."""
    for line in segment.splitlines():
        if _HEADING_RE.match(line):
            slug = _SLUG_RE.sub("-", line.lstrip("#").strip().lower()).strip("-")
            if slug:
                return slug[:50]
    return fallback


# mineru drops the space after , ; : between letters ("symmetric,and positive")
# and the glitch flows into RECON concepts and note titles. Letters-only guard
# keeps digits ("10,000") and LaTeX macros ("\alpha,\beta") untouched.
_TIGHT_PUNCT_RE = re.compile(r"(?<=[A-Za-zà-ÿ])([,;:])(?=[A-Za-zà-ÿ])")


def _respace_prose(md: str) -> str:
    """Re-insert the missing space after ,;: in prose — not in code or math.

    ponytail: inline $…$ spans are skipped per line; display-math interiors are
    not tracked ("x,y" → "x, y" renders identically in LaTeX). Glued words with
    no punctuation ("overthe") need a dictionary — out of scope.
    """
    out: list[str] = []
    in_fence = False
    for line in md.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            parts = line.split("$")
            for i in range(0, len(parts), 2):  # even = outside $…$
                parts[i] = _TIGHT_PUNCT_RE.sub(r"\1 ", parts[i])
            line = "$".join(parts)
        out.append(line)
    return "".join(out)


def _doc_to_md(target: str, dest_dir: str) -> list[str]:
    src = _resolve_input(target)
    # The provider seam is PDF-only — mineru/docling/opendataloader all take a
    # PDF and nothing else, so DOCX/EPUB/… go straight to pymupdf.
    if src.suffix.lower() != ".pdf":
        provider = _via_pymupdf
    elif CONFIG.pdf_provider in PDF_PROVIDERS:
        provider = PDF_PROVIDERS[CONFIG.pdf_provider]
    else:
        raise ValueError(
            f"unknown pdf_provider {CONFIG.pdf_provider!r} "
            f"(known: {', '.join(PDF_PROVIDERS)})"
        )
    with tempfile.TemporaryDirectory() as tmp:
        md_text, images_src = provider(src, Path(tmp))
        if not md_text.strip():
            # Silence here would write an empty inbox note and call it success.
            # The usual cause is a scan with no text layer, and pymupdf — the
            # default — has no OCR at all, so name the provider that does.
            raise ValueError(
                f"no text extracted from {src.name} — a scanned document needs OCR: "
                "`pip install 'silica-agent[pdf]'` and set SILICA_PDF_PROVIDER=mineru"
            )
        # Copy only images the markdown references: mineru dumps every crop it
        # detects (477 files for a 200-page book, 19 referenced) — the rest
        # would land in the vault as orphans.
        referenced = {os.path.basename(m.group(1)) for m in _MD_IMG_RE.finditer(md_text)}
        renamed = _copy_images(                                  # before tmp is cleaned
            images_src, _images_dest(dest_dir), _image_prefix(src), only=referenced
        )
    body = _rewrite_image_links(_respace_prose(strip_degenerate_runs(md_text)), renamed)
    from silica.driver import DRIVER
    from silica.kernel.vault_manifest import active_inbox_dir

    inbox = active_inbox_dir() or "Inbox"
    segments = split_markdown(body)
    # Every segment names the real file it came from: the provenance ledger
    # only ever records the inbox note's basename, so without this the original
    # PDF is untraceable once the inbox note is archived. Plain quoted string,
    # not a link — the pointer must not enter the graph. CLEANUP carries it
    # into the source leaf when the note is later nucleated with keep_sources.
    fm = _provenance_fm(src)
    # Single segment (a paper, an article) keeps the flat inbox path — no change
    # in behaviour, no subdir for the common case. Image links are basename
    # embeds (![[fig.png]]) so they resolve from any segment regardless of dir.
    if len(segments) == 1:
        note_rel = f"{inbox}/{src.stem}.md"
        DRIVER.upsert(note_rel, fm + body.lstrip("\n"))  # re-converting the same source refreshes its inbox note
        return [note_rel]

    width = len(str(len(segments)))
    paths: list[str] = []
    for i, seg in enumerate(segments, 1):
        slug = _segment_slug(seg, "part")
        note_rel = f"{inbox}/{src.stem}/{i:0{width}d}-{slug}.md"
        DRIVER.upsert(note_rel, fm + seg.lstrip("\n"))  # re-converting the same source refreshes its segments
        paths.append(note_rel)
    logger.info("PDF %s split into %d inbox segment(s)", src.name, len(segments))
    return paths


# --- providers (each: src pdf, workdir → markdown text, images dir) ---------
#
# TODO(real-api): each provider's third-party call surface is only exercised by
# hand-faked modules in tests/test_convert.py — a library rename would drift the
# fakes and pass silently. Add a real-install smoke test to catch API drift.

def _via_pymupdf(src: Path, workdir: Path) -> tuple[str, Path]:
    """Default provider — pymupdf4llm: no torch, no JVM, ~60 MB installed.

    The only provider that opens the non-PDF `DOC_EXTS`, and the only one in the
    base install. It has no OCR: a scan with no text layer yields nothing, which
    `_doc_to_md`'s empty guard turns into an error naming mineru.
    """
    try:
        import contextlib
        import io

        import pymupdf
        # pymupdf4llm prints a "consider pymupdf_layout" advert to stdout at
        # import; that package is Polyform Noncommercial, so the advice is one
        # we cannot take and the line is pure noise in the TUI.
        with contextlib.redirect_stdout(io.StringIO()):
            import pymupdf4llm
    except ImportError:
        raise ValueError(
            "pymupdf4llm not installed — `pip install 'pymupdf4llm>=0.3.4,<1'`, "
            "or set SILICA_PDF_PROVIDER to mineru/docling/opendataloader"
        ) from None

    doc = pymupdf.open(src)
    images = workdir / "images"
    images.mkdir(parents=True, exist_ok=True)
    # The embedded outline beats font-size guessing wherever it exists: 23
    # headings vs 12 on an 19-entry probe paper, matching mineru exactly. But
    # TocHeaders REPLACES the font heuristic rather than backing it up, so on a
    # document with no outline it collapsed 10 headings to 1 — hence the guard.
    hdr = pymupdf4llm.TocHeaders(doc) if doc.get_toc() else None
    md = pymupdf4llm.to_markdown(
        doc, hdr_info=hdr, write_images=True, image_path=str(images), image_format="png"
    )
    return md, images


def _pdf_via_docling(src: Path, workdir: Path) -> tuple[str, Path]:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import ImageRefMode
    except ImportError:
        raise ValueError(
            "docling not installed — `pip install docling`, "
            "or set SILICA_PDF_PROVIDER to mineru/opendataloader"
        ) from None

    opts = PdfPipelineOptions()
    opts.generate_picture_images = True  # else REFERENCED export emits placeholders
    # Maximum-precision non-generative pins. No do_formula_enrichment /
    # do_code_enrichment: CodeFormula is a generative model, out of boundary.
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.table_structure_options.do_cell_matching = True
    opts.images_scale = 2.0  # extracted figures at 144 dpi instead of 72
    opts.do_ocr = True
    # docling's default language list omits Italian; csv config, split here.
    opts.ocr_options.lang = [s.strip() for s in CONFIG.pdf_ocr_lang.split(",") if s.strip()]
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    doc = converter.convert(str(src)).document
    images = workdir / "images"
    md_path = workdir / f"{src.stem}.md"
    doc.save_as_markdown(md_path, image_mode=ImageRefMode.REFERENCED, artifacts_dir=images)
    return md_path.read_text(encoding="utf-8", errors="replace"), images


def _pdf_via_opendataloader(src: Path, workdir: Path) -> tuple[str, Path]:
    # Java-backed (JVM per convert), Apache-2.0. Strong on complex tables and
    # multi-column reading order; the wheel bundles the CLI but needs Java 11+.
    try:
        import opendataloader_pdf
    except ImportError:
        raise ValueError(
            "opendataloader-pdf not installed — `pip install opendataloader-pdf` "
            "(needs Java 11+), or set SILICA_PDF_PROVIDER to docling/mineru"
        ) from None

    out = workdir / "out"
    images = workdir / "images"
    # use_struct_tree: when the PDF carries native structure tags, headings and
    # reading order come from the author's own markup. `hybrid` (the only OCR
    # path) is never passed — it is generative, out of boundary — so scanned
    # PDFs yield nothing from this provider; use mineru/docling for those. If
    # the installed wrapper predates the kwarg, the TypeError names it.
    opendataloader_pdf.convert(
        input_path=str(src), output_dir=str(out),
        format="markdown", image_output="external", image_dir=str(images),
        use_struct_tree=True,
    )
    hits = glob(str(out / "**" / "*.md"), recursive=True)
    if not hits:
        raise ValueError("opendataloader produced no markdown")
    return Path(hits[0]).read_text(encoding="utf-8", errors="replace"), images


def _mineru_error(stderr: str) -> str:
    """One-line, human-readable error from mineru's stderr.

    mineru may write a JSON task blob (with an ``error`` field) or a loguru
    stream. Pull the ``error`` field when present. Otherwise the cause is NOT
    at the head: this mineru version spins up an internal ``mineru-api`` server
    and floods stderr with startup logs + tqdm bars before any work, so
    head-truncating just surfaces "Started local mineru-api ...". Drop
    INFO/progress noise, then return the last error-ish line (else the last
    meaningful line) — a Python traceback puts "XError: msg" last too.
    """
    err = stderr.strip()
    try:
        parsed = json.loads(err)
        return str(parsed.get("error") or err[:300])
    except (ValueError, AttributeError):
        pass
    # The task blob is usually EMBEDDED in the final "Error: N task(s) failed"
    # line, where its "error" field sits past any truncation window — pull it
    # straight out (last match = final task).
    fields = re.findall(r'"error":\s*"((?:[^"\\]|\\.)+)"', err)
    if fields:
        return fields[-1][:300]
    lines = [
        ln.strip() for ln in err.splitlines()  # \r-split too: tqdm bars separate
        if ln.strip() and not _MINERU_NOISE_RE.search(ln)
    ]
    if not lines:
        return err[:300]
    hits = [ln for ln in lines if _MINERU_ERR_RE.search(ln)]
    return (hits[-1] if hits else lines[-1])[:300]


def _pdf_via_mineru(src: Path, workdir: Path) -> tuple[str, Path]:
    out = workdir / "out"
    try:
        proc = subprocess.run(
            ["mineru", "-p", str(src), "-o", str(out), "-b", _MINERU_BACKEND, *_MINERU_ARGS],
            capture_output=True, text=True, timeout=_MINERU_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise ValueError(
            "mineru not installed — `pip install 'silica-agent[pdf]'` (or `pip install "
            "'mineru[pipeline]'`), or set SILICA_PDF_PROVIDER to docling/opendataloader"
        ) from None
    if proc.returncode != 0:
        raise ValueError(f"mineru failed: {_mineru_error(proc.stderr)}")
    hits = glob(str(out / src.stem / "**" / f"{src.stem}.md"), recursive=True)
    if not hits:
        raise ValueError("mineru produced no markdown")
    md_path = Path(hits[0])
    return md_path.read_text(encoding="utf-8", errors="replace"), md_path.parent / "images"


PDF_PROVIDERS = {
    "pymupdf": _via_pymupdf,
    "docling": _pdf_via_docling,
    "mineru": _pdf_via_mineru,
    "opendataloader": _pdf_via_opendataloader,
}


# --- shared helpers ---------------------------------------------------------

def _provenance_fm(src: Path) -> str:
    """Frontmatter block naming the converted file's real origin (absolute path)."""
    quoted = str(src).replace("\\", "\\\\").replace('"', '\\"')
    return f'---\nsource_file: "{quoted}"\n---\n\n'


def _resolve_input(target: str) -> Path:
    """Absolute as given; relative tried vault-first, then cwd.

    ProseAdapter.read resolves vault-only because its inputs are notes, which
    live in the vault. A file to CONVERT is the opposite: a PDF sits where the
    user is standing (a download dir, a repo), not among the markdown — so cwd
    is a real fallback here, not just the no-vault special case.
    """
    p = Path(target)
    if not p.is_absolute():
        vault = (CONFIG.vault_path or "").strip()
        tries = ([Path(vault) / target] if vault else []) + [Path.cwd() / target]
        p = next((c for c in tries if c.exists()), tries[-1])
    if not p.exists():
        raise ValueError(f"file not found: {target}")
    return p


def _images_dest(dest_dir: str) -> Path:
    from silica.kernel.vault_manifest import active_inbox_dir

    base = dest_dir.strip() or active_inbox_dir() or "Inbox"
    return Path(CONFIG.vault_path) / base / "Images"


def _image_prefix(src: Path) -> str:
    """Per-source namespace for the flat `Images/` dir.

    Derived from the source STEM, not its content: re-converting the same PDF
    must reproduce the same image names, or every run would leave the previous
    run's figures behind as orphans. Same identity the note path already assumes
    (`{inbox}/{src.stem}.md`), so two sources that collide here already collide
    there.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", src.stem).strip("-_")[:40]
    return slug or "doc"


def _copy_images(
    src_dir: Path,
    dest_dir: Path,
    prefix: str,
    only: set[str] | None = None,
) -> dict[str, str]:
    """Copy referenced images into the flat vault dir, namespaced per source.

    Returns `{original basename: copied basename}` for the link rewrite. The
    prefix is load-bearing: providers name figures by page index
    (`_page_0_Figure_1.jpeg`), which repeats across documents, and both the copy
    and the `![[basename]]` embed are flat — an un-namespaced second PDF would
    overwrite the first's figure AND silently repoint the first note's embed at
    it.
    """
    if not src_dir.is_dir():
        return {}
    files = [
        f for f in src_dir.iterdir()
        if f.is_file() and (only is None or f.name in only)
    ]
    if not files:
        return {}
    dest_dir.mkdir(parents=True, exist_ok=True)
    renamed: dict[str, str] = {}
    for f in files:
        name = f"{prefix}-{f.name}"
        shutil.copy2(f, dest_dir / name)
        renamed[f.name] = name
    return renamed


def _rewrite_image_links(md: str, renamed: dict[str, str] | None = None) -> str:
    """`![alt](any/path/x.png)` → `![[x.png]]` (basename, Obsidian embed).

    `renamed` maps the provider's basename to the namespaced one actually
    copied into the vault; an unmapped basename embeds unchanged.
    """
    def repl(m: "re.Match[str]") -> str:
        base = os.path.basename(m.group(1))
        if not base.lower().endswith(_IMG_EXTS):
            return m.group(0)
        return f"![[{(renamed or {}).get(base, base)}]]"

    return _MD_IMG_RE.sub(repl, md)
