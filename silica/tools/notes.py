# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Single-note tools — fast-path create/patch with /undo checkpoints.

The interactive-edit counterparts of the batch gate in kernel.write.gate: one
note, no snapshot, a checkpoint for /undo instead of a journal run.
"""
from __future__ import annotations


import re
from typing import Annotated, Any

from pydantic import Field

from silica.driver import DRIVER
from silica.tools import tool
from silica.kernel.write.notes import write_note
from silica.kernel.write.ops import Op, OpType

# One description, two tools: write_note and patch_note bind `documents:` the
# same way, and a model that learns the contract on one must read the same words
# on the other. Kept as a bare string, not a Field: the parameter's default now
# lives in the signature, and pydantic refuses a default declared twice.
_DOCUMENTS_DESC = (
    "Repo-relative paths (file or directory) whose rationale this note "
    "records — the why, not the what: closed directions, measured "
    "ceilings, constraints not derivable from the source. Conventionally "
    "the note lives at <wiki_dir>/<repo-path>.md so the vault mirrors "
    "the code tree. /stale flags the note once a commit moves the "
    "bound path past the recorded code_ref."
)


def _bind_documents(entries: list[str]) -> tuple[list[str], str | None, str | None]:
    """Validate a `documents:` binding against the repo. Returns
    (paths, code_ref, error) — on error the caller must not write."""
    from silica.config import CONFIG
    from silica.kernel.code import codedocs
    from silica.kernel.base import gitstate
    from silica.kernel.base.roots import repo_root_for

    root = repo_root_for(CONFIG.vault_path or "")
    if root is None:
        return [], None, "no repo for this vault: `documents:` needs codebase mode"
    docs, err = codedocs.validate_documents(entries, root)
    if err:
        return [], None, err
    # code_ref only when a file is bound: a directory binding records a
    # rationale, and a rationale does not expire because some file under the
    # package changed (a file binding keeps staleness — see codedocs).
    ref = gitstate.head_ref(root) if any((root / p).is_file() for p in docs) else None
    return docs, ref, None


# Both write tools' text fields carry it, and every refusal repeats it. The two
# shapes are the ones the 2026-10-28 mind map broke on: `(` and `[` inside node
# text open a node shape, and a [[link]] there is not drawn as a link anyway.
_MERMAID_RULE = ('In a ```mermaid block, quote node text that has brackets or punctuation '
                 '(A["f(x)"]) and put no [[links]] in it.')
# Corrections a note's diagrams get (owner, 2026-10-28). The parser stops at
# the first error, so a diagram with two takes two rounds (that mind map failed
# on line 4, then on line 33), and past four the model's text is written as it
# is rather than lost for a diagram.
_MERMAID_TRIES = 4
# ponytail: counted per note, not per chat, so a chat that gives up mid-way
# leaves its count to the next chat on that path, which gets fewer corrections.
# Count per session if a pilot report shows it before 2027-01-31.
_mermaid_refused: dict[str, int] = {}


def _mermaid_gate(tool: str, path: str, text: str) -> tuple[dict | None, str | None]:
    """(refusal, warning) for the ```mermaid fences of `text`. Only the model's
    own call is ever refused (MODEL_CALL): the same function run by code or
    inside another tool, a reading card or a rework, has no one to fix the
    diagram, and a refusal there would lose the text it carries."""
    from silica.config import CONFIG
    from silica.kernel.link.mermaid import parse_errors
    from silica.tools import MODEL_CALL

    if MODEL_CALL.get() != tool:
        return None, None
    key = f"{CONFIG.vault_path}::{path}"
    tries = _mermaid_refused.pop(key, 0)
    errors = parse_errors(text)
    if not errors:
        return None, None
    if tries >= _MERMAID_TRIES:
        # Under the 80 characters the eager projection keeps of a write result
        # (compaction.generic_projection), or the model never reads it.
        return None, f"Mermaid still fails to parse after {tries} corrections: tell the user"
    _mermaid_refused[key] = tries + 1
    return {"error": (f"Not written: the user would see an error instead of the diagram. Fix it and "
                      f"call {tool} again (correction {tries + 1} of {_MERMAID_TRIES}). {_MERMAID_RULE}\n"
                      + "\n".join(errors))}, None


@tool(cls="composed", collapse="eager", ingress=False, mutates="written", note_args=("name",),
      label=("patch note", "name"))
def silica_patch_note(
    name: Annotated[str, Field(description='Name or vault-relative path of the note to patch')],
    heading: Annotated[str, Field(description='Concept/section heading the snippet is filed under')],
    snippet: Annotated[str, Field(description='Distilled body text to append to the note. ' + _MERMAID_RULE)],
    source_basename: Annotated[str, Field(description='Provenance: source filename this snippet derives from')],
    hub: Annotated[str | None, Field(description='Optional [[Hub]] to link in frontmatter if missing')] = None,
    documents: Annotated[list[str] | None, Field(description=_DOCUMENTS_DESC)] = None,
) -> dict[str, Any]:
    """Append a snippet under a heading in a single EXISTING note — the fast path
    for interactive edits.

    To create a new note use silica_write_note<<; for nucleating whole documents
    into many notes use silica_run_injector>>. Every successful patch is
    checkpointed and can be reverted with /undo.
    """
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.base.layout import contain_in_vault
    from silica.kernel.write import templates as tpl
    from silica.kernel.write.bulk import execute_one
    from silica.kernel.write.checkpoints import checkpoint_landed
    from silica.kernel.workqueue import path_lease

    docs: list[str] = []
    code_ref: str | None = None
    if documents:
        docs, code_ref, err = _bind_documents(documents)
        if err:
            return {"error": err}

    # Resolve the note to its vault-relative path (read is idempotent).
    try:
        path = DRIVER.read_note(name).ref.path or name
    except Exception as e:
        return {"error": f"Failed to read note '{name}': {e}"}

    # read_note falls back to the resolved absolute path for anything it found
    # outside the vault, and the patch below writes back to whatever it returned:
    # confine it before the lease so the patch can never land outside.
    try:
        path = contain_in_vault(path, Path(CONFIG.vault_path))
    except ValueError as e:
        return {"error": f"Refused note '{name}': {e}"}

    refused, mermaid_warning = _mermaid_gate("silica_patch_note", path, snippet)
    if refused:
        return refused

    # Two meanings of `heading` meet here. Nucleation's is the CONCEPT: the
    # landing (templates.patch_snippet) drops a leading `## <heading>` as the
    # note's own name and joins the text to the lead. A chat caller's is the
    # SECTION: "add a '## Da ripassare' section at the end" put one bare line
    # at the top of three lectures (prompt-rules probe, 2026-09-28). A section
    # that is not the note's own name becomes the snippet's H2, and the op
    # names the note itself as the concept, so the landing files it at the
    # tail and nucleation's rule stays as it was.
    # ponytail: a section the note already has gets a second one of that
    # name, not the text under the first. Merge via templates.append_under
    # when a run shows a duplicated section.
    note_name = Path(path).stem
    if (heading.strip() and heading.strip().casefold() != note_name.casefold()
            and not re.match(r"#{1,6}\s", snippet.lstrip())):
        snippet = f"## {heading.strip()}\n\n{snippet.lstrip()}"
        heading = note_name

    op = Op(
        op=OpType.patch,
        heading=heading,
        source_basename=source_basename,
        path=path,
        snippet=snippet,
        hub=hub,
    )

    # Read prior content, patch and checkpoint all under the lease: the
    # read-modify-write must not interleave with another writer on this note.
    with path_lease(path):
        try:
            prior_content = DRIVER.read_note(path).content
        except Exception as e:
            return {"error": f"Failed to read note '{name}': {e}"}

        try:
            result = execute_one(op)
        except Exception as e:
            return {"error": f"Failed to patch '{name}': {e}"}

        if docs:
            try:
                patched = DRIVER.read_note(path).content
                stamped = tpl.stamp_documents(patched, docs, code_ref)
                if stamped != patched:
                    DRIVER.overwrite(path, stamped)
            except Exception as e:
                return {"error": f"Failed to bind documents on '{name}': {e}"}

            try:
                from silica.config import CONFIG
                from silica.kernel.code import codedocs
                codedocs.invalidate_snapshot(CONFIG.vault_path)
            except Exception:
                pass  # cache hygiene must never fail the write

        checkpoint = checkpoint_landed(path, prior_content)

    return {**result, "note": name, "path": path, **checkpoint,
            **({"warning": mermaid_warning} if mermaid_warning else {})}


