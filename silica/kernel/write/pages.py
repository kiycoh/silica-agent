# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A page the chat writes (ADR-0171): one self-contained HTML file under
`Pages/`, or a small site in `Pages/<slug>/`, beside the notes and never one of
them.

Written past the driver, which is a note store: its create stamps a note's type,
indexes the file as a note and, over the Obsidian bridge, hands it to a plugin
that knows only Markdown. What a write owes the person is kept all the same: the
lease, the Changes row, and a restore point in the checkpoint store, which keys
any path, so the card's revert and `/undo` take a page back as they do a note.
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
from pathlib import Path, PurePosixPath

from silica.kernel.base.durable import atomic_write_bytes
from silica.kernel.base.layout import contain_in_vault
from silica.kernel.write import session_changes
from silica.kernel.write.checkpoints import get_checkpoint_store

logger = logging.getLogger(__name__)

PAGES_DIR = "Pages"

# What a page may not reach for (ADR-0171 point 3): anything on another host.
# The route's CSP blocks every request but one: the page's script navigating its
# own frame, which carries data out in the address and which only the app's
# frame-src holds (security.APP_FRAMES), in the app and not in a tab of its own.
# So the plain spellings of a navigation are refused here too; an obfuscated one
# passes this check, and that is the frame-src's to stop. An `xmlns` and a URL
# in the text are no request, and pass.
_REMOTE = r"""\s*["']?\s*(?:(?:https?|wss?|ftp):)?//"""
_NETWORK = re.compile(
    r"\b(?:src|href|srcset|poster|action|formaction|data|background|xlink:href|ping|manifest)\s*=" + _REMOTE
    + r"|\burl\s*(?:\(|=)" + _REMOTE
    + r"""|@import\s+["']\s*(?:(?:https?|wss?|ftp):)?//"""
    + r"|\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\s*\("
    + r"|\blocation\s*(?:=(?!=)|\.\s*(?:href\s*=(?!=)|assign\s*\(|replace\s*\())|\bwindow\s*\.\s*open\s*\("
    + r"""|http-equiv\s*=\s*["']?\s*refresh""",
    re.IGNORECASE)

# Written by code and replaced on each write, so a revision cannot keep a stale list.
_FOOTER = re.compile(r"\s*<footer data-silica-sources\b.*?</footer>", re.IGNORECASE | re.DOTALL)


def page_path(path: str) -> str:
    """`path` as a page's vault path, or ValueError saying what one looks like."""
    p = PurePosixPath(str(path or "").strip().replace("\\", "/"))
    parts = p.parts
    if len(parts) < 2 or parts[0] != PAGES_DIR or p.suffix.lower() != ".html" \
            or any(part.startswith(".") for part in parts):
        raise ValueError(f"a page is {PAGES_DIR}/<slug>.html, or {PAGES_DIR}/<slug>/<name>.html "
                         "for one page of a site")
    return p.as_posix()


def is_page(path: str) -> bool:
    try:
        page_path(path)
    except ValueError:
        return False
    return True


def page_file(vault: Path, path: str) -> Path | None:
    """The page on disk, None when `path` names none inside `vault`."""
    try:
        rel = page_path(path)
    except ValueError:
        return None
    root = vault.resolve()
    found = (root / rel).resolve()
    return found if found.is_relative_to(root) and found.is_file() else None


def network_refs(html: str) -> list[str]:
    """The places in `html` that would reach another host, as written."""
    return [m.group(0).strip() for m in _NETWORK.finditer(html or "")]


def with_footer(html: str, sources: list[str]) -> str:
    """`html` closing on the list of the notes it was built from."""
    items = "".join(f"<li>{_html.escape(s)}</li>" for s in sources)
    footer = ('<footer data-silica-sources style="margin-top:2.5rem;font:13px/1.5 system-ui,sans-serif">'
              f"<p>Built by Silica from these notes in your vault:</p><ul>{items}</ul></footer>")
    body = _FOOTER.sub("", html or "")
    at = body.lower().rfind("</body>")
    return f"{body[:at]}{footer}\n{body[at:]}" if at >= 0 else f"{body}\n{footer}\n"


def write_page(vault: Path, path: str, html: str, sources: list[str]) -> dict:
    """Write the page and its footer; a dict with `error` when it did not land.
    An existing page is replaced: a revision, which the card takes back."""
    from silica.kernel.workqueue import path_lease

    try:
        rel = contain_in_vault(page_path(path), vault)
    except ValueError as e:
        return {"error": f"Refused path '{path}': {e}"}
    if bad := network_refs(html):
        return {"error": "Not written: a page reaches nothing outside itself and goes nowhere but "
                         f"to its own pages, and the viewer blocks the rest: {', '.join(dict.fromkeys(bad))[:300]}. "
                         "Inline the CSS, JS, fonts and images (data: URIs), link a vault image or "
                         "another page of the site by a relative path, and move between parts of a "
                         "page without changing its address."}
    if "<" not in (html or ""):
        return {"error": "Not written: `html` holds no HTML document"}
    text = with_footer(html, sources)
    full = vault / rel
    with path_lease(rel):
        prior = full.read_text(encoding="utf-8") if full.is_file() else None
        full.parent.mkdir(parents=True, exist_ok=True)
        session_changes.touched(rel, prior)
        atomic_write_bytes(full, text.encode("utf-8"))
        try:
            depth, ok = get_checkpoint_store().push(rel, prior, text), True
        except Exception as e:
            # As checkpoints.checkpoint_landed: the page landed, so it is not
            # reported as failed, and the result says /undo has no step for it.
            logger.warning("checkpoint push failed for '%s': %s", rel, e)
            depth, ok = None, False
    return {"op": "write", "success": True, "path": rel, "created": prior is None,
            "checkpoint_depth": depth, "checkpoint_ok": ok}


def _removed(vault: Path, rel: str) -> Path:
    """Where /undo keeps a page the chat created and /undo removed, so the next
    /undo brings it back, as the trash does a note (ADR-0128): the vault's own
    dot-folder, which no tree, search or export shows. One per path, the last."""
    return vault / ".silica" / "pages-removed" / rel


def undo_page(vault: Path, rel: str) -> str:
    """/undo on a page, said as the person reads it: the version before the last
    write, or no page when the chat created it, or that page back again."""
    store = get_checkpoint_store()
    full, kept = vault / rel, _removed(vault, rel)
    on_disk = full.read_text(encoding="utf-8") if full.is_file() else None
    content = store.undo(rel, current=on_disk)
    if content is None:
        if on_disk is not None or not kept.is_file():
            return f"Nothing to undo for {rel} (already at original)."
        os.replace(kept, full)
        store.push(rel, None, full.read_text(encoding="utf-8"))
        return f"Restored: {rel} (/undo had removed it)"
    if store.absent_floor(rel):
        kept.parent.mkdir(parents=True, exist_ok=True)
        os.replace(full, kept)
        return f"Undone: {rel} removed (the chat created it); /undo {rel} brings it back"
    atomic_write_bytes(full, content.encode("utf-8"))
    return f"Undone: {rel}"
