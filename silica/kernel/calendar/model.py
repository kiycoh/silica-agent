# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Event notes: parse, validate, scan (spec-calendar-events, ADR-0024).

A note IS an Event note iff its frontmatter has ``event_start``; the folder
is not load-bearing. Two deliberate time semantics live here:

- **Local wall-clock, no timezone.** All datetimes are naive; rrule over
  naive datetimes keeps a 09:00 recurring event at 09:00 across DST, which
  is what a personal calendar wants (ADR-0024). A timezone that sneaks in
  through YAML is stripped.
- **All-day ``event_end`` is INCLUSIVE** (`start: 2026-08-20`,
  `end: 2026-08-22` = 3 days), diverging from iCal's exclusive DTEND:
  frontmatter is read by humans. Internally the bound is normalized to the
  EXCLUSIVE midnight after, so day-coverage needs a single rule everywhere.

Read side is tolerant (a malformed note degrades or is skipped with a
warning, never crashes the agenda); ``validate_event`` is the strict write
side. PyYAML types ``event_start`` three ways for the same intent —
``2026-08-20`` is a date, ``2026-08-20 15:00`` a str (its resolver wants
seconds), ``15:00:00`` a datetime — all three normalize identically.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from silica.kernel.write import frontmatter

logger = logging.getLogger(__name__)

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEAD_RE = re.compile(r"^(\d+)([mhd])$")
_LEAD_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
_STATUS_VALUES = ("done", "cancelled")


@dataclass(frozen=True)
class Event:
    stem: str
    path: str                       # vault-relative
    title: str
    start: dt.datetime              # midnight for all-day
    end: dt.datetime | None         # EXCLUSIVE bound; None = timed instant
    all_day: bool
    rrule: str | None
    reminder: dt.timedelta | None   # lead time before each occurrence
    reminder_raw: str               # canonical lead string ("30m"), "" if none
    status: str                     # "" (open) | "done" | "cancelled"


def _parse_when(value) -> tuple[dt.datetime, bool] | None:
    """value -> (naive datetime, all_day), or None if unusable."""
    if isinstance(value, dt.datetime):
        return (value.replace(tzinfo=None) if value.tzinfo else value), False
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day), True
    s = str(value).strip()
    if _DATE_ONLY_RE.match(s):
        try:
            d = dt.date.fromisoformat(s)
        except ValueError:
            return None
        return dt.datetime(d.year, d.month, d.day), True
    try:
        parsed = dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return (parsed.replace(tzinfo=None) if parsed.tzinfo else parsed), False


def _parse_lead(value, *, strict: bool = False) -> dt.timedelta | None:
    s = str(value).strip()
    if not strict:
        s = s.casefold()
    m = _LEAD_RE.match(s)
    if not m:
        return None
    return dt.timedelta(**{_LEAD_UNITS[m.group(2)]: int(m.group(1))})


def _check_rrule(rule: str, dtstart: dt.datetime) -> str | None:
    """The rrulestr error message, or None if the rule is accepted."""
    from dateutil.rrule import rrulestr
    try:
        rrulestr(rule, dtstart=dtstart)
    except Exception as e:
        return str(e)
    return None


def parse_event(data: dict, *, stem: str, path: str) -> Event | None:
    """Tolerant read of one note's frontmatter. None = not an Event note, or
    an unusable one (malformed start) — every degrade logs a warning."""
    if not data or data.get("event_start") is None:
        return None
    when = _parse_when(data["event_start"])
    if when is None:
        logger.warning("calendar: %s skipped, event_start not parseable: %r",
                       path, data["event_start"])
        return None
    start, all_day = when

    end: dt.datetime | None = None
    raw_end = data.get("event_end")
    if raw_end is not None:
        end_when = _parse_when(raw_end)
        if end_when is None:
            logger.warning("calendar: %s event_end not parseable, dropped: %r", path, raw_end)
        elif all_day:
            end_date = end_when[0].date()
            if end_date < start.date():
                logger.warning("calendar: %s event_end before start, dropped", path)
            else:  # inclusive user date -> exclusive internal midnight
                end = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time())
        else:
            if end_when[0] < start:
                logger.warning("calendar: %s event_end before start, dropped", path)
            else:
                end = end_when[0]
    if all_day and end is None:
        end = start + dt.timedelta(days=1)

    rrule = str(data.get("event_rrule") or "").strip() or None
    if rrule:
        err = _check_rrule(rrule, start)
        if err:
            logger.warning("calendar: %s event_rrule rejected (%s), degrading to one-shot", path, err)
            rrule = None

    reminder = None
    reminder_raw = ""
    raw_rem = data.get("event_reminder")
    if raw_rem is not None:
        reminder = _parse_lead(raw_rem)
        if reminder is None:
            logger.warning("calendar: %s event_reminder invalid, dropped: %r", path, raw_rem)
        else:
            reminder_raw = str(raw_rem).strip().casefold()

    raw_status = str(data.get("event_status") or "").strip().casefold()
    status = raw_status if raw_status in _STATUS_VALUES else ""
    if raw_status and not status:
        logger.warning("calendar: %s event_status unknown, reading as open: %r", path, raw_status)

    title = str(data.get("title") or "").strip() or stem
    return Event(stem=stem, path=path, title=title, start=start, end=end,
                 all_day=all_day, rrule=rrule, reminder=reminder,
                 reminder_raw=reminder_raw, status=status)


