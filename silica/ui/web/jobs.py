# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A conversion that leaves the request (ADR-0099).

Every route that calls `convert()` records one job per file here and answers
at once; one worker thread per process runs the jobs in the order they came.
The record is a JSON file in `~/.silica/jobs/<id>.json`, written whole each
time (temp file, then rename), so a process killed at any instant leaves the
last state it reached: `running` is written before the conversion starts,
`done` only after its notes are in the vault.

Nothing resumes by itself. At startup a job still `queued` or `running`
belonged to a process that is gone, and becomes `interrupted` (point 4): under
`Restart=on-failure`, a file that kills the tenant would otherwise crash-loop
it. Retry is the student's, and reruns from the Inbox copy.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from silica.config import CONFIG, hosted

logger = logging.getLogger(__name__)

# The topic the narration stream forwards to the browser as `event: job`.
TOPIC = "jobs/update"
INTERRUPTED = "the server restarted during this conversion"
# Open 2, accepted 2026-10-13: a finished job's record goes after 30 days; a
# failed or interrupted one stays until the student dismisses it.
DONE_KEEP_S = 30 * 86400
_ID = re.compile(r"[0-9a-f]{12}")
_HASH_CHUNK = 1024 * 1024

_LOCK = threading.Lock()   # every read-modify-write of a record
_QUEUE: queue.Queue[str] = queue.Queue()
# The ids this process queued. Startup recovery leaves them alone: the suite
# starts the app many times in one process, and a job its worker is running
# is not interrupted by a second startup.
_LIVE: set[str] = set()
_PENDING = 0               # queued here and not yet ended, for drain()
_IDLE = threading.Condition()
_WORKER: threading.Thread | None = None
# In-process only: a hook a route hands over, run when the job ends (the
# ingest line of ADR-0100). A restart loses it, which loses a metric line and
# nothing a student sees.
_ON_END: dict[str, Callable[[dict], None]] = {}


class Refused(ValueError):
    """A job action turned away. The text is the sentence the student reads."""


def jobs_dir() -> Path:
    # paths._SILICA_HOME and not Path.home(): the suite repoints that one name
    # for every store under ~/.silica, as media_admission's counter relies on.
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "jobs"


def _path(job_id: str) -> Path:
    if not _ID.fullmatch(job_id or ""):
        raise Refused("there is no such conversion")
    return jobs_dir() / f"{job_id}.json"


def read(job_id: str) -> dict | None:
    import json

    path = _path(job_id)  # outside the try: a malformed id is refused, not "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        # Written whole by rename, so a record that does not parse was edited
        # by hand or damaged on disk: it is reported as missing rather than
        # taking the list of every other job down with it.
        logger.warning("unreadable job record %s", job_id)
        return None


def _all() -> list[dict]:
    out = []
    root = jobs_dir()
    if root.is_dir():
        for f in root.glob("*.json"):
            rec = read(f.stem) if _ID.fullmatch(f.stem) else None
            if rec is not None:
                out.append(rec)
    out.sort(key=lambda r: r.get("created") or 0)
    return out


def _write(rec: dict) -> None:
    import json

    from silica.kernel.recall.paths import atomic_write_bytes

    atomic_write_bytes(_path(rec["id"]), json.dumps(rec, sort_keys=True).encode("utf-8"), mode=0o600)
    _publish(rec)


def _publish(rec: dict) -> None:
    from silica.agent.bus import BUS

    BUS.publish(TOPIC, public(rec))


def _update(job_id: str, expect: tuple[str, ...] | None = None, **fields: Any) -> dict | None:
    """Apply `fields` to the record as it is on disk now, or do nothing and
    return None when the record is gone or its state is not in `expect`."""
    with _LOCK:
        rec = read(job_id)
        if rec is None or (expect is not None and rec.get("state") not in expect):
            return None
        rec.update(fields, updated=time.time())
        _write(rec)
        return rec


def public(rec: dict) -> dict:
    """The record as the browser gets it, on the stream and from GET /jobs
    alike: without the vault's path on disk, which on a hosted tenant names
    the host's directories (ADR-0085 point 5), and with whether Retry can
    work, a stopped job whose file is still there."""
    out = {k: v for k, v in rec.items() if k != "vault"}
    src = dict(rec.get("source") or {})
    if Path(src.get("path") or "").is_absolute():
        src["path"] = src.get("name") or ""
    out["source"] = src
    out["retry"] = rec.get("state") in ("failed", "interrupted") and _absolute(rec).is_file()
    return out


