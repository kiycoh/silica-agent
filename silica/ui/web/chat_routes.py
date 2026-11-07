# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The conversation's routes: a turn, the phone's, the transcript, the chat list and the live narration."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from silica.agent import turn_notes
from silica.kernel.text.mentions import IMAGE_MARK, IMAGE_MARK_RE
from silica.tools import tool_failed
from silica.agent.recall_watch import numbers_of_session, read_refs, READS_KEY
from silica.config import CONFIG, demo
from silica.kernel.base.formats import IMAGE_SUFFIX, ext_alternation
from silica.kernel.graph.mindmap import note_resolver
from silica.session import meter
from silica.session import turn as session_turn
from silica.sources import drop_turn
from silica.ui.web import drop, flags, render, session, sse, turn
from silica.ui.web.callback import tool_calls_to_json
from silica.ui.web.failures import Refused
from silica.ui.web.session import SESSION

router = APIRouter()


# NOT GZipMiddleware. It would also wrap /chat, which is text/event-stream, and
# Starlette's gzip responder writes each chunk into a zlib compressor that
# buffers until it has enough to emit — so the SSE frames that make a turn
# stream would be withheld and the transcript would arrive in bursts. /graph
# compresses itself instead, at the one route where the payload is large enough
# to matter (see graph()).

# Images a chat turn may carry, and how large each may be: a phone photo is a
# few MB, and the model sees at most about 1,000 tokens of any of them.
_MAX_ATTACHED = 8


def _save_attachments(images: list) -> list[Path]:
    """Decode the composer's data URLs into `attachments_dir()`, named by content
    so the same picture sent twice is one file. A 400 before the turn starts:
    an attachment the model cannot read must not cost a turn to find out."""
    import base64
    import binascii
    import hashlib

    from silica.kernel.base.durable import atomic_write_bytes
    from silica.kernel.base.home import attachments_dir

    if len(images) > _MAX_ATTACHED:
        raise HTTPException(status_code=400, detail=f"at most {_MAX_ATTACHED} images per message")
    out: list[Path] = []
    for img in images:
        name = str((img or {}).get("name") or "image")
        m = re.match(r"data:(image/[\w.+-]+);base64,(.+)", str((img or {}).get("data") or ""), re.S)
        ext = IMAGE_SUFFIX.get(m.group(1)) if m else None
        if m is None or ext is None:
            raise HTTPException(status_code=400, detail=f"{name}: not an image the chat model reads "
                                                        "(png, jpg, webp, gif, bmp)")
        try:
            data = base64.b64decode(m.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail=f"{name}: the image data is damaged") from None
        if len(data) > drop.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail=f"{name}: larger than {drop.MAX_IMAGE_BYTES // 2**20} MB")
        path = attachments_dir() / f"{hashlib.sha256(data).hexdigest()[:16]}{ext}"
        if not path.exists():
            atomic_write_bytes(path, data)
        out.append(path)
    return out


_ATTACHMENT_NAME_RE = re.compile(rf"[0-9a-f]{{16}}\.{ext_alternation(IMAGE_SUFFIX.values())}")


def _attached_images(content: str) -> tuple[str, list[dict]]:
    """A reloaded user turn as its words plus its images: the history keeps the
    saved path for the model (silica_look opens it), the person sees the picture
    they sent, as the live bubble showed it."""
    images = [{"src": f"/attachment/{Path(p).name}", "name": Path(p).name}
              for p in IMAGE_MARK_RE.findall(content) if _ATTACHMENT_NAME_RE.fullmatch(Path(p).name)]
    return IMAGE_MARK_RE.sub("", content).rstrip(), images


@router.get("/attachment/{name}")
def attachment(name: str):
    """An image attached to a chat turn. The name is the content hash the server
    gave it, so nothing else under ~/.silica can be asked for by name."""
    from silica.kernel.base.home import attachments_dir
    path = attachments_dir() / name
    if not _ATTACHMENT_NAME_RE.fullmatch(name) or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, headers=render.ASSET_HEADERS)


def _demo_refuses(payload: dict) -> str:
    """Why a demo refuses this message, or "" (ADR-0139 points 5 and 6). Asked
    before the turn starts: a 404 is a refusal the door counts no question for,
    where the stream's own error frame for a withheld command arrives in a 200."""
    if not demo():
        return ""
    if payload.get("images"):
        return "images are not part of the demo"
    if payload.get("story"):
        return "Discover is not part of the demo"
    word = session_turn.withheld_command(str(payload.get("text") or "")).lower()
    return f"'{word}' is not part of the demo" if word else ""


