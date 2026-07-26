# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Code-lane agent tools — silica_document (ADR-0012) and silica_code_why.

silica_document — stage a skeleton stub from a source file (ADR-0012).

Thin agent-facing wrapper over the code SourceAdapter (ADR-0014): guards,
sanitization and stub assembly live in silica/sources/code.py. Writes ONLY
to Inbox/ — RBAC inbox-write, never the vault. No LLM call here: the
curation pipeline refines Inbox stubs.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from silica.tools import tool


class DocumentArgs(BaseModel):
    path: str = Field(description="Repo-relative path to the source file to document")


@tool(DocumentArgs, cls="composed")
def silica_document(path: str) -> dict:
    """Extract a shallow AST skeleton from a source code file and stage it as a
    documentation stub in Inbox/ (never directly in the vault). Sets
    documents:/code_ref frontmatter for staleness tracking; source-derived text
    is sanitized and fenced. Nucleate the stub afterwards with silica_run_injector."""
    from silica.driver import DRIVER
    from silica.sources.code import CODE

    try:
        item = CODE.read(path)
        stub = CODE.to_stub(item)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    DRIVER.upsert(stub.note_path, stub.body)  # re-running on the same file refreshes the stub
    return {
        "status": "ok",
        "note_path": stub.note_path,
        "code_ref": item.meta.get("code_ref", ""),
        "skeleton": item.meta.get("language") is not None,
    }


class CodeWhyArgs(BaseModel):
    path: str = Field(
        default="",
        description="Repo-relative path, file or directory. '' means the repo root.",
    )


@tool(CodeWhyArgs, cls="composed")
def silica_code_why(path: str = "") -> dict:
    """Recorded rationale for a code path: why it is the way it is, which
    directions were tried and closed, which ceilings were measured.

    This is the part grep cannot answer. The source says what the code does; the
    notes bound to it say what was decided and what was rejected — read this
    BEFORE proposing a change to a path, or you will re-propose something that
    was already measured and killed.

    Rolls up containment: a query on a directory also returns notes bound to
    files inside it and to the packages above it. `stale: true` means the bound
    file moved past the note's recorded commit, so weigh it accordingly.
    Coverage is sparse by design — an empty result means nothing was recorded,
    not that nothing was decided. Bind new rationale with the `documents`
    argument of silica_write_note / silica_patch_note.
    """
    from dataclasses import asdict

    from silica.config import CONFIG
    from silica.kernel import codetree

    notes, residue = codetree.why_for(getattr(CONFIG, "vault_path", "") or "", path)
    return {"status": "ok", "path": path,
            "notes": [asdict(n) for n in notes], "residue": residue}