def _absolute(rec: dict) -> Path:
    p = Path(rec["source"]["path"])
    return p if p.is_absolute() else Path(rec.get("vault") or ".") / p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _is_media(name: str) -> bool:
    from silica.sources.convert import MEDIA_EXTS

    return Path(name).suffix.lower() in MEDIA_EXTS


# --- recording a job -------------------------------------------------------------------

def record(path: str, *, origin: str, text: str = "", session: str | None = None,
           dest_dir: str = "", on_end: Callable[[dict], None] | None = None) -> dict:
    """Record one conversion of `path` and queue it; return its record.

    `path` is what convert() takes: vault-relative, or absolute for a file a
    command named outside the vault. The file must already be whole on disk.
    A media file passes admission here (ADR-0099 Consequences), so a file
    over the length cap or this month's minutes never enters the queue; the
    decoded length and the charge stay in `_via_asr`, where the audio is.
    Raises the admission's MediaRefused, whose text is the student's sentence.
    """
    vault = CONFIG.vault_path or ""
    p = Path(path)
    absolute = p if p.is_absolute() else Path(vault or ".") / p
    if p.is_absolute() and vault and absolute.resolve().is_relative_to(Path(vault).resolve()):
        p = absolute.resolve().relative_to(Path(vault).resolve())
    media = _is_media(absolute.name)
    seconds = None
    if media:
        from silica.sources import media_admission as admission

        admission.admit(absolute)
        seconds = admission.probe_seconds(absolute)
    digest = _sha256(absolute)
    rel = p.as_posix()
    with _LOCK:
        # The same bytes already waiting or converting: a second job would
        # transcribe and charge them twice (a drop sent again, /convert typed
        # while the drop's job runs).
        for rec in _all():
            if (rec.get("state") in ("queued", "running") and rec.get("vault") == vault
                    and rec["source"].get("path") == rel and rec["source"].get("sha256") == digest):
                return public(rec)
        now = time.time()
        rec = {
            "id": uuid.uuid4().hex[:12], "kind": "convert", "origin": origin,
            "source": {"path": rel, "name": absolute.name, "size": absolute.stat().st_size,
                       "sha256": digest},
            "dest_dir": dest_dir, "vault": vault, "text": text, "session": session,
            "media": media, "seconds": seconds,
            "state": "queued", "created": now, "updated": now, "started": None, "finished": None,
            "attempts": 0, "notes": [], "hidden": [], "error": "", "dismissed": False,
        }
        _write(rec)
        if on_end is not None:
            _ON_END[rec["id"]] = on_end
        _enqueue(rec["id"])
    return public(rec)


def annotate(ids: list[str], **fields: Any) -> None:
    """Fill in what the route learned after recording, such as the session a
    turn opened. Through `_update`, so the worker's own write is never lost."""
    for job_id in ids:
        _update(job_id, **fields)


# --- the worker ------------------------------------------------------------------------

def _enqueue(job_id: str) -> None:
    global _PENDING, _WORKER
    _LIVE.add(job_id)
    with _IDLE:
        _PENDING += 1
    _QUEUE.put(job_id)
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_work, name="silica-jobs", daemon=True)
        _WORKER.start()


def _work() -> None:
    global _PENDING
    while True:
        job_id = _QUEUE.get()
        try:
            _run(job_id)
        except Exception:
            # Tolerated: the bookkeeping around one job failed (a full disk
            # under the record, a broken subscriber). The queue must go on for
            # the student's other files; this record keeps its last state, and
            # the next startup shows it interrupted with Retry.
            logger.exception("job %s: bookkeeping failed", job_id)
        finally:
            with _IDLE:
                _PENDING -= 1
                _IDLE.notify_all()


def drain(timeout: float = 30.0) -> bool:
    """Wait until every job this process queued has ended. For tests and for
    a caller that must not race the worker; True when it got there in time."""
    deadline = time.monotonic() + timeout
    with _IDLE:
        while _PENDING:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _IDLE.wait(left)
    return True


