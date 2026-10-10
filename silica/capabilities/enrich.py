# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Enrich capability: what a lean note is missing, landed as an appended section.

A patch and never an overwrite. The whole-body rewrite this used to be is the
edit shape that loses content, dates and numbers first (arXiv 2608.11775:
3.05% of temporal expressions survive a generic rewrite), and the only guard
it had checked links and length. An addition cannot drop anything, so it needs
no anti-loss guard at all; the same shape dedup uses for a merge (S2 of
docs/audits/2026-09-17-paper-backed-simplifications.md, 2026-09-19).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

from silica.agent.bounds import enrich_bounds
from silica.agent.commit import commit_ops
from silica.capabilities._base import emit_feedback, load_prompt, parse_content, read_or_skip
from silica.kernel.workqueue import WorkItem
from silica.kernel.write.ops import Op, OpType

logger = logging.getLogger(__name__)

# The provenance a patch needs: the addition has no source document, so the
# footnote and the `sources:` entry name the capability, as a merge without an
# inbox file names "dedup".
SOURCE_LABEL = "enrich"


def run_enrich(item: WorkItem, config: Any) -> dict[str, Any]:
    target_path = item.target_path
    # The hub reaches the bounds as a FORBIDDEN path matched by basename, so
    # defaulting it to the target's own title made every note forbid itself.
    # No declared hub means None.
    hub = item.context.get("hub") or None

    emit_feedback(item, "reading")
    original, skip = read_or_skip(target_path)
    if skip is not None:
        return skip
    if item.cancel_token.is_set():
        return {"status": "cancelled"}

    emit_feedback(item, "calling_llm")
    addition = _enrich_note(config, target_path, original, hub)
    if not addition.strip():
        return {"status": "no_change", "reason": "enricher produced no content"}
    if item.cancel_token.is_set():
        return {"status": "cancelled"}

    emit_feedback(item, "committing")
    op = Op(
        op=OpType.patch,
        heading=os.path.splitext(os.path.basename(target_path))[0],
        source_basename=SOURCE_LABEL,
        path=target_path,
        snippet=addition,
        hub=hub,
        reason="semantic enrichment",
    )
    return commit_ops(
        [op],
        target_dir=os.path.dirname(target_path),
        hub=hub,
        bounds=enrich_bounds(target_path, hub=hub),
    )


def _enrich_note(config: Any, target_path: str, original: str, hub: str | None) -> str:
    """The LLM seam: the markdown to append, or "" when it has nothing to add."""
    from silica.agent.providers import get_provider
    from silica.kernel.context_builder import build_context

    rules = [
        "1. Write ONLY what the note is missing: the concepts, definitions, steps, formulas or examples a rigorous treatment needs and the note does not have.",
        "2. Never restate, rewrite or summarise what the note already says. Your text is appended after it.",
        "3. Obsidian Flavored Markdown: callouts (> [!tip], > [!note]), LaTeX blocks ($$ ... $$) where appropriate, lists, bold key terms. No top-level heading; start at ## or below.",
        "4. Write in the language of the note.",
        "5. Return STRICT JSON with a single key 'addition' holding the markdown to append. If the note already covers its topic completely, return {\"addition\": \"\"}.",
    ]
    system_prompt = (
        "You are an academic assistant expert in writing and structuring notes in Obsidian Flavored Markdown.\n"
        "Your task is to add what the target note lacks.\n"
        "Fundamental rules:\n"
        + "\n".join(rules)
        + "\n\n" + load_prompt("_anti_slop.txt")
    )

    title = os.path.splitext(os.path.basename(target_path))[0]
    note_payload = f"Title: {title}\nPath: {target_path}\nCurrent content:\n{original}"
    ctx = build_context(checkpoint_id="enrich", payload=note_payload)
    user_message = f"Add what the following note is missing.\n\n{ctx}"

    provider = get_provider(config, role="worker")
    response = provider.call_llm(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        tools=None,
        response_schema=NoteAddition,
        max_tokens=int(os.getenv("MAX_TOKENS", "32768")),
    )
    return parse_content(response.text or "", key="addition")



class NoteAddition(BaseModel):
    """The structured result of an enrich decision: markdown to append."""

    addition: str = ""
