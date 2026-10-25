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

from silica.kernel.text import dates
from silica.kernel.text.text import squash

# The criteria Jev chooses among, each worded on the claim's own point. The
# first wording ("a passage states ... a different fact") read any other fact
# in a passage as a contradiction: on 2026-10-24 Jev called "received by email
# on 3 February 2026" contradicted by a passage that says exactly that and
# "not yet reviewed" (0.97); worded on the point, 8 of 8 dev cases came right.
CRITERIA = {
    "supported": "the passages say what the claim says about its point, in the same or other words or language",
    "contradicted": ("the passages give a different value for the claim's own point: another date, number, name, "
                     "party or outcome, or the opposite"),
    "overstated": ("the passages say something narrower about the claim's point: the claim drops a condition, a "
                   "qualifier or an exception they state, or generalises it to always, every or any"),
    "off-point": "the passages are about a different subject from the claim",
    "not in these passages": "the passages are about the claim's subject but do not address its point",
    "not a statement of fact": "the claim is a question, a plan, an opinion or an instruction",
}
QUESTION = {"verdict": {
    "type": "choice",
    "instructions": ("Judge only the CLAIM's own point against the PASSAGES. Other facts in the passages that the "
                     "claim does not mention are not a contradiction. Use only the passages, never outside "
                     "knowledge; the claim and the passages may be in different languages."),
    "criteria": CRITERIA,
}}
# Per kind, the probability Jev must give its choice for the verdict to stand;
# below it the block is not checked. Set at step 2's gate on the step 1
# drafts and frozen (ADR-0110 point 14).
THRESHOLDS: dict[str, float] = {k: 0.5 for k in CRITERIA}
# "Supported" vouches for the block, so it needs more: the second test set's
# two false "supported" came at 0.60 and 0.67, its eight true ones at 0.77 or
# more (2026-10-25). The first set had one true at 0.62, now not checked: a
# block left open costs less than one wrongly vouched for.
THRESHOLDS["supported"] = 0.75
# run.NOTE_CHARS: a short note reaches the judge whole.
PASSAGE_CHARS = 1800

Decide = Callable[[dict, dict], "dict | None"]
Chat = Callable[[str], str]


def _iso(text: str) -> str:
    """Absolute dates as ISO days. Jev read "3 febbraio 2026" against "3 February
    2026" as two dates (contradicted, 0.92, 2026-10-24); the rule reader already
    knows they are one."""
    for d, a, b in reversed(dates.find_dates(text)):
        text = text[:a] + d + text[b:]
    return text


def _state(text: str, passages: list[dict]) -> dict:
    return {"claim": _iso(text), "passages": [
        {"source": f"{p['path']}:{p['line']}", "text": _iso(p["text"][:PASSAGE_CHARS])} for p in passages]}


def which_passage(text: str, passages: list[dict], decide: Decide) -> dict | None:
    """The passage a verdict rests on: one more choice over the passages, so
    the box can open its line. None when Jev cannot say."""
    if len(passages) == 1:
        return passages[0]
    criteria = {str(i): p["text"][:300] for i, p in enumerate(passages, 1)}
    answers = decide(_state(text, passages), {"rests_on": {
        "type": "choice", "instructions": "Which passage bears most directly on the claim's point?", "criteria": criteria}})
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


_PROMPT = """A person wrote TEXT and relies on PASSAGES from their own sources. Judge only TEXT's own point against the PASSAGES; other facts in a passage that TEXT does not mention are not a contradiction. Never use outside knowledge. TEXT and PASSAGES may be in different languages.
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
