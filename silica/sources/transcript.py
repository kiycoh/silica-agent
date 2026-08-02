# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Conversation transcripts — the single owner of format parsing.

Producer (`silica capture`) and importer stay dumb: they deposit the vendor's
raw bytes and nothing else. Every format quirk lives here, so when a vendor
changes schema there is exactly one module to fix. Output is a common
intermediate `[(role, text, ts)]`, rendered to speaker-tagged prose for the
distill lane.

Transcript content is untrusted input (ADR-0009, ingress frontier): whatever
leaves this module has been through `strip_degenerate_runs`.
"""
from __future__ import annotations

import json

from silica.kernel.text.sanitize import strip_degenerate_runs

Turn = tuple[str, str, str]  # role, text, iso timestamp


def _text_blocks(content) -> str:
    """The human-readable text of one message, tool noise dropped.

    `content` is a bare string on simple turns and a block list otherwise;
    only `text` blocks survive, which drops tool_use, tool_result and
    thinking without naming them.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _parse_claude_code_jsonl(payload: str) -> list[Turn]:
    turns: list[Turn] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn last line is normal in a live-tailed transcript
        if row.get("type") not in ("user", "assistant") or row.get("isMeta"):
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        text = _text_blocks(message.get("content"))
        if text:
            turns.append((row["type"], text, row.get("timestamp", "")))
    return turns


def _chatgpt_text(message: dict) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return "\n".join(p for p in parts if isinstance(p, str) and p.strip()).strip()


def _chatgpt_thread(mapping: dict, current_node: str) -> list[dict]:
    """The canonical thread: the parent chain from `current_node`, root last.

    An export keeps every regenerated branch in `mapping`; only the chain
    behind the current leaf is the conversation the user actually had. A
    broken chain (missing node, cycle) falls back to chronological order,
    which is wrong-but-complete rather than empty.
    """
    chain: list[dict] = []
    seen: set[str] = set()
    node_id = current_node
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        chain.append(node)
        node_id = node.get("parent")
    if not chain:
        return sorted(
            mapping.values(),
            key=lambda n: ((n.get("message") or {}).get("create_time") or 0.0),
        )
    return list(reversed(chain))


def _parse_chatgpt_mapping(payload: str) -> list[Turn]:
    conversation = json.loads(payload)
    mapping = conversation.get("mapping") or {}
    turns: list[Turn] = []
    for node in _chatgpt_thread(mapping, conversation.get("current_node") or ""):
        message = node.get("message")
        if not isinstance(message, dict):
            continue  # the synthetic root node carries none
        role = (message.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue  # system preambles and tool calls are not the conversation
        text = _chatgpt_text(message)
        if text:
            turns.append((role, text, str(message.get("create_time") or "")))
    return turns


def _parse_claude_ai_messages(payload: str) -> list[Turn]:
    conversation = json.loads(payload)
    turns: list[Turn] = []
    for message in conversation.get("chat_messages") or []:
        role = "user" if message.get("sender") == "human" else "assistant"
        # `text` is the flat field of the classic export; newer ones also carry
        # a block list, and a message can have either.
        text = (message.get("text") or "").strip() or _text_blocks(message.get("content"))
        if text:
            turns.append((role, text, message.get("created_at") or ""))
    return turns


def _parse_silica_session(payload: str) -> list[Turn]:
    """Silica's own sessions. Already the intermediate, minus the tuple shape:
    the producer filtered roles and dropped tool traffic at the source."""
    return [
        (t["role"], t["content"].strip(), t.get("ts") or "")
        for t in json.loads(payload)
        if isinstance(t, dict) and isinstance(t.get("content"), str)
        and t.get("role") in ("user", "assistant") and t["content"].strip()
    ]


_PARSERS = {
    "claude-code-jsonl": _parse_claude_code_jsonl,
    "silica-session": _parse_silica_session,
    "chatgpt-mapping": _parse_chatgpt_mapping,
    "claude-ai-messages": _parse_claude_ai_messages,
}


def parse(envelope: dict) -> list[Turn]:
    """Envelope payload to the common intermediate. Unknown format ⇒ []."""
    parser = _PARSERS.get(envelope.get("format", ""))
    return parser(envelope.get("payload") or "") if parser else []


def render(envelope: dict) -> str:
    """Speaker-tagged prose under a provenance header; "" when there is no
    conversation in the envelope.

    The header is what tells the distiller (and a human reading the staged
    file) where this text came from, since the envelope itself never reaches
    the vault.
    """
    turns = parse(envelope)
    if not turns:
        return ""
    stamps = [ts for _, _, ts in turns if ts]
    header = [
        f"# Conversation ({envelope.get('source', 'unknown')})",
        "",
        f"- session: {envelope.get('session_id', '')}",
    ]
    if envelope.get("title"):
        header.insert(1, f"\n{envelope['title']}")
    if envelope.get("cwd"):
        header.append(f"- cwd: {envelope['cwd']}")
    if stamps:
        header.append(f"- range: {stamps[0]} to {stamps[-1]}")
    body = "\n\n".join(f"**{role}**: {text}" for role, text, _ in turns)
    return strip_degenerate_runs("\n".join(header) + "\n\n" + body)
