# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one list of what the registry holds, and the one call that fills it.

`@tool` registers a function when its module is imported, so what TOOLS holds
used to be whatever had been imported so far: the list of modules sat in
silica/cli.py, and the registry was complete only after importing the REPL.
Every process that reads TOOLS calls `load()` first: the CLI, the web app, an
eval, a script. Nothing below this module calls it, because this module imports
every tool implementation and sits above all of them.
"""
from __future__ import annotations

from silica.tools import REGISTRARS, TOOLS, Tool

# In prompt order: the loader's catalog (tools/loader.py) walks TOOLS, and that
# description is part of the prompt prefix (ADR-0050). load() holds TOOLS to
# this order, so a module some earlier import pulled in cannot move a tool.
_MODULES = (
    "silica.tools.user_memory",
    "silica.tools.atomic",
    "silica.tools.pipeline",
    "silica.tools.notes",
    "silica.tools.events",
    "silica.tools.graph",
    "silica.tools.aliases",
    "silica.tools.curate",
    "silica.tools.sessions",
    "silica.tools.rework",
    "silica.tools.runners",
    "silica.tools.wrapped",
    "silica.tools.codedocs_tool",
    "silica.tools.loader",
    "silica.tools.tabular",
    "silica.sources.web_fetch",
    "silica.sources.web_research",
)

# shortcut: these tools state their facts here and not on their decorators,
# because the files that hold them belong to other lanes of the 2026-11 refactor
# (tools/runners.py is being split, sources/ and the learn tools change under
# them). Fold each row into its decorator once those lanes are merged, by
# 2026-11-20; test_tool_facts counts this table so a row cannot be forgotten.
_DECLARED_HERE: dict[str, dict] = {
    "silica_run_injector": {"ingress": True, "label": ("injector", "inbox_file")},  # an ingested file's text, distilled into the turn
    "silica_ledger_digest": {"ingress": False},
    "silica_dedup_pairs": {"ingress": False},
    "silica_dedup": {"ingress": False},
    "silica_enrich_batch": {"ingress": False},
    "silica_generate_taxonomy": {"ingress": False},
    "silica_run_organizer": {"ingress": False},
    "silica_record_quiz": {"ingress": False, "label": ("record quiz", None)},
    "silica_learn_items": {"ingress": False},
    "silica_review_queue": {"ingress": False, "label": ("review queue", None)},
    "web_fetch": {"ingress": True, "label": ("web fetch", "url")},  # /web: a page's text
    "web_search": {"ingress": True, "label": ("web search", "query")},  # /web: result titles and snippets
    "remember": {"ingress": True},  # /web: a verbatim quote banked from a page (not silica_remember)
    "find_in_page": {"ingress": True},  # /web: lines grepped out of a fetched page
    "plan": {"ingress": False},
    # An answer composed from fetched web pages. The question is the label's
    # target: the chat model rewords the user's own, and this row is the only
    # place that rewording is ever visible. Without it a wrong answer reads as
    # "web search is bad" rather than "it asked something else".
    "silica_web_answer": {"ingress": True, "label": ("web answer", "question"), "reply_flag": "final"},
}

_loaded = False


def _connectors() -> None:
    from silica.connectors.mcp_tools import ensure_registered

    ensure_registered()


def load() -> dict[str, Tool]:
    """Fill TOOLS and return it. Idempotent."""
    global _loaded
    if _loaded:
        return TOOLS
    import importlib

    for module in _MODULES:
        importlib.import_module(module)
    rank = {module: i for i, module in enumerate(_MODULES)}
    ordered = sorted(TOOLS.values(), key=lambda t: rank.get(t.fn.__module__, len(rank)))
    TOOLS.clear()
    TOOLS.update({t.name: t for t in ordered})
    for name, facts in _DECLARED_HERE.items():
        for fact, value in facts.items():
            setattr(TOOLS[name], fact, value)
    # One `mcp__<key>` per configured server (ADR-0091), and not here: it reads
    # the keyring, 0.6 s idle and 3.4 s under load (2026-10-12), so it waits for
    # the first read of the chat's tool set.
    REGISTRARS.append(_connectors)
    _loaded = True
    return TOOLS
