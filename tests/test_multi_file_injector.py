"""Acceptance tests for multi-file Injector + per-chunk containment (T1–T8).

Tests are isolated from the live driver and ledger; all I/O is mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.fsm_patches import patch_run

import pytest

from silica.sources import ingest
from silica.kernel.write.ops import Txn
from silica.router.states import finalize
from silica.router.orchestrator import InjectorFSM
from silica.router.phases import InjectorState
from silica.tools import TOOLS
from silica.router.run_state import ChunkState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_recon_result(inbox_file: str = "Inbox/a.md") -> dict:
    return {"file": inbox_file, "collisions": [], "new_concepts": ["Concept A"]}


def _make_payload_multi(file_a: str = "Inbox/a.md", file_b: str = "Inbox/b.md") -> dict:
    """Payload with two file batches so partition_by_file yields f0 and f1 groups."""
    return {
        "schema_version": 1,
        "batches": [
            {"inbox_file": file_a, "concepts": [{"name": "ConceptA"}]},
            {"inbox_file": file_b, "concepts": [{"name": "ConceptB"}]},
        ],
    }


def _validate_ok():
    return {
        "success": True,
        "rejection_rate": 0.0,
        "validated_count": 1,
        "rejected_count": 0,
        "validated_ops": [{"op": "write", "path": "Concepts/ConceptA.md", "heading": "ConceptA", "source_basename": "a.md"}],
        "rejected_ops": [],
    }


# ---------------------------------------------------------------------------
# T1 — InjectorFSM accepts N files
# ---------------------------------------------------------------------------

class TestT1MultiFileInit:
    def test_inbox_files_stored(self):
        """FSM stores all files; inbox_file is first for compat."""
        fsm = InjectorFSM(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
        assert len(fsm.inbox_files) == 2
        assert "Inbox/a.md" in fsm.inbox_files[0]  # to_vault_relative preserves basename
        assert fsm.inbox_file == fsm.inbox_files[0]

    def test_single_file_compat(self):
        """Single-file positional arg still works; inbox_files = [inbox_file]."""
        fsm = InjectorFSM("Inbox/test.md", "Concepts")
        assert len(fsm.inbox_files) == 1
        assert "Inbox/test.md" in fsm.inbox_files[0]
        assert fsm.inbox_file == fsm.inbox_files[0]

    def test_inbox_file_and_inbox_files_merged(self):
        """inbox_file inserted at front of inbox_files if not already present."""
        fsm = InjectorFSM(
            inbox_file="Inbox/a.md",
            inbox_files=["Inbox/b.md"],
            target_dir="Concepts",
        )
        assert len(fsm.inbox_files) == 2

    def test_empty_inbox_raises(self):
        with pytest.raises(ValueError):
            InjectorFSM(target_dir="Concepts")

    def test_progress_inputs_has_inbox_files(self):
        """ProgressLedger.inputs stores inbox_files list for digest rendering."""
        fsm = InjectorFSM(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
        assert "inbox_files" in fsm.progress.inputs
        assert len(fsm.progress.inputs["inbox_files"]) == 2

    @patch("silica.router.states.setup.recon_file")
    @patch("silica.router.states.setup.payload_chunks")
    @patch("silica.kernel.write.ledger.get_ledger")
    def test_payload_produces_f0_and_f1_tasks(self, mock_ledger, mock_payload, mock_recon):
        """Per-file pipeline: each file's PAYLOAD pass registers its own f{fi}_* tasks."""
        mock_ledger.return_value.is_committed.return_value = False

        mock_recon.side_effect = [
            _make_recon_result("Inbox/a.md"),
            _make_recon_result("Inbox/b.md"),
        ]
        # Payload is called once per file, with that file's recon only
        mock_payload.side_effect = [
            {"chunks": [{
                "schema_version": 1,
                "batches": [
                    {"inbox_file": "Inbox/a.md", "concepts": [{"name": "ConceptA"}]},
                ],
            }]},
            {"chunks": [{
                "schema_version": 1,
                "batches": [
                    {"inbox_file": "Inbox/b.md", "concepts": [{"name": "ConceptB"}]},
                ],
            }]},
        ]

        # No builtins.open patch here — make_tmp needs real file I/O to ~/.silica/tmp
        # We bypass run() by setting state directly so no inbox file open is needed
        fsm = InjectorFSM(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
        fsm.file_canonicals = ["inbox/a", "inbox/b"]
        fsm.file_content_hashes = ["", ""]
        # File 0 setup pass
        fsm.state = InjectorState.RECON
        fsm.step()  # RECON → PAYLOAD
        fsm.step()  # PAYLOAD → SALIENCE

        task_ids = [t.id for t in fsm.progress.tasks]
        assert [tid for tid in task_ids if tid.startswith("f0_")], f"No f0_* tasks in {task_ids}"
        # File 1 not yet processed — its tasks appear only after its own PAYLOAD pass
        assert not [tid for tid in task_ids if tid.startswith("f1_")]

        # File advance (normally triggered by _on_pipeline_end after f0's last chunk)
        assert fsm._advance_file_or_done()
        assert fsm.state == InjectorState.RECON
        fsm.step()  # RECON → PAYLOAD
        fsm.step()  # PAYLOAD → SALIENCE

        task_ids = [t.id for t in fsm.progress.tasks]
        f1_tasks = [tid for tid in task_ids if tid.startswith("f1_")]
        assert f1_tasks, f"No f1_* tasks found in {task_ids}"

    def test_missing_target_dir_rejected_before_the_fsm_runs(self):
        """No target → {TARGET}/{HUB_NAME} render literally; fail before the run."""
        with patch("silica.router.orchestrator.InjectorFSM") as mock_fsm_cls:
            from silica.tools.runners import silica_run_injector
            result = silica_run_injector(inbox_files=["Inbox/a.md"], target_dir="  ")
        assert "target_dir" in result["error"]
        assert mock_fsm_cls.call_count == 0

    def test_silica_run_injector_single_fsm_for_multiple_files(self):
        """silica_run_injector no longer fans out to N separate FSMs."""
        with patch("silica.router.orchestrator.InjectorFSM") as mock_fsm_cls:
            mock_instance = MagicMock()
            mock_instance.run.return_value = {"final_status": "Success"}
            mock_fsm_cls.return_value = mock_instance
            from silica.tools.runners import silica_run_injector
            result = silica_run_injector(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
        # FSM constructed ONCE, not twice
        assert mock_fsm_cls.call_count == 1
        # Result is the direct dict, not {"files": [...]}
        assert "files" not in result


# ---------------------------------------------------------------------------
# T2 — Per-chunk failure containment
# ---------------------------------------------------------------------------

class TestT2ChunkContainment:
    """Chunk failure preserves prior successful chunks and advances to the next."""

    @patch("silica.router.states.setup.recon_file")
    @patch("silica.router.states.setup.payload_chunks")
    # The USE site, as the other eleven distiller patches in the suite already
    # target: states/distill.py binds run_distiller at import, so patching the
    # module that DEFINES it left the real one running. This test then passed
    # only where a model was configured and the call happened to come back, and
    # died in CI with "LLM Provider NOT provided" turning `partial` into
    # `failed`.
    @patch("silica.router.states.distill.run_distiller")
    @patch("silica.router.states.distill.sanitize_output")
    @patch("silica.router.states.distill.validate_ops")
    @patch("silica.router.states.write.build_txn")
    @patch("silica.kernel.write.gate.bulk_write_atomic")
    @patch("silica.kernel.write.gate.archive_source")
    @patch("silica.kernel.write.gate.apply_inverses")
    @patch("silica.kernel.write.ledger.get_ledger")
    def test_write_failure_contained_at_chunk_level(self, mock_ledger, mock_restore, mock_cleanup, mock_bulk_write, mock_snapshot, mock_validate, mock_sanitize, mock_distiller, mock_payload, mock_recon, run_driver):
        """Write failure on chunk 1 → chunk 0 stays committed, run='partial', no ERROR."""
        from silica.kernel.write.gate import AtomicBulkResult, NoteCommitResult

        mock_ledger.return_value.is_committed.return_value = False
        mock_recon.return_value = _make_recon_result()
        mock_payload.return_value = {
            "chunks": [
                {"chunk_id": 0, "concepts": ["a"]},
                {"chunk_id": 1, "concepts": ["b"]},
            ]
        }
        mock_distiller.return_value = {"updates": []}
        mock_sanitize.return_value = {"parsed": []}
        mock_validate.return_value = {
            "success": True, "rejection_rate": 0.0,
            "validated_count": 1, "rejected_count": 0, "validated_ops": []}
        mock_snapshot.return_value = Txn(id="txn_x", inverses=[])
        mock_restore.return_value = {"success": True}
        mock_cleanup.return_value = {"success": True}
        run_driver.graph_snapshot.return_value = MagicMock()

        # Chunk 0 write succeeds (1 committed); chunk 1 write fails (0 committed, 1 failed)
        write_call_count = [0]
        def bulk_write_side_effect(ops, hub=None, lint=True):
            write_call_count[0] += 1
            if write_call_count[0] == 1:
                return AtomicBulkResult(
                    committed=[NoteCommitResult(ok=True, path="Concepts/a.md", op="write")],
                    failed=[],
                    total=1,
                )
            return AtomicBulkResult(
                committed=[],
                failed=[NoteCommitResult(ok=False, path="Concepts/b.md", op="write",
                                         error="forced write failure on chunk 1", reverted=True)],
                total=1,
            )
        mock_bulk_write.side_effect = bulk_write_side_effect

        with patch("silica.kernel.graph_diff.check_graph_regression", return_value=(True, [])):
            fsm = InjectorFSM("Inbox/test.md", "Concepts")
            res = fsm.run()

        # Run must conclude as partial, not ERROR
        assert fsm.state == InjectorState.DONE
        assert res["final_status"] == "partial"

        # Progress has partial failure flag
        assert res.get("has_partial_failure") is True

    def test_context_reset_between_chunks(self):
        """Per-chunk namespace is atomically cleared after contain_chunk_failure."""
        fsm = InjectorFSM("Inbox/test.md", "Concepts")
        # Simulate state after a failed chunk using the chunk namespace
        fsm.run_state.chunks[fsm.chunk_idx] = ChunkState(
            ops_path="/tmp/ops.json",
            sanitized={"parsed": []},
            snapshot={"txn_id": "txn_1", "inverses": []},
            txn_id="txn_1",
        )
        fsm.chunk_idx = 0
        fsm.chunks = [{"chunk": 0}, {"chunk": 1}]
        fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (0, 1)}

        fsm.contain_chunk_failure()

        # The chunk's state goes whole, so nothing of it reaches the next one
        assert 0 not in fsm.run_state.chunks
        # Advanced to next chunk
        assert fsm.chunk_idx == 1
        assert fsm.state in (InjectorState.COLLISION, InjectorState.DELEGATE)

    def test_last_chunk_failure_goes_to_done(self):
        """When the only chunk fails, state → DONE with final_status='failed'
        (zero commits: 'partial' would oversell a fully-failed run)."""
        fsm = InjectorFSM("Inbox/test.md", "Concepts")
        fsm.chunks = [{"chunk": 0}]
        fsm.chunk_idx = 0
        fsm.chunk_flat_to_fi_ci = {0: (0, 0)}

        fsm.contain_chunk_failure()

        assert fsm.state == InjectorState.DONE
        assert fsm.run_state.final_status == "failed"
        assert fsm.run_state.has_partial_failure is True
        assert len(fsm.run_state.failed_chunks) == 1  # per-chunk ledger, not last-error-wins

    def test_last_chunk_failure_still_records_file_outcome(self, tmp_vault):
        """A file whose LAST chunk fails never reaches CLEANUP, but its earlier
        committed notes must still land in log.md + files_summary (accounting
        used to be CLEANUP-anchored, so it silently vanished on this path)."""
        from pathlib import Path
        from silica.config import CONFIG
        from silica.kernel.progress import RunManifestEntry
        from silica.kernel.base.layout import DEFAULT_LOG_FILENAME

        fsm = InjectorFSM("Inbox/lezione-07.md", "Concepts")
        fsm.chunks = [{"chunk": 0}, {"chunk": 1}]
        fsm.chunk_idx = 1  # last chunk of the file
        fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (0, 1)}
        fsm.file_chunks = {0: {"source_file": "Inbox/lezione-07.md",
                                "chunks": [{"chunk": 0}, {"chunk": 1}]}}
        # chunk 0 committed a real note before chunk 1 blew up
        fsm.manifest.entries.append(RunManifestEntry(
            title="A", path="Concepts/A", parent=None, cluster_id=-1,
            source_basename="lezione-07.md", op="write",
        ))

        fsm.contain_chunk_failure()

        content = (Path(CONFIG.vault_path) / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8")
        assert "nucleate `lezione-07.md` → 1 new, 0 patch" in content
        assert fsm.run_state.files_summary == [
            {"file": "lezione-07.md", "new": 1, "patch": 0, "deferred": 0}
        ]

    def test_file_outcome_logged_once_across_paths(self, tmp_vault):
        """The success (CLEANUP) and failure (containment) entry points must not
        both record the same file — the files_logged guard keeps it to one."""
        fsm = InjectorFSM("Inbox/lezione-07.md", "Concepts")
        from silica.router.states.finalize import log_nucleate_completion
        log_nucleate_completion(fsm, 0, "Inbox/lezione-07.md")
        log_nucleate_completion(fsm, 0, "Inbox/lezione-07.md")
        assert len(fsm.run_state.files_summary) == 1

    def test_on_error_per_chunk_routes_to_rollback(self):
        """DELEGATE/SANITIZE/SNAPSHOT errors now route to ROLLBACK (not ERROR)."""
        for state in (InjectorState.DELEGATE, InjectorState.SANITIZE,
                      InjectorState.SNAPSHOT, InjectorState.VALIDATE):
            assert state.value.on_error == "rollback"
        # Setup phases still abort
        assert InjectorState.RECON.value.on_error == "abort"
        assert InjectorState.PAYLOAD.value.on_error == "abort"


# ---------------------------------------------------------------------------
# T3 — Resume content-addressed
# ---------------------------------------------------------------------------

class TestT3Resume:
    def test_resume_run_id_loads_existing_ledger(self, tmp_path, monkeypatch):
        """resume_run_id causes the FSM to load the existing ProgressLedger."""
        import silica.kernel.progress as prog_mod
        # Redirect runs dir to a tmp directory for isolation
        monkeypatch.setattr(prog_mod, "runs_dir", lambda: tmp_path / "runs")
        from silica.kernel.progress import ProgressLedger

        # Simulate a prior run
        prior = ProgressLedger.new(mode="inject", inputs={"inbox_files": ["Inbox/a.md"]})
        prior.save()
        run_id = prior.run_id

        # New FSM with resume_run_id should load the prior ledger
        fsm = InjectorFSM(
            inbox_files=["Inbox/a.md"],
            target_dir="Concepts",
            resume_run_id=run_id,
        )
        assert fsm.progress.run_id == run_id

    def test_resume_run_id_nonexistent_starts_fresh(self):
        """Invalid resume_run_id falls back to a new run (no exception)."""
        fsm = InjectorFSM(
            inbox_files=["Inbox/a.md"],
            target_dir="Concepts",
            resume_run_id="nonexistent_run_id_xyz",
        )
        # New run was created with a different id
        assert fsm.progress.run_id != "nonexistent_run_id_xyz"

    def test_silica_run_injector_passes_resume_run_id(self):
        """silica_run_injector forwards resume_run_id to InjectorFSM."""
        with patch("silica.router.orchestrator.InjectorFSM") as mock_fsm_cls:
            mock_instance = MagicMock()
            mock_instance.run.return_value = {"final_status": "Success"}
            mock_fsm_cls.return_value = mock_instance
            from silica.tools.runners import silica_run_injector
            silica_run_injector(inbox_files=["Inbox/a.md"], target_dir="C", resume_run_id="run_abc")
        _, kwargs = mock_fsm_cls.call_args
        assert kwargs.get("resume_run_id") == "run_abc"


# ---------------------------------------------------------------------------
# T4 — Per-file cleanup awareness
# ---------------------------------------------------------------------------

class TestT4PerFileCleanup:
    def test_write_ledger_for_file_uses_per_file_canonical(self):
        """write_ledger_for_file records ops with the correct file's canonical."""
        fsm = InjectorFSM("Inbox/a.md", "Concepts")
        fsm.file_canonicals = ["inbox/a", "inbox/b"]
        fsm.file_content_hashes = ["hash_a", "hash_b"]

        mock_op = MagicMock()
        mock_op.op.value = "write"
        mock_op.op.__eq__ = lambda self, other: False  # not OpType.skip

        with patch_run("load_ops", return_value=[mock_op]):
            with patch("silica.kernel.write.ledger.get_ledger") as mock_ledger:
                fsm.chunk.ops_path = "/tmp/ops.json"
                fsm.chunk.txn_id = "txn_1"
                fsm.write_ledger_for_file(1, "committed")
        # source_canonical should be for fi=1 ("inbox/b")
        assert mock_ledger.return_value.record.called
        args = mock_ledger.return_value.record.call_args
        assert args.kwargs.get("source_canonical") == "inbox/b" or \
               (args.args and "inbox/b" in args.args)

    def test_cleanup_defers_archive_for_non_last_chunk(self):
        """Cleanup does NOT archive when ci < last chunk of the file."""
        with patch("silica.kernel.write.gate.archive_source") as mock_cleanup:
            with patch_run("load_ops", return_value=[]):
                with patch("silica.kernel.write.ledger.get_ledger"):
                    fsm = InjectorFSM("Inbox/test.md", "Concepts")
                    # Two chunks under one file group
                    fsm.file_chunks = {0: {"source_file": "Inbox/test.md", "chunks": [{"a": 1}, {"b": 2}]}}
                    fsm.chunks = [{"a": 1}, {"b": 2}]
                    fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (0, 1)}
                    fsm.chunk_idx = 0  # ci=0, not last
                    fsm.chunk.ops_path = "/tmp/ops.json"
                    fsm.chunk.txn_id = "txn_1"
                    finalize.handle_cleanup(fsm)
        mock_cleanup.assert_not_called()

    def test_cleanup_archives_on_last_chunk_of_file(self):
        """Cleanup DOES archive when ci == last chunk of the file (no failures)."""
        with patch("silica.kernel.write.gate.archive_source", return_value={"success": True}) as mock_cleanup:
            with patch_run("load_ops", return_value=[]):
                with patch("silica.kernel.write.ledger.get_ledger"):
                    fsm = InjectorFSM("Inbox/test.md", "Concepts")
                    fsm.file_chunks = {0: {"source_file": "Inbox/test.md", "chunks": [{"a": 1}, {"b": 2}]}}
                    fsm.chunks = [{"a": 1}, {"b": 2}]
                    fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (0, 1)}
                    fsm.chunk_idx = 1  # ci=1, last chunk
                    fsm.chunk.ops_path = "/tmp/ops.json"
                    fsm.chunk.txn_id = "txn_1"
                    finalize.handle_cleanup(fsm)
        mock_cleanup.assert_called_once_with("Inbox/test.md")


