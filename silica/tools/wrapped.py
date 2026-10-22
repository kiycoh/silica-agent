# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Wrapped tools — L0 atomics with domain invariants (Golden Rules) baked in.

From SILICA.md §4.4:
  Wrapped tools enforce invariants in the toolset, not in the system prompt.
  - silica_move always updates wikilinks (graph-safe).
  - silica_delete refuses a note other notes link to: that delete is the
    user's, with /delete (ADR-0092).

C3 rollback strategy (ADR-0009):
  - write ops   → InverseOp(delete_created, path)
  - patch/overwrite ops → InverseOp(restore_version, path, version=N)
  - Txn.inverses: list[InverseOp] replaces the ad-hoc created_paths field.

C3 clarification: silica_snapshot DOES NOT leak _txn_obj through the tool
registry. The orchestrator holds the Txn directly (it calls snapshot
programmatically and receives the return value). The tool is kept for CLI
discoverability only — the FSM bypasses it.
"""
from __future__ import annotations

from typing import Annotated

import logging
from pathlib import Path
from typing import Any

from pydantic import Field

from silica.driver import DRIVER
from silica.driver.base import NoteRef, Txn
from silica.kernel.write.ops import InverseOp, InverseOpKind, OpType, Op
from silica.kernel.write.ops_io import parse_ops, load_ops
from silica.tools import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# silica_move
# ---------------------------------------------------------------------------

@tool(cls="wrapped", collapse="eager")
def silica_move(ref: Annotated[str, Field(description='Name or path of the note to move')], to: Annotated[str, Field(description='Destination path')]) -> dict[str, Any]:
    """Move/rename a note safely. Obsidian updates all wikilinks (graph-safe).
    `to` without ".md" is a folder, and the note keeps its name there, unless
    its last part is the note's own name."""
    # Both readings of a bare target were seen on 2026-09-28 (prompt-rules
    # probe): "Corsi/Machine Learning/Archivio/Lezione 30" meant the note,
    # "Corsi/Machine Learning/Archivio" meant the folder the user named. Taken
    # literally the first made a file Obsidian does not list and the second
    # renamed the note to Archivio.md. What moves is a note, so the target is
    # one, and a last part that is not the note's name is where it goes.
    from silica.config import CONFIG
    from silica.kernel.write.undo_journal import CHAT_MOVE_SOURCE, get_undo_journal

    try:
        origin = DRIVER.read_note(ref).ref.path or str(ref)
    except Exception as e:
        return {"error": str(e)}  # the same refusal DRIVER.move would give
    target = to.strip()
    if not target.lower().endswith(".md"):
        name = Path(origin).name
        last = target.rstrip("/").rsplit("/", 1)[-1]
        if not target.endswith("/") and last.casefold() == Path(name).stem.casefold():
            target += ".md"
        else:
            target = f"{target.rstrip('/')}/{name}" if target.rstrip("/") else name
    # Before the restore point: DRIVER.move replaces a file at the destination
    # (see move_preflight), and a note the chat moved onto another was gone.
    why = move_preflight([(origin, target)])
    if why:
        return {"error": f"not moved: {why}"}
    # The restore point first, as silica_delete does: /undo and the GUI's
    # "move this note back" find the move here.
    try:
        journal = get_undo_journal()
        run_id = journal.start_run(source=CHAT_MOVE_SOURCE,
                                   vault=(CONFIG.vault_path or "").strip() or None)
        journal.record(run_id, InverseOp(kind=InverseOpKind.move_back, path=origin,
                                         to_path=target), post_hash=None)
    except Exception as e:
        return {"error": f"not moved: no restore point could be recorded ({e})"}
    try:
        DRIVER.move(ref, target)
        return {"success": True, "moved": ref, "to": target}
    except Exception as e:
        journal.mark_reverted(run_id)  # nothing moved: nothing to move back
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# moving several notes as the user's act (ADR-0126)
# ---------------------------------------------------------------------------

def _vault_file(rel: str) -> Path:
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
        target = _vault_file(dst)
        if not target.exists():
            continue
        try:
            if target.samefile(_vault_file(src)):
                continue  # `Indice.md` to `indice.md` on a disk that reads them as one
        except OSError:
            pass
        return f"{dst} already exists"
    return None


