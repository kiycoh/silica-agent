# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One typed decision call: TypeSafe Jev through OpenRouter's Decisions endpoint (ADR-0044).

A function beside `call_llm`, not a provider, because Jev is not a chat
model: it answers `{state, questions}` with a typed choice / score / noul plus
probabilities, and OpenRouter serves it only from `/api/alpha/decisions`,
never from chat/completions, so litellm cannot route it. The contract with
the caller is one-sided: every non-answer here is None and the caller runs
the LLM path it ran before. A valid answer with a low probability is still
an answer (a noul near 0 is a confident no); thresholds belong to the caller
because a wrong yes and a wrong no cost differently per site.

No retry on purpose. This is an accelerator in front of a fallback, and a
retry chain makes the slow path slower than the path it accelerates. Four
outcomes reach the log, so a run can be read afterwards: disabled, error
(transport or HTTP), invalid (a reply the schema does not cover), done.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ponytail: one host. OpenRouter documents the alpha path as movable; the same
# body is accepted by api.typesafe.ai/v1/systemone, which is the upgrade path
# if it moves before 2026-12-31.
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
# The budget is the extra delay a caller accepts before its own path runs,
# not a ceiling on Jev: measured 2026-09-19 over 63 sniff calls, p50 0.41 s,
# p95 0.80 s, against an LLM sniff at p50 0.96 s. 3 s bounds the worst
# case (timeout, then the LLM) at about four LLM calls.
TIMEOUT_S = 3.0


def decide(state: Any, questions: dict[str, dict], *,
           timeout_s: float = TIMEOUT_S) -> dict[str, dict] | None:
    """Answers keyed like `questions`, or None: disabled, transport/HTTP
    error, or a reply that fails validation. Never raises."""
    from silica.config import CONFIG

    model = CONFIG.decision_model
    if not model:
        logger.debug("decision disabled (SILICA_DECISION_MODEL unset)")
        return None
    t0 = time.time()
    try:
        resp = httpx.post(
            DECISIONS_URL,
            json={"model": model, "state": state, "questions": questions},
            headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY', '')}"},
            timeout=timeout_s,
        )
    except Exception as e:  # httpx transport errors, timeouts
        _log(model, "error", t0, f"{type(e).__name__}: {str(e)[:120]}")
        return None
    if resp.status_code != 200:
        _log(model, "error", t0, f"HTTP {resp.status_code}: {resp.text[:120]}")
        return None
    try:
        body = resp.json()
    except Exception as e:
        _log(model, "invalid", t0, f"not JSON: {e}")
        return None
    answers = body.get("answers") if isinstance(body, dict) else None
    try:
        problem = _validate(answers, questions)
    except Exception as e:  # a shape the checks did not expect (a list for a str)
        problem = f"invalid: malformed reply ({type(e).__name__})"
    if problem or not isinstance(answers, dict):  # "" from _validate means a dict
        _log(model, "invalid", t0, problem)
        return None
    _log(model, "done", t0, _brief(answers), served=body.get("model"),
         usage=body.get("usage"), answers=answers)
    return answers


def _brief(answers: dict[str, dict]) -> str:
    """The typed values only, for the log line: what was decided is what a
    reader reviews later, and it is a few tokens, unlike the state."""
    parts = []
    for qid, a in answers.items():
        if a.get("type") == "choice":
            p = (a.get("probabilities") or {}).get(a.get("choice"))
            parts.append(f"{qid}={a.get('choice')} p={p}")
        elif a.get("type") == "noul":
            parts.append(f"{qid}={a.get('noul')}")
        else:
            parts.append(f"{qid}={a.get('score')}")
    return " ".join(parts)


def _log(model: str, outcome: str, t0: float, detail: str, *,
         served: str | None = None, usage: dict | None = None,
         answers: dict | None = None) -> None:
    """One compact line per call (no state dump: that is what the LLM
    trace flag is for) and one beat on the same `call` stream as the LLM
    calls, so a session trace shows the decision next to what it replaced."""
    duration = round(time.time() - t0, 3)
    logger.info("decision %s %s %.3fs %s", model, outcome, duration, detail)
    from silica.agent.narration import NARRATOR

    NARRATOR.narrate(
        "call", "done" if outcome == "done" else "failed",
        f"decide {model} {outcome}",
        {"model": model, "served": served, "outcome": outcome,
         "duration_s": duration, "detail": detail, "usage": usage,
         "answers": answers},
    )
    return None


def _unit(v: Any) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v) and 0.0 <= v <= 1.0


def _validate(answers: Any, questions: dict[str, dict]) -> str:
    """"" when every question has a well-typed answer, else what is wrong."""
    if not isinstance(answers, dict):
        return "invalid: no answers"
    for qid, q in questions.items():
        a = answers.get(qid)
        kind = q.get("type")
        if not isinstance(a, dict) or a.get("type") != kind:
            return f"invalid: {qid} missing or not a {kind}"
        if kind == "choice":
            criteria = q.get("criteria", {})
            if not isinstance(a.get("choice"), str) or a["choice"] not in criteria:
                return f"invalid: {qid} choice {a.get('choice')!r} outside criteria"
            probs = a.get("probabilities")
            if (not isinstance(probs, dict) or set(probs) - set(criteria)
                    or not all(_unit(p) for p in probs.values())):
                return f"invalid: {qid} probabilities"
        elif kind == "noul":
            if not _unit(a.get("noul")):
                return f"invalid: {qid} noul"
        elif kind == "score":
            s = a.get("score")
            if not (isinstance(s, (int, float)) and math.isfinite(s)):
                return f"invalid: {qid} score"
        if "confidence" in a and not _unit(a["confidence"]):
            return f"invalid: {qid} confidence"
    return ""
