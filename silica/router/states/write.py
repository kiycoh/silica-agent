# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Injector write states: SNAPSHOT, WRITE, HUB_UPDATE (+ MOC helpers).

Handler bodies for InjectorFSM: each function takes the FSM instance and
mutates its state.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, TYPE_CHECKING

from silica.config import CONFIG
from silica.driver import DRIVER
from silica.kernel.write.gate import build_txn
from silica.kernel.write.ops_io import load_ops
from silica.router.phases import phase

if TYPE_CHECKING:
    from silica.router.orchestrator import InjectorFSM

logger = logging.getLogger(__name__)


from silica.kernel.write.ops import OpType

# Extracted to kernel/moc.py so the deferred-retry path (tools/pipeline.py) can
# reuse them; aliased here to keep the FSM code and existing tests unchanged.
from silica.kernel.write.moc import (
    hub_desc as _hub_desc,
    moc_target as _moc_target,
    merge_moc_section as _merge_moc_section,
    moc_heading as _moc_heading,
    moc_line as _moc_line,
)


def _refresh_cooccurrence_for_ops(
    ops: list,
    committed_paths: set,
    *,
    read_body: Callable[[str], str],
    lang: str = "english",
    store: Any | None = None,
    save: bool = True,
) -> int:
    """Refresh the embedder-free co-occurrence index for committed write/patch ops.

    The freshness twin of the embedding refresh, but the STABLE leg: it imports
    only the cooccurrence module (never the embedder/provider stack), so the
    index stays fresh even when LM Studio is down. Uses build_index(force=True)
    so a note's prior contribution is replaced, never inflated, with a single
    save — replacement semantics only: it deliberately does NOT pass
    refreeze=True, so the store's frozen stemming language is never re-detected
    from a write batch (re-detection is reserved for /cooccur --force).
    Note edges need no step here: they are derived from the contributions on
    read (CooccurStore.note_adjacency, ADR-0029). Best-effort: a per-note read
    failure is skipped and the whole call never raises. Returns the number of
    notes refreshed.
    """
    from silica.kernel.recall.cooccurrence import build_index as _cooccur_build

    notes: list[tuple[str, str, str]] = []
    concepts_by_path: dict[str, list[str]] = {}
    seen: set[str] = set()
    for op in ops:
        path = op.touched_ref()
        if op.op not in (OpType.write, OpType.patch) or not path:
            continue
        if path not in committed_paths or path in seen:
            continue
        seen.add(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        idx_path = path.removesuffix(".md")
        try:
            body = read_body(path) or ""
        except Exception:
            continue
        notes.append((idx_path, stem, body))
        # #9: forward LLM-extracted concept phrases to reinforce this note.
        op_concepts = getattr(op, "concepts", None)
        if op_concepts:
            concepts_by_path[idx_path] = op_concepts

    if not notes:
        return 0
    try:
        # save=False defers the single save to the end-of-run flush, which
        # rewrites the same singleton.
        built = _cooccur_build(notes, store=store, lang=lang, force=True,
                               concepts_by_path=concepts_by_path or None, save=False)
        if save:
            built.save()
    except Exception as exc:
        logger.debug("WRITE: cooccur refresh skipped (%s)", exc)
        return 0
    return len(notes)


def _refresh_embeddings_for_ops(
    ops: list,
    committed_paths: set,
    *,
    read_body: Callable[[str], str],
    embedder: Any,
    store: Any,
    patches: bool = False,
) -> int:
    """Embed the notes this chunk wrote, once each, so the next chunk's dedup
    sees them. Patches are left out: the note already has a vector the dedup
    can read, and the sweep that ends the run (Coordinator.run) embeds its new
    text once, after the passes that edit notes past WRITE. Hosted t01 on
    2026-10-23 spent 47% of its embedder time here, 49 of 218 refreshes on
    patches, at ~4 s each on a 2-core host. `patches` puts them back where no
    sweep runs (a driver with no mtime_of, the index sweep turned off).
    Returns the notes embedded.
    """
    from silica.kernel.recall.embed import refresh_note

    kinds = (OpType.write, OpType.patch) if patches else (OpType.write,)
    seen: set[str] = set()
    for op in ops:
        path = op.touched_ref()
        if op.op not in kinds or not path or path not in committed_paths or path in seen:
            continue
        seen.add(path)
        try:
            body = read_body(path) or ""
            # Fix A: defer the whole-index write to a single end-of-run flush
            # (_run_loop); upsert into the shared in-memory store only.
            refresh_note(embedder, path.removesuffix(".md"), os.path.splitext(os.path.basename(path))[0],
                         body, store=store, save=False)
        except Exception as exc:
            # One note missing from the index is caught by the run-end sweep;
            # the chunk's other notes still go in.
            seen.discard(path)
            logger.debug("WRITE: embed refresh failed for '%s': %s", path, exc)
    return len(seen)


def _commit_docs_for_ops(
    ops: list,
    committed_paths: set,
    *,
    vault: str,
    git_commit: str,
) -> str | None:
    """Commit touched vault paths for write/patch ops to git.

    Git safety net behind SILICA_GIT_COMMIT=auto: an additive snapshot on top
    of the undo journal (ADR-0002), never a replacement. Best-effort: no git
    binary, vault outside a repo, nothing staged, or any subprocess failure all
    yield None and never raise. Commits ONLY vault paths — the out-of-vault
    guard lives inside gitstate.commit_docs, so a bug cannot commit source files.
    """
    from silica.kernel.base import gitstate
    from pathlib import Path as _Path

    if git_commit != "auto" or not vault:
        return None

    seen: set[str] = set()
    abs_paths: list[_Path] = []
    for op in ops:
        path = op.touched_ref()
        if op.op not in (OpType.write, OpType.patch) or not path:
            continue
        if path not in committed_paths or path in seen:
            continue
        seen.add(path)
        abs_paths.append(_Path(vault) / path)

    if not abs_paths:
        return None

    try:
        from silica.kernel.base.roots import repo_root_for

        root = repo_root_for(vault)
        if root is None:
            return None
        n = len(abs_paths)
        return gitstate.commit_docs(root, vault, abs_paths, f"silica: write {n} note(s)")
    except Exception as _ge:
        logger.debug("WRITE: git auto-commit skipped (%s)", _ge)
        return None


def handle_snapshot(fsm: "InjectorFSM") -> None:
    with phase(fsm, fsm._chunk_task_id("snapshot"), "snapshot"):
        from silica.driver.base import NoteRef

        try:
            ops = load_ops(fsm._chunk_ctx["ops_path"])
            fsm._txn = build_txn(ops)
        except Exception as e:
            raise RuntimeError(f"SNAPSHOT failed: {e}")
        fsm._chunk_ctx["txn_id"] = fsm._txn.id
        fsm._chunk_ctx["snapshot"] = {"txn_id": fsm._txn.id, "created_paths": fsm._txn.created_paths,
                                      "inverses": fsm._txn.inverses_serialized}

        # S3.2: Take pre-write graph snapshot incrementally
        try:
            from silica.kernel.base.markdown import extract_links as _extract_links
            touched_refs = []
            snapshot_domain = set()

            for op in ops:
                path = op.touched_ref()
                if path:
                    name = os.path.splitext(os.path.basename(path))[0]
                    ref = NoteRef(name=name, path=path)
                    touched_refs.append(ref)
                    snapshot_domain.add(ref)

                    if op.op in (OpType.patch, OpType.overwrite, OpType.delete):
                        # Capture current outgoing targets so we can detect orphaning.
                        try:
                            for target_ref in DRIVER.links(ref):
                                snapshot_domain.add(target_ref)
                        except Exception as ex:
                            logger.warning("Failed to fetch pre-write links for %s: %s", path, ex)

                    elif op.op == OpType.write:
                        # A write op creates a new note that didn't exist at pre-snapshot
                        # time.  After the write, graph_snapshot expands its neighborhood
                        # to include every vault note the new note links to.  If those
                        # linked notes carry pre-existing unresolved links, they appear as
                        # new_unres in graph_diff Rule 2 — a false positive.
                        # Fix: add those link targets to the pre-snapshot domain now so
                        # their existing ghost links cancel out in the diff.
                        content = op.snippet or op.content or ""
                        for link_target in _extract_links(content):
                            target_stem = link_target.removesuffix(".md")
                            target_key = target_stem.lower()
                            try:
                                if "/" in target_stem:
                                    target_name = os.path.splitext(os.path.basename(target_stem))[0]
                                    snapshot_domain.add(NoteRef(name=target_name, path=target_stem + ".md"))
                                else:
                                    for match in DRIVER.search_names(target_stem):
                                        if match.name.lower() == target_key:
                                            snapshot_domain.add(match)
                            except Exception as ex:
                                logger.debug("Snapshot domain expansion: could not resolve '%s': %s", link_target, ex)

            snapshot_domain_list = list(snapshot_domain)
            fsm._chunk_ctx["snapshot_domain"] = [{"name": r.name, "path": r.path} for r in snapshot_domain_list]
            fsm._pre_graph = DRIVER.graph_snapshot(snapshot_domain_list)
        except Exception as e:
            logger.error("Failed to take pre-write graph snapshot: %s", e)
            raise RuntimeError(f"Pre-write graph snapshot failed: {e}")


def _attach_section_images(fsm: "InjectorFSM", ops: list) -> None:
    """Re-attach source images to the notes distilled from their section.

    The distiller never saw these embeds (media.strip_images strips them from
    its payload), so the produced notes would otherwise lose every figure. Here
    we read the chunk's raw source file and, for each new/patched/rewritten note, append
    the images from the section whose heading matches the note's concept — the
    same heading→section join payload.py uses to build the excerpt.
    """
    # One source file per chunk (same derivation as HUB_UPDATE below).
    _fi, _ci = fsm._chunk_flat_to_fi_ci.get(fsm._current_chunk_idx, (0, 0))
    source_file = (
        fsm._file_chunks[_fi]["source_file"]
        if _fi in fsm._file_chunks
        else fsm.inbox_file
    )
    if not source_file:
        return
    try:
        source = DRIVER.read_note(source_file).content or ""
    except Exception:
        return
    if "![" not in source:  # fast bail: no embeds at all
        return
    from silica.kernel.text.media import append_section_images
    for op in ops:
        if op.op not in (OpType.write, OpType.patch, OpType.overwrite):
            continue
        path = op.touched_ref()
        if not path:
            continue
        concept = os.path.splitext(os.path.basename(path))[0]
        # An overwrite (collision-enrich) is the whole rewritten note and lives in
        # `content`. It was skipped for fear of stacking a figure the rewrite had
        # kept; append_section_images now skips what the body already carries,
        # and skipping the op cost every enriched note its figures (144 notes of
        # the study vault embed one, 2026-09-09).
        if op.op == OpType.overwrite:
            if op.content:
                op.content = append_section_images(op.content, source, concept)
        else:
            op.snippet = append_section_images(op.snippet or "", source, concept)


def _settle_flagged(fsm: "InjectorFSM", ops: list, committed: set[str]) -> None:
    """Soft-gate verdicts, once the note exists: ledger row + the follow-up.

    After the commit and not at VALIDATE, because the workers run concurrently
    with the FSM: a judge that rules "duplicate" before the note lands has no
    loser to mark, and an expand that fires first writes where the chunk is
    about to write. Only ops that actually landed are settled; a flagged op
    the write reverted is already in the deferred store.
    """
    from silica.router.states.distill import (
        _enqueue_near_title_dedups, _enqueue_short_snippet_expands,
    )
    flagged = [
        {"op": op.model_dump(), "reason": op.review}
        for op in ops
        if op.review and op.touched_ref() in committed
    ]
    if not flagged:
        return
    ledger = getattr(fsm, "warning_ledger", None)
    if ledger is not None:
        for f in flagged:
            ledger.add(f["op"].get("path", ""), "soft_gate", f["reason"])
    _enqueue_near_title_dedups(fsm, flagged, landed=True)
    _enqueue_short_snippet_expands(fsm, flagged)


def handle_write(fsm: "InjectorFSM") -> None:
    from silica.kernel.write.gate import bulk_write_atomic
    fsm._progress_note(fsm._chunk_task_id("write"), "write", "running")

    ops = load_ops(fsm._chunk_ctx["ops_path"])
    try:
        _attach_section_images(fsm, ops)
    except Exception as _ie:
        logger.debug("WRITE: image re-attach skipped (%s)", _ie)

    # Claim stamp: the current file's event clock rides every note this chunk
    # writes. Resolved once per file in run(); an unresolved index leaves
    # valid_from None, which emits no stamp at all.
    try:
        _valid_from = fsm._file_valid_from[fsm._current_file_idx]
        for op in ops:
            if op.op in (OpType.write, OpType.patch):
                op.valid_from = _valid_from
    except (AttributeError, IndexError) as _ve:
        logger.debug("WRITE: claim stamp skipped (%s)", _ve)

    result = bulk_write_atomic(ops, hub=fsm.hub, lint=True)
    _settle_flagged(fsm, ops, committed={r.path for r in result.committed})

    # Per-op lint tolerated these pre-existing violations (patch baseline);
    # hand them to the chunk LINT gate so it applies the same tolerance.
    fsm._chunk_ctx["lint_baseline"] = {
        r.path: r.baseline_errors for r in result.committed if r.baseline_errors
    }

    # Accumulate surviving notes' inverses for the undo journal (recorded at
    # CLEANUP after autolink finalises content → correct version-guard hashes).
    for r in result.committed:
        for inv in r.inverses:
            fsm._run_inverses.append((r.path, inv, None))

    # Brief run-yield for the TUI summary: count NEW notes actually committed
    # (created_paths ∩ committed), accumulated across chunks/files.
    created = {p.removesuffix(".md") for p in (fsm._txn.created_paths or [])} if fsm._txn else set()
    new_notes = sum(1 for r in result.committed if r.path.removesuffix(".md") in created)
    fsm.context["yield_notes"] = fsm.context.get("yield_notes", 0) + new_notes

    if result.failed:
        try:
            # Default None + skip: a failed result whose path matches no op (a
            # normalization drift) must not StopIteration out of the whole
            # deferral/skip-marking block below (A4).
            _deferred = [
                o.model_dump()
                for r in result.failed
                if (o := next((o for o in ops if o.touched_ref() == r.path), None)) is not None
            ]
            _errors = {r.path: (r.error or "lint/write failed") for r in result.failed}
            fsm._defer_ops(_deferred, _errors, phase="WRITE")
            # The reasons used to live only in the deferred bundle: a verbose
            # run showed "2 op(s) failed lint/write" and nothing else.
            for _p, _e in _errors.items():
                logger.debug("WRITE: deferred %s — %s", _p, _e)
            logger.warning(
                "WRITE: %d op(s) failed lint/write — deferred. "
                "Continuing with %d committed op(s).",
                len(result.failed), len(result.committed),
            )
            # Mark failed/deferred ops as skip so subsequent phases (lint, autolink, backlink, cleanup, etc.) skip them
            failed_paths = {r.path for r in result.failed}
            for op in ops:
                if op.touched_ref() in failed_paths:
                    op.op = OpType.skip
                    op.reason = "Deferred because write/lint failed"
            from silica.kernel.write.ops_io import dump_ops
            dump_ops(fsm._chunk_ctx["ops_path"], ops)
        except Exception as _de:
            logger.debug("WRITE: deferred save/update failed (non-fatal): %s", _de)
        fsm.context["has_partial_failure"] = True

    if not result.committed and result.failed:
        fsm._progress_note(fsm._chunk_task_id("write"), "write", "failed",
                            error=f"all {len(result.failed)} ops failed lint/write")

    # Synthesise the legacy `write` result shape that downstream code reads.
    fsm.context["write"] = {
        "success": True,
        "successful": len(result.committed),
        "failed": [{"path": r.path, "error": r.error} for r in result.failed],
        "total": result.total,
    }

    committed_paths = {r.path for r in result.committed}
    _deferred_stems: frozenset[str] = frozenset(
        os.path.splitext(os.path.basename(r.path))[0].lower()
        for r in result.failed if r.path
    )
    fsm._chunk_ctx["deferred_stems"] = list(_deferred_stems)

    # Register committed notes in the RunManifest and refresh embedding index.
    try:
        from silica.kernel.progress import RunManifestEntry
        vault_ctx = fsm.context.get("vault_graph_ctx", {})
        _chunk_source = getattr(fsm, "_current_source_file", "") or ""
        for op in ops:
            path = op.touched_ref()
            if op.op not in (OpType.write, OpType.patch) or not path:
                continue
            if path not in committed_paths:
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            path_key = path.removesuffix(".md")
            cluster_id = vault_ctx.get(path_key, {}).get("cluster_id", -1)
            fsm.manifest.record(RunManifestEntry(
                title=stem,
                path=path_key,
                parent=op.parent,
                cluster_id=cluster_id,
                source_basename=op.source_basename or "",
                op=op.op.value,
                source_file=(_chunk_source if _chunk_source and op.source_basename
                             == os.path.basename(_chunk_source) else ""),
            ))
        fsm.manifest.save()

        # Best-effort incremental embed index refresh
        try:
            from silica.model.providers import get_embedder
            from silica.kernel.recall.embed import get_store
            # The run-end sweep that embeds patches skips a driver with no
            # mtime_of (the ws backend) and an index_sweep turned off.
            swept = (getattr(DRIVER, "mtime_of", None) is not None
                     and getattr(CONFIG, "index_sweep", True))
            if _refresh_embeddings_for_ops(
                ops, committed_paths, read_body=lambda p: DRIVER.read_note(p).content or "",
                embedder=get_embedder(CONFIG), store=get_store(), patches=not swept,
            ):
                # Mark the in-memory index dirty so the end-of-run flush knows
                # to persist (and only then — no flush when nothing changed).
                fsm.context["_embed_dirty"] = True
        except Exception as _ee:
            logger.debug("WRITE: embed refresh skipped (%s)", _ee)

        # Best-effort incremental co-occurrence refresh — the STABLE leg.
        # Separate from the embed block above so it stays fresh even when the
        # embedder is down (no network, pure local compute). Also deferred (Fix A).
        _n_cooccur = _refresh_cooccurrence_for_ops(
            ops,
            committed_paths,
            read_body=lambda p: DRIVER.read_note(p).content or "",
            lang=CONFIG.cooccurrence_lang,
            save=False,
        )
        if _n_cooccur:
            fsm.context["_cooccur_dirty"] = True

        # Best-effort incremental lexical (BM25) refresh — opt-in by index
        # presence, so vaults without a lexical index stay byte-identical.
        try:
            from silica.kernel.base import home
            if (home.index_dir() / "lexical.json").is_file():
                from silica.kernel.recall.lexical import get_lexical_store
                lex = get_lexical_store()
                for op in ops:
                    path = op.touched_ref()
                    if op.op not in (OpType.write, OpType.patch) or not path:
                        continue
                    if path not in committed_paths:
                        continue
                    idx_path = path.removesuffix(".md")
                    stem = os.path.splitext(os.path.basename(path))[0]
                    try:
                        body = DRIVER.read_note(path).content or ""
                        lex.upsert(idx_path, stem, body)
                        # Only mark dirty after a real upsert, so a run that
                        # indexed nothing never triggers a spurious index save
                        # (mirrors the embed/cooccur dirty-flag gating).
                        fsm.context["_lexical_dirty"] = True
                    except Exception as _re:
                        logger.debug("WRITE: lexical refresh failed for '%s': %s", path, _re)
        except Exception as _le:
            logger.debug("WRITE: lexical refresh skipped (%s)", _le)
    except Exception as _me:
        logger.debug("WRITE: manifest update failed (non-fatal): %s", _me)

    # Git safety net (SILICA_GIT_COMMIT=auto): snapshot the write batch.
    try:
        _commit_docs_for_ops(
            ops, committed_paths,
            vault=CONFIG.vault_path, git_commit=CONFIG.git_commit,
        )
    except Exception as _ge:
        logger.debug("WRITE: git auto-commit skipped (%s)", _ge)

    if result.committed or not result.failed:
        fsm._progress_note(fsm._chunk_task_id("write"), "write", "done")

    # Title-index run cache (Tier 1): make this chunk's new notes visible to
    # later chunks' AUTOLINK/BACKLINK without a fresh full-vault scan. Only
    # append when the cache exists — first AUTOLINK builds it lazily.
    _cached_refs = getattr(fsm, "_run_title_refs", None)
    if _cached_refs is not None and fsm._txn is not None:
        from silica.driver.base import NoteRef
        _known = {os.path.abspath(r.path) for r in _cached_refs if getattr(r, "path", None)}
        for _p in (fsm._txn.created_paths or []):
            if os.path.abspath(_p) not in _known:
                _stem = os.path.splitext(os.path.basename(_p))[0]
                _cached_refs.append(NoteRef(name=_stem, path=_p))

    fsm._transition_success()


def handle_hub_update(fsm: "InjectorFSM") -> None:
    """Append MOC links to the Hub note for all newly written notes."""
    fsm._progress_note(fsm._chunk_task_id("hub_update"), "hub_update", "running")
    if not fsm.hub:
        logger.info("HUB_UPDATE: no hub configured, skipping")
        fsm._progress_note(fsm._chunk_task_id("hub_update"), "hub_update", "done")
        fsm._transition_success()
        return

    try:
        ops = load_ops(fsm._chunk_ctx["ops_path"])
    except Exception as e:
        raise RuntimeError(f"HUB_UPDATE: failed to read ops: {e}")

    hub_name = fsm.hub.strip("[]")
    hub_name_lower = hub_name.lower()

    # Collect write ops grouped by effective parent:
    # notes with op.parent set go to that parent note; others fall back to hub.
    hub_notes: list[tuple[str, str]] = []       # (note_path, desc)
    parent_notes: dict[str, list[tuple[str, str]]] = {}  # parent_name → [(note_path, desc)]
    for op in ops:
        if op.op != OpType.write:
            continue
        path = op.touched_ref()
        if not path:
            continue
        note_name = os.path.splitext(os.path.basename(path))[0]
        if note_name.lower() == hub_name_lower:
            continue
        snippet = (op.snippet or "").strip()
        desc = _hub_desc(snippet)
        effective_parent = (op.parent.strip("[]") if op.parent else None) or hub_name
        if effective_parent.lower() == hub_name_lower:
            hub_notes.append((path, desc))
        else:
            parent_notes.setdefault(effective_parent, []).append((path, desc))

    if not hub_notes and not parent_notes:
        logger.info("HUB_UPDATE: no new notes to link, skipping")
        fsm._progress_note(fsm._chunk_task_id("hub_update"), "hub_update", "done")
        fsm._transition_success()
        return

    hub_path = _moc_target(hub_name, fsm.target_dir)
    if not hub_path:  # the hub resolved to an inbox note — never a MOC target
        logger.info("HUB_UPDATE: hub '%s' is staging, skipping", hub_name)
        fsm._progress_note(fsm._chunk_task_id("hub_update"), "hub_update", "done")
        fsm._transition_success()
        return
    from silica.driver.base import NoteRef
    hub_ref = NoteRef(name=hub_name, path=hub_path)

    try:
        hub_note = DRIVER.read_note(hub_ref)
    except Exception as e:
        logger.warning("HUB_UPDATE: hub '%s' not readable: %s — skipping", hub_path, e)
        fsm._progress_note(fsm._chunk_task_id("hub_update"), "hub_update", "done")
        fsm._transition_success()
        return

    # If hub pre-existed (not created in this txn), register a content-based
    # rollback inverse using the content we just read — more reliable than
    # history:restore whose version positions shift after each new write.
    hub_path_norm = hub_path.replace("\\", "/")
    hub_is_new = fsm._txn is not None and any(
        p.replace("\\", "/") == hub_path_norm
        for p in (fsm._txn.created_paths or [])
    )
    if not hub_is_new and fsm._txn is not None:
        from silica.kernel.write.ops import InverseOp, InverseOpKind
        hub_inverse = InverseOp(
            kind=InverseOpKind.restore_version,
            path=hub_path,
            prior_content=hub_note.content,
        )
        fsm._txn.inverses.append(hub_inverse)

    # Cross-cluster integrity check: warn when new notes land in a different
    # cluster from the hub.  This is informational only — the MOC link is
    # still written, but the log helps identify structural drift.
    _gctx = fsm.context.get("vault_graph_ctx", {})
    _hub_key = hub_path.removesuffix(".md")
    _hub_cluster = _gctx.get(_hub_key, {}).get("cluster_id", -1)
    if _gctx and _hub_cluster >= 0:
        for note_path, _ in hub_notes:
            note_name = os.path.splitext(os.path.basename(note_path))[0]
            _note_key = f"{fsm.target_dir}/{note_name}".replace("//", "/")
            _note_cluster = _gctx.get(_note_key, {}).get("cluster_id", -1)
            if _note_cluster >= 0 and _note_cluster != _hub_cluster:
                logger.warning(
                    "HUB_UPDATE: '%s' (cluster %d) linked to hub '%s' (cluster %d) — cross-cluster MOC",
                    note_name, _note_cluster, hub_name, _hub_cluster,
                )

    # Derive the actual source file for this chunk (fsm.inbox_file is always
    # the first file and never updates in multi-file runs — use the flat index map).
    _fi, _ci = fsm._chunk_flat_to_fi_ci.get(fsm._current_chunk_idx, (0, 0))
    _source_file = (
        fsm._file_chunks[_fi]["source_file"]
        if _fi in fsm._file_chunks
        else fsm.inbox_file
    )
    source_name = os.path.splitext(os.path.basename(_source_file))[0]

    # "## From: {name}"; the sample is vestigial (moc.moc_heading).
    _lang_sample = hub_note.content + " ".join(d for _, d in hub_notes[:3])
    moc_heading = _moc_heading(source_name, _lang_sample)

    # Build note link lines.
    note_lines = [_moc_line(p, d) for p, d in hub_notes]

    # Merge: append to existing section if present (same file, multiple chunks),
    # otherwise create a new section.  Use overwrite to avoid the settle race.
    new_hub_content = _merge_moc_section(hub_note.content, moc_heading, note_lines)

    try:
        DRIVER.overwrite(hub_path, new_hub_content)
        # Explicitly wait until the section header is readable.
        _deadline = time.monotonic() + 5.0
        while time.monotonic() < _deadline:
            try:
                if moc_heading in DRIVER.read_note(hub_ref).content:
                    break
            except Exception:
                pass
            time.sleep(0.15)
        else:
            logger.warning("HUB_UPDATE: MOC block settle timeout for hub '%s' — graph may lag", hub_path)
        logger.info("HUB_UPDATE: updated hub '%s' with %d links", hub_path, len(hub_notes))
    except Exception as e:
        raise RuntimeError(f"HUB_UPDATE: failed to update hub '{hub_path}': {e}")

    # Extend snapshot_domain so LINT's graph regression check covers the hub's new links
    existing_paths = {d["path"] for d in fsm._chunk_ctx.get("snapshot_domain", [])}
    if hub_path not in existing_paths:
        fsm._chunk_ctx.setdefault("snapshot_domain", []).append({"name": hub_name, "path": hub_path})

    # Write MOC sections to specific parent notes (best-effort — only active when
    # the distiller emits op.parent, which requires the Block 4 prompt update).
    if parent_notes:
        from silica.kernel.write.ops import InverseOp, InverseOpKind
        for parent_name, p_new_notes in parent_notes.items():
            # Resolve the parent wherever it actually lives in the vault (mirrors
            # validate's search_names check) instead of assuming it sits under
            # target_dir — otherwise an existing parent in another folder reads
            # as "File not found". Fall back to target_dir only for a new note.
            parent_path = _moc_target(parent_name, fsm.target_dir)
            if not parent_path:  # staging note, never a MOC target
                logger.info("HUB_UPDATE: parent '%s' is staging, skipping", parent_name)
                continue
            try:
                from silica.driver.base import NoteRef as _NR
                p_ref = _NR(name=parent_name, path=parent_path)
                p_note = DRIVER.read_note(p_ref)
                # Register rollback inverse for parent note
                if fsm._txn is not None:
                    p_inverse = InverseOp(
                        kind=InverseOpKind.restore_version,
                        path=parent_path,
                        prior_content=p_note.content,
                    )
                    fsm._txn.inverses.append(p_inverse)
                # Build and write parent MOC block (same language-aware heading,
                # same deduplication logic as the hub section above).
                p_heading = _moc_heading(source_name, p_note.content)
                p_note_lines = [_moc_line(p, d) for p, d in p_new_notes]
                new_p_content = _merge_moc_section(p_note.content, p_heading, p_note_lines)
                DRIVER.overwrite(parent_path, new_p_content)
                existing_paths = {d["path"] for d in fsm._chunk_ctx.get("snapshot_domain", [])}
                if parent_path not in existing_paths:
                    fsm._chunk_ctx.setdefault("snapshot_domain", []).append(
                        {"name": parent_name, "path": parent_path}
                    )
                logger.info(
                    "HUB_UPDATE: updated parent '%s' with %d sub-spoke link(s)",
                    parent_path, len(p_new_notes),
                )
            except Exception as _pe:
                logger.warning("HUB_UPDATE: failed to update parent '%s': %s", parent_path, _pe)

    fsm._progress_note(fsm._chunk_task_id("hub_update"), "hub_update", "done")
    fsm._transition_success()
