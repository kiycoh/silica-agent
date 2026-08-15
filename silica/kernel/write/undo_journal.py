# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

# silica/kernel/write/undo_journal.py
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from silica.kernel.write.ops import InverseOp, InverseOpKind

logger = logging.getLogger(__name__)

_DEFAULT_JOURNAL_PATH = Path.home() / ".silica" / "undo_journal.db"


class UndoJournalStore:
    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _DEFAULT_JOURNAL_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # WAL + per-thread connections; sqlite's busy_timeout serialises
        # writers, so no app-level lock. A thread's conn is closed only by GC when
        # the thread dies — fine for the GUI's small to_thread pool.
        self._local = threading.local()
        try:
            self._init_schema()
        except sqlite3.DatabaseError as e:
            # A corrupt journal must not brick startup or the /revert of future
            # runs. Quarantine it and start fresh; the durable backstop for older
            # history is git (SILICA_GIT_COMMIT=auto), not this file.
            logger.warning(
                "undo journal at %s is corrupt (%s); quarantining and starting fresh",
                self._path, e,
            )
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
            # paths.quarantine: timestamped, never clobbered — the flat
            # ".corrupt" rename here overwrote the previous preserved copy on
            # the second corruption, and doctor's *.corrupt.* glob never saw it.
            from silica.kernel.recall.paths import quarantine
            quarantine(self._path)
            for suffix in ("-wal", "-shm"):
                # a stale WAL sidecar must not be replayed into the fresh db
                Path(str(self._path) + suffix).unlink(missing_ok=True)
            self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path))
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.DatabaseError:
                conn.close()
                raise
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        self._conn().executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                source      TEXT,
                vault       TEXT,
                started_at  REAL NOT NULL,
                reverted_at REAL
            );
            CREATE TABLE IF NOT EXISTS inverses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT NOT NULL,
                path          TEXT NOT NULL,
                kind          TEXT NOT NULL,
                version       INTEGER,
                prior_content TEXT,
                post_hash     TEXT,
                to_path       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inverses_run ON inverses(run_id);
            """
        )
        # Migration: pre-scoping DBs lack `vault`. Legacy rows stay NULL, so a
        # vault-filtered last_active_run() never surfaces them — foreign/stale
        # runs from a deleted or reorganised vault retire themselves.
        conn = self._conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        if "vault" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN vault TEXT")
        # Migration: pre-move_back DBs lack `to_path` (only restore_version /
        # delete_created / recreate_deleted were ever journalled). Legacy rows
        # stay NULL — those kinds don't use it.
        inv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(inverses)")}
        if "to_path" not in inv_cols:
            conn.execute("ALTER TABLE inverses ADD COLUMN to_path TEXT")
        conn.commit()

    def start_run(self, source: str | None = None, vault: str | None = None) -> str:
        run_id = uuid.uuid4().hex
        conn = self._conn()
        conn.execute(
            "INSERT INTO runs (run_id, source, vault, started_at) VALUES (?, ?, ?, ?)",
            (run_id, source, vault, time.time()),
        )
        conn.commit()
        return run_id

    def record(self, run_id: str, inverse: InverseOp, post_hash: str | None) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO inverses (run_id, path, kind, version, prior_content, post_hash, to_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, inverse.path, inverse.kind.value, inverse.version,
             inverse.prior_content, post_hash, inverse.to_path),
        )
        conn.commit()

    def last_active_run(self, vault: str | None = None) -> str | None:
        """Most recent un-reverted run that has inverses.

        When `vault` is given, only runs stamped with that vault are eligible —
        so /revert never walks back into another vault's (or a deleted vault's)
        history. `vault=None` keeps the unscoped behaviour (tests, legacy calls).
        """
        query = (
            "SELECT r.run_id FROM runs r WHERE r.reverted_at IS NULL "
            "AND EXISTS (SELECT 1 FROM inverses i WHERE i.run_id = r.run_id)"
        )
        params: list[str] = []
        if vault is not None:
            query += " AND r.vault = ?"
            params.append(vault)
        query += " ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1"
        row = self._conn().execute(query, params).fetchone()
        return row["run_id"] if row else None

    def inverses_for(self, run_id: str) -> list[tuple[InverseOp, str | None]]:
        rows = self._conn().execute(
            "SELECT path, kind, version, prior_content, post_hash, to_path "
            "FROM inverses WHERE run_id = ? ORDER BY id DESC",
            (run_id,),
        ).fetchall()
        out: list[tuple[InverseOp, str | None]] = []
        for r in rows:
            inv = InverseOp(
                kind=InverseOpKind(r["kind"]), path=r["path"],
                version=r["version"], prior_content=r["prior_content"],
                to_path=r["to_path"],
            )
            out.append((inv, r["post_hash"]))
        return out

    def run_info(self, run_id: str) -> dict | None:
        """`{source, vault, started_at}` for a run, or None when unknown.

        /revert shows this next to the id: the journal's run ids live in a
        different id-space than the progress ledger's (log.md), so a bare id
        never tells the user WHAT they are about to revert.
        """
        row = self._conn().execute(
            "SELECT source, vault, started_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return {"source": row["source"], "vault": row["vault"],
                "started_at": row["started_at"]}

    def refresh_post_hashes(self, run_id: str) -> int:
        """Re-hash every journalled path from the vault's CURRENT content.

        FINALIZE records post-write hashes, but the coordinator's end-of-run
        passes (dangling-link sweep, orphan repairs) edit the run's notes AFTER
        that — so /revert's "modified since inject" guard refused the run's own
        writes. Called once when the run is truly over: the state on disk at
        that moment is the state the guard should protect. Returns the number
        of rows updated.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT DISTINCT path FROM inverses WHERE run_id = ?", (run_id,)
        ).fetchall()
        updated = 0
        for r in rows:
            path = r["path"]
            try:
                current = DRIVER.read_note(path).content
                new_hash: str | None = _content_hash(current)
            except Exception:
                new_hash = None  # note absent — the stale guard handles it
            conn.execute(
                "UPDATE inverses SET post_hash = ? WHERE run_id = ? AND path = ?",
                (new_hash, run_id, path),
            )
            updated += 1
        conn.commit()
        return updated

    def mark_reverted(self, run_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE runs SET reverted_at = ? WHERE run_id = ?", (time.time(), run_id)
        )
        conn.commit()


