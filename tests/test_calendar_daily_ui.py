# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0062 in the calendar tab: the day's daily note is its head, and a task
row opens its note at its line. The drawing itself is verified by opening the
app; this pins the wiring between the payload and the views."""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"


def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start)]


def test_the_grid_cell_opens_its_daily_note_as_a_triangle():
    cal = _src("app-calendar.js")
    tri = _fn(cal, "calDailyBtn")
    assert '"cal-daily"' in tri and "openNote(daily.path)" in tri and "stopPropagation" in tri
    # the month cell and the week's day head both carry it (ADR-0118)
    assert "calDailyBtn(daily)" in _fn(cal, "calBuildWeek")
    assert "calDailyBtn(row.daily)" in _fn(cal, "calDayHead")
    assert ".cal-daily" in _src("app-work.css")


def test_the_cell_counts_the_days_tasks():
    derived = _fn(_src("app-calendar.js"), "calDerived")
    assert "row.tasks" in derived and "to do" in derived and "overdue" in derived


def test_the_agenda_heads_the_day_with_its_daily_note_and_lists_its_tasks():
    day = _fn(_src("app-calendar.js"), "calAgendaDay")
    assert 'axis("daily"' in day and 'axis("task"' in day
    assert day.index('axis("daily"') < day.index('axis("task"') < day.index('axis("note"')
    assert "openNote(t.path, t.line)" in day


def test_a_note_opens_at_a_line():
    notes = _src("app-notes.js")
    assert "async function openNote(path, line, facts)" in notes
    assert "ed.revealLine(line)" in _fn(notes, "openNote")
    editor = _src("app-editor.js")
    assert "revealLine(n)" in editor and "scrollIntoView" in editor[editor.index("revealLine(n)"):]
