# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Code-lane agent tools — silica_document (ADR-0012), silica_code_why, silica_code_pack.

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
    from silica.kernel.code import codetree

    notes, residue = codetree.why_for(getattr(CONFIG, "vault_path", "") or "", path)
    return {"status": "ok", "path": path,
            "notes": [asdict(n) for n in notes], "residue": residue}


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
    """Deterministic context pack for one source file: the file itself plus the
    declared facts around it, inside a character budget.

    Pass a repo-relative path, optionally narrowed with '#Class' or
    '#Class.member'. You get back the target verbatim, its declared supertypes
    and the repo classes that extend it, the public signatures of the files it
    can see (resolved imports, plus same-package siblings in Java) filtered to
    the ones it actually names, its external dependencies, and the files that
    import it.

    This is a closure, not a search: no ranking, no embeddings, no language
    server. The same repo state gives the same bytes. Use it before rewriting
    or porting a file, so you read the surrounding contracts in one call
    instead of ten greps.

    `dropped` tells you what you are not seeing, in two kinds. An entry
    starting with `note: ` is a degrade note: something was unavailable and
    the pack got poorer, not something you can fetch. Every other entry reads
    `<section>: <label>` and is a real thing that did not fit the budget, so
    you can ask for it directly by its label. A section header's count (e.g.
    `importers (fan-in N)`) is always the true repo-wide total, even when the
    list printed under it is shorter because budget trimming dropped some of
    those entries.
    """
    from silica.config import CONFIG
    from silica.kernel.code import codepack

    vault = getattr(CONFIG, "vault_path", "") or ""
    if not vault:
        return {"status": "error", "message": "no vault configured"}
    try:
        pack = codepack.code_pack(vault, target, budget_chars)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok", **pack}
