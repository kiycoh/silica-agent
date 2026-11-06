# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One agent turn, transport-neutral: the browser's as a stream of wire dicts, the phone's
answered whole. Framing a turn for HTTP is `sse`'s job, so this module loads no web
framework and the bridge (`silica connect`) can run a turn without one."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from silica.agent import turn_notes
from silica.agent.constraints import (
    AgentConstraints,
    chat_deferred,
    chat_tools,
    DEMO_MAX_ITERATIONS,
    PHONE_TOOLS,
    web_turn_constraints,
)
from silica.agent.loop import run_agent
from silica.agent.mentions import image_mark
from silica.agent.sentinels import EMPTY_COMPLETION
from silica.tools import tool_failed
from silica.agent.recall_watch import numbers_notes, read_refs, READS_KEY, RecallWatch, unfounded_in_turn
from silica.config import CONFIG, demo
from silica.kernel import vault_files
from silica.kernel.graph.mindmap import note_resolver
from silica.metrics import emit
from silica.model.llm import meter_totals
from silica.obs.events import LLMStreamEvent, ToolCompleteEvent, ToolErrorEvent, ToolStartEvent
from silica.sources.web_research import WebTurn
from silica.ui.web import render, session
from silica.ui.web.callback import event_to_json
from silica.ui.web.failures import explain as _failure
from silica.ui.web.session import SESSION

logger = logging.getLogger(__name__)


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
    # says so on "work/tool" (ingest.nucleate), so its block draws as the
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
            # streams its block and phases while it works (ingest.nucleate),
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
            agent_msg += "".join(image_mark(p) for p in images)
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
            yield {"type": "files", "files": await asyncio.to_thread(files_shown, msg["silica_files"])}
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
        empty = answer == EMPTY_COMPLETION
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
            # Before linkify and before the compaction sweep: the Sources block
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


def phone_turn(text: str, cancel: threading.Event) -> dict:
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
        outcome = "empty" if answer == EMPTY_COMPLETION else "done"
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
