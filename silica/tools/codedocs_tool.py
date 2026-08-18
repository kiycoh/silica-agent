# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Code-lane agent tools — silica_document (ADR-0012), silica_code_pack.

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
        item.meta["stage_to_inbox"] = True  # RBAC inbox-write, never the vault
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


class CodePackArgs(BaseModel):
    target: str = Field(
        description="Repo-relative source path, optionally narrowed with "
                    "'#Class' or '#Class.member'"
    )
    budget_chars: int = Field(
        default=24000,
        description="Character budget for the whole pack. The target is always "
                    "served, sections fill what is left.",
    )


@tool(CodePackArgs, cls="composed")
def silica_code_pack(target: str, budget_chars: int = 24000) -> dict:
    """Deterministic context pack for one source file inside a character
    budget: the target plus its supertypes, extenders, the visible signatures
    it actually names, external dependencies, and importers. A closure, not a
    search — same repo state, same bytes. Use before rewriting or porting a
    file, instead of ten greps.

    `target` is repo-relative, optionally narrowed with '#Class' or
    '#Class.member'. `target_mode`: "verbatim" = whole file; "symbol" = that
    declaration whole, rest as outline; "outline" = signatures only (over
    budget) — check `truncated` before treating the target as complete.
    `dropped`: `note: ...` entries are degrades (not fetchable); other entries
    are `<section>: <label>` items that did not fit and can be requested by
    label. Section counts are true repo-wide totals.
    """
    from silica.config import CONFIG
    from silica.kernel.code import codepack

    vault = str(getattr(CONFIG, "vault_path", "") or "").strip()
    if not vault:
        return {"status": "error", "message": "no vault configured"}
    try:
        pack = codepack.code_pack(vault, target, budget_chars)
    except (ValueError, OSError) as e:
        # OSError: loading the code graph can write its store, and an
        # unwritable store is a tool-level error, not a crash of the caller.
        return {"status": "error", "message": str(e)}
    return {"status": "ok", **pack}
