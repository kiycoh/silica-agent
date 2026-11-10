# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the note, tree and library routes share: is there room, is the name one a tree may
write, a change as the rows a drawer draws, and a written note's index refresh."""
from __future__ import annotations

import re
import threading

from fastapi import HTTPException

from silica.config import CONFIG
from silica.kernel.recall import note_index
from silica.ui.web import vectors


def has_room(notes: int, size: int) -> None:
    """A 400 past the vault's limits, the status `saved_page.save` reaches
    through /vault/save. A laptop sets none, so this passes there."""
    from silica.sources import saved_page

    short = saved_page.room(notes, size)["short"]
    if short:
        raise HTTPException(400, f"this vault has no room for that: {short}")


def tree_name_ok(name: str) -> bool:
    """A note's or a folder's name the tree may write: a workspace's rule
    (ADR-0112), and no leading dot, which the index reads as hidden."""
    from silica.kernel.base.syntax import NAME_UNSAFE_RE

    return bool(name) and not NAME_UNSAFE_RE.search(name) and not name.startswith(".")


BAD_NAME_WHY = "a name cannot be empty, start with a dot or hold / \\ : * ? \" < > | # ^ [ ]"


_DIFF_CONTEXT = 3
# A hard line cap, tail dropped with a count — a decided constant, not a
# deferral: past a few hundred lines a diff stops being reviewable in a
# drawer and the note itself is one click away.
MAX_DIFF_LINES = 800
# difflib opens every diff with a hunk header, but a gap marker only *means*
# something when lines were skipped above it — which is not the case when the
# first hunk starts at the top of the file (or at 0, for a create or a delete).
_HUNK_AT_TOP = re.compile(r"^@@ -[01](?:,\d+)? \+[01](?:,\d+)? @@")


def diff_rows(before: str, after: str) -> list[dict]:
    """Flat diff rows: `-` removed, `+` added, ` ` context, `@` gap."""
    import difflib

    rows: list[dict] = []
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                lineterm="", n=_DIFF_CONTEXT)
    for i, ln in enumerate(diff):
        if i < 2:
            continue  # the ---/+++ file headers difflib always emits first
        if ln.startswith("@@"):
            if not rows and _HUNK_AT_TOP.match(ln):
                continue  # nothing was skipped above the first line
            rows.append({"op": "@", "text": ""})
            continue
        rows.append({"op": ln[:1] or " ", "text": ln[1:]})
    return rows


# --- the index follows the person (ADR-0155 point 18) --------------------------
# A save comes every 800 ms of pause, and a re-embed is a paid call on hosted, so
# one refresh goes 60 s after a note's LAST save or when it is let go, never per
# save. It never runs on /note/save's thread and it asks no chat model.
_INDEX_AFTER_S = 60.0
_INDEX_LOCK = threading.Lock()
_INDEX_PENDING: dict[str, threading.Timer] = {}


def index_later(canon: str) -> None:
    """(Re)start the note's refresh. The vault rides along: a timer that fires
    after a switch must not read the same path in another vault."""
    key = canon.removesuffix(".md")
    timer = threading.Timer(_INDEX_AFTER_S, _index_due, (key, CONFIG.vault_path))
    timer.daemon = True
    with _INDEX_LOCK:
        if (old := _INDEX_PENDING.get(key)) is not None:
            old.cancel()
        _INDEX_PENDING[key] = timer
    timer.start()


def _index_due(key: str, vault: str) -> None:
    with _INDEX_LOCK:
        _INDEX_PENDING.pop(key, None)
    if vault == CONFIG.vault_path:
        index_now(key)


def let_go(canon: str) -> None:
    """The editor left this note: its pending refresh runs now, and a note with
    none pending costs nothing."""
    key = canon.removesuffix(".md")
    with _INDEX_LOCK:
        timer = _INDEX_PENDING.pop(key, None)
    if timer is not None:
        timer.cancel()
        index_now(key)


def index_now(key: str) -> None:
    vectors.refresh()  # the engine's section vectors, a store of their own
    note_index.index_note(key)
