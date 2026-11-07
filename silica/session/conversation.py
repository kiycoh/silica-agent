# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What one chat remembers, on whichever surface holds it."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Conversation:
    """What one chat remembers. A field added here starts clean with each new
    chat by being here: `Session.reset` builds a new one and names none of them."""

    messages: list[dict] = field(default_factory=list)
    collapsed: set[int] = field(default_factory=set)  # message indices elided by compaction, across turns
    # Number tokens of every result this session read, raw (ADR-0095): compaction
    # and a relevance verdict stub the tool messages, and must not decide what grounds.
    seen_numbers: set[str] = field(default_factory=set)
    # The narration sid. The store moved to ~/.silica/narration (docs/specs/narration/spec.md §5);
    # legacy web_sessions/*.json are read forever through silica.obs.narration.
    sid: str | None = None
    workspace: str | None = None  # vault path of the workspace this chat is opened in (ADR-0112)
