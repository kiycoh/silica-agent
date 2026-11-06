# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What an injector run knows while it runs: typed, and declared here.

This was one dict of 45 string keys, `chunk_{i}_*` and `file_{i}_*` families
among them, written in one module and read in another with nothing saying
which keys existed. None of it is persisted: the run's disk state is the
progress ledger, the manifest and the journal, so a resumed run starts these
empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from silica.driver.base import GraphSnapshot
    from silica.kernel.write.ops import Txn


@dataclass
class FileState:
    """What one inbox file pins for all of its chunks, by its index in
    inbox_files."""
    lane: str = ""
    profile: str | None = None
    # None until the form ladder ran: "" is a verdict, and the ladder runs once.
    form: str | None = None
    form_origin: str = ""
    language: str | None = None
    # Left by warm_next_file for RECON, PAYLOAD and SALIENCE to take at their
    # normal place in the sequence.
    warm_recon: dict | None = None
    warm_payload: dict | None = None
    salience_done: bool = False


@dataclass
class ChunkState:
    """One chunk's working state, by its flat index.

    Keyed and not one shared bag because COLLISION runs ahead of the cursor for
    the distill prefetch window. Dropped whole at the chunk boundary: `txn` and
    `snapshot` hold notes' prior content, and a long run must not keep them.
    """
    collision_done: bool = False
    collision_ops: list[dict] = field(default_factory=list)
    # Taken before COLLISION mutates the chunk: COLLISION re-routes on what the
    # vault holds now, which a partial run and its resume see differently.
    input_hash: str = ""
    # The key the validated-ops checkpoint is filed under; a steer retry keeps it.
    hash: str = ""
    outline: Any = None
    steer_context: str | None = None
    steer_attempts: int = 0
    retry_payload: dict | None = None
    carry_ops: list = field(default_factory=list)
    distiller_output_path: str = ""
    sanitized: dict = field(default_factory=dict)
    ops_path: str = ""
    txn: "Txn | None" = None
    # S3.2 pre-write graph snapshot, for LINT's regression gate.
    pre_graph: "GraphSnapshot | None" = None
    txn_id: str | None = None
    snapshot: dict = field(default_factory=dict)
    snapshot_domain: list[dict] = field(default_factory=list)
    lint_baseline: dict = field(default_factory=dict)
    deferred_stems: list[str] = field(default_factory=list)
    # None until a phase failed: an exception with no message is still a reason.
    abort_reason: str | None = None


@dataclass
class RunState:
    # What the run reports (see result()).
    final_status: str | None = None
    error: str | None = None
    committed_chunks: int = 0
    # The per-chunk ledger: `error` is last-write-wins, which once collapsed 6
    # batch failures into "batch 5 failed" and fed a false "5/6 ok" report.
    failed_chunks: list[dict] = field(default_factory=list)
    files_summary: list[dict] = field(default_factory=list)
    yield_notes: int = 0
    yield_links: int = 0
    renucleated: dict[str, int] = field(default_factory=dict)

    # Run-wide verdict inputs.
    has_partial_failure: bool = False
    run_had_ops: bool = False

    # The first file's identity, for a caller that never ran run().
    source_canonical: str = ""
    source_content_hash: str = ""

    recon: list[dict] = field(default_factory=list)
    payload: dict | None = None
    # None until built, once per run: an empty context is a built one.
    vault_graph_ctx: dict | None = None
    deferred: list[dict] = field(default_factory=list)
    salience_dropped: int | None = None
    escalations: int = 0
    distill_secs: dict[int, float] = field(default_factory=dict)
    validate: dict = field(default_factory=dict)
    write: dict = field(default_factory=dict)
    grounding: list[dict] = field(default_factory=list)
    ambiguous_links: dict[str, int] = field(default_factory=dict)
    run_cleared_parents: list = field(default_factory=list)
    cleanup_warning: str = ""
    rollback_error: str = ""
    # Memoized corpus signatures for the embedder-free dedup leg.
    minhash_sig_cache: dict = field(default_factory=dict)

    # Which in-memory indexes hold upserts the end-of-run flush must save.
    embed_dirty: bool = False
    cooccur_dirty: bool = False
    lexical_dirty: bool = False

    files: dict[int, FileState] = field(default_factory=dict)
    chunks: dict[int, ChunkState] = field(default_factory=dict)

    def file(self, fi: int) -> FileState:
        return self.files.setdefault(fi, FileState())

    def chunk(self, idx: int) -> ChunkState:
        return self.chunks.setdefault(idx, ChunkState())

    def result(self) -> dict[str, Any]:
        """What a caller gets back from a run, and the Coordinator adds to: the
        outcome only, so nothing downstream can mistake a planned concept for a
        created note. What did not happen is absent, as every reader expects."""
        keys = ("final_status", "error", "committed_chunks", "failed_chunks", "files_summary",
                "yield_notes", "yield_links", "renucleated")
        return {k: v for k in keys if (v := getattr(self, k))}
