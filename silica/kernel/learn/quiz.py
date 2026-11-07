# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Quiz outcome log — which notes the reader actually failed to recall.

The signal the attention list was missing. File mtime answers "when did
anyone last touch this note"; a graded answer answers "when was this last
tested, and how did it go". Append-only JSONL under the vault's index dir:
derived state, deletable, rebuilt by quizzing again.

ponytail: one line per graded question, full scan on read. A reader who
answers 20 questions a day writes 7k lines a year; index it when a scan
shows up in a profile, not before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from silica.kernel.base.notekey import ledger_key
from silica.kernel.learn import store

logger = logging.getLogger(__name__)


def log_path() -> Path:
    from silica.config import CONFIG
    from silica.kernel.base import home

    return home.index_dir_for(CONFIG.vault_path) / "quiz.jsonl"


def record(results: list[dict]) -> int:
    """Append graded answers. Each entry: {"path": <note>, "correct": bool},
    plus optional evidence fields: "concepts" (the 1-3 concepts the question
    tested, as the asking model named them — resolved to the index keyspace at
    read time, never here), "q" (the question text), "anchor" (a "#heading" or
    existing "#^id" citation into the note). Absent fields are omitted, so a
    legacy-shaped call writes a legacy-shaped line.

    Returns the number of entries written.
    """
    now = datetime.now(timezone.utc).isoformat()
    lines = []
    for r in results:
        path = str((r or {}).get("path") or "").strip()
        if not path:
            continue  # a question with no source note carries no recall signal
        rec: dict = {"ts": now, "path": path, "correct": bool(r.get("correct"))}
        concepts = [str(c).strip() for c in (r.get("concepts") or []) if str(c).strip()]
        if concepts:
            rec["concepts"] = concepts
        for k in ("q", "anchor", "kind", "grader", "misconception", "session", "plan", "modality"):
            v = str(r.get(k) or "").strip()
            if v:
                rec[k] = v
        _v2_fields(r, rec)
        lines.append(rec)
    if not lines:
        return 0
    store.append_rows(log_path(), lines)
    return len(lines)


# learn-suite D14: fixed labels, for data stability
CONFIDENCE = (("certain", 90), ("pretty sure", 70), ("half unsure", 50), ("guessing", 25))
BANDS = tuple(band for _, band in CONFIDENCE)


def _v2_fields(r: dict, rec: dict) -> None:
    """The learn-suite D11 fields, each written only when valid and present.

    `rating` is the scheduling decision (1..4) and `correct` the rubric; the
    two are logged as given and never derived from each other, because a
    complete assisted answer is correct AND receives Hard. `conf` is one of
    the four bands or null (skipped): a null line is a real event that simply
    does not count toward calibration, so `None` is written, not dropped.
    """
    rating = r.get("rating")
    if isinstance(rating, int) and not isinstance(rating, bool) and 1 <= rating <= 4:
        rec["rating"] = rating
    grader_rating = r.get("grader_rating")
    if isinstance(grader_rating, int) and not isinstance(grader_rating, bool) and 1 <= grader_rating <= 4:
        rec["grader_rating"] = grader_rating
    level = r.get("level")
    if isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 4:
        rec["level"] = level
    if "conf" in r:
        conf = r.get("conf")
        if conf is None or conf in BANDS:
            rec["conf"] = conf
    src = r.get("source")
    if isinstance(src, dict) and str(src.get("leaf") or "").strip():
        rec["source"] = {"leaf": str(src["leaf"]).strip(), "line": int(src.get("line") or 0)}
    hints = r.get("hints")
    if isinstance(hints, int) and not isinstance(hints, bool) and hints >= 0:
        rec["hints"] = hints
    for k in ("told", "paraphrase"):
        if isinstance(r.get(k), bool):
            rec[k] = r[k]
    # learn-suite D27/D28: what the line is evidence for, and which point of
    # the note it covered; `endpoints` is what integration() reads back.
    objective = str(r.get("objective") or "").strip().lower()
    if objective:
        rec["objective"] = objective
    point = r.get("point")
    if isinstance(point, int) and not isinstance(point, bool) and point > 0:
        rec["point"] = point
    ends = r.get("endpoints")
    if isinstance(ends, list) and len(ends) == 2 and all(str(x).strip() for x in ends):
        rec["endpoints"] = [str(x).strip() for x in ends]
    # ADR-0135: a placement's line and its place in the walk (point 3), a
    # retest (point 4), the hints shown in order (point 5), the grader's class
    # of a miss (point 6). Each optional, so an older reader sees the line it knew.
    origin = str(r.get("origin") or "").strip()
    if origin:
        rec["origin"] = origin
    for k in ("step", "rank"):
        v = r.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            rec[k] = v
    if r.get("retest") is True:
        rec["retest"] = True
    rungs = r.get("rungs")
    if isinstance(rungs, list) and rungs and all(isinstance(x, str) and x.strip() for x in rungs):
        rec["rungs"] = [x.strip() for x in rungs]
    miss = str(r.get("miss") or "").strip().lower()
    if miss:
        rec["miss"] = miss


def entries(path: Path | None = None) -> list[dict]:
    """Every graded answer as logged, oldest first — the raw event stream the
    learner view derives from. Optional fields pass through untouched; a line
    missing path or correct carries no event and is skipped, like torn lines.
    `path` reads another vault's ledger (the session hook, outside CONFIG).
    """
    p = path if path is not None else log_path()
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("quiz: unreadable log %s (%s)", p, exc)
        return []
    out: list[dict] = []
    for rec in store.parse_rows(text):
        try:
            rec["path"] = str(rec["path"])
            rec["correct"] = bool(rec["correct"])
        except (KeyError, TypeError):
            continue  # a line with no path or verdict carries no event
        out.append(rec)
    out.sort(key=lambda e: str(e.get("ts") or ""))
    return out


def stats() -> dict[str, dict]:
    """{key: {"path", "misses", "correct", "last"}} over the whole log."""
    p = log_path()
    if not p.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("quiz: unreadable log %s (%s)", p, exc)
        return {}
    for rec in store.parse_rows(text):
        try:
            path = str(rec["path"])
        except (KeyError, TypeError):
            continue  # a line with no path carries no event
        s = out.setdefault(ledger_key(path), {"path": path, "misses": 0, "correct": 0, "last": ""})
        s["correct" if rec.get("correct") else "misses"] += 1
        ts = str(rec.get("ts") or "")
        if ts > s["last"]:
            s["last"] = ts
            s["path"] = path  # last spelling wins: notes get renamed
    return out
