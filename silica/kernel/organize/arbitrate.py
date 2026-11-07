# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L2 of the classifier: one model call over the notes the rules left ambiguous.

Apart from `classify` because the two change for different reasons: its rules
with the taxonomy and the vault, this with the model and its prompt.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L2 arbiter — constrained-decoding wrapper models
#
# json_schema (response_format) requires an object root, so the arbiter's
# per-note assignments are wrapped in an object instead of returned as a
# bare JSON array.
# ---------------------------------------------------------------------------

class FolderAssignment(BaseModel):
    index: int
    folder: str


class ArbitrationResult(BaseModel):
    assignments: list[FolderAssignment]


def llm_arbitrate(
    ambiguous: list[tuple[str, str, list[Any]]],  # (note_path, snippet, candidate_rules)
    taxonomy: Any,  # Taxonomy
) -> dict[str, str]:
    """Batch LLM call for ambiguous notes. Returns {note_path: folder}."""
    if not ambiguous:
        return {}

    from silica.model.llm import call_llm
    from silica.model.reply import parse_reply
    from silica.config import CONFIG

    folder_list = "\n".join(
        f"  - folder: \"{r.folder}\" — {r.description or ', '.join(r.themes[:3])}"
        for r in taxonomy.rules
    )
    uncategorized_line = f"  - folder: \"{taxonomy.uncategorized}\" — no clear match"

    note_entries = []
    for idx, (note_path, snippet, _rules) in enumerate(ambiguous):
        title = os.path.splitext(os.path.basename(note_path))[0]
        note_entries.append(
            f"{idx}. title: \"{title}\"\n   snippet: \"{snippet[:200].strip()}\""
        )
    notes_block = "\n".join(note_entries)

    system_prompt = (
        "You are a vault organizer. Assign each note to exactly one folder from the list.\n"
        "Return a JSON object: {\"assignments\": [{\"index\": 0, \"folder\": \"Concepts/AI\"}, ...]}\n"
        "Use only the folder values listed. No explanation."
    )
    user_msg = (
        f"Available folders:\n{folder_list}\n{uncategorized_line}\n\n"
        f"Notes to classify:\n{notes_block}"
    )

    try:
        response = call_llm(
            model=CONFIG.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            tools=None,
            response_format=ArbitrationResult,
        )
    except Exception as exc:
        logger.warning("LLM arbiter failed (%s) — falling back to uncategorized", exc)
        return {note_path: taxonomy.uncategorized for note_path, _s, _r in ambiguous}

    parsed = parse_reply(response.text)
    assignments = parsed.get("assignments") if isinstance(parsed, dict) else None
    if not isinstance(assignments, list):
        logger.warning("LLM arbiter response missing 'assignments' list: %r — falling back", parsed)
        return {note_path: taxonomy.uncategorized for note_path, _s, _r in ambiguous}

    valid_folders = {r.folder for r in taxonomy.rules} | {taxonomy.uncategorized}
    result: dict[str, str] = {}
    for entry in assignments:
        if not isinstance(entry, dict):
            continue
        raw_idx: object = entry.get("index")
        folder = entry.get("folder", "")
        # Reject non-int and out-of-range (incl. negative): a schema-valid -1 would
        # relabel the last note, and a string index raises TypeError on `idx >= len`.
        if not isinstance(raw_idx, int) or not (0 <= raw_idx < len(ambiguous)):
            continue
        note_path = ambiguous[raw_idx][0]
        result[note_path] = folder if folder in valid_folders else taxonomy.uncategorized

    for note_path, _s, _r in ambiguous:
        result.setdefault(note_path, taxonomy.uncategorized)

    return result
