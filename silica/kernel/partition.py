# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import json


def partition_by_file(
    payload: dict,
    max_concepts: int,
    max_bytes: int = 80 * 1024,
) -> list[dict]:
    """Partition payload into per-source-file groups, each chunked internally.

    Returns a list of dicts:
        [{"source_file": str, "chunks": [<chunk_dict>, ...]}, ...]

    Invariant: no chunk spans two source files. Each chunk dict has the standard
    {"schema_version": ..., "batches": [{"inbox_file": str, "concepts": [...]}]}
    shape, plus a "source_file" key tagging which inbox file it belongs to.

    Chunk size constraints (max_concepts, max_bytes) are applied per-file using
    the existing partition_by_concepts logic.
    """
    schema_version = payload.get("schema_version", 1)
    result: list[dict] = []

    for batch in payload.get("batches", []):
        inbox_file: str = batch.get("inbox_file", "")
        concepts: list = batch.get("concepts", [])
        if not concepts:
            continue

        # Build a single-file sub-payload and partition it
        sub_payload = {"schema_version": schema_version, "batches": [{"inbox_file": inbox_file, "concepts": concepts}]}
        chunks = partition_by_concepts(sub_payload, max_concepts, max_bytes)

        # Tag each chunk with its source_file
        tagged_chunks = [dict(chunk, source_file=inbox_file) for chunk in chunks]
        result.append({"source_file": inbox_file, "chunks": tagged_chunks})

    return result


def partition_by_concepts(payload: dict, max_concepts: int, max_bytes: int = 80 * 1024) -> list:
    """Deterministic partition of payload into chunks.
    
    Each chunk is a payload dict of the form:
      {"schema_version": schema_version, "batches": [...]}
    such that:
      1. Total concept count in the chunk <= max_concepts (if max_concepts > 0)
      2. JSON-serialized size of the chunk <= max_bytes
    
    If a single concept itself exceeds max_bytes, it is placed in its own chunk.
    Order of batches and concepts is strictly preserved for determinism.
    """
    schema_version = payload.get("schema_version", 1)
    limit = max_concepts if max_concepts > 0 else float("inf")

    flat = [
        (batch["inbox_file"], concept)
        for batch in payload.get("batches", [])
        for concept in batch.get("concepts", [])
    ]

    def build(items: list[tuple[str, dict]]) -> dict:
        batches: dict[str, list[dict]] = {}
        for inbox_file, concept in items:
            batches.setdefault(inbox_file, []).append(concept)
        return {
            "schema_version": schema_version,
            "batches": [{"inbox_file": k, "concepts": v} for k, v in batches.items()],
        }

    # ponytail: one exact json.dumps per concept (O(n) dumps of O(max_bytes) each).
    # A running byte counter would need to re-derive json's separator/escaping rules
    # here; not worth the drift risk unless partitioning shows up in a profile.
    chunks: list[dict] = []
    current: list[tuple[str, dict]] = []
    for item in flat:
        candidate = current + [item]
        chunk = build(candidate)
        # `or` short-circuits: the dumps is skipped when the count already trips.
        if len(candidate) > limit or len(json.dumps(chunk, ensure_ascii=False).encode()) > max_bytes:
            # An item that overflows an empty chunk can only go in one alone.
            chunks.append(build(current) if current else chunk)
            current = [item] if current else []
        else:
            current = candidate

    if current:
        chunks.append(build(current))

    return chunks
