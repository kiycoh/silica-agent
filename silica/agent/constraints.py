# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Optional constraints that turn run_agent into a bounded worker loop.

Carries only the three generic dials (tools, model, iteration cap). The leash is
deliberately NOT here — write safety lives inside the write tool / apply_op, so
run_agent stays domain-agnostic (Rune 1 / ADR set).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConstraints:
    tools: tuple[str, ...]          # subset of TOOLS the loop may expose + dispatch
    model: str | None = None        # override the model arg when set
    max_iterations: int | None = None  # override the default safety cap when set


# Batch/maintenance tools the conversational loop does not need to *choose*: the
# user reaches each one by name through its slash command. Excluding them from
# the chat toolset is worth ~2.4k tokens per turn (~24% of the tool block).
#
# The cut is bounded by one hard constraint, enforced by
# test_chat_tools_keeps_every_recovery_path_it_advertises: tool descriptions name
# each other as follow-ups ("run silica_cooccurrence_refresh first", "use
# silica_curate"). Hiding a tool that a visible tool tells the model to call
# turns that instruction into a dead end. That constraint is what keeps
# silica_curate, silica_dedup and silica_graph_export in the set despite each
# having a slash command, and why this list is 12 entries rather than 30.
#
# ponytail: one explicit list, not a per-tool flag. A tool added later defaults
# to being visible in chat, which costs tokens but never breaks — the safe
# direction. Revisit if this grows past ~20 entries.
_CHAT_EXCLUDED = frozenset({
    "silica_anneal",             # vault-wide maintenance pass, FSM-driven
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
    "silica_run_organizer",      # /organize
})


def chat_tools() -> tuple[str, ...]:
    """Toolset for the interactive chat loop: everything except batch maintenance.

    Deliberately NOT scoped per turn. The vault-review protocol spans turns —
    step 1 reports, step 2 applies via silica_ledger_next after the user agrees —
    so a plain "yes, go ahead" turn still needs the ledger tools. Anything that
    picked tools from the current message alone would strand that second turn.
    """
    from silica.tools import TOOLS

    return tuple(
        n for n, t in TOOLS.items()
        if not t.sensitive and not t.internal and n not in _CHAT_EXCLUDED
    )
