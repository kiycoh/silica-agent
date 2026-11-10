# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""WorkQueue — the producer/consumer channel between the Injector and sub-agents.

The Injector (router) *produces* WorkItems as it commits batches; a pool of
leashed sub-agents *consumes* them concurrently on the worker model.  The router
never blocks on a sub-agent: it enqueues fire-and-forget and the Coordinator
drains/joins the queue only at the end of the run.

Concurrency safety has two layers:
  1. Temporal disjointness — items reference only notes from already-committed
     batches (or pre-existing vault notes), so the router is never mid-write on a
     sub-agent's target.
  2. Per-path lease — `path_lease()` serialises writes to the same note across
     sub-agents (and any lease-aware writer).  Both the Injector and the pool run
     as threads in one process, so an in-process lock registry is sufficient.

WorkItem kinds are owned by the capability registry — see
silica/capabilities/__init__.py (CAPABILITIES), the single dispatch table for
background work. tests/test_capability_seam.py enforces that every kind
produced anywhere has a registered capability.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Generic, Iterator, TypeVar
from uuid import uuid4

import orjson

from silica.kernel.base.notekey import ledger_key

try:  # POSIX only; Windows falls back to the in-process lock alone.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-path advisory lease (shared across the Injector, the sub-agent pool, and
# any *other process* pointed at the same vault — e.g. the GUI and a CLI run)
# ---------------------------------------------------------------------------

_LEASES: dict[str, threading.Lock] = {}
_LEASES_GUARD = threading.Lock()


def _lock_file(key: str) -> Path | None:
    """Vault-scoped lock file for `key`; None when fcntl is unavailable.

    Lives under the per-vault index namespace so every process resolving the
    same vault agrees on the same file; the filename is a hash of the key so
    deep paths and separators never leak into the filesystem.
    """
    if fcntl is None:
        return None
    from silica.kernel.base.home import index_dir

    d = index_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".lock")


