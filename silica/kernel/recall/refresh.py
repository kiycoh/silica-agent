# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Build or refresh the three indexes a vault's notes feed.

`silica.tools.graph` keeps the tools of these names, which silica-hosted calls;
what they do lives here because it is the index lane's, and the three read loops
that had been pasted beside them had to stay equal to the write hook's.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from silica.driver import DRIVER

logger = logging.getLogger(__name__)


def _read_all(folder: str) -> tuple[list, list[tuple[str, str, str]], list[str], dict | None]:
    """(the listing, (index key, name, body) per note, read errors, refusal).

    One loop for the three indexes, and the body stays RAW for all of them, as
    the sweep and the write hook pass it: `note_text` alone decides what a figure
    becomes, build_contribution strips frontmatter and media itself, and a
    bulk-seeded index has to match an incrementally maintained one, or the two
    paths sign the same note differently and redo each other's work.
    """
    try:
        all_refs = DRIVER.list_files(folder or None)
    except Exception as e:
        return [], [], [], {"error": f"Failed to list vault files: {e}"}
    notes: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for ref in all_refs:
        path = ref.path or ref.name
        name = ref.name or path
        try:
            body = DRIVER.read_note(path).content or ""
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            continue
        notes.append((path.removesuffix(".md"), name, body))
    if not notes:
        return all_refs, notes, errors, {"error": "No notes found to index", "read_errors": errors}
    return all_refs, notes, errors, None


def embed(folder: str = "", force: bool = False, *, ask: Callable[..., Any]) -> dict[str, Any]:
    """`ask` is the chat model's look at a figure (agent.vision.ask_chat_model):
    only this bulk pass pays the model for a reading, and the agent sits above."""
    from silica.config import CONFIG
    from silica.kernel.recall.embed import build_index
    from silica.model.providers import get_embedder

    _refs, notes, errors, refused = _read_all(folder)
    if refused:
        return refused

    # Images first: a reading changes the text a note embeds, and only this
    # bulk pass pays the model for it (image_readings module docstring).
    from silica.kernel.recall import image_readings
    try:
        images_read = image_readings.read_missing(((p, b) for p, _n, b in notes), ask=ask)
    except Exception as e:
        # The index must not wait on the reader: a vault whose images cannot be
        # read embeds as it did before ADR-0077, and the next /embed retries.
        logger.warning("image reading failed, embedding without new readings: %s", e)
        images_read = 0

    try:
        embedder = get_embedder(CONFIG)
        # prune drops every indexed path missing from `notes`, so `notes` may
        # only be trusted as the live set when EVERY listed note was read. A
        # transient read failure (non-UTF-8 byte, permission blip, Obsidian
        # mid-write) would otherwise evict a note that is still on disk, and no
        # later incremental refresh re-adds what it never read.
        store = build_index(embedder, notes, force=force, prune=not errors, folder=folder)
    except Exception as e:
        return {"error": f"Index build failed: {e}", "read_errors": errors}

    out = {
        "indexed": len(store),
        "total_notes": len(notes),
        "read_errors": errors,
        "index_path": str(store._path),
        "images_read": images_read,
    }
    # First whole-vault build also seeds the lexical index: the sweep maintains
    # stores that exist and never builds one, and until 2026-09-01 no vault on
    # the field had a lexical.json because it was a separate opt-in, so the
    # cheap stage of silica_vaults (1.4 MB for 872 notes) never existed where
    # the 18 MB embed store did. Whole vault only: a folder-scoped first build
    # would leave a partial index the sweep then keeps partial.
    from silica.kernel.base import home
    # New readings re-sign the notes that embed them, so the embed pass above
    # caught them; the lexical store keys on nothing it could compare, so it is
    # rebuilt whole (seconds, 1.4 MB on 872 notes) rather than left without them.
    lexical_exists = home.lexical_built()
    if (not folder and not lexical_exists) or (images_read and lexical_exists):
        out["lexical"] = lexical().get("indexed")
    return out


def cooccurrence(folder: str = "", force: bool = False) -> dict[str, Any]:
    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import _index_path, build_index, get_cooccur_store

    _refs, notes, errors, refused = _read_all(folder)
    if refused:
        return refused

    store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
    try:
        # prune=not errors: `notes` is the live set only when every listed note
        # was actually read — see embed(). A note skipped by a
        # transient read failure must not be un-edged and deleted.
        # refreeze rides on force: `/cooccur --force` is the deliberate rebuild
        # (and the doctor remedy for a wrong-frozen store language) — it
        # re-processes every note, so re-detecting store.lang here is safe.
        # A plain incremental /cooccur skips already-indexed notes and must NOT
        # refreeze: flipping the language without re-stemming existing
        # contributions would mix stemmers across node keys.
        # prune: drop nodes for notes deleted out-of-band. save=False: one
        # flush at the end after the prune.
        build_index(
            notes, store=store, lang=CONFIG.cooccurrence_lang,
            force=force, refreeze=force, save=False, prune=not errors, folder=folder,
        )
    except Exception as e:
        return {"error": f"Index build failed: {e}", "read_errors": errors}

    # A refresh that indexed nothing must not rewrite the index. Every graph
    # export calls this, the file is ~9.5 MB on a 700-note vault, and an
    # unchanged rewrite still moves the mtime - which is what vault_version()
    # digests, so the GUI's "vault changed" chip fired after every build of the
    # graph it had just drawn. force still writes, and so does an absent file:
    # skipping there would leave the vault with no index and nothing to say so.
    if force or store.is_dirty() or not _index_path().exists():
        store.save()

    return {
        "indexed": len(store),
        "total_notes": len(notes),
        "read_errors": errors,
        "index_path": str(store._path),
    }


