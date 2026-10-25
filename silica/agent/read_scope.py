# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Whole or part: a typed decision (ADR-0044's `decide`) on the user's
request, in front of a narrowed `silica_read_note` (ADR-0053).

Why not the chat model's own choice: on qwen3.5-9b it got it wrong both
ways. With a bare `query=` it narrowed 12 of 16 summary reads
(docs/evaluation/read-query-2026-09-30.md, v2); with an explicit `scope` and
written rules it chose "part" on 9 of 137 reads, the targeted questions
included, so the saving went with the errors (v3). The request is the
evidence for the choice, and a decision model reads it the same way
whatever size the chat model is.

Why only when the model wrote a `query`: the query is the words a part
would be found by. Without them there is nothing to narrow on.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

from silica.agent.decide import decide

logger = logging.getLogger(__name__)

# On wherever a decision model is set (SILICA_DECISION_MODEL); without one
# no read is narrowed. Adopted 2026-10-01 on the probe's `jev_scope` arm, 35
# runs against 35 whole-note ones: right on 28 of 29 decisions, 0.96x prompt
# tokens, truthfulness and accuracy level (docs/evaluation/read-query-2026-09-30.md).
ENABLED = True
# The user's requests of the running turn, oldest first, set by run_agent at
# entry. Tools run in the loop's thread, so they see it; a tool called outside
# run_agent (a direct TOOLS call, a test) sees () and no decision is asked.
REQUESTS: ContextVar[tuple[str, ...]] = ContextVar("read_scope_requests", default=())
# A wrong "part" drops content the user asked about, a wrong "whole" costs
# only tokens, so "part" needs a margin and an unsure verdict reads whole.
# 0.6 is the floor the sniff site already uses (forms.SNIFF_ACCEPT); reopen it
# with a labelled set of 100 judged reads.
PART_ACCEPT = 0.6
_USER_LINE = 4  # the current request and the three before it, as relevance.py

QUESTION = {
    "scope": {
        "type": "choice",
        "instructions": (
            "The assistant is about to read a long note to act on the user's current "
            "request. Does the request need the whole note, or only the passages about "
            "the query words?"),
        "criteria": {
            "whole": ("the request is about the note as a whole: summarise, outline or "
                      "explain it, compare it, quiz on it, edit or append to it, check it, "
                      "or the request names only the note"),
            "part": ("the request asks about one topic inside the note, and the query "
                     "words name that topic"),
        },
    },
}


def wants_part(name: str, query: str) -> bool | None:
    """True/False when a decision was made, None when none was asked or it
    came back empty. Only True narrows."""
    from silica.config import CONFIG

    requests = REQUESTS.get()
    if not ENABLED or not CONFIG.decision_model or not requests or not query.strip():
        return None
    try:
        answers = decide({"current_request": requests[-1],
                          "earlier_requests": list(requests[:-1]),
                          "note": name, "query": query}, QUESTION)
    except Exception as exc:
        # decide() never raises by contract; a read that failed here would cost
        # the turn its note, while reading it whole costs at most tokens.
        logger.warning("read scope decision failed (%s): note read whole", exc)
        return None
    if not answers:
        return None
    verdict = answers["scope"]
    return verdict["choice"] == "part" and verdict["probabilities"].get("part", 0.0) > PART_ACCEPT


def set_requests(messages: list[dict]) -> None:
    REQUESTS.set(tuple(str(m.get("content") or "") for m in messages
                       if m.get("role") == "user")[-_USER_LINE:])
