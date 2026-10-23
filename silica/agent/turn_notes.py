# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Every line drawn under a chat answer goes through this one channel (ADR-0094).

A turn note is `{"kind": ..., "text": ...}`, stored under `KEY` on the turn's
final assistant message. On the message and not in the `done` frame alone,
because the frame is never stored: the thin-coverage hint it used to carry was
gone from every reloaded chat. The message is what the narration beat records,
what `/messages` reads and what `/session/load` rebuilds, and both compactions
rewrite only tool results, so a note survives every path the answer does.
`providers.INTERNAL_KEYS` keeps it off the wire.
"""
from __future__ import annotations

KEY = "silica_turn_notes"

# Cause before effect: a retrieval leg that was missing (ADR-0094) can explain
# a number the answer could not ground (ADR-0095), and either can explain a
# graph check that came up short (ADR-0092); never the other way round. A
# proposed skill (ADR-0127) explains nothing about the answer, so it is last.
KINDS = ("retrieval", "numbers", "graph", "skill")


def ordered(notes: list[dict]) -> list[dict]:
    """`notes` sorted by `KINDS`, two notes of one kind kept as given.

    An unknown kind raises rather than sorting last: a new kind's place in the
    order is a decision, and it is made by adding it to `KINDS`.
    """
    for note in notes:
        if note.get("kind") not in KINDS:
            raise ValueError(f"unknown turn note kind: {note.get('kind')!r}")
    return sorted(notes, key=lambda n: KINDS.index(n["kind"]))


def attach(messages: list[dict], notes: list[dict]) -> list[dict]:
    """Order `notes` and store them on the turn's answer; returns them ordered.

    Called after attribution and figures and before the final narration beat,
    so the beat stores them. Only an assistant message with no tool calls is an
    answer: after a stopped turn `messages[-1]` can be a tool-calling step, and
    the caller still draws the returned notes live.
    """
    notes = ordered(notes)
    last = messages[-1] if messages else None
    if notes and last is not None and last.get("role") == "assistant" and not last.get("tool_calls"):
        last[KEY] = notes
    return notes


def of(message: dict) -> list[dict]:
    """The notes a stored message carries, [] for one with none."""
    return list(message.get(KEY) or [])
