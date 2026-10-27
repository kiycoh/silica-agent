# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Learn tab's routes (learn-suite §6.1), an APIRouter mounted on the app.

The engine lives in silica/kernel/learn; these routes hold the one live
session and translate its state for the tab. Answer keys stay server-side,
keyed by the session id; the ledger line is written on `rate`, once per
item; the receipt is rebuilt from the ledger when the live session is gone.

Since 2026-09-07 (ADR-0041) a session knows its origin (a plan, a note, or
the vault), a note session is sized on the note's key points, every item
carries the objective it is evidence for, and `GET /learn/session` lets a
reloaded tab re-attach to the live session instead of meeting a 409.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from silica.kernel.learn.schedule import DUE_R

logger = logging.getLogger(__name__)

# The reason a suggestion is on the list, in the reader's words (refinement §19).
REASON = {"due": "Suggested for review", "unexplored": "Not practised yet", "stale": "Changed since last practice",
          "contrast": "Compare these notes", "contradiction": "Check a disagreement", "revisit": "Queued by you",
          "frontier": "Plan frontier", "note": "Practice this note"}
RATING_NAME = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
MILESTONES = (7.0, 30.0)
SUGGESTIONS_N = 8
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
_SETS = ("skipped", "escalated", "dropped")
_SAVE_LOCK = threading.Lock()


def _reset() -> None:
    """What a restart does to memory; the file stays, as it does on disk."""
    global _LIVE, _LAST
    _LIVE = _LAST = None


def _live_file(vault: str | None = None) -> Path:
    from silica.config import CONFIG
    from silica.kernel.recall import paths

    return paths.index_dir_for(vault or CONFIG.vault_path) / LIVE_FILE


def _save() -> None:
    """Write the live session whole, in the vault it was started in."""
    s = _LIVE
    if s is None:
        return
    from silica.kernel.recall.paths import atomic_write_bytes

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
    from silica.agent import llm

    return llm.meter_scope(s.setdefault("meter", llm.new_meter()))


def _meter_shown(s: dict) -> dict | None:
    """What the session cost, or None where nothing is metered: a laptop
    without SILICA_TOKEN_METER counts nothing, and a zero would be false."""
    from silica.agent import llm

    return dict(s.get("meter") or llm.new_meter()) if llm._METER_ON else None


def _drop_file(s: dict) -> None:
    _live_file(s.get("vault")).unlink(missing_ok=True)


def _load_live() -> None:
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


def _require_same_origin(request: Request) -> None:
    from silica.ui.web.server import _require_same_origin as check

    check(request)


router = APIRouter(prefix="/learn", dependencies=[Depends(_load_live)])
_SAME_ORIGIN = [Depends(_require_same_origin)]


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso_day(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None


def _minutes(questions: int) -> int:
    from silica.kernel.learn.items import MINUTES_PER_QUESTION

    return max(1, round(questions * MINUTES_PER_QUESTION))


# ---------------------------------------------------------------- readings

def _plan_scopes(plan_list: list[dict]) -> dict[str, dict]:
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


def _also_in(path: str, scopes: dict[str, dict]) -> list[dict]:
    return [{"path": k, "name": v["name"]} for k, v in scopes.items() if path in v["paths"]]


def _objectives_for(path: str, scopes: dict[str, dict]) -> list[str]:
    """What a free session on a note looks for: the union of the objectives
    of the plans that include it, else the free defaults."""
    from silica.kernel.learn import objectives as obj

    got = [o for v in scopes.values() if path in v["paths"] for o in v["objectives"]]
    return obj.normalize(got) or list(FREE_OBJECTIVES)


@router.get("")
def learn_home():
    from silica.kernel.learn import mastery, pick, plans
    from silica.kernel.report import learner, quiz

    entries = quiz.entries()
    now = _now()
    rows = learner.view(now_ts=now, _entries_override=entries)
    measured = [r for r in rows.values() if r["attempts"]]
    due_rows = [r for r in rows.values() if r["R"] is not None and r["R"] < DUE_R]
    unexplored = [r for r in rows.values() if r["attempts"] == 0]
    horizon = []
    for d in range(7):
        lo, hi = now + d * 86400, now + (d + 1) * 86400
        n = sum(1 for r in rows.values() if r["due"] and (r["due"] < hi if d == 0 else lo <= r["due"] < hi))
        horizon.append({"day": _iso_day(lo), "notes": n})
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
    scopes = _plan_scopes(plan_list)
    preview = pick.session(n=SUGGESTIONS_N, now_ts=now, _entries_override=entries)
    sug = [_preview_row(it, scopes) for it in preview["items"]]
    counts = {"review": sum(1 for s in sug if s["pool"] == "due"), "first": sum(1 for s in sug if s["pool"] == "unexplored" and not s["stale"]),
              "changed": sum(1 for s in sug if s["stale"]), "other": sum(1 for s in sug if s["pool"] not in ("due", "unexplored"))}
    last = _last_session(entries)
    plan_rows = [_plan_row(p, rows, entries) for p in plan_list]
    groups = _next_groups(entries, rows, preview, plan_rows, scopes, now)
    session_q = len(preview["items"])
    evs_by = _events_of(entries)
    return {
        "next": groups, "mix": _mix(preview["items"], groups),
        "session": {"questions": session_q, "minutes": _minutes(session_q)} if session_q else None,
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
                    "counts": counts, "first_four_minutes": _minutes(sum(s["questions"] for s in sug[:4]))},
        "live": _LIVE["id"] if _LIVE else None, "live_info": _live_info(),
        "session_rows": [_session_row(it, evs_by, now) for it in preview["items"]],
        "recent": _recent(rows, now),
    }


def _session_row(it: dict, evs_by: dict[str, list[dict]], now: float) -> dict:
    """A note of the next session as the home lists it (ADR-0119, D): its
    reason in ADR-0081's words and the mark of its last answer."""
    from silica.kernel.report import learner

    evs = evs_by.get(learner.key_of(it["path"]), [])
    title = " · ".join(_stem(x) for x in it["pair"]) if it.get("pair") else _stem(it["path"])
    mark = "never" if not evs else {"met": "full", "assisted": "half", "missed": "open"}[_outcome(evs[-1])]
    return {"path": it["path"], "title": title, "why": _reason(it, evs, now, pool=it["pool"]), "mark": mark,
            "pair": it.get("pair")}


RECENT_DAYS = 7
RECENT_N = 4


def _recent(rows: dict, now: float) -> list[dict]:
    """The notes changed this week, newest first: what a student who studies
    to their own plan most likely wants to be tested on (t4, messages 3, 7)."""
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.report import learner

    # the view's rows carry no mtime; the note walk does, and says what is material
    meta = learner._notes_meta(Path(CONFIG.vault_path))
    fresh = [(m["mtime"], p) for p, m in meta.items()
             if learner.is_material(m) and (m.get("mtime") or 0) >= now - RECENT_DAYS * 86400
             and learner.key_of(p) in rows]
    fresh.sort(reverse=True)
    return [{"path": p, "title": _stem(p)} for _t, p in fresh[:RECENT_N]]


