# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A history message as the provider sees it.

The history carries silica's own fields beside the OpenAI ones, and the wire is
an allow-list so a field nobody registered stays home: `origin` is turn
provenance, `silica_reasoning` the model's thinking kept so a reopened chat can
replay it (sent back, it would re-bill a multi-thousand-token trace on every
later iteration of the tool loop), and the other `silica_*` keys are what their
owners keep on a turn for a reopened chat. Owners name their keys where they
live; nothing here has to hear about a new one. A list of keys to strip did,
and `silica_story` reached the provider for missing from it.
"""
from __future__ import annotations

SILICA_CLI_OPEN = "<silica-cli>"
SILICA_CLI_CLOSE = "</silica-cli>"

# The OpenAI chat message, plus the two reasoning carriers a route reads back:
# `reasoning_content` is what `llm._call_llm` hands a Jinja route, and
# `thinking_blocks` Anthropic's own record of a turn.
WIRE_KEYS = frozenset({
    "role", "content", "name", "tool_calls", "tool_call_id", "function_call", "refusal", "audio",
    "reasoning_content", "thinking_blocks",
})


def to_wire(msg: dict) -> dict:
    """`msg` with everything but WIRE_KEYS dropped and the CLI marker rendered.

    When ``origin == "cli"`` the content is wrapped in <silica-cli> markers so
    the model can tell a harness directive apart from a human turn. A message
    already in wire form is returned unchanged.
    """
    if msg.keys() <= WIRE_KEYS:
        return msg
    wire = {k: v for k, v in msg.items() if k in WIRE_KEYS}
    if msg.get("origin") == "cli" and wire.get("content"):
        wire["content"] = f"{SILICA_CLI_OPEN}{wire['content']}{SILICA_CLI_CLOSE}"
    return wire


def text_of(content) -> str:
    """The text of a message's content, plain or in parts."""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))
