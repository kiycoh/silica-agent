# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Content-addressed cache for distiller replies, keyed by two fingerprints.

The namespace is the prompt text and the entry is the call input, because the
two change for different reasons and have to invalidate different things. Edit
the lens and every reply it produced goes unreachable in one step, while an arm
still running the old lens keeps reading its own entries; change one source and
only that entry misses. A single key buys either mixed vintages inside one
corpus or a full re-extraction on every unrelated release.

That split is what an honest prompt A/B needs. Re-running an arm today
re-distills the whole corpus, so every note the prompt never touched is
re-rolled too and the two corpora differ by far more than the change under
test. With the namespace, an unchanged input under an unchanged prompt is
replayed rather than re-rolled, and the measured delta is the prompt.

Off unless SILICA_DISTILL_CACHE=1: a hit replays a stored reply verbatim,
which is what a frozen arm needs and not what a live vault wants.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FP_LEN = 12


def enabled() -> bool:
    return os.getenv("SILICA_DISTILL_CACHE", "0") == "1"


def cache_root() -> Path:
    """Cache home. A function and not a constant so a test can point it away
    from the developer's real ~/.silica."""
    return Path.home() / ".silica" / "cache" / "distill"


def prompt_fingerprint(prompt: str) -> str:
    """Name the lens. Short by design: the directory is meant to be read by a
    human comparing two arms, and 12 hex chars do not collide over the handful
    of prompts one machine ever holds."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:_FP_LEN]


def entry_key(inputs: object) -> str:
    """Fingerprint the call inputs. Canonical JSON, because dict order is an
    artifact of how the caller assembled the object and never of what was
    asked."""
    blob = json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def entry_path(namespace: str, key: str) -> Path:
    return cache_root() / f"p{namespace}" / f"{key}.json"


def load(namespace: str, key: str) -> dict | None:
    """Return a stored reply, or None for anything that is not one."""
    path = entry_path(namespace, key)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        # A half-written entry costs one re-run. It must never cost a batch.
        logger.warning("distill cache: unreadable entry %s (%s) — counting a miss",
                       path, e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("distill cache: entry %s is not a reply object — counting a miss",
                       path)
        return None
    return parsed


def store(namespace: str, key: str, reply: dict) -> None:
    """Record a reply. Callers store successes only: an error entry would
    freeze one transient provider fault into every later run of that arm."""
    from silica.kernel.recall.paths import atomic_write_bytes

    path = entry_path(namespace, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, json.dumps(reply, ensure_ascii=False).encode("utf-8"))
    except OSError as e:
        # The cache is an optimisation; losing an entry must not fail the run.
        logger.warning("distill cache: could not store %s (%s)", path, e)
