# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`silica hook <event>` — the hook-side producer for coding-agent harnesses.

Claude Code, Codex and DeepSeek Harness all run a `hooks.json` command with
the event payload on stdin and add plain stdout to the session as context on
SessionStart (ADR-0025). Plain text rather than the JSON envelope on purpose:
the envelope's field names differ per dialect, while stdout text is what all
three accept on that event.

Nothing here may fail loud. This process runs inside someone else's session,
so every exit is 0 and every problem is silence; `silica.capture` made the
same choice for the end of the session.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from silica.capture import find_vault


def session_start(stdin_text: str) -> str:
    """The session's opening line about its vault, or "" when cwd has none."""
    try:
        payload = json.loads(stdin_text or "{}")
        cwd = payload.get("cwd") or os.getcwd()
    except (ValueError, AttributeError):
        return ""  # the payload is the client's; anything unreadable is "no vault"
    vault = find_vault(str(cwd))
    if vault is None:
        return ""
    from silica.ui.mcp import INSTRUCTIONS

    # The path and the loop, nothing counted: a note count needs the excludes
    # the indexes apply (a repo vault here walked 37,897 gitignored fixtures
    # as "notes"), and the count changes nothing the agent does next.
    return (f"Silica vault: {vault}.\n{INSTRUCTIONS}\n{_drift_line(vault)}{_migrate_line(vault)}"
            f"{_due_line(vault)}{_zotero_line(vault)}")


def _drift_line(vault: str | Path) -> str:
    """Code drift since the stale snapshot, or "" (ADR-0038: detection at
    session open, from the cache and one git diff; never a recompute, never
    a write). Names at most three notes per clause: the line is a pointer to
    /stale, not the report."""
    try:
        from silica.kernel.code.codedocs import CHANGE_STRUCTURAL, drift
        d = drift(vault)
    except Exception:
        return ""  # the brief is an aid; a broken code lane must not break the session
    if not d:
        return ""
    parts = []
    if d["affected"]:
        parts.append(f"{len(d['changed'])} documented path(s) changed since the stale "
                     f"snapshot at {d['snap_head'][:8]} ({_few(d['changed'])}); notes "
                     f"to re-verify: {_few(sorted(d['affected']))}")
    structural = sorted(n for n, lvl in d["stale"].items() if lvl == CHANGE_STRUCTURAL)
    if structural:
        parts.append(f"{len(structural)} note(s) structurally stale at that snapshot: "
                     f"{_few(structural)}")
    if not parts:
        return ""
    return ("Code drift: " + "; ".join(parts) + ". Read them with silica_read_note "
            "before citing (each read carries its stale banner); /stale in the REPL "
            "refreshes the snapshot.\n")


def _migrate_line(vault: str | Path) -> str:
    """Notes still in a shape the gate no longer writes, or "" (ADR-0039: the
    count at session open, the rewrite on /migrate --write). A walk and a
    regex, 0.06 s on 944 notes; never a write."""
    try:
        from silica.kernel.write.migrate import MIGRATIONS, count
        found = [(m.name, count(vault, m)) for m in MIGRATIONS]
    except Exception:
        return ""  # the brief is an aid; a broken write lane must not break the session
    found = [(name, n) for name, n in found if n]
    if not found:
        return ""
    what = ", ".join(f"{n} note(s) in the {name} shape" for name, n in found)
    return (f"Older shape: {what}. /migrate in the REPL lists them, /migrate --write "
            "rewrites them as one revertible run per shape.\n")


def _due_line(vault: str | Path) -> str:
    """Measured notes due for review, as a count (learn-suite D18), or "".

    Count only, never a path: a note path echoed into the agent's context is
    vault data in an instruction stream, and the drift line already spends
    that budget. Skipped entirely when the vault has no ledger, so a vault
    that never quizzed pays no notes walk at session open.
    """
    try:
        from silica.config import CONFIG
        from silica.kernel.learn.schedule import DUE_R, view
        from silica.kernel.recall.paths import index_dir_for
        from silica.kernel.report import quiz
        from silica.kernel.report.learner import _notes_meta

        same = Path(str(CONFIG.vault_path or "")).resolve() == Path(vault).resolve()
        log = quiz.log_path() if same else index_dir_for(str(vault)) / "quiz.jsonl"
        if not log.exists():
            return ""
        entries = quiz.entries() if same else quiz.entries(log)
        if not entries:
            return ""
        rows = view(_notes_override=_notes_meta(Path(vault)), _entries_override=entries)
        due = sum(1 for r in rows.values() if r["attempts"] and r["R"] is not None and r["R"] < DUE_R)
    except Exception:
        return ""  # the brief is an aid; a broken ledger must not break the session
    if not due:
        return ""
    minutes = max(1, round(due * 0.6))
    return f"{due} note{'s' if due != 1 else ''} due for review · ~{minutes} min · /learn\n"


def _zotero_line(vault: str | Path) -> str:
    """Papers new in the linked Zotero library and PDFs changed under vault
    papers, as counts, or "" (ADR-0055 decision 9: detection, never a write)."""
    try:
        from silica.sources.zotero import detect
        d = detect(vault)
    except Exception:
        return ""  # the brief is an aid; a broken Zotero link must not break the session
    if not d:
        return ""
    if d["other"]:
        return (f"Zotero: another library answers ({d['other']}); writes to Zotero are off "
                "until the linked one is back.\n")
    parts = []
    if d["new"]:
        n = len(d["new"])
        parts.append(f"{n} paper{'s' if n != 1 else ''} new since the last session")
    if d["changed"]:
        m = len(d["changed"])
        parts.append(f"{m} PDF{'s' if m != 1 else ''} changed under papers in the vault")
    if not parts:
        return ""
    return "Zotero: " + ", ".join(parts) + ". /zotero in the REPL, or Explore › Library.\n"


def _few(items: list[str], n: int = 3) -> str:
    return ", ".join(items[:n]) + (f" +{len(items) - n}" if len(items) > n else "")


_PRODUCERS = {"SessionStart": session_start}


def run_hook(argv: list[str], stdin_text: str) -> int:
    producer = _PRODUCERS.get(argv[0] if argv else "")
    if producer is None:
        return 0  # an event this version has nothing to say about is not an error
    text = producer(stdin_text)
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0
