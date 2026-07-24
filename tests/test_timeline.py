# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica.kernel.timeline — chronological index over dated notes."""
from __future__ import annotations

from silica.kernel.timeline import timeline


def _note(path, date="", session_id=""):
    fm = ["---"]
    if date:
        fm.append(f"date: {date}")
    if session_id:
        fm.append(f"session_id: {session_id}")
    fm.append("---")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm) + "\nbody\n", encoding="utf-8")


def test_dated_sorted_undated_excluded(tmp_path):
    _note(tmp_path / "b.md", date="2026-02-01")
    _note(tmp_path / "a.md", date="2026-01-01", session_id="session_1")
    _note(tmp_path / "undated.md")
    t = timeline(tmp_path)
    assert t["total_dated"] == 2 and t["dropped"] == 0
    assert t["rows"] == [("2026-01-01", "session_1", "a"),
                         ("2026-02-01", "b", "b")]


def test_range_bounds_inclusive(tmp_path):
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        _note(tmp_path / f"{d}.md", date=d)
    t = timeline(tmp_path, start="2026-01-01", end="2026-01-02")
    assert [r[0] for r in t["rows"]] == ["2026-01-01", "2026-01-02"]


def test_overflow_keeps_most_recent(tmp_path):
    for i in range(5):
        _note(tmp_path / f"n{i}.md", date=f"2026-01-0{i + 1}")
    t = timeline(tmp_path, limit=2)
    assert t["dropped"] == 3 and t["total_dated"] == 5
    assert [r[0] for r in t["rows"]] == ["2026-01-04", "2026-01-05"]


def test_equal_dates_deterministic_stem_tiebreak(tmp_path):
    _note(tmp_path / "z.md", date="2026-01-01")
    _note(tmp_path / "a.md", date="2026-01-01")
    assert [r[2] for r in timeline(tmp_path)["rows"]] == ["a", "z"]


def test_empty_vault(tmp_path):
    assert timeline(tmp_path) == {"rows": [], "total_dated": 0, "dropped": 0}


def test_sources_and_hidden_excluded(tmp_path):
    _note(tmp_path / "sources" / "leaf.md", date="2026-01-01")
    _note(tmp_path / ".trash" / "gone.md", date="2026-01-01")
    _note(tmp_path / "kept.md", date="2026-01-02")
    t = timeline(tmp_path)
    assert [r[2] for r in t["rows"]] == ["kept"] and t["total_dated"] == 1
