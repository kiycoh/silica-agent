"""Cross-file prefetch warm-up (boundary stall lever, 2026-08-16).

The first distill of every file used to run inline (~40-65s exposed per file
on the library batches). warm_next_file preps the next uncommitted file early
(recon → payload assembly → salience) and attaches its chunks so the distill
prefetch window can cross the file boundary; the per-file states then
fast-path through their warm guards at their normal position in the FSM.
"""
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from silica.router.states import setup as st
from silica.router.run_state import ChunkState, RunState


def _recon_res(name="Inbox/f1.md"):
    return {"file": name, "collisions": [], "new_concepts": ["warm concept"]}


def _payload_res(name="Inbox/f1.md"):
    return {"chunks": [{"schema_version": 1,
                        "batches": [{"inbox_file": name,
                                     "concepts": [{"name": "warm concept",
                                                   "inbox_excerpt": "text"}]}]}]}


def _warm_stub(n_files=2, cur_fi=0):
    fsm = SimpleNamespace()
    fsm.inbox_files = [f"Inbox/f{i}.md" for i in range(n_files)]
    fsm.inbox_file = fsm.inbox_files[cur_fi]
    fsm.file_idx = cur_fi
    fsm.next_uncommitted_file_idx = lambda start: start
    c0 = {"schema_version": 1,
          "batches": [{"inbox_file": fsm.inbox_files[0],
                       "concepts": [{"name": "c0"}]}]}
    fsm.chunks = [c0]
    fsm.chunk_flat_to_fi_ci = {0: (0, 0)}
    fsm.file_chunks = {0: {"source_file": fsm.inbox_files[0], "chunks": [c0]}}
    fsm.run_state = RunState(vault_graph_ctx={})
    fsm.file_content_hashes = []
    fsm.distill_profile = None
    fsm.progress = SimpleNamespace(tasks=[], inputs={}, save=lambda: None)

    def _add_task(cap, task_id=None, depends_on=None, input_ref=None):
        t = SimpleNamespace(id=task_id or cap, capability_name=cap)
        fsm.progress.tasks.append(t)
        return t

    fsm.progress.add_task = _add_task
    fsm.make_tmp = lambda obj: (lambda p: (json.dump(obj, open(p, "w")), p)[1])(
        tempfile.mktemp(suffix=".json"))
    fsm.progress_note = MagicMock()
    fsm.transition_success = MagicMock()
    return fsm


def test_warm_next_file_attaches_chunks_and_marks():
    fsm = _warm_stub()
    with patch.object(st, "recon_file", return_value=_recon_res()) as rec, \
         patch.object(st, "payload_chunks", return_value=_payload_res()), \
         patch("silica.model.providers.get_embedder_or_none", return_value=None):
        assert st.warm_next_file(fsm) is True
    rec.assert_called_once_with("Inbox/f1.md")
    assert len(fsm.chunks) == 2
    assert fsm.chunk_flat_to_fi_ci[1] == (1, 0)
    assert fsm.file_chunks[1]["source_file"] == "Inbox/f1.md"
    assert fsm.run_state.file(1).warm_recon == _recon_res()
    assert fsm.run_state.file(1).salience_done is True
    assert any(t.id == "f1_c0_distill" for t in fsm.progress.tasks)
    # the FSM's own chunk cursor is untouched — file 0 is still being processed
    assert not hasattr(fsm, "chunk_idx") or fsm.chunk_idx == 0


def test_warm_next_file_bails_on_recon_error():
    fsm = _warm_stub()
    with patch.object(st, "recon_file", return_value={"error": "boom"}):
        assert st.warm_next_file(fsm) is False
    assert len(fsm.chunks) == 1
    assert 1 not in fsm.file_chunks




def test_warm_next_file_bails_when_no_next_file():
    fsm = _warm_stub(n_files=1)
    with patch.object(st, "recon_file") as rec:
        assert st.warm_next_file(fsm) is False
    rec.assert_not_called()


def test_handle_recon_consumes_warm_result():
    fsm = _warm_stub(cur_fi=1)
    fsm.run_state.file(1).warm_recon = _recon_res()
    with patch.object(st, "recon_file") as rec:
        st.handle_recon(fsm)
    rec.assert_not_called()
    assert fsm.run_state.recon == [_recon_res()]
    fsm.transition_success.assert_called_once()


def test_handle_payload_skips_reassembly_for_warmed_file():
    fsm = _warm_stub(cur_fi=1)
    w0 = {"schema_version": 1,
          "batches": [{"inbox_file": "Inbox/f1.md", "concepts": [{"name": "b"}]}]}
    fsm.chunks.append(w0)
    fsm.chunk_flat_to_fi_ci[1] = (1, 0)
    fsm.file_chunks[1] = {"source_file": "Inbox/f1.md", "chunks": [w0]}
    fsm.run_state.file(1).warm_payload = {"warm": True}
    with patch.object(st, "payload_chunks") as pay:
        st.handle_payload(fsm)
    pay.assert_not_called()
    assert len(fsm.chunks) == 2  # no double append
    assert fsm.chunk_idx == 1
    assert fsm.run_state.payload == {"warm": True}
    fsm.transition_success.assert_called_once()


def test_handle_salience_skips_when_warm_marker():
    fsm = _warm_stub(cur_fi=1)
    fsm.run_state.file(1).salience_done = True
    with patch("silica.model.providers.get_embedder_or_none") as emb:
        st.handle_salience(fsm)
    emb.assert_not_called()
    fsm.transition_success.assert_called_once()


def test_on_pipeline_end_advances_file_when_next_chunk_crosses():
    from silica.router.orchestrator import InjectorFSM
    fsm = SimpleNamespace(
        run_state=RunState(),
        chunks_from_payload=lambda: None,
        chunks=[{}, {}, {}, {}],
        chunk_flat_to_fi_ci={0: (0, 0), 1: (0, 1), 2: (1, 0), 3: (1, 1)},
        chunk_idx=1,
        file_idx=0,
        _next_uncommitted_chunk_idx=lambda start: start,
        _advance_file_or_done=MagicMock(return_value=True),
        state=None,
    )
    InjectorFSM._on_pipeline_end(fsm)
    fsm._advance_file_or_done.assert_called_once()
    # cursor untouched: the warmed PAYLOAD guard positions it for the new file
    assert fsm.chunk_idx == 1


def test_contain_chunk_failure_advances_file_when_next_chunk_crosses():
    from silica.router.orchestrator import InjectorFSM
    progress = MagicMock()
    progress.tasks = []
    fsm = SimpleNamespace(
        chunk_idx=1,
        file_idx=0,
        chunk_flat_to_fi_ci={0: (0, 0), 1: (0, 1), 2: (1, 0)},
        progress=progress,
        run_state=RunState(),
        chunk=ChunkState(),
        run_inverses=[],
        file_chunks={0: {"source_file": "f0", "chunks": [{}, {}]}},
        inbox_file="f0",
        _failed_phase_id=lambda: "",
        chunks_from_payload=lambda: None,
        chunks=[{}, {}, {}],
        _next_uncommitted_chunk_idx=lambda start: start,
        _advance_file_or_done=MagicMock(return_value=True),
        state=None,
    )
    InjectorFSM.contain_chunk_failure(fsm)
    fsm._advance_file_or_done.assert_called_once()
    assert fsm.chunk_idx == 1