_store: UndoJournalStore | None = None


def get_undo_journal(path: Path | str | None = None) -> UndoJournalStore:
    global _store
    if _store is None:
        _store = UndoJournalStore(path)
    return _store


import hashlib as _hashlib

from silica.driver import DRIVER


def _content_hash(text: str | None) -> str:
    return _hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def revert_run(run_id: str, *, store: UndoJournalStore | None = None) -> dict:
    """Replay a run's inverses LIFO, refusing notes modified since the inject.

    Version guard: re-read each note, hash it. If recorded post_hash exists and
    the current hash differs (note edited since inject), skip it — don't clobber
    newer work. Mark the run reverted when done.
    """
    from silica.tools.wrapped import silica_restore

    store = store or get_undo_journal()
    entries = store.inverses_for(run_id)  # LIFO
    reverted: list[str] = []
    skipped: list[dict] = []
    stale: list[dict] = []
    errors: list[dict] = []
    applied_paths: set[str] = set()

    for inv, post_hash in entries:
        try:
            current = DRIVER.read_note(inv.path).content
            cur_hash: str | None = _content_hash(current)
        except Exception:
            cur_hash = None  # note absent

        # Stale (B): the target note no longer exists in this vault, so there is
        # nothing to restore or delete — the journal describes a vault that was
        # reorganised or replaced. Report it honestly instead of counting an
        # empty overwrite as an error or an absent delete as a revert.
        # (recreate_deleted is exempt: an absent note is its expected precondition.)
        if cur_hash is None and inv.kind in (
            InverseOpKind.restore_version, InverseOpKind.delete_created
        ):
            stale.append({"path": inv.path, "reason": "note absent (vault changed)"})
            continue

        # The guard belongs to the NEWEST inverse per path only: two chunks
        # patching one note both carry the FINAL content's hash, so the older
        # link of the chain can never match once the newer one applied. After
        # the newest passes, the older ones are the chain's own history.
        if (
            inv.path not in applied_paths
            and post_hash is not None and cur_hash is not None
            and cur_hash != post_hash
        ):
            skipped.append({"path": inv.path, "reason": "modified since inject"})
            continue

        try:
            res = silica_restore(txn_id=run_id, inverses=[inv.model_dump()])
            if res["errors"]:
                # silica_restore swallows per-op failures into its return value;
                # route them to errors instead of miscounting as reverted.
                errors.append({"path": inv.path, "error": "; ".join(res["errors"])})
            else:
                reverted.append(inv.path)
                applied_paths.add(inv.path)
        except Exception as e:
            errors.append({"path": inv.path, "error": str(e)})

    # A partial revert can delete notes that surviving (skipped) notes still
    # link to — the run's own hub kept [[links]] to notes the revert removed.
    # Sweep only the survivors this run wrote: they are journalled paths, so
    # the edit stays inside the run's own footprint.
    if reverted and skipped:
        try:
            from silica.kernel.link.sweep import sweep_dangling_links

            sweep_dangling_links(sorted({s["path"] for s in skipped}))
        except Exception as e:
            logger.debug("revert: survivor link sweep failed (non-fatal): %s", e)

    store.mark_reverted(run_id)
    return {"run_id": run_id, "reverted": reverted, "skipped": skipped,
            "stale": stale, "errors": errors}
