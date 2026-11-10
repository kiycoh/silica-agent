# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""RenderEvent -> JSON. The single source of truth for the wire event map, a turn's
terminal frames and the bridge's framing of them included.

Mirrors the table in docs/specs/gui-web.md. Reasoning/thinking events are
dropped in v1 (return None -> the callback skips them).
"""
from __future__ import annotations

from typing import Any

import json

from silica.obs.events import (
    ApprovalRequestEvent,
    BatchRunStartEvent,
    LLMStreamEvent,
    PhaseEvent,
    ToolCompleteEvent,
    ToolErrorEvent,
    ToolStartEvent,
)
from silica.tools import TOOLS, note_keys, tool_failed
from silica.ui.tool_labels import (  # same verb + target the TUI shows
    CHUNK_PHASES,
    FILE_PHASES,
    PHASE_LABELS,
    tool_target,
    tool_verb,
)

def _effect(name: str) -> str:
    """How a tool changes the vault, for the chat footer's grouping: Tool.mutates,
    and a read for everything else, a batch pipeline that names an ops file rather
    than a note among them: it produces no chip."""
    return getattr(TOOLS.get(name), "mutates", "") or "read"


# The notes a search call found (ADR-0121 point 7): an answer that stood on
# search snippets and read no note showed nothing to click. The first three of
# each call, which the chat shows only when no note was read.
_FOUND_PER_CALL = 3


def _found(name: str, result: Any) -> list[str]:
    """The notes a search call returned, best first, as vault paths, read through
    the key the tool declares (Tool.found). A string is a path or recall's store
    key, which has no suffix; a `{path}` item is taken as written, since a body
    search also hits files that are not notes."""
    key = getattr(TOOLS.get(name), "found", "")
    if not key or not isinstance(result, str):
        return []
    try:
        data = json.loads(result)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    paths: list[str] = []
    for item in data.get(key) or []:
        if isinstance(item, str):
            paths.append(item if item.endswith(".md") else item + ".md")
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.append(item["path"])
    return list(dict.fromkeys(paths))[:_FOUND_PER_CALL]


# What the expandable tool card carries. A read of a long note returns the whole
# note, and the transcript would grow a second copy of the vault; the drawer is
# one click away and holds the real thing, so the card only has to show enough to
# recognise WHICH result this was. The number is a character count and not a line
# count on purpose: one minified JSON line and forty lines of frontmatter cost
# the reader the same attention and must cost the frame the same bytes.
_CARD_CHARS = 1200


def _card_text(value: Any) -> dict | None:
    """One side of a tool card: the text, and whether it was cut.

    Cut is reported rather than elided into the string, because "…" at the end of
    a result is indistinguishable from a result that genuinely ends in an
    ellipsis, and the card has to be able to say which happened.
    """
    if value is None:
        return None
    # An argument-less call would otherwise open onto the two characters "{}",
    # which is a disclosure that costs a click to say nothing.
    if isinstance(value, (dict, list)) and not value:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.strip()
    if not text:
        return None
    return {"text": text[:_CARD_CHARS], "cut": max(0, len(text) - _CARD_CHARS)}


def _refusal(name: str, result: Any) -> dict | None:
    """A delete refused because other notes link the note (ADR-0092): the
    line its card shows in place of "delete failed", and the path the card's
    "delete anyway" hands to /delete. None for anything else. The card's
    label already says "not deleted", so the line is the rest of the
    refusal's first line, and the card reads as that line whole."""
    if name != "silica_delete":
        return None
    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("error"):
        return None
    links, path = data.get("linked_from"), data.get("path")
    if not isinstance(links, list) or not links or not isinstance(path, str):
        return None
    n = len(links)
    return {"text": f"{n} {'note links' if n == 1 else 'notes link'} to it",
            "path": path}


def _note_refs(name: str, args: dict) -> list[str]:
    """The notes a call names (read=name, write=path, related=note,
    mindmap=note_path, move/delete=ref). Missing one only omits a chip from the
    chat 'sources' footer, it never reports a wrong note. The live event and a
    reloaded transcript both read this, so the two draw the same chips."""
    # A move leaves the note at `to`, so that is the ref worth offering as a
    # chip; `ref` is a path that no longer resolves once the move lands.
    if name == "silica_move" and isinstance(args.get("to"), str):
        return [args["to"].strip()]
    refs = [args[k].strip() for k in note_keys()
            if isinstance(args.get(k), str) and args[k].strip()]
    paths = args.get("note_paths")
    if isinstance(paths, list):
        refs += [p.strip() for p in paths if isinstance(p, str) and p.strip()]
    return refs


