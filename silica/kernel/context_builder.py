# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Context assembler for single-checkpoint LLM calls.

The whole contract: an LLM step sees the run digest, its own payload, and
optionally pre-fetched candidates — nothing else. Pure, so tests need no
external dependencies.
"""
from __future__ import annotations

import json


def build_context(
    checkpoint_id: str,
    payload: dict | str | None = None,
    ledger_digest: str | None = None,
    substrate: str | None = None,
) -> str:
    """One checkpoint's user-message context, "" when every input is empty."""
    parts: list[str] = []
    if ledger_digest and ledger_digest.strip():
        parts.append("## Run Context\n" + ledger_digest.strip())
    if substrate and substrate.strip():
        parts.append("## Related Notes (candidates)\n" + substrate.strip())
    if payload is not None:
        body = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, indent=2
        )
        parts.append(f"## Checkpoint: {checkpoint_id}\n" + body)
    return "\n\n".join(parts)
