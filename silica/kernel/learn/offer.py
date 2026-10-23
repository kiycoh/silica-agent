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


def struggles(paths: list[str], entries: list[dict]) -> dict[str, str]:
    """{note: why} for the notes among `paths` whose last answers say the
    reader struggles: MISSES wrong of the last RECENT, the latest answered by
    being told, or the latest an answer Learn's confidence marks: wrong while
    sure (a blind spot, the error most worth correcting) or right while unsure
    (fragile). The confidence bands are Learn's own (mastery.BLIND_SPOT_CONF,
    FRAGILE_CONF), read the way its suggestions read them."""
    from silica.kernel.learn.mastery import BLIND_SPOT_CONF, FRAGILE_CONF, eligible
    from silica.kernel.report.quiz import key

    by_key: dict[str, list[dict]] = {}
    for e in entries:
        by_key.setdefault(key(str(e.get("path") or "")), []).append(e)
    out: dict[str, str] = {}
    for p in paths:
        evs = sorted(by_key.get(key(p), []), key=lambda e: str(e.get("ts")))[-RECENT:]
        if not evs:
            continue
        name = p.removesuffix(".md").rsplit("/", 1)[-1]
        last = evs[-1]
        if sum(1 for e in evs if not e.get("correct")) >= MISSES or last.get("told"):
            out[p] = f"you missed {name} in recent practice"
        elif eligible(last) and last.get("conf", 0) >= BLIND_SPOT_CONF and not last.get("correct"):
            out[p] = f"you were sure about {name} and missed it"
        elif eligible(last) and last.get("conf") == FRAGILE_CONF and last.get("correct"):
            out[p] = f"you got {name} right but were unsure"
    return out


def struggling(paths: list[str], entries: list[dict]) -> list[str]:
    """The notes among `paths` the reader struggles with (`struggles`)."""
    return list(struggles(paths, entries))


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
    hard = struggles(notes, entries)
    lost = says_lost(text)
    if not hard and not lost:
        return None
    target = next(iter(hard), notes[0])
    name = target.removesuffix(".md").rsplit("/", 1)[-1]
    why = hard[target] if hard and not lost else "you said this was not clear"
    return {"kind": "study", "text": why, "note": target, "commands": aids(target, name, lost)}


AIDS_SHOWN = 5   # practice plus four forms: one short line under the answer

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.M)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.M)
_LINK_RE = re.compile(r"\[\[")


def aids(path: str, name: str, lost: bool = False, text: str | None = None) -> list[dict]:
    """The ways to work on a hard note, practice first, then the forms its
    shape asks for, at most AIDS_SHOWN.

    Practice leads because it is the one with the strongest evidence
    (practice testing d = 0.74, and retrieval beat concept mapping, d = 1.50;
    research reports difficulty-detection-and-study-aids and
    course-corpus-to-knowledge-graph, 2026-10-23). A reader who said it was not
    clear is offered the explanation (/explain, a report) next. Then the note's
    shape picks among the others: items to compare make a table (/schematize),
    numbered steps or formulas a diagram, many links a map, several points
    cards, several sections slides. Every form stays one command away whether
    shown here or not."""
    if text is None:
        from silica.kernel.learn.items import read_note

        text = read_note(path)
    shown = [{"label": "practise it", "command": f'/quiz "{path}" --n=3'}]
    if lost:
        shown.append({"label": "explain it", "command": f'/explain "{name}"'})
    shape = [
        (len(_TABLE_ROW_RE.findall(text)) >= 3 or len(_LIST_ITEM_RE.findall(text)) >= 4,
         {"label": "as a table", "command": f'/schematize "{name}"'}),
        (len(_STEP_RE.findall(text)) >= 3 or text.count("$$") >= 2,
         {"label": "as a diagram", "command": f'/diagram "{name}"'}),
        (len(_LINK_RE.findall(text)) >= 5, {"label": "map it", "command": f'/map "{name}"'}),
        (len(_LIST_ITEM_RE.findall(text)) >= 3 or text.count("[^") >= 3,
         {"label": "flashcards", "command": f'/flashcards "{name}" --n=8'}),
        (len(_HEADING_RE.findall(text)) >= 3, {"label": "slides", "command": f'/slides "{name}"'}),
    ]
    shown += [aid for fits, aid in shape if fits]
    # a note too short to show a shape still gets the two the reader can always use
    if len(shown) < 3:
        shown += [{"label": "as a diagram", "command": f'/diagram "{name}"'},
                  {"label": "flashcards", "command": f'/flashcards "{name}" --n=8'}]
    return list({a["label"]: a for a in shown}.values())[:AIDS_SHOWN]


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
