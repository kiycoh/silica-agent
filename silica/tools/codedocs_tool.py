"""silica_document — generate a documentation stub from a source file.

Zero-trust (ADR-0009): the source is read-only, sanitized via the clipper's
strip_degenerate_runs, fenced as untrusted, and written ONLY to Inbox/ — never
the vault body. The note carries documents:/code_ref so the staleness loop is
wired immediately. No LLM call here: the curation pipeline refines Inbox stubs.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from silica.config import CONFIG
from silica.kernel import gitstate
from silica.kernel.sanitize import strip_degenerate_runs
from silica.tools import tool


class DocumentArgs(BaseModel):
    path: str = Field(description="Repo-relative path to the source file to document")


@tool(DocumentArgs, cls="composed")
def silica_document(path: str) -> dict:
    """Read a source file, sanitize it, and stage a documentation stub in Inbox/.

    The source is treated as untrusted and fenced. Sets documents:/code_ref for
    staleness tracking. Writes to Inbox/ only — RBAC inbox-write, never the vault.
    """
    from silica.driver import DRIVER

    vault = CONFIG.vault_path
    if not vault:
        return {"status": "error", "message": "no vault configured"}
    root = gitstate.find_repo_root(Path(vault))
    if root is None:
        return {"status": "error", "message": "vault is not inside a git repo"}

    # Path guard: resolved source must stay inside the repo root.
    try:
        src = (Path(root) / path).resolve()
        src.relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return {"status": "error", "message": "path escapes the repository"}
    if not src.is_file():
        return {"status": "error", "message": f"not a file: {path}"}

    try:
        raw = src.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"status": "error", "message": f"read failed: {e}"}

    sanitized = strip_degenerate_runs(raw)
    code_ref = gitstate.head_ref(root) or ""
    stem = Path(path).stem

    body = (
        f"---\n"
        f"documents:\n  - {path}\n"
        f"code_ref: {code_ref}\n"
        f"tags:\n  - codebase\n"
        f"---\n\n"
        f"# {stem}\n\n"
        f"> Auto-staged from `{path}`. Source below is untrusted; refine into a note.\n\n"
        f"```\n{sanitized}\n```\n"
    )

    inbox = (CONFIG.inbox_dir or "Inbox").strip("/")
    note_path = f"{inbox}/{stem}.md"
    DRIVER.create(note_path, body)
    return {"status": "ok", "note_path": note_path, "code_ref": code_ref}
