# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One note, for the drawer and the editor: read, save, new, assets, checks and history."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from silica.config import CONFIG
from silica.kernel import vault_files
from silica.kernel.graph.mindmap import note_resolver
from silica.kernel.recall import note_index
from silica.kernel.text import note_parts
from silica.kernel.write import session_changes
from silica.metrics import emit
from silica.ui.web import drop, note_ops, render, session, sse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/note")
def note(path: str = ""):
    """Read-only rendered note for the drawer. Graceful on miss (never 500).

    Only keys present in the vault index resolve, so an out-of-vault `path`
    falls through to the graceful message — path traversal is closed for free.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef

    resolve = note_resolver()
    canon = resolve(path)
    if not canon:
        return {"title": path, "html": "<p>note not found in vault.</p>"}
    try:
        content = get_driver().read_note(NoteRef(name=note_parts.clean_name(canon), path=canon)).content
    except Exception:
        return {"title": note_parts.clean_name(canon), "html": "<p>note unreadable.</p>"}
    props, body = note_parts.split_frontmatter(content)
    html = render.linkify(body, resolve)
    if props:
        html = html + render.render_frontmatter(props)
    return {**_note_identity(canon, props), "html": html}


def _note_identity(canon: str, props: dict | None) -> dict:
    """The title a note is shown under, and a Zotero paper's identity for the
    drawer's strip (ADR-0055): a paper's leaf is titled by the paper, not by
    its citekey stem. One helper because the reader and the editor's raw read
    both head the drawer, and two copies of the rule would title it twice."""
    if not props or not props.get("zotero_key"):
        raw = _raw_of(canon, props)
        return {"title": note_parts.clean_name(canon), **_saved_identity(props), **({"raw": raw} if raw else {})}
    from silica.ui.web.zotero_web import _PAPER_FM

    return {"title": str(props.get("title") or "") or note_parts.clean_name(canon),
            "paper": {k: props[k] for k in _PAPER_FM if props.get(k)}}


def _saved_identity(props: dict | None) -> dict:
    """A note saved from another page names its author over the text, as a
    paper names its own (ADR-0143 point 11); {} for the person's own notes."""
    props = props or {}
    author = props.get("author")
    if not isinstance(author, str) or not author.strip():
        return {}
    raw = props.get("source")
    source = raw if isinstance(raw, str) else ""
    return {"saved": {"author": "@" + author.strip().lstrip("@"),
                      "source": source if source.startswith("https://") else "",
                      "saved": str(props.get("saved") or "")}}


# The raws the drawer shows in the browser's own viewer (ADR-0129 point 1);
# every other one is a download.
# shortcut: PDF only, the one type checked in an iframe under _ASSET_HEADERS'
# sandbox (Chrome 156, Firefox 158, 2026-11-03); images and audio, which the
# ADR also lets open, join when that is checked for them.
_RAW_OPENS = (".pdf",)


def _raw_found(canon: str, props: dict | None) -> tuple[str, Path | None]:
    """The note's `source_file` and the file it names in the vault, which is
    None when the file is gone or not one Silica brings in as a raw."""
    from silica.kernel import raws
    from silica.sources.convert import DOC_EXTS

    value = (props or {}).get("source_file")
    if not isinstance(value, str) or not CONFIG.vault_path:
        return "", None
    found = raws.resolve(value, canon, Path(CONFIG.vault_path))
    if found is not None and (found.suffix.lower() not in DOC_EXTS or not raws.kept(found, props or {})):
        found = None
    return value, found


