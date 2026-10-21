# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Optional constraints that turn run_agent into a bounded worker loop.

Carries only the three generic dials (tools, model, iteration cap). The leash is
deliberately NOT here — write safety lives inside the write tool / apply_op, so
run_agent stays domain-agnostic (Rune 1 / ADR set).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentConstraints:
    tools: tuple[str, ...]          # subset of TOOLS the loop may expose + dispatch
    model: str | None = None        # override the model arg when set
    max_iterations: int | None = None  # override the default safety cap when set
    # An interactive turn with a constrained toolset. run_agent used to read
    # "constraints is None" as "interactive": streaming on, no worker slot.
    # That conflated two independent facts — the GUI chat wants the chat_tools
    # cut (a smaller tool block every iteration) while keeping its live stream
    # and staying off the worker-pool cap. This flag says which of the two a
    # constrained caller is; batch/worker callers leave it False.
    interactive: bool = False
    # Streams its text to the progress callback without being a chat turn: the
    # web door's lane, whose answer can be the reply (ADR-0124 amendment).
    # `interactive` would also run the chat turn's relevance sweep, open a
    # preserve window and declare links, none of which a sub-loop may do.
    stream: bool = False
    # Permitted but not offered: the loop adds one to the offered set when
    # silica_load_tools names it, or when the model calls it directly (a hosted
    # route lets a model call a tool it read about; refusing that call would
    # turn a permitted tool into a dead end).
    deferred: tuple[str, ...] = ()
    # Where `tools` are looked up, when not the global TOOLS. An MCP
    # connector's sub-loop (ADR-0091) holds one server's tools under the
    # server's own names; registered globally they would sit beside every
    # other server's and the vault's, and a `send_message` of two servers
    # would collide.
    registry: "Mapping[str, Any] | None" = None


# Batch/maintenance tools the conversational loop does not need to *choose*: the
# user reaches each one by name through its slash command. Excluding them from
# the chat toolset is worth ~2.4k tokens per turn (~24% of the tool block).
#
# The cut is bounded by one hard constraint, enforced by
# test_chat_tools_keeps_every_recovery_path_it_advertises: tool descriptions name
# each other as follow-ups ("run silica_cooccurrence_refresh first", "use
# silica_curate"). Hiding a tool that a visible tool tells the model to call
# turns that instruction into a dead end. That constraint is what keeps
# silica_curate, silica_dedup and silica_graph_export reachable (loadable
# since ADR-0050) despite each having a slash command, and why this list is
# 14 entries rather than 30.
#
# silica_anneal was here until 2026-08-23 and is deliberately NOT: it is the
# only tool that resolves a deferred bundle, and the nucleate summary tells the
# user how many are still deferred. Hidden, the obvious follow-up ("resolve the
# 24 deferred notes") reached a turn with no tool that could do it — the same
# dead end _summoned was written for, except no slash command names it.
#
# ponytail: one explicit list, not a per-tool flag. A tool added later defaults
# to being visible in chat, which costs tokens but never breaks — the safe
# direction. Revisit if this grows past ~20 entries.
_CHAT_EXCLUDED = frozenset({
    "silica_aliases",            # /aliases
    "silica_code_pack",          # MCP-facing (external rewrite agents); no chat flow
    "silica_deferred_list",      # deferred-ops bookkeeping, surfaced by the FSM
    "silica_deferred_flush",
    "silica_deferred_retry",
    "silica_delegate",           # fan-out, driven by the Coordinator not by chat
    "silica_document",           # /wiki
    "silica_generate_taxonomy",  # /organize
    "silica_health",             # /status
    "silica_inbox_ls",
    "silica_ledger_digest",      # /report runs it directly, no agent involved
    "silica_mindmap",            # /map
    "silica_record_quiz",        # /quiz and /learn name it in their directives
    "silica_run_organizer",      # /organize
})


