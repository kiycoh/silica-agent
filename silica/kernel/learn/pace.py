# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a plan with a date asks of the days left (D24, ADR-0131 points 8 and 9):
the workload's arithmetic, the day-by-day calendar and the budget under it.
Over a plan as `plans.read_plan` reads it; nothing here reads a note.
"""
from __future__ import annotations

from silica.kernel.learn.reference import step_label


# Answers a note needs by the date: a simulation of the dated picker gave 2.4
# to 2.8 attempts per note by an 18-day exam (research report
# study-plans-and-deadline-scheduling, section 3). "See every note once" said a
# third of the work. Reopen at 20 completed dated plans or 2027-03-31, with
# FINAL_DAYS.
ATTEMPTS_PER_NOTE = 2.5


RECENT_PACE_DAYS = 7    # the reader's own pace: answers a day over the last week


def _count(n: int, word: str) -> str:
    """'1 day', '7 days': the plan's own sentence picks the plural, where "day(s)" made the reader do it."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def pace_of(plan: dict, below: int, n_default: int = 8, entries: list[dict] | None = None) -> dict | None:
    """Workload arithmetic for a deadline (D24): the answers a day the notes
    below reach need before the final pass (pick.FINAL_DAYS), set against the
    reader's own answers a day lately. Never a promise, never a division by zero."""
    import math
    from datetime import date, datetime, timedelta, timezone

    from silica.kernel.learn.pick import FINAL_DAYS, SESSION_MAX

    if not plan.get("by"):
        return None
    try:
        by = date.fromisoformat(str(plan["by"])[:10])
    except ValueError:
        return None
    days_left = max(0, (by - date.today()).days)
    if days_left == 0:
        return {"days_left": 0, "per_day": below, "below": below,
                "say": f"The date has arrived; {_count(below, 'note')} remaining below the target."}
    # a project window the reference states takes the last days, as in the
    # calendar (ADR-0131 point 9): the answers fit the study days before it
    b = budget(plan) if plan.get("steps") else None
    study_days = b["study_days"] if b and b.get("window") else max(1, days_left - FINAL_DAYS)
    answers = math.ceil(below * ATTEMPTS_PER_NOTE)
    per_day = -(-answers // study_days)  # ceil
    size = max(1, min(SESSION_MAX, per_day)) if below else n_default
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_PACE_DAYS)).isoformat()
    recent = sum(1 for e in (entries or []) if str(e.get("ts") or "") >= since) / RECENT_PACE_DAYS
    final = (f", then {b['window']} days for the project" if b and b.get("window")
             else f", then {FINAL_DAYS} days of review" if days_left > FINAL_DAYS else "")
    lately = (f" Lately you answer about {recent:.0f} a day." if entries is not None and recent >= 0.5
              else " You have not answered yet this week." if entries is not None else "")
    return {"days_left": days_left, "per_day": per_day, "below": below, "session": size,
            "answers": answers, "recent_per_day": round(recent, 1),
            "say": (f"About {per_day} answers a day for {_count(study_days, 'day')}{final}: {_count(below, 'note')} at about "
                    f"{ATTEMPTS_PER_NOTE:g} answers each to hold them at the date; a session of {size} covers a "
                    f"day.{lately}")}


# The calendar a plan with a date implies, written by code. Asked for a study
# plan, the local 9B dated it "about 17 weeks" and numbered its lessons at
# random (2026-10-23), and the research behind Learn's plans says a calendar
# is arithmetic a language model should not do (GPT-4 solved 0.6% of
# TravelPlanner; docs research report study-plans-and-deadline-scheduling).
def _left(plan: dict, passed: list[bool] | None = None) -> list[dict]:
    """The steps still to study: neither checked on the plan note nor passed
    in the ledger (ADR-0131 amendment, point 2). A calendar drawn over every
    step started again from step 1 however far the reader had gone."""
    steps = plan.get("steps") or []
    passed = list(passed or []) + [False] * (len(steps) - len(passed or []))
    return [s for s, ok in zip(steps, passed) if not (ok or s.get("checked"))]


