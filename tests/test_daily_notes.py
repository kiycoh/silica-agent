# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0062 Decision 1: a daily note is the note Obsidian would open as that
day's note, read from `.obsidian/daily-notes.json` and nothing else."""
from __future__ import annotations

import datetime as dt
import json

from silica.kernel.calendar.daily import daily_date, daily_notes, daily_rule


def _vault(tmp_path, *, config=None, enabled=True, core_list=False):
    obs = tmp_path / ".obsidian"
    obs.mkdir()
    core = ["daily-notes"] if core_list else {"daily-notes": enabled, "graph": True}
    if not (core_list and not enabled):
        (obs / "core-plugins.json").write_text(json.dumps(core), encoding="utf-8")
    if config is not None:
        (obs / "daily-notes.json").write_text(json.dumps(config), encoding="utf-8")
    return tmp_path


def _date(vault, rel):
    return daily_date(rel, daily_rule(vault))


def test_the_founders_config_folder_and_format(tmp_path):
    v = _vault(tmp_path, config={"folder": "daily", "format": "YYYY-MM-DD",
                                 "template": "Templates/daily_note.template.md"})
    assert _date(v, "daily/2026-09-18.md") == dt.date(2026, 9, 18)
    # Outside the folder, or one level below it, is not that day's note.
    assert _date(v, "2026-09-18.md") is None
    assert _date(v, "daily/old/2026-09-18.md") is None
    assert _date(v, "daily/2026-09-18 call with X.md") is None


def test_enabled_without_a_config_means_the_plugins_defaults(tmp_path):
    v = _vault(tmp_path)
    assert _date(v, "2026-09-18.md") == dt.date(2026, 9, 18)
    assert _date(v, "daily/2026-09-18.md") is None


def test_an_empty_format_is_the_default_one(tmp_path):
    v = _vault(tmp_path, config={"folder": "/Journal/", "format": ""})
    assert _date(v, "Journal/2026-01-02.md") == dt.date(2026, 1, 2)


def test_a_slash_in_the_format_makes_subfolders(tmp_path):
    v = _vault(tmp_path, config={"folder": "daily", "format": "YYYY/MM/YYYY-MM-DD"})
    assert _date(v, "daily/2026/09/2026-09-18.md") == dt.date(2026, 9, 18)
    # The same token twice must name the same value.
    assert _date(v, "daily/2025/09/2026-09-18.md") is None


def test_month_and_weekday_names(tmp_path):
    v = _vault(tmp_path, config={"format": "YYYY/MMMM/YYYY-MMM-DD"})
    assert _date(v, "2023/January/2023-Jan-01.md") == dt.date(2023, 1, 1)
    v2 = tmp_path / "v2"
    v2.mkdir()
    _vault(v2, config={"format": "YYYY-MM-DD dddd"})
    # The weekday is decoration: numbers carry the date, any name passes.
    assert daily_date("2026-09-18 venerdì.md", daily_rule(v2)) == dt.date(2026, 9, 18)


def test_a_month_name_with_no_number_must_be_english(tmp_path):
    v = _vault(tmp_path, config={"format": "D MMMM YYYY"})
    assert _date(v, "18 September 2026.md") == dt.date(2026, 9, 18)
    assert _date(v, "18 settembre 2026.md") is None


def test_brackets_and_ordinals(tmp_path):
    v = _vault(tmp_path, config={"format": "[Day] Do MMM YY"})
    assert _date(v, "Day 3rd Feb 26.md") == dt.date(2026, 2, 3)


def test_an_impossible_date_is_not_a_day(tmp_path):
    v = _vault(tmp_path)
    assert _date(v, "2026-02-30.md") is None


def test_an_unknown_token_fails_closed(tmp_path):
    v = _vault(tmp_path, config={"format": "gggg-[W]ww"})
    assert daily_rule(v) is None
    assert _date(v, "2026-W38.md") is None


def test_no_obsidian_or_plugin_off_means_no_daily_notes(tmp_path):
    assert daily_rule(tmp_path) is None
    off = tmp_path / "off"
    off.mkdir()
    _vault(off, config={"folder": "daily"}, enabled=False)
    assert daily_rule(off) is None


def test_the_older_list_form_of_core_plugins(tmp_path):
    v = _vault(tmp_path, core_list=True)
    assert _date(v, "2026-09-18.md") == dt.date(2026, 9, 18)


def test_daily_notes_walks_the_vault(tmp_path):
    v = _vault(tmp_path, config={"folder": "daily"})
    for rel in ("daily/2026-09-18.md", "daily/2026-09-20.md", "daily/notes.md",
                "Inbox/2026-09-18.md", ".trash/daily/2026-09-19.md"):
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text("x", encoding="utf-8")
    assert daily_notes(v) == {"daily/2026-09-18.md": dt.date(2026, 9, 18),
                              "daily/2026-09-20.md": dt.date(2026, 9, 20)}
