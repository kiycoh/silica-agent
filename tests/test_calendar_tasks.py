# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0062 Decision 3: task lines read with the Tasks plugin's meaning.

The lines below are the founder's own, from daily/2026-09-18.md, so the
parser is pinned on what Tasks actually wrote and not on a tidied example.
"""
from __future__ import annotations

import datetime as dt
import json

from silica.kernel.calendar.tasks import parse_tasks, scan_tasks

D = dt.date


def test_fields_are_read_and_stripped_from_the_text():
    text = ("## Da fare oggi\n"
            "- [x] **Interiorizzare il progetto Datapolis** ⏫ 🔁 every day 📅 2026-09-25 ✅ 2026-09-23\n"
            "    - [ ] Leggere i documenti consegnati 🆔 zeidbg 🔺 📅 2026-09-19\n"
            "- [ ] Esame Machine Learning ⏫ 📅 2026-11-10\n"
            "- [ ] Scheduled one ⏳ 2026-10-01 🛫 2026-09-28 ➕ 2026-09-18\n")
    got = parse_tasks(text)
    assert [(t.line, t.status, t.text) for t in got] == [
        (2, "done", "Interiorizzare il progetto Datapolis"),
        (3, "open", "Leggere i documenti consegnati"),
        (4, "open", "Esame Machine Learning"),
        (5, "open", "Scheduled one"),
    ]
    assert (got[0].due, got[0].done) == (D(2026, 9, 25), D(2026, 9, 23))
    assert got[1].due == D(2026, 9, 19)
    assert (got[3].scheduled, got[3].start, got[3].due) == (D(2026, 10, 1), D(2026, 9, 28), None)


def test_a_date_tasks_would_not_read_is_not_read():
    # Tasks requires YYYY-MM-DD; the founder's "2026-10-5" is text to it.
    [t] = parse_tasks("- [ ] **Completare iscrizione università**🆔 zxvn3h 🔺 📅 2026-10-5\n")
    assert t.due is None and t.text == "Completare iscrizione università"


def test_statuses():
    got = parse_tasks("- [ ] a\n- [/] b\n- [X] c\n- [-] d\n* [ ] e\n1. [ ] f\n> - [ ] g\n")
    assert [(t.text, t.status) for t in got] == [
        ("a", "open"), ("b", "open"), ("c", "done"), ("d", "cancelled"),
        ("e", "open"), ("f", "open"), ("g", "open")]


def test_an_empty_checkbox_and_fenced_code_are_not_tasks():
    text = "- [ ] \n```tasks\n- [ ] inside a query\nnot done\n```\n- [ ] real\n"
    assert [t.text for t in parse_tasks(text)] == ["real"]


def test_the_global_filter():
    got = parse_tasks("- [ ] #task pay rent\n- [ ] grocery list item\n", global_filter="#task")
    assert [t.text for t in got] == ["#task pay rent"]


def _vault(tmp_path, tasks_settings=None):
    obs = tmp_path / ".obsidian"
    obs.mkdir()
    (obs / "core-plugins.json").write_text(json.dumps({"daily-notes": True}), encoding="utf-8")
    (obs / "daily-notes.json").write_text(json.dumps({"folder": "daily"}), encoding="utf-8")
    if tasks_settings is not None:
        d = obs / "plugins" / "obsidian-tasks-plugin"
        d.mkdir(parents=True)
        (d / "data.json").write_text(json.dumps(tasks_settings), encoding="utf-8")
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-09-18.md").write_text("- [ ] undated\n- [ ] dated 📅 2026-10-01\n",
                                                      encoding="utf-8")
    (tmp_path / "Lecture.md").write_text("- [ ] checklist item\n", encoding="utf-8")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "old.md").write_text("- [ ] gone 📅 2026-10-01\n", encoding="utf-8")
    return tmp_path


def test_scan_knows_which_tasks_sit_in_a_daily_note(tmp_path):
    v = _vault(tmp_path)
    got = {t.text: t for t in scan_tasks(v)}
    assert set(got) == {"undated", "dated", "checklist item"}
    assert got["undated"].path == "daily/2026-09-18.md" and got["undated"].line == 1
    assert got["undated"].daily == D(2026, 9, 18)
    assert got["checklist item"].daily is None
    assert got["undated"].scheduled is None


def test_filename_as_scheduled_date_dates_only_the_undated(tmp_path):
    v = _vault(tmp_path, {"useFilenameAsScheduledDate": True, "filenameAsDateFolders": [],
                          "filenameAsScheduledDateFormat": "", "globalFilter": ""})
    got = {t.text: t for t in scan_tasks(v)}
    assert got["undated"].scheduled == D(2026, 9, 18)
    assert got["dated"].scheduled is None
    assert got["checklist item"].scheduled is None


def test_filename_dates_respect_the_folder_list(tmp_path):
    v = _vault(tmp_path, {"useFilenameAsScheduledDate": True,
                          "filenameAsDateFolders": ["journal"]})
    assert {t.text: t for t in scan_tasks(v)}["undated"].scheduled is None


def test_the_dataview_format_is_not_read(tmp_path):
    v = _vault(tmp_path, {"taskFormat": "dataview"})
    assert scan_tasks(v) == []
