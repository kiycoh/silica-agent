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

import functools
import re
from urllib.parse import quote

# The reader saying it did not follow, in the two languages this was written
# against. A word list, not a classifier: self-declared beats inferred. The
# adverb slot is there because "still" is how a second turn says it: "non ho
# ancora capito" went unread, and the escalation it should start with it.
_LOST_RE = re.compile(
    r"\b(non\s+(?:l['’])?(?:ho\s+)?(?:ancora\s+|proprio\s+|bene\s+|del\s+tutto\s+)?capi(?:t[oaie]|sco|re)"
    r"|non\s+mi\s+(?:è|e['’])\s+(?:ancora\s+|del\s+tutto\s+)?chiar[oa]|spiegami\s+(?:meglio|di\s+nuovo)"
    r"|più\s+semplice|non\s+riesco\s+a\s+capire|i\s+(?:still\s+)?(?:don'?t|do\s+not)\s+(?:get|understand)"
    r"|i'?m\s+(?:still\s+)?lost|explain\s+(?:it\s+)?again|simpler)\b",
    re.IGNORECASE,
)
RECENT = 3    # answers looked at per note
MISSES = 2    # of the RECENT that were wrong for a note to count as a struggle


# Jev (agent/decide.py) reads what the word list misses. On 80 labelled
# messages (40 saying the student did not follow, 40 questions, requests and
# "ho capito", 2026-10-23) the list caught 12 of the 40, Jev 39 at p >= 0.6,
# neither said yes to a negative (the highest at 0.37), Jev at p50 0.32 s.
# The list stays first, instant and free; Jev reads only a short message the
# list did not match, once per message. The owner asked for Jev where it
# pays; reopen with 200 labelled turns from the shadow log.
LOST_ACCEPT = 0.6
LOST_MAX_CHARS = 280   # a declaration of confusion is a line; a long message is a request with its content
LOST_QUESTION = {"lost": {
    "type": "choice",
    "instructions": ("A student wrote this message to a study assistant. Does the student say they did not "
                     "understand, are confused, lost or stuck, or ask for the same thing again, differently or "
                     "more simply?"),
    "criteria": {
        "yes": ("the message says the student does not understand, is confused, lost or stuck, or asks for an "
                "explanation again, differently or more simply"),
        "no": "a question, a request or a statement that does not say the student failed to understand",
    },
}}


@functools.lru_cache(maxsize=512)
def _jev_lost(text: str) -> bool:
    from silica.agent.decide import decide

    answers = decide({"message": text}, LOST_QUESTION)
    return bool(answers) and answers["lost"]["probabilities"].get("yes", 0.0) >= LOST_ACCEPT


def says_lost(text: str) -> bool:
    t = (text or "").strip()
    if _LOST_RE.search(t):
        return True
    if not t or t.startswith("/") or len(t) > LOST_MAX_CHARS:
        return False
    from silica.config import CONFIG

    return bool(CONFIG.decision_model) and _jev_lost(t)


