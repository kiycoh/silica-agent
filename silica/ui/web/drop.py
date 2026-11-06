# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What reaches the vault by a person's hand: an upload streamed to disk under its caps, a drop
staged for its turn, an import's files, and the embedding that follows them."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from silica.config import CONFIG
from silica.kernel.base.formats import INGEST_LABELS
from silica.kernel.recall import note_index
from silica.metrics import emit
from silica.ui.web.failures import explain as _failure

logger = logging.getLogger(__name__)


MAX_IMAGE_BYTES = 20 * 2**20


# A scanned book is the honest ceiling for one drop; past that the file is not
# something the nucleate lane can chew, and the Inbox is a vault folder, not a
# dump. The body is streamed in chunks rather than read whole, so an oversized
# upload is refused after one chunk instead of after materialising it in memory.
UPLOAD_MAX_BYTES = 256 * 1024 * 1024
UPLOAD_MAX_FILES = 32
UPLOAD_CHUNK = 1024 * 1024
# A mail export is no drop: Google Takeout's "All mail" file runs to gigabytes,
# and the drop cap above refused every real one (2026-10-12). The ceiling here
# is RAM, not the nucleate lane: mail.plan() holds every message body.
# ponytail: measured 2026-10-12 on a synthetic Takeout, 1 GiB took 17 s to a
# first dry run and held 290 MB, so 8 GiB holds about 2.3 GB, twice that for
# non-ASCII mail. The upgrade is a headers-first pass that reads bodies only
# for the threads written; reopen at ADR-0090's gate 4 review.
MAIL_UPLOAD_MAX_BYTES = 8 * 1024 ** 3


async def _write_upload(f: UploadFile, dest: Path) -> None:
    """Stream one dropped file to `dest`, refusing it past the drop cap."""
    async def chunks():
        while chunk := await f.read(UPLOAD_CHUNK):
            yield chunk

    await write_stream(chunks(), dest, UPLOAD_MAX_BYTES,
                        f"{dest.name} is over the {UPLOAD_MAX_BYTES // (1024 * 1024)} MB upload limit")


def _part_path(dest: Path) -> Path:
    """`.<name>.part` beside `dest`, its name cut to the 255 bytes a file name
    may have: a drop's name is the browser's, and only the import caps it."""
    stem = dest.name
    while len(f".{stem}.part".encode("utf-8")) > 255:
        stem = stem[:-1]
    return dest.with_name(f".{stem}.part")


async def write_stream(chunks, dest: Path, cap: int, too_big: str) -> None:
    """Write `chunks` to `dest`, refusing past `cap` bytes with `too_big`.

    Written whole before `dest` exists (ADR-0099 point 3): into `.<name>.part`,
    fsynced, then renamed. A file written in place was readable half-done by
    anything listing the Inbox, and a kill mid-upload left that half where a
    job record could name it.

    Opened without following a link: a symlink planted at Inbox/<name> made
    an upload overwrite the file it pointed to, outside the vault (found end
    to end 2026-10-12). O_NOFOLLOW makes the check and the open one step; the
    is_symlink() check stands in where the platform has no O_NOFOLLOW. The
    rename replaces a link planted at `dest` after the check, never its target."""
    import errno

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if dest.is_symlink():
        raise HTTPException(400, f"{dest.name} in the inbox is a link; not writing through it")
    part = _part_path(dest)
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HTTPException(400, f"{dest.name} in the inbox is a link; not writing through it") from None
        raise
    written = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in chunks:
                written += len(chunk)
                if written > cap:
                    raise HTTPException(413, too_big)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, dest)
    except BaseException:
        # No half file left in the vault, whatever stopped it: the cap, a full
        # disk, or a browser that stopped sending a multi-gigabyte export.
        part.unlink(missing_ok=True)
        raise