# ---------------------------------------------------------------------------
# T5 — Dead code removal in _handle_payload
# ---------------------------------------------------------------------------

class TestT5DeadCodeRemoved:
    @patch("silica.router.states.setup.recon_file")
    @patch("silica.router.states.setup.payload_chunks")
    @patch("silica.kernel.write.ledger.get_ledger")
    def test_no_task_ledger_load_in_payload(self, mock_ledger, mock_payload, mock_recon, caplog):
        """No FileNotFoundError is swallowed during PAYLOAD phase."""
        import logging
        mock_ledger.return_value.is_committed.return_value = False
        mock_recon.return_value = _make_recon_result()
        mock_payload.return_value = {"chunks": [{"chunk_id": 0}]}

        fsm = InjectorFSM("Inbox/test.md", "Concepts")
        fsm.file_canonicals = ["inbox/test"]
        fsm.file_content_hashes = [""]
        fsm.state = InjectorState.RECON
        with caplog.at_level(logging.DEBUG):
            fsm.step()  # RECON
            fsm.step()  # PAYLOAD

        # The old code silently swallowed FileNotFoundError from TaskLedger.load
        # with a bare `except`. Verify no such error appears in logs.
        error_msgs = [r.message for r in caplog.records if "FileNotFoundError" in str(r.message)]
        assert not error_msgs, f"FileNotFoundError still appearing in logs: {error_msgs}"


