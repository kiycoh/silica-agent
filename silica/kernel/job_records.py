# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The job records in `~/.silica/jobs`: one JSON file a job, written whole by
rename, so a process killed at any instant leaves the last state it reached
(ADR-0099, here ADR-0135 point 1).

Two kinds share the directory on purpose: a conversion (`ui/web/jobs.py`, with
its queue, tray and retry) and a Learn reading (`kernel/learn/reading.py`). A
restart must interrupt both and the release drain must count both, and neither
owner can import the other. This module is what they share: where a record
lives, what an id is, one lock over a read-modify-write, how a record is read
and written.

The rest of the contract, for a new kind: a record carries `kind`, `state`,
`vault`, `created` and `source`. At startup `ui/web/jobs.recover` turns every
`queued` or `running` record into `interrupted`, in the conversions' words,
because the process that owned it is gone; a kind reads the state and says its
own sentence (`reading.job_status`). A record with no `kind` is a conversion,
written before readings joined.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from pathlib import Path

from silica.config import silica_home

logger = logging.getLogger(__name__)

CONVERT = "convert"
_ID = re.compile(r"[0-9a-f]{12}")
LOCK = threading.Lock()   # every read-modify-write of a record, of any kind


def directory() -> Path:
    # silica_home() and not Path.home(): the suite repoints that one name
    # for every store under ~/.silica, as media_admission's counter relies on.
    return silica_home() / "jobs"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def is_id(text: str) -> bool:
    return bool(_ID.fullmatch(text or ""))


def path_of(job_id: str) -> Path:
    return directory() / f"{job_id}.json"


def kind_of(rec: dict) -> str:
    return rec.get("kind", CONVERT)


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Written whole by rename, so a record that does not parse was edited
        # by hand or damaged on disk: it is reported as missing rather than
        # taking the list of every other job down with it.
        logger.warning("unreadable job record %s", path.stem)
        return None


def all_records() -> list[dict]:
    """Every readable record of every kind, oldest first."""
    out = []
    root = directory()
    if root.is_dir():
        for f in root.glob("*.json"):
            rec = read(f) if is_id(f.stem) else None
            if rec is not None:
                out.append(rec)
    out.sort(key=lambda r: r.get("created") or 0)
    return out


def write(rec: dict) -> None:
    from silica.kernel.base.durable import atomic_write_bytes

    directory().mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path_of(rec["id"]), json.dumps(rec, sort_keys=True).encode("utf-8"), mode=0o600)
