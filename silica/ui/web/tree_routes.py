# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The rail's acts on many notes, the trash they leave, and what this session changed."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from silica.config import CONFIG
from silica.kernel.graph.mindmap import note_resolver
from silica.kernel.text import note_parts
from silica.kernel.write import session_changes
from silica.ui.web import note_ops, session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/tree/delete")
def tree_delete(payload: dict):
    """Delete a note, or every note the rail shows under a folder, from the
    tree row's menu (ADR-0123). The user's act, as /delete is (ADR-0092): no
    refusal for a linked note, journaled under user:/delete as ONE run, so
    the toast's undo, /undo and /revert each bring the whole set back.

    A folder is the notes the tree counts under it, from the builder the tree
    is drawn with, so the count the menu names is the count that goes; the
    folder's other files and the directory itself stay."""
    from silica.driver import DRIVER
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE
    from silica.tools.wrapped import delete_notes_with_restore_point

    note = str(payload.get("path") or "")
    folder = str(payload.get("folder") or "").strip().strip("/")
    many = payload.get("paths")
    if isinstance(many, list) and many:
        # the tree's selection (ADR-0126 point 7): one run, as a folder is
        paths = _tree_paths(many)
    elif note:
        paths = _tree_paths([note])
    elif folder:
        from silica.kernel.graph.graph_export import build_graph_data

        nodes, _edges = build_graph_data(folder=folder)
        paths = sorted(n["path"] for n in nodes if n.get("type") != "ghost" and n.get("path"))
        if not paths:
            raise HTTPException(404, "no notes in that folder")
    else:
        raise HTTPException(400, "name a note or a folder")
    # The turn's slot, as an import takes it: a turn reading or patching a
    # note this deletes would fail mid-answer on a file the person removed.
    with session.turn_slot("a turn is running: delete when it ends"):
        res = delete_notes_with_restore_point(
            [(p, DRIVER.read_note(p).content) for p in paths], USER_DELETE_SOURCE)
    if not res["deleted"]:
        # The driver's text can carry the vault's absolute path, which the
        # hosted pilot does not show its tenants (ADR-0085): logged, not sent.
        logger.warning("tree delete refused: %s", res.get("error"))
        raise HTTPException(409, "not deleted: the vault refused the delete")
    if "error" in res:
        logger.warning("tree delete stopped after %d of %d: %s",
                       len(res["deleted"]), len(paths), res["error"])
        res["error"] = f"{len(paths) - len(res['deleted'])} of {len(paths)} could not be deleted"
    return res


def _tree_paths(refs: list) -> list[str]:
    """The vault paths of the notes a tree request names, or 404 on the
    first it cannot resolve: the page sends the row's own id, so a miss is
    a note gone since the tree was drawn."""
    resolve = note_resolver()
    out = []
    for ref in refs:
        canon = resolve(str(ref))
        if not canon:
            raise HTTPException(404, "note not found in vault")
        out.append(canon)
    return out


