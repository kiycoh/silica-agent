# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Chronological index over the vault's dated notes.

Product promotion of the eval overlay (evals/locomo/runner.py::
build_timeline_seed, spec-harness-promotion 2026-07-24 §1): read each note's
``date``/``session_id`` frontmatter, sort by date ascending, and return one
row per note pointing at it by the identifier silica_read_note resolves (its
filename stem). Undated notes are EXCLUDED: a note with no date has no place
on a chronology, and "end of list" would read as most-recent — wrong.

A note the FSM wrote carries no frontmatter ``date`` at all — its event clock
lives per-claim, in ``<!-- silica: valid_from=… -->`` stamps — so reading
frontmatter alone left this index blind to everything nucleated (measured on a
real vault: 1 row out of 86 stamped notes). ``note_clock`` is the fallback and
the same one ``suppress_contest`` reads, so a note dates identically here and
in a contest. It is a MAX over the note's stamps: a note fed by nine sources
sits at its most recent claim, which is the reading a recency-sorted
chronology wants.

Pure and LLM-free. Full rglob + frontmatter parse per call; the FS body
cache absorbs most of the read cost.
# ponytail: no row cache — add an mtime-keyed one only if 10k+ vaults hurt.
"""
from __future__ import annotations

from pathlib import Path

from silica.kernel.write import frontmatter
from silica.kernel.write.contested import note_clock
from silica.kernel.recall.paths import SOURCES_DIR


_rows_memo: dict[str, tuple[str, list[tuple[str, str, str]]]] = {}  # vault -> (epoch, rows)


def _all_rows(vault: Path) -> list[tuple[str, str, str]]:
    """Every dated (date, label, stem) row of `vault`, unfiltered.

    Memoized on the vault's file-state epoch: the walk YAML-parses every
    note, and the MCP tool re-runs it per query.
    """
    from silica.kernel.recall.paths import ignore_matcher, vault_epoch

    epoch = vault_epoch(str(vault))
    if epoch:
        hit = _rows_memo.get(str(vault))
        if hit is not None and hit[0] == epoch:
            return hit[1]

    ignored = ignore_matcher(vault)
    rows: list[tuple[str, str, str]] = []
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts):
            continue  # .obsidian, .trash, .silica
        if any(ignored(p) for p in parts[:-1]):
            continue  # .silicaignore / NOISE_DIRS: node_modules under a repo vault
        if parts[0] == SOURCES_DIR:
            continue  # verbatim leaves: reachable only via ## Sources links (§2)
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # One non-UTF-8 note (a latin-1 file, an embedded binary blob)
            # must skip that file, never take the whole timeline down.
            continue
        data, _raw, _body = frontmatter.split(text)
        # Frontmatter first: an explicit `date:` is the note's own statement
        # about itself and outranks what its claims happen to carry.
        date = (data or {}).get("date") or note_clock(text)
        if not date:
            continue
        label = str((data or {}).get("session_id") or f.stem)
        rows.append((str(date)[:10], label, f.stem))

    if epoch:
        _rows_memo.clear()
        _rows_memo[str(vault)] = (epoch, rows)
    return rows


def timeline(vault: Path, start: str = "", end: str = "", limit: int = 50) -> dict:
    """Dated notes of `vault`, chronological. Rows are (date, label, stem).

    `start`/`end` are inclusive ISO-date bounds; empty means unbounded. On
    overflow the most recent `limit` rows are kept (recency is the useful
    default) and `dropped` reports how many older rows were cut.
    `total_dated` counts the in-range dated notes before the cut.
    """
    rows = [
        r for r in _all_rows(vault)
        # day precision: keeps datetime values inside inclusive bounds
        if not (start and r[0] < start) and not (end and r[0] > end)
    ]
    rows.sort(key=lambda r: (r[0], r[2]))  # date asc; stem tie-break for determinism
    total = len(rows)
    dropped = max(0, total - max(limit, 0))
    return {"rows": rows[dropped:], "total_dated": total, "dropped": dropped}
