# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Learn tab's routes (learn-suite §6.1), an APIRouter mounted on the app.

The engine lives in silica/kernel/learn, and the session in `session.py`: these
routes call it and translate its answers and its refusals for the tab, and
shape the readings the tab draws (the queue, the plan rows, the map).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from silica.kernel.base import note_index, noteshape
from silica.kernel.base.notekey import ledger_key
from silica.kernel.learn import items, reasons, session
from silica.kernel.learn.schedule import DUE_R
from silica.ui.web import render

logger = logging.getLogger(__name__)

SUGGESTIONS_N = 8

router = APIRouter(prefix="/learn", dependencies=[Depends(session.load_live)])

# What a refusal of the engine answers on this surface.
_STATUS = {session.BUSY: 409, session.MISSING: 404, session.INVALID: 400, session.NO_MODEL: 502}


@contextmanager
def _engine() -> Iterator[None]:
    try:
        yield
    except session.Refused as exc:
        raise HTTPException(_STATUS[exc.why], str(exc))


# ---------------------------------------------------------------- readings

def _also_in(path: str, scopes: dict[str, dict]) -> list[dict]:
    return [{"path": k, "name": v["name"]} for k, v in scopes.items() if path in v["paths"]]


@router.get("")
def learn_home():
    from silica.kernel.learn import mastery, pick, plans
    from silica.kernel.learn import learner, quiz

    entries = quiz.entries()
    now = session.now()
    rows = learner.view(now_ts=now, _entries_override=entries)
    measured = [r for r in rows.values() if r["attempts"]]
    due_rows = [r for r in rows.values() if r["R"] is not None and r["R"] < DUE_R]
    unexplored = [r for r in rows.values() if r["attempts"] == 0]
    horizon = []
    for d in range(7):
        lo, hi = now + d * 86400, now + (d + 1) * 86400
        n = sum(1 for r in rows.values() if r["due"] and (r["due"] < hi if d == 0 else lo <= r["due"] < hi))
        horizon.append({"day": session.iso_day(lo), "notes": n})
    areas: dict[str, dict] = {}
    for r in rows.values():
        folder = r["path"].rsplit("/", 1)[0] if "/" in r["path"] else "(root)"
        a = areas.setdefault(folder, {"folder": folder, "notes": 0, "measured": 0, "due": 0, "never": 0, "_r": []})
        a["notes"] += 1
        if r["attempts"]:
            a["measured"] += 1
            if r["R"] is not None:
                a["_r"].append(r["R"])
        else:
            a["never"] += 1
        if r["R"] is not None and r["R"] < DUE_R:
            a["due"] += 1
    area_rows = []
    for a in sorted(areas.values(), key=lambda a: (-a["measured"], a["folder"]))[:12]:
        rs = a.pop("_r")
        a["R_mean"] = (sum(rs) / len(rs)) if rs else None
        area_rows.append(a)
    stab = sorted(r["S"] for r in measured if r["S"])
    plan_list = plans.list_plans()
    scopes = session.plan_scopes(plan_list)
    preview = pick.session(n=SUGGESTIONS_N, now_ts=now, _entries_override=entries)
    sug = [_preview_row(it, scopes) for it in preview["items"]]
    counts = {"review": sum(1 for s in sug if s["pool"] == "due"), "first": sum(1 for s in sug if s["pool"] == "unexplored" and not s["stale"]),
              "changed": sum(1 for s in sug if s["stale"]), "other": sum(1 for s in sug if s["pool"] not in ("due", "unexplored"))}
    last = _last_session(entries)
    plan_rows = [_plan_row(p, rows, entries) for p in plan_list]
    groups = _next_groups(entries, rows, preview, plan_rows, scopes, now)
    session_q = len(preview["items"])
    evs_by = reasons.events_of(entries)
    live = session.live()
    return {
        "next": groups, "mix": _mix(preview["items"], groups),
        "session": {"questions": session_q, "minutes": items.minutes(session_q)} if session_q else None,
        # the bar is the vault and says so; a plan's count says "in this plan" (ADR-0081 §4)
        "stamp": {"waiting": len(due_rows), "more": sum(h["notes"] for h in horizon[1:]),
                  "by": horizon[-1]["day"], "title": (f"{len(due_rows)} waiting after a break is spacing doing its job, "
                                                      "not a debt; a session takes a few and the rest waits.")},
        "measured": len(measured), "total": len(rows), "due": len(due_rows), "unexplored": len(unexplored),
        "horizon": horizon, "areas": area_rows, "median_S": stab[len(stab) // 2] if stab else None,
        "plans": plan_rows,
        "calibration": mastery.calibration(entries), "grader": mastery.grader_agreement(entries),
        "last_session": last, "suggestions": mastery.suggestions(entries),
        "preview": {"items": sug, "short": preview["short"], "n": SUGGESTIONS_N,
                    # the picker's line speaks of a session; here the eight are a list
                    "amnesty": (f"{preview['due']} to review after a break is spacing doing its job, not a debt. "
                                f"The list shows {SUGGESTIONS_N}; the rest waits.") if preview["amnesty"] else None,
                    "counts": counts, "first_four_minutes": items.minutes(sum(s["questions"] for s in sug[:4]))},
        "live": live["id"] if live else None, "live_info": session.live_info(),
        "session_rows": [_session_row(it, evs_by, now) for it in preview["items"]],
        "recent": _recent(rows, now),
    }


def _session_row(it: dict, evs_by: dict[str, list[dict]], now: float) -> dict:
    """A note of the next session as the home lists it (ADR-0119, D): its
    reason in ADR-0081's words and the mark of its last answer."""

    evs = evs_by.get(ledger_key(it["path"]), [])
    title = " · ".join(reasons.stem(x) for x in it["pair"]) if it.get("pair") else reasons.stem(it["path"])
    mark = "never" if not evs else {"met": "full", "assisted": "half", "missed": "open"}[reasons.outcome(evs[-1])]
    return {"path": it["path"], "title": title, "why": reasons.reason(it, evs, now, pool=it["pool"]), "mark": mark,
            "pair": it.get("pair")}


RECENT_DAYS = 7
RECENT_N = 4


def _recent(rows: dict, now: float) -> list[dict]:
    """The notes changed this week, newest first: what a student who studies
    to their own plan most likely wants to be tested on (t4, messages 3, 7)."""
    from silica.config import CONFIG

    # the view's rows carry no mtime; the note walk does, and says what is material
    meta = note_index.meta(Path(CONFIG.vault_path))
    fresh = [(m["mtime"], p) for p, m in meta.items()
             if noteshape.is_material(m) and (m.get("mtime") or 0) >= now - RECENT_DAYS * 86400
             and ledger_key(p) in rows]
    fresh.sort(reverse=True)
    return [{"path": p, "title": reasons.stem(p)} for _t, p in fresh[:RECENT_N]]


def _preview_row(it: dict, scopes: dict[str, dict]) -> dict:
    pool = "stale" if it.get("stale") else it["pool"]
    detail = []
    if it.get("stale"):
        detail.append("edited " + (datetime.fromtimestamp(it["mtime"], timezone.utc).strftime("%b %d") if it.get("mtime") else "recently"))
    elif it["pool"] == "unexplored":
        detail.append("AI-written" if it.get("ai") else "written by you")
    elif isinstance(it.get("S"), (int, float)) and not isinstance(it.get("S"), bool):
        detail.append(f"holds ~{round(it['S'])} d est.")
    points = items.key_points(it["path"])
    objectives = session.objectives_for(it["path"], scopes)
    asks = items.ask_plan(points, objectives, len(items.relations_of(it["path"])))
    return {"path": it["path"], "title": items.title_of(it["path"]), "pool": it["pool"], "reason": session.REASON.get(pool, pool),
            "detail": " · ".join(detail), "kind": it["kind"], "level": it["level"], "folder": it.get("folder", ""),
            "S": it.get("S"), "R": it.get("R"), "ai": it.get("ai"), "stale": bool(it.get("stale")), "pair": it.get("pair"),
            "attempts": it.get("attempts", 0), "last": it.get("last_event"),
            "also_in": _also_in(it["path"], scopes), "points": len(points), "questions": len(asks),
            "minutes": items.minutes(len(asks)) if asks else 0, "objectives": objectives}


# ---------------------------------------------------------------- Next: the queue that says why (ADR-0081)
NEXT_DUE_N = 12
NEXT_NEVER_N = 6


def _next_row(path: str, title: str, reason: str, group: str, scopes: dict, **extra) -> dict:
    why = reason.removeprefix("not tested yet · ") if group == "never" else reason
    also = _also_in(path, scopes)
    if also and group != "plans":
        why += f" · in {also[0]['name']}"
    return {"path": path, "title": title, "reason": reason, "why": why, "group": group,
            "also_in": also, **extra}


def _plan_next_why(p: dict, row: dict | None, evs: list[dict], now: float, missed: dict | None = None) -> str:
    """A plan row's clause: where it resumes and why that note. A next note
    missed lately says the miss: it is the more useful of the two reasons."""
    if not p.get("next"):
        return "nothing pending" if not p.get("gaps") else f"not started · {p['gaps']} step(s) not in the vault"
    if p["next"].get("queued"):
        return f"{p['next']['title']} · queued by you"
    if missed:
        return f"{p['next']['title']} · {reasons.miss_clause(missed['misses'], evs[-1])}"
    if not evs:
        return f"{p['next']['title']} · first in order, not tested yet"
    return f"{p['next']['title']} · first in the plan's order"


def _next_groups(entries: list[dict], rows: dict, preview: dict, plan_rows: list[dict], scopes: dict, now: float) -> dict:

    evs_by = reasons.events_of(entries)
    lately = reasons.missed_lately(evs_by, reasons.lately_cutoff(entries, now))
    by_miss = {m["path"]: m for m in lately}
    taken: set[str] = set()
    plans_g = []
    for p in plan_rows:
        if p["status"] == "completed":
            continue
        nxt = (p.get("next") or {}).get("path")
        evs = evs_by.get(ledger_key(nxt), []) if nxt else []
        if nxt:
            taken.add(nxt)
        plans_g.append({"path": p["path"], "name": p["name"],
                        "why": _plan_next_why(p, rows.get(ledger_key(nxt or "")), evs, now, by_miss.get(nxt or "")),
                        "next": p.get("next"), "open_session": p.get("open_session"), "by": p.get("by"), "group": "plans"})
    missed = []
    for m in lately:
        # the ledger outlives a renamed or deleted note; its row would open a 404
        if m["path"] in taken or ledger_key(m["path"]) not in rows:
            continue
        taken.add(m["path"])
        k = ledger_key(m["path"])
        missed.append(_next_row(m["path"], reasons.stem(m["path"]), reasons.reason(rows.get(k, {}), evs_by.get(k, []), now, misses=m["misses"]),
                                "missed", scopes, questions=len(m["misses"])))
    due: list[dict] = []
    never: list[dict] = []
    in_session = {id(it) for it in preview["items"]}
    for it in list(preview["items"]) + list(preview.get("backfill") or []):
        if it["path"] in taken:
            continue
        group = "never" if it["pool"] == "unexplored" and not it.get("stale") and not it.get("attempts") else "due"
        if (group == "due" and len(due) >= NEXT_DUE_N) or (group == "never" and len(never) >= NEXT_NEVER_N):
            continue
        taken.add(it["path"])
        k = ledger_key(it["path"])
        title = " · ".join(reasons.stem(x) for x in it["pair"]) if it.get("pair") else reasons.stem(it["path"])
        row = _next_row(it["path"], title, reasons.reason(it, evs_by.get(k, []), now, pool=it["pool"]), group, scopes,
                        pair=it.get("pair"), in_session=id(it) in in_session)
        (due if group == "due" else never).append(row)
    return {"plans": plans_g, "missed": missed, "due": due, "never": never}


def _mix(preview_items: list[dict], groups: dict) -> str:
    missed = {r["path"] for r in groups["missed"]}
    n = {"due": 0, "missed": 0, "never": 0}
    for it in preview_items:
        # Never answered is "not tested yet" from any pool but the schedule's
        # own, as _reason says. Read off the "due" list, which also holds the
        # pairs, a contrast pair on a fresh vault read "1 fading" (2026-10-26).
        g = ("missed" if it["path"] in missed
             else "never" if not it.get("attempts") and it["pool"] != "due" else "due")
        n[g] += 1
    words = {"due": "fading", "missed": "missed", "never": "not tested yet"}
    return ", ".join(f"{v} {words[k]}" for k, v in n.items() if v)


def _last_session(entries: list[dict]) -> dict | None:
    by_id: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("session"):
            by_id.setdefault(e["session"], []).append(e)
    if not by_id:
        return None
    sid, evs = max(by_id.items(), key=lambda kv: max(str(e.get("ts")) for e in kv[1]))
    return {"id": sid, "ts": max(str(e.get("ts")) for e in evs), "items": len(evs), "plan": next((e.get("plan") for e in evs if e.get("plan")), "")}


def _plan_row(p: dict, rows: dict, entries: list[dict]) -> dict:
    from silica.kernel.learn import mastery, objectives as obj, plans

    # Read once for every title lookup below: steps_passed without it read the
    # vault's notes per step note, 84 epoch walks and 24 s of a 30 s Learn
    # home on a 2,166-note vault (2026-10-23, profiled).
    notes = note_index.meta(plans._vault())
    scoped = plans.scope_of(p, notes=notes)
    node = mastery.node_readings(scoped["paths"], rows, entries, p.get("created"), p["objectives"])
    frontier = scoped["frontier"]
    gaps = [s for s in p["steps"] if plans.is_gap(s, notes)]
    passed = sum(mastery.steps_passed(p, entries, notes=notes)) if p["steps"] else None
    nxt = None
    # a queued note is asked first (pick.session takes revisit before the
    # frontier), so it is where the plan resumes and what its row names
    first = (scoped["revisit"] or frontier or [None])[0]
    if first:
        o = plans.next_objective(p, first, entries)
        nxt = {"path": first, "title": reasons.stem(first), "objective": o, "label": obj.LABEL[o],
               "queued": first in scoped["revisit"],
               "step": next((i + 1 for i, s in enumerate(p["steps"]) if Path(first).stem in s["notes"]), None)}
    return {
        "path": p["path"], "name": p["name"], "scope": p["scope"], "target": p["target"], "reach": p["reach"],
        "reach_label": plans.REACH_LABEL[p["reach_level"]], "by": p.get("by"), "status": p["status"],
        "objectives": p["objectives"], "objective_labels": [obj.LABEL[o] for o in p["objectives"]],
        "objectives_chosen": p["objectives_chosen"],
        "completed": p.get("completed"), "sessions": len(p["sessions"]), "last": p["sessions"][-1]["date"] if p["sessions"] else None,
        "coverage": node["coverage"], "due": node["due"], "R_mean": node["R_mean"], "frontier": [Path(x).stem for x in frontier],
        "next": nxt, "revisit": len(scoped["revisit"]), "gaps": len(gaps), "steps": len(p["steps"]),
        "steps_passed": passed, "all_met": node["all_met"], "met_by_objective": node["met_by_objective"],
        "percent": plans.percent_done(node["all_met"], node["coverage"][1], len(gaps)),
        "open_session": session.is_open(p["path"]),
        "pace": plans.pace_of(p, len(scoped["paths"]) - node["all_met"], entries=entries),
    }


@router.get("/note")
def note_detail(path: str):
    """What a session on one note would cover (refinement §13, §18): the key
    points with the objective each serves, the question count, the plans
    that include the note. Never a question, never a claim's wording. Since
    ADR-0081 also why it is up, as a clause and a sentence, and its history:
    every ledger line, which is the questions the reader already answered."""
    from silica.config import CONFIG
    from silica.kernel.learn import plans
    from silica.kernel.learn import learner, quiz

    text = items.read_note(path)
    if not text:
        raise HTTPException(404, f"no note at {path!r}")
    entries = quiz.entries()
    rows = learner.view(_entries_override=entries)
    row = rows.get(ledger_key(path)) or {}
    scopes = session.plan_scopes(plans.list_plans())
    points = items.key_points(path, text)
    objectives = session.objectives_for(path, scopes)
    rels = items.relations_of(path, text)
    asks = items.ask_plan(points, objectives, len(rels))
    by_point: dict[int, list[dict]] = {}
    for a in asks:
        by_point.setdefault(a["point"], []).append(a)
    out_points = []
    for pt in points:
        qs = by_point.get(pt["i"], [])
        out_points.append({"i": pt["i"], "kind": pt["kind"], "heading": pt["heading"], "line": pt["note_line"],
                           "objective": qs[0]["objective"] if qs else None, "questions": len(qs)})
    if by_point.get(0):
        out_points.append({"i": 0, "kind": "relation", "heading": "Relations", "line": 0,
                           "objective": by_point[0][0]["objective"], "questions": len(by_point[0])})
    leaves = sorted({c["leaf"] for c in points if c.get("leaf") and not c.get("self")})
    last = max((t for e in entries if ledger_key(str(e.get("path") or "")) == ledger_key(path) and (t := e.get("ts"))),
               default=None)
    misses = sum(1 for e in entries if ledger_key(str(e.get("path") or "")) == ledger_key(path) and not e.get("correct"))
    now = session.now()
    history = reasons.history(path, entries)
    evs = reasons.events_of(entries).get(ledger_key(path), [])
    lately = next((m for m in reasons.missed_lately({ledger_key(path): evs}, reasons.lately_cutoff(entries, now))), None) if evs else None
    stale_row = {**row, "mtime": (note_index.meta(Path(CONFIG.vault_path)).get(path) or {}).get("mtime")}
    stale_row["stale"] = bool(evs and stale_row["mtime"] and stale_row["mtime"] > (reasons.ts(evs[-1].get("ts")) or now) + 1.0)
    reason = reasons.reason(stale_row, evs, now, misses=lately["misses"] if lately else None)
    also = _also_in(path, scopes)
    live = session.live()
    return {
        "history": history, "why": reason, "sentence": reasons.reason_sentence(reason, history, also[0]["name"] if also else ""),
        "group": "missed" if lately else "never" if not evs and not row.get("attempts") else "due",
        "path": path, "title": reasons.stem(path), "points": out_points, "questions": max(1, len(asks)) if points else 0,
        "minutes": items.minutes(max(1, len(asks))), "atomic": len(points) == 1, "objectives": objectives,
        "also_in": _also_in(path, scopes), "leaves": leaves, "self_grounded": bool(points) and all(c.get("self") for c in points),
        "R": row.get("R"), "S": row.get("S"), "attempts": row.get("attempts", 0), "misses": misses, "last": last,
        "ai": row.get("ai"), "due": row.get("R") is not None and row["R"] < DUE_R,
        "reason": session.REASON["due"] if (row.get("R") is not None and row["R"] < DUE_R) else session.REASON["unexplored"] if not row.get("attempts") else "Practice this note",
        "grounded": bool(points) or bool(rels),
        "live": live["id"] if live and live.get("note") == path else None,
        "reading": _reading_of(path),
    }


def _reading_of(path: str) -> dict | None:
    """The plan step whose reading explains this note (ADR-0132): the Study
    tools' Explanation opens it instead of the chat."""
    from silica.kernel.learn import plans, reading

    for p in plans.list_plans():
        i = reading.step_of_note(p, path)
        if i is not None:
            return {"plan": p["path"], "step": i}
    return None


# ---------------------------------------------------------------- the session

@router.post("/session")
def start_session(payload: dict):
    with _engine():
        return session.start(n=payload.get("n"), plan=str(payload.get("plan") or ""), note=str(payload.get("note") or ""),
                             scope=payload.get("scope"), step=payload.get("step"), points=payload.get("points"),
                             teach=bool(payload.get("teach")), placement=bool(payload.get("placement")))


@router.get("/session")
def live_session(id: str):
    with _engine():
        return session.reload(id)


@router.post("/answer")
def answer(payload: dict):
    with _engine():
        return session.answer(str(payload.get("id") or ""), payload.get("i"), conf=payload.get("conf"),
                              hints=payload.get("hints"), told=payload.get("told"), production=payload.get("production"),
                              modality=payload.get("modality"), self_graded=payload.get("self"),
                              force_model=payload.get("force_model"))


@router.post("/skip")
def skip(payload: dict):
    with _engine():
        return session.skip(str(payload.get("id") or ""), payload.get("i"))


@router.post("/rate")
def rate(payload: dict):
    with _engine():
        return session.rate(str(payload.get("id") or ""), payload.get("i"), payload.get("rating", ""))


def _aid_html(text: str, note: str) -> str:
    from silica.kernel.graph.mindmap import answer_resolver

    # the chat's resolver: a name two notes share links to this item's note
    resolve, _read = answer_resolver([note])
    return render.linkify(text, resolve, remote_images=False)


@router.post("/aid")
def aid(payload: dict):
    with _engine():
        return session.aid(str(payload.get("id") or ""), payload.get("i"), _aid_html)


MATH_MAX = 200        # formulas one page asks for at once; a session's page holds a few dozen
MATH_CHARS = 2000     # one formula's LaTeX; a note's longest display block is a few hundred


@router.post("/math")
def math(payload: dict):
    """The formulas in Learn's text as MathML, converted as the chat's are
    (render.mathml: the browser draws them, and a formula that does not
    convert comes back as written)."""
    tex = [str(t)[:MATH_CHARS] for t in (payload.get("tex") or [])][:MATH_MAX]
    display = list(payload.get("display") or [])
    return {"mathml": [render.mathml(t, bool(display[i]) if i < len(display) else False) for i, t in enumerate(tex)]}


@router.post("/teach")
def teach(payload: dict):
    with _engine():
        return session.teach(str(payload.get("id") or ""), payload.get("i"), str(payload.get("reply") or "").strip())


@router.post("/close")
def close(payload: dict):
    with _engine():
        return session.close(str(payload.get("id") or ""))


@router.get("/receipt")
def receipt(id: str):
    with _engine():
        return session.receipt(id)


# ---------------------------------------------------------------- plans

@router.get("/plans")
def list_plans():
    from silica.kernel.learn import plans
    from silica.kernel.learn import learner, quiz

    entries = quiz.entries()
    rows = learner.view(_entries_override=entries)
    return [_plan_row(p, rows, entries) for p in plans.list_plans()]


@router.get("/plan")
def plan_detail(path: str):
    """The plan's report, plus ADR-0081's two blocks: Lately (its misses as
    rows, a misconception line joined to its row) and After this (three
    in-scope rows with reasons)."""
    from silica.kernel.learn import plans
    from silica.kernel.learn import learner, quiz

    p = plans.find_plan(path)
    if p is None:
        raise HTTPException(404, f"no plan at {path!r}")
    entries = quiz.entries()
    view = learner.view(_entries_override=entries)
    rep = plans.plan_report(p, entries=entries, view=view)
    rep["open_session"] = session.is_open(p["path"])
    rep["live_info"] = session.live_info() if rep["open_session"] else None
    now = session.now()
    scoped = set(rep["paths"])
    evs_by = {k: v for k, v in reasons.events_of(entries).items() if v and str(v[-1]["path"]) in scoped}
    mis = {m["path"]: m["misconception"] for m in rep.get("misconceptions") or []}
    lately = []
    for m in reasons.missed_lately(evs_by, reasons.lately_cutoff(entries, now)):
        k = ledger_key(m["path"])
        lately.append({"path": m["path"], "title": reasons.stem(m["path"]), "questions": len(m["misses"]),
                       "why": reasons.reason(view.get(k, {}), evs_by.get(k, []), now, misses=m["misses"]),
                       "misconception": mis.get(m["path"], "")})
    rep["lately"] = lately
    skip = {x["path"] for x in lately} | {(rep.get("next") or {}).get("path")}
    order = {pth: i for i, pth in enumerate(rep["paths"])}
    cands = [pth for pth in rep["paths"] if pth not in skip]
    cands.sort(key=lambda pth: (not (view.get(ledger_key(pth), {}).get("attempts") and (view.get(ledger_key(pth), {}).get("R") or 1) < DUE_R),
                                bool(view.get(ledger_key(pth), {}).get("attempts")), order[pth]))
    rep["after"] = [{"path": pth, "title": reasons.stem(pth),
                     "why": reasons.reason(view.get(ledger_key(pth), {}), evs_by.get(ledger_key(pth), []), now)}
                    for pth in cands[:3]]
    return rep


@router.get("/map")
def learn_map():
    """What you know as marks (ADR-0081 §7): one row per folder, one cell per
    note. ◌ never asked, ○ asked and open, ◐ met with help, ● met without
    help, from the note's last answer; no model call, no new store."""
    from silica.config import CONFIG
    from silica.kernel.learn import plans
    from silica.kernel.learn import learner, quiz

    entries = quiz.entries()
    now = session.now()
    notes = note_index.meta(Path(CONFIG.vault_path))
    rows = learner.view(now_ts=now, _notes_override=notes, _entries_override=entries)
    evs_by = reasons.events_of(entries)
    cutoff = reasons.lately_cutoff(entries, now)
    plan_list = [p for p in plans.list_plans() if p["status"] != "completed"]
    in_plan: dict[str, list[str]] = {}
    for pl in plan_list:
        try:
            for pth in plans.scope_of(pl)["paths"]:
                in_plan.setdefault(pth, []).append(pl["path"])
        except Exception as exc:
            logger.debug("learn: map without the scope of %s (%s)", pl["path"], exc)
    folders: dict[str, dict] = {}
    for pth, meta in notes.items():
        if not noteshape.is_material(meta):
            continue
        k = ledger_key(pth)
        row, evs = rows.get(k) or {}, evs_by.get(k, [])
        mark = "never" if not evs else {"met": "full", "assisted": "half", "missed": "open"}[reasons.outcome(evs[-1])]
        lately = reasons.missed_lately({k: evs}, cutoff) if evs else []
        why = reasons.reason({**row, "attempts": len(evs)}, evs, now, misses=lately[0]["misses"] if lately else None) if evs else "never asked"
        folder = pth.rsplit("/", 1)[0] if "/" in pth else "(root)"
        f = folders.setdefault(folder, {"folder": folder, "notes": [], "practised": 0, "plans": set()})
        f["notes"].append({"path": pth, "title": Path(pth).stem, "mark": mark, "why": why, "plans": in_plan.get(pth, [])})
        f["practised"] += bool(evs)
        f["plans"].update(in_plan.get(pth, []))
    out = []
    for f in sorted(folders.values(), key=lambda f: (not (f["practised"] or f["plans"]), -f["practised"], -len(f["notes"]), f["folder"])):
        f["notes"].sort(key=lambda n: ({"open": 0, "half": 1, "full": 2, "never": 3}[n["mark"]], n["title"].casefold()))
        out.append({**f, "plans": sorted(f["plans"]), "total": len(f["notes"])})
    return {"folders": out, "total": sum(f["total"] for f in out), "practised": sum(f["practised"] for f in out),
            "plans": [{"path": pl["path"], "name": pl["name"]} for pl in plan_list]}


@router.get("/objectives")
def objective_options():
    from silica.kernel.learn import objectives as obj

    return [{"id": i, "label": obj.LABEL[i], "description": obj.DESCRIPTION[i]} for i in obj.INTENTIONS]


@router.post("/plan")
def write_plan(payload: dict):
    """Preview, objectives, create or edit (ADR-0081 §12 to §17). A preview
    is fast and model-free: the material, its marks and the plan's own
    bases, the match kind of each syllabus line, the parts of a topic by
    folder, what Silica will ask per part. `cards_only` writes the objective
    cards (the one model call, run in the background by the form). With
    `path` it edits that plan; otherwise it creates one."""
    from silica.kernel.learn import objectives as obj, plans

    name = str(payload.get("name") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    target = payload.get("target")
    restrict = str(payload.get("restrict") or "")
    # a refused line by its index (this form) or its text (the plan note's `refused:`)
    refused = [int(i) if str(i).lstrip("-").isdigit() else str(i) for i in (payload.get("refused") or []) if str(i).strip()]
    chosen = obj.normalize(payload.get("objectives"))
    reference = payload.get("reference")
    if isinstance(reference, str) and reference.strip():
        try:
            scope, target, restrict = _with_reference(scope, target, restrict, reference)
        except Exception as exc:
            raise HTTPException(400, str(exc))
    if scope not in plans.SCOPES:
        raise HTTPException(400, f"scope must be one of {plans.SCOPES}")
    # ADR-0131: a key left out keeps what the plan says, or what Silica reads
    spec = plans.PlanSpec(
        name, scope, target, by=payload.get("by") or None, restrict=restrict, objectives=chosen,
        # no `refused` (Reword on the notes) keeps the plan's own refusals
        refused=refused if "refused" in payload else None, declared=_declared(payload),
        # roles the form sends back unchanged from the plan are the plan's, not a choice made now
        declared_why=("as the plan says" if payload.get("roles_kept")
                      else "confirmed in the form" if payload.get("roles_confirmed") else "you set it"),
        hours=payload.get("hours"), assessment=payload.get("assessment"),
        guide=str(payload.get("guide") or "") if "guide" in payload else None,
        matched=plans.parse_matched(payload.get("matched")) if "matched" in payload else None,
        reach=str(payload.get("reach") or "relational"))
    if payload.get("preview") or payload.get("cards_only"):
        try:
            res = plans.resolve(spec)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        if payload.get("cards_only"):
            proposed = obj.propose(res["tree"], chosen)
            for c in proposed:
                seen = obj.reads_as(c.get("title", "")) if c["source"] != "gap" else None
                c["reads_as"] = seen if seen and seen != c["intention"] else None
            return {"cards": proposed, "model": any(c.get("wording") == "model" for c in proposed)}
        out = _preview(res, chosen)
        if scope == "steps":
            out.update(sources=res.get("sources") or [], assessment=res.get("assessment") or [],
                       guides=plans.guide_candidates(restrict), order_note=res.get("order_note") or "")
        return out
    cards = payload.get("cards")
    cards = cards if isinstance(cards, list) else None
    if cards is None and payload.get("templates"):
        try:
            # read without `matched`, as this path always has, so Save words the cards it worded
            cards = obj.propose(plans.resolve(replace(spec, matched=None))["tree"], chosen, model=False)
        except Exception as exc:
            raise HTTPException(400, str(exc))
    spec = replace(spec, cards=cards)
    if payload.get("path"):
        plan = plans.find_plan(str(payload["path"]))
        if plan is None:
            raise HTTPException(404, "no such plan")
        try:
            return plans.edit_plan(plan["path"], replace(spec, name=name or plan["name"]))
        except Exception as exc:
            raise HTTPException(400, str(exc))
    if not name:
        raise HTTPException(400, "a plan needs a name")
    try:
        return plans.write_plan(replace(spec, objectives=chosen if "objectives" in payload else None))
    except Exception as exc:
        raise HTTPException(400, str(exc))


def _declared(payload: dict) -> dict[str, str] | None:
    """{label: role} from the form's `materials`, `references` and `unused`
    lists, or None when the form sent none of them (Silica reads the roles,
    or Edit plan keeps the plan's)."""
    from silica.kernel.learn import sources as so

    keys = (("materials", so.MATERIAL), ("references", so.REFERENCE), ("unused", so.UNUSED))
    if not any(k in payload for k, _r in keys):
        return None
    out: dict[str, str] = {}
    for k, role in keys:
        items = payload.get(k) or []
        for label in (items if isinstance(items, list) else str(items).split(" | ")):
            if str(label).strip():
                out[str(label).strip()] = role
    return out


def _with_reference(scope: str, target, restrict: str, reference: str) -> tuple[str, Any, str]:
    """Study and a Reference as today's syllabus scope (ADR-0119): the
    reference's lines are the steps, and what is studied bounds where they
    are looked for. A folder stays a prefix, so a note added to it later is
    found; notes, a topic or a workspace are bounded by the notes they
    resolve to now. No Study: the whole vault, as a syllabus plan always was."""
    from silica.kernel.learn import plans

    if not scope:
        return "steps", reference, ""
    if scope == "folder":
        return "steps", reference, plans.RESTRICT_SEP.join(plans.folder_parts(target))
    paths = plans.resolve_scope(scope, target, restrict=restrict)["paths"]
    if not paths:
        raise ValueError("nothing to study resolves, so the reference has nowhere to be looked for")
    return "steps", reference, plans.RESTRICT_SEP.join(paths)


def _preview(res: dict, chosen: list[str]) -> dict:
    """The preview's counts are the plan's own bases (ADR-0081 §12, fault 7):
    `to review` counts practised notes under the target, as the plan does,
    where the old preview counted every note the schedule held due."""
    from silica.kernel.learn import mastery, objectives as obj, pick
    from silica.kernel.learn import learner, quiz

    entries = quiz.entries()
    view = learner.view(_entries_override=entries)
    rd = mastery.node_readings(res["paths"], view, entries)
    evs_by = reasons.events_of(entries)
    marks = {}
    for pth in res["paths"]:
        evs = evs_by.get(ledger_key(pth), [])
        marks[pth] = "never" if not evs else {"met": "full", "assisted": "half", "missed": "open"}[reasons.outcome(evs[-1])]
    infer = []
    if not chosen:
        for node in res["tree"]:
            leaves = [x for x in node.get("leaves", []) if x]
            if leaves and not node.get("gap"):
                try:
                    infer.append({"part": node["node"], "intention": obj.infer(leaves[0])})
                except Exception as exc:
                    logger.debug("learn: no inferred intention for %s (%s)", node["node"], exc)
    return {"paths": res["paths"], "tree": res["tree"], "gaps": res["gaps"], "neighbours": res.get("neighbours", []),
            "measured": rd["coverage"][0], "never": rd["not_measured"], "due": rd["due"], "marks": marks,
            "parts": sum(1 for n in res["tree"] if n.get("leaves")), "objectives": chosen, "infer": infer,
            "session": pick.DEFAULT_N, "path": None, "cards": []}


@router.post("/plan/budget")
def plan_budget(payload: dict):
    """The hours under a plan's calendar for the form (ADR-0131 point 8),
    from the preview's steps, the date, the hours a day and the assessment,
    without resolving the reference again."""
    from silica.kernel.learn import plans

    steps = [{"hours": s.get("hours"), "unit": str(s.get("unit") or ""), "notes": [""] * int(s.get("notes") or 0)}
             for s in (payload.get("steps") or []) if isinstance(s, dict)]
    parts = plans.parse_assessment(payload["assessment"]) if isinstance(payload.get("assessment"), str) else [
        {"part": str(a.get("part")), "days": a.get("days")} for a in (payload.get("assessment") or []) if isinstance(a, dict)]
    b = plans.budget({"by": payload.get("by"), "hours": plans.hours_of(payload.get("hours")), "steps": steps,
                      "assessment": parts})
    return {"say": b["say"] if b else "", "budget": b}


@router.post("/plan/read")
def read_plan_sentence(payload: dict):
    """The sentence of Create plan in, the four fields out (ADR-0081 §11)."""
    from silica.kernel.learn import plans

    return plans.read_sentence(str(payload.get("sentence") or ""))


_SYLLABUS_NAME = re.compile(r"programm|syllab|indice|esame|exam|outline|course|corso", re.I)


@router.get("/scopes")
def scope_options(q: str = ""):
    """The Study typeahead (ADR-0081 §12): folders, notes, topics and
    syllabus notes matching `q`, each with its kind, target and note count,
    from the note walk and the co-occurrence store."""
    from silica.config import CONFIG
    from silica.kernel.learn import plans

    notes = note_index.meta(Path(CONFIG.vault_path))
    needle = q.strip().casefold()
    folders = plans.folders_of(notes)
    fs = [f for f in folders if needle in f.casefold()] if needle else list(folders)
    fs.sort(key=lambda f: (needle and not f.rstrip("/").rsplit("/", 1)[-1].casefold().startswith(needle), -folders[f], f))
    out = [{"kind": "folder", "target": f, "label": f, "count": folders[f]} for f in fs[:6]]
    if not needle:
        return out
    ns = sorted((p for p, m in notes.items() if noteshape.is_material(m) and needle in Path(p).stem.casefold()),
                key=lambda p: (not Path(p).stem.casefold().startswith(needle), len(p), p))[:6]
    for pth in ns:
        out.append({"kind": "notes", "target": Path(pth).stem, "label": Path(pth).stem, "count": 1, "path": pth})
        # almost every note here is a bulleted list, so only a name that says
        # syllabus offers the note as one; Paste a syllabus covers the rest
        if not _SYLLABUS_NAME.search(Path(pth).stem):
            continue
        listed = sum(1 for ln in items.read_note(pth).splitlines() if re.match(r"\s*(?:[-*]|\d+[.)])\s+\S", ln))
        if listed >= 3:
            out.append({"kind": "steps", "target": Path(pth).stem, "label": Path(pth).stem, "count": listed, "path": pth})
    for t in plans.topics_like(needle):
        out.append({"kind": "topic", "target": t["label"], "label": t["label"], "count": t["count"]})
    return out


@router.post("/revisit")
def revisit(payload: dict):
    from silica.kernel.learn import plans

    plan = plans.find_plan(str(payload.get("plan") or ""))
    if plan is None:
        raise HTTPException(404, "no such plan")
    paths = [str(p) for p in (payload.get("paths") or []) if p]
    plans.set_revisit(plan["path"], paths)
    return {"queued": paths, "plan": plan["path"]}


@router.post("/plan/close")
def close_plan(payload: dict):
    from silica.kernel.learn import plans

    plan = plans.find_plan(str(payload.get("path") or ""))
    if plan is None:
        raise HTTPException(404, "no such plan")
    return plans.close_plan(plan["path"], reopen=bool(payload.get("reopen")))


# ---------------------------------------------------------------- a step's reading (ADR-0132, ADR-0134)

def _step_of(payload_or_args) -> tuple[str, int]:
    plan = str(payload_or_args.get("plan") or "")
    step = payload_or_args.get("step")
    if not plan or not str(step).isdigit():
        raise HTTPException(400, "a reading needs a plan and a step")
    return plan, int(step)


def _reading_shown(plan: str, step: int) -> dict:
    """The reading as the page reads it: a card that failed carries the sentence
    the chat would say, so a limit's reset reaches the reader's clock the same way.
    The raw text goes: a provider's can hold a fragment of the key (ADR-0085 point 5),
    and the job's reached the reader as a toast of litellm's words."""
    from silica.kernel.learn import reading
    from silica.ui.web import failures

    st = reading.state(plan, step)
    for c in st["cards"]:
        if raw := c.pop("error", ""):
            c["failure"] = failures.explain(raw)
    if st["job"] and st["job"].get("error"):
        st["job"]["error"] = failures.explain(st["job"]["error"])["error"]
    return st


@router.get("/reading")
def reading_view(plan: str, step: int):
    """A step's reading as Learn shows it: its cards, whether the notes
    changed since it was written, what the reader wrote, the practice's size,
    and the job writing it, if one runs."""
    try:
        return _reading_shown(plan, int(step))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/reading/write")
def reading_write(payload: dict):
    """Write or rewrite the reading in the background; the page polls GET /reading."""
    from silica.kernel.learn import plans, reading

    plan_path, step = _step_of(payload)
    plan = plans.find_plan(plan_path)
    if plan is None:
        raise HTTPException(404, "no such plan")
    try:
        reading.step_notes(plan, step)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return reading.start(plan["path"], step)


@router.post("/reading/card")
def reading_card(payload: dict):
    """The reader is at card `n`: it and the next one are written if they wait
    (ADR-0134, amendment of 2026-11-06). Answers the reading as it stands."""
    from silica.kernel.learn import reading

    plan_path, step = _step_of(payload)
    n = payload.get("n")
    if not str(n).isdigit():
        raise HTTPException(400, "n is a card's number")
    try:
        reading.reach(plan_path, step, int(str(n)))
        return _reading_shown(plan_path, step)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/reading/answer")
def reading_answer(payload: dict):
    """A card opened, or the answer to its closing question: kept, never
    graded, never in the answer ledger. `section` is the card's number."""
    from silica.kernel.learn import plans, reading

    plan_path, step = _step_of(payload)
    plan = plans.find_plan(plan_path)
    if plan is None:
        raise HTTPException(404, "no such plan")
    kind = str(payload.get("kind") or "")
    section = payload.get("section")
    if kind not in reading.KINDS or not str(section).isdigit():
        raise HTTPException(400, f"kind is one of {reading.KINDS} and section a number")
    return reading.record(reading.reading_path(plan, step), int(str(section)), kind, str(payload.get("text") or ""),
                          unknown=bool(payload.get("unknown")))
