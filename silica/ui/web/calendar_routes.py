# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Calendar view's reads and writes, and the reminders the page polls."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from silica.config import CONFIG

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/calendar")
def calendar(start: str = "", days: int = 7):
    """The 4-axis agenda days for the calendar tab — one endpoint, one build
    (the /shape pattern), plus the one reading only a calendar can carry.

    `bursting` (V6) is what the last fortnight of WRITING turned out to be
    about. It is here and not on explore because it is the only one of the
    seven variables with no position in the graph at all: its axis is time,
    which is this tab's axis. Read from the cheap pass (~0.3 s) rather than
    from the report's co-occurrence depth (9 s cold) — both call the same
    `signals.burst` over the same inputs, and tests/test_graph_variables.py
    holds them equal.
    """
    from silica.kernel.report.structure import bursting
    from silica.tools.events import silica_agenda

    out = silica_agenda(start=start or "today", days=max(1, min(90, days)))
    try:
        out["bursting"] = bursting()
    except Exception:
        # A reading, never the tab: an index that cannot answer costs the strip
        # and leaves every scheduled day exactly where it was.
        logger.debug("calendar: burst unavailable", exc_info=True)
    return out


@router.get("/calendar/event/path")
def calendar_event_path(title: str = "", start: str = "", rrule: str = ""):
    """The note the quick card would write, named before it is saved."""
    from silica.kernel.calendar.write import event_note_path

    return event_note_path(title, start, rrule)


@router.post("/calendar/event")
def calendar_event_create(payload: dict):
    """The quick card's Save (ADR-0118 point 1): the same writer as the chat's
    `silica_event_create`, so the calendar is a second door and not a second
    implementation of the event note."""
    from silica.kernel.calendar.write import create_event

    f = {k: str(payload.get(k) or "") for k in
         ("title", "start", "end", "rrule", "reminder", "body", "plan")}
    return create_event(f.pop("title"), f.pop("start"), **f, person=True)


@router.post("/calendar/event/status")
def calendar_event_status(payload: dict):
    """The event card's Done and Cancel (ADR-0118 point 6). Open is not a
    status a card can set: `silica_event_update` keeps a field it is passed
    empty, so a closed series is reopened in its note."""
    from silica.tools.events import silica_event_update

    status = str(payload.get("status") or "")
    if status not in ("done", "cancelled"):
        return {"error": "status must be done or cancelled"}
    return silica_event_update(note=str(payload.get("path") or ""), status=status)


@router.post("/reminders")
def reminders_poll():
    """The front-end poll IS the reminder tick: compute due, advance the
    high-water marks, return the list. POST, not GET — the poll mutates the
    sidecar, and a cacheable GET risks a stale 200 swallowing a delivery.
    At-most-once: the mark advances on delivery; REPL and GUI share the
    sidecar, so whichever surface polls first delivers."""
    import datetime as _dt
    from pathlib import Path as _Path

    from silica.kernel.calendar.model import scan_events
    from silica.kernel.calendar.reminders import (
        advance_marks, delivery_lock, due_reminders, load_marks, save_marks,
    )

    vault = _Path(CONFIG.vault_path)
    events = scan_events(vault)
    with delivery_lock(vault):
        marks = load_marks(vault)
        due = due_reminders(events, marks, _dt.datetime.now())
        if due:
            save_marks(vault, advance_marks(marks, due))
    return {"due": [{"stem": r["stem"], "title": r["title"],
                     "start": r["start"].isoformat(sep=" "), "late": r["late"]}
                    for r in due]}
