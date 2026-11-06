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
from pathlib import Path
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


# Jev (model/decide.py) reads what the word list misses. On 80 labelled
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
    from silica.model.decide import decide

    answers = decide({"message": text}, LOST_QUESTION)
    return bool(answers and answers["lost"]["probabilities"].get("yes", 0.0) >= LOST_ACCEPT)


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
    from silica.kernel.learn.quiz import key

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
        from silica.kernel.learn.quiz import entries as ledger

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
    from silica.kernel.base import frontmatter

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


def plan_offer(text: str, plans: list[dict], draft: dict | None = None) -> dict | None:
    """The line under an answer to a study-plan request: the plan this turn
    computed, saved by the reader's click with what the Memorize form posts
    (`draft`, plan_context's third value), or the plan Memorize already
    tracks. None for any other message, and for the turn that asked the
    reader for the material: a second way in under that question is noise."""
    if draft:
        by = f", paced to {draft['by']}" if draft.get("by") else ""
        return {"kind": "study", "text": f"Memorize can keep this plan{by}", "note": "",
                "commands": [{"label": "save it in Memorize", "save": draft}]}
    if not asks_plan(text):
        return None
    active = [p for p in plans if p.get("status") != "completed"]
    if active:
        p = active[0]
        by = f", by {p['by']}" if p.get("by") else ""
        return {"kind": "study", "text": f"Memorize tracks a plan for this: {p['name']}{by}", "note": p["path"],
                "commands": [{"label": "open it", "href": "#memorize/plan/" + quote(p["path"], safe="")},
                             {"label": "continue it", "command": "/memorize"}]}
    return None


# What Learn's plan computes and the chat model would otherwise make up, put
# in the turn before the model answers: asked for a study plan, the calendar
# from the plan's steps and date (plans.day_by_day); asked what the syllabus
# holds that the lectures do not, the topics no lecture deck mentions
# (plans.lecture_gaps). The local 9B dated a plan "about 17 weeks" and listed
# half the programme as missing from the slides (2026-10-23).
_SYLLABUS_RE = re.compile(r"\b(programma|scheda|syllabus|curriculum)\b", re.I)
_LECTURES_RE = re.compile(r"\b(slides?|lezion[ei]|lectures?|lessons?)\b", re.I)
# Every plan or calendar the code appends to an answer carries this phrase,
# and the question for the material never does: the next turn reads it to
# tell a plan already shown from that question.
COMPUTED = "computed by Memorize"
FOLDERS_NAMED = 4   # the folders the question for the material names as examples
TREE_SHOWN = 12     # the parts of a plan with no steps listed under the answer


def plan_request(text: str, prev_user: str = "", prev_answer: str = "") -> str:
    """The words a new plan is read from, or "": this message when it asks
    for a study plan, else the request of the turn before and this message
    together, when that turn asked for a plan and got none (its answer was
    the question for the material). One turn of follow-up only: a question
    on the material after a plan was shown is not read as a plan again."""
    if not text or text.startswith("/"):
        return ""
    if asks_plan(text):
        return text
    if asks_plan(prev_user) and COMPUTED not in (prev_answer or ""):
        return f"{prev_user}\n{text}"
    return ""


