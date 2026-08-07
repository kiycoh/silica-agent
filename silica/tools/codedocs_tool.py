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
    """Deterministic context pack for one source file: the file itself plus the
    declared facts around it, inside a character budget.

    Pass a repo-relative path, optionally narrowed with '#Class' or
    '#Class.member'. You get back the target itself, its declared supertypes
    and the repo classes that extend it, the public signatures of the files it
    can see (resolved imports, plus same-package siblings in Java) filtered to
    the ones it actually names, its external dependencies, and the files that
    import it.

    `target_mode` says how the target itself came back. "verbatim" is the whole
    file. A selector that resolves gives "symbol": that declaration whole, the
    rest of the file as a signature outline. A file too big for the budget with
    no selector gives "outline": signatures only. `truncated` is true for both
    degrades, so check it before treating the target as complete source.

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
