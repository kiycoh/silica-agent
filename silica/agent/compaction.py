# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Context-window hygiene for the top-level agent loop.

Pure, I/O-free, LLM-free helpers. Three levers:
  • eager projection — write/gate tool results are summarised at emission so
    the fat JSON never enters the message history (the TUI event still gets it).
  • invalidation — a read the conversation has already made WRONG (its note was
    edited afterwards) or REDUNDANT (the same read was re-issued) is collapsed
    whatever the budget says. This is correctness, not thrift: a pre-patch body
    left verbatim in history is something the model can still reason from.
  • lazy compaction — remaining old read-tool results are rewritten in place to
    elision stubs once the context crosses a token budget, protecting the last
    K turns.

Loss is recoverable in every case: the stub names the call to re-issue and
keeps the fields small enough to survive (paths, counts), so the model can tell
whether it wants the body back without paying a round trip to find out.

Nothing here touches the network, the disk, or any global state.
"""
from __future__ import annotations

import json
from typing import Any

MIN_COLLAPSE_CHARS = 200  # don't collapse a body smaller than its own stub

# ponytail: fixed knobs, promote to Config only if someone actually needs to tune
# them. Here rather than in the CLI because the agent loop compacts too.
COMPACT_FRACTION = 0.6   # collapse old reads once history crosses 60% of the window
COMPACT_FLOOR_TURNS = 3  # the last N assistant turns are never collapsed
STUB_FIELD_CHARS = 500   # per-field budget in a collapsed read stub

# Tools whose call invalidates any earlier read of the same note: once one of
# these has run, the body sitting in history is factually wrong, not merely old.
MUTATING_TOOLS = frozenset({
    "silica_write_note",
    "silica_patch_note",
    "silica_flag_note",
    "silica_move",
    "silica_delete",
})

# Arg keys that name a note, in resolution order — the tools disagree on the
# spelling (`name` for reads and patches, `path` for writes, `ref` for
# move/delete, `note` for the graph lane).
_NOTE_ARG_KEYS = ("name", "note", "path", "ref")


def generic_projection(result: dict) -> str:
    """Conservative fallback stub for an eager tool with no `summarize`.

    Keeps scalars and short strings verbatim; elides long collections to a
    `<N items>` placeholder. Appends a hint that the full body is recoverable —
    only when something was actually elided. A result made of scalars survives
    whole, and telling the model to re-call a lossless stub is an instruction to
    redo work it already finished: the agent read "notes_processed=1 … ⟨↻
    re-call to expand⟩" off a completed batch autolink and re-ran it six times.
    """
    parts: list[str] = []
    elided = False
    for k, v in result.items():
        if isinstance(v, (list, dict)) and len(v) > 3:
            parts.append(f"{k}=<{len(v)} items>")
            elided = True
        elif isinstance(v, str) and len(v) > 80:
            parts.append(f"{k}=<{len(v)} chars>")
            elided = True
        else:
            parts.append(f"{k}={v}")
    body = "; ".join(parts)
    return f"{body} ⟨↻ re-call to expand⟩" if elided else body


def read_projection(result_str: str) -> str:
    """Keep the small fields of a read result, elide only the fat ones.

    A recall answers with a huge rendered `context` next to a small `notes`
    list of paths; a bare stub throws both away, so the model has to re-call
    just to learn which notes it already saw. Every field that still fits
    STUB_FIELD_CHARS survives verbatim — the paths, the query, the counts —
    which is what the model actually reads to decide whether it wants the body
    back. Returns "" for a non-JSON / non-dict body (a plain note body has no
    fields to keep).
    """
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    parts: list[str] = []
    for k, v in parsed.items():
        rendered = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        if len(rendered) <= STUB_FIELD_CHARS:
            parts.append(f"{k}={rendered}")
        elif isinstance(v, (list, dict)):
            parts.append(f"{k}=<{len(v)} items>")
        else:
            parts.append(f"{k}=<{len(rendered)} chars>")
    return "; ".join(parts)


def read_stub(
    tool_name: str,
    arguments: str,
    content: str = "",
    why: str = "to save context",
) -> str:
    """Elision marker for a collapsed read result. Names the call to re-issue.

    `why` is the reason the body went away — a stale read is a different fact
    from a budget eviction, and the model should not re-issue a call whose
    answer the conversation has already superseded.
    """
    head = (
        f"⟪silica: result elided {why} — "
        f"re-call {tool_name} with {arguments} to view again"
    )
    kept = read_projection(content)
    return f"{head} · kept: {kept}⟫" if kept else f"{head}⟫"


def eager_stub(tool: Any, result_str: str) -> str:
    """Project a write/gate tool's JSON result to its one-line summary.

    Uses the tool's own `summarize(dict)->str` when declared, else a generic
    projection. Non-JSON / non-dict payloads pass through unchanged so a tool
    that returns a bare string is never corrupted.
    """
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str
    if not isinstance(parsed, dict):
        return result_str
    summarize = getattr(tool, "summarize", None)
    if summarize is not None:
        try:
            return summarize(parsed)
        except Exception:
            return generic_projection(parsed)
    return generic_projection(parsed)


def _note_key(arguments: str) -> str:
    """Note identity from a tool call's JSON arguments, or "" if it names none.

    Reads take a wikilink name ("Computer Vision"), writes take a vault path
    ("Computer Science/Computer Vision.md"), so both reduce to the lowercased
    stem to compare.

    # ponytail: stem only, so two same-named notes in different folders collide
    # into one key — the same collision the vault's own wikilink resolution
    # already has, and a false collapse costs one re-call, not data. Compare
    # full relative paths if that ever shows up in practice.
    """
    try:
        args = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(args, dict):
        return ""
    for k in _NOTE_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            stem = v.strip().replace("\\", "/").rsplit("/", 1)[-1]
            return stem.removesuffix(".md").lower()
    return ""


def _invalidated_reads(
    messages: list[dict],
    id_to_call: dict[str, tuple[str, str]],
    tools: dict,
) -> dict[int, str]:
    """Map tool-result index -> reason, for reads the conversation has voided.

    Two provable cases, both read straight off the call sequence and neither
    needing a token count:

      STALE       a mutating tool touched the note after the read, so the body
                  in history no longer describes the note on disk.
      SUPERSEDED  the *same* read was re-issued later, so the earlier copy is
                  redundant. Keyed per tool, not per note: `silica_related` on
                  a note the model read earlier answers a different question
                  and supersedes nothing.

    Only the latest occurrence of each key matters, so one forward pass.
    """
    last_mutation: dict[str, int] = {}
    last_read: dict[tuple[str, str], int] = {}
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            key = _note_key(fn.get("arguments", "{}"))
            if not key:
                continue
            if name in MUTATING_TOOLS:
                last_mutation[key] = i
            elif getattr(tools.get(name), "collapse", None) == "lazy":
                last_read[(name, key)] = i

    if not last_mutation and not last_read:
        return {}

    out: dict[int, str] = {}
    for j, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        if len(m.get("content") or "") <= MIN_COLLAPSE_CHARS:
            continue
        name, arguments = id_to_call.get(m.get("tool_call_id", ""), ("", "{}"))
        tool = tools.get(name) if name else None
        if tool is None or getattr(tool, "collapse", "lazy") != "lazy":
            continue
        key = _note_key(arguments)
        if not key:
            continue
        # The call that produced this result sits at an index below j, so a
        # strictly-greater index is always a *later* turn — never itself.
        if last_mutation.get(key, -1) > j:
            out[j] = "— the note was edited afterwards, so this copy is stale"
        elif last_read.get((name, key), -1) > j:
            out[j] = "— a later call of the same read superseded it"
    return out


def compact_read_history(
    messages: list[dict],
    collapsed: set[int],
    prompt_tokens: int,
    budget: int,
    floor_turns: int,
    tools: dict,
) -> set[int]:
    """Collapse read-tool results that are void, then old ones when over budget.

    Pass 1 is event-driven and unconditional: a read the conversation has made
    stale or redundant is collapsed at any budget, because leaving it verbatim
    is a correctness hazard, not just a cost. It ignores the recency floor for
    the same reason — a body contradicted one turn ago is no safer than one
    contradicted fifty turns ago.

    Pass 2 is the budget sweep, strategy (i): when triggered, collapse *all*
    remaining eligible lazy reads beyond the recency floor in one go —
    self-hysteresing, no per-message estimation. Eager/never/unknown tools and
    bodies <= MIN_COLLAPSE_CHARS are left alone by both passes.
    """
    # Map tool_call_id -> (name, arguments) from assistant messages; collect turn markers.
    id_to_call: dict[str, tuple[str, str]] = {}
    assistant_indices: list[int] = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            assistant_indices.append(i)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                id_to_call[tc.get("id", "")] = (fn.get("name", ""), fn.get("arguments", "{}"))

    updated = set(collapsed)
    for i, why in _invalidated_reads(messages, id_to_call, tools).items():
        if i in updated:
            continue
        name, arguments = id_to_call.get(messages[i].get("tool_call_id", ""), ("", "{}"))
        messages[i]["content"] = read_stub(name, arguments, messages[i]["content"], why)
        updated.add(i)

    if prompt_tokens <= budget:
        return updated
    if len(assistant_indices) <= floor_turns:
        return updated  # not enough turns for anything to be "old"

    boundary = assistant_indices[-floor_turns]
    for i, m in enumerate(messages):
        if i >= boundary:
            break
        if i in updated or m.get("role") != "tool":
            continue
        if len(m.get("content") or "") <= MIN_COLLAPSE_CHARS:
            continue
        name, arguments = id_to_call.get(m.get("tool_call_id", ""), ("", "{}"))
        tool = tools.get(name) if name else None
        if tool is None or getattr(tool, "collapse", "lazy") != "lazy":
            continue
        m["content"] = read_stub(name, arguments, m["content"])
        updated.add(i)
    return updated