def _raw_of(canon: str, props: dict | None) -> dict | None:
    """What a source's strip says of its raw (ADR-0129 point 1): the file, the
    pages the note spans, whether the drawer can open it, and when it is gone,
    whether a second import of it can be recognised. None for a note that
    names no raw, or one outside the vault, which the drawer offers nothing
    for (ADR-0129: "A note whose source_file does not resolve inside the
    vault offers neither")."""
    from silica.config import hosted
    from silica.sources.convert import DOC_EXTS

    props = props or {}
    value, found = _raw_found(canon, props)
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or Path(name).suffix.lower() not in DOC_EXTS:
        return None
    vault = Path(CONFIG.vault_path).resolve()
    named = value.strip().replace("\\", "/")
    if found is None and "/" in named and not (vault / named).resolve().is_relative_to(vault):
        return None  # a path out of the vault: an absolute one joins as itself
    # A chat drop's file sits in its chat's dot-folder (ADR-0121 point 5), which
    # /raw never serves: the strip would call a file that is there lost.
    if found is None and any(part.startswith(".") for part in (
            *Path(canon).parts, *(vault / named).resolve().relative_to(vault).parts[:-1])):
        return None
    ints = {k: props[k] for k in ("source_pages", "source_page", "source_page_end")
            if isinstance(props.get(k), int) and props[k] > 0}
    out = {"name": name, "kept": found is not None,
           "open": found is not None and found.suffix.lower() in _RAW_OPENS,
           "pages": ints.get("source_pages"), "from": ints.get("source_page"),
           "to": ints.get("source_page_end")}
    if found is None:
        # A bare name on the server is a note written while the server
        # deleted an import's file once converted (ADR-0098 point 7, until
        # 2026-11-04): it is gone for that reason, not mislaid. The sha256 is
        # what lets the vault's + recognise the same file brought in again.
        out["gone"] = "removed" if hosted() and "/" not in value.replace("\\", "/") else "missing"
        out["again"] = bool(props.get("source_sha256"))
    else:
        # its place in the vault, which the rail marks while the drawer shows it
        path = found.relative_to(vault).as_posix()
        out["path"] = path
        if props.get("document"):
            from silica.kernel import raws

            out["parts"] = max(0, len(raws.index(vault).get(path, [])) - 1)
    return out


def _broken_links(text: str, resolve) -> list[str]:
    """The wikilink targets in `text` the vault does not resolve, keyed as the
    editor keys a link: the part before `|` and `#`, trimmed (ADR-0059).

    The editor draws a wikilink itself, so it cannot ask the reader's renderer
    whether one resolves; it gets the answer as a list with the bytes and with
    every save. Embeds are left out: /note/render draws those, and marks its
    own. So is `[[#Heading]]`, a link into the same note."""
    out = set()
    for m in render.WIKILINK.finditer(text or ""):
        if m.group(1):
            continue
        target = m.group(2).partition("|")[0].split("#", 1)[0].strip()
        if target and not resolve(target):
            out.add(target)
    return sorted(out)


# --- the drawer's editor (GET /note/raw, POST /note/save) --------------------
# Obsidian's source view, in the drawer: the note's own bytes in a textarea,
# saved as they are typed. The save goes through the driver's overwrite and not
# a direct file write, so a hand edit is a session change like an agent's: the
# ledger takes its baseline, the diff segment enables and /undo can put the
# bytes back. An etag of the bytes rides out with the read and back with each
# save, so an edit typed over a note an agent rewrote meanwhile is refused (409,
# carrying the disk's bytes) instead of landing on top of it.

