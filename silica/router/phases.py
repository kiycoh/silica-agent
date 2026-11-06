# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The injector's states, and the bracket a linear handler runs inside.

Below both the FSM and its handlers, so a handler can name a state without
importing the machine that dispatches it.
"""
from __future__ import annotations

from contextlib import contextmanager
from enum import Enum, auto


class InjectorState(Enum):
    INIT = auto()
    RECON = auto()         # Phase 1
    PAYLOAD = auto()       # Phase 2.0
    SALIENCE = auto()      # Phase 2.05 — thematic salience gate (drop off-theme concepts)
    COLLISION = auto()     # Phase 5 — dedup routing: high-sim→patch, borderline→defer, low→write
    DELEGATE = auto()      # Phase 2.1 — real Distiller LLM
    SANITIZE = auto()      # Phase 2.2
    VALIDATE = auto()      # Phase 2.3 (Gate) — C4: overwrites ops_path
    SNAPSHOT = auto()      # Phase 2.5 — C3: builds InverseOp Txn
    WRITE = auto()         # Phase 3
    HUB_UPDATE = auto()    # Phase 3.5 — patch Hub note with MOC links
    AUTOLINK = auto()      # Phase 4 — inject wikilinks into touched notes
    BACKLINK = auto()      # Phase 4.5 — reverse: inject links to new notes into pre-existing ones
    LINT = auto()          # Phase 5 (Gate)
    CLEANUP = auto()       # Phase 5 — C5: only from DONE
    ROLLBACK = auto()      # On gate fail — C3: apply inverses
    DONE = auto()
    ERROR = auto()


@contextmanager
def phase(fsm, task_id: str, capability_name: str):
    """Bracket a handler's happy path: 'running' on entry, then 'done' +
    _transition_success() on clean exit. An exception propagates WITHOUT
    emitting 'done'/transition (the caller's raise routes to ROLLBACK/error
    exactly as before). Fits only linear handlers with a single trailing
    success; handlers with early-exit transitions, a split done/transition,
    or ROLLBACK routing keep their explicit progress notes.

    A free function (not an FSM method) so it depends only on `_progress_note`
    and `_transition_success`: handler unit tests keep stubbing those two on a
    plain fake without needing to know about (or bind) the concrete FSM."""
    fsm._progress_note(task_id, capability_name, "running")
    yield
    fsm._progress_note(task_id, capability_name, "done")
    fsm._transition_success()