@router.post("/chat")
async def chat(payload: dict):
    if refused := _demo_refuses(payload):
        raise HTTPException(status_code=404, detail=refused)
    workspace = str(payload.get("workspace") or "")
    if workspace:
        from silica.kernel.workspaces import find_workspace

        # A chip naming a workspace renamed or deleted since must not run the
        # turn on the whole vault under that chip.
        if await asyncio.to_thread(find_workspace, workspace) is None:
            raise HTTPException(status_code=404, detail="that workspace no longer exists")
    attachments = _save_attachments(payload.get("images") or [])
    # The note open beside the chat (the composer's chip), as a vault path;
    # named_notes keeps it only if the vault has it.
    note = str(payload.get("note") or "")
    # A story from Discover (ADR-0137 point 8): its id and where the answer
    # comes from. Web makes it a /web turn, the one whose tools read pages.
    story = str(payload.get("story") or "")
    scope = str(payload.get("scope") or "story")
    with session.turn_slot() as slot:
        text = payload.get("text", "")
        if story and scope == "web" and text.strip() and not text.startswith("/"):
            text = "/web " + text
        # /convert and /nucleate <file> typed here convert as jobs and answer at
        # once (ADR-0099 point 1), with the 202 a drop of files alone gets.
        from silica.ui.web import jobs

        queued = await asyncio.to_thread(jobs.from_command, text, SESSION.chat.sid)
        if queued is not None:
            return JSONResponse(queued, status_code=202)
        if SESSION.chat.sid and text.strip() and not text.startswith("/"):
            # A file read after its drop got no turn joins this message (ADR-0121
            # point 4): otherwise "this paper" names nothing the chat was given.
            joined = await asyncio.to_thread(jobs.join_next, SESSION.chat.sid)
            if joined:
                notes, files, earlier = joined
                composed = await asyncio.to_thread(drop_turn.compose_nucleate_turn, text, notes, [], earlier=earlier)
                return sse.turn_response(slot, turn.Ask(composed, attachments, workspace,
                                                        withhold=drop_turn.WITHHELD_WITH_FILES, files=files, said=text,
                                                        note=note, story=story, scope=scope))
        return sse.turn_response(slot, turn.Ask(text, attachments, workspace, note=note, story=story, scope=scope,
                                                said=text.removeprefix("/web ") if story and scope == "web" else ""))


@router.post("/phone/turn")
async def phone_turn(payload: dict):
    """One turn from the phone, answered once and whole: a Telegram message is
    sent complete, so the caller has no use for a stream (ADR-0089 amendment,
    H5). 409 while any turn runs, the browser's included."""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is empty")
    with session.turn_slot():
        SESSION.phone_running = True
        # The browser's Stop sets this token, so it ends a phone turn too.
        SESSION.cancel = cancel = threading.Event()
        # Named as the gate's task, so a /chat stream closing in the same instant
        # (sweep_if_orphaned) does not take this turn for an orphan and free it.
        SESSION.task = task = asyncio.ensure_future(asyncio.to_thread(turn.phone_turn, text, cancel))
        try:
            return await task
        finally:
            SESSION.phone_running = False


@router.post("/phone/new")
async def phone_new():
    """`/new` from the phone: its next turn opens a fresh conversation."""
    if SESSION.busy:
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    SESSION.reset_phone()
    return {"ok": True}


@router.get("/commands")
def list_commands():
    """Commands the web GUI's fuzzy picker offers — everything the chat turn can
    actually dispatch. `repl_only` ones are terminal-session business and would
    only answer 'not available in this session' if offered here."""
    from silica.config import withheld
    from silica.ui.commands import commands

    gone = withheld("commands")
    return [
        {"name": c.name, "summary": c.summary, "usage": c.usage}
        for c in commands()
        if not c.repl_only and c.name not in gone
    ]