def plan_context(text: str, plans: list[dict], prev_user: str = "",
                 prev_answer: str = "") -> tuple[str, str, dict | None]:
    """(the block for the model, the text appended under its answer, the
    plan to save) for this message, ("", "", None) when no plan has anything
    for it. A request that names material no open plan covers gets a plan
    computed now and offered for saving (plan_draft); one that names none,
    with no plan open, gets the model asking for it; otherwise the plan the
    message names, else the first open one. The calendar is appended by
    code, not copied by the model: told to reproduce it, the 9B merged three
    days and dropped 1 November (2026-10-23)."""
    from silica.kernel.learn import plans as pl

    open_plans = [p for p in plans if p.get("status") != "completed"]
    low = text.casefold()
    # a plan named like a request ("Piano di studio") would catch every later
    # request by its name and no new plan could be drawn from the chat
    row = next((p for p in open_plans if p.get("name") and not asks_plan(p["name"]) and p["name"].casefold() in low),
               None)
    sentence = "" if row else plan_request(text, prev_user, prev_answer)
    if sentence:
        read = pl.read_sentence(sentence)
        if read.get("scope") and read.get("target"):
            kept = pl.target_text(read["scope"], read["target"])
            row = next((p for p in open_plans if p.get("scope") == read["scope"] and p.get("target") == kept), None)
            if row is None:
                draft = plan_draft(read)
                return (*draft_context(draft), draft["body"])
        elif not open_plans:
            return ask_block(), "", None
    if not open_plans or not (asks_plan(text) or (_SYLLABUS_RE.search(text) and _LECTURES_RE.search(text))):
        return "", "", None
    plan = pl.read_plan((row or open_plans[0])["path"])
    if asks_plan(text):
        from silica.kernel.learn import mastery

        # the calendar of what is left: a step the ledger says passed is done
        passed = mastery.steps_passed(plan) if plan.get("steps") else []
        rows = pl.day_by_day(plan, passed=passed)
        if not rows:
            return "", "", None
        table = pl.day_by_day_table(rows, plan, passed)
        facts = _heavy_steps(plan)
        gaps = pl.lecture_gaps(plan) if plan.get("scope") == "steps" else None
        if gaps and gaps[0]:
            facts += f" Topics of the syllabus no lecture deck mentions, to study from the book or the guide: {', '.join(gaps[0])}."
        return (f"[Memorize tracks the plan {plan['name']}, by {plan['by']}. Silica appends its day-by-day calendar, "
                f"computed from the plan's steps in the syllabus's order and its date, under your answer; do not "
                f"write a calendar or a table of days yourself. {facts} Write a short introduction and advice for "
                f"this plan built on these facts and the steps below, naming the steps and topics; no study tip "
                f"that would fit any plan:\n{table}]",
                f"Calendar {COMPUTED} from the plan {plan['name']} (by {plan['by']}):\n\n{table}", None)
    found = pl.lecture_gaps(plan) if plan.get("scope") == "steps" else None
    if not found:
        return "", "", None
    missing, decks = found
    return (f"[Memorize's plan {plan['name']} reads its syllabus, {plan['target']}, against the text of the {decks} "
            f"lecture decks. Topics of the syllabus that no deck mentions: {', '.join(missing) or 'none'}. Use this "
            f"list for what the slides do not cover; a topic named another way in the slides can still be there.]",
            "", None)


def _heavy_steps(plan: dict) -> str:
    """The advice stands on the plan's facts, not on study tips any plan
    would get: its heaviest steps, by the reference's hours when it gives
    them (ADR-0131), else by their notes."""
    from silica.kernel.learn import plans as pl

    heavy = sorted((s for s in plan.get("steps") or [] if s.get("notes")),
                   key=lambda s: (-(s.get("hours") or 0), -len(s["notes"])))[:3]
    if not heavy:
        return ""
    return "The heaviest steps: " + "; ".join(
        f"{pl._step_label(s)} ({s['hours']:g} {s.get('unit') or 'weight'})" if s.get("hours")
        else f"{pl._step_label(s)} ({len(s['notes'])} notes)" for s in heavy) + "."


def plan_draft(read: dict) -> dict:
    """The plan a request reads as (plans.read_sentence), composed and read
    back, not written: {body, plan, resolved}. `body` is what the line under
    the answer posts to /learn/plan, the Memorize form's own save, so the
    click writes the plan this turn showed. The name moves to "<name> 2" when
    Plans/<name>.md exists, because the write gate refuses a path that does."""
    from silica.kernel.learn import plans as pl
    from silica.kernel.publish import PLANS_DIR
    from silica.kernel.base import frontmatter

    base = read.get("name") or ""
    if not base or asks_plan(base):
        # read_sentence named it after the request ("Piano di studio", live
        # run 2026-11-03), a name the reader cannot edit here and plan_context
        # passes over: the material names it instead, as in read_sentence
        base = pl.name_of(read["scope"], read["target"]) or "Plan"
    name, n = base, 2
    while (pl._vault() / PLANS_DIR / f"{name}.md").exists():
        name, n = f"{base} {n}", n + 1
    body = {"name": name, "scope": read["scope"], "target": read["target"],
            "restrict": read.get("restrict") or "", "by": read.get("by") or None, "templates": True}
    if read.get("objectives"):
        body["objectives"] = read["objectives"]
    d = pl.compose_plan(name, body["scope"], body["target"], by=body["by"], restrict=body["restrict"],
                        objectives=body.get("objectives"), folder=PLANS_DIR, templates=True)
    plan = pl.read_plan(d["path"], frontmatter.dump(d["props"], d["body"]))
    return {"body": body, "plan": plan, "resolved": d["resolved"]}


