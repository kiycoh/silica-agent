"""Tests for the WorkQueue + per-path lease (silica/kernel/workqueue.py)."""
import json
import threading
import time

import pytest

from silica.kernel.workqueue import (
    DedupFamily, DedupJob, ExpandJob, OrphanJob, WorkItem, WorkQueue, path_lease,
)
from tests.jobs import dedup_job


def test_enqueue_claim_complete_roundtrip():
    q = WorkQueue()
    item = WorkItem(kind="dedup", target_path="A.md", reason="borderline")
    q.enqueue(item)
    assert len(q) == 1

    claimed = q.claim(timeout=0.1)
    assert claimed is item
    q.complete(claimed, "done", {"patched": True})
    assert claimed.status == "done"
    assert q.summary() == {"done": 1}


def test_claim_returns_none_when_empty():
    q = WorkQueue()
    assert q.claim(timeout=0.05) is None


def test_close_blocks_further_enqueue_and_enables_drain():
    q = WorkQueue()
    q.enqueue(WorkItem(kind="refine", target_path="A.md"))
    q.close()
    assert q.closed
    # enqueue after close is a no-op
    q.enqueue(WorkItem(kind="refine", target_path="B.md"))
    assert len(q) == 1
    # not drained until the item is consumed
    assert not q.drained()
    it = q.claim(timeout=0.1)
    q.complete(it, "done")
    assert q.drained()


def test_persistence_to_run_dir(tmp_path):
    q = WorkQueue(run_dir=tmp_path)
    q.enqueue(WorkItem(kind="dedup", target_path="A.md"))
    assert (tmp_path / "workqueue.json").exists()


def test_path_lease_serialises_concurrent_writers():
    order: list[str] = []
    started = threading.Event()

    def worker(tag: str, hold: float):
        with path_lease("Notes/Same.md"):
            order.append(f"{tag}-enter")
            started.set()
            time.sleep(hold)
            order.append(f"{tag}-exit")

    t1 = threading.Thread(target=worker, args=("A", 0.2))
    t1.start()
    started.wait(timeout=1.0)
    t2 = threading.Thread(target=worker, args=("B", 0.0))
    t2.start()
    t1.join()
    t2.join()

    # B must not enter the same-path critical section before A exits.
    assert order == ["A-enter", "A-exit", "B-enter", "B-exit"]


def test_path_lease_independent_paths_do_not_block():
    # Different paths use different locks → both can hold simultaneously.
    both_in = threading.Barrier(2, timeout=1.0)

    def worker(path: str):
        with path_lease(path):
            both_in.wait()  # raises BrokenBarrierError if the other never arrives

    t1 = threading.Thread(target=worker, args=("A.md",))
    t2 = threading.Thread(target=worker, args=("B.md",))
    t1.start(); t2.start(); t1.join(); t2.join()
    # No exception ⇒ both entered concurrently.


def test_lease_key_normalises_md_and_case():
    # "A.md" and "a" should map to the same lease.
    acquired_second = threading.Event()

    def holder():
        with path_lease("A.md"):
            time.sleep(0.15)

    t = threading.Thread(target=holder)
    t.start()
    time.sleep(0.03)
    t2_start = time.monotonic()
    with path_lease("a"):
        waited = time.monotonic() - t2_start
        acquired_second.set()
    t.join()
    # Acquiring "a" had to wait for "A.md" to release.
    assert waited >= 0.1


def test_persist_is_atomic_under_concurrent_enqueue(tmp_path):
    """Two threads racing on enqueue must not produce a stale workqueue.json."""
    wq = WorkQueue(run_dir=tmp_path)
    barrier = threading.Barrier(2)

    def enqueue_many(items):
        barrier.wait()
        for item in items:
            wq.enqueue(WorkItem(kind="dedup", target_path=item, context=dedup_job({}), reason="test"))

    t1 = threading.Thread(target=enqueue_many, args=(["a.md", "b.md", "c.md"],))
    t2 = threading.Thread(target=enqueue_many, args=(["d.md", "e.md", "f.md"],))
    t1.start(); t2.start()
    t1.join(); t2.join()

    saved = json.loads((tmp_path / "workqueue.json").read_bytes())
    assert len(saved) == 6, f"Expected 6 items, got {len(saved)}: {[s['target_path'] for s in saved]}"


