# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The public page's routes (ADR-0143), an APIRouter mounted on the app.

`/public*` is the view: the choice, the switch, Publish; ADR-0140 gates it
by its prefix like any view. `/vault/save*` is Save to Silica, which every
account gets, beside `/vault/fetch`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from silica.ui.web.settings import _tilde

logger = logging.getLogger(__name__)
_EMBED = re.compile(r"!\[\[([^\]|#]+\.(?:png|jpe?g|gif|webp|svg|bmp))", re.IGNORECASE)


def _require_same_origin(request: Request) -> None:
    from silica.ui.web.server import _require_same_origin as check

    check(request)


router = APIRouter()
_SAME_ORIGIN = [Depends(_require_same_origin)]


def _title(path: str, fm: dict) -> str:
    t = fm.get("title")
    return t.strip() if isinstance(t, str) and t.strip() else path.rsplit("/", 1)[-1].removesuffix(".md")


def _view() -> dict:
    """Everything the view and the globes draw, from one walk of the vault:
    the choice, what Publish would send, and what it would leave out."""
    from silica.config import hosted
    from silica.driver import get_driver
    from silica.kernel import publish
    from silica.ui.web.public_site import last_published

    vault = publish._vault()
    h = publish.handle(vault)
    sel = publish.selection(vault)
    notes, fms, folders = sel["notes"], sel["frontmatter"], sel["folders"]
    title = {p: _title(p, fms.get(p) or {}) for p in notes}
    on = sorted((p for p, s in notes.items() if s == "on"), key=lambda p: title[p].lower())
    in_public = lambda p: any(p == f or p.startswith(f + "/") for f in folders)  # noqa: E731
    last = last_published(h) if h else {}
    before = set(last.get("notes", []))

    plain: list[dict] = []
    try:
        _notes, _unresolved, g = get_driver().graph_data()
        for p in on:
            for t in sorted(g.successors(p)) if p in g else ():
                if t in notes and notes[t] != "on":
                    plain.append({"words": title[t], "in": title[p]})
    except Exception as exc:
        # The list is advice, not the guard: the renderer drops those links
        # whatever this says, so a graph that fails to read costs the list only.
        logger.warning("public view: links to private notes not listed (%s)", exc)
    images: list[dict] = []
    for p in on:
        try:
            body = (vault / p).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        images += [{"name": m.rsplit("/", 1)[-1], "in": title[p]} for m in dict.fromkeys(_EMBED.findall(body))]

    by_folder = []
    for f in folders:
        under = [p for p, s in notes.items() if s != "never" and (p == f or p.startswith(f + "/"))]
        by_folder.append({"folder": f, "on": sum(1 for p in under if notes[p] == "on"), "of": len(under)})
    private = [{"title": title[p], "why": "left out of " + next(f for f in folders if p.startswith(f + "/"))}
               for p, s in notes.items() if s == "struck"]
    private += [{"title": title[p], "why": "saved from elsewhere: its own globe only"}
                for p, s in notes.items() if s == "off" and in_public(p) and (fms.get(p) or {}).get("form") == "clip"]
    return {
        "available": publish.available(vault),
        "handle": h,
        "url": publish.page_url(vault),
        # where the files are, on a laptop: the person serves them; a hosted
        # path names the server's disk and says nothing to the account. The home
        # folder reads as ~, as the bug report prints it: the line is read, not typed.
        "dir": _tilde(str(publish.site_root(h) / "live")) if h and not hosted() else "",
        "page": sel["page"],
        "folders": folders,
        "never": sel["never"],
        # off is every other note: only the states a row draws travel
        "notes": {p: s for p, s in notes.items() if s in ("on", "struck")},
        "authors": {p: str(fm.get("author")).strip() for p, fm in fms.items()
                    if notes.get(p) != "never" and isinstance(fm.get("author"), str) and fm["author"].strip()},
        "out": [title[p] for p in on],
        "by_folder": by_folder,
        "single": [title[p] for p in on if not in_public(p)],
        "private": private[:20],
        "private_count": sum(1 for s in notes.values() if s in ("off", "struck")),
        "plain": plain[:20],
        "plain_count": len(plain),
        "images": images[:20],
        "new": [title[p] for p in on if last and p not in before][:20],
        "published_at": last.get("published_at", ""),
        "published": len(before),
    }


@router.get("/public")
def public_view():
    return _view()


