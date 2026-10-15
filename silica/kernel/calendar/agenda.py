# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Pure 5-axis day merge over injected inputs — testable without a vault.

The axes are asymmetric by nature and this contract states it: Occurrences
land on every day they cover; dated notes (timeline rows) and `log.md`
lines land on their own dates; review-due rows land ONLY on today's row —
`review_queue()` carries no date dimension ("due" means R is below
threshold NOW), and projecting R-decay crossing dates forward would be
stale after the first quiz. Event notes are excluded from the dated-notes
axis so a hand-written event carrying `date:` never appears twice.

Tasks (ADR-0062) share that asymmetry: an open task lands on its day only
while the day is ahead; once it has passed, or when a daily note's task has
no day at all, it is due NOW and lands on today's row. A day's daily note
heads its row and leaves the dated-notes list, for the reason event notes do.

A study plan lands on its `by` day and nowhere else: the deadline is the one
date a plan states, and spreading it over the days before would be a schedule
the reader never chose.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence

from silica.kernel.calendar.occurrences import Occurrence, days
from silica.kernel.calendar.tasks import Task

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


def _task_day(t: Task, today: dt.date) -> dt.date | None:
    """The one day a task lands on (ADR-0062 Decision 3), or None."""
    if t.status == "done":
        return t.done
    if t.status != "open":
        return None
    # Start is "not before", never "on": it places nothing (Tasks' `happens`
    # matches it, but a day the work may begin is not a day it is due).
    when = t.scheduled or t.due
    if when is not None:
        return max(when, today)
    return today if t.daily else None


def agenda(win_start: dt.date, win_end: dt.date, *,
           occurrences: list[Occurrence],
           timeline_rows: list[tuple[str, str, str]],
           log_lines: list[str],
           review_rows: list[dict],
           event_stems: set[str],
           today: dt.date,
           tasks: Sequence[Task] = (),
           dailies: dict[str, dt.date] | None = None,
           plans: Sequence[dict] = ()) -> list[dict]:
    """One DayRow per day of the half-open window [win_start, win_end).

    DayRow: {date, daily, events, notes, tasks, activity, review, plans}, all
    JSON-ready. `dailies` is the vault's daily notes, path -> day; `plans` is
    `plans.list_plans()`.
    """
    by_day: dict[str, dict] = {}
    d = win_start
    while d < win_end:
        by_day[d.isoformat()] = {"date": d.isoformat(), "daily": None, "events": [],
                                 "notes": [], "tasks": [], "activity": [], "review": [],
                                 "plans": []}
        d += dt.timedelta(days=1)

    daily_stems: set[str] = set()
    for path, day in sorted((dailies or {}).items()):
        stem = path.rsplit("/", 1)[-1].removesuffix(".md")
        daily_stems.add(stem)
        row = by_day.get(day.isoformat())
        if row is not None:
            row["daily"] = {"stem": stem, "path": path}

    for o in sorted(occurrences, key=lambda o: (not o.all_day, o.start)):
        for day in days(o):
            row = by_day.get(day.isoformat())
            if row is not None:
                row["events"].append(_event_dict(o))

    for date, label, stem in timeline_rows:
        if stem in event_stems or stem in daily_stems:
            continue
        row = by_day.get(str(date)[:10])
        if row is not None:
            row["notes"].append({"label": label, "stem": stem})

    for t in tasks:
        tday = _task_day(t, today)
        row = by_day.get(tday.isoformat()) if tday else None
        if row is not None:
            when = t.scheduled or t.due
            row["tasks"].append({"text": t.text, "path": t.path, "line": t.line,
                                 "status": t.status,
                                 "due": t.due.isoformat() if t.due else "",
                                 "overdue": t.status == "open" and when is not None
                                            and when < today})

    for p in plans:
        row = by_day.get(str(p.get("by") or "")[:10])
        if row is not None:
            row["plans"].append({"name": p["name"], "path": p["path"], "status": p["status"]})

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