async def stage_uploads(files: list[UploadFile], folder: str | None = None, **job: Any
                         ) -> tuple[list[str], list[str], list[tuple[str, str]], list[dict]]:
    """Write uploads to Inbox and mechanically stage them, mirroring the inline
    half of `/nucleate` (silica/sources/ingest.py): code/notebooks become skeleton stubs,
    prose stays as-is, and a file that needs convert() becomes a job.
    Returns (ready, stubs, skipped, queued): markdown notes ready for the
    injector/reading, code stub note paths already written to the vault,
    (file name, reason) for each file no lane took, and the job records
    (`job` carries the drop's text and session into each). The semantic step
    (nucleate? summarize?) is the agent's, driven by the user's message: see
    `drop_turn.compose_nucleate_turn`.

    `folder`, vault-relative, takes the place of the inbox: a chat's drop is
    written to the chat's own folder (ADR-0121 point 5).

    A conversion never runs here (ADR-0099 point 1): a lecture's transcription
    outlasts the edge's 125 s, and a restart lost it without a trace. stage()
    reads whole files, so it runs in a worker thread: on the loop thread it
    blocked /stop, leaving a visible Stop button that could not be served.
    """
    from silica.config import hosted
    from silica.kernel.base.formats import HOSTED_EXTENSIONS, IMG_EXTS
    from silica.kernel.base.manifest import active_write_dir, get_active_manifest, resolve_inbox_dir
    from silica.sources import pdf_extract
    from silica.sources.registry import adapter_for, stage
    from silica.ui.web import jobs

    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(413, f"at most {UPLOAD_MAX_FILES} files per drop")
    # ADR-0087 point 5 on the server: a drop takes what the chat's + offers, the
    # hosted list and the images that ride with it, because a dragged file
    # never meets the picker's `accept`. Until 2026-10-14 a dragged video or
    # .docx reached the converter here.
    taken = (*HOSTED_EXTENSIONS, *IMG_EXTS) if hosted() else None
    # The inbox inside the write boundary, as active_inbox_dir() composes it; a
    # literal root Inbox/ staged drops outside a vault that writes only under
    # docs/silica. "Inbox" stands in when SILICA_INBOX_DIR switches the inbox
    # off, because a drop still needs a folder and that one is composed too.
    vault = CONFIG.vault_path or "."
    inbox_rel = folder or resolve_inbox_dir(vault, active_write_dir(), CONFIG.inbox_dir or "Inbox")
    inbox = Path(vault) / inbox_rel
    inbox.mkdir(parents=True, exist_ok=True)
    enabled = get_active_manifest().sources
    ready: list[str] = []
    stubs: list[str] = []
    skipped: list[tuple[str, str]] = []
    queued: list[dict] = []
    for f in files:
        # `.name` is the containment: a client-chosen filename never contributes
        # a directory, and one that is nothing but directories ("..") gets the
        # fallback rather than resolving to the Inbox itself.
        dest = inbox / (Path(f.filename or "").name or "dropped")
        if is_mail(dest.name):
            continue  # never written: see is_mail
        # The file's ingest line (ADR-0100 point 3). The page counts come from
        # the conversion's own read (pdf_extract.report), never a second parse
        # here, which would double the untrusted PDF work ADR-0087 keeps in a
        # child; they stay null for anything that is not a PDF.
        ingest: dict[str, Any] = {"ext": _ingest_ext(dest.name), "bytes": None, "pages": None,
                  "pages_without_text": None, "pages_read": None, "outcome": "error"}
        job_writes_it = False  # a converted file's line waits for its job's outcome
        try:
            if taken is not None and dest.suffix.lower() not in taken:
                # Never written, but counted: the line is the attempt's, by type.
                ingest["bytes"] = f.size
                skipped.append((dest.name, f"{dest.suffix.lower() or 'no extension'}: not a type Silica reads here"))
                continue
            await _write_upload(f, dest)
            ingest["bytes"] = dest.stat().st_size
            rel = f"{inbox_rel}/{dest.name}"
            adapter = adapter_for(rel, enabled=enabled)
            if adapter is None:  # no source claims it: convert() runs as a job (ADR-0099)
                try:
                    queued.append(await asyncio.to_thread(
                        jobs.record, rel, origin="drop", **job, on_end=partial(_ingest_ended, ingest)))
                    job_writes_it = True
                except ValueError as exc:  # admission refused it, before it was queued
                    logger.warning("nucleate: skipped %s: %s", dest.name, exc)
                    skipped.append((dest.name, _skip_reason(exc)))
                    ingest.update(pdf_extract.report(dest)[0], outcome=getattr(exc, "outcome", "error"))
                continue
            result = await asyncio.to_thread(stage, adapter, rel)
            if result["status"] == "distill":       # prose → injector re-reads it
                ready.append(rel)
                ingest["outcome"] = "ok"
            elif result["status"] == "ok":            # code/notebook → stub written
                stubs.append(result["note_path"])
                ingest["outcome"] = "ok"
            else:
                logger.warning("nucleate: %s: %s", dest.name, result.get("message", ""))
                skipped.append((dest.name, _skip_reason(result.get("message") or "no lane could read it")))
        finally:
            if not job_writes_it:
                emit("ingest", **ingest)
    return ready, stubs, skipped, queued


