# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One agent turn, transport-neutral: the browser's as a stream of wire dicts, the phone's
answered whole. The turn itself is `silica.session.turn`; this module is the browser's and
the phone's lists of its steps, and the queue that carries the events out. Framing a turn
for HTTP is `sse`'s job, so this module loads no web framework and the bridge (`silica
connect`) can run a turn without one."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from silica.agent.constraints import AgentConstraints, DEMO_MAX_ITERATIONS, PHONE_TOOLS
from silica.config import CONFIG, demo
from silica.kernel import job_records, system_lines, vault_files
from silica.kernel.graph.mindmap import note_resolver
from silica.kernel.text.mentions import image_mark
from silica.obs.events import ToolCompleteEvent
from silica.session import commands, meter, seed
from silica.session import turn as session_turn
from silica.session.turn import Turn, UsageLine
from silica.session.words import command_word
from silica.tools import tool_failed
from silica.ui.web import callback, render, session
from silica.ui.web.failures import explain as _failure
from silica.ui.web.session import SESSION

logger = logging.getLogger(__name__)


def _join_workspace(ws_path: str, ev) -> None:
    """A note this chat wrote joins its workspace's `notes:` (ADR-0112 point
    4); the write to the workspace note lands among the chat's changes."""
    from silica.kernel import workspaces

    if tool_failed(ev.result):
        return
    path = ""
    try:
        out = json.loads(ev.result)
        path = str(out.get("path") or "") if isinstance(out, dict) else ""
    except (TypeError, ValueError):
        pass  # a non-JSON result names no path; the call's own argument does
    path = path or str((ev.args or {}).get("path") or "")
    if not path:
        return
    try:
        ws = workspaces.read_workspace(ws_path)
        if path.removesuffix(".md") not in {k.removesuffix(".md") for k in workspaces.members(ws)}:
            workspaces.add_line(ws_path, "notes", path)
    except Exception:
        # The note is written either way; a workspace note deleted or broken
        # mid-turn only loses the listing, which the drawer then shows absent.
        logger.warning("could not add %s to the workspace %s", path, ws_path, exc_info=True)


def _turn_kind(text: str) -> str:
    """The turn line's kind: "chat", a built-in command's name, or "slash".

    Only names from COMMANDS pass: a skill installed into the vault
    (ADR-0097) is a slash command its user named, and an unknown one is
    whatever the user typed, so both stay "slash" and no free text reaches
    the journal."""
    if not text.startswith("/"):
        return "chat"
    from silica.ui.commands import COMMANDS

    word = command_word(text)
    return word[1:] if any(c.name == word for c in COMMANDS) else "slash"


@dataclass(frozen=True)
class Ask:
    """What one web turn is asked: the words, and what rides them.

    ADR-0121: `withhold` names tools this turn is not offered (a turn that brings
    attached files in is not offered the injector, point 3); `files` and `said`
    ride the user message for the bubble a reload draws (point 4); `prebuilt` is
    a stored user message asked again as it was (POST /turn/redo). `note` is the
    vault path of the note open beside the chat (the composer's chip): it joins
    the named notes and rides the user message as `silica_note`. `story` is a
    Discover story the composer's story chip names (ADR-0137 point 8): its
    articles ride the turn as a fenced block, `scope` says what the answer may
    draw on, and the chip rides the message as `silica_story`.
    """

    text: str
    attachments: "Sequence[Path]" = ()
    workspace: str = ""
    withhold: "Sequence[str]" = ()
    files: "Sequence[dict]" = ()
    said: str = ""
    prebuilt: dict | None = None
    note: str = ""
    story: str = ""
    scope: str = "story"


@dataclass
class _ChatTurn(Turn):
    """What only the browser's turn carries."""

    ask: Ask = field(default_factory=lambda: Ask(""))
    chip: dict | None = None  # the story chip the message stores
    images: list[Path] = field(default_factory=list)


def _settled(t: _ChatTurn) -> None:
    """This chat's conversions that ended since the agent last heard of them: the
    history still says "still converting" (jobs.settled)."""
    from silica.ui.web import jobs

    if t.web is None:
        if block := jobs.settled(t.conv.sid):
            t.add(block)


def _story(t: _ChatTurn) -> None:
    if t.ask.story:
        from silica.sources.discover.story import chat_block

        block, t.chip = chat_block(None, t.ask.story, t.ask.scope)
        t.add(block)


def _image_marks(t: _ChatTurn) -> None:
    """An attached image rides this turn only (ADR-0077 point 3): the transcript,
    the narration and every later turn carry its saved path, which silica_look
    opens again, because the base64 would otherwise be re-sent with every later
    turn and stored with the session."""
    t.images = [] if t.text.startswith("/") else list(t.ask.attachments)
    t.content += "".join(image_mark(p) for p in t.images)


# Off the loop, each of them: they read the driver's graph, and with the ws
# backend installed (silica connect) a driver call on the loop thread deadlocks
# (`_rpc` blocks the very loop that must send the frame).
_BEFORE = (session_turn.name_notes, session_turn.escalate, session_turn.plan, _settled, _story, _image_marks)


def _message(t: _ChatTurn) -> dict:
    ask = t.ask
    msg = session_turn.user_message(t)
    if ask.files:
        msg["silica_files"] = list(ask.files)
    if ask.said:
        msg["silica_said"] = ask.said
    elif t.text.startswith("/") and t.content != t.text:
        # A command's expansion is the model's instructions, not the person's
        # words: a reloaded chat drew /quiz's directive as what they had typed
        # (tour run, 2026-10-29).
        msg["silica_said"] = t.text
    if t.opened:
        msg["silica_note"] = t.opened
    if t.chip:
        msg["silica_story"] = t.chip
    return msg if ask.prebuilt is None else ask.prebuilt


def _figures(t: _ChatTurn) -> None:
    """The figures the turn read go beside the paragraph they illustrate
    (ADR-0082). Here and not in the loop: only this surface draws an embed, and
    the stored message must carry them for a reopened chat."""
    from silica.agent.figures import place_figures

    if not t.stopped:
        t.answer = place_figures(t.answer, t.conv.messages, exists=lambda f: render.find_asset(f) is not None)


def _offer_next(t: _ChatTurn) -> None:
    from silica.ui.commands import COMMANDS

    session_turn.offer_next(t, [c.name.lstrip("/") for c in COMMANDS])


_AFTER = (session_turn.check_numbers, session_turn.attribute, _figures, session_turn.plan_under_answer,
          _offer_next, session_turn.note, session_turn.stamp_reads, session_turn.beat,
          session_turn.recount)


def _answered(messages: list[dict], typed: dict, answer: str, *, narrated: bool = False) -> None:
    """A command's whole exchange, which no model answered."""
    from silica.obs import narration as _narr

    messages.append(typed)
    if not narrated:
        session.narrate_user_turn(typed)
    messages.append({"role": "assistant", "content": answer})
    _narr.NARRATOR.turn(messages[-1])


async def run_turn(ask: Ask) -> AsyncIterator[dict]:
    """One agent turn as a stream of transport-neutral wire dicts.

    Yields the agent's events as they stream, then exactly one terminal dict
    (`callback.done_frame` or `error_frame`). Owns the whole turn lifecycle
    (session append, sync→async queue bridge, cancel token, context compaction,
    save). Both `--gui` (SSE) and `connect` (WS) consume this — the framing is the
    transport's job, not this core's.

    Gate lifecycle: the slot is freed on normal end/error at once; on abandonment
    (the consumer stops iterating — a dropped SSE/WS client) the worker keeps
    running, so we signal cancel and defer the release to the worker's exit, so
    no second turn overlaps a zombie still mutating `messages`.
    """
    messages = SESSION.chat.messages
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    SESSION.cancel = cancel = threading.Event()  # module-level so /stop can see it
    task: asyncio.Future | None = None
    line = UsageLine()
    t = _ChatTurn(SESSION.chat, ask.text, note=ask.note, ask=ask)

    def cb(ev):  # runs in the agent/LLM worker thread
        if isinstance(ev, ToolCompleteEvent) and t.scope is not None and ev.name == "silica_write_note":
            _join_workspace(t.scope["path"], ev)
        line(ev)
        data = callback.event_to_json(ev)
        if data is not None:
            loop.call_soon_threadsafe(q.put_nowait, data)

    # InjectorFSM phase transitions arrive on BUS, not through the agent
    # callback: they are emitted from inside the tool, several layers below the
    # loop that owns `cb`. Subscribed per turn and dropped in the finally, so a
    # turn never receives another turn's phases and nothing accumulates across
    # turns. Publishing happens on the FSM's thread, hence the same
    # call_soon_threadsafe hop `cb` uses.
    def on_phase(ev):
        try:
            cb(ev)
        except RuntimeError:
            pass  # loop already closed: the turn is gone, the event is moot

    from silica.obs.bus import BUS
    BUS.subscribe("work/phase", on_phase)
    # A typed /nucleate runs the injector with no agent call around it, and
    # says so on "work/tool" (ingest.nucleate), so its block draws as the
    # agent's does.
    BUS.subscribe("work/tool", on_phase)

    async def off_loop(fn, *args):
        """`fn` on a thread, held as the turn's task: a dropped tab keeps the gate
        shut until it ends, and only a task the drop did not cancel can say so."""
        nonlocal task
        SESSION.task = task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        return await asyncio.shield(task)

    try:
        # A slash command follows the order `session.commands` names. A persona
        # comes before all of it: it is worn, not run.
        worn = await off_loop(session_turn.wear, t.text, t.conv)
        if worn is not None and not worn[1]:
            _answered(messages, {"role": "user", "content": t.text, "origin": "cli"}, worn[0])
            meter.update_context_tokens(messages)
            line.outcome = "done"
            yield callback.done_frame(worn[0], render.linkify(worn[0], remote_images=False), session.ctx_meter())
            return
        if worn is not None:
            t.text = t.content = worn[1]
        if refusal := session_turn.refused(t.text):
            line.outcome = "error"
            yield callback.error_frame({"error": refusal})
            return
        # /web comes first: it is neither direct nor a workflow expansion but an
        # agent turn with web-only tools. A usage error raises ValueError, which
        # the except below turns into the single error event.
        t.web = session_turn.expand_web_turn(t.text, messages) if t.text.startswith("/") else None
        if t.web is not None:
            t.content = t.web[1]
        elif command_word(t.text) == "/remember" and len(t.text.split(None, 1)) > 1:
            # /remember <text> (ADR-0076 §6) is written here and not by the
            # direct handler below, whose console text would come back wrapped
            # in a code block: the chat draws this as a Saved line with Undo,
            # from the `memory` the done frame carries. Bare /remember goes on
            # to the expansion below, which asks the model.
            from silica.obs import narration as _narr
            from silica.kernel import user_memory
            from silica.session.words import remember_text

            out = user_memory.save("vault", remember_text(t.text), session=_narr.NARRATOR.sid or "")
            answer = user_memory.describe(out)
            _answered(messages, {"role": "user", "content": t.text, "origin": "cli"}, answer)
            line.outcome = "done"
            yield callback.done_frame(answer, render.linkify(answer, remote_images=False), session.ctx_meter(),
                                      memory=user_memory.undo_payload(out))
            return
        elif command_word(t.text) == "/flashcards":
            # Cards are made as data and checked before they are shown
            # (learn.artifacts.flashcards), and the answer is their Markdown:
            # the direct path below would wrap what the command prints in a
            # code block, and the formulas on the cards would not render.
            import shlex

            from silica.kernel.learn.artifacts import flashcards
            from silica.session.words import int_flag, positional, str_flag

            try:
                args = shlex.split(t.text)[1:]
            except ValueError:
                raise ValueError("unbalanced quotes in command. Wrap paths with spaces in \"...\".") from None
            targets = positional(args)
            if not targets:
                raise ValueError("/flashcards needs a note or a folder. Usage: /flashcards <note|folder> [--n=12] [--save=<path>]")
            answer = await off_loop(flashcards, targets, int_flag(args, "--n=", 12), str_flag(args, "--save").strip())
            _answered(messages, {"role": "user", "content": t.text, "origin": "cli"}, answer)
            meter.update_context_tokens(messages)
            line.outcome = "done"
            yield callback.done_frame(answer, await off_loop(render.answer_html, answer, []), session.ctx_meter())
            return
        elif t.text.startswith("/"):
            from silica.term.console import CONSOLE

            def _run_slash():
                # The dispatch prints its result to CONSOLE and can do real work,
                # so it runs under the capture and off the loop thread. The
                # expansion is not a pure string builder: /fetch, /web-search and
                # /convert do the whole job inside it and return "" to say there
                # is nothing left for the agent. Reading that "" as "not
                # available" was reporting failure for work that had already
                # written notes to disk, with the success line going to the
                # server's stdout where no browser user can see it.
                with CONSOLE.capture() as capture:
                    expanded = commands.dispatch(t.text, messages)
                return expanded, capture.get()

            # Pumped as the agent's turn is: a command that runs the injector
            # streams its block and phases while it works (ingest.nucleate),
            # and they reached no one while this awaited the thread whole. Held
            # as the turn's task, so a dropped tab keeps the gate shut until
            # the work ends instead of freeing it under a running import.
            done_slash = object()
            typed = {"role": "user", "content": t.text, "origin": "cli"}
            told: list[bool] = []

            def _turn_before_its_work(ev) -> None:
                # The run projection starts at a user turn, so the injector's
                # beats must follow this one. A command that ran a unit is past
                # the agent fallback (its target is resolved), so the turn
                # narrated here is never narrated again as an expansion.
                if not told:
                    told.append(True)
                    session.narrate_user_turn(typed)

            BUS.subscribe("work/tool", _turn_before_its_work)
            try:
                SESSION.task = task = asyncio.ensure_future(asyncio.to_thread(_run_slash))
                task.add_done_callback(lambda _: q.put_nowait(done_slash))
                while (item := await q.get()) is not done_slash:
                    yield item
                expanded, captured_out = await task
            finally:
                BUS.unsubscribe("work/tool", _turn_before_its_work)

            if expanded == "":
                # Appended only once the verdict is in: a directive falls through
                # to the agent below, which appends the expanded turn itself.
                # A GUI started from a terminal renders CONSOLE in colour and the
                # capture keeps the escape codes, which the chat showed verbatim
                # around /fetch's note path. The browser gets the text alone.
                from rich.text import Text
                shown = Text.from_ansi(captured_out).plain.strip()
                answer = f"```text\n{shown}\n```" if shown else "```text\n(done)\n```"
                _answered(messages, typed, answer, narrated=bool(told))
                line.outcome = "done"
                yield callback.done_frame(answer, render.linkify(answer, note_resolver(), remote_images=False),
                                          session.ctx_meter())
                return

            if expanded is None:
                line.outcome = "error"
                yield callback.error_frame({"error": f"'{t.text}' not available in this session"})
                return
            t.content = expanded

        for step in _BEFORE:
            await off_loop(step, t)
        t.origin = "cli" if t.text.startswith("/") else None
        msg = _message(t)
        t.scope = await off_loop(session.resolve_workspace, ask.workspace)
        await off_loop(session.apply_workspace, t.scope)
        if t.scope is not None:
            msg["silica_workspace"] = t.scope["path"]   # the scope this turn ran in
        messages.append(msg)
        session.narrate_user_turn(msg)
        if msg.get("silica_files"):
            # The bubble's file chips, with what "add to vault" needs (ADR-0121
            # point 4): the page only knew the names it uploaded.
            yield callback.files_frame(await off_loop(files_shown, msg["silica_files"]))
        if t.images:
            from silica.agent import vision
            if vision.separate():
                # A chat model that takes text only gets the pixels as words
                # (ADR-0077 amendment 2026-10-21). They ride this turn only, as
                # the pixels did: the model's part below restores the text.
                msg["content"] = t.content + await off_loop(vision.read_attached, t.images, t.text)
            else:
                from silica.kernel.recall.image_readings import image_part
                msg["content"] = [{"type": "text", "text": t.content}, *map(image_part, t.images)]

        session_turn.watched(t, cb)
        cut = session_turn.constraints(t, ask.withhold, DEMO_MAX_ITERATIONS if demo() else None)

        def model_part():
            try:
                session_turn.ask_model(t, cancel, cut)
            finally:
                if t.images:
                    # Before the steps below, so the context meter and compaction
                    # already see the text form.
                    msg["content"] = t.content

        sentinel = object()
        SESSION.task = task = asyncio.ensure_future(asyncio.to_thread(model_part))
        task.add_done_callback(lambda _: q.put_nowait(sentinel))

        while True:
            item = await q.get()
            if item is sentinel:
                break
            yield item

        assert t.read is not None  # `watched` above
        # Before the await, which re-raises: a turn that failed still read what
        # its results showed, and the narration a reload rebuilds from has them.
        SESSION.chat.seen_numbers.update(t.read.numbers)
        await task  # re-raises if run_agent failed
        # Stop is an outcome, not an answer (audit P3): the loop hands back its
        # sentinel once the token is set, and the text that had streamed stays
        # in the transcript untouched. The frame says who stopped it and the GUI
        # draws that as a row under what arrived, never as the reply. `answer`
        # keeps the sentinel: the WS bridge (silica connect) reads it as the
        # turn's text and has no row to draw.
        t.stopped = SESSION.cancel.is_set()
        # New chat may have replaced the conversation while the turn ran, keeping
        # its message list: what the turn leaves goes to the chat now current.
        t.conv = SESSION.chat
        for step in _AFTER:
            await off_loop(step, t)
        # answer_resolver reads the DRIVER graph — with the ws backend installed
        # (silica connect) a driver call on the loop thread deadlocks (`_rpc`
        # blocks the very loop that must send the frame), so render off-loop.
        html = await off_loop(render.answer_html, t.answer, t.refs)
        line.outcome = "cancelled" if t.stopped else "empty" if t.empty else "done"
        yield callback.done_frame(t.answer, html, session.ctx_meter(), stopped=t.stopped, notes=t.notes, offer=t.offer)
    except Exception as exc:  # never leave the UI stuck on the spinner
        # Off the loop: hosted, a 403 reads the key to name the spent layer.
        failure = await asyncio.to_thread(_failure, exc)
        line.failed(failure)
        if line.limit is None:
            logger.exception("web turn failed")
        yield callback.error_frame(failure)
    finally:
        BUS.unsubscribe("work/phase", on_phase)
        BUS.unsubscribe("work/tool", on_phase)
        line.write(_turn_kind(t.text), "web", scope="workspace" if t.scope is not None else "vault")
        session.prewarm_seed()  # the turn may have written notes — refresh the new-chat seed
        if task is not None and not task.done():
            SESSION.cancel.set()  # abandonment: stop the zombie...
            task.add_done_callback(lambda _: SESSION.release())  # ...free the gate when it exits
        else:
            SESSION.release()  # normal / error / early-return: free now


# The system prompt teaches the write workflows and a phone turn has none of
# their tools, so without this line "save this" reads to the model as a request
# it can meet. It goes before the seed's closing language line, which must stay
# last (session.seed).
_PHONE_LINE = (
    "This conversation is on Telegram, where you can search and read the vault "
    "but not change it: nothing is written, moved or remembered from here. If "
    "asked to, say it has to be done in Silica itself. Telegram shows plain "
    "text, so write without Markdown, tables or LaTeX."
)


@contextmanager
def _narrating_phone():
    """Point the narrator at the phone's session for one turn, then at the
    browser's chat as it stands when the turn ends: New chat may have replaced
    it meanwhile (Session.reset), and reopening the one held at the start
    would append the next chat's turns to the file the student left. No
    browser turn narrates in between, because the gate is the phone's."""
    from silica.obs import narration as _narr

    _narr.NARRATOR.close()
    try:
        fresh = SESSION.phone.sid is None
        # Named before the session beat is written, so narration_sse already
        # knows the phone's beats by their sid and keeps them off the tab.
        SESSION.phone.sid = SESSION.phone.sid or uuid.uuid4().hex[:12]
        _narr.NARRATOR.ensure_session(driver="gui", sid=SESSION.phone.sid)
        if fresh:
            # A title beat before the first turn names the chat in the list,
            # as a rename does (narration._narration_row).
            _narr.NARRATOR.narrate("title", "done", "Telegram", {"title": "Telegram"}, parent=None)
        yield
    finally:
        _narr.NARRATOR.close()
        if SESSION.chat.sid:
            try:
                _narr.NARRATOR.ensure_session(driver="gui", sid=SESSION.chat.sid)
            except _narr.SessionBusy:
                # Another process took the browser's file while the phone held
                # the narrator. Nothing is lost by going on: the browser's next
                # turn meets the same SessionBusy in narrate_user_turn, which
                # is where it is reported.
                logger.warning("narration not restored after a phone turn")


# ADR-0089's reduced turn: no blocks before the model, nothing under the answer.
_PHONE_AFTER = (session_turn.beat, session_turn.recount)


def phone_turn(text: str, cancel: threading.Event) -> dict:
    """One phone turn, whole: `{"answer": ...}`, or `{"error": ...}` with the
    sentence the browser would show, a usage limit's reset included."""
    from silica.obs import narration as _narr

    # The browser reopened the phone's session from the chat list and now
    # writes it, so the phone goes on in a new one rather than beside it. A
    # chat deleted from the list starts over too: its history must not reach
    # the model again, nor its file come back without a title.
    if SESSION.phone.sid is not None and (
            SESSION.phone.sid == SESSION.chat.sid
            or not (_narr.narration_dir() / f"{SESSION.phone.sid}.jsonl").exists()):
        SESSION.reset_phone()
    line = UsageLine()
    try:
        if not SESSION.phone.messages:
            head = seed.seed_messages()
            system_lines.insert(head, {"role": "system", "content": _PHONE_LINE})
            SESSION.phone.messages[:] = head
        t = Turn(SESSION.phone, text, origin="telegram", shown=False, watch=line)
        with _narrating_phone():
            msg = session_turn.user_message(t)
            SESSION.phone.messages.append(msg)
            _narr.NARRATOR.turn(msg)
            session_turn.ask_model(t, cancel, AgentConstraints(tools=PHONE_TOOLS, interactive=True))
            for step in _PHONE_AFTER:
                step(t)
        if cancel.is_set():
            return {"answer": "Stopped."}
        line.outcome = "empty" if t.empty else "done"
        return {"answer": t.answer}
    except Exception as exc:
        failure = _failure(exc)
        line.failed(failure)
        if line.limit is None:
            logger.exception("phone turn failed")
        return {"error": failure["error"]}
    finally:
        line.write("chat", "telegram")


def files_shown(files: Any) -> list[dict]:
    """A user message's files as the bubble draws them (ADR-0121 point 4):
    each with whether the Inbox has it, and a converted file's notes filled
    in from its job when the message went out before they were written."""
    from silica.ui.web import jobs

    out: list[dict] = []
    if not isinstance(files, list):
        return out
    vault = Path(CONFIG.vault_path or ".")
    inbox = vault_files.inbox_rel()
    for f in files:
        if not isinstance(f, dict) or not f.get("source"):
            continue
        f = dict(f)
        if not f.get("notes") and f.get("job"):
            rec = jobs.read(f["job"]) if job_records.is_id(str(f["job"])) else None
            f["notes"] = (rec or {}).get("notes") or []
        try:
            _, rest = vault_files.chat_rel(f["source"])
            # By size too: another file of the same name in the Inbox is not
            # this one, and "add to vault" refuses to overwrite it.
            mine, there = vault / f["source"], vault / inbox / rest
            f["in_vault"] = there.is_file() and mine.is_file() and there.stat().st_size == mine.stat().st_size
        except (vault_files.NotAChatFile, OSError):
            f["in_vault"] = False
        out.append(f)
    return out
