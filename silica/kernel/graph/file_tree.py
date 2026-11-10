# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault as a collapsible file tree: its shape, and the sidebar of the graph
frame, which only opens notes. The app's rail draws its own rows over the same
shape (silica/ui/web/rail.py), since each of them names a route or a control of
the app, which nothing in the kernel may.

Pure: nodes -> HTML. Folders become nested <details>/<summary> (native collapse, no
JS) whose summary carries the count of rows under them; notes show NAME without its
`.md` (the drawer states the path; the extension said nothing a reader of a rail of
notes did not already know, audit P19). Ghost nodes (type == "ghost" or empty path)
are unresolved links, not files, so they are skipped. Folders sort before notes at
each level; both groups sort case-insensitively.
"""
from __future__ import annotations

import html
from collections.abc import Callable


def grow(nodes: list[dict], raws: dict[str, list[str]], files: dict[str, str] | None = None) -> dict:
    """Folders as nested dicts, each with its `__notes__` rows: (leaf, id) for a
    note, (leaf, raw, the note naming it) for a raw, (leaf, path, None, state) for
    a file no note names."""
    root: dict = {}
    for n in nodes:
        if n.get("type") == "ghost":
            continue
        path = n.get("path") or ""
        if not path:
            continue
        *folders, leaf = path.split("/")
        cur = root
        for f in folders:
            cur = cur.setdefault(f, {})
        cur.setdefault("__notes__", []).append((leaf, n.get("id", path)))
    for raw, notes in raws.items():
        *folders, leaf = raw.split("/")
        cur = root
        for f in folders:
            cur = cur.setdefault(f, {})
        cur.setdefault("__notes__", []).append((leaf, raw, notes[0]))
    for rel, state in (files or {}).items():
        *folders, leaf = rel.split("/")
        cur = root
        for f in folders:
            cur = cur.setdefault(f, {})
        cur.setdefault("__notes__", []).append((leaf, rel, None, state))
    return root


def _count(tree: dict, notes_only: bool = False) -> int:
    # notes_only for the folder menu's delete, which names how many notes
    # go: tree_delete removes the notes, never a raw or another file
    return sum(1 for row in tree.get("__notes__", []) if not notes_only or len(row) == 2) + sum(
        _count(v, notes_only) for k, v in tree.items() if k != "__notes__")


def _tally(tree: dict) -> str:
    """A folder's number: every row under it. The notes alone ride on it as
    `data-notes`, which the folder's delete names, only where files make the
    number larger."""
    total, notes = _count(tree), _count(tree, True)
    apart = f' data-notes="{notes}"' if notes != total else ""
    return f'<span class="tree-n"{apart}>{total}</span>'


def draw(tree: dict, folder_head: Callable[[str, str, str], str], note_row: Callable[[str, str], str],
         file_row: Callable[..., str], prefix: str = "") -> str:
    """The rows under `tree`: `folder_head(folder, name, tally)` is the <details>'
    <summary>, `note_row(id, shown)` a note's row, `file_row(leaf, *rest)` a raw's
    or a file's, with the rest of its row as `grow` keeps it."""
    out = []
    for name in sorted((k for k in tree if k != "__notes__"), key=str.lower):
        folder = f"{prefix}{name}/"
        out.append(f"<details>{folder_head(folder, name, _tally(tree[name]))}")
        out.append(draw(tree[name], folder_head, note_row, file_row, folder))
        out.append("</details>")
    for leaf, nid, *raw_note in sorted(tree.get("__notes__", []), key=lambda x: x[0].lower()):
        if raw_note:
            out.append(file_row(leaf, nid, *raw_note))
            continue
        out.append(note_row(nid, leaf[:-3] if leaf.endswith(".md") else leaf))
    return "".join(out)


def _frame_head(_folder: str, name: str, tally: str) -> str:
    return f"<summary>{html.escape(name)}{tally}</summary>"


def _frame_note(nid: str, shown: str) -> str:
    # A button and not a div: the tree is the primary route into a note, and as a
    # click-only div every one of a vault's notes reported to the accessibility
    # tree as `generic` and sat outside the tab order. Nothing else changes: the
    # class, the data-id and the delegated `closest('.tree-note')` handlers are
    # the same, and the CSS resets the button back to a row.
    return (f'<button type="button" class="tree-note" data-id="{html.escape(nid, quote=True)}">'
            f"{html.escape(shown)}</button>")


def frame_tree(nodes: list[dict]) -> str:
    """The tree in the graph frame's sidebar. No pin and no menu: they would point
    at a rail that document does not have, and it has no router to answer a hash.
    Notes only, so it has no file rows to draw."""
    return f'<div id="file-tree">{draw(grow(nodes, {}), _frame_head, _frame_note, lambda *_row: "")}</div>'
