# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Where Learn reads and appends: the vault it studies, and the append-only
JSONL logs it keeps beside the index (the answer ledger, the offers, the
reader's answers to a reading).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def vault() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path)


def append_rows(path: Path, rows: list[dict]) -> None:
    """Appended and never rewritten, so a crash mid-write costs the tail of one
    round and never the history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def parse_rows(text: str) -> list[Any]:
    """A log's lines as read back. A torn or hand-edited line is one lost
    record, never the log: a crash mid-append leaves one, and the rows before
    it stand."""
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
