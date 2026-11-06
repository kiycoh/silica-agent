# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Per-note checkpoint stack — durable undo for interactive patches.

Every successful interactive patch (silica_patch_note) pushes the resulting
note content onto a per-path stack. ``/undo`` marks the top undone and restores
the note to the new top, walking back one patch at a time. The very first push
for a note also seeds the *original* (pre-patch) content as the immovable floor,
so a chain of undos returns the note to exactly how it was before Silica touched it.

This is deliberately separate from the FSM's transactional snapshot/rollback
(silica_snapshot / silica_restore): that protects a whole pipeline run and is
discarded on success, whereas this is a lightweight, user-facing edit history
for single notes that survives across REPL sessions.

The stack is also the note's history (ADR-0144): an undone row is marked, never
deleted, and an edit the person made outside Silica is saved as a ``hand`` row
before Silica writes over it or undoes past it.

Storage: ~/.silica/checkpoints.db (SQLite), one row per restore point, ordered
by autoincrement id. Keyed by vault-relative path within the row's vault.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from silica.config import silica_home

def _checkpoint_path() -> Path:
    return silica_home() / "checkpoints.db"

# A row is live while it is on the undo chain: `undone = 0`. Rows written before
# ADR-0144 carry no vault and stay visible from every vault, as they always were.
_SCOPE = "path = ? AND (vault IS NULL OR vault = ?)"
_LIVE = f"{_SCOPE} AND undone = 0"


def _current_vault() -> str | None:
    """The vault a row belongs to: the one Silica serves at the call, read late
    because the GUI switches vault inside one process."""
    try:
        from silica.config import CONFIG

        return str(Path(CONFIG.vault_path).resolve()) if CONFIG.vault_path else None
    except Exception:
        return None  # no config yet (a bare test): the row is the legacy, vault-less kind


def _same_text(a: str | None, b: str | None) -> bool:
    """Equal up to line endings and trailing whitespace: a driver that normalises
    either on write must not read as a hand edit (and warn about one) every time."""
    def norm(s: str | None) -> str:
        return (s or "").replace("\r\n", "\n").rstrip()
    return norm(a) == norm(b)


def _outside_kind(path: str, text: str) -> str:
    """`run` when `text` is what a journaled run (/nucleate, Zotero, a
    migration) last wrote at `path`, else `hand`: both land between two of the
    chat's writes, and only one of them is the person editing outside Silica."""
    import hashlib

    try:
        from silica.config import CONFIG
        from silica.kernel.write.undo_journal import get_undo_journal

        post = get_undo_journal().newest_post_hash(path, (CONFIG.vault_path or "").strip() or None)
    except Exception:
        return "hand"  # no journal to ask: the body is still kept, only its label is the general one
    return "run" if post == hashlib.sha256(text.encode("utf-8")).hexdigest() else "hand"


