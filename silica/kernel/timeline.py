# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Chronological index over the vault's dated notes.

Product promotion of the eval overlay (evals/locomo/runner.py::
build_timeline_seed, spec-harness-promotion 2026-07-24 §1): read each note's
``date``/``session_id`` frontmatter, sort by date ascending, and return one
row per note pointing at it by the identifier silica_read_note resolves (its
filename stem). Undated notes are EXCLUDED: a note with no date has no place
on a chronology, and "end of list" would read as most-recent — wrong.

Pure and LLM-free. Full rglob + frontmatter parse per call; the FS body
cache absorbs most of the read cost.
# ponytail: no row cache — add an mtime-keyed one only if 10k+ vaults hurt.
"""
from __future__ import annotations

from pathlib import Path

from silica.kernel import frontmatter
from silica.kernel.paths import SOURCES_DIR


def timeline(vault: Path, start: str = "", end: str = "", limit: int = 50) -> dict:
    """Dated notes of `vault`, chronological. Rows are (date, label, stem).

    `start`/`end` are inclusive ISO-date bounds; empty means unbounded. On
    overflow the most recent `limit` rows are kept (recency is the useful
    default) and `dropped` reports how many older rows were cut.
    `total_dated` counts the in-range dated notes before the cut.
    """
    rows: list[tuple[str, str, str]] = []
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts):
            continue  # .obsidian, .trash, .silica
        if parts[0] == SOURCES_DIR:
            continue  # verbatim leaves: reachable only via ## Sources links (§2)
        try:
            data, _raw, _body = frontmatter.split(f.read_text(encoding="utf-8"))
        except OSError:
            continue
        date = (data or {}).get("date")
        if not date:
            continue
        date = str(date)[:10]  # day precision: keeps datetime values inside inclusive bounds
        if (start and date < start) or (end and date > end):
            continue
        label = str((data or {}).get("session_id") or f.stem)
        rows.append((date, label, f.stem))

    rows.sort(key=lambda r: (r[0], r[2]))  # date asc; stem tie-break for determinism
    total = len(rows)
    dropped = max(0, total - max(limit, 0))
    return {"rows": rows[dropped:], "total_dated": total, "dropped": dropped}
