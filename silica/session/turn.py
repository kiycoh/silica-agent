# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One turn, on whichever surface: the steps before the model, the model, the steps after.

A step is a function of the `Turn`. The REPL, `run_turn` and the phone each hold
the list of steps they take, in the order they take them, with their own among
them where they draw; ADR-0089's reduced phone turn is a shorter list. A step
added to a turn is written here once and named in the lists that want it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from silica.agent import turn_notes
from silica.agent.constraints import AgentConstraints, chat_constraints, web_turn_constraints
from silica.agent.loop import run_agent
from silica.agent.recall_watch import numbers_notes, read_refs, READS_KEY, RecallWatch, unfounded_in_turn
from silica.agent.sentinels import EMPTY_COMPLETION
from silica.config import CONFIG
from silica.metrics import emit
from silica.model.llm import meter_totals
from silica.obs.events import LLMStreamEvent, ToolErrorEvent, ToolStartEvent
from silica.session import meter, offers
from silica.session.conversation import Conversation
from silica.session.words import command_word, first_word
from silica.sources.web_research import WebTurn

logger = logging.getLogger(__name__)


@dataclass
class Turn:
    """What one turn knows as it passes through its steps."""

    conv: Conversation
    text: str                  # the person's words: what the named notes, offers and checks read
    content: str = ""          # what the model is asked, as the blocks join it
    origin: str | None = None  # a turn the harness composed, which the wire and the gates tell apart
    web: tuple[str, str] | None = None   # a /web turn's (question, instruction)
    note: str = ""             # the note open beside the chat, a vault path
    scope: dict | None = None  # the workspace the turn runs in (ADR-0112 point 4)
    shown: bool = True         # whether this conversation's window is what the meter shows
    opened: str = ""           # the open note, once the named-notes block kept it
    escalated: bool = False    # the answer is the aid itself (learn.offer.escalation)
    plan_appendix: str = ""    # Learn's calendar, appended under the answer
    plan_draft: dict | None = None   # the plan this turn computed, offered for saving
    read: RecallWatch | None = None  # counts what the turn read and missed
    watch: Any = None          # what the loop calls with each event
    answer: str = ""
    empty: bool = False        # the model gave nothing, read before anything is appended to the answer
    stopped: bool = False      # the person stopped it: an outcome, not an answer
    unread: list[str] = field(default_factory=list)
    skill_note: dict | None = None
    offer: dict | None = None
    notes: list[dict] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.content = self.content or self.text

    def add(self, block: str) -> None:
        if block:
            self.content += "\n\n" + block


# --- before the model ---------------------------------------------------------


def wear(text: str, conv: Conversation) -> tuple[str, str] | None:
    """A persona (ADR-0127 point 3) is worn, not run: its command edits this
    conversation's system line and asks no model; with words after the name,
    those words are this turn, already under the persona. None for anything else."""
    from silica.kernel import personas

    if not text.startswith("/"):
        return None
    return personas.command(text, conv.messages, CONFIG.vault_path or None, not conv.collapsed)


def withheld_command(text: str) -> str:
    """The command `text` opens with, as typed, when this process withholds it,
    else "". A view's command goes with the view (ADR-0140 point 4), and a demo's
    refused ones with it (ADR-0139 point 6); the picker does not offer them, but a
    typed one does not come from it.

    Two doors ask this and they answer differently on purpose. The demo's, before
    the turn (`chat_routes._demo_refuses`), is a 404 the door counts no question
    for. The turn's (`refused`) is an error frame in a 200, because the words after
    a persona's name only exist once the turn has read it."""
    from silica.config import withheld

    head = first_word(text)
    return head if head.startswith("/") and head.lower() in withheld("commands") else ""


def refused(text: str) -> str:
    """Why this account does not run what `text` names, "" when it does. Asked
    after the persona, whose words are this turn, and before /web, which runs ahead
    of the table: "/<persona> /web" passed the 404 at the demo's door and reached
    the internet (review 2026-10-31)."""
    head = withheld_command(text)
    return f"'{head}' is not part of this account" if head else ""


_WEB_USAGE = (
    "/web has nothing to search for. Usage: /web <keywords>, or a bare /web "
    "right after a question to answer that question from the web."
)


def expand_web_turn(user_input: str, messages: list[dict]) -> tuple[str, str] | None:
    """`/web [keywords]` — the consent turn. Returns (question, instruction).

    None when the input is not `/web`. Raises ValueError (usage) when there are
    neither keywords nor a prior question to escalate.

    Deliberately NOT a direct handler: run_agent appends the assistant and tool
    turns to the shared `messages` itself, so a handler running its own loop and
    then reporting the answer would append that answer a second time — the GUI's
    generic direct-command wrapper renders it as a fenced text block, which is
    how a web answer would arrive both as markdown and as a code block.
    """
    parts = user_input.strip().split()
    if command_word(user_input) != "/web":
        return None  # "/web-search" is its own command and must not match here
    question = " ".join(parts[1:]).strip()
    if not question:
        # Bare /web: the question is already in the history, no pending state
        # needed. `origin` marks CLI-expanded directives — re-asking one of those
        # on the web would escalate a harness instruction, not a human question.
        question = next(
            (
                m["content"] for m in reversed(messages)
                if m.get("role") == "user" and not m.get("origin") and m.get("content")
            ),
            "",
        )
        if not question:
            raise ValueError(_WEB_USAGE)
    from silica.sources.web_research import web_turn_instruction

    return question, web_turn_instruction(question)


