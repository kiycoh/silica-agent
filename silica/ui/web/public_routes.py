# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Page's routes (ADR-0143, ADR-0151), an APIRouter mounted on the app.

`/public*` is the view: the vaults, their choices, Publish; ADR-0140 gates it
by its prefix like any view. `/vault/save*` is Save to Silica and
`/vault/invitations*` the Private vaults shared with the account, which every
account gets, beside `/vault/fetch`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from silica.ui.web.settings import _tilde

logger = logging.getLogger(__name__)


router = APIRouter()
# How many titles the view's "Goes out" shows of a vault, the best linked first.
_GOES_OUT = 24


def _title(path: str, fm: dict) -> str:
    t = fm.get("title")
    return t.strip() if isinstance(t, str) and t.strip() else path.rsplit("/", 1)[-1].removesuffix(".md")


def _degrees(paths: set[str]) -> dict[str, int]:
    """Links each note has to another of `paths`, from the vault graph; {} when it cannot be read:
    the counts order the titles, and an order by name costs nothing that matters."""
    from silica.driver import get_driver

    deg = dict.fromkeys(paths, 0)
    try:
        _notes, _unresolved, g = get_driver().graph_data()
    except Exception as exc:
        logger.warning("page view: links not counted (%s)", exc)
        return deg
    for p in paths:
        for t in g.successors(p) if p in g else ():
            if t in paths and t != p:
                deg[p] += 1
                deg[t] += 1
    return deg


def _folders(vault: Path, taken: list[str]) -> list[str]:
    """The folders "+ New vault" offers: every folder of the vault but Silica's own, a dot-folder,
    and any inside or around a vault already made (ADR-0151 point 1)."""
    from silica.kernel import publish

    dirs = publish.never_dirs()
    out = []
    for d in sorted(vault.rglob("*")):
        if not d.is_dir():
            continue
        rel = d.relative_to(vault).as_posix()
        if rel.count("/") > 2 or publish.never(rel + "/x", dirs):
            continue
        if any(rel == t or rel.startswith(t + "/") or t.startswith(rel + "/") for t in taken):
            continue
        out.append(rel)
    return out[:400]


def _view() -> dict:
    """Everything the view and the globes draw, from one walk of the vault."""
    from silica.config import hosted
    from silica.kernel import publish
    from silica.ui.web.public_site import last_published

    vault = publish._vault()
    turned = publish.enforce_tier(vault)
    h = publish.handle(vault)
    sel = publish.selection(vault)
    notes, fms, of = sel["notes"], sel["frontmatter"], sel["vault_of"]
    last = (last_published(h) if h else {}).get("vaults", {})
    page_url = publish.page_url(vault)
    out_paths = {p for p, s in notes.items() if s == "on"}
    deg = _degrees(out_paths)
    reviews = publish.host_reviews()
    vaults = []
    for v in sel["vaults"]:
        mine = [p for p in out_paths if of.get(p) == v["slug"]]
        before = set(last.get(v["slug"], {}).get("notes", []))
        top = sorted(mine, key=lambda p: (-deg.get(p, 0), _title(p, fms.get(p) or {}).lower()))[:_GOES_OUT]
        vaults.append({
            **{k: v[k] for k in ("slug", "folder", "title", "line", "license", "visibility", "listed",
                                 "readers", "published", "downgraded")},
            "url": f"{page_url}/{v['slug']}/" if page_url else "",
            "out": len(mine),
            "top": [_title(p, fms.get(p) or {}) for p in top],
            "struck": sum(1 for p, s in notes.items() if s == "struck" and of.get(p) == v["slug"]),
            "new": sum(1 for p in mine if before and p not in before),
            "published_at": last.get(v["slug"], {}).get("published_at", ""),
            "review": reviews.get(v["slug"], {}),
            "blocked": last.get(v["slug"], {}).get("blocked", []) if v["visibility"] == "public" else [],
        })
    return {
        "available": publish.available(vault),
        "handle": h,
        "ask_handle": publish.handle_state(vault),
        "base": os.environ.get("SILICA_PUBLIC_BASE", "").strip().rstrip("/"),
        "url": page_url,
        "hosted": hosted(),
        # where the files are, on a laptop: the person serves them; a hosted
        # path names the server's disk and says nothing to the account. The home
        # folder reads as ~, as the bug report prints it: the line is read, not typed.
        "dir": _tilde(str(publish.site_root(h) / "live")) if h and not hosted() else "",
        "private": publish.private_allowed(),
        "max": publish.max_vaults(),
        "plan": publish.paid_plan(),
        "licenses": list(publish.LICENSES),
        "vaults": vaults,
        "turned_off": turned,
        "folders": _folders(vault, [v["folder"] for v in sel["vaults"]]),
        "never": sel["never"],
        # off is every other note: only the states a row draws travel
        "notes": {p: s for p, s in notes.items() if s in ("on", "struck")},
        "of": {p: of[p] for p in of if notes.get(p) != "never"},
        "outside": sum(1 for p, fm in fms.items() if p not in of and fm.get("publish") is True),
        "authors": {p: str(fm.get("author")).strip() for p, fm in fms.items()
                    if p in of and notes.get(p) != "never" and isinstance(fm.get("author"), str)
                    and fm["author"].strip()},
    }


