# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L3 Router / Orchestrator for Silica — Organizer FSM.

Deterministic state machine for the /organize pipeline.

States:
    SCAN       — list vault notes in scope
    CLASSIFY   — L1 co-occurrence matching (zero LLM cost)
    ARBITRATE  — L2 LLM arbiter for ambiguous notes (best-effort)
    PLAN       — generate MoveOp list from Classifications
    SNAPSHOT   — snapshot pre-move state for rollback
    MOVE       — execute DRIVER.move() calls (graph-safe)
    LINT       — graph regression gate (orphan check)
    CLEANUP    — ledger commit
    ROLLBACK   — restore pre-move state on gate failure
    DONE / ERROR — terminal states
"""
from __future__ import annotations

import logging
import os
from typing import Any, TypedDict

from silica.driver import DRIVER
from silica.driver.base import NoteRef, GraphSnapshot
from silica.kernel.write import gate
from silica.kernel.write.ops import Op, OpType
from silica.kernel.organize.taxonomy import Taxonomy
from silica.router.base_fsm import BaseFSM
from silica.router.phases import Phase, PipelineState

logger = logging.getLogger(__name__)


class OrganizerState(PipelineState):
    INIT      = Phase("init")
    SCAN      = Phase("scan", "_handle_scan")
    CLASSIFY  = Phase("classify", "_handle_classify")
    ARBITRATE = Phase("arbitrate", "_handle_arbitrate")
    PLAN      = Phase("plan", "_handle_plan")
    SNAPSHOT  = Phase("snapshot", "_handle_snapshot")
    MOVE      = Phase("move", "_handle_move", on_error="rollback")
    LINT      = Phase("lint", "_handle_lint", on_error="rollback")
    CLEANUP   = Phase("cleanup", "_handle_cleanup")
    ROLLBACK  = Phase("rollback", "_handle_rollback", scope="exception")
    DONE      = Phase("done")
    ERROR     = Phase("error")


# MOVE's gate: past this share of failed moves the run rolls back.
MOVE_FAILURE_MAX = 0.10


class OrganizeResult(TypedDict, total=False):
    """The run's state and silica_organize's reply in one: the tool returns this
    dict as it stands, so a key set here reaches the model, in the order a run
    set it. That is why it stays a dict and the injector's state does not."""
    note_paths: list[str]
    classifications: list[Any]
    move_ops: list[Any]
    move_results: list[dict]
    move_failures: list[dict]
    plan_summary: dict
    snapshot: dict
    txn_id: str
    abort_reason: str
    error: str
    final_status: str


