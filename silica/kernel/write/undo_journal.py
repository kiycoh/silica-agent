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
from silica.config import silica_home

logger = logging.getLogger(__name__)

def _journal_path() -> Path:
    return silica_home() / "undo_journal.db"


# A delete from the chat is a one-op journal run under this source, so /undo
# and /revert both find it. `post_hash` is ABSENT_HASH, which no content hash
# equals: revert_run's version guard then skips the recreate when a note has
# appeared at the path since, instead of overwriting it with the old body.
CHAT_DELETE_SOURCE = "chat:silica_delete"
ABSENT_HASH = "absent"
# A move from the chat, the same way: one move_back inverse. Until 2026-10-11
# a move recorded nothing, and the GUI's "move this note back" did nothing.
CHAT_MOVE_SOURCE = "chat:silica_move"
# A move the user made from the tree (a drop on a folder, a rename in the row,
# ADR-0126): one run for the whole set, one move_back per note, under its own
# source so the tree's undo takes back only what the person did there.
USER_MOVE_SOURCE = "user:/move"
MOVE_SOURCES = (CHAT_MOVE_SOURCE, USER_MOVE_SOURCE)
# A delete the user ran (/delete, or the card's "delete anyway"): a note other
# notes link to is the user's to delete, not the model's (ADR-0092). Its own
# source, so a check that judges the chat's deletes (ADR-0093's re-create)
# can leave the user's out by name.
USER_DELETE_SOURCE = "user:/delete"
DELETE_SOURCES = (CHAT_DELETE_SOURCE, USER_DELETE_SOURCE)
# A note the user wrote by hand (ADR-0155 point 8): one run, one trash_created.
# Its post_hash is None and not the first save's: the note is typed into for an
# hour after the create, and the version guard would then refuse every undo.
USER_WRITE_SOURCE = "user:/write"


