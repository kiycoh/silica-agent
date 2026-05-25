"""Canonical Op schema for the Silica pipeline (ADR-007 / Addendum C1).

This is the single source of truth imported by sanitize, validate, snapshot,
bulk, and lint. No module defines its own op structure locally.

Key invariant: touched_ref() returns op.path — NEVER a field named 'name'
(which does not exist). This closes B1 at the root.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class OpType(str, Enum):
    write = "write"          # create new note (path MUST NOT exist)
    patch = "patch"          # enrich existing note (path MUST exist)
    overwrite = "overwrite"  # rewrite whole note, preserve identity/history
    delete = "delete"        # only via wrapped tool + confirm
    skip = "skip"            # explicit no-op (excluded from gate denominator)


class Op(BaseModel):
    op: OpType
    heading: str                        # concept name; provenance key in payload
    source_basename: str                # inbox filename (basename) this op derives from
    path: str | None = None             # vault-relative path; required for write/patch/overwrite/delete
    snippet: str = ""                   # distilled body (write / patch)
    hub: str | None = None              # [[Hub]] link required for write ops
    content: str | None = None          # full body (overwrite only)
    tags: list[str] | None = None
    related: list[str] | None = None
    reason: str | None = None           # skip reason / rejection note

    def touched_ref(self) -> str | None:
        """The vault path touched by this op.

        This is the ONLY authorised way for lint/snapshot to derive the
        note reference from an op. Using any other field (e.g. 'name') is a
        violation — 'name' does not exist on Op (closes B1).
        """
        if self.op in (OpType.write, OpType.patch, OpType.overwrite, OpType.delete):
            return self.path
        return None


# ---------------------------------------------------------------------------
# Rollback inverse ops (ADR-009 / Addendum C3)
# ---------------------------------------------------------------------------

class InverseOpKind(str, Enum):
    delete_created = "delete_created"       # undo a write: delete the note that was created
    restore_version = "restore_version"     # undo a patch: history:restore to prior version
    recreate_deleted = "recreate_deleted"   # undo a delete: recreate with prior content


class InverseOp(BaseModel):
    kind: InverseOpKind
    path: str
    version: int | None = None            # for restore_version
    prior_content: str | None = None      # for recreate_deleted