# ---------------------------------------------------------------------------
# The typed jobs: refused when incomplete, unchanged on disk
# ---------------------------------------------------------------------------

def test_a_dedup_job_without_its_run_id_is_refused():
    """COLLISION once built its items by hand without `run_id`, and nothing
    noticed: the workers' notes went to the ledger under the journal's id."""
    with pytest.raises(TypeError, match="run_id"):
        DedupJob(concept="A", excerpt="e", candidate="B", score=0.7, inbox_file="in.md",
                 hub="Hub", content_hash="h", target_dir="T")


def _origin():
    return {"hub": "Hub", "content_hash": "h", "run_id": "r", "target_dir": "T"}


def test_workqueue_json_keeps_its_shape(tmp_path):
    """Key for key and in order, as a run wrote them before the jobs were typed."""
    collision = DedupJob(concept="A", excerpt="e", candidate="B", score=0.7,
                         inbox_file="in.md", **_origin())
    landed = DedupJob(concept="C", excerpt="f", candidate="B", score=0.0, title_score=0.9,
                      inbox_file="in.md", loser_path="T/C.md", **_origin())
    q = WorkQueue(run_dir=tmp_path)
    q.enqueue(WorkItem(kind="dedup", target_path="T/B.md", context=collision))
    q.enqueue(WorkItem(kind="dedup", target_path="T/B.md", context=DedupFamily([collision, landed])))
    q.enqueue(WorkItem(kind="expand", target_path="T/D.md", context=ExpandJob(
        op={"heading": "D"}, excerpt="x", reason="snippet too short", inbox_file="in.md", **_origin())))
    q.enqueue(WorkItem(kind="orphan", target_path="T/E.md",
                       context=OrphanJob(candidates=[{"name": "F", "path": "F.md"}], hub="Hub")))
    q.enqueue(WorkItem(kind="enrich", target_path="T/G.md", context={"hub": "Hub"}))

    rows = json.loads((tmp_path / "workqueue.json").read_text())
    assert list(rows[0]) == ["kind", "target_path", "context", "reason", "id", "status", "result"]
    contexts = [r["context"] for r in rows]
    assert list(contexts[0].items()) == [
        ("concept", "A"), ("excerpt", "e"), ("candidate", "B"), ("score", 0.7), ("inbox_file", "in.md"),
        ("hub", "Hub"), ("content_hash", "h"), ("run_id", "r"), ("target_dir", "T")]
    assert list(contexts[1]) == ["candidate", "hub", "content_hash", "run_id", "target_dir", "concepts"]
    assert contexts[1]["concepts"] == [
        {"concept": "A", "excerpt": "e", "score": 0.7, "inbox_file": "in.md"},
        {"concept": "C", "excerpt": "f", "score": 0.0, "title_score": 0.9, "inbox_file": "in.md",
         "loser_path": "T/C.md"}]
    assert list(contexts[2]) == ["op", "excerpt", "reason", "inbox_file", "hub", "content_hash",
                                 "run_id", "target_dir"]
    assert contexts[3] == {"candidates": [{"name": "F", "path": "F.md"}], "hub": "Hub"}
    assert contexts[4] == {"hub": "Hub"}


def test_two_notes_make_a_job_with_no_run_behind_it():
    job = DedupJob.between_notes(loser="Dir/Small.md", winner="Dir/Big.md",
                                 loser_body="x" * 5000, score=0.8)
    assert (job.concept, job.candidate, job.inbox_file, job.loser_path) == (
        "Small", "Big", "Dir/Small.md", "Dir/Small.md")
    assert len(job.excerpt) == 4000
    assert (job.hub, job.content_hash, job.run_id, job.target_dir) == (None, "", "", "")
