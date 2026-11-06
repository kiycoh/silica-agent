# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""FSM bookkeeping guards: checkpoint ids and per-file defer attribution.

Both are silent-corruption bugs: nothing raises when a checkpoint is filed
under an id nobody reads, or when a file's deferred work is attributed to the
previous file — the run just does the wrong thing on the NEXT run.
"""
from unittest.mock import patch


from silica.router.orchestrator import InjectorFSM
from silica.router.phases import InjectorState
from silica.router.states import distill as d
from silica.router.states import setup as s


# ----------------------------------------------------------------------
# VALIDATE checkpoint id  ⟷  DELEGATE resume check
# ----------------------------------------------------------------------

_OP = {"type": "write", "path": "Concepts/Alpha.md", "heading": "Alpha",
       "content": "Alpha is a thing."}

_VALIDATE_OK = {
    "success": True,
    "validated_count": 1,
    "rejected_count": 0,
    "validated_ops": [_OP],
    "rejected_ops": [],
    "rejection_rate": 0.0,
}


def _validated_fsm(chunk_hash: str = "deadbeef") -> InjectorFSM:
    """An FSM parked on the second file's first chunk, ready for VALIDATE."""
    fsm = InjectorFSM(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
    fsm.chunks = [{"batches": []}, {"batches": []}]
    fsm.chunk_flat_to_fi_ci = {0: (0, 0), 1: (1, 0)}
    fsm.chunk_idx = 1
    fsm.file_idx = 1
    fsm.file_chunks = {
        0: {"source_file": "Inbox/a.md", "chunks": [fsm.chunks[0]]},
        1: {"source_file": "Inbox/b.md", "chunks": [fsm.chunks[1]]},
    }
    fsm.run_state.chunk(1).hash = chunk_hash
    fsm.run_state.chunk(1).input_hash = chunk_hash
    fsm.chunk.sanitized = {"parsed": {"updates": [_OP]}}
    fsm.state = InjectorState.VALIDATE
    return fsm


def test_validate_checkpoint_is_filed_under_the_id_the_readers_use():
    fsm = _validated_fsm()

    with patch.object(d, "validate_ops", return_value=_VALIDATE_OK):
        d.handle_validate(fsm)

    task_id = fsm.chunk_task_id("validate")
    assert task_id == "f1_c0_validate"
    assert fsm.progress.is_checkpoint_done(task_id, "deadbeef") == fsm.chunk.ops_path

    # The legacy "chunk_<flat idx>_validate" id must not be created at all: it
    # both hid the checkpoint and left the real task running forever.
    ids = [t.id for t in fsm.progress.tasks]
    assert "chunk_1_validate" not in ids
    assert [t.status for t in fsm.progress.tasks if t.id == task_id] == ["done"]


def test_resume_skips_the_distiller_for_an_unchanged_chunk():
    """The checkpoint VALIDATE writes is what DELEGATE reads to skip a chunk."""
    fsm = _validated_fsm()

    with patch.object(d, "validate_ops", return_value=_VALIDATE_OK):
        d.handle_validate(fsm)

    # A resume re-enters DELEGATE for the same chunk with the same input hash.
    fsm.state = InjectorState.DELEGATE
    fsm.chunk.ops_path = ""
    with patch.object(d, "run_distiller",
                      side_effect=AssertionError("distiller re-ran a checkpointed chunk")):
        d.handle_delegate(fsm)

    assert fsm.state == InjectorState.SNAPSHOT
    assert fsm.chunk.ops_path


# ----------------------------------------------------------------------
# Pre-chunk defers are attributed to the file being assembled
# ----------------------------------------------------------------------

def _payload_fsm() -> InjectorFSM:
    """An FSM entering PAYLOAD for file 1 with file 0's chunk still under the cursor."""
    fsm = InjectorFSM(inbox_files=["Inbox/a.md", "Inbox/b.md"], target_dir="Concepts")
    fsm.file_content_hashes = ["hash-of-a", "hash-of-b"]
    fsm.chunks = [{"batches": []}]
    fsm.chunk_flat_to_fi_ci = {0: (0, 0)}
    fsm.file_chunks = {0: {"source_file": "Inbox/a.md", "chunks": [fsm.chunks[0]]}}
    fsm.chunk_idx = 0          # still file 0's chunk
    fsm.file_idx = 1
    fsm.run_state.recon = [{"concepts": []}]
    fsm.run_state.payload = {"chunks": [{"batches": [{"inbox_file": "Inbox/a.md"}]}]}
    fsm.run_state.vault_graph_ctx = {}
    fsm.state = InjectorState.PAYLOAD
    return fsm




def test_payload_reassembles_a_file_whose_chunks_never_attached():
    """`file_chunks[fi]` is claimed before assembly, so the warm fast-path must
    key on the chunk list, not on the key's presence — otherwise a file that
    failed mid-assembly would fast-path with zero chunks on the next attempt."""
    fsm = _payload_fsm()
    fsm.file_chunks[1] = {"source_file": "Inbox/b.md", "chunks": []}

    res = {"chunks": [{"schema_version": 1, "batches": [
        {"inbox_file": "Inbox/b.md", "concepts": [{"name": "Beta", "excerpt": "y"}]}]}]}
    with patch.object(s, "payload_chunks", return_value=res):
        s.handle_payload(fsm)

    assert fsm.file_chunks[1]["chunks"], "PAYLOAD fast-pathed on an empty chunk list"
    assert fsm.chunk_idx == 1
    assert fsm.chunk_flat_to_fi_ci[1] == (1, 0)


def test_warm_next_file_reprepares_a_file_whose_chunks_never_attached():
    fsm = _payload_fsm()
    fsm.file_chunks[1] = {"source_file": "Inbox/b.md", "chunks": []}
    fsm.file_idx = 0

    with patch.object(s, "recon_file", return_value={"error": "boom"}) as recon:
        assert s.warm_next_file(fsm) is False
    assert recon.called, "warm stood down on a file it had only claimed, not attached"