@router.post("/tree/move")
def tree_move(payload: dict):
    """A selection dropped on a folder, `{paths, folder}`, or a note renamed
    in its row, `{path, name}` (ADR-0126 points 3 and 8). Each note keeps its
    name under `folder` (`""` is the vault root; a folder that does not exist
    is made); a note already there is skipped and counted. The user's act in
    ONE run under user:/move, as a tree delete is under user:/delete, so the
    toast's undo and /undo bring the whole set back.

    Refused before anything moves (409): a destination a note already holds,
    two notes of the set landing on one path, and a rename to a name another
    note carries anywhere in the vault, read without case, since the move
    writes the bare new name into every referrer and two notes of one name
    would send those links wherever the resolver's shortest path points. A
    change of case alone passes. Refused while a turn runs (409), by the slot
    an import takes. The driver's text for a refusal after the preflight can
    carry the vault's absolute path (ADR-0085): logged, not sent."""
    from silica.driver import DRIVER
    from silica.kernel.write.undo_journal import USER_MOVE_SOURCE
    from silica.tools.wrapped import linked_from, move_notes_with_restore_point, move_preflight

    skipped: list[str] = []
    if payload.get("name") is not None:
        refs = [payload["path"]] if payload.get("path") else list(payload.get("paths") or [])
        if len(refs) != 1:
            raise HTTPException(400, "a rename takes one note")
        name = " ".join(str(payload["name"]).split())
        if not note_ops.tree_name_ok(name):
            raise HTTPException(400, note_ops.BAD_NAME_WHY)
        canon = _tree_paths(refs)[0]
        folder = canon.rsplit("/", 1)[0] if "/" in canon else ""
        dst = f"{folder}/{name}.md" if folder else f"{name}.md"
        if dst == canon:
            return {"run": None, "moved": [], "skipped": [canon], "rewritten": [], "error": None}
        other = next((p for p in DRIVER.notes_named(name) if p != canon), None)
        by_title = note_resolver()(name)
        if other or by_title not in (None, canon):
            raise HTTPException(409, f"another note is called {name}: {other or by_title}")
        moves = [(canon, dst)]
    else:
        paths = payload.get("paths")
        if "folder" not in payload or not isinstance(paths, list) or not paths:
            raise HTTPException(400, "name the notes and the folder, or a note and its new name")
        folder = str(payload["folder"] or "").strip().strip("/")
        if folder and not all(note_ops.tree_name_ok(seg) for seg in folder.split("/")):
            raise HTTPException(400, note_ops.BAD_NAME_WHY)
        moves = []
        for canon in _tree_paths(paths):
            leaf = canon.rsplit("/", 1)[-1]
            dst = f"{folder}/{leaf}" if folder else leaf
            if dst == canon:
                skipped.append(canon)
            else:
                moves.append((canon, dst))
    why = move_preflight(moves)
    if why:
        raise HTTPException(409, why)
    if not moves:
        return {"run": None, "moved": [], "skipped": skipped, "rewritten": [], "error": None}
    # The notes whose links the move rewrites, read before it: the page
    # reloads the one it holds open, or the editor would save the old link back.
    own = {src for src, _ in moves}
    rewritten = sorted(set().union(*(linked_from(src) for src in own)) - own)
    with session.turn_slot("a turn is running: move when it ends"):
        res = move_notes_with_restore_point(moves, USER_MOVE_SOURCE)
    if res["error"]:
        logger.warning("tree move stopped after %d of %d: %s", len(res["moved"]), len(moves), res["error"])
        res["error"] = f"{len(moves) - len(res['moved'])} of {len(moves)} could not be moved"
    return {"run": res["run"], "moved": [{"from": a, "to": b} for a, b in res["moved"]],
            "skipped": skipped, "rewritten": rewritten, "error": res["error"]}


@router.post("/tree/undo")
def tree_undo(payload: dict):
    """The toast's undo for /tree/delete, /tree/move and /note/new: the run
    back, every note in it. Was /tree/undo-delete until ADR-0126."""
    from silica.kernel.write.undo_journal import (USER_DELETE_SOURCE, USER_MOVE_SOURCE, USER_WRITE_SOURCE,
                                                  get_undo_journal, revert_run)

    run = str(payload.get("run") or "")
    info = get_undo_journal().run_info(run) if run else None
    # The id comes from the page: only what the person did here is this
    # button's to take back; any other run is /revert's, with its own report.
    if not info or info["source"] not in (USER_DELETE_SOURCE, USER_MOVE_SOURCE, USER_WRITE_SOURCE):
        raise HTTPException(404, "nothing to undo")
    if info["reverted_at"]:
        raise HTTPException(409, "already undone")
    with session.turn_slot("a turn is running: undo when it ends"):
        res = revert_run(run)
    return {"restored": res["reverted"],
            "skipped": [s["path"] for s in res["skipped"] + res["errors"]]}


def _trash_items() -> list[dict]:
    """This vault's trash (ADR-0128), the vault spelled as a delete stamps it.
    `taken`: a note is at the item's path again, so its restore is refused."""
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.tools.wrapped import _vault_file

    items = get_undo_journal().trashed((CONFIG.vault_path or "").strip() or None)
    for t in items:
        t["taken"] = _vault_file(t["path"]).exists()
    return items


def _trash_item(payload: dict) -> dict:
    """The item a request names by run and path, or 404: both come from the
    page, and only what this vault's trash lists is the request's to touch."""
    run, path = str(payload.get("run") or ""), str(payload.get("path") or "")
    item = next((t for t in _trash_items() if t["run"] == run and t["path"] == path), None)
    if item is None:
        raise HTTPException(404, "not in the trash")
    return item


@router.get("/trash")
def trash_list():
    """The notes deleted from this vault that nothing has brought back."""
    return {"items": _trash_items()}


