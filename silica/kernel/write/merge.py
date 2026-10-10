# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""3-way merge with conflict callout (Tier 2, Item 7, ADR-0007 soft-failure sink).

When a write lands on a note that was modified concurrently (base != current),
we inject an Obsidian danger callout rather than silently overwriting.  The
incoming content is still written; the callout gives the vault owner a clear
signal to review the merge manually.

Terminology:
    base     — content at snapshot time (what we expected)
    current  — content on disk when the write lands
    incoming — what the op wants to write
"""
from __future__ import annotations

CONFLICT_CALLOUT_HEADER = "> [!danger] Semantic Conflict"

# Emitted until 2026-08. Recognized forever: this header line is the idempotency
# key for inject_conflict_callout, so a note that already carries a callout in
# the old spelling must keep matching or the next conflict stacks a second
# callout on top of the first. Never emitted — read side only.
LEGACY_CONFLICT_CALLOUT_HEADER = "> [!danger] Conflitto Semantico"

CONFLICT_CALLOUT_HEADERS = (CONFLICT_CALLOUT_HEADER, LEGACY_CONFLICT_CALLOUT_HEADER)

_CALLOUT_BODY = f"""\
{CONFLICT_CALLOUT_HEADER}
> This note was modified concurrently. Review and merge the sections below manually.

"""


def detect_conflict(base: str | None, current: str | None) -> bool:
    """Return True iff base and current both exist and differ."""
    if base is None or current is None:
        return False
    return base != current


def inject_conflict_callout(content: str) -> str:
    """Prepend the conflict callout to content (idempotent)."""
    if any(header in content for header in CONFLICT_CALLOUT_HEADERS):
        return content
    return _CALLOUT_BODY + content


def three_way_merge(
    base: str | None,
    current: str | None,
    incoming: str,
) -> tuple[str, bool]:
    """Merge incoming content, injecting a conflict callout if base != current.

    Returns:
        (merged_content, had_conflict)
    """
    if detect_conflict(base, current):
        return inject_conflict_callout(incoming), True
    return incoming, False
