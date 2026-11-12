# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A deep research's routes (ADR-0158 point 8): the plan, the approval that
starts the job, its stop, and the /nucleate it offers over the parts picked.

The plan lives in the page until it is approved: nothing is recorded before
the spend, so an abandoned form leaves nothing behind for a restart to
interrupt.
"""
from __future__ import annotations

import shlex

from fastapi import APIRouter

from silica.sources import deep_research
from silica.ui.web import session, sse, turn
from silica.ui.web.failures import Refused

router = APIRouter(prefix="/research")


def local_model() -> bool:
    """Whether the chat's calls go to a model on this machine, judged the way
    `silica doctor` does: a provider row that is a local server, or a custom
    endpoint here. A hosted provider's calls go to its own address whatever
    base URL is pinned beside it (llm.py's api_base), so that pin says nothing:
    reading it refused a deep research on OpenRouter (first live run)."""
    from silica.config import CONFIG
    from silica.onboarding.serve import is_local
    from silica.provider_table import PROVIDERS

    row = PROVIDERS.get(CONFIG.provider)
    return bool(row and row.fixed_key) or (
        CONFIG.provider == "custom" and is_local(CONFIG.provider_base_url or ""))


def _hosted_model_only() -> None:
    """ADR-0158 point 6: the local model gets no deep research until a probe
    on it passes (Open 2)."""
    if local_model():
        raise Refused(409, "a deep research runs on the hosted model; the local one has not been measured on it")


@router.get("")
def list_research():
    """This vault's research jobs, for the tray on load and on reconnect, and
    the depths the form offers, so the page and the caps cannot disagree."""
    return {"research": deep_research.listing(), "depths": deep_research.DEPTHS}


@router.post("/plan")
def plan_research(payload: dict):
    """One call writes the plan the person then edits; nothing is fetched."""
    _hosted_model_only()
    body = payload or {}
    return deep_research.plan(
        str(body.get("shape") or ""), str(body.get("question") or ""),
        note=str(body.get("note") or ""), depth=str(body.get("depth") or "standard"),
        scholarly=bool(body.get("scholarly")),
    )


@router.post("/start")
def start_research(payload: dict):
    """The approved plan: the spend gate. The job runs behind the chat."""
    _hosted_model_only()
    return deep_research.start((payload or {}).get("plan") or {})


@router.post("/{job_id}/stop")
def stop_research(job_id: str):
    return deep_research.stop(job_id)


@router.post("/{job_id}/dismiss")
def dismiss_research(job_id: str):
    deep_research.dismiss(job_id)
    return {"ok": True}


@router.get("/{job_id}/notes")
def admitted_notes(job_id: str):
    """The notes /nucleate wrote from the parts, for the study plan offer."""
    return {"notes": deep_research.admitted(job_id)}


@router.post("/{job_id}/nucleate")
def nucleate_parts(job_id: str, payload: dict | None = None):
    """The finished job's offer (point 8, Deliver): /nucleate over the parts
    the person ticked, as a chat turn. Their yes here is the step from the
    Inbox into the vault; the parent note is a list of links and is not sent."""
    parts = [str(p) for p in (payload or {}).get("parts") or []]
    with session.turn_slot() as slot:
        chosen = deep_research.mark_nucleated(job_id, parts)
        return sse.turn_response(slot, turn.Ask(shlex.join(["/nucleate", *chosen])))