def day_by_day(plan: dict, today=None, passed: list[bool] | None = None) -> list[dict] | None:
    """[{day, steps: [(label, notes)], hours: {label: h}}] from today to the
    plan's date: the steps in their order over the study days, each given
    days in proportion to its weight and its notes split evenly over them,
    then the pick.FINAL_DAYS days of review and the date itself. None
    without a date or a step.

    The weight is the reference's own number for the step (its hours, `· 8
    h` on the step line), a step without one the median of the others; with
    no number anywhere, its notes (ADR-0131 point 8). A project the
    reference gives a lead time takes those last days and the study days end
    before them; an oral or a presentation puts a rehearsal on the day before
    the date (point 9). With the plan's `hours:` a day each row says the
    study hours of each step that day. `passed` (the ledger's reading per
    step) and the note's checked boxes take steps out: the calendar is of
    what is left."""
    from datetime import date, timedelta

    if not plan.get("by") or not plan.get("steps"):
        return None
    try:
        by = date.fromisoformat(str(plan["by"])[:10])
    except ValueError:
        return None
    today = today or date.today()
    b = budget(plan, today, passed)
    if b is None:
        return None
    steps = _left(plan, passed)
    days, study, window = b["days"], b["study_days"], b["window"]
    weights = b["weights"]
    total = sum(weights) or 1.0
    per_day = plan.get("hours")
    rows = [{"day": (today + timedelta(days=k)).isoformat(), "steps": [], "hours": {}} for k in range(study)]
    if not steps:
        # every step passed or checked: the study days are review
        for r in rows:
            r["steps"].append(("Review: the notes Memorize shows due", []))
    done = 0.0
    for step, w in zip(steps, weights):
        start, end = done / total * study, (done + w) / total * study
        done += w
        first, last = int(start), max(int(start), min(study - 1, int(end - 1e-9)))
        span = range(first, last + 1)
        notes = list(step.get("notes") or [])
        label = step_label(step)
        # even slices: rounded up, a share left the last days of a span with no
        # note (the course plan's 7 November, one note over two days); with
        # fewer notes than days a day goes on with the note it is on
        for i, k in enumerate(span):
            lo, hi = i * len(notes) // len(span), (i + 1) * len(notes) // len(span)
            rows[k]["steps"].append((label, notes[lo:max(hi, lo + 1)]))
            if per_day:
                # the share of day k this step's span covers, in hours
                share = min(end, k + 1) - max(start, k)
                rows[k]["hours"][label] = round(share * per_day, 1)
    parts = {p["part"] for p in plan.get("assessment") or []}
    for k in range(study, days):
        say = ("Project: the main work; review the notes Memorize shows due" if window
               else "Review: the notes Memorize shows due")
        rows.append({"day": (today + timedelta(days=k)).isoformat(), "steps": [(say, [])], "hours": {}})
    if parts & {"oral", "presentation"} and days >= 2:
        what = "present the project and answer a question per step aloud" if "project" in parts else \
            "answer a question per step aloud"
        rows[days - 1]["steps"].append((f"Rehearse the oral: {what}", []))
    rows.append({"day": by.isoformat(), "steps": [("Exam", [])], "hours": {}})
    return rows


