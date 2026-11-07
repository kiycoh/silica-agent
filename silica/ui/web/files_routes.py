# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Files in and out of the vault: a drop, an import, a link, a mail export and the vault's zip."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from silica.config import CONFIG
from silica.kernel import vault_files
from silica.sources import drop_turn
from silica.ui.web import drop, security, session, sse, turn
from silica.ui.web.failures import explain as _failure
from silica.ui.web.session import SESSION

logger = logging.getLogger(__name__)

router = APIRouter()


# The body a request may carry through Cloudflare on the Free and Pro plans
# (Error 413 page, read 2026-10-13; it names no exception for Tunnel traffic).
# On the server the browser refuses a file over it before sending, with this
# reason, rather than show the edge's own 413 page (ADR-0099 Consequences).
_EDGE_MAX_MB = 100


@router.get("/supported_types")
def supported_types():
    """Extensions the pickers offer: `extensions` drives the chat `+`'s
    `accept`, `import` the vault `+`'s and its folder filter (ADR-0098 point 3).
    `mail` is the Mail row's picker, kept apart: the composer stages no mail
    (`drop.is_mail`). On the server both lists are ADR-0087 point 5's: the host
    has no mineru and no LibreOffice, and offered 60 extensions until then.
    `images` ride the chat's `+` even where no converter reads them: they go with
    the message. `import_max` is what one vault `+` may hold, for the page to say
    so before it sends the first of its batches."""
    from silica.config import hosted
    from silica.kernel.base.formats import HOSTED_EXTENSIONS, IMAGE_MIME
    from silica.sources.mail import SUFFIXES
    from silica.sources.registry import supported_nucleate_extensions
    from silica.ui.web.vault_import import MAX_FILES_PER_IMPORT, importable_extensions

    on_server = hosted()
    exts = list(HOSTED_EXTENSIONS) if on_server else supported_nucleate_extensions()
    return {"extensions": exts, "import": importable_extensions(on_server), "mail": list(SUFFIXES),
            "upload_max_mb": _EDGE_MAX_MB if on_server else None,
            "images": list(IMAGE_MIME), "import_max": MAX_FILES_PER_IMPORT}


@router.post("/nucleate")
async def nucleate(files: list[UploadFile] = File(...), text: str = Form("")):
    """A drop: the files into the chat's own folder, and the turn that acts on them.

    The files are the chat's, not the vault's (ADR-0121 point 5): written to
    `.silica-chat/<session>/`, which no walk lists, and copied into the Inbox
    only when the student asks. So the session opens first, and a fresh one is
    named now, after the words or the files: the chat then lists at once, and
    each job shows in that chat only.

    A file that needs convert() is a job (ADR-0099), so nothing here waits on
    a conversion. A drop with words and a file still to convert starts no turn
    (ADR-0121 point 1): it answers 202 with the jobs, and the page that holds
    this chat asks the question once the drop's last job is done. A drop of
    only such files and no words answers 202 too; each one, read, offers its
    turns. Otherwise the turn starts with the files that were ready."""
    from silica.kernel.base.layout import chat_folder
    from silica.ui.web import jobs

    with session.turn_slot() as slot:
        fresh = not any(m.get("role") == "user" for m in SESSION.chat.messages)
        sid = await asyncio.to_thread(session.open_session) or uuid.uuid4().hex[:12]
        names = [Path(f.filename or "").name for f in files]
        if fresh:
            title = (text.strip().splitlines() or [", ".join(names)])[0]
            await asyncio.to_thread(_name_session, sid, title)
        # dest_dir too: a converted PDF's pictures go to `<dest_dir>/Images`,
        # which would otherwise be the vault's Inbox.
        folder = chat_folder(sid)
        ready, stubs, skipped, queued = await drop.stage_uploads(
            files, folder=folder, text=text, session=sid, drop=uuid.uuid4().hex[:12], dest_dir=folder)
        mail = [n for n in names if drop.is_mail(n)]
        # The question waits for its files (ADR-0121 point 1) but not for a PDF
        # with a text layer, which this turn reads while it converts (ADR-0099
        # amendment 2026-10-20). A drop waits whole when one file must: its
        # question is asked once, with all of them (the amendment's recommendation).
        now = ([j for j in queued if await asyncio.to_thread(drop_turn.readable_now, j["source"]["path"])]
               if text.strip() else [])
        waits = bool(text.strip()) and len(now) < len(queued)
        if queued and (waits or not (ready or stubs or now)):
            # The ready files of the same drop ride its question when it is asked.
            await asyncio.to_thread(jobs.annotate, [j["id"] for j in queued], beside=ready)
            return JSONResponse({"jobs": queued, "waits": waits, "mail": mail, "session": sid,
                                 "skipped": [{"name": n, "reason": why} for n, why in skipped]},
                                status_code=202)
        if now:
            # Before the turn: a short PDF can be converted before it ends.
            # Answered: the page would ask the drop's question again once the note
            # is written. Not told: the next turn hears of the note (jobs.settled).
            await asyncio.to_thread(jobs.annotate, [j["id"] for j in now], answered=True)
        files_said = [drop_turn.file_entry(p) for p in ready] + [drop_turn.file_entry(j["source"]["path"], job=j["id"]) for j in queued]
        # A thread: the manifest reads each staged note, and a textbook read on
        # the loop thread blocks /stop.
        resp = sse.turn_response(slot, turn.Ask(await asyncio.to_thread(
            drop_turn.compose_nucleate_turn, text, ready, stubs, mail, skipped,
            converting=[j["source"]["path"] for j in queued]),
            withhold=drop_turn.WITHHELD_WITH_FILES, files=files_said, said=text.strip()))
        if queued:
            resp.headers["X-Silica-Jobs"] = ",".join(j["id"] for j in queued)
        return resp