# One picture per set of notes and links, kept for the process: a layout of a few hundred notes
# takes about a second, and the view asks every time it opens.
_COVER: dict[int, dict] = {}


@router.get("/public/cover")
def public_cover():
    """The view's cover (ADR-0146 point 4): the notes that would go out and the links between
    them, laid out by the published page's own `cover_layout`, so both print one picture. The
    links are the vault graph's between two outgoing notes, so a private note is never a mark.
    Each mark carries its width in pixels (`mark_px`), the published SVG's rule."""
    from silica.driver import get_driver
    from silica.kernel import publish
    from silica.ui.web.public_site import cover_layout, mark_px

    vault = publish._vault()
    sel = publish.selection(vault)
    on = sorted(p for p, s in sel["notes"].items() if s == "on")
    if not on:
        return {"n": [], "e": []}
    out = set(on)
    pairs: set[tuple[str, str]] = set()
    try:
        _notes, _unresolved, g = get_driver().graph_data()
        for p in on:
            for t in g.successors(p) if p in g else ():
                if t in out and t != p:
                    pairs.add((min(p, t), max(p, t)))
    except Exception as exc:
        # A cover without lines still shows the page's notes; the boxes below it, which say
        # what goes out, do not read the graph.
        logger.warning("public cover: links not drawn (%s)", exc)
    key = hash((tuple(on), tuple(sorted(pairs))))
    if key in _COVER:
        return _COVER[key]
    marks, links = cover_layout(on, pairs)
    fms = sel["frontmatter"]
    cover = {
        "n": [[x, y, deg, _title(n, fms.get(n) or {}), mark_px(len(marks), deg)] for n, x, y, deg in marks],
        "e": [list(e) for e in links],
    }
    # one entry: a view shows one page, and an old picture is never asked for again
    _COVER.clear()
    _COVER[key] = cover
    return cover


def _canon(path: str) -> str:
    from silica.kernel.recall.mindmap import note_resolver

    canon = note_resolver()(path or "")
    if not canon:
        raise HTTPException(404, "note not found in vault")
    return canon


@router.post("/public/page", dependencies=_SAME_ORIGIN)
def public_page(payload: dict):
    from silica.kernel import publish

    publish.set_page(bool(payload.get("on")))
    return _view()


@router.post("/public/folder", dependencies=_SAME_ORIGIN)
def public_folder(payload: dict):
    from silica.kernel import publish

    try:
        publish.set_folder(str(payload.get("folder") or ""), bool(payload.get("on")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _view()


@router.post("/public/note", dependencies=_SAME_ORIGIN)
def public_note(payload: dict):
    from silica.kernel import publish

    try:
        publish.set_note(_canon(str(payload.get("path") or "")), bool(payload.get("on")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _view()


@router.post("/public/publish", dependencies=_SAME_ORIGIN)
async def public_publish():
    """Write the page and put it online; the button reads "Turn on and
    publish" while the page is off, so it turns the page on first."""
    from silica.kernel import publish
    from silica.ui.web.public_site import build

    if not publish.available():
        raise HTTPException(400, "this account has no public address yet")
    publish.set_page(True)
    try:
        out = await asyncio.to_thread(build)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**_view(), "result": out}


# --- Save to Silica (ADR-0143 point 8) ----------------------------------------

@router.get("/vault/save/preview")
async def save_preview(url: str = ""):
    """What the confirm card shows. A GET that only reads: the page it names
    is fetched, nothing is written until the POST."""
    from silica.sources import saved_page

    try:
        p = await asyncio.to_thread(saved_page.prepare, url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        # httpx raises its own on a refused connection or a timeout: that page
        # could not be read, which is the reader's to know, not a 500.
        logger.warning("save preview failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(502, "that page could not be read right now")
    p.pop("content")
    return p


@router.post("/vault/save", dependencies=_SAME_ORIGIN)
async def save_page(payload: dict):
    from silica.sources import saved_page
    from silica.ui.web.server import _begin_turn, _embed_imported, _end_turn

    if not _begin_turn():
        raise HTTPException(409, "a turn is running: save the page when it ends")
    try:
        path = await asyncio.to_thread(saved_page.save, str(payload.get("url") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.warning("save failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(502, "that page could not be read right now")
    finally:
        _end_turn()
    _embed_imported({str(Path(path).parent)})
    return {"path": path}
