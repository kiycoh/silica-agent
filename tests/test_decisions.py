"""ADR-0167: a decision is a plain note; /decide writes it as typed, /outcome
dates what came of it, /decisions and the Calendar read it, no model anywhere."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from silica.kernel.base import frontmatter
from silica.kernel.calendar import decisions as dc
from silica.kernel.calendar.agenda import agenda


def test_the_note_reads_back_as_written_with_real_booleans_and_dates():
    text = dc.note_text("Move the backups to the NAS", today="2026-11-12", why="[[Backup costs]]",
                        expect="restores under an hour", review="2026-12-15")
    data, _raw, _body = frontmatter.split(text)
    assert data["decision"] is True and data["decided"] == dt.date(2026, 11, 12) and data["status"] == "open"
    row = dc.read("Decisions/x.md", text, dt.date(2026, 11, 12))
    assert (row["decision"], row["why"], row["expected"], row["outcomes"]) == (
        "Move the backups to the NAS", "[[Backup costs]]", "restores under an hour", [])
    assert (row["review_on"], row["due"]) == ("2026-12-15", False)
    assert dc.read("x.md", text, dt.date(2026, 12, 15))["due"] is True
    assert dc.read("x.md", "# no decision\n") is None


def test_an_outcome_is_a_dated_line_and_may_close_the_decision():
    text = dc.note_text("D", today="2026-11-12")
    once = dc.add_outcome(text, "first restore took 40 minutes", "2026-11-20")
    twice = dc.add_outcome(once, "second took 35", "2026-11-27", "kept")
    row = dc.read("x.md", twice)
    assert row["status"] == "kept" and row["due"] is False
    assert row["outcomes"] == [{"date": "2026-11-20", "status": "", "text": "first restore took 40 minutes"},
                               {"date": "2026-11-27", "status": "kept", "text": "second took 35"}]
    # a note that became a decision by hand has no Outcomes section: it gets one
    bare = "---\ndecision: true\nstatus: open\n---\n\nWe chose X.\n"
    assert "## Outcomes\n- 2026-11-20: it worked" in dc.add_outcome(bare, "it worked", "2026-11-20")


def test_a_file_name_keeps_the_words_and_drops_what_breaks_a_path_or_a_link():
    assert dc.name_for("Use [[Postgres]]: not #MySQL / SQLite?") == "Use Postgres not MySQL SQLite"
    assert dc.name_for("   ") == "Decision"
    assert len(dc.name_for("word " * 60)) <= dc.NAME_CHARS


def test_an_open_decision_lands_on_its_review_day_then_on_today():
    rows = [{"name": "Ahead", "path": "a.md", "status": "open", "review_on": "2026-08-21"},
            {"name": "Late", "path": "b.md", "status": "open", "review_on": "2026-08-01"},
            {"name": "Closed", "path": "c.md", "status": "kept", "review_on": "2026-08-01"},
            {"name": "Words", "path": "d.md", "status": "open", "review_on": "next spring"}]
    days = {r["date"]: r for r in agenda(dt.date(2026, 8, 17), dt.date(2026, 8, 24), occurrences=[],
                                         timeline_rows=[], log_lines=[], review_rows=[], event_stems=set(),
                                         today=dt.date(2026, 8, 19), decisions=rows)}
    assert [d["name"] for d in days["2026-08-19"]["decisions"]] == ["Late"]
    assert days["2026-08-19"]["decisions"][0]["overdue"] is True
    assert [d["name"] for d in days["2026-08-21"]["decisions"]] == ["Ahead"]
    assert sum(len(r["decisions"]) for r in days.values()) == 2


@pytest.fixture
def vault(tmp_vault):
    from silica.config import CONFIG

    tmp_vault.note("Notes/Backup costs.md", "# Backup costs\n")
    return Path(CONFIG.vault_path)


def test_decide_writes_the_persons_words_and_undo_takes_the_note_back(vault):
    from tests.run_command import direct

    assert direct('/decide Move the backups to the NAS --why [[Backup costs]] said so '
                  '--expect restores under an hour --review 2026-12-15', [])
    note = vault / "Decisions/Move the backups to the NAS.md"
    text = note.read_text()
    data, _raw, _body = frontmatter.split(text)
    assert "AI" not in data and data["review_on"] == dt.date(2026, 12, 15)
    assert "## Why\n\n[[Backup costs]] said so\n" in text
    # the same words again: a second note beside the first, never over it
    assert direct("/decide Move the backups to the NAS", [])
    assert (vault / "Decisions/Move the backups to the NAS 2.md").exists()
    assert direct("/undo Decisions/Move the backups to the NAS 2.md", [])
    assert not (vault / "Decisions/Move the backups to the NAS 2.md").exists()


def test_decide_refuses_a_bad_date_and_a_folder_outside_the_vault(vault):
    from tests.run_command import direct

    direct("/decide X --review soon", [])
    direct("/decide X --target ../elsewhere", [])
    assert not (vault / "Decisions").exists() and not (vault.parent / "elsewhere").exists()


def test_outcome_and_the_listing(vault):
    from silica.command_table import Result
    from silica.commands import decisions as cmd
    from tests.run_command import direct

    direct("/decide Ship the beta --review 2000-01-01", [])
    assert direct('/outcome "Ship the beta" two users found the import slow --status changed', [])
    row = dc.read("x.md", (vault / "Decisions/Ship the beta.md").read_text())
    assert row["status"] == "changed" and row["outcomes"][0]["text"] == "two users found the import slow"
    direct("/decide Hire a designer --review 2000-01-01", [])
    assert [r["name"] for r in dc.scan(vault, "due")] == ["Hire a designer"]
    assert {r["name"] for r in dc.scan(vault, "all")} == {"Hire a designer", "Ship the beta"}
    out = Result()
    from silica.command_table import COMMANDS, Call

    row_cmd = next(c for c in COMMANDS if c.name == "/decisions")
    cmd.listing(Call(row_cmd, ["all"], "/decisions all", [], out))
    said = "\n".join(str(a[0]) for a, _k in out.lines)
    assert "Hire a designer" in said and "(due)" in said and "[changed]" in said


def test_the_chat_tool_names_its_note_and_stamps_it_as_written_by_silica(vault):
    from silica.tools.notes import silica_decide

    res = silica_decide(path="Decisions/Pick a host", decision="Pick a host", why="[[Backup costs]]")
    assert res["success"] and res["path"] == "Decisions/Pick a host.md"
    data, _raw, _body = frontmatter.split((vault / "Decisions/Pick a host.md").read_text())
    assert data["decision"] is True and data["AI"] is True
    assert "error" in silica_decide(path="Decisions/Pick a host.md", decision="again")
    assert "error" in silica_decide(path="Decisions/Bad.md", decision="x", review_on="tomorrow")


def test_undo_takes_back_an_outcome_and_leaves_the_decision(vault):
    from tests.run_command import direct

    direct("/decide Keep the old printer", [])
    note = vault / "Decisions/Keep the old printer.md"
    before = note.read_text()
    assert direct("/outcome \"Keep the old printer\" it jammed twice --status reversed", [])
    assert dc.read("x.md", note.read_text())["status"] == "reversed"
    assert direct("/undo Decisions/Keep the old printer.md", [])
    assert note.exists() and dc.read("x.md", note.read_text())["outcomes"] == []
    assert frontmatter.split(note.read_text())[2] == frontmatter.split(before)[2]


def test_the_chat_can_load_the_decision_tool():
    import silica.cli  # noqa: F401  registers every tool
    from silica.agent.constraints import CHAT_CORE, chat_deferred

    assert "silica_decide" in chat_deferred() and "silica_decide" not in CHAT_CORE
