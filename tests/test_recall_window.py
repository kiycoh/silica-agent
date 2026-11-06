# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_recall takes a date window (ADR-0148): the notes dated inside it,
on the dates silica_timeline reads, are the only ones its legs may rank, and
an undated note is outside every window."""
from __future__ import annotations

import pytest

import silica.tools.notes  # noqa: F401  (the write gate registers on import)
from silica.kernel import workspaces as wsk

BODY = "Kalman filter covariance prediction update gain innovation residual smoothing.\n" * 3


@pytest.fixture
def vault(tmp_vault, monkeypatch):
    from silica.model import providers as prov
    from silica.tools.graph import silica_cooccurrence_refresh

    tmp_vault.note("Diary/January.md", "---\ndate: 2026-01-10\n---\n# January\n\n" + BODY)
    tmp_vault.note("Diary/March.md", "---\ndate: 2026-03-05\n---\n# March\n\n" + BODY)
    tmp_vault.note("Loose.md", "# Loose\n\n" + BODY)
    monkeypatch.setattr(prov, "get_reranker", lambda *a, **k: None)
    silica_cooccurrence_refresh()
    return tmp_vault


def _recall(**kw):
    from silica.tools.graph import silica_recall

    return silica_recall("kalman filter covariance", memory=False, **kw)


def test_without_a_window_every_note_ranks(vault):
    out = _recall()
    assert set(out["notes"]) == {"Diary/January", "Diary/March", "Loose"}
    assert "window" not in out


def test_a_window_keeps_the_notes_dated_inside_it(vault):
    out = _recall(since="2026-02")
    assert out["notes"] == ["Diary/March"]
    assert out["window"] == {"since": "2026-02", "until": "", "dated": 1, "undated": 1}


def test_a_month_bound_keeps_the_whole_month(vault):
    # "2026-03-05" sorts after "2026-03" as a string: the bound is compared on
    # its own length.
    assert _recall(until="2026-03")["notes"] != []
    assert set(_recall(since="2026-01", until="2026-03")["notes"]) == {"Diary/January", "Diary/March"}
    assert _recall(until="2026-02")["notes"] == ["Diary/January"]


def test_an_empty_window_says_so_and_names_the_way_out(vault):
    out = _recall(since="2025", until="2025")
    assert out["notes"] == [] and out["context"] == ""
    assert out["window"]["dated"] == 0 and out["window"]["undated"] == 1
    assert "retry without since/until" in out["hint"]


@pytest.mark.parametrize("bound", ["March 2026", "2026/03", "26-03", "2026-3"])
def test_a_bound_that_is_not_an_iso_prefix_is_refused(vault, bound):
    out = _recall(since=bound)
    assert "not a date bound" in out["error"] and out["notes"] == []


def test_named_notes_take_no_window(vault):
    out = _recall(notes=["January"], since="2026-03")
    assert "named notes" in out["error"]


def test_the_window_turns_the_memory_lane_off_and_reaches_the_legs(vault, monkeypatch):
    import silica.kernel.recall.perception as perception

    seen = {}
    real = perception.perceive

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(perception, "perceive", spy)
    from silica.tools.graph import silica_recall

    silica_recall("kalman", since="2026-03")  # memory left at its default, True
    assert seen["use_memory"] is False
    assert seen["allow"] == frozenset({"Diary/March"})


def test_a_workspace_and_a_window_intersect(vault):
    vault.note("Workspaces/Focus.md", "---\ntype: workspace\nnotes: []\nfolders:\n- Diary\nwhere: []\n---\n# Focus\n")
    tok = wsk.open_scope(wsk.find_workspace("Focus"))
    try:
        out = _recall(until="2026-01")
    finally:
        wsk.close_scope(tok)
    assert out["notes"] == ["Diary/January"]
    # Loose.md is outside the workspace, so it is not counted as undated here.
    assert out["window"]["undated"] == 0


def test_timeline_end_at_a_month_keeps_that_month(vault):
    from silica.tools.graph import silica_timeline

    out = silica_timeline(end="2026-03")
    assert "March" in out["timeline"] and out["total_dated"] == 2
