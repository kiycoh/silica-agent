"""A run whose write failures were all recovered is not "partial".

Measured on the theology library (run 37e5da91, 2026-08-16): three ops failed
lint/write, the boundary anneal wrote all three notes at 13:55:50-52, and the
run still announced `nucleate finished: partial`. has_partial_failure is a
one-way latch set at WRITE, and CLEANUP computes final_status before the anneal
runs — so the verdict describes a state the run left behind.
"""
import silica.kernel.recall.deferred as deferred_mod
import silica.tools.pipeline as pipeline_mod
from silica.router.orchestrator import InjectorFSM


class _Store:
    """Deferred store stub: `left` are the hashes still holding a bundle."""

    def __init__(self, left=()):
        self._left = set(left)

    def list_all(self):
        return [{"content_hash": h} for h in self._left] or [{"content_hash": "other"}]

    def get(self, content_hash):
        return {"rejected_ops": [{}]} if content_hash in self._left else None


def _fsm(monkeypatch, *, left=(), written=3, **ctx):
    fsm = InjectorFSM("Inbox/test.md", "TargetDir")
    fsm._file_content_hashes = ["h1"]
    fsm.context.update(
        {"has_partial_failure": True, "run_had_ops": True, "final_status": "partial"}
    )
    fsm.context.update(ctx)
    monkeypatch.setattr(deferred_mod, "get_deferred_store", lambda: _Store(left))
    monkeypatch.setattr(pipeline_mod, "silica_anneal", lambda **kw: {"written": written})
    return fsm


def test_recovered_write_failures_lift_the_partial_verdict(monkeypatch):
    fsm = _fsm(monkeypatch)

    fsm._boundary_anneal()

    assert fsm.context["final_status"] == "Success"
    assert not fsm.context["has_partial_failure"]


def test_a_bundle_still_deferred_keeps_the_partial_verdict(monkeypatch):
    """Recovering some ops is not recovering the file."""
    fsm = _fsm(monkeypatch, left=("h1",))

    fsm._boundary_anneal()

    assert fsm.context["final_status"] == "partial"


def test_a_rolled_back_chunk_keeps_the_partial_verdict(monkeypatch):
    """A whole chunk that aborted is not something the anneal can undo."""
    fsm = _fsm(monkeypatch, failed_chunks=[{"chunk": "f0_c1", "phase": "WRITE"}])

    fsm._boundary_anneal()

    assert fsm.context["final_status"] == "partial"


def test_an_anneal_that_recovered_nothing_changes_no_verdict(monkeypatch):
    fsm = _fsm(monkeypatch, written=0)

    fsm._boundary_anneal()

    assert fsm.context["final_status"] == "partial"