def _preview_row(it: dict, scopes: dict[str, dict]) -> dict:
    from silica.kernel.learn import items

    pool = "stale" if it.get("stale") else it["pool"]
    detail = []
    if it.get("stale"):
        detail.append("edited " + (datetime.fromtimestamp(it["mtime"], timezone.utc).strftime("%b %d") if it.get("mtime") else "recently"))
    elif it["pool"] == "unexplored":
        detail.append("AI-written" if it.get("ai") else "written by you")
    elif typeof_num(it.get("S")):
        detail.append(f"holds ~{round(it['S'])} d est.")
    points = items.key_points(it["path"])
    objectives = _objectives_for(it["path"], scopes)
    asks = items.ask_plan(points, objectives, len(items.relations_of(it["path"])))
    return {"path": it["path"], "title": items.title_of(it["path"]), "pool": it["pool"], "reason": REASON.get(pool, pool),
            "detail": " · ".join(detail), "kind": it["kind"], "level": it["level"], "folder": it.get("folder", ""),
            "S": it.get("S"), "R": it.get("R"), "ai": it.get("ai"), "stale": bool(it.get("stale")), "pair": it.get("pair"),
            "attempts": it.get("attempts", 0), "last": it.get("last_event"),
            "also_in": _also_in(it["path"], scopes), "points": len(points), "questions": len(asks),
            "minutes": _minutes(len(asks)) if asks else 0, "objectives": objectives}


def typeof_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---------------------------------------------------------------- Next: the queue that says why (ADR-0081)
# A reason is one clause derived from ledger fields, never a model call: the
# category shows none ("last seen 12 days ago" on no product's card), and a
# model would invent causes (D26). LATELY_DAYS and LATELY_SESSIONS bound
# Missed lately, whichever reaches further, so it never becomes a log.
LATELY_DAYS = 14
LATELY_SESSIONS = 3
NEXT_DUE_N = 12
NEXT_NEVER_N = 6
_COUNT_WORD = {2: "twice", 3: "three times"}


def _day(ts) -> str:
    """Oct 6: the ledger's days in the UI's English (ADR-0081's vocabulary), local."""
    if isinstance(ts, str):
        ts = _ts(ts)
    if not ts:
        return ""
    # the reader's day, not UTC's: the server runs on the reader's machine,
    # and an answer at 00:30 read as the day before
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}"


def _outcome(e: dict) -> str:
    """met | assisted | missed, the receipt's words for one ledger line."""
    if e.get("correct") and not e.get("hints") and not e.get("told"):
        return "met"
    return "assisted" if e.get("correct") else "missed"


def _events_of(entries: list[dict]) -> dict[str, list[dict]]:
    from silica.kernel.report import learner

    out: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("path"):
            out.setdefault(learner.key_of(str(e["path"])), []).append(e)
    for evs in out.values():
        evs.sort(key=lambda e: str(e.get("ts")))
    return out


def _lately_cutoff(entries: list[dict], now: float) -> float:
    starts: dict[str, float] = {}
    for e in entries:
        t = _ts(e.get("ts"))
        if e.get("session") and t:
            starts[e["session"]] = min(starts.get(e["session"], t), t)
    recent = sorted(starts.values(), reverse=True)[:LATELY_SESSIONS]
    return min([now - LATELY_DAYS * 86400] + recent)


def _missed_lately(evs_by: dict[str, list[dict]], cutoff: float) -> list[dict]:
    """Notes with a miss since the cutoff whose last answer is not a success
    without help: a note asked again and met has left the group."""
    out: list[dict[str, Any]] = []
    for evs in evs_by.values():
        recent = [e for e in evs if (_ts(e.get("ts")) or 0) >= cutoff]
        misses = [e for e in recent if _outcome(e) == "missed"]
        if not misses or _outcome(evs[-1]) == "met":
            continue
        out.append({"path": str(evs[-1]["path"]), "misses": misses, "last": evs[-1]})
    out.sort(key=lambda m: str(m["misses"][-1].get("ts")), reverse=True)
    return out


def _miss_clause(misses: list[dict], last: dict) -> str:
    if last.get("told"):
        return f"told the answer {_day(last.get('ts'))}"
    if _outcome(last) == "missed" and (last.get("conf") or 0) >= 70:
        return f"sure, but wrong · {_day(last.get('ts'))}"
    first, end = _day(misses[0].get("ts")), _day(misses[-1].get("ts"))
    n = len(misses)
    if n == 1:
        return f"missed {end}"
    times = _COUNT_WORD.get(n, f"{n} times")
    return f"missed {times} {end}" if first == end else f"missed {times}, last {end}"


def _reason(row: dict, evs: list[dict], now: float, pool: str = "", misses: list[dict] | None = None) -> str:
    """The one clause of ADR-0081's vocabulary that says why a note is up."""
    if pool == "revisit":
        return "queued by you"
    if pool == "contrast":
        return "two notes kept apart"
    if pool == "contradiction":
        return "two notes disagree"
    if misses:
        return _miss_clause(misses, evs[-1] if evs else misses[-1])
    if row.get("stale") and row.get("mtime"):
        return f"edited {_day(row['mtime'])}, after you practised it"
    if evs:
        last = evs[-1]
        if last.get("told"):
            return f"told the answer {_day(last.get('ts'))}"
        if last.get("hints") and last.get("correct"):
            return f"met with a hint {_day(last.get('ts'))}"
        if last.get("correct") and last.get("conf") == 25:
            return f"right, but unsure · {_day(last.get('ts'))}"
    due = row.get("due")
    # a note the reader wrote holds a card from its writing (D10), so the
    # picker can hold it due before any answer: the due date is its reason
    if row.get("attempts", 0) == 0 and not evs and not (pool == "due" and due):
        return "not tested yet · " + ("AI-written, a prediction first" if row.get("ai") else "written by you")
    if due:
        return f"waiting since {_day(due)}" if due <= now else f"falls due {_day(due)}"
    return "practise this note"


def _stem(path: str) -> str:
    """A note as the vault names it. items.title_of reads the first heading,
    which in this vault is often a section ("La categorizzazione" for Il
    pensiero.md), so a row would not match the sidebar."""
    return path.rsplit("/", 1)[-1].removesuffix(".md")


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
        return f"{p['next']['title']} · {_miss_clause(missed['misses'], evs[-1])}"
    if not evs:
        return f"{p['next']['title']} · first in order, not tested yet"
    return f"{p['next']['title']} · first in the plan's order"