def _name_session(sid: str, title: str) -> None:
    """A chat named by its drop (ADR-0121 point 1): a session with no turn yet
    is unlisted, and a deferred question has none until its file is read."""
    from silica.obs import narration as _narr

    title = title.strip()
    _narr.rename_session(sid, title[:57] + "…" if len(title) > 58 else title, CONFIG.vault_path or "")


@router.post("/chat/files/add")
async def chat_file_add(payload: dict):
    """"add to vault" on a file of the open chat (ADR-0121 point 5): the file
    and its notes copied into the Inbox, with no agent turn. The notes are
    the job's when the file was converted, else the file itself."""
    from silica.kernel import job_records

    source = str(payload.get("source") or "")
    vault_files.chat_rel(source, SESSION.chat.sid or "")
    notes = [source]
    vault = CONFIG.vault_path or ""
    done = [r for r in job_records.all_records() if r.get("vault") == vault and r["source"].get("path") == source
            and r.get("state") == "done" and r.get("notes")]
    if done:
        notes = done[-1]["notes"]
    elif not source.endswith((".md", ".txt", ".markdown")):
        raise HTTPException(409, "that file is still being read")
    with session.turn_slot("a turn is running: add it when it ends"):
        added = await asyncio.to_thread(vault_files.add_to_vault, notes, [source])
    return {"notes": added}


@router.post("/vault/import")
async def vault_import(files: list[UploadFile] = File(...), paths: list[str] = Form([]),
                       last: str = Form(""), workspace: str = Form(""), obsidian: str = Form("")):
    """The vault's `+` (ADR-0098 point 5): files, or a folder's files with
    their relative paths, at the top of the vault, each through its lane, with
    no agent turn. One result per file; one file's failure never stops the others (H4).

    `paths[i]` is the folder-relative path of `files[i]` (`webkitRelativePath`),
    "" for a file picked on its own. `last` marks the page's final batch, after
    which the imported folders are embedded in the background. `workspace`
    (ADR-0112 point 6) is the workspace whose `+` was used: each imported
    note joins its `notes:`, a file still converting by its name, which
    resolves when the job writes the note. `obsidian` names the picked folder
    when it held `.obsidian/` (ADR-0142 point 5).

    The turn slot is held while the batch writes: `turn_slot` is the one
    serialisation of vault writes the server has, and an import racing a
    turn's writes is a case nothing tests.
    ponytail: one writer at a time, so an import waits for a turn; a lock per
    Inbox path is the upgrade, due when students ask to import mid-turn.
    """
    if len(files) > drop.UPLOAD_MAX_FILES:
        raise HTTPException(413, f"at most {drop.UPLOAD_MAX_FILES} files per request")
    if paths and len(paths) != len(files):
        raise HTTPException(400, "paths must name every file, in order")
    with session.turn_slot("a turn is running: import when it ends"):
        results, folders = await drop.import_files(files, paths or [""] * len(files))
    if obsidian:
        _mark_obsidian_root(obsidian, paths)
    if last and folders:
        drop.embed_imported(folders)
    if workspace:
        await asyncio.to_thread(_list_imports_in, workspace, results)
    return {"files": results}