def _ingest_ended(ingest: dict, rec: dict) -> None:
    """A converted drop's ingest line (ADR-0100 point 3), written when its job
    ends. An interrupted job writes none: the line counts conversions that had
    an outcome, and the retry that gives it one is a new run, not an upload.
    The page counts and the outcome's word (partial, no_text, garbage) are the
    job's, from the read its conversion did (ADR-0087)."""
    if rec.get("state") == "done":
        outcome = "partial" if rec.get("pages_not_read") else "ok"
    else:
        outcome = rec.get("outcome") or "error"
    emit("ingest", **{**ingest, **(rec.get("pages") or {}), "outcome": outcome})


def _skip_reason(exc: Exception | str) -> str:
    """Why a dropped file was not staged, as the agent will repeat it.

    A refusal admission wrote for the user goes as it is. Any other converter
    text can carry a host path or an internal URL (the transcriber's address,
    ffmpeg's stderr), so hosted, where raw errors stay in the log (ADR-0085
    point 5), it becomes the vault import's sentence for the same case."""
    from silica.config import hosted
    from silica.sources.media_admission import MediaRefused
    from silica.sources.pdf_extract import PdfUnreadable

    # PdfUnreadable is ADR-0087 point 3's sentence, written for the student
    # and naming no tool or path, so it goes as it is on the server too.
    if isinstance(exc, (MediaRefused, PdfUnreadable)) or not hosted():
        return str(exc)
    return "it could not be converted"


