# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The retrieval engine this tree serves, from the silica-core package (ADR-0043).

Below the tools, because the memory ledger asks it for a file's version and the
CLI's /find for a search, and both were importing a 2,000-line tool module for
one function.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# silica.sources.pdf_extract, which hands itself over when it is imported:
# sources sits above this lane. Unset, the engine refuses to run, exactly as it
# does for a silica-core that renamed its reader: the one thing it must never do
# is go back to opening PDFs in this process.
pdf_reader: Any = None


def engine():
    """The retrieval engine, from the silica-core package (ADR-0043). It
    carries its own CONFIG singleton, so the vault this tree serves is
    mirrored into it at every call, and it gets its own index file: its
    build drops every path its walk did not produce, so pointed at
    `lexical.json` it would prune the fusion leg's entries (ADR-0029).

    Its walk does not apply is_source_leaf: sources/ leaves are searchable here
    and hidden from the fusion indexes, one vault with two rules (S3 of
    docs/audits/2026-09-17-paper-backed-simplifications.md)."""
    from silica.config import CONFIG
    from silica_core import config as core_config
    from silica_core import core

    core.INDEX = "retrieval"
    core_config.CONFIG.vault_path = CONFIG.vault_path
    # The engine reads SILICA_EMBEDDING_DOC_PREFIX and this tree
    # SILICA_EMBEDDING_DOCUMENT_PREFIX: unmirrored, a page would be embedded
    # bare while every query carried "Query: " (found 2026-11-08 on jina v5).
    core_config.CONFIG.embedding_doc_prefix = CONFIG.embedding_document_prefix
    core_config.CONFIG.embedding_query_prefix = CONFIG.embedding_query_prefix
    # A silica-core that renamed either name would go back to opening PDFs in
    # this process without a word, so the engine refuses to run instead. The
    # server installs the lock (0.10.1), but a wheel install resolves
    # `silica-core>=0.10.1` to the newest release, untested here; 0.11.0 keeps
    # both names (checked 2026-10-20).
    if not (callable(getattr(core, "_pdf_text", None)) and callable(getattr(core, "_extract_cache", None))):
        raise RuntimeError("silica-core no longer reads PDFs through _pdf_text: see silica/kernel/recall/engine.py")
    if pdf_reader is None:
        raise RuntimeError("no PDF reader was handed to the engine: see silica/kernel/recall/engine.py")
    core._pdf_text = pdf_text
    return core


# The tag on the page map this tree writes beside the engine's extraction:
# silica-core's own map is a bare list, and its text kept the hidden lines.
_PDF_READER = "pdf_extract/1"


def pdf_text(rel: str, full: Path) -> tuple[str, list[dict], str]:
    """silica-core's `_pdf_text`, through which its index, its search windows
    and `silica_read` open every PDF, read by `pdf_extract` instead: in the
    limited child on the server (ADR-0098 H1) and without the text a student
    never sees (H6). The engine opened each PDF it walked with PDFium in this
    process and kept those lines, so a dropped PDF, which stays in the Inbox
    while it converts, reached the server's own process at the next search
    and its white text reached the turn (found 2026-10-20).

    Same return and the same cache file, whose path `silica_read` hands out as
    `extract_path`. The map beside it carries `_PDF_READER`, so a fresh
    extraction the engine wrote before this is read again, never served.
    The pages are the text alone (`vision=0`): the renders are the
    conversion's, and up to 60 a file would be paid on every PDF the index
    walks."""
    import orjson

    from silica.kernel.base.durable import atomic_write_bytes
    from silica_core import core

    cache = core._extract_cache(rel)
    pages_file = cache.with_suffix(".json")
    try:
        if cache.stat().st_mtime >= full.stat().st_mtime:
            saved = orjson.loads(pages_file.read_bytes())
            if isinstance(saved, dict) and saved.get("reader") == _PDF_READER:
                text = cache.read_text(encoding="utf-8", errors="replace")
                return text, saved["pages"], "" if text else "no text layer"
    except (OSError, ValueError):
        pass  # no extraction yet, or a torn one: the read below writes it again
    try:
        data = pdf_reader.read(full, vision=0)
    except Exception as exc:
        # One file the reader refuses (encrypted, a bomb the child died of, not
        # a PDF) is that file listed `unconverted` with its reason, as the
        # engine's own reader did: the index build has no guard of its own and
        # would stop for every other file. Only ADR-0087's sentences, written
        # for the student, go out as they are; anything else can carry a path.
        why = str(exc) if isinstance(exc, pdf_reader.PdfUnreadable) else type(exc).__name__
        return "", [], f"unreadable: {why}"
    bodies = ["\n".join(line for line, _size in page).strip() for page in data["pages"]]
    parts: list[str] = []
    pages: list[dict] = []
    line, offset = 1, 0
    for n, body in enumerate(bodies, 1):
        chunk = body + "\n\n"
        pages.append({"page": n, "line": line, "offset": offset})
        parts.append(chunk)
        line += chunk.count("\n")
        offset += len(chunk)
    text = "".join(parts) if any(bodies) else ""
    pages = pages if text else []
    cache.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(cache, text.encode("utf-8"))
    atomic_write_bytes(pages_file, orjson.dumps({"reader": _PDF_READER, "pages": pages}))
    return text, pages, "" if text else "no text layer"
