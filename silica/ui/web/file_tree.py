# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault as a collapsible file tree, in the two places one is drawn: the app's
rail, whose rows act, and the sidebar of the graph frame, which only opens notes.

Pure: nodes -> HTML. Folders become nested <details>/<summary> (native collapse, no
JS) whose summary carries the note count under them; notes show NAME without its
`.md` (the drawer states the path; the extension said nothing a reader of a rail of
notes did not already know, audit P19). Ghost nodes (type == "ghost" or empty path)
are unresolved links, not files, so they are skipped. Folders sort before notes at
each level; both groups sort case-insensitively.
"""
from __future__ import annotations

import html
from collections.abc import Callable
from urllib.parse import quote


def _more_button(target: str, shown: str, *, folder: bool) -> str:
    """The row's menu (ADR-0123): its workspaces (ADR-0112 point 6, the ◇
    that stood here) and its delete. One "…" and not a glyph per action, as a
    chat row has: a destructive action as the third of three 20 px glyphs
    beside pin and ◇ was one slip from a click meant for its neighbour. The
    rail's only, as the pin is."""
    attr = "data-folder" if folder else "data-note"
    what = "the folder " if folder else ""
    return (f'<button type="button" class="tree-more" {attr}="{html.escape(target, quote=True)}" '
            f'aria-haspopup="menu" aria-expanded="false" title="workspaces, delete" '
            f'aria-label="more for {what}{html.escape(shown, quote=True)}">…</button>')


# The raw's glyph in the rail (ADR-0129, amended 2026-11-04): a page with its
# corner folded, the shape a file manager gives a document.
_RAW_GLYPH = ('<svg aria-hidden="true" width="13" height="13" viewBox="0 0 16 16" fill="none" '
              'stroke="currentColor" stroke-width="1.3" stroke-linejoin="round">'
              '<path d="M4 1.75h5.25L12.5 5v9.25h-8.5z"></path><path d="M9 1.75V5.25h3.5"></path>'
              '<path d="M6.25 8.5h4M6.25 11h4"></path></svg>')


def _grow(nodes: list[dict], raws: dict[str, list[str]]) -> dict:
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
    return root


def _count(tree: dict) -> int:
    # notes only, raws not: the folder menu's delete names this count, and
    # tree_delete removes the notes the tree counts, never their raws
    return sum(1 for row in tree.get("__notes__", []) if len(row) == 2) + sum(
        _count(v) for k, v in tree.items() if k != "__notes__")


def _draw(tree: dict, folder_head: Callable[[str, str, int], str], note_row: Callable[[str, str], str],
          prefix: str = "") -> str:
    """The rows under `tree`: `folder_head(folder, name, count)` is the <details>'
    <summary>, `note_row(id, shown)` a note's row."""
    out = []
    for name in sorted((k for k in tree if k != "__notes__"), key=str.lower):
        folder = f"{prefix}{name}/"
        out.append(f"<details>{folder_head(folder, name, _count(tree[name]))}")
        out.append(_draw(tree[name], folder_head, note_row, folder))
        out.append("</details>")
    for leaf, nid, *raw_note in sorted(tree.get("__notes__", []), key=lambda x: x[0].lower()):
        if raw_note:
            stem, dot, ext = leaf.rpartition(".")
            out.append(
                f'<div class="tree-row"><a class="tree-file" '
                f'href="/raw?path={quote(raw_note[0], safe="")}" '
                f'data-raw="{html.escape(nid, quote=True)}" data-note="{html.escape(raw_note[0], quote=True)}">'
                f'{_RAW_GLYPH}'
                f'<span>{html.escape(stem or leaf)}<span class="tree-ext">{dot}{html.escape(ext if stem else "")}</span></span></a></div>')
            continue
        out.append(note_row(nid, leaf[:-3] if leaf.endswith(".md") else leaf))
    return "".join(out)


def _frame_head(_folder: str, name: str, count: int) -> str:
    return f'<summary>{html.escape(name)}<span class="tree-n">{count}</span></summary>'


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
    at a rail that document does not have, and it has no router to answer a hash."""
    return f'<div id="file-tree">{_draw(_grow(nodes, {}), _frame_head, _frame_note)}</div>'


def _rail_head(folder: str, name: str, count: int) -> str:
    more = _more_button(folder.rstrip("/"), name, folder=True)
    # the folder's globe (ADR-0143 point 2), drawn hidden: app-public.js
    # shows it where the account gets Public page and the folder is not Silica's
    pub = (f'<button type="button" class="tree-pub" hidden aria-pressed="false" '
           f'data-pub-folder="{html.escape(folder.rstrip("/"), quote=True)}" '
           f'aria-label="make the folder {html.escape(name, quote=True)} a wiki"></button>')
    # the rail's one folder action, Learn's "Plan this folder" (ADR-0081 §17)
    plan = (f'<button type="button" class="tree-plan" data-folder="{html.escape(folder, quote=True)}" '
            f'title="plan this folder in Memorize" aria-label="plan the folder {html.escape(name, quote=True)}">'
            f"plan</button>")
    # a note's <a> drags by itself; a summary only when told, and the
    # rail drops a folder on a workspace row as its menu adds one
    return (f'<summary draggable="true">{html.escape(name)}'
            f'<span class="tree-n">{count}</span>{more}{pub}{plan}</summary>')


def _rail_note(nid: str, shown: str) -> str:
    # The URL is the state (audit P7), so a middle click opens the note in a second
    # tab and a plain click is intercepted by the rail's own handler. A `.tree-row`
    # wraps it and not a button in a button, which is what a nested control would be.
    return (
        f'<div class="tree-row"><a class="tree-note" href="#chat?note={quote(nid, safe="")}" '
        f'data-id="{html.escape(nid, quote=True)}">{html.escape(shown)}</a>'
        f'<button type="button" class="tree-pin" aria-pressed="false" '
        f'data-pin="{html.escape(nid, quote=True)}" '
        f'title="keep this note in the rail" '
        f'aria-label="pin {html.escape(shown, quote=True)}">'
        f'<svg aria-hidden="true" width="11" height="11" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f'<line x1="12" y1="17" x2="12" y2="22"></line>'
        f'<path d="M5 17h14l-2-4V4H7v9z"></path></svg></button>'
        f'<button type="button" class="tree-pub" hidden aria-pressed="false" '
        f'data-pub="{html.escape(nid, quote=True)}" '
        f'aria-label="put {html.escape(shown, quote=True)} in its wiki"></button>'
        f'{_more_button(nid, shown, folder=False)}</div>'
    )


def rail_tree(nodes: list[dict], raws: dict[str, list[str]] | None = None) -> str:
    """The app's rail: each note a link with its pin and "…", each folder its tools.

    `raws` is {a raw's vault path: the notes naming it} (ADR-0129, amended
    2026-11-04): each raw is a row in its own folder, with its extension, a link
    to the note that opens it. Not a `.tree-note`, which every search, count,
    selection and drag of the rail reads as a note."""
    return f'<div id="file-tree">{_draw(_grow(nodes, raws or {}), _rail_head, _rail_note)}</div>'
