# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Contradictions set side by side and settled by the person (ADR-0166): the
vault menu's count, the drawer's band, and the one write behind its four buttons.
Every account has them: the record lives on the note, and so does the band."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from silica.config import CONFIG
from silica.kernel.recall.resolve import note_resolver
from silica.ui.web import note_ops
from silica.ui.web.failures import Refused

router = APIRouter()


@router.get("/contradictions")
def contradictions():
    """The open records and the checks' proposals, for the vault menu. No model:
    the records are read off the notes, the proposals out of `.silica/checks/`."""
    from silica.kernel.check import contradictions as cx

    return {"open": cx.open_records(), "proposed": cx.proposals(CONFIG.vault_path)}


@router.get("/contradictions/note")
async def contradictions_note(path: str = ""):
    """One note's records and proposals, each side with the line it stands on."""
    from silica.kernel.check import contradictions as cx

    canon = await asyncio.to_thread(lambda: note_resolver()(path))
    if not canon:
        raise HTTPException(404, "note not found in vault")
    return await asyncio.to_thread(cx.note_view, CONFIG.vault_path, canon if canon.endswith(".md") else canon + ".md")


@router.post("/contradictions/resolve")
def contradictions_resolve(payload: dict):
    """`{path, holds, ref}` settles a record, `{path, holds, at, line}` a
    proposal; `condition` is what "both hold" says. One run of the person's,
    whose `run` the page's Undo hands to /tree/undo. No turn slot, as
    /note/save takes none: the person's own edit of their own note."""
    from silica.kernel.check import contradictions as cx

    canon = note_resolver()(str(payload.get("path") or ""))
    if not canon:
        raise HTTPException(404, "note not found in vault")
    path = canon if canon.endswith(".md") else canon + ".md"
    try:
        line = int(payload.get("line") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "line is a number") from None
    try:
        res = cx.resolve(CONFIG.vault_path, path, str(payload.get("holds") or ""), ref=str(payload.get("ref") or ""),
                         at=str(payload.get("at") or ""), line=line, condition=str(payload.get("condition") or ""))
    except ValueError as e:
        raise Refused(400, e)
    except LookupError as e:
        raise Refused(409, e)
    for p in res["paths"]:
        note_ops.index_later(p)
    return res
