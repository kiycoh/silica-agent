# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Wrapped tools — L0 atomics with domain invariants (Golden Rules) baked in.

From SILICA.md §4.4:
  Wrapped tools enforce invariants in the toolset, not in the system prompt.
  - silica_move always updates wikilinks (graph-safe).
  - silica_delete refuses a note other notes link to: that delete is the
    user's, with /delete (ADR-0092).

The restore points and the rollback they stand on are kernel.write.gate's.
silica_snapshot and silica_restore stay registered for CLI discoverability
only: the FSM holds the Txn itself.
"""
from __future__ import annotations

from typing import Annotated

import logging
from pathlib import Path
from typing import Any

from pydantic import Field

from silica.driver import DRIVER
from silica.kernel.write import gate
from silica.kernel.write.ops import InverseOp, InverseOpKind
from silica.kernel.write.ops_io import load_ops
from silica.tools import tool

logger = logging.getLogger(__name__)


@tool(cls="wrapped", collapse="eager", ingress=False, mutates="moved", note_args=("ref",))
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
    why = gate.move_preflight([(origin, target)])
    if why:
        return {"error": f"not moved: {why}"}
    # The restore point first, as silica_delete does: /undo and the GUI's
    # "move this note back" find the move here.
    try:
        journal = get_undo_journal()
        run_id = gate.open_run(CHAT_MOVE_SOURCE)
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


@tool(cls="wrapped", collapse="eager", ingress=False, mutates="deleted", note_args=("ref",),
      label=("delete", "ref"))
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
    referrers = gate.linked_from(path)
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
    res = gate.delete_with_restore_point(path, nc.content, CHAT_DELETE_SOURCE)
    res.pop("dangling", None)  # nothing linked the note, so no link dangles
    return res


@tool(cls="wrapped", collapse="eager", internal=True, ingress=False, label=("snapshot", "ops_json_path"))
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
        txn = gate.build_txn(ops)
    except Exception as e:
        return {"error": f"Snapshot failed: {e}"}

    return {
        "success": True,
        "txn_id": txn.id,
        "refs": [r.name for r in txn.refs],
        "created_paths": txn.created_paths,
        "inverses": txn.inverses_serialized,
    }


@tool(cls="wrapped", collapse="eager", internal=True, ingress=False, mutates="written",
      label=("restore", "txn_id"))
def silica_restore(txn_id: Annotated[str, Field(description='Transaction ID to restore (for audit log only)')], inverses: Annotated[list[dict], Field(description='InverseOp list from silica_snapshot result')]) -> dict[str, Any]:
    """Apply InverseOp list to rollback a transaction.

    Accepts the 'inverses' list produced by silica_snapshot — fully
    JSON-serialisable, no hidden Python objects.
    """
    return gate.apply_inverses(txn_id, inverses)
