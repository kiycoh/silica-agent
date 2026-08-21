# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Human-readable append-only journal at `<vault>/log.md`.

Every other run-state store under `.silica/` is machine-facing (Run
manifest, ledger, deferred store). This is the one artifact meant to be read
by a human — or by the agent at session start, via the vault-map tail —
without opening JSON.

One line per completed event, projected from data the caller already has:
this module contributes no new computation, only formatting + idempotent
append. The curator (future) and /organize are meant to log through the
same `append_log_line` helper so every write path narrates itself the same
way.

Kernel-only: no router/capabilities imports (import-linter boundary).
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from silica.kernel.recall.paths import resolve_vault_path

logger = logging.getLogger(__name__)

DEFAULT_LOG_FILENAME = "log.md"


def _log_path(vault: str, filename: str) -> Path:
    """`<vault>/<write_dir>/<filename>` — the journal is Silica's own artifact.

    A vault whose writes are confined to `silica/` was still getting a `log.md`
    dropped in the user's root, beside their own README. Reads fall back to a
    root journal written before this: legacy stays readable forever.
    """
    from silica.kernel.vault_manifest import in_write_dir

    root = Path(vault)
    try:
        composed = root / in_write_dir(filename)
    except Exception as exc:  # config not resolvable — root is the honest guess
        logger.debug("run_log: write-dir compose failed (non-fatal): %s", exc)
        return root / filename
    if composed != root / filename and not composed.exists() and (root / filename).exists():
        return root / filename
    return composed


def format_nucleate_event(source_basename: str, new: int, patch: int, deferred: int) -> str:
    """`nucleate \\`file.md\\` → 7 new, 3 patch, 2 deferred` — the nucleate event shape."""
    return f"nucleate `{source_basename}` → {new} new, {patch} patch, {deferred} deferred"


_CURATE_ORDER = ("dedup", "refine", "orphan", "autolink")


def format_curate_event(counts: dict[str, int]) -> str:
    """`curate → 10 item (2 dedup, 1 refine, 3 orphan, 4 autolink)` — curator shape.

    `counts` maps item kind → count; kinds with zero (or absent) counts are
    omitted from the breakdown. Total is the sum of all kinds.
    """
    total = sum(counts.get(k, 0) for k in _CURATE_ORDER) + sum(
        v for k, v in counts.items() if k not in _CURATE_ORDER
    )
    parts = [f"{counts[k]} {k}" for k in _CURATE_ORDER if counts.get(k)]
    parts += [f"{v} {k}" for k, v in counts.items() if k not in _CURATE_ORDER and v]
    breakdown = f" ({', '.join(parts)})" if parts else ""
    return f"curate → {total} item{breakdown}"


def format_revert_event(source: str, reverted: int, skipped: int) -> str:
    """`revert (nucleate) → 4 note(s) restored, 6 kept (modified since)`.

    Without this line log.md kept narrating notes a /revert had already taken
    back out — the journal lied by omission.
    """
    head = f"revert ({source})" if source else "revert"
    line = f"{head} → {reverted} note(s) restored"
    if skipped:
        line += f", {skipped} kept (modified since)"
    return line


def append_log_line(
    event: str,
    run_id: str,
    *,
    vault_path: str | None = None,
    filename: str = DEFAULT_LOG_FILENAME,
    dedup_key: str | None = None,
) -> bool:
    """Append `- <date> · <event> · run <short_id>` to `<vault>/<filename>`.

    Idempotency: a re-run/resume must not duplicate a line. Without
    `dedup_key` the unit is the run — any existing line carrying
    `run <short_id>` suppresses the append. With `dedup_key` the unit is
    (run_id, key): only a line carrying BOTH markers suppresses it. Callers
    that log multiple events under one run_id (e.g. nucleate: one line per
    source file of a multi-file run) MUST pass a per-event key, or every
    event after the first is silently swallowed.

    Creates the file (and vault dir, if missing) on first use. Best-effort:
    swallows I/O errors rather than failing the caller's pipeline — a human
    journal is a courtesy, not a critical path.

    Returns True iff a line was appended.
    """
    resolved = resolve_vault_path(vault_path)
    if not resolved:
        return False

    short_id = (run_id or "")[:8]
    marker = f"run {short_id}"
    log_path = _log_path(resolved, filename)

    try:
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    except Exception as exc:
        logger.debug("run_log: read failed (non-fatal): %s", exc)
        existing = ""

    if dedup_key is None:
        duplicate = marker in existing
    else:
        duplicate = any(
            marker in ln and dedup_key in ln for ln in existing.splitlines()
        )
    if duplicate:
        return False

    date = datetime.now().strftime("%Y-%m-%d")
    line = f"- {date} · {event} · {marker}\n"

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.debug("run_log: append failed (non-fatal): %s", exc)
        return False
    return True


def tail_log(
    n: int = 5,
    *,
    vault_path: str | None = None,
    filename: str = DEFAULT_LOG_FILENAME,
) -> list[str]:
    """Last `n` non-empty lines of `<vault>/<filename>`; `[]` if missing/unreadable."""
    resolved = resolve_vault_path(vault_path)
    if not resolved or n <= 0:
        return []
    log_path = _log_path(resolved, filename)
    if not log_path.exists():
        return []
    try:
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as exc:
        logger.debug("run_log: tail read failed (non-fatal): %s", exc)
        return []
    return lines[-n:]