def budget(plan: dict, today=None, passed: list[bool] | None = None) -> dict | None:
    """The arithmetic under the calendar, and what it says about it: the
    days, the study days and the project window, each left step's weight and
    where it came from, the reference's own hours (and those of the steps
    left) against the hours before the date at the plan's hours a day. None
    without a date or a step."""
    from datetime import date

    from silica.kernel.learn.pick import FINAL_DAYS

    every = plan.get("steps") or []
    if not plan.get("by") or not every:
        return None
    steps = _left(plan, passed)
    try:
        by = date.fromisoformat(str(plan["by"])[:10])
    except ValueError:
        return None
    today = today or date.today()
    days = (by - today).days
    if days <= 0:
        return None
    lead = next((p.get("days") for p in plan.get("assessment") or [] if p["part"] == "project" and p.get("days")), None)
    window, says = 0, []
    if lead:
        if lead * 2 <= days:
            window = lead
            says.append(f"The reference gives the project {lead} days before the date: the last {lead} days are its.")
        else:
            says.append(f"The reference gives the project {lead} days before the date, more than half the "
                        f"{days} days left: no days are kept for it.")
    study = days - window if window else (max(1, days - FINAL_DAYS) if days > FINAL_DAYS else days)
    all_known = sorted(h for h in (s.get("hours") for s in every) if h)
    unit = next((s.get("unit") for s in every if s.get("hours")), "") or ""
    if all_known:
        mid = all_known[len(all_known) // 2]
        weights = [s.get("hours") or mid for s in steps]
        whole, left = sum(s.get("hours") or mid for s in every), sum(weights)
        part = f" ({left:g} h in the steps left)" if len(steps) < len(every) else ""
        if unit == "h" and plan.get("hours"):
            have = study * float(plan["hours"])
            says.append(f"The reference's own hours: {whole:g} h{part}; before the study days end, {study} days at "
                        f"{float(plan['hours']):g} h make {have:g} h."
                        + ((" Fewer than the reference's own." if not part else " Fewer than the hours left.")
                           if have < left else ""))
        else:
            says.append(f"Steps weighted by the reference's own numbers ({whole:g}{' ' + unit if unit else ''}).")
    else:
        weights = [max(1, len(s.get("notes") or [])) for s in steps]
        says.append("Weighted by notes: the reference gives no hours.")
    return {"days": days, "study_days": study, "window": window, "weights": weights,
            "unit": unit, "reference_hours": sum(all_known) if all_known else None, "left": len(steps),
            "hours_per_day": plan.get("hours"), "say": " ".join(says)}


def _short(label: str, width: int = 48) -> str:
    """A step's name for a sentence: a syllabus block's whole topic list ran
    past 90 characters and was cut after a comma (2026-10-25)."""
    label = label.rstrip(" ,.;")
    if len(label) <= width:
        return label
    cut = label[:width].rsplit(" ", 1)[0].rstrip(" ,.;")
    return cut + "…"


def today_line(plan: dict, passed: list[bool] | None = None, today=None) -> dict | None:
    """What the plan asks of today and how far the reader is from it
    (ADR-0131 amendment, point 2): today's row of the calendar of the steps
    left, and against the calendar drawn when the plan was made, how many
    steps the first one not done lies behind (or ahead of) the step that
    calendar had for today. Drawn from today only, the row would always
    name the first step not done and never say the reader is behind."""
    from datetime import date

    today = today or date.today()
    live = day_by_day(plan, today, passed)
    if not live:
        return None
    row = next((r for r in live if r["day"] == today.isoformat()), None)
    doing = [(label, (row or {}).get("hours", {}).get(label)) for label, _n in (row or {}).get("steps", [])]
    steps = plan.get("steps") or []
    done = [bool(ok) for ok in (list(passed or []) + [False] * (len(steps) - len(passed or [])))]
    done = [d or bool(s.get("checked")) for d, s in zip(done, steps)]
    first = next((i for i, d in enumerate(done) if not d), None)
    behind, expected = None, ""
    try:
        made = date.fromisoformat(str(plan.get("created") or "")[:10])
    except ValueError:
        made = None
    if made and made < today and first is not None:
        anchor = day_by_day(plan, made) or []
        labels = {step_label(s): i for i, s in enumerate(steps)}
        due = [labels[label] for r in anchor if r["day"] == today.isoformat() for label, _n in r["steps"] if label in labels]
        if due:
            behind, expected = min(due) - first, step_label(steps[min(due)])
        elif anchor and today.isoformat() > max(r["day"] for r in anchor if any(l in labels for l, _n in r["steps"])):
            behind, expected = len(steps) - first, ""
    bits = ["Today: " + "; ".join(f"{_short(label)} ({h:g} h)" if h else _short(label) for label, h in doing) + "."
            if doing else ""]
    if behind and behind > 0 and first is not None:
        bits.append(f"{behind} step{'s' if behind > 1 else ''} behind the calendar drawn when the plan was made"
                    + (f": it had {_short(expected)} for today" if expected else ": it had every step done by now")
                    + f", and {_short(step_label(steps[first]))} is the first not passed.")
    elif behind and behind < 0:
        bits.append(f"{-behind} step{'s' if behind < -1 else ''} ahead of the calendar drawn when the plan was made.")
    return {"steps": doing, "behind": behind, "expected": expected,
            "first": step_label(steps[first]) if first is not None else "", "say": " ".join(b for b in bits if b)}


def day_by_day_table(rows: list[dict], plan: dict | None = None, passed: list[bool] | None = None) -> str:
    """The calendar as a Markdown table, three notes named a day at most, an
    Hours column when the plan has hours a day, and the budget's sentence
    under it when a plan is given."""
    timed = any(r.get("hours") for r in rows)
    out = ["| Day | Step | Notes |" + (" Hours |" if timed else ""), "|---|---|---|" + ("---|" if timed else "")]
    for r in rows:
        for label, notes in r["steps"]:
            named = ", ".join(f"[[{n}]]" for n in notes[:3]) + (f" and {len(notes) - 3} more" if len(notes) > 3 else "")
            h = r.get("hours", {}).get(label)
            out.append(f"| {r['day']} | {label.replace('|', '/')} | {named} |" + (f" {h:g} |" if timed and h else " |" if timed else ""))
    b = budget(plan, passed=passed) if plan else None
    return "\n".join(out) + (f"\n\n{b['say']}" if b and b.get("say") else "")
