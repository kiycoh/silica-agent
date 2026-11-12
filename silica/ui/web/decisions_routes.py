# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Calendar's Decisions list (ADR-0167): the ledger read, and an outcome
added. Under /calendar, so it goes with the view wherever the view goes."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from silica.config import CONFIG
from silica.ui.web import note_ops
from silica.ui.web.failures import Refused

router = APIRouter()


@router.get("/calendar/decisions")
def decisions(which: str = "open"):
    """The decisions, due ones first; `which` is open, due or all."""
    from silica.kernel.calendar.decisions import scan

    if which not in ("open", "due", "all"):
        raise HTTPException(400, "which is open, due or all")
    return {"decisions": scan(Path(CONFIG.vault_path), which)}


@router.post("/calendar/decisions/outcome")
def decision_outcome(payload: dict):
    """`{path, text, status}`: one dated outcome, as /outcome writes it, with
    the run the page's Undo hands to /tree/undo."""
    from silica.kernel.calendar.decisions import outcome

    try:
        res = outcome(str(payload.get("path") or ""), str(payload.get("text") or ""),
                      str(payload.get("status") or "").lower())
    except ValueError as e:
        raise Refused(400, e)
    except (RuntimeError, OSError):
        raise HTTPException(404, "note not found in vault") from None
    note_ops.index_later(res["path"])
    return res
