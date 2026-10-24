# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The verdict on a block's meaning (ADR-0110 point 7, as decided 2026-10-24):
Jev decides, one typed choice per block, and the chat model answers only when
Jev answers nothing.

The two legs come in as arguments, `decide(state, questions)` with
agent/decide.py's contract and `chat(prompt) -> str`, so the kernel stays clear
of the agent and a test hands in a dict.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

from silica.kernel.text.text import squash

# The criteria Jev chooses among: the verdict names of point 7, each worded as
# what the block does to its passages.
CRITERIA = {
    "supported": "the passages state what the text says, in the same or other words",
    "contradicted": "a passage states the opposite of the text, or a different fact, date, number or outcome",
    "overstated": ("a passage supports only a narrower claim: the text drops a condition, a qualifier or an "
                   "exception the passage states, or turns 'some', 'usually' or 'where' into 'always' or 'any'"),
    "off-point": "the passages are on another subject, or a related one that neither supports nor contradicts the text",
    "not in these passages": "the passages are on the same subject but say nothing about this point",
    "not a statement of fact": "the text states no fact: a question, a plan, an opinion or an instruction",
}
QUESTION = {"verdict": {
    "type": "choice",
    "instructions": ("A person wrote the text below and cites or relies on the passages, which come from their "
                     "own sources. Judge the text against the passages only, never against outside knowledge. "
                     "The text and the passages may be in different languages."),
    "criteria": CRITERIA,
}}
# Per kind, the probability Jev must give its choice for the verdict to stand;
# below it the block is not checked. Set at step 2's gate on the step 1
# drafts and frozen (ADR-0110 point 14).
THRESHOLDS: dict[str, float] = {k: 0.5 for k in CRITERIA}
PASSAGE_CHARS = 900

Decide = Callable[[dict, dict], "dict | None"]
Chat = Callable[[str], str]


def _state(text: str, passages: list[dict]) -> dict:
    return {"text": text, "passages": [{"source": f"{p['path']}:{p['line']}", "text": p["text"][:PASSAGE_CHARS]}
                                       for p in passages]}


def which_passage(text: str, passages: list[dict], decide: Decide) -> dict | None:
    """The passage a verdict rests on: one more choice over the passages, so
    the box can open its line. None when Jev cannot say."""
    if len(passages) == 1:
        return passages[0]
    criteria = {str(i): p["text"][:300] for i, p in enumerate(passages, 1)}
    answers = decide(_state(text, passages), {"rests_on": {
        "type": "choice", "instructions": "Which passage bears most directly on the text?", "criteria": criteria}})
    if not answers:
        return None
    return passages[int(answers["rests_on"]["choice"]) - 1]


def by_jev(text: str, passages: list[dict], decide: Decide) -> dict | None:
    """{kind, p, by, at?} or {state: "not checked", ...}; None when Jev
    answered nothing (unset, error, invalid), which is the fallback's turn."""
    answers = decide(_state(text, passages), QUESTION)
    if not answers:
        return None
    a = answers["verdict"]
    kind, p = a["choice"], float((a.get("probabilities") or {}).get(a["choice"], 0.0))
    if p < THRESHOLDS.get(kind, 1.0):
        return {"state": "not checked", "kind": kind, "p": p, "by": "jev"}
    out = {"kind": kind, "p": p, "by": "jev"}
    if kind in ("contradicted", "overstated", "off-point") and passages:
        rest = which_passage(text, passages, decide)
        if rest:
            out["at"] = f"{rest['path']}:{rest['line']}"
    return out


_PROMPT = """A person wrote TEXT and relies on PASSAGES from their own sources. Judge TEXT against the PASSAGES only, never against outside knowledge. TEXT and PASSAGES may be in different languages.
Choose one verdict:
{criteria}
Reply with JSON only: {{"verdict": "<one of the verdicts>", "evidence": "<words copied exactly from one passage, or empty>", "source": "<that passage's source>"}}

TEXT:
{text}

PASSAGES:
{passages}
"""


def by_chat(text: str, passages: list[dict], chat: Chat) -> dict:
    """The fallback: one verdict per block from the chat model, kept only when
    its evidence is copied from a passage. A paraphrased quote is the model
    arguing, so the block is not checked."""
    prompt = _PROMPT.format(
        criteria="\n".join(f"- {k}: {v}" for k, v in CRITERIA.items()), text=text,
        passages="\n\n".join(f"[{p['path']}:{p['line']}]\n{p['text'][:PASSAGE_CHARS]}" for p in passages))
    try:
        raw = chat(prompt)
        m = re.search(r"\{.*\}", raw or "", re.S)
        reply = json.loads(m.group(0)) if m else {}
    except Exception:
        reply = {}  # an unreadable reply is one more unanswered block, never a verdict
    kind = str(reply.get("verdict") or "").strip().lower()
    if kind not in CRITERIA:
        return {"state": "not checked", "by": "chat", "why": "no verdict in the reply"}
    evidence = squash(str(reply.get("evidence") or "")).casefold()
    if kind not in ("supported", "not a statement of fact", "not in these passages", "off-point") or evidence:
        hit = next((p for p in passages if _quoted(evidence, p["text"])), None)
        if hit is None:
            return {"state": "not checked", "by": "chat", "why": "its evidence is not in any passage"}
        return {"kind": kind, "by": "chat", "at": f"{hit['path']}:{hit['line']}"}
    return {"kind": kind, "by": "chat"}


def _quoted(evidence: str, passage: str) -> bool:
    """Point 7's quote rule: the words are in the passage, "..." allowed
    between segments of six words or more, in order."""
    if not evidence:
        return False
    hay = squash(passage).casefold()
    parts = [s.strip() for s in re.split(r"\.\.\.|…", evidence) if s.strip()]
    if len(parts) > 1 and any(len(s.split()) < 6 for s in parts):
        return False
    at = 0
    for s in parts:
        at = hay.find(s, at)
        if at < 0:
            return False
        at += len(s)
    return True


def judge(text: str, passages: list[dict], *, decide: Decide | None, chat: Chat | None) -> dict | None:
    """Jev first; the chat model only when Jev answered nothing."""
    if decide is not None:
        verdict = by_jev(text, passages, decide)
        if verdict is not None:
            return verdict
    return by_chat(text, passages, chat) if chat is not None else None