def lexical(folder: str = "", force: bool = False) -> dict[str, Any]:
    from silica.kernel.base.layout import in_folder
    from silica.kernel.recall.lexical import get_lexical_store

    all_refs, notes, errors, refused = _read_all(folder)
    if refused:
        return refused

    store = get_lexical_store()
    if force:
        for p in list(store.paths()):
            if in_folder(p, folder):
                store.remove(p)
    for idx_path, name, body in notes:
        store.upsert(idx_path, name, body)

    # GC: drop indexed notes no longer present in the (folder-scoped) vault.
    # Scoped to the full LISTING, not the notes read this run: a note whose read
    # failed transiently is still on disk, and evicting it here would silently
    # cost recall forever (no later incremental refresh re-adds what it skipped).
    current_paths = {(ref.path or ref.name).removesuffix(".md") for ref in all_refs}
    stale_paths = [
        p for p in store.paths()
        if in_folder(p, folder) and p not in current_paths
    ]
    for p in stale_paths:
        store.remove(p)
    store.save()

    return {
        "indexed": len(store),
        "total_notes": len(notes),
        "read_errors": errors,
        "index_path": str(store._path),
    }


def flush(*, embed: bool, cooccur: bool, lexical: bool) -> None:
    """Persist a run's deferred upserts, once (Fix A).

    The write path upserts into the shared in-memory singletons with save=False
    and marks the index dirty; this single flush rewrites each dirty index file
    once instead of per note (1.17s/note at 10k). Gated on the dirty flags so a
    run that wrote nothing (or had the embedder down) never rewrites the index.
    Runs in the _run_loop finally so it fires on success, error, and Ctrl+C; a
    hard kill is repaired by the index sweep.
    """
    if embed:
        try:
            from silica.kernel.recall.embed import get_store
            get_store().save()
        except Exception as e:
            logger.debug("flush: embed index save skipped (%s)", e)
    if cooccur:
        try:
            from silica.kernel.recall.cooccurrence import get_cooccur_store
            get_cooccur_store().save()
        except Exception as e:
            logger.debug("flush: cooccur index save skipped (%s)", e)
    if lexical:
        try:
            from silica.kernel.recall.lexical import get_lexical_store
            get_lexical_store().save()
        except Exception as e:
            logger.debug("flush: lexical index save skipped (%s)", e)


# --- the first build, when the app opens a vault (ADR-0173) ---------------------

# The stores the running first build has still to make. A set and not a flag:
# a search during it asks about one store, and "embeddings" is pending only
# when an embedder came with the build.
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()
# Notes per embedding call, as build_index batches them; the rail steps by it.
_EMBED_CHUNK = 32


def building(store: str | None = None) -> bool:
    """A first build of this vault's stores runs in this process (`store`: that
    one is still to come). A search that finds no store meanwhile is waiting on
    it, which recall says as `building` rather than as `missing`."""
    with _PENDING_LOCK:
        return bool(_PENDING) if store is None else store in _PENDING


def missing(*, embed: bool) -> list[str]:
    """The stores recall reads that this vault lacks, in the order they are
    built: co-occurrence and lexical, then embeddings when `embed`. Empty
    counts as missing, because the sweep maintains only a seeded store."""
    from silica.config import CONFIG
    from silica.kernel.base import home
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.recall.embed import get_store

    out = []
    if not get_cooccur_store(lang=CONFIG.cooccurrence_lang).paths():
        out.append("cooccurrence")
    if not home.lexical_built():
        out.append("lexical")
    if embed and not len(get_store()):
        out.append("embeddings")
    return out


def build_missing(*, embedder: Any = None,
                  progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    """Build the stores this vault lacks (`missing`), as /cooccur, /lexical and
    /embed would: the first two always, since neither calls a model; embeddings
    only with `embedder`. Returns what each build reported, {} when nothing was
    missing or the vault holds no note.

    The embeddings go through embed.build_index and never through `embed`
    above, whose figure readings pay the chat model per image: a build the
    person did not ask for must not spend their key. `progress(stage, done,
    total)` takes the page vectors' stages ("index", then "vectors") so the
    rail draws it with the labels it has, and an exception it raises (a vault
    switched under the build) stops the build there.
    """
    todo = missing(embed=embedder is not None)
    if not todo:
        return {}
    step = progress or (lambda stage, done, total: None)
    with _PENDING_LOCK:
        _PENDING.update(todo)
    done: dict[str, Any] = {}
    try:
        for name, build in (("cooccurrence", cooccurrence), ("lexical", lexical)):
            if name in todo:
                step("index", 0, 0)
                done[name] = build()
                if "error" in done[name]:  # a vault with no note: nothing to build, nothing to retry
                    return {}
                with _PENDING_LOCK:
                    _PENDING.discard(name)
        if "embeddings" in todo:
            done["embeddings"] = _embed_all(embedder, step)
    finally:
        with _PENDING_LOCK:
            _PENDING.clear()
    return done


def _embed_all(embedder: Any, step: Callable[[str, int, int], None]) -> dict[str, Any]:
    from silica.kernel.recall.embed import build_index, get_store
    from silica.kernel.recall.note_index import EMBED_LOCK

    store = get_store()  # bound to this vault's file before the read: a switch cannot redirect it
    _refs, notes, errors, refused = _read_all("")
    if refused:
        return refused
    step("vectors", 0, len(notes))
    for i in range(0, len(notes), _EMBED_CHUNK):
        # Per chunk: a person's save (note_index) and an import embed into the
        # same store, and neither should wait for a whole vault.
        with EMBED_LOCK:
            build_index(embedder, notes[i:i + _EMBED_CHUNK], store=store, save=False)
        step("vectors", min(i + _EMBED_CHUNK, len(notes)), len(notes))
    store.save()
    return {"indexed": len(store), "total_notes": len(notes), "read_errors": errors}