class UndoJournalStore:
    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _journal_path()
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
            from silica.kernel.base.durable import quarantine
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
                # The trash's "delete forever" (ADR-0128) is a DELETE here, and
                # without this sqlite leaves the note's text in the freed pages.
                conn.execute("PRAGMA secure_delete=ON")
            except sqlite3.DatabaseError:
                conn.close()
                raise
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        self._conn().executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id        TEXT PRIMARY KEY,
                source        TEXT,
                vault         TEXT,
                started_at    REAL NOT NULL,
                reverted_at   REAL,
                ledger_run_id TEXT
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
        # Migration: pre-source-revert DBs lack `ledger_run_id`, the FSM's own
        # run id that the provenance ledger keys on (ADR-0028). Legacy rows stay NULL, so
        # /revert --source reports them as not in the journal instead of
        # guessing a join by file name; /revert <run-id> still reaches them.
        if "ledger_run_id" not in cols:
            conn.execute("ALTER TABLE runs ADD COLUMN ledger_run_id TEXT")
        conn.commit()

    def start_run(self, source: str | None = None, vault: str | None = None,
                  ledger_run_id: str | None = None) -> str:
        run_id = uuid.uuid4().hex
        conn = self._conn()
        conn.execute(
            "INSERT INTO runs (run_id, source, vault, started_at, ledger_run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, source, vault, time.time(), ledger_run_id),
        )
        conn.commit()
        return run_id

    def match_runs(self, ref: str, vault: str | None = None) -> list[tuple[str, str | None]]:
        """(run_id, ledger_run_id) of the un-reverted runs `ref` names, newest
        first: a journal run id, or the ledger's, whole or by a prefix of at
        least 4 characters. The ledger's is the one a person sees (`/status`
        prints `RUN 3fa9c1d2`), while the journal's never reached a screen
        whole, so `/revert <run-id>` had nothing it could be given."""
        ref = (ref or "").strip().lower()
        if len(ref) < 4 or not all(c in "0123456789abcdef-" for c in ref):
            return []  # ids are hex: anything else names no run, and LIKE never sees a wildcard
        query = ("SELECT run_id, ledger_run_id FROM runs WHERE reverted_at IS NULL "
                 "AND (run_id LIKE ? OR ledger_run_id LIKE ?)")
        params: list[str] = [ref + "%", ref + "%"]
        if vault is not None:
            query += " AND vault = ?"
            params.append(vault)
        query += " ORDER BY started_at DESC, rowid DESC"
        return [(r["run_id"], r["ledger_run_id"]) for r in self._conn().execute(query, params).fetchall()]

    def runs_for_ledger(self, ledger_run_ids: list[str],
                        vault: str | None = None) -> list[str]:
        """Un-reverted journal runs opened under these ledger run ids, newest
        first: a later run's inverses restore what an earlier one wrote, so
        that is the only order in which replaying both is sound."""
        ids = [i for i in ledger_run_ids if i]
        if not ids:
            return []
        query = ("SELECT run_id FROM runs WHERE reverted_at IS NULL AND ledger_run_id IN (%s)"
                 % ",".join("?" * len(ids)))
        params: list[str] = list(ids)
        if vault is not None:
            query += " AND vault = ?"
            params.append(vault)
        query += " ORDER BY started_at DESC, rowid DESC"
        return [r["run_id"] for r in self._conn().execute(query, params).fetchall()]

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

    def last_chat_delete(self, vault: str | None,
                         path: str | None = None) -> tuple[str, str, float] | None:
        """(run_id, path, started_at) of the newest un-reverted chat delete in
        `vault`, of `path` when given (any spelling, via provenance.note_key)."""
        return self.last_delete(vault, path, sources=(CHAT_DELETE_SOURCE,))

    def last_delete(self, vault: str | None, path: str | None = None,
                    sources: tuple[str, ...] = DELETE_SOURCES
                    ) -> tuple[str, str, float] | None:
        """The same over every source in `sources`: by default a chat delete
        or one the user ran with /delete, both of which /undo takes back."""
        from silica.kernel.base.notekey import note_key

        marks = ",".join("?" * len(sources))
        query = ("SELECT r.run_id, i.path, r.started_at FROM runs r "
                 "JOIN inverses i ON i.run_id = r.run_id "
                 f"WHERE r.reverted_at IS NULL AND r.source IN ({marks}) AND i.kind = ?")
        params: list = [*sources, InverseOpKind.recreate_deleted.value]
        if vault is not None:
            query += " AND r.vault = ?"
            params.append(vault)
        query += " ORDER BY r.started_at DESC, r.rowid DESC"
        want = note_key(path) if path else None
        for row in self._conn().execute(query, params):
            if want is None or note_key(row["path"]) == want:
                return row["run_id"], row["path"], row["started_at"]
        return None

    def newest_post_hash(self, path: str, vault: str | None) -> str | None:
        """The post-write hash of the newest un-reverted inverse for `path`
        in `vault` (any spelling, via provenance.note_key): what the agent last
        landed there. ADR-0093's draft pass compares it with the note now."""
        from silica.kernel.base.notekey import note_key

        query = ("SELECT i.path, i.post_hash FROM inverses i JOIN runs r ON r.run_id = i.run_id "
                 "WHERE r.reverted_at IS NULL")
        params: list = []
        if vault is not None:
            query += " AND r.vault = ?"
            params.append(vault)
        query += " ORDER BY i.id DESC"
        want = note_key(path)
        for row in self._conn().execute(query, params):
            if note_key(row["path"]) == want:
                return row["post_hash"]
        return None

    def last_chat_move(self, vault: str | None, ref: str | None = None
                       ) -> tuple[str, str, str, float] | None:
        """(run_id, from, to, started_at) of the newest un-reverted move in
        `vault`, the chat's or the tree's (ADR-0126): /undo takes both back. `ref` matches either end, or the folder the note landed in:
        the GUI's moved card sends the call's raw `to`, which may be a folder."""
        from silica.kernel.base.notekey import note_key

        marks = ",".join("?" * len(MOVE_SOURCES))
        query = ("SELECT r.run_id, i.path, i.to_path, r.started_at FROM runs r "
                 "JOIN inverses i ON i.run_id = r.run_id "
                 f"WHERE r.reverted_at IS NULL AND r.source IN ({marks}) AND i.kind = ?")
        params: list = [*MOVE_SOURCES, InverseOpKind.move_back.value]
        if vault is not None:
            query += " AND r.vault = ?"
            params.append(vault)
        query += " ORDER BY r.started_at DESC, r.rowid DESC"
        want = note_key(ref) if ref else None
        for row in self._conn().execute(query, params):
            to = row["to_path"] or ""
            if want is None or want in {note_key(row["path"]), note_key(to),
                                        note_key(to.rsplit("/", 1)[0])}:
                return row["run_id"], row["path"], to, row["started_at"]
        return None

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
        """`{source, vault, started_at, ledger_run_id, reverted_at}` for a run,
        or None when unknown.

        /revert shows this next to the id: the journal's run ids live in a
        different id-space than the progress ledger's (log.md), so a bare id
        never tells the user WHAT they are about to revert. `reverted_at` is
        what lets a surface offer an undo only while there is one.
        """
        row = self._conn().execute(
            "SELECT source, vault, started_at, ledger_run_id, reverted_at FROM runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return {"source": row["source"], "vault": row["vault"],
                "started_at": row["started_at"], "ledger_run_id": row["ledger_run_id"],
                "reverted_at": row["reverted_at"]}

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
                new_hash: str | None = content_hash(current)
            except Exception:
                new_hash = None  # note absent — the stale guard handles it
            conn.execute(
                "UPDATE inverses SET post_hash = ? WHERE run_id = ? AND path = ?",
                (new_hash, run_id, path),
            )
            updated += 1
        conn.commit()
        return updated

    def drop_inverse(self, run_id: str, path: str) -> None:
        """Forget the inverses `run_id` recorded for `path`: the op they would
        undo did not happen. A recorded recreate for a note the driver then
        refused to delete is not inert: revert_run skips it (the note is
        there), and a run with a skip and a restore sweeps the skipped note's
        dangling links, which would edit a note the run never touched."""
        conn = self._conn()
        conn.execute("DELETE FROM inverses WHERE run_id = ? AND path = ?", (run_id, path))
        conn.commit()

    def mark_reverted(self, run_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE runs SET reverted_at = ? WHERE run_id = ?", (time.time(), run_id)
        )
        conn.commit()

    def trashed(self, vault: str | None) -> list[dict]:
        """The trash (ADR-0128): every note a delete took out of `vault` that
        no undo, restore or erase has dealt with, newest first, as
        `{run, path, deleted_at, source}`. One item per delete, so a path
        deleted twice is two items, two different bodies."""
        marks = ",".join("?" * len(DELETE_SOURCES))
        query = ("SELECT r.run_id, i.path, r.started_at, r.source FROM runs r "
                 "JOIN inverses i ON i.run_id = r.run_id "
                 f"WHERE r.reverted_at IS NULL AND r.source IN ({marks}) AND i.kind = ?")
        params: list = [*DELETE_SOURCES, InverseOpKind.recreate_deleted.value]
        if vault is not None:
            query += " AND r.vault = ?"
            params.append(vault)
        query += " ORDER BY r.started_at DESC, i.id DESC"
        return [{"run": r["run_id"], "path": r["path"], "deleted_at": r["started_at"],
                 "source": r["source"]} for r in self._conn().execute(query, params)]

    def before_images(self, path: str, vault: str | None) -> list[dict]:
        """Every body a run found at `path` in `vault` before it wrote over the
        note or deleted it, as `{id, at, kind: "before", source, reverted,
        chars}` with `id` = "j<inverse id>": the part of a note's history that
        /nucleate and the other runs keep, not the checkpoint store (ADR-0144
        point 6). A note a run created has no before-image."""
        from silica.kernel.base.notekey import note_key

        query = ("SELECT i.id, i.path, LENGTH(i.prior_content) AS chars, r.started_at, "
                 "r.source, r.reverted_at FROM inverses i JOIN runs r ON r.run_id = i.run_id "
                 "WHERE i.prior_content IS NOT NULL")
        params: list = []
        if vault is not None:
            query += " AND r.vault = ?"
            params.append(vault)
        want = note_key(path)
        return [{"id": f"j{r['id']}", "at": r["started_at"], "kind": "before",
                 "source": r["source"], "reverted": r["reverted_at"] is not None,
                 "chars": r["chars"]}
                for r in self._conn().execute(query + " ORDER BY i.id DESC", params)
                if note_key(r["path"]) == want]

    def before_image(self, inverse_id: int, path: str, vault: str | None) -> str | None:
        """The body of one before-image of `path` in `vault`; None when the id is not one."""
        from silica.kernel.base.notekey import note_key

        row = self._conn().execute(
            "SELECT i.path, i.prior_content, r.vault FROM inverses i "
            "JOIN runs r ON r.run_id = i.run_id WHERE i.id = ?", (inverse_id,)).fetchone()
        if row is None or note_key(row["path"]) != note_key(path):
            return None
        return row["prior_content"] if vault is None or row["vault"] == vault else None

    def leave_trash(self, run_id: str, path: str) -> None:
        """`path` leaves run `run_id`'s trash, restored or erased: its inverse
        goes, and a run with none left is closed, so the tree's undo of the
        whole run does not report a restore of nothing as one."""
        self.drop_inverse(run_id, path)
        conn = self._conn()
        if conn.execute("SELECT 1 FROM inverses WHERE run_id = ? LIMIT 1", (run_id,)).fetchone() is None:
            self.mark_reverted(run_id)
        # The WAL still holds the page the body was written in until a
        # checkpoint copies the zeroed page over it and truncates the log.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