def _etag(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# --- the index follows the person (ADR-0155 point 18) --------------------------
# A save comes every 800 ms of pause, and a re-embed is a paid call on hosted, so
# one refresh goes 60 s after a note's LAST save or when it is let go, never per
# save. It never runs on /note/save's thread and it asks no chat model.
_INDEX_AFTER_S = 60.0
_INDEX_LOCK = threading.Lock()
_INDEX_PENDING: dict[str, threading.Timer] = {}


def _index_later(canon: str) -> None:
    """(Re)start the note's refresh. The vault rides along: a timer that fires
    after a switch must not read the same path in another vault."""
    key = canon.removesuffix(".md")
    timer = threading.Timer(_INDEX_AFTER_S, _index_due, (key, CONFIG.vault_path))
    timer.daemon = True
    with _INDEX_LOCK:
        if (old := _INDEX_PENDING.get(key)) is not None:
            old.cancel()
        _INDEX_PENDING[key] = timer
    timer.start()


def _index_due(key: str, vault: str) -> None:
    with _INDEX_LOCK:
        _INDEX_PENDING.pop(key, None)
    if vault == CONFIG.vault_path:
        note_index.index_note(key)


@router.post("/note/let-go")
def note_let_go(payload: dict):
    """The editor left this note: its pending refresh runs now, and a note
    with none pending costs nothing, so the page can send this on every leave."""
    key = str(payload.get("path") or "").removesuffix(".md")
    with _INDEX_LOCK:
        timer = _INDEX_PENDING.pop(key, None)
    if timer is not None:
        timer.cancel()
        note_index.index_note(key)
    return {"ok": True}


@router.get("/note/raw")
def note_raw(path: str = ""):
    """The note's bytes for the editor, which is how the drawer shows a note
    (ADR-0059): so it carries what heads the drawer too, the title and a
    paper's identity, and opening a note stays one request. 404 and not the
    reader's graceful message: an editor opened on nothing must not offer a
    save."""
    from silica.driver import get_driver
    from silica.driver.base import NoteRef

    resolve = note_resolver()
    canon = resolve(path)
    if not canon:
        raise HTTPException(404, "note not found in vault")
    try:
        text = get_driver().read_note(NoteRef(name=note_parts.clean_name(canon), path=canon)).content
    except Exception:
        raise HTTPException(404, "note unreadable")
    props, _body = note_parts.split_frontmatter(text)
    return {"path": canon, **_note_identity(canon, props), "text": text, "etag": _etag(text),
            "broken": _broken_links(text, resolve)}


@router.get("/raw")
def raw_file(path: str = "", download: bool = False):
    """The raw a note names (ADR-0129 point 1): the PDF the drawer frames, or
    any raw as a download under its own name. Keyed by the note and never by
    a file: only a path some note's `source_file` names, inside the vault, is
    served, and the tenant never sees a server path (ADR-0085). /asset's
    headers, whose `sandbox` keeps a navigated file off the GUI's origin;
    checked 2026-11-03, Chrome 156 and Firefox 158 still draw a PDF under
    them, in an iframe and at #page=N."""
    from silica.driver import get_driver
    from silica.driver.base import NoteRef

    canon = note_resolver()(path)
    if not canon:
        raise HTTPException(404)
    try:
        text = get_driver().read_note(NoteRef(name=note_parts.clean_name(canon), path=canon)).content
    except Exception:
        raise HTTPException(404)
    _value, found = _raw_found(canon, note_parts.split_frontmatter(text)[0])
    if found is None:
        raise HTTPException(404)
    attach = download or found.suffix.lower() not in _RAW_OPENS
    return FileResponse(found, headers=render.ASSET_HEADERS, filename=found.name,
                        content_disposition_type="attachment" if attach else "inline")


@router.post("/note/save")
def note_save(payload: dict):
    from silica.driver import DRIVER

    canon = note_resolver()(str(payload.get("path", "")))
    if not canon:
        raise HTTPException(404, "note not found in vault")
    from silica.kernel.workspaces import _EDIT_LOCK

    # The workspace line edits' lock, from the etag check to the write: a turn
    # adding a note it wrote to the workspace between the two was erased by
    # this save, or put back a line the person had just removed (review of
    # dbbcfa0, 2026-10-15). One process-wide lock: saves are milliseconds.
    with _EDIT_LOCK:
        current = session_changes.current_text(canon)
        if current is None:
            raise HTTPException(404, "note unreadable")
        if payload.get("etag") and payload["etag"] != _etag(current):
            return JSONResponse({"detail": "note changed on disk", "text": current,
                                 "etag": _etag(current)}, status_code=409)
        text = str(payload.get("text", ""))
        if text != current:
            from silica.kernel.write.preserve import Loss

            DRIVER.overwrite(canon, text, loss=Loss.PERSON)  # the person typed it
            _index_later(canon)
    # overwrite() may stamp a derived `type:` into the frontmatter, so what
    # landed is read back rather than assumed: the editor's next etag has to be
    # the disk's, or its next save refuses itself.
    landed = session_changes.current_text(canon)
    if landed is None:
        landed = text
    # A link typed since the last read is checked here, so it is drawn broken
    # within one save of being typed rather than at the next open.
    return {"ok": True, "path": canon, "text": landed, "etag": _etag(landed),
            "broken": _broken_links(landed, note_resolver())}


@router.post("/note/new")
def note_new(payload: dict):
    """A note written by hand (ADR-0155 points 7 to 9), `{folder, name, text}`:
    `folder/name.md` holding exactly `text`, as one run under user:/write whose
    undo sends it to the trash. `""` is the vault's root; an empty name with text
    picks `Untitled`, `Untitled 2`. A folder that does not exist is made. Refused
    with the sentence shown under the title: a taken path (409), a bad name or
    Silica's own folder (400), a full vault (`_has_room`).

    No turn slot: /note/save takes none, and a new path is one no turn is
    reading (ADR-0155 point 17: writing does not wait for the model). A name
    another folder holds is allowed (ADR-0114), and `same_name` says which."""
    from silica.driver import DRIVER
    from silica.kernel import publish
    from silica.kernel.workspaces import _EDIT_LOCK
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE
    from silica.tools.wrapped import _vault_file, create_note_with_restore_point
    from silica.ui.web.vault_import import MAX_NAME_BYTES

    folder = str(payload.get("folder") or "").strip().strip("/")
    name = " ".join(str(payload.get("name") or "").split())
    text = str(payload.get("text") or "")
    if folder and not all(note_ops.tree_name_ok(seg) for seg in folder.split("/")):
        raise HTTPException(400, note_ops.BAD_NAME_WHY)
    if name and not note_ops.tree_name_ok(name) or not name and not text:
        raise HTTPException(400, note_ops.BAD_NAME_WHY)
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise HTTPException(400, f"a name is at most {MAX_NAME_BYTES} bytes")
    if folder and publish.never(f"{folder}/x.md"):
        raise HTTPException(400, f"{folder} is Silica's own folder: write in another")
    note_ops.has_room(1, len(text.encode("utf-8")))

    def at(stem: str) -> str:
        return f"{folder}/{stem}.md" if folder else f"{stem}.md"

    # The workspace line edits' lock, as /note/save: one person, one writer here.
    with _EDIT_LOCK:
        try:
            if not name:
                stems = (f"Untitled {n}" if n > 1 else "Untitled" for n in itertools.count(1))
                name = next(s for s in stems if not _vault_file(at(s)).exists())
            path = at(name)
            if _vault_file(path).exists():
                raise FileExistsError(path)
            run = create_note_with_restore_point(path, text, USER_WRITE_SOURCE)
        except (FileExistsError, NotADirectoryError):
            raise HTTPException(409, f"{at(name)} is already taken") from None
        except ValueError:
            # contain_in_vault: a folder that is a link out of the vault
            raise HTTPException(400, note_ops.BAD_NAME_WHY) from None
    _index_later(path)
    landed = session_changes.current_text(path)
    landed = text if landed is None else landed
    other = next((p for p in DRIVER.notes_named(name) if p != path), None)
    return {"ok": True, "path": path, "name": name, "text": landed, "etag": _etag(landed), "run": run,
            "broken": _broken_links(landed, note_resolver()), **({"same_name": other} if other else {})}


@router.post("/note/asset")
async def note_asset(note: str = Form(""), file: UploadFile = File(...)):
    """An image pasted or dropped into a note (ADR-0155 point 15): the file in
    the note's attachment folder as `Pasted image <stamp>.ext`. Told by its first
    bytes, so an SVG named `.png` is refused: /asset's sandbox guards a navigated
    document, and a picture needs none of what an SVG can carry."""
    from silica.ui.web import vault_import as vi

    canon = await asyncio.to_thread(lambda: note_resolver()(note))
    if not canon:
        raise HTTPException(404, "note not found in vault")
    data = bytearray()
    while chunk := await file.read(drop.UPLOAD_CHUNK):
        data.extend(chunk)
        if len(data) > drop.MAX_IMAGE_BYTES:
            raise HTTPException(400, f"larger than {drop.MAX_IMAGE_BYTES // 2**20} MB")
    suffix = vi.image_suffix(bytes(data[:16]))
    if suffix is None:
        raise HTTPException(400, "not a PNG, JPEG, GIF or WebP image")
    await asyncio.to_thread(note_ops.has_room, 0, len(data))
    path = await asyncio.to_thread(_write_asset, canon, bytes(data), suffix)
    return {"ok": True, "path": path, "embed": f"![[{path.rsplit('/', 1)[-1]}]]"}


def _write_asset(canon: str, data: bytes, suffix: str) -> str:
    """The bytes under a free `Pasted image` name; its vault-relative path.
    Under `_EDIT_LOCK` because the name is chosen by looking: two pastes in one
    second take ` 2`, and atomic_write_bytes would replace the first."""
    from datetime import datetime

    from silica.kernel.base.durable import atomic_write_bytes
    from silica.kernel.base.layout import contain_in_vault
    from silica.kernel.workspaces import _EDIT_LOCK

    root = Path(CONFIG.vault_path)
    stem = f"Pasted image {datetime.now():%Y%m%d%H%M%S}"
    try:
        folder = contain_in_vault(f"{vault_files.attachment_folder(canon)}/{stem}{suffix}".lstrip("/"), root).rpartition("/")[0]
    except ValueError:
        # `../x` in app.json, or a link out of the vault: a setting does not
        # get to place a file outside it.
        folder = f"{canon.rpartition('/')[0]}/attachments".lstrip("/")

    def named(n: int) -> str:
        return f"{folder}/{stem}{f' {n}' if n > 1 else ''}{suffix}".lstrip("/")

    with _EDIT_LOCK:
        rel = next(r for r in map(named, itertools.count(1)) if not (root / r).exists())
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(root / rel, data)
    return rel


@router.post("/note/render")
def note_render(payload: dict):
    """One block of the editor's live preview (a table, a callout, a formula,
    the properties) drawn by the reader's own renderer, so the two surfaces
    cannot typeset the same markdown two ways. POST because a table does not
    fit in a query string; guarded like a write because it runs the resolver
    over the vault on another page's say-so."""
    props, body = note_parts.split_frontmatter(str(payload.get("text", "")))
    html = render.linkify(body, note_resolver()) if body.strip() else ""
    if props:
        html = render.render_frontmatter(props) + html
    return {"html": html}


class _CheckHeld(Exception):
    """A long pass waits for the person's go (ADR-0110 point 13)."""


@router.post("/note/check")
async def note_check(payload: dict, request: Request):
    """The drawer's `check sources` (ADR-0110 step 3), as JSON lines: the rule
    findings at once, one line per block as the model judges it, then the
    report. It reads the note on disk, so the drawer saves first.

    A request of its own and not a chat turn, so a check never locks the
    chat; and not ADR-0099's jobs channel, whose one worker would queue it
    behind a conversion. Cancel is the browser dropping the request: the run
    stops before its next call and keeps the verdicts paid for. `rules: true`
    asks the model nothing (the rerun after an edit, served from the cache);
    `confirm: true` lets a pass of more than CONFIRM_OVER calls go;
    `lines: [first, last]` checks only the blocks those lines touch (a card of
    a reading the reader writes, ADR-0134 point 4, or a selection, ADR-0110's
    amendment of 2026-11-06). `web: true` with `claim`, on `lines`, then asks
    the web about that claim: the vault leg's events as ever, `web` for each
    step, `web_verdict`, and `done` last."""
    from silica.cli import _check_judge, _check_legs, _check_web_legs
    from silica.kernel.check import run as check
    from silica.kernel.check import web as check_web
    from silica.kernel.base.manifest import active_write_dir
    from silica.metrics import ms_since, outcome_of

    # Off the event loop: the resolver reads the driver's graph, and every
    # open stream (the narration SSE among them) waits while the loop does.
    canon = await asyncio.to_thread(lambda: note_resolver()(str(payload.get("path", ""))))
    if not canon:
        raise HTTPException(404, "note not found in vault")
    path = canon if canon.endswith(".md") else canon + ".md"
    rules_only = payload.get("rules") is True
    confirmed = payload.get("confirm") is True
    span = payload.get("lines")
    card_lines = None
    if span is not None:
        if not (isinstance(span, list) and len(span) == 2 and all(isinstance(x, int) and x >= 1 for x in span)
                and span[0] <= span[1]):
            raise HTTPException(400, "lines is [first, last], 1-based")
        card_lines = (span[0], span[1])
    web_on = payload.get("web") is True and not rules_only
    if web_on and not check_web.is_open():
        raise HTTPException(404, "the web check is not open")
    claim, where = "", (0, 0)
    aimed: list = []
    if web_on:
        if card_lines is None:
            raise HTTPException(400, "a web check takes the selection's lines")
        text = await asyncio.to_thread(session_changes.current_text, canon)
        if text is None:
            raise HTTPException(404, "note unreadable")
        try:
            claim, aimed = check_web.aim(text, card_lines, str(payload.get("claim") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        where = (aimed[0].line, aimed[-1].end) if aimed else card_lines
    if rules_only and not await asyncio.to_thread(check.read_cache, CONFIG.vault_path, path):
        # Never checked (or its file torn): a rerun has no marks to bring back.
        return JSONResponse({"event": "done", "unchecked": True}, media_type="application/x-ndjson")
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def put(ev: dict | None) -> None:
        with suppress(RuntimeError):  # loop closed: the browser is gone, and so is the reader
            loop.call_soon_threadsafe(q.put_nowait, ev)

    def progress(ev: dict) -> None:
        if ev["event"] == "rules" and ev["calls"] > check.CONFIRM_OVER and not confirmed:
            put({**ev, "event": "confirm"})
            raise _CheckHeld
        put(ev)

    def press(report: dict, judge) -> None:
        """The web leg of a selection, after the vault's (point 8: two
        verdicts, never one). A range that touches nothing checkable asks no
        model. Only a verdict is kept: "the web did not answer" says nothing
        about the block, and a verdict paid for earlier stands."""
        result = {"web": "", "say": check_web.NOTHING_TO_CHECK}
        if aimed:
            lanes, fetch = _check_web_legs()
            result = check_web.check_claim(claim, search=lanes, fetch=fetch, judge=judge,
                                           step=lambda ev: put({"event": "web", **ev}), cancelled=stop.is_set)
        findings, states = check_web.rows(result, claim, check_web.vault_says(report), *where)
        if result["web"]:
            row = (findings or states)[0]
            check.keep_web(CONFIG.vault_path, path, aimed[0],
                           {k: v for k, v in row.items() if k not in ("line", "end")})
        put({"event": "web_verdict", "findings": findings, "states": states})

    def work() -> None:
        started = time.monotonic()
        said = {"web": True} if web_on else {}  # that a web was asked, never what (ADR-0100)
        try:
            search, dense, floor = _check_legs()
            judge = None if rules_only else _check_judge(cite=card_lines is not None)
            report = check.check_note(path, CONFIG.vault_path, write_dir=active_write_dir(), search=search,
                                      dense=dense, dense_floor=floor, judge=judge,
                                      cache_only=rules_only, progress=progress, cancelled=stop.is_set,
                                      lines=card_lines)
            if web_on:
                press(report, judge)
            put({**report, "event": "done"})
            if not rules_only:  # a rerun after an edit is not a check the person asked for
                c = report["counts"]
                emit("check", outcome="ok", ms=ms_since(started), blocks=report["blocks"], cached=report["cached"],
                     error=c["error"], warning=c["warning"], nit=c["nit"], **said)
        except _CheckHeld:
            pass
        except Exception as e:
            logger.warning("/note/check failed on %s: %s", path, e)
            put({"event": "error", "say": f"the check failed: {e}"})
            if not rules_only:
                emit("check", outcome=outcome_of(e), ms=ms_since(started), **said)
        finally:
            put(None)

    threading.Thread(target=work, name="note-check", daemon=True).start()

    async def lines():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), sse.POLL_S)
                except asyncio.TimeoutError:
                    if session.stopping() or await request.is_disconnected():
                        return
                    continue
                if ev is None:
                    return
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        finally:
            stop.set()  # a dropped request is the cancel

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})


