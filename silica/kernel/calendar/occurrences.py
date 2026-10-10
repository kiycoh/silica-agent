# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Occurrence expansion over a half-open window [win_start, win_end).

Membership is INTERSECTION of the occurrence span [start, end) with the
window, not start-in-window: a multi-day trip that began before the window
must still show on the days it covers. The recurrence iterator therefore
starts at win_start minus the event's duration, and compute is capped at
CAP occurrences per window so FREQ=SECONDLY cannot hang the agenda.

Day coverage is one rule for timed and all-day alike (the model normalizes
the all-day inclusive end to an exclusive midnight): an occurrence covers
every day whose midnight is < end, starting from its start day — so an end
exactly at midnight occupies no extra day, and an instant covers its day.
"""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass

from silica.kernel.calendar.model import Event

CAP = 1000  # occurrences per window; guards pathological rules


@dataclass(frozen=True)
class Occurrence:
    stem: str
    title: str
    path: str                # vault-relative, for the note-open click
    start: dt.datetime
    end: dt.datetime | None  # exclusive bound; None = instant
    all_day: bool
    status: str


def _duration(event: Event) -> dt.timedelta:
    return (event.end - event.start) if event.end else dt.timedelta(0)


def expand(event: Event, win_start: dt.datetime, win_end: dt.datetime,
           now: dt.datetime | None = None) -> list[Occurrence]:
    """Occurrences of `event` intersecting [win_start, win_end).

    A closed series (`event.status`) suppresses occurrences with
    start >= now when `now` is given; the past always renders.
    """
    from dateutil.rrule import rrulestr

    dur = _duration(event)
    if event.rrule:
        rr = rrulestr(event.rrule, dtstart=event.start)
        starts = []
        for s in itertools.islice(rr.xafter(win_start - dur, inc=True), CAP):
            if s >= win_end:
                break
            starts.append(s)
    else:
        starts = [event.start]

    out: list[Occurrence] = []
    for s in starts:
        end = s + dur if event.end else None
        eff_end = end if end and end > s else s
        if not (s < win_end and (eff_end > win_start or s >= win_start)):
            continue
        if event.status and now is not None and s >= now:
            continue
        out.append(Occurrence(stem=event.stem, title=event.title, path=event.path,
                              start=s, end=end, all_day=event.all_day,
                              status=event.status))
    return out


def next_occurrence(event: Event, after: dt.datetime) -> dt.datetime | None:
    """First occurrence start strictly after `after`; None when exhausted."""
    from dateutil.rrule import rrulestr

    if event.rrule:
        return rrulestr(event.rrule, dtstart=event.start).after(after)
    return event.start if event.start > after else None


def days(occ: Occurrence) -> list[dt.date]:
    """Every calendar day the occurrence covers (see module docstring)."""
    if occ.end is None or occ.end <= occ.start:
        return [occ.start.date()]
    out = []
    d = occ.start.date()
    while dt.datetime.combine(d, dt.time()) < occ.end:
        out.append(d)
        d += dt.timedelta(days=1)
    return out