# ---------------------------------------------------------------------------
# T6 — Direct CLI shortcuts (bypass LLM)
# ---------------------------------------------------------------------------

class TestT6DirectShortcuts:
    def setup_method(self):
        # Ensure tools are registered
        import silica.tools.atomic  # noqa: F401
        import silica.cli  # noqa: F401  registers every tool
        import silica.tools.wrapped  # noqa: F401

    def test_status_handled_directly(self):
        from tests.run_command import direct
        # Tool.run catches its own errors (missing run dir) and returns JSON error string
        # direct still returns True (command was recognized)
        with patch("silica.cli.CONSOLE"):
            result = direct("/status", [])
        assert result is True

    def _swap_tool_fn(self, tool_name: str, new_fn):
        """Context manager that temporarily replaces Tool.fn (bypasses __slots__ read-only issue)."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            tool = TOOLS[tool_name]
            orig = tool.fn
            tool.fn = new_fn
            try:
                yield
            finally:
                tool.fn = orig

        return _ctx()

    def test_find_preserves_case(self):
        """'/find Neural Networks' reaches the search with original casing."""
        received_query = []
        from tests.run_command import direct

        # /find reads the scored core, not the tool, whose payload drops
        # `score` for the model (survey card C2); the stub mirrors its signature.
        def capture_search(text: str, k: int, memory: bool = True, folder: str = ""):
            received_query.append(text)
            return {"results": []}

        with patch("silica.kernel.recall.search.facade_search", capture_search):
            with patch("silica.cli.CONSOLE"):
                result = direct("/find Neural Networks", [])

        assert result is True
        assert received_query == ["Neural Networks"], f"Case destroyed: got {received_query}"

    def test_embed_with_force_flag(self):
        """'/embed --force' passes force=True to the tool."""
        received = {}
        from tests.run_command import direct

        def capture_embed(folder: str = "", force: bool = False):
            received["folder"] = folder
            received["force"] = force
            return {"indexed": 0, "total_notes": 0, "read_errors": 0, "index_path": "/tmp/idx"}

        with self._swap_tool_fn("silica_embed_refresh", capture_embed):
            with patch("silica.cli.CONSOLE"):
                direct("/embed --force", [])

        assert received.get("force") is True

    def test_graph_positional_args(self):
        """/graph Out.html Concepts passes correct output_path and folder."""
        received = {}
        from tests.run_command import direct

        def capture_graph(output_path: str = "graph.html", folder: str = "",
                          title: str = "", mode: str = "links", knn_k: int = 6):
            received["output_path"] = output_path
            received["folder"] = folder
            return {"output_path": output_path}

        # /graph is the user's own command: it calls the export behind the
        # tool, which takes any path, not the vault-held tool (S1).
        with patch("silica.tools.graph.graph_export", capture_graph):
            with patch("silica.cli.CONSOLE"):
                direct("/graph Out.html Concepts", [])

        assert received.get("output_path") == "Out.html"
        assert received.get("folder") == "Concepts"

    def test_unknown_slash_command_returns_false(self):
        from tests.run_command import direct
        result = direct("/unknown_command", [])
        assert result is False

    def test_find_returns_false_for_non_find_cmd(self):
        from tests.run_command import direct
        # /report should NOT be handled by direct
        result = direct("/report", [])
        assert result is False


# ---------------------------------------------------------------------------
# T7 — /nucleate agent-directed shortcut
# ---------------------------------------------------------------------------

class TestT7NucleateShortcut:
    def _expand(self, cmd: str, monkeypatch) -> tuple[str | None, list[dict]]:
        """Expand with a stubbed Coordinator; return (msg, recorded ctor kwargs)."""
        import silica.router.coordinator as coord_mod
        calls: list[dict] = []

        class _Fake:
            def __init__(self, **kw):
                calls.append(kw)

            def run(self):
                return {"final_status": "Success"}

        monkeypatch.setattr(coord_mod, "Coordinator", _Fake)
        from tests.run_command import expansion
        return expansion(cmd), calls

    def test_nucleate_single_file(self, monkeypatch):
        msg, calls = self._expand("/nucleate Inbox/a.md --target=Concepts/AI", monkeypatch)
        assert msg == ""
        assert calls[0]["inbox_files"] == ["Inbox/a.md"]
        assert calls[0]["target_dir"] == "Concepts/AI"

    def test_nucleate_multi_file(self, monkeypatch):
        msg, calls = self._expand("/nucleate Inbox/a.md Inbox/b.md --target=Concepts/AI", monkeypatch)
        assert msg == ""
        assert calls[0]["inbox_files"] == ["Inbox/a.md", "Inbox/b.md"]
        assert calls[0]["target_dir"] == "Concepts/AI"

    def test_nucleate_with_hub(self, monkeypatch):
        msg, calls = self._expand("/nucleate Inbox/a.md --target=Concepts/AI --hub=AI", monkeypatch)
        assert msg == ""
        assert calls[0]["hub"] == "AI"

    def test_nucleate_missing_target_falls_back_when_pick_fails(self, monkeypatch):

        def boom(files):
            raise ValueError("no llm")

        monkeypatch.setattr(ingest, "pick_target_folder", boom)
        msg, _ = self._expand("/nucleate Inbox/a.md", monkeypatch)
        assert msg is not None and "silica_run_injector" in msg
        assert "target_dir=<chosen folder>" in msg

    def test_nucleate_missing_files_returns_error(self, monkeypatch):
        msg, _ = self._expand("/nucleate --target=Concepts/AI", monkeypatch)
        assert msg is not None
        assert "Error" in msg or "file" in msg.lower()

    def test_nucleate_case_preserved_in_paths(self, monkeypatch):
        """File paths must preserve their original casing."""
        msg, calls = self._expand("/nucleate Inbox/MyNote.md --target=Concepts/AI", monkeypatch)
        assert msg == ""
        assert calls[0]["inbox_files"] == ["Inbox/MyNote.md"]

    def test_report_still_works_after_nucleate_added(self, monkeypatch):
        """/report shortcut is unaffected by /nucleate addition."""
        msg, _ = self._expand("/report Concepts/ML", monkeypatch)
        assert msg is not None
        assert "silica_vault_report" in msg


# ---------------------------------------------------------------------------
# T8 — /help includes new commands
# ---------------------------------------------------------------------------

class TestT8Help:
    def test_help_lists_nucleate(self):
        from silica.ui.commands import command_names
        assert "/nucleate" in command_names()

    def test_help_lists_direct_commands(self):
        from silica.command_table import COMMANDS
        names = {c.name for c in COMMANDS}
        assert "/status" in names
        assert "/embed" in names
        assert "/graph" in names
        assert "/find" in names


def test_next_uncommitted_chunk_idx_skips_committed_files():
    """_next_uncommitted_chunk_idx must skip chunks from committed files."""
    from silica.router.orchestrator import InjectorFSM

    fsm = InjectorFSM(inbox_files=["Inbox/committed.md", "Inbox/new.md"], target_dir="Concepts")
    fsm.chunks = [
        {"source_file": "Inbox/committed.md", "batches": []},
        {"source_file": "Inbox/new.md", "batches": [{"concepts": [{"title": "T"}]}]},
    ]
    fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (1, 0)}
    fsm.committed_file_indices = {0}  # file 0 is committed

    # From start=1, should return 1 (fi=1, not committed)
    assert fsm._next_uncommitted_chunk_idx(1) == 1

    # From start=0, chunk 0 is fi=0 (committed) → skip to chunk 1 (fi=1) → returns 1
    assert fsm._next_uncommitted_chunk_idx(0) == 1

    # With both committed, should return len(chunks) = 2 (exhausted)
    fsm.committed_file_indices = {0, 1}
    assert fsm._next_uncommitted_chunk_idx(0) == 2


def test_current_source_file_returns_per_chunk_file():
    """current_source_file must return the file for the currently-processed chunk."""
    from silica.router.orchestrator import InjectorFSM

    fsm = InjectorFSM(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
    fsm.file_chunks = {
        0: {"source_file": "Inbox/a.md", "chunks": [{}]},
        1: {"source_file": "Inbox/b.md", "chunks": [{}]},
    }
    fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (1, 0)}

    fsm.chunk_idx = 0
    assert fsm.current_source_file == "Inbox/a.md"

    fsm.chunk_idx = 1
    assert fsm.current_source_file == "Inbox/b.md"
