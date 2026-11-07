# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Where a late system line goes in a conversation, and how it is found again.

A seed closes with the language rule, which must stay last: it is restated there
because a vault's notes in another language drown it anywhere further up. Every
line that joins a conversation after its seed was built (a persona, a workspace,
user memory, the phone's) goes before that rule, and is known afterwards by its
`origin`, the key the wire boundary strips.
"""
from __future__ import annotations


def index_of(messages: list[dict], origin: str) -> int | None:
    return next((n for n, m in enumerate(messages) if m.get("origin") == origin), None)


def insert(messages: list[dict], line: dict) -> None:
    """`line` before the seed's last system message."""
    head = next((n for n, m in enumerate(messages) if m.get("role") != "system"), len(messages))
    messages.insert(max(head - 1, 0), line)
