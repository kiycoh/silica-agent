# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""RenderEvent -> JSON. The single source of truth for the wire event map.

Mirrors the table in docs/spec-gui-web.md. Reasoning/thinking events are
dropped in v1 (return None -> the callback skips them).
"""
from __future__ import annotations

import json

from silica.agent.events import (
    BatchRunStartEvent,
    LLMStreamEvent,
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
)
from silica.ui.renderer import _tool_target, _tool_verb  # same verb + target the TUI shows

# How a tool changes the vault, for the chat footer's grouping. Only the tools
# that mutate notes are listed; everything else is a read. Deliberately keyed on
# the note-touching surface (the tools whose args `_note_refs` can resolve), so a
# batch pipeline that names an ops file rather than a note produces no chip and
# needs no entry here.
_TOOL_EFFECT: dict[str, str] = {
    "silica_write_note": "written",
    "silica_patch_note": "written",
    "silica_flag_note": "written",
    "silica_bulk_write": "written",
    "silica_restore": "written",
    "silica_delete": "deleted",
    "silica_move": "moved",
}

# Arg keys that name a note across the tool surface (read=name, write=path,
# related=note, mindmap=note_path, move/delete=ref). A small allowlist, not
# per-tool logic: missing one only omits a chip from the chat 'sources' footer,
# it never reports a wrong note.
_NOTE_KEYS = ("name", "path", "note", "note_path", "ref")


def _note_refs(args: dict) -> list[str]:
    refs = [args[k].strip() for k in _NOTE_KEYS
            if isinstance(args.get(k), str) and args[k].strip()]
    paths = args.get("note_paths")
    if isinstance(paths, list):
        refs += [p.strip() for p in paths if isinstance(p, str) and p.strip()]
    return refs


def event_to_json(ev) -> dict | None:
    if isinstance(ev, LLMStreamEvent):
        return {"type": "delta", "kind": ev.chunk_type, "text": ev.content}
    if isinstance(ev, ToolStartEvent):
        # A move leaves the note at `to`, so that is the ref worth offering as a
        # chip; `ref` is a path that no longer resolves once the move lands.
        notes = _note_refs(ev.args)
        if ev.name == "silica_move" and isinstance(ev.args.get("to"), str):
            notes = [ev.args["to"].strip()]
        return {"type": "tool_start", "name": _tool_verb(ev.name), "id": ev.call_id,
                "target": _tool_target(ev.name, ev.args),
                "effect": _TOOL_EFFECT.get(ev.name, "read"),
                "notes": notes}
    if isinstance(ev, ToolCompleteEvent):
        return {"type": "tool_done", "name": _tool_verb(ev.name), "id": ev.call_id}
    if isinstance(ev, ToolErrorEvent):
        return {"type": "tool_error", "name": _tool_verb(ev.name), "id": ev.call_id, "error": ev.error}
    if isinstance(ev, BatchRunStartEvent):
        return {"type": "batch", "kind": ev.kind, "label": ev.label}
    return None  # ReasoningEvent / Thinking* — ignored in v1


def tool_calls_to_json(msg: dict, failed: set[str] | None = None) -> list[dict]:
    """The tool lines of a *stored* assistant message, for transcript replay.

    Same verb + target the live `tool_start` event carries, so reopening a chat
    shows the steps it showed while streaming. Without this the reload dropped
    every tool call and the answer read as if the agent had touched nothing.
    """
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        out.append({"name": _tool_verb(name), "target": _tool_target(name, args),
                    "error": bool(failed and tc.get("id") in failed)})
    return out
