# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Pure 4-axis day merge over injected inputs — testable without a vault.

The axes are asymmetric by nature and this contract states it: Occurrences
land on every day they cover; dated notes (timeline rows) and `log.md`
lines land on their own dates; review-due rows land ONLY on today's row —
`review_queue()` carries no date dimension ("due" means R is below
threshold NOW), and projecting R-decay crossing dates forward would be
stale after the first quiz. Event notes are excluded from the dated-notes
axis so a hand-written event carrying `date:` never appears twice.
"""
from __future__ import annotations

import datetime as dt
import re

from silica.kernel.calendar.occurrences import Occurrence, days

_LOG_LINE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (.*)$")


def _event_dict(o: Occurrence) -> dict:
    if o.all_day:
        start = o.start.date().isoformat()
        end = (o.end - dt.timedelta(days=1)).date().isoformat() if o.end else ""
    else:
        start = o.start.strftime("%Y-%m-%d %H:%M")
        end = o.end.strftime("%Y-%m-%d %H:%M") if o.end else ""
    return {"stem": o.stem, "title": o.title, "path": o.path, "start": start,
            "end": end, "all_day": o.all_day, "status": o.status}


def agenda(win_start: dt.date, win_end: dt.date, *,
           occurrences: list[Occurrence],
           timeline_rows: list[tuple[str, str, str]],
           log_lines: list[str],
           review_rows: list[dict],
           event_stems: set[str],
           today: dt.date) -> list[dict]:
    """One DayRow per day of the half-open window [win_start, win_end).

    DayRow: {date, events, notes, activity, review} — all JSON-ready.
    """
    by_day: dict[str, dict] = {}
    d = win_start
    while d < win_end:
        by_day[d.isoformat()] = {"date": d.isoformat(), "events": [],
                                 "notes": [], "activity": [], "review": []}
        d += dt.timedelta(days=1)

    for o in sorted(occurrences, key=lambda o: (not o.all_day, o.start)):
        for day in days(o):
            row = by_day.get(day.isoformat())
            if row is not None:
                row["events"].append(_event_dict(o))

    for date, label, stem in timeline_rows:
        if stem in event_stems:
            continue
        row = by_day.get(str(date)[:10])
        if row is not None:
            row["notes"].append({"label": label, "stem": stem})

    for line in log_lines:
        m = _LOG_LINE_RE.match(line.strip())
        if not m:
            continue
        row = by_day.get(m.group(1))
        if row is not None:
            row["activity"].append(m.group(2))

    today_row = by_day.get(today.isoformat())
    if today_row is not None:
        today_row["review"] = list(review_rows)

    return list(by_day.values())