def _mark_obsidian_root(name: str, paths: list[str]) -> None:
    """ADR-0142 point 5: below this folder, a path is where the person keeps
    that file in Obsidian. The page's word is taken, because a wrong mark only
    misdirects links in that person's own archive; it must still name a folder
    of this batch. The server's only, as the downloads that read it."""
    from silica.config import hosted
    from silica.ui.web import vault_export as ve
    from silica.ui.web import vault_import as vi

    if not hosted():
        return
    firsts = set()
    for p in paths:
        try:
            firsts.add(vi.clean_rel(p).parts[0])
        except vi.Refused:
            continue  # the import refused that file too
    try:
        root = vi.clean_rel(name).parts[0]
    except vi.Refused:
        return
    if root in firsts:
        ve.mark_root(Path(CONFIG.vault_path), root)


# --- the vault's two downloads (ADR-0142) ------------------------------------
# The server's only: on a laptop the vault is the person's own folder, which
# Obsidian already opens.

def _export_vault() -> Path:
    from silica.config import hosted

    if not hosted():
        raise HTTPException(404, "the vault is a folder on this machine: there is nothing to download")
    if not CONFIG.vault_path:
        raise HTTPException(409, "no vault is open")
    return Path(CONFIG.vault_path)


@router.get("/vault/export/summary")
def vault_export_summary():
    """What each of the vault menu's two downloads would give, asked when the
    menu opens, so that "nothing new" is said before a click (ADR-0142 point 1)."""
    from silica.ui.web import vault_export as ve

    return ve.summary(_export_vault())


# The one read the rule by method does not reach (ADR-0142): a GET, because a
# link is what streams the archive to the person's disk, and still this page's
# alone, or any other page could ask for the whole vault.
@router.get("/vault/export", dependencies=[Depends(security.require_same_origin)])
def vault_export():
    """The vault whole, every file in its tree (ADR-0142 point 2). A link the
    browser follows, so the archive streams to the person's disk."""
    from silica.ui.web import vault_export as ve

    vault = _export_vault()
    top = time.strftime("Silica vault %Y-%m-%d")
    return StreamingResponse(ve.whole(vault, top), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{top}.zip"'})


@router.post("/vault/export/added")
def vault_export_added():
    """What Silica added since the last download, written for the root of the
    person's Obsidian vault (ADR-0142 points 3 and 4); 204 when nothing is new.
    A POST, because it moves the sent list. The page reads the folder's name
    and the links left as written off the headers, to say where it goes."""
    from silica.ui.web import vault_export as ve

    vault = _export_vault()
    if not ve.BUSY.acquire(blocking=False):
        raise HTTPException(409, "a download of what Silica added is already running")
    try:
        plan = ve.plan_additions(vault, ve.load_state(vault), time.strftime("Silica %Y-%m-%d %H%M%S"))
    except BaseException:
        ve.BUSY.release()
        raise
    if not plan.files:
        ve.BUSY.release()
        return Response(status_code=204)
    stream = ve.added(vault, plan)
    next(stream)  # started, its finally releases BUSY even if no byte is ever asked for
    return StreamingResponse(stream, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{plan.top}.zip"',
        "X-Silica-Folder": plan.top, "X-Silica-Files": str(len(plan.files)),
        "X-Silica-Left": str(plan.left)})


