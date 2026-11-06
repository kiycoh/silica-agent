# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a tool call and a run's phases are called on screen, for the TUI and the web alike.
Plain tables and two pure functions, so the web's event map loads no terminal renderer."""
from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

from silica.router.phases import InjectorState
from silica.ui.style import GLYPHS


# tool → (verb, arg key shown as the bold target). Command-form grammar: `verb "target"`.
TOOL_DESC: dict[str, tuple[str, str | None]] = {
    # Two tools, two verbs. Both read "search" until 2026-08-27, and the panel
    # then showed the same `search "boosting"` line answering `{"paths": []}`
    # once and a page of body snippets the next — same badge, same argument,
    # nothing on screen saying one matches titles and the other matches text.
    "silica_search": ("search titles", "query"),
    "silica_search_context": ("search inside", "query"),
    "silica_read_note": ("read", "name"),
    "silica_write_note": ("write note", "path"),
    "silica_patch_note": ("patch note", "name"),
    "silica_flag_note": ("flag note", "name"),
    "silica_require": ("require", "name"),
    "silica_record_quiz": ("record quiz", None),
    "silica_review_queue": ("review queue", None),
    "silica_props": ("props", "name"),
    "silica_outline": ("outline", "name"),
    "silica_links": ("links", "name"),
    "silica_backlinks": ("backlinks", "name"),
    "silica_orphans": ("orphans", None),
    "silica_unresolved": ("unresolved links", None),
    "silica_files": ("list files", "folder"),
    "silica_exists": ("exists", "path"),
    "silica_deferred_list": ("deferred list", None),
    "silica_deferred_flush": ("deferred flush", None),
    "silica_deferred_retry": ("deferred retry", None),
    "silica_anneal": ("anneal", None),
    "silica_inbox_ls": ("inbox", None),
    "silica_recon": ("recon", "inbox_file"),
    "silica_payload": ("payload", "recon_report_path"),
    "silica_sanitize": ("sanitize", "distiller_output_path"),
    "silica_validate_ops": ("validate", "ops_json_path"),
    "silica_bulk_write": ("bulk write", "ops_json_path"),
    "silica_lint": ("lint", "note_name"),
    "silica_run_injector": ("injector", "inbox_file"),
    "silica_delete": ("delete", "ref"),
    "silica_snapshot": ("snapshot", "ops_json_path"),
    "silica_restore": ("restore", "txn_id"),
    "silica_cleanup": ("cleanup", "inbox_file"),
    "web_search": ("web search", "query"),
    "web_fetch": ("web fetch", "url"),
    # The question the chat model handed to the web lane — it rewords the user's
    # own, and this row is the only place that rewording is ever visible. Without
    # it a wrong answer reads as "web search is bad" rather than "it asked
    # something else".
    "silica_web_answer": ("web answer", "question"),
}

# Value transforms for targets that are unreadable raw. Beside _TOOL_DESC and
# applied by both readers below, so the next one lands in one place instead of
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


def tool_verb(name: str) -> str:
    verb, _ = connector_desc(name) or TOOL_DESC.get(
        name, (name.removeprefix("silica_").replace("_", " "), None))
    return verb


def tool_target(name: str, args: dict) -> str:
    """The one argument that names what the tool acted on, as plain text.

    Same table as `_synthetic_tool_desc`, without the rich markup, so the web
    transcript can name the note a write touched instead of showing a bare verb.
    """
    if name == "silica_move":
        return f'{args.get("ref", "")} {GLYPHS["arrow"]} {args.get("to", "")}'.strip()
    if name == "silica_run_injector":
        return injector_label(args)
    _, key = connector_desc(name) or TOOL_DESC.get(name, ("", None))
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


# The injector's two nested scopes, as the FSM actually runs them.
# File-scope phases run once per inbox file; the chunk cycle re-enters at
# COLLISION for every chunk of that file (orchestrator.py) and never at
# RECON. Splitting them is what lets a frontend reset the chunk track per chunk
# without wrongly clearing the file track.
FILE_PHASES: dict[str, str] = {p.id: p.shown for p in InjectorState.phases() if p.scope == "file"}
CHUNK_PHASES: dict[str, str] = {p.id: p.shown for p in InjectorState.phases() if p.scope == "chunk"}

# rollback is an exception branch, not a step.
# It is deliberately absent from PHASE_ORDER: as the 16th entry it rendered as
# "· rollback" pending on every healthy run, showing "everything went wrong" as
# something about to happen. It is drawn only once it actually fires.
PHASE_LABELS: dict[str, str] = {p.id: p.shown for p in InjectorState.phases()}


PHASE_ORDER: list[str] = [*FILE_PHASES.values(), *CHUNK_PHASES.values()]
