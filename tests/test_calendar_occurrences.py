"""Window expansion: half-open [a, b), duration inheritance, day coverage.

The contract: an Occurrence is in the window iff its [start, end) span
INTERSECTS it (a trip that started before the window still shows on covered
days); the window is half-open (at win_start kept, at win_end excluded);
compute is capped at 1000 occurrences per window (FREQ=SECONDLY guard);
closed series suppress occurrences with start >= now, never the past.
"""
from __future__ import annotations

import datetime as dt

from silica.kernel.calendar.model import parse_event
from silica.kernel.calendar.occurrences import CAP, days, expand, next_occurrence


def _ev(**fm):
    return parse_event(fm, stem="e", path="e.md")


D = dt.datetime


# --- window membership -------------------------------------------------------

def test_one_shot_inside_window_appears_outside_does_not():
    e = _ev(event_start="2026-08-20 15:00")
    assert len(expand(e, D(2026, 8, 17), D(2026, 8, 24))) == 1
    assert expand(e, D(2026, 8, 24), D(2026, 8, 31)) == []


def test_window_is_half_open():
    at_start = _ev(event_start="2026-08-17 00:00")
    at_end = _ev(event_start="2026-08-24 00:00")
    win = (D(2026, 8, 17), D(2026, 8, 24))
    assert len(expand(at_start, *win)) == 1   # exactly at win_start: kept
    assert expand(at_end, *win) == []          # exactly at win_end: excluded


def test_multi_day_started_before_window_intersects():
    e = _ev(event_start="2026-08-10", event_end="2026-08-19")  # all-day trip
    got = expand(e, D(2026, 8, 17), D(2026, 8, 24))
    assert len(got) == 1


def test_ended_exactly_at_window_start_is_out():
    e = _ev(event_start="2026-08-16 22:00", event_end="2026-08-17 00:00")
    assert expand(e, D(2026, 8, 17), D(2026, 8, 24)) == []


# --- recurrence --------------------------------------------------------------

def test_weekly_byday_with_duration_inheritance():
    e = _ev(event_start="2026-08-05 15:00", event_end="2026-08-05 16:00",
            event_rrule="FREQ=WEEKLY;BYDAY=WE")
    got = expand(e, D(2026, 8, 17), D(2026, 8, 31))
    assert [o.start for o in got] == [D(2026, 8, 19, 15, 0), D(2026, 8, 26, 15, 0)]
    assert all(o.end - o.start == dt.timedelta(hours=1) for o in got)


def test_count_and_until_are_respected():
    e = _ev(event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY;COUNT=3")
    assert len(expand(e, D(2026, 8, 1), D(2026, 9, 1))) == 3
    e2 = _ev(event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY;UNTIL=20260805T090000")
    assert len(expand(e2, D(2026, 8, 1), D(2026, 9, 1))) == 3


def test_monthly_on_the_31st_skips_short_months():
    e = _ev(event_start="2026-01-31 10:00", event_rrule="FREQ=MONTHLY")
    got = expand(e, D(2026, 1, 1), D(2026, 5, 1))
    assert [o.start.date() for o in got] == [dt.date(2026, 1, 31), dt.date(2026, 3, 31)]


def test_wall_clock_stable_across_dst():
    # Europe DST springs forward 2026-03-29; naive datetimes must not care
    e = _ev(event_start="2026-03-27 09:00", event_rrule="FREQ=DAILY")
    got = expand(e, D(2026, 3, 27), D(2026, 4, 1))
    assert len(got) == 5
    assert all(o.start.hour == 9 and o.start.minute == 0 for o in got)


def test_cap_bounds_a_pathological_rule():
    e = _ev(event_start="2026-08-01 00:00", event_rrule="FREQ=MINUTELY")
    got = expand(e, D(2026, 8, 1), D(2026, 9, 1))
    assert len(got) == CAP


# --- closed series -----------------------------------------------------------

def test_closed_series_suppresses_start_at_or_after_now():
    e = _ev(event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY",
            event_status="done")
    now = D(2026, 8, 5, 9, 0)  # equal counts as suppressed (start >= now)
    got = expand(e, D(2026, 8, 1), D(2026, 9, 1), now=now)
    assert [o.start.day for o in got] == [3, 4]
    assert all(o.status == "done" for o in got)


def test_open_series_ignores_now():
    e = _ev(event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY;COUNT=5")
    got = expand(e, D(2026, 8, 1), D(2026, 9, 1), now=D(2026, 8, 4))
    assert len(got) == 5


# --- next_occurrence ---------------------------------------------------------

def test_next_occurrence_strictly_after():
    e = _ev(event_start="2026-08-05 15:00", event_rrule="FREQ=WEEKLY")
    assert next_occurrence(e, D(2026, 8, 5, 15, 0)) == D(2026, 8, 12, 15, 0)
    one = _ev(event_start="2026-08-20 15:00")
    assert next_occurrence(one, D(2026, 8, 1)) == D(2026, 8, 20, 15, 0)
    assert next_occurrence(one, D(2026, 8, 20, 15, 0)) is None


def test_next_occurrence_exhausted_series():
    e = _ev(event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY;COUNT=2")
    assert next_occurrence(e, D(2026, 9, 1)) is None


# --- day coverage (one rule for timed and all-day) ---------------------------

def _occ(start_fm, end_fm=None):
    fm = {"event_start": start_fm}
    if end_fm:
        fm["event_end"] = end_fm
    [o] = expand(_ev(**fm), D(2026, 1, 1), D(2027, 1, 1))
    return o


def test_all_day_span_covers_inclusive_days():
    o = _occ("2026-08-20", "2026-08-22")
    assert days(o) == [dt.date(2026, 8, 20), dt.date(2026, 8, 21), dt.date(2026, 8, 22)]


def test_timed_multi_day_covers_start_to_end_days():
    o = _occ("2026-08-20 22:00", "2026-08-22 01:00")
    assert days(o) == [dt.date(2026, 8, 20), dt.date(2026, 8, 21), dt.date(2026, 8, 22)]


def test_timed_end_exactly_at_midnight_does_not_occupy_end_day():
    o = _occ("2026-08-20 22:00", "2026-08-21 00:00")
    assert days(o) == [dt.date(2026, 8, 20)]


def test_instant_covers_its_own_day():
    o = _occ("2026-08-20 15:00")
    assert days(o) == [dt.date(2026, 8, 20)]


# --- BI occurrence flattener --------------------------------------------------

def test_occurrence_rows_window_contract(tmp_path):
    from silica.kernel.calendar.model import occurrence_rows
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "gym.md").write_text(
        "---\nevent_start: 2026-08-03 18:00\nevent_rrule: FREQ=DAILY;COUNT=10\n---\n",
        encoding="utf-8")
    rows = occurrence_rows(vault, D(2026, 8, 5), D(2026, 8, 8))
    assert [r["start"] for r in rows] == [
        "2026-08-05 18:00:00", "2026-08-06 18:00:00", "2026-08-07 18:00:00"]
    assert set(rows[0]) == {"stem", "title", "start", "end", "all_day", "status"}