def _sentence(exc: Exception) -> str:
    """Why a conversion failed, as the student reads it.

    A sentence written for the student (admission, the PDF reader) goes as it
    is. Any other converter text can carry a host path or an internal URL, so
    on a hosted tenant, where raw errors stay in the log (ADR-0085 point 5),
    it becomes the vault import's sentence for the same case; on a laptop the
    paths in it are the owner's own, and the text is the most useful thing to
    show."""
    from silica.sources.media_admission import MediaRefused
    from silica.sources.pdf_extract import PdfUnreadable

    if isinstance(exc, MediaRefused | PdfUnreadable | Refused):
        return str(exc.args[0]) if exc.args else "it could not be converted"
    if isinstance(exc, ValueError):
        return "it could not be converted" if hosted() else str(exc)
    return "it could not be read"


def _run(job_id: str) -> None:
    from silica.sources.convert import convert

    rec = read(job_id)
    if rec is None or rec.get("state") != "queued":
        # Dismissed, or marked interrupted by another process's startup that
        # shares this ~/.silica: the student decides again, nothing runs here.
        _ON_END.pop(job_id, None)
        return
    rec = _update(job_id, expect=("queued",), state="running", started=time.time(),
                  attempts=int(rec.get("attempts") or 0) + 1)
    if rec is None:
        return
    src = _absolute(rec)
    try:
        if rec.get("vault") != (CONFIG.vault_path or ""):
            raise Refused("the vault changed since this file was added: add it again in this one")
        notes = convert(str(src), dest_dir=rec.get("dest_dir") or "")
        hidden = _hidden(src)
        pages, unread = _pages(src)
    except Exception as exc:
        logger.warning("job %s failed: %s", job_id, type(exc).__name__)
        rec = _update(job_id, state="failed", error=_sentence(exc), finished=time.time(),
                      outcome=getattr(exc, "outcome", "error"), pages=_pages(src)[0])
    else:
        rec = _update(job_id, state="done", notes=notes, hidden=hidden, error="", finished=time.time(),
                      pages=pages, pages_not_read=unread)
    if rec is None:
        return
    _settle_source(rec)
    hook = _ON_END.pop(job_id, None)
    if hook is not None:
        hook(rec)


def _pages(src: Path) -> tuple[dict, str]:
    """ADR-0087's page counts and the sentence naming the pages no model read,
    from the read convert() just did, before the server may delete the PDF;
    nothing for any other file. Read here because the drop's turn and the
    import's row no longer wait for the conversion that knows them."""
    if src.suffix.lower() != ".pdf":
        return {}, ""
    from silica.sources import pdf_extract

    return pdf_extract.report(src)


def _hidden(src: Path) -> list[dict]:
    """The pages of a PDF whose hidden text the reader left out (ADR-0098 H6),
    from the read convert() just did; nothing for any other file."""
    if src.suffix.lower() != ".pdf":
        return []
    from silica.sources import pdf_extract

    seen = pdf_extract.cached(src)
    if not seen or not seen.get("hidden"):
        return []
    return [{"page": int(i) + 1, "characters": n} for i, n in sorted(seen["hidden"].items())]


def _settle_source(rec: dict) -> None:
    """ADR-0098 point 7 on the server: an imported PDF goes once its note is
    written, and an imported file that failed goes too, since nothing refused
    stays in a capped directory. A drop keeps its file in the Inbox, as it
    always has, and so does a laptop."""
    if not hosted() or rec.get("origin") != "import":
        return
    if rec["state"] == "failed" or (rec["state"] == "done" and rec["source"]["name"].lower().endswith(".pdf")):
        _remove_source(rec)


def _remove_source(rec: dict) -> None:
    src = _absolute(rec)
    if src.is_file() and not src.is_symlink():
        src.unlink()


# --- startup, retry, dismiss -----------------------------------------------------------

def recover() -> int:
    """At startup, before serving: a job still queued or running belonged to a
    process that no longer exists, and becomes interrupted. A finished job
    past DONE_KEEP_S goes. Returns how many were interrupted.

    Here and not in a `finally`: SIGTERM skips every one (a tenant is gone
    1.2 s after it, `timeout_graceful_shutdown=1`), and a kill at MemoryMax
    skips everything."""
    stale = time.time() - DONE_KEEP_S
    n = 0
    for rec in _all():
        if rec["id"] in _LIVE:
            continue
        if rec.get("state") in ("queued", "running"):
            if _update(rec["id"], expect=("queued", "running"), state="interrupted",
                       error=INTERRUPTED, finished=time.time()):
                n += 1
        elif rec.get("state") == "done" and (rec.get("finished") or 0) < stale:
            _path(rec["id"]).unlink(missing_ok=True)
    return n


