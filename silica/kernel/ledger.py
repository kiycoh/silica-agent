"""SQLite ledger for idempotent resume (ADR-011 / Addendum C5).

Tracks per-op outcomes. The orchestrator writes here after commit.
On re-run, the router checks the ledger: a source_basename with all ops
'committed' is not reprocessed.

Schema:
    ops(txn_id, source_basename, path, op, status, ts)
    status ∈ {committed, failed, rolled_back}
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Default ledger location: project root or user can override via env
_DEFAULT_LEDGER_PATH = Path.home() / ".silica" / "ledger.db"


class Ledger:
    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _DEFAULT_LEDGER_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ops (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_id        TEXT NOT NULL,
                source_basename TEXT NOT NULL,
                path          TEXT,
                op            TEXT NOT NULL,
                status        TEXT NOT NULL,
                ts            REAL NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_source ON ops(source_basename)"
        )
        self._conn.commit()

    def record(self, txn_id: str, source_basename: str, path: str | None,
               op: str, status: str) -> None:
        """Insert or update an op record."""
        self._conn.execute(
            "INSERT INTO ops(txn_id,source_basename,path,op,status,ts) VALUES (?,?,?,?,?,?)",
            (txn_id, source_basename, path, op, status, time.time()),
        )
        self._conn.commit()

    def is_committed(self, source_basename: str) -> bool:
        """True if ALL ops for this source are committed (safe to skip re-run)."""
        rows = self._conn.execute(
            "SELECT status FROM ops WHERE source_basename=?",
            (source_basename,),
        ).fetchall()
        if not rows:
            return False
        return all(r[0] == "committed" for r in rows)

    def mark_rolled_back(self, txn_id: str) -> None:
        """Mark all ops of a transaction as rolled_back."""
        self._conn.execute(
            "UPDATE ops SET status='rolled_back' WHERE txn_id=?",
            (txn_id,),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# Module-level singleton — lazily initialised
_ledger: Ledger | None = None


def get_ledger(path: Path | str | None = None) -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(path)
    return _ledger
