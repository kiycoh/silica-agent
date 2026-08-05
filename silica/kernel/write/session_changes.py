# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What this process has changed in the vault — the ledger behind the GUI's
Changes list.

One dict for the life of the process: vault path -> the note as it stood before
silica first touched it. The *after* side is deliberately not stored; it is read
off disk when a diff is asked for. So a note written five times in one run still
yields one honest row, an edit you made yourself in Obsidian shows up in the same
diff, and an /undo that puts the bytes back makes the row disappear on its own.

The driver is the only writer here. That is the whole reason this is one file and
not a reporting duty spread across the tool surface: interactive patch, bulk
nucleation, a move's link rewrites and delete all reach disk through the same
four methods, so they all land in the list without any of them knowing it exists.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

# ponytail: baseline bodies live in memory, oldest dropped past the cap. A note is
# a few KB, so 200 of them is single-digit MB; if a big /organize run ever needs
# the full history, back this with the checkpoints DB instead of a dict.
MAX_TRACKED = 200

_lock = threading.Lock()


@dataclass(frozen=True)
class Baseline:
    """How a note stood before this session. ``before`` is None when the note did
    not exist yet; ``origin`` is the path it was moved from, if it was moved."""

    before: str | None
    origin: str | None = None


_baselines: dict[str, Baseline] = {}  # insertion-ordered, oldest first


def touched(path: str, prior: str | None) -> None:
    """Record the state a note was in before this session's FIRST write to it.

    Later writes to the same note are no-ops: the baseline is what the diff is
    measured against, and it must not move under the reader.
    """
    with _lock:
        if path in _baselines:
            return
        _baselines[path] = Baseline(before=prior)
        while len(_baselines) > MAX_TRACKED:
            _baselines.pop(next(iter(_baselines)))


def touched_from_disk(path: str) -> None:
    """Same, for a backend that writes through someone else.

    The ws backend hands the write to the Obsidian plugin and keeps no copy of
    the bytes, but the plugin has the same folder open that CONFIG points at —
    so the baseline is one open() away and costs no round-trip. Call it BEFORE
    the write, or the baseline is the result instead of the starting point.
    """
    with _lock:
        if path in _baselines:
            return  # already tracked — don't pay for a read that would be dropped
    from pathlib import Path

    from silica.config import CONFIG

    try:
        prior = (Path(CONFIG.vault_path) / path).read_text(encoding="utf-8")
    except OSError:
        prior = None  # not there yet: this write is a create
    touched(path, prior)


def renamed(old: str, new: str) -> None:
    """Follow a moved note, so it keeps one row instead of splitting into a
    phantom pair (a deletion at the old path, a creation at the new one)."""
    with _lock:
        base = _baselines.pop(old, None)
        if base is None:
            return
        _baselines[new] = Baseline(before=base.before, origin=base.origin or old)


def snapshot() -> dict[str, Baseline]:
    with _lock:
        return dict(_baselines)


def clear() -> None:
    """Drop the ledger — the vault it describes is no longer the current one."""
    with _lock:
        _baselines.clear()
