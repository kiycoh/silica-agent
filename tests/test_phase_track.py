# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a frontend draws and what the FSM stamps come from one table.

They used to be hand-kept copies of a recipe, so these pinned the copies. Now
the copies are derived, and what is left to hold is the meaning: every phase is
drawn, in the run's order, under the scope the event carries, and the exception
branch is not a step.
"""
from __future__ import annotations

from silica.router.phases import InjectorState
from silica.ui.tool_labels import CHUNK_PHASES, FILE_PHASES, PHASE_LABELS, PHASE_ORDER


def _ids() -> list[str]:
    return [p.id for p in InjectorState.phases()]


def test_display_tables_cover_every_phase():
    """A phase the run executes but no track knows renders as a bare id at best,
    and silently vanishes from the track at worst."""
    assert set(FILE_PHASES) | set(CHUNK_PHASES) | {"rollback"} == set(_ids())
    assert set(PHASE_LABELS) == set(_ids())


def test_the_event_scope_is_the_track_a_frontend_files_it_under():
    """Two readings, one meaning: the scope the FSM stamps on a PhaseEvent must be
    the scope the frontend files it under, or events land in the wrong track."""
    from silica.router.orchestrator import InjectorFSM

    fsm = InjectorFSM.__new__(InjectorFSM)
    fsm.inbox_files = ["Inbox/a.md"]
    fsm._current_file_idx = 0
    fsm._current_chunk_idx = 0
    fsm._chunk_flat_to_fi_ci = {}
    fsm._file_chunks = {}
    assert {p for p in _ids() if fsm._phase_position(p)["scope"] == "file"} == set(FILE_PHASES)
    assert {p for p in _ids() if fsm._phase_position(p)["scope"] == "chunk"} == set(CHUNK_PHASES)
    assert fsm._phase_position("rollback")["scope"] == "exception"


def test_rollback_is_not_a_step():
    """rollback is an exception branch. As the 16th entry of the ordered track it
    rendered as a pending "· rollback" on every healthy run: "everything went
    wrong" shown as something about to happen."""
    assert InjectorState.ROLLBACK not in InjectorState.steps()
    assert "rollback" not in PHASE_ORDER


def test_phase_order_follows_the_run():
    """The track is a sequence, so its order has to be the run's order."""
    assert PHASE_ORDER == [s.value.shown for s in InjectorState.steps()]
    assert [s.value.id for s in InjectorState.steps()][:3] == list(FILE_PHASES)


def test_the_label_is_what_the_client_matches():
    """The client matches rows by exact string, and no id-to-label rule covers
    this one."""
    assert PHASE_LABELS["hub_update"] == "hub-update"