# The injector's two phase tracks, in run order, sent once at tool_start so the
# client can draw the whole pipeline greyed out instead of growing it a row at a
# time. rollback is absent by design: it is an exception branch, and listing it
# made every healthy run display a pending "rollback" step that was never going
# to run.
_PHASE_TRACKS = {
    "file": list(FILE_PHASES.values()),
    "chunk": list(CHUNK_PHASES.values()),
}

# final_status as the FSM writes it -> what the user is told. The FSM emits
# "Success" capitalised and the rest lowercase (states/finalize.py); normalising
# here rather than renaming at the source keeps ingest's _DRAIN_SETTLED and the
# FSM tests on their existing contract.
_STATUS_TEXT: dict[str, tuple[str, str]] = {
    "success":           ("ok", ""),
    "partial":           ("partial", ""),
    "no_ops":            ("empty", "no operations produced"),
    "already_nucleated": ("empty", "already in the vault"),
}


def _injector_summary(result: str | None) -> dict:
    """The completion line of a run_injector call, from its stored result.

    Shared by the live tool_done event and by transcript replay, so a reloaded
    chat states the same outcome it stated while streaming.
    """
    try:
        data = json.loads(result) if isinstance(result, str) else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = str(data.get("final_status") or "unknown")
    kind, reason = _STATUS_TEXT.get(raw.lower(), ("failed", ""))
    failed = [f for f in (data.get("failed_chunks") or []) if isinstance(f, dict)]
    return {
        "kind": kind,               # ok | partial | empty | failed
        "reason": reason,
        "status": raw.lower(),
        "notes": data.get("yield_notes") or 0,
        "links": data.get("yield_links") or 0,
        "files": data.get("files_total") or 0,
        "committed": data.get("chunks_committed") or 0,
        "failed_chunks": [{"chunk": f.get("chunk", ""), "phase": f.get("phase", "")}
                          for f in failed],
    }


def event_to_json(ev) -> dict | None:
    if isinstance(ev, ApprovalRequestEvent):
        # The full arguments, uncut: they are what leaves the machine, and the
        # user's reading of them is the only check (ADR-0091, residual risk).
        return {"type": "approval", "id": ev.id, "server": ev.server, "tool": ev.tool,
                "args": ev.args, "timeout_s": ev.timeout_s}
    if isinstance(ev, LLMStreamEvent):
        return {"type": "delta", "kind": ev.chunk_type, "text": ev.content}
    if isinstance(ev, ToolStartEvent):
        out: dict[str, Any] = {
               "type": "tool_start", "name": tool_verb(ev.name), "id": ev.call_id,
               "target": tool_target(ev.name, ev.args),
               "effect": _effect(ev.name),
               "notes": _note_refs(ev.name, ev.args)}
        # The call's own arguments are the IN half of the expandable card: the
        # one-word target the row prints answers "which note", never "asking
        # what of it", and a recall whose query the reader cannot see is a step
        # they have to take on trust.
        if (card := _card_text(ev.args)):
            out["input"] = card
        if ev.name == "silica_run_injector":
            out["pipeline"] = _PHASE_TRACKS
        return out
    if isinstance(ev, ToolCompleteEvent):
        out = {"type": "tool_done", "name": tool_verb(ev.name), "id": ev.call_id}  # noqa: F841 (same shape)
        # Measured by the loop around the actual call, not by a client timer
        # started when the event was painted: an SSE frame can land a frame late
        # and a browser tab in the background stops scheduling altogether, so a
        # client clock reports the transport's latency as the tool's cost.
        out["ms"] = round(ev.duration_s * 1000)
        if (card := _card_text(ev.result)):
            out["output"] = card
        if ev.name == "silica_run_injector":
            out["summary"] = _injector_summary(ev.result)
        if (found := _found(ev.name, ev.result)):
            out["found"] = found
        # A tool that returns {"error": ...} still completes, and the chat
        # claimed its notes as landed while a reloaded chat, which reads the
        # stored result, drew the same call as failed.
        if tool_failed(ev.result):
            out["failed"] = True
            if (refused := _refusal(ev.name, ev.result)):
                out["refused"] = refused
        if ev.name == "silica_remember":
            # Every write is visible and undoable (ADR-0076 §7): the row's card
            # is collapsed by default, so the write also gets a line of its own.
            from silica.tools.user_memory import parse_result, suggestion

            if (saved := parse_result(ev.result)) is not None:
                out["memory"] = saved
            elif cmd := suggestion(ev.result):
                out["memory_suggest"] = cmd
        return out
    if isinstance(ev, PhaseEvent):
        # The label, not the phase id: the client matches rows by exact string,
        # and no id->label rule it could apply covers hub_update/hub-update.
        # Guessing left the renamed phase grey for the whole run.
        return {"type": "phase", "phase": PHASE_LABELS.get(ev.phase, ev.phase),
                "status": ev.status,
                "scope": ev.scope, "source_file": ev.source_file,
                "file_idx": ev.file_idx, "file_total": ev.file_total,
                "chunk_idx": ev.chunk_idx, "chunk_total": ev.chunk_total}
    if isinstance(ev, ToolErrorEvent):
        # `plain` is the authored sentence (failures.py); `error` stays the
        # tool's own text, which the row discloses under it.
        from silica.ui.web.failures import sentence
        return {"type": "tool_error", "name": tool_verb(ev.name), "id": ev.call_id,
                "error": ev.error, "plain": sentence(str(ev.error))}

    if isinstance(ev, BatchRunStartEvent):
        return {"type": "batch", "kind": ev.kind, "label": ev.label}
    return None  # ReasoningEvent / Thinking* — ignored in v1


