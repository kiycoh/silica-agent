# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Refine capability — stylistic rewrite of a note under the anti-info-loss bounds."""
from __future__ import annotations

import logging
import os
from typing import Any

from silica.kernel.workqueue import WorkItem
from silica.capabilities._base import (
    NoteContent, load_prompt, parse_content, run_note_rewrite,
)

logger = logging.getLogger(__name__)


def run_refine(item: WorkItem, config: Any) -> dict[str, Any]:
    return run_note_rewrite(
        item, config,
        reason="stylistic refine",
        worker_label="refiner",
        hub=item.context.get("hub"),
        skip_empty=True,
        rewrite=lambda path, original, hub: _refine_note(config, path, original),
    )


def _refine_note(config: Any, target_path: str, original: str) -> NoteContent:
    from silica.agent.providers import get_provider

    prompt = load_prompt("refiner_prompt.txt") + "\n\n" + load_prompt("_anti_slop.txt")
    user_message = f"{prompt}\n\n---\nNOTE ({target_path}):\n{original}\n"
    provider = get_provider(config, role="worker")
    response = provider.call_llm(
        messages=[{"role": "user", "content": user_message}],
        tools=None,
        response_schema=NoteContent,
        max_tokens=int(os.getenv("MAX_TOKENS", "32768")),
    )
    return NoteContent(content=parse_content(response.text or ""))
