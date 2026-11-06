# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Jobs panel's routes (ADR-0099) and the turns a read file offers (ADR-0121)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from silica.config import CONFIG
from silica.kernel import vault_files
from silica.sources import drop_turn
from silica.ui.web import jobs, session, sse
from silica.ui.web.session import SESSION

router = APIRouter(prefix="/jobs")


def _refused(exc: jobs.Refused) -> HTTPException:
    return HTTPException(409, str(exc.args[0]))


@router.get("")
def list_jobs():
    """What the browser asks for on load and on every reconnect of the
    narration stream (point 5): an event it missed is read back here."""
    return {"jobs": jobs.listing()}


@router.get("/running")
def running_jobs():
    """What a restart would lose (point 8), for the operator's release drain:
    the turn slot and the jobs not yet ended. It reads this process's state
    only and probes nothing remote, unlike GET /settings, which asks four
    endpoints for their models. No file names: the operator sees counts."""
    vault = CONFIG.vault_path or ""
    rows = [{"id": r["id"], "state": r["state"], "kind": r.get("kind", "convert")}
            for r in jobs._all() if r.get("state") in ("queued", "running") and r.get("vault") == vault]
    return {"turn": bool(SESSION.busy), "jobs": rows}


@router.post("/{job_id}/retry")
def retry_job(job_id: str):
    try:
        return jobs.retry(job_id)
    except jobs.Refused as exc:
        raise _refused(exc) from None


@router.post("/{job_id}/dismiss")
def dismiss_job(job_id: str):
    try:
        jobs.dismiss(job_id)
    except jobs.Refused as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.post("/{job_id}/turn")
def offered_turn(job_id: str, payload: dict | None = None):
    """The turn a read file offers (ADR-0121 points 1 and 6). The job never
    starts it: the page that holds the chat asks, for the drop's question as
    soon as its last file is done, or on a click.

    `as` is what the turn does: "ask" reruns the words typed with the drop,
    "summarize" and "quiz" compose their request, "nucleate" copies the files
    into the Inbox and nucleates the copies. `{"ask": true}` is the older
    spelling of "ask". A drop is answered once: a second ask, from another tab,
    is refused, which is what keeps two tabs from answering it twice."""
    body = payload or {}
    kind = str(body.get("as") or ("ask" if body.get("ask") else "nucleate"))
    if kind not in drop_turn.OFFERED_TURNS:
        raise HTTPException(400, "a read file offers ask, summarize, quiz or nucleate")
    # The slot first, then the record: a typed message that took this file in
    # (join_next) holds the slot while it marks the file answered.
    with session.turn_slot() as slot:
        rec = jobs.read(job_id) if jobs._ID.fullmatch(job_id or "") else None
        if rec is None or rec.get("state") != "done" or not rec.get("notes"):
            raise HTTPException(409, "this conversion has no note to offer")
        # A drop read from its PDF's text layer in its own turn is answered and
        # still offers the rest (ADR-0099 amendment 2026-10-20); one asked from
        # its row is dismissed too, which ends its row in every tab.
        if rec.get("dismissed") or (kind == "ask" and rec.get("answered")):
            raise HTTPException(409, "this file's turn was already asked")
        group = jobs._drop_of(rec)
        if any(g.get("state") != "done" for g in group):
            raise HTTPException(409, "the other files dropped with it are still being read")
        if kind == "ask" and not (rec.get("text") or "").strip():
            kind = "summarize"
        notes, files, words = jobs._answer(group)
        words, said = drop_turn.offered_ask(kind, words, ", ".join(f["name"] for f in files))
        if kind == "nucleate":
            # Copied into the Inbox first (ADR-0121 point 5): the injector reads
            # inbox files, and nucleating is the student adding them to the vault.
            # The one composed turn still offered the injector.
            inbox = vault_files.add_to_vault(notes, [f["source"] for f in files])
            return sse.turn_response(slot, drop_turn.compose_nucleate_turn(words, inbox, [], inline=False),
                                     files=files, said=said)
        return sse.turn_response(slot, drop_turn.compose_nucleate_turn(words, notes, []),
                                 withhold=drop_turn.WITHHELD_WITH_FILES, files=files, said=said)