def _ingest_ext(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if ext in INGEST_LABELS else "other"


def is_mail(name: str) -> bool:
    """A mail export dropped into the chat (ADR-0090 point 5): never staged.
    Staged, it wrote a note outside any undo run (so /revert took back the
    import before it) with no dry run and no address filter, and a refusal
    reached only the log (found end to end 2026-10-12). Mail goes through the
    Mail row or /mail import, which have both."""
    from silica.sources.mail import SUFFIXES

    return Path(name).suffix.lower() in SUFFIXES


# Measured from the pilot host 2026-10-17: yt-dlp gets "Sign in to confirm
# you're not a bot" for a public video, as YouTube answers datacenter
# addresses. With no account cookies on the server it is refused before a
# request goes out; with them, it is this same sentence when YouTube asks
# again (the account gone, the cookies expired), with the way that works.
YOUTUBE_ON_SERVER = ("YouTube does not let the server read videos (it asks it to sign in): "
                     "download the video's audio and add that file, and it is transcribed")


async def import_files(files: list[UploadFile], paths: list[str]) -> tuple[list[dict], set[str]]:
    from silica.config import hosted
    from silica.kernel.base.layout import ignore_matcher
    from silica.kernel.base.manifest import (
        _UNRESOLVABLE_WRITE_DIR,
        active_write_dir,
        get_active_manifest,
    )
    from silica.sources import pdf_extract
    from silica.sources.media_admission import MediaRefused
    from silica.sources.registry import adapter_for, stage
    from silica.ui.web import jobs
    from silica.ui.web import vault_import as vi

    on_server = hosted()
    vault = Path(CONFIG.vault_path or ".")
    # At the top of the vault, or of the folder writes are confined to: what the
    # + brings is the person's own material, used as it is, and moving a folder
    # into the Inbox is how they ask for it to be nucleated (owner, 2026-11-02,
    # amending ADR-0098 point 4, which put every import in the Inbox).
    root_rel = active_write_dir().strip("/")
    if root_rel == _UNRESOLVABLE_WRITE_DIR:
        # The sentinel is a hidden folder the index never reads: an import
        # there would be files no question can reach, so it is refused.
        raise HTTPException(409, "the vault's write folder cannot be found: fix write_dir in vault.yaml first")
    root = vault / root_rel if root_rel else vault
    root.mkdir(parents=True, exist_ok=True)
    allowed = set(vi.importable_extensions(on_server))
    ignore = ignore_matcher(vault)
    enabled = get_active_manifest().sources
    results: list[dict] = []
    folders: set[str] = set()
    waiting: dict | None = None  # vi.orphans, once a request: notes whose raw is gone, by hash
    for f, given in zip(files, paths):
        row: dict = {"name": given or Path(f.filename or "").name}
        dest: Path | None = None
        checked = False  # written, and its bytes are what its extension says
        try:
            # A file picked on its own keeps its name only, as a drop does; a
            # folder's file keeps the folder's tree (point 4).
            rel = vi.clean_rel(given or Path(f.filename or "").name)
            suffix = rel.suffix.lower()
            if vi.skipped(rel, ignore):
                row.update(status="skipped", reason="inside a hidden or ignored folder")
                continue
            if suffix not in allowed:
                row.update(status="skipped", reason=f"{suffix or 'no extension'}: not a type Silica reads here")
                continue
            if suffix == ".txt":
                # The index reads .md only: plain text comes in as the note it
                # already is, not as a file no question can reach.
                rel = rel.with_suffix(".md")
            dest = vi.numbered(root / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.parent.resolve().is_relative_to(root.resolve()):
                raise vi.Refused("its folder leads outside the vault")
            dest_rel = dest.relative_to(vault).as_posix()
            if suffix in (".md", ".txt"):
                # A note goes in through the driver, so the index knows it at
                # once: a raw write followed by the driver's next write in the
                # same folder was absorbed by its directory stamps, and the
                # tree showed 3 of 5 imported notes (seen 2026-10-13).
                data = bytearray()
                while chunk := await f.read(UPLOAD_CHUNK):
                    data.extend(chunk)
                    if len(data) > vi.TEXT_MAX_BYTES:
                        raise vi.Refused(f"it is over the {vi.TEXT_MAX_BYTES // (1024 * 1024)} MB limit for a note")
                content = vi.check_text(bytes(data))
                from silica.driver import DRIVER

                await asyncio.to_thread(DRIVER.create, dest_rel, content)
            else:
                if waiting is None:
                    # Before this file is written: a note naming its bare name
                    # would find it beside itself and no longer read as lost.
                    waiting = await asyncio.to_thread(vi.orphans, vault)
                cap = pdf_extract.MAX_BYTES if on_server else UPLOAD_MAX_BYTES

                async def chunks(upload=f):
                    while chunk := await upload.read(UPLOAD_CHUNK):
                        yield chunk

                await write_stream(chunks(), dest, cap, f"it is over the {cap // (1024 * 1024)} MB limit")
                if suffix == ".pdf":
                    pdf_extract.require_header(dest)
                vi.check_signature(dest, suffix)
            checked = True
            row["path"] = dest_rel
            top = rel.parts[0] if len(rel.parts) > 1 else ""
            # A file picked alone at the top of a whole-vault boundary names no
            # folder: "" would embed the entire vault, the notes still waiting
            # on their vectors included, so the search's own sweep catches it up.
            if folder := "/".join(x for x in (root_rel, top) if x):
                folders.add(folder)
            if suffix not in (".md", ".txt"):
                attached = await asyncio.to_thread(vi.attach, dest, vault, waiting or {})
                if attached:  # the file its notes lost, brought back: nothing to convert
                    row.update(status="note", notes=attached, attached=True)
                    continue
            adapter = adapter_for(dest_rel, enabled=enabled)
            if adapter is None:  # no source claims it: the converter, as a job (ADR-0099)
                # Its notes, and the hidden text it left out, arrive on the job.
                row.update(status="queued", job=(await asyncio.to_thread(jobs.record, dest_rel, origin="import"))["id"])
                continue
            staged = await asyncio.to_thread(stage, adapter, dest_rel)
            if staged["status"] == "distill":  # prose: the file is the note
                row.update(status="note", notes=[dest_rel])
            elif staged["status"] == "ok":       # code or a notebook: a stub note
                row.update(status="note", notes=[staged["note_path"]])
            else:
                raise vi.Refused(staged.get("message") or "no lane could read it")
        except HTTPException as exc:  # the size cap, or a symlink in the way: authored above
            row.update(status="refused", reason=exc.detail)
        except (vi.Refused, pdf_extract.PdfUnreadable, MediaRefused) as exc:
            # Sentences written for the student (vault_import, pdf_extract,
            # media admission), so the text itself is the reason.
            row.update(status="refused", reason=exc.args[0])
        except ValueError as exc:
            # A converter's own reason can carry a path or a tool's output, so
            # it is detail, which explain() leaves empty on the server (ADR-0085).
            row.update(status="refused", reason="it could not be converted", detail=_failure(exc)["detail"])
        except Exception as exc:
            # Tolerated, not swallowed: one file a lane chokes on must not cost
            # the student the rest of the folder (H4), so it is named in its
            # row and logged with its type; the batch goes on.
            logger.warning("vault import: %s failed: %s", row["name"], type(exc).__name__)
            row.update(status="refused", reason="it could not be read")
        finally:
            results.append(row)
            # A queued file is its job's now, which settles it when it ends.
            if dest is not None and dest.is_file() and not dest.is_symlink() and row.get("status") != "queued":
                refused = row.get("status") == "refused"
                # On the server nothing refused stays in a capped directory; a
                # file that became a note stays as its raw (ADR-0129 point 1).
                # Locally only a file that is not what its extension says goes:
                # a real document a lane could not read stays for mineru and
                # /report.
                if (on_server and refused) or (refused and not checked):
                    dest.unlink()
    return results, folders


# How long the embedding of an import waits for the conversion queue to
# drain. It bounds a waiting thread, not the work: an import is up to 500
# files (ADR-0098 H2) and the queue runs one at a time, so a large one can
# outlast it, and the notes it has not reached wait for the next /embed.
_EMBED_WAIT_S = 3 * 3600


def embed_imported(folders: set[str]) -> None:
    """Embed what an import wrote, so the dense leg finds it without a manual
    /embed (ADR-0098 point 6). In the background: on the pilot's CPU embedder
    a folder can take minutes, and the import's answer should not wait. After
    the conversion queue drains (ADR-0099): a PDF's note exists only then.
    ponytail: an agent-run /embed during this thread writes the same store; a
    lock on the store itself is the upgrade, due when a run shows the race."""
    def run() -> None:
        from silica.tools import TOOLS
        from silica.ui.web import jobs

        jobs.drain(_EMBED_WAIT_S)
        with note_index.EMBED_LOCK:
            for folder in sorted(folders):
                try:
                    TOOLS["silica_embed_refresh"].run(folder=folder)
                except Exception as exc:
                    # The import is done and the notes are searchable by words;
                    # vectors arrive with the next /embed instead.
                    logger.warning("vault import: embedding %s failed: %s", folder, type(exc).__name__)

    threading.Thread(target=run, name="vault-import-embed", daemon=True).start()
