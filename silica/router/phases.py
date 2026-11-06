# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The pipelines' phases, declared once.

A state of a pipeline is a row of its table: the enum member IS the row, so the
order, the handler, the error route, the scope and the label of a phase cannot
disagree, and adding one is adding a line. Everything that used to be kept in
step with it by hand is derived: the plan a run persists, the per-chunk task
ids, the two tracks a frontend draws.

The table replaced two bundled YAML recipes (the injector's and the
organizer's) that nobody could override and half of whose keys nothing read.

Below the FSM, the handlers and the UI, so this imports nothing but the
standard library: `silica.ui.tool_labels` reads it on every start, and a
handler is therefore named, not imported.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class Phase:
    id: str
    handler: str = ""
    # PlanStep.kind: printed in the plan digest the distiller reads.
    kind: str = "mechanical"
    # PlanStep.objective: text the run ledger keeps, read by nothing that
    # dispatches; "" reads as the id. Not a tool reference: 8 of the 12 names
    # below are no registered tool (silica_hub_update among them, counted
    # 2026-11-06). They stay because a resumed run must read the plan it wrote.
    objective: str = ""
    on_error: Literal["abort", "rollback"] = "abort"
    scope: Literal["file", "chunk", "exception"] = "chunk"
    label: str = ""
    # An unhandled failure skips to the next phase instead of aborting the run
    # (A26). Without this a phase whose handler doesn't self-guard would route
    # to ERROR, and for post-write AUTOLINK/BACKLINK that bypasses ROLLBACK and
    # strands a half-committed chunk.
    best_effort: bool = False
    # Registered in the progress ledger when a fresh run starts, each depending
    # on the one before; every other task is added as its phase reaches it.
    seeded: bool = False

    @property
    def shown(self) -> str:
        return self.label or self.id


class PipelineState(Enum):
    """A pipeline's table. A member without a handler is a terminal state."""

    @classmethod
    def phases(cls) -> list[Phase]:
        return [s.value for s in cls if s.value.handler]

    @classmethod
    def steps(cls) -> list["PipelineState"]:
        """The run's sequence: every phase but the exception branch, which is
        reached by failing and never by finishing the phase before it."""
        return [s for s in cls if s.value.handler and s.value.scope != "exception"]


class InjectorState(PipelineState):
    INIT = Phase("init")
    RECON = Phase("recon", "setup.handle_recon", objective="silica_recon",
                  scope="file", seeded=True)
    PAYLOAD = Phase("payload", "setup.handle_payload", objective="silica_payload",
                    scope="file", seeded=True)
    # Thematic salience gate: drops off-theme concepts.
    SALIENCE = Phase("salience", "setup.handle_salience", scope="file", best_effort=True)
    # Dedup routing: high similarity patches, borderline defers, low writes.
    COLLISION = Phase("collision", "collision.handle_collision", best_effort=True)
    # The real Distiller LLM.
    DELEGATE = Phase("distill", "distill.handle_delegate", kind="semantic",
                     objective="distiller", on_error="rollback")
    SANITIZE = Phase("sanitize", "distill.handle_sanitize", objective="silica_sanitize",
                     on_error="rollback")
    # Gate. C4: overwrites ops_path.
    VALIDATE = Phase("validate", "distill.handle_validate", kind="gate",
                     objective="silica_validate_ops", on_error="rollback")
    # C3: builds the InverseOp Txn.
    SNAPSHOT = Phase("snapshot", "write.handle_snapshot", kind="txn",
                     objective="silica_snapshot", on_error="rollback")
    WRITE = Phase("write", "write.handle_write", objective="silica_bulk_write",
                  on_error="rollback")
    # Patches the Hub note with MOC links.
    HUB_UPDATE = Phase("hub_update", "write.handle_hub_update", objective="silica_hub_update",
                       on_error="rollback", label="hub-update")
    # Injects wikilinks into touched notes.
    AUTOLINK = Phase("autolink", "linking.handle_autolink", objective="silica_autolink",
                     best_effort=True)
    # The reverse: injects links to new notes into pre-existing ones.
    BACKLINK = Phase("backlink", "linking.handle_backlink", objective="silica_backlink",
                     best_effort=True)
    LINT = Phase("lint", "finalize.handle_lint", kind="gate", objective="silica_lint",
                 on_error="rollback")
    # C5: only reachable from a chunk that committed.
    CLEANUP = Phase("cleanup", "finalize.handle_cleanup", objective="silica_cleanup")
    # On gate fail. C3: applies the inverses.
    ROLLBACK = Phase("rollback", "finalize.handle_rollback", kind="txn",
                     objective="silica_restore", scope="exception")
    DONE = Phase("done")
    ERROR = Phase("error")


# Concepts per chunk: past this a file's payload is partitioned.
PARTITION_IF_OVER = 7
# VALIDATE logs a run past this rejection rate; partial success still proceeds.
REJECTION_RATE_MAX = 0.10
# One in-flight steer: rejected ops are parked in the deferred store, and the
# second recovery pass happens at the boundary (batched, off the critical path).
MAX_STEER_ATTEMPTS = 1


@contextmanager
def phase(fsm, task_id: str, capability_name: str):
    """Bracket a handler's happy path: 'running' on entry, then 'done' +
    transition_success() on clean exit. An exception propagates WITHOUT
    emitting 'done'/transition (the caller's raise routes to ROLLBACK/error
    exactly as before). Fits only linear handlers with a single trailing
    success; handlers with early-exit transitions, a split done/transition,
    or ROLLBACK routing keep their explicit progress notes.

    A free function (not an FSM method) so it depends only on `progress_note`
    and `transition_success`: handler unit tests keep stubbing those two on a
    plain fake without needing to know about (or bind) the concrete FSM."""
    fsm.progress_note(task_id, capability_name, "running")
    yield
    fsm.progress_note(task_id, capability_name, "done")
    fsm.transition_success()