def _edited(content: str, words: str, note: str = "") -> str:
    """A stored user message with its typed words replaced (POST /turn/redo):
    the manifest and the images stay, the named notes follow the new words,
    and the note that was open beside the chat stays the one the turn reads."""
    from silica.kernel.text.mentions import named_notes, strip

    base = strip(content)
    cuts = [i for i in (base.find(drop_turn.MANIFEST_MARK), base.find(f"\n\n{IMAGE_MARK}")) if i >= 0]
    rest = base[min(cuts):] if cuts else ""
    block = named_notes(words, None, note)
    if block:
        at = rest.find(f"\n\n{IMAGE_MARK}")
        rest = rest + "\n\n" + block if at < 0 else rest[:at] + "\n\n" + block + rest[at:]
    return words + rest


@router.post("/turn/redo")
async def redo_turn(payload: dict | None = None):
    """Ask the last question again (ADR-0121 point 7): regenerate, or with
    `text`, edit it. The last user message and everything after it leave the
    conversation, here and in the narration (a `chat/rewind` beat that replay
    folds the same way), and the message runs again as it was stored, with its
    files and its scope.
    ponytail: the last turn only; an earlier question is a rewind by ordinal,
    due when a student asks to edit one."""
    from silica.obs import narration as _narr

    words = (payload or {}).get("text")
    with session.turn_slot() as slot:
        at = max((k for k, m in enumerate(SESSION.chat.messages) if m.get("role") == "user"), default=-1)
        if at < 0:
            raise HTTPException(409, "there is no question to ask again")
        msg = dict(SESSION.chat.messages[at])
        content: Any = msg.get("content")
        if isinstance(content, list):   # an image turn cut short: its text part
            content = next((p.get("text") for p in content if p.get("type") == "text"), "")
        if not isinstance(content, str) or content.startswith("/"):
            # A direct command answered without the model has nothing to redo.
            raise HTTPException(409, "a command runs again by typing it")
        if words is not None:
            words = str(words).strip()
            if not words:
                raise HTTPException(400, "an edited question needs words")
            content = await asyncio.to_thread(_edited, content, words, msg.get("silica_note") or "")
            if "silica_said" in msg or msg.get("silica_files"):
                msg["silica_said"] = words
        msg["content"] = content
        del SESSION.chat.messages[at:]
        SESSION.chat.collapsed = {c for c in SESSION.chat.collapsed if c < at}
        _narr.NARRATOR.narrate(_narr.REWIND, "done", "asked again", {}, parent=None)
        withhold = drop_turn.WITHHELD_WITH_FILES if msg.get("silica_files") and not content.startswith(drop_turn.NUCLEATE_ASK) else ()
        return sse.turn_response(slot, turn.Ask(content, workspace=msg.get("silica_workspace") or "", withhold=withhold,
                                                prebuilt=msg))


