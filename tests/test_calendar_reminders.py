"""Idempotent reminder delivery via a stem-keyed high-water sidecar.

The contract: due = occurrences past their fire moment (start - lead) and
above the mark; a note with no mark reads as mark = -inf, with all missed
occurrences COLLAPSED to one late notice (the most recent past one) — a
daily closed for a month must not spam 30 notices. `late` means the fire
moment was missed by more than a small grace, so an `0m` reminder caught on
the next tick still reads as on-time. Closed series never remind. The
sidecar write is atomic (tmp + rename).
"""
from __future__ import annotations

import datetime as dt
import json

from silica.kernel.calendar.model import parse_event
from silica.kernel.calendar.reminders import (
    advance_marks,
    due_reminders,
    load_marks,
    marks_path,
    save_marks,
)

D = dt.datetime


def _ev(stem="gym", **fm):
    return parse_event(fm, stem=stem, path=f"{stem}.md")


DAILY = dict(event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY",
             event_reminder="30m")


# --- on-time path ------------------------------------------------------------

def test_due_inside_lead_window_not_before():
    e = _ev(**DAILY)
    assert due_reminders([e], {}, D(2026, 8, 3, 8, 15)) == []  # 45m early
    [r] = due_reminders([e], {"gym": "2026-08-02T09:00:00"}, D(2026, 8, 3, 8, 45))
    assert r["stem"] == "gym" and r["start"] == D(2026, 8, 3, 9, 0)
    assert r["late"] is False


def test_idempotent_after_mark_advance():
    e = _ev(**DAILY)
    now = D(2026, 8, 3, 8, 45)
    marks = {"gym": "2026-08-02T09:00:00"}
    due = due_reminders([e], marks, now)
    marks = advance_marks(marks, due)
    assert due_reminders([e], marks, now) == []


# --- missed-while-closed recovery --------------------------------------------

def test_no_mark_collapses_a_month_of_misses_to_one_late_notice():
    e = _ev(**DAILY)
    now = D(2026, 9, 3, 12, 0)  # a month later, far from the next fire moment
    due = due_reminders([e], {}, now)
    assert len(due) == 1
    assert due[0]["start"] == D(2026, 9, 3, 9, 0)  # the most recent missed one
    assert due[0]["late"] is True


def test_late_and_upcoming_can_both_fire_then_nothing():
    e = _ev(**DAILY)
    now = D(2026, 8, 5, 8, 45)  # yesterday missed AND today's window open
    due = due_reminders([e], {"gym": "2026-08-03T09:00:00"}, now)
    assert [(r["start"], r["late"]) for r in due] == [
        (D(2026, 8, 4, 9, 0), True), (D(2026, 8, 5, 9, 0), False)]
    marks = advance_marks({"gym": "2026-08-03T09:00:00"}, due)
    assert marks["gym"] == "2026-08-05T09:00:00"  # max delivered start
    assert due_reminders([e], marks, now) == []


def test_one_shot_expired_delivers_exactly_one_late():
    e = _ev(stem="call", event_start="2026-08-10 15:00", event_reminder="1d")
    due = due_reminders([e], {}, D(2026, 8, 12, 10, 0))
    assert [(r["start"], r["late"]) for r in due] == [(D(2026, 8, 10, 15, 0), True)]


# --- grace: an 0m reminder caught next tick is on-time -----------------------

def test_grace_keeps_at_start_reminders_on_time():
    e = _ev(stem="pill", event_start="2026-08-10 15:00", event_reminder="0m")
    [r] = due_reminders([e], {}, D(2026, 8, 10, 15, 1))  # one tick after start
    assert r["late"] is False
    [r2] = due_reminders([e], {}, D(2026, 8, 10, 15, 10))
    assert r2["late"] is True


# --- exclusions --------------------------------------------------------------

def test_closed_series_and_reminderless_events_never_remind():
    done = _ev(stem="old", event_status="done", **DAILY)
    silent = _ev(stem="quiet", event_start="2026-08-03 09:00", event_rrule="FREQ=DAILY")
    assert due_reminders([done, silent], {}, D(2026, 9, 1, 12, 0)) == []


def test_mark_is_keyed_by_stem_not_path():
    # the same stem after a folder move keeps its mark
    moved = parse_event(dict(DAILY), stem="gym", path="archive/2026/gym.md")
    marks = {"gym": "2026-09-03T09:00:00"}
    assert due_reminders([moved], marks, D(2026, 9, 3, 12, 0)) == []


# --- sidecar -----------------------------------------------------------------

def test_sidecar_roundtrip_atomic_and_created_on_demand(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert load_marks(vault) == {}  # missing file is an empty mark set
    save_marks(vault, {"gym": "2026-08-05T09:00:00"})
    assert load_marks(vault) == {"gym": "2026-08-05T09:00:00"}
    p = marks_path(vault)
    assert p.parent.name == ".silica"
    assert json.loads(p.read_text(encoding="utf-8")) == {"gym": "2026-08-05T09:00:00"}
    assert list(p.parent.glob("*.tmp")) == []  # no torn write left behind


def test_corrupt_sidecar_reads_as_empty(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".silica").mkdir(parents=True)
    marks_path(vault).write_text("{not json", encoding="utf-8")
    assert load_marks(vault) == {}