@contextmanager
def path_lease(path: str) -> Iterator[None]:
    """Serialise writes to a single vault note across threads *and processes*.

    Two layers: an in-process ``threading.Lock`` (fast path, and it makes the
    OS lock contention-free between threads of one process) plus an advisory
    ``flock`` on a vault-scoped lock file so separate processes writing the
    same vault (the GUI and the REPL, either + a running pipeline) cannot interleave.

    Advisory: only protects writers that opt in by acquiring the same lease.
    Lock-file bookkeeping is best-effort — if it fails we log and fall back to
    in-process-only rather than break the write (the threading lock still holds
    for same-process writers). ``commit_ops`` acquires multiple leases in sorted
    order, so cross-process acquisition order is consistent and deadlock-free.
    """
    key = ledger_key(path)
    with _LEASES_GUARD:
        lock = _LEASES.setdefault(key, threading.Lock())
    lock.acquire()
    fd: int | None = None
    try:
        try:
            lf = _lock_file(key)
            if lf is not None:
                fd = os.open(str(lf), os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            logger.warning("path_lease: cross-process lock unavailable for %s (%s); "
                           "in-process lock only", key, e)
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
        lock.release()


class _Job:
    def to_dict(self) -> dict[str, Any]:
        """The context a persisted item carries in workqueue.json."""
        return asdict(self)  # type: ignore[call-overload]

    def provenance(self) -> tuple[str, str, str] | None:
        """(source key, content sha, ledger run id) the notes this job writes are
        filed under, None for a job whose notes come from no source. A job type
        answers for itself, so a new worker never edits the consumer."""
        return None


def _provenance(src: str, content_hash: str, run_id: str) -> tuple[str, str, str] | None:
    """The run id is the FSM's own `progress.run_id`: the key CLEANUP stamps and
    the key the dangling-link sweep and check_renucleate read back. It is NOT the
    undo journal's run id, which is a separate uuid4 keyspace."""
    from silica.kernel.base.manifest import source_key

    return (source_key(src), content_hash, run_id) if src and content_hash else None


@dataclass(kw_only=True)
class DedupJob(_Job):
    """One incoming concept, judged against the item's target note.

    The fields without a default have none on purpose: a builder that forgets
    one is refused here. COLLISION once left out `run_id`, nothing noticed, and
    its workers' notes reached the provenance ledger under the undo journal's id.
    """
    concept: str
    excerpt: str
    candidate: str
    score: float
    full_score: float | None = None
    title_score: float | None = None
    inbox_file: str
    hub: str | None
    content_hash: str
    run_id: str
    # Where a distinct concept's spoke goes; "" (two existing notes) writes none.
    target_dir: str
    # A real note on the losing side: a duplicate verdict marks it superseded
    # and a distinct one settles it. "" for a concept that was never written.
    loser_path: str = ""

    @classmethod
    def between_notes(cls, *, loser: str, winner: str, loser_body: str, score: float,
                      full_score: float | None = None,
                      title_score: float | None = None) -> "DedupJob":
        """Two notes already in the vault (/dedup, /curate): no run, no source.
        The loser stands where the FSM path has a source document, and being a
        real note it can be marked on merge."""
        stem = lambda p: p.removesuffix(".md").rsplit("/", 1)[-1]  # noqa: E731
        return cls(concept=stem(loser), excerpt=loser_body[:4000], candidate=stem(winner),
                   score=score, full_score=full_score, title_score=title_score,
                   inbox_file=loser, loser_path=loser,
                   hub=None, content_hash="", run_id="", target_dir="")

    def provenance(self) -> tuple[str, str, str] | None:
        # The inbox path, so the key is the path under the inbox (ADR-0114).
        return _provenance(self.inbox_file, self.content_hash, self.run_id)

    @property
    def effective_score(self) -> float:
        full = self.score if self.full_score is None else self.full_score
        return max(full or 0.0, self.title_score or 0.0)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key in ("full_score", "title_score"):
            if out[key] is None:
                del out[key]
        if not self.loser_path:
            del out["loser_path"]
        return out


_BATCH_CONCEPT_KEYS = ("concept", "excerpt", "score", "full_score", "title_score", "inbox_file", "loser_path")


@dataclass
class DedupFamily(_Job):
    """Jobs sharing a candidate note, judged in one call and routed one by one.
    The candidate body is the bulk of the judge's prompt, so a family of N
    judged per-item repeats it N times."""
    members: list[DedupJob]

    def to_dict(self) -> dict[str, Any]:
        rows = [m.to_dict() for m in self.members]
        shared = {k: v for k, v in rows[0].items() if k not in _BATCH_CONCEPT_KEYS}
        shared["concepts"] = [{k: r[k] for k in _BATCH_CONCEPT_KEYS if k in r} for r in rows]
        return shared

    def provenance(self) -> tuple[str, str, str] | None:
        # One batch is always one file (the sha it carries is
        # fsm.current_content_hash), so the first member answers for all.
        return next((m for m in self.members if m.inbox_file), self.members[0]).provenance()


@dataclass(kw_only=True)
class ExpandJob(_Job):
    """A write op rejected as too short, for the worker to author a body for."""
    op: dict[str, Any]
    excerpt: str
    reason: str
    inbox_file: str
    hub: str | None
    content_hash: str
    run_id: str
    target_dir: str

    def provenance(self) -> tuple[str, str, str] | None:
        # Only the op's source_basename, which the ledger reads as that file
        # when the vault holds one of the name.
        return _provenance(self.inbox_file or self.op.get("source_basename") or "",
                           self.content_hash, self.run_id)


@dataclass(kw_only=True)
class OrphanJob(_Job):
    """A note with no incoming link, and the notes it may be linked from."""
    candidates: list[dict]
    hub: str | None = None


JobT = TypeVar("JobT")


@dataclass
class WorkItem(Generic[JobT]):
    kind: str                       # "dedup" | "enrich" | "orphan" | "expand"
    target_path: str                # vault-rel note the sub-agent may write
    # The kind's job; a plain dict for the kinds with no fields of their own to
    # declare (enrich's hub, a worker profile's goal and inputs).
    context: JobT = field(default_factory=dict)  # type: ignore[assignment]
    reason: str = ""                # the FSM warning that produced this item
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "pending"         # pending | done | failed | rejected
    result: dict[str, Any] | None = None
    # Runtime-only: not serialised. Set by caller to request early exit.
    cancel_token: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
        compare=False,
        hash=False,
    )

    def provenance(self) -> tuple[str, str, str] | None:
        """The job's (_Job.provenance); None for a plain-dict context."""
        return self.context.provenance() if isinstance(self.context, _Job) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target_path": self.target_path,
            "context": self.context.to_dict() if isinstance(self.context, _Job) else self.context,
            "reason": self.reason,
            "id": self.id,
            "status": self.status,
            "result": self.result,
        }


_SENTINEL = object()  # poison pill — injected by close() to unblock consumers