@router.get("/messages")
def get_messages():
    messages = SESSION.chat.messages
    resolve = note_resolver()
    # A call whose result carries an error must not replay as a tick: the one
    # place the user checks whether a write landed is this transcript.
    failed = {
        m["tool_call_id"] for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") and tool_failed(m.get("content"))
    }
    # Tool results, which the loop below skips over: a nucleate run's outcome
    # (notes, links, which chunks died and where) exists nowhere else, so
    # without this a reloaded chat could only say the injector had run.
    results = {
        m["tool_call_id"]: m.get("content") or ""
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    # Each user row names its turn for the flag (ADR-0100 point 6), hosted
    # only. The seed is all system messages, so the session's user messages
    # and its user beats pair one to one; when they do not (a turn narrated
    # nowhere because another process held the session), no row is named,
    # because a flag pointing at the neighbouring turn is worse than none.
    from silica.config import hosted

    turn_of: dict[int, dict] = {}
    sid = flags.plain_sid(SESSION.chat.sid) if hosted() and not demo() else ""
    if sid:
        seqs = flags.user_turn_seqs(flags.session_beats(sid))
        users = [m for m in messages if m.get("role") == "user"]
        if len(users) == len(seqs):
            done = flags.flagged(sid)
            turn_of = {id(m): {"session": sid, "seq": s, "flagged": s in done}
                       for m, s in zip(users, seqs)}
    from silica.kernel.graph.mindmap import _driver_graph

    graph = _driver_graph()   # once for every answer below, as `resolve` is for the rest
    data = []
    for at, m in enumerate(messages):
        if m.get("role") not in ("user", "assistant"):
            continue
        tools = tool_calls_to_json(m, failed, results) if m["role"] == "assistant" else []
        content = m.get("content") or ""
        # The thinking that produced this step, kept out of the wire by _to_wire.
        # Plain text, not rendered: it is a trace, and the live block shows it raw.
        thinking = m.get("silica_reasoning") or "" if m["role"] == "assistant" else ""
        if not content and not tools and not thinking:
            continue
        images: list[dict] = []
        if m["role"] == "user" and isinstance(content, str) and (
                "<named-notes>" in content or "<converted-files>" in content or "<discover-story>" in content):
            from silica.kernel.text.mentions import strip

            content = strip(content)
        if m["role"] == "user" and isinstance(content, str) and IMAGE_MARK in content:
            content, images = _attached_images(content)
        files: list[dict] = []
        if m["role"] == "user":
            # The words the student typed, never the manifest the model reads
            # (ADR-0121 point 4); a chat saved before that cuts at the same mark.
            if isinstance(m.get("silica_said"), str):
                content = m["silica_said"]
            elif isinstance(content, str) and drop_turn.MANIFEST_MARK in content:
                content = content.split(drop_turn.MANIFEST_MARK, 1)[0]
                content = "" if content == drop_turn.DROP_ASK else content
            files = turn.files_shown(m.get("silica_files"))
        data.append({"role": m["role"], "content": content, "tools": tools, "files": files,
                     "note": m.get("silica_note") or "",
                     "story": m.get("silica_story") or None,
                     "thinking": thinking, "images": images,
                     "notes": turn_notes.of(m) if m["role"] == "assistant" else [],
                     "html": "" if not content else
                     render.answer_html(content, m.get(READS_KEY) or read_refs(messages, at + 1), graph)
                     if m["role"] == "assistant" and not m.get("tool_calls") else
                     render.linkify(content, resolve, remote_images=False)})
        if id(m) in turn_of:
            data[-1]["turn"] = turn_of[id(m)]
    # Vault label + context usage ride headers so the body stays a plain list.
    # The breakdown goes as JSON in one header rather than three: it is one
    # reading of one number, and splitting it invites a client to read two of
    # the three and draw a ring that does not close.
    meter = session.ctx_meter()
    return JSONResponse(data, headers={
        "X-Silica-Vault": CONFIG.vault_path or "",
        "X-Silica-Context-Tokens": str(meter["context_tokens"]),
        "X-Silica-Max-Context-Tokens": str(meter["max_context_tokens"]),
        "X-Silica-Context-Parts": json.dumps(meter["context_parts"]),
        "X-Silica-Compact-At": str(meter["compact_at"]),
    })


@router.get("/narration")
def narration_replay(from_seq: int = 0, sid: str = ""):
    """Replay from a cursor — record zero by default: a joining client that
    replays everything cannot see a span whose opening it missed (ticket 08)."""
    from silica.obs import narration as _narr
    target = sid or SESSION.chat.sid
    if not target or not str(target).isalnum():
        return JSONResponse({"sid": None, "beats": []})
    path = _narr.narration_dir() / f"{target}.jsonl"
    return JSONResponse({"sid": target,
                         "beats": list(_narr.read_beats(path, from_seq=from_seq))})


def _beat_for_tab(rec: dict) -> bool:
    """A phone turn's beats stay off the browser's live stream (ADR-0089
    amendment): the work panel adopts any new sid and would drop the browser's
    run, and their seqs would move its Last-Event-ID cursor. Unless the browser
    itself has the phone's chat open."""
    return not (SESSION.phone.sid and rec.get("sid") == SESSION.phone.sid and SESSION.phone.sid != SESSION.chat.sid)


@router.get("/narration/sse")
async def narration_sse(request: Request, from_seq: int = 0):
    """Live beats. The SSE `id:` field is the seq, so the browser's own
    Last-Event-ID header is the reconnect cursor — no custom client state."""
    from silica.obs import narration as _narr
    from silica.obs.bus import BUS

    last = request.headers.get("last-event-id")
    cursor = int(last) if last and last.isdigit() else from_seq
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_beat(rec: dict) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, rec)
        except RuntimeError:
            pass   # loop closed: the client is gone, the beat is moot

    BUS.subscribe(_narr.BEAT_TOPIC, on_beat)

    # A job's transitions ride the same stream (ADR-0099 point 5), as their
    # own event with no `id:`, so the browser's Last-Event-ID stays the beats'.
    from silica.ui.web import jobs

    def on_job(rec: dict) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, ("job", rec))
        except RuntimeError:
            pass   # loop closed: the client is gone, and GET /jobs answers its next page

    BUS.subscribe(jobs.TOPIC, on_job)

    async def gen():
        try:
            sid = SESSION.chat.sid
            if sid:
                path = _narr.narration_dir() / f"{sid}.jsonl"
                for rec in _narr.read_beats(path, from_seq=cursor):
                    yield (f"event: beats\nid: {rec['seq']}\n"
                           f"data: {json.dumps({'sid': sid, 'beats': [rec]}, default=str)}\n\n")
            while True:
                # Not a plain `await q.get()`: this body never returns on its
                # own, and uvicorn's shutdown waits for every open response, so
                # one live tab held Ctrl+C forever (measured 2026-08-23: 0.5s to
                # exit with no stream, never with one). The poll lets the stream
                # end itself while the server is still waiting politely, which
                # is what keeps the exit free of the 40-line ASGI traceback that
                # a cancelled connection task prints.
                try:
                    rec = await asyncio.wait_for(q.get(), sse.POLL_S)
                except asyncio.TimeoutError:
                    if session.stopping():
                        return
                    continue
                if isinstance(rec, tuple):
                    yield f"event: job\ndata: {json.dumps(rec[1], default=str)}\n\n"
                    continue
                if not _beat_for_tab(rec):
                    continue
                yield (f"event: beats\nid: {rec.get('seq')}\n"
                       f"data: {json.dumps({'sid': rec.get('sid'), 'beats': [rec]}, default=str)}\n\n")
        finally:
            BUS.unsubscribe(_narr.BEAT_TOPIC, on_beat)
            BUS.unsubscribe(jobs.TOPIC, on_job)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.get("/sessions")
