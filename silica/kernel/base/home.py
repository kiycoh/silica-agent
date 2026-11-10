# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Where Silica keeps its own state: the runtime home and the per-vault namespaces under it."""
from __future__ import annotations

import hashlib
from pathlib import Path

from silica.config import CONFIG, silica_home

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

        return CONFIG.vault_path or None
    except Exception:
        return None


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


# The per-vault index files, each named once. The stores, the readers of an
# explicit vault (vault_registry, the memory lane) and the presence checks each
# spelled the names themselves, so a renamed file would have left some of them
# reading nothing, and nothing would have said so (untangle re-audit, 2026-11-10).
EMBEDDINGS = "embeddings"
COOCCURRENCE = "cooccurrence"
LEXICAL = "lexical"
VAULT_BRIEF = "vault_brief"
# The index marker: names the vault a one-way digest dir belongs to.
VAULT_MARKER = "vault"


def index_file(name: str, vault: str | None = None) -> Path:
    """Resolve one derived-index file (``<name>.json``) under the index dir of
    `vault`, or of the current vault when None. The per-store ``_index_path``
    shims delegate here (kept as module functions so tests can still
    monkeypatch them per store). An explicit ``""`` is the legacy unkeyed
    namespace, as in `index_dir_for`."""
    return (index_dir() if vault is None else index_dir_for(vault)) / f"{name}.json"


def lexical_built(vault: str | None = None) -> bool:
    """Whether `vault` (None: the current one) has a lexical index on disk.

    The lexical leg is maintained only where it exists: the sweep, the write
    hook and the explicit-vault reader never create one, and the first
    whole-vault /embed is what seeds it (recall.refresh)."""
    return index_file(LEXICAL, vault).is_file()


def energy_file(vault: str) -> Path:
    """`<vault>/.silica/energy.json`: the last E(vault) the report wrote, which
    /status reads, with its trend (`energy.jsonl`) beside it."""
    return Path(vault) / ".silica" / "energy.json"


def runs_dir() -> Path:
    """`~/.silica/runs/`, one folder per pipeline run, machine-wide (each run's
    ledger names its vault). Here and not only in kernel.progress, which writes
    the runs, because the co-write report reads them and must not import
    progress: progress reaches model.llm through recall.episodic."""
    return silica_home() / "runs"


# The two files of a run that the co-write report reads beside progress.
RUN_LEDGER = "ledger.json"
RUN_MANIFEST = "manifest.json"


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
    return index_dir_for(CONFIG.vault_path or "")