@tool(cls="composed", collapse="eager", ingress=False, mutates="written",
      note_args=("name", "ref"), label=("flag note", "name"))
def silica_flag_note(name: Annotated[str, Field(description='Name or vault-relative path of the note to flag')], reason: Annotated[str, Field(description='Why the note is wrong or stale, in a few words')] = "", clear: Annotated[bool, Field(description='Clear a previously set flag instead of setting one')] = False,
                     ref: Annotated[str, Field(description="With clear: resolve only this entry of the note's `contradictions:` list, verbatim; default resolves every open one")] = "") -> dict[str, Any]:
    """Flag an EXISTING note as wrong or stale: marks `contested` in
    frontmatter (clear with clear=True). Call it when the user asked for it or
    agreed; otherwise name the conflict and offer it. Contested notes are
    demoted and marked at recall — never silently dropped — and surfaced in
    the run digest. Content is never edited or deleted; the human decides.
    Revertible with /undo.
    """
    import datetime

    from silica.kernel import contested_register
    from silica.kernel.write.checkpoints import checkpoint_landed
    from silica.kernel.write.contested import (
        contested_refs,
        mark_contested,
        resolve_contested,
    )
    from silica.kernel.write.notetype import agent_id

    try:
        nc = DRIVER.read_note(name)
    except Exception as e:
        return {"error": f"Failed to read note '{name}': {e}"}

    path = nc.ref.path or name
    prior_content = nc.content
    # `user` only when no agent identity exists at all: a person at the REPL.
    who = agent_id() or "user"
    today = datetime.date.today().isoformat()

    if clear:
        # Not clear_contested: dropping the flag while leaving a body callout
        # that still reads "Unresolved" makes the note lie about its own state.
        # resolve_contested files the callout under `## Superseded` first.
        new_content = resolve_contested(prior_content, resolved_by=who, valid_to=today,
                                        source_ref=ref or None)
    else:
        source_ref = f"flagged: {reason} (by {who}, {today})"
        new_content = mark_contested(prior_content, source_ref)

    # Read the state back off the note instead of assuming `not clear`: with a
    # `ref` the clear is partial, and a note with contradictions still open is
    # contested whatever the caller asked for.
    still_contested = bool(contested_refs(new_content))

    if new_content == prior_content:
        return {"note": name, "path": path, "contested": still_contested, "changed": False}

    from silica.kernel.write.preserve import Loss

    try:
        # A clear files each resolved callout under ## Superseded and rewrites
        # its tail (RESOLVE); setting a flag only adds, and is judged as such.
        DRIVER.overwrite(path, new_content, loss=Loss.RESOLVE if clear else None)
    except Exception as e:
        return {"error": f"Failed to write '{name}': {e}"}

    contested_register.note(path, still_contested)
    return {"note": name, "path": path, "contested": still_contested,
            **checkpoint_landed(path, prior_content, new_content)}