def draft_context(draft: dict) -> tuple[str, str]:
    """(the block for the model, the plan appended under its answer) for a
    plan computed and not saved: its steps in the reference's order, or its
    parts when it has none, and the day-by-day calendar when it has a date."""
    from silica.kernel.learn import plans as pl

    plan, res = draft["plan"], draft["resolved"]
    material = plan["target"] + (f" under {plan['restrict']}" if plan.get("restrict") else "")
    when = f"by {plan['by']}" if plan.get("by") else "with no date"
    steps = plan.get("steps") or []
    if steps:
        listing = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(steps, 1))
    else:
        # three notes named a part at most, as the calendar names a day's
        parts = [f"- {n['node']}: " + ", ".join(f"[[{Path(p).stem}]]" for p in n["leaves"][:3])
                 + (f" and {len(n['leaves']) - 3} more" if len(n["leaves"]) > 3 else "")
                 for n in res["tree"] if not n.get("gap") and n.get("leaves")]
        listing = "\n".join(parts[:TREE_SHOWN] + ([f"- and {len(parts) - TREE_SHOWN} more"] if len(parts) > TREE_SHOWN else []))
    rows = pl.day_by_day(plan) if steps else None
    table = pl.day_by_day_table(rows, plan) if rows else ""
    facts = f"{len(res['paths'])} notes" + (f" in {len(steps)} steps." if steps else ".")
    b = pl.budget(plan) if rows else None
    if b:
        # the count the model otherwise makes up: "6 passi in 6 giornate" for 37 days (2026-11-03)
        facts += f" {b['days']} days to the date, {b['study_days']} of them for the steps, then review."
    facts += (" " + _heavy_steps(plan)) if steps else ""
    if res.get("gaps"):
        facts += f" Not in the vault: {', '.join(res['gaps'])}."
    # a step met on some of its words is a guess the form asks the reader to
    # confirm; here the summary says so: the model called "Gradient boosting"
    # missing while the plan gave it Discesa del gradiente (2026-11-03)
    unsure = "; ".join(f"{n['node']} as {', '.join(Path(p).stem for p in n['leaves'][:3])}"
                       for n in res["tree"] if not n.get("gap") and n.get("doubt"))
    check = f"Matched on some of their words, not by name: {unsure}." if unsure else ""
    dated = "" if plan.get("by") else " It has no date: say in one line that a date paces it day by day, and ask for it."
    block = (f"[Memorize computed a study plan for this request, not saved yet: {plan['name']}, from {material}, "
             f"{when}. {facts}{f' {check} The reader should check them.' if check else ''}{dated} Silica appends the plan{' and its day-by-day calendar' if table else ''} under "
             f"your answer and shows the reader a line that saves it in Memorize; do not write the steps, a calendar "
             f"or a table of days yourself, and do not create, edit or save any note. Write a summary of this plan "
             f"in at most four sentences, built on these facts and the steps below: name its heaviest steps, what "
             f"the vault does not hold and the steps to check, and do not list the steps one by one (they are under your answer); no study "
             f"tip that would fit any plan. End by asking whether to keep it in Memorize. Write in the reader's "
             f"language.\n{listing}]")
    # no "not saved" here: the answer is stored, and the click saves the plan after it
    appendix = (f"Plan {COMPUTED} from {material}, {when}:\n\n{listing}"
                + (f"\n\n{check} Check them in Memorize with Edit plan." if check else "")
                + (f"\n\n{table}" if table else ""))
    return block, appendix


