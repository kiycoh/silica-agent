# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The write gate: validate, snapshot, write, lint, roll back, and record what
landed, in one place. Router, capabilities, tools and ui call it as plain Python.

Three ways through it, by what a failure may leave behind:

  * `commit_note_atomic` / `bulk_write_atomic`: one note at a time, each
    self-reverting, so a bad note costs only itself (the FSM's WRITE);
  * `commit_ops`: one batch under the leases of every note it touches, for the
    bounded sub-agents; `land_batch` is its lifecycle, shared with the deferred
    retry, which rolls back on any failure and has no lint;
  * `commit_derived`: one machine-derived note, no concept validators.

Which undo a write gets is named here, and only here:

  * a run of the undo journal (/revert), for what a run wrote: `open_run` then
    `record_landed`, or `journaled_write` when one note is the whole run;
  * a restore point taken BEFORE the act, for what a person or the chat does to
    a whole note: the `*_with_restore_point` family;
  * a checkpoint (/undo), for a single-note edit in the chat:
    `checkpoints.push_landed`, kept beside the note's history (ADR-0144).

`apply_inverses` is the one rollback executor. Every InverseOp, from a chunk's
ROLLBACK, a run's /revert or the trash, is interpreted by it.

C3 rollback strategy (ADR-0009):
  - write ops   → InverseOp(delete_created, path)
  - patch/overwrite ops → InverseOp(restore_version, path, prior_content=<body>)
  - Txn.inverses: list[InverseOp] replaces the ad-hoc created_paths field.
"""
from __future__ import annotations

import contextvars
import logging
import os
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from silica.driver import DRIVER
from silica.driver.base import NoteRef
from silica.kernel.base.markdown import WIKILINK_TARGET_RE
from silica.kernel.write import undo_journal
from silica.kernel.write.bulk import VerifyMismatchError, execute_one, execute_operations
from silica.kernel.write.linter import validate_note
from silica.kernel.write.ops import BulkResult, InverseOp, InverseOpKind, Op, OpType, Txn
from silica.kernel.write.ops_io import parse_ops
from silica.kernel.write.preserve import Loss
from silica.kernel.write.validate import validate_ops

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The run a write belongs to
# ---------------------------------------------------------------------------

# Ambient undo-journal run for a subagent batch. run_subagent_batch opens one
# run and sets this in every pool worker, so each commit_ops within the batch
# records its inverses under the SAME run — /revert then undoes the whole
# /enrich or /dedup, not just the last note. Default None: callers
# outside a batch (interactive tools) journal nothing, unchanged.
_current_undo_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "silica_undo_run", default=None
)

# Ambient PROVENANCE-ledger run for writes that happen outside CLEANUP but
# still belong to a run — the boundary anneal, which fires inside the FSM's
# `finally`. Distinct from _current_undo_run: that one is the undo journal's
# uuid4, this one is `fsm.progress.run_id`, the key every ledger reader matches
# on. Unset for a standalone tool call, which records under "anneal".
_current_ledger_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "silica_ledger_run", default=None
)

# (source_basename, content_sha[, ledger_run_id]) of the work item a worker
# thread is handling. commit_ops has no manifest and no CLEANUP behind it, so
# this is the only way a sub-agent's note reaches the provenance ledger. Unset
# for ad-hoc commits, which have no source to attribute to. The run id is
# optional so a 2-tuple (older callers, tests) still works.
_current_provenance: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "silica_current_provenance", default=None
)


@contextmanager
def in_run(*, undo: str | None = None, ledger: str | None = None,
           provenance: tuple[str, ...] | None = None) -> Iterator[None]:
    """Writes inside this block belong to these runs. A thread starts with an
    empty context, so a pool worker enters this itself. None leaves that part
    of the context as it is."""
    given: tuple[tuple[contextvars.ContextVar[Any], Any], ...] = (
        (_current_undo_run, undo), (_current_ledger_run, ledger), (_current_provenance, provenance))
    tokens = [(var, var.set(value)) for var, value in given if value is not None]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def open_run(source: str | None, *, vault: str | None = None,
             ledger_run_id: str | None = None) -> str:
    """A new journal run under `source`. `vault` defaults to the active one, in
    the one spelling /revert filters by."""
    return undo_journal.get_undo_journal().start_run(
        source=source, vault=vault or undo_journal.active_vault(), ledger_run_id=ledger_run_id)


def record_landed(run_id: str, inverse: InverseOp) -> None:
    """Journal `inverse` under `run_id` with the hash of what landed.

    The hash is the read-back's, never the text the caller asked for, so it
    matches what revert_run's modified-since guard recomputes after any
    normalisation the backend applied on the way in. Without it /revert loses
    that guard for this note but still undoes the write: the journal entry is
    worth more than the guard, and the bytes are already on disk.
    """
    where = (inverse.to_path or inverse.path) if inverse.kind == InverseOpKind.move_back else inverse.path
    try:
        landed = undo_journal.content_hash(DRIVER.read_note(where).content)
    except Exception:
        landed = None
        if inverse.kind != InverseOpKind.recreate_deleted:  # a delete leaves nothing to read back
            logger.warning("could not hash %s post-write; /revert guard disabled for it", where)
    undo_journal.get_undo_journal().record(run_id, inverse, landed)


def undo_of(path: str, prior: str | None) -> InverseOp:
    """How to take back a write that found `prior` at `path`: a note that did
    not exist is deleted, one that did gets its body back."""
    if prior is None:
        return InverseOp(kind=InverseOpKind.delete_created, path=path)
    return InverseOp(kind=InverseOpKind.restore_version, path=path, prior_content=prior)


def journaled_write(path: str, mutate: Callable[[str], str], *, source: str,
                    loss: Loss | None = None, free=(), run: str = "",
                    vault: str | None = None) -> str:
    """One revertible overwrite (ADR-0039's shape): the prior bytes go to the
    undo journal, so /revert undoes exactly this write. Returns the run id, ""
    when nothing changed. `run` joins an open run; empty opens one at the first
    real change, so a pass that rewrites nothing leaves no empty run for /revert
    to find. `loss` and `free` reach the overwrite: each caller names what its
    write may change (ADR-0093)."""
    from silica.kernel.workqueue import path_lease

    with path_lease(path):
        before = DRIVER.read_note(path).content or ""
        after = mutate(before)
        if after == before:
            return ""
        run = run or open_run(source, vault=vault)
        DRIVER.overwrite(path, after, loss=loss, free=free)
        record_landed(run, undo_of(path, before))
    return run


# ---------------------------------------------------------------------------
# Inverses: built before a write, interpreted here and nowhere else
# ---------------------------------------------------------------------------

def build_txn(ops_data: list[Op] | list[dict]) -> Txn:
    """Build a Txn with InverseOp entries before WRITE executes.

    Rollback strategies (C3):
      write   → delete_created(path)         — note didn't exist; undo = delete
      patch / overwrite → restore_version(path, prior_content=<full body>)
                          — note existed; undo = overwrite with saved content.
                          prior_content is the primary rollback path; version is
                          kept as a best-effort hint for backends that support it.
      delete  → recreate_deleted(path, prior_content=<full body>)
    """
    from silica.kernel.write.mirror import seed_mirror_copy

    ops = parse_ops(ops_data)
    patch_refs: list[NoteRef] = []
    prior_contents: dict[str, str | None] = {}
    seeded_here: set[str] = set()
    inverses: list[InverseOp] = []

    for op in ops:
        op_type = op.op
        path = op.touched_ref()
        if not path or op_type == OpType.skip:
            continue

        if op_type == OpType.write:
            # write's contract is "path MUST NOT exist", but nothing enforces it
            # at the FS boundary (DRIVER.create overwrites verbatim). If the path
            # already holds a note, undo must RESTORE it, not delete it — else
            # /revert turns an accidental clobber into data loss. Snapshot the
            # prior body and pick the inverse accordingly.
            try:
                prior = DRIVER.read_note(path).content
            except Exception:
                prior = None
            if prior:
                inverses.append(InverseOp(
                    kind=InverseOpKind.restore_version, path=path, prior_content=prior,
                ))
            else:
                inverses.append(InverseOp(kind=InverseOpKind.delete_created, path=path))
        elif op_type in (OpType.patch, OpType.overwrite):
            name = path.rsplit("/", 1)[-1].removesuffix(".md")
            ref = NoteRef(name=name, path=path)
            patch_refs.append(ref)
            # Read current content now (before WRITE) for content-based rollback.
            # This is more reliable than history:restore whose version numbering
            # shifts after each new write (position 1 becomes position 2, etc.).
            try:
                nc = DRIVER.read_note(ref)
                prior_contents[path] = nc.content
            except Exception as e:
                # Safe mode: the patch lands on the note's mirror copy, which
                # _execute_patch seeds at WRITE time — after this snapshot and
                # after the pre-write graph. Seed it here instead. Born later,
                # the copy gave every same-folder [[link]] a nearer target
                # between the two graph snapshots, and the original's lost
                # backlinks read as vandalism to the regression gate; born now,
                # both worlds contain it and the diff is clean. It also gives
                # the rollback a body to restore instead of skipping the op.
                seed_mirror_copy(path)
                try:
                    prior_contents[path] = DRIVER.read_note(ref).content
                    seeded_here.add(path)
                except Exception:
                    logger.warning("build_txn: could not read prior content for %s: %s", path, e)
                    prior_contents[path] = None
        elif op_type == OpType.delete:
            name = path.rsplit("/", 1)[-1].removesuffix(".md")
            ref = NoteRef(name=name, path=path)
            try:
                nc = DRIVER.read_note(ref)
                prior_content = nc.content
            except Exception as e:
                # Can't snapshot the body we're about to delete → /revert will be
                # unable to recreate it. Surface it now, not silently at revert.
                logger.warning(
                    "build_txn: cannot snapshot %s before delete; /revert won't "
                    "recreate it: %s", path, e)
                prior_content = None
            inverses.append(InverseOp(
                kind=InverseOpKind.recreate_deleted,
                path=path,
                prior_content=prior_content
            ))

    for ref in patch_refs:
        key = ref.path or ref.name
        if key in seeded_here:
            # A copy this txn brought into being is undone by removing it, not
            # by restoring a body the vault never had before the chunk. The
            # created_paths below picks it up too, so the graph gate grants it
            # the same forward-reference exemption as any other new note.
            inverses.append(InverseOp(kind=InverseOpKind.delete_created, path=ref.path))
            continue
        inverses.append(InverseOp(
            kind=InverseOpKind.restore_version,
            path=ref.path,
            prior_content=prior_contents.get(key),
        ))

    created_paths = [
        inv.path for inv in inverses
        if inv.kind == InverseOpKind.delete_created
    ]
    return Txn(
        id=f"txn_{uuid.uuid4().hex}",
        refs=patch_refs,
        created_paths=created_paths,
        inverses=inverses,
    )


def apply_inverses(txn_id: str, inverses: list[InverseOp] | list[dict]) -> dict[str, Any]:
    """Apply an InverseOp list to roll a transaction back. Never raises: a
    failed op lands in `errors`. A restore_version with no prior_content is
    skipped with a warning and not an error, which _revert counts for itself."""
    errors: list[str] = []
    applied: list[str] = []

    for raw in inverses:
        try:
            inv = raw if isinstance(raw, InverseOp) else InverseOp(**raw)
        except Exception as e:
            errors.append(f"Invalid InverseOp {raw}: {e}")
            continue

        path = inv.path
        try:
            if inv.kind == InverseOpKind.delete_created:
                try:
                    DRIVER.delete(path)
                    applied.append(f"deleted_created:{path}")
                except Exception as e:
                    err_str = str(e).lower()
                    if "not found" in err_str or "no such file" in err_str:
                        applied.append(f"deleted_created:{path} (already_absent)")
                    else:
                        raise

            elif inv.kind == InverseOpKind.restore_version:
                if inv.prior_content is not None:
                    # Overwrite with captured content (reliable across backends).
                    DRIVER.overwrite(path, inv.prior_content, loss=Loss.RESTORE)
                    applied.append(f"restored_content:{path}")
                else:
                    logger.warning("restore_version: no prior_content for %s — skipped", path)

            elif inv.kind == InverseOpKind.recreate_deleted:
                if inv.prior_content is not None:
                    # upsert: a re-run restore (partial rollback retry) finds the
                    # note already recreated — restoring content is still correct.
                    DRIVER.upsert(path, inv.prior_content, loss=Loss.RESTORE)
                    applied.append(f"recreated_deleted:{path}")
                else:
                    errors.append(f"recreate_deleted missing prior_content for {path}")

            elif inv.kind == InverseOpKind.move_back:
                # Undo a move: the note goes from where it landed back to its
                # origin. DRIVER.move replaces a file at the destination (the
                # ingest's archive step relies on it, see move_preflight), so
                # the origin is checked here: a note written there since the
                # move is not overwritten by the one coming back (ADR-0126).
                if not inv.to_path:
                    errors.append(f"move_back missing to_path for {path}")
                elif vault_file(path).exists():
                    errors.append(f"move_back: {path} is taken by a note written since")
                else:
                    DRIVER.move(inv.to_path, path)
                    applied.append(f"moved_back:{inv.to_path}->{path}")

            elif inv.kind == InverseOpKind.trash_created:
                # The trash lists delete runs, so the note leaves as a person's
                # delete of what it holds now, and comes back from there (ADR-0128).
                res = delete_notes_with_restore_point([(path, DRIVER.read_note(path).content)],
                                                      undo_journal.USER_DELETE_SOURCE)
                if res["deleted"]:
                    applied.append(f"trashed_created:{path}")
                else:
                    errors.append(f"trash_created: {res['error']}")

        except Exception as e:
            errors.append(f"Inverse op {inv.kind} on {path} failed: {e}")
            logger.error("Rollback error: %s", e)

    return {
        "success": not errors,
        "txn_id": txn_id,
        "applied": applied,
        "errors": errors,
    }


def _revert(txn_id: str, inverses: list) -> tuple[bool, str | None]:
    """Apply the micro-snapshot inverses; report whether disk was truly restored.

    apply_inverses never raises — it returns {"success", "errors"} — and a
    restore_version whose prior_content is None (build_txn could read neither
    the note nor its mirror seed) is SKIPPED with only a warning, never landing
    in errors. Claiming reverted=True there tells the caller the note was left
    untouched while the rejected body is still on disk, so a skipped inverse
    counts as a failed revert here.
    """
    unrestorable = [
        inv.path for inv in inverses
        if isinstance(inv, InverseOp) and inv.kind == InverseOpKind.restore_version
        and inv.prior_content is None
    ]
    res = apply_inverses(txn_id=txn_id, inverses=inverses)
    errors = list(res.get("errors") or [])
    if unrestorable:
        errors.append("no snapshot content for " + ", ".join(unrestorable))
    if errors:
        return False, "; ".join(errors)
    return True, None


def revert_run(run_id: str, *, store: undo_journal.UndoJournalStore | None = None,
               only_paths: set[str] | None = None) -> dict:
    """Replay a run's inverses LIFO, refusing notes modified since the inject.

    Version guard: re-read each note, hash it. If recorded post_hash exists and
    the current hash differs (note edited since inject), skip it — don't clobber
    newer work. Mark the run reverted when done.

    `only_paths` scopes the replay to those notes (any spelling; folded through
    provenance.note_key): the rest are skipped as outside the scope and the run
    stays open, since what was not replayed is still undoable. A run scoped to
    its whole footprint closes as an unscoped one does.

    A partial revert can leave survivors linking to notes it removed: the
    caller sweeps them (link.sweep.revert_and_sweep), since the write lane
    does not reach up into link.
    """
    from silica.kernel.write.provenance import note_key

    store = store or undo_journal.get_undo_journal()
    entries = store.inverses_for(run_id)  # LIFO
    scope = {note_key(p) for p in only_paths} if only_paths is not None else None
    reverted: list[str] = []
    skipped: list[dict] = []
    stale: list[dict] = []
    errors: list[dict] = []
    applied_paths: set[str] = set()
    out_of_scope = 0

    for inv, post_hash in entries:
        if scope is not None and note_key(inv.path) not in scope:
            skipped.append({"path": inv.path, "reason": "outside the requested scope"})
            out_of_scope += 1
            continue
        try:
            current = DRIVER.read_note(inv.path).content
            cur_hash: str | None = undo_journal.content_hash(current)
        except Exception:
            cur_hash = None  # note absent

        # Stale (B): the target note no longer exists in this vault, so there is
        # nothing to restore or delete — the journal describes a vault that was
        # reorganised or replaced. Report it honestly instead of counting an
        # empty overwrite as an error or an absent delete as a revert.
        # (recreate_deleted is exempt: an absent note is its expected precondition.)
        if cur_hash is None and inv.kind in (
            InverseOpKind.restore_version, InverseOpKind.delete_created, InverseOpKind.trash_created
        ):
            stale.append({"path": inv.path, "reason": "note absent (vault changed)"})
            continue

        # The guard belongs to the NEWEST inverse per path only: two chunks
        # patching one note both carry the FINAL content's hash, so the older
        # link of the chain can never match once the newer one applied. After
        # the newest passes, the older ones are the chain's own history.
        if (
            inv.path not in applied_paths
            and post_hash is not None and cur_hash is not None
            and cur_hash != post_hash
        ):
            skipped.append({"path": inv.path, "reason": "modified since inject"})
            continue

        try:
            res = apply_inverses(txn_id=run_id, inverses=[inv])
            if res["errors"]:
                # apply_inverses swallows per-op failures into its return value;
                # route them to errors instead of miscounting as reverted.
                errors.append({"path": inv.path, "error": "; ".join(res["errors"])})
            else:
                reverted.append(inv.path)
                applied_paths.add(inv.path)
        except Exception as e:
            errors.append({"path": inv.path, "error": str(e)})

    if not out_of_scope:
        store.mark_reverted(run_id)
    return {"run_id": run_id, "reverted": reverted, "skipped": skipped,
            "stale": stale, "errors": errors}


def restore_from_trash(item: dict, *, store: undo_journal.UndoJournalStore | None = None) -> str | None:
    """Bring back one note of the trash, an item of `trashed()`, and take it
    out of the trash; None when it is back, else why not (ADR-0128).

    One note and not its run, which is what /undo and the tree's undo bring
    back: a folder deleted at once is one item per note, and the person picks.
    Not revert_run(only_paths=...) with its sweep: that edits every note of the
    run it skipped, and a note written since at another trashed path would be
    edited by a restore that never named it."""
    store = store or undo_journal.get_undo_journal()
    inv = next((i for i, _ in store.inverses_for(item["run"])
                if i.path == item["path"] and i.kind == InverseOpKind.recreate_deleted), None)
    if inv is None:
        return "not in the trash"
    # apply_inverses's recreate is an upsert: it would overwrite the note
    if vault_file(inv.path).exists():
        return "a note is at that path now"
    res = apply_inverses(txn_id=item["run"], inverses=[inv])
    if res["errors"]:
        return res["errors"][0]
    store.leave_trash(item["run"], inv.path)
    return None


def resolve_source(source: str, *, vault: str | None) -> tuple[str, list[str]]:
    """`(key, candidates)` for what the user typed after `--source`.

    A ledger key as written wins, then the key the path names
    (`Inbox/CorsoA/Lezione 1.md`), then a bare name, which resolves only when
    one source carries it: `candidates` lists every key of that name, and more
    than one means the caller must ask which.
    """
    from silica.kernel.base.manifest import source_key
    from silica.kernel.write.provenance import source_keys

    keys = source_keys(vault_path=vault)
    for k in (source, source_key(source)):
        if k in keys:
            return k, [k]
    name = source.replace("\\", "/").rsplit("/", 1)[-1]
    named = sorted(k for k in keys if k.rsplit("/", 1)[-1] == name)
    return (named[0] if len(named) == 1 else source), named


def revert_source(source: str, *, vault: str | None,
                  store: undo_journal.UndoJournalStore | None = None,
                  after_run: Callable[[dict], Any] | None = None) -> dict:
    """Undo every journalled run that derived notes from `source` (ADR-0028).

    The join is the ledger's run id, which the journal records at start_run:
    the ledger says which runs touched the source and which notes each of
    them attributed to it, the journal holds the inverses and the guard.
    Newest run first, each scoped to its own ledger notes, so a hub another
    source also patched is left to the guard rather than rewound past the
    other source's work. Ledger runs with no active journal row (legacy rows
    from before the join, a pruned journal, a run already reverted by id) are
    reported under `unrevertable` with their note count, never acted on: the
    ledger knows the paths but not the inverses, and deleting on a guess is
    the one thing a revert must never do.

    `after_run` sees each run's report before the next run replays, which is
    where the link lane sweeps that run's survivors.
    """
    from silica.kernel.write.provenance import read_records

    key, candidates = resolve_source(source, vault=vault)
    if len(candidates) > 1:
        # Two sources share the name (CorsoA/, CorsoB/ "Lezione 1.md"): reverting
        # both deleted notes the user never asked about (ADR-0114). The caller
        # names one; nothing is touched until then.
        return {"source": source, "runs": [], "unrevertable": [], "ambiguous": candidates}
    store = store or undo_journal.get_undo_journal()
    notes_by_ledger_run: dict[str, set[str]] = {}
    for r in read_records(key, vault_path=vault):
        rid = r.get("run_id")
        if rid:
            notes_by_ledger_run.setdefault(rid, set()).update(r.get("notes") or [])
    if not notes_by_ledger_run:
        return {"source": source, "runs": [], "unrevertable": []}

    runs: list[dict] = []
    covered: set[str] = set()
    for run_id in store.runs_for_ledger(list(notes_by_ledger_run), vault=vault):
        ledger_id = (store.run_info(run_id) or {}).get("ledger_run_id") or ""
        res = revert_run(run_id, store=store,
                         only_paths=notes_by_ledger_run.get(ledger_id, set()))
        if after_run is not None:
            after_run(res)
        res["ledger_run_id"] = ledger_id
        runs.append(res)
        covered.add(ledger_id)
    unrevertable = [{"run_id": rid, "notes": len(notes)}
                    for rid, notes in notes_by_ledger_run.items() if rid not in covered]
    return {"source": source, "runs": runs, "unrevertable": unrevertable}


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

def lint_note(note_name: str, op_type: str = "", hub: str = "") -> dict[str, Any]:
    """Post-write gate: executes the OFM linter to find structural regressions."""
    errors, warnings = validate_note(note_name, hub=hub or None, op_type=op_type or None)
    return {"success": len(errors) == 0, "note": note_name, "errors": errors, "warnings": warnings}


def lint_baseline(path: str, op_type: str, hub: str) -> set[str]:
    """The violations a note may keep through this write.

    A patch appends to a note the user may already have written violations into
    (an [!definizione] callout is the field case): only what the patch
    INTRODUCES may block it, or one pre-existing issue reverts every patch to
    that note forever. Taken on the pre-write content; a write has no prior
    text, an overwrite answers for the whole body it lands.
    """
    if op_type != OpType.patch.value:
        return set()
    try:
        return set(lint_note(path, op_type=op_type, hub=hub).get("errors", []))
    except Exception as e:
        # Tolerated: with no baseline the lint after the write judges the whole
        # note, the strict side of the same check.
        logger.debug("lint baseline unavailable for %s: %s", path, e)
        return set()


def new_lint_errors(path: str, op_type: str, hub: str, baseline=()) -> list[str]:
    """What the lint says now that `baseline` did not already excuse."""
    return [e for e in lint_note(path, op_type=op_type, hub=hub).get("errors", []) if e not in baseline]


# ---------------------------------------------------------------------------
# One note at a time
# ---------------------------------------------------------------------------

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
    the single-note tools) — patch is read-modify-write, and without the lease
    a concurrent append to the same note is silently lost.

    Args:
        op:   The single Op to apply (write / patch / overwrite / delete / skip).
        hub:  Optional hub override (falls back to op.hub).
        lint: Whether to run the linter after the write. On lint failure the
              write is reverted before returning.

    Returns:
        NoteCommitResult with ok=True on success, ok=False + reverted=True if
        lint failed and the note was restored to its prior state.
    """
    # Every FSM lane commits here, so the formula spacing of the source
    # converter is tightened once for all of them. The dedup and expand
    # workers commit through bounds instead; their prompt carries the LaTeX
    # rule, the mechanical pass does not reach them (ponytail: one funnel).
    if op.snippet:
        from silica.kernel.text.text import normalize_math_spacing
        op.snippet = normalize_math_spacing(op.snippet)
    from silica.kernel.workqueue import path_lease

    path = op.touched_ref() or ""
    if not path:  # skip ops touch nothing — no lease to take
        return _commit_note_atomic_unlocked(op, hub=hub, lint=lint)
    with path_lease(path):
        return _commit_note_atomic_unlocked(op, hub=hub, lint=lint)


def _commit_note_atomic_unlocked(op: Op, *, hub: str | None = None, lint: bool = True) -> NoteCommitResult:
    path = op.touched_ref() or ""
    op_name = op.op.value if op.op else ""
    lint_hub = hub or op.hub or ""

    # 1. micro-snapshot — captures prior content / inverse strategy BEFORE write
    txn = build_txn([op])
    inverses: list[InverseOp] = list(txn.inverses)

    baseline_errors = lint_baseline(path, op_name, lint_hub) if lint and path else set()

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
    # (not in the pre-write baseline) count — see lint_baseline.
    if lint and path:
        new_errors = new_lint_errors(path, op_name, lint_hub, baseline_errors)
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
            post_hash = undo_journal.content_hash(DRIVER.read_note(path).content)
        except Exception:
            # Tolerated: nothing journals this hash, the caller's record_landed
            # re-reads the note once the chunk's later phases have settled it.
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


# ---------------------------------------------------------------------------
# One batch
# ---------------------------------------------------------------------------

@dataclass
class Landing:
    """What land_batch left. `failure` is why the batch was rolled back, None
    when it stands: {"error": ...} or {"lint_failures": [...]}, plus
    "rollback_error" when disk could not be put back."""
    txn: Txn
    bulk: BulkResult
    failure: dict | None = None


def land_batch(ops: list[Op], *, lint: bool, partial_ok: bool) -> Landing:
    """Snapshot, write, lint and roll back a validated batch, with every touched
    note held under `path_lease` for the whole window, so a concurrent writer
    cannot interleave on the same note.

    `partial_ok`: the batch stands while one op landed (sub-agents, whose lint
    then judges each note); False rolls back on any failed op (a deferred retry,
    where a half-landed bundle would be retried whole). Raises what build_txn
    raises, and nothing has landed then.
    """
    from silica.kernel.workqueue import path_lease

    touched = [(p, op.op.value, op.hub or "") for op in ops
               if (p := op.touched_ref()) and op.op is not OpType.skip]
    with ExitStack() as stack:
        # Deterministic lock ordering (sorted) avoids deadlock between sub-agents.
        for p in sorted({p for p, _, _ in touched}):
            stack.enter_context(path_lease(p))
        baseline = {p: lint_baseline(p, op_type, h) for p, op_type, h in touched} if lint else {}
        txn = build_txn(ops)
        bulk = execute_operations(ops)

        def rolled_back(failure: dict) -> Landing:
            reverted, why = _revert(txn.id, txn.inverses)
            if not reverted:
                logger.error("batch rollback failed: %s", why)
                failure["rollback_error"] = why
            return Landing(txn, bulk, failure)

        if not bulk.ok and not partial_ok:
            return rolled_back({"error": f"{len(bulk.failed)} of {bulk.total} write ops failed"})
        if bulk.successful == 0 and bulk.total > 0:
            return rolled_back({"error": "all write ops failed"})
        if lint:
            lint_failures = [{"path": p, "errors": new}
                             for p, op_type, h in touched
                             if (new := new_lint_errors(p, op_type, h, baseline.get(p, ())))]
            if lint_failures:
                return rolled_back({"lint_failures": lint_failures})
    return Landing(txn, bulk)


def _append_provenance(run_id: str | None, touched: list[tuple[str, str, str]]) -> None:
    """Record this worker's notes against the source it was dispatched for.

    Only the worker lane needs this: the FSM's own writes reach the ledger
    through CLEANUP's manifest. Best-effort — the notes are already on disk.
    """
    prov = _current_provenance.get()
    if not prov:
        return
    source, sha, *tail = prov
    if not (source and sha):
        return
    # No `.md`: the ledger stores note refs bare, and CLEANUP writes them that way.
    # `overwrite` is in the set because it is the ONLY op four sub-agent profiles
    # (see the bounds below) may emit, and what all three dedup merges emit, so
    # leaving it out silently unrecorded the majority of this lane's writes —
    # byte for byte the defect this function exists to close.
    try:
        from silica.kernel.write.provenance import append_record, is_deriving_op

        notes = sorted({p.removesuffix(".md") for p, op_type, _ in touched
                        if is_deriving_op(op_type)})
        if not notes:
            return
        # The ledger's key is the FSM's own run id, which the item carries: every
        # reader (Coordinator._sweep_dangling_links, CLEANUP's own writer,
        # check_renucleate) matches on `fsm.progress.run_id`. `run_id` here is
        # the UNDO-journal id — a uuid4 from journal.start_run, a different
        # keyspace — so records written under it matched nothing and the sweep
        # still missed exactly the notes this attribution exists to reach.
        ledger_run = (tail[0] if tail else "") or run_id or "worker"
        append_record(source, sha, ledger_run, notes)
    except Exception as exc:
        logger.debug("commit_ops: provenance append failed (non-fatal): %s", exc)


def commit_ops(
    ops: list[Op],
    *,
    target_dir: str = "",
    hub: str | None = None,
    bounds: CapabilityBounds | None = None,
    read_note: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Commit `ops` through leash → validate → snapshot → write → lint (+rollback).

    Lets bounded sub-agents commit through the same deterministic safety
    machinery the main pipeline uses, with CapabilityBounds filtering ops to the
    permitted envelope before anything is written.

    Returns a result dict with a `status` of:
      committed | rolled_back | no_ops | error
    plus `committed` (count), `txn_id`, and `rejected_by_bounds` (ops the bounds dropped).
    """
    rejected_by_bounds: list[dict] = []
    if bounds is not None:
        ops, rejected_by_bounds = bounds.enforce(ops, read_note=read_note)

    actionable = [o for o in ops if o.op != OpType.skip]
    if not actionable:
        return {"status": "no_ops", "committed": 0, "rejected_by_bounds": rejected_by_bounds}

    vres = validate_ops(ops, target_dir=target_dir, hub=hub or "")
    if vres.get("validated_count", 0) == 0:
        return {"status": "no_ops", "committed": 0, "rejected_by_bounds": rejected_by_bounds, "validate": vres}
    ops = parse_ops(vres["validated_ops"])  # C4: what lands is the coerced, deduped list

    touched = [
        (p, op.op.value if op.op else "", op.hub or "")
        for op in ops
        if (p := op.touched_ref()) and op.op is not OpType.skip
    ]

    # The write-gate lane (spec §4): what is about to touch the vault and
    # why, narrated from the material this function already computed.
    from silica.obs import narration as _narr_mod
    _wid = f"w-{uuid.uuid4().hex[:8]}"
    _narr_mod.NARRATOR.narrate(
        "write", "proposed",
        f"write {len(touched)} note(s): " + ", ".join(p for p, _, _ in touched[:3])
        + ("…" if len(touched) > 3 else ""),
        {"touched": touched, "rejected_by_bounds": len(rejected_by_bounds)},
        id=_wid)

    try:
        landing = land_batch(ops, lint=True, partial_ok=True)
    except Exception as e:
        return {"status": "error", "error": f"Snapshot failed: {e}",
                "rejected_by_bounds": rejected_by_bounds}
    txn_id = landing.txn.id

    if landing.failure is not None:
        reason = {**landing.failure, "status": "rolled_back", "committed": 0, "txn_id": txn_id,
                  "rejected_by_bounds": rejected_by_bounds}
        _narr_mod.NARRATOR.narrate(
            "write", "rolled_back",
            f"rolled back {len(touched)} note(s): "
            + str(reason.get("error") or reason.get("lint_failures") or "")[:80],
            {"txn_id": txn_id, "reason": {k: v for k, v in reason.items()
                                          if k in ("error", "lint_failures")}},
            id=_wid)
        return reason

    # Committed cleanly — journal the inverses if inside a batch run so the
    # whole pass is revertable via /revert.
    undo_run_id = _current_undo_run.get()
    if undo_run_id:
        for raw in landing.txn.inverses:
            record_landed(undo_run_id, raw if isinstance(raw, InverseOp) else InverseOp(**raw))
    _append_provenance(undo_run_id, touched)

    committed = landing.bulk.successful
    _narr_mod.NARRATOR.narrate(
        "write", "committed",
        f"committed {committed} note(s), txn {txn_id[:8]}",
        {"txn_id": txn_id, "committed": committed},
        id=_wid)
    return {
        "status": "committed",
        "committed": committed,
        "txn_id": txn_id,
        "rejected_by_bounds": rejected_by_bounds,
    }


def commit_derived(rel: str, content: str) -> dict[str, Any]:
    """Commit ONE machine-derived note: lease → snapshot → write → lint → rollback.

    Derived artifacts (code wiki) have their ground truth outside the vault
    (the deterministic digest), so the nucleate concept validators do not apply:
    the hub fallback would inject a junk hub note for the target dir, and the
    anti-info-loss bounds would reject a legitimately shrunk regen. The
    transactional guarantees stay: prior content is captured before the write
    and restored when the OFM lint fails. Creates or overwrites as needed —
    the single write path for first writes and regens alike.

    Returns {"status": committed | rolled_back | error, "reason": ...}.
    """
    from silica.kernel.write.templates import ensure_system_floor
    from silica.kernel.workqueue import path_lease

    with path_lease(rel):
        try:
            prior: str | None = DRIVER.read_note(rel).content
        except Exception:
            prior = None
        content = ensure_system_floor(content, prior=prior)
        try:
            if prior is None:
                DRIVER.create(rel, content)
            else:
                # DERIVED: the wiki is the code's, and a regen may shrink it.
                DRIVER.overwrite(rel, content, loss=Loss.DERIVED)
        except Exception as e:
            return {"status": "error", "reason": str(e)}
        lr = lint_note(rel, op_type="overwrite")
        if not lr.get("success", True):
            reason = "; ".join(str(e) for e in lr.get("errors") or []) or "lint failed"
            res = apply_inverses(txn_id=rel, inverses=[undo_of(rel, prior)])
            if res["errors"]:
                why = "; ".join(res["errors"])
                logger.error("commit_derived rollback failed for %s: %s", rel, why)
                return {"status": "error", "reason": f"{reason}; rollback failed: {why}"}
            return {"status": "rolled_back", "reason": reason}
    return {"status": "committed"}


def record_recovered(txn: Txn, validated: list[Op], content_hash: str, bundle: dict) -> None:
    """Journal + provenance for ops recovered outside the FSM's CLEANUP.

    Joins the ambient run when there is one. The boundary anneal fires inside
    the FSM's `finally`, so its writes belong to the run the user just started:
    a fresh journal run carries a later `started_at`, `last_active_run` orders
    by that, and `/revert` therefore undid the handful of recovered notes and
    left the whole nucleation on disk. Only a STANDALONE `silica_deferred_retry`
    — no run to join — opens its own revertible unit. Best-effort throughout:
    the notes are already on disk, and failing here must not undo them.
    """
    try:
        run_id = _current_undo_run.get() or open_run("anneal")
        for inv in txn.inverses:
            record_landed(run_id, inv)
    except Exception as exc:
        logger.debug("anneal: journal record failed (non-fatal): %s", exc)

    try:
        from silica.kernel.base.manifest import source_key
        from silica.kernel.write.provenance import append_record, is_deriving_op

        source = source_key(bundle.get("source_path", "") or "")
        notes = sorted({
            (op.path or "").removesuffix(".md")
            for op in validated
            if is_deriving_op(op.op) and op.path
        })
        if source and content_hash and notes:
            # The LEDGER's run id, which is not the journal's: every reader of
            # this field (Coordinator._sweep_dangling_links) matches on
            # `fsm.progress.run_id`, so stamping the journal's uuid4 here wrote
            # a record nothing could ever find. A standalone retry has no run to
            # name and stays "anneal". A separate record either way — the ledger
            # is append-only history, never a rewrite of what CLEANUP recorded.
            append_record(source, content_hash,
                          _current_ledger_run.get() or "anneal", notes)
    except Exception as exc:
        logger.debug("anneal: provenance append failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Whole notes, the act of a person or the chat: the restore point comes first
# ---------------------------------------------------------------------------

def vault_file(rel: str) -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path or "") / rel


def move_preflight(moves: list[tuple[str, str]]) -> str | None:
    """Why a set of (from, to) moves must not start, or None. Checked before
    anything moves, so a refusal names one note and moves none: a destination
    a note already holds, or two notes of the set landing on one path. Both
    read without case, because a link resolves its name without case and a
    second `lezione 2.md` beside `Lezione 2.md` is one name to every link. A
    change of case alone is the same file and passes.

    Here and not in DRIVER.move, which replaces the file at the destination:
    the ingest's archive step (cleanup_source) relies on that replacement
    when a source is nucleated a second time, so the refusal is the tree's
    and the chat's, the two callers that mean one note and not a copy."""
    seen: dict[str, str] = {}
    for src, dst in moves:
        key = dst.lower()
        if key in seen:
            return f"two notes would both become {dst}"
        seen[key] = src
        target = vault_file(dst)
        if not target.exists():
            continue
        try:
            if target.samefile(vault_file(src)):
                continue  # `Indice.md` to `indice.md` on a disk that reads them as one
        except OSError:
            pass
        return f"{dst} already exists"
    return None


def move_notes_with_restore_point(moves: list[tuple[str, str]], source: str,
                                  prepare: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """silica_move's restore point over several (from, to) pairs as ONE
    journal run (ADR-0126 point 3): the toast's undo and /undo bring the
    whole set back. The caller ran move_preflight; what this meets is the
    driver's own refusal. `prepare` runs on each note before it moves, inside
    the same failure handling: the tree passes link.rename.explicit_links.

    Stops at the first note the driver refuses. The notes before it stay
    moved and in the run. The refused note's inverse leaves the run only
    when nothing moved, its file where it was and the destination empty; a
    move torn after the rename, with the file at the destination and the
    referrers half rewritten, keeps it, or the note could not be brought
    back (silica_move marked the run reverted on any exception, which was
    wrong for that case)."""
    try:
        journal = undo_journal.get_undo_journal()
        run_id = open_run(source)
    except Exception as e:
        return {"run": None, "moved": [], "error": f"not moved: no restore point could be recorded ({e})"}
    moved: list[tuple[str, str]] = []
    error: str | None = None
    for src, dst in moves:
        try:
            journal.record(run_id, InverseOp(kind=InverseOpKind.move_back, path=src, to_path=dst),
                           post_hash=None)
        except Exception as e:
            error = f"not moved: no restore point could be recorded ({e})"
            break
        try:
            if prepare is not None:
                prepare(src)
            DRIVER.move(src, dst)
        except Exception as e:
            error = str(e)
            if vault_file(src).exists() and not vault_file(dst).exists():
                try:
                    journal.drop_inverse(run_id, src)
                except Exception as drop_error:
                    # Tolerated: the notes already moved must still be reported
                    # with their run. What stays is a move_back revert_run
                    # refuses, since the origin is there (apply_inverses).
                    logger.warning("move: could not drop the inverse of %s: %s", src, drop_error)
            break
        moved.append((src, dst))
    if not journal.inverses_for(run_id):
        journal.mark_reverted(run_id)  # nothing moved: nothing to move back later
    return {"run": run_id, "moved": moved, "error": error}


def create_note_with_restore_point(path: str, text: str, source: str) -> str:
    """A note a person wrote, as ONE journal run under `source`, whose id this
    returns (ADR-0155 point 8). `text` is what lands: no heading, no frontmatter.
    The restore point first, as a delete's: a write the toast cannot take back
    is not one this makes. Raises what the driver raises, FileExistsError on a
    taken path, with the run closed, since no note means nothing to take back."""
    journal = undo_journal.get_undo_journal()
    run_id = open_run(source)
    journal.record(run_id, InverseOp(kind=InverseOpKind.trash_created, path=path), post_hash=None)
    try:
        DRIVER.create(path, text, loss=Loss.PERSON)
    except Exception:
        journal.drop_inverse(run_id, path)
        journal.mark_reverted(run_id)
        raise
    return run_id


def linked_from(path: str) -> list[str]:
    """The notes that link `path`, the note itself left out."""
    return sorted({r.path for r in DRIVER.backlinks(path) if r.path} - {path})


def delete_with_restore_point(path: str, content: str, source: str) -> dict[str, Any]:
    """Record the restore point under `source`, delete `path`, and say what
    the delete left: `dangling`, each link that now points to no note, and
    `orphaned`, each note left with no incoming link. One helper for the
    model's delete and the user's /delete, so both report the same way.

    Both lists come from the driver's snapshots of the note's neighbourhood
    before and after, which hold every note the delete can change: a link
    the delete breaks has its source among the referrers, and a note it
    orphans was linked by the deleted note, so is among its successors. They
    equal rules 2 and 1 of the graph gate only because the driver's index
    repairs itself on a delete (ADR-0092 point 1).
    """
    res = delete_notes_with_restore_point([(path, content)], source)
    if "error" in res:
        return {"error": res["error"]}
    return {"success": True, "deleted": path, "undo": f"/undo {path}",
            "orphaned": res["orphaned"], "dangling": res["dangling"]}


def delete_notes_with_restore_point(notes: list[tuple[str, str]], source: str) -> dict[str, Any]:
    """`delete_with_restore_point` over several (path, content) pairs as ONE
    journal run, so a single /undo or revert_run brings the whole set back:
    the rail's folder delete (ADR-0123). The one-note helper is this with one
    pair, so the two cannot drift in what they report.

    `dangling` and `orphaned` are over the set: a link from one deleted note
    to another leaves nothing anyone keeps pointing nowhere, so neither list
    names a note of the set.

    Stops at the first note the driver refuses. The notes before it stay
    deleted and stay in the run (`deleted` names them), because they are
    undoable as they are; the refused note's inverse leaves the run (see
    UndoJournalStore.drop_inverse for why it is not inert).
    """
    paths = [p for p, _ in notes]
    pre = DRIVER.graph_snapshot([NoteRef(name=Path(p).stem, path=p) for p in paths])
    own = {p.removesuffix(".md") for p in paths}
    around = [k for k in pre.link_counts if k not in own]
    # The restore point first: a delete that cannot be taken back is the one
    # this tool no longer does (until 2026-09-28 it took none, and /undo could
    # not bring the note back).
    try:
        journal = undo_journal.get_undo_journal()
        run_id = open_run(source)
    except Exception as e:
        return {"error": f"not deleted: no restore point could be recorded ({e})", "deleted": []}
    deleted: list[str] = []
    for path, content in notes:
        try:
            journal.record(run_id, InverseOp(kind=InverseOpKind.recreate_deleted, path=path,
                                             prior_content=content), post_hash=undo_journal.ABSENT_HASH)
        except Exception as e:
            error = f"not deleted: no restore point could be recorded ({e})"
            break
        try:
            DRIVER.delete(path)
        except Exception as e:
            error = str(e)
            try:
                journal.drop_inverse(run_id, path)
            except Exception as drop_error:
                # Tolerated: the notes already deleted must still be reported
                # with their run, or the person cannot undo them. What stays
                # is a recreate revert_run skips, since the note is there.
                logger.warning("delete: could not drop the inverse of %s: %s", path, drop_error)
            break
        deleted.append(path)
    else:
        error = None
    if not deleted:
        journal.mark_reverted(run_id)  # nothing was deleted: nothing to recreate later
        return {"error": error, "deleted": []}
    post = DRIVER.graph_snapshot([NoteRef(name=k.rsplit("/", 1)[-1], path=f"{k}.md")
                                  for k in around])
    keys = set(around)
    before = {(lk.source.path, lk.target) for lk in pre.unresolved}
    dangling = sorted({(lk.source.path, lk.target) for lk in post.unresolved
                       if lk.source.path.removesuffix(".md") in keys} - before)
    orphaned = sorted(f"{k}.md" for k in around
                      if pre.backlink_counts.get(k, 0) > 0 and post.backlink_counts.get(k) == 0)
    out = {"success": error is None, "deleted": deleted, "run": run_id,
           "orphaned": orphaned,
           "dangling": [{"note": src, "link": tgt} for src, tgt in dangling]}
    if error is not None:
        out["error"] = error
    return out


def archive_source(inbox_file: str, done_dir: str = "") -> dict[str, Any]:
    """Archive the inbox file under Done/ after successful pipeline completion.

    C5: Only callable from DONE state — the orchestrator enforces this.

    A source that sits OUTSIDE the write boundary is never moved: under safe
    mode the run is a preview the user merges by pasting, and consuming the
    source would mutate the one tree the boundary exists to protect. It stays
    in the inbox, which is also where a re-run needs to find it.

    The archive keeps the source's inbox folder structure — see
    `archive_path_for`, which owns that rule. `done_dir` overrides the resolved
    root; empty asks `active_done_dir`, which keeps a pre-2026-08-23 `done/`
    where it is rather than renaming it.
    """
    from silica.kernel.base.manifest import archive_path_for, active_write_dir, within

    write_root = active_write_dir()
    if write_root and not within(inbox_file, write_root):
        return {"success": True, "skipped": inbox_file,
                "reason": f"outside the write boundary '{write_root}/'"}
    target = archive_path_for(inbox_file, done_dir)
    # The fs backend creates the destination parents itself; the Obsidian one
    # forwards to a plugin that does not, and every nested source now needs a
    # subfolder under the archive that has never existed. Make it here, once,
    # for both — an empty folder is inert if the move then fails.
    try:
        from silica.config import CONFIG

        vault = (getattr(CONFIG, "vault_path", "") or "").strip()
        if vault and "/" in target:
            (Path(vault) / target).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("CLEANUP: archive folder pre-create skipped (%s)", exc)
    try:
        DRIVER.move(inbox_file, target)
        return {"success": True, "moved": inbox_file, "to": target}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# CapabilityBounds: what a bounded sub-agent may write
# ---------------------------------------------------------------------------
# A bounded sub-agent (dedup, refiner) is allowed to *write*, but only within a
# strictly bounded envelope. The framework, not the model, decides:
#
#   * which op-types are permitted (`allowed_ops`),
#   * which note paths it may touch (`target_predicate` + `forbidden_paths`),
#   * that no information is lost on a rewrite (`content_guard`).
#
# `CapabilityBounds.enforce()` runs BEFORE the writer: any op outside the envelope
# is dropped with a reason, so a small/eager model can never escalate beyond its
# bounds. The kept ops still flow through commit_ops.
#
# Design note: enforcement is mechanical and deterministic. The model only ever
# proposes; the bounds dispose.

def _wikilinks(text: str) -> set[str]:
    """Return the set of wikilink targets in `text` (case-insensitive, trimmed)."""
    return {m.strip().lower() for m in WIKILINK_TARGET_RE.findall(text or "") if m.strip()}


def norm_path(path: str | None) -> str:
    """Canonical comparison key for a vault path: posix, no .md, lowercase."""
    if not path:
        return ""
    return path.replace("\\", "/").removesuffix(".md").lower()


def make_backlink_guard(orphan_title: str) -> Callable[[Op, str], str | None]:
    """Build a content_guard requiring a patch to ADD a wikilink to `orphan_title`.

    Used by the orphan connector, which writes into a NEIGHBOUR: "added some
    wikilink" is not enough there, because only a link pointing back at the
    orphan lowers the orphan's in-degree. Any other link is a no-op that would
    still report the orphan as repaired.
    """
    want = norm_path(orphan_title).rsplit("/", 1)[-1]

    def guard(op: Op, original: str) -> str | None:
        added = op.content if op.content is not None else (op.snippet or "")
        targets = {t.rsplit("/", 1)[-1] for t in _wikilinks(added)}
        if not want or want not in targets:
            return f"orphan repair added no wikilink to '{orphan_title}'"
        return None

    return guard


@dataclass(frozen=True)
class CapabilityBounds:
    """Execution bounds for a bounded sub-agent."""

    name: str
    allowed_ops: frozenset[OpType]
    # path → True if the sub-agent may touch it. Receives the raw op path.
    target_predicate: Callable[[str], bool] = field(default=lambda _p: True)
    # exact vault paths that are never touchable (e.g. the run hub).
    forbidden_paths: frozenset[str] = frozenset()
    # optional semantic guard for rewrites: (op, original_content) → reason|None.
    content_guard: Callable[[Op, str], str | None] | None = None

    def allows_path(self, path: str | None) -> bool:
        norm = norm_path(path)
        if not norm:
            return False
        forbidden_norms = {norm_path(p) for p in self.forbidden_paths}
        if norm in forbidden_norms:
            return False
        # Bare-name forbidden entries (no "/" or "\") may be matched by the
        # incoming path's basename — e.g. hub="Concepts" blocks "notes/Concepts.md".
        # Only apply basename expansion for bare entries to avoid false positives
        # where a note named "Foo.md" is blocked by hub="Foo" even when the hub
        # is actually a different full path like "other/Foo".
        bare_forbidden = {
            norm_path(p)
            for p in self.forbidden_paths
            if "/" not in p and "\\" not in p
        }
        if bare_forbidden and norm_path(os.path.basename(path or "")) in bare_forbidden:
            return False
        return bool(self.target_predicate(path or ""))

    def enforce(
        self,
        ops: list[Op],
        *,
        read_note: Callable[[str], str] | None = None,
    ) -> tuple[list[Op], list[dict]]:
        """Split `ops` into (kept, rejected) according to the envelope.

        `read_note(path) -> str` supplies the original note body for the
        content_guard; defaults to the live DRIVER.  rejected entries are
        {"op": <dict>, "reason": <str>} so the caller can log/defer them.
        """
        kept: list[Op] = []
        rejected: list[dict] = []

        guarded_ops = {OpType.overwrite, OpType.patch}

        for op in ops:
            # Explicit no-ops always pass through untouched.
            if op.op == OpType.skip:
                kept.append(op)
                continue

            if op.op not in self.allowed_ops:
                rejected.append({
                    "op": op.model_dump(),
                    "reason": f"op '{op.op.value}' not permitted by bounds '{self.name}'",
                })
                continue

            path = op.touched_ref()
            if not self.allows_path(path):
                rejected.append({
                    "op": op.model_dump(),
                    "reason": f"target '{path}' outside bounds '{self.name}'",
                })
                continue

            if self.content_guard is not None and op.op in guarded_ops:
                original = self._read_original(path, read_note)
                reason = self.content_guard(op, original)
                if reason is not None:
                    rejected.append({
                        "op": op.model_dump(),
                        "reason": f"{reason} (bounds '{self.name}')",
                    })
                    continue

            kept.append(op)

        return kept, rejected

    @staticmethod
    def _read_original(path: str | None, read_note: Callable[[str], str] | None) -> str:
        if not path:
            return ""
        if read_note is not None:
            try:
                return read_note(path)
            except Exception:
                return ""
        # Fall back to the live driver (best-effort; missing note → empty).
        try:
            return DRIVER.read_note(path).content or ""
        except Exception:
            return ""


def _one_note_bounds(path: str, op: OpType, name: str, *, hub: str | None) -> CapabilityBounds:
    """One op of one kind on the framework-derived `path`, hub never touchable.
    `name` sets log attribution: the envelopes below differ in nothing else."""
    key = norm_path(path)
    return CapabilityBounds(
        name=name,
        allowed_ops=frozenset({op}),
        target_predicate=lambda p: norm_path(p) == key,
        forbidden_paths=frozenset({hub} if hub else set()),
    )


def dedup_bounds(larger_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Dedup bounds: append-only into the LARGER note of a borderline pair.

    The only permitted action is a `patch` against `larger_path`.  The model may
    never overwrite, delete, or create notes, and never touch the hub.  Which note
    is "larger" is decided mechanically by the framework (via ofm.metrics), not by
    the model.
    """
    return _one_note_bounds(larger_path, OpType.patch, "dedup", hub=hub)


def dedup_supersede_bounds(loser_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Supersede bounds: one `overwrite` of the note a merge just absorbed.

    The content is framework-computed (a single `superseded_by` frontmatter
    key), never model-authored — the envelope exists so the write cannot drift
    onto another path if that ever changes.
    """
    return _one_note_bounds(loser_path, OpType.overwrite, "dedup_supersede", hub=hub)


def dedup_alias_bounds(winner_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Alias bounds: one `overwrite` of the note a merge just absorbed INTO.

    Sibling of dedup_supersede_bounds on the other side of the merge. Separate
    from dedup_bounds, which guards a model-authored body and must stay
    patch-only, or a merge could rewrite the note it was supposed to append to.
    """
    return _one_note_bounds(winner_path, OpType.overwrite, "dedup_alias", hub=hub)


def alias_consolidation_bounds(canonical_path: str) -> CapabilityBounds:
    """One `overwrite` of the note gaining framework-computed `aliases:` keys.

    Same envelope as dedup_alias_bounds, own name for log attribution: this
    write comes from the /aliases consolidation pass, not from a merge.
    """
    return _one_note_bounds(canonical_path, OpType.overwrite, "alias_consolidation", hub=None)


def dedup_settle_bounds(path: str, *, hub: str | None = None) -> CapabilityBounds:
    """A soft-gate note judged distinct: one overwrite that drops its
    `review:` key and appends the relation trace. Framework-computed content."""
    return _one_note_bounds(path, OpType.overwrite, "dedup_settle", hub=hub)


def dedup_spoke_bounds(spoke_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Spoke bounds for a dedup `distinct` verdict: create exactly ONE new note.

    The judge that ruled a borderline pipeline concept distinct also authored
    its spoke (C2 verdict routing); the only permitted action is a `write` of
    `spoke_path` — the path the framework derived from the title, never one the
    model picked — and the hub is never touchable.
    """
    return _one_note_bounds(spoke_path, OpType.write, "dedup_spoke", hub=hub)


def expand_bounds(spoke_path: str, *, hub: str | None = None, landed: bool = False) -> CapabilityBounds:
    """Expand bounds: re-author exactly ONE thin spoke.

    A single op on the path the validator already sanitized, hub never
    touchable, under its own name so logs attribute it to the expand retry,
    not the dedup judge. `landed`: the thin note is on disk (soft gate), so
    the envelope is one overwrite of it instead of one write.
    """
    return _one_note_bounds(spoke_path, OpType.overwrite if landed else OpType.write, "expand", hub=hub)


def enrich_bounds(target_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Enrich bounds: append-only into the note being enriched.

    The only permitted action is a `patch` against `target_path`, the hub never
    touchable. The twin of dedup_bounds under its own name for log attribution.
    An addition cannot lose what the note says, which is why this envelope
    carries no content guard where the overwrite it replaced needed one.
    """
    return _one_note_bounds(target_path, OpType.patch, "enricher", hub=hub)


def orphan_bounds(
    neighbour_paths: list[str], *, orphan_title: str, hub: str | None = None
) -> CapabilityBounds:
    """Connector bounds: append-only patch into a NEIGHBOUR that links TO the orphan.

    An orphan is a note with in-degree 0 — literally what graph_report counts —
    so the link has to land in the neighbour and point back at the orphan. The
    pre-2026-08-14 bounds permitted a patch into the orphan itself, which only
    ever gave it out-degree: on a 795-note vault 39 of the 100 orphans already
    carried outgoing links and were still counted, so the `orphans` term of
    E(vault) sat frozen at +100 no matter how many repairs ran.

    Permits `patch` against the offered neighbours only, each introducing a
    wikilink to `orphan_title`.  Never overwrites, deletes, or creates.
    """
    allowed = {norm_path(p) for p in neighbour_paths if p}
    forbidden = frozenset({hub} if hub else set())
    return CapabilityBounds(
        name="orphan",
        allowed_ops=frozenset({OpType.patch}),
        target_predicate=lambda p: norm_path(p) in allowed,
        forbidden_paths=forbidden,
        content_guard=make_backlink_guard(orphan_title),
    )
