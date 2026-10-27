# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the reader already knows of a plan, asked before it starts (ADR-0135
point 3). An offer, never a gate: ADR-0134 withdrew a required guess on the
owner's word, and the same rule holds here.

Per step, two candidates in one batch call: the step's deepest note in
prerequisite order, then one halfway down that order. The walk is code. Met
without help on the deepest note, the step is placed and the second question
is dropped; missed, the second is asked, and either way the step is not
placed. The answers are ordinary ledger lines (`origin: "placement"`), so
FSRS seeds itself through the replay; "placed" is derived from them on read
and never stored, and placed is not passed (`mastery.steps_passed`).
"""
from __future__ import annotations

PER_STEP = 2
# Ten steps of two, twenty questions: under the session's cap of 24
# (learn_routes.start_session). A longer plan places its first ten steps.
MAX_STEPS = 10
ORIGIN = "placement"


def _ordered(paths: list[str]) -> list[str]:
    """`paths` in prerequisite order, the step's own order breaking ties."""
    from silica.kernel.report import learner

    prereqs = {learner.key_of(k): v for k, v in learner.prerequisites_map().items()}
    place = {p: i for i, p in enumerate(paths)}
    rows = [{"path": p} for p in paths]
    return [r["path"] for r in learner._topological(rows, prereqs, lambda r: place[r["path"]])]


def step_paths(plan: dict, notes: dict | None = None) -> list[list[str]]:
    """Each step's note paths, in the step's order, as reading.step_notes
    gives them, with the vault's metadata read once: resolve_title alone reads
    it on every call, 0.08 s a title, which made a 241-note plan's page 27 s
    slower (measured 2026-10-27)."""
    from silica.kernel.learn import plans
    from silica.kernel.report import learner

    notes = notes if notes is not None else learner._notes_meta(plans._vault())
    out = []
    for step in plan.get("steps") or []:
        seen: list[str] = []
        for title in step.get("notes") or []:
            path = plans.resolve_title(title, notes)
            if path and path not in seen:
                seen.append(path)
        out.append(seen)
    return out


def kind_for(plan: dict) -> str:
    """`apply` where the plan asks for it, else `explain`: a placement asks
    for use, not recall, because recall is what a reader who skimmed has."""
    return "apply" if "apply" in (plan.get("objectives") or []) else "explain"


def candidates(plan: dict, view: dict | None = None, steps: list[list[str]] | None = None) -> list[dict]:
    """The rows to build, step by step, each tagged with its step and rank
    (0 the deepest note, 1 the middle one). A note already taken by an
    earlier step is not asked twice; a note the learner view does not hold
    (a lecture spine, a note outside the material) is not asked at all."""
    from silica.kernel.learn.items import LEVEL_OF
    from silica.kernel.report import learner

    view = learner.view() if view is None else view
    steps = step_paths(plan) if steps is None else steps
    kind = kind_for(plan)
    out: list[dict] = []
    taken: set[str] = set()
    for step in range(min(MAX_STEPS, len(steps))):
        paths = [p for p in steps[step] if learner.key_of(p) in view and p not in taken]
        if not paths:
            continue
        order = _ordered(paths)
        picks = [order[-1]]
        if len(order) >= 3:
            picks.append(order[(len(order) - 1) // 2])
        elif len(order) == 2:
            picks.append(order[0])
        for rank, p in enumerate(picks[:PER_STEP]):
            taken.add(p)
            out.append({**view[learner.key_of(p)], "path": p, "pool": ORIGIN, "kind": kind,
                        "level": LEVEL_OF[kind], "objective": kind, "step": step, "rank": rank})
    return out


def met_unaided(entry: dict) -> bool:
    return bool(entry.get("correct") and not entry.get("hints") and not entry.get("told"))


def placed_steps(plan: dict, entries: list[dict], steps: list[list[str]] | None = None) -> list[int]:
    """The steps a placement placed: a deepest-note answer met without help,
    on a note that still belongs to that step. Derived from the ledger on
    every read, so a step the plan no longer has, or a note moved out of it,
    places nothing."""
    by_path = {(e.get("path"), e.get("step")) for e in entries
               if e.get("origin") == ORIGIN and e.get("plan") == plan.get("path")
               and e.get("rank") == 0 and met_unaided(e)}
    if not by_path:
        return []   # no placement answer met: no step to look up
    steps = step_paths(plan) if steps is None else steps
    return [i for i, paths in enumerate(steps) if any((p, i) in by_path for p in paths)]