def name_notes(t: Turn) -> None:
    """The notes the person named, as paths (ADR-0097 point 2). From their words,
    so a skill's expansion keeps its links and the block adds their paths. Not on
    a /web turn: its tools cannot read a note."""
    from silica.kernel.text.mentions import named_notes

    if t.web is None and (block := named_notes(t.text, None, t.note)):
        t.add(block)
        # Only a note the block kept: one renamed under the open drawer was left
        # out, and the bubble must not claim it was read.
        t.opened = t.note if t.note and ": " + t.note in block else ""


def escalate(t: Turn) -> None:
    """A reader still stuck on a note gets the aid as the answer itself
    (learn.offer.escalation), and an offer's command sent is recorded."""
    if t.web is None and (block := offers.escalation(t.text, t.conv.messages, t.conv.sid or "")):
        t.add(block)
        t.escalated = True


def plan(t: Turn) -> None:
    """Learn's plan for this message, or the note that a reader who says it was
    unclear asked to understand, not to write: the local 9B answered "non ho
    capito il Boosting" with a new note of its own (2026-10-23)."""
    from silica.kernel.learn.offer import says_lost

    if t.web is not None:
        return
    block, t.plan_appendix, t.plan_draft = offers.plan_context(t.text, t.conv.messages)
    if block:
        t.add(block)
    elif says_lost(t.text):
        t.add("[The reader says this was not clear. Answer here in the chat; READ-ONLY: "
              "do not create, edit, patch, or move any note.]")


def user_message(t: Turn) -> dict:
    """The turn as the history stores it."""
    from silica.kernel import personas

    msg: dict[str, Any] = {"role": "user", "content": t.content}
    if t.origin:
        msg["origin"] = t.origin
    if persona := personas.active(t.conv.messages):
        msg[personas.TURN_KEY] = persona
    return msg


def watched(t: Turn, renderer, surface: str = "gui") -> None:
    """Both wrappers forward every event to the renderer untouched: WebTurn
    records the trace the citations are built from, RecallWatch counts recall
    misses for the thin-coverage hint. A /web turn is not checked for numbers,
    but its pages were read: the RecallWatch under its WebTurn collects them for
    the session set."""
    t.read = RecallWatch(renderer, surface=surface)
    t.watch = WebTurn(t.web[0], t.read) if t.web else t.read


def constraints(t: Turn, withhold=(), max_iterations: int | None = None) -> AgentConstraints:
    """The /web lane's for a /web turn, else the chat turn's, the same on every
    chat surface."""
    if t.web:
        return web_turn_constraints()
    return chat_constraints(t.conv.messages, withhold, max_iterations)


# --- the model ----------------------------------------------------------------


def ask_model(t: Turn, cancel, cut: AgentConstraints) -> None:
    """Run the agent on this conversation, with retrieval narrowed to the turn's
    workspace (ADR-0112 point 4). Set and reset here, on the thread that runs the
    agent: `to_thread` copies the context, and a token must be reset in the
    context that set it."""
    from silica.kernel import workspaces

    token = workspaces.open_scope(t.scope) if t.scope else None
    try:
        t.answer = run_agent(t.conv.messages, CONFIG.model, t.watch, cancel_token=cancel, constraints=cut)
    finally:
        if token is not None:
            workspaces.close_scope(token)
    t.empty = t.answer == EMPTY_COMPLETION


# --- after the model ----------------------------------------------------------


def check_numbers(t: Turn) -> None:
    """ADR-0095: the answer as the model wrote it, before attribution and figures
    add text it did not; not a stopped turn's, not a /web turn's, whose answer is
    bound to banked verbatim quotes."""
    assert t.read is not None  # a list with this step calls `watched` first
    t.unread = ([] if t.stopped or t.web else
                unfounded_in_turn(t.answer, t.read.numbers, t.conv.messages, t.conv.seen_numbers))


def attribute(t: Turn) -> None:
    """Before anything draws the answer and before the compaction sweep: the
    Sources block belongs to what the person sees AND to what the history carries.
    isinstance, not `if t.web:`, because only this one says which half of the
    union carries `attribute`."""
    if t.stopped:
        return
    if isinstance(t.watch, WebTurn):
        t.answer = t.watch.attribute(t.answer, t.conv.messages)
    elif t.watch.web_answer:
        from silica.sources.web_research import relay_sources

        t.answer = relay_sources(t.answer, t.conv.messages)


