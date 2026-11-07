# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Safe mode: the mirror copy of a vault note, seeded before it is enriched."""
from __future__ import annotations

import logging

from silica.kernel.base.manifest import active_write_dir, within

logger = logging.getLogger(__name__)


def seed_mirror_copy(path: str) -> None:
    """Safe mode: copy the vault original into the mirror before it is enriched.

    Every seam that ENRICHES an existing note (patch, hub MOC, parent MOC) works
    on `silica/<same path>`; the mirror is a preview of the vault after the
    paste, so that copy has to start as the note itself or pasting it would drop
    everything the note already said. No-op outside safe mode, when the copy
    already exists, or when there is no original — the caller's own read then
    reports the miss exactly as before.

    Goes through the DRIVER, not the filesystem: with the Obsidian bridge
    attached, reads are answered by Obsidian's own vault index, and a copy
    written straight to disk came back "file not found" to the very seam that
    had just asked for it.

    Best-effort: a copy that cannot be made leaves the read to report it.
    """
    from silica.driver import DRIVER
    from silica.onboarding.adopt import SAFE_WRITE_DIR

    if active_write_dir() != SAFE_WRITE_DIR or not within(path, SAFE_WRITE_DIR):
        return
    rel = path.replace("\\", "/").strip("/")[len(SAFE_WRITE_DIR) + 1:]
    if not rel:
        return
    try:
        DRIVER.read_note(path)
        return  # the copy is already there
    except Exception:
        pass  # not exists(): a dead bridge must end at the read below, quietly, and not raise here
    try:
        original = DRIVER.read_note(rel).content
    except Exception:
        return  # nothing to mirror — the caller's own read reports the miss
    try:
        DRIVER.create(path, original)
        logger.debug("mirror: seeded '%s' from '%s'", path, rel)
    except Exception as e:
        logger.warning("mirror: could not seed '%s': %s", path, e)