@router.post("/note/check/keep")
async def note_check_keep(payload: dict):
    """`keep in Inbox` in a web verdict's box (ADR-0110, amendment of
    2026-11-06, point 13): `{path, url, quote, claim}` as one cited note in the
    Inbox. Regime 2 of ADR-0015 and no third: the note waits for /nucleate. The
    turn's slot, as /vault/fetch takes it for the same Inbox."""
    from silica.kernel.check import web as check_web
    from silica.sources.web_research import keep_check

    if not check_web.is_open():
        raise HTTPException(404, "the web check is not open")
    canon = await asyncio.to_thread(lambda: note_resolver()(str(payload.get("path", ""))))
    if not canon:
        raise HTTPException(404, "note not found in vault")
    url, quote, claim = (str(payload.get(k) or "").strip() for k in ("url", "quote", "claim"))
    if not url.startswith(("https://", "http://")) or not quote or not claim:
        raise HTTPException(400, "name the page, its words and the claim")
    await asyncio.to_thread(note_ops.has_room, 1, len(quote) + len(claim))
    with session.turn_slot("a turn is running: keep it when it ends"):
        return {"ok": True, "path": await asyncio.to_thread(keep_check, claim, url, quote, canon)}


# --- a note's history (ADR-0144) ---------------------------------------------
# Versions come from two stores: the checkpoint rows of interactive writes (an
# int id) and the undo journal's before-images of runs ("j<id>"). The drawer's
# History section behind Info reads these; it is drawn after the owner's pick.

