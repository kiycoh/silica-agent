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
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request

from silica.ui.web.settings import _tilde

logger = logging.getLogger(__name__)


def _require_same_origin(request: Request) -> None:
    from silica.ui.web.server import _require_same_origin as check

    check(request)


router = APIRouter()
_SAME_ORIGIN = [Depends(_require_same_origin)]
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


@router.post("/public/handle", dependencies=_SAME_ORIGIN)
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
    from silica.kernel.recall.mindmap import note_resolver

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


@router.post("/public/vault", dependencies=_SAME_ORIGIN)
def public_add_vault(payload: dict):
    from silica.kernel import publish

    try:
        v = publish.add_vault(str(payload.get("folder") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {**_view(), "added": v["slug"]}


@router.post("/public/vault/{slug}", dependencies=_SAME_ORIGIN)
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


@router.post("/public/vault/{slug}/remove", dependencies=_SAME_ORIGIN)
def public_remove_vault(slug: str):
    from silica.kernel import publish

    try:
        publish.remove_vault(slug)
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


# --- Save as a file (ADR-0157 point 5) ------------------------------------------

def _downloads() -> Path:
    """Where a computer saves a wiki file: the desktop's Downloads folder, else ~/Downloads."""
    home = Path.home()
    if shutil.which("xdg-user-dir"):
        try:
            said = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            # a desktop that cannot answer is one with no folder of its own: ~/Downloads is the
            # folder every other platform uses
            said = ""
        # With no Downloads set it answers the home folder itself, and the line under the button
        # says "in Downloads".
        if said and Path(said).is_dir() and Path(said).resolve() != home.resolve():
            return Path(said)
    d = home / "Downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _into_downloads(path: Path, name: str) -> Path:
    """The zip copied under a name no file in Downloads has, "x (2).zip" after "x.zip" as a browser
    names a second download. Created exclusively, so a file of the person's, or a save running
    beside this one, is never overwritten."""
    folder, stem, n = _downloads(), name.removesuffix(".zip"), 1
    while True:
        dest = folder / (name if n == 1 else f"{stem} ({n}).zip")
        n += 1
        try:
            out = dest.open("xb")
        except FileExistsError:
            continue
        try:
            with out, path.open("rb") as src:
                shutil.copyfileobj(src, out)
        except BaseException:
            dest.unlink(missing_ok=True)  # half a zip in Downloads would be passed on as if whole
            raise
        return dest


def _open_folder(folder: Path) -> None:
    opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    # not waited for: a file manager that stays open must not hold the request
    subprocess.Popen([opener, str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


@router.get("/public/vault/{slug}/file", dependencies=_SAME_ORIGIN)
async def public_file(slug: str):
    """The wiki file as a download, on every install: how the hosted service hands it over."""
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from silica.ui.web.public_site import build_file

    try:
        path, out = await asyncio.to_thread(build_file, slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # Set here and not through `filename=`, which Starlette writes as `filename="..."` for an ASCII
    # name. The folder goes once the zip is sent; one a dropped download leaves, the next build sweeps.
    said = f"attachment; filename*=UTF-8''{quote(out['name'], safe='')}"
    return FileResponse(path, media_type="application/zip", headers={"Content-Disposition": said},
                        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True))


@router.post("/public/vault/{slug}/file", dependencies=_SAME_ORIGIN)
async def public_file_save(slug: str):
    """The wiki file written into this computer's Downloads, so the screen can say where it is."""
    from silica.config import hosted
    from silica.ui.web.public_site import build_file

    if hosted():
        # the server's disk is not the person's: there the browser downloads the GET
        raise HTTPException(404)
    try:
        path, out = await asyncio.to_thread(build_file, slug)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        dest = await asyncio.to_thread(_into_downloads, path, out["name"])
    except OSError as exc:
        raise HTTPException(400, f"Downloads did not take the file ({exc.strerror or exc})")
    finally:
        # a folder that will not go is swept by the next build, past its hour
        shutil.rmtree(path.parent, ignore_errors=True)
    # the folder by its own name: a desktop set to another language calls Downloads "Scaricati"
    return {"path": str(dest), "name": dest.name, "folder": dest.parent.name, "notes": out["notes"],
            "bytes": out["bytes"]}


@router.post("/public/reveal", dependencies=_SAME_ORIGIN)
def public_reveal(payload: dict):
    """Show in folder, for a wiki file this computer saved. Only inside Downloads, so whatever
    reaches this route cannot make it open any other folder."""
    from silica.config import hosted

    if hosted():
        raise HTTPException(404)
    raw = str(payload.get("path") or "")
    # both sides resolved, so neither `..` nor a link planted in Downloads leads out of it
    p = Path(raw).resolve() if raw and Path(raw).is_absolute() else None
    # A file that exists, so its parent is a folder: `<Downloads>/x.desktop/y` resolves without
    # complaint, and its parent is a file the opener would launch.
    if p is None or not p.is_file() or _downloads().resolve() not in p.parents:
        raise HTTPException(400, "only a file in Downloads can be shown in its folder")
    try:
        _open_folder(p.parent)
    except OSError as exc:
        raise HTTPException(400, f"the folder could not be opened ({exc.strerror or exc})")
    return {"ok": True}


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


@router.post("/public/check/answer", dependencies=_SAME_ORIGIN)
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


@router.post("/public/check/note", dependencies=_SAME_ORIGIN)
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


@router.post("/vault/save", dependencies=_SAME_ORIGIN)
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


@router.post("/vault/invitations/save", dependencies=_SAME_ORIGIN)
async def invitation_save(payload: dict):
    from silica.sources import saved_page

    out = await _write_turn(saved_page.save_invitation, str(payload.get("author") or ""),
                            str(payload.get("vault") or ""), _areas(payload.get("areas")))
    return {"path": out["first"], **out}


@router.post("/vault/invitations/file", dependencies=_SAME_ORIGIN)
async def invitation_file(request: Request):
    """Open a file (ADR-0157 point 7): the body is the zip, refused past the cap as it arrives,
    so a larger one is never held whole."""
    from silica.sources import saved_page

    too_big = HTTPException(413, str(saved_page._file_too_big()))
    said = request.headers.get("content-length", "")
    if said.isdigit() and int(said) > saved_page.WIKI_FILE_MAX:
        raise too_big
    # shortcut: held in memory, twice during the join, so 200 MB at the cap beside the 234 MiB a
    # tenant holds on average (2026-11-06); spool it to silica_tmp_dir() and open the zip from
    # there if a tenant is ever killed during an Open
    chunks, held = [], 0
    async for chunk in request.stream():
        held += len(chunk)
        if held > saved_page.WIKI_FILE_MAX:
            raise too_big
        chunks.append(chunk)
    try:
        out = await asyncio.to_thread(saved_page.open_file, b"".join(chunks))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"opened": out, "invitations": saved_page.invitations()}


@router.post("/vault/invitations/decline", dependencies=_SAME_ORIGIN)
def invitation_decline(payload: dict):
    from silica.sources import saved_page

    try:
        saved_page.decline_invitation(str(payload.get("author") or ""), str(payload.get("vault") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"invitations": saved_page.invitations()}
