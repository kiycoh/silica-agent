# SPDX-License-Identifier: AGPL-3.0-or-later
"""Residue gate (verification-based, 2026-08-17): at a file's last-chunk
CLEANUP the gate declares the VERIFIED-missing facts (report + log + deferred
store) and never spawns a re-distill round — the round was refuted by the
2026-08-16 ROI audit (it re-added facts already present, 60-170s each)."""

import types
from unittest.mock import MagicMock

import silica.router.states.finalize as finalize

# Bound at import time, before the autouse network guard stubs the attr.
_real_residue_facts = finalize.residue_facts


def _fsm(n_chunks=1, context=None, entries=(), tasks=()):
    manifest = types.SimpleNamespace(entries=list(entries))
    return types.SimpleNamespace(
        _get_chunks_from_context_if_empty=lambda: None,
        _chunks=[{"schema_version": 1,
                  "batches": [{"inbox_file": "Inbox/a.md",
                               "concepts": [{"name": f"c{i}"}]}]}
                 for i in range(n_chunks)],
        _chunk_flat_to_fi_ci={i: (0, i) for i in range(n_chunks)},
        _current_chunk_idx=n_chunks - 1,
        _current_file_idx=0,
        _progress_note=lambda *a, **k: None,
        _write_ledger_for_file=lambda *a, **k: None,
        _file_chunks={0: {"chunks": [{"schema_version": 1,
                                      "batches": [{"inbox_file": "Inbox/a.md",
                                                   "concepts": [{"name": f"c{i}"}]}]}
                                     for i in range(n_chunks)],
                          "source_file": "Inbox/a.md"}},
        progress=types.SimpleNamespace(tasks=list(tasks), run_id="r1",
                                       started_at="2026-08-16T00:00:00",
                                       inputs={}),
        inbox_file="Inbox/a.md",
        context=dict(context or {}),
        manifest=manifest,
        _undo_run_id=None,
        _run_inverses=[],
        _transition_success=lambda: None,
        _chunk_task_id=lambda *a, **k: "cleanup",
        distill_profile=None,
        _current_content_hash="hash0",
        target_dir="Dir",
        hub=None,
    )


class TestResidueGate:
    def test_draft_files_are_skipped_without_a_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(finalize, "residue_facts",
                            lambda *a, **k: calls.append(1) or ["x"])
        fsm = _fsm(context={"file_0_form": "draft"})
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        assert calls == []

    def test_a_failed_file_is_skipped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(finalize, "residue_facts",
                            lambda *a, **k: calls.append(1) or ["x"])
        fsm = _fsm()
        finalize._residue_gate(fsm, 0, "Inbox/a.md", True)
        assert calls == []

    def test_empty_residue_parks_then_records_clean_instrument(self, monkeypatch):
        monkeypatch.setattr(finalize, "_verify_now",
                            lambda fsm, fi, f: {"missing": [], "total": 0,
                                                "judged": 0, "failures": 0,
                                                "off_theme": 0})
        store = MagicMock()
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: store)
        fsm = _fsm()
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        # no dispatch ran, so the gate parks instead of verifying inline
        assert "declared_residue" not in fsm.context
        finalize.flush_residue_pending(fsm, wait=True)
        assert "declared_residue" not in fsm.context
        store.put_residue_facts.assert_not_called()
        assert fsm.progress.inputs["residue"]["f0"]["missing"] == []
        fsm._residue_executor.shutdown(wait=False)

    def test_missing_facts_are_declared_and_deferred_never_respawned(self, monkeypatch):
        monkeypatch.setattr(finalize, "_verify_now",
                            lambda fsm, fi, f: {"missing": ["uncovered"],
                                                "total": 1, "judged": 1,
                                                "failures": 0, "off_theme": 0})
        store = MagicMock()
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: store)
        logged = []
        monkeypatch.setattr("silica.kernel.recall.run_log.append_log_line",
                            lambda *a, **k: logged.append(a))
        fsm = _fsm()
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        finalize.flush_residue_pending(fsm, wait=True)
        assert len(fsm._chunks) == 1  # no round, ever
        assert fsm.context["declared_residue"]["a.md"] == ["uncovered"]
        store.put_residue_facts.assert_called_once_with(
            "hash0", "Inbox/a.md", "Dir", None, ["uncovered"])
        assert logged  # log.md line emitted
        fsm._residue_executor.shutdown(wait=False)

    def test_report_is_capped_but_deferred_facts_are_not(self, monkeypatch):
        many = [f"fact {i}" for i in range(20)]
        monkeypatch.setattr(finalize, "_verify_now",
                            lambda fsm, fi, f: {"missing": many, "total": 20,
                                                "judged": 20, "failures": 0,
                                                "off_theme": 0})
        store = MagicMock()
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: store)
        monkeypatch.setattr("silica.kernel.recall.run_log.append_log_line",
                            lambda *a, **k: None)
        fsm = _fsm()
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        finalize.flush_residue_pending(fsm, wait=True)
        assert len(fsm.context["declared_residue"]["a.md"]) == 12
        assert store.put_residue_facts.call_args.args[4] == many  # uncapped
        assert fsm.progress.inputs["residue"]["f0"]["missing"] == many
        fsm._residue_executor.shutdown(wait=False)

    def test_flush_records_stats_and_saves_ledger(self, monkeypatch):
        monkeypatch.setattr(finalize, "_verify_now",
                            lambda fsm, fi, f: {"missing": ["m"], "total": 5,
                                                "judged": 4, "failures": 1,
                                                "off_theme": 2})
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: MagicMock())
        monkeypatch.setattr("silica.kernel.recall.run_log.append_log_line",
                            lambda *a, **k: None)
        fsm = _fsm()
        saves = []
        fsm.progress.save = lambda: saves.append(1)
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        finalize.flush_residue_pending(fsm, wait=True)
        rec = fsm.progress.inputs["residue"]["f0"]
        assert rec["total"] == 5 and rec["judged"] == 4 and rec["failures"] == 1
        assert rec["off_theme"] == 2
        assert "f0" in fsm.progress.inputs["residue_secs"]
        # a flush past the last progress note must persist itself
        assert saves
        fsm._residue_executor.shutdown(wait=False)


