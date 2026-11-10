# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The page vectors of the vault's documents, kept current by the app
(ADR-0156, amended 2026-11-08): a build when the GUI starts, then one after
every write that adds or changes a document (a note saved, an import, a
conversion, a vault switch), on a laptop whose embedder is this machine's.
Before this nothing built them: a library's search and Explore's similar
edges waited for someone to run a script.

silica-core reads only the documents whose mtime moved and saves as it goes
(0.13.0), so a refresh that finds nothing costs a walk, and a first build of
hours that the app's closing cuts resumes where it stopped. One build at a
time: a request during one runs one more after it, which finds what the
request was about."""
from __future__ import annotations

import logging
import threading
import time

from silica.config import CONFIG, hosted
from silica.kernel.recall import engine as recall_engine

logger = logging.getLogger(__name__)

# What the launcher itself waits for an embedder it starts
# (SILICA_SERVE_TIMEOUT, onboarding/serve.py); one still loading is polled.
_WAIT_S = 180.0
_POLL_S = 2.0

# The status rides the narration stream as a `vectors` event (server.py),
# at most every half second within one state: reading the documents of a
# vault of notes is thousands of steps a second, embedding them one a while.
TOPIC = "vectors/status"
_PUBLISH_S = 0.5

_LOCK = threading.Lock()
_AGAIN = threading.Event()
_THREAD: threading.Thread | None = None
_STATUS: dict = {"state": "idle"}
_PUBLISHED = 0.0


class _Switched(Exception):
    """The vault changed under the build: it stops, and the next build is the new vault's."""


def status() -> dict:
    """{state: idle | waiting | index | vectors | failed, done, total, vault}:
    `waiting` for the embedder, `index` reading the documents, `vectors`
    embedding them."""
    return dict(_STATUS)


def _eligible() -> bool:
    """A laptop (ADR-0085: a tenant's vectors are the host's) and an
    embedder on this machine: a remote one sends every page off it, paid,
    so that stays the person's `silica-core index --embed`."""
    if hosted():
        return False
    from silica_core import embeddings

    recall_engine.engine()  # raises on a silica-core this tree cannot drive, before a thread starts
    return embeddings.enabled() and embeddings.remote_host() is None


def refresh() -> bool:
    """A build now, or one more after the running one. False when this
    process builds no vectors (`_eligible`)."""
    global _THREAD
    try:
        if not _eligible():
            return False
    except Exception:
        # Tolerated: a silica-core engine() refuses costs the vectors only.
        # The callers are a note's own index and a finished job's follow-up,
        # which must not fail for it.
        logger.exception("page vectors: not started")
        return False
    with _LOCK:
        if _THREAD is not None:
            _AGAIN.set()
            return True
        _THREAD = threading.Thread(target=_run, name="silica-vectors", daemon=True)
        _THREAD.start()
    return True


def _run() -> None:
    global _THREAD
    while True:
        _AGAIN.clear()
        _build_once()
        # Decided under the lock refresh() takes: a request landing as this
        # build ends either sees the thread and sets _AGAIN before this
        # check, or finds no thread and starts one.
        with _LOCK:
            if not _AGAIN.is_set():
                _THREAD = None
                return


def _embedder_up() -> bool:
    from silica.onboarding.serve import ready
    from silica_core.config import CONFIG as CORE

    base = CORE.embedding_base_url
    if not base:  # a model2vec model in this process: nothing to wait for
        return True
    deadline = time.monotonic() + _WAIT_S
    while not ready(base):
        if time.monotonic() > deadline:
            return False
        time.sleep(_POLL_S)
    return True


def _set(**status) -> None:
    """One assignment, so `status()` never reads a dict half rewritten; a
    new state is always published, a step within one at most every `_PUBLISH_S`."""
    global _STATUS, _PUBLISHED
    moved = status.get("state") != _STATUS.get("state")
    _STATUS = status
    now = time.monotonic()
    if moved or now - _PUBLISHED >= _PUBLISH_S:
        _PUBLISHED = now
        from silica.obs.bus import BUS

        BUS.publish(TOPIC, dict(status))


def _build_once() -> None:
    vault = CONFIG.vault_path
    if not vault:
        return
    _set(state="waiting", vault=vault)
    if not _embedder_up():
        logger.warning("page vectors: the embedder did not answer within %.0f s", _WAIT_S)
        _set(state="failed", vault=vault, error="the embedder did not answer")
        return

    def progress(stage: str, done: int, total: int) -> None:
        if CONFIG.vault_path != vault:
            raise _Switched
        _set(state=stage, vault=vault, done=done, total=total)

    started = time.monotonic()
    try:
        out = recall_engine.engine().build_index(embed=True, progress=progress)
    except _Switched:
        _AGAIN.set()
        return
    except Exception:
        # Tolerated: the search stays lexical and the next write or start
        # builds again from silica-core's last save.
        logger.exception("page vectors: the build failed")
        _set(state="failed", vault=vault, error="the build failed")
        return
    if CONFIG.vault_path != vault:  # stopped while embedding: core answers that as an error
        _AGAIN.set()
        return
    if "error" in out:
        hint = out["error"].get("hint", "")
        logger.warning("page vectors: %s", hint)
        _set(state="failed", vault=vault, error=hint)
        return
    logger.info("page vectors: %s in %.0f s", out.get("embeddings"), time.monotonic() - started)
    _set(state="idle", vault=vault, sections=(out.get("embeddings") or {}).get("sections", 0))
