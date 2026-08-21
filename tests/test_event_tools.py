"""The three event tools through the real fs commit path on a tmp vault.

Contracts: strict validation before any write; the note lands inside the
write boundary; the system floor (`AI: true`) is stamped so the note is
lint-clean; creation is checkpointed for /undo; update re-validates the
MERGED field set and clears the reminder mark when the time shape
(start/rrule) changes; the agenda tool merges all four axes and never
shows an Event note on the dated-notes axis.
"""
from __future__ import annotations

import datetime as dt

import pytest

import silica.kernel.write.checkpoints as checkpoints
from silica.kernel.calendar.reminders import load_marks, save_marks
from silica.kernel.write import frontmatter
from silica.tools.events import silica_agenda, silica_event_create, silica_event_update


@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    checkpoints.get_checkpoint_store(tmp_path / "checkpoints.db")
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)


def _fm(vault, rel):
    data, _, _ = frontmatter.split((vault / rel).read_text(encoding="utf-8"))
    return data


# --- create ------------------------------------------------------------------

def test_create_one_shot_full_note(vault):
    res = silica_event_create(title="Dentist", start="2026-08-20 15:00",
                              end="2026-08-20 16:00", reminder="30m",
                              body="bring the card")
    assert res.get("success") is True
    assert res["path"] == "calendar/2026-08-20 Dentist.md"
    data = _fm(vault, res["path"])
    assert data["title"] == "Dentist"
    assert str(data["event_start"]) == "2026-08-20 15:00"
    assert str(data["event_reminder"]) == "30m"
    assert data["AI"] is True  # system floor: the note is lint-clean
    assert "bring the card" in (vault / res["path"]).read_text(encoding="utf-8")
    assert res["checkpoint_ok"] is True  # /undo has a restore point


def test_create_rejects_invalid_before_writing(vault):
    res = silica_event_create(title="Bad", start="2026-08-20 15:00",
                              rrule="FREQ=BOGUS")
    assert "error" in res
    assert not list(vault.rglob("*.md"))


def test_create_recurring_stem_is_title_only_with_collision_suffix(vault):
    res1 = silica_event_create(title="Gym", start="2026-08-03 18:00",
                               rrule="FREQ=WEEKLY;BYDAY=MO")
    res2 = silica_event_create(title="Gym", start="2026-08-05 18:00",
                               rrule="FREQ=WEEKLY;BYDAY=WE")
    assert res1["path"] == "calendar/Gym.md"
    assert res2["path"] == "calendar/Gym 2.md"


def test_create_all_day_from_date_only(vault):
    res = silica_event_create(title="Fair", start="2026-08-20")
    from silica.kernel.calendar.model import scan_events
    [e] = scan_events(vault)
    assert e.all_day is True and res["path"] == "calendar/2026-08-20 Fair.md"


def test_create_lands_inside_the_write_boundary(vault, monkeypatch):
    monkeypatch.setattr("silica.kernel.vault_manifest.active_write_dir",
                        lambda: "silica")
    res = silica_event_create(title="Scoped", start="2026-08-20")
    assert res["path"].startswith("silica/calendar/")


# --- update ------------------------------------------------------------------

def test_update_patches_one_field_and_closes_series(vault):
    silica_event_create(title="Gym", start="2026-08-03 18:00",
                        rrule="FREQ=WEEKLY;BYDAY=MO")
    res = silica_event_update(note="Gym", status="done")
    assert res.get("success") is True
    data = _fm(vault, "calendar/Gym.md")
    assert data["event_status"] == "done"
    assert "FREQ=WEEKLY" in str(data["event_rrule"])  # untouched field survives


def test_update_revalidates_the_merged_set(vault):
    silica_event_create(title="Call", start="2026-08-20 15:00",
                        end="2026-08-20 16:00")
    res = silica_event_update(note="Call", end="2026-08-20 14:00")
    assert "error" in res
    assert str(_fm(vault, "calendar/2026-08-20 Call.md")["event_end"]) == "2026-08-20 16:00"


def test_update_clears_the_mark_when_time_shape_changes(vault):
    silica_event_create(title="Gym", start="2026-08-03 18:00",
                        rrule="FREQ=WEEKLY;BYDAY=MO", reminder="30m")
    save_marks(vault, {"Gym": "2026-08-10T18:00:00", "other": "x"})
    silica_event_update(note="Gym", start="2026-08-03 17:00")
    marks = load_marks(vault)
    assert "Gym" not in marks and marks["other"] == "x"


def test_update_keeps_the_mark_on_non_time_changes(vault):
    silica_event_create(title="Gym", start="2026-08-03 18:00", reminder="30m")
    save_marks(vault, {"Gym": "2026-08-10T18:00:00"})
    silica_event_update(note="Gym", status="done")
    assert load_marks(vault) == {"Gym": "2026-08-10T18:00:00"}


def test_update_missing_note_errors(vault):
    assert "error" in silica_event_update(note="Ghost", status="done")


# --- agenda tool -------------------------------------------------------------

def test_agenda_merges_axes_and_excludes_event_notes_from_dated_axis(vault):
    silica_event_create(title="Dentist", start="2026-08-20 15:00")
    (vault / "lecture.md").write_text("---\ndate: 2026-08-20\n---\nnotes\n",
                                      encoding="utf-8")
    (vault / "log.md").write_text(
        "- 2026-08-20 · nucleate `x.md` → 1 new · run abc12345\n", encoding="utf-8")
    res = silica_agenda(start="2026-08-17", days=7)
    day = {r["date"]: r for r in res["days"]}["2026-08-20"]
    assert [e["stem"] for e in day["events"]] == ["2026-08-20 Dentist"]
    assert [n["stem"] for n in day["notes"]] == ["lecture"]
    assert day["activity"] == ["nucleate `x.md` → 1 new · run abc12345"]
    assert "Dentist" in res["text"]


def test_agenda_default_window_starts_today(vault):
    res = silica_agenda()
    assert res["days"][0]["date"] == dt.date.today().isoformat()
    assert len(res["days"]) == 7


def test_agenda_review_axis_excludes_event_notes(vault, monkeypatch):
    # On a fresh vault review_queue marks every note unexplored, so without a
    # filter today's row floods with the event notes themselves — schedule,
    # not study material. Same exclusion the dated-notes axis already has.
    silica_event_create(title="Dentist", start="2026-08-20 15:00")
    (vault / "lecture.md").write_text("---\ndate: 2026-08-20\n---\nnotes\n",
                                      encoding="utf-8")
    monkeypatch.setattr(
        "silica.kernel.report.learner.review_queue",
        lambda limit=10: [{"path": "calendar/2026-08-20 Dentist.md", "why": "unexplored"},
                          {"path": "lecture.md", "why": "unexplored"}])
    res = silica_agenda()  # review lands on today's row
    today = res["days"][0]
    assert [r["path"] for r in today["review"]] == ["lecture.md"]


# --- MCP exposure ------------------------------------------------------------

def test_event_tools_are_on_the_core_mcp_surface():
    from silica.ui.mcp import WRITE_TOOLS, exposed_tools
    core = exposed_tools()
    for name in ("silica_event_create", "silica_event_update", "silica_agenda"):
        assert name in core
    assert {"silica_event_create", "silica_event_update"} <= WRITE_TOOLS