def explicit_links(path: str) -> bool:
    """Write the path into each bare wikilink of `path` whose name several
    notes carry, so the note means the same notes after it moves: the
    resolver reads a bare name as the same folder's namesake first, and a
    move out of that folder would send the link to another folder's. A
    unique name stays bare, as the person wrote it. Body links only: the
    frontmatter's are the move's own rewrite (rename.py). Returns whether
    it wrote. Under Loss.RENAME, the one reason a link target may change."""
    from silica.kernel.link.rename import _WIKILINK_FULL_RE, _build_skip_mask, _span_is_clear
    from silica.kernel.write.preserve import Loss

    content = DRIVER.read_note(path).content
    mask = _build_skip_mask(content)
    resolved = {r.name.lower(): r.path for r in DRIVER.links(path) if r.path}
    out: list[str] = []
    at = 0
    for m in _WIKILINK_FULL_RE.finditer(content):
        target = m.group(2).strip()
        if not target or "/" in target or not _span_is_clear(mask, m.start(), m.end()):
            continue
        if len(DRIVER.notes_named(target)) < 2:
            continue
        to = resolved.get(target.lower())
        if not to:
            continue
        out.append(content[at:m.start()])
        out.append(f"{m.group(1)}[[{to.removesuffix('.md')}{m.group(3) or ''}{m.group(4) or ''}]]")
        at = m.end()
    if not out:
        return False
    out.append(content[at:])
    DRIVER.overwrite(path, "".join(out), loss=Loss.RENAME)
    return True


def move_notes_with_restore_point(moves: list[tuple[str, str]], source: str) -> dict[str, Any]:
    """silica_move's restore point over several (from, to) pairs as ONE
    journal run (ADR-0126 point 3): the toast's undo and /undo bring the
    whole set back. The caller ran move_preflight; what this meets is the
    driver's own refusal.

    Stops at the first note the driver refuses. The notes before it stay
    moved and in the run. The refused note's inverse leaves the run only
    when nothing moved, its file where it was and the destination empty; a
    move torn after the rename, with the file at the destination and the
    referrers half rewritten, keeps it, or the note could not be brought
    back (silica_move marked the run reverted on any exception, which was
    wrong for that case)."""
    from silica.config import CONFIG
    from silica.kernel.write.undo_journal import get_undo_journal

    try:
        journal = get_undo_journal()
        run_id = journal.start_run(source=source,
                                   vault=(CONFIG.vault_path or "").strip() or None)
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
            explicit_links(src)
            DRIVER.move(src, dst)
        except Exception as e:
            error = str(e)
            if _vault_file(src).exists() and not _vault_file(dst).exists():
                try:
                    journal.drop_inverse(run_id, src)
                except Exception as drop_error:
                    # Tolerated: the notes already moved must still be reported
                    # with their run. What stays is a move_back revert_run
                    # refuses, since the origin is there (silica_restore).
                    logger.warning("move: could not drop the inverse of %s: %s", src, drop_error)
            break
        moved.append((src, dst))
    if not journal.inverses_for(run_id):
        journal.mark_reverted(run_id)  # nothing moved: nothing to move back later
    return {"run": run_id, "moved": moved, "error": error}


# ---------------------------------------------------------------------------
# silica_delete
# ---------------------------------------------------------------------------

@tool(cls="wrapped", collapse="eager")
def silica_delete(ref: Annotated[str, Field(description='Name or path of the note to delete')]) -> dict[str, Any]:
    """Delete a note that no other note links to. Revertible: /undo (or
    /revert) recreates it from the copy taken before the delete. A note other
    notes link to is not deleted: the result names them, and the user deletes
    it with /delete <path>."""
    # `confirm` was removed 2026-10-11 (ADR-0092): both recorded chat deletes
    # passed confirm=true on their first call, so it guarded nothing. A linked
    # note is the user's to delete, and no argument, list or token completes
    # that delete here, in this turn or a later one: a list the model copies
    # back would be the same ritual with more characters.
    from silica.kernel.write.undo_journal import CHAT_DELETE_SOURCE

    try:
        nc = DRIVER.read_note(ref)
    except Exception as e:
        return {"error": str(e)}  # the delete would refuse it the same way
    path = nc.ref.path or str(ref)
    referrers = linked_from(path)
    if referrers:
        n = len(referrers)
        return {
            "error": f"not deleted: {n} {'note links' if n == 1 else 'notes link'} to {Path(path).stem}",
            "linked_from": referrers,
            # The note the refusal is about, resolved: the chat card's "delete
            # anyway" sends /delete with it, where `ref` may be a bare name.
            "path": path,
            "next": ("Deleting a note other notes link to is the user's act. Tell the user "
                     f"which notes link to it. They delete it with /delete {path}, and "
                     "/undo brings it back."),
        }
    res = delete_with_restore_point(path, nc.content, CHAT_DELETE_SOURCE)
    res.pop("dangling", None)  # nothing linked the note, so no link dangles
    return res


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
    from silica.config import CONFIG
    from silica.kernel.write.undo_journal import ABSENT_HASH, get_undo_journal

    paths = [p for p, _ in notes]
    pre = DRIVER.graph_snapshot([NoteRef(name=Path(p).stem, path=p) for p in paths])
    own = {p.removesuffix(".md") for p in paths}
    around = [k for k in pre.link_counts if k not in own]
    # The restore point first: a delete that cannot be taken back is the one
    # this tool no longer does (until 2026-09-28 it took none, and /undo could
    # not bring the note back).
    try:
        journal = get_undo_journal()
        run_id = journal.start_run(source=source,
                                   vault=(CONFIG.vault_path or "").strip() or None)
    except Exception as e:
        return {"error": f"not deleted: no restore point could be recorded ({e})", "deleted": []}
    deleted: list[str] = []
    for path, content in notes:
        try:
            journal.record(run_id, InverseOp(kind=InverseOpKind.recreate_deleted, path=path,
                                             prior_content=content), post_hash=ABSENT_HASH)
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