def list_sessions():
    # Current id rides a header so the body stays a plain list (matches /messages).
    # `live` marks the one chat a turn is running in, so the list can show a
    # working dot on it from any other tab (audit P18).
    rows = [{**r, "live": bool(SESSION.busy and r.get("id") == SESSION.chat.sid)}
            for r in session.list_sessions()]
    return JSONResponse(rows, headers={"X-Silica-Session": SESSION.chat.sid or ""})


@router.post("/session/load")
def load_session(payload: dict):
    if SESSION.busy:
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    from silica.obs import narration as _narr

    sid = str(payload.get("id", ""))
    if not sid.isalnum():  # ids are hex — blocks path traversal
        raise HTTPException(status_code=404, detail="no such session")
    replayed = _narr.load_session_messages(sid, CONFIG.vault_path or "")
    if replayed is None:
        raise HTTPException(status_code=404, detail="no such session")
    if (_narr.narration_dir() / f"{sid}.jsonl").exists():
        try:
            _narr.NARRATOR.resume(sid)   # continue appending to the same account
        except _narr.SessionBusy as e:
            raise Refused(409, e) from None
        SESSION.chat.sid = sid
    else:
        # Legacy snapshot: continue as a NEW narration session seeded with its
        # turns — emit new, recognise legacy (ticket 05).
        _narr.NARRATOR.close()
        SESSION.chat.sid = _narr.NARRATOR.ensure_session(driver="gui")
        for m in replayed:
            _narr.NARRATOR.turn(m)
    # The narration records turns, never the seed, so a reopened chat used to
    # run with no system prompt, vault scope or memory at all. The TUI's
    # /resume already put a fresh seed in front; this is the same.
    SESSION.chat.messages[:] = session.seeded()[0] + replayed
    SESSION.chat.collapsed = set()
    # The scope its last turn ran in (ADR-0112 point 4): the seed holds no
    # workspace line, so it is written again here, as the seed is.
    last: dict = next((m for m in reversed(replayed) if m.get("role") == "user"), {})
    ws = session.resolve_workspace(str(last.get("silica_workspace") or ""))
    if ws is not None:
        session.apply_workspace(ws)
    # Its persona too (ADR-0127 point 3), read from the file as it is now; one
    # removed since leaves the chat without it rather than failing to open.
    from silica.kernel import personas

    if (worn := personas.available(CONFIG.vault_path or None).get(str(last.get(personas.TURN_KEY) or ""))):
        personas.apply(SESSION.chat.messages, worn)
    SESSION.chat.seen_numbers.clear()
    SESSION.chat.seen_numbers.update(numbers_of_session(SESSION.chat.sid))
    meter.update_context_tokens(SESSION.chat.messages)
    return {"ok": True}