class OrganizerFSM(BaseFSM[OrganizerState]):
    """Deterministic state machine for the /organize pipeline."""

    def __init__(
        self,
        taxonomy: Taxonomy,
        scope: str = "",
        dry_run: bool = True,
        llm_arbiter: bool = True,
        move_uncategorized: bool = False,
    ) -> None:
        self.taxonomy = taxonomy
        self.scope = scope
        self.dry_run = dry_run
        self.llm_arbiter = llm_arbiter
        self.move_uncategorized = move_uncategorized

        self.state = OrganizerState.INIT
        self.context: OrganizeResult = {
            "note_paths": [],
            "classifications": [],
            "move_ops": [],
            "move_results": [],
        }
        self._tmp_files: list[str] = []
        self._txn: Any = None  # Txn | None at runtime
        self._pre_graph: GraphSnapshot | None = None
        self.undo_run_id: str | None = None

        # BaseFSM contract
        self._phase_label = "Organizer"
        self._done_state = OrganizerState.DONE
        self._error_state = OrganizerState.ERROR
        self._rollback_state = OrganizerState.ROLLBACK

        self._HANDLERS = {
            state: getattr(self, state.value.handler)
            for state in OrganizerState if state.value.handler
        }

    def _record_error(self, message: str) -> None:
        self.context["error"] = message

    def _on_step_error(self, exc: Exception) -> bool:
        """A failure with moves on record rolls them back; before any, the run
        just ends."""
        if self.state.value.on_error == "rollback" and self._txn:
            self.context["abort_reason"] = str(exc)
            self.state = self._rollback_state
            return True
        return False

    def run(self) -> dict[str, Any]:
        self.state = OrganizerState.SCAN
        self._run_loop()

        if self.state == self._done_state:
            if "final_status" not in self.context:
                self.context["final_status"] = "Success"
        elif self.state == self._error_state:
            if "final_status" not in self.context:
                self.context["final_status"] = (
                    f"Failed: {self.context.get('error', 'unknown error')}"
                )

        return dict(self.context)

    def _handle_scan(self) -> None:
        """List all vault notes in the taxonomy scope."""
        try:
            refs = DRIVER.list_files(self.scope or "")
        except Exception as e:
            raise RuntimeError(f"SCAN: list_files failed: {e}") from e

        note_paths = [
            ref.path or ref.name
            for ref in refs
            if (ref.path or ref.name).endswith(".md")
        ]

        if self.taxonomy.scope:
            scope_prefix = self.taxonomy.scope.replace("\\", "/").rstrip("/") + "/"
            note_paths = [
                p for p in note_paths
                if p.replace("\\", "/").startswith(scope_prefix)
            ]

        logger.info("SCAN: found %d notes in scope '%s'", len(note_paths), self.scope or "(vault-wide)")
        self.context["note_paths"] = note_paths
        self.transition_success()

    def _handle_classify(self) -> None:
        """L1 deterministic classification via co-occurrence stems."""
        from silica.kernel.organize.classify import classify_notes

        note_paths = self.context["note_paths"]
        if not note_paths:
            logger.info("CLASSIFY: no notes to classify — skipping")
            self.context["classifications"] = []
            self.transition_success()
            return

        # Load co-occurrence store once and pass it in to avoid repeated disk reads
        cooccur_store = None
        try:
            from silica.kernel.recall.cooccurrence import get_cooccur_store
            from silica.config import CONFIG
            cooccur_store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
            if len(cooccur_store) == 0:
                cooccur_store = None
        except Exception as exc:
            logger.debug("CLASSIFY: CooccurStore unavailable (%s) — continuing with keyword-only", exc)

        # Run L1 only (no LLM); ARBITRATE phase handles the LLM leg
        classifications = classify_notes(
            note_paths,
            self.taxonomy,
            cooccur_store=cooccur_store,
            llm_arbiter=False,   # L2 handled separately in ARBITRATE
            move_uncategorized=self.move_uncategorized,
        )

        logger.info(
            "CLASSIFY: %d notes classified (%d need move)",
            len(classifications),
            sum(1 for c in classifications if c.needs_move),
        )
        self.context["classifications"] = classifications
        self.transition_success()

    def _handle_arbitrate(self) -> None:
        """L2 LLM arbiter for notes in the ambiguous confidence band."""
        from silica.kernel.organize.arbitrate import llm_arbitrate
        from silica.kernel.organize.classify import (
            Classification,
            DEFAULT_TAU_HIGH,
            DEFAULT_TAU_LOW,
        )

        classifications: list[Classification] = self.context["classifications"]

        if not self.llm_arbiter:
            logger.info("ARBITRATE: llm_arbiter=False — skipping")
            self.transition_success()
            return

        ambiguous: list[tuple[str, str, list]] = [
            (c.note_path, "", [])
            for c in classifications
            if c.evidence not in ("keyword", "llm") and DEFAULT_TAU_LOW <= c.confidence < DEFAULT_TAU_HIGH
        ]

        if not ambiguous:
            logger.info("ARBITRATE: no ambiguous notes — skipping LLM call")
            self.transition_success()
            return

        logger.info("ARBITRATE: sending %d ambiguous notes to LLM", len(ambiguous))

        enriched_ambiguous = []
        for note_path, _s, _r in ambiguous:
            snippet = ""
            try:
                nc = DRIVER.read_note(note_path)
                from silica.kernel.base import frontmatter
                _data, _fm, body = frontmatter.split(nc.content)
                snippet = body[:300]
            except Exception:
                pass  # the arbiter judges the path alone, which it can
            enriched_ambiguous.append((note_path, snippet, self.taxonomy.rules))

        try:
            llm_choices = llm_arbitrate(enriched_ambiguous, self.taxonomy)
        except Exception as exc:
            # ARBITRATE is best-effort — log and continue with L1 results
            logger.warning("ARBITRATE: LLM arbiter failed (%s) — using L1 results", exc)
            self.transition_success()
            return

        path_to_idx = {c.note_path: i for i, c in enumerate(classifications)}
        for note_path, _s, _r in enriched_ambiguous:
            chosen = llm_choices.get(note_path, self.taxonomy.uncategorized)
            idx = path_to_idx.get(note_path)
            if idx is None:
                continue
            c = classifications[idx]
            classifications[idx] = Classification(
                note_path=c.note_path,
                current_folder=c.current_folder,
                target_folder=chosen,
                confidence=1.0,
                evidence="llm",
                needs_move=(
                    c.current_folder != chosen
                    and (self.move_uncategorized or chosen != self.taxonomy.uncategorized)
                ),
                title=c.title,
                rule_themes=c.rule_themes,
            )

        logger.info("ARBITRATE: %d notes reclassified by LLM", len(llm_choices))
        self.context["classifications"] = classifications
        self.transition_success()

    def _handle_plan(self) -> None:
        """Generate MoveOp list from classifications."""
        from silica.kernel.organize.classify import Classification

        classifications: list[Classification] = self.context["classifications"]
        move_ops: list[Op] = []

        for c in classifications:
            if not c.needs_move:
                continue
            basename = os.path.basename(c.note_path)
            to_path = os.path.join(
                c.target_folder.replace("\\", "/"),
                basename,
            ).replace("\\", "/")

            if c.note_path.replace("\\", "/") == to_path:
                continue

            move_ops.append(Op(
                op=OpType.move,
                heading=c.title,
                source_basename=basename,
                from_path=c.note_path,
                to_path=to_path,
            ))

        logger.info(
            "PLAN: %d moves planned out of %d classified notes",
            len(move_ops), len(classifications),
        )

        self.context["move_ops"] = move_ops
        self.context["plan_summary"] = {
            "total_notes": len(classifications),
            "moves_planned": len(move_ops),
            "plan": [
                {
                    "from": op.from_path,
                    "to": op.to_path,
                    "confidence": next(
                        (c.confidence for c in classifications if c.note_path == op.from_path),
                        0.0,
                    ),
                    "evidence": next(
                        (c.evidence for c in classifications if c.note_path == op.from_path),
                        "?",
                    ),
                }
                for op in move_ops
            ],
        }

        if self.dry_run:
            logger.info("PLAN: dry_run=True — returning plan without executing moves")
            self.context["final_status"] = "DryRun"
            self.state = OrganizerState.DONE
            return

        self.transition_success()

    def _handle_snapshot(self) -> None:
        """Snapshot pre-move state for rollback (C3 strategy)."""
        move_ops = self.context["move_ops"]
        if not move_ops:
            self.context["snapshot"] = {}
            self.transition_success()
            return

        from silica.kernel.write.ops import InverseOp, InverseOpKind, Txn
        import uuid

        # Open an undo-journal run so `/revert` can move these notes back later.
        # Only reached in apply mode with real moves (PLAN returns early on dry_run).
        self.undo_run_id = gate.open_run(f"organize:{self.scope or 'vault'}")

        # For move ops, the inverse is moving back (from_path → to_path becomes to_path → from_path)
        # We also pre-capture the pre-move graph snapshot for the lint gate.
        inverses = [
            InverseOp(
                kind=InverseOpKind.move_back,
                path=op.from_path,
                to_path=op.to_path,
            )
            for op in move_ops
        ]

        txn_id = uuid.uuid4().hex
        refs = [
            NoteRef(name=os.path.splitext(os.path.basename(op.from_path))[0], path=op.from_path)
            for op in move_ops
        ]
        self._txn = Txn(id=txn_id, refs=refs, inverses=inverses)
        self.context["snapshot"] = {
            "txn_id": txn_id,
            "inverses": [inv.model_dump() for inv in inverses],
        }
        self.context["txn_id"] = txn_id

        try:
            self._pre_graph = DRIVER.graph_snapshot(refs)
        except Exception as exc:
            logger.warning("SNAPSHOT: pre-graph snapshot failed (%s) — lint gate will skip", exc)

        self.transition_success()

    def _handle_move(self) -> None:
        """Execute moves via DRIVER.move() — graph-safe (wikilinks updated automatically)."""
        move_ops = self.context["move_ops"]
        if not move_ops:
            self.context["move_results"] = []
            self.transition_success()
            return

        results = []
        failures = []
        for op in move_ops:
            try:
                DRIVER.move(op.from_path, op.to_path)
                results.append({"from": op.from_path, "to": op.to_path, "success": True})
                logger.debug("MOVE: %s → %s", op.from_path, op.to_path)
            except Exception as exc:
                failures.append({"from": op.from_path, "to": op.to_path, "error": str(exc)})
                logger.error("MOVE: failed %s → %s: %s", op.from_path, op.to_path, exc)

        self.context["move_results"] = results
        self.context["move_failures"] = failures

        max_failure_rate = MOVE_FAILURE_MAX
        failure_rate = len(failures) / len(move_ops) if move_ops else 0.0
        if failure_rate > max_failure_rate:
            raise RuntimeError(
                f"MOVE: failure rate {failure_rate:.1%} > {max_failure_rate:.1%} "
                f"({len(failures)}/{len(move_ops)} failed)"
            )

        logger.info(
            "MOVE: completed — %d moved, %d failed",
            len(results), len(failures),
        )
        self.transition_success()

    def _handle_lint(self) -> None:
        """Graph regression gate: check for unplanned orphans after moves."""
        if self._pre_graph is None:
            logger.info("LINT: no pre-graph snapshot — skipping graph regression")
            self.transition_success()
            return

        move_ops = self.context["move_ops"]
        refs_after = [
            NoteRef(name=os.path.splitext(os.path.basename(op.to_path))[0], path=op.to_path)
            for op in move_ops
        ]

        try:
            post_graph = DRIVER.graph_snapshot(refs_after)
        except Exception as exc:
            logger.warning("LINT: post-graph snapshot failed (%s) — skipping gate", exc)
            self.transition_success()
            return

        try:
            from silica.kernel.graph_diff import check_graph_regression
            created_paths = [op.to_path for op in move_ops]
            success, errors = check_graph_regression(
                self._pre_graph, post_graph, created_paths, frozenset()
            )
            if not success:
                nonblocking = ("Unplanned orphans", "Backlink drift")
                orphan_errors = [e for e in errors if e.startswith(nonblocking)]
                blocking_errors = [e for e in errors if not e.startswith(nonblocking)]
                if orphan_errors:
                    logger.warning("LINT: orphan/drift warning (non-blocking): %s", "; ".join(orphan_errors))
                if blocking_errors:
                    self.context["abort_reason"] = (
                        f"Graph regression after move: {'; '.join(blocking_errors)}"
                    )
                    self.state = OrganizerState.ROLLBACK
                    return
        except Exception as exc:
            logger.error("LINT: graph_diff check failed (%s) — treating as non-blocking", exc)

        self.transition_success()

    def _handle_cleanup(self) -> None:
        """Mark run as committed in the ledger and journal move inverses for /revert."""
        self.context["final_status"] = "Success"
        self._write_ledger("committed")
        self._record_undo_inverses()
        self.transition_success()

    def _record_undo_inverses(self) -> None:
        """Persist each committed move's inverse so `/revert <run>` can move it back.

        Only successful moves are journalled — a move that failed (or was rolled
        back before reaching CLEANUP) never lands here.
        """
        if not self.undo_run_id:
            return
        from silica.kernel.write.ops import InverseOp, InverseOpKind

        for res in self.context.get("move_results", []):
            if res.get("success"):
                gate.record_landed(self.undo_run_id, InverseOp(
                    kind=InverseOpKind.move_back, path=res["from"], to_path=res["to"]))

    def _handle_rollback(self) -> None:
        """Undo the moves that landed, through the gate's one rollback executor."""
        snapshot = self.context.get("snapshot", {})
        inverses = snapshot.get("inverses", [])

        if not inverses:
            logger.info("ROLLBACK: no inverses to apply")
        else:
            landed = {res["to"] for res in self.context.get("move_results", [])}
            res = gate.apply_inverses(snapshot.get("txn_id", ""),
                                      [inv for inv in inverses if inv.get("to_path") in landed])
            for applied in res["applied"]:
                logger.info("ROLLBACK: %s", applied)
            for error in res["errors"]:
                logger.error("ROLLBACK: %s", error)

        self.context["final_status"] = (
            f"Rolled Back: {self.context.get('abort_reason', 'unknown reason')}"
        )
        self.transition_success()

    def _write_ledger(self, status: str) -> None:
        try:
            from silica.kernel.write.ledger import get_ledger
            txn_id = self.context.get("txn_id", "unknown")
            for op in self.context.get("move_ops", []):
                canonical = (op.to_path or op.from_path or "").removesuffix(".md").lower()
                get_ledger().record(
                    txn_id=txn_id,
                    source_canonical=canonical,
                    path=op.to_path or op.from_path or "",
                    op="move",
                    status=status,
                )
        except Exception as exc:
            logger.warning("ORGANIZER: ledger write failed: %s", exc)


