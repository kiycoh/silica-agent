# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""FastAPI backend for the localhost GUI.

Single in-memory session (localhost, one user, no auth). The critical seam is
sync `run_agent` (blocking) -> async SSE: run it in a worker thread and bridge
its callback events onto the event loop with `call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import html as _html
import itertools
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from silica.agent.constraints import (
    DEMO_MAX_ITERATIONS,
    PHONE_TOOLS,
    AgentConstraints,
    chat_deferred,
    chat_tools,
    web_turn_constraints,
)
from silica.obs.events import LLMStreamEvent, ToolCompleteEvent, ToolErrorEvent, ToolStartEvent
from silica.model.llm import meter_totals
from silica.agent.loop import _EMPTY_COMPLETION, _is_tool_failure, run_agent
from silica.agent import turn_notes
from silica.agent.recall_watch import (
    READS_KEY,
    RecallWatch,
    numbers_notes,
    numbers_of_session,
    read_refs,
    unfounded_in_turn,
)
from silica.config import CONFIG, demo
from silica.kernel.graph.mindmap import note_resolver
from silica.metrics import emit
from silica.kernel.write import session_changes
from silica.sources.web_research import WebTurn
from silica.ui.web.callback import event_to_json, tool_calls_to_json
from silica.ui.web import security
from silica.ui.web.failures import explain as _failure
from silica.kernel.text import note_parts
from silica.ui.web import render
from silica.ui.web.session import SESSION
from silica.ui.web import session
from silica.kernel import vault_files
from silica.sources import drop_turn
from silica.kernel.recall import note_index
from silica.kernel.report import vault_shape
from silica.kernel.report import note_context
from silica.kernel.report import brief

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _prewarm_learn() -> None:
    """Learn's vault-wide reads before the first click: the notes, their
    parsed frontmatter and the prerequisite map were 8 s of a plan page's
    first open after a restart (2,166 notes, 2026-10-23). A failure costs only
    that wait, which the click then pays."""
    try:
        from silica.kernel.learn import plans
        from silica.kernel.learn import learner

        learner._notes_meta(plans._vault())
        plans.list_plans()
        learner.prerequisites_map()
    except Exception:
        logger.exception("learn prewarm failed")


def _scoped_run(ws: dict | None, fn, *args, **kwargs):
    """Run the agent with retrieval narrowed to `ws` (ADR-0112 point 4). Set
    and reset on the worker thread itself: `to_thread` copies the context, and
    a token must be reset in the context that set it."""
    from silica.kernel import workspaces

    token = workspaces.open_scope(ws) if ws else None
    try:
        return fn(*args, **kwargs)
    finally:
        if token is not None:
            workspaces.close_scope(token)


def _join_workspace(ws_path: str, ev) -> None:
    """A note this chat wrote joins its workspace's `notes:` (ADR-0112 point
    4); the write to the workspace note lands among the chat's changes."""
    from silica.kernel import workspaces

    if _is_tool_failure(ev.result):
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


def _skill_after_turn(text: str, messages: list[dict]) -> tuple[dict | None, dict | None]:
    """(turn note, offer) for the turn that just ended (ADR-0127 points 5, 6):
    a proposal written from a request the person made in enough chats, or
    else the installed skill that answers it, offered as the next turn. An
    incognito chat proposes nothing: it asked to leave no trace."""
    from silica.obs import narration as _narr
    from silica.kernel import proposed_skills as ps
    from silica.kernel import user_memory

    from silica.kernel.learn.offer import says_lost

    vault = CONFIG.vault_path or ""
    # "non ho capito la Lezione 14" said in three chats is a reader stuck, not
    # a request to make reusable: it became the skill /non-ho-capito
    # (2026-10-23). The study offer answers that message. A demo neither
    # drafts one (a worker call no question counts) nor offers one it may
    # refuse, /explain among them (ADR-0139 point 6).
    if not vault or demo() or user_memory.incognito() or says_lost(text):
        return None, None
    try:
        evidence = ps.detect(text, messages, vault, _narr.NARRATOR.sid)
        if evidence is not None:
            drafted = ps.draft(evidence["asks"], CONFIG)
            name = ps.stage(vault, drafted, evidence, CONFIG.worker_model or CONFIG.model or "") if drafted else None
            if name:
                return {"kind": "skill", "text": f"/{name} written from a request you made in "
                                                 f"{len(evidence['sessions'])} chats · review it in "
                                                 "Settings › Skills"}, None
        return None, ps.offer(text, vault)
    except Exception:
        # A proposal is an extra: a chat file that does not parse or a worker
        # that is down costs the person the proposal, never the answer.
        logger.warning("skill proposal skipped", exc_info=True)
        return None, None


def _study_offer(text: str, messages: list[dict], plan_draft: dict | None = None) -> dict | None:
    """The study offer for the turn that just ended, from the notes it read
    (learn.offer). An incognito chat gets none, as it gets no proposal, and
    a demo none: its commands are Learn's and the demo's refused ones.
    `plan_draft` is the plan this turn computed (_plan_context), offered for
    saving."""
    from silica.config import withheld
    from silica.kernel import user_memory
    from silica.kernel.learn.offer import study_offer

    if demo() or user_memory.incognito():
        return None
    try:
        from silica.kernel.learn.offer import asks_plan, plan_offer

        if (plan_draft or asks_plan(text)) and "/learn" not in withheld("routes"):
            # A plan asked of the chat: the one it computed, or the one Learn tracks.
            from silica.kernel.learn.plans import list_plans

            return plan_offer(text, list_plans(), plan_draft)
        # The turn names its reads as the calls did, a title or a path; the
        # ledger keys notes by path, so the titles are resolved first.
        from silica.kernel.learn.offer import says_lost
        from silica.kernel.graph.mindmap import _driver_graph, answer_resolver

        graph = _driver_graph()
        _resolve, read = answer_resolver(read_refs(messages), graph=graph)
        if says_lost(text):
            # The note the reader named is the one the offer is about: a turn
            # that read nothing (the local 9B answered "punti di supporto" from
            # memory), or one whose first read was a neighbour ("Limite
            # d'errore con slack" for "variabili slack di margine", 2026-10-23).
            low = text.casefold()
            named = [(t, p) for p, t in graph[1].items()
                     if len(t) >= 6 and t.casefold() in low and "/sources/" not in f"/{p}"]
            read = [p for _t, p in sorted(named, key=lambda tp: -len(tp[0]))[:1]] + list(read)
        from silica.kernel.learn.offer import allowed, log_offer, mode

        if mode() == "off":
            return None
        offer = study_offer(text, [p if p.endswith(".md") else p + ".md" for p in read])
        if offer is None:
            return None
        # Two offers a chat, none on a note whose offer went unused in the last
        # week, and in shadow mode only the record (learn.offer, research rule).
        chat = SESSION.chat.sid or ""
        show = mode() == "on" and allowed(chat, offer["note"])
        log_offer(chat, offer, shown=show)
        return offer if show else None
    except Exception:
        # An offer is an extra: a ledger that does not parse costs the offer,
        # never the answer.
        logger.warning("study offer skipped", exc_info=True)
        return None


def _escalation(text: str, messages: list[dict]) -> str:
    """The escalation block for this turn, or "": the note the reader names,
    stuck on it by the ledger, by an unused offer or by saying so twice. Logs
    an offer acted on first. Nothing in an incognito chat."""
    from silica.kernel import user_memory
    from silica.kernel.learn import offer as learn_offer

    if user_memory.incognito() or learn_offer.mode() == "off":
        return ""
    try:
        chat = SESSION.chat.sid or ""
        learn_offer.log_acted(chat, text)
        from silica.kernel.graph.mindmap import _driver_graph
        from silica.kernel.learn.quiz import entries

        low = (text or "").casefold()
        named = sorted(((t, p) for p, t in _driver_graph()[1].items()
                        if len(t) >= 6 and t.casefold() in low and "/sources/" not in f"/{p}"),
                       key=lambda tp: -len(tp[0]))
        if not named:
            return ""
        name, path = named[0]
        note = path if path.endswith(".md") else path + ".md"
        history = [str(m.get("content") or "") for m in messages if m.get("role") == "user"][-3:]
        why = learn_offer.escalation(note, chat, text, history, entries())
        if not why:
            return ""
        # in shadow mode the record only: what would have fired, nothing shown;
        # a shadowed reason the same in any mode (ADR-0135 point 9)
        shown = learn_offer.mode() == "on" and not learn_offer.shadowed(why)
        learn_offer._append({"chat": chat, "note": note, "escalated": True, "why": why, "displayed": shown})
        if not shown:
            return ""
        from silica.kernel.learn.items import read_note

        return learn_offer.escalation_block(name, why, read_note(note))
    except Exception:
        # An escalation is an extra: a ledger that does not parse costs it,
        # never the turn.
        logger.warning("study escalation skipped", exc_info=True)
        return ""


def _plan_context(text: str, messages: list[dict]) -> tuple[str, str, dict | None]:
    """Learn's plan for this message (learn.offer.plan_context), or ("", "",
    None): the block for the model, the plan or calendar appended under its
    answer, and the plan to save when this turn computed one. `messages` is
    the chat before this turn, whose last exchange may be the question for
    the material that this message answers. An incognito chat gets none, as
    it gets no offer, and an account Memorize is not served to none: the
    plan it would offer to save lands on a route that answers 404."""
    from silica.config import withheld
    from silica.kernel import user_memory

    if "/learn" in withheld("routes") or user_memory.incognito() or not text or text.startswith("/"):
        return "", "", None
    try:
        from silica.kernel.learn.offer import plan_context
        from silica.kernel.learn.plans import list_plans

        prev_user, prev_answer = "", ""
        for m in reversed(messages):
            if m.get("role") == "assistant" and not prev_answer and not prev_user:
                prev_answer = str(m.get("content") or "")
            elif m.get("role") == "user":
                # the person's words: the blocks this server appended to the
                # turn start at a blank line and a bracket, and the question
                # for the material said "study plan" itself
                prev_user = str(m.get("silica_said") or m.get("content") or "").split("\n\n[", 1)[0]
                break
        return plan_context(text, list_plans(), prev_user, prev_answer)
    except Exception:
        # The plan's numbers are an extra: a plan note that does not parse,
        # or a reading model that is down, costs the block, never the turn.
        logger.warning("plan context skipped", exc_info=True)
        return "", "", None


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _turn_kind(text: str) -> str:
    """The turn line's kind: "chat", a built-in command's name, or "slash".

    Only names from COMMANDS pass: a skill installed into the vault
    (ADR-0097) is a slash command its user named, and an unknown one is
    whatever the user typed, so both stay "slash" and no free text reaches
    the journal."""
    if not text.startswith("/"):
        return "chat"
    from silica.ui.commands import COMMANDS

    word = text.split(None, 1)[0].lower()
    return word[1:] if any(c.name == word for c in COMMANDS) else "slash"


async def run_turn(text: str, attachments: "Sequence[Path]" = (), workspace: str = "",
                   withhold: "Sequence[str]" = (), files: "Sequence[dict]" = (), said: str = "",
                   prebuilt: dict | None = None, note: str = "", story: str = "",
                   scope: str = "story") -> AsyncIterator[dict]:
    """One agent turn as a stream of transport-neutral wire dicts.

    Yields `event_to_json(...)` dicts as the agent streams, then exactly one
    terminal dict: `{"type": "done", ...}` or `{"type": "error", ...}`. Owns the
    whole turn lifecycle (session append, sync→async queue bridge, cancel token,
    context compaction, save). Both `--gui` (SSE) and `connect` (WS) consume this
    — the framing is the transport's job, not this core's.

    Gate lifecycle: the slot is freed on normal end/error at once; on abandonment
    (the consumer stops iterating — a dropped SSE/WS client) the worker keeps
    running, so we signal cancel and defer the release to the worker's exit, so
    no second turn overlaps a zombie still mutating `messages`.

    ADR-0121: `withhold` names tools this turn is not offered (a turn that
    brings attached files in is not offered the injector, point 3); `files` and
    `said` ride the user message for the bubble a reload draws (point 4);
    `prebuilt` is a stored user message asked again as it was (POST /turn/redo).
    `note` is the vault path of the note open beside the chat (the composer's
    chip): it joins the named notes and rides the user message as `silica_note`.
    `story` is a Discover story the composer's story chip names (ADR-0137 point
    8): its articles ride the turn as a fenced block, `scope` says what the
    answer may draw on, and the chip rides the message as `silica_story`.
    """
    from silica.cli import _compact_context, _expand_web_turn, _update_context_tokens

    messages = SESSION.chat.messages
    if not SESSION.busy:  # direct callers (tests, future WS) that didn't pre-claim
        SESSION.busy = True
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    SESSION.cancel = threading.Event()  # module-level so /stop can see it
    task: asyncio.Task | None = None
    # The turn's usage line (ADR-0100 point 3), written in the finally.
    # Abandonment ends the stream without a verdict, so "cancelled" until a
    # terminal frame says otherwise.
    started, tokens_before = time.monotonic(), meter_totals()
    first_token: list[float] = []
    tools: dict[str, int] = {}
    tool_errors: dict[str, int] = {}
    outcome = "cancelled"
    limit: str | None = None  # the layer a `limited` turn met (ADR-0102 point 10)
    ws: dict | None = None    # the workspace this turn runs in (ADR-0112 point 4)

    def cb(ev):  # runs in the agent/LLM worker thread
        if isinstance(ev, ToolCompleteEvent) and ws is not None and ev.name == "silica_write_note":
            _join_workspace(ws["path"], ev)
        if isinstance(ev, ToolStartEvent):
            tools[ev.name] = tools.get(ev.name, 0) + 1
        elif isinstance(ev, ToolErrorEvent):
            tool_errors[ev.name] = tool_errors.get(ev.name, 0) + 1
        elif not first_token and isinstance(ev, LLMStreamEvent) and ev.chunk_type == "text" and ev.content:
            first_token.append(time.monotonic())
        data = event_to_json(ev)
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
    # says so on "work/tool" (cli._sc_nucleate), so its block draws as the
    # agent's does.
    BUS.subscribe("work/tool", on_phase)

    try:
        # A slash command follows the REPL's dispatch order (silica/cli.py): the
        # direct handler first — synchronous, no LLM round-trip — then the
        # workflow expansion. Asking the same handler the REPL asks is the point:
        # the hand-kept list of "web commands" that used to gate this drifted, and
        # /lexical /wiki /graph /map /find /vault fell through it into an error.
        #
        # A persona (ADR-0127 point 3) comes before all of them: it is worn, not
        # run. Its command edits this conversation's system line and asks no
        # model; with words after the name, those words are this turn, already
        # under the persona.
        from silica.kernel import personas

        worn = (await asyncio.to_thread(personas.command, text, messages, CONFIG.vault_path or None,
                                        not SESSION.chat.collapsed) if text.startswith("/") else None)
        if worn is not None and not worn[1]:
            from silica.obs import narration as _narr

            messages.append({"role": "user", "content": text, "origin": "cli"})
            session.narrate_user_turn(messages[-1])
            messages.append({"role": "assistant", "content": worn[0]})
            _narr.NARRATOR.turn(messages[-1])
            _update_context_tokens(messages)
            outcome = "done"
            yield {"type": "done", "answer": worn[0], "html": render.linkify(worn[0], remote_images=False),
                   **session.ctx_meter()}
            return
        if worn is not None:
            text = worn[1]
        from silica.config import withheld

        # A view's command goes with the view (ADR-0140 point 4), and a demo's
        # refused ones with it (ADR-0139 point 6); the picker does not offer
        # them, but a typed one does not come from it. Asked after the persona,
        # whose words are this turn, and before /web and /flashcards, which run
        # ahead of the dispatchers: "/<persona> /web" passed the 404 in
        # _demo_refuses and reached the internet (review 2026-10-31). The first
        # word as split() finds it, lowered as both dispatchers lower it.
        head = (text.split(None, 1) or [""])[0]
        if head.startswith("/") and head.lower() in withheld("commands"):
            outcome = "error"
            yield {"type": "error", "error": f"'{head}' is not part of this account"}
            return
        agent_msg = text
        escalated = False   # the turn's answer is the aid itself (learn.offer.escalation)
        plan_appendix = ""  # Learn's calendar, appended under the answer (learn.offer.plan_context)
        plan_draft = None   # the plan this turn computed, offered for saving under the answer
        # /web comes first: it is neither direct nor a workflow expansion but an
        # agent turn with web-only tools. A usage error raises ValueError, which
        # the except below turns into the single error event.
        web = _expand_web_turn(text, messages) if text.startswith("/") else None
        if web is not None:
            agent_msg = web[1]
        elif text.lower().split(None, 1)[:1] == ["/remember"] and len(text.split(None, 1)) > 1:
            # /remember <text> (ADR-0076 §6) is written here and not by the
            # direct handler below, whose console text would come back wrapped
            # in a code block: the chat draws this as a Saved line with Undo,
            # from the `memory` the done frame carries. Bare /remember goes on
            # to the expansion below, which asks the model.
            from silica.obs import narration as _narr
            from silica.cli import remember_text
            from silica.kernel import user_memory

            out = user_memory.save("vault", remember_text(text), session=_narr.NARRATOR.sid or "")
            answer = user_memory.describe(out)
            messages.append({"role": "user", "content": text, "origin": "cli"})
            session.narrate_user_turn(messages[-1])
            messages.append({"role": "assistant", "content": answer})
            _narr.NARRATOR.turn(messages[-1])
            done = {"type": "done", "answer": answer, "html": render.linkify(answer, remote_images=False), **session.ctx_meter()}
            if (saved := user_memory.undo_payload(out)) is not None:
                done["memory"] = saved
            outcome = "done"
            yield done
            return
        elif text.lower().split(None, 1)[:1] == ["/flashcards"]:
            # Cards are made as data and checked before they are shown
            # (learn.artifacts.flashcards), and the answer is their Markdown:
            # the direct path below would wrap what the command prints in a
            # code block, and the formulas on the cards would not render.
            import shlex

            from silica.obs import narration as _narr
            from silica.cli import _int_flag, _positional, _str_flag
            from silica.kernel.learn.artifacts import flashcards

            try:
                args = shlex.split(text)[1:]
            except ValueError:
                raise ValueError("unbalanced quotes in command. Wrap paths with spaces in \"...\".") from None
            targets = _positional(args)
            if not targets:
                raise ValueError("/flashcards needs a note or a folder. Usage: /flashcards <note|folder> [--n=12] [--save=<path>]")
            answer = await asyncio.to_thread(flashcards, targets, _int_flag(args, "--n=", 12),
                                             _str_flag(args, "--save").strip())
            messages.append({"role": "user", "content": text, "origin": "cli"})
            session.narrate_user_turn(messages[-1])
            messages.append({"role": "assistant", "content": answer})
            _narr.NARRATOR.turn(messages[-1])
            _update_context_tokens(messages)
            outcome = "done"
            yield {"type": "done", "answer": answer, "html": await asyncio.to_thread(render.answer_html, answer, []),
                   **session.ctx_meter()}
            return
        elif text.startswith("/"):
            from silica.cli import _expand_workflow_shortcut, _handle_direct_shortcut
            from silica.ui.console import CONSOLE

            def _run_slash():
                # Both dispatchers print their result to CONSOLE and both can do
                # real work, so both run under the capture and off the loop
                # thread. The expansion is not a pure string builder: /fetch,
                # /web-search and /convert do the whole job inside it and return
                # "" to say the REPL has nothing left for the agent. Reading
                # that "" as "not available" was reporting failure for work that
                # had already written notes to disk, with the success line going
                # to the server's stdout where no browser user can see it.
                with CONSOLE.capture() as capture:
                    handled = _handle_direct_shortcut(text, messages)
                    expanded = None if handled else _expand_workflow_shortcut(text)
                return handled or expanded == "", expanded, capture.get()

            # Pumped as the agent's turn is: a command that runs the injector
            # streams its block and phases while it works (cli._sc_nucleate),
            # and they reached no one while this awaited the thread whole. Held
            # as the turn's task, so a dropped tab keeps the gate shut until
            # the work ends instead of freeing it under a running import.
            done_slash = object()
            typed = {"role": "user", "content": text, "origin": "cli"}
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
                task.add_done_callback(lambda t: q.put_nowait(done_slash))
                while (item := await q.get()) is not done_slash:
                    yield item
                handled, expanded, captured_out = await task
            finally:
                BUS.unsubscribe("work/tool", _turn_before_its_work)

            if handled:
                # Appended only once the verdict is in: a False falls through to
                # the agent below, which appends the expanded turn itself.
                messages.append(typed)
                if not told:
                    session.narrate_user_turn(typed)
                # A GUI started from a terminal renders CONSOLE in colour and the
                # capture keeps the escape codes, which the chat showed verbatim
                # around /fetch's note path. The browser gets the text alone.
                from rich.text import Text
                shown = Text.from_ansi(captured_out).plain.strip()
                answer = f"```text\n{shown}\n```" if shown else "```text\n(done)\n```"
                messages.append({"role": "assistant", "content": answer})
                from silica.obs import narration as _narr
                _narr.NARRATOR.turn(messages[-1])

                # Yield a fake agent turn with the direct result
                outcome = "done"
                yield {
                    "type": "done",
                    "answer": answer,
                    "html": render.linkify(answer, note_resolver(), remote_images=False),
                    **session.ctx_meter(),
                }
                return

            if expanded is None:
                outcome = "error"
                yield {"type": "error", "error": f"'{text}' not available in this session"}
                return
            agent_msg = expanded

        # The notes the user named, as paths (ADR-0097 point 2). From `text`,
        # so a skill's expansion keeps its links and the block adds their
        # paths. Not on a /web turn: its tools cannot read a note. Off the
        # loop, because it reads the driver's graph.
        opened = ""
        if web is None:
            from silica.agent.mentions import named_notes
            from silica.kernel.learn.offer import says_lost
            from silica.ui.web import jobs

            if block := await asyncio.to_thread(named_notes, text, None, note):
                agent_msg += "\n\n" + block
                # Only a note the block kept: one renamed under the open drawer
                # was left out, and the bubble must not claim it was read.
                opened = note if note and ": " + note in block else ""
            # A reader still stuck on a note gets the aid as the answer itself
            # (learn.offer.escalation), and an offer's command sent is recorded.
            if block := await asyncio.to_thread(_escalation, text, messages):
                agent_msg += "\n\n" + block
                escalated = True
            block, plan_appendix, plan_draft = await asyncio.to_thread(_plan_context, text, messages)
            if block:
                agent_msg += "\n\n" + block
            elif await asyncio.to_thread(says_lost, text):   # Jev over HTTP behind the word list: off the loop
                # A reader saying it is unclear asked to understand, not to
                # write: the local 9B answered "non ho capito il Boosting"
                # with a new note of its own (2026-10-23).
                agent_msg += ("\n\n[The reader says this was not clear. Answer here in the chat; READ-ONLY: "
                              "do not create, edit, patch, or move any note.]")
            # This chat's conversions that ended since the agent last heard of
            # them: the history still says "still converting" (jobs.settled).
            if block := await asyncio.to_thread(jobs.settled, SESSION.chat.sid):
                agent_msg += "\n\n" + block

        story_chip = None
        if story:
            from silica.sources.discover.story import chat_block

            block, story_chip = await asyncio.to_thread(chat_block, None, story, scope)
            if block:
                agent_msg += "\n\n" + block
        # An attached image rides this turn only (ADR-0077 point 3): the
        # transcript, the narration and every later turn carry its saved path,
        # which silica_look opens again, because the base64 would otherwise be
        # re-sent with every later turn and stored with the session.
        images = [] if text.startswith("/") else list(attachments)
        if images:
            agent_msg += "".join(f"\n\n[image attached: {p}]" for p in images)
        msg: dict[str, Any] = {"role": "user", "content": agent_msg}
        if text.startswith("/"):
            msg["origin"] = "cli"
        if persona := personas.active(messages):
            msg[personas.TURN_KEY] = persona
        if files:
            msg["silica_files"] = list(files)
        if said:
            msg["silica_said"] = said
        elif text.startswith("/") and agent_msg != text:
            # A command's expansion is the model's instructions, not the
            # person's words: a reloaded chat drew /quiz's directive as what
            # they had typed (tour run, 2026-10-29).
            msg["silica_said"] = text
        if opened:
            msg["silica_note"] = opened
        if story_chip:
            msg["silica_story"] = story_chip
        if prebuilt is not None:
            msg = prebuilt
        ws = await asyncio.to_thread(session.resolve_workspace, workspace)
        await asyncio.to_thread(session.apply_workspace, ws)
        if ws is not None:
            msg["silica_workspace"] = ws["path"]   # the scope this turn ran in
        messages.append(msg)
        session.narrate_user_turn(msg)
        if msg.get("silica_files"):
            # The bubble's file chips, with what "add to vault" needs (ADR-0121
            # point 4): the page only knew the names it uploaded.
            yield {"type": "files", "files": await asyncio.to_thread(_files_shown, msg["silica_files"])}
        if images:
            from silica.agent import vision
            if vision.separate():
                # A chat model that takes text only gets the pixels as words
                # (ADR-0077 amendment 2026-10-21). They ride this turn only, as
                # the pixels did: the done-callback below restores agent_msg.
                msg["content"] = agent_msg + await asyncio.to_thread(vision.read_attached, images, text)
            else:
                from silica.kernel.recall.image_readings import image_part
                msg["content"] = [{"type": "text", "text": agent_msg}, *map(image_part, images)]

        # Both wrappers forward every event to `cb` untouched: WebTurn records the
        # trace the citations are built from, RecallWatch counts recall misses for
        # the thin-coverage hint.
        # A /web turn is not checked for numbers, but its pages were read: the
        # RecallWatch under its WebTurn collects them for the session set.
        read = RecallWatch(cb)
        watch = WebTurn(web[0], read) if web else read

        sentinel = object()
        task = asyncio.create_task(
            asyncio.to_thread(
                _scoped_run, ws, run_agent, messages, CONFIG.model, watch,
                cancel_token=SESSION.cancel,
                # Same chat_tools cut as the CLI REPL (the tool block is the
                # biggest per-iteration cost); interactive keeps the GUI's live
                # stream and keeps the turn off the worker-slot cap. Slash
                # directives carry origin="cli", so excluded tools a command
                # names are summoned back exactly as in the terminal.
                constraints=(
                    web_turn_constraints() if web
                    else AgentConstraints(tools=tuple(n for n in chat_tools(messages) if n not in withhold),
                                          deferred=chat_deferred(messages), interactive=True,
                                          max_iterations=DEMO_MAX_ITERATIONS if demo() else None)
                ),
            )
        )
        SESSION.task = task
        task.add_done_callback(lambda t: q.put_nowait(sentinel))
        if images:
            # After the sentinel's callback and before the stream reader resumes,
            # so the context meter and compaction already see the text form.
            task.add_done_callback(lambda t: msg.__setitem__("content", agent_msg))

        while True:
            item = await q.get()
            if item is sentinel:
                break
            yield item

        # Before the await, which re-raises: a turn that failed still read what
        # its results showed, and the narration a reload rebuilds from has them.
        SESSION.chat.seen_numbers.update(read.numbers)
        answer = await task  # re-raises if run_agent failed
        # Read before attribution and figures append to it.
        empty = answer == _EMPTY_COMPLETION
        # Stop is an outcome, not an answer (audit P3): the loop hands back its
        # sentinel once the token is set, and the text that had streamed stays
        # in the transcript untouched. The frame says who stopped it and the GUI
        # draws that as a row under what arrived, never as the reply. `answer`
        # keeps the sentinel: the WS bridge (silica connect) reads it as the
        # turn's text and has no row to draw.
        stopped = SESSION.cancel.is_set()
        # ADR-0095: the answer as the model wrote it, before attribution and
        # figures add text it did not; not a stopped turn's, not a /web turn's,
        # whose answer is bound to banked verbatim quotes.
        unread = ([] if stopped or web else
                  unfounded_in_turn(answer, read.numbers, messages, SESSION.chat.seen_numbers))
        # isinstance, not `if web:` — the two are the same condition (watch is
        # built from it one screen up) but only this one says which half of the
        # union carries `attribute`.
        if stopped:
            pass
        elif isinstance(watch, WebTurn):
            # Before _linkify and before the compaction sweep: the Sources block
            # belongs to what the user sees AND to what the history carries.
            answer = watch.attribute(answer, messages)
        elif watch.web_answer:
            from silica.sources.web_research import relay_sources

            answer = relay_sources(answer, messages)
        if not stopped:
            # The figures the turn read go beside the paragraph they illustrate
            # (ADR-0082). Here and not in the loop: only this surface draws an
            # embed, and the stored message must carry them for a reopened chat.
            from silica.agent.figures import place_figures

            answer = place_figures(answer, messages, exists=lambda f: render.find_asset(f) is not None)
            if plan_appendix and answer:
                # Learn's calendar under the answer, written by code so every
                # date is the plan's (learn.offer.plan_context)
                answer = answer.rstrip() + "\n\n" + plan_appendix
                if messages and messages[-1].get("role") == "assistant":
                    messages[-1]["content"] = answer
        # A request made in enough chats becomes a proposed skill, or one already
        # installed is offered as the next turn (ADR-0127 points 5 and 6).
        skill_note, offer = ((None, None) if stopped or web or not answer
                             else await asyncio.to_thread(_skill_after_turn, text, messages))
        if offer is None and not (stopped or web or not answer or escalated):
            # A note the reader keeps missing, or a message saying it was not
            # clear: a way to work on it, launched by the reader (learn.offer).
            # Not under an answer that is already the aid.
            offer = await asyncio.to_thread(_study_offer, text, messages, plan_draft)
        # The lines under the answer (ADR-0094 point 8), stored on it before the
        # beat below so a reopened chat draws them too. A stopped turn has no
        # answer to carry them: they are drawn live only.
        notes = watch.notes() if isinstance(watch, RecallWatch) else []
        notes += numbers_notes(unread)
        if not (stopped or web):
            # A card or a slide point its cited note does not hold (learn.artifacts).
            from silica.kernel.learn.artifacts import grounding_notes

            notes += grounding_notes(text, answer)
        notes += [skill_note] if skill_note else []
        notes = turn_notes.ordered(notes) if stopped else turn_notes.attach(messages, notes)
        # The notes this turn read, before the beat stores the answer and before
        # compaction stubs the results that name them (ADR-0127).
        refs = read_refs(messages)
        if refs and not stopped and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            messages[-1][READS_KEY] = refs
        # Final-assistant turn beat, post-attribution (see loop.py).
        if messages and messages[-1].get("role") == "assistant":
            from silica.obs import narration as _narr
            _narr.NARRATOR.turn(messages[-1])
        _update_context_tokens(messages)
        SESSION.chat.collapsed = _compact_context(messages, SESSION.chat.collapsed)
        # answer_resolver reads the DRIVER graph — with the ws backend installed
        # (silica connect) a driver call on the loop thread deadlocks (`_rpc`
        # blocks the very loop that must send the frame), so render off-loop.
        html = await asyncio.to_thread(render.answer_html, answer, refs)
        done = {
            "type": "done",
            "answer": answer,
            "html": html,
            **session.ctx_meter(),
        }
        if stopped:
            done["stopped"] = True
        if notes:
            done["notes"] = notes
        if offer:
            done["offer"] = offer
        outcome = "cancelled" if stopped else "empty" if empty else "done"
        yield done
    except Exception as exc:  # never leave the UI stuck on the spinner
        # Off the loop: hosted, a 403 reads the key to name the spent layer.
        failure = await asyncio.to_thread(_failure, exc)
        # A limit reached is the tenant's allowance at work, not a fault of
        # the server: counted as `limited` (ADR-0102 point 10), and kept out
        # of the error log the nightly rollup counts.
        limit = failure.get("limit")
        if limit is None:
            logger.exception("web turn failed")
        outcome = "limited" if limit else "error"
        yield {"type": "error", **failure}
    finally:
        BUS.unsubscribe("work/phase", on_phase)
        BUS.unsubscribe("work/tool", on_phase)
        # ponytail: the tokens are the difference of the process totals, so a
        # background job running during the turn is charged to it (its own
        # llm_call lines still name it by call-site), and an abandoned turn's
        # worker spends past this line uncounted. A context variable per turn
        # is the upgrade, due when ADR-0099's jobs are built.
        tokens_after = meter_totals()
        emit("turn", kind=_turn_kind(text), channel="web", outcome=outcome,
             ms=round((time.monotonic() - started) * 1000),
             first_token_ms=round((first_token[0] - started) * 1000) if first_token else None,
             calls=tokens_after[0] - tokens_before[0], prompt=tokens_after[1] - tokens_before[1],
             cached=tokens_after[3] - tokens_before[3],
             completion=tokens_after[2] - tokens_before[2],
             tools=dict(tools), tool_errors=dict(tool_errors), limit=limit,
             scope="workspace" if ws is not None else "vault")
        session.prewarm_seed()  # the turn may have written notes — refresh the new-chat seed
        if task is not None and not task.done():
            SESSION.cancel.set()  # abandonment: stop the zombie...
            task.add_done_callback(lambda t: SESSION.release())  # ...free the gate when it exits
        else:
            SESSION.release()  # normal / error / early-return: free now


# Cloudflare cuts a request whose origin sends no byte for 125 s (ADR-0099
# Context), and a slash command or a long tool call streams nothing while it
# works: hosted t01's /nucleate got a 524 at 124,990 ms on 2026-10-23, while web
# turns that kept streaming events ran to 333 s and ended done. ADR-0099 left
# streamed bytes untried for the jobs; a turn that stays a request needs them.
_KEEPALIVE_S = 30.0


async def _with_keepalive(chunks: AsyncGenerator[str, None], every: float) -> AsyncIterator[str]:
    """`chunks`, with an SSE comment line whenever `every` seconds pass without
    one. The pending step is waited on, never timed out, so a keepalive does not
    cancel the turn; a dropped client cancels it, as it did with no wrapper."""
    step = asyncio.ensure_future(chunks.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({step}, timeout=every)
            if not done:
                yield ": keepalive\n\n"
                continue
            try:
                chunk = step.result()
            except StopAsyncIteration:
                return
            yield chunk
            step = asyncio.ensure_future(chunks.__anext__())
    finally:
        if step.done():
            await chunks.aclose()
        else:
            step.cancel()
            # What the cancelled turn raises on its way out has no reader left:
            # its own finally already wrote the verdict and holds the gate.
            with suppress(asyncio.CancelledError, Exception):
                await step


def _turn_response(text: str, attachments: "Sequence[Path]" = (), workspace: str = "",
                   **extra: Any) -> StreamingResponse:
    """One agent turn as an SSE stream. Caller must claim the slot via
    `_begin_turn()` first; `_sweep_if_orphaned` frees it if the body never runs."""

    async def gen():
        from silica.config import hosted

        # The done frame names the turn a flag would point at (ADR-0100 point
        # 6), hosted only: the one place a flag exists. Not a demo's, whose
        # flags would die unread with its HOME (ADR-0139 point 6). Read where
        # the session stood first, since the _busy gate lets only this turn
        # narrate in it.
        flaggable = hosted() and not demo()
        sid0 = SESSION.chat.sid
        seq0 = await asyncio.to_thread(_session_head, sid0) if flaggable else 0
        async for item in run_turn(text, attachments, workspace, **extra):
            if flaggable and item.get("type") == "done":
                turn = await asyncio.to_thread(_turn_since, sid0, seq0)
                if turn:
                    item["turn"] = turn
            yield _sse(item)

    return StreamingResponse(
        _with_keepalive(gen(), _KEEPALIVE_S), media_type="text/event-stream",
        background=BackgroundTask(SESSION.sweep_if_orphaned)
    )


# The system prompt teaches the write workflows and a phone turn has none of
# their tools, so without this line "save this" reads to the model as a request
# it can meet. It goes before the seed's closing language line, which must stay
# last (cli.seed_messages).
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
    it meanwhile (_reset_session), and reopening the one held at the start
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
                # turn meets the same SessionBusy in _narrate_user_turn, which
                # is where it is reported.
                logger.warning("narration not restored after a phone turn")


def _phone_turn(text: str, cancel: threading.Event) -> dict:
    """One phone turn, whole: `{"answer": ...}`, or `{"error": ...}` with the
    sentence the browser would show, a usage limit's reset included."""
    from silica.obs import narration as _narr
    from silica.cli import _compact_context, _update_context_tokens, seed_messages

    # The browser reopened the phone's session from the chat list and now
    # writes it, so the phone goes on in a new one rather than beside it. A
    # chat deleted from the list starts over too: its history must not reach
    # the model again, nor its file come back without a title.
    if SESSION.phone.sid is not None and (
            SESSION.phone.sid == SESSION.chat.sid
            or not (_narr.narration_dir() / f"{SESSION.phone.sid}.jsonl").exists()):
        SESSION.reset_phone()
    started, tokens_before = time.monotonic(), meter_totals()
    first_token: list[float] = []
    tools: dict[str, int] = {}
    tool_errors: dict[str, int] = {}
    outcome, limit = "cancelled", None
    # CONFIG.context_tokens is the browser's ring. The phone's compaction
    # borrows the meter and hands it back as it found it.
    browser_tokens = CONFIG.context_tokens

    def cb(ev):  # the counts the turn line carries, as run_turn's cb keeps them
        if isinstance(ev, ToolStartEvent):
            tools[ev.name] = tools.get(ev.name, 0) + 1
        elif isinstance(ev, ToolErrorEvent):
            tool_errors[ev.name] = tool_errors.get(ev.name, 0) + 1
        elif not first_token and isinstance(ev, LLMStreamEvent) and ev.chunk_type == "text" and ev.content:
            first_token.append(time.monotonic())

    try:
        if not SESSION.phone.messages:
            seed = seed_messages()
            seed.insert(len(seed) - 1, {"role": "system", "content": _PHONE_LINE})
            SESSION.phone.messages[:] = seed
        with _narrating_phone():
            msg = {"role": "user", "content": text, "origin": "telegram"}
            SESSION.phone.messages.append(msg)
            _narr.NARRATOR.turn(msg)
            answer = run_agent(SESSION.phone.messages, CONFIG.model, cb, cancel_token=cancel,
                               constraints=AgentConstraints(tools=PHONE_TOOLS, interactive=True))
            if SESSION.phone.messages[-1].get("role") == "assistant":
                _narr.NARRATOR.turn(SESSION.phone.messages[-1])
            _update_context_tokens(SESSION.phone.messages)
            SESSION.phone.collapsed = _compact_context(SESSION.phone.messages, SESSION.phone.collapsed)
        if cancel.is_set():
            return {"answer": "Stopped."}
        outcome = "empty" if answer == _EMPTY_COMPLETION else "done"
        return {"answer": answer}
    except Exception as exc:
        failure = _failure(exc)
        limit = failure.get("limit")
        if limit is None:
            logger.exception("phone turn failed")
        outcome = "limited" if limit else "error"
        return {"error": failure["error"]}
    finally:
        CONFIG.context_tokens = browser_tokens
        tokens_after = meter_totals()
        emit("turn", kind="chat", channel="telegram", outcome=outcome,
             ms=round((time.monotonic() - started) * 1000),
             first_token_ms=round((first_token[0] - started) * 1000) if first_token else None,
             calls=tokens_after[0] - tokens_before[0], prompt=tokens_after[1] - tokens_before[1],
             cached=tokens_after[3] - tokens_before[3],
             completion=tokens_after[2] - tokens_before[2],
             tools=dict(tools), tool_errors=dict(tool_errors), limit=limit)


def _zotero_detect() -> None:
    try:
        from silica.sources.zotero import detect
        detect(CONFIG.vault_path)
    except Exception:
        logger.debug("zotero detection skipped", exc_info=True)  # detection is an aid


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Host the Obsidian bridge for the GUI session — the plugin dials in and
    the driver hot-swaps to ws (falls back on drop). No-op without the
    [connect] extra or when the vault has no .obsidian/."""
    from silica.ui.connect import maybe_start_bridge
    from silica.ui.web import jobs

    # Before the first request (ADR-0099 point 4): a job this process did not
    # queue and that never ended was cut by the restart, and the student is
    # shown that, never a file that silently stopped.
    if n := jobs.recover():
        logger.warning("%d conversion(s) interrupted by the restart", n)
    bridge = None
    try:
        bridge = await maybe_start_bridge()
    except Exception:
        logger.exception("bridge auto-start failed")  # the GUI must not die for it
    # Session open for the web (ADR-0055 decision 9): what changed in Zotero
    # since the last session, for the Library tab's count. Off the loop, because
    # it is HTTP to Zotero; silent on failure, like the hook's line.
    asyncio.get_running_loop().run_in_executor(None, _zotero_detect)
    # MCP servers open in the background (ADR-0091), off the loop: reading
    # mcp.json and the keyring is file and D-Bus I/O.
    from silica.connectors.mcp_tools import start as _start_connectors
    asyncio.get_running_loop().run_in_executor(None, _start_connectors)
    asyncio.get_running_loop().run_in_executor(None, _prewarm_learn)
    yield
    if bridge is not None:
        await bridge.stop()


app = FastAPI(lifespan=_lifespan, dependencies=[Depends(security.same_origin_writes)])
# Added in this order because the last added runs first: a refused host is
# answered before anything else looks at the request.
app.middleware("http")(security.hosted_image_policy)
app.middleware("http")(security.withheld_views)
app.middleware("http")(security.require_loopback_host)


def _refused(status: int):
    async def answer(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": exc.args[0]}, status_code=status)

    return answer


# Sentences written for the person by code below this layer, which names no status.
app.add_exception_handler(vault_files.NotAChatFile, _refused(400))
app.add_exception_handler(vault_files.InboxClash, _refused(409))

# NOT GZipMiddleware. It would also wrap /chat, which is text/event-stream, and
# Starlette's gzip responder writes each chunk into a zlib compressor that
# buffers until it has enough to emit — so the SSE frames that make a turn
# stream would be withheld and the transcript would arrive in bursts. /graph
# compresses itself instead, at the one route where the payload is large enough
# to matter (see graph()).

# Images a chat turn may carry, and how large each may be: a phone photo is a
# few MB, and the model sees at most about 1,000 tokens of any of them.
_MAX_ATTACHED = 8
_MAX_IMAGE_BYTES = 20 * 2**20


def _save_attachments(images: list) -> list[Path]:
    """Decode the composer's data URLs into `attachments_dir()`, named by content
    so the same picture sent twice is one file. A 400 before the turn starts:
    an attachment the model cannot read must not cost a turn to find out."""
    import base64
    import binascii
    import hashlib

    from silica.kernel.recall.image_readings import _MIME
    from silica.kernel.base.durable import atomic_write_bytes
    from silica.kernel.base.home import attachments_dir

    if len(images) > _MAX_ATTACHED:
        raise HTTPException(status_code=400, detail=f"at most {_MAX_ATTACHED} images per message")
    out: list[Path] = []
    for img in images:
        name = str((img or {}).get("name") or "image")
        m = re.match(r"data:(image/[\w.+-]+);base64,(.+)", str((img or {}).get("data") or ""), re.S)
        ext = next((e for e, mime in _MIME.items() if m and mime == m.group(1)), None)
        if m is None or ext is None:
            raise HTTPException(status_code=400, detail=f"{name}: not an image the chat model reads "
                                                        "(png, jpg, webp, gif, bmp)")
        try:
            data = base64.b64decode(m.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail=f"{name}: the image data is damaged") from None
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail=f"{name}: larger than {_MAX_IMAGE_BYTES // 2**20} MB")
        path = attachments_dir() / f"{hashlib.sha256(data).hexdigest()[:16]}{ext}"
        if not path.exists():
            atomic_write_bytes(path, data)
        out.append(path)
    return out



_ATTACHED_RE = re.compile(r"\n*\[image attached: ([^\]\n]+)\]")
_ATTACHMENT_NAME_RE = re.compile(r"[0-9a-f]{16}\.(?:png|jpg|webp|gif|bmp)")


def _attached_images(content: str) -> tuple[str, list[dict]]:
    """A reloaded user turn as its words plus its images: the history keeps the
    saved path for the model (silica_look opens it), the person sees the picture
    they sent, as the live bubble showed it."""
    images = [{"src": f"/attachment/{Path(p).name}", "name": Path(p).name}
              for p in _ATTACHED_RE.findall(content) if _ATTACHMENT_NAME_RE.fullmatch(Path(p).name)]
    return _ATTACHED_RE.sub("", content).rstrip(), images


@app.get("/attachment/{name}")
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
    from silica.config import withheld

    if not demo():
        return ""
    if payload.get("images"):
        return "images are not part of the demo"
    if payload.get("story"):
        return "Discover is not part of the demo"
    # The first word as split() finds it, as jobs.from_command and run_turn's
    # /flashcards branch find it: " /convert x" got past a startswith("/")
    # here and queued a conversion (review 2026-10-31).
    text = str(payload.get("text") or "")
    word = (text.split(None, 1) or [""])[0].lower()
    return f"'{word}' is not part of the demo" if word in withheld("commands") else ""


@app.post("/chat")
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
    if not SESSION.claim():
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    text = payload.get("text", "")
    if story and scope == "web" and text.strip() and not text.startswith("/"):
        text = "/web " + text
    # /convert and /nucleate <file> typed here convert as jobs and answer at
    # once (ADR-0099 point 1), with the 202 a drop of files alone gets.
    from silica.ui.web import jobs

    try:
        queued = await asyncio.to_thread(jobs.from_command, text, SESSION.chat.sid)
    except BaseException:
        SESSION.release()
        raise
    if queued is not None:
        SESSION.release()
        return JSONResponse(queued, status_code=202)
    if SESSION.chat.sid and text.strip() and not text.startswith("/"):
        # A file read after its drop got no turn joins this message (ADR-0121
        # point 4): otherwise "this paper" names nothing the chat was given.
        joined = await asyncio.to_thread(jobs.join_next, SESSION.chat.sid)
        if joined:
            notes, files, earlier = joined
            composed = await asyncio.to_thread(drop_turn.compose_nucleate_turn, text, notes, [], earlier=earlier)
            return _turn_response(composed, attachments, workspace,
                                  withhold=drop_turn.WITHHELD_WITH_FILES, files=files, said=text, note=note,
                                  story=story, scope=scope)
    return _turn_response(text, attachments, workspace, note=note, story=story, scope=scope,
                          **({"said": text.removeprefix("/web ")} if story and scope == "web" else {}))


@app.post("/phone/turn")
async def phone_turn(payload: dict):
    """One turn from the phone, answered once and whole: a Telegram message is
    sent complete, so the caller has no use for a stream (ADR-0089 amendment,
    H5). 409 while any turn runs, the browser's included."""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is empty")
    if not SESSION.claim():
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    SESSION.phone_running = True
    # The browser's Stop sets this token, so it ends a phone turn too.
    SESSION.cancel = cancel = threading.Event()
    # Named as the gate's task, so a /chat stream closing in the same instant
    # (_sweep_if_orphaned) does not take this turn for an orphan and free it.
    SESSION.task = task = asyncio.ensure_future(asyncio.to_thread(_phone_turn, text, cancel))
    try:
        return await task
    finally:
        SESSION.phone_running = False
        SESSION.release()


@app.post("/phone/new")
async def phone_new():
    """`/new` from the phone: its next turn opens a fresh conversation."""
    if SESSION.busy:
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    SESSION.reset_phone()
    return {"ok": True}


# The body a request may carry through Cloudflare on the Free and Pro plans
# (Error 413 page, read 2026-10-13; it names no exception for Tunnel traffic).
# On the server the browser refuses a file over it before sending, with this
# reason, rather than show the edge's own 413 page (ADR-0099 Consequences).
_EDGE_MAX_MB = 100


@app.get("/supported_types")
def supported_types():
    """Extensions the pickers offer: `extensions` drives the chat `+`'s
    `accept`, `import` the vault `+`'s and its folder filter (ADR-0098 point 3).
    `mail` is the Mail row's picker, kept apart: the composer stages no mail
    (`_is_mail`). On the server both lists are ADR-0087 point 5's: the host
    has no mineru and no LibreOffice, and offered 60 extensions until then."""
    from silica.config import hosted
    from silica.sources.mail import SUFFIXES
    from silica.sources.registry import supported_nucleate_extensions
    from silica.ui.web.vault_import import HOSTED_EXTENSIONS, importable_extensions

    on_server = hosted()
    exts = list(HOSTED_EXTENSIONS) if on_server else supported_nucleate_extensions()
    return {"extensions": exts, "import": importable_extensions(on_server), "mail": list(SUFFIXES),
            "upload_max_mb": _EDGE_MAX_MB if on_server else None}


@app.get("/commands")
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


# A scanned book is the honest ceiling for one drop; past that the file is not
# something the nucleate lane can chew, and the Inbox is a vault folder, not a
# dump. The body is streamed in chunks rather than read whole, so an oversized
# upload is refused after one chunk instead of after materialising it in memory.
_UPLOAD_MAX_BYTES = 256 * 1024 * 1024
_UPLOAD_MAX_FILES = 32
_UPLOAD_CHUNK = 1024 * 1024
# A mail export is no drop: Google Takeout's "All mail" file runs to gigabytes,
# and the drop cap above refused every real one (2026-10-12). The ceiling here
# is RAM, not the nucleate lane: mail.plan() holds every message body.
# ponytail: measured 2026-10-12 on a synthetic Takeout, 1 GiB took 17 s to a
# first dry run and held 290 MB, so 8 GiB holds about 2.3 GB, twice that for
# non-ASCII mail. The upgrade is a headers-first pass that reads bodies only
# for the threads written; reopen at ADR-0090's gate 4 review.
_MAIL_UPLOAD_MAX_BYTES = 8 * 1024 ** 3


async def _write_upload(f: UploadFile, dest: Path) -> None:
    """Stream one dropped file to `dest`, refusing it past the drop cap."""
    async def chunks():
        while chunk := await f.read(_UPLOAD_CHUNK):
            yield chunk

    await _write_stream(chunks(), dest, _UPLOAD_MAX_BYTES,
                        f"{dest.name} is over the {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB upload limit")


def _part_path(dest: Path) -> Path:
    """`.<name>.part` beside `dest`, its name cut to the 255 bytes a file name
    may have: a drop's name is the browser's, and only the import caps it."""
    stem = dest.name
    while len(f".{stem}.part".encode("utf-8")) > 255:
        stem = stem[:-1]
    return dest.with_name(f".{stem}.part")


async def _write_stream(chunks, dest: Path, cap: int, too_big: str) -> None:
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
    import os

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


async def _stage_uploads(files: list[UploadFile], folder: str | None = None, **job: Any
                         ) -> tuple[list[str], list[str], list[tuple[str, str]], list[dict]]:
    """Write uploads to Inbox and mechanically stage them, mirroring the inline
    half of `/nucleate` (silica/cli.py): code/notebooks become skeleton stubs,
    prose stays as-is, and a file that needs convert() becomes a job.
    Returns (ready, stubs, skipped, queued): markdown notes ready for the
    injector/reading, code stub note paths already written to the vault,
    (file name, reason) for each file no lane took, and the job records
    (`job` carries the drop's text and session into each). The semantic step
    (nucleate? summarize?) is the agent's, driven by the user's message: see
    `_compose_nucleate_turn`.

    `folder`, vault-relative, takes the place of the inbox: a chat's drop is
    written to the chat's own folder (ADR-0121 point 5).

    A conversion never runs here (ADR-0099 point 1): a lecture's transcription
    outlasts the edge's 125 s, and a restart lost it without a trace. stage()
    reads whole files, so it runs in a worker thread: on the loop thread it
    blocked /stop, leaving a visible Stop button that could not be served.
    """
    from silica.config import hosted
    from silica.kernel.base.manifest import active_write_dir, get_active_manifest, resolve_inbox_dir
    from silica.sources import pdf_extract
    from silica.sources.convert import IMG_EXTS
    from silica.sources.registry import adapter_for, stage
    from silica.ui.web import jobs
    from silica.ui.web.vault_import import HOSTED_EXTENSIONS

    if len(files) > _UPLOAD_MAX_FILES:
        raise HTTPException(413, f"at most {_UPLOAD_MAX_FILES} files per drop")
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
        if _is_mail(dest.name):
            continue  # never written: see _is_mail
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


# Extensions the ingest line may name. A suffix is part of the file's name,
# which is the student's text, so anything outside this list is "other".
_INGEST_EXTS = frozenset({
    ".pdf", ".md", ".markdown", ".txt", ".docx", ".pptx", ".xlsx", ".odt", ".ods",
    ".epub", ".rtf", ".html", ".htm", ".csv", ".json", ".ipynb", ".py",
    ".png", ".jpg", ".jpeg", ".mp3", ".m4a", ".wav",
    # ADR-0120's additions to the hosted list, so its uploads count by type
    ".fb2", ".odp", ".webp", ".gif", ".aac", ".flac", ".ogg", ".opus", ".wma",
    ".3gp", ".3g2", ".aiff", ".aif", ".aifc", ".amr", ".au", ".snd",
})


def _ingest_ext(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if ext in _INGEST_EXTS else "other"


def _is_mail(name: str) -> bool:
    """A mail export dropped into the chat (ADR-0090 point 5): never staged.
    Staged, it wrote a note outside any undo run (so /revert took back the
    import before it) with no dry run and no address filter, and a refusal
    reached only the log (found end to end 2026-10-12). Mail goes through the
    Mail row or /mail import, which have both."""
    from silica.sources.mail import SUFFIXES

    return Path(name).suffix.lower() in SUFFIXES


@app.post("/nucleate")
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
    if not SESSION.claim():
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    from silica.kernel.base.layout import chat_folder
    from silica.ui.web import jobs

    try:
        fresh = not any(m.get("role") == "user" for m in SESSION.chat.messages)
        sid = await asyncio.to_thread(session.open_session) or uuid.uuid4().hex[:12]
        names = [Path(f.filename or "").name for f in files]
        if fresh:
            title = (text.strip().splitlines() or [", ".join(names)])[0]
            await asyncio.to_thread(_name_session, sid, title)
        # dest_dir too: a converted PDF's pictures go to `<dest_dir>/Images`,
        # which would otherwise be the vault's Inbox.
        folder = chat_folder(sid)
        ready, stubs, skipped, queued = await _stage_uploads(
            files, folder=folder, text=text, session=sid, drop=uuid.uuid4().hex[:12], dest_dir=folder)
    except Exception:
        SESSION.release()  # release the slot the staging never got to use
        raise
    mail = [n for n in names if _is_mail(n)]
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
        SESSION.release()
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
    resp = _turn_response(await asyncio.to_thread(
        drop_turn.compose_nucleate_turn, text, ready, stubs, mail, skipped,
        converting=[j["source"]["path"] for j in queued]),
        withhold=drop_turn.WITHHELD_WITH_FILES, files=files_said, said=text.strip())
    if queued:
        resp.headers["X-Silica-Jobs"] = ",".join(j["id"] for j in queued)
    return resp


def _name_session(sid: str, title: str) -> None:
    """A chat named by its drop (ADR-0121 point 1): a session with no turn yet
    is unlisted, and a deferred question has none until its file is read."""
    from silica.obs import narration as _narr

    title = title.strip()
    _narr.rename_session(sid, title[:57] + "…" if len(title) > 58 else title, CONFIG.vault_path or "")


@app.post("/chat/files/add")
async def chat_file_add(payload: dict):
    """"add to vault" on a file of the open chat (ADR-0121 point 5): the file
    and its notes copied into the Inbox, with no agent turn. The notes are
    the job's when the file was converted, else the file itself."""
    from silica.ui.web import jobs

    source = str(payload.get("source") or "")
    vault_files.chat_rel(source, SESSION.chat.sid or "")
    notes = [source]
    vault = CONFIG.vault_path or ""
    done = [r for r in jobs._all() if r.get("vault") == vault and r["source"].get("path") == source
            and r.get("state") == "done" and r.get("notes")]
    if done:
        notes = done[-1]["notes"]
    elif not source.endswith((".md", ".txt", ".markdown")):
        raise HTTPException(409, "that file is still being read")
    if not SESSION.claim():   # the one serialisation of vault writes (see /vault/import)
        raise HTTPException(409, "a turn is running: add it when it ends")
    try:
        added = await asyncio.to_thread(vault_files.add_to_vault, notes, [source])
    finally:
        SESSION.release()
    return {"notes": added}


def _files_shown(files: Any) -> list[dict]:
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
            rec = jobs.read(f["job"]) if jobs._ID.fullmatch(str(f["job"])) else None
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


def _edited(content: str, words: str, note: str = "") -> str:
    """A stored user message with its typed words replaced (POST /turn/redo):
    the manifest and the images stay, the named notes follow the new words,
    and the note that was open beside the chat stays the one the turn reads."""
    from silica.agent.mentions import named_notes, strip

    base = strip(content)
    cuts = [i for i in (base.find(drop_turn.MANIFEST_MARK), base.find("\n\n[image attached: ")) if i >= 0]
    rest = base[min(cuts):] if cuts else ""
    block = named_notes(words, None, note)
    if block:
        at = rest.find("\n\n[image attached: ")
        rest = rest + "\n\n" + block if at < 0 else rest[:at] + "\n\n" + block + rest[at:]
    return words + rest


@app.post("/turn/redo")
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
    if not SESSION.claim():
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    try:
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
    except BaseException:
        SESSION.release()
        raise
    withhold = drop_turn.WITHHELD_WITH_FILES if msg.get("silica_files") and not content.startswith(drop_turn.NUCLEATE_ASK) else ()
    return _turn_response(content, workspace=msg.get("silica_workspace") or "", withhold=withhold, prebuilt=msg)


@app.post("/vault/import")
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

    The turn slot is held while the batch writes: `_begin_turn` is the one
    serialisation of vault writes the server has, and an import racing a
    turn's writes is a case nothing tests.
    ponytail: one writer at a time, so an import waits for a turn; a lock per
    Inbox path is the upgrade, due when students ask to import mid-turn.
    """
    if len(files) > _UPLOAD_MAX_FILES:
        raise HTTPException(413, f"at most {_UPLOAD_MAX_FILES} files per request")
    if paths and len(paths) != len(files):
        raise HTTPException(400, "paths must name every file, in order")
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: import when it ends")
    try:
        results, folders = await _import_files(files, paths or [""] * len(files))
    finally:
        SESSION.release()
    if obsidian:
        _mark_obsidian_root(obsidian, paths)
    if last and folders:
        _embed_imported(folders)
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


@app.get("/vault/export/summary")
def vault_export_summary():
    """What each of the vault menu's two downloads would give, asked when the
    menu opens, so that "nothing new" is said before a click (ADR-0142 point 1)."""
    from silica.ui.web import vault_export as ve

    return ve.summary(_export_vault())


# The one read the rule by method does not reach (ADR-0142): a GET, because a
# link is what streams the archive to the person's disk, and still this page's
# alone, or any other page could ask for the whole vault.
@app.get("/vault/export", dependencies=[Depends(security.require_same_origin)])
def vault_export():
    """The vault whole, every file in its tree (ADR-0142 point 2). A link the
    browser follows, so the archive streams to the person's disk."""
    from silica.ui.web import vault_export as ve

    vault = _export_vault()
    top = time.strftime("Silica vault %Y-%m-%d")
    return StreamingResponse(ve.whole(vault, top), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{top}.zip"'})


@app.post("/vault/export/added")
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


# Measured from the pilot host 2026-10-17: yt-dlp gets "Sign in to confirm
# you're not a bot" for a public video, as YouTube answers datacenter
# addresses. With no account cookies on the server it is refused before a
# request goes out; with them, it is this same sentence when YouTube asks
# again (the account gone, the cookies expired), with the way that works.
YOUTUBE_ON_SERVER = ("YouTube does not let the server read videos (it asks it to sign in): "
                     "download the video's audio and add that file, and it is transcribed")


@app.post("/vault/fetch")
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
    from silica.sources.web_fetch import _YT_DOMAINS, YoutubeBlocked, host_matches, youtube_cookies
    from silica.sources.web_research import fetch_to_inbox

    url = str(payload.get("url") or "").strip()
    workspace = str(payload.get("workspace") or "")
    if not url or len(url) > 2048 or any(c.isspace() for c in url):
        raise HTTPException(400, "a link is one web address, at most 2,048 characters")
    row: dict = {"name": url}
    if (hosted() and youtube_cookies() is None
            and host_matches(url if "://" in url else f"https://{url}", *_YT_DOMAINS)):
        return {"files": [{**row, "status": "refused", "reason": YOUTUBE_ON_SERVER}]}
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: add the link when it ends")
    try:
        note = await asyncio.to_thread(fetch_to_inbox, url)
        row.update(status="note", notes=[note], path=note)
    except YoutubeBlocked:
        # web_fetch logged the renewal line; the student gets the way around it.
        row.update(status="refused", reason=YOUTUBE_ON_SERVER if hosted() else
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
    finally:
        SESSION.release()
    if row["status"] == "note":
        _embed_imported({str(Path(row["path"]).parent)})
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


# --- workspaces (ADR-0112) --------------------------------------------------------

@app.get("/workspaces")
def workspaces_list():
    """Every workspace as the drawer shows it, and the one this chat is
    opened in."""
    from silica.kernel import workspaces

    try:
        rows = [workspaces.summary(w) for w in workspaces.list_workspaces()]
    except Exception as exc:
        return _failure(exc)
    return {"workspaces": rows, "current": SESSION.chat.workspace}


@app.post("/workspaces")
def workspaces_create(payload: dict):
    """A new workspace note, optionally holding the note or folder its menu
    was opened on."""
    from silica.kernel import workspaces

    notes, folders = payload.get("notes") or [], payload.get("folders") or []
    if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (notes, folders)):
        raise HTTPException(status_code=400, detail="notes and folders must be lists of names")
    try:
        path = workspaces.create_workspace(str(payload.get("name") or ""), notes=notes, folders=folders)
    except ValueError as exc:
        # create_workspace's own sentences (name refused or taken), never a path
        raise HTTPException(status_code=400, detail=exc.args[0]) from None
    except FileExistsError:
        raise HTTPException(status_code=409, detail="a note with that name is already there") from None
    except OSError:
        raise HTTPException(status_code=400, detail="that name cannot be a file name here") from None
    return workspaces.summary(workspaces.read_workspace(path))


@app.post("/workspaces/line")
def workspaces_line(payload: dict):
    """Add or remove one line of a workspace: the user's act from the
    drawer's menu. Removing a line never touches the note or folder it names."""
    from silica.kernel import workspaces

    ws = workspaces.find_workspace(str(payload.get("workspace") or ""))
    if ws is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    key, value = str(payload.get("key") or ""), str(payload.get("value") or "")
    on = payload.get("on", True)
    if key not in workspaces.LINES or not value.strip() or not isinstance(on, bool):
        raise HTTPException(status_code=400, detail="key must be notes, folders or where, with a value")
    try:
        if on:
            workspaces.add_line(ws["path"], key, value)
        else:
            workspaces.remove_line(ws["path"], key, value)
    except (OSError, RuntimeError) as exc:
        # not 200: the menu checks r.ok and would show the line as saved
        return JSONResponse(_failure(exc), status_code=500)
    return workspaces.summary(workspaces.read_workspace(ws["path"]))


@app.get("/workspaces/membership")
def workspaces_membership(path: str, folder: bool = False):
    """For the drawer's menu on one note or folder: every workspace, whether
    the row is a listed line of it, and any other reason it is a member."""
    from silica.kernel import workspaces

    return {"path": path, "folder": folder, "workspaces": workspaces.membership_of(path, is_folder=folder)}


async def _import_files(files: list[UploadFile], paths: list[str]) -> tuple[list[dict], set[str]]:
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
                while chunk := await f.read(_UPLOAD_CHUNK):
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
                cap = pdf_extract.MAX_BYTES if on_server else _UPLOAD_MAX_BYTES

                async def chunks(upload=f):
                    while chunk := await upload.read(_UPLOAD_CHUNK):
                        yield chunk

                await _write_stream(chunks(), dest, cap, f"it is over the {cap // (1024 * 1024)} MB limit")
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


def _embed_imported(folders: set[str]) -> None:
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


@app.post("/mail/upload")
async def mail_upload(request: Request, name: str):
    """The Mail row's export (ADR-0090 point 5), saved into the inbox inside
    the write boundary as `_stage_uploads` saves a drop, and not staged: the
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
    gb = _MAIL_UPLOAD_MAX_BYTES // 1024 ** 3
    await _write_stream(request.stream(), Path(vault) / inbox_rel / name, _MAIL_UPLOAD_MAX_BYTES,
                        f"{name} is over the {gb} GB the Mail row takes: export one Gmail label "
                        "from Takeout, or run /mail import with the file's path")
    return {"path": f"{inbox_rel}/{name}"}


# How many rows of an uncapped list the metrics view receives. The report caps
# its own ranked lists at top_k; orphans and dangling are exhaustive, so they get
# cut here — and the true length always rides along in `totals`, so a cut list
# can never read as "this is all of them".
#
# 12, not 60: at 60 the orphans and dangling cards ran to 60 rows each and the
# dashboard became two long lists with charts above them (8.5k px on a 686-note
# vault). A card samples; GRAPH_REPORT.md is where the full list lives.
_METRICS_ROWS = 12


# How many stored readings the trend band draws. The series gains a line only
# when a count actually moves, so 60 is years of daily use on a vault that keeps
# changing -- and past that a sparkline 80px wide is drawing more points than it
# has pixels, which is a smear, not a trend.
_HISTORY_POINTS = 60


def _metrics_history(vault: str | None) -> list[dict]:
    """The stored readings the trend band draws, oldest last.

    Best-effort like the delta beside it: a vault that is read-only or unbound
    still gets its metrics, and losing the trend costs a band, not the view.
    """
    if not vault:
        return []
    try:
        from silica.kernel.report.history import read_history

        return read_history(vault)[-_HISTORY_POINTS:]
    except Exception as exc:
        logger.debug("metrics: history unreadable (%s)", exc)
        return []


def _lean_limit() -> int:
    """The character count under which a note is called lean.

    Read from the linter rather than restated here: the pane prints it beside
    every row, and a copy would keep saying 600 for a year after the limit moved.
    """
    try:
        from silica.kernel.link.ofm import LIMITS

        return int(LIMITS["lean_chars"])
    except Exception as exc:
        logger.debug("metrics: lean limit unreadable (%s)", exc)
        return 0  # the pane drops the comparison rather than inventing a bound


@app.get("/vault_version")
def vault_version():
    """A digest the explore surfaces poll to learn the vault moved under them.

    Deliberately NOT the BUS/SSE the turn stream uses: that bus is in-process,
    so it carries this browser's own agent and nothing else — and the writes
    this exists for are the ones from OUTSIDE it (Obsidian, `silica nucleate`
    in a terminal, a second window). A poll is the only signal that sees them
    without a resident watcher, which the charter rules out anyway.
    """
    from silica.kernel.recall.sync import vault_version as _version

    return {"version": _version()}


@app.get("/shape")
def shape():
    """Three read-only views over ONE graph build: containment, area coupling,
    and a derived reading order.

    They share an endpoint because they share the expensive part — build_graph_data
    plus community detection — and splitting them into three routes would pay it
    three times for surfaces a reader flips between.
    """
    from silica.kernel.graph.graph_export import build_graph_data, detect_communities

    try:
        nodes, edges = build_graph_data(folder="")
        # Assigns node["group"] in place, which group_of reads back below; the
        # return is read only for each area's colour, which Areas paints its
        # discs with so a disc and the graph's dots agree (ADR-0060).
        communities = detect_communities(nodes, edges)
    except Exception as exc:
        logger.warning("shape: graph build failed (%s)", exc)
        return _failure(exc)
    real = [n for n in nodes if n.get("type") != "ghost"]
    group_of = {n["id"]: n.get("group", -1) for n in real}
    label_of = {n["id"]: n.get("label") or n["id"].rsplit("/", 1)[-1] for n in real}

    # One tally with the report and the gap/bridge ranking (area_tally): linked
    # pairs counted once, hubs by degree with ties to the larger id. This loop
    # used to live here a third time and picked hubs by file order.
    from silica.kernel.graph.graph_export import area_hubs, area_tally

    t = area_tally(real, edges)
    sizes, intra, inter, deg, hub_of = dict(t.sizes), t.intra, t.inter, t.deg, area_hubs(t)
    adj: dict[str, set[str]] = {}
    for a, b in t.pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    # Multi-note communities only, biggest first. A singleton is its own
    # community: on the matrix it is a row and column of zeroes with a perfect
    # diagonal, which is 65 rows of noise around the 26 that carry the vault.
    ids = sorted((g for g, s in sizes.items() if s > 1), key=lambda g: (-sizes[g], g))

    def cohesion(g: int) -> float:
        s = sizes[g]
        possible = s * (s - 1) / 2
        return round(intra.get(g, 0) / possible, 4) if possible else 0.0

    colour = {c.id: (c.color, c.color_paper) for c in communities}
    areas = [
        {"id": g, "label": label_of.get(hub_of.get(g, ""), f"#{g}"), "path": hub_of.get(g, ""),
         "size": sizes[g], "cohesion": cohesion(g), "intra": intra.get(g, 0),
         "color": colour.get(g, ("", ""))[0], "color_paper": colour.get(g, ("", ""))[1]}
        for g in ids
    ]
    matrix = [[(intra.get(a, 0) if a == b else inter.get((min(a, b), max(a, b)), 0))
               for b in ids] for a in ids]

    return {
        "areas": areas,
        "matrix": matrix,
        # Every real note, for the containment view. Three fields, not the whole
        # node: the treemap needs a path, an area and a weight, and shipping the
        # colour/font/title the canvas uses would triple the payload for nothing.
        "notes": [{"path": n["id"], "size": n.get("size") or 1, "area": group_of.get(n["id"], -1)}
                  for n in real],
        "reading": vault_shape.shape_reading(adj, deg, areas, label_of),
        "totals": {"notes": len(real), "areas": len(ids),
                   "singletons": sum(1 for s in sizes.values() if s <= 1)},
    }


@app.get("/calendar")
def calendar(start: str = "", days: int = 7):
    """The 4-axis agenda days for the calendar tab — one endpoint, one build
    (the /shape pattern), plus the one reading only a calendar can carry.

    `bursting` (V6) is what the last fortnight of WRITING turned out to be
    about. It is here and not on explore because it is the only one of the
    seven variables with no position in the graph at all: its axis is time,
    which is this tab's axis. Read from the cheap pass (~0.3 s) rather than
    from the report's co-occurrence depth (9 s cold) — both call the same
    `signals.burst` over the same inputs, and tests/test_graph_variables.py
    holds them equal.
    """
    from silica.kernel.report.structure import bursting
    from silica.tools.events import silica_agenda

    out = silica_agenda(start=start or "today", days=max(1, min(90, days)))
    try:
        out["bursting"] = bursting()
    except Exception:
        # A reading, never the tab: an index that cannot answer costs the strip
        # and leaves every scheduled day exactly where it was.
        logger.debug("calendar: burst unavailable", exc_info=True)
    return out


@app.get("/calendar/event/path")
def calendar_event_path(title: str = "", start: str = "", rrule: str = ""):
    """The note the quick card would write, named before it is saved."""
    from silica.tools.events import event_note_path

    return event_note_path(title, start, rrule)


@app.post("/calendar/event")
def calendar_event_create(payload: dict):
    """The quick card's Save (ADR-0118 point 1): the same writer as the chat's
    `silica_event_create`, so the calendar is a second door and not a second
    implementation of the event note."""
    from silica.tools.events import create_event

    f = {k: str(payload.get(k) or "") for k in
         ("title", "start", "end", "rrule", "reminder", "body", "plan")}
    return create_event(f.pop("title"), f.pop("start"), **f, person=True)


@app.post("/calendar/event/status")
def calendar_event_status(payload: dict):
    """The event card's Done and Cancel (ADR-0118 point 6). Open is not a
    status a card can set: `silica_event_update` keeps a field it is passed
    empty, so a closed series is reopened in its note."""
    from silica.tools.events import silica_event_update

    status = str(payload.get("status") or "")
    if status not in ("done", "cancelled"):
        return {"error": "status must be done or cancelled"}
    return silica_event_update(note=str(payload.get("path") or ""), status=status)


@app.post("/reminders")
def reminders_poll():
    """The front-end poll IS the reminder tick: compute due, advance the
    high-water marks, return the list. POST, not GET — the poll mutates the
    sidecar, and a cacheable GET risks a stale 200 swallowing a delivery.
    At-most-once: the mark advances on delivery; REPL and GUI share the
    sidecar, so whichever surface polls first delivers."""
    import datetime as _dt
    from pathlib import Path as _Path

    from silica.config import CONFIG
    from silica.kernel.calendar.model import scan_events
    from silica.kernel.calendar.reminders import (
        advance_marks, delivery_lock, due_reminders, load_marks, save_marks,
    )

    vault = _Path(CONFIG.vault_path)
    events = scan_events(vault)
    with delivery_lock(vault):
        marks = load_marks(vault)
        due = due_reminders(events, marks, _dt.datetime.now())
        if due:
            save_marks(vault, advance_marks(marks, due))
    return {"due": [{"stem": r["stem"], "title": r["title"],
                     "start": r["start"].isoformat(sep=" "), "late": r["late"]}
                    for r in due]}


def _report_head(report, payload: dict, elapsed_s: float, started) -> dict:
    """The run head of the Report panel, plus the reading it can only get from a store.

    Everything else the metrics view shows is derived from THIS report. The one
    thing it cannot derive is what moved, so this is where the report is filed
    and the last different one is handed back.

    Best-effort by construction: a vault that is read-only, unbound or on a full
    disk still gets its metrics. Losing the delta costs a section of one panel;
    failing the call costs the whole view.
    """
    import datetime as _dt

    from silica.kernel.report.history import record_report, signals_of

    areas = len([c for c in report.clusters if c.size > 1])
    # compute_report memoises per vault epoch, so a second open of the tab costs
    # microseconds and "0.0s" in the run head would read as "the audit is free"
    # rather than "you are looking at the one from two minutes ago". The duration
    # is stated only when this call actually computed it; when it did not, the
    # head's own timestamp is the honest answer and the panel drops the figure.
    #
    # Compared against the instant before the call rather than against a
    # tolerance: `generated_at` is stamped inside compute_report, so a fresh
    # report's is strictly after `started` and a memo hit's is strictly before.
    # No threshold to tune and no wrong answer on a fast vault.
    fresh = True
    try:
        made = _dt.datetime.fromisoformat(report.generated_at)
        fresh = made >= started
    except (TypeError, ValueError):
        pass  # an unparseable stamp is not a reason to drop the duration
    head = {
        "at": report.generated_at,
        "elapsed_s": round(elapsed_s, 2) if fresh else None,
        "notes": (report.totals or {}).get("notes", 0),
        "depth": payload["depth"],
        "signals": {},
        "previous": None,
        "since": None,
    }
    try:
        signals = signals_of(report.totals or {}, areas)
        head["signals"] = signals
        if CONFIG.vault_path:
            prev = record_report(CONFIG.vault_path, signals)
            if prev:
                head["previous"] = prev.get("signals")
                head["since"] = prev.get("at")
    except Exception as exc:
        logger.debug("metrics: report history skipped (%s)", exc)
    return head


@app.get("/metrics")
def metrics(proposals: bool = False):
    """Everything the L1 graph report measures, as JSON for the metrics tab.

    Two depths, because the co-occurrence leg costs an order of magnitude more
    than the rest and, unlike the rest, grows with the square of the vault
    (_compute_cooccur_delta ranks every note against every other):

      default          — analytics + embeddings (~2s on a 686-note vault).
                         `depth: "structural"`.
      ?proposals=1     — adds the co-occurrence delta (autolink candidates,
                         stale links, missing hubs, integration deficits).
                         ~7s on the same vault. `depth: "full"`.

    The depth rides in the payload because E(vault) is only comparable across
    reports built at the same depth (see vault_energy): its `deficits` term is
    zero without the co-occurrence leg, and on a real vault that term dominates.
    The client labels the number rather than letting two different E's look alike.
    """
    import datetime as _datetime
    from collections import Counter

    from silica.kernel.report.graph_report import compute_report
    from silica.kernel.report.vault_energy import vault_energy

    t0, started = time.perf_counter(), _datetime.datetime.now(_datetime.timezone.utc)
    try:
        report = compute_report(
            analytics=True, with_embeddings=True, with_cooccurrence=proposals, top_k=20,
        )
    except Exception as exc:
        logger.warning("metrics: report failed (%s)", exc)
        return _failure(exc)
    elapsed_s = time.perf_counter() - t0

    e = vault_energy(report)
    short = lambda nid: note_parts.clean_name(nid or "")  # noqa: E731  one name with /shape: no folder, no .md
    # An area is named by its hub note's *name*: the full store path is a folder
    # tree, and in a table cell it wraps to three lines and says nothing extra.
    label = {c.cluster_id: (short(c.hub) or f"#{c.cluster_id}") for c in report.clusters}
    size = {c.cluster_id: c.size for c in report.clusters}

    payload = {
        "path": CONFIG.vault_path or "",
        "generated_at": report.generated_at,
        "depth": "full" if proposals else "structural",
        "totals": report.totals,
        "discourse_state": report.discourse_state,
        "energy": {
            "total": round(e.total, 2),
            # Ordered as E is composed: the one negative (bond-forming) term
            # first, then the entropic costs. `deficits` is dropped rather than
            # printed as 0.00 when the leg that measures it never ran — a zero
            # would read as "measured, came out flat". It contributes 0.0 either
            # way, so the terms still sum to `total`.
            "terms": [
                {"name": "cohesion", "value": round(e.cohesion, 2)},
                {"name": "orphans", "value": round(e.orphans, 2)},
                {"name": "dangling", "value": round(e.dangling, 2)},
                {"name": "gaps", "value": round(e.gaps, 2)},
                *([{"name": "deficits", "value": round(e.deficits, 2)}] if proposals else []),
                {"name": "contested", "value": round(e.contested, 2)},
            ],
        },
        "degree_histogram": vault_shape.degree_histogram(report.degree_map),
        "clusters": [
            {"id": c.cluster_id, "size": c.size, "hub": short(c.hub), "path": c.hub,
             "cohesion": c.cohesion}
            for c in sorted(report.clusters, key=lambda c: -c.size)
        ],
        "hubs": [
            {"label": n.label, "path": n.id, "area": label.get(n.cluster, f"#{n.cluster}"),
             "degree": n.degree, "in": n.in_degree, "out": n.out_degree,
             "betweenness": n.betweenness}
            for n in report.god_nodes
        ],
        "bridges": [
            {"source": short(b.source), "target": short(b.target),
             "source_path": b.source, "target_path": b.target,
             "links_between": b.links_between, "joined": b.joined}
            for b in report.bridges
        ],
        # The two area sizes ride along because they *are* the ranking:
        # gap_score = size_a * size_b / (1 + inter_edges). gap_density is left
        # out — on a real vault it reads 99.7-100% on every row, and a column
        # that never varies cannot explain the order it is sitting in.
        "gaps": [
            {"a": short(g.hub_a), "b": short(g.hub_b), "a_path": g.hub_a, "b_path": g.hub_b,
             "inter_edges": g.inter_edges, "size_a": size.get(g.cluster_a, 0),
             "size_b": size.get(g.cluster_b, 0)}
            for g in report.structural_gaps
        ],
        "orphans": [{"label": short(p), "path": p} for p in report.orphans[:_METRICS_ROWS]],
        # A target is a row you act on, so it carries what it needs to be judged:
        # who asks for it. Three names and a count, because at 372px of evidence
        # pane the fourth name is what starts wrapping.
        "dangling": [
            {"target": d["target"], "refs": d["refs"],
             "from": [short(src) for src in d.get("sources", [])[:3]],
             "from_more": max(0, len(d.get("sources", [])) - 3)}
            for d in report.dangling[:_METRICS_ROWS]
        ],
        # The tail, over EVERY target and not the twelve above. The shape IS the
        # reading -- a handful of targets carry most of the references and the
        # rest are asked for once each, which is the difference between "write
        # twenty notes" and "stub four hundred" -- and it cannot be seen from a
        # top-12 slice, so it is summarised here rather than shipped as rows.
        "dangling_hist": [
            {"refs": refs, "targets": n}
            for refs, n in sorted(Counter(d["refs"] for d in report.dangling).items())
        ],
        "dangling_top_refs": sum(d["refs"] for d in report.dangling[:20]),
        "contested": [
            {"label": short(c.path), "path": c.path, "refs": c.refs} for c in report.contested
        ],
        "source_drift": [
            {"label": short(d.note), "path": d.note, "source": d.source}
            for d in report.source_drift[:_METRICS_ROWS]
        ],
        "attention": [
            {"label": short(a.path), "path": a.path, "days_idle": a.days_idle,
             "degree": a.degree, "misses": a.misses, "attempts": a.attempts,
             "score": round(a.score, 2)}
            for a in report.attention_candidates
        ],
        "deficits": [
            {"label": short(d.path), "path": d.path, "concepts": d.concepts,
             "degree": d.degree, "score": round(d.score, 2)}
            for d in report.integration_deficits
        ],
        # Confirmed first — those are the merge candidates; the borderline band
        # is only "link, don't merge". Neither list is capped by the report, and
        # on a real vault they run to the hundreds, so the slice happens here.
        "duplicates": ([
            {"a": short(d.source), "b": short(d.target), "a_path": d.source,
             "b_path": d.target, "score": d.score, "confirmed": True}
            for d in report.confirmed_duplicate_pairs
        ] + [
            {"a": short(d.source), "b": short(d.target), "a_path": d.source,
             "b_path": d.target, "score": d.score, "confirmed": False}
            for d in report.duplicate_pairs
        ])[:_METRICS_ROWS],
        # Sliced like every other uncapped list: the report caps the
        # co-occurrence leg at top_k, but the import-derived candidates
        # _compute_code_signals appends are exhaustive — 13k pairs on a
        # 400-note vault, which is a 4 MB payload and a card 390,000 px tall.
        # The true count rides in `totals`, so the cut list can't read as all.
        "autolinks": [
            {"a": short(a.source), "b": short(a.target), "a_path": a.source,
             "b_path": a.target, "weight": a.weight, "shared": a.shared[:4]}
            for a in report.autolink_candidates[:_METRICS_ROWS]
        ],
        "stale_links": [
            {"a": short(s.source), "b": short(s.target), "a_path": s.source, "b_path": s.target}
            for s in report.stale_links
        ],
        "missing_hubs": [
            {"concept": h.concept, "centrality": round(h.centrality, 3)}
            for h in report.missing_hubs
        ],
        # `chars` and the limit it is under, together: a bare "412" is a number
        # the reader has to be told the meaning of, and the pane cannot say
        # "under 600" for a row without knowing what 600 is.
        "lean_notes": [
            {"label": short(p), "path": p, "chars": report.lean_chars.get(p, 0)}
            for p in report.lean_notes[:_METRICS_ROWS]
        ],
        "lean_limit": _lean_limit(),
        # V6 (spec 2026-08-22) rides the co-occurrence depth, so it is absent
        # at structural depth rather than shipped empty: an empty list would
        # read as "measured, found nothing".
        "bursting": [
            {"concept": b.concept, "z": round(b.z, 2), "recent": b.recent, "total": b.total}
            for b in report.bursting_concepts
        ],
        "temporal": (
            {
                "notes_scanned": report.temporal.notes_scanned,
                "by_tier": {str(k): v for k, v in report.temporal.by_tier.items()},
                "stamped": report.temporal.stamped,
                "superseded_notes": report.temporal.superseded_notes,
                "superseded_sections": report.temporal.superseded_sections,
                "oldest_valid_from": report.temporal.oldest_valid_from,
            }
            if report.temporal and report.temporal.notes_scanned
            else None
        ),
        "sessions": vault_shape.write_sessions(report),
        # The three readings the charts need that no single report can hold.
        # history is the only one that outlives a run: it is what turns four
        # tiles from a state into a direction.
        "history": _metrics_history(CONFIG.vault_path),
        "area_matrix": vault_shape.area_matrix(report),
        "signal_areas": vault_shape.signal_areas(report),
        "code_coverage": (
            {
                "documented": report.code_coverage.documented,
                "total": report.code_coverage.total,
                "undocumented": [
                    {"path": p, "fan_in": f}
                    for p, f in report.code_coverage.undocumented[:_METRICS_ROWS]
                ],
            }
            if report.code_coverage and report.code_coverage.total
            else None
        ),
    }
    payload["report"] = _report_head(report, payload, elapsed_s, started)
    return payload


@app.get("/graph")
def graph(request: Request):
    """The explore iframe's whole document, force-graph bundles inlined.

    That inlining is what makes this route ~6 MB, and it used to ship
    uncompressed with no validator at all, so every visit to the explore tab
    refetched every byte. Two cheap fixes, in this order:

      * an ETag over the payload, so a revisit costs a 304 and nothing else.
        Content-addressed rather than time-based, because the document changes
        when the vault does and no clock knows when that was.
      * gzip when the client asks for it, which measured 6,229,645 -> 766,371
        bytes on a 1,199-note vault. Done here rather than with GZipMiddleware,
        which would also wrap the SSE turn stream and stall it.

    no-cache, not no-store: the browser must revalidate (the vault changes under
    it) but is allowed to keep the bytes and take the 304.
    """
    import gzip as _gzip
    import hashlib
    import tempfile

    from silica.tools.graph import graph_export

    # Per-request directory, not a fixed name in /tmp: the route is a sync def,
    # so two overlapping requests ran in the threadpool and shared one file — and
    # a world-writable path anyone can pre-create as a symlink is not somewhere
    # to write a document we then serve back.
    try:
        with tempfile.TemporaryDirectory(prefix="silica-graph-") as tmp:
            out = Path(tmp) / "graph.html"
            graph_export(output_path=str(out), folder="")
            body = out.read_text(encoding="utf-8").encode("utf-8")
    except Exception as exc:
        return HTMLResponse(
            f"<p style='font-family:monospace'>graph unavailable: {_html.escape(_failure(exc)['error'])}"
            f"<br><small>{_html.escape(_failure(exc)['detail'])}</small></p>"
        )

    etag = '"' + hashlib.blake2b(body, digest_size=8).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        body = _gzip.compress(body, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(body, media_type="text/html; charset=utf-8", headers=headers)


# A note lights its degree over the layers on show: 86 at most on the 730-note
# test vault. The bound is for a body no frame would send, since the placement
# checks each note against every one placed before it.
_MAP_LAYOUT_MAX = 2000


@app.post("/map/layout")
def map_layout(payload: dict):
    """The graph frame's lit set, arranged as the Map (ADR-0088).

    The frame sends what it lit and the community each dot is painted with, so
    the set is the graph's and a wedge cannot disagree with its dots; only the
    geometry is computed here, by the builder the .canvas export uses. POST
    because a hub lights dozens of paths; guarded like a write because it reads
    the vault on another page's say-so.
    """
    from silica.kernel.graph.mindmap import arrange_in_vault

    root, ids, groups = payload.get("root"), payload.get("ids"), payload.get("groups") or {}
    if not isinstance(root, str) or not isinstance(ids, list) or not isinstance(groups, dict):
        raise HTTPException(400, "expected {root: str, ids: [str], groups: {id: int}}")
    community_of = {k: v for k, v in groups.items()
                    if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)}
    lit = [i for i in ids if isinstance(i, str)][:_MAP_LAYOUT_MAX]
    return arrange_in_vault(root, lit, community_of)


@app.get("/link")
def link_written(request: Request):
    """Where a clicked link is written (ADR-0152): the note, the line, its words.

    A wikilink edge is one per pair, so the line may be in either end. Only
    keys in the vault index resolve, which keeps a path out of the vault out of
    the read, as /note does. Asked per click and not shipped on the export:
    1,263 lines on Cultura would ride every graph for one look.
    """
    from silica.kernel.graph.mindmap import note_resolver

    resolve = note_resolver()
    a, b = (resolve(request.query_params.get(k, "")) for k in ("from", "to"))
    if not a or not b:
        return {"path": "", "line": 0, "text": ""}
    for src, target in ((a, b), (b, a)):
        line, text = note_context.link_line(src, target)
        if line:
            return {"path": src, "line": line, "text": text}
    return {"path": "", "line": 0, "text": ""}


@app.get("/path")
def prereq_path(note: str = ""):
    """The prerequisite ladder around one note (V2, RefD): read-order space.

    Flow reads this (ADR-0152), because the other surfaces cannot show
    DIRECTION: the graph and the Map lay notes out by how they connect, Clusters
    by area, Folders by where they are filed. All are undirected, and "what do
    I read before this" has no answer in any of them.

    Empty `note` returns the landing instead of an error, like /map: the notes
    that root the biggest ladders, which is the one list that cannot be got
    from the file tree.
    """
    from silica.kernel.graph.mindmap import note_resolver
    from silica.kernel.report.structure import ladder, structure_map

    try:
        m = structure_map()
    except Exception as exc:
        logger.warning("path: structure map failed (%s)", exc)
        return _failure(exc)
    if not m.prereq:
        return {
            "picks": [],
            # The one thing that leaves Flow empty, said by Flow itself rather
            # than as a bare graph: RefD needs the co-occurrence
            # index, and a vault that never built one has no reading order to
            # show, not a reading order of length zero.
            "hint": "No reading order yet: the prerequisite direction is derived from the "
                    "co-occurrence index. Run /report to build it.",
        }

    if not note.strip():
        # Ranked by how much of a ladder each note actually roots. Computing the
        # real ladder for every candidate would be O(V) walks, so the rank is
        # the cheap proxy (own prerequisites + own dependents) and the top slice
        # is then measured for real, which is what the count on the row says.
        rough = sorted(
            m.prereq.keys() | m.unlocks.keys(),
            key=lambda k: (-(len(m.prereq.get(k, ())) + len(m.unlocks.get(k, ()))), k),
        )[:40]
        picks: list[dict[str, Any]] = []
        for k in rough:
            l = ladder(k + ".md")
            if len(l["nodes"]) < 2:
                continue
            picks.append({
                "name": note_parts.clean_name(k), "path": k + ".md",
                "notes": len(l["nodes"]),
                "before": sum(1 for n in l["nodes"] if n["depth"] < 0),
                "after": sum(1 for n in l["nodes"] if n["depth"] > 0),
            })
        # A note with notes on BOTH sides is the instructive root: it is the one
        # whose ladder shows an order rather than a fan. Ranking by size alone
        # put the vault's biggest hub first every time, and its ladder is one
        # rung of 59 dependents, which is a list with a title.
        picks.sort(key=lambda r: (-min(r["before"], r["after"]), -r["notes"], r["name"]))
        return {"picks": picks[:12], "hint": ""}

    # `ladder` resolves its own root and hands the graph id back, so the two
    # keyspaces (graph ids carry `.md`, RefD works without it) are bridged once,
    # inside the kernel, and never here.
    resolve = note_resolver()
    l = ladder(resolve(note) or note)
    canon = l["root"]
    if not canon:
        return {"error": f"'{note}' not found in vault."}
    if not l["nodes"]:
        return {
            "root": note_context.row(canon), "levels": [], "edges": [], "cycles": 0, "truncated": False,
            "hint": "nothing reads before or after this note: RefD found no direction "
                    "between it and its neighbours.",
        }
    by_depth: dict[int, list[dict]] = {}
    for n in l["nodes"]:
        by_depth.setdefault(n["depth"], []).append(
            {**note_context.row(n["path"]), "cyclic": n["cyclic"], "root": n["path"] == canon}
        )
    levels = [
        {"depth": d, "notes": sorted(by_depth[d], key=lambda r: r["name"])}
        for d in sorted(by_depth)
    ]
    return {
        "root": note_context.row(canon), "levels": levels, "edges": l["edges"],
        "cycles": l["cycles"], "truncated": l["truncated"], "hint": "",
    }


@app.get("/find")
def find(q: str = "", k: int = 5):
    """Direct semantic-search panel: calls the tool straight, same pattern as /graph and /map."""
    # The core and not the tool, whose payload drops `score` for the model
    # (graph._without_score): this panel shows it to the person.
    from silica.tools.graph import _facade_search

    q = q.strip()
    if not q:
        return HTMLResponse("<p style='font-family:monospace;color:#8a93a3'>usage: /find &lt;query&gt; [--k=N]</p>")
    try:
        parsed = _facade_search(q, k=k)
    except Exception as exc:
        return HTMLResponse(
            f"<p style='font-family:monospace'>find unavailable: {_html.escape(_failure(exc)['error'])}"
            f"<br><small>{_html.escape(_failure(exc)['detail'])}</small></p>"
        )
    if "error" in parsed:
        return HTMLResponse(f"<p style='font-family:monospace;color:#8a93a3'>{_html.escape(parsed['error'])}</p>")
    results = parsed.get("results", [])
    if not results:
        return HTMLResponse(f"<p style='font-family:monospace;color:#8a93a3'>no results for '{_html.escape(q)}'.</p>")
    rows = []
    for r in results:
        p = r.get("path") or r.get("name") or "?"
        rows.append(
            f'<div class="find-result">{render.anchor(p, note_parts.clean_name(p))}'
            f'<span class="find-score">{r.get("score", 0.0):.3f}</span></div>'
        )
    return HTMLResponse("".join(rows))


@app.get("/note")
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


@app.post("/note/let-go")
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


@app.get("/note/raw")
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


@app.get("/raw")
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


@app.post("/note/save")
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


@app.post("/note/new")
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
    if folder and not all(_tree_name_ok(seg) for seg in folder.split("/")):
        raise HTTPException(400, _BAD_NAME_WHY)
    if name and not _tree_name_ok(name) or not name and not text:
        raise HTTPException(400, _BAD_NAME_WHY)
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise HTTPException(400, f"a name is at most {MAX_NAME_BYTES} bytes")
    if folder and publish.never(f"{folder}/x.md"):
        raise HTTPException(400, f"{folder} is Silica's own folder: write in another")
    _has_room(1, len(text.encode("utf-8")))

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
            raise HTTPException(400, _BAD_NAME_WHY) from None
    _index_later(path)
    landed = session_changes.current_text(path)
    landed = text if landed is None else landed
    other = next((p for p in DRIVER.notes_named(name) if p != path), None)
    return {"ok": True, "path": path, "name": name, "text": landed, "etag": _etag(landed), "run": run,
            "broken": _broken_links(landed, note_resolver()), **({"same_name": other} if other else {})}


@app.post("/note/asset")
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
    while chunk := await file.read(_UPLOAD_CHUNK):
        data.extend(chunk)
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(400, f"larger than {_MAX_IMAGE_BYTES // 2**20} MB")
    suffix = vi.image_suffix(bytes(data[:16]))
    if suffix is None:
        raise HTTPException(400, "not a PNG, JPEG, GIF or WebP image")
    await asyncio.to_thread(_has_room, 0, len(data))
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


@app.post("/tree/delete")
def tree_delete(payload: dict):
    """Delete a note, or every note the rail shows under a folder, from the
    tree row's menu (ADR-0123). The user's act, as /delete is (ADR-0092): no
    refusal for a linked note, journaled under user:/delete as ONE run, so
    the toast's undo, /undo and /revert each bring the whole set back.

    A folder is the notes the tree counts under it, from the builder the tree
    is drawn with, so the count the menu names is the count that goes; the
    folder's other files and the directory itself stay."""
    from silica.driver import DRIVER
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE
    from silica.tools.wrapped import delete_notes_with_restore_point

    note = str(payload.get("path") or "")
    folder = str(payload.get("folder") or "").strip().strip("/")
    many = payload.get("paths")
    if isinstance(many, list) and many:
        # the tree's selection (ADR-0126 point 7): one run, as a folder is
        paths = _tree_paths(many)
    elif note:
        paths = _tree_paths([note])
    elif folder:
        from silica.kernel.graph.graph_export import build_graph_data

        nodes, _edges = build_graph_data(folder=folder)
        paths = sorted(n["path"] for n in nodes if n.get("type") != "ghost" and n.get("path"))
        if not paths:
            raise HTTPException(404, "no notes in that folder")
    else:
        raise HTTPException(400, "name a note or a folder")
    # The turn's slot, as an import takes it: a turn reading or patching a
    # note this deletes would fail mid-answer on a file the person removed.
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: delete when it ends")
    try:
        res = delete_notes_with_restore_point(
            [(p, DRIVER.read_note(p).content) for p in paths], USER_DELETE_SOURCE)
    finally:
        SESSION.release()
    if not res["deleted"]:
        # The driver's text can carry the vault's absolute path, which the
        # hosted pilot does not show its tenants (ADR-0085): logged, not sent.
        logger.warning("tree delete refused: %s", res.get("error"))
        raise HTTPException(409, "not deleted: the vault refused the delete")
    if "error" in res:
        logger.warning("tree delete stopped after %d of %d: %s",
                       len(res["deleted"]), len(paths), res["error"])
        res["error"] = f"{len(paths) - len(res['deleted'])} of {len(paths)} could not be deleted"
    return res


def _tree_paths(refs: list) -> list[str]:
    """The vault paths of the notes a tree request names, or 404 on the
    first it cannot resolve: the page sends the row's own id, so a miss is
    a note gone since the tree was drawn."""
    resolve = note_resolver()
    out = []
    for ref in refs:
        canon = resolve(str(ref))
        if not canon:
            raise HTTPException(404, "note not found in vault")
        out.append(canon)
    return out


def _has_room(notes: int, size: int) -> None:
    """A 400 past the vault's limits, the status `saved_page.save` reaches
    through /vault/save. A laptop sets none, so this passes there."""
    from silica.sources import saved_page

    short = saved_page.room(notes, size)["short"]
    if short:
        raise HTTPException(400, f"this vault has no room for that: {short}")


def _tree_name_ok(name: str) -> bool:
    """A note's or a folder's name the tree may write: a workspace's rule
    (ADR-0112), and no leading dot, which the index reads as hidden."""
    from silica.kernel.workspaces import _BAD_NAME

    return bool(name) and not _BAD_NAME.search(name) and not name.startswith(".")


_BAD_NAME_WHY = "a name cannot be empty, start with a dot or hold / \\ : * ? \" < > | # ^ [ ]"


@app.post("/tree/move")
def tree_move(payload: dict):
    """A selection dropped on a folder, `{paths, folder}`, or a note renamed
    in its row, `{path, name}` (ADR-0126 points 3 and 8). Each note keeps its
    name under `folder` (`""` is the vault root; a folder that does not exist
    is made); a note already there is skipped and counted. The user's act in
    ONE run under user:/move, as a tree delete is under user:/delete, so the
    toast's undo and /undo bring the whole set back.

    Refused before anything moves (409): a destination a note already holds,
    two notes of the set landing on one path, and a rename to a name another
    note carries anywhere in the vault, read without case, since the move
    writes the bare new name into every referrer and two notes of one name
    would send those links wherever the resolver's shortest path points. A
    change of case alone passes. Refused while a turn runs (409), by the slot
    an import takes. The driver's text for a refusal after the preflight can
    carry the vault's absolute path (ADR-0085): logged, not sent."""
    from silica.driver import DRIVER
    from silica.kernel.write.undo_journal import USER_MOVE_SOURCE
    from silica.tools.wrapped import linked_from, move_notes_with_restore_point, move_preflight

    skipped: list[str] = []
    if payload.get("name") is not None:
        refs = [payload["path"]] if payload.get("path") else list(payload.get("paths") or [])
        if len(refs) != 1:
            raise HTTPException(400, "a rename takes one note")
        name = " ".join(str(payload["name"]).split())
        if not _tree_name_ok(name):
            raise HTTPException(400, _BAD_NAME_WHY)
        canon = _tree_paths(refs)[0]
        folder = canon.rsplit("/", 1)[0] if "/" in canon else ""
        dst = f"{folder}/{name}.md" if folder else f"{name}.md"
        if dst == canon:
            return {"run": None, "moved": [], "skipped": [canon], "rewritten": [], "error": None}
        other = next((p for p in DRIVER.notes_named(name) if p != canon), None)
        by_title = note_resolver()(name)
        if other or by_title not in (None, canon):
            raise HTTPException(409, f"another note is called {name}: {other or by_title}")
        moves = [(canon, dst)]
    else:
        paths = payload.get("paths")
        if "folder" not in payload or not isinstance(paths, list) or not paths:
            raise HTTPException(400, "name the notes and the folder, or a note and its new name")
        folder = str(payload["folder"] or "").strip().strip("/")
        if folder and not all(_tree_name_ok(seg) for seg in folder.split("/")):
            raise HTTPException(400, _BAD_NAME_WHY)
        moves = []
        for canon in _tree_paths(paths):
            leaf = canon.rsplit("/", 1)[-1]
            dst = f"{folder}/{leaf}" if folder else leaf
            if dst == canon:
                skipped.append(canon)
            else:
                moves.append((canon, dst))
    why = move_preflight(moves)
    if why:
        raise HTTPException(409, why)
    if not moves:
        return {"run": None, "moved": [], "skipped": skipped, "rewritten": [], "error": None}
    # The notes whose links the move rewrites, read before it: the page
    # reloads the one it holds open, or the editor would save the old link back.
    own = {src for src, _ in moves}
    rewritten = sorted(set().union(*(linked_from(src) for src in own)) - own)
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: move when it ends")
    try:
        res = move_notes_with_restore_point(moves, USER_MOVE_SOURCE)
    finally:
        SESSION.release()
    if res["error"]:
        logger.warning("tree move stopped after %d of %d: %s", len(res["moved"]), len(moves), res["error"])
        res["error"] = f"{len(moves) - len(res['moved'])} of {len(moves)} could not be moved"
    return {"run": res["run"], "moved": [{"from": a, "to": b} for a, b in res["moved"]],
            "skipped": skipped, "rewritten": rewritten, "error": res["error"]}


@app.post("/tree/undo")
def tree_undo(payload: dict):
    """The toast's undo for /tree/delete, /tree/move and /note/new: the run
    back, every note in it. Was /tree/undo-delete until ADR-0126."""
    from silica.kernel.write.undo_journal import (USER_DELETE_SOURCE, USER_MOVE_SOURCE, USER_WRITE_SOURCE,
                                                  get_undo_journal, revert_run)

    run = str(payload.get("run") or "")
    info = get_undo_journal().run_info(run) if run else None
    # The id comes from the page: only what the person did here is this
    # button's to take back; any other run is /revert's, with its own report.
    if not info or info["source"] not in (USER_DELETE_SOURCE, USER_MOVE_SOURCE, USER_WRITE_SOURCE):
        raise HTTPException(404, "nothing to undo")
    if info["reverted_at"]:
        raise HTTPException(409, "already undone")
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: undo when it ends")
    try:
        res = revert_run(run)
    finally:
        SESSION.release()
    return {"restored": res["reverted"],
            "skipped": [s["path"] for s in res["skipped"] + res["errors"]]}


def _trash_items() -> list[dict]:
    """This vault's trash (ADR-0128), the vault spelled as a delete stamps it.
    `taken`: a note is at the item's path again, so its restore is refused."""
    from silica.config import CONFIG
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.tools.wrapped import _vault_file

    items = get_undo_journal().trashed((CONFIG.vault_path or "").strip() or None)
    for t in items:
        t["taken"] = _vault_file(t["path"]).exists()
    return items


def _trash_item(payload: dict) -> dict:
    """The item a request names by run and path, or 404: both come from the
    page, and only what this vault's trash lists is the request's to touch."""
    run, path = str(payload.get("run") or ""), str(payload.get("path") or "")
    item = next((t for t in _trash_items() if t["run"] == run and t["path"] == path), None)
    if item is None:
        raise HTTPException(404, "not in the trash")
    return item


@app.get("/trash")
def trash_list():
    """The notes deleted from this vault that nothing has brought back."""
    return {"items": _trash_items()}


@app.post("/trash/restore")
def trash_restore(payload: dict):
    """One note back from the trash, not the run it was deleted in."""
    from silica.kernel.write.undo_journal import restore_from_trash

    item = _trash_item(payload)
    if item["taken"]:
        raise HTTPException(409, "a note is at that path now")
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: restore when it ends")
    try:
        why = restore_from_trash(item)
    finally:
        SESSION.release()
    if why:
        # the driver's text can carry the vault's absolute path (ADR-0085)
        logger.warning("trash restore of %s refused: %s", item["path"], why)
        raise HTTPException(409, "the vault refused the restore")
    return {"restored": item["path"]}


@app.post("/trash/erase")
def trash_erase(payload: dict):
    """Delete for good one item, `{run, path}`, or the whole trash, `{all:
    true}`. No turn slot: it touches the journal and no note."""
    from silica.kernel.write.undo_journal import get_undo_journal

    items = _trash_items() if payload.get("all") is True else [_trash_item(payload)]
    journal = get_undo_journal()
    for t in items:
        journal.leave_trash(t["run"], t["path"])
    return {"erased": len(items)}


@app.post("/note/render")
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


@app.post("/note/check")
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
                    ev = await asyncio.wait_for(q.get(), _SSE_POLL_S)
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


# --- what this session changed (GET /changes, /changes/diff) ------------------
# The ledger and the tally are the driver's (silica.kernel.write.session_changes),
# shared with the REPL's /changes. All that is added here is a display name and
# the line-by-line rendering, which only a drawer needs.

_DIFF_CONTEXT = 3
# A hard line cap, tail dropped with a count — a decided constant, not a
# deferral: past a few hundred lines a diff stops being reviewable in a
# drawer and the note itself is one click away.
_MAX_DIFF_LINES = 800
# difflib opens every diff with a hunk header, but a gap marker only *means*
# something when lines were skipped above it — which is not the case when the
# first hunk starts at the top of the file (or at 0, for a create or a delete).
_HUNK_AT_TOP = re.compile(r"^@@ -[01](?:,\d+)? \+[01](?:,\d+)? @@")


def _review_of(path: str) -> str:
    """The judge's sentence on a note the soft gate landed with `review:` set
    (kernel/write/validate.py), or "". Read off the file each time: keep
    clears it and a second write may set it again, and the ledger holds
    neither."""
    text = session_changes.current_text(path)
    if not text:
        return ""
    props, _ = note_parts.split_frontmatter(text)
    return str((props or {}).get("review") or "").strip()


def _change_rows() -> list[dict]:
    from silica.kernel.write.checkpoints import get_checkpoint_store

    # `undo_deletes` is what a write card's button says: the ledger's
    # "created" is a first touch by this process, the click is /undo <path>,
    # which reads the checkpoint store (CheckpointStore.undo_removes).
    store = get_checkpoint_store()
    return [{**r, "name": note_parts.clean_name(r["path"]), "review": _review_of(r["path"]),
             "undo_deletes": store.undo_removes(r["path"])}
            for r in session_changes.rows()]


@app.get("/changes")
def changes():
    """Every note this session has changed, oldest first, each with the
    judge's `review` sentence when the gate was unsure (audit P4)."""
    return _change_rows()


@app.post("/note/check/keep")
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
    await asyncio.to_thread(_has_room, 1, len(quote) + len(claim))
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: keep it when it ends")
    try:
        return {"ok": True, "path": await asyncio.to_thread(keep_check, claim, url, quote, canon)}
    finally:
        SESSION.release()


@app.post("/changes/keep")
def changes_keep(payload: dict):
    """The person looked at a write under review and keeps it: `review:` comes
    off the frontmatter through the same overwrite path /undo uses, so the
    ledger sees it and the diff stays honest."""
    from silica.driver import DRIVER
    from silica.kernel.write.templates import drop_props

    path = str(payload.get("path", ""))
    text = session_changes.current_text(path) if path else None
    if text is None:
        raise HTTPException(status_code=404, detail="no such note in this session's changes")
    cleared = drop_props(text, ("review",))
    if cleared != text:
        DRIVER.overwrite(path, cleared)
    return {"ok": True, "path": path}


@app.get("/changes/diff")
def changes_diff(path: str = ""):
    """One note's diff as flat rows: `-` removed, `+` added, ` ` context, `@` gap."""
    base = session_changes.snapshot().get(path)
    if base is None:
        # No baseline: this session never touched the note, so there is no diff
        # to read and the caller should show the note itself. Distinct from a
        # note that WAS touched and is now byte-identical again, which has a
        # baseline and an empty line list — the drawer says so in its own words,
        # and a write card must not silently degrade one into the other.
        return {"path": path, "name": note_parts.clean_name(path), "kind": "unchanged",
                "baseline": False, "lines": []}
    before, after = base.before or "", session_changes.current_text(path)
    added, removed = session_changes.tally(before, after or "")
    rows = _diff_rows(before, after or "")
    return {
        "path": path,
        "name": note_parts.clean_name(path),
        "kind": session_changes.kind(base.before, after, base.origin, bool(added or removed)),
        "from": base.origin,
        "baseline": True,
        "added": added,
        "removed": removed,
        "lines": rows[:_MAX_DIFF_LINES],
        "clipped": max(0, len(rows) - _MAX_DIFF_LINES),
    }


def _diff_rows(before: str, after: str) -> list[dict]:
    """Flat diff rows: `-` removed, `+` added, ` ` context, `@` gap."""
    import difflib

    rows: list[dict] = []
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                lineterm="", n=_DIFF_CONTEXT)
    for i, ln in enumerate(diff):
        if i < 2:
            continue  # the ---/+++ file headers difflib always emits first
        if ln.startswith("@@"):
            if not rows and _HUNK_AT_TOP.match(ln):
                continue  # nothing was skipped above the first line
            rows.append({"op": "@", "text": ""})
            continue
        rows.append({"op": ln[:1] or " ", "text": ln[1:]})
    return rows


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


@app.get("/note/history")
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


@app.get("/note/history/diff")
def note_history_diff(path: str = "", id: str = ""):
    """One past version against the note as it is now: `-` is what restoring
    it would take out of the note, `+` what it would bring back."""
    from silica.driver import DRIVER

    note = _history_note(path)
    body = _history_body(note, id)
    current = DRIVER.read_note(note).content
    rows = _diff_rows(current, body)
    added, removed = session_changes.tally(current, body)
    return {"path": note, "id": id, "added": added, "removed": removed,
            "lines": rows[:_MAX_DIFF_LINES], "clipped": max(0, len(rows) - _MAX_DIFF_LINES)}


@app.post("/note/history/restore")
def note_history_restore(payload: dict):
    """Write one past version back as the note's body. A restore is a write
    like any other: it is pushed as a version, so /undo takes it back."""
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import push_landed
    from silica.kernel.write.preserve import Loss

    note = _history_note(str(payload.get("path", "")))
    version = str(payload.get("id", ""))
    body = _history_body(note, version)
    if not SESSION.claim():
        raise HTTPException(409, "a turn is running: restore when it ends")
    try:
        prior = DRIVER.read_note(note).content
        DRIVER.overwrite(note, body, loss=Loss.RESTORE)
        push_landed(note, prior, body, kind="restore", of=version)
    finally:
        SESSION.release()
    return {"restored": note, "id": payload.get("id")}


def _note_concepts(target: str, k: int = 18) -> list[dict]:
    from silica.tools.graph import silica_concepts

    try:
        return silica_concepts(note=target, k=k).get("concepts") or []
    except Exception:
        logger.debug("context: concepts failed for %s", target, exc_info=True)
        return []


def _ghost_context(name: str) -> dict:
    """An unresolved wikilink as a subject of its own.

    Today a ghost node carries path "" (graph_export.py), so clicking one used to
    post an empty path and open nothing. It has no body to read and no reader
    mode — what it does have is a name, the notes that invoke it, and their
    merged concepts, which is exactly the material for deciding whether to write
    it.
    """
    from collections import Counter

    stem = note_parts.clean_name(name).lower()
    invokers = sorted({
        link.source.path for link in note_context.unresolved_links()
        if note_parts.clean_name(link.target).lower() == stem and link.source.path
    })
    merged: Counter = Counter()
    for src in invokers[:12]:  # decided cap: a 12-note cloud is already dense
        for c in _note_concepts(src, k=12):
            merged[c["concept"]] += c.get("weight", 1)
    return {
        "title": note_parts.clean_name(name),
        "path": "",
        "ghost": True,
        "snippets": [],
        "concepts": [{"concept": c, "weight": w} for c, w in merged.most_common(18)],
        "related": {"frontmatter": [], "outgoing": [], "backlinks": [note_context.row(p) for p in invokers]},
        "suggested": [],
        "hint": "",
    }


@app.get("/context")
def context(path: str = "", name: str = "", ghost: bool = False):
    """Everything deterministic the vault knows about one note, in one call.

    Sections: key snippets (what it says), concepts (what it is about), related
    (how it IS connected), suggested (how it SHOULD be). Zero LLM calls — every
    number here is index lookup, so the drawer is a read, not a turn. Graceful
    on miss, like /note: never 500.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef
    from silica.tools.graph import silica_related

    if ghost or (not path and name):
        return _ghost_context(name or path)

    resolve = note_resolver()
    canon = resolve(path)
    if not canon:
        return {"title": path, "path": path, "ghost": False, "error": "note not found in vault."}

    driver = get_driver()
    try:
        content = driver.read_note(NoteRef(name=note_parts.clean_name(canon), path=canon)).content
    except Exception:
        content = ""
    props, body = note_parts.split_frontmatter(content)

    def _refs(fn) -> list[dict]:
        # Resolved only. DRIVER.links() also returns a synthesised ref for every
        # UNRESOLVED wikilink (path "<Target>.md", a file that does not exist),
        # and listing those here would put a dead row under "how it IS
        # connected" — they belong under suggested, as links worth writing.
        try:
            return [note_context.row(p) for r in fn(canon) if (p := resolve(r.path or ""))]
        except Exception:
            logger.debug("context: %s failed for %s", fn.__name__, canon, exc_info=True)
            return []

    outgoing = _refs(driver.links)
    backlinks = _refs(driver.backlinks)

    # frontmatter `related:` is a hand-written claim, so it is shown as written
    # and resolved only for the click target; an unresolvable entry still lists.
    # The hand writing it is usually Obsidian's, which writes `- "[[B]]"` — and
    # neither resolve() nor _clean_name() sees through the brackets, so every
    # such entry used to come back named "[[B]]" with an empty path: a dead row
    # under a heading that promises a connection. _fm_ref splits the two jobs.
    fm_raw = (props or {}).get("related") or []
    if not isinstance(fm_raw, (list, tuple)):
        fm_raw = [fm_raw]
    frontmatter = []
    for v in fm_raw:
        if not v:
            continue
        target, display = note_context.fm_ref(str(v))
        frontmatter.append({"name": display, "path": resolve(target) or ""})

    try:
        rel_out = silica_related(note=canon, k=12)
    except Exception:
        logger.debug("context: related failed for %s", canon, exc_info=True)
        rel_out = {}
    rel = rel_out.get("results") or []

    linked = {r["path"] for r in outgoing} | {r["path"] for r in backlinks}
    # Whether the semantic leg contributed is READ OFF the ranking's own
    # provenance — every result names the metric that proposed it (embed:0.83,
    # cooccur:w9, edge:0.57). Asking the embed store directly would reach past
    # the relatedness facade for a fact the facade already reports.
    embed_ran = any(
        str(e).startswith("embed:") for r in rel for e in (r.get("evidence") or [])
    )

    # A Zotero paper's leaf carries its identity (ADR-0055): the panel heads it
    # with the paper's title, not the citekey stem, and adds its citations.
    paper = None
    if props and props.get("zotero_key"):
        from silica.ui.web.zotero_web import paper_context
        try:
            paper = paper_context(canon, props, backlinks)
        except Exception:
            logger.debug("context: paper block failed for %s", canon, exc_info=True)
    return {
        **({"paper": paper} if paper else {}),
        "title": str(props.get("title") or "") if paper and props and props.get("title") else note_parts.clean_name(canon),
        "path": canon,
        "ghost": False,
        # A paper's text before its first section is the byline, not a claim.
        "snippets": [s for s in note_context.key_snippets(body) if s["heading"] or not paper],
        "concepts": _note_concepts(canon),
        "related": {"frontmatter": frontmatter, "outgoing": outgoing, "backlinks": backlinks},
        "suggested": note_context.suggested(canon, rel, linked, resolve),
        "structure": note_context.note_structure(canon),
        "ladder": note_context.note_ladder(canon),
        # Without embeddings `related` ranks on co-occurrence alone, and the
        # section looks thin for a reason the reader cannot see from here.
        # silica_related's own hint wins when it has one — it knows more about
        # why it came back empty than an inference from the evidence can.
        "hint": rel_out.get("hint") or ("" if embed_ran else
                "no embedding index — relatedness is co-occurrence only "
                "(run /embed to add the semantic half)"),
    }


@app.get("/concept")
def concept(term: str = "", k: int = 20):
    """The notes that carry one concept — the click target of the context
    drawer's cloud, which lights them all in the graph at once. Paths are
    resolved back to graph keys so the ids match the nodes the viewer holds."""
    from silica.tools.graph import silica_concepts

    resolve = note_resolver()
    try:
        res = silica_concepts(term=term, k=k)
    except Exception as exc:
        logger.debug("concept: lookup failed for %s", term, exc_info=True)
        return {"term": term, "notes": [], **_failure(exc)}
    return {
        "term": term,
        "concept": res.get("concept") or term,
        "notes": [
            p for n in (res.get("notes") or [])
            if (p := resolve(n.get("path", "")) or n.get("path", ""))
        ],
    }


@app.get("/concepts")
def concepts(terms: str = ""):
    """The notes that carry EVERY listed concept, with the lines that carry each.

    The co-occurrence store knows which notes hold a stem and nothing about
    where; the driver's search knows lines and nothing about stems. Joined
    here and not in the store, because positions would triple its size for
    one reader. Each term is searched as typed and as its canonical label,
    which covers the two spellings a pill can show and misses a third
    ("feedback" and "retroazione" are one stem and two searches).
    """
    from silica.driver import get_driver
    from silica.tools.graph import silica_concepts

    words = [t.strip() for t in terms.split(",") if t.strip()][:6]
    resolve = note_resolver()
    sets: list[set[str]] = []
    label: dict[str, str] = {}
    for w in words:
        try:
            # k bounds the notes returned; the intersection needs all of them.
            res = silica_concepts(term=w, k=100_000)
        except Exception as exc:
            logger.debug("concepts: lookup failed for %s", w, exc_info=True)
            return {"terms": [], "notes": [], **_failure(exc)}
        label[w] = res.get("concept") or w
        sets.append({p for n in (res.get("notes") or [])
                     if (p := resolve(n.get("path", "")) or n.get("path", ""))})
    common = set.intersection(*sets) if sets else set()
    passages: dict[str, list[dict]] = {}
    if common:
        queries = {q: w for w in words for q in (w, label[w])}
        try:
            hits = get_driver().search_context_batch(sorted(queries))
        except Exception:
            # No lines is a poorer answer, not a wrong one: the notes still light.
            logger.debug("concepts: search failed", exc_info=True)
            hits = {}
        # One entry per line, naming every term it carries: the line that holds
        # two of them together is the one the reader came for, not a duplicate.
        by_line: dict[tuple[str, int], dict] = {}
        for q, hs in hits.items():
            for h in hs:
                path = resolve(h.ref.path) or h.ref.path
                text = note_context.plain(note_context.LIST_OR_QUOTE.sub("", h.snippet))[:240]
                # A one-token line is a tag or a frontmatter entry, not a sentence
                # ("- classificazione-statistica" carried half the hits on a
                # statistics note): the note still counts, the line is not quoted.
                if path not in common or " " not in text.strip():
                    continue
                row = by_line.setdefault((path, h.line), {"line": h.line, "text": text, "terms": []})
                if queries[q] not in row["terms"]:
                    row["terms"].append(queries[q])
        for (path, _line), row in by_line.items():
            passages.setdefault(path, []).append(row)
    notes = [{"path": p, "name": note_parts.clean_name(p),
              "passages": sorted(passages.get(p, []), key=lambda x: x["line"])[:8]}
             for p in common]
    notes.sort(key=lambda n: (-len(n["passages"]), n["name"]))
    return {"terms": [{"term": w, "concept": label[w], "notes": len(st)} for w, st in zip(words, sets)],
            "notes": notes}


def _select(nodes: list[dict], sel: str, edges: list | None = None
            ) -> tuple[set[str], str] | None:
    """`folder:<prefix>` | `area:<id>` | `collection:<name>` |
    `collections:<json list of names>` -> (note ids, label), or None.
    `collections:` with no list is every collection (ADR-0066 decision 5)."""
    kind, _, rest = sel.partition(":")
    real = [n for n in nodes if n.get("type") != "ghost"]
    if kind == "collections":
        try:
            names = set(json.loads(rest)) if rest else None
        except ValueError:
            return None
        papers = {n["id"] for n in real
                  if any(names is None or c in names
                         for c in ((n.get("paper") or {}).get("collections") or []))}
        notes = {e["from"] for e in (edges or [])
                 if e.get("type") == "EXTRACTED" and e.get("to") in papers}
        return papers | notes, ("all collections" if names is None else f"{len(names)} collections")
    if kind == "collection" and rest:
        # A Zotero collection (ADR-0055) is its paper leaves plus the notes that
        # cite them: a leaf links to nothing, so leaves alone could never meet.
        papers = {n["id"] for n in real
                  if rest in ((n.get("paper") or {}).get("collections") or [])}
        notes = {e["from"] for e in (edges or [])
                 if e.get("type") == "EXTRACTED" and e.get("to") in papers}
        return papers | notes, rest
    if kind == "folder" and rest:
        pre = rest if rest.endswith("/") else rest + "/"
        return {n["id"] for n in real if n["id"].startswith(pre)}, pre
    if kind == "area" and rest.lstrip("-").isdigit():
        from silica.kernel.graph.graph_export import area_hubs, area_tally

        gid = int(rest)
        ids = {n["id"] for n in real if n.get("group", -1) == gid}
        hub = area_hubs(area_tally(real, [])).get(gid, "")
        return ids, note_parts.clean_name(hub) if hub else f"area {gid}"
    return None


@app.get("/compare")
def compare(a: str = "", b: str = ""):
    """Two parts of the vault side by side (compare_sets): what characterises
    each, and the wikilinks and k-NN pairs that put them in touch, each link
    with the sentence that carries it. Selectors are the scope picker's:
    folder:<prefix>, area:<id>, collection:<name>."""
    from silica.config import CONFIG
    from silica.kernel.recall.compare import compare_sets
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.graph.graph_export import build_graph_data, detect_communities, knn_edges

    try:
        nodes, edges = build_graph_data(folder="")
        detect_communities(nodes, edges)
    except Exception as exc:
        return _failure(exc)
    sim: list[dict] = []
    try:
        sim = knn_edges(nodes, k=6)
    except Exception:
        logger.debug("compare: semantic layer unavailable", exc_info=True)
    if a.startswith("collection") or b.startswith("collection"):
        from silica.sources.citations import paper_layer
        from silica.ui.web.graph_view import _stamp_collections

        paper_layer(nodes)
        _stamp_collections(nodes)
    A, B = _select(nodes, a, edges), _select(nodes, b, edges)
    if not A or not B:
        return {"error": "selectors are folder:<prefix>, area:<id> or collection:<name>",
                "a": a, "b": b}
    store = None
    try:
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        if len(store) == 0:
            store = None
    except Exception:
        logger.debug("compare: co-occurrence store unavailable", exc_info=True)
    real_n = sum(1 for n in nodes if n.get("type") != "ghost")
    out = compare_sets(A[0], B[0], store=store, vault_size=real_n, nodes=nodes, edges=edges, sim=sim)
    end = lambda p: {"name": note_parts.clean_name(p), "path": p}  # noqa: E731

    def scaffold(x: str, y: str) -> bool:
        return any(e.get("scaffold") and {e.get("from"), e.get("to")} == {x, y} for e in edges)

    return {
        "a": {"selector": a, "label": A[1], "size": out["a"]["size"], "terms": out["a"]["terms"], "paths": sorted(A[0])},
        "b": {"selector": b, "label": B[1], "size": out["b"]["size"], "terms": out["b"]["terms"], "paths": sorted(B[0])},
        "links": [{"a": end(x), "b": end(y), "passage": "" if scaffold(x, y) else note_context.bridge_passage(x, y) or note_context.bridge_passage(y, x)}
                  for x, y in out["links"]],
        "knn": [{"a": end(x), "b": end(y)} for x, y in out["knn"]],
    }


@app.get("/asset")
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


def _tree_raws() -> dict[str, list[str]]:
    """The raws the rail lists where they sit (ADR-0129, amended 2026-11-04),
    each with the notes naming it, from the notes' `source_file` and never
    from the disk: an embedded image or a figure Silica extracted is no raw."""
    from silica.kernel import raws
    from silica.sources.convert import DOC_EXTS

    if not CONFIG.vault_path:
        return {}
    return {r: n for r, n in raws.index(Path(CONFIG.vault_path)).items()
            if Path(r).suffix.lower() in DOC_EXTS}


@app.get("/vault_info")
def vault_info():
    """Sidebar data: vault stats + file tree, from the same builders as the
    graph view so the numbers can't disagree between the two surfaces."""
    from silica.kernel.check.web import is_open as check_web_open
    from silica.kernel.publish import never_dirs
    from silica.kernel.graph.graph_export import build_graph_data
    from silica.ui.web.graph_view import render_tree

    try:
        nodes, edges = build_graph_data(folder="")
    except Exception as exc:
        return _failure(exc)
    return {
        # `path` so the header label follows a `/vault <dir>` switch mid-session:
        # this endpoint already re-runs after every turn, the boot-time header
        # never did.
        "path": CONFIG.vault_path or "",
        "unresolved": sum(1 for n in nodes if n.get("type") == "ghost"),
        # actions=True: the rail's tree carries a pin per note. The graph
        # frame renders the same tree without one, because a pin there would
        # point at a rail that document does not have.
        "tree": render_tree(nodes, actions=True, raws=_tree_raws()),
        # Silica's own folders: no folder in view and no "Write a note" (ADR-0155
        # points 4, 5). The page asks here because it has the tree from here.
        "never": list(never_dirs()),
        "check_web": check_web_open(),
        # The chat landing's ways to start (ADR-0048, amendment 2026-10-22):
        # the notes the vault links most. The counts, the area count and the
        # topic labels the landing stated before the greeting went with it,
        # and with them the community pass this endpoint ran after every turn.
        "hubs": brief.top_hubs(nodes, edges),
    }


@app.get("/limits")
def limits():
    """How much of each usage limit is used and when it resets (ADR-0102
    point 7): `{"limits": [{layer, used, resets_at}]}`, the window first, then
    the week and the month, `used` a share from 0 to 1 and never a currency. An
    empty list off hosted, where the key has no limit or the model is not on
    OpenRouter.
    Sync, so FastAPI runs the read of the key on its thread pool."""
    from silica.model.usage_limits import status

    return {"limits": status(CONFIG.model or "")}


@app.get("/vault_brief")
def vault_brief(refresh: int = 0):
    """One sentence about what the vault holds, written by the worker model.

    `stamp` is the corpus shape the sentence was written against, so a vault
    that grew gets a new sentence and a vault that only sat there replays the
    one on disk. A failure here is never an error: the agent describes a vault
    without it, from its name and its coverage.
    """
    if not CONFIG.vault_brief:
        return {"enabled": False, "text": ""}

    from silica.kernel.graph.graph_export import build_graph_data, detect_communities

    try:
        nodes, edges = build_graph_data(folder="")
        communities = detect_communities(nodes, edges)
    except Exception as exc:
        return {"enabled": True, "text": "", **_failure(exc)}

    notes = sum(1 for n in nodes if n.get("type") != "ghost")
    topics = [c.label for c in communities if c.size > 1][:8]
    stamp = f"{notes}|" + "|".join(topics)

    cache = brief.brief_path()
    if not refresh:
        try:
            got = json.loads(cache.read_text(encoding="utf-8"))
            if got.get("stamp") == stamp and got.get("text"):
                return {"enabled": True, "text": got["text"], "cached": True}
        except Exception:
            pass  # no cache, unreadable cache, or a stamp from an older shape

    hubs = [h["name"] for h in brief.top_hubs(nodes, edges, top_n=12)]
    text = brief.write_brief(notes, topics, hubs)
    if not text:
        return {"enabled": True, "text": ""}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "text": text}), encoding="utf-8")
    except Exception:
        pass  # an uncacheable brief is still a usable brief
    return {"enabled": True, "text": text}


@app.get("/messages")
def get_messages():
    messages = SESSION.chat.messages
    resolve = note_resolver()
    # A call whose result carries an error must not replay as a tick: the one
    # place the user checks whether a write landed is this transcript.
    failed = {
        m["tool_call_id"] for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") and _is_tool_failure(m.get("content"))
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
    sid = _plain_sid(SESSION.chat.sid) if hosted() and not demo() else ""
    if sid:
        seqs = _user_turn_seqs(_session_beats(sid))
        users = [m for m in messages if m.get("role") == "user"]
        if len(users) == len(seqs):
            done = _flagged(sid)
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
            from silica.agent.mentions import strip

            content = strip(content)
        if m["role"] == "user" and isinstance(content, str) and "[image attached: " in content:
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
            files = _files_shown(m.get("silica_files"))
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


@app.get("/narration")
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


# The GUI's own uvicorn server, so an endless response can ask whether the
# process is leaving. None under TestClient and in the suite, where nothing is
# shutting down and every stream is read to its end by its caller.
_SSE_POLL_S = 0.25   # four wakeups a second per open tab, against a 1s deadline


def _beat_for_tab(rec: dict) -> bool:
    """A phone turn's beats stay off the browser's live stream (ADR-0089
    amendment): the work panel adopts any new sid and would drop the browser's
    run, and their seqs would move its Last-Event-ID cursor. Unless the browser
    itself has the phone's chat open."""
    return not (SESSION.phone.sid and rec.get("sid") == SESSION.phone.sid and SESSION.phone.sid != SESSION.chat.sid)


@app.get("/narration/sse")
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
                    rec = await asyncio.wait_for(q.get(), _SSE_POLL_S)
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


@app.get("/sessions")
def list_sessions():
    # Current id rides a header so the body stays a plain list (matches /messages).
    # `live` marks the one chat a turn is running in, so the list can show a
    # working dot on it from any other tab (audit P18).
    rows = [{**r, "live": bool(SESSION.busy and r.get("id") == SESSION.chat.sid)}
            for r in session.list_sessions()]
    return JSONResponse(rows, headers={"X-Silica-Session": SESSION.chat.sid or ""})


@app.post("/session/load")
def load_session(payload: dict):
    if SESSION.busy:
        raise HTTPException(status_code=409, detail="a turn is already in progress")
    from silica.cli import _update_context_tokens
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
            raise HTTPException(status_code=409, detail=str(e)) from None
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
    _update_context_tokens(SESSION.chat.messages)
    return {"ok": True}


@app.post("/session/rename")
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
        raise HTTPException(status_code=409, detail=str(e)) from None
    if not ok:
        raise HTTPException(status_code=404, detail="no such session")
    return {"ok": True, "title": title}


@app.post("/session/delete")
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
        raise HTTPException(status_code=409, detail=str(e)) from None
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


def _session_beats(sid: str) -> list[dict]:
    from silica.obs import narration as _narr

    return list(_narr.read_beats(_narr.narration_dir() / f"{sid}.jsonl"))


def _user_turn_seqs(beats: list[dict]) -> list[int]:
    """The seqs of the user `turn` beats, in file order."""
    from silica.obs.narration import REWIND

    out = []
    for b in beats:
        p = b.get("payload")
        m = p.get("message") if isinstance(p, dict) else None
        if b.get("kind") == "turn" and isinstance(m, dict) and m.get("role") == "user" and b.get("seq"):
            out.append(b["seq"])
        elif b.get("kind") == REWIND and out:
            out.pop()   # the question asked again left the conversation (ADR-0121 point 7)
    return out


def _flagged(sid: str) -> set[int]:
    """The answers of `sid` already flagged, so a reopened chat says so and a
    second click is not a second flag."""
    from silica.config import silica_home

    try:
        lines = (silica_home() / "flags.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    out = set()
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue  # a torn last line from a full disk: the other flags still count
        if isinstance(rec, dict) and rec.get("session") == sid and isinstance(rec.get("answer"), int):
            out.add(rec["answer"])
    return out


def _plain_sid(sid: object) -> str:
    """A session id as a file stem, or "" when it could name anything else."""
    s = str(sid or "")
    return s if s.isascii() and s.isalnum() else ""


def _session_head(sid: str | None) -> int:
    """The last seq written to `sid`, 0 for a session not yet opened."""
    sid = _plain_sid(sid)
    return max((b.get("seq") or 0 for b in _session_beats(sid)), default=0) if sid else 0


def _turn_since(sid0: str | None, seq0: int) -> dict | None:
    """The turn a flag would point at: the first user beat narrated past where
    the session stood before the turn. None when this turn narrated none (the
    session was busy in another process), so no flag can point at a neighbour."""
    sid = _plain_sid(SESSION.chat.sid)
    if not sid:
        return None
    after = seq0 if sid == sid0 else 0
    seqs = [s for s in _user_turn_seqs(_session_beats(sid)) if s > after]
    return {"session": sid, "seq": seqs[0]} if seqs else None


@app.post("/flag")
def flag_answer(payload: dict):
    """Append one flag to `<home>/flags.jsonl`: time, session, the seq of the
    user beat that opened the turn, and the student's own line."""
    from silica.config import hosted
    from silica.config import silica_home

    if not hosted():
        raise HTTPException(status_code=404, detail="Not Found")
    sid = _plain_sid(payload.get("session"))
    if not sid:
        raise HTTPException(status_code=400, detail="that is not a chat id")
    seq = payload.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise HTTPException(status_code=400, detail="that is not an answer id")
    note = str(payload.get("note") or "").strip()
    if len(note) > _FLAG_NOTE_MAX:
        raise HTTPException(status_code=400, detail=f"keep the note under {_FLAG_NOTE_MAX} characters")
    if seq not in _user_turn_seqs(_session_beats(sid)):
        raise HTTPException(status_code=404, detail="that answer is not in this chat")
    if seq in _flagged(sid):
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


@app.post("/reset")
def reset():

    session.capture_own_session()
    SESSION.reset()
    return {"ok": True, "vault": CONFIG.vault_path}


@app.post("/stop")
def stop():
    if SESSION.cancel is not None:
        SESSION.cancel.set()
        from silica.obs import narration as _narr
        _narr.NARRATOR.cancel(driver="gui", target=None, scope="turn")
    return {"ok": True}


@app.get("/health")
def health(all: bool = False):
    """The doctor's findings — non-ok by default, everything with `?all=1`.

    A server the user forgot to start degrades recall silently here: the
    embedder/reranker warnings go to the launching terminal's stderr, which the
    browser never shows. Same checks as `silica doctor`, so the two surfaces
    cannot disagree; ok rows are dropped for the sidebar notice, which is for
    what needs fixing, and kept for the settings panel's diagnostics and for a
    bug report, which needs the passing rows just as much.
    """
    from silica.config import hosted
    from silica.onboarding.checks import run_checks

    # "session capture" tells the user to edit .claude/settings.json — a
    # Claude-Code-integration concern the browser can do nothing about. It
    # stays out of the sidebar notices; ?all=1 (diagnostics) keeps it.
    # "converters" too when hosted: it reports the host's binaries, which
    # host.env chose (pdfium, no mineru) and a tenant cannot install.
    hidden = {"session capture", *(("converters",) if hosted() else ())}
    return [
        {"name": r.name, "status": r.status, "detail": r.detail, "hint": r.hint}
        for r in run_checks(CONFIG)
        if all or (r.status != "ok" and r.name not in hidden)
    ]


# 180 s of 16 kHz mono 16-bit PCM is 5.76 MB, Learn's take ceiling (app-learn.js);
# the cap is generous enough for that and still refuses a body that was never a clip.
_STT_MAX_BYTES = 8 * 1024 * 1024


@app.get("/stt")
def stt_status(warm: bool = True):
    """Whether dictation can work, and which lane will do it, asked before the
    browser requests the mic.

    A probe, not a config flag: the dictation endpoint falls back to
    stt_base_url, which has a default, so "configured" and
    "listening" are different questions and only the second one is useful. Shares
    ensure_local_servers' readiness check, which knows that llama.cpp-family
    servers answer 503 while they load and that an open port therefore lies.

    `browser` and `lang` ride along for the page's last lane, the Web Speech API,
    which only runs when neither transcriber here answers, and for Learn's read
    aloud, which asks with `warm=0` (ADR-0049).
    """
    from silica.agent import stt as local
    from silica.config import hosted
    from silica.onboarding.serve import ready

    url, key = local.dictation_endpoint(CONFIG)
    base = {"url": url, "browser": CONFIG.stt_browser, "lang": local.speech_lang(CONFIG),
            "hosted": hosted()}
    if url and ready(url):
        return {**base, "ok": True, "via": "endpoint", "detail": ""}
    if local.has_local_stt():
        model = local.local_stt_model(CONFIG)
        cold = not local.loaded(model)
        if warm:
            local.warm(model)
        return {**base, "ok": True, "via": "local", "model": model, "cold": cold, "detail": ""}
    if base["hosted"]:
        # A tenant can set neither the key nor the extra, and a loopback URL
        # names nothing it can reach, so the sentence names neither.
        why = "the transcriber is not answering" if url else "this server does not transcribe dictation"
        return {**base, "ok": False, "via": "", "detail": why}
    why = f"nothing is answering at {url}" if url else f"{key} is empty"
    return {**base, "ok": False, "via": "", "detail": why + " and the [stt] extra is not installed"}


async def _stt_via_endpoint(url: str, clip: bytes) -> tuple[str, str]:
    """One clip through the configured /audio/transcriptions endpoint:
    `(text, "")`, or `("", the sentence the user reads)` so the route can try
    the in-process lane before it reports anything."""
    import httpx

    from silica.metrics import outcome_of, transcribed

    form = {"model": CONFIG.stt_model, "response_format": "json"}
    if CONFIG.stt_lang:
        form["language"] = CONFIG.stt_lang
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            for _ in range(2):
                resp = await client.post(
                    f"{url.rstrip('/')}/audio/transcriptions",
                    files={"file": ("clip.wav", clip, "audio/wav")},
                    data=form,
                    headers={"Authorization": f"Bearer {CONFIG.stt_api_key}"},
                )
                # whisper-server needs "auto" named, a hosted API rejects it
                # with 400 and detects the language itself without the field:
                # the same retry as convert._asr_via_endpoint.
                if resp.status_code != 400 or form.get("language") != "auto":
                    break
                del form["language"]
    except Exception as exc:
        transcribed("dictation", started, outcome_of(exc))
        return "", f"transcription endpoint unreachable: {exc}"
    if resp.status_code != 200:
        transcribed("dictation", started, str(resp.status_code))
        return "", f"transcription failed ({resp.status_code}): {resp.text[:200]}"
    try:
        payload = resp.json()
        text = (payload.get("text") or "").strip()
    except Exception as exc:
        transcribed("dictation", started, "malformed")
        return "", f"transcription endpoint returned no JSON: {exc}"
    transcribed("dictation", started, "ok" if text else "empty", payload)
    return text, ""


@app.post("/stt")
async def stt(audio: UploadFile = File(...)):
    """Transcribe one recorded clip: the endpoint when it answers, else in process.

    The browser sends 16 kHz mono WAV. MediaRecorder can only produce webm/opus,
    and whisper.cpp's server reads WAV unless it was built with ffmpeg, so the
    conversion happens in app-boot.js, where it costs no dependency on either side.

    Any endpoint failure falls through to the [stt] extra when it is installed:
    the default URL points at a server most installs never start, and a refused
    connection costs nothing. The reranker gave up its own in-process fallback
    (ADR-0073, 1.4 GB in every process); this lane's RAM is not measured yet.
    """
    from starlette.concurrency import run_in_threadpool

    from silica.agent import stt as local

    clip = await audio.read()
    if not clip:
        raise HTTPException(400, "empty recording")
    if len(clip) > _STT_MAX_BYTES:
        raise HTTPException(413, f"recording over {_STT_MAX_BYTES // (1024 * 1024)} MB")
    failed = ""
    url, _key = local.dictation_endpoint(CONFIG)
    if url:
        text, failed = await _stt_via_endpoint(url, clip)
        if not failed:
            return {"text": text, "via": "endpoint"}
    if not local.has_local_stt():
        if failed:
            from silica.config import hosted

            if hosted():
                # A spent allowance reaches dictation as OpenRouter's 403, whose
                # body is no sentence for a student: explain() names the layer
                # and its reset (ADR-0102 point 6). It reads the key, so off the loop.
                failed = (await asyncio.to_thread(_failure, failed))["error"]
            raise HTTPException(502, failed)
        raise HTTPException(503, "no transcription endpoint configured and the [stt] extra is not installed")
    try:
        # A thread, not the event loop: a CPU transcription of a long take runs
        # for seconds, and every other request (the narration stream included)
        # would stall behind it.
        text = await run_in_threadpool(
            local.transcribe_local, clip, lang=CONFIG.stt_lang, model=local.local_stt_model(CONFIG),
        )
    except Exception as exc:
        reasons = [r for r in (failed, f"in-process transcription failed: {exc}") if r]
        raise HTTPException(502, "; ".join(reasons)) from exc
    return {"text": text, "via": "local"}


@app.get("/config")
def get_config():
    """Session config for the header panel: the active model (read-only — Silica
    has no runtime model-switch op, so this mirrors the TUI's display-only
    /model) plus the one live toggle the web surfaces, thinking (/thinking)."""
    from silica.model.providers import model_limits

    window = 0
    if CONFIG.model:
        window, _ = model_limits(CONFIG.provider, CONFIG.model)
    return {
        "model": CONFIG.model or "",
        # Empty means "the chat model does the worker's job too" (every call site
        # falls back to CONFIG.model), and the reader decides what to do with it.
        "worker_model": CONFIG.worker_model or "",
        "provider": CONFIG.provider or "",
        "context_window": window or 0,
        "show_thinking": CONFIG.show_thinking,
    }


# --- settings panel ----------------------------------------------------------
# The write half of /config is absorbed here: `thinking` is a persisted row like
# any other now, not a session-only flip. /config stays as the header chip's
# cheap read — GET /settings probes four endpoints, which is seconds the header
# label must not wait for.


@app.get("/settings")
def get_settings():
    """Every admitted row: value, where the value came from, whether it is
    locked, and its suggestions. Plus what About needs, so opening the panel is
    one round trip."""
    from silica import __version__
    from silica.config import hosted
    from silica.onboarding.wizard import resolve_env_path
    from silica.ui.web import settings as st
    from silica.update import behind_count

    on_server = hosted()
    return {
        "env_path": st.short_path(resolve_env_path()),
        "busy": SESSION.busy,
        # No hosted row has a pick-list fed by /models (ADR-0104 point 3), so
        # the four probes would run on every opening of the panel for nothing.
        "sections": st.read_sections(probe=not on_server),
        "version": __version__,
        # A tenant cannot update itself; the page drops the line by `hosted`.
        "behind": 0 if on_server else behind_count(),
        "issues_url": "https://github.com/kiycoh/silica-agent/issues",
        "hosted": on_server,
    }


def _reject_if_busy_or_locked(key: str) -> None:
    """One rule for every write: nothing lands while a turn is running.

    Deliberately not a per-row list of what a turn reads — that list would rot
    at the first new tool and no test would catch it. The cost is waiting for a
    response to finish.
    """
    from silica.ui.web import settings as st

    if SESSION.busy:
        raise HTTPException(status_code=409, detail="a response is running")
    if st.locked(key):
        raise HTTPException(status_code=409, detail=f"defined in the environment ({key})")


@app.post("/settings")
def set_setting(payload: dict):
    """Apply one row: live in CONFIG, persisted in the .env that wins at boot."""
    from silica.ui.web import settings as st

    key = str(payload.get("key", ""))
    _reject_if_busy_or_locked(key)
    if key == st.VAULT_KEY or key in st.EMBED_KEYS:
        raise HTTPException(status_code=400, detail="this row goes through /settings/confirm")
    result = st.apply(key, payload.get("value", ""))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/settings/confirm")
async def confirm_setting(payload: dict):
    """The two rows that need a sequence, not an assignment: switching the vault
    and swapping the embedding model.

    Both are long enough to block, so they run off the event loop. The embedding
    swap does not repair itself: `sweep()` decides what to re-embed from mtimes,
    which a model change never touches, so stale vectors would be compared in an
    incompatible space in silence until something forces a full re-index.
    """
    from silica.config import hosted
    from silica.ui.web import settings as st

    key = str(payload.get("key", ""))
    value = str(payload.get("value", ""))
    if hosted():
        # Neither row is a hosted row (ADR-0104 point 1). Caddy refuses this
        # path too, but a vault switch skips apply's allow-list, so the refusal
        # must not rest on the front door alone.
        raise HTTPException(status_code=404, detail="not on this server")
    _reject_if_busy_or_locked(key)
    if key == st.VAULT_KEY:
        return await asyncio.to_thread(_apply_vault_switch, value)
    if key in st.EMBED_KEYS:
        return await asyncio.to_thread(_apply_embedding_swap, key, value)
    raise HTTPException(status_code=400, detail="this row does not need confirming")


def _apply_vault_switch(path: str) -> dict:
    from silica.cli import switch_vault
    from silica.ui.web import settings as st

    switched = switch_vault(path)
    if switched.error:
        return {"ok": False, "error": switched.error}
    # The Changes list describes paths in the vault we just left.
    session_changes.clear()
    # Live only, never persisted: a SILICA_VAULT line in the .env is ignored at
    # boot (config.load_user_env), the next launch curates the folder it starts
    # in. The resolved absolute path is what the caches were just rebuilt for.
    result = {"ok": True, "values": {st.VAULT_KEY: switched.vault}}
    # The fresh-session seed carries the old vault's map until it is rebuilt.
    session.prewarm_seed()
    notes = []
    if switched.write_dir:
        notes.append(f"writes confined to {switched.write_dir}/")
    if switched.invalid_write_dir:
        notes.append("vault.yaml declares an invalid write_dir — every write will be rejected")
    if switched.repo_warning:
        notes.append(switched.repo_warning)
    if switched.language_drift:
        notes.append(
            f"language {switched.language}, co-occurrence store frozen "
            f"{switched.store_language} — rebuild it with /cooccur --force"
        )
    return {**result, "vault": switched.vault, "notes": notes}


def _apply_embedding_swap(key: str, value: str) -> dict:
    from silica.tools import TOOLS
    from silica.ui.web import settings as st

    result = st.apply(key, value)
    if not result["ok"]:
        return result
    raw = TOOLS["silica_embed_refresh"].run(folder="", force=True)
    try:
        report = json.loads(raw)
    except (TypeError, ValueError):
        report = {"result": str(raw)[:200]}
    return {**result, "reindex": report}


@app.get("/bug_report")
def get_bug_report():
    """The diagnostic block a bug report attaches. Built server-side on purpose:
    in the browser the API keys sit in the panel's own fields, and a public issue
    is exactly the wrong place for one."""
    from silica.ui.web import settings as st

    return st.bug_report()


# --- User memory (ADR-0076) ----------------------------------------------------
# The Settings > Memory page and the chat's Undo. None of these checks the
# memory switch or incognito: they are the user's own hand on the files, and
# erasing must work while memory is paused.

def _memory_scope(scope: str) -> str:
    from silica.kernel import user_memory

    if scope not in user_memory.SCOPES:
        raise HTTPException(status_code=404, detail=f"no memory scope {scope!r}")
    return scope


@app.get("/memory")
def memory_view():
    from silica.kernel import user_memory
    from silica.ui.web import settings as st

    view = user_memory.view()
    for s in view["scopes"].values():
        s["path"] = st.short_path(Path(s["path"])) if s["path"] else ""
    return view


@app.put("/memory/{scope}")
def memory_write(scope: str, payload: dict):
    from silica.kernel import user_memory

    out = user_memory.write_file(_memory_scope(scope), str(payload.get("text", "")))
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["error"])
    return out


@app.post("/memory/undo")
def memory_undo(payload: dict):
    from silica.kernel import user_memory

    out = user_memory.undo(_memory_scope(str(payload.get("scope", ""))),
                           str(payload.get("token", "")))
    if not out["ok"]:
        raise HTTPException(status_code=409, detail=out["error"])
    return out


@app.post("/memory/reset")
def memory_reset():
    from silica.kernel import user_memory

    return user_memory.reset()


# --- vault instructions (ADR-0127 point 4) --------------------------------------
# A file and not a settings row, as memory is; on hosted the vault is the one
# root an account can write and there is no file manager, so this is the way in.


@app.get("/instructions")
def instructions_view():
    from silica.kernel import vault_instructions as vi

    vault = CONFIG.vault_path or None
    return {"available": bool(vault), "text": vi.read(vault), "cap": vi.CAP, "path": vi.REL}


@app.put("/instructions")
def instructions_write(payload: dict):
    from silica.kernel import vault_instructions as vi

    out = vi.write(CONFIG.vault_path or None, str(payload.get("text", "")))
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["error"])
    SESSION.seed = None   # built again by the next chat, with the new text (a prewarm could lose the race)
    return out


# --- Zotero (ADR-0055) ---------------------------------------------------------
# Sync defs: every route talks to Zotero over HTTP, so they run in the
# threadpool and a slow Zotero never stalls the event loop that streams turns.


def _zotero_call(fn, *args):
    from silica.sources.zotero import ZoteroError

    try:
        return fn(*args)
    except ZoteroError as exc:
        # A ZoteroError is a sentence written for the person (what failed and
        # where to fix it), never a raw transport error: zotero.Zotero.send
        # turns httpx's URL-bearing text into one before it gets here.
        raise HTTPException(status_code=409, detail=exc.args[0] if exc.args else "Zotero failed")


@app.get("/zotero/status")
def zotero_status():
    from silica.ui.web import zotero_web
    return zotero_web.status()


@app.post("/zotero/link")
def zotero_link(payload: dict):
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.link, bool(payload.get("on")))


@app.post("/zotero/setting")
def zotero_setting(payload: dict):
    from silica.ui.web import zotero_web
    try:
        return zotero_web.setting(str(payload.get("key", "")), payload.get("value"))
    except KeyError:
        raise HTTPException(status_code=400, detail="not a Zotero setting")


@app.post("/zotero/grant")
def zotero_grant():
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.grant)


@app.get("/zotero/library")
def zotero_library():
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.library)


@app.get("/zotero/collection")
def zotero_collection(key: str):
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.collection, key)


@app.get("/zotero/item")
def zotero_item(key: str):
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.item, key)


@app.post("/zotero/file")
def zotero_file(payload: dict):
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.file, list(payload.get("moves") or []))


@app.get("/zotero/groups")
def zotero_groups():
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.groups_preview)


@app.post("/zotero/groups")
def zotero_groups_write():
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.groups_write)


@app.get("/zotero/peek")
def zotero_peek(key: str):
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.peek, key)


@app.get("/zotero/covers")
def zotero_covers_status():
    from silica.ui.web import zotero_web
    return zotero_web.covers_status()


@app.post("/zotero/covers")
def zotero_covers_start():
    from silica.ui.web import zotero_web
    return zotero_web.covers_start()


@app.get("/zotero/labels")
def zotero_labels():
    from silica.ui.web import zotero_web
    return zotero_web.labels()


@app.post("/zotero/unlink")
def zotero_unlink():
    from silica.ui.web import zotero_web
    return _zotero_call(zotero_web.unlink)


# Settings › Connectors (ADR-0089 point 8): every block's status, and one
# action of one block. Sync GET on purpose: FastAPI runs it in the threadpool,
# and a status reads the keyring, which may block on a D-Bus round trip.
@app.get("/connectors")
def connectors_status():
    from silica.connectors import connectors
    return [c.status() for c in connectors()]


@app.post("/connectors/{name}/{action}")
async def connectors_act(name: str, action: str, request: Request):
    from silica.connectors import find

    block = find(name)
    if block is None:
        raise HTTPException(404, f"no connector named {name}")
    try:
        payload = await request.json()
    except ValueError:
        payload = {}  # an action without a body (reload) posts nothing
    # Off the loop: Add lists a new server (up to 20 s) and a refusal is an
    # `ok: false` body, never a 5xx the panel would read as the server dying.
    return await asyncio.to_thread(block.act, action, payload if isinstance(payload, dict) else {})


@app.get("/endpoints")
def get_endpoints():
    from silica.ui.web import settings as st

    return st.endpoint_status()


@app.post("/endpoints/start")
async def start_endpoint(payload: dict):
    """Start one local endpoint from the command its own .env key names. Loading
    a model takes tens of seconds, so this waits on a worker thread."""
    from silica.ui.web import settings as st

    return await asyncio.to_thread(st.start_endpoint, str(payload.get("label", "")))


@app.get("/")
def index(request: Request):
    # Cache-bust the churning assets by content hash: StaticFiles sets no
    # Cache-Control, so browsers serve them stale from heuristic freshness
    # (edited JS never reaches the page). A content-keyed URL can't be stale.
    # The big vendored bundles keep their long-lived cache — only these churn.
    import hashlib

    from silica.config import CONFIG, hosted, served_views, withheld

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # work.js belongs here as much as the others: it owns the whole node
    # panel and churns with it, and without a version an edit to it reaches
    # nobody who has already loaded the page once.
    # Read back out of the document rather than listed here: app.js is now eight
    # cuts whose load order lives in index.html and nowhere else, and a second
    # copy of that list is a copy that drifts. The pattern is deliberately not
    # every /static/ href -- the vendored bundles keep their long-lived cache,
    # and hashing 3.5 MB of mermaid on each page load is the reason why.
    for asset in dict.fromkeys(
        re.findall(r"/static/(app[\w-]*\.(?:js|css)|work\.js)", html)
    ):
        ver = hashlib.sha256((STATIC_DIR / asset).read_bytes()).hexdigest()[:8]
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    # The preference, not the resolution: "auto" is a question only the browser
    # can answer, and the inline script in <head> answers it before first paint.
    # Stamping it server-side is what keeps a light session from flashing dark.
    pref = CONFIG.theme if CONFIG.theme in ("auto", "dark", "light") else "auto"
    html = html.replace('data-theme-pref="auto"', f'data-theme-pref="{pref}"')
    # The new chat's greeting says it (ADR-0048, amendment 2026-10-22), and
    # stamping it is what lets the first word written be the name.
    if CONFIG.user_name:
        html = html.replace('<html lang="en"', '<html lang="en" data-name="'
                            f'{_html.escape(CONFIG.user_name, quote=True)}"', 1)
    # A demo's controls for what it refuses (ADR-0139 point 6) go before the
    # first paint too, by the same kind of stamp, and the field names whose
    # notes these are. Ahead of the hosted stamp, which the account's line reads.
    if demo():
        html = html.replace('<html lang="en"', '<html lang="en" data-demo', 1)
        html = html.replace('placeholder="Ask about your notes"', 'placeholder="Ask about the sample vault"', 1)
    # Stamped, not probed, for the same reason: the views a hosted account
    # does not get (ADR-0111 point 3) must be gone before the first paint. A
    # demo is stamped as served_views() clamps it, even on a unit that lost
    # SILICA_HOSTED, or the page offers tabs the server answers 404 for.
    if hosted() or demo():
        html = html.replace('<html lang="en"', '<html lang="en" data-hosted', 1)
        # The views this account gets (ADR-0140 point 4): every element of one
        # is marked, and the CSS hides each unmarked one, so a view added later
        # stays hidden until the tenant's list serves it.
        for view in served_views():
            html = re.sub(rf'data-(?:tab|view)="{view}"', r"\g<0> data-served", html)
        # A help row keyed by a route is marked too, unless this process refuses
        # the route, as a demo refuses Settings and uploads (ADR-0139 point 6).
        gone = withheld("routes")
        html = re.sub(r'data-route="([^"]*)"', lambda m: m[0] if m[1] in gone else f"{m[0]} data-served", html)
        # The account the window is signed in as, for the menu under SILICA
        # (ADR-0122, amendment 2026-10-22). The gateway sets X-Silica-Email
        # (ADR-0154), Cloudflare Access its own header until the switch, and the
        # host's Caddy passes a tenant only what one of them set (silica-hosted
        # deploy/Caddyfile). It is shown back to whoever sent it and decides
        # nothing, so a forged one forges only its own page. Sign out is offered
        # behind the gateway alone: under Access /sign-out would reach this
        # process and answer 404.
        gateway = request.headers.get("x-silica-email", "")
        email = gateway or request.headers.get("cf-access-authenticated-user-email", "")
        if email:
            signout = " data-signout" if gateway else ""
            html = html.replace(
                '<html lang="en" data-hosted',
                f'<html lang="en" data-hosted data-user="{_html.escape(email, quote=True)}"{signout}', 1)
    return HTMLResponse(html)


# The Learn tab's routes live in their own module (learn-suite §6.1): the
# engine is silica/kernel/learn, the router only holds the live session.
from silica.ui.web.learn_routes import router as _learn_router  # noqa: E402

app.include_router(_learn_router)

from silica.ui.web.skills_routes import router as _skills_router  # noqa: E402

app.include_router(_skills_router)

from silica.ui.web.jobs import router as _jobs_router  # noqa: E402

app.include_router(_jobs_router)

from silica.ui.web.discover_routes import router as _discover_router  # noqa: E402

app.include_router(_discover_router)

from silica.ui.web.public_routes import router as _public_router  # noqa: E402

app.include_router(_public_router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def serve(port: int = 8765) -> None:
    """Apply config, print the address, then block on uvicorn. No browser is
    opened: the person opens the address the banner prints."""
    import uvicorn

    from silica.ui.banner import print_banner
    from silica.ui.console import CONSOLE

    SESSION.reset()

    # A hosted tenant listens on a Unix socket in its own directory, never on a
    # TCP port (ADR-0085 point 1): loopback is open to every tenant, because the
    # shared embedder lives there, so a port would let one tenant's process
    # reach another's GUI. uvicorn makes the socket 0666; the directory's
    # permissions are what keep it the proxy's alone.
    sock = os.environ.get("SILICA_GUI_SOCKET", "").strip() or None

    print_banner()
    where = f"unix:{sock}" if sock else f"http://127.0.0.1:{port}"
    CONSOLE.print(f"  [dim]GUI live at[/] [cyan]{where}[/]\n")

    # Built here rather than through uvicorn.run(), which keeps the Server to
    # itself: /narration/sse polls should_exit to end itself on the way out.
    # timeout_graceful_shutdown is the backstop under it, for the one stream
    # that does not poll - an in-flight /chat turn, which carries its own cancel
    # path and would otherwise hold Ctrl+C for the length of the turn.
    # log_config=None: uvicorn installs no logging of its own, so its records
    # reach the handler `_setup_logging` already put on root and obey its level
    # (quiet by default, the full access log under --verbose). Two reasons, and
    # the second is a defect. The banner it prints duplicates the line above.
    # And its colourized formatter re-renders each record from the
    # `color_message` extra, which still carries the raw `%d`/`%s`, while
    # litellm >=1.98 attaches a redaction filter to `uvicorn.error` that
    # interpolates the message and then sets `record.args = None` — so on a TTY
    # every uvicorn line with arguments printed its format string verbatim
    # ("Started server process [%d]", "Uvicorn running on %s://%s:%d"). Piped
    # output escaped it, which is why it never showed in a captured log.
    SESSION.server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, uds=sock, timeout_graceful_shutdown=1,
        log_config=None))
    try:
        try:
            SESSION.server.run()
        except KeyboardInterrupt:
            # uvicorn re-raises the signal it captured, AFTER it has shut down
            # cleanly, and `silica` is installed as cli:main - so the module's
            # own __main__ guard never runs and nothing above here catches it.
            # Without this a tidy Ctrl+C ends in a traceback that reads as a
            # crash. The shutdown already happened; there is nothing to salvage.
            pass
    finally:
        session.capture_own_session()  # last chance: this conversation ends with the server
