# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Shared building blocks for capabilities.

A capability is a plain ``run(item, config) -> dict`` function living in its own
module. The behaviours share a small skeleton — emit a feedback phase, read the
target note (or skip), check the cancel token — so those steps live here as free
functions each ``run()`` composes, keeping the per-behaviour variation explicit
rather than hidden in a base-class template method.
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from silica.kernel.workqueue import WorkItem

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class NoteContent(BaseModel):
    """The structured result of a whole-body authoring decision (expand / codewiki)."""

    content: str = ""


def emit_feedback(item: WorkItem, phase: str, detail: str = "") -> None:
    """Publish a WorkFeedbackEvent to the global bus (best-effort)."""
    from silica.agent.bus import BUS
    from silica.agent.events import WorkFeedbackEvent
    from silica.agent import narration as _narr_mod
    BUS.publish("work/feedback", WorkFeedbackEvent(item.id, item.kind, phase, detail))
    # The work lane (spec §4): repeated running beats on one id are progress
    # updates; the terminal lands in consume() where the outcome exists.
    _narr_mod.NARRATOR.narrate(
        "work", "running", f"{item.kind} {phase}" + (f": {detail}" if detail else ""),
        {"kind": item.kind, "phase": phase, "detail": detail}, id=f"wk-{item.id}")


def read_or_skip(path: str) -> tuple[str, dict | None]:
    """Read a note body. Returns ``(body, None)`` on success, or
    ``("", {"status": "skipped", ...})`` if the note is unreadable.

    The failure body is "" and not None on purpose: a tuple return cannot say
    "the body is a str exactly when skip is None", so every caller was left
    holding an Optional it then had to re-narrow. Both are falsy and every
    caller either returns on `skip` or tests the body for truth, so the two
    spellings were already interchangeable — this one is checkable.
    """
    from silica.driver import DRIVER
    try:
        return DRIVER.read_note(path).content or "", None
    except Exception as e:
        return "", {"status": "skipped", "reason": f"unreadable: {e}"}


def load_prompt(name: str) -> str:
    path = _PROMPT_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def parse_content(raw: str, key: str = "content") -> str:
    """Extract one string key from a structured authoring response.

    Returns ``""`` on any parse failure — the callers' no-op signal.
    """
    from silica.kernel.text.sanitize import parse_json
    try:
        parsed, _ = parse_json(raw, strict=False)
        if isinstance(parsed, dict) and key in parsed:
            return str(parsed[key])
    except Exception as e:
        logger.debug("content parse failed: %s", e)
    return ""