class TestAsyncDeclaration:
    """The round is gone, so nothing forces the gate to wait for the judge:
    a still-running verification is parked and declared at a later gate or
    at run end (report/log/bundle are order-insensitive within the run)."""

    def _pending_fsm(self, done=False):
        from concurrent.futures import Future
        fsm = _fsm()
        fsm._file_content_hashes = ["hash0"]
        fut = Future()
        if done:
            fut.set_result([False])  # fact judged missing
        fsm._residue_future = (0, ["missing fact"], [fut])
        return fsm, fut

    def test_gate_parks_running_verification_without_waiting(self, monkeypatch):
        store = MagicMock()
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: store)
        fsm, fut = self._pending_fsm(done=False)
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        assert "declared_residue" not in fsm.context
        assert fsm._residue_pending and fsm._residue_pending[0][:2] == ("verdicts", 0)
        assert fsm._residue_future is None
        fut.set_result([False])  # let the executorless future finish

    def test_flush_declares_completed_pending(self, monkeypatch):
        store = MagicMock()
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: store)
        monkeypatch.setattr("silica.kernel.recall.run_log.append_log_line",
                            lambda *a, **k: None)
        fsm, fut = self._pending_fsm(done=False)
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        fut.set_result([False])
        finalize.flush_residue_pending(fsm, wait=False)
        assert fsm.context["declared_residue"]["a.md"] == ["missing fact"]
        store.put_residue_facts.assert_called_once_with(
            "hash0", "Inbox/a.md", "Dir", None, ["missing fact"])
        assert fsm._residue_pending == []
        assert fsm.progress.inputs["residue"]["f0"]["missing"] == ["missing fact"]

    def test_flush_without_wait_keeps_running_pending(self, monkeypatch):
        fsm, fut = self._pending_fsm(done=False)
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        finalize.flush_residue_pending(fsm, wait=False)
        assert len(fsm._residue_pending) == 1  # still running, still parked
        assert "declared_residue" not in fsm.context
        fut.set_result([False])

    def test_completed_verification_declares_synchronously(self, monkeypatch):
        monkeypatch.setattr(finalize, "residue_facts", _real_residue_facts)
        store = MagicMock()
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: store)
        monkeypatch.setattr("silica.kernel.recall.run_log.append_log_line",
                            lambda *a, **k: None)
        fsm, _fut = self._pending_fsm(done=True)
        finalize._residue_gate(fsm, 0, "Inbox/a.md", False)
        assert fsm.context["declared_residue"]["a.md"] == ["missing fact"]
        assert not getattr(fsm, "_residue_pending", [])

    def test_pipeline_done_flushes_pending_with_wait(self, monkeypatch):
        from types import SimpleNamespace as NS
        from unittest.mock import MagicMock as MM
        from silica.router.orchestrator import InjectorFSM
        flush = MM()
        monkeypatch.setattr(finalize, "flush_residue_pending", flush)
        fsm = NS(
            context={}, _txn=1, _pre_graph=1,
            _get_chunks_from_context_if_empty=lambda: None,
            _chunks=[{}],
            _chunk_flat_to_fi_ci={0: (0, 0)},
            _current_chunk_idx=0,
            _current_file_idx=0,
            _next_uncommitted_chunk_idx=lambda start: start,
            _advance_file_or_done=MM(return_value=False),
            _has_collision_phase=True,
            state=None,
        )
        InjectorFSM._on_pipeline_end(fsm)
        flush.assert_called_once_with(fsm, wait=True)

    def test_failure_done_also_flushes_pending(self, monkeypatch):
        from types import SimpleNamespace as NS
        from unittest.mock import MagicMock as MM
        from silica.router.orchestrator import InjectorFSM
        flush = MM()
        monkeypatch.setattr(finalize, "flush_residue_pending", flush)
        progress = MM()
        progress.tasks = []
        fsm = NS(
            _current_chunk_idx=0,
            _current_file_idx=0,
            _chunk_flat_to_fi_ci={0: (0, 0)},
            _chunk_ctx={},
            progress=progress,
            context={},
            _run_inverses=[],
            _file_chunks={0: {"source_file": "f0", "chunks": [{}]}},
            inbox_file="f0",
            _failed_phase_id=lambda: "",
            _get_chunks_from_context_if_empty=lambda: None,
            _chunks=[{}],
            _next_uncommitted_chunk_idx=lambda start: start,
            _advance_file_or_done=MM(return_value=False),
            _has_collision_phase=True,
            state=None,
        )
        InjectorFSM._contain_chunk_failure(fsm)
        flush.assert_called_once_with(fsm, wait=True)


