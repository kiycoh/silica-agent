# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""How much of the model's window a conversation fills, and what gives way when it is full.

Every surface reads the same meter: the REPL's toolbar, the composer's ring, the
phone's compaction. It counts what is sent, so the tool block a chat ships is in it.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from silica.config import CONFIG, env_raw


@functools.cache
def _cl100k():
    """tiktoken's cl100k_base, read from the copy litellm ships, never fetched.

    litellm points TIKTOKEN_CACHE_DIR at that copy when it is imported, and the
    meter now runs before it is (ADR-0073). Without the variable tiktoken
    downloads the file: offline that is a ProxyError, online a copy in /tmp that
    a reboot deletes. find_spec locates the package without importing it.
    """
    import importlib.util

    spec = importlib.util.find_spec("litellm")
    if spec and spec.submodule_search_locations:
        bundled = Path(spec.submodule_search_locations[0]) / "litellm_core_utils" / "tokenizers"
        if bundled.is_dir():
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(bundled))
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


# What the chat template wraps around each message (`<|im_start|>role\n` ...
# `<|im_end|>\n` on Qwen); OpenAI's own envelope is 3.
_TOKENS_PER_MESSAGE = 4


def count_context_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Pure counter — lets callers (e.g. the web seed prewarm) count a candidate
    message list without clobbering the live session's CONFIG.context_tokens.

    cl100k over each message's text and tool calls, plus the tool block as JSON,
    which is how the Jinja chat templates of the local routes render it. Not
    litellm.token_counter: against llama-server's own prompt_tokens on Qwen3.5-9B
    (2026-10-07) it was 23% short on the seed and 9 to 12% short on the largest
    sessions, against 2% and 4 to 8% here (ADR-0073), and importing litellm to
    count cost the GUI 161 MB at startup. A sum, so parts add up to the total.
    """
    # Counted on the WIRE form: the kept thinking trace would otherwise show up
    # in the meter as context the provider is charged for, when `to_wire` drops
    # it before the request.
    from silica.model.limits import ROUGH_CHARS_PER_TOKEN
    from silica.model.wire import text_of, to_wire

    messages = [to_wire(m) for m in messages]
    try:
        enc = _cl100k()

        def n(text: str) -> int:
            # disallowed_special=(): a note that quotes "<|endoftext|>" is text to
            # count, and tiktoken's default raises on it, which sent the whole
            # window to the fallback below.
            return len(enc.encode(text, disallowed_special=()))

        total = sum(
            n(text_of(m.get("content"))
              + (json.dumps(m["tool_calls"]) if m.get("tool_calls") else ""))
            + _TOKENS_PER_MESSAGE
            for m in messages
        )
        return total + (n(json.dumps(tools)) if tools else 0)
    except Exception:
        # No encoding (a broken tiktoken install): a rough meter beats none.
        return sum(len(text_of(m.get("content"))) for m in messages) // ROUGH_CHARS_PER_TOKEN


def chat_tool_schemas(messages: list[dict]) -> list[dict]:
    """The tool block every interactive turn ships alongside the history.

    Counted because it is *sent*: 51 schemas measured 7.2k tokens on 2026-09-02
    against 2.1k of seed messages, so a meter that totalled the messages alone
    read 2.1k while the provider billed 12.7k for the same turn — the ring was
    reporting a sixth of the window it was there to guard. `chat_tools` is the
    same selector the REPL and the GUI hand to AgentConstraints; a batch run
    (nucleate, injector) ships a different set, but the meter is a chat surface
    and never shows one.
    """
    from silica.agent.constraints import chat_deferred, chat_tools
    from silica.tools import TOOLS

    names = chat_tools(messages)
    # the reach the loop cuts a schema's pointers by, or the meter counts text never sent
    reach = {*names, *chat_deferred(messages)}
    return [TOOLS[n].json_schema(reach) for n in names if n in TOOLS]


def update_context_tokens(messages: list[dict]) -> None:
    CONFIG.context_tokens = count_context_tokens(messages, chat_tool_schemas(messages))


# What put a message in the window. `tool_io` takes the assistant turns that
# carry tool_calls as well as the results: the arguments are what the model paid
# to emit, and an assistant message holding a call carries no prose worth billing
# to the conversation.
def _context_group(m: dict) -> str:
    role = m.get("role")
    if role == "system":
        return "system"
    if role == "tool" or (role == "assistant" and m.get("tool_calls")):
        return "tool_io"
    return "messages"


def context_breakdown(messages: list[dict]) -> dict[str, int]:
    """The same window `count_context_tokens` totals, split by what filled it.

    The counter is a sum over messages plus the tool block, so the parts add up
    to the total beside them by construction. The tool block is its own part
    rather than folded into the system prompt: it is the single biggest resident
    of an idle window (7.2k against 2.1k of instructions), and a part that large
    hidden inside another is the reading the panel exists to prevent.
    """
    groups: dict[str, list[dict]] = {"system": [], "tool_io": [], "messages": []}
    for m in messages:
        groups[_context_group(m)].append(m)
    counts = {k: (count_context_tokens(v) if v else 0) for k, v in groups.items()}
    schemas = chat_tool_schemas(messages)
    counts["tool_specs"] = count_context_tokens([], schemas) if schemas else 0
    return counts


def compact_context(messages: list[dict], collapsed: set[int], tokens: int) -> set[int]:
    """Collapse old read-tool results once the window's `tokens` cross the budget.

    The between-turns sweep; the agent loop runs the same pass per iteration
    (see run_agent). Loss is recoverable: each stub names the call to re-issue.
    """
    from silica.agent.compaction import (
        COMPACT_FLOOR_TURNS,
        COMPACT_FRACTION,
        compact_read_history,
    )
    from silica.tools import TOOLS

    return compact_read_history(
        messages,
        collapsed,
        prompt_tokens=tokens,
        budget=int(COMPACT_FRACTION * CONFIG.max_context_tokens),
        floor_turns=COMPACT_FLOOR_TURNS,
        tools=TOOLS,
    )


def recount(conv, shown: bool = True) -> None:
    """A turn's end: count the window, sweep it if that crossed the budget, and
    count again when anything collapsed so the meter reflects the slimmer history.

    `shown` says this is the conversation the meter draws: the phone's window
    (ADR-0089 amendment) counts for its own compaction and must not move the
    browser's ring.
    """
    tokens = count_context_tokens(conv.messages, chat_tool_schemas(conv.messages))
    updated = compact_context(conv.messages, conv.collapsed, tokens)
    if updated != conv.collapsed:
        tokens = count_context_tokens(conv.messages, chat_tool_schemas(conv.messages))
    conv.collapsed = updated
    if shown:
        CONFIG.context_tokens = tokens


def resolve_context_budget() -> None:
    """Size the context meter to the live model's window, at boot and whenever
    Settings changes the model.

    SILICA_MAX_CONTEXT, when set, is the window on every provider. Locally it
    stands in for a served window silica can't reliably introspect (LM Studio,
    Ollama). On OpenRouter it is a price decision, a laptop's as much as the
    host's: a 32k cap cost 62% less a chat than the 1M window at equal answers
    (docs/evaluation/context-cap-2026-10-13.md). Until 2026-10-27 a laptop's
    pin was ignored on OpenRouter, so a 65536 in ~/.silica/.env ran DeepSeek
    at 1M. Falls back to the static default when the provider is unreachable.
    """
    if not CONFIG.model.strip():
        return
    pin = env_raw("SILICA_MAX_CONTEXT")
    if pin and CONFIG.provider != "openrouter":
        CONFIG.max_context_tokens = int(pin)
        return
    from silica.model.limits import model_limits
    window, _ = model_limits(CONFIG.provider, CONFIG.model)
    if window:
        # OpenRouter's window is the one it serves, so a pin left from a larger
        # model is cut to it instead of sending prompts the provider refuses.
        # From the pin and not the field: the last model may have cut the field.
        CONFIG.max_context_tokens = min(window, int(pin)) if pin else window
