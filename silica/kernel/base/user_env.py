# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The writer of the user-level `.env`, the file `config.py` reads at boot.

Below every surface that keeps a key: the first-run wizard, the settings panel and a
Zotero write grant. It sat in the wizard, so the grant, which is a source's, imported
the onboarding package to keep one key.
"""
from __future__ import annotations

import re
from pathlib import Path

from silica.config import USER_ENV


# Optional leading `#` so merge_env can uncomment-and-fill a `# KEY=default`
# line seeded from .env.example, not just rewrite an already-active key.
_KEY_RE = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def resolve_env_path() -> Path:
    """The `.env` a settings write must land in — the one that would win at boot.

    Always the user-level file, because config.py layers that one and nothing
    else: it is the only path where a write is still there at the next launch.
    This used to prefer a project .env when one existed, so a settings edit made
    inside any checkout landed in that checkout's file — and, once the project
    layer was removed, would have landed in a file nobody reads.

    Creates the parent directory, never the file.
    """
    USER_ENV.parent.mkdir(parents=True, exist_ok=True)
    return USER_ENV


def write_env(updates: dict[str, str]) -> Path:
    """Persist keys into the user .env, atomically, readable by its owner only.

    The one writer for a key: the settings panel and the Zotero grant both call
    it, because the grant had its own copy and missed the 0600 the panel got
    (2026-10-11). The file holds every API key the user has, so a crash
    mid-write must not truncate it and a group must not read it.
    """
    from silica.kernel.base.durable import atomic_write_bytes

    path = resolve_env_path()
    base = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write_bytes(path, merge_env(base, updates).encode("utf-8"), mode=0o600)
    return path


def merge_env(existing: str, updates: dict[str, str]) -> str:
    """Update KEY=VALUE lines in place — uncommenting a `# KEY=default` line when
    KEY is collected — preserve every other line untouched, and append keys that
    were not present. Never deletes a line it did not write."""
    pending = dict(updates)
    out: list[str] = []
    for line in existing.splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) in pending:
            key = m.group(1)
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    text = "\n".join(out)
    return text + "\n" if text else ""