def web_turn_constraints() -> AgentConstraints:
    """`/web` — the consented web turn: the four web tools and nothing else.
    (`remember` banks a verbatim quote from a fetched page and `find_in_page`
    greps one; both touch only per-turn module state in
    silica/sources/web_research.py, never the vault.)

    48 is the twin of `_DEFAULT_MAX_SEARCHES` in silica/sources/web_research.py,
    which carries the reasoning for the number. Duplicated rather than imported:
    the import direction is sources -> agent, so this module must not read that
    one; test_web_turn_iteration_cap_matches_web_research holds them equal.

    Held equal even though this lane is interactive and that one is batch: the
    cap is a ceiling, not a target, so a /web question that converges in four
    steps costs four either way. Split the two the day a /web turn is seen
    spending a ceiling it had no reason to spend.
    """
    return AgentConstraints(
        tools=("web_search", "web_fetch", "remember", "find_in_page"),
        max_iterations=48,
    )


def web_answer_constraints() -> AgentConstraints:
    """`silica_web_answer` — the chat's own door to the web, which the user
    waits through mid-turn.

    No `remember`: see web_answer_instruction in silica/sources/web_research.py.

    8 and not /web's 48: the chat door is where /web's ceiling was spent for
    no reason. A live V4 Flash smoke test on 2026-10-21 ran one question to 132
    calls and 12 minutes with no answer, the chat calling this door four times.
    On the latency bench of 2026-10-22 the lane answered four questions of five
    in 1 to 3 calls; the fifth spent all 12 of a first cap on ten searches
    against challenged lanes, and the steps past the eighth bought nothing. A
    question that needs more is research, and /web-search writes it up as a note.

    Thinking stays on. Off (OpenRouter `reasoning: {enabled: false}`) V4 Flash
    0731 on DeepInfra wrote its draft into the answer, an English "Let me
    answer." and the answer again, in 3 of 10 bench answers against 0 of 10 with
    it on (2026-10-22).

    Streams, so a `final` answer can paint into the reply while it is written;
    whether it does is the calling loop's decision (loop.py).
    """
    return AgentConstraints(
        tools=("web_search", "web_fetch", "find_in_page"),
        max_iterations=8,
        stream=True,
    )


# The chat core (ADR-0050): the tools a chat turn is offered before it asks for
# any, in this order. Chosen from use, not taste: GUI and TUI sessions through
# 2026-09-28 made 445 calls over 22 distinct tools. The core keeps the ones
# called in 3 or more of those sessions except silica_semantic_search and
# silica_related (slower routes to recall) and silica_file_links (10 calls,
# 3 sessions: one load step is cheaper than its schema on every call), plus
# every tool BASE names. The order is part of the prompt prefix, so it is
# fixed and loaded tools go after it.
# Reopen the list when a deferred tool is loaded in more than 1 turn in 10
# (count `silica_load_tools` in ~/.silica/narration against user turns).
# silica_remember joined 2026-10-07 without use data (ADR-0076): a "remember
# this" has to land on the turn it is said in, and a load step there is one
# more chance for a 9B model to answer "noted" and save nothing. It sits just
# before the loader, so the head every earlier session cached is unchanged up
# to it.
# silica_rework joined 2026-10-16 without use data either (ADR-0116), for the
# same reason: asked to rework a long text, the chat model answers it itself
# and condenses it, and a load step is one more chance to do that. Also just
# before the loader.
CHAT_CORE = (
    "silica_recall",
    "silica_search",
    "silica_search_context",
    "silica_read_note",
    "silica_read",
    "silica_outline",
    "silica_files",
    "silica_props",
    "silica_links",
    "silica_backlinks",
    "silica_write_note",
    "silica_patch_note",
    "silica_move",
    "silica_flag_note",
    "silica_vault_report",
    "silica_ledger_next",
    "silica_ledger_update",
    "silica_web_answer",
    "silica_run_injector",
    "silica_remember",
    "silica_rework",
    "silica_load_tools",
)

# The review's step 2 is in the core, not unlocked by the report: unlocked, the
# report's own turn read a schema pointing at silica_ledger_next and did not
# have it. Offering the pair did not by itself stop qwen3.5-9b from skipping
# the plan (seed_ledger=false, 1 of 1 more run); removing that parameter did
# (silica_vault_report, prompt-rules probe 2026-09-28).
_REVIEW_APPLY = ("silica_ledger_next", "silica_ledger_update")


# What a turn from Telegram is offered (ADR-0089 point 5): the core's reads, in
# the core's order, and nothing deferred, so no loader either. A Telegram
# account is a second door to the vault, held by whoever holds the phone, and
# Silica has no approval step: with no write tool and no web here, no text a
# phone turn reads can make it write a note, save a memory or fetch a URL.
PHONE_TOOLS = (
    "silica_recall",
    "silica_search",
    "silica_search_context",
    "silica_read_note",
    "silica_read",
    "silica_outline",
    "silica_files",
    "silica_props",
    "silica_links",
    "silica_backlinks",
)