@router.post("/session/rename")
def rename_session(payload: dict):
    """The chat list's rename (audit P18): a title beat, never a rewrite."""
    from silica.obs import narration as _narr

    sid = str(payload.get("id", ""))
    title = str(payload.get("title", "")).strip()[:120]
    if not sid.isalnum() or not title:
        raise HTTPException(status_code=400, detail="a session id and a title are needed")
    try:
        ok = _narr.rename_session(sid, title, CONFIG.vault_path or "")
    except _narr.SessionBusy as e:
        raise Refused(409, e) from None
    if not ok:
        raise HTTPException(status_code=404, detail="no such session")
    return {"ok": True, "title": title}


@router.post("/session/delete")
def delete_session(payload: dict):
    """Delete one chat. The open one is closed first and the tab lands on a
    fresh session, so the record and the conversation go together."""
    from silica.obs import narration as _narr

    sid = str(payload.get("id", ""))
    if not sid.isalnum():
        raise HTTPException(status_code=404, detail="no such session")
    if sid == SESSION.chat.sid:
        if SESSION.busy:
            raise HTTPException(status_code=409, detail="a turn is running in this chat")
        SESSION.reset()  # releases the flock; the delete below takes it
    try:
        ok = _narr.delete_session(sid, CONFIG.vault_path or "")
    except _narr.SessionBusy as e:
        raise Refused(409, e) from None
    if not ok:
        raise HTTPException(status_code=404, detail="no such session")
    # The chat's attached files go with it (ADR-0121 point 5); a copy the
    # student added to the vault stays, being the vault's now.
    import shutil

    from silica.kernel.base.layout import chat_folder

    # Tolerated: a chat that never had a file has no folder, and a file the
    # system will not remove stays behind a deletion that already happened
    # (the narration is gone); it is hidden, and no walk lists it.
    shutil.rmtree(Path(CONFIG.vault_path or ".") / chat_folder(sid), ignore_errors=True)
    return {"ok": True}


# --- flagged answers (ADR-0100 point 6) ---------------------------------------
# A hosted student marks one answer as wrong, and that consent is the only way
# the owner reads a turn for research. What is stored is a pointer, never a
# copy: the session id and the narration seq of the user beat that opened the
# turn. Deleting the chat therefore withdraws the flag (the pointer leads
# nowhere, and `bin/flags prune` drops it), and the owner reads the turn where
# it already lives. The seq and not an ordinal of bubbles: the page draws a
# bubble for a send the server never stored (an unknown command, /find, a
# retry), so any count the browser keeps drifts from the file.

_FLAG_NOTE_MAX = 300


@router.post("/flag")
def flag_answer(payload: dict):
    """Append one flag to `<home>/flags.jsonl`: time, session, the seq of the
    user beat that opened the turn, and the student's own line."""
    from silica.config import hosted
    from silica.config import silica_home

    if not hosted():
        raise HTTPException(status_code=404, detail="Not Found")
    sid = flags.plain_sid(payload.get("session"))
    if not sid:
        raise HTTPException(status_code=400, detail="that is not a chat id")
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise HTTPException(status_code=400, detail="that is not an answer id")
    note = str(payload.get("note") or "").strip()
    if len(note) > _FLAG_NOTE_MAX:
        raise HTTPException(status_code=400, detail=f"keep the note under {_FLAG_NOTE_MAX} characters")
    if seq not in flags.user_turn_seqs(flags.session_beats(sid)):
        raise HTTPException(status_code=404, detail="that answer is not in this chat")
    if seq in flags.flagged(sid):
        return {"ok": True, "already": True}  # one flag per answer; a second click is not news
    path = silica_home() / "flags.jsonl"
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "session": sid,
           "answer": seq, "note": note}
    path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    with open(path, "a", encoding="utf-8") as fh:
        # The host's nightly `bin/flags prune` rewrites this file in place under
        # the same lock; without it a flag sent in that instant could be lost.
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True}


@router.post("/reset")
def reset():

    session.capture_own_session()
    SESSION.reset()
    return {"ok": True, "vault": CONFIG.vault_path}


@router.post("/stop")
def stop():
    if SESSION.cancel is not None:
        SESSION.cancel.set()
        from silica.obs import narration as _narr
        _narr.NARRATOR.cancel(driver="gui", target=None, scope="turn")
    return {"ok": True}
