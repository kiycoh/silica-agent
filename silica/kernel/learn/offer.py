# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""When a chat answer offers the reader a way to work on what it explained.

A pure rule over the quiz ledger and the reader's own words, with no model
call to decide: asking a model whether a topic is hard correlates at about
0.28 with real difficulty, and models used as triggers over-fire (docs
research report difficulty-detection-and-study-aids, 2026-10-23). The
signals that hold up are attempts and what the reader says: two misses in the
last three answers on a note, a note the reader was last told rather than
recalled, or a message that says it did not understand. The offer is one line
under the answer and the reader launches it; help pushed after every error
did worse than help on request (Razzaq and Heffernan 2010).
"""
from __future__ import annotations

import re
from urllib.parse import quote

# The reader saying it did not follow, in the two languages this was written
# against. A word list, not a classifier: self-declared beats inferred.
_LOST_RE = re.compile(
    r"\b(non\s+(?:l['’])?(?:ho\s+)?capi(?:t[oaie]|sco)|non\s+mi\s+è\s+chiar[oa]|spiegami\s+(?:meglio|di\s+nuovo)"
    r"|più\s+semplice|non\s+riesco\s+a\s+capire|i\s+(?:don'?t|do\s+not)\s+(?:get|understand)"
    r"|i'?m\s+lost|explain\s+(?:it\s+)?again|simpler)\b",
    re.IGNORECASE,
)
RECENT = 3    # answers looked at per note
MISSES = 2    # of the RECENT that were wrong for a note to count as a struggle


def says_lost(text: str) -> bool:
    return bool(_LOST_RE.search(text or ""))


def struggling(paths: list[str], entries: list[dict]) -> list[str]:
    """The notes among `paths` whose last answers say the reader struggles:
    MISSES wrong of the last RECENT, or the latest one answered by being told."""
    from silica.kernel.report.quiz import key

    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(key(str(e.get("path") or "")), []).append(e)
    out = []
    for p in paths:
        evs = sorted(by_key.get(key(p), []), key=lambda e: str(e.get("ts")))[-RECENT:]
        if not evs:
            continue
        if sum(1 for e in evs if not e.get("correct")) >= MISSES or evs[-1].get("told"):
            out.append(p)
    return out


def study_offer(text: str, reads: list[str], entries: list[dict] | None = None) -> dict | None:
    """The offer under an answer, or None: silent unless the ledger or the
    reader's words say so. The commands are ones the reader already has
    (/quiz practises a note, /diagram draws its mechanism); the click puts one
    in the box and the reader sends it."""
    notes = [p for p in reads if p.endswith(".md") and "/sources/" not in f"/{p}"]
    if not notes:
        return None
    if entries is None:
        from silica.kernel.report.quiz import entries as ledger

        entries = ledger()
    hard = struggling(notes, entries)
    lost = says_lost(text)
    if not hard and not lost:
        return None
    target = (hard or notes)[0]
    name = target.removesuffix(".md").rsplit("/", 1)[-1]
    why = (f"you missed {name} in recent practice" if hard and not lost
           else f"you said this was not clear")
    return {"kind": "study", "text": why, "note": target,
            "commands": [{"label": "practise it", "command": f'/quiz "{target}" --n=3'},
                         {"label": "see it as a diagram", "command": f'/diagram "{name}"'}]}


# The reader asking the chat for a study plan. The chat writes one into its
# answer, untracked: on 2026-10-23 the local 9B dated it "about 17 weeks" for an
# exam 18 days away, while Learn held the plan with its pace to the date.
_PLAN_RE = re.compile(
    r"\b(piano\s+(?:di|per\s+lo)\s+studio|programma\s+di\s+studio|pianific\w*\s+(?:lo\s+|il\s+mio\s+)?studi\w*"
    r"|study\s+plan|plan\s+(?:my|the)\s+stud\w*|schedule\s+(?:my\s+)?stud\w*)\b",
    re.IGNORECASE,
)


def asks_plan(text: str) -> bool:
    return bool(_PLAN_RE.search(text or ""))


def plan_offer(text: str, plans: list[dict]) -> dict | None:
    """The line under an answer to a study-plan request: the plan Learn
    already tracks, or the form that writes one. None for any other message."""
    if not asks_plan(text):
        return None
    active = [p for p in plans if p.get("status") != "completed"]
    if active:
        p = active[0]
        by = f", by {p['by']}" if p.get("by") else ""
        return {"kind": "study", "text": f"Learn tracks a plan for this: {p['name']}{by}", "note": p["path"],
                "commands": [{"label": "open it", "href": "#learn/plan/" + quote(p["path"], safe="")},
                             {"label": "continue it", "command": "/learn"}]}
    return {"kind": "study", "text": "Learn can track this as a plan, paced to its date", "note": "",
            "commands": [{"label": "write it as a plan", "href": "#learn/new"}]}
