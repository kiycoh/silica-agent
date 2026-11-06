# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Where Silica keeps its own state: the runtime home and the per-vault namespaces under it."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from silica.config import CONFIG, silica_home

logger = logging.getLogger(__name__)


    # ponytail: no directory fsync — post-power-loss rename durability is
    # filesystem-dependent, and every caller's file is rebuildable or
    # re-produceable; upgrade if a real loss is ever traced here.


def resolve_vault_path(vault_path: str | None = None) -> str | None:
    """An explicit vault path, else the live CONFIG's, else None.

    The `vault_path=None means "the active vault"` convention shared by the
    kernel's on-disk ledgers (provenance, run_log). CONFIG is read lazily and
    defensively: a caller running before config import must get None, not an
    ImportError.
    """
    if vault_path:
        return vault_path
    try:
        from silica.config import CONFIG

        return getattr(CONFIG, "vault_path", None) or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Silica runtime directory helpers
# ---------------------------------------------------------------------------



def silica_tmp_dir() -> Path:
    """Return the pipeline staging directory (~/.silica/tmp/), creating it if needed.

    All FSM temporary files (ops JSON, payload chunks, distiller output) live
    here instead of the system temp directory so they survive the pipeline run
    and are inspectable for debugging.  The FSM removes them on successful
    completion via _cleanup_tmp().
    """
    d = silica_home() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_file(name: str) -> Path:
    """Resolve one derived-index file (``<name>.json``) under the current vault's
    index dir. The per-store ``_index_path`` shims delegate here (kept as module
    functions so tests can still monkeypatch them per store)."""
    return index_dir() / f"{name}.json"


def attachments_dir() -> Path:
    """Images attached to a chat turn (ADR-0077), kept by content outside every
    vault: saving one into a vault stays an explicit nucleate.

    ponytail: never pruned, not even with its session; a chat image is a few
    hundred KB, so reopen when the folder passes 1 GB.
    """
    return silica_home() / "attachments"


def index_dir_for(vault: str) -> Path:
    """Per-vault index namespace for an explicit `vault` path, independent of
    the global CONFIG singleton. Same digest scheme as `index_dir()` — the
    two agree whenever `vault == CONFIG.vault_path`.

    Callers that need to resolve a *specific* vault's on-disk index (e.g. a
    diagnostic comparing a passed-in config's vault against whatever vault
    the live global CONFIG currently points at) MUST use this rather than
    `index_dir()`, which only ever resolves the global singleton and would
    silently compare the wrong vault's state.
    """
    base = silica_home() / "index"
    vault = (vault or "").strip()
    if not vault:
        return base
    return base / vault_digest(vault)


def vault_digest(vault: str) -> str:
    """The per-vault namespace key: sha1 of the resolved vault path, 12 chars.

    One scheme shared by every per-vault runtime directory (index, capture
    WAL), so a namespace computed by one seam is readable by the others.
    """
    return hashlib.sha1(str(Path(vault).resolve()).encode("utf-8")).hexdigest()[:12]


def inbox_dir_for(vault: str) -> Path:
    """Per-vault capture WAL: ~/.silica/inbox/<digest12>/.

    Out of the vault on purpose: raw transcripts are private conversation data
    and must never sit inside a committable repo, out of the note scanner, and
    out of Obsidian's index.
    """
    return silica_home() / "inbox" / vault_digest(vault)


def index_dir() -> Path:
    """Per-vault index namespace: ~/.silica/index/<digest12>/ keyed by the
    resolved vault path; legacy global ~/.silica/index/ when no vault is
    configured. Per-vault state follows the vault (ADR-0014), so /vault
    switch no longer serves another vault's entries."""
    return index_dir_for(getattr(CONFIG, "vault_path", "") or "")
