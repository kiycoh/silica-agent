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
import re
from typing import Any

MIN_COLLAPSE_CHARS = 200  # don't collapse a body smaller than its own stub

# Fixed knobs (config promotion declined 2026-08-19; revisit only with a real
# tuner). Here rather than in the CLI because the agent loop compacts too.
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

    Non-JSON / non-dict payloads pass through unchanged so a tool that returns
    a bare string is never corrupted. `tool` is accepted (and ignored) to keep
    the call shape uniform with read_stub.
    """
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str
    if not isinstance(parsed, dict):
        return result_str
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


def _read_view(arguments: str) -> str:
    """The part of a read's arguments that is NOT the note: the slice it asked
    for (start/end/section/max_chars...), canonical so key order is irrelevant.
    "" for a whole-note read. Two reads are the same read only when note AND
    view match: on the live run d08dccb60587 (2026-09-22) every `silica_read`
    of another line range of Lezione 32 collapsed the previous range as
    "superseded", so the model re-read the same slices five times over and
    never answered."""
    try:
        args = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(args, dict):
        return ""
    rest = {k: v for k, v in args.items() if k not in _NOTE_ARG_KEYS and v not in ("", None)}
    return json.dumps(rest, sort_keys=True, ensure_ascii=False) if rest else ""


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
                  redundant. Keyed per tool AND per view, not per note:
                  `silica_related` on a note the model read earlier answers a
                  different question, and another line range of the same note
                  is another read; neither supersedes anything.

    Only the latest occurrence of each key matters, so one forward pass.
    """
    last_mutation: dict[str, int] = {}
    last_read: dict[tuple[str, str, str], int] = {}
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
                last_read[(name, key, _read_view(fn.get("arguments", "{}")))] = i

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
        elif last_read.get((name, key, _read_view(arguments)), -1) > j:
            out[j] = "— a later call of the same read superseded it"
    return out


_STUB_HEAD = "⟪silica: result elided"


def relevance_candidates(messages: list[dict], tools: dict,
                         min_chars: int) -> list[tuple[int, str, str]]:
    """(index, tool name, arguments) of the results a relevance pass may judge:
    lazy reads answered before the current user message, at least `min_chars`
    long and not already a stub. Writes are projected at emission and hold
    nothing worth judging; the current turn is still being acted on."""
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(users) < 2:
        return []
    id_to_call: dict[str, tuple[str, str]] = {}
    out: list[tuple[int, str, str]] = []
    for i, m in enumerate(messages[:users[-1]]):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                id_to_call[tc.get("id", "")] = (fn.get("name", ""), fn.get("arguments", "{}"))
            continue
        content = m.get("content")
        if m.get("role") != "tool" or not isinstance(content, str) or len(content) < min_chars:
            continue
        if content.startswith(_STUB_HEAD):
            continue
        name, arguments = id_to_call.get(m.get("tool_call_id", ""), ("", "{}"))
        if getattr(tools.get(name), "collapse", None) == "lazy":
            out.append((i, name, arguments))
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
        # A stub from another pass (relevance, a caller's earlier sweep) holds
        # nothing left to elide; rewriting it only moves the cache break.
        if i in updated or str(messages[i].get("content") or "").startswith(_STUB_HEAD):
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
        if str(m.get("content")).startswith(_STUB_HEAD):
            continue
        name, arguments = id_to_call.get(m.get("tool_call_id", ""), ("", "{}"))
        tool = tools.get(name) if name else None
        if tool is None or getattr(tool, "collapse", "lazy") != "lazy":
            continue
        m["content"] = read_stub(name, arguments, m["content"])
        updated.add(i)
    return updated