def ask_block() -> str:
    """The turn that asks for a study plan and names no material the vault
    holds: the model asks for it, naming the vault's largest folders so the
    question can be answered by picking one, and writes no plan of its own."""
    from silica.kernel.learn import plans as pl
    from silica.kernel.publish import never

    try:
        # a folder of Silica's own (Plans, sources, the inbox) is never material
        folders = sorted(((c, f) for f, c in pl.folders_of().items() if f.count("/") <= 2 and not never(f + "_")),
                         reverse=True)[:FOLDERS_NAMED]
    except Exception:
        # the examples are an extra: a vault that does not scan still gets the question
        folders = []
    named = f" The vault's largest folders: {', '.join(f for _c, f in folders)}." if folders else ""
    return ("[The reader asks for a study plan and names no material Silica found in the vault. Do not write a plan, "
            "a schedule or a list of weeks, and do not create or edit any note. Ask in one short question which "
            f"folder, note or course syllabus of the vault the plan is for, and the exam date.{named} Ask in the "
            "reader's language.]")


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
# A miss at confidence 90 (ADR-0135 point 9): sure and wrong is the strongest
# signal one answer gives (LearnHarness's confidence doctrine). It ships logged
# and never shown; it acts once 20 sessions show at least half of its fires
# followed by another miss on the same note (`confident_readout`).
CONFIDENT_CONF = 90
CONFIDENT_REASON = "a miss at confidence 90"
CONFIDENT_SHADOW = True
# The grader's structural class (ADR-0135 point 6), read behind the same gate
# as the picker's (pick.MISS_CLASS_READ).
STRUCTURAL_REASON = "a wrong idea held"


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
    from silica.kernel.base.home import index_dir

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
    from silica.kernel.learn.quiz import key

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
    last = evs[-1] if evs else {}
    if not last.get("correct") and last.get("miss") == "structural":
        from silica.kernel.learn import pick

        if pick.MISS_CLASS_READ:
            return STRUCTURAL_REASON
    if evs and not last.get("correct") and last.get("conf") == CONFIDENT_CONF:
        return CONFIDENT_REASON
    return ""


def shadowed(why: str) -> bool:
    """A reason that is logged and never shown, whatever the mode."""
    return why == CONFIDENT_REASON and CONFIDENT_SHADOW


def confident_readout(entries: list[dict], records: list[dict] | None = None) -> dict:
    """The shadow reason's record against the ledger (ADR-0135 point 9): its
    fires, how many the next answer on the same note missed again, and the
    Learn sessions it was seen in. It acts at 20 sessions and half followed."""
    from silica.kernel.learn.quiz import key

    from datetime import datetime

    def at(ts) -> float:
        try:
            return datetime.fromisoformat(str(ts)).timestamp()
        except ValueError:
            return float("inf")

    records = _records() if records is None else records
    fires = [r for r in records if r.get("why") == CONFIDENT_REASON]
    ordered = sorted(entries, key=lambda e: at(e.get("ts")))
    followed = 0
    for r in fires:
        # a record's time is to the second and the miss that fired it is a
        # fraction of that second earlier: the next answer starts past it
        since = at(r.get("ts")) + 1
        nxt = next((e for e in ordered if key(str(e.get("path") or "")) == key(str(r.get("note") or ""))
                    and at(e.get("ts")) >= since), None)
        followed += bool(nxt and not nxt.get("correct"))
    sessions = {r.get("session") for r in fires if r.get("session")}
    return {"fires": len(fires), "followed_by_miss": followed, "sessions": len(sessions),
            "three_miss_fires": sum(1 for r in records if r.get("why") == f"{ESCALATE_MISSES} misses in a row")}


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
    from silica.kernel.learn import learner

    try:
        prereqs = learner.prerequisites_of(path)
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

    from silica.model.ask import ask_once
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
    return (ask_once(prompt, max_tokens=AID_MAX_TOKENS, temperature=0.2).text or "").strip()


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
    from silica.kernel.learn.quiz import key

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
