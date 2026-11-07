# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Durable state: torn-write-proof files and the on-disk index stores built on them."""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path


try:  # POSIX only; see index_lock for what Windows gives up.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# The mode an ordinary `open(path, "w")` would have produced, snapshotted once:
# mkstemp opens 0600, so a file this helper CREATES would otherwise be private
# where every other tool's would not. Read at import (before the pools start)
# because os.umask is process-global and read-modify-write — probing it later
# would hand a concurrent creator a 0666 window.
_UMASK = os.umask(0o022)  # os.umask only reads by writing; put it straight back
os.umask(_UMASK)


_NEW_FILE_MODE = 0o666 & ~_UMASK


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Torn-write-proof write: tmp file in the same dir, fsync, os.replace.

    For derived indexes and bundles rewritten in place — a crash or full
    disk mid-write must leave the previous file intact, not a truncated one.

    Four edges beyond the naive tmp+replace (each one a fielded failure in
    graphify's sibling helper): resolve symlinks first so the tmp lands on the
    destination's filesystem and the write goes THROUGH the link instead of
    replacing it; preserve an existing destination's mode, and give a brand-new
    one the umask default, instead of leaving mkstemp's 0600 — notes are the
    user's own files and an external syncer or a second account has to keep the
    access an ordinary create would have granted; and when os.replace hits a
    locked destination (Windows: antivirus or an open reader) retry briefly —
    the hold is transient — and only then degrade to copy2, which is NOT atomic
    (it truncates before it copies), so the degradation is logged instead of
    silently voiding the guarantee this function is named after.
    """
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=real.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # `mode` overrides both for a file whose secrecy outranks that access:
        # the .env holds every API key, and the umask default is 0664 under
        # the common 0002. Set on the tmp, so the replace never exposes it.
        if mode is None:
            try:
                mode = os.stat(real).st_mode & 0o7777
            except OSError:
                mode = _NEW_FILE_MODE  # the destination does not exist yet
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        try:
            os.replace(tmp, real)
        except PermissionError:
            import time
            for _ in range(3):
                time.sleep(0.05)
                try:
                    os.replace(tmp, real)
                    break
                except PermissionError:
                    continue
            else:
                logger.warning(
                    "atomic_write_bytes: %s still locked after retries — "
                    "falling back to a non-atomic copy (torn-write window "
                    "while the copy runs)", real)
                shutil.copy2(tmp, real)
                os.unlink(tmp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # ponytail: no directory fsync — post-power-loss rename durability is
    # filesystem-dependent, and every caller's file is rebuildable or
    # re-produceable; upgrade if a real loss is ever traced here.


def quarantine(path: Path) -> Path | None:
    """Rename a corrupt state file aside — never clobbered, never deleted.

    Derived stores rebuild from empty afterwards; authoritative stores keep
    the bytes here for manual inspection. `silica doctor` surfaces any
    `*.corrupt.*` file it finds. Returns the quarantine path, or None if the
    rename itself failed (callers treat that as "proceed anyway": read paths
    must not raise).
    """
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(f"{path.name}.corrupt.{stamp}")
    n = 0
    while dest.exists():  # same-second collision: bump, never overwrite
        n += 1
        dest = path.with_name(f"{path.name}.corrupt.{stamp}.{n}")
    try:
        path.rename(dest)
        return dest
    except OSError:
        return None


def build_postings(docs: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Invert ``{doc: {term: count}}`` into the postings index ``{term: {doc: count}}``.
    The shared inner loop of every term/stem postings build (co-occurrence, lexical)."""
    idx: dict[str, dict[str, int]] = {}
    for doc, terms in docs.items():
        for term, count in terms.items():
            idx.setdefault(term, {})[doc] = count
    return idx


def path_keyed_singleton(cache: dict, key: str, factory):
    """Return cache[key], building it via factory() on first access.

    The shared shape behind every per-index-path store singleton (embed,
    co-occurrence, lexical): keying by resolved index path follows a /vault
    switch automatically. Callers own the cache dict (and its clear()), and
    call `sync_from_disk()` on what they get back (see DiskSynced)."""
    inst = cache.get(key)
    if inst is None:
        inst = factory()
        cache[key] = inst
    return inst


def disk_stamp(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of an index file, None when it does not exist.

    The change signal behind DiskSynced. Any difference counts, not just a
    newer mtime: a restored backup moves it backwards, the same rule the
    index sweep's stamps and the driver's roster already follow. Size is the
    tie-break for a rewrite landing on the same timestamp (coarse
    filesystems round mtime to a second or worse).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@contextlib.contextmanager
def index_lock(path: Path) -> Iterator[None]:
    """Advisory flock held across one read-merge-write of an index file.

    The lock file is derived from the index path alone (`<dir>/locks/<name>`),
    NOT from `workqueue.path_lease`, whose lock dir comes from the CURRENT
    vault: the save that most needs the lock is the atexit flush of a store
    whose vault was already switched away from, and two processes have to
    agree on the file without consulting config. Without fcntl (Windows) the
    write proceeds unlocked: the cross-process race then narrows to the
    serialize window instead of closing. A lock-file failure is logged and
    degrades the same way rather than failing the save.
    """
    if fcntl is None:
        yield
        return
    real = Path(os.path.realpath(path))
    fd: int | None = None
    try:
        try:
            lock_dir = real.parent / "locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_dir / (real.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            logger.warning("index_lock: cross-process lock unavailable for %s (%s)", real.name, e)
            if fd is not None:
                os.close(fd)
                fd = None
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class DiskSynced:
    """Keeps a process-singleton index store honest against other processes.

    `path_keyed_singleton` hands every caller in a process ONE store, and that
    store read its file exactly once. So a second Silica process writing the
    same index (`silica nucleate` in a terminal while the GUI is up) was
    invisible to the first until restart, and the first's
    next save() then wrote its stale memory over the second's work. Last
    writer won, and nothing logged it.

    Two rules, no daemon, no database:
      * a `disk_stamp` of the file is taken at every load and save; a
        different stamp on the next access means someone else wrote, and the
        store re-reads the file and lays its own unsaved entries back on top
        (`_take_disk`), so memory = disk + mine, mine winning per key;
      * save() is read-merge-write under `index_lock`, never a blind
        overwrite.

    Subclasses own the dirty bookkeeping: which keys they upserted or dropped
    since the last sync, as live sets returned by `_dirty_sets()`. The mixin
    empties them once a save lands and refills them if it does not. Hooks:
    `_read_disk()` (file -> state, empty on absent/corrupt, never raises),
    `_take_disk(state)` (replace own state with it, overlay dirty),
    `_snapshot()` (under `_lock`: a consistent view), `_serialize(snapshot)`
    (outside it: the slow part, hundreds of ms on a big index).
    """
    _path: Path
    _lock: "threading.RLock"
    _disk_stamp: tuple[int, int] | None = None

    def _read_disk(self):  # pragma: no cover - contract
        raise NotImplementedError

    def _take_disk(self, state) -> None:  # pragma: no cover - contract
        raise NotImplementedError

    def _snapshot(self):  # pragma: no cover - contract
        raise NotImplementedError

    def _serialize(self, snapshot) -> bytes:  # pragma: no cover - contract
        raise NotImplementedError

    def _dirty_sets(self) -> tuple[set, ...]:  # pragma: no cover - contract
        raise NotImplementedError

    def _load(self) -> None:
        """First read, from __init__. Unconditional: `sync_from_disk` would
        skip an absent file, and the embed store still has a legacy path to
        fall back to on exactly that case."""
        self._disk_stamp = disk_stamp(self._path)
        self._take_disk(self._read_disk())

    def sync_from_disk(self) -> bool:
        """Re-read the file if another process wrote it. True when it did.

        One stat when nothing changed; the accessors call this on every
        lookup, so that is the price of the guarantee.
        """
        seen = disk_stamp(self._path)
        if seen == self._disk_stamp:
            return False
        with self._lock:
            if seen == self._disk_stamp:  # a sibling thread took it first
                return False
            # Stamp BEFORE reading. A write landing between the two leaves
            # the stamp older than the file, so the next access re-reads (one
            # redundant load). The reverse order would record a file we never
            # read and hide that write until the one after it.
            self._disk_stamp = seen
            self._take_disk(self._read_disk())
        return True

    def is_dirty(self) -> bool:
        """True when this store holds something the file does not.

        A read of exactly what save() would write, from the bookkeeping
        subclasses already keep. It exists because a no-op save is not free and
        not silent: rewriting an unchanged index still moves the file's mtime,
        and `vault_version()` digests those mtimes to tell the GUI that the
        vault moved under its cached views. The co-occurrence refresh that every
        graph export runs made the strip announce "vault changed" after every
        build of the graph it had just drawn, and rewrote 9.5 MB to say it.
        """
        return any(self._dirty_sets())

    def save(self) -> Path:
        with index_lock(self._path):
            with self._lock:
                self.sync_from_disk()
                snapshot = self._snapshot()
                sets = self._dirty_sets()
                saved = [set(s) for s in sets]
                # Emptied IN PLACE, not rebound: a mutation racing the write
                # below adds to this same set and stays dirty, because it is
                # not in the snapshot being written.
                for s in sets:
                    s.clear()
            try:
                atomic_write_bytes(self._path, self._serialize(snapshot))
            except BaseException:
                with self._lock:
                    for s, back in zip(sets, saved):
                        s |= back
                raise
            with self._lock:
                # Under the file lock, so the stamp is of OUR file: a reader
                # thread that synced between the write and here recorded the
                # same one and overlaid the live dirty set, which is exactly
                # the state this process should hold.
                self._disk_stamp = disk_stamp(self._path)
        return self._path
