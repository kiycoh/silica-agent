# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Create one note, the fast path for a single new note, the chat's and the code's, and set
one property on a note that exists.

Below the tools, so a Memorize plan or a reading card writes a note without
reaching up into the registry for a tool's function.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from silica.driver import DRIVER


def write_note(
    path: str,
    body: str,
    title: str | None = None,
    tags: list[str] | None = None,
    related: list[str] | None = None,
    parent: str | None = None,
    template: str | None = None,
    props: dict[str, str] | None = None,
    *,
    bind: Callable[[], tuple[list[str], str | None, str | None]] | None = None,
    gate: Callable[[str], dict | None] | None = None,
) -> dict[str, Any]:
    """Create a new note; a dict with `error` if it did not land. Fails if the
    note exists.

    `bind` and `gate` are the chat tool's: its `documents:` binding, which the
    code lane validates and this lane may not import, and the check of a model's
    diagrams, which has a turn to answer to. Each runs where the tool's refusal
    has always landed: the path and reserved keys are refused before `bind`, and
    the whole note is built before `gate`.
    """
    from silica.config import CONFIG
    from silica.kernel.base.layout import contain_in_vault
    from silica.kernel.write import templates as tpl
    from silica.kernel.write.checkpoints import checkpoint_landed
    from silica.kernel.workqueue import path_lease

    # `path` is model-supplied and this fast path never runs validate_operations,
    # so the vault boundary is enforced here — before the lease, so a rejected
    # path never takes a lock — and as an error dict rather than the driver's
    # exception, which the model cannot act on.
    try:
        path = contain_in_vault(path, Path(CONFIG.vault_path))
    except ValueError as e:
        return {"error": f"Refused path '{path}': {e}"}

    # `AI:`, `last modified:` and `verified:` are the system floor and the human
    # trust tier — a model writing them would hand the agent a lever on its own
    # authority (OKF §5.2). Reject, never silently drop: the caller must know.
    if props:
        reserved = [k for k in props if k.strip().lower() in ("ai", "last modified", "verified")]
        if reserved:
            return {"error": f"Reserved frontmatter key(s) {reserved}: "
                             "AI, last modified and verified are system-owned."}

    docs: list[str] = []
    code_ref: str | None = None
    if bind is not None:
        docs, code_ref, err = bind()
        if err:
            return {"error": err}

    if template == "none":
        content = body
    else:
        try:
            source = tpl.resolve_template(template)
        except tpl.TemplateNotFoundError as e:
            return {"error": str(e)}
        fields = tpl.prepare_fields(
            title=title or PurePosixPath(path).stem,
            body=body,
            tags=tags,
            related=related,
            parent=parent,
        )
        content = tpl.render_note(source, fields)
    content = tpl.ensure_system_floor(content)
    if props:
        try:
            content = tpl.upsert_props(content, props)
        except ValueError as e:
            return {"error": str(e)}
    if docs:
        content = tpl.stamp_documents(content, docs, code_ref)

    if gate is not None and (refused := gate(path)):
        return refused

    # The existence check and the create must be atomic: the fs backend's
    # create() now raises on an existing note, but the pre-check under lease
    # stays: it returns a friendly "use silica_patch_note" error instead of a
    # backend exception, and cross-process it still guards other agents.
    with path_lease(path):
        if DRIVER.exists(path):
            return {"error": f"Note '{path}' already exists: use silica_patch_note to modify it."}

        try:
            ref = DRIVER.create(path, content)
        except Exception as e:
            return {"error": f"Failed to create note '{path}': {e}"}

        checkpoint = checkpoint_landed(path, None)  # None: created, /undo removes it

    return {"op": "write", "success": True, "path": ref.path or path, **checkpoint}


def unset_prop(path: str, key: str) -> bool:
    """Remove a note's property; False on a backend that cannot, which is the Obsidian bridge:
    Obsidian itself can, so what to do then is the caller's."""
    if not DRIVER.composes_props:
        return False
    DRIVER.unset_prop(path, key)
    return True


def write_prop(path: str, key: str, value: bool | None) -> None:
    """Set or remove a note's property through the driver, so the change is a
    session change like any edit, and leave /undo a restore point: it reads the
    checkpoints and not the session's ledger. Not `overwrite`, which stamps a
    `type:` the person never asked for. Lowercase YAML booleans, from the dumper."""
    from silica.kernel.base import frontmatter as fm
    from silica.kernel.write.checkpoints import checkpoint_landed

    prior = DRIVER.read_note(path).content
    data, raw, _body = fm.split(prior)
    if data is None and raw is not None:
        raise ValueError(f"the properties of {path} do not parse; fix them before publishing it")
    if value is not None:
        DRIVER.set_prop(path, key, value)
    elif not unset_prop(path, key):
        raise ValueError(f"remove `{key}:` from {path} in Obsidian")
    # `hand`: the person's click and no body of Silica's, or a note they edited would read
    # as untouched to `edited_since_write` and could not be marked as theirs (ADR-0153 point 26).
    checkpoint_landed(path, prior, prior, kind="hand")