@router.get("/public")
def public_view():
    return _view()


@router.post("/public/handle")
def public_handle(payload: dict):
    """The handle My Wikis asks for on its first visit (ADR-0143 point 7, amended 2026-11-02)."""
    from silica.kernel import publish

    try:
        publish.claim(str(payload.get("handle") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _view()


# One picture per set of notes and links, kept for the process: a layout of a few hundred notes
# takes about a second, and the view asks every time it opens a vault.
_COVER: dict[int, dict] = {}


@router.get("/public/cover")
def public_cover(vault: str = ""):
    """A vault's cover (ADR-0146 point 4) before it is published: the notes that would go out
    and the links between them, laid out as the published vault lays out its own (spring
    layout, seed 7). The links are the vault graph's between two outgoing notes, so a private
    note is never a mark."""
    from silica.driver import get_driver
    from silica.kernel import publish
    from silica.ui.web.public_site import cover

    sel = publish.selection(publish._vault())
    on = sorted(p for p, s in sel["notes"].items() if s == "on" and sel["vault_of"].get(p) == vault)
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
        # A cover without lines still shows the vault's notes; the view below it, which says
        # what goes out, does not read the graph.
        logger.warning("page cover: links not drawn (%s)", exc)
    key = hash((tuple(on), tuple(sorted(pairs))))
    if key in _COVER:
        return _COVER[key]
    fms = sel["frontmatter"]
    c = cover(on, pairs, {p: _title(p, fms.get(p) or {}) for p in on})
    result = {"n": [[*row, _title(p, fms.get(p) or {})] for row, p in zip(c["n"], c["nodes"])], "e": c["e"],
              "areas": c["areas"], "unlinked": len(c["unlinked"])}
    # one entry: a view shows one vault, and an old picture is asked for again only on a switch
    _COVER.clear()
    _COVER[key] = result
    return result


def _canon(path: str) -> str:
    from silica.kernel.graph.mindmap import note_resolver

    canon = note_resolver()(path or "")
    if not canon:
        raise HTTPException(404, "note not found in vault")
    return canon


def _refresh_page() -> None:
    """The Page lists the Public vaults, so it follows a visibility change at once."""
    from silica.ui.web.public_site import write_page

    try:
        write_page()
    except OSError as exc:
        # The choice is saved in vault.yaml either way; the Page catches up at the next Publish.
        logger.warning("page: index not rewritten (%s)", exc)


@router.post("/public/vault")
def public_add_vault(payload: dict):
    from silica.kernel import publish

    try:
        v = publish.add_vault(str(payload.get("folder") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**_view(), "added": v["slug"]}


@router.post("/public/vault/{slug}")
def public_update_vault(slug: str, payload: dict):
    from silica.kernel import publish
    from silica.ui.web.public_site import set_listed

    changes = {k: payload[k] for k in ("title", "line", "license", "listed", "readers") if k in payload}
    try:
        if "visibility" in payload:
            publish.set_visibility(slug, str(payload["visibility"]))
            _refresh_page()
        v = publish.update_vault(slug, changes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if "listed" in changes:
        set_listed(slug, v["listed"] and v["visibility"] == "public")
    return _view()


@router.post("/public/vault/{slug}/remove")
def public_remove_vault(slug: str):
    from silica.kernel import publish

    try:
        publish.remove_vault(slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _view()


@router.post("/public/note")
def public_note(payload: dict):
    from silica.kernel import publish

    try:
        publish.set_note(_canon(str(payload.get("path") or "")), bool(payload.get("on")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _view()


@router.post("/public/publish")
async def public_publish(payload: dict | None = None):
    """Write one vault and put it out; the button reads "Turn on and
    publish" while the vault is Off, so it turns the vault Public first."""
    from silica.kernel import publish
    from silica.ui.web.public_site import build

    slug = str((payload or {}).get("vault") or "")
    if not publish.available():
        raise HTTPException(400, "this account has no public address yet")
    try:
        if publish.find(slug)["visibility"] == "off":
            publish.set_visibility(slug, "public")
        out = await asyncio.to_thread(build, slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**_view(), "result": out}


# --- the check before Publish (ADR-0153 points 9, 20 and 26) --------------------

# The facts of the wiki on the screen: reading them walks the notes and the sources, and an answer
# changes only what is made of them. One process per account, so one screen.
_WIKI: dict[str, dict] = {}


def _sheet(slug: str, *, fresh: bool = False) -> dict:
    from silica.kernel import rights
    from silica.sources.discover.read import fetch_figure

    if fresh or slug not in _WIKI:
        _WIKI.clear()
        _WIKI[slug] = rights.read_wiki(slug, fetch=fetch_figure)
    return rights.sheet(_WIKI[slug], rights.answers())


@router.get("/public/check")
async def public_check(vault: str = ""):
    try:
        return await asyncio.to_thread(_sheet, vault, fresh=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/public/check/answer")
def public_check_answer(payload: dict):
    """One answer, for a file, a folder of images or a site this wiki shows."""
    from silica.kernel import rights

    slug, key = str(payload.get("vault") or ""), str(payload.get("key") or "")
    off = payload.get("off")
    try:
        sheet = _sheet(slug)
        if key not in {row["key"] for row in sheet["files"] + sheet["groups"]}:
            raise ValueError(f"nothing to answer for {key}")
        rights.save_answer(key, str(payload.get("answer") or ""), license=str(payload.get("license") or ""),
                           credit=str(payload.get("credit") or ""),
                           off=[str(x) for x in off] if isinstance(off, list) else None)
        return _sheet(slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/public/check/note")
def public_check_note(payload: dict):
    """A note the author says they wrote themselves, out of a file that stays (ADR-0153 point 26)."""
    from silica.kernel import rights

    slug, path, on = str(payload.get("vault") or ""), str(payload.get("path") or ""), bool(payload.get("on"))
    try:
        _sheet(slug)
        wiki = _WIKI[slug]
        # The facts name the note, so no read of the graph: on 1,748 notes that read is seconds.
        rights.check_mark(wiki, path, on)
        rights.set_rewritten(path, on)
        wiki["notes"][path]["rewritten"] = on
        return _sheet(slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/public/check/image")
def public_check_image(name: str = ""):
    """A kept copy of a remote image, so the screen shows it without the page leaving this origin."""
    from fastapi.responses import FileResponse

    from silica.kernel import rights

    found = rights.kept_image(name)
    if found is None:
        raise HTTPException(404)
    return FileResponse(found)


# --- Save to Silica (ADR-0143 point 8, ADR-0151 point 8) -----------------------

def _areas(raw) -> list[int] | None:
    """`?areas=0,3` or `?areas=0&areas=3` or a JSON list: the areas a reader picked; None is all."""
    if raw is None or raw == "" or raw == []:
        return None
    items = raw if isinstance(raw, list) else str(raw).split(",")
    return [int(a) for a in items if str(a).strip().lstrip("-").isdigit()]


def _names(raw) -> list[str] | None:
    """`?notes=a,b` or a JSON list: the notes a reader picked by name, beside any whole areas;
    None is none named. Only ever compared with a manifest's slugs, so a stray word picks nothing."""
    if raw is None or raw == "" or raw == []:
        return None
    items = raw if isinstance(raw, list) else str(raw).split(",")
    return [str(s).strip() for s in items if str(s).strip()]


@router.get("/vault/save/preview")
async def save_preview(request: Request, url: str = ""):
    """What the confirm card shows. A GET that only reads: the page it names
    is fetched, nothing is written until the POST."""
    from silica.sources import saved_page

    areas = _areas([a for v in request.query_params.getlist("areas") for a in v.split(",")])
    named = _names([s for v in request.query_params.getlist("notes") for s in v.split(",")])
    try:
        if saved_page.is_vault(url):
            p = await asyncio.to_thread(saved_page.prepare_vault, url, areas, named=named)
            p.pop("files")
            return {**p, "kind": "vault", "chosen": areas}
        p = await asyncio.to_thread(saved_page.prepare, url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        # httpx raises its own on a refused connection or a timeout: that page
        # could not be read, which is the reader's to know, not a 500.
        logger.warning("save preview failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(502, "that page could not be read right now")
    p.pop("content")
    return {**p, "kind": "note"}


async def _write_turn(fn, *args, **kwargs):
    from silica.ui.web.server import _begin_turn, _embed_imported, _end_turn

    if not _begin_turn():
        raise HTTPException(409, "a turn is running: save when it ends")
    try:
        out = await asyncio.to_thread(fn, *args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.warning("save failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(502, "that page could not be read right now")
    finally:
        _end_turn()
    folder = out["folder"] if isinstance(out, dict) else str(Path(out).parent)
    _embed_imported({folder})
    return out


@router.post("/vault/save")
async def save_page(payload: dict):
    from silica.sources import saved_page

    url = str(payload.get("url") or "")
    if saved_page.is_vault(url):
        out = await _write_turn(saved_page.save_vault, url, _areas(payload.get("areas")),
                                named=_names(payload.get("notes")))
        return {"path": out["first"], **out}
    return {"path": await _write_turn(saved_page.save, url)}


@router.get("/vault/invitations")
def invitations():
    from silica.sources import saved_page

    return {"invitations": saved_page.invitations()}


@router.get("/vault/invitations/preview")
def invitation_preview(request: Request, author: str = "", vault: str = ""):
    from silica.sources import saved_page

    try:
        return saved_page.prepare_invitation(
            author, vault, _areas([a for v in request.query_params.getlist("areas") for a in v.split(",")]))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/vault/invitations/save")
async def invitation_save(payload: dict):
    from silica.sources import saved_page

    out = await _write_turn(saved_page.save_invitation, str(payload.get("author") or ""),
                            str(payload.get("vault") or ""), _areas(payload.get("areas")))
    return {"path": out["first"], **out}


@router.post("/vault/invitations/decline")
def invitation_decline(payload: dict):
    from silica.sources import saved_page

    try:
        saved_page.decline_invitation(str(payload.get("author") or ""), str(payload.get("vault") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"invitations": saved_page.invitations()}