# What new text will cost is counted at 2 chars a token, so the estimate does
# not come in under the request. chars // 3 was assumed to overestimate and does
# not: Qwen3.5 tokenizes the test vault's converted lecture slides at 2.05 to
# 2.41 chars a token, 26 of 30 notes under 3 (2026-10-01), and a request judged
# to fit overflowed a 32k window by 820 tokens. Text under 2 chars a token (CJK,
# minified code) can still overflow; llama.cpp's 400 then names the real count
# and the loop resends once (loop.py). clamp_max_tokens counts input the same way.
TEXT_CHARS_PER_TOKEN = 2
# A collapsed read is credited at chars // 3: under what it frees on that dense
# text, so the estimate after a collapse stays high; over by a twelfth on English
# prose (~4 chars a token), which the 2-char count on the added side absorbs.
_CHARS_PER_TOKEN = 3


def fit_window(
    messages: list[dict],
    collapsed: set[int],
    *,
    estimate: int,
    limit: int,
    tools: dict,
) -> tuple[set[int], int]:
    """Collapse lazy read results, oldest first and floor included, until the
    request about to be sent is estimated under `limit` tokens.

    compact_read_history cannot prevent an overflow: it measures the request
    already sent and never touches the last COMPACT_FLOOR_TURNS turns, so one
    batch of fat reads lands whole in the next request. On a local window that
    request is rejected (llama.cpp: 400 exceed_context_size_error) and the turn
    dies; a stub the model can re-issue keeps it alive. Returns the collapsed
    set and the estimate left after the collapses.
    """
    id_to_name: dict[str, tuple[str, str]] = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            id_to_name[tc.get("id", "")] = (fn.get("name", ""), fn.get("arguments", "{}"))
    updated = set(collapsed)
    for i, m in enumerate(messages):
        if estimate <= limit:
            break
        body = str(m.get("content") or "")
        if (i in updated or m.get("role") != "tool" or len(body) <= MIN_COLLAPSE_CHARS
                or body.startswith(_STUB_HEAD)):
            continue
        name, arguments = id_to_name.get(m.get("tool_call_id", ""), ("", "{}"))
        tool = tools.get(name) if name else None
        if tool is None or getattr(tool, "collapse", "lazy") != "lazy":
            continue
        m["content"] = read_stub(name, arguments, body, "to fit the context window")
        estimate -= (len(body) - len(m["content"])) // _CHARS_PER_TOKEN
        updated.add(i)
    return updated, estimate


# One turn's recalls overlap: six calls on the study-plan turn handed the model
# 87 windows for 56 notes, 8 of them byte-identical (docs/evaluation/
# study-plan-rubric-2026-09-22.md). Collapsed BY EXCERPT, never by note: a
# second query can return a complementary passage of the same note, and that
# one must survive. The header stays so rank and path remain visible.
_RECALL_HEAD = re.compile(r"^\[#(\d+) \| ([^|\]\n]+)((?: \| [^\]\n]*)?)\]$", re.M)
_SAME_EXCERPT = "(same excerpt as an earlier recall this turn)"


def dedup_recall_excerpts(result_str: str, seen: set[str]) -> str:
    """Stub excerpts already delivered this turn; `seen` is the turn's memo.

    Non-JSON and error replies pass through unchanged; a reply with no
    context (an error, an empty vault) has nothing to collapse."""
    try:
        d = json.loads(result_str)
    except (ValueError, TypeError):
        return result_str
    if not isinstance(d, dict) or not isinstance(d.get("context"), str) or not d["context"]:
        return result_str
    c = d["context"]
    heads = list(_RECALL_HEAD.finditer(c))
    if not heads:
        return result_str
    out, pos, changed = [], 0, False
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(c)
        body = c[h.end():end]
        # The block separator ("\n\n---\n\n") belongs to the layout, not the excerpt.
        sep_at = body.rfind("\n\n---\n\n")
        excerpt, sep = (body[:sep_at], body[sep_at:]) if sep_at >= 0 else (body, "")
        key = excerpt.strip()
        if key and key in seen:
            out.append(c[pos:h.end()] + "\n" + _SAME_EXCERPT + sep)
            changed = True
        else:
            seen.add(key)
            out.append(c[pos:end])
        pos = end
    out.append(c[pos:])
    if not changed:
        return result_str
    d["context"] = "".join(out)
    return json.dumps(d, ensure_ascii=False)
