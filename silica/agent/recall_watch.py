# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Did every recall call in this turn come back empty, or run without a leg?
Read off the trace.

The verdict is mechanical on purpose. Asking the model to self-assess whether
what it recalled was sufficient (the L2 sufficiency router) was kill-tested and
killed on 2026-07-24: the prompt changed its behavior without improving the
answers. So the hint fires from tool results, never from the model's judgment.
The same holds for the degraded line (ADR-0094): it names the missing leg and
where to fix it, and tells nobody what to conclude.
"""
from __future__ import annotations

import json

from silica.agent.events import ToolCompleteEvent

THIN_COVERAGE_HINT = "vault coverage was thin: /web to search the web"

# The degraded line, one per leg: `<prefix>: the <role> <phrase><fix>` (ADR-0094
# point 5). The prefix says what the answer lost, not which server failed: the
# reader judges the answer, and "lexical only" is what changes how to read it.
_LEG = {"embed": ("lexical only", "embedder", "SILICA_EMBEDDING_TIMEOUT", "embedding_timeout"),
        "rerank": ("not reranked", "reranker", "SILICA_RERANK_TIMEOUT", "rerank_timeout")}
_PHRASE = {"unreachable": "is unreachable", "loading": "is still loading",
           "rejected": "refused the model name", "failed": "failed"}
# Where to fix it, per surface. The GUI names its own panels; the REPL has none,
# so both Settings pointers become the doctor; a tool result can be read by
# either, or by an MCP client, so it names both. Loading has no fix: it ends.
_FIX = {
    "gui": {"unreachable": "start it in Settings, Endpoints",
            "diagnose": "Settings, Diagnostics names the cause"},
    "repl": {"unreachable": "run silica doctor", "diagnose": "run silica doctor"},
    "tool": {"unreachable": "start it in Settings, Endpoints, or run silica doctor",
             "diagnose": "Settings, Diagnostics names the cause, or run silica doctor"},
}


def degraded_lines(degraded: dict, surface: str = "gui") -> list[str]:
    """One line per leg this call or turn ran without, the embedder first."""
    from silica.config import CONFIG

    lines = []
    for leg in ("embed", "rerank"):
        cause = degraded.get(leg)
        if cause is None:
            continue
        prefix, role, knob, timeout_attr = _LEG[leg]
        if cause == "slow":
            phrase, fix = f"did not answer in {getattr(CONFIG, timeout_attr):g} s", f"raise {knob}"
        else:
            phrase = _PHRASE.get(cause, "failed")
            fix = {"unreachable": _FIX[surface]["unreachable"],
                   "loading": ""}.get(cause, _FIX[surface]["diagnose"])
        lines.append(f"{prefix}: the {role} {phrase}" + (f" · {fix}" if fix else ""))
    return lines

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

    def __init__(self, inner=None, surface: str = "gui") -> None:
        self._inner = inner
        # Which surface ran the turn: the stored note keeps the fix it named.
        self.surface = surface
        self.calls = 0
        self.misses = 0
        # Leg to cause over every retrieval call this turn, first cause per leg.
        self.degraded: dict[str, str] = {}
        # Did this turn go to the web through silica_web_answer? The UI reapplies
        # the citation block when it did (web_research.relay_sources).
        self.web_answer = False

    def __call__(self, event) -> None:
        if isinstance(event, ToolCompleteEvent):
            if event.name == "silica_web_answer":
                self.web_answer = True
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
        if not isinstance(data, dict):
            return
        # Before the error check: a call with no leg left answers with an error,
        # and the error alone points at rebuilding an index that is fine.
        degraded = data.get("degraded")
        for leg, cause in (degraded.items() if isinstance(degraded, dict) else ()):
            self.degraded.setdefault(leg, cause)
        if "error" in data:
            # A degraded index or an offline embedder is not thin coverage:
            # sending the user to the web here would mask the real problem.
            return
        self.calls += 1
        if predicate(data):
            self.misses += 1

    @property
    def thin(self) -> bool:
        """True when the turn searched the vault and every search missed.

        Never when a search ran without the embedder: that turn did not search
        by meaning, so its misses, silica_search_context's included, are not a
        finding about coverage and the web is the wrong fix. A turn that lost
        only the reranker keeps the rule: a reorder cannot change membership.
        """
        if "embed" in self.degraded:
            return False
        return self.calls > 0 and self.calls == self.misses

    def notes(self) -> list[dict]:
        """This turn's `retrieval` notes for the end-of-turn channel (turn_notes):
        the degraded legs first, the cause before the miss it may explain."""
        lines = degraded_lines(self.degraded, self.surface)
        if self.thin:
            lines.append(THIN_COVERAGE_HINT)
        return [{"kind": "retrieval", "text": line} for line in lines]
