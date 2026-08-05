# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The phase tables are hardcoded in two places away from the recipe.

That is safe by construction — merge_overlay (silica/router/overlay.py, ADR-0005)
rejects any Domain Pack that adds, removes or reorders phases, so a pack cannot
make them drift. The one thing that can is an edit to recipes/injector.yaml, and
these tests are what fails on that day.
"""
from __future__ import annotations

from silica.router.orchestrator import _FILE_SCOPE_PHASES
from silica.router.recipe_parser import load_recipe
from silica.ui.renderer import _CHUNK_PHASES, _FILE_PHASES, _PHASE_ORDER


def _recipe_ids() -> list[str]:
    return [p["id"] for p in load_recipe("injector")["phases"]]


def test_display_tables_cover_every_recipe_phase():
    """A phase the recipe runs but no table knows renders as a bare id at best,
    and silently vanishes from the track at worst."""
    ids = set(_recipe_ids())
    covered = set(_FILE_PHASES) | set(_CHUNK_PHASES) | {"rollback"}
    assert covered == ids, (
        f"phase tables out of sync with recipes/injector.yaml — "
        f"missing {ids - covered}, stale {covered - ids}"
    )


def test_orchestrator_and_renderer_agree_on_file_scope():
    """Two tables, one meaning: the scope the FSM stamps on a PhaseEvent must be
    the scope the frontend files it under, or events land in the wrong track."""
    assert set(_FILE_SCOPE_PHASES) == set(_FILE_PHASES)


def test_rollback_is_not_a_step():
    """rollback is on_gate_fail: an exception branch. As the 16th entry of the
    ordered track it rendered as a pending "· rollback" on every healthy run —
    "everything went wrong" shown as something about to happen."""
    recipe = {p["id"]: p for p in load_recipe("injector")["phases"]}
    assert recipe["rollback"].get("on_gate_fail") is True
    assert "rollback" not in _PHASE_ORDER


def test_phase_order_follows_the_recipe():
    """The track is a sequence, so its order has to be the run's order."""
    ordered = [p for p in _recipe_ids() if p != "rollback"]
    labels = {**_FILE_PHASES, **_CHUNK_PHASES}
    assert _PHASE_ORDER == [labels[p] for p in ordered]