def validate_event(data: dict) -> list[str]:
    """Strict write-side validation over frontmatter-shaped fields."""
    errors: list[str] = []
    raw = data.get("event_start")
    if raw is None:
        return ["event_start is required"]
    when = _parse_when(raw)
    if when is None:
        return [f"event_start not parseable: {raw!r}"]
    start, all_day = when

    raw_end = data.get("event_end")
    if raw_end is not None:
        end_when = _parse_when(raw_end)
        if end_when is None:
            errors.append(f"event_end not parseable: {raw_end!r}")
        elif all_day and end_when[0].date() < start.date():
            errors.append("event_end before event_start")
        elif not all_day and end_when[0] < start:
            errors.append("event_end before event_start")

    rrule = str(data.get("event_rrule") or "").strip()
    if rrule:
        err = _check_rrule(rrule, start)
        if err:
            errors.append(f"event_rrule rejected: {err}")

    raw_rem = data.get("event_reminder")
    if raw_rem is not None and _parse_lead(raw_rem, strict=True) is None:
        errors.append(f"event_reminder must be <N>m|h|d (lowercase): {raw_rem!r}")

    raw_status = data.get("event_status")
    if raw_status is not None:
        s = str(raw_status).strip().casefold()
        if s and s not in _STATUS_VALUES:
            errors.append(f"event_status must be done or cancelled: {raw_status!r}")
    return errors


_scan_memo: dict[str, tuple[str, list[Event]]] = {}  # vault -> (epoch, events)


def scan_events(vault: Path) -> list[Event]:
    """Every Event note of `vault`. Walk parity with `timeline._all_rows`
    (dot-parts, ignore_matcher, SOURCES_DIR); memoized on the vault's
    file-state epoch like the timeline walk."""
    from silica.kernel.recall.paths import SOURCES_DIR, ignore_matcher, vault_epoch

    epoch = vault_epoch(str(vault))
    if epoch:
        hit = _scan_memo.get(str(vault))
        if hit is not None and hit[0] == epoch:
            return hit[1]

    ignored = ignore_matcher(vault)
    events: list[Event] = []
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts):
            continue  # .obsidian, .trash, .silica
        if any(ignored(p) for p in parts[:-1]):
            continue  # .silicaignore / NOISE_DIRS
        if parts[0] == SOURCES_DIR:
            continue  # verbatim leaves
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable note must never take the agenda down
        data, _raw, _body = frontmatter.split(text)
        if not data or data.get("event_start") is None:
            continue
        ev = parse_event(data, stem=f.stem, path="/".join(parts))
        if ev:
            events.append(ev)

    if epoch:
        _scan_memo.clear()
        _scan_memo[str(vault)] = (epoch, events)
    return events


def _fmt_start(e: Event) -> str:
    return e.start.date().isoformat() if e.all_day else e.start.strftime("%Y-%m-%d %H:%M")


def _fmt_end(e: Event) -> str:
    """User-facing end: inclusive date for all-day, wall-clock for timed."""
    if e.end is None:
        return ""
    if e.all_day:
        return (e.end - dt.timedelta(days=1)).date().isoformat()
    return e.end.strftime("%Y-%m-%d %H:%M")


def event_rows(vault: Path) -> list[dict]:
    """BI flattener, one row per Event note. Stable column contract:
    stem, title, start, end, all_day, rrule, reminder, status, folder."""
    rows = []
    for e in scan_events(vault):
        parent = str(PurePosixPath(e.path).parent)
        rows.append({
            "stem": e.stem, "title": e.title,
            "start": _fmt_start(e), "end": _fmt_end(e),
            "all_day": e.all_day, "rrule": e.rrule or "",
            "reminder": e.reminder_raw, "status": e.status,
            "folder": "" if parent == "." else parent,
        })
    return rows


def occurrence_rows(vault: Path, win_start: dt.datetime, win_end: dt.datetime) -> list[dict]:
    """BI flattener, one row per materialized Occurrence in the half-open
    window [win_start, win_end). Columns: stem, title, start, end, all_day,
    status."""
    from silica.kernel.calendar.occurrences import expand

    rows = []
    for e in scan_events(vault):
        for occ in expand(e, win_start, win_end):
            rows.append({
                "stem": occ.stem, "title": occ.title,
                "start": occ.start.isoformat(sep=" "),
                "end": occ.end.isoformat(sep=" ") if occ.end else "",
                "all_day": occ.all_day, "status": occ.status,
            })
    return rows
