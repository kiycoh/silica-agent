# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a tool call and a run's phases are called on screen, for the TUI and the web alike.
Each tool's verb is its own label (Tool.label); what is here is pure, so the web's event
map loads no terminal renderer."""
from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

from silica.tools import TOOLS
from silica.ui.style import GLYPHS


# Value transforms for targets that are unreadable raw. Applied by both readers
# below, so the next one lands in one place instead of
# growing a third copied branch in each of two near-duplicate functions.
TARGET_FMT: dict[str, Callable[[str], str]] = {
    # The host, not the URL. Live, "which sources" is a site you judge at a
    # glance; the full URL with its title arrives at the end of the turn anyway,
    # in the answer's own `## Sources (web)` block.
    "web_fetch": lambda url: urlsplit(url).netloc or url,
}


def connector_desc(name: str) -> tuple[str, str] | None:
    """An MCP connector (ADR-0091) is named by its server, and its target is
    the request it carried: one per configured server, so no table holds it."""
    return (name[len("mcp__"):], "request") if name.startswith("mcp__") else None


def tool_desc(name: str) -> tuple[str, str | None]:
    """(verb, the argument shown as its target), command-form: `verb "target"`.
    Tool.label, or the name read aloud for a tool that declares none."""
    return (connector_desc(name) or getattr(TOOLS.get(name), "label", None)
            or (name.removeprefix("silica_").replace("_", " "), None))


def tool_verb(name: str) -> str:
    return tool_desc(name)[0]


def tool_target(name: str, args: dict) -> str:
    """The one argument that names what the tool acted on, as plain text.

    Same table as `_synthetic_tool_desc`, without the rich markup, so the web
    transcript can name the note a write touched instead of showing a bare verb.
    """
    if name == "silica_move":
        return f'{args.get("ref", "")} {GLYPHS["arrow"]} {args.get("to", "")}'.strip()
    if name == "silica_run_injector":
        return injector_label(args)
    key = tool_desc(name)[1]
    val = str(args.get(key) or "").strip() if key else ""
    return TARGET_FMT.get(name, str)(val) if val else ""


def _injector_files(args: dict) -> list[str]:
    """The inbox files of a run_injector call, plural key first.

    `inbox_file` is the legacy singular; every real caller passes `inbox_files`
    (the CLI tells the agent to, silica/cli.py). Reading only the singular is why
    the web transcript showed a bare "injector" with no filename at all.
    """
    files = args.get("inbox_files")
    files = [f for f in files if isinstance(f, str) and f.strip()] if isinstance(files, list) else []
    single = args.get("inbox_file")
    if isinstance(single, str) and single.strip() and single not in files:
        files.insert(0, single)
    return files


def injector_label(args: dict) -> str:
    files = _injector_files(args)
    if not files:
        return ""
    extra = f" +{len(files) - 1}" if len(files) > 1 else ""
    return f"{files[0]}{extra}"


# The injector recipe's two nested scopes, as the FSM actually runs them.
# File-scope phases run once per inbox file; the chunk cycle re-enters at
# COLLISION/DELEGATE for every chunk of that file (orchestrator.py) and never at
# RECON. Splitting them is what lets a frontend reset the chunk track per chunk
# without wrongly clearing the file track.
#
# Hardcoded on purpose: this can only drift from an edit to
# recipes/injector.yaml — which tests/test_phase_track.py pins.
FILE_PHASES: dict[str, str] = {
    "recon":      "recon",
    "payload":    "payload",
    "salience":   "salience",
}

CHUNK_PHASES: dict[str, str] = {
    "collision":  "collision",
    "distill":    "distill",
    "sanitize":   "sanitize",
    "validate":   "validate",
    "snapshot":   "snapshot",
    "write":      "write",
    "hub_update": "hub-update",
    "autolink":   "autolink",
    "backlink":   "backlink",
    "lint":       "lint",
    "cleanup":    "cleanup",
}

# rollback carries on_gate_fail in the recipe: an exception branch, not a step.
# It is deliberately absent from _PHASE_ORDER — as the 16th entry it rendered as
# "· rollback" pending on every healthy run, showing "everything went wrong" as
# something about to happen. It is drawn only once it actually fires.
PHASE_LABELS: dict[str, str] = {**FILE_PHASES, **CHUNK_PHASES, "rollback": "rollback"}


PHASE_ORDER: list[str] = [*FILE_PHASES.values(), *CHUNK_PHASES.values()]
