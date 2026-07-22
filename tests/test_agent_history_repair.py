# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Hot-path audit A33 — orphaned assistant tool_calls block must be repaired.

An interrupt or convergence abort mid-dispatch leaves an assistant message with
tool_calls that have no matching `tool` responses; the next call_llm 400s the
whole session. `repair_tool_call_history` backfills synthetic results so history
stays API-valid, and is idempotent.
"""
from silica.agent.loop import repair_tool_call_history


def _assistant(*ids):
    return {"role": "assistant", "tool_calls": [
        {"id": i, "type": "function", "function": {"name": "t", "arguments": "{}"}} for i in ids
    ]}


def test_backfills_fully_unanswered_block():
    msgs = [{"role": "user", "content": "hi"}, _assistant("a", "b")]
    n = repair_tool_call_history(msgs)
    assert n == 2
    tool_ids = [m["tool_call_id"] for m in msgs if m.get("role") == "tool"]
    assert tool_ids == ["a", "b"]


def test_backfills_partial_block_before_next_message():
    # dispatch was interrupted after answering "a"; a new user turn already queued
    msgs = [
        _assistant("a", "b"),
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "user", "content": "next turn"},
    ]
    n = repair_tool_call_history(msgs)
    assert n == 1
    # synthetic "b" result must sit AFTER "a" and BEFORE the new user message
    roles = [(m.get("role"), m.get("tool_call_id")) for m in msgs]
    assert roles == [
        ("assistant", None), ("tool", "a"), ("tool", "b"), ("user", None),
    ]


def test_idempotent_on_healthy_history():
    msgs = [
        _assistant("a"),
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert repair_tool_call_history(msgs) == 0
    assert len(msgs) == 2