def _next_groups(entries: list[dict], rows: dict, preview: dict, plan_rows: list[dict], scopes: dict, now: float) -> dict:
    from silica.kernel.report import learner

    evs_by = _events_of(entries)
    lately = _missed_lately(evs_by, _lately_cutoff(entries, now))
    by_miss = {m["path"]: m for m in lately}
    taken: set[str] = set()
    plans_g = []
    for p in plan_rows:
        if p["status"] == "completed":
            continue
        nxt = (p.get("next") or {}).get("path")
        evs = evs_by.get(learner.key_of(nxt), []) if nxt else []
        if nxt:
            taken.add(nxt)
        plans_g.append({"path": p["path"], "name": p["name"],
                        "why": _plan_next_why(p, rows.get(learner.key_of(nxt or "")), evs, now, by_miss.get(nxt or "")),
                        "next": p.get("next"), "open_session": p.get("open_session"), "by": p.get("by"), "group": "plans"})
    missed = []
    for m in lately:
        # the ledger outlives a renamed or deleted note; its row would open a 404
        if m["path"] in taken or learner.key_of(m["path"]) not in rows:
            continue
        taken.add(m["path"])
        k = learner.key_of(m["path"])
        missed.append(_next_row(m["path"], _stem(m["path"]), _reason(rows.get(k, {}), evs_by.get(k, []), now, misses=m["misses"]),
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
        k = learner.key_of(it["path"])
        title = " · ".join(_stem(x) for x in it["pair"]) if it.get("pair") else _stem(it["path"])
        row = _next_row(it["path"], title, _reason(it, evs_by.get(k, []), now, pool=it["pool"]), group, scopes,
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


def _history(path: str, entries: list[dict]) -> list[dict]:
    """Every ledger line of one note, newest first: the log ADR-0081 reads on
    request instead of a permanent mistakes list."""
    from silica.kernel.report import learner

    key = learner.key_of(path)
    out = []
    for e in sorted((e for e in entries if learner.key_of(str(e.get("path") or "")) == key),
                    key=lambda e: str(e.get("ts")), reverse=True):
        o = _outcome(e)
        word = ("told the answer" if e.get("told") else "met with a hint" if o == "assisted"
                else "sure, but wrong" if o == "missed" and (e.get("conf") or 0) >= 70 else o)
        out.append({"ts": e.get("ts"), "day": _day(e.get("ts")), "q": e.get("q", ""), "outcome": o, "word": word,
                    "conf": e.get("conf"), "rating": RATING_NAME.get(e.get("rating") or 0, ""),
                    "line": (e.get("source") or {}).get("line"), "objective": e.get("objective") or "",
                    "session": e.get("session") or ""})
    return out


def _reason_sentence(reason: str, history: list[dict], in_plan: str) -> str:
    """The reason as one sentence for the right pane: what the last session
    on the note established, then what it means now."""
    parts = []
    if history:
        day = history[0]["day"]
        last = [h for h in history if h["day"] == day]
        got = {w: sum(1 for h in last if h["outcome"] == w) for w in ("missed", "assisted", "met")}
        bits = [f"{got['missed']} missed" if got["missed"] else "", f"{got['assisted']} met with help" if got["assisted"] else "",
                f"{got['met']} met" if got["met"] else ""]
        parts.append(f"On {day}, {len(last)} {'answer' if len(last) == 1 else 'answers'}: " + ", ".join(b for b in bits if b) + ".")
    if reason.startswith(("missed", "told", "sure, but wrong")):
        parts.append("A miss schedules nothing by itself: ask again now, or the note comes back "
                     + (f"when {in_plan} reaches it." if in_plan else "when it falls due."))
    elif reason.startswith("waiting since"):
        parts.append(f"The schedule has asked for it since {reason.removeprefix('waiting since ')}; it can wait, and a session now spaces it further.")
    elif reason.startswith("falls due"):
        parts.append(f"It {reason}; practising it early costs nothing.")
    elif reason.startswith("edited"):
        parts.append(f"It was {reason.split(',')[0]}, after the last answer: the questions are on the new text.")
    elif reason.startswith("not tested yet"):
        parts.append("Not tested yet. " + ("Written by the AI, so you predict first, then read." if "AI" in reason
                                            else "Written by you, so a first question checks what writing it left."))
    elif reason.startswith("met with a hint"):
        parts.append("The hint meant it was not counted: one unaided answer settles it.")
    elif reason.startswith("right, but unsure"):
        parts.append("Right, but you said you were guessing: one more answer tells luck from knowing.")
    return " ".join(parts)


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
    from silica.kernel.report.learner import _notes_meta

    # Read once for every title lookup below: steps_passed without it read the
    # vault's notes per step note, 84 epoch walks and 24 s of a 30 s Learn
    # home on a 2,166-note vault (2026-10-23, profiled).
    notes = _notes_meta(plans._vault())
    scoped = plans.scope_of(p, notes=notes)
    node = mastery.node_readings(scoped["paths"], rows, entries, p.get("created"), p["objectives"])
    frontier = scoped["frontier"]
    gaps = [s for s in p["steps"] if not s["notes"]]
    passed = sum(mastery.steps_passed(p, entries, notes=notes)) if p["steps"] else None
    nxt = None
    # a queued note is asked first (pick.session takes revisit before the
    # frontier), so it is where the plan resumes and what its row names
    first = (scoped["revisit"] or frontier or [None])[0]
    if first:
        o = plans.next_objective(p, first, entries)
        nxt = {"path": first, "title": _stem(first), "objective": o, "label": obj.LABEL[o],
               "queued": first in scoped["revisit"],
               "step": next((i + 1 for i, s in enumerate(p["steps"]) if plans.Path(first).stem in s["notes"]), None)}
    return {
        "path": p["path"], "name": p["name"], "scope": p["scope"], "target": p["target"], "reach": p["reach"],
        "reach_label": plans.REACH_LABEL[p["reach_level"]], "by": p.get("by"), "status": p["status"],
        "objectives": p["objectives"], "objective_labels": [obj.LABEL[o] for o in p["objectives"]],
        "objectives_chosen": p["objectives_chosen"],
        "completed": p.get("completed"), "sessions": len(p["sessions"]), "last": p["sessions"][-1]["date"] if p["sessions"] else None,
        "coverage": node["coverage"], "due": node["due"], "R_mean": node["R_mean"], "frontier": [plans.Path(x).stem for x in frontier],
        "next": nxt, "revisit": len(scoped["revisit"]), "gaps": len(gaps), "steps": len(p["steps"]),
        "steps_passed": passed, "all_met": node["all_met"], "met_by_objective": node["met_by_objective"],
        "unit": "steps passed" if p["steps"] else "notes practised",
        "progress": [passed or 0, len(p["steps"])] if p["steps"] else list(node["coverage"]),
        "open_session": bool(_LIVE and _LIVE["plan"] == p["path"]),
        "pace": plans.pace_of(p, len(scoped["paths"]) - node["all_met"], entries=entries),
    }


@router.get("/note")
def note_detail(path: str):
    """What a session on one note would cover (refinement §13, §18): the key
    points with the objective each serves, the question count, the plans
    that include the note. Never a question, never a claim's wording. Since
    ADR-0081 also why it is up, as a clause and a sentence, and its history:
    every ledger line, which is the questions the reader already answered."""
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.learn import items, plans
    from silica.kernel.report import learner, quiz

    text = items.read_note(path)
    if not text:
        raise HTTPException(404, f"no note at {path!r}")
    entries = quiz.entries()
    rows = learner.view(_entries_override=entries)
    row = rows.get(learner.key_of(path)) or {}
    scopes = _plan_scopes(plans.list_plans())
    points = items.key_points(path, text)
    objectives = _objectives_for(path, scopes)
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
    last = max((t for e in entries if learner.key_of(str(e.get("path") or "")) == learner.key_of(path) and (t := e.get("ts"))),
               default=None)
    misses = sum(1 for e in entries if learner.key_of(str(e.get("path") or "")) == learner.key_of(path) and not e.get("correct"))
    now = _now()
    history = _history(path, entries)
    evs = _events_of(entries).get(learner.key_of(path), [])
    lately = next((m for m in _missed_lately({learner.key_of(path): evs}, _lately_cutoff(entries, now))), None) if evs else None
    stale_row = {**row, "mtime": (learner._notes_meta(Path(CONFIG.vault_path)).get(path) or {}).get("mtime")}
    stale_row["stale"] = bool(evs and stale_row["mtime"] and stale_row["mtime"] > (_ts(evs[-1].get("ts")) or now) + 1.0)
    reason = _reason(stale_row, evs, now, misses=lately["misses"] if lately else None)
    also = _also_in(path, scopes)
    return {
        "history": history, "why": reason, "sentence": _reason_sentence(reason, history, also[0]["name"] if also else ""),
        "group": "missed" if lately else "never" if not evs and not row.get("attempts") else "due",
        "path": path, "title": _stem(path), "points": out_points, "questions": max(1, len(asks)) if points else 0,
        "minutes": _minutes(max(1, len(asks))), "atomic": len(points) == 1, "objectives": objectives,
        "also_in": _also_in(path, scopes), "leaves": leaves, "self_grounded": bool(points) and all(c.get("self") for c in points),
        "R": row.get("R"), "S": row.get("S"), "attempts": row.get("attempts", 0), "misses": misses, "last": last,
        "ai": row.get("ai"), "due": row.get("R") is not None and row["R"] < DUE_R,
        "reason": REASON["due"] if (row.get("R") is not None and row["R"] < DUE_R) else REASON["unexplored"] if not row.get("attempts") else "Practice this note",
        "grounded": bool(points) or bool(rels),
        "live": _LIVE["id"] if _LIVE and _LIVE.get("note") == path else None,
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

def _asked(s: dict) -> list[dict]:
    """The session's items without those a placement's walk dropped: a
    step placed on its deepest note never asks its second question."""
    dropped = s.get("dropped") or set()
    return [it for it in s["items"] if it["i"] not in dropped]


def _live_info() -> dict | None:
    if not _LIVE:
        return None
    s = _LIVE
    done = set(s["recorded"]) | set(s["skipped"])
    asked = _asked(s)
    cur = next((it["i"] for it in asked if it["i"] not in done), asked[-1]["i"] if asked else 0)
    return {"id": s["id"], "plan": s["plan"], "note": s.get("note", ""), "origin": s["origin"], "n": len(asked),
            "cur": cur, "recorded": len(s["recorded"]), "skipped": len(s["skipped"]), "started": s["started"],
            "title": (s["items"][cur - 1]["title"] if 0 < cur <= len(s["items"]) else "")}


@router.post("/session", dependencies=_SAME_ORIGIN)
def start_session(payload: dict):
    global _LIVE
    from silica.config import CONFIG
    from silica.kernel.learn import items, pick, plans

    if _LIVE is not None:
        raise HTTPException(409, "a session is live; close it first")
    n = max(1, min(24, int(payload.get("n") or pick.DEFAULT_N)))
    plan_path = str(payload.get("plan") or "")
    note = str(payload.get("note") or "")
    # Raw payload: a list of paths, or a folder prefix turned into one below
    scope: Any = payload.get("scope")
    frontier = revisit = None
    plan = None
    objectives = list(FREE_OBJECTIVES)
    origin = {"kind": "vault", "path": "", "label": "Suggested practice"}
    if plan_path:
        plan = plans.find_plan(plan_path)
        if plan is None:
            raise HTTPException(404, f"no plan at {plan_path!r}")
        scoped = plans.scope_of(plan)
        scope, frontier, revisit = scoped["paths"], scoped["frontier"], scoped["revisit"]
        objectives = plan["objectives"]
        origin = {"kind": "plan", "path": plan["path"], "label": plan["name"]}
    # the practice after a step's reading (ADR-0132, point 5): that step's
    # notes first and only, one question a note up to the cap, notes in turn
    step = payload.get("step")
    stepped = plan is not None and not note and str(step).isdigit()
    if stepped and plan is not None:
        from silica.kernel.learn import reading

        try:
            in_step = reading.step_notes(plan, int(str(step)))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        scope = [p for _t, p in in_step]
        frontier, revisit = list(scope), None
        if not payload.get("n"):
            n = reading.session_size(len(scope))
        origin = {**origin, "label": f"{plan['name']} · {plans._step_label(plan['steps'][int(str(step))])}"}
    teach = bool(payload.get("teach")) and bool(note)
    # a placement (ADR-0135 point 3): asked of a plan before it starts, on offer
    placing = plan is not None and not note and not stepped and bool(payload.get("placement"))
    if placing and plan is not None:
        from silica.kernel.learn import placement

        rows = placement.candidates(plan)
        if not rows:
            raise HTTPException(400, "this plan has no step with a note to ask about")
        picked = {"items": rows, "backfill": [], "amnesty": None, "short": None}
        n = len(rows)
        origin = {**origin, "label": f"{plan['name']} · placement"}
    elif note:
        picked = _note_rows(note, payload.get("points"), objectives if plan else None)
        objectives = picked["objectives"]
        if plan is None:
            origin = {"kind": "note", "path": note, "label": items.title_of(note)}
        # one explanation covers the note, so a teach-back is one item
        n = 1 if teach else max(1, sum(len(r.get("asks") or []) or 1 for r in picked["items"]))
    else:
        if isinstance(scope, str) and scope:
            from pathlib import Path

            from silica.config import CONFIG
            from silica.kernel.report import learner

            prefix = scope.casefold()
            scope = [p for p in learner._notes_meta(Path(CONFIG.vault_path)) if p.casefold().startswith(prefix)]
        later: set[str] = set()
        if plan is not None:
            later = _placed_notes(plan)
            frontier = [p for p in frontier or [] if p not in later] + [p for p in frontier or [] if p in later]
        picked = pick.session(n=n, scope=scope, frontier=frontier, revisit=revisit, later=later,
                              deadline=plan.get("by") if plan else None)
        if plan is not None:
            plans.assign_objectives(plan, picked["items"])
    from silica.agent import llm

    meter = llm.new_meter()
    with llm.meter_scope(meter):
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
    sid = uuid.uuid4().hex
    _LIVE = {"id": sid, "plan": plan["path"] if plan else "", "note": note, "origin": origin, "objectives": objectives,
             "reach": plan["reach_level"] if plan else 4, "n": n, "items": built, "verdicts": {}, "recorded": {},
             "skipped": set(), "teach": {}, "started": _now(), "before": {}, "revisit": revisit or [],
             "mode": "teach" if teach else "questions", "vault": str(CONFIG.vault_path or ""), "meter": meter}
    _save()
    short = None
    if len(built) < n:
        short = picked["short"] or (f"{len(built)} of {n}: the other picked notes carry no line to ask about "
                                    f"(a footnoted line, a Relations bullet, or a prose line of 40+ characters)")
    return _session_payload(_LIVE, amnesty=picked["amnesty"], short=short)


def _note_rows(note: str, only=None, objectives: list[str] | None = None) -> dict:
    """The one row of a note session, bypassing the pools: a note the reader
    asks to practise is practised whether or not the schedule would pick
    it. Its `asks` size the session on the key points (D28)."""
    from silica.kernel.learn import items, plans
    from silica.kernel.report import learner

    rows = learner.view()
    r = rows.get(learner.key_of(note))
    if r is None:
        raise HTTPException(404, f"no note at {note!r}")
    text = items.read_note(note)
    points = items.key_points(note, text)
    if objectives is None:
        objectives = _objectives_for(note, _plan_scopes(plans.list_plans()))
    only_i = [int(x) for x in (only or []) if str(x).isdigit()] or None
    asks = items.ask_plan(points, objectives, len(items.relations_of(note, text)), only=only_i)
    row = {**r, "pool": "note", "kind": "explain", "level": 3, "asks": asks, "objective": objectives[0],
           "stale": False, "folder": note.rsplit("/", 1)[0] if "/" in note else "", "why": "note"}
    return {"items": [row] if (asks or items.relations_of(note, text) or points) else [], "backfill": [],
            "amnesty": None, "short": None if asks else f"{note} has no grounded line to ask about", "due": 0, "unexplored": 0,
            "objectives": objectives}


def _session_payload(s: dict, amnesty=None, short=None) -> dict:
    from silica.kernel.learn import items, objectives as obj

    return {"id": s["id"], "plan": s["plan"], "note": s.get("note", ""), "origin": s["origin"], "n": s["n"],
            "objectives": s["objectives"], "objective_labels": [obj.LABEL[o] for o in s["objectives"]],
            # numbered on the whole list, then filtered: `i` is the item's place in
            # s["items"], which every route resolves it by
            "items": [c for c in items.cue_payload(s["items"]) if c["i"] not in (s.get("dropped") or set())],
            "reasons": [REASON.get("stale" if it.get("stale") else it["pool"], it["pool"]) for it in _asked(s)],
            "minutes": _minutes(len(_asked(s))), "mode": s.get("mode", "questions")}


@router.get("/session")
def live_session(id: str):
    """The live session for a tab that reloaded (refinement, the reload
    trap): cues, what was recorded or skipped, and the verdicts awaiting a
    rating. The unsubmitted answer is the one thing a reload loses."""
    s = _live(id)
    out = _session_payload(s)
    out["done"] = sorted(s["recorded"])
    out["skipped"] = sorted(s["skipped"])
    out["verdicts"] = {str(i): _verdict_payload(v, s["items"][i - 1]) for i, v in s["verdicts"].items() if i not in s["recorded"]}
    out["results"] = {str(i): r["result"] for i, r in s["recorded"].items()}
    # a teach-back in progress: the reader's words and the student's questions
    out["teach"] = {str(i): {"turn": t["turn"], "transcript": t["transcript"]} for i, t in s["teach"].items()}
    return out


def _live(sid: str) -> dict:
    if _LIVE is None or _LIVE["id"] != sid:
        raise HTTPException(404, "no live session with that id")
    return _LIVE


def _item(s: dict, i: Any) -> dict:
    try:
        return s["items"][int(i) - 1]
    except (IndexError, ValueError, TypeError):
        raise HTTPException(404, f"no item {i}")


def _verdict_payload(v: dict, it: dict) -> dict:
    out = {k: v.get(k) for k in ("grade", "rating", "correct", "feedback_line", "rubric_notes", "grader",
                                 "paraphrase", "misconception", "note_defect", "skip", "needs_self_grade", "told")}
    out.update({"claim": it.get("claim", ""), "answer": it.get("answer", ""), "criteria": it.get("criteria", ""),
                "span": {**(it.get("source") or {}), "text": it.get("span", "")}, "path": it["path"],
                "title": it.get("title", ""), "kind": it["kind"], "level": it["level"],
                "objective": it.get("objective"), "point": it.get("point")})
    return out


@router.post("/answer", dependencies=_SAME_ORIGIN)
def answer(payload: dict):
    from silica.kernel.learn import grade

    s = _live(str(payload.get("id") or ""))
    it = _item(s, payload.get("i"))
    conf = payload.get("conf")
    conf = conf if conf in (90, 70, 50, 25) else None
    hints = max(0, int(payload.get("hints") or 0))
    told = bool(payload.get("told"))
    production = str(payload.get("production") or "")
    # Anything but "voice" is typed: the page sends voice once a dictation
    # landed in this answer, however much was typed around it (ADR-0049).
    modality = "voice" if payload.get("modality") == "voice" else "typed"
    if (it.get("grader") == "self" or payload.get("self")) and not told and not payload.get("force_model"):
        # A mechanical item, or the 20 s fallback (§5.1): the evidence shows
        # and the reader rates; a late model verdict cannot replace a rating.
        v = grade._base(it, "self")
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


@router.post("/skip", dependencies=_SAME_ORIGIN)
def skip(payload: dict):
    """Skipping is not a wrong answer (refinement §22): the point stays open,
    nothing is recorded, the receipt says skipped."""
    s = _live(str(payload.get("id") or ""))
    it = _item(s, payload.get("i"))
    if it["i"] in s["recorded"]:
        raise HTTPException(400, "already recorded")
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


@router.post("/rate", dependencies=_SAME_ORIGIN)
def rate(payload: dict):
    with _RATE_LOCK:
        return _rate(payload)


def _rate(payload: dict):
    from silica.kernel.learn import objectives as obj
    from silica.kernel.learn.items import AXIS
    from silica.kernel.report import learner, quiz

    s = _live(str(payload.get("id") or ""))
    it = _item(s, payload.get("i"))
    if it["i"] in s["recorded"]:
        rec = s["recorded"][it["i"]]
        return {**rec["result"], "duplicate": True}
    v = s["verdicts"].get(it["i"])
    if v is None:
        raise HTTPException(400, "answer first")
    if v.get("skip"):
        raise HTTPException(400, "the material invalidated this item; nothing is recorded")
    try:
        rating = int(payload.get("rating", ""))
    except (TypeError, ValueError):
        raise HTTPException(400, "rating 1..4 required")
    rating = max(1, min(4, rating))
    grader = v.get("grader", "self")
    proposed = v.get("rating")
    if grader == "self" or v.get("needs_self_grade"):
        from silica.kernel.learn import grade

        sg = grade.self_grade(it, rating=rating, hints=v.get("hints", 0), told=v.get("told", False))
        correct, rating, grader, proposed = sg["correct"], sg["rating"], "self", None
    else:
        correct = bool(v.get("correct"))
    key = learner.key_of(it["path"])
    before = learner.view().get(key)
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
    if v.get("modality") == "voice":
        entry["modality"] = "voice"
    if s["plan"]:
        entry["plan"] = s["plan"]
    if it.get("endpoints"):
        entry["endpoints"] = it["endpoints"]
    if it.get("placement"):
        entry.update(origin="placement", step=it["placement"]["step"], rank=it["placement"]["rank"])
    written = quiz.record([entry])
    after = learner.view().get(key)
    first = bool(before and before.get("attempts") == 0 and correct)
    result = {
        "recorded": written == 1, "rating": rating, "rating_name": RATING_NAME[rating], "correct": correct,
        "grader": grader, "S_before": before.get("S") if before else None, "S_after": after.get("S") if after else None,
        "next_due": _iso_day(after.get("due")) if after else None,
        "momentum": _momentum(before, after, first) if rating >= 3 else None,
        "objective": entry["objective"], "unassisted": bool(correct and not entry["hints"] and not entry["told"]),
        "escalate": None if correct else _escalate(s, it),
    }
    drop = _walk(s, it, entry)
    if drop:
        result["drop"] = drop
    s["recorded"][it["i"]] = {"entry": entry, "result": result, "ts": _now()}
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


def _placed_notes(plan: dict) -> set[str]:
    """The notes of the steps a placement placed, which a plan session
    takes after every other first attempt."""
    from silica.kernel.learn import placement, reading
    from silica.kernel.report import quiz

    out: set[str] = set()
    for step in placement.placed_steps(plan, quiz.entries()):
        out.update(p for _t, p in reading.step_notes(plan, step))
    return out


def _escalate(s: dict, it: dict) -> dict | None:
    """The escalate rung after a miss (learn.offer): once a note a session,
    logged, and in shadow mode only logged."""
    from pathlib import Path

    from silica.kernel.learn import offer
    from silica.kernel.report import quiz

    if offer.mode() == "off" or it["path"] in s.setdefault("escalated", set()):
        return None
    try:
        entries = quiz.entries()
        why = offer.escalation(it["path"], "", "", [], entries)
        if not why:
            return None
        s["escalated"].add(it["path"])
        shown = offer.mode() == "on"
        offer.log_aid(it["path"], s["id"], why, shown)
        if not shown:
            return None
        prereq = offer.weakest_prerequisite(it["path"])
        return {"why": why, "form": "worked", "prereq": prereq, "prereq_title": Path(prereq).stem if prereq else None}
    except Exception:
        # The aid is an extra: a ledger that does not parse costs it, never the rating.
        logger.warning("learn: escalation skipped", exc_info=True)
        return None


@router.post("/aid", dependencies=_SAME_ORIGIN)
def aid(payload: dict):
    """The aid a rating escalated to, written when the reader asks for it
    (one click: the owner's call of 2026-10-23 on ASSISTments' experiments,
    where aids asked for helped and one that came unasked did not), once,
    and kept for a reload. Reading it records nothing in the ledger; the
    click is logged beside the escalation."""
    from silica.kernel.learn import offer
    from silica.kernel.recall.mindmap import answer_resolver
    from silica.ui.web.server import _linkify

    s = _live(str(payload.get("id") or ""))
    it = _item(s, payload.get("i"))
    rec = s["recorded"].get(it["i"])
    esc = rec["result"].get("escalate") if rec else None
    if not esc:
        raise HTTPException(400, "this answer did not escalate")
    done = s.setdefault("aids", {})
    if it["i"] not in done:
        v = s["verdicts"].get(it["i"]) or {}
        try:
            with _metered(s):
                text = offer.learn_aid(it, esc["why"], v.get("production", ""), esc.get("prereq"))
        except Exception as exc:
            raise HTTPException(502, f"the model did not answer: {exc}")
        if not text:
            raise HTTPException(502, "the model wrote nothing")
        offer.log_aid_opened(it["path"], s["id"])
        # the chat's resolver: a name two notes share links to this item's note
        resolve, _read = answer_resolver([it["path"]])
        done[it["i"]] = {"text": text, "html": _linkify(text, resolve, remote_images=False)}
        _save()
    return done[it["i"]]


MATH_MAX = 200        # formulas one page asks for at once; a session's page holds a few dozen
MATH_CHARS = 2000     # one formula's LaTeX; a note's longest display block is a few hundred


@router.post("/math", dependencies=_SAME_ORIGIN)
def math(payload: dict):
    """The formulas in Learn's text as MathML, converted as the chat's are
    (server._mathml: the browser draws them, and a formula that does not
    convert comes back as written)."""
    from silica.ui.web.server import _mathml

    tex = [str(t)[:MATH_CHARS] for t in (payload.get("tex") or [])][:MATH_MAX]
    display = list(payload.get("display") or [])
    return {"mathml": [_mathml(t, bool(display[i]) if i < len(display) else False) for i, t in enumerate(tex)]}


@router.post("/teach", dependencies=_SAME_ORIGIN)
def teach(payload: dict):
    from silica.kernel.learn import grade, items

    s = _live(str(payload.get("id") or ""))
    it = _item(s, payload.get("i"))
    st = s["teach"].setdefault(it["i"], {"transcript": [], "turn": 0})
    reply = str(payload.get("reply") or "").strip()
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
    try:
        concepts = [c.get("concept", "") for c in (items.__dict__.get("_concepts_of") or (lambda p: []))(it["path"])]
    except Exception:
        concepts = []
    if not concepts:
        concepts = [it.get("title", "")] + [h for h in items.headings_of(items.read_note(it["path"]))[1:4]]
    with _metered(s):
        q = grade.teach_question(it, st["transcript"], concepts)
    st["transcript"].append({"role": "student", "text": q})
    st["turn"] += 1
    _save()
    return {"turn": st["turn"], "question": q, "done": False, "of": grade.TEACH_TURNS}


# ---------------------------------------------------------------- the receipt

def _status_of(it: dict, v: dict, rec: dict | None, skipped: bool) -> str:
    """met | assisted | missed | skipped | open | not_scored, the receipt's
    one word per question (refinement §16)."""
    if skipped:
        return "skipped"
    if v.get("skip"):
        return "not_scored"
    if rec is None:
        return "open"
    e = rec["entry"]
    if e["correct"] and not e["hints"] and not e["told"]:
        return "met"
    if e["correct"]:
        return "assisted"
    return "missed"


def _receipt(s: dict) -> dict:
    from silica.kernel.learn import mastery, objectives as obj, plans
    from silica.kernel.report import learner, quiz

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
            "level_label": mastery.DEPTH_LABEL.get(it["level"]), "q": it.get("q", ""),
            "objective": it.get("objective") or obj.of_kind(it["kind"]), "point": it.get("point"),
            "correct": rec["entry"]["correct"] if rec else None, "marker": marker, "conf": v.get("conf"),
            "rating": rec["entry"]["rating"] if rec else None,
            "rating_name": RATING_NAME.get(rec["entry"]["rating"]) if rec else None,
            "S_before": rec["result"]["S_before"] if rec else None, "S_after": rec["result"]["S_after"] if rec else None,
            "next_due": rec["result"]["next_due"] if rec else None,
            "skip": bool(v.get("skip")), "note_defect": v.get("note_defect") or "", "span": it.get("source") or {},
            "answered": it["i"] in s["verdicts"], "status": _status_of(it, v, rec, it["i"] in s["skipped"]),
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
        "duration": _now() - s["started"], "n": s["n"], "items": rows, "objectives": s["objectives"],
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
        "followup": {"points": open_points, "questions": len(open_points), "minutes": _minutes(len(open_points))} if s.get("note") and open_points else None,
        "empty": not recorded and not s["verdicts"],
        "meter": _meter_shown(s),
        # the same count Today prints: every note with a card below the desired retention
        "next": _next_line(rows), "due_left": sum(1 for r in learner.view().values()
                                                  if r["R"] is not None and r["R"] < DUE_R),
        "plan_line": None,
    }


def _state_words(st: dict) -> str:
    if st["met"]:
        return f"met without help on {_iso_day(_ts(st['met']))}"
    if st["assisted"]:
        return "met with help, not counted"
    if st["missed"]:
        return "missed"
    return "still to verify"


def _ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return None


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
    from silica.kernel.report import learner

    out = []
    try:
        for p in learner.prerequisites_map().get(learner.key_of(path), [])[:3]:
            out.append({"path": p, "why": "a prerequisite the vault names"})
    except Exception:
        pass
    try:
        title = items.title_of(path)
        for r in items.relations_of(path)[:3]:
            other = r["object"] if r["subject"] == title else r["subject"]
            out.append({"path": other, "why": f"{r['label']} in the note's relations"})
    except Exception:
        pass
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


@router.post("/close", dependencies=_SAME_ORIGIN)
def close(payload: dict):
    global _LIVE, _LAST
    from silica.kernel.learn import plans

    sid = str(payload.get("id") or "")
    if _LIVE is None or _LIVE["id"] != sid:
        if _LAST and _LAST["id"] == sid:
            return _LAST
        raise HTTPException(404, "no live session with that id")
    s = _LIVE
    rc = _receipt(s)
    if s["plan"] and rc["recorded"]:
        t = rc["tally"]
        met = " · met: " + ", ".join(f"{d['objective']} {d['met']}" for d in rc["by_objective"]) if rc["by_objective"] else ""
        line = (f"{datetime.now(timezone.utc).date().isoformat()} · {rc['recorded']} items · ratings: "
                f"{t['Good'] + t['Easy']} Good/Easy · {t['Hard']} Hard · {t['Again']} Again{met} · {rc['next'].rstrip('.')} · session: {sid}")
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


@router.get("/receipt")
def receipt(id: str):
    from silica.kernel.learn import mastery, objectives as obj
    from silica.kernel.report import quiz

    if _LAST and _LAST["id"] == id:
        return _LAST
    evs = [e for e in quiz.entries() if e.get("session") == id]
    if not evs:
        raise HTTPException(404, "no recorded events for that session")
    tally = {name: 0 for name in RATING_NAME.values()}
    rows = []
    for e in evs:
        r = e.get("rating") or (3 if e.get("correct") else 1)
        tally[RATING_NAME[r]] += 1
        o = obj.of_entry(e)
        status = "met" if (e.get("correct") and not e.get("hints") and not e.get("told")) else "assisted" if e.get("correct") else "missed"
        rows.append({"path": e["path"], "title": e["path"].rsplit("/", 1)[-1].removesuffix(".md"), "kind": e.get("kind", ""),
                     "level": e.get("level"), "level_label": mastery.DEPTH_LABEL.get(e.get("level") or 0), "q": e.get("q", ""),
                     "objective": o, "objective_label": obj.LABEL[o], "point": {"i": e["point"]} if e.get("point") else None,
                     "correct": e.get("correct"), "conf": e.get("conf"), "rating": r, "rating_name": RATING_NAME[r],
                     "marker": "told" if e.get("told") else "assisted" if e.get("hints") else "self-assessed" if e.get("grader") == "self" else "",
                     "span": e.get("source") or {}, "skip": False, "note_defect": "", "answered": True, "status": status})
    plan = next((e.get("plan") for e in evs if e.get("plan")), "")
    paths = list(dict.fromkeys(e["path"] for e in evs))
    origin = {"kind": "plan", "path": plan, "label": plan.rsplit("/", 1)[-1].removesuffix(".md")} if plan else \
        {"kind": "note", "path": paths[0], "label": rows[0]["title"]} if len(paths) == 1 else {"kind": "vault", "path": "", "label": "Suggested practice"}
    by_obj: dict[str, dict] = {}
    for r in rows:
        d = by_obj.setdefault(r["objective"], {"objective": r["objective"], "label": r["objective_label"], "asked": 0, "met": 0, "assisted": 0, "missed": 0, "skipped": 0})
        d["asked"] += 1
        d[r["status"]] += 1
    return {"id": id, "plan": plan, "note": paths[0] if len(paths) == 1 else "", "origin": origin, "items": rows, "recorded": len(rows),
            "tally": tally, "moved": [], "revisit": [], "defects": [], "from_ledger": True, "by_objective": list(by_obj.values()),
            "objectives": list(by_obj), "objective_labels": [d["label"] for d in by_obj.values()], "points": [],
            "tally3": {"answered": len(rows), "of": len(rows), "skipped": 0, "met": sum(1 for r in rows if r["status"] == "met"),
                       "points": len(rows), "assisted": sum(1 for r in rows if r["status"] == "assisted"),
                       "missed": sum(1 for r in rows if r["status"] == "missed"),
                       "with_evidence": sum(1 for d in by_obj.values() if d["met"]), "objectives": len(by_obj)},
            "established": "Recorded events only: verdict text, answers and invalid items are not kept in the ledger.",
            "next": "", "ts": evs[0].get("ts"), "plan_line": None, "followup": None, "empty": False}


# ---------------------------------------------------------------- plans

@router.get("/plans")
def list_plans():
    from silica.kernel.learn import plans
    from silica.kernel.report import learner, quiz

    entries = quiz.entries()
    rows = learner.view(_entries_override=entries)
    return [_plan_row(p, rows, entries) for p in plans.list_plans()]


@router.get("/plan")
def plan_detail(path: str):
    """The plan's report, plus ADR-0081's two blocks: Lately (its misses as
    rows, a misconception line joined to its row) and After this (three
    in-scope rows with reasons)."""
    from silica.kernel.learn import plans
    from silica.kernel.report import learner, quiz

    p = plans.find_plan(path)
    if p is None:
        raise HTTPException(404, f"no plan at {path!r}")
    entries = quiz.entries()
    view = learner.view(_entries_override=entries)
    rep = plans.plan_report(p, entries=entries, view=view)
    rep["open_session"] = bool(_LIVE and _LIVE["plan"] == p["path"])
    from silica.kernel.learn import placement

    rep["placed"] = placement.placed_steps(p, entries)
    rep["live_info"] = _live_info() if rep["open_session"] else None
    now = _now()
    scoped = set(rep["paths"])
    evs_by = {k: v for k, v in _events_of(entries).items() if v and str(v[-1]["path"]) in scoped}
    mis = {m["path"]: m["misconception"] for m in rep.get("misconceptions") or []}
    lately = []
    for m in _missed_lately(evs_by, _lately_cutoff(entries, now)):
        k = learner.key_of(m["path"])
        lately.append({"path": m["path"], "title": _stem(m["path"]), "questions": len(m["misses"]),
                       "why": _reason(view.get(k, {}), evs_by.get(k, []), now, misses=m["misses"]),
                       "misconception": mis.get(m["path"], "")})
    rep["lately"] = lately
    skip = {x["path"] for x in lately} | {(rep.get("next") or {}).get("path")}
    order = {pth: i for i, pth in enumerate(rep["paths"])}
    cands = [pth for pth in rep["paths"] if pth not in skip]
    cands.sort(key=lambda pth: (not (view.get(learner.key_of(pth), {}).get("attempts") and (view.get(learner.key_of(pth), {}).get("R") or 1) < DUE_R),
                                bool(view.get(learner.key_of(pth), {}).get("attempts")), order[pth]))
    rep["after"] = [{"path": pth, "title": _stem(pth),
                     "why": _reason(view.get(learner.key_of(pth), {}), evs_by.get(learner.key_of(pth), []), now)}
                    for pth in cands[:3]]
    return rep


@router.get("/map")
def learn_map():
    """What you know as marks (ADR-0081 §7): one row per folder, one cell per
    note. ◌ never asked, ○ asked and open, ◐ met with help, ● met without
    help, from the note's last answer; no model call, no new store."""
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.learn import plans
    from silica.kernel.report import learner, quiz

    entries = quiz.entries()
    now = _now()
    notes = learner._notes_meta(Path(CONFIG.vault_path))
    rows = learner.view(now_ts=now, _notes_override=notes, _entries_override=entries)
    evs_by = _events_of(entries)
    cutoff = _lately_cutoff(entries, now)
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
        if not learner.is_material(meta):
            continue
        k = learner.key_of(pth)
        row, evs = rows.get(k) or {}, evs_by.get(k, [])
        mark = "never" if not evs else {"met": "full", "assisted": "half", "missed": "open"}[_outcome(evs[-1])]
        lately = _missed_lately({k: evs}, cutoff) if evs else []
        why = _reason({**row, "attempts": len(evs)}, evs, now, misses=lately[0]["misses"] if lately else None) if evs else "never asked"
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


@router.post("/plan", dependencies=_SAME_ORIGIN)
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
    # ADR-0131: the collections' roles the reader set, study hours a day, the
    # assessment's parts ("none" clears them), a guide for the steps' depth;
    # a key left out keeps what the plan says, or what Silica reads
    declared = _declared(payload)
    # roles the form sends back unchanged from the plan are the plan's, not a choice made now
    declared_why = ("as the plan says" if payload.get("roles_kept")
                    else "confirmed in the form" if payload.get("roles_confirmed") else "you set it")
    hours = payload.get("hours") if "hours" in payload else None
    assessment = payload.get("assessment") if "assessment" in payload else None
    guide = str(payload.get("guide") or "") if "guide" in payload else None
    matched = plans.parse_matched(payload.get("matched")) if "matched" in payload else None
    reference = payload.get("reference")
    if isinstance(reference, str) and reference.strip():
        try:
            scope, target, restrict = _with_reference(scope, target, restrict, reference)
        except Exception as exc:
            raise HTTPException(400, str(exc))
    if scope not in plans.SCOPES:
        raise HTTPException(400, f"scope must be one of {plans.SCOPES}")
    if payload.get("preview") or payload.get("cards_only"):
        try:
            res = plans.resolve_scope(scope, target, restrict=restrict, refused=refused, declared=declared,
                                      declared_why=declared_why, matched=matched)
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
            cards = obj.propose(plans.resolve_scope(scope, target, restrict=restrict, refused=refused,
                                                    declared=declared)["tree"], chosen, model=False)
        except Exception as exc:
            raise HTTPException(400, str(exc))
    if payload.get("path"):
        plan = plans.find_plan(str(payload["path"]))
        if plan is None:
            raise HTTPException(404, "no such plan")
        try:
            # no `refused` (Reword on the notes) keeps the plan's own refusals
            return plans.edit_plan(plan["path"], name or plan["name"], scope, target, by=payload.get("by") or None,
                                   restrict=restrict, objectives=chosen, cards=cards,
                                   refused=refused if "refused" in payload else None, declared=declared,
                                   hours=hours, assessment=assessment, guide=guide, declared_why=declared_why,
                                   matched=matched)
        except Exception as exc:
            raise HTTPException(400, str(exc))
    if not name:
        raise HTTPException(400, "a plan needs a name")
    try:
        out = plans.write_plan(name, scope, target, by=payload.get("by") or None, restrict=restrict,
                               objectives=chosen if "objectives" in payload else None,
                               reach=str(payload.get("reach") or "relational"), cards=cards, refused=refused,
                               declared=declared, hours=hours, assessment=assessment, guide=guide or "",
                               declared_why=declared_why, matched=matched)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return out


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
    from silica.kernel.learn import mastery, objectives as obj
    from silica.kernel.report import learner, quiz

    entries = quiz.entries()
    view = learner.view(_entries_override=entries)
    rd = mastery.node_readings(res["paths"], view, entries)
    evs_by = _events_of(entries)
    marks = {}
    for pth in res["paths"]:
        evs = evs_by.get(learner.key_of(pth), [])
        marks[pth] = "never" if not evs else {"met": "full", "assisted": "half", "missed": "open"}[_outcome(evs[-1])]
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
            "session": 8, "path": None, "cards": []}


@router.post("/plan/budget", dependencies=_SAME_ORIGIN)
def plan_budget(payload: dict):
    """The hours under a plan's calendar for the form (ADR-0131 point 8),
    from the preview's steps, the date, the hours a day and the assessment,
    without resolving the reference again."""
    from silica.kernel.learn import plans

    steps = [{"hours": s.get("hours"), "unit": str(s.get("unit") or ""), "notes": [""] * int(s.get("notes") or 0)}
             for s in (payload.get("steps") or []) if isinstance(s, dict)]
    parts = plans.parse_assessment(payload["assessment"]) if isinstance(payload.get("assessment"), str) else [
        {"part": str(a.get("part")), "days": a.get("days")} for a in (payload.get("assessment") or []) if isinstance(a, dict)]
    b = plans.budget({"by": payload.get("by"), "hours": plans._hours_of(payload.get("hours")), "steps": steps,
                      "assessment": parts})
    return {"say": b["say"] if b else "", "budget": b}


@router.post("/plan/read", dependencies=_SAME_ORIGIN)
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
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.learn import items, plans
    from silica.kernel.report import learner

    notes = learner._notes_meta(Path(CONFIG.vault_path))
    needle = q.strip().casefold()
    folders = plans.folders_of(notes)
    fs = [f for f in folders if needle in f.casefold()] if needle else list(folders)
    fs.sort(key=lambda f: (needle and not f.rstrip("/").rsplit("/", 1)[-1].casefold().startswith(needle), -folders[f], f))
    out = [{"kind": "folder", "target": f, "label": f, "count": folders[f]} for f in fs[:6]]
    if not needle:
        return out
    ns = sorted((p for p, m in notes.items() if learner.is_material(m) and needle in Path(p).stem.casefold()),
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


@router.post("/revisit", dependencies=_SAME_ORIGIN)
def revisit(payload: dict):
    from silica.kernel.learn import plans

    plan = plans.find_plan(str(payload.get("plan") or ""))
    if plan is None:
        raise HTTPException(404, "no such plan")
    paths = [str(p) for p in (payload.get("paths") or []) if p]
    plans.set_revisit(plan["path"], paths)
    return {"queued": paths, "plan": plan["path"]}


@router.post("/plan/close", dependencies=_SAME_ORIGIN)
def close_plan(payload: dict):
    from silica.kernel.learn import plans

    plan = plans.find_plan(str(payload.get("path") or ""))
    if plan is None:
        raise HTTPException(404, "no such plan")
    return plans.close_plan(plan["path"], reopen=bool(payload.get("reopen")))


# ---------------------------------------------------------------- a step's reading (ADR-0132)

def _step_of(payload_or_args) -> tuple[str, int]:
    plan = str(payload_or_args.get("plan") or "")
    step = payload_or_args.get("step")
    if not plan or not str(step).isdigit():
        raise HTTPException(400, "a reading needs a plan and a step")
    return plan, int(step)


@router.get("/reading")
def reading_view(plan: str, step: int):
    """A step's reading as Learn shows it: its sections, whether the notes
    changed since it was written, what the reader wrote, the practice's size,
    and the job writing it, if one runs."""
    from silica.kernel.learn import reading

    try:
        return reading.state(plan, int(step))
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/reading/write", dependencies=_SAME_ORIGIN)
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


@router.post("/reading/answer", dependencies=_SAME_ORIGIN)
def reading_answer(payload: dict):
    """A guess before a section, or the reader's own words after it: kept,
    never graded, never in the answer ledger."""
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
