# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Enrich capability — semantic expansion of a note under the anti-info-loss bounds."""
from __future__ import annotations

import logging
import os
from typing import Any

from silica.kernel.workqueue import WorkItem
from silica.capabilities._base import (
    NoteContent, load_prompt, parse_content, run_note_rewrite,
)

logger = logging.getLogger(__name__)


def run_enrich(item: WorkItem, config: Any) -> dict[str, Any]:
    # The hub reaches refiner_bounds as a FORBIDDEN path, and allows_path expands
    # a bare entry against the incoming path's basename — so defaulting it to the
    # target's own title made every note forbid itself and the enriching
    # overwrite was always dropped as out of bounds. No declared hub => None,
    # exactly as run_refine does.
    hub = item.context.get("hub") or None
    return run_note_rewrite(
        item, config,
        reason="semantic enrichment",
        worker_label="enricher",
        hub=hub,
        rewrite=lambda path, original, h: _enrich_note(config, path, original, h),
    )


def _enrich_note(config: Any, target_path: str, original: str, hub: str | None) -> NoteContent:
    from silica.agent.providers import get_provider
    from silica.kernel.context_builder import build_context

    rules = [
        "1. Produce a rigorous, complete, and exhaustive academic text.",
        "2. Preserve all factual information and concepts already present in the note (anti-deletion policy). Do not remove pre-existing information, but expand upon it.",
        "3. Perform structuring in Obsidian Flavored Markdown: use callouts (> [!tip], > [!note]), LaTeX equation blocks ($$ ... $$) if appropriate, lists, and bold text.",
    ]
    # Only a real parent earns the wikilink rule: with no declared hub the note
    # would be told to link to itself.
    if hub:
        rules.append(
            f"{len(rules) + 1}. You must include a wikilink [[{hub}]] to the hub/parent note"
            " (for example in a final section called '# Relations' or '# Connections')."
        )
    rules.append(
        f"{len(rules) + 1}. Return the result structured in JSON format containing a single key"
        " 'content' with the full body of the note (including normalized and updated YAML"
        " frontmatter tags, and the enriched body)."
    )

    system_prompt = (
        "You are an academic assistant expert in writing and structuring notes in Obsidian Flavored Markdown (OFM) in English.\n"
        "Your task is to enrich the note specified by the target.\n"
        "Fundamental rules:\n"
        + "\n".join(rules)
        + "\n\n" + load_prompt("_anti_slop.txt")
    )

    title = os.path.splitext(os.path.basename(target_path))[0]
    note_payload = f"Title: {title}\nPath: {target_path}\nCurrent content:\n{original}"
    ctx = build_context(checkpoint_id="enrich", payload=note_payload)
    user_message = f"Enrich the following note.\n\n{ctx}"

    provider = get_provider(config, role="worker")
    response = provider.call_llm(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        tools=None,
        response_schema=NoteContent,
        max_tokens=int(os.getenv("MAX_TOKENS", "32768")),
    )
    return NoteContent(content=parse_content(response.text or ""))