@router.post("/vault/fetch")
async def vault_fetch(payload: dict):
    """The vault's + "a link…" (ADR-0120): one web page, or locally a
    YouTube video's captions, into the Inbox as a note, with no agent turn:
    `/fetch` from the GUI. One row in `/vault/import`'s shape, so the page
    shows it as it shows a file.

    The address is the signed-in student's own, typed here, which is the case
    `web_fetch._validated`'s ponytail leaves open on DNS rebinding; on the
    server the unit's IPAddressDeny holds the private ranges and link-local
    under it."""
    from silica.config import hosted
    from silica.sources.web_fetch import YT_DOMAINS, YoutubeBlocked, host_matches, youtube_cookies
    from silica.sources.web_research import fetch_to_inbox

    url = str(payload.get("url") or "").strip()
    workspace = str(payload.get("workspace") or "")
    if not url or len(url) > 2048 or any(c.isspace() for c in url):
        raise HTTPException(400, "a link is one web address, at most 2,048 characters")
    row: dict = {"name": url}
    if (hosted() and youtube_cookies() is None
            and host_matches(url if "://" in url else f"https://{url}", *YT_DOMAINS)):
        return {"files": [{**row, "status": "refused", "reason": drop.YOUTUBE_ON_SERVER}]}
    with session.turn_slot("a turn is running: add the link when it ends"):
        try:
            note = await asyncio.to_thread(fetch_to_inbox, url)
            row.update(status="note", notes=[note], path=note)
        except YoutubeBlocked:
            # web_fetch logged the renewal line; the student gets the way around it.
            row.update(status="refused", reason=drop.YOUTUBE_ON_SERVER if hosted() else
                       "YouTube asked to sign in: set SILICA_YOUTUBE_COOKIES to an account's cookies "
                       "file, or download the audio and add that")
        except Exception as exc:
            # Not only ValueError: httpx raises its own on a refused connection or
            # a timeout, and either is this link refused, not a 500. The reason
            # is detail, as an import's converter text is, which explain() leaves
            # empty on the server (ADR-0085 point 5); the log keeps it there.
            logger.warning("vault fetch failed: %s: %s", type(exc).__name__, exc)
            row.update(status="refused", detail=_failure(exc)["detail"],
                       reason="that page could not be read: it may need a login, refuse automated "
                              "readers, or not be a web page")
    if row["status"] == "note":
        drop.embed_imported({str(Path(row["path"]).parent)})
        if workspace:
            await asyncio.to_thread(_list_imports_in, workspace, [row])
    return {"files": [row]}


def _list_imports_in(ref: str, results: list[dict]) -> None:
    from silica.kernel import workspaces

    ws = workspaces.find_workspace(ref)
    if ws is None:
        return
    for row in results:
        # a file still converting is listed by the note it will become
        names = row.get("notes") or ([str(Path(row["path"]).with_suffix(""))]
                                     if row.get("status") == "queued" and row.get("path") else [])
        for n in names:
            workspaces.add_line(ws["path"], "notes", n)


@router.post("/mail/upload")
async def mail_upload(request: Request, name: str):
    """The Mail row's export (ADR-0090 point 5), saved into the inbox inside
    the write boundary as `drop.stage_uploads` saves a drop, and not staged: the
    row's dry run comes next, and nothing is written before its Import.

    The body is the file itself and `name` its name, not a multipart form:
    Starlette spools a form's file to the temp dir first, which is RAM where
    /tmp is a tmpfs, and an export runs to gigabytes."""
    from silica.kernel.base.manifest import active_write_dir, resolve_inbox_dir
    from silica.sources.mail import SUFFIXES

    name = Path(name).name  # a client-chosen name never adds a directory
    if Path(name).suffix.lower() not in SUFFIXES:
        raise HTTPException(400, "the Mail row imports an .mbox or .eml file")
    vault = CONFIG.vault_path or "."
    inbox_rel = resolve_inbox_dir(vault, active_write_dir(), CONFIG.inbox_dir or "Inbox")
    (Path(vault) / inbox_rel).mkdir(parents=True, exist_ok=True)
    gb = drop.MAIL_UPLOAD_MAX_BYTES // 1024 ** 3
    await drop.write_stream(request.stream(), Path(vault) / inbox_rel / name, drop.MAIL_UPLOAD_MAX_BYTES,
                        f"{name} is over the {gb} GB the Mail row takes: export one Gmail label "
                        "from Takeout, or run /mail import with the file's path")
    return {"path": f"{inbox_rel}/{name}"}
