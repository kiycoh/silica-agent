# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One note's vectors and co-occurrence, kept fresh after a person's save."""
from __future__ import annotations

import logging
import threading

from silica.config import CONFIG

logger = logging.getLogger(__name__)


EMBED_LOCK = threading.Lock()


def index_note(key: str) -> None:
    """The note's vectors and co-occurrence, as the write path keeps an agent's
    note. `EMBED_LOCK` because /embed and an import write the same store. Each
    leg fails alone and is left to /embed: the text is safe on disk, and a stale
    vector costs a search, never a keystroke."""
    from silica.model.providers import get_embedder_or_none
    from silica.driver import DRIVER
    from silica.kernel.recall.cooccurrence import build_index
    from silica.kernel.recall.embed import refresh_note

    embedder = get_embedder_or_none(CONFIG, "note index", level="debug")
    if embedder is None:
        return
    name = key.rsplit("/", 1)[-1]
    try:
        body = DRIVER.read_note(f"{key}.md").content or ""
    except Exception as exc:
        # Moved or deleted since it was saved: the next /embed sees the vault as it is.
        logger.debug("note index: %s not read: %s", key, type(exc).__name__)
        return
    with EMBED_LOCK:
        try:
            refresh_note(embedder, key, name, body)
        except Exception as exc:
            logger.warning("note index: embedding failed: %s", type(exc).__name__)
        try:
            # force without refreeze: this note's old share out, and the store's
            # frozen language left alone (states.write._refresh_cooccurrence_for_ops).
            build_index([(key, name, body)], lang=CONFIG.cooccurrence_lang, force=True)
        except Exception as exc:
            logger.warning("note index: co-occurrence failed: %s", type(exc).__name__)
