# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A long document's routes (ADR-0158 points 2, 3 and 11): what a selection
would read, the start that reads and plans, the outline the person sends back
to write, the replan after a gap was filled, and the stop.

Start, write and replan spend model calls, so each asks for the hosted model;
the count and the listing read the vault only.
"""
from __future__ import annotations

from fastapi import APIRouter

from silica.sources import long_document
from silica.ui.web.failures import Refused

router = APIRouter(prefix="/document")


def _hosted_model_only() -> None:
    """ADR-0158 point 6: the local model gets no long documents until a probe
    on it passes (Open 2); local as the deep research judges it."""
    from silica.ui.web.research_routes import local_model

    if local_model():
        raise Refused(409, "a long document is written on the hosted model; the local one has not been measured on it")


@router.get("")
def list_documents():
    """This vault's documents and the types the form offers."""
    return {"documents": long_document.listing(), "types": long_document.types()}


@router.post("/count")
def count_selection(payload: dict):
    """What Read would read of a selection: no model call."""
    return long_document.count((payload or {}).get("selection"))


@router.post("/start")
def start_document(payload: dict):
    """Read the selection and plan; the job then waits for the person."""
    _hosted_model_only()
    body = payload or {}
    return long_document.start(str(body.get("type") or ""), body.get("selection"), body.get("params"))


@router.post("/{job_id}/write")
def write_document(job_id: str, payload: dict):
    """The outline as the person kept it: the spend gate for the writing."""
    _hosted_model_only()
    return long_document.write(job_id, payload)


@router.post("/{job_id}/replan")
def replan_document(job_id: str):
    """Read what is new or changed in the selection, then plan again (point 11)."""
    _hosted_model_only()
    return long_document.replan(job_id)


@router.post("/{job_id}/stop")
def stop_document(job_id: str):
    return long_document.stop(job_id)


@router.post("/{job_id}/dismiss")
def dismiss_document(job_id: str):
    long_document.dismiss(job_id)
    return {"ok": True}
