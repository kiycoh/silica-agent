# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The chat's page writer (ADR-0171): an HTML page built from the notes, into
the vault's Pages/ folder, with a write card and its revert."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from silica.tools import tool


@tool(cls="composed", collapse="eager", ingress=False, mutates="written", note_args=("path",),
      label=("write page", "path"))
def silica_write_page(
    path: Annotated[str, Field(description="Pages/<slug>.html, or Pages/<slug>/<name>.html for each page of a site")],
    html: Annotated[str, Field(description="The whole page, one HTML document with its CSS and JS inline. No request "
                                           "of any kind: no CDN, web font, remote image or fetch. A vault image by a "
                                           "relative path is fine, and so is a data: URI. Pages of a site link each "
                                           "other by relative path")],
    sources: Annotated[list[str], Field(description="The notes the page was built from, by path, after reading "
                                                    "them: the page's footer lists them")],
) -> dict[str, Any]:
    """Write a self-contained HTML page (or one page of a small site) into the
    vault, built from notes you read first, as for any answer. The person views
    it in the app, sandboxed. A path that exists is replaced: to revise a page,
    read it with silica_read and write it back whole. Revertible with /undo."""
    from silica.config import CONFIG
    from silica.kernel.recall.resolve import note_resolver
    from silica.kernel.write.pages import write_page

    resolve = note_resolver()
    found = {s: resolve(s) for s in dict.fromkeys(s.strip() for s in sources or () if s.strip())}
    notes = [p for p in dict.fromkeys(found.values()) if p]
    if not notes:
        # The footer is the page's citations: a page that names no note of the
        # vault cannot show where it came from (PRODUCT.md, principle 1).
        return {"error": "Not written: a page is built from notes. Read the notes it draws on and "
                         "name them by path in `sources`."}
    out = write_page(Path(CONFIG.vault_path), path, html, notes)
    if missing := [s for s, p in found.items() if not p]:
        out["warning"] = "not notes of this vault, left out of the footer: " + ", ".join(missing)[:200]
    return out