def plan_under_answer(t: Turn) -> None:
    """Learn's calendar under the answer, written by code so every date is the
    plan's (learn.offer.plan_context)."""
    messages = t.conv.messages
    if t.stopped or not (t.plan_appendix and t.answer):
        return
    t.answer = t.answer.rstrip() + "\n\n" + t.plan_appendix
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = t.answer


def offer_next(t: Turn, reserved) -> None:
    """A request made in enough chats becomes a proposed skill, or one already
    installed is offered as the next turn (ADR-0127 points 5 and 6). Failing that,
    a note the reader keeps missing, or a message saying it was not clear: a way
    to work on it, launched by the reader (learn.offer). Not under an answer that
    is already the aid. `reserved` is the surface's own commands, which a proposal
    may not shadow; required, as `proposed_skills.stage` requires it."""
    if t.stopped or t.web or not t.answer:
        return
    t.skill_note, t.offer = offers.skill_after_turn(t.text, t.conv.messages, reserved)
    if t.offer is None and not t.escalated:
        t.offer = offers.study_offer(t.text, t.conv.messages, t.conv.sid or "", t.plan_draft)


def note(t: Turn) -> None:
    """The lines under the answer (ADR-0094 point 8), stored on it before the beat
    so a reopened chat draws them too. A stopped turn has no answer to carry them:
    they are drawn live only."""
    notes = t.watch.notes() if isinstance(t.watch, RecallWatch) else []
    notes += numbers_notes(t.unread)
    if not (t.stopped or t.web):
        # A card or a slide point its cited note does not hold (learn.artifacts).
        from silica.kernel.learn.artifacts import grounding_notes

        notes += grounding_notes(t.text, t.answer)
    notes += [t.skill_note] if t.skill_note else []
    t.notes = turn_notes.ordered(notes) if t.stopped else turn_notes.attach(t.conv.messages, notes)


def stamp_reads(t: Turn) -> None:
    """The notes this turn read, before the beat stores the answer and before
    compaction stubs the results that name them (ADR-0127)."""
    messages = t.conv.messages
    t.refs = read_refs(messages)
    if t.refs and not t.stopped and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
        messages[-1][READS_KEY] = t.refs


def beat(t: Turn) -> None:
    """Final-assistant turn beat, post-attribution (see loop.py): the replay must
    carry what the person actually read, sources included."""
    from silica.obs import narration as _narr

    messages = t.conv.messages
    if messages and messages[-1].get("role") == "assistant":
        _narr.NARRATOR.turn(messages[-1])


def recount(t: Turn) -> None:
    """run_agent already appended the final assistant message to the history, so
    nothing is appended here: the meter reads the turn, and the sweep it may trip."""
    meter.recount(t.conv, shown=t.shown)


# --- the usage line -----------------------------------------------------------


class UsageLine:
    """The turn's usage line (ADR-0100 point 3), counted as its events pass.
    Abandonment ends a stream without a verdict, so `cancelled` until the surface
    says otherwise."""

    def __init__(self) -> None:
        self.started, self.tokens_before = time.monotonic(), meter_totals()
        self.first_token: float | None = None
        self.tools: dict[str, int] = {}
        self.tool_errors: dict[str, int] = {}
        self.outcome = "cancelled"
        self.limit: str | None = None  # the layer a `limited` turn met (ADR-0102 point 10)

    def __call__(self, ev) -> None:
        if isinstance(ev, ToolStartEvent):
            self.tools[ev.name] = self.tools.get(ev.name, 0) + 1
        elif isinstance(ev, ToolErrorEvent):
            self.tool_errors[ev.name] = self.tool_errors.get(ev.name, 0) + 1
        elif self.first_token is None and isinstance(ev, LLMStreamEvent) and ev.chunk_type == "text" and ev.content:
            self.first_token = time.monotonic()

    def failed(self, failure: dict) -> None:
        """A limit reached is the tenant's allowance at work, not a fault of the
        server: counted as `limited`, and kept out of the error log the nightly
        rollup counts, which is why the caller reads `limit` next."""
        self.limit = failure.get("limit")
        self.outcome = "limited" if self.limit else "error"

    def write(self, kind: str, channel: str, **extra) -> None:
        # ponytail: the tokens are the difference of the process totals, so a
        # background job running during the turn is charged to it (its own
        # llm_call lines still name it by call-site), and an abandoned turn's
        # worker spends past this line uncounted. A context variable per turn
        # is the upgrade, due when ADR-0099's jobs are built.
        after = meter_totals()
        emit("turn", kind=kind, channel=channel, outcome=self.outcome,
             ms=round((time.monotonic() - self.started) * 1000),
             first_token_ms=round((self.first_token - self.started) * 1000) if self.first_token is not None else None,
             calls=after[0] - self.tokens_before[0], prompt=after[1] - self.tokens_before[1],
             cached=after[3] - self.tokens_before[3],
             completion=after[2] - self.tokens_before[2],
             tools=dict(self.tools), tool_errors=dict(self.tool_errors), limit=self.limit, **extra)