class CheckpointStore:
    """A persistent stack of full-note restore points, keyed by vault path."""

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _checkpoint_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                path       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_path
                ON checkpoints(path);
            """
        )
        # A floor for a note that did not exist before the first write: /undo
        # down to it removes the note. Before 2026-10-11 that floor was "" and
        # undo wrote a 0-byte note that kept taking name links. Stores made
        # before the column read every legacy floor as present.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(checkpoints)")}
        for name, ddl in (("absent", "INTEGER NOT NULL DEFAULT 0"),
                          ("undone", "INTEGER NOT NULL DEFAULT 0"),
                          ("vault", "TEXT"),
                          ("kind", "TEXT NOT NULL DEFAULT 'silica'"),
                          ("of", "TEXT")):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE checkpoints ADD COLUMN {name} {ddl}")
        self._conn.commit()

    # -- internals ---------------------------------------------------------

    def _insert(self, path: str, content: str, absent: bool = False, *,
                kind: str = "silica", undone: bool = False, of: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO checkpoints (path, content, created_at, absent, undone, vault, kind, of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (path, content, time.time(), int(absent), int(undone), _current_vault(), kind, of),
        )
        self._conn.commit()

    def _live(self, path: str, limit: int = 2) -> list[sqlite3.Row]:
        """The newest live rows of ``path``, newest first."""
        return self._conn.execute(
            f"SELECT id, content, absent FROM checkpoints WHERE {_LIVE} ORDER BY id DESC LIMIT ?",
            (path, _current_vault(), limit),
        ).fetchall()

    # -- write -------------------------------------------------------------

    def push(self, path: str, prior_content: str | None, new_content: str,
             kind: str = "silica", of: str | None = None) -> int:
        """Record a restore point after a patch.

        On the first push for ``path`` the original ``prior_content`` is seeded
        as the floor entry before ``new_content``, so undo can reach the
        pre-patch state. ``None`` means the write created the note: the floor
        is then marked absent (see ``absent_floor``). On a later push,
        ``prior_content`` that is not the top is an edit the person made
        outside Silica since its last write: it is saved as a ``hand`` row
        first, or undoing this write would restore the older Silica body over
        it (ADR-0144, reproduced 2026-11-01).

        Returns the resulting stack depth.
        """
        top = self._live(path, 1)
        if not top:
            # A person's write seeds their own text: `hashes_for` must not take it for Silica's.
            self._insert(path, prior_content or "", absent=prior_content is None,
                         kind="hand" if kind == "hand" else "original")
        elif prior_content is not None and not _same_text(prior_content, top[0]["content"]):
            self._insert(path, prior_content, kind=_outside_kind(path, prior_content))
        self._insert(path, new_content, kind=kind, of=of)
        return self.depth(path)

    # -- read --------------------------------------------------------------

    def depth(self, path: str) -> int:
        """Number of live restore points stored for a note (floor included)."""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM checkpoints WHERE {_LIVE}", (path, _current_vault())
        ).fetchone()
        return int(row["n"]) if row else 0

    def hashes_for(self, path: str) -> set[str]:
        """sha256 of every version this store holds for ``path``: the set of
        bodies Silica itself wrote or restored, which is what separates a hand
        edit from a tool write (undo_journal.edited_since_write). ``hand`` rows
        are the person's, so they stay out; undone rows were Silica's and stay in."""
        import hashlib
        rows = self._conn.execute(
            f"SELECT content FROM checkpoints WHERE {_SCOPE} AND kind != 'hand'",
            (path, _current_vault()),
        ).fetchall()
        return {hashlib.sha256((r["content"] or "").encode("utf-8")).hexdigest() for r in rows}

    def absent_floor(self, path: str) -> bool:
        """True when only the floor is left and it stands for a note that did
        not exist: the caller removes the note instead of writing "" to it."""
        rows = self._live(path, 2)
        return len(rows) == 1 and bool(rows[0]["absent"])

    def undo_removes(self, path: str) -> bool:
        """True when the next ``undo`` of ``path`` leaves only an absent floor,
        i.e. /undo <path> deletes the note. A write card labels its button
        from this, the state the click will read: the session ledger's
        "created" is the first touch by this process, and a note nucleated
        earlier in the process and patched later read "created ... delete"
        over a click that only took back the patch (2026-10-26)."""
        rows = self._live(path, 3)
        return len(rows) == 2 and bool(rows[1]["absent"])

    def latest_at(self, path: str) -> float | None:
        """When the newest restore point for ``path`` was taken: /undo <path>
        compares it with a chat move that landed the note there."""
        row = self._conn.execute(
            f"SELECT MAX(created_at) AS t FROM checkpoints WHERE {_LIVE}", (path, _current_vault())
        ).fetchone()
        return row["t"] if row else None

    def most_recent(self) -> tuple[str, float] | None:
        """(path, created_at) of the newest restore point: /undo compares it
        with the newest chat delete to take back whichever came last."""
        row = self._conn.execute(
            "SELECT path, created_at FROM checkpoints WHERE undone = 0 "
            "AND (vault IS NULL OR vault = ?) ORDER BY id DESC LIMIT 1", (_current_vault(),)
        ).fetchone()
        return (row["path"], row["created_at"]) if row else None

    def most_recent_path(self) -> str | None:
        """Vault path of the most recently pushed checkpoint, or None if empty.

        Backs ``/undo`` with no argument, including after a restart.
        """
        recent = self.most_recent()
        return recent[0] if recent else None

    def changed_since(self, path: str, current: str | None) -> str | None:
        """How the note on disk came to differ from its newest live version:
        `hand` (edited outside Silica), `run` (a journaled run wrote it), or
        None when it does not differ."""
        top = self._live(path, 1)
        if not top or current is None or _same_text(current, top[0]["content"]):
            return None
        return _outside_kind(path, current)

    def settle(self, path: str, landed: str) -> None:
        """Make the newest live version the text the driver actually left: a
        restore through overwrite gains the derived `type:` stamp, and a top
        that is not the disk reads as an outside edit at the next write."""
        top = self._live(path, 1)
        if top and top[0]["content"] != landed:
            self._conn.execute("UPDATE checkpoints SET content = ? WHERE id = ?", (landed, top[0]["id"]))
            self._conn.commit()

    def history(self, path: str, current: str | None = None) -> list[dict]:
        """Every version of ``path`` in this vault, newest first, undone ones
        included: ``{id, at, kind, undone, absent, chars, now}``, and ``of``
        (the version it brought back) on a restore. The oldest row is always
        the floor, whatever kind a store older than ADR-0144 wrote. ``now``
        marks the newest live version when it is ``current``, the note on disk."""
        rows = self._conn.execute(
            f"SELECT id, content, created_at, absent, undone, kind, of FROM checkpoints "
            f"WHERE {_SCOPE} ORDER BY id", (path, _current_vault()),
        ).fetchall()
        live = [r for r in rows if not r["undone"]]
        now_id = live[-1]["id"] if live and current is not None and _same_text(
            live[-1]["content"], current) else None
        out = [{"id": r["id"], "at": r["created_at"],
                "kind": "original" if i == 0 else r["kind"],
                "undone": bool(r["undone"]), "absent": bool(r["absent"]),
                "chars": len(r["content"] or ""), "now": r["id"] == now_id,
                **({"of": r["of"]} if r["of"] is not None else {})}
               for i, r in enumerate(rows)]
        return out[::-1]

    def version(self, path: str, version_id: int) -> str | None:
        """The body of one version of ``path`` in this vault; None when the id
        is not one of its rows, or the row stands for a note that did not exist."""
        row = self._conn.execute(
            f"SELECT content, absent FROM checkpoints WHERE id = ? AND {_SCOPE}",
            (version_id, path, _current_vault()),
        ).fetchone()
        return None if row is None or row["absent"] else row["content"]

    # -- undo --------------------------------------------------------------

    def undo(self, path: str, current: str | None = None) -> str | None:
        """Mark the top restore point undone and return the content to restore to.

        Returns the content of the new top (the previous patch's result, or the
        original floor). The floor is never undone: once only it remains there
        is nothing left to undo and this returns None. The caller is
        responsible for overwriting the note with the returned content.

        ``current`` is the note as it is on disk. When it is not the top, the
        person edited it after Silica's write: it is saved as an undone ``hand``
        version, so the undo takes back the write and those edits together and
        the edits stay in the history (ADR-0144 point 3).
        """
        live = self._live(path, 2)
        if len(live) <= 1:
            return None
        if current is not None and not _same_text(current, live[0]["content"]):
            self._insert(path, current, kind=_outside_kind(path, current), undone=True)
        self._conn.execute("UPDATE checkpoints SET undone = 1 WHERE id = ?", (live[0]["id"],))
        self._conn.commit()
        return live[1]["content"]

    def clear(self, path: str) -> None:
        """Drop all restore points for a note. Runs only after an undo removed a
        note the chat created, whose body the trash keeps (ADR-0128)."""
        self._conn.execute(f"DELETE FROM checkpoints WHERE {_SCOPE}", (path, _current_vault()))
        self._conn.commit()


def push_landed(path: str, prior: str | None, asked: str, kind: str = "silica",
                of: str | None = None) -> int:
    """Push what the driver left on disk after a write, not what the caller
    asked for: the driver stamps a derived `type:` on overwrite, and a version
    that is not the disk read as a hand edit at the next /undo (reproduced
    2026-11-01: /verify then /undo warned of edits nobody made)."""
    from silica.driver import DRIVER

    try:
        landed = DRIVER.read_note(path).content
    except Exception:
        landed = asked  # unreadable right after its own write: keep what was asked
    return get_checkpoint_store().push(path, prior, landed, kind=kind, of=of)


_store: CheckpointStore | None = None


def get_checkpoint_store(path: Path | str | None = None) -> CheckpointStore:
    global _store
    if _store is None:
        _store = CheckpointStore(path)
    # shortcut: no eviction, every version kept; revisit when one tenant's store
    # passes 100 MB (1.4 MB on the owner's machine 2026-11-01, ADR-0144 point 8).
    return _store
