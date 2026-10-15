# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The picker (learn-suite D13): four pools, one interleave rule, an amnesty.

A session of `n` items draws half from the due pool (R below the desired
retention), a quarter from the unexplored pool, at most one contrast pair the
dedup gate kept apart and at most one contradiction the vault registers,
then backfills from due and unexplored. Nothing here reads a note body: the
generators do, after the pick. Rows are the learner view's, so the surfaces
(REPL tool, GUI routes) agree on what "due" means.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from silica.kernel.learn.items import LEVEL_OF
from silica.kernel.learn.schedule import DUE_R, events_by_key

logger = logging.getLogger(__name__)

DEFAULT_N = 8
AMNESTY_FACTOR = 2   # due > 2n: one calm line, the capped session, never the backlog
# engram's copy (MIT): a break is not a debt.
AMNESTY_LINE = "{due} due after a break is spacing doing its job, not a debt. This session takes {n}; the rest waits."


def _folder(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def contrast_pairs(notes: dict[str, dict]) -> list[tuple[str, str]]:
    """Pairs the dedup gate scored near and the reader kept apart: the
    `> [!info] Related: [[X]] (judged distinct...)` traces in note bodies and
    the `review: near_title ... path='...'` frontmatter flag.

    ponytail: the contrast pool reads the dedup traces and the near-title
    flag; a cosine neighbour that was never a dedup candidate is invisible to
    it. Kill by 2026-12-31 if the ledger shows zero `contrast` events.
    """
    from silica.config import CONFIG
    from silica.kernel.write import frontmatter
    from silica.kernel.write.templates import parse_related_traces

    vault = Path(CONFIG.vault_path)
    by_title = {Path(p).stem.casefold(): p for p in notes}
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(a: str, b: str) -> None:
        key = (min(a, b), max(a, b))
        if a != b and key not in seen and a in notes and b in notes:
            seen.add(key)
            out.append((a, b))

    for path in notes:
        try:
            text = (vault / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        data, _raw, body = frontmatter.split(text)
        for cand, _why in parse_related_traces(body):
            other = by_title.get(cand.split("|")[0].split("#")[0].strip().casefold())
            if other:
                add(path, other)
        m = re.search(r"path='([^']+)'", str((data or {}).get("review") or ""))
        if m and m.group(1) in notes:
            add(path, m.group(1))
    return out


def conflicts(notes: dict[str, dict]) -> list[str]:
    """Notes the vault registers as in conflict: `contested: true`, a
    `contradictions:` list, or a source that moved on without them
    (provenance drift). ponytail: pool D; kill by 2026-12-31 with zero
    `contradiction` events in the ledger."""
    from silica.config import CONFIG
    from silica.kernel.write import frontmatter
    from silica.kernel.write.provenance import drifted_notes, note_key

    vault = Path(CONFIG.vault_path)
    out: list[str] = []
    for path in notes:
        try:
            data, _raw, _body = frontmatter.split((vault / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if data and (data.get("contested") or data.get("contradictions")):
            out.append(path)
    try:
        drifted = {note_key(n) for n, _src in drifted_notes(vault_path=str(vault))}
    except Exception:
        drifted = set()
    if drifted:
        out += [p for p in notes if note_key(p) in drifted and p not in out]
    return out


def _stale(row: dict) -> bool:
    """Edited after its last rating: the evidence is about other text."""
    last = row.get("last_event")
    mtime = row.get("mtime")
    return bool(last and mtime and mtime > last + 1.0)


def session(
    n: int = DEFAULT_N,
    *,
    now_ts: float | None = None,
    scope: list[str] | None = None,
    frontier: list[str] | None = None,
    revisit: list[str] | None = None,
    _notes_override: dict | None = None,
    _entries_override: list | None = None,
    _pairs_override: list[tuple[str, str]] | None = None,
    _conflicts_override: list[str] | None = None,
    _planned_override: set[str] | None = None,
    _store=None,
) -> dict:
    """Pick the rows of one session. Returns {items, due, unexplored,
    amnesty, short}; each item carries path, title-less row fields, pool,
    kind, level, stale, and `pair` for a contrast item.

    `scope` restricts every pool to those paths (a plan's, D24); without one,
    a never-practised note outside every active plan is left out
    (learner.off_plan_dropped). `frontier` names the paths that come first
    inside it; `revisit` is the reader's own queue (D26) and precedes
    everything.
    """
    from datetime import datetime, timezone

    from silica.kernel.report import learner, quiz

    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    entries = _entries_override if _entries_override is not None else quiz.entries()
    notes = _notes_override
    if notes is None:
        from silica.config import CONFIG

        notes = learner._notes_meta(Path(CONFIG.vault_path))
    rows = learner.view(now_ts=now, _notes_override=notes, _entries_override=entries)
    rows = {k: r for k, r in rows.items() if learner.is_material(notes.get(r["path"], {}))}
    last_event: dict[str, float] = {}
    for k, evs in events_by_key(entries).items():
        last_event[k] = max(ts for ts, _r, _ok in evs)
    for k, r in rows.items():
        r["mtime"] = notes.get(r["path"], {}).get("mtime")
        r["last_event"] = last_event.get(k)
        r["stale"] = _stale(r)
        r["folder"] = _folder(r["path"])

    if scope is not None:
        allowed = {p for p in scope}
        rows = {k: r for k, r in rows.items() if r["path"] in allowed}
    else:
        from silica.kernel.learn.plans import planned_paths

        planned = _planned_override if _planned_override is not None else planned_paths()
        rows = learner.off_plan_dropped(rows, planned)

    def why(r: dict) -> str:
        if r["stale"]:
            return "unexplored"  # changed content: a first attempt, not a lapse
        if r["R"] is not None and r["R"] < DUE_R:
            return "due"
        if r["attempts"] == 0:
            return "unexplored"
        return "known"

    for r in rows.values():
        r["why"] = why(r)
    gain = learner.gain_fn(entries, _store)
    due = sorted((r for r in rows.values() if r["why"] == "due"),
                 key=lambda r: (round(r["R"], 1), -gain(r), r["path"]))
    unexplored = sorted((r for r in rows.values() if r["why"] == "unexplored"),
                        key=lambda r: (not r["ai"], -gain(r), r["path"]))

    by_path = {r["path"]: r for r in rows.values()}
    taken: set[str] = set()
    picked: list[dict] = []

    def take(r: dict, pool: str, kind: str | None = None, **extra) -> None:
        if r["path"] in taken:
            return
        taken.add(r["path"])
        k = kind or ("predict" if (r["ai"] and r["attempts"] == 0) or r["stale"] else "recall")
        picked.append({**r, "pool": pool, "kind": k, "level": LEVEL_OF[k], **extra})

    for p in revisit or []:
        if p in by_path:
            take(by_path[p], "revisit")
    for p in frontier or []:
        if p in by_path:
            take(by_path[p], by_path[p]["why"] if by_path[p]["why"] != "known" else "frontier")

    n_due, n_unexp = max(1, n // 2), max(1, n // 4)
    for r in due[:n_due]:
        take(r, "due")
    for r in unexplored[:n_unexp]:
        take(r, "unexplored")

    pairs = _pairs_override if _pairs_override is not None else contrast_pairs(notes)
    for a, b in pairs:
        if a in by_path and b in by_path and a not in taken and b not in taken:
            take(by_path[a], "contrast", "contrast", pair=[a, b])
            break
    conflict_paths = _conflicts_override if _conflicts_override is not None else conflicts(notes)
    for p in conflict_paths:
        if p in by_path and p not in taken:
            take(by_path[p], "contradiction", "contradiction")
            break

    for r in due + unexplored:  # backfill from A, then B
        if len(picked) >= n:
            break
        take(r, r["why"])
    # The next candidates, for the generator: a picked note with no grounded
    # material (no footnoted claim, no relation) is dropped there, and the
    # session backfills from these rather than repeating or inventing (D15).
    backfill = []
    for r in due + unexplored:
        if r["path"] in taken:
            continue
        backfill.append({**r, "pool": r["why"], "kind": "recall" if not (r["ai"] and r["attempts"] == 0) else "predict",
                         "level": 1})
        if len(backfill) >= n:
            break

    head = [it for it in picked if it["pool"] in ("revisit",) or it["path"] in set(frontier or [])]
    rest = [it for it in picked if it not in head]
    items = head + _interleave(rest)
    items = items[:n]
    for i, it in enumerate(items, 1):
        it["i"] = i

    n_due_total = len(due)
    amnesty = AMNESTY_LINE.format(due=n_due_total, n=n) if n_due_total > AMNESTY_FACTOR * n else None
    short = None
    if len(items) < n:
        short = (f"{len(items)} item(s) in scope: {n_due_total} due, {len(unexplored)} unexplored"
                 if scope is not None else
                 f"{len(items)} eligible item(s): {n_due_total} due, {len(unexplored)} unexplored")
    return {"items": items, "backfill": backfill, "due": n_due_total, "unexplored": len(unexplored),
            "amnesty": amnesty, "short": short}


def _interleave(items: list[dict]) -> list[dict]:
    """Folder diversity as a tie-breaker: the next item is the first whose
    folder differs from the previous one's, else the first. A single-folder
    scope therefore keeps every item, in order."""
    out: list[dict] = []
    rest = list(items)
    while rest:
        prev = out[-1]["folder"] if out else None
        nxt = next((it for it in rest if it["folder"] != prev), rest[0])
        rest.remove(nxt)
        out.append(nxt)
    return out