@tool(cls="composed", collapse="eager", ingress=False, mutates="written", note_args=("name",),
      label=("require", "name"))
def silica_require(
    name: Annotated[str, Field(description='Name or vault-relative path of the note that requires another')],
    prerequisite: Annotated[str, Field(description='Name or path of the note to read before it')],
    clear: Annotated[bool, Field(description='Remove this prerequisite instead of adding it')] = False,
) -> dict[str, Any]:
    """Record that a note requires another: `requires:` in its frontmatter
    (ADR-0145). Memorize orders study by it and /path follows it. Call it only
    for a pair the user accepted; otherwise propose the pair. Revertible with /undo.
    """
    from silica.kernel.base import frontmatter
    from silica.kernel.write.checkpoints import checkpoint_landed

    try:
        nc = DRIVER.read_note(name)
        target = DRIVER.read_note(prerequisite).ref.path
    except Exception as e:
        return {"error": f"Failed to read '{name}' or '{prerequisite}': {e}"}
    path = nc.ref.path or name
    if target == path:
        return {"error": "A note cannot require itself."}
    # The shortest link that names one note: the file name, unless several
    # notes share it (ADR-0114), then the path.
    stem = target.rsplit("/", 1)[-1].removesuffix(".md")
    try:
        shared = len(DRIVER.notes_named(stem)) > 1
    except Exception:
        shared = True  # a backend without the name index: the path is never ambiguous
    link = target.removesuffix(".md") if shared else stem

    data, _raw, _body = frontmatter.split(nc.content)
    names = frontmatter.requires_in(data)
    same = {link.casefold(), stem.casefold(), target.removesuffix(".md").casefold()}
    if clear:
        wanted = [n for n in names if n.casefold() not in same]
    else:
        wanted = names if any(n.casefold() in same for n in names) else names + [link]
    if wanted == names:
        return {"note": name, "path": path, "requires": names, "changed": False}
    try:
        if wanted or not DRIVER.composes_props:  # the Obsidian bridge has no removal
            DRIVER.set_prop(path, "requires", [f"[[{n}]]" for n in wanted])
        else:
            DRIVER.unset_prop(path, "requires")
    except Exception as e:
        return {"error": f"Failed to write '{name}': {e}"}

    return {"note": name, "path": path, "requires": wanted, **checkpoint_landed(path, nc.content)}


