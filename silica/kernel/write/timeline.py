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

Pure and LLM-free. The walk (rglob + frontmatter parse) runs once per
file-state epoch: `_all_rows` memoizes on `vault_epoch`, so repeated
queries between vault changes cost one stat walk, not a re-parse.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from silica.kernel.write import frontmatter
from silica.kernel.write.contested import note_clock
from silica.kernel.recall.paths import SOURCES_DIR


# vault -> (epoch, dated rows, paths of the notes the walk found no date for)
_rows_memo: dict[str, tuple[str, list[tuple[str, str, str, str]], list[str]]] = {}

# What a date bound may be: a year, a month or a day (ADR-0148 point 1).
DATE_PREFIX = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


def in_window(date: str, start: str = "", end: str = "") -> bool:
    """`date` inside the inclusive bounds, each an ISO prefix: compared on the
    end's own length, an `end` of "2026-03" keeps all of March, where a plain
    string compare put "2026-03-05" after it."""
    return (not start or date >= start) and (not end or date[:len(end)] <= end)


def _all_rows(vault: Path) -> list[tuple[str, str, str, str]]:
    """Every dated (date, label, stem, vault-relative path) row of `vault`,
    unfiltered.

    Memoized on the vault's file-state epoch: the walk YAML-parses every
    note, and the tool re-runs it per query.
    """
    return _scan(vault)[0]


def _scan(vault: Path) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """(the dated rows of `_all_rows`, the vault-relative paths of the notes
    the same walk found no date for), memoized together."""
    from silica.kernel.recall.paths import ignore_matcher, vault_epoch

    epoch = vault_epoch(str(vault))
    if epoch:
        hit = _rows_memo.get(str(vault))
        if hit is not None and hit[0] == epoch:
            return hit[1], hit[2]

    from silica.kernel.calendar.daily import daily_date, daily_rule

    ignored = ignore_matcher(vault)
    days = daily_rule(vault)
    rows: list[tuple[str, str, str, str]] = []
    undated: list[str] = []
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
        # about itself and outranks what its claims happen to carry. A daily
        # note's name is the next statement (ADR-0062): Obsidian named it after
        # its day, and a daily template need not write a date at all.
        date = ((data or {}).get("date") or daily_date("/".join(parts), days)
                or note_clock(text))
        if not date:
            undated.append("/".join(parts))
            continue
        label = str((data or {}).get("session_id") or f.stem)
        rows.append((str(date)[:10], label, f.stem, "/".join(parts)))

    if epoch:
        _rows_memo.clear()
        _rows_memo[str(vault)] = (epoch, rows, undated)
    return rows, undated


def dated_window(vault: Path, since: str = "", until: str = "",
                 keep: Callable[[str], bool] | None = None) -> tuple[frozenset[str], int]:
    """(the store keys of the notes dated inside [since, until], how many notes
    have no date and so fall outside every window), both counted over the
    notes `keep` accepts. The keys are what recall's legs take as `allow`
    (ADR-0148 point 2): a path without its `.md`."""
    rows, undated = _scan(vault)
    keys = frozenset(r[3].removesuffix(".md") for r in rows
                     if in_window(r[0], since, until) and (keep is None or keep(r[3])))
    return keys, sum(1 for p in undated if keep is None or keep(p))


def timeline(vault: Path, start: str = "", end: str = "", limit: int = 50,
             keep: Callable[[str], bool] | None = None) -> dict:
    """Dated notes of `vault`, chronological. Rows are (date, label, stem).

    `start`/`end` are inclusive ISO-date bounds; empty means unbounded. On
    overflow the most recent `limit` rows are kept (recency is the useful
    default) and `dropped` reports how many older rows were cut.
    `total_dated` counts the in-range dated notes before the cut. `keep`
    takes a vault-relative path and drops a note before anything is counted:
    a workspace's scope (ADR-0112), which a stem alone cannot apply, since two
    folders' README.md share one.
    """
    rows = [
        r[:3] for r in _all_rows(vault)
        # day precision: keeps datetime values inside inclusive bounds
        if in_window(r[0], start, end) and (keep is None or keep(r[3]))
    ]
    rows.sort(key=lambda r: (r[0], r[2]))  # date asc; stem tie-break for determinism
    total = len(rows)
    dropped = max(0, total - max(limit, 0))
    return {"rows": rows[dropped:], "total_dated": total, "dropped": dropped}
