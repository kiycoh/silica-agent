# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Why a note is up, in the reader's words (ADR-0081): one clause per note
derived from its ledger lines, and the words for what those lines say.

A reason is never a model call: the category shows none ("last seen 12 days
ago" on no product's card), and a model would invent causes (D26). Below the
session engine and the plan rows, which all read a note's answers this way.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from silica.kernel.base.notekey import ledger_key

RATING_NAME = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
# LATELY_DAYS and LATELY_SESSIONS bound Missed lately, whichever reaches
# further, so it never becomes a log.
LATELY_DAYS = 14
LATELY_SESSIONS = 3
_COUNT_WORD = {2: "twice", 3: "three times"}


def ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (ValueError, TypeError):
        return None


def day(when) -> str:
    """Oct 6: the ledger's days in the UI's English (ADR-0081's vocabulary), local."""
    if isinstance(when, str):
        when = ts(when)
    if not when:
        return ""
    # the reader's day, not UTC's: the server runs on the reader's machine,
    # and an answer at 00:30 read as the day before
    d = datetime.fromtimestamp(when)
    return f"{d:%b} {d.day}"


def outcome(e: dict) -> str:
    """met | assisted | missed, the receipt's words for one ledger line."""
    if e.get("correct") and not e.get("hints") and not e.get("told"):
        return "met"
    return "assisted" if e.get("correct") else "missed"


def events_of(entries: list[dict]) -> dict[str, list[dict]]:

    out: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("path"):
            out.setdefault(ledger_key(str(e["path"])), []).append(e)
    for evs in out.values():
        evs.sort(key=lambda e: str(e.get("ts")))
    return out


def lately_cutoff(entries: list[dict], now: float) -> float:
    starts: dict[str, float] = {}
    for e in entries:
        t = ts(e.get("ts"))
        if e.get("session") and t:
            starts[e["session"]] = min(starts.get(e["session"], t), t)
    recent = sorted(starts.values(), reverse=True)[:LATELY_SESSIONS]
    return min([now - LATELY_DAYS * 86400] + recent)


def missed_lately(evs_by: dict[str, list[dict]], cutoff: float) -> list[dict]:
    """Notes with a miss since the cutoff whose last answer is not a success
    without help: a note asked again and met has left the group."""
    out: list[dict[str, Any]] = []
    for evs in evs_by.values():
        recent = [e for e in evs if (ts(e.get("ts")) or 0) >= cutoff]
        misses = [e for e in recent if outcome(e) == "missed"]
        if not misses or outcome(evs[-1]) == "met":
            continue
        out.append({"path": str(evs[-1]["path"]), "misses": misses, "last": evs[-1]})
    out.sort(key=lambda m: str(m["misses"][-1].get("ts")), reverse=True)
    return out


def miss_clause(misses: list[dict], last: dict) -> str:
    if last.get("told"):
        return f"told the answer {day(last.get('ts'))}"
    if outcome(last) == "missed" and (last.get("conf") or 0) >= 70:
        return f"sure, but wrong · {day(last.get('ts'))}"
    first, end = day(misses[0].get("ts")), day(misses[-1].get("ts"))
    n = len(misses)
    if n == 1:
        return f"missed {end}"
    times = _COUNT_WORD.get(n, f"{n} times")
    return f"missed {times} {end}" if first == end else f"missed {times}, last {end}"


def reason(row: dict, evs: list[dict], now: float, pool: str = "", misses: list[dict] | None = None) -> str:
    """The one clause of ADR-0081's vocabulary that says why a note is up."""
    if pool == "revisit":
        return "queued by you"
    if pool == "contrast":
        return "two notes kept apart"
    if pool == "contradiction":
        return "two notes disagree"
    if misses:
        return miss_clause(misses, evs[-1] if evs else misses[-1])
    if row.get("stale") and row.get("mtime"):
        return f"edited {day(row['mtime'])}, after you practised it"
    if evs:
        last = evs[-1]
        if last.get("told"):
            return f"told the answer {day(last.get('ts'))}"
        if last.get("hints") and last.get("correct"):
            return f"met with a hint {day(last.get('ts'))}"
        if last.get("correct") and last.get("conf") == 25:
            return f"right, but unsure · {day(last.get('ts'))}"
    due = row.get("due")
    # a note the reader wrote holds a card from its writing (D10), so the
    # picker can hold it due before any answer: the due date is its reason
    if row.get("attempts", 0) == 0 and not evs and not (pool == "due" and due):
        return "not tested yet · " + ("AI-written, a prediction first" if row.get("ai") else "written by you")
    if due:
        return f"waiting since {day(due)}" if due <= now else f"falls due {day(due)}"
    return "practise this note"


def stem(path: str) -> str:
    """A note as the vault names it. items.title_of reads the first heading,
    which in this vault is often a section ("La categorizzazione" for Il
    pensiero.md), so a row would not match the sidebar."""
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def history(path: str, entries: list[dict]) -> list[dict]:
    """Every ledger line of one note, newest first: the log ADR-0081 reads on
    request instead of a permanent mistakes list."""

    key = ledger_key(path)
    out = []
    for e in sorted((e for e in entries if ledger_key(str(e.get("path") or "")) == key),
                    key=lambda e: str(e.get("ts")), reverse=True):
        o = outcome(e)
        word = ("told the answer" if e.get("told") else "met with a hint" if o == "assisted"
                else "sure, but wrong" if o == "missed" and (e.get("conf") or 0) >= 70 else o)
        out.append({"ts": e.get("ts"), "day": day(e.get("ts")), "q": e.get("q", ""), "outcome": o, "word": word,
                    "conf": e.get("conf"), "rating": RATING_NAME.get(e.get("rating") or 0, ""),
                    "line": (e.get("source") or {}).get("line"), "objective": e.get("objective") or "",
                    "session": e.get("session") or ""})
    return out


def reason_sentence(reason: str, history: list[dict], in_plan: str) -> str:
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
