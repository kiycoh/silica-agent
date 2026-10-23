# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Mastery readings (learn-suite D25) and calibration signals (D26).

Pure functions over the ledger, the view and a scope. Several readings, no
composite: retention (R, S), depth (the highest level met without help),
transfer (apply / transfer held), integration (relations answered on),
dated misconception observations, and, per note and objective, whether the
criteria were met without help on a dated question (D27). A note never
asked is `Not measured`, not failed; with no measured notes R and depth are
unknown, not zero. Nothing here reads `rating`: the schedule does, the
readings read `correct`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from silica.kernel.learn.schedule import DUE_R

DEPTH_LABEL = {1: "Facts", 2: "Parts", 3: "Mechanisms", 4: "New contexts"}
CALIBRATION_MIN = 10          # eligible events before any figure shows (G4)
BANDS = (90, 70, 50, 25)
BLIND_SPOT_CONF = 70
FRAGILE_CONF = 25
_STRUCTURE_KINDS = ("structure", "contrast", "contradiction")


def _key(path: str) -> str:
    from silica.kernel.report.quiz import key

    return key(path)


def _ts(e: dict) -> float:
    try:
        return datetime.fromisoformat(str(e.get("ts"))).timestamp()
    except (ValueError, TypeError):
        return 0.0


def unassisted(e: dict) -> bool:
    """The depth criterion: the rubric met with no hint and not told."""
    return bool(e.get("correct")) and not int(e.get("hints") or 0) and not e.get("told")


def _level(e: dict) -> int | None:
    lv = e.get("level")
    return lv if isinstance(lv, int) and not isinstance(lv, bool) and 1 <= lv <= 4 else None


def by_path(entries: list[dict], paths: list[str] | None = None) -> dict[str, list[dict]]:
    """{path: [entries]} in ledger order, keyed like the view; `paths` fixes
    the display spelling and restricts the scope."""
    want = {_key(p): p for p in paths} if paths is not None else None
    out: dict[str, list[dict]] = {p: [] for p in (paths or [])}
    for e in entries:
        k = _key(str(e.get("path") or ""))
        if want is not None:
            if k not in want:
                continue
            out.setdefault(want[k], []).append(e)
        else:
            out.setdefault(str(e.get("path")), []).append(e)
    return out


def depth_of(evs: list[dict], since: str | None = None) -> dict:
    """One note's depth reading: the highest level met without help on a
    dated item; every leveled item as a dated observation beside it; whether
    the credit is self-graded; the two transfer flags."""
    cut = _since_ts(since)
    obs = []
    level = held_at = None
    self_graded = False
    applied = transferred = False
    for e in sorted(evs, key=_ts):
        lv = _level(e)
        if lv is None:
            continue
        t = _ts(e)
        obs.append({"ts": e.get("ts"), "level": lv, "kind": e.get("kind", ""), "q": e.get("q", ""),
                    "correct": bool(e.get("correct")), "hints": int(e.get("hints") or 0),
                    "told": bool(e.get("told")), "grader": e.get("grader", "")})
        if not unassisted(e) or e.get("grader") == "told" or (cut and t < cut):
            continue
        if level is None or lv > level:
            level, held_at, self_graded = lv, e.get("ts"), e.get("grader") == "self"
        elif lv == level and e.get("grader") != "self":
            self_graded = False
        if e.get("kind") == "apply":
            applied = True
        if e.get("kind") == "transfer":
            transferred = True
    return {"level": level, "label": DEPTH_LABEL.get(level) if level else None, "held_at": held_at,
            "self": self_graded, "observations": obs, "applied": applied, "transferred": transferred,
            "measured": bool(evs)}


