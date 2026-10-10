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


# --- ADR-0062: the day's daily note, and the tasks axis ---------------------

from silica.kernel.calendar.tasks import Task  # noqa: E402


def _t(text, **kw):
    return Task(path=kw.pop("path", "daily/2026-08-17.md"), line=kw.pop("line", 1),
                text=text, status=kw.pop("status", "open"), **kw)


def test_the_daily_note_heads_its_day_and_leaves_the_notes_list():
    rows = agenda(*WIN, occurrences=[], log_lines=[], review_rows=[], event_stems=set(),
                  timeline_rows=[("2026-08-18", "2026-08-18", "2026-08-18"),
                                 ("2026-08-18", "Meeting", "Meeting")],
                  dailies={"daily/2026-08-18.md": dt.date(2026, 8, 18)}, today=TODAY)
    day = {r["date"]: r for r in rows}["2026-08-18"]
    assert day["daily"] == {"stem": "2026-08-18", "path": "daily/2026-08-18.md"}
    assert day["notes"] == [{"label": "Meeting", "stem": "Meeting"}]
    assert {r["date"]: r for r in rows}["2026-08-17"]["daily"] is None


def _tasks_by_day(tasks, today=TODAY):
    rows = agenda(*WIN, occurrences=[], timeline_rows=[], log_lines=[], review_rows=[],
                  event_stems=set(), tasks=tasks, today=today)
    return {r["date"]: [t["text"] for t in r["tasks"]] for r in rows if r["tasks"]}


def test_an_open_task_lands_on_scheduled_else_due():
    D = dt.date
    got = _tasks_by_day([
        _t("due", due=D(2026, 8, 21)),
        _t("scheduled wins", scheduled=D(2026, 8, 20), due=D(2026, 8, 22)),
        _t("start places nothing", start=D(2026, 8, 20), daily=None),
    ])
    assert got == {"2026-08-20": ["scheduled wins"], "2026-08-21": ["due"]}


def test_overdue_and_undated_daily_tasks_land_on_today_only():
    D = dt.date
    got = _tasks_by_day([
        _t("overdue", due=D(2026, 8, 18)),
        _t("undated in a daily", daily=D(2026, 8, 17)),
        _t("undated elsewhere", path="Lecture.md"),
    ])
    assert got == {"2026-08-19": ["overdue", "undated in a daily"]}
    rows = agenda(*WIN, occurrences=[], timeline_rows=[], log_lines=[], review_rows=[],
                  event_stems=set(), tasks=[_t("overdue", due=D(2026, 8, 18))], today=TODAY)
    [row] = [t for r in rows for t in r["tasks"]]
    assert row["overdue"] is True and row["due"] == "2026-08-18"
    # Today outside the window: nothing to carry them on.
    assert _tasks_by_day([_t("overdue", due=D(2026, 8, 18))], today=dt.date(2026, 9, 1)) == {}


def test_done_lands_on_its_done_day_and_cancelled_nowhere():
    D = dt.date
    got = _tasks_by_day([
        _t("done", status="done", due=D(2026, 8, 25), done=D(2026, 8, 18)),
        _t("done undated", status="done"),
        _t("cancelled", status="cancelled", due=D(2026, 8, 20)),
    ])
    assert got == {"2026-08-18": ["done"]}


def test_a_task_row_carries_where_to_open_it():
    rows = agenda(*WIN, occurrences=[], timeline_rows=[], log_lines=[], review_rows=[],
                  event_stems=set(), tasks=[_t("x", line=7, due=dt.date(2026, 8, 20))],
                  today=TODAY)
    [row] = [t for r in rows for t in r["tasks"]]
    assert row == {"text": "x", "path": "daily/2026-08-17.md", "line": 7, "status": "open",
                   "due": "2026-08-20", "overdue": False}