def _history_note(path: str) -> str:
    """The vault path a history request names, resolved the way /undo resolves
    a card's argument; 404 when no note answers to it."""
    from silica.driver import DRIVER

    try:
        return DRIVER.read_note(path).ref.path
    except Exception:
        raise HTTPException(status_code=404, detail="no such note")


def _history_body(path: str, version: str) -> str:
    from silica.config import CONFIG
    from silica.kernel.write.checkpoints import get_checkpoint_store
    from silica.kernel.write.undo_journal import get_undo_journal

    body = None
    v = str(version)
    if v.startswith("j") and v[1:].isdigit():
        body = get_undo_journal().before_image(int(v[1:]), path,
                                               (CONFIG.vault_path or "").strip() or None)
    elif v.isdigit():
        body = get_checkpoint_store().version(path, int(v))
    if body is None:
        raise HTTPException(status_code=404, detail="no such version of this note")
    return body


@router.get("/note/history")
def note_history(path: str = ""):
    """Every version of one note, newest first: what Silica wrote, what it
    found edited by hand, what a run found before writing over it."""
    from silica.config import CONFIG
    from silica.kernel.write.checkpoints import get_checkpoint_store
    from silica.kernel.write.undo_journal import get_undo_journal

    from silica.driver import DRIVER

    note = _history_note(path)
    versions = get_checkpoint_store().history(note, current=DRIVER.read_note(note).content) + get_undo_journal().before_images(
        note, (CONFIG.vault_path or "").strip() or None)
    versions.sort(key=lambda v: v["at"], reverse=True)
    return {"path": note, "name": note_parts.clean_name(note), "versions": versions}


