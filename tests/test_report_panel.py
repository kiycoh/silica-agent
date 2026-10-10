# tests/test_report_panel.py
"""The report history is a STORE, and Metrics' Trend reads it.

Every other number the metrics view shows is derived from the report in front
of it. "Since the last report" is the one that cannot be: it needs a previous
reading, and until the store nothing kept one. The drawer's Report panel was
the first reader; ADR-0084 retired it with its only entry, and Trend remains.

These tests pin the three things that decide whether the delta is true:
what gets stored (only signals that mean the same at both report depths), when
a line is appended (movement, not "the tab was opened again"), and which
reading is handed back to diff against.
"""
from __future__ import annotations

from pathlib import Path


from silica.kernel.report.history import (
    SIGNALS,
    history_path,
    read_history,
    record_report,
    signals_of,
)

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
WORK_JS = WEB / "work.js"
from tests.webassets import app_js


def _sig(**over) -> dict[str, int]:
    base = dict.fromkeys(SIGNALS, 0)
    base.update(over)
    return base


# --- the store ---------------------------------------------------------------

def test_the_first_report_has_nothing_to_diff_against(tmp_path):
    """A delta invented against zero would read as if the whole vault had just
    been written in one go, which is the opposite of what the panel says."""
    assert record_report(tmp_path, _sig(notes=690, unresolved=566)) is None
    assert len(read_history(tmp_path)) == 1


def test_a_line_lands_only_when_something_moved(tmp_path):
    """Opening the metrics tab is not a data point. The view recomputes on every
    open, and a series that grew per open would make an untouched vault look
    busy and push the real previous reading out of reach."""
    a = _sig(notes=690, unresolved=566)
    record_report(tmp_path, a)
    record_report(tmp_path, a)
    record_report(tmp_path, a)
    assert len(read_history(tmp_path)) == 1

    record_report(tmp_path, _sig(notes=690, unresolved=532))
    assert len(read_history(tmp_path)) == 2


def test_the_previous_reading_is_the_last_DIFFERENT_one(tmp_path):
    """Not "the last line". Once a vault stops changing, the head equals the
    present, and diffing it against itself blanks the panel on every second
    open -- the reading a person wants ("532, was 566") has to survive until
    the vault actually moves again."""
    record_report(tmp_path, _sig(notes=690, unresolved=566))
    prev = record_report(tmp_path, _sig(notes=690, unresolved=532))
    assert prev["signals"]["unresolved"] == 566

    # same counts again: nothing appended, and 566 is still the answer
    again = record_report(tmp_path, _sig(notes=690, unresolved=532))
    assert again["signals"]["unresolved"] == 566
    assert len(read_history(tmp_path)) == 2


def test_a_torn_last_line_is_skipped_not_raised(tmp_path):
    """Same rule quiz.jsonl reads by: a half-written line costs the reading it
    held, never the whole series."""
    record_report(tmp_path, _sig(notes=690, unresolved=566))
    with history_path(tmp_path).open("ab") as fh:
        fh.write(b'{"at": "2026-08-2')
    assert len(read_history(tmp_path)) == 1
    assert record_report(tmp_path, _sig(notes=691, unresolved=560)) is not None


def test_only_depth_independent_signals_are_stored():
    """`integration_deficits` and the autolink family are zero unless the
    co-occurrence leg ran, so a structural report diffed against a full one
    would report 137 deficits "closed" for work nobody did. Same trap
    vault_energy documents for E(vault); the way out is to never store them."""
    totals = {"notes": 690, "unresolved": 532, "integration_deficits": 137,
              "autolink_candidates": 88, "missing_links": 40, "clusters": 210}
    sig = signals_of(totals, areas=90)
    assert "integration_deficits" not in sig
    assert "autolink_candidates" not in sig
    assert "missing_links" not in sig
    # and an area is a cluster of more than one note, which is why it is passed
    # in rather than read off totals["clusters"]
    assert sig["areas"] == 90 and "clusters" not in sig


# --- the wiring --------------------------------------------------------------

def test_the_two_bulk_turns_are_written_once():
    """The metrics view offers two bulk turns. A write prompt stated twice is two
    turns that can drift, and the one people would notice is the one that writes
    twenty notes. The drawer's report panel offered them a second time until
    ADR-0084 retired it with its only entry."""
    src = app_js()
    assert src.count("function bulkWritePrompt(") == 1
    assert src.count("function bulkAutolinkPrompt(") == 1
    assert src.count("most referenced ones that do not exist yet") == 1
    assert src.count("have nothing linking to them") == 1
    work = WORK_JS.read_text()
    assert "silica:report" not in work and "silica:report" not in src
    assert '"/metrics' not in work


# --- the endpoint ------------------------------------------------------------

def test_metrics_files_the_report_and_hands_back_yesterdays(tmp_vault):
    """End to end: the panel's whole reason to exist is a number the view in
    front of it cannot derive, so the seam that has to hold is /metrics filing
    this report and returning the last different one with it.

    The earlier reading is written straight into the store rather than produced
    by a second call, because compute_report memoises per vault epoch and the
    driver caches the file list: two calls in one test see the same vault, which
    is correct behaviour and useless for a delta.
    """
    from fastapi.testclient import TestClient

    from silica.config import CONFIG
    from silica.ui.web import server

    tmp_vault.note("A.md", "# A\n\nlinks to [[B]] and [[Ghost]]\n")
    tmp_vault.note("B.md", "# B\n\nback to [[A]]\n")
    yesterday = _sig(notes=1, unresolved=4, orphans=2)
    record_report(CONFIG.vault_path, yesterday, at="2026-08-06T09:00:00")

    payload = TestClient(server.app).get("/metrics").json()
    assert "error" not in payload, payload.get("error")
    head = payload["report"]
    assert head["signals"]["notes"] == 2
    assert head["previous"] == yesterday
    assert head["since"] == "2026-08-06T09:00:00"
    assert head["depth"] == "structural" and head["elapsed_s"] >= 0

    # and this report is now on file for the next one
    stored = read_history(CONFIG.vault_path)
    assert [r["signals"]["notes"] for r in stored] == [1, 2]


def test_a_memo_hit_does_not_claim_a_duration_it_did_not_spend(tmp_vault):
    """The second call inside one vault epoch is a dict lookup. It is still a
    true report and still worth showing; what it is not is a two-second audit."""
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    tmp_vault.note("A.md", "# A\n\nto [[B]]\n")
    client = TestClient(server.app)
    first = client.get("/metrics").json()["report"]
    second = client.get("/metrics").json()["report"]
    assert first["elapsed_s"] is not None
    assert second["elapsed_s"] is None
    assert second["at"] == first["at"]      # same report, not a new one


def test_metrics_survives_a_vault_it_cannot_write_to(tmp_vault, monkeypatch):
    """Losing the delta costs a section of one panel. Failing the call costs the
    whole view, which is why the store is best-effort at the call site."""
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    tmp_vault.note("A.md", "# A\n")
    monkeypatch.setattr("silica.kernel.report.history.record_report",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only vault")))
    payload = TestClient(server.app).get("/metrics").json()
    assert "error" not in payload
    assert payload["report"]["previous"] is None
    assert payload["report"]["signals"]["notes"] == 1