@router.post("/trash/restore")
def trash_restore(payload: dict):
    """One note back from the trash, not the run it was deleted in."""
    from silica.kernel.write.undo_journal import restore_from_trash

    item = _trash_item(payload)
    if item["taken"]:
        raise HTTPException(409, "a note is at that path now")
    with session.turn_slot("a turn is running: restore when it ends"):
        why = restore_from_trash(item)
    if why:
        # the driver's text can carry the vault's absolute path (ADR-0085)
        logger.warning("trash restore of %s refused: %s", item["path"], why)
        raise HTTPException(409, "the vault refused the restore")
    return {"restored": item["path"]}


@router.post("/trash/erase")
def trash_erase(payload: dict):
    """Delete for good one item, `{run, path}`, or the whole trash, `{all:
    true}`. No turn slot: it touches the journal and no note."""
    from silica.kernel.write.undo_journal import get_undo_journal

    items = _trash_items() if payload.get("all") is True else [_trash_item(payload)]
    journal = get_undo_journal()
    for t in items:
        journal.leave_trash(t["run"], t["path"])
    return {"erased": len(items)}


# --- what this session changed (GET /changes, /changes/diff) ------------------
# The ledger and the tally are the driver's (silica.kernel.write.session_changes),
# shared with the REPL's /changes. All that is added here is a display name and
# the line-by-line rendering, which only a drawer needs.

def _review_of(path: str) -> str:
    """The judge's sentence on a note the soft gate landed with `review:` set
    (kernel/write/validate.py), or "". Read off the file each time: keep
    clears it and a second write may set it again, and the ledger holds
    neither."""
    text = session_changes.current_text(path)
    if not text:
        return ""
    props, _ = note_parts.split_frontmatter(text)
    return str((props or {}).get("review") or "").strip()


def _change_rows() -> list[dict]:
    from silica.kernel.write.checkpoints import get_checkpoint_store

    # `undo_deletes` is what a write card's button says: the ledger's
    # "created" is a first touch by this process, the click is /undo <path>,
    # which reads the checkpoint store (CheckpointStore.undo_removes).
    store = get_checkpoint_store()
    return [{**r, "name": note_parts.clean_name(r["path"]), "review": _review_of(r["path"]),
             "undo_deletes": store.undo_removes(r["path"])}
            for r in session_changes.rows()]


@router.get("/changes")
def changes():
    """Every note this session has changed, oldest first, each with the
    judge's `review` sentence when the gate was unsure (audit P4)."""
    return _change_rows()


@router.post("/changes/keep")
def changes_keep(payload: dict):
    """The person looked at a write under review and keeps it: `review:` comes
    off the frontmatter through the same overwrite path /undo uses, so the
    ledger sees it and the diff stays honest."""
    from silica.driver import DRIVER
    from silica.kernel.write.templates import drop_props

    path = str(payload.get("path", ""))
    text = session_changes.current_text(path) if path else None
    if text is None:
        raise HTTPException(status_code=404, detail="no such note in this session's changes")
    cleared = drop_props(text, ("review",))
    if cleared != text:
        DRIVER.overwrite(path, cleared)
    return {"ok": True, "path": path}


@router.get("/changes/diff")
def changes_diff(path: str = ""):
    """One note's diff as flat rows: `-` removed, `+` added, ` ` context, `@` gap."""
    base = session_changes.snapshot().get(path)
    if base is None:
        # No baseline: this session never touched the note, so there is no diff
        # to read and the caller should show the note itself. Distinct from a
        # note that WAS touched and is now byte-identical again, which has a
        # baseline and an empty line list — the drawer says so in its own words,
        # and a write card must not silently degrade one into the other.
        return {"path": path, "name": note_parts.clean_name(path), "kind": "unchanged",
                "baseline": False, "lines": []}
    before, after = base.before or "", session_changes.current_text(path)
    added, removed = session_changes.tally(before, after or "")
    rows = note_ops.diff_rows(before, after or "")
    return {
        "path": path,
        "name": note_parts.clean_name(path),
        "kind": session_changes.kind(base.before, after, base.origin, bool(added or removed)),
        "from": base.origin,
        "baseline": True,
        "added": added,
        "removed": removed,
        "lines": rows[:note_ops.MAX_DIFF_LINES],
        "clipped": max(0, len(rows) - note_ops.MAX_DIFF_LINES),
    }