def struggles(paths: list[str], entries: list[dict]) -> dict[str, str]:
    """{note: why} for the notes among `paths` whose last answers say the
    reader struggles: MISSES wrong of the last RECENT, the latest answered by
    being told, or the latest marked by Learn's confidence: wrong while sure
    (mastery.BLIND_SPOT_CONF) or right while unsure (mastery.FRAGILE_CONF).
    Confidence is read only once the ledger holds mastery.CALIBRATION_MIN
    eligible answers (research report difficulty-detection-and-study-aids,
    section 8). The report left a right answer given unsure to the scheduler;
    on 4,918 real students (Eedi NeurIPS 2020) the next answer after one was
    wrong 43.6% of the time, after a confident miss 40.9%, after silence
    36.6%, so it counts (the owner's call, 2026-10-23)."""
    from silica.kernel.learn.mastery import BLIND_SPOT_CONF, CALIBRATION_MIN, FRAGILE_CONF, eligible
    from silica.kernel.report.quiz import key

    calibrated = sum(1 for e in entries if eligible(e)) >= CALIBRATION_MIN
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
        elif calibrated and eligible(last):
            if not last.get("correct") and last["conf"] >= BLIND_SPOT_CONF:
                out[p] = f"you were sure about {name} and missed it"
            elif last.get("correct") and last["conf"] <= FRAGILE_CONF:
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
    # Said unclear, the note it is about leads (the caller puts the named note
    # first): a note missed in the ledger had taken the offer from the lesson
    # the reader named, under a line saying "you said this was not clear".
    target = notes[0] if lost else next(iter(hard))
    name = target.removesuffix(".md").rsplit("/", 1)[-1]
    why = hard[target] if hard and not lost else "you said this was not clear"
    state = ("lost" if lost else "blind" if "were sure" in why else "fragile" if "unsure" in why else "missed")
    return {"kind": "study", "text": why, "note": target, "commands": aids(target, name, state)}


AIDS_SHOWN = 5   # practice plus four forms: one short line under the answer

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.M)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.M)
_LINK_RE = re.compile(r"\[\[")


_SERVICE_RE = re.compile(r"^#{1,6}\s+(?:Relations|Sources)\s*$", re.M)


def shape_forms(text: str) -> list[str]:
    """The forms a note's shape allows, in the order they help: slides for a
    lesson's outline (a deck already), a table for items to compare, a
    diagram for numbered steps or formulas, a map for many links, cards for
    several points, slides for several sections.

    Read on the body. The frontmatter's `related:`, `tags:` and `sources:`
    are YAML lists, and counted as items they made nearly every nucleated
    note a table (the course, 2026-10-23: AdaGrad, an algorithm in formulas,
    came out a table). Relations and Sources are the note's links, not its
    content: they count for the map, never for items."""
    from silica.kernel.write import frontmatter

    spine = bool(re.search(r"^section:\s*['\"]?\(outline\)", text, re.M))
    _data, _raw, body = frontmatter.split(text)
    cut = _SERVICE_RE.search(body)
    main = body[:cut.start()] if cut else body
    linked = body.split("## Sources", 1)[0]
    has_items = len(_TABLE_ROW_RE.findall(main)) >= 3 or len(_LIST_ITEM_RE.findall(main)) >= 4
    # four inline formulas weigh as two display ones: a course note's
    # algorithm is written inline ($\boldsymbol{r} \leftarrow ...$)
    formulas = main.count("$$") >= 2 or main.replace("$$", "").count("$") >= 8
    fits = [
        (spine, "slides"),
        (has_items, "table"),
        (len(_STEP_RE.findall(main)) >= 3 or formulas, "diagram"),
        (len(_LINK_RE.findall(linked)) >= 5, "map"),
        (len(_LIST_ITEM_RE.findall(main)) >= 3 or body.count("[^") >= 3, "cards"),
        (len(_HEADING_RE.findall(main)) >= 3, "slides"),
    ]
    return list(dict.fromkeys(form for ok, form in fits if ok))


_FORM_COMMANDS = {
    "slides": ("slides", '/slides "{name}"'),
    "table": ("as a table", '/schematize "{name}"'),
    "diagram": ("as a diagram", '/diagram "{name}"'),
    "map": ("map it", '/map "{name}"'),
    "cards": ("flashcards", '/flashcards "{name}" --n=8'),
}


