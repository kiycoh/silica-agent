# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Learn session (learn-suite §6.1, ADR-0041, ADR-0135): the one engine,
whichever surface runs it.

Answer keys stay here, keyed by the session id; the ledger line is written on
`rate`, once per item; the receipt is rebuilt from the ledger when the live
session is gone. A session knows its origin (a plan, a note, or the vault), a
note session is sized on the note's key points, and every item carries the
objective it is evidence for.

Nothing here speaks HTTP: a turned-away action raises `Refused`, which a
surface translates. `draw` is the start both ways in share: `start` keeps what
it drew as the live session, and `silica_learn_items`, which holds no session,
hands the items to the agent.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from silica.kernel.base import note_index
from silica.kernel.base.notekey import ledger_key
from silica.kernel.learn.reasons import RATING_NAME, outcome, ts

logger = logging.getLogger(__name__)


class Refused(Exception):
    """A session action turned away. The text is what the reader is told; `why`
    is BUSY, MISSING, INVALID or NO_MODEL, for a surface to say in its own terms."""

    def __init__(self, why: str, text: str):
        super().__init__(text)
        self.why = why


# Who is turned away and for what: another session holds the slot, the thing
# named is not there, the request cannot be done, the model gave nothing.
BUSY, MISSING, INVALID, NO_MODEL = "busy", "missing", "invalid", "no_model"

# The reason a suggestion is on the list, in the reader's words (refinement §19).
REASON = {"due": "Suggested for review", "unexplored": "Not practised yet", "stale": "Changed since last practice",
          "contrast": "Compare these notes", "contradiction": "Check a disagreement", "revisit": "Queued by you",
          "frontier": "Plan frontier", "note": "Practice this note"}
MILESTONES = (7.0, 30.0)
# The default objectives of a session no plan owns: definitions as recall,
# everything else as explain, so a free session still says what it looks for.
FREE_OBJECTIVES = ["recall", "explain"]

# One live session, keyed by id; a second tab replaces it after `close`. It is
# held in memory and in a file beside the answer ledger, written whole after
# every change (ADR-0135 point 1): on the hosted pilot a deploy restarts the
# tenant, and a session held only in memory died with it.
_LIVE: dict | None = None
_LAST: dict | None = None
LIVE_FILE = "learn-live.json"
# JSON has no int keys and no sets: these fields are converted on the way out
# and back, every other value is already plain JSON.
_INT_KEYED = ("verdicts", "recorded", "teach", "aids")
_SETS = ("skipped", "escalated", "dropped", "retested")
_SAVE_LOCK = threading.Lock()
_ITEMS_LOCK = threading.Lock()   # a retest is appended from a route and from its generator's thread
# Asked again before the end (ADR-0135 point 4): one a note, a quarter of
# pick.SESSION_MAX.
RETEST_MAX = 6


def _reset() -> None:
    """What a restart does to memory; the file stays, as it does on disk."""
    global _LIVE, _LAST
    _LIVE = _LAST = None


def _live_file(vault: str | None = None) -> Path:
    from silica.config import CONFIG
    from silica.kernel.base import home

    return home.index_dir_for(vault or CONFIG.vault_path) / LIVE_FILE


def _save() -> None:
    """Write the live session whole, in the vault it was started in."""
    s = _LIVE
    if s is None:
        return
    from silica.kernel.base.durable import atomic_write_bytes

    doc = dict(s)
    for k in _INT_KEYED:
        if k in doc:
            doc[k] = {str(i): v for i, v in doc[k].items()}
    for k in _SETS:
        if k in doc:
            doc[k] = sorted(doc[k])
    path = _live_file(s.get("vault"))
    try:
        with _SAVE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(path, json.dumps(doc, ensure_ascii=False).encode("utf-8"), mode=0o600)
    except OSError:
        # Tolerated: the session goes on in memory, and the answer in hand is
        # already in the ledger or still on the page. A failed write costs only
        # the next restart's copy of the session.
        logger.warning("learn: the live session was not saved", exc_info=True)


def _metered(s: dict):
    """The session's own count of its model calls (ADR-0135 point 8)."""
    from silica.model import llm

    return llm.meter_scope(s.setdefault("meter", llm.new_meter()))


def _meter_shown(s: dict) -> dict | None:
    """What the session cost, or None where nothing is metered: a laptop
    without SILICA_TOKEN_METER counts nothing, and a zero would be false."""
    from silica.model import llm

    return dict(s.get("meter") or llm.new_meter()) if llm._METER_ON else None


def _drop_file(s: dict) -> None:
    _live_file(s.get("vault")).unlink(missing_ok=True)