# ---------------------------------------------------------------------------
# build_txn — internal helper (not a tool)
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
    from silica.kernel.vault_manifest import seed_mirror_copy

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

    # Txn id comes from the driver; rollback is content-based (prior_content).
    base_txn = DRIVER.snapshot_versions(patch_refs)

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
    txn = Txn(
        id=base_txn.id,
        refs=patch_refs,
        created_paths=created_paths,
        inverses=inverses,
    )
    return txn


# ---------------------------------------------------------------------------
# silica_snapshot
# ---------------------------------------------------------------------------

@tool(cls="wrapped", collapse="eager", internal=True)
def silica_snapshot(ops_json_path: Annotated[str, Field(description='Path to validated operations JSON to snapshot before writing')]) -> dict[str, Any]:
    """Snapshot the current state of notes before they are modified.

    Builds InverseOp entries (C3):
      - write ops   → delete_created(path)     — rollback by deleting the new note
      - patch / overwrite ops → restore_version(path, N) — rollback via history:restore

    The orchestrator holds the returned Txn object directly.
    The tool result is JSON-serialisable (no _txn_obj leak per addendum note).
    """
    try:
        ops = load_ops(ops_json_path)
    except Exception as e:
        return {"error": f"Failed to load operations for snapshot: {e}"}

    try:
        txn = build_txn(ops)
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}

    return {
        "success": True,
        "txn_id": txn.id,
        "refs": [r.name for r in txn.refs],
        "created_paths": txn.created_paths,
        "inverses": txn.inverses_serialized,
        # _txn_obj intentionally absent — orchestrator calls build_txn() directly
    }


# ---------------------------------------------------------------------------
# silica_restore (real tool, usable by YAML recipe engine at S3.3)
# ---------------------------------------------------------------------------

@tool(cls="wrapped", collapse="eager", internal=True)
def silica_restore(txn_id: Annotated[str, Field(description='Transaction ID to restore (for audit log only)')], inverses: Annotated[list[dict], Field(description='InverseOp list from silica_snapshot result')]) -> dict[str, Any]:
    """Apply InverseOp list to rollback a transaction.

    Accepts the 'inverses' list produced by silica_snapshot — fully
    JSON-serialisable, no hidden Python objects.
    """
    from silica.kernel.write.preserve import Loss

    errors: list[str] = []
    applied: list[str] = []

    for raw in inverses:
        try:
            inv = InverseOp(**raw)
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
                elif _vault_file(path).exists():
                    errors.append(f"move_back: {path} is taken by a note written since")
                else:
                    DRIVER.move(inv.to_path, path)
                    applied.append(f"moved_back:{inv.to_path}->{path}")

        except Exception as e:
            errors.append(f"Inverse op {inv.kind} on {path} failed: {e}")
            logger.error("Rollback error: %s", e)

    return {
        "success": not errors,
        "txn_id": txn_id,
        "applied": applied,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# silica_cleanup (real tool for S3.3 YAML recipe)
# ---------------------------------------------------------------------------

@tool(cls="wrapped", collapse="eager", internal=True)
def silica_cleanup(inbox_file: Annotated[str, Field(description='Vault-relative path of the inbox file to archive')], done_dir: Annotated[str, Field(description="Destination folder; empty resolves this vault's archive root")] = "") -> dict[str, Any]:
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
    from silica.kernel.vault_manifest import archive_path_for, active_write_dir, within

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