def aids(path: str, name: str, state: str = "missed", text: str | None = None) -> list[dict]:
    """The ways to work on a hard note, at most AIDS_SHOWN: practice first,
    then the aid the reader's state calls for, then the forms the note's shape
    allows (`shape_forms`).

    From the research report difficulty-detection-and-study-aids (section 8),
    by state: a reader who declared it unclear ("lost") or missed it ("missed")
    gets the worked explanation first (/explain; worked examples help a reader
    low in prior knowledge, d = 0.505); a confident miss ("blind") gets a
    contrast to fill (a table), not another explanation. Practice leads in
    every case, since every aid must end in a retrieval check and practice has
    the strongest evidence (d = 0.74; retrieval beat concept mapping,
    d = 1.50). Every form stays one command away."""
    if text is None:
        from silica.kernel.learn.items import read_note

        text = read_note(path)
    practise = {"label": "practise it", "command": f'/quiz "{path}" --n=3'}
    explain = {"label": "explain it", "command": f'/explain "{name}"'}
    table = {"label": "as a table", "command": f'/schematize "{name}"'}
    # right but unsure: practice and the note's forms, no new explanation (the
    # report's table, section 8: a fragile hit wants retrieval, not more text)
    shown = [practise] + ([table, explain] if state == "blind" else [] if state == "fragile" else [explain])
    for form in shape_forms(text):
        label, command = _FORM_COMMANDS[form]
        shown.append({"label": label, "command": command.format(name=name)})
    if len(shown) < 3:
        # a note too short to show a shape: the two forms any note allows
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


# What Learn's plan computes and the chat model would otherwise make up, put
# in the turn before the model answers: asked for a study plan, the calendar
# from the plan's steps and date (plans.day_by_day); asked what the syllabus
# holds that the lectures do not, the topics no lecture deck mentions
# (plans.lecture_gaps). The local 9B dated a plan "about 17 weeks" and listed
# half the programme as missing from the slides (2026-10-23).
_SYLLABUS_RE = re.compile(r"\b(programma|scheda|syllabus|curriculum)\b", re.I)
_LECTURES_RE = re.compile(r"\b(slides?|lezion[ei]|lectures?|lessons?)\b", re.I)


def plan_context(text: str, plans: list[dict]) -> str:
    """The block for this message, or "": the plan the message names, else the
    first open one."""
    from silica.kernel.learn import plans as pl

    open_plans = [p for p in plans if p.get("status") != "completed"]
    if not open_plans or not (asks_plan(text) or (_SYLLABUS_RE.search(text) and _LECTURES_RE.search(text))):
        return ""
    low = text.casefold()
    row = next((p for p in open_plans if p.get("name", "").casefold() in low), open_plans[0])
    plan = pl.read_plan(row["path"])
    if asks_plan(text):
        rows = pl.day_by_day(plan)
        if not rows:
            return ""
        return (f"[Learn tracks the plan {plan['name']}, by {plan['by']}. Its day-by-day calendar, computed from "
                f"the plan's steps in the syllabus's order and its date; reproduce this table as it is, without "
                f"changing a date or a step, then add advice if useful:\n{pl.day_by_day_table(rows)}]")
    found = pl.lecture_gaps(plan) if plan.get("scope") == "steps" else None
    if not found:
        return ""
    gaps, decks = found
    return (f"[Learn's plan {plan['name']} reads its syllabus, {plan['target']}, against the text of the {decks} "
            f"lecture decks. Topics of the syllabus that no deck mentions: {', '.join(gaps) or 'none'}. Use this "
            f"list for what the slides do not cover; a topic named another way in the slides can still be there.]")


# --- the offer's own record: caps, declines, escalation, and its validation --
#
# The research report's section 8 (difficulty-detection-and-study-aids): stay
# silent once two offers were made in a session or an offer on the note was
# declined or ignored in the last 7 days; escalate to a generated aid after 3
# misses in a row, or 2 more misses after an ignored offer; and ship the rule
# in shadow mode first, logging what would fire. The log below is that record,
# one line per offer shown or held back and per offer acted on, so the rule
# can be checked against what readers then did (`replay`).
MAX_OFFERS_PER_CHAT = 2
DECLINE_DAYS = 7
ESCALATE_MISSES = 3