class WorkQueue:
    """Thread-safe producer/consumer queue with optional disk persistence."""

    def __init__(self, run_dir: str | Path | None = None):
        self._q: queue.Queue = queue.Queue()
        self._items: list[WorkItem] = []        # every item ever enqueued (feeds summary/inspection)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._pending: int = 0                  # items enqueued but not yet complete
        # Tolerate a non-path run_dir (e.g. a mocked progress object): persistence
        # is best-effort, so fall back to no-disk rather than raising.
        try:
            self._run_dir = Path(run_dir) if isinstance(run_dir, (str, Path)) else None
        except (TypeError, ValueError):
            self._run_dir = None

    # --- producer side ----------------------------------------------------

    def enqueue(self, item: WorkItem) -> None:
        """Add an item. No-op after close() (producers have finished)."""
        if self._closed.is_set():
            return
        with self._lock:
            self._items.append(item)
            self._pending += 1
        self._q.put(item)
        self._persist()

    def close(self) -> None:
        """Signal that no further items will be produced.

        Injects a sentinel into the queue so blocking consumers wake up and
        drain cleanly.  The sentinel cascades: each consumer that receives it
        rebroadcasts it before returning, waking the next consumer.
        """
        self._closed.set()
        self._q.put(_SENTINEL)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # --- consumer side ----------------------------------------------------

    def claim(self, *, timeout: float | None = None) -> WorkItem | None:
        """Pop the next item.

        With ``timeout=None`` (default): blocks at OS level until an item
        arrives or the queue is closed (sentinel-based — no polling).
        Returns None only when the queue has been closed and is empty.

        With ``timeout=<float>``: legacy mode — returns None if no item
        arrives within that many seconds (backward-compatible with tests).
        """
        if timeout is None:
            item = self._q.get()
        else:
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                return None

        if item is _SENTINEL:
            self._q.put(_SENTINEL)  # rebroadcast so the next consumer wakes up
            return None
        return item

    def complete(self, item: WorkItem, status: str, result: dict[str, Any] | None = None) -> None:
        """Mark an item finished and record the outcome."""
        with self._lock:
            item.status = status
            item.result = result
            self._pending -= 1
        self._persist()

    def drained(self) -> bool:
        """True when producers are done and every queued item is consumed."""
        with self._lock:
            return self._closed.is_set() and self._pending == 0

    # --- inspection / persistence ----------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def items(self) -> list[WorkItem]:
        with self._lock:
            return list(self._items)

    def summary(self) -> dict[str, int]:
        """Counts by status, for the final run report."""
        with self._lock:
            return dict(Counter(it.status for it in self._items))

    def _persist(self) -> None:
        # Atomic (tmp + rename): a crash mid-dump must not leave the one
        # post-mortem artifact torn.
        # Write-only debug dump — workqueue.json has no reader/from_dict, so
        # this is post-mortem inspection only; a loader arrives with resume,
        # if resume is ever built (until then, no resume path exists).
        if not self._run_dir:
            return
        try:
            from silica.kernel.base.durable import atomic_write_bytes

            self._run_dir.mkdir(parents=True, exist_ok=True)
            # The rename stays INSIDE the lock: snapshotting under it and
            # replacing outside lets a thread that snapshotted an older list
            # win the race to os.replace and clobber a fresher dump with a
            # stale one — atomic per write, lost update across writes.
            with self._lock:
                payload = [it.to_dict() for it in self._items]
                atomic_write_bytes(
                    self._run_dir / "workqueue.json",
                    orjson.dumps(payload, option=orjson.OPT_INDENT_2),
                )
        except Exception:
            # Persistence is best-effort; never break the pipeline over it.
            pass


# Fixed cap bounds the batch prompt; raise it the day real families outgrow it.
_MAX_FAMILY_BATCH = 8


def batch_dedup_items(items: list[WorkItem]) -> list[WorkItem]:
    """Collapse dedup WorkItems that share a candidate note into family batches.

    Grouping key is ``target_path``: COLLISION's borderline concepts and
    /curate's union-find duplicate families both converge there by
    construction. The candidate body is the bulk of the judge's prompt, so a
    family of N concepts judged per-item repeats it N times; one batch item is
    one judge call (the dedup capability fans the verdicts back out to the
    family's members). Singletons and non-dedup items pass through
    untouched, so callers can apply this unconditionally before dispatch.

    Lives kernel-side (not in the dedup capability) so both producers — the
    router's COLLISION state and /curate — reach it without importing the
    capabilities package, which would cycle through the P9 peer boundary.
    """
    out: list[WorkItem] = []
    groups: dict[str, list[WorkItem]] = {}
    for it in items:
        if it.target_path and isinstance(it.context, DedupJob):
            groups.setdefault(it.target_path, []).append(it)
        else:
            out.append(it)
    for path, group in groups.items():
        for i in range(0, len(group), _MAX_FAMILY_BATCH):
            chunk = group[i:i + _MAX_FAMILY_BATCH]
            if len(chunk) == 1:
                out.append(chunk[0])
                continue
            out.append(WorkItem(
                kind="dedup",
                target_path=path,
                context=DedupFamily([it.context for it in chunk]),
                reason=f"dedup_family n={len(chunk)} → {path.rsplit('/', 1)[-1]}",
            ))
    return out
