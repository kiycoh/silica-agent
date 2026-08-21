# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Idempotent reminder delivery: stem-keyed high-water marks in a sidecar.

Delivery state is machine state, not knowledge: it lives in
`.silica/calendar_notified.json` (note stem -> ISO start of the last
occurrence already notified), never in frontmatter — stamping the note
would churn mtime/git/undo and fight the note open in Obsidian. Keyed by
STEM because that is the identifier the vault resolves notes by, so a
folder move keeps the mark; a rename resets it, which costs at most one
collapsed late notice.

A note with no mark reads as mark = -inf. Missed occurrences collapse to
ONE late notice (the most recent past one) — found via rrule.before(), not
enumeration, so a daily closed for a year costs O(1), not a 365-item walk.
`late` means the event started more than `grace` ago: an `0m` (at-start)
reminder caught on the next tick still reads as on-time.

At-most-once across surfaces: REPL and GUI share the sidecar; whichever
tick fires first advances the mark. Ticks serialize on `delivery_lock`
(advisory fcntl on a sibling .lock file), so two surfaces polling inside
one window cannot both read the pre-advance marks and double-deliver.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
from pathlib import Path

from silica.kernel.calendar.model import Event
from silica.kernel.calendar.occurrences import next_occurrence

logger = logging.getLogger(__name__)

GRACE = dt.timedelta(minutes=2)


def _last_at_or_before(event: Event, now: dt.datetime) -> dt.datetime | None:
    from dateutil.rrule import rrulestr

    if event.rrule:
        return rrulestr(event.rrule, dtstart=event.start).before(now, inc=True)
    return event.start if event.start <= now else None


def due_reminders(events: list[Event], marks: dict[str, str],
                  now: dt.datetime, grace: dt.timedelta = GRACE) -> list[dict]:
    """Reminders to deliver now: [{stem, title, start, late}].

    Per event, at most one collapsed late notice (most recent occurrence
    with start <= now, above the mark) plus at most one on-time notice (the
    next occurrence whose fire moment start - lead has arrived).
    """
    out: list[dict] = []
    for e in events:
        if e.reminder is None or e.status:
            continue  # no lead, or the series is closed
        mark = marks.get(e.stem)
        mark_dt = dt.datetime.fromisoformat(mark) if mark else None

        last_past = _last_at_or_before(e, now)
        if last_past is not None and (mark_dt is None or last_past > mark_dt):
            out.append({"stem": e.stem, "title": e.title, "start": last_past,
                        "late": last_past < now - grace})

        nxt = next_occurrence(e, now)
        if (nxt is not None and nxt - e.reminder <= now
                and (mark_dt is None or nxt > mark_dt)):
            out.append({"stem": e.stem, "title": e.title, "start": nxt,
                        "late": False})
    return out


def advance_marks(marks: dict[str, str], delivered: list[dict]) -> dict[str, str]:
    """New mark set with every delivered start folded in (max per stem)."""
    new = dict(marks)
    for r in delivered:
        iso = r["start"].isoformat()
        prev = new.get(r["stem"])
        if prev is None or iso > prev:  # fixed-format ISO: lexical = chronological
            new[r["stem"]] = iso
    return new


def marks_path(vault: Path) -> Path:
    return Path(vault) / ".silica" / "calendar_notified.json"


@contextlib.contextmanager
def delivery_lock(vault: Path):
    """Serialize one load-compute-save tick across processes (REPL + GUI).

    Advisory fcntl lock on a sibling .lock file, blocking: the window is a
    few ms, so waiting beats a skipped delivery. Platforms without fcntl
    (Windows) degrade to the historical unlocked behavior, whose worst case
    is one duplicate notice.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    p = marks_path(vault).with_suffix(".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_marks(vault: Path) -> dict[str, str]:
    p = marks_path(vault)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("calendar: sidecar %s unreadable (%s), starting empty", p, e)
        return {}


def save_marks(vault: Path, marks: dict[str, str]) -> None:
    """Atomic write: tmp + rename, so a crash never leaves a torn sidecar."""
    p = marks_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps(marks, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)