def done_frame(answer: str, html: str, meter: dict, **extra: Any) -> dict:
    """A turn's end. `meter` is `session.ctx_meter()`; `extra` carries what the
    turn left under its answer (`stopped`, `notes`, `offer`, `memory`), each only
    when set, because the page tells a turn with a note from one without by the key."""
    return {"type": "done", "answer": answer, "html": html, **meter, **{k: v for k, v in extra.items() if v}}


def error_frame(failure: dict) -> dict:
    """A turn's end in failure: `failures.explain`'s sentence, or a refusal's."""
    return {"type": "error", **failure}


def files_frame(files: list[dict]) -> dict:
    """The bubble's file chips (ADR-0121 point 4)."""
    return {"type": "files", "files": files}


def bridge_frame(item: dict, turn_id: str) -> dict:
    """A turn's frame as the Obsidian bridge sends it (obsidian-silica PROTOCOL.md)."""
    kind = item.get("type")
    if kind == "done":
        return {"type": "chat_done", "turnId": turn_id, "answer": item.get("answer", ""),
                "html": item.get("html", "")}
    if kind == "error":
        return {"type": "chat_error", "turnId": turn_id, "error": item.get("error", "")}
    return {"type": "chat_event", "turnId": turn_id, "event": item}


def tool_calls_to_json(
    msg: dict,
    failed: set[str] | None = None,
    results: dict[str, str] | None = None,
) -> list[dict]:
    """The tool lines of a *stored* assistant message, for transcript replay.

    Same verb + target the live `tool_start` event carries, so reopening a chat
    shows the steps it showed while streaming. Without this the reload dropped
    every tool call and the answer read as if the agent had touched nothing.

    `results` maps tool_call_id -> stored result content. A nucleate run's
    outcome lives only in that result, so without it a reloaded chat showed a
    bare "injector" line for a run that had reported notes, links and failures.
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
        line = {"name": tool_verb(name), "target": tool_target(name, args),
                "error": bool(failed and tc.get("id") in failed),
                # The footer of a replayed turn (audit P4) is built from these
                # two, exactly as the live turn builds it from tool_start.
                "effect": _effect(name), "notes": _note_refs(name, args)}
        if line["error"] and results:
            # Why it failed, in the authored sentence (audit P12): the bare
            # mark said only that something did not land.
            from silica.ui.web.failures import sentence
            raw = str(results.get(tc.get("id", "")) or "")
            if raw:
                line["reason"] = sentence(raw)
            if (refused := _refusal(name, raw)):
                line["refused"] = refused
        if name == "silica_run_injector" and results:
            line["summary"] = _injector_summary(results.get(tc.get("id", "")))
        if results and (found := _found(name, results.get(tc.get("id", "")))):
            line["found"] = found
        out.append(line)
    return out