def mode() -> str:
    """SILICA_STUDY_OFFER: "on" (default) shows the offer and the escalated
    aid, "shadow" logs them without showing them, "off" neither. Hosted runs
    "on" too: the owner's call of 2026-10-23, against the report's shadow
    first, with the rule replayed on 4,163 real students (ASSISTments:
    the next answer wrong 55.1% after it fires, 23.6% after silence);
    `shadow` stays one variable away for a tenant."""
    import os

    value = (os.getenv("SILICA_STUDY_OFFER") or "on").strip().lower()
    return value if value in ("on", "shadow", "off") else "on"


def _log_path():
    from silica.kernel.recall.paths import index_dir

    return index_dir() / "study_offers.jsonl"


def _records() -> list[dict]:
    import json

    try:
        lines = _log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn last line from a crash is one lost record, not a lost log
    return out


def _append(record: dict) -> None:
    import json
    from datetime import datetime, timezone

    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a log that cannot be written costs its record, never the turn


def _age_days(ts: str, now: float) -> float:
    from datetime import datetime

    try:
        return (now - datetime.fromisoformat(ts).timestamp()) / 86400
    except (TypeError, ValueError):
        return float("inf")


def allowed(chat: str, note: str, now: float | None = None, records: list[dict] | None = None) -> bool:
    """Whether an offer on `note` may be shown in `chat`: fewer than
    MAX_OFFERS_PER_CHAT shown there, and none on the note ignored elsewhere in
    the last DECLINE_DAYS."""
    import time

    now = time.time() if now is None else now
    records = _records() if records is None else records
    if sum(1 for r in records if r.get("chat") == chat and r.get("shown")) >= MAX_OFFERS_PER_CHAT:
        return False
    acted = {(r.get("chat"), r.get("note")) for r in records if r.get("acted")}
    return not any(r.get("shown") and r.get("note") == note and r.get("chat") != chat
                   and (r.get("chat"), note) not in acted and _age_days(str(r.get("ts")), now) <= DECLINE_DAYS
                   for r in records)


def log_offer(chat: str, offer: dict, shown: bool) -> None:
    _append({"chat": chat, "note": offer.get("note", ""), "why": offer.get("text", ""), "shown": shown,
             "commands": [c.get("command") or c.get("href") for c in offer.get("commands", [])]})


def log_acted(chat: str, text: str) -> None:
    """Mark the offer of this chat whose command the reader just sent."""
    typed = (text or "").strip()
    if not typed.startswith("/"):
        return
    for r in reversed(_records()):
        if r.get("chat") == chat and r.get("shown") and any(c and typed.startswith(c) for c in r.get("commands", [])):
            _append({"chat": chat, "note": r.get("note", ""), "acted": True, "command": typed.split()[0]})
            return


def escalation(note: str, chat: str, text: str, history: list[str], entries: list[dict],
               records: list[dict] | None = None) -> str:
    """Why the next answer on `note` should be the aid itself, or "": three
    misses in a row, two more misses after an offer on it was ignored, or a
    reader still lost after an answer in this chat already said so."""
    from silica.kernel.report.quiz import key

    evs = sorted((e for e in entries if key(str(e.get("path") or "")) == key(note)), key=lambda e: str(e.get("ts")))
    if len(evs) >= ESCALATE_MISSES and not any(e.get("correct") for e in evs[-ESCALATE_MISSES:]):
        return f"{ESCALATE_MISSES} misses in a row"
    records = _records() if records is None else records
    acted = {(r.get("chat"), r.get("note")) for r in records if r.get("acted")}
    for r in records:
        if r.get("shown") and r.get("note") == note and (r.get("chat"), note) not in acted:
            later = [e for e in evs if str(e.get("ts")) > str(r.get("ts")) and not e.get("correct")]
            if len(later) >= 2:
                return "two more misses after an offer went unused"
    if says_lost(text) and any(says_lost(h) for h in history):
        return "still unclear after an answer"
    return ""


