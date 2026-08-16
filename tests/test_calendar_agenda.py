"""The 4-axis day merge, pure over injected inputs (no vault, no LLM).

The axes are asymmetric by contract: Occurrences land on any covered day,
dated notes and log lines on their own dates, review-due rows ONLY on
today's row (absent when today is outside the window). Event notes never
appear on the dated-notes axis.
"""
from __future__ import annotations

import datetime as dt

from silica.kernel.calendar.agenda import agenda
from silica.kernel.calendar.model import parse_event
from silica.kernel.calendar.occurrences import expand


def _occs(**fm):
    e = parse_event(fm, stem=fm.pop("_stem", "e"), path="e.md")
    return expand(e, dt.datetime(2026, 8, 1), dt.datetime(2026, 9, 1))


WIN = (dt.date(2026, 8, 17), dt.date(2026, 8, 24))  # half-open, 7 days
TODAY = dt.date(2026, 8, 19)


def _agenda(occurrences=(), timeline_rows=(), log_lines=(), review_rows=(),
            event_stems=frozenset(), today=TODAY):
    return agenda(*WIN, occurrences=list(occurrences),
                  timeline_rows=list(timeline_rows), log_lines=list(log_lines),
                  review_rows=list(review_rows), event_stems=set(event_stems),
                  today=today)


def test_one_row_per_day_of_the_window():
    rows = _agenda()
    assert [r["date"] for r in rows] == [
        f"2026-08-{d}" for d in ("17", "18", "19", "20", "21", "22", "23")]


def test_multi_day_occurrence_buckets_into_every_covered_day():
    occs = _occs(_stem="trip", event_start="2026-08-18", event_end="2026-08-20")
    rows = {r["date"]: r for r in _agenda(occurrences=occs)}
    for day in ("18", "19", "20"):
        assert [e["stem"] for e in rows[f"2026-08-{day}"]["events"]] == ["trip"]
    assert rows["2026-08-17"]["events"] == [] and rows["2026-08-21"]["events"] == []


def test_all_day_sorts_before_timed():
    occs = _occs(_stem="meet", event_start="2026-08-18 09:00") + \
           _occs(_stem="fair", event_start="2026-08-18")
    rows = {r["date"]: r for r in _agenda(occurrences=occs)}
    assert [e["stem"] for e in rows["2026-08-18"]["events"]] == ["fair", "meet"]


def test_event_stems_excluded_from_the_dated_notes_axis():
    tl = [("2026-08-18", "lecture", "lecture"), ("2026-08-18", "party", "party")]
    rows = {r["date"]: r for r in _agenda(timeline_rows=tl, event_stems={"party"})}
    assert [n["stem"] for n in rows["2026-08-18"]["notes"]] == ["lecture"]


def test_log_lines_bucket_by_date_prefix():
    lines = [
        "- 2026-08-18 · nucleate `a.md` → 3 new · run abc12345",
        "- 2026-08-25 · curate → 2 item · run def",  # outside the window
        "not a log line",
    ]
    rows = {r["date"]: r for r in _agenda(log_lines=lines)}
    assert rows["2026-08-18"]["activity"] == ["nucleate `a.md` → 3 new · run abc12345"]
    assert all(r["activity"] == [] for d, r in rows.items() if d != "2026-08-18")


def test_review_rows_land_only_on_today():
    review = [{"path": "a.md", "R": 0.1, "why": "due"}]
    rows = {r["date"]: r for r in _agenda(review_rows=review)}
    assert rows["2026-08-19"]["review"] == review
    assert all(r["review"] == [] for d, r in rows.items() if d != "2026-08-19")


def test_review_absent_when_today_outside_window():
    review = [{"path": "a.md", "R": 0.1, "why": "due"}]
    rows = _agenda(review_rows=review, today=dt.date(2026, 9, 9))
    assert all(r["review"] == [] for r in rows)


def test_event_dicts_are_json_ready():
    occs = _occs(_stem="meet", event_start="2026-08-18 09:00",
                 event_end="2026-08-18 10:00")
    rows = {r["date"]: r for r in _agenda(occurrences=occs)}
    [e] = rows["2026-08-18"]["events"]
    assert e == {"stem": "meet", "title": "meet", "path": "e.md",
                 "start": "2026-08-18 09:00", "end": "2026-08-18 10:00",
                 "all_day": False, "status": ""}
