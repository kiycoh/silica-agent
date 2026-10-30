# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Save to Silica (ADR-0143 point 8): a public page's markdown, copied into
the reader's vault under `Saved/@handle/`, never read from another vault.

The page publishes a `.md` beside its `.html` whose links already point at
the author's pages and whose private links are already plain words, so the
copy can only rewrite the links the reader can serve from their own vault:
a note of the same author they saved before.
"""
from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

SAVED_DIR = "Saved"
_PAGE_PATH = re.compile(r"^/@([a-z0-9-]+)/([a-z0-9-]+)$")
_MD_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\((https?://[^)\s]+)\)")


def allowed_hosts() -> set[str]:
    # the hosted pages' host (ADR-0143 point 6): the apex is the landing, on GitHub Pages
    raw = os.environ.get("SILICA_PUBLIC_HOSTS", "").strip() or "pages.silica-agent.dev"
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def page_of(url: str) -> tuple[str, str, str]:
    """(url, handle, slug) of a public page address, or ValueError. Only an
    https page on a host the server lists: the confirm card must never be a
    way to make the app fetch an arbitrary address."""
    parts = urlsplit((url or "").strip())
    m = _PAGE_PATH.match(parts.path or "")
    if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed_hosts() or not m \
            or parts.query or parts.fragment:
        raise ValueError("that is not the address of a note on a Silica public page")
    clean = f"https://{(parts.hostname or '').lower()}{parts.path}"
    return clean, m.group(1), m.group(2)


def _fetch_markdown(url: str) -> str:
    """The page's `.md`, through the web reader's guard, which revalidates
    every redirect hop (a public page must not be a door into the server)."""
    from silica.sources.web_fetch import _fetch

    resp, final = _fetch(url + ".md", types=("text/",))
    if (urlsplit(final).hostname or "").lower() not in allowed_hosts():
        raise ValueError("that page moved to a host Silica does not save from")
    return resp.text


def saved_copies(handle: str, vault: Path | None = None) -> dict[str, str]:
    """{source url: vault path} of the notes already saved from @handle.
    shortcut: reads Saved/@handle only, so a copy the reader moved elsewhere
    is not found and its link stays a web link; widen to every note's
    `source:` if moved copies turn out common."""
    from silica.kernel.publish import frontmatters
    from silica.kernel.vault_manifest import in_write_dir

    folder = in_write_dir(f"{SAVED_DIR}/@{handle}") + "/"
    out = {}
    for path, fm in frontmatters(vault).items():
        src = fm.get("source")
        if path.startswith(folder) and isinstance(src, str):
            out[src.strip()] = path
    return out


def _credit(props: dict, handle: str, url: str) -> tuple[str, str]:
    """(author, source) the copy carries. A page that is itself a saved copy
    names its original, and the new copy credits that one: provenance does
    not chain through readers (ADR-0143 point 10). Both come from a file
    someone else wrote, so the credit is taken only when `source` is itself
    a public page address and its handle is the declared author: anything
    else (a path in `author`, a source elsewhere) credits the page fetched."""
    author, source = props.get("author"), props.get("source")
    if isinstance(author, str) and isinstance(source, str):
        try:
            orig, who, _slug = page_of(source)
        except ValueError:
            return f"@{handle}", url
        if who == author.strip().lstrip("@"):
            return f"@{who}", orig
    return f"@{handle}", url


def prepare(url: str, vault: Path | None = None) -> dict:
    """What the confirm card shows, and the note it would write."""
    from silica.kernel.vault_manifest import in_write_dir
    from silica.kernel.write import frontmatter as fm

    url, handle, slug = page_of(url)
    text = _fetch_markdown(url)
    props, _raw, body = fm.split(text)
    props = props or {}
    author, source = _credit(props, handle, url)
    who = author.lstrip("@")
    title = props.get("title") if isinstance(props.get("title"), str) else slug.replace("-", " ")
    mine = saved_copies(who, vault)
    links, seen = [], set()

    def relink(m: re.Match) -> str:
        words, target = m.group(1), m.group(2)
        own = mine.get(target)
        if target not in seen:
            seen.add(target)
            links.append({"text": words, "to": "copy" if own else "web"})
        return f"[[{own.removesuffix('.md')}|{words}]]" if own else m.group(0)

    new_body = _MD_LINK.sub(relink, body)
    tags = props.get("tags") if isinstance(props.get("tags"), list) else []
    note = {"title": title, **({"tags": tags} if tags else {}), "author": author,
            "source": source, "saved": datetime.date.today().isoformat(), "form": "clip"}
    folder = in_write_dir(f"{SAVED_DIR}/@{who}")
    return {
        "url": url, "title": title, "author": author, "source": source, "folder": folder,
        "links": links, "images": len(_MD_IMAGE.findall(body)),
        "existing": mine.get(source, ""),
        "content": fm.dump(note, new_body),
    }


def save(url: str, vault: Path | None = None) -> str:
    """Write the copy; returns its vault path. A second save of one page is a
    second copy beside the first, never a rewrite of it: the first is a
    snapshot the reader may have annotated."""
    from silica.driver import DRIVER

    p = prepare(url, vault)
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", p["title"]).strip() or "Saved note"
    path, n = f"{p['folder']}/{name}.md", 2
    while _exists(path):
        path, n = f"{p['folder']}/{name} {n}.md", n + 1
    DRIVER.create(path, p["content"])
    return path


def _exists(rel: str) -> bool:
    from silica.config import CONFIG

    return (Path(CONFIG.vault_path or ".") / rel).exists()