def retry(job_id: str) -> dict:
    """Run a failed or interrupted job again from the Inbox copy. Refused with
    a sentence when that file is gone or its bytes changed: a conversion of
    other bytes under the old job would be a result nobody asked for. A rerun
    writes the same note names (`convert` derives them from the file), so a
    retry after a half-written run replaces, never duplicates."""
    rec = read(job_id)
    if rec is None:
        raise Refused("there is no such conversion")
    if rec.get("state") not in ("failed", "interrupted"):
        raise Refused(f"{rec['source']['name']} is not stopped, so there is nothing to retry")
    if rec.get("vault") != (CONFIG.vault_path or ""):
        raise Refused(f"{rec['source']['name']} was added to another vault: open that one to retry it")
    src = _absolute(rec)
    name = rec["source"]["name"]
    if not src.is_file():
        raise Refused(f"{name} is no longer in the Inbox, so it cannot be converted again: add it again")
    if _sha256(src) != rec["source"].get("sha256"):
        raise Refused(f"{name} changed since it was added, so this conversion would not be of the "
                      "same file: add it again")
    if rec.get("media"):
        from silica.sources import media_admission as admission

        try:
            admission.admit(src)
        except admission.MediaRefused as exc:
            raise Refused(str(exc.args[0])) from None
    with _LOCK:
        cur = read(job_id)
        if cur is None or cur.get("state") not in ("failed", "interrupted"):
            raise Refused(f"{name} is not stopped, so there is nothing to retry")
        cur.update(state="queued", error="", notes=[], hidden=[], finished=None,
                   dismissed=False, updated=time.time())
        _write(cur)
        _enqueue(job_id)
    return public(cur)


def dismiss(job_id: str) -> None:
    """The student is done with a job. A failed or interrupted record goes
    (Open 2: kept until dismissed); a finished one is hidden and goes with the
    30-day prune; a job that has not ended cannot be dismissed."""
    with _LOCK:
        rec = read(job_id)
        if rec is None:
            return
        if rec.get("state") in ("queued", "running"):
            raise Refused(f"{rec['source']['name']} is still converting")
        rec.update(dismissed=True, updated=time.time())
        if rec.get("state") == "done":
            _write(rec)
            return
        _path(job_id).unlink(missing_ok=True)
        _publish(rec)  # the record is gone; every other open tab drops its row too
    if hosted() and rec.get("origin") == "import" and rec.get("vault") == (CONFIG.vault_path or ""):
        _remove_source(rec)  # point 7: an interrupted import's file goes with it


def listing() -> list[dict]:
    """The jobs the student has not dismissed, in this vault, oldest first."""
    vault = CONFIG.vault_path or ""
    return [public(rec) for rec in _all() if not rec.get("dismissed") and rec.get("vault") == vault]


# --- commands typed in the chat ------------------------------------------------------

def from_command(text: str, session: str | None) -> dict | None:
    """`/convert` and `/nucleate <file>` typed in the chat, as jobs: the turn
    that ran them converted inside the request, which the edge cuts at 125 s,
    and a disconnect freed the turn slot while the thread ran on (ADR-0099
    Consequences). None when the command converts nothing, so it runs as a
    turn as before.

    /nucleate turns into jobs only for the files it names that need a
    conversion it has not had; it does not run now, and the jobs offer it.
    ponytail: a folder, a glob or a Zotero key still converts inside the turn,
    because only `_sc_nucleate` expands them. The upgrade is the same split
    after expansion, due when a turn line of kind nucleate passes 125000 ms on
    the pilot (ADR-0100 point 3).
    """
    if not text.strip().startswith("/"):
        return None
    word = text.split(None, 1)[0].lower()
    if word not in ("/convert", "/nucleate"):
        return None
    try:
        parts = shlex.split(text.strip())
    except ValueError:
        return None  # the command's own parser names the unbalanced quote
    from silica.cli import _positional, _rejoin_spaced_paths, _str_flag
    from silica.sources.convert import DOC_EXTS, _resolve_input

    args = _rejoin_spaced_paths(parts[1:])
    files = _positional(args)
    target = _str_flag(args, "--target")
    if not files:
        return None  # usage, or /nucleate's drain of the write-ahead log
    if word == "/nucleate":
        files = _owed(files)
        if not files:
            return None
    jobs: list[dict] = []
    skipped: list[dict] = []
    for f in files:
        name = Path(f).name or f
        if Path(f).suffix.lower() not in DOC_EXTS:
            skipped.append({"name": name, "reason": "Silica has no converter for "
                            + (Path(f).suffix or "a file without an extension")})
            continue
        try:
            src = _resolve_input(f)
        except ValueError:
            skipped.append({"name": name, "reason": "it was not found"})
            continue
        try:
            # No `text`: the offered turn reruns a drop's words, and a slash
            # command composed as an instruction would parse as a command.
            jobs.append(record(str(src), origin="command", session=session, dest_dir=target))
        except ValueError as exc:  # admission's refusal, written for the student
            skipped.append({"name": name, "reason": _sentence(exc)})
    return {"jobs": jobs, "skipped": skipped, "command": word}


