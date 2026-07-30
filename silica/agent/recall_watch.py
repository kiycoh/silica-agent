# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Did every recall call in this turn come back empty? Read off the trace.

The verdict is mechanical on purpose. Asking the model to self-assess whether
what it recalled was sufficient (the L2 sufficiency router) was kill-tested and
killed on 2026-07-24: the prompt changed its behavior without improving the
answers. So the hint fires from tool results, never from the model's judgment.
"""
from __future__ import annotations

import json

from silica.agent.events import ToolCompleteEvent

THIN_COVERAGE_HINT = "vault coverage was thin: /web to search the web"

# Per-tool emptiness. `silica_related` is excluded on purpose: it takes an
# existing note, so an empty result means "no neighbors", not "no coverage".
# `silica_search` (title match only) is too weak a signal to escalate on.
#
# ponytail: hard miss only — every recall call returned zero. "Few or low-scored
# hits" needs a scored threshold nobody has benched; revisit with an eval.
_MISS = {
    "silica_recall": lambda d: not d.get("notes"),
    "silica_semantic_search": lambda d: not d.get("results"),
    "silica_search_context": lambda d: not d.get("notes_matched"),
}


class RecallWatch:
    """Wraps a turn's tool_progress_callback; `.thin` is the verdict afterwards.

    Forwards every event untouched — the renderer downstream never knows it is
    being watched.
    """

    def __init__(self, inner=None) -> None:
        self._inner = inner
        self.calls = 0
        self.misses = 0

    def __call__(self, event) -> None:
        if isinstance(event, ToolCompleteEvent):
            self._inspect(event)
        if self._inner is not None:
            self._inner(event)

    def _inspect(self, event: ToolCompleteEvent) -> None:
        predicate = _MISS.get(event.name)
        if predicate is None:
            return
        try:
            data = json.loads(event.result)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict) or "error" in data:
            # A degraded index or an offline embedder is not thin coverage:
            # sending the user to the web here would mask the real problem.
            return
        self.calls += 1
        if predicate(data):
            self.misses += 1

    @property
    def thin(self) -> bool:
        """True when the turn searched the vault and every search missed."""
        return self.calls > 0 and self.calls == self.misses