class TestCleanupWiring:
    def _wire(self, monkeypatch, fsm):
        seen = {"cleanup": [], "log": []}
        monkeypatch.setattr(finalize, "_record_provenance", lambda *a, **k: None)
        monkeypatch.setattr(finalize, "_log_nucleate_completion",
                            lambda f, fi, src: seen["log"].append(src))
        monkeypatch.setattr(finalize, "_write_source_leaf", lambda *a, **k: None,
                            raising=False)
        monkeypatch.setattr("silica.tools.wrapped.silica_cleanup",
                            lambda *a, **k: seen["cleanup"].append(a) or {"success": True})
        return seen

    def test_declared_residue_no_longer_defers_archive(self, monkeypatch, tmp_vault):
        monkeypatch.setattr(finalize, "_verify_now",
                            lambda fsm, fi, f: {"missing": ["uncovered"],
                                                "total": 1, "judged": 1,
                                                "failures": 0, "off_theme": 0})
        monkeypatch.setattr("silica.kernel.recall.deferred.get_deferred_store",
                            lambda *a, **k: MagicMock())
        monkeypatch.setattr("silica.kernel.recall.run_log.append_log_line",
                            lambda *a, **k: None)
        fsm = _fsm()
        seen = self._wire(monkeypatch, fsm)

        finalize.handle_cleanup(fsm)

        assert len(seen["cleanup"]) == 1  # archived in the same pass
        assert seen["log"] == ["Inbox/a.md"]
        assert len(fsm._chunks) == 1      # nothing spawned
        # declaration is asynchronous: lands at the flush, not in the pass
        finalize.flush_residue_pending(fsm, wait=True)
        assert fsm.context["declared_residue"]["a.md"] == ["uncovered"]
        fsm._residue_executor.shutdown(wait=False)