def _owed(files: list[str]) -> list[str]:
    """The files of a /nucleate that name one document each and still need a
    conversion: no source claims them, and no earlier conversion of the same
    file sits in the inbox or in done/ (the batch resume `_sc_nucleate` does)."""
    from silica.cli import _prior_conversions
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.sources.convert import DOC_EXTS, _resolve_input
    from silica.sources.registry import adapter_for

    enabled = get_active_manifest().sources
    out: list[str] = []
    prior: dict | None = None
    for f in files:
        if any(ch in f for ch in "*?["):
            return []  # a glob: the whole command runs as before
        try:
            src = _resolve_input(f)
        except ValueError:
            continue  # a Zotero key, or a missing file: the command says which
        if src.is_dir():
            return []  # a folder: the whole command runs as before
        if src.suffix.lower() not in DOC_EXTS or adapter_for(f, enabled=enabled) is not None:
            continue  # a note or code: staged by the command, no conversion
        prior = _prior_conversions() if prior is None else prior
        seen = prior.get(str(src))
        if not (seen and (seen["inbox"] or seen["done"])):
            out.append(f)
    return out


# --- routes ----------------------------------------------------------------------------

def _require_same_origin(request: Request) -> None:
    from silica.ui.web.server import _require_same_origin as check

    check(request)


router = APIRouter(prefix="/jobs")
_SAME_ORIGIN = [Depends(_require_same_origin)]


def _refused(exc: Refused) -> HTTPException:
    return HTTPException(409, str(exc.args[0]))


@router.get("")
def list_jobs():
    """What the browser asks for on load and on every reconnect of the
    narration stream (point 5): an event it missed is read back here."""
    return {"jobs": listing()}


@router.get("/running")
def running_jobs():
    """What a restart would lose (point 8), for the operator's release drain:
    the turn slot and the jobs not yet ended. It reads this process's state
    only and probes nothing remote, unlike GET /settings, which asks four
    endpoints for their models. No file names: the operator sees counts."""
    from silica.ui.web import server

    vault = CONFIG.vault_path or ""
    rows = [{"id": r["id"], "state": r["state"], "kind": r.get("kind", "convert")}
            for r in _all() if r.get("state") in ("queued", "running") and r.get("vault") == vault]
    return {"turn": bool(server._busy), "jobs": rows}


@router.post("/{job_id}/retry", dependencies=_SAME_ORIGIN)
def retry_job(job_id: str):
    try:
        return retry(job_id)
    except Refused as exc:
        raise _refused(exc) from None


@router.post("/{job_id}/dismiss", dependencies=_SAME_ORIGIN)
def dismiss_job(job_id: str):
    try:
        dismiss(job_id)
    except Refused as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.post("/{job_id}/turn", dependencies=_SAME_ORIGIN)
def offered_turn(job_id: str, payload: dict | None = None):
    """The turn a finished job offers (point 6, Open 1): the student starts
    it, the job never does. "Nucleate it" composes NUCLEATE_ASK over the
    job's notes; `ask` reruns the words the student typed
    with the drop, which reached the turn before the file was ready."""
    from silica.ui.web import server

    rec = read(job_id) if _ID.fullmatch(job_id or "") else None
    if rec is None or rec.get("state") != "done" or not rec.get("notes"):
        raise HTTPException(409, "this conversion has no note to offer")
    ask = bool((payload or {}).get("ask")) and bool((rec.get("text") or "").strip())
    if not server._begin_turn():
        raise HTTPException(409, "a turn is already in progress")
    _update(job_id, dismissed=True)
    # "Nucleate it" is the student's choice, so it says so: the empty words
    # of a drop now mean describe-and-ask (server.DROP_ASK).
    words = rec["text"] if ask else server.NUCLEATE_ASK
    return server._turn_response(server._compose_nucleate_turn(words, rec["notes"], []))
