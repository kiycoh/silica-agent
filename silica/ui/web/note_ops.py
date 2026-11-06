# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the note and tree routes both ask: is there room, is the name one a tree may write,
and a change as the rows a drawer draws."""
from __future__ import annotations

import re

from fastapi import HTTPException


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
    from silica.kernel.workspaces import _BAD_NAME

    return bool(name) and not _BAD_NAME.search(name) and not name.startswith(".")


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
