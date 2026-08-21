# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Per-note self-atomic write primitive.

commit_note_atomic applies a single Op with a micro-snapshot → write → lint
→ self-revert-on-failure lifecycle. This is the building block for per-note
independent atomicity without requiring a full batch transaction.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from silica.driver import DRIVER
from silica.kernel.write.bulk import VerifyMismatchError, execute_one
from silica.kernel.write.ops import Op, OpType, InverseOp, InverseOpKind


def _sha256(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _revert(txn_id: str, inverses: list[InverseOp]) -> tuple[bool, str | None]:
    """Apply the micro-snapshot inverses; report whether disk was truly restored.

    silica_restore never raises — it returns {"success", "errors"} — and a
    restore_version whose prior_content is None (build_txn could read neither
    the note nor its mirror seed) is SKIPPED with only a warning, never landing
    in errors. Claiming reverted=True there tells the caller the note was left
    untouched while the rejected body is still on disk, so a skipped inverse
    counts as a failed revert here.
    """
    from silica.tools.wrapped import silica_restore

    unrestorable = [
        inv.path for inv in inverses
        if inv.kind == InverseOpKind.restore_version and inv.prior_content is None
    ]
    res = silica_restore(txn_id=txn_id, inverses=[inv.model_dump() for inv in inverses])
    errors = list(res.get("errors") or [])
    if unrestorable:
        errors.append("no snapshot content for " + ", ".join(unrestorable))
    if errors:
        return False, "; ".join(errors)
    return True, None


@dataclass
class NoteCommitResult:
    ok: bool
    path: str
    op: str
    inverses: list[InverseOp] = field(default_factory=list)
    post_hash: str | None = None
    error: str | None = None
    reverted: bool = False
    # Pre-write lint violations this op was allowed to inherit (patch baseline).
    # The chunk LINT gate must tolerate exactly these, or a pre-existing issue
    # in a user-authored note aborts every chunk that patches it.
    baseline_errors: list[str] = field(default_factory=list)


def commit_note_atomic(op: Op, *, hub: str | None = None, lint: bool = True) -> NoteCommitResult:
    """Apply one Op atomically: micro-snapshot -> write -> lint -> self-revert on fail.

    The whole window runs under ``path_lease`` so the FSM ingest path
    serializes with every other lease-holding writer (subagent commit_ops,
    the MCP note tools) — patch is read-modify-write, and without the lease
    a concurrent append to the same note is silently lost.

    Args:
        op:   The single Op to apply (write / patch / overwrite / delete / skip).
        hub:  Optional hub override (falls back to op.hub).
        lint: Whether to run silica_lint after the write. On lint failure the
              write is reverted before returning.

    Returns:
        NoteCommitResult with ok=True on success, ok=False + reverted=True if
        lint failed and the note was restored to its prior state.
    """
    from silica.kernel.workqueue import path_lease

    path = op.touched_ref() or ""
    if not path:  # skip ops touch nothing — no lease to take
        return _commit_note_atomic_unlocked(op, hub=hub, lint=lint)
    with path_lease(path):
        return _commit_note_atomic_unlocked(op, hub=hub, lint=lint)


def _commit_note_atomic_unlocked(op: Op, *, hub: str | None = None, lint: bool = True) -> NoteCommitResult:
    from silica.tools.wrapped import build_txn
    from silica.tools.composed import silica_lint

    path = op.touched_ref() or ""
    op_name = op.op.value if op.op else ""

    # 1. micro-snapshot — captures prior content / inverse strategy BEFORE write
    txn = build_txn([op])
    inverses: list[InverseOp] = list(txn.inverses)

    # Diff-aware patch lint baseline: a patch appends to a user-authored note
    # that may already carry violations (e.g. an [!definizione] callout the
    # user uses). Only violations the patch INTRODUCES should block it — else a
    # pre-existing issue reverts every patch to that note forever. Captured on
    # the pre-write content; writes (new notes) keep an empty baseline.
    baseline_errors: set[str] = set()
    if lint and path and op.op == OpType.patch:
        try:
            baseline_errors = set(
                silica_lint(path, op_type=op_name, hub=hub or op.hub or "").get("errors", [])
            )
        except Exception:
            baseline_errors = set()

    # 2. execute the single op
    try:
        execute_one(op)
    except VerifyMismatchError as e:
        # The DRIVER call already happened and the post-write read-back
        # proves something (possibly corrupted) landed on disk — unlike every
        # other execute_one failure (missing params, a driver error raised
        # before/without a successful write), "nothing landed" no longer
        # holds here. Revert via the same micro-snapshot inverses the
        # lint-failure branch below uses (built at step 1, before the write).
        reverted, revert_error = _revert(txn.id, inverses)
        return NoteCommitResult(
            ok=False, path=path, op=op_name, inverses=inverses,
            error=str(e) if reverted else f"{e}; revert failed: {revert_error}",
            reverted=reverted,
        )
    except Exception as e:
        # Nothing landed (param validation, or the DRIVER call itself raised
        # before any write) — no revert needed, mirroring the pre-verify-gate
        # invariant this branch always had.
        return NoteCommitResult(
            ok=False, path=path, op=op_name, inverses=inverses, error=str(e)
        )

    # 3. optional lint on this note; revert on failure. Only NEW violations
    # (not in the pre-write baseline) count — see baseline_errors above.
    if lint and path:
        lr = silica_lint(path, op_type=op_name, hub=hub or op.hub or "")
        new_errors = [e for e in lr.get("errors", []) if e not in baseline_errors]
        if new_errors:
            reverted, revert_error = _revert(txn.id, inverses)
            error = f"lint failed: {new_errors}"
            if not reverted:
                # The rejected body is still on disk. Saying reverted=True here
                # would tell the caller the note was left untouched.
                error += f"; revert failed: {revert_error}"
            return NoteCommitResult(
                ok=False,
                path=path,
                op=op_name,
                inverses=inverses,
                error=error,
                reverted=reverted,
            )

    # 4. success — capture post-write content hash
    post_hash: str | None = None
    if path:
        try:
            post_hash = _sha256(DRIVER.read_note(path).content)
        except Exception:
            post_hash = None

    return NoteCommitResult(
        ok=True, path=path, op=op_name, inverses=inverses, post_hash=post_hash,
        baseline_errors=sorted(baseline_errors),
    )


@dataclass
class AtomicBulkResult:
    committed: list[NoteCommitResult] = field(default_factory=list)
    failed: list[NoteCommitResult] = field(default_factory=list)
    total: int = 0

    @property
    def ok(self) -> bool:
        return len(self.failed) == 0


def bulk_write_atomic(ops: list[Op], *, hub: str | None = None, lint: bool = True) -> AtomicBulkResult:
    """Apply ops one note at a time; each note is self-atomic. No shared Txn.

    Skip ops are excluded. A note that fails (exec or lint) is reverted in place
    and recorded in `failed`; its siblings are untouched and land in `committed`.
    """
    committed: list[NoteCommitResult] = []
    failed: list[NoteCommitResult] = []
    actionable = [o for o in ops if o.op != OpType.skip]
    for op in actionable:
        res = commit_note_atomic(op, hub=hub, lint=lint)
        (committed if res.ok else failed).append(res)
    return AtomicBulkResult(committed=committed, failed=failed, total=len(actionable))