@tool(cls="composed", collapse="eager", ingress=False, mutates="written", note_args=("path",),
      label=("write note", "path"))
def silica_write_note(
    path: Annotated[str, Field(description="Vault-relative path for the new note (e.g. 'Computer Science/Computer Vision.md')")],
    body: Annotated[str, Field(description='Markdown body only — NO YAML frontmatter; it is applied mechanically from the vault template. ' + _MERMAID_RULE)],
    title: Annotated[str | None, Field(description='H1 title; defaults to the filename stem')] = None,
    tags: Annotated[list[str] | None, Field(description='Frontmatter tags; normalized automatically')] = None,
    related: Annotated[list[str] | None, Field(description='Related note names, rendered as frontmatter wikilinks')] = None,
    parent: Annotated[str | None, Field(description="Parent note name for the 'parent note' frontmatter key")] = None,
    template: Annotated[str | None, Field(description="Named template from the vault's templates dir; 'none' skips the skeleton (AI/last-modified floor still applied)")] = None,
    props: Annotated[dict[str, str] | None, Field(description="Extra scalar frontmatter keys, e.g. {'type': 'syllabus'}; AI/last modified/verified are reserved")] = None,
    documents: Annotated[list[str] | None, Field(description=_DOCUMENTS_DESC)] = None,
) -> dict[str, Any]:
    """Create a new note — the fast path for single-note creation.

    Pass frontmatter as structured fields (title/tags/related/parent, scalar
    `props`), never raw YAML in `body` (a leading YAML block is stripped).
    Skeleton from the vault template (explicit `template` > vault default >
    built-in); `template="none"` writes the body as-is. Fails if the note
    exists: append with silica_patch_note<<; multi-note nucleation with gates
    and rollback: silica_run_injector>>. Checkpointed, revertible with /undo.
    """
    bound: list[str] = []

    def bind() -> tuple[list[str], str | None, str | None]:
        docs, code_ref, err = _bind_documents(documents or [])
        bound.extend(docs)
        return docs, code_ref, err

    warning: list[str] = []

    def gate(landing: str) -> dict | None:
        refused, mermaid_warning = _mermaid_gate("silica_write_note", landing, body)
        if mermaid_warning:
            warning.append(mermaid_warning)
        return refused

    out = write_note(path, body, title=title, tags=tags, related=related, parent=parent,
                     template=template, props=props, bind=bind if documents else None, gate=gate)
    if bound and out.get("success"):
        try:
            from silica.config import CONFIG
            from silica.kernel.code import codedocs
            codedocs.invalidate_snapshot(CONFIG.vault_path)
        except Exception:
            pass  # cache hygiene must never fail the write
    return {**out, "warning": warning[0]} if warning and out.get("success") else out