def _since_ts(since: str | None) -> float | None:
    if not since:
        return None
    try:
        return datetime.strptime(str(since)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def objective_status(evs: list[dict], objective: str, since: str | None = None) -> dict:
    """One note, one objective: `met` is the date its criteria were met
    without help (a hint or "just tell me" never counts, D27), `assisted`
    and `missed` the last dated observations beside it, `asked` how many
    questions served it. `still to verify` until `met` exists."""
    from silica.kernel.learn import objectives as obj

    cut = _since_ts(since)
    out: dict[str, Any] = {"objective": objective, "met": None, "assisted": None, "missed": None, "asked": 0}
    last = "open"   # the latest observation decides the state until a success without help
    for e in sorted(evs, key=_ts):
        if obj.of_entry(e) != objective:
            continue
        t = _ts(e)
        if cut and t < cut:
            continue
        out["asked"] += 1
        if unassisted(e) and e.get("grader") != "told":
            out["met"] = out["met"] or e.get("ts")
        elif e.get("correct"):
            out["assisted"], last = e.get("ts"), "assisted"
        else:
            out["missed"], last = e.get("ts"), "missed"
    out["state"] = "met" if out["met"] else last
    return out


def objectives_met(paths: list[str], objectives: list[str], entries: list[dict] | None = None,
                   since: str | None = None) -> dict[str, dict[str, dict]]:
    """{path: {objective: objective_status}} over a scope; an entry whose
    objective the plan did not choose is still read (it is evidence for
    that intention) but never counted toward the plan's."""
    if entries is None:
        from silica.kernel.report import quiz

        entries = quiz.entries()
    return {p: {o: objective_status(evs, o, since) for o in objectives} for p, evs in by_path(entries, paths).items()}


def depth_by_path(paths: list[str], entries: list[dict] | None = None, since: str | None = None) -> dict[str, dict]:
    if entries is None:
        from silica.kernel.report import quiz

        entries = quiz.entries()
    return {p: depth_of(evs, since) for p, evs in by_path(entries, paths).items()}


def next_ask(evs: list[dict], reach: int = 4) -> int:
    """The level to ask next: one above the last unassisted success, capped
    at reach; after a failure the failed demand again; level 4 only once 3
    held. A pacing heuristic, not a law of learning."""
    ordered = sorted((e for e in evs if _level(e) is not None), key=_ts)
    held = 0
    for e in ordered:
        if unassisted(e) and e.get("grader") != "told":
            held = max(held, _level(e) or 0)  # `ordered` kept only entries with a level
    if ordered and not ordered[-1].get("correct"):
        return max(1, min(reach, _level(ordered[-1]) or 0))
    nxt = min(reach, held + 1)
    if nxt >= 4 and held < 3:
        nxt = 3
    return max(1, nxt)


def integration(paths: list[str], entries: list[dict], relations: list[tuple[str, str]]) -> dict:
    """Relations inside the scope the reader answered on: held when an item
    named both endpoints and met its criterion without help. `total` is the
    scope's relation count; with none, the label says so instead of zero."""
    scope = {_key(p) for p in paths}
    stems = {_key(p).rsplit("/", 1)[-1]: _key(p) for p in paths}
    total = [(a, b) for a, b in relations if _key(a) in scope and _key(b) in scope]
    held: set[tuple[str, str]] = set()
    items = []
    for e in entries:
        if e.get("kind") not in _STRUCTURE_KINDS or _key(str(e.get("path") or "")) not in scope:
            continue
        ends = e.get("endpoints") or []
        keys = [k for x in ends if (k := stems.get(_key(str(x)).rsplit("/", 1)[-1]))]
        if len(keys) < 2:
            continue
        pair = (min(keys[0], keys[1]), max(keys[0], keys[1]))
        items.append({"path": e.get("path"), "q": e.get("q", ""), "held": unassisted(e), "ts": e.get("ts")})
        if unassisted(e):
            held.add(pair)
    known = {(min(_key(a), _key(b)), max(_key(a), _key(b))) for a, b in total}
    n_held = len(held & known) if known else len(held)
    return {"held": n_held, "total": len(known), "items": items,
            "label": "No relations available" if not known else f"{n_held} of {len(known)} relations held"}


# The grader answers MISCONCEPTION in the note's language: "nessuna" on Il
# pensiero (2026-10-06) showed on the plan as "Observations 1" (ADR-0081
# fault 1). A none in any of these words is no observation.
_NONE = {"none", "nessuna", "nessuno", "niente", "n/a", "na", "-", "no"}


def misconceptions(paths: list[str], entries: list[dict]) -> list[dict]:
    """Dated, specific observations, never closed by a later success."""
    out = []
    for p, evs in by_path(entries, paths).items():
        for e in evs:
            m = str(e.get("misconception") or "").strip()
            if m and m.casefold().rstrip(".") not in _NONE:
                out.append({"path": p, "ts": e.get("ts"), "q": e.get("q", ""), "misconception": m,
                            "level": _level(e), "kind": e.get("kind", "")})
    return sorted(out, key=lambda o: str(o["ts"]))


def eligible(e: dict) -> bool:
    """D26: valid, model-graded, unassisted, non-null confidence, not a pretest."""
    return (e.get("grader") == "model" and not int(e.get("hints") or 0) and not e.get("told")
            and e.get("conf") in BANDS and e.get("kind") != "predict")


def suggestions(entries: list[dict], paths: list[str] | None = None) -> dict:
    """Blind spots and fragile answers from `conf` and `correct`, never `rating`."""
    blind, fragile = [], []
    for p, evs in by_path(entries, paths).items():
        for e in evs:
            if not eligible(e):
                continue
            row = {"path": p, "ts": e.get("ts"), "q": e.get("q", ""), "conf": e.get("conf"),
                   "kind": e.get("kind", "")}
            if e["conf"] >= BLIND_SPOT_CONF and not e.get("correct"):
                blind.append({**row, "say": "High confidence; criteria not fully met"})
            elif e["conf"] == FRAGILE_CONF and e.get("correct"):
                fragile.append({**row, "say": "Correct with low confidence"})
    return {"blind_spots": blind, "fragile": fragile}


def calibration(entries: list[dict], paths: list[str] | None = None) -> dict:
    """Brier and signed bias over the eligible events, only at n >= 10."""
    evs = [e for evs in by_path(entries, paths).values() for e in evs if eligible(e)]
    n = len(evs)
    out: dict = {"n": n, "enough": n >= CALIBRATION_MIN, "min": CALIBRATION_MIN}
    bands: dict[str, dict] = {}
    for b in BANDS:
        rows = [e for e in evs if e["conf"] == b]
        if rows:
            bands[str(b)] = {"n": len(rows), "hit": sum(1 for e in rows if e.get("correct")) / len(rows)}
        else:
            bands[str(b)] = {"n": 0, "label": "Not enough evidence"}
    out["bands"] = bands
    if not out["enough"]:
        return out
    ps = [(e["conf"] / 100.0, 1.0 if e.get("correct") else 0.0) for e in evs]
    out["brier"] = sum((p - y) ** 2 for p, y in ps) / n
    out["bias"] = sum(p - y for p, y in ps) / n
    out["bias_say"] = ("Confidence above observed results in this sample" if out["bias"] > 0.05 else
                       "Confidence below observed results in this sample" if out["bias"] < -0.05 else
                       "Confidence close to observed results in this sample")
    return out


def grader_agreement(entries: list[dict], paths: list[str] | None = None) -> dict:
    """Share of model-graded items whose proposal the reader kept. Agreement
    about a scheduling rating, not accuracy.

    Split by modality once any answer was spoken (ADR-0049): a lower share on
    `voice` than on `typed` says the transcription, not the grader prompt, is
    what the reader keeps correcting. A line with no modality predates
    dictation in Learn, so it was typed.
    """
    evs = [e for evs in by_path(entries, paths).values() for e in evs if e.get("grader") == "model" and e.get("rating")]

    def share(sub: list[dict]) -> dict:
        n = len(sub)
        kept = sum(1 for e in sub if e.get("grader_rating") in (None, e.get("rating")))
        return {"n": n, "kept": kept, "share": (kept / n) if n else None}

    out = share(evs)
    voice = [e for e in evs if e.get("modality") == "voice"]
    if voice:
        out["voice"] = share(voice)
        out["typed"] = share([e for e in evs if e.get("modality") != "voice"])
    return out


def node_readings(leaves: list[str], view: dict[str, dict], entries: list[dict], since: str | None = None,
                  objectives: list[str] | None = None) -> dict:
    """Aggregate over DISTINCT leaves: coverage (measured of total), mean R
    over measured leaves with the due count, depth profile, scenario counts,
    and with `objectives` the leaves that met each one (D27). A leaf under
    several children counts once here; child sums need not equal the parent
    and the UI says so."""
    distinct = list(dict.fromkeys(leaves))
    rows: dict[str, Any] = {p: view.get(_key(p)) for p in distinct}
    depth = depth_by_path(distinct, entries, since)
    objs = objectives_met(distinct, objectives, entries, since) if objectives else {}
    measured = [p for p in distinct if rows.get(p) and rows[p].get("attempts")]
    rs = [rows[p]["R"] for p in measured if rows[p].get("R") is not None]
    due = sum(1 for p in measured if rows[p].get("R") is not None and rows[p]["R"] < DUE_R)
    profile: dict[str, int] = {}
    for p in distinct:
        lv = depth[p]["level"]
        if lv:
            profile[str(lv)] = profile.get(str(lv), 0) + 1
    per_leaf = {}
    for p in distinct:
        r = rows.get(p) or {}
        per_leaf[p] = {"R": r.get("R"), "S": r.get("S"), "due": r.get("due"), "attempts": r.get("attempts", 0),
                       "measured": bool(r.get("attempts")), "depth": depth[p]["level"],
                       "depth_label": depth[p]["label"], "self": depth[p]["self"],
                       "applied": depth[p]["applied"], "transferred": depth[p]["transferred"],
                       "last": r.get("last"), "objectives": objs.get(p, {})}
    met_by = {o: sum(1 for p in distinct if objs.get(p, {}).get(o, {}).get("met")) for o in (objectives or [])}
    return {
        "coverage": [len(measured), len(distinct)], "R_mean": (sum(rs) / len(rs)) if rs else None,
        "due": due, "depth_profile": profile, "not_measured": len(distinct) - len(measured),
        "applied": sum(1 for p in distinct if depth[p]["applied"]),
        "transferred": sum(1 for p in distinct if depth[p]["transferred"]),
        "leaves": per_leaf, "met_by_objective": met_by,
        "all_met": sum(1 for p in distinct if objectives and all(objs.get(p, {}).get(o, {}).get("met") for o in objectives)),
    }


def steps_passed(plan: dict, entries: list[dict] | None = None, notes: dict | None = None) -> list[bool]:
    """A step passes when every note in it met every objective of the plan
    without help after the plan was written (D17, D27). The checkbox is
    presentation; this is truth."""
    from silica.kernel.learn.plans import resolve_title

    if entries is None:
        from silica.kernel.report import quiz

        entries = quiz.entries()
    objectives = plan.get("objectives") or ["explain"]
    out = []
    for s in plan.get("steps", []):
        paths = [p for p in (resolve_title(n, notes) for n in s.get("notes", [])) if p]
        if not paths:
            out.append(False)  # a gap or an unresolved step never passes
            continue
        met = objectives_met(paths, objectives, entries, since=plan.get("created"))
        out.append(all(met[p][o]["met"] for p in paths for o in objectives))
    return out


def status_line(plan: str = "") -> str:
    """The readings in one line for `/learn --status [plan]`."""
    from silica.kernel.report import learner, quiz

    rows = learner.view()
    entries = quiz.entries()
    paths = None
    label = "vault"
    if plan:
        from silica.kernel.learn import plans

        p = plans.find_plan(plan)
        if p is None:
            return f"No plan named {plan!r}."
        paths = plans.scope_of(p)["paths"]
        rows = {k: r for k, r in rows.items() if r["path"] in set(paths)}
        label = p["name"]
    measured = [r for r in rows.values() if r["attempts"]]
    due = [r for r in measured if r["R"] is not None and r["R"] < DUE_R]
    cal = calibration(entries, paths)
    cal_s = (f"Brier {cal['brier']:.2f}, bias {cal['bias']:+.2f} over {cal['n']}" if cal["enough"]
             else f"not enough evidence ({cal['n']} of {CALIBRATION_MIN})")
    ag = grader_agreement(entries, paths)
    ag_s = f"{ag['share']:.0%} of {ag['n']}" if ag["share"] is not None else "no model-graded items"
    return (f"{label}: {len(measured)} measured of {len(rows)} · {len(due)} due · "
            f"calibration: {cal_s} · grader agreement: {ag_s}")