# The aid an escalated chat answer becomes, by the note's first shape: the
# artifact the offer would have named, made in the answer instead of offered
# (the goal of 2026-10-23: slides, maps, cards and tables to make a hard topic
# digestible in the chat, not only on request).
ESCALATE_FORMS = {
    "slides": ("a short deck of it in its outline's order: 4 to 6 slides separated by lines holding only ---, "
               "each a ## title and 2 to 4 bullets in the notes' own words"),
    "table": "the items it compares as one Markdown table, one row each, in the note's own terms",
    "diagram": "a worked example step by step in the note's own terms, then the mechanism as one Mermaid flowchart",
    "map": ("one Mermaid flowchart (graph LR) from it to the notes it links, each edge labelled with the "
            "relation in a few words, then two lines on the links that matter most"),
    # spelled out: given "`question :: answer`", the 9B wrote "Q1 :: <the whole
    # point>" five times (2026-10-23), cards with no question
    "cards": ("5 flashcards, one bullet each: a real question ending with a question mark, then ` :: `, then "
              "its short answer in the note's words; one point of the note each"),
}
ESCALATE_DEFAULT = ("a worked example step by step in the note's own terms, then the mechanism as one small "
                    "Markdown table or one Mermaid diagram")


def escalation_block(name: str, why: str, text: str = "") -> str:
    """The instruction that makes the next answer the aid (the report's
    "escalate" rung), in the form the note's shape calls for, ending on a
    retrieval question."""
    forms = shape_forms(text) if text else []
    form = ESCALATE_FORMS[forms[0]] if forms else ESCALATE_DEFAULT
    # "this answer", said twice: told only to make the answer the aid, the
    # local 9B wrote the deck as a new note and answered with its link
    # (2026-10-23), a write nobody asked for.
    # "give no answer to it": the AdaGrad aid closed on its question and then
    # its answer in brackets (2026-10-23), which leaves nothing to retrieve
    return (f"[The reader is still struggling with [[{name}]] ({why}). Make this answer the aid, written from "
            f"the note: {form}. End with one question the reader can answer only by using it, and give no "
            f"answer to it. Ground every step in the note; formulas in $...$ as the note writes them. Write "
            f"the aid in this answer, here in the chat; READ-ONLY: do not create, edit, patch, or move any note.]")


# The escalate rung inside Learn, where the report puts it: after the rating
# that makes it three misses in a row, or two more after an offer went
# unused, the chat model writes the aid from the note's checked points when
# the reader asks for it (one click, the owner's call of 2026-10-23: in
# ASSISTments' randomized experiments the aids asked for helped and the one
# that came unasked after an error did not). Reading it is never an answer:
# credit comes from the next unassisted one (recommendation 5).
AID_MAX_TOKENS = 900   # 6 steps or a 4-row table plus a question is about 400 tokens; formulas take the rest
# The worked example, for every state: in ASSISTments' randomized experiments
# an example beat the answer alone (d +0.24; +0.33 for students below the
# average) while feedback aimed at the common wrong answer did not (d -0.05,
# 7,046 students), so the contrast table a misconception used to get here is
# gone (the owner's call, 2026-10-23); /schematize still makes one on request.
AID_FORM = ("a worked example in 3 to 6 numbered steps, each one line in the note's own terms, that reaches "
            "what the note supports")
AID_PROMPT = """A reader has just missed a question on the note "{title}" ({why}). Write the next tutor move: {form}.

Use only what these points of the note state, and name nothing they do not hold.
POINTS:
{points}
THE QUESTION: {q}
WHAT THE NOTE SUPPORTS: {answer}
WHAT THE READER WROTE: {production}
{prereq}End with one line in bold holding one question the reader can answer only by using what you wrote, and give no answer to it.
Write all of it in {language}. No preamble and no heading; under 220 words; formulas in $...$ as the points write them.
"""


