# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Code-lane agent tools: silica_document (ADR-0012) and silica_impact.

silica_document — stage a skeleton stub from a source file (ADR-0012).

Thin agent-facing wrapper over the code SourceAdapter (ADR-0014): guards,
sanitization and stub assembly live in silica/sources/code.py. Writes ONLY
to Inbox/ — RBAC inbox-write, never the vault. No LLM call here: the
curation pipeline refines Inbox stubs.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

from silica.tools import tool


@tool(cls="composed", ingress=False, note_args=("path",))
def silica_document(path: Annotated[str, Field(description='Repo-relative path to the source file to document')]) -> dict:
    """Extract a shallow AST skeleton from a source code file and stage it as a
    documentation stub in Inbox/ (never directly in the vault). Sets
    documents:/code_ref frontmatter for staleness tracking; source-derived text
    is sanitized and fenced. Nucleate the stub afterwards with silica_run_injector."""
    from silica.driver import DRIVER
    from silica.sources.code import CODE

    try:
        item = CODE.read(path)
        item.meta["stage_to_inbox"] = True  # RBAC inbox-write, never the vault
        stub = CODE.to_stub(item)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    from silica.kernel.write.preserve import Loss

    # STAGED holds only inside the inbox, which check_write tests at run time.
    DRIVER.upsert(stub.note_path, stub.body, loss=Loss.STAGED)  # re-running refreshes the stub
    return {
        "status": "ok",
        "note_path": stub.note_path,
        "code_ref": item.meta.get("code_ref", ""),
        "skeleton": item.meta.get("language") is not None,
    }


_IMPACT_CAP = 100  # entries; a diff wider than this is a rewrite, not a change


@tool(cls="composed", ingress=False)
def silica_impact(range_spec: Annotated[str, Field(description="Git range or ref (e.g. 'HEAD~3..HEAD', 'main..HEAD', one SHA = that ref vs the working tree). Empty = uncommitted changes vs HEAD.")] = "") -> dict:
    """Which notes a code change touches: changed source files (working tree
    vs HEAD by default, or a git range) classified cosmetic/structural, each
    with the notes documenting it and the notes of its 1-hop import
    neighbors, sorted structural-first by fan-in. The blast-radius call to
    make before and after editing code the vault documents. Deterministic,
    no LLM; a vault outside a git repo answers status "no_repo"."""
    from silica.config import CONFIG
    from silica.kernel.code.codegraph import compute_impact

    vault = str(getattr(CONFIG, "vault_path", "") or "").strip()
    if not vault:
        return {"status": "error", "message": "no vault configured"}
    try:
        entries = compute_impact(vault, range_spec.strip() or None)
    except (ValueError, OSError) as e:
        # OSError: the graph load may write its derived store, and an
        # unwritable store is a tool-level error.
        return {"status": "error", "message": str(e)}
    if entries is None:
        return {
            "status": "no_repo",
            "message": "vault is not inside a git repository; the code lane is off",
        }
    return {
        "status": "ok",
        "total": len(entries),
        "entries": [
            {
                "path": e.path,
                "change_level": e.change_level,
                "details": list(e.details),
                "fan_in": e.fan_in,
                "notes": list(e.notes),
                "neighbor_notes": list(e.neighbor_notes),
            }
            for e in entries[:_IMPACT_CAP]
        ],
        # Never a silent cap (same discipline as the tabular lane).
        "truncated": len(entries) > _IMPACT_CAP,
    }
