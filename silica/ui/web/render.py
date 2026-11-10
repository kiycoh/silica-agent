# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The app's binding of the renderer: what a note link and an image are on this origin, and
the file /asset serves. `silica.kernel.text.render` holds the grammar and knows no route."""
from __future__ import annotations

import html as _html
from pathlib import Path
from urllib.parse import quote as _quote

from silica.config import CONFIG
from silica.kernel import vault_files
from silica.kernel.recall.resolve import answer_resolver
from silica.kernel.text import render as ofm

# Vault attachments the drawer may inline; served only through /asset.

# An SVG is a document, not just a picture: an <img> context runs none of its
# scripts, but /asset is a same-origin URL a note can link and the address bar
# can reach, and a navigated SVG runs them on the GUI's origin. `sandbox` puts
# any such navigation in an opaque origin with scripting off, and default-src
# 'none' stops the file fetching anything; neither affects an <img> load, so
# inline vault images keep rendering. nosniff pins the guessed type, which is
# already correct for every extension above.
ASSET_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}

# The raws the drawer shows in the browser's own viewer (ADR-0129 point 1);
# every other one is a download.
# shortcut: PDF only, the one type checked in an iframe under ASSET_HEADERS'
# sandbox (Chrome 156, Firefox 158, 2026-11-03); images and audio, which the
# ADR also lets open, join when that is checked for them.
RAW_OPENS = (".pdf",)


def anchor(path: str, display: str) -> str:
    return (
        f'<a class="note-link" data-path="{_html.escape(path, quote=True)}">'
        f"{_html.escape(display)}</a>"
    )


def asset_url(target: str) -> str:
    return "/asset?path=" + _quote(target)


def linkify(text: str, resolve=None, remote_images: bool = True, reads=()) -> str:
    """A note or an answer as the app draws it. A note link carries its path for the click
    to open and an image comes through /asset, so `/asset?` is this origin's own."""
    return ofm.render(text, link=anchor, asset=asset_url, resolve=resolve, own=("/asset?",),
                      remote_images=remote_images, reads=reads)


def answer_html(answer: str, refs, graph=None) -> str:
    """A chat answer drawn with its turn's reads (ADR-0127): a name several
    notes share links the one the turn read, and a read note's title links
    where the answer names it bare. Reads the driver graph unless given one."""
    resolve, reads = answer_resolver(refs or (), graph=graph)
    return linkify(answer, resolve, remote_images=False, reads=reads)


def render_frontmatter(props: dict) -> str:
    """Properties box for the note drawer: native <details>, one row per key,
    list values as individual chips. Closed, and placed AFTER the body by the
    caller: the note is read for its text, and an open table of tags and dates
    above the first line was the first thing every note said (2026-09-22
    surfaces experiment). The summary row stays, so the reader who wants the
    metadata has one click and knows where it is."""
    rows = []
    for key, val in props.items():
        vals = val if isinstance(val, (list, tuple)) else [val]
        chips = "".join(
            f'<span class="fm-val">{_html.escape("" if v is None else str(v))}</span>'
            for v in vals
        )
        rows.append(
            f'<div class="fm-row"><span class="fm-key">{_html.escape(str(key))}</span>{chips}</div>'
        )
    return '<details class="fm"><summary>Properties</summary>' + "".join(rows) + "</details>"


def find_asset(path: str) -> Path | None:
    """The file /asset would serve for `path`, None when it would 404. Shared
    with the figure placement (ADR-0082), which must not put an embed in an
    answer that draws as a broken image."""
    return vault_files.find_image(CONFIG.vault_path, path)
