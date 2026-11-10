# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's files as a library (ADR-0156): a file no note names, opened,
its chapters, studied or OCR'd from the drawer, and the page vectors' build
the rail reports. The laptop's only: on the server a tenant reaches a file
through the note that names it (ADR-0085)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from silica.config import CONFIG
from silica.kernel.base.formats import SHOWN_IMAGE_EXTS
from silica.ui.web import note_ops, render, vectors
from silica.ui.web.failures import Refused

router = APIRouter()

# What the drawer shows of a file no note names: a PDF in the browser's viewer
# and a picture as itself. Not SVG, which is a document that runs (see
# render.ASSET_HEADERS): it goes as a download.
_FILE_OPENS = (*render.RAW_OPENS, *(e for e in SHOWN_IMAGE_EXTS if e != ".svg"))


def _vault_file(path: str) -> Path:
    """The vault file a rail row names, or 404: the laptop's only, inside the
    vault, never under a dot-folder or a dot-file."""
    from silica.config import hosted

    if hosted() or not path or not CONFIG.vault_path:
        raise HTTPException(404)
    root = Path(CONFIG.vault_path).resolve()
    target = (root / path).resolve()
    if not (target.is_relative_to(root) and target.is_file()) \
            or any(part.startswith(".") for part in target.relative_to(root).parts):
        raise HTTPException(404)
    return target


def _rel(found: Path) -> str:
    return found.relative_to(Path(CONFIG.vault_path).resolve()).as_posix()


@router.get("/file")
def vault_file(path: str = "", download: bool = False):
    """A vault file no note names (ADR-0129, amended 2026-11-08), which /raw,
    keyed by a note, answers for none of: on the laptop every file is a rail
    row since that day. Under /asset's headers, whose `sandbox` keeps a
    navigated file off the GUI's origin."""
    found = _vault_file(path)
    attach = download or found.suffix.lower() not in _FILE_OPENS
    return FileResponse(found, headers=render.ASSET_HEADERS, filename=found.name,
                        content_disposition_type="attachment" if attach else "inline")


@router.get("/file/chapters")
def file_chapters(path: str = ""):
    """A vault PDF's chapters for the drawer's info view (ADR-0156 point 6):
    {source, pages, steps}, the one ladder Memorize studies by too; {} for a
    file with none to read."""
    from silica.sources.library import chapters

    return chapters(_rel(_vault_file(path)))


@router.post("/file/study")
def file_study(payload: dict):
    """The drawer's "Study this" on a PDF Silica reads (ADR-0156 point 7): its
    chapters written beside it as notes, one undo run, and the folder they are
    in, which Memorize's new plan opens on. No model call: the text layer."""
    from silica.sources.library import write_chapters

    found = _vault_file(str(payload.get("path") or ""))
    try:
        out = write_chapters(_rel(found))
    except ValueError as exc:  # no text to study: the sentence says to OCR it first
        raise Refused(400, exc) from None
    except FileExistsError as exc:  # a chapter's path is taken by a note of the person's
        raise Refused(409, f"{exc.args[0] if exc.args else 'a chapter'} is already taken") from None
    for path in out["parts"]:
        note_ops.index_later(path)
    return out


@router.post("/file/ocr")
def file_ocr(payload: dict):
    """The drawer's "OCR this" on a file Silica holds no text of (ADR-0129,
    amended 2026-11-08): one conversion job, as the vault's + makes, whose
    note lands beside the file and names it in `source_file`, so the row is
    a raw of a note, read, once the job is done. The configured PDF provider
    reads it: on a laptop set to mineru, mineru's OCR."""
    from silica.sources.media_admission import MediaRefused
    from silica.ui.web import jobs

    found = _vault_file(str(payload.get("path") or ""))
    try:
        return jobs.record(_rel(found), origin="ocr")
    except MediaRefused as exc:  # a recording over the length cap: its text is the person's sentence
        raise Refused(400, exc) from None


@router.get("/vectors")
def vectors_status():
    """The page vectors' build as the rail shows it: on page load and on each
    reconnect of the stream that carries its steps. Always `idle` on the
    server, whose tenants build none here (ADR-0156, amended 2026-11-08)."""
    return vectors.status()


@router.post("/vectors/refresh")
def vectors_refresh():
    """The rail's "Try again" after a build that failed."""
    return {"started": vectors.refresh()}