@router.get("/note/history/diff")
def note_history_diff(path: str = "", id: str = ""):
    """One past version against the note as it is now: `-` is what restoring
    it would take out of the note, `+` what it would bring back."""
    from silica.driver import DRIVER

    note = _history_note(path)
    body = _history_body(note, id)
    current = DRIVER.read_note(note).content
    rows = note_ops.diff_rows(current, body)
    added, removed = session_changes.tally(current, body)
    return {"path": note, "id": id, "added": added, "removed": removed,
            "lines": rows[:note_ops.MAX_DIFF_LINES], "clipped": max(0, len(rows) - note_ops.MAX_DIFF_LINES)}


@router.post("/note/history/restore")
def note_history_restore(payload: dict):
    """Write one past version back as the note's body. A restore is a write
    like any other: it is pushed as a version, so /undo takes it back."""
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import push_landed
    from silica.kernel.write.preserve import Loss

    note = _history_note(str(payload.get("path", "")))
    version = str(payload.get("id", ""))
    body = _history_body(note, version)
    with session.turn_slot("a turn is running: restore when it ends"):
        prior = DRIVER.read_note(note).content
        DRIVER.overwrite(note, body, loss=Loss.RESTORE)
        push_landed(note, prior, body, kind="restore", of=version)
    return {"restored": note, "id": payload.get("id")}


@router.get("/asset")
def asset(path: str = ""):
    """Vault-relative attachment for the note drawer, `<img>`-only by contract.
    Extension whitelist + resolved-inside-the-vault check close traversal.

    `![[img.png]]` embeds name an attachment by basename even when the file
    lives in an attachments subfolder, so an exact-path miss falls back to a
    first-match basename search under the vault (Obsidian's shortest-path rule
    minus the nearest-to-note tiebreak). rglob stays inside root, so traversal
    is still closed on the fallback path.
    ponytail: per-request rglob on the miss case; build a basename index if a
    large vault makes it slow."""
    found = render.find_asset(path)
    if found is None:
        raise HTTPException(status_code=404)
    return FileResponse(found, headers=render.ASSET_HEADERS)
