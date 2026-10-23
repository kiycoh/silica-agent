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
    being told, or the latest a miss Learn's confidence marks as a blind spot
    (wrong while sure, mastery.BLIND_SPOT_CONF). Confidence is read only once
    the ledger holds mastery.CALIBRATION_MIN eligible answers, and a right
    answer given unsure triggers nothing, the scheduler brings it back: the
    rule of the research report difficulty-detection-and-study-aids, section 8
    (an earlier version of this offered help for those, 2026-10-23)."""
    from silica.kernel.learn.mastery import BLIND_SPOT_CONF, CALIBRATION_MIN, eligible
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
        elif (calibrated and eligible(last) and last.get("conf", 0) >= BLIND_SPOT_CONF
              and not last.get("correct")):
            out[p] = f"you were sure about {name} and missed it"
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
    state = "lost" if lost else "blind" if "were sure" in why else "missed"
    return {"kind": "study", "text": why, "note": target, "commands": aids(target, name, state)}


AIDS_SHOWN = 5   # practice plus four forms: one short line under the answer

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.M)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_STEP_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.M)
_LINK_RE = re.compile(r"\[\[")


def aids(path: str, name: str, state: str = "missed", text: str | None = None) -> list[dict]:
    """The ways to work on a hard note, at most AIDS_SHOWN: practice first,
    then the aid the reader's state calls for, then the forms the note's shape
    allows.

    From the research report difficulty-detection-and-study-aids (section 8),
    by state: a reader who declared it unclear ("lost") or missed it ("missed")
    gets the worked explanation first (/explain; worked examples help a reader
    low in prior knowledge, d = 0.505); a confident miss ("blind") gets a
    contrast to fill (a table), not another explanation. Practice leads in
    every case, since every aid must end in a retrieval check and practice has
    the strongest evidence (d = 0.74; retrieval beat concept mapping,
    d = 1.50). The note's shape then picks among a diagram (numbered steps or
    formulas), a map (many links), cards (several points) and slides (several
    sections, or a lesson's outline). Every form stays one command away."""
    if text is None:
        from silica.kernel.learn.items import read_note

        text = read_note(path)
    practise = {"label": "practise it", "command": f'/quiz "{path}" --n=3'}
    explain = {"label": "explain it", "command": f'/explain "{name}"'}
    table = {"label": "as a table", "command": f'/schematize "{name}"'}
    shown = [practise] + ([table, explain] if state == "blind" else [explain])
    has_items = len(_TABLE_ROW_RE.findall(text)) >= 3 or len(_LIST_ITEM_RE.findall(text)) >= 4
    spine = bool(re.search(r"^section:\s*['\"]?\(outline\)", text, re.M))
    shape = [
        # A lesson's outline is a deck already: its slides come first.
        (spine, {"label": "slides", "command": f'/slides "{name}"'}),
        (has_items, table),
        (len(_STEP_RE.findall(text)) >= 3 or text.count("$$") >= 2,
         {"label": "as a diagram", "command": f'/diagram "{name}"'}),
        (len(_LINK_RE.findall(text)) >= 5, {"label": "map it", "command": f'/map "{name}"'}),
        (len(_LIST_ITEM_RE.findall(text)) >= 3 or text.count("[^") >= 3,
         {"label": "flashcards", "command": f'/flashcards "{name}" --n=8'}),
        (len(_HEADING_RE.findall(text)) >= 3, {"label": "slides", "command": f'/slides "{name}"'}),
    ]
    shown += [aid for fits, aid in shape if fits]
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
    """SILICA_STUDY_OFFER: "on" (default) shows the offer, "shadow" logs it
    without showing it, "off" neither."""
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


def escalation_block(name: str, why: str) -> str:
    """The instruction that makes the next answer the aid (the report's
    "escalate" rung): worked, drawn, and ending on a retrieval question."""
    return (f"[The reader is still struggling with [[{name}]] ({why}). Make this answer the aid: a worked "
            f"example step by step in the note's own terms, then the mechanism as one small Markdown table "
            f"or one Mermaid diagram, and end with one question the reader can answer only by using it. "
            f"Ground every step in the note.]")


# The escalate rung inside Learn, where the report puts it ("generate the aid as
# the next tutor move inside Learn"): after the rating that makes it three
# misses in a row, or two more after an offer went unused, the chat model
# writes the aid from the note's checked points. Reading it is never an
# answer: credit comes from the next unassisted one (recommendation 5).
AID_MAX_TOKENS = 900   # 6 steps or a 4-row table plus a question is about 400 tokens; formulas take the rest
AID_FORMS = {
    "worked": ("a worked example in 3 to 6 numbered steps, each one line in the note's own terms, that reaches "
               "what the note supports"),
    # a confident miss or a recorded misconception gets a contrast, not an
    # explanation (report section 8; comparing cases, Alfieri 2013, d = 0.50);
    # the blank keeps it an attempt
    # the blank's answer nowhere else in the table: the first table the 9B
    # wrote put it in row 1, so the question was answered by copying (2026-10-23)
    "contrast": ("one Markdown table of 2 to 4 rows with three columns, the aspect, what you wrote (speak to the "
                 "reader as you) and what the note says, headed in the language below; the last row's note cell "
                 "left as ____ for the reader to fill, on an aspect whose answer appears nowhere else in the table"),
}
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


def aid_form(path: str, misconception: str | None, entries: list[dict]) -> str:
    """"contrast" for a recorded misconception or a calibrated confident miss,
    "worked" otherwise."""
    if misconception or "were sure" in struggles([path], entries).get(path, ""):
        return "contrast"
    return "worked"


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


def learn_aid(item: dict, why: str, form: str, production: str, prereq: str | None) -> str:
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
    prompt = AID_PROMPT.format(title=title, why=why, form=AID_FORMS.get(form, AID_FORMS["worked"]),
                               points="\n".join(f"- {p}" for p in points) or "- (none)", q=item.get("q", ""),
                               answer=item.get("answer") or item.get("claim", ""), production=production or "(none)",
                               prereq=pre, language=grade.reply_language(item.get("q", ""), production))
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}], max_tokens=AID_MAX_TOKENS,
                    temperature=0.2, reasoning=False)
    text = (resp.text or "").strip()
    return _blank_last_cell(text) if form == "contrast" and "____" not in text else text


def _blank_last_cell(md: str) -> str:
    """The contrast table with its last row's note cell blanked, when the
    model left none (the second table the 9B wrote, 2026-10-23): the blank is
    what keeps the aid an attempt rather than a page to read."""
    lines = md.splitlines()
    rows = [i for i, line in enumerate(lines) if line.strip().startswith("|") and line.strip().endswith("|")]
    if len(rows) < 3:  # a header, its rule and one row at least
        return md
    cells = lines[rows[-1]].strip()[1:-1].split("|")
    if not cells[-1].strip() or set(cells[-1].strip()) <= set("-: "):
        return md
    cells[-1] = " ____ "
    lines[rows[-1]] = "|" + "|".join(cells) + "|"
    return "\n".join(lines)


def log_aid(note: str, session: str, why: str, displayed: bool) -> None:
    """A Learn aid's record. It carries no `shown`, so neither the chat's cap,
    nor the decline memory, nor an escalation counts it as an offer."""
    _append({"aid": "learn", "session": session, "note": note, "why": why, "displayed": displayed})


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
            "aids_shown": sum(1 for r in records if r.get("aid") and r.get("displayed"))}