def weakest_prerequisite(path: str) -> str | None:
    """The prerequisite of `path` the reader holds least: never practised
    first, then the lowest estimated recall. The report's escalate rung
    attaches it, as the H2 hint already names prerequisites."""
    from silica.kernel.report import learner

    try:
        prereqs = learner.prerequisites_map().get(learner.key_of(path), [])
        rows = learner.view()
    except Exception:
        return None  # a store that does not read costs the attachment, never the aid

    def held(p: str) -> float:
        row = rows.get(learner.key_of(p)) or {}
        return row["R"] if row.get("attempts") and row.get("R") is not None else -1.0

    return min(prereqs, key=held) if prereqs else None


def learn_aid(item: dict, why: str, production: str, prereq: str | None) -> str:
    """The aid's Markdown, from the item's note and the reader's answer."""
    from pathlib import Path

    from silica.agent.llm import call_llm
    from silica.config import CONFIG
    from silica.kernel.learn import grade, items

    try:
        points = [kp["text"] for kp in items.key_points(item["path"])][:12]
    except Exception:
        points = []  # claims that do not parse: the item's own span still grounds it
    if item.get("span") and item["span"] not in points:
        points.append(item["span"])
    title = item.get("title") or Path(item["path"]).stem
    pre = (f"Before the last line, one line on how [[{Path(prereq).stem}]], the prerequisite the reader holds "
           f"least, is used here.\n" if prereq else "")
    prompt = AID_PROMPT.format(title=title, why=why, form=AID_FORM,
                               points="\n".join(f"- {p}" for p in points) or "- (none)", q=item.get("q", ""),
                               answer=item.get("answer") or item.get("claim", ""), production=production or "(none)",
                               prereq=pre, language=grade.reply_language(item.get("q", ""), production))
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}], max_tokens=AID_MAX_TOKENS,
                    temperature=0.2, reasoning=False)
    return (resp.text or "").strip()


def log_aid(note: str, session: str, why: str, displayed: bool) -> None:
    """A Learn aid's record. It carries no `shown`, so neither the chat's cap,
    nor the decline memory, nor an escalation counts it as an offer."""
    _append({"aid": "learn", "session": session, "note": note, "why": why, "displayed": displayed})


def log_aid_opened(note: str, session: str) -> None:
    """The reader asked for the aid the escalation put one click away."""
    _append({"aid": "learn", "session": session, "note": note, "opened": True})


def replay(entries: list[dict], records: list[dict] | None = None) -> dict:
    """The rule run over a ledger as it grew: at each graded answer, would the
    offer have fired on what came before, and was that answer then wrong? A
    rule worth its line fires before misses more than before hits. Plus how
    often a shown offer was acted on. Counts only; reading them is the point."""
    from silica.kernel.report.quiz import key

    by_note: dict[str, list[dict]] = {}
    for e in entries:
        by_note.setdefault(key(str(e.get("path") or "")), []).append(e)
    fired = {"wrong": 0, "right": 0}
    silent = {"wrong": 0, "right": 0}
    for evs in by_note.values():
        evs.sort(key=lambda e: str(e.get("ts")))
        for i in range(1, len(evs)):
            hit = bool(struggles([str(evs[i].get("path"))], evs[:i]))
            bucket = fired if hit else silent
            bucket["right" if evs[i].get("correct") else "wrong"] += 1
    records = _records() if records is None else records
    shown = sum(1 for r in records if r.get("shown"))
    acted = sum(1 for r in records if r.get("acted"))

    def rate(b: dict) -> float | None:
        n = b["wrong"] + b["right"]
        return round(b["wrong"] / n, 3) if n else None

    return {"fired": fired, "silent": silent, "miss_rate_when_fired": rate(fired),
            "miss_rate_when_silent": rate(silent), "offers_shown": shown, "offers_acted": acted,
            "aids_shown": sum(1 for r in records if r.get("aid") and r.get("displayed")),
            "aids_opened": sum(1 for r in records if r.get("aid") and r.get("opened"))}