def load_live() -> None:
    """After a restart, read the live session back from its file. A router
    dependency, so every Learn route sees the session memory lost."""
    global _LIVE
    if _LIVE is not None:
        return
    try:
        doc = json.loads(_live_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        # Written whole by rename, so a file that does not parse was damaged on
        # disk or edited by hand: Learn starts without the session rather than
        # failing every route on it, and the next start overwrites the file.
        logger.warning("learn: the live session's file does not read", exc_info=True)
        return
    for k in _INT_KEYED:
        if k in doc:
            doc[k] = {int(i): v for i, v in doc[k].items()}
    for k in _SETS:
        if k in doc:
            doc[k] = set(doc[k])
    _LIVE = doc


def now() -> float:
    return datetime.now(timezone.utc).timestamp()


def iso_day(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None


def plan_scopes(plan_list: list[dict]) -> dict[str, dict]:
    """{plan path: {name, paths}} for the active plans, resolved once per
    read so every suggestion can say `also in <plan>` (refinement §6)."""
    from silica.kernel.learn import plans

    out = {}
    for p in plan_list:
        if p["status"] == "completed":
            continue
        try:
            out[p["path"]] = {"name": p["name"], "paths": set(plans.scope_of(p)["paths"]), "objectives": p["objectives"]}
        except Exception as exc:
            logger.debug("learn: scope of %s not resolved (%s)", p["path"], exc)
    return out


def objectives_for(path: str, scopes: dict[str, dict]) -> list[str]:
    """What a free session on a note looks for: the union of the objectives
    of the plans that include it, else the free defaults."""
    from silica.kernel.learn import objectives as obj

    got = [o for v in scopes.values() if path in v["paths"] for o in v["objectives"]]
    return obj.normalize(got) or list(FREE_OBJECTIVES)


def _asked(s: dict) -> list[dict]:
    """The session's items without those a placement's walk dropped: a
    step placed on its deepest note never asks its second question."""
    dropped = s.get("dropped") or set()
    return [it for it in s["items"] if it["i"] not in dropped]


def live() -> dict | None:
    return _LIVE


def is_open(plan_path: str) -> bool:
    """Whether the live session is this plan's."""
    return bool(_LIVE and _LIVE["plan"] == plan_path)


def live_info() -> dict | None:
    if not _LIVE:
        return None
    s = _LIVE
    done = set(s["recorded"]) | set(s["skipped"])
    asked = _asked(s)
    cur = next((it["i"] for it in asked if it["i"] not in done), asked[-1]["i"] if asked else 0)
    return {"id": s["id"], "plan": s["plan"], "note": s.get("note", ""), "origin": s["origin"], "n": len(asked),
            "cur": cur, "recorded": len(s["recorded"]), "skipped": len(s["skipped"]), "started": s["started"],
            "title": (s["items"][cur - 1]["title"] if 0 < cur <= len(s["items"]) else "")}


def notes_under(prefix: str) -> list[str]:
    """The vault's notes under a folder prefix, a session's scope as a surface
    names it. A note's own path is a prefix of itself."""
    from silica.config import CONFIG

    low = prefix.casefold()
    return [p for p in note_index.meta(Path(CONFIG.vault_path)) if p.casefold().startswith(low)]


def draw(n: int, *, plan: dict | None = None, note: str = "", scope: Any = None, step: Any = None, sized: bool = True,
         points=None, teach: bool = False, placement: bool = False, paced: bool = True, within=None,
         meter: dict | None = None) -> dict:
    """What a session asks: {picked, built, n, objectives, origin, revisit}, no
    live session touched. `scope` is a list of paths or a folder prefix.

    `paced` False is the agent's way in (`silica_learn_items`): a dated plan's
    deadline and a placement's later notes stay out of its pick. `within` is the
    workspace an agent's turn studies. `meter` counts the build's model calls;
    None leaves them to whatever scope the caller is in, which a turn's are."""
    from silica.kernel.learn import items, pick, plans
    from silica.model import llm

    frontier = revisit = None
    objectives = list(FREE_OBJECTIVES)
    origin = {"kind": "vault", "path": "", "label": "Suggested practice"}
    if plan is not None:
        scoped = plans.scope_of(plan)
        scope, frontier, revisit = scoped["paths"], scoped["frontier"], scoped["revisit"]
        objectives = plan["objectives"]
        origin = {"kind": "plan", "path": plan["path"], "label": plan["name"]}
    # the practice after a step's reading (ADR-0132, point 5): that step's
    # notes first and only, one question a note up to the cap, notes in turn
    stepped = plan is not None and not note and str(step).isdigit()
    if stepped and plan is not None:
        from silica.kernel.learn import reading
        from silica.kernel.learn.reference import step_label

        try:
            in_step = reading.step_notes(plan, int(str(step)))
        except ValueError as exc:
            raise Refused(INVALID, str(exc))
        scope = [p for _t, p in in_step]
        frontier, revisit = list(scope), None
        if not sized:
            n = reading.session_size(len(scope))
        origin = {**origin, "label": f"{plan['name']} · {step_label(plan['steps'][int(str(step))])}"}
    teach = teach and bool(note)
    # a placement (ADR-0135 point 3): asked of a plan before it starts, on offer
    placing = plan is not None and not note and not stepped and placement
    if placing and plan is not None:
        from silica.kernel.learn import placement as placing_mod

        rows = placing_mod.candidates(plan)
        if not rows:
            raise Refused(INVALID, "this plan has no step with a note to ask about")
        picked: dict[str, Any] = {"items": rows, "backfill": [], "amnesty": None, "short": None}
        n = len(rows)
        origin = {**origin, "label": f"{plan['name']} · placement"}
    elif note:
        picked = _note_rows(note, points, objectives if plan else None)
        objectives = picked["objectives"]
        if plan is None:
            origin = {"kind": "note", "path": note, "label": items.title_of(note)}
        # one explanation covers the note, so a teach-back is one item
        n = 1 if teach else max(1, sum(len(r.get("asks") or []) or 1 for r in picked["items"]))
    else:
        if isinstance(scope, str) and scope:
            scope = notes_under(scope)
        if within is not None:
            scope = [p for p in (scope if scope is not None else within) if p in within]
        later: set[str] = set()
        if plan is not None and paced:
            later = _placed_notes(plan)
            frontier = [p for p in frontier or [] if p not in later] + [p for p in frontier or [] if p in later]
        picked = pick.session(n=n, scope=scope, frontier=frontier, revisit=revisit, later=later,
                              deadline=plan.get("by") if plan and paced else None)
        if plan is not None:
            plans.assign_objectives(plan, picked["items"])
    with llm.meter_scope(meter) if meter is not None else nullcontext():
        built = items.build_session(picked["items"], picked["backfill"], n=n)
    if teach:
        # build_session sizes a note's items on its asks, not on n
        built = built[:1]
    if stepped:
        built = pick.alternate_notes(built)
    if placing:
        tags = {r["path"]: {"step": r["step"], "rank": r["rank"]} for r in picked["items"]}
        for it in built:
            if it["path"] in tags:
                it["placement"] = tags[it["path"]]
    return {"picked": picked, "built": built, "n": n, "objectives": objectives, "origin": origin, "revisit": revisit,
            "teach": teach}


def start(n=None, plan: str = "", note: str = "", scope: Any = None, step: Any = None, points=None,
          teach: bool = False, placement: bool = False) -> dict:
    """The one live session: refused while another is live. `n` as the surface
    sent it: unset sizes a step's practice on its notes."""
    global _LIVE
    from silica.config import CONFIG
    from silica.kernel.learn import pick, plans
    from silica.model import llm

    if _LIVE is not None:
        raise Refused(BUSY, "a session is live; close it first")
    size = max(1, min(pick.SESSION_MAX, int(n or pick.DEFAULT_N)))
    found = None
    if plan:
        found = plans.find_plan(plan)
        if found is None:
            raise Refused(MISSING, f"no plan at {plan!r}")
    meter = llm.new_meter()
    d = draw(size, plan=found, note=note, scope=scope, step=step, sized=bool(n), points=points, teach=teach,
             placement=placement, meter=meter)
    built, picked, size = d["built"], d["picked"], d["n"]
    sid = uuid.uuid4().hex
    _LIVE = {"id": sid, "plan": found["path"] if found else "", "note": note, "origin": d["origin"],
             "objectives": d["objectives"], "reach": found["reach_level"] if found else 4, "n": size, "items": built,
             "verdicts": {}, "recorded": {}, "skipped": set(), "teach": {}, "started": now(), "before": {},
             "revisit": d["revisit"] or [], "mode": "teach" if d["teach"] else "questions",
             "vault": str(CONFIG.vault_path or ""), "meter": meter}
    _save()
    short = None
    if len(built) < size:
        short = picked["short"] or (f"{len(built)} of {size}: the other picked notes carry no line to ask about "
                                    f"(a footnoted line, a Relations bullet, or a prose line of 40+ characters)")
    return _payload(_LIVE, amnesty=picked["amnesty"], short=short)


def _note_rows(note: str, only=None, objectives: list[str] | None = None) -> dict:
    """The one row of a note session, bypassing the pools: a note the reader
    asks to practise is practised whether or not the schedule would pick
    it. Its `asks` size the session on the key points (D28)."""
    from silica.kernel.learn import items, plans
    from silica.kernel.learn import learner

    rows = learner.view()
    r = rows.get(ledger_key(note))
    if r is None:
        raise Refused(MISSING, f"no note at {note!r}")
    text = items.read_note(note)
    points = items.key_points(note, text)
    if objectives is None:
        objectives = objectives_for(note, plan_scopes(plans.list_plans()))
    only_i = [int(x) for x in (only or []) if str(x).isdigit()] or None
    asks = items.ask_plan(points, objectives, len(items.relations_of(note, text)), only=only_i)
    row = {**r, "pool": "note", "kind": "explain", "level": 3, "asks": asks, "objective": objectives[0],
           "stale": False, "folder": note.rsplit("/", 1)[0] if "/" in note else "", "why": "note"}
    return {"items": [row] if (asks or items.relations_of(note, text) or points) else [], "backfill": [],
            "amnesty": None, "short": None if asks else f"{note} has no grounded line to ask about", "due": 0, "unexplored": 0,
            "objectives": objectives}


def _payload(s: dict, amnesty=None, short=None) -> dict:
    from silica.kernel.learn import items, objectives as obj

    return {"id": s["id"], "plan": s["plan"], "note": s.get("note", ""), "origin": s["origin"], "n": s["n"],
            "objectives": s["objectives"], "objective_labels": [obj.LABEL[o] for o in s["objectives"]],
            # numbered on the whole list, then filtered: `i` is the item's place in
            # s["items"], which every route resolves it by
            "items": [c for c in items.cue_payload(s["items"]) if c["i"] not in (s.get("dropped") or set())],
            "reasons": [REASON.get("stale" if it.get("stale") else it["pool"], it["pool"]) for it in _asked(s)],
            "minutes": items.minutes(len(_asked(s))), "mode": s.get("mode", "questions")}


def reload(sid: str) -> dict:
    """The live session for a tab that reloaded (refinement, the reload
    trap): cues, what was recorded or skipped, and the verdicts awaiting a
    rating. The unsubmitted answer is the one thing a reload loses."""
    s = _live(sid)
    out = _payload(s)
    out["done"] = sorted(s["recorded"])
    out["skipped"] = sorted(s["skipped"])
    out["verdicts"] = {str(i): _verdict_payload(v, s["items"][i - 1]) for i, v in s["verdicts"].items() if i not in s["recorded"]}
    out["results"] = {str(i): r["result"] for i, r in s["recorded"].items()}
    # a teach-back in progress: the reader's words and the student's questions
    out["teach"] = {str(i): {"turn": t["turn"], "transcript": t["transcript"]} for i, t in s["teach"].items()}
    return out


def _live(sid: str) -> dict:
    if _LIVE is None or _LIVE["id"] != sid:
        raise Refused(MISSING, "no live session with that id")
    return _LIVE


def _item(s: dict, i: Any) -> dict:
    try:
        return s["items"][int(i) - 1]
    except (IndexError, ValueError, TypeError):
        raise Refused(MISSING, f"no item {i}")


def _verdict_payload(v: dict, it: dict) -> dict:
    out = {k: v.get(k) for k in ("grade", "rating", "correct", "feedback_line", "rubric_notes", "grader",
                                 "paraphrase", "misconception", "note_defect", "skip", "needs_self_grade", "told")}
    out.update({"claim": it.get("claim", ""), "answer": it.get("answer", ""), "criteria": it.get("criteria", ""),
                "span": {**(it.get("source") or {}), "text": it.get("span", "")}, "path": it["path"],
                "title": it.get("title", ""), "kind": it["kind"], "level": it["level"],
                "objective": it.get("objective"), "point": it.get("point")})
    return out


def answer(sid: str, i: Any, *, conf=None, hints=0, told=False, production="", modality="", self_graded=False,
           force_model=False) -> dict:
    from silica.kernel.learn import grade

    s = _live(sid)
    it = _item(s, i)
    conf = conf if conf in (90, 70, 50, 25) else None
    hints = max(0, int(hints or 0))
    told = bool(told)
    production = str(production or "")
    # Anything but "voice" is typed: the page sends voice once a dictation
    # landed in this answer, however much was typed around it (ADR-0049).
    modality = "voice" if modality == "voice" else "typed"
    if (it.get("grader") == "self" or self_graded) and not told and not force_model:
        # A mechanical item, or the 20 s fallback (§5.1): the evidence shows
        # and the reader rates; a late model verdict cannot replace a rating.
        v = grade.base(it, "self")
        v.update(grade=None, correct=None, rating=None, feedback_line="", rubric_notes="",
                 needs_self_grade=True, skip=False)
    else:
        with _metered(s):
            v = grade.grade(it, production=production, conf=conf, hints=hints, told=told, spoken=modality == "voice")
    v.update(conf=conf, hints=hints, told=told, production=production, modality=modality)
    s["verdicts"][it["i"]] = v
    s["skipped"].discard(it["i"])
    _save()
    return _verdict_payload(v, it)


def skip(sid: str, i: Any) -> dict:
    """Skipping is not a wrong answer (refinement §22): the point stays open,
    nothing is recorded, the receipt says skipped."""
    s = _live(sid)
    it = _item(s, i)
    if it["i"] in s["recorded"]:
        raise Refused(INVALID, "already recorded")
    s["skipped"].add(it["i"])
    s["verdicts"].pop(it["i"], None)
    _save()
    return {"skipped": sorted(s["skipped"])}


def _momentum(before: dict | None, after: dict | None, first_success: bool) -> str | None:
    """One line on a milestone only (D14 step 5): first success, stability
    crossing 7 or 30 days, or a doubling. Never on Hard or Again."""
    if not after or after.get("S") is None:
        return None
    s1 = float(after["S"])
    s0 = float(before["S"]) if before and before.get("S") is not None else None
    if first_success and s0 is None:
        return f"First hold on this note: estimated {f'~{round(s1)} days' if s1 >= 0.5 else 'under a day'}."
    if s0 is None:
        return None
    crossed = [m for m in MILESTONES if s0 < m <= s1]
    if crossed:
        return f"Estimated hold ~{round(s1)} days, was ~{round(s0)}; past {int(crossed[-1])} days."
    # a doubling worth a line is one the reader can see: from 0.1 to 0.3 days
    # read "Estimated hold ~0 days, was ~0." (2026-10-23)
    if s1 >= 2 * s0 and round(s1) > round(s0):
        return f"Estimated hold ~{round(s1)} days, was ~{round(s0)}."
    return None


# One rating at a time: the route is sync, so FastAPI runs two posts on two
# threads, and both passed the `recorded` check before either wrote (two
# ledger lines for one answer, 2026-10-16, Enter pressed while the first post
# took 1.5 s to read the view twice). Ratings are one per answer: a lock costs
# nothing here.
_RATE_LOCK = threading.Lock()


def rate(sid: str, i: Any, rating: Any) -> dict:
    with _RATE_LOCK:
        return _rate(sid, i, rating)


def ledger_entry(s: dict, it: dict, v: dict, correct: bool, rating: int, grader: str, proposed: int | None) -> dict:
    """The line `quiz.record` writes for one rated item. `quiz.py` keeps only
    the fields it names, so every field here but NOT_IN_LEDGER is one it
    knows (tests/test_learn_session.py holds the two together)."""
    from silica.kernel.learn import objectives as obj
    from silica.kernel.learn.items import AXIS

    entry = {
        "path": it["path"], "correct": correct, "rating": rating, "kind": it["kind"], "level": it["level"],
        "q": it.get("q", ""), "anchor": ("#" + it["anchor"]) if it.get("anchor") else "",
        "concepts": [], "conf": v.get("conf"), "source": it.get("source") or None, "grader": grader,
        "hints": int(v.get("hints") or 0), "told": bool(v.get("told")), "session": s["id"], "axis": AXIS.get(it["kind"], ""),
        "objective": it.get("objective") or obj.of_kind(it["kind"]),
    }
    if isinstance(it.get("point"), dict) and it["point"].get("i"):
        entry["point"] = int(it["point"]["i"])
    if proposed is not None and proposed != rating:
        entry["grader_rating"] = proposed
    if v.get("paraphrase") is not None:
        entry["paraphrase"] = v["paraphrase"]
    if v.get("misconception"):
        entry["misconception"] = v["misconception"]
    if v.get("miss") and not correct:
        entry["miss"] = v["miss"]
    if v.get("modality") == "voice":
        entry["modality"] = "voice"
    if s["plan"]:
        entry["plan"] = s["plan"]
    if it.get("endpoints"):
        entry["endpoints"] = it["endpoints"]
    if it.get("placement"):
        entry.update(origin="placement", step=it["placement"]["step"], rank=it["placement"]["rank"])
    if it.get("retest"):
        entry["retest"] = True
    if entry["hints"] and it.get("rungs"):
        entry["rungs"] = it["rungs"][:entry["hints"]]   # which help, not only how much (ADR-0135 point 5)
    return entry


# `axis` rides in the live session's copy of the entry and no further: the
# ledger never took it and nothing reads it, and the file a restart reads back
# keeps its shape.
NOT_IN_LEDGER = frozenset({"axis"})


def _rate(sid: str, i: Any, raw: Any) -> dict:
    from silica.kernel.learn import learner, quiz

    s = _live(sid)
    it = _item(s, i)
    if it["i"] in s["recorded"]:
        rec = s["recorded"][it["i"]]
        return {**rec["result"], "duplicate": True}
    v = s["verdicts"].get(it["i"])
    if v is None:
        raise Refused(INVALID, "answer first")
    if v.get("skip"):
        raise Refused(INVALID, "the material invalidated this item; nothing is recorded")
    try:
        rating = int(raw)
    except (TypeError, ValueError):
        raise Refused(INVALID, "rating 1..4 required")
    rating = max(1, min(4, rating))
    grader = v.get("grader", "self")
    proposed = v.get("rating")
    if grader == "self" or v.get("needs_self_grade"):
        from silica.kernel.learn import grade

        sg = grade.self_grade(it, rating=rating, hints=v.get("hints", 0), told=v.get("told", False))
        correct, rating, grader, proposed = sg["correct"], sg["rating"], "self", None
    else:
        correct = bool(v.get("correct"))
    key = ledger_key(it["path"])
    before = learner.view().get(key)
    entry = ledger_entry(s, it, v, correct, rating, grader, proposed)
    written = quiz.record([entry])
    after = learner.view().get(key)
    first = bool(before and before.get("attempts") == 0 and correct)
    result = {
        "recorded": written == 1, "rating": rating, "rating_name": RATING_NAME[rating], "correct": correct,
        "grader": grader, "S_before": before.get("S") if before else None, "S_after": after.get("S") if after else None,
        "next_due": iso_day(after.get("due")) if after else None,
        "momentum": _momentum(before, after, first) if rating >= 3 else None,
        "objective": entry["objective"], "unassisted": bool(correct and not entry["hints"] and not entry["told"]),
        "escalate": None if correct else _escalate(s, it),
    }
    drop = _walk(s, it, entry)
    if drop:
        result["drop"] = drop
    added = _retest(s, it, entry)
    if added:
        result["added"] = [added]
    s["recorded"][it["i"]] = {"entry": entry, "result": result, "ts": now()}
    s["skipped"].discard(it["i"])
    _save()
    return {**result, "duplicate": False}


def _walk(s: dict, it: dict, entry: dict) -> list[int]:
    """A placement's walk (ADR-0135 point 3): the deepest note met without
    help places its step, and the step's second question is dropped."""
    from silica.kernel.learn import placement

    pl = it.get("placement")
    if not pl or pl["rank"] != 0 or not placement.met_unaided(entry):
        return []
    drop = [x["i"] for x in s["items"] if (x.get("placement") or {}).get("step") == pl["step"]
            and x["i"] != it["i"] and x["i"] not in s["recorded"]]
    s.setdefault("dropped", set()).update(drop)
    return drop


def _retest(s: dict, it: dict, entry: dict) -> dict | None:
    """A miss or a "Just tell me", asked again before the session ends in
    another kind (ADR-0135 point 4). A model kind's miss gets a mechanical
    item, ready now with no call; a mechanical kind's miss, or a note with no
    mechanical item, gets one generated in the background, appended when it
    lands. Returns the cue of an item appended now."""
    from silica.kernel.learn import items
    from silica.kernel.learn.items import LEVEL_OF, MECHANICAL
    from silica.kernel.learn import learner

    if entry["correct"] or it.get("retest") or it.get("placement") or s.get("mode") == "teach":
        return None
    done = s.setdefault("retested", set())
    if it["path"] in done or len(done) >= RETEST_MAX:
        return None
    done.add(it["path"])
    row = {**(learner.view().get(ledger_key(it["path"])) or {}), "path": it["path"], "pool": "retest",
           "objective": it.get("objective")}
    if it["kind"] not in MECHANICAL:
        made = items.build_session([{**row, "kind": "cloze", "level": LEVEL_OF["cloze"]}], [], n=1)
        if made:
            return _append_retest(s, made[0], it["kind"])
    kind = "explain" if it["kind"] in ("cloze", "recall", "predict") else "recall"
    threading.Thread(target=_generate_retest, args=(s, {**row, "kind": kind, "level": LEVEL_OF[kind]}, it["kind"]),
                     daemon=True, name="learn-retest").start()
    return None


def _append_retest(s: dict, new: dict, missed_kind: str) -> dict:
    from silica.kernel.learn import items

    with _ITEMS_LOCK:
        s["items"].append({**new, "retest": True, "missed_kind": missed_kind, "pool": "retest",
                           "i": len(s["items"]) + 1})
        return items.cue_payload(s["items"])[-1]


def _generate_retest(s: dict, row: dict, missed_kind: str) -> None:
    from silica.model import llm
    from silica.kernel.learn import items

    try:
        with llm.meter_scope(s.get("meter")):
            made = items.build_session([row], [], n=1)
    except Exception:
        # Tolerated: a retest is an extra, and this thread has no caller to
        # raise to; the session goes on without it.
        logger.warning("learn: a retest was not generated", exc_info=True)
        return
    # the fallback can land on the kind just missed: that is no retest
    if made and made[0]["kind"] != missed_kind and _LIVE is s:
        _append_retest(s, made[0], missed_kind)
        _save()


def _placed_notes(plan: dict) -> set[str]:
    """The notes of the steps a placement placed, which a plan session
    takes after every other first attempt."""
    from silica.kernel.learn import placement
    from silica.kernel.learn import quiz

    entries = quiz.entries()
    if not any(e.get("origin") == placement.ORIGIN for e in entries):
        return set()   # never placed: no step's notes to resolve
    steps = placement.step_paths(plan)
    return {p for s in placement.placed_steps(plan, entries, steps) for p in steps[s]}


def _escalate(s: dict, it: dict) -> dict | None:
    """The escalate rung after a miss (learn.offer): once a note a session,
    logged, and in shadow mode only logged."""
    from silica.kernel.learn import offer

    if offer.mode() == "off" or it["path"] in s.setdefault("escalated", set()):
        return None
    try:
        why, shown = offer.decide_aid(it["path"], s["id"])
        if why and not offer.shadowed(why):
            s["escalated"].add(it["path"])   # a shadow fire never takes the session's one aid on the note
        if not shown:
            return None
        prereq = offer.weakest_prerequisite(it["path"])
        return {"why": why, "form": "worked", "prereq": prereq, "prereq_title": Path(prereq).stem if prereq else None}
    except Exception:
        # The aid is an extra: a ledger that does not parse costs it, never the rating.
        logger.warning("learn: escalation skipped", exc_info=True)
        return None


def aid(sid: str, i: Any, html_of: Callable[[str, str], str]) -> dict:
    """The aid a rating escalated to, written when the reader asks for it
    (one click: the owner's call of 2026-10-23 on ASSISTments' experiments,
    where aids asked for helped and one that came unasked did not), once,
    and kept for a reload. Reading it records nothing in the ledger; the
    click is logged beside the escalation. `html_of(text, note)` is the
    surface's rendering, kept with the text so a restart keeps both."""
    from silica.kernel.learn import offer

    s = _live(sid)
    it = _item(s, i)
    rec = s["recorded"].get(it["i"])
    esc = rec["result"].get("escalate") if rec else None
    if not esc:
        raise Refused(INVALID, "this answer did not escalate")
    done = s.setdefault("aids", {})
    if it["i"] not in done:
        v = s["verdicts"].get(it["i"]) or {}
        try:
            with _metered(s):
                text = offer.learn_aid(it, esc["why"], v.get("production", ""), esc.get("prereq"))
        except Exception as exc:
            raise Refused(NO_MODEL, f"the model did not answer: {exc}")
        if not text:
            raise Refused(NO_MODEL, "the model wrote nothing")
        offer.log_aid_opened(it["path"], s["id"])
        done[it["i"]] = {"text": text, "html": html_of(text, it["path"])}
        _save()
    return done[it["i"]]


def teach(sid: str, i: Any, reply: str) -> dict:
    from silica.kernel.learn import grade, items

    s = _live(sid)
    it = _item(s, i)
    st = s["teach"].setdefault(it["i"], {"transcript": [], "turn": 0})
    if reply:
        st["transcript"].append({"role": "reader", "text": reply})
    if st["turn"] >= grade.TEACH_TURNS:
        claims = [c["text"] for c in items.claims_of(it["path"])][:12]
        with _metered(s):
            v = grade.teach_summary(it, st["transcript"], claims)
        v.update(conf=None, hints=0, told=False, production="\n".join(t["text"] for t in st["transcript"] if t["role"] == "reader"))
        it["kind"], it["level"], it["objective"] = "teach", 3, "explain"
        s["verdicts"][it["i"]] = v
        _save()
        return {"turn": st["turn"], "done": True, **_verdict_payload(v, it), "summary": v.get("summary", "")}
    concepts = [it.get("title", "")] + [h for h in items.headings_of(items.read_note(it["path"]))[1:4]]
    with _metered(s):
        q = grade.teach_question(it, st["transcript"], concepts)
    st["transcript"].append({"role": "student", "text": q})
    st["turn"] += 1
    _save()
    return {"turn": st["turn"], "question": q, "done": False, "of": grade.TEACH_TURNS}


def _status_of(v: dict, rec: dict | None, skipped: bool) -> str:
    """met | assisted | missed | skipped | open | not_scored, the receipt's
    one word per question (refinement §16)."""
    if skipped:
        return "skipped"
    if v.get("skip"):
        return "not_scored"
    if rec is None:
        return "open"
    return outcome(rec["entry"])


def _receipt(s: dict) -> dict:
    from silica.kernel.learn import items, mastery, objectives as obj, plans
    from silica.kernel.learn import learner, quiz
    from silica.kernel.learn.schedule import DUE_R

    recorded = [s["recorded"][i] for i in sorted(s["recorded"])]
    entries = quiz.entries()
    session_entries = [e for e in entries if e.get("session") == s["id"]]
    tally = {name: 0 for name in RATING_NAME.values()}
    for r in recorded:
        tally[RATING_NAME[r["entry"]["rating"]]] += 1
    rows = []
    for it in _asked(s):
        v = s["verdicts"].get(it["i"]) or {}
        rec = s["recorded"].get(it["i"])
        marker = ("told" if v.get("told") else "assisted" if v.get("hints") else "self-assessed" if v.get("grader") == "self"
                  else "first attempt" if it["kind"] == "predict" else "")
        rows.append({
            "i": it["i"], "path": it["path"], "title": it.get("title", ""), "kind": it["kind"], "level": it["level"],
            "retest": bool(it.get("retest")),
            "level_label": mastery.DEPTH_LABEL.get(it["level"]), "q": it.get("q", ""),
            "objective": it.get("objective") or obj.of_kind(it["kind"]), "point": it.get("point"),
            "correct": rec["entry"]["correct"] if rec else None, "marker": marker, "conf": v.get("conf"),
            "rating": rec["entry"]["rating"] if rec else None,
            "rating_name": RATING_NAME.get(rec["entry"]["rating"]) if rec else None,
            "S_before": rec["result"]["S_before"] if rec else None, "S_after": rec["result"]["S_after"] if rec else None,
            "next_due": rec["result"]["next_due"] if rec else None,
            "skip": bool(v.get("skip")), "note_defect": v.get("note_defect") or "", "span": it.get("source") or {},
            "answered": it["i"] in s["verdicts"], "status": _status_of(v, rec, it["i"] in s["skipped"]),
        })
    for r in rows:
        r["objective_label"] = obj.LABEL.get(r["objective"], r["objective"])
    paths = list(dict.fromkeys(it["path"] for it in _asked(s)))
    prior = [e for e in entries if e.get("session") != s["id"]]
    depth_after = mastery.depth_by_path(paths, entries)
    depth_before = mastery.depth_by_path(paths, prior)
    moved = []
    for p in paths:
        b, a = depth_before[p]["level"], depth_after[p]["level"]
        if a and a != b:
            moved.append({"path": p, "what": "depth", "from": mastery.DEPTH_LABEL.get(b) if b else "not measured",
                          "to": mastery.DEPTH_LABEL.get(a)})
    for r in recorded:
        res = r["result"]
        if res["S_before"] and res["S_after"] and abs(res["S_after"] - res["S_before"]) >= 1:
            moved.append({"path": r["entry"]["path"], "what": "hold", "from": f"~{round(res['S_before'])} d",
                          "to": f"~{round(res['S_after'])} d"})
    # objectives: before and after, against every plan that owns these notes
    owners = []
    if s["plan"]:
        pl = plans.find_plan(s["plan"])
        if pl:
            owners.append(pl)
    else:
        for pl in plans.list_plans():
            if pl["status"] != "completed" and any(p in set(plans.scope_of(pl)["paths"]) for p in paths):
                owners.append(pl)
    for pl in owners[:3]:
        since = pl.get("created")
        for p in paths:
            b = mastery.objectives_met([p], pl["objectives"], prior, since)[p]
            a = mastery.objectives_met([p], pl["objectives"], entries, since)[p]
            for o in pl["objectives"]:
                if b[o]["state"] != a[o]["state"]:
                    moved.append({"path": p, "what": o, "what_label": obj.LABEL[o], "plan": pl["name"],
                                  "from": _state_words(b[o]), "to": _state_words(a[o])})
    sug = mastery.suggestions(session_entries)
    related = []
    for kind, lst in (("blind_spot", sug["blind_spots"]), ("fragile", sug["fragile"])):
        for x in lst:
            related.append({**x, "type": kind, "related": _related_notes(x["path"])})
    defects = [{"path": r["path"], "title": r["title"], "note_defect": r["note_defect"], "span": r["span"]}
               for r in rows if r["note_defect"]]
    by_obj: dict[str, dict] = {}
    for r in rows:
        d = by_obj.setdefault(r["objective"], {"objective": r["objective"], "label": r["objective_label"], "asked": 0,
                                               "met": 0, "assisted": 0, "missed": 0, "skipped": 0})
        d["asked"] += 1
        if r["status"] in d:
            d[r["status"]] += 1
    points = _points_covered(s, rows)
    open_points = [p["i"] for p in points if p["status"] in ("missed", "skipped", "assisted", "open") and p["i"]]
    met_points = sum(1 for p in points if p["status"] == "met")
    answered = sum(1 for r in rows if r["answered"] and r["status"] != "skipped")
    established = _established(rows, moved, points)
    return {
        "id": s["id"], "plan": s["plan"], "note": s.get("note", ""), "origin": s["origin"], "started": s["started"],
        "duration": now() - s["started"], "n": s["n"], "items": rows, "objectives": s["objectives"],
        "objective_labels": [obj.LABEL[o] for o in s["objectives"]],
        "recorded": len(recorded), "tally": tally, "moved": moved, "revisit": related, "defects": defects,
        "established": established, "from_ledger": False, "by_objective": list(by_obj.values()),
        "points": points, "tally3": {
            "answered": answered, "of": len(rows), "skipped": len(s["skipped"]),
            "met": met_points if points else sum(1 for r in rows if r["status"] == "met"),
            "points": len(points) or len(rows), "assisted": sum(1 for r in rows if r["status"] == "assisted"),
            "missed": sum(1 for r in rows if r["status"] == "missed"),
            "with_evidence": sum(1 for d in by_obj.values() if d["met"]), "objectives": len(s["objectives"]),
        },
        "followup": {"points": open_points, "questions": len(open_points), "minutes": items.minutes(len(open_points))} if s.get("note") and open_points else None,
        "empty": not recorded and not s["verdicts"],
        "meter": _meter_shown(s),
        "retests": {"asked": sum(1 for r in recorded if r["entry"].get("retest")),
                    "met": sum(1 for r in recorded if r["entry"].get("retest") and r["entry"]["correct"])},
        # the same count Today prints: every note with a card below the desired retention
        "next": _next_line(rows), "due_left": sum(1 for r in learner.view().values()
                                                  if r["R"] is not None and r["R"] < DUE_R),
        "plan_line": None,
    }


def _state_words(st: dict) -> str:
    if st["met"]:
        return f"met without help on {iso_day(ts(st['met']))}"
    if st["assisted"]:
        return "met with help, not counted"
    if st["missed"]:
        return "missed"
    return "still to verify"


def _points_covered(s: dict, rows: list[dict]) -> list[dict]:
    """The key points a note session touched, one status each: the best
    outcome among the questions on the point, `met` only without help."""
    if not s.get("note"):
        return []
    order = {"met": 0, "assisted": 1, "missed": 2, "skipped": 3, "not_scored": 4, "open": 5}
    by_point: dict[int, dict] = {}
    for r in rows:
        pt = r.get("point") or {}
        i = int(pt.get("i") or 0)
        d = by_point.setdefault(i, {"i": i, "kind": pt.get("kind", "relation"), "heading": pt.get("heading", ""),
                                    "objective": r["objective"], "label": r["objective_label"], "status": None, "items": []})
        d["items"].append(r["i"])
        if d["status"] is None or order.get(r["status"], 9) < order.get(d["status"], 9):
            d["status"] = r["status"]
    return [by_point[i] for i in sorted(by_point, key=lambda i: (i == 0, i))]


def _related_notes(path: str) -> list[dict]:
    from silica.kernel.learn import items
    from silica.kernel.learn import learner

    out = []
    try:
        for p in learner.prerequisites_of(path)[:3]:
            out.append({"path": p, "why": "a prerequisite the vault names"})
    except Exception:
        pass   # a suggestion on the receipt: no graph costs this line, never the close
    try:
        title = items.title_of(path)
        for r in items.relations_of(path)[:3]:
            other = r["object"] if r["subject"] == title else r["subject"]
            out.append({"path": other, "why": f"{r['label']} in the note's relations"})
    except Exception:
        pass   # a note gone or unparsable since the session: the receipt stands without its relations
    return out[:4]


def _established(rows: list[dict], moved: list[dict], points: list[dict]) -> str:
    from silica.kernel.learn import objectives as obj

    parts = []
    if points:
        held = [p["kind"] for p in points if p["status"] == "met"]
        missed = [p["kind"] for p in points if p["status"] == "missed"]
        skipped = [p["kind"] for p in points if p["status"] == "skipped"]
        title = (rows[0]["title"] or rows[0]["path"]) if rows else ""
        if held:
            parts.append(f"{title}: {_join(held)} held without help.")
        if missed:
            parts.append(f"Missed: {_join(missed)}.")
        if skipped:
            parts.append(f"Skipped: {_join(skipped)}.")
    else:
        held = [r["title"] or r["path"] for r in rows if r["status"] == "met"]
        missed = [r["title"] or r["path"] for r in rows if r["status"] == "missed"]
        if held:
            parts.append(f"{', '.join(held[:3])}: criteria met without help.")
        if missed:
            parts.append(f"Still missing: {', '.join(missed[:3])}.")
    ups = [m for m in moved if m["what"] in obj.INTENTIONS and "met" in m["to"] and "help" in m["to"]]
    if ups:
        parts.append("For the plan: " + "; ".join(f"{m['what']} gained evidence on {m['path'].rsplit('/', 1)[-1].removesuffix('.md')}" for m in ups[:3]) + ".")
    return " ".join(parts) or "Nothing was recorded in this session."


def _join(words: list[str]) -> str:
    from collections import Counter

    c = Counter(words)
    return ", ".join(f"{k}" + (f" ×{n}" if n > 1 else "") for k, n in c.items())


def _next_line(rows: list[dict]) -> str:
    miss = next((r for r in rows if r["correct"] is False), None)
    return f"Next session starts from {miss['title'] or miss['path']}." if miss else "Next session draws from what is due."


# ---------------------------------------------------------------- the `## Sessions` line
# A closed session's line on its plan note, composed and read here and nowhere
# else: position 0 is the date, 1 the items, and the id, the tag's value, is
# what makes a retry append nothing. `static/app-learn.js` reads the same text.

_SESSION_ID_RE = re.compile(r"session:\s*([0-9a-f]{8,32})")


def compose_line(rc: dict, sid: str) -> str:
    """The receipt as its plan's line."""
    t = rc["tally"]
    met = " · met: " + ", ".join(f"{d['objective']} {d['met']}" for d in rc["by_objective"]) if rc["by_objective"] else ""
    return (f"{datetime.now(timezone.utc).date().isoformat()} · {rc['recorded']} items · ratings: "
            f"{t['Good'] + t['Easy']} Good/Easy · {t['Hard']} Hard · {t['Again']} Again{met} · {rc['next'].rstrip('.')} · session: {sid}")


def line_id(line: str) -> str:
    m = _SESSION_ID_RE.search(line)
    return m.group(1) if m else ""


def has_line(text: str, sid: str) -> bool:
    """Whether a note already carries this session's line."""
    return f"session: {sid}" in text


def parse_lines(lines: list[str]) -> list[dict]:
    """The `## Sessions` section's lines as {date, items, line, session, met}."""
    out = []
    for line in lines:
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = [p.strip() for p in line[2:].split(" · ")]
        m = re.match(r"(\d+) item", parts[1] if len(parts) > 1 else "")
        met = next((x[len("met: "):] for x in parts if x.startswith("met: ")), "")
        out.append({"date": parts[0][:10], "items": int(m.group(1)) if m else 0,
                    "line": line[2:], "session": line_id(line), "met": met})
    return out


# ---------------------------------------------------------------- closing

def close(sid: str) -> dict:
    global _LIVE, _LAST
    from silica.kernel.learn import plans

    if _LIVE is None or _LIVE["id"] != sid:
        if _LAST and _LAST["id"] == sid:
            return _LAST
        raise Refused(MISSING, "no live session with that id")
    s = _LIVE
    rc = _receipt(s)
    if s["plan"] and rc["recorded"]:
        line = compose_line(rc, sid)
        try:
            plans.append_session(s["plan"], line)
            rc["plan_line"] = line
            consumed = [r["entry"]["path"] for r in (s["recorded"][i] for i in sorted(s["recorded"])) if r["entry"]["path"] in set(s["revisit"])]
            if consumed:
                plans.clear_revisit(s["plan"], consumed)
        except Exception as exc:
            logger.warning("learn: plan summary not saved (%s)", exc)
            rc["plan_line"] = None
            rc["plan_error"] = "Answers recorded; plan summary not saved"
    _drop_file(s)
    _LAST, _LIVE = rc, None
    return rc


def receipt(sid: str) -> dict:
    """The closed session's receipt, or what the ledger alone can say of it
    once memory has none."""
    from silica.kernel.learn import mastery, objectives as obj
    from silica.kernel.learn import quiz

    if _LAST and _LAST["id"] == sid:
        return _LAST
    evs = [e for e in quiz.entries() if e.get("session") == sid]
    if not evs:
        raise Refused(MISSING, "no recorded events for that session")
    tally = {name: 0 for name in RATING_NAME.values()}
    rows = []
    for e in evs:
        r = e.get("rating") or (3 if e.get("correct") else 1)
        tally[RATING_NAME[r]] += 1
        o = obj.of_entry(e)
        rows.append({"path": e["path"], "title": e["path"].rsplit("/", 1)[-1].removesuffix(".md"), "kind": e.get("kind", ""),
                     "retest": bool(e.get("retest")),
                     "level": e.get("level"), "level_label": mastery.DEPTH_LABEL.get(e.get("level") or 0), "q": e.get("q", ""),
                     "objective": o, "objective_label": obj.LABEL[o], "point": {"i": e["point"]} if e.get("point") else None,
                     "correct": e.get("correct"), "conf": e.get("conf"), "rating": r, "rating_name": RATING_NAME[r],
                     "marker": "told" if e.get("told") else "assisted" if e.get("hints") else "self-assessed" if e.get("grader") == "self" else "",
                     "span": e.get("source") or {}, "skip": False, "note_defect": "", "answered": True, "status": outcome(e)})
    plan = next((e.get("plan") for e in evs if e.get("plan")), "")
    paths = list(dict.fromkeys(e["path"] for e in evs))
    origin = {"kind": "plan", "path": plan, "label": plan.rsplit("/", 1)[-1].removesuffix(".md")} if plan else \
        {"kind": "note", "path": paths[0], "label": rows[0]["title"]} if len(paths) == 1 else {"kind": "vault", "path": "", "label": "Suggested practice"}
    by_obj: dict[str, dict] = {}
    for r in rows:
        d = by_obj.setdefault(r["objective"], {"objective": r["objective"], "label": r["objective_label"], "asked": 0, "met": 0, "assisted": 0, "missed": 0, "skipped": 0})
        d["asked"] += 1
        d[r["status"]] += 1
    return {"id": sid, "plan": plan, "note": paths[0] if len(paths) == 1 else "", "origin": origin, "items": rows, "recorded": len(rows),
            "tally": tally, "moved": [], "revisit": [], "defects": [], "from_ledger": True, "by_objective": list(by_obj.values()),
            "objectives": list(by_obj), "objective_labels": [d["label"] for d in by_obj.values()], "points": [],
            "tally3": {"answered": len(rows), "of": len(rows), "skipped": 0, "met": sum(1 for r in rows if r["status"] == "met"),
                       "points": len(rows), "assisted": sum(1 for r in rows if r["status"] == "assisted"),
                       "missed": sum(1 for r in rows if r["status"] == "missed"),
                       "with_evidence": sum(1 for d in by_obj.values() if d["met"]), "objectives": len(by_obj)},
            "established": "Recorded events only: verdict text, answers and invalid items are not kept in the ledger.",
            "next": "", "ts": evs[0].get("ts"), "plan_line": None, "followup": None, "empty": False}