def _eligible() -> dict:
    from silica.tools import TOOLS
    import silica.tools.loader  # noqa: F401  the core names it; register before reading TOOLS
    from silica.connectors.mcp_tools import ensure_registered

    # One `mcp__<key>` per configured server (ADR-0091), from the file alone
    # and once per process, so the loadable catalog it joins stays static.
    ensure_registered()
    return {n: t for n, t in TOOLS.items() if not t.sensitive and not t.internal}


def loadable() -> tuple[str, ...]:
    """Chat tools reachable through silica_load_tools: everything chat may call
    except the core and the slash-command-only batch tools. Static for a
    process, so the loader's catalog (and the prompt prefix it sits in) never
    changes between turns."""
    return tuple(n for n in _eligible() if n not in CHAT_CORE and n not in _CHAT_EXCLUDED)


def _in_play(messages: "list[dict] | None") -> list[str]:
    """Tools this conversation already brought in, in first-use order: loaded
    by name or called directly. Read off the assistant tool calls, which
    compaction never rewrites."""
    import json

    seen: list[str] = []
    for m in messages or ():
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or ():
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            if name == "silica_load_tools":
                try:
                    names = json.loads(fn.get("arguments") or "{}").get("names") or []
                except (json.JSONDecodeError, AttributeError):
                    names = []  # a malformed load loaded nothing; the loop said so at the time
            else:
                names = [name]
            seen += [n for n in names if isinstance(n, str) and n not in seen]
    return seen


def _summoned(messages: "list[dict] | None") -> set[str]:
    """Tools outside the core that a slash command in THIS conversation named.

    `/organize` expands into a directive that tells the model to call
    silica_generate_taxonomy and then silica_run_organizer. Both are excluded
    above, so the turn dead-ended on tools the model could not see — the very
    failure the _CHAT_EXCLUDED comment warns about for tool descriptions, which
    the description-level test could not see because this instruction lives in
    prompt text, not in a description.

    Read over the whole history rather than the current message: /organize is a
    four-step protocol (generate -> confirm -> dry run -> apply) and the user's
    "yes, go ahead" carries no tool name of its own. That is the same reason
    chat_tools() is not scoped per turn.

    ponytail: substring match over cli-origin turns only, so ordinary prose that
    happens to quote a tool name cannot summon it. Compaction that drops the
    directive drops the tools with it and the user re-runs the command; revisit
    if that is ever seen for real.
    """
    if not messages:
        return set()
    text = "\n".join(
        m["content"] for m in messages
        if m.get("origin") == "cli" and isinstance(m.get("content"), str)
    )
    if not text:
        return set()
    # Any tool the directive names, not only the excluded ones: /dedup's
    # directive says "use silica_ledger_next with this run_id", and under the
    # core that tool is not offered until a report has run.
    return {n for n in _eligible() if n not in CHAT_CORE and n in text}


def chat_tools(messages: "list[dict] | None" = None) -> tuple[str, ...]:
    """What an interactive chat turn is offered: the core, then every tool this
    conversation already brought in (loaded or called), then any a slash
    command named. Stable across turns: the order only grows
    at the end, so the prompt prefix a turn shares with the last one survives.

    Deliberately NOT scoped per turn. The vault-review protocol spans turns —
    step 1 reports, step 2 applies via silica_ledger_next after the user agrees —
    so a plain "yes, go ahead" turn still needs the ledger tools. Anything that
    picked tools from the current message alone would strand that second turn.
    """
    eligible = _eligible()
    offered = [n for n in CHAT_CORE if n in eligible]
    for n in _in_play(messages) + sorted(_summoned(messages)):
        if n in eligible and n not in offered:
            offered.append(n)
    return tuple(offered)


def chat_deferred(messages: "list[dict] | None" = None) -> tuple[str, ...]:
    """The loadable tools this conversation has not brought in yet: what the
    loop may add to `chat_tools(messages)` mid-turn."""
    offered = set(chat_tools(messages))
    return tuple(n for n in loadable() if n not in offered)
