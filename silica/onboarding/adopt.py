# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Vault adoption — declare the write boundary once, at the moment a path becomes
the vault.

A vault path is adopted as-is: Silica reads the folder the user pointed at, not a
subfolder it invented. What still needs deciding is where it may *write*, and the
answer differs by content:

  * an Obsidian vault, or a folder of prose → in place (`write_dir: ""`), which is
    the manifest default, so nothing is written and behaviour is unchanged;
  * a source tree → notes land in `docs/silica`, declared in `vault.yaml` so the
    choice is visible, versionable and editable instead of re-guessed every run.

No prompt: the detection picks a default, the caller prints it, and `vault.yaml`
makes it changeable. A question at adoption time would block the GUI and MCP
entry points for a decision one line of YAML already expresses.
"""
from __future__ import annotations

import logging
from pathlib import Path

from silica.kernel.paths import is_obsidian_vault, looks_like_code
from silica.kernel.vault_manifest import MANIFEST_REL

logger = logging.getLogger(__name__)

# Where notes go in a source tree: visible and committable next to the code.
CODE_WRITE_DIR = "docs/silica"


def write_dir_for(vault: str | Path) -> str:
    """The write boundary this folder's *content* calls for; "" ⇒ in place.

    Pure decision, no I/O beyond the detection scan. Callers that compose
    `vault.yaml` themselves (the first-run wizard) use this; `declare_write_dir`
    is the persisting variant.

    A `docs/silica` that already holds notes settles it whatever the content
    ratio says: that is a vault from before the write boundary existed, and the
    same declaration every new repo gets is also its migration — the notes stay
    where they are, the vault goes back to being the folder you launched in.
    """
    root = Path(vault)
    if not root.is_dir() or is_obsidian_vault(root):
        return ""
    # glob is lazy and stops at the first hit; a missing dir yields nothing.
    if next((root / CODE_WRITE_DIR).glob("**/*.md"), None):
        return CODE_WRITE_DIR
    return CODE_WRITE_DIR if looks_like_code(root) else ""


def declare_write_dir(vault: str | Path) -> str | None:
    """Persist `write_dir` in `<vault>/vault.yaml` when the vault needs one.

    Returns the declared value when this call wrote it (the caller announces it),
    or None when there was nothing to declare: a manifest already exists (the
    vault has spoken, never overrule it), or the content reads as prose and the
    in-place default already fits, in which case the vault stays file-free.

    Deliberately creates no directory: an empty `docs/silica` would then read as
    a pre-`write_dir` vault to every back-compat lookup.
    """
    root = Path(vault)
    manifest = root / MANIFEST_REL
    if not root.is_dir() or manifest.exists():
        return None
    declared = write_dir_for(root)
    if not declared:
        return None
    try:
        manifest.write_text(f"write_dir: {declared}\n", encoding="utf-8")
    except OSError as exc:
        # A read-only or unwritable vault root is the user's business, not a
        # crash: fall through to the in-place default and say nothing new.
        logger.warning("could not write %s (%s) — write_dir stays the default", manifest, exc)
        return None
    return declared
