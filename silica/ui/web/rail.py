# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The app's rail: the vault's tree as `kernel/graph/file_tree` shapes it, with the
rows that act. Here and not in the kernel because every row names this app: its
routes (/file, /raw), the hash router's grammar (#chat?note=), the buttons
app-shell.js wires, and the words a person reads. The graph frame draws the same
shape with none of them (file_tree.frame_tree)."""
from __future__ import annotations

import html
from urllib.parse import quote

from silica.kernel.base.formats import file_act
from silica.kernel.graph import file_tree


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

# A file row's mark (ADR-0129, amended 2026-11-08): only what Silica lacks is
# marked, since a row with nothing on it is a file Silica reads.
_FILE_MARKS = {
    "unread": ("not read", "Silica holds no text of this file"),
    "unindexed": ("not indexed", "Silica has not indexed this file yet: the next search reads it"),
}

# What the drawer says of the same file (app-notes.js fileSays, through VOCAB):
# a sentence per state, and an unread file by its kind, since that is what OCR or
# a conversion would change. Not the marks' words: a mark is two words on a row.
FILE_SAYS = {
    "read": "Silica reads its text.",
    "unindexed": "Silica has not indexed it yet: the next search reads it.",
    "pdf": "No text layer: Silica has not read it.",
    "picture": "A picture: Silica has not read it.",
    "unread": "Silica has not read it.",
}


def _head(folder: str, name: str, tally: str) -> str:
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
    return f'<summary draggable="true">{html.escape(name)}{tally}{more}{pub}{plan}</summary>'


def _note(nid: str, shown: str) -> str:
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


def _file(leaf: str, key: str, note: str | None, state: str = "") -> str:
    """A raw (`note` names it) or a file no note names (`note` None, keyed by its
    own path), with its extension."""
    stem, dot, ext = leaf.rpartition(".")
    shown = (f'{_RAW_GLYPH}<span>{html.escape(stem or leaf)}<span class="tree-ext">'
             f'{dot}{html.escape(ext if stem else "")}</span></span>')
    if note is None:
        mark = _FILE_MARKS.get(state)
        act = file_act(key) if state == "unread" else ""
        return (f'<div class="tree-row"><a class="tree-file" '
                f'href="/file?path={quote(key, safe="")}" '
                f'data-file="{html.escape(key, quote=True)}" data-state="{state}"'
                + (f' data-act="{act}"' if act else "") + f">{shown}"
                + (f'<span class="tree-mark" title="{mark[1]}">{mark[0]}</span>' if mark else "")
                + "</a></div>")
    return (f'<div class="tree-row"><a class="tree-file" '
            f'href="/raw?path={quote(note, safe="")}" '
            f'data-raw="{html.escape(key, quote=True)}" data-note="{html.escape(note, quote=True)}">'
            f"{shown}</a></div>")


def rail_tree(nodes: list[dict], raws: dict[str, list[str]] | None = None,
              files: dict[str, str] | None = None) -> str:
    """The app's rail: each note a link with its pin and "…", each folder its tools.

    `raws` is {a raw's vault path: the notes naming it} (ADR-0129, amended
    2026-11-04): each raw is a row in its own folder, with its extension, a link
    to the note that opens it. Not a `.tree-note`, which every search, count,
    selection and drag of the rail reads as a note.

    `files`, the laptop's (ADR-0129, amended 2026-11-08), is every other vault
    file with what Silica holds of it, `read`, `unread` or `unindexed`: a row as
    a raw's, keyed by the file and not a note, marked when Silica lacks its text."""
    tree = file_tree.grow(nodes, raws or {}, files)
    return f'<div id="file-tree">{file_tree.draw(tree, _head, _note, _file)}</div>'
