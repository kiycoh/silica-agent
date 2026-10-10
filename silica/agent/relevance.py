# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Turn-start compaction by relevance: a typed decision per earlier tool result
(ADR-0044's `decide`) says whether the user's current request, read against
the requests before it, still needs that result. A confident no becomes the
same recoverable stub the budget sweep writes; every non-answer keeps the body.

Why at the start of a turn and never between iterations: rewriting a message
re-bills every token after it on a prefix cache (Anthropic breakpoints,
llama.cpp slot reuse), so the history changes once per user turn, where the
new request is the evidence that an earlier read went out of scope. Inside a
turn the model is still acting on what it just read.

Why a stub and not a summary: the stub keeps the small fields (paths, counts,
query) and names the call to re-issue, so a wrong no costs one re-read, while
a summary written by a model can drop the one line the next turn asks about
with nothing in the history to say so.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from silica.agent.compaction import read_stub, relevance_candidates
from silica.agent.decide import decide

logger = logging.getLogger(__name__)

# On wherever a decision model is set (SILICA_DECISION_MODEL, ADR-0044); the
# flag is the probe's `no_relevance` arm. Probe of 2026-09-30 on qwen3.5-9b,
# 5 runs per arm (docs/evaluation/compaction-2026-09-30.md): a conversation
# that changes subject every turn went from 207k to 103k prompt tokens
# (median, Mann-Whitney p=0.008), 20 of 20 cases passing in both arms.
ENABLED = True
# LiteLLM's TypeSafe compaction guardrail ships 0.2, with no measurement on
# its page. On the probe every result a follow-up still needed scored 0.68 or
# more (29 verdicts) and every out-of-scope one 0.37 or less (15), so 0.2
# dropped 13 of the 15 and nothing needed. Reopen it when a labelled set of
# 100 judged results exists: 4 cases are not a calibration.
DROP_BELOW = 0.2
# A result smaller than this costs less to keep than the cache miss its stub
# causes: every message after it is re-sent uncached once.
MIN_CHARS = 2000
_RESULT_HEAD = 1500   # what the decision sees of a result
_USER_LINE = 4        # the current request and the three before it
_ANSWER_HEAD = 600    # an elliptical follow-up ("and the other one?") needs it
_WORKERS = 8
_WHY = "— judged not needed for the current request"

QUESTION = {
    "needed": {
        "type": "noul",
        "instructions": (
            "Does the assistant still need this earlier tool result to answer the "
            "user's current request? Read the current request against the earlier "
            "ones and the previous answer: a follow-up that refers back to what was "
            "read (the same note, 'that', 'the other one', 'go ahead') needs it; a "
            "request about a different subject does not."),
    },
}


def typed_state(requests: list[str], previous_answer: str, name: str,
                arguments: str, content: str) -> dict:
    return {
        "current_request": requests[-1],
        "earlier_requests": requests[:-1],
        "previous_answer": previous_answer[:_ANSWER_HEAD],
        "tool_result": {"tool": name, "arguments": arguments, "head": content[:_RESULT_HEAD]},
    }


def compact_by_relevance(messages: list[dict], tools: dict) -> list[int]:
    """Stub the earlier tool results the current request does not need, in
    place; returns their indices. Never raises into the loop: a failed
    decision keeps the result, which is what the history held before."""
    from silica.config import CONFIG

    if not ENABLED or not CONFIG.decision_model:
        return []  # off: no candidate scan for a verdict nobody asks for
    candidates = relevance_candidates(messages, tools, MIN_CHARS)
    if not candidates:
        return []
    last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    requests = [str(m.get("content") or "") for m in messages
                if m.get("role") == "user"][-_USER_LINE:]
    previous = next((str(m.get("content") or "") for m in reversed(messages[:last_user])
                     if m.get("role") == "assistant" and m.get("content")
                     and not m.get("tool_calls")), "")

    def ask(c: tuple[int, str, str]) -> float | None:
        i, name, arguments = c
        try:
            answers = decide(typed_state(requests, previous, name, arguments,
                                         messages[i]["content"]), QUESTION)
        except Exception as exc:
            # decide() never raises by contract; a worker that did would cost
            # the whole turn, while keeping the body costs only tokens.
            logger.warning("relevance decision failed (%s): result kept", exc)
            return None
        return answers["needed"]["noul"] if answers else None

    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(candidates))) as ex:
        ps = list(ex.map(ask, candidates))
    dropped: list[int] = []
    for (i, name, arguments), p in zip(candidates, ps):
        if p is not None and p < DROP_BELOW:
            messages[i]["content"] = read_stub(name, arguments, messages[i]["content"], _WHY)
            dropped.append(i)
    from silica.agent.narration import NARRATOR

    NARRATOR.narrate("compaction", "done",
                     f"relevance: {len(dropped)} of {len(candidates)} result(s) elided",
                     {"by": "relevance", "indices": dropped,
                      "judged": [{"index": c[0], "tool": c[1],
                                  "p": None if p is None else round(p, 3)}
                                 for c, p in zip(candidates, ps)]},
                     parent=None)
    return dropped
