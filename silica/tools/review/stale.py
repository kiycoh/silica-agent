# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Notes whose documented code changed shape since their `code_ref`: /stale's
code lane, listed from the HEAD-keyed snapshot, so a listing reads git and
calls no model.

A yes re-badges the note: `code_ref` moves to HEAD, the "edit and re-badge"
/stale has always asked for, journalled so the batch's revert puts the old ref
back. It says the note still holds, which only a person who read it can say,
so `apply all` passes it over. /stale's other two lanes are not kinds: a
person's attestation is /verify's alone to write, and a note edited by hand
since the gate wrote it has nothing to apply."""
from __future__ import annotations

from pathlib import Path

from silica.kernel.review import Kind, Proposal, proposal_id, register
from silica.kernel.text.note_parts import clean_name


def _pending(vault: str) -> list[Proposal]:
    from silica.kernel.code import codedocs

    by_note: dict[str, list] = {}
    for sd in codedocs.snapshot(Path(vault)):
        by_note.setdefault(sd.note_path, []).append(sd)
    out = []
    for note, docs in sorted(by_note.items()):
        level, details = codedocs.note_verdict(docs)
        if level != codedocs.CHANGE_STRUCTURAL:
            continue  # cosmetic drift is /stale --all's, not a thing to act on
        first = docs[0]
        more = f", and {len(docs) - 1} more file(s)" if len(docs) > 1 else ""
        out.append(Proposal(
            # The refs the files moved to are in the id: a commit after a no
            # makes a new proposal, since the note may have fallen further behind.
            id=proposal_id("stale", note, *sorted({d.current_ref for d in docs})), kind="stale",
            title=clean_name(note),
            why=f"{first.code_path} changed in {len(first.intervening)} commit(s) "
                f"since {first.recorded_ref[:8]}{more}",
            notes=(note,), preview="\n".join(details[:6]), data={"note": note}))
    return out


def _apply(vault: str, props: list[Proposal]) -> dict[str, str | None]:
    from silica.kernel.base import frontmatter, gitstate, roots
    from silica.kernel.code import codedocs
    from silica.kernel.write import gate
    from silica.kernel.write.templates import stamp_documents

    root = roots.repo_root_for(vault)
    head = gitstate.head_ref(root) if root else None
    if not head:
        return {p.id: "no commit to badge it with: the vault is in no git repository" for p in props}

    def rebadge(text: str) -> str:
        data, _raw, _body = frontmatter.split(text)
        docs = frontmatter.documents_of(data if isinstance(data, dict) else {})
        return stamp_documents(text, docs, head) if docs else text

    out: dict[str, str | None] = {}
    for p in props:
        landed = gate.journaled_write(p.data["note"], rebadge, source="review", run=gate.current_run() or "")
        out[p.id] = None if landed else "nothing to badge: it names no documents"
    codedocs.invalidate_snapshot(Path(vault))  # keyed on HEAD, which a badge does not move
    return out


register(Kind("stale", "Notes behind their code", _pending, _apply, in_all=False))