_store: UndoJournalStore | None = None


def get_undo_journal(path: Path | str | None = None) -> UndoJournalStore:
    global _store
    if _store is None:
        _store = UndoJournalStore(path)
    return _store


import hashlib as _hashlib

from silica.driver import DRIVER


def content_hash(text: str | None) -> str:
    """The post-write hash: sha256 of the read-back text. revert_run compares it
    and the checkpoint store labels by it, so a change of input here (CRLF
    handling, say) must reach every recorded row or the guard refuses them all."""
    return _hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def active_vault() -> str | None:
    """The active vault as a run stamps it and /revert filters by it: one
    spelling, or a run recorded under another is invisible to its own revert."""
    from silica.config import CONFIG

    return (CONFIG.vault_path or "").strip() or None


def written_notes(vault: str, *, store: UndoJournalStore | None = None) -> dict[str, str | None]:
    """{path: newest post-write hash} of every note the journal holds for `vault`, reverted runs
    left out. A note absent from it has no record here: the journal is per machine, keyed by the
    vault's path, and can be pruned."""
    store = store or get_undo_journal()
    conn = store._conn()
    want = Path(vault).resolve()
    run_ids = [r["run_id"] for r in conn.execute(
        "SELECT run_id, vault FROM runs WHERE reverted_at IS NULL AND vault IS NOT NULL"
    ).fetchall() if _same_vault(r["vault"], want)]
    if not run_ids:
        return {}
    marks = ",".join("?" * len(run_ids))
    latest: dict[str, str | None] = {}
    for r in conn.execute(
        f"SELECT path, post_hash FROM inverses WHERE run_id IN ({marks}) ORDER BY id", run_ids
    ).fetchall():
        latest[r["path"]] = r["post_hash"]
    return latest


def edited_since_write(vault: str, *, store: UndoJournalStore | None = None,
                       checkpoints=None) -> list[str]:
    """Notes the gate wrote into `vault` whose current body matches neither
    the journal's post-write hash nor any version a Silica tool checkpointed
    since (spec M4). Unknown to both stores means a hand edit. No ordering
    across the two stores is needed: a note is Silica's if ANY store knows its
    current bytes. Reverted runs are ignored (their notes are the user's
    again); a missing note is not an edit. Reads through DRIVER, so `vault`
    must be the active one, which is how `/stale` calls it."""
    from silica.kernel.write.checkpoints import get_checkpoint_store

    store = store or get_undo_journal()
    cp = checkpoints or get_checkpoint_store()
    edited: list[str] = []
    for path, post_hash in written_notes(vault, store=store).items():
        if not post_hash:
            continue
        try:
            current = content_hash(DRIVER.read_note(path).content)
        except Exception:
            continue
        if current == post_hash or current in cp.hashes_for(path):
            continue
        edited.append(path)
    return sorted(edited)


def _same_vault(recorded: str | None, want: Path) -> bool:
    if not recorded:
        return False
    try:
        return Path(recorded).resolve() == want
    except OSError:
        return False
