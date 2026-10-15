# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Injector terminal states: LINT (gate), CLEANUP, ROLLBACK.

Handler bodies for InjectorFSM, extracted from orchestrator.py: each function
takes the FSM instance and mutates its context/state exactly as the former
method did. Patchable collaborators (DRIVER, CONFIG, tools, load_ops, time)
are resolved through the orchestrator module namespace (orch.X) so tests that
patch silica.router.orchestrator.* keep working.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from silica.router import orchestrator as orch

if TYPE_CHECKING:
    from silica.router.orchestrator import InjectorFSM

logger = logging.getLogger(__name__)


from silica.kernel.write.ops import OpType


def handle_lint(fsm: "InjectorFSM") -> None:
    fsm._progress_note(fsm._chunk_task_id("lint"), "lint", "running")
    try:
        ops = orch.load_ops(fsm._chunk_ctx["ops_path"])
    except Exception as e:
        raise RuntimeError(f"LINT: failed to read ops: {e}")

    touched = [
        (op.touched_ref(), op.op.value if op.op else "", op.hub or "")
        for op in ops
        if op.touched_ref() and op.op not in (OpType.delete, OpType.skip)
    ]

    # Per-op lint runs diff-aware (only violations a patch INTRODUCES block it);
    # this gate re-checks after autolink/backlink/hub mutations and must apply
    # the same tolerance, or a pre-existing issue in a patched user note aborts
    # the whole chunk (run 880b9aa9: f1_c0/f1_c1 died on an inherited
    # "frontmatter 'AI' missing" the WRITE phase had correctly waved through).
    baselines = fsm._chunk_ctx.get("lint_baseline", {})
    for path, op_type, hub in touched:
        res = orch.silica_lint(path, op_type=op_type or "", hub=hub or "")
        new_errors = [
            e for e in res.get("errors", []) if e not in set(baselines.get(path, []))
        ]
        if orch.CONFIG.verbose:
            logger.info(
                "[DEBUG LINT Gate]: File: %s | Type: %s | Hub: %s | Success: %s | Errors: %s",
                path,
                op_type,
                hub,
                res["success"],
                res.get("errors", []),
            )
        if not res["success"] and new_errors:
            fsm._chunk_ctx["abort_reason"] = f"Lint failed for {path}: {new_errors}"
            fsm._progress_note(fsm._chunk_task_id("lint"), "lint", "failed", error=fsm._chunk_ctx["abort_reason"])
            fsm.state = orch.InjectorState.ROLLBACK
            return

    # S3.2: Run graph-diff check
    regression_rule = fsm._get_recipe_gate("graph_regression", "forbid_new_orphans")
    if regression_rule != "allow":
        if fsm._pre_graph is None:
            fsm._chunk_ctx["abort_reason"] = "Graph regression gate failed: pre-write snapshot is missing"
            fsm._progress_note(fsm._chunk_task_id("lint"), "lint", "failed", error=fsm._chunk_ctx["abort_reason"])
            fsm.state = orch.InjectorState.ROLLBACK
            return
        try:
            from silica.driver.base import NoteRef
            snapshot_domain_dicts = fsm._chunk_ctx.get("snapshot_domain", [])
            if snapshot_domain_dicts:
                snapshot_domain = [NoteRef(**d) for d in snapshot_domain_dicts]
            else:
                # Fallback to touched refs if snapshot_domain is missing
                snapshot_domain = []
                for op in ops:
                    path = op.touched_ref()
                    if path:
                        name = os.path.splitext(os.path.basename(path))[0]
                        snapshot_domain.append(NoteRef(name=name, path=path))
            
            post_graph = orch.DRIVER.graph_snapshot(snapshot_domain)
            from silica.kernel.graph_diff import check_graph_regression
            
            created_paths = fsm._txn.created_paths if fsm._txn else []
            # Fold only chunks appended since the last LINT into the run-scoped stem
            # union (B8: was an O(chunks × concepts) rescan on every LINT). A chunk
            # collision-collapses after it is first folded, but its pre-collapse stems
            # only widen the allowed set — never manufacture a false regression.
            try:
                from silica.kernel.write.templates import slugify
                chunks = getattr(fsm, "_chunks", [])
                for chunk in chunks[fsm._run_concept_stems_n:]:
                    for batch in chunk.get("batches", []):
                        for concept in batch.get("concepts", []):
                            name = concept.get("name")
                            if name:
                                stem = os.path.splitext(os.path.basename(name))[0].lower()
                                fsm._run_concept_stems.add(stem)
                                fsm._run_concept_stems.add(slugify(stem))
                fsm._run_concept_stems_n = len(chunks)
            except Exception as _ce:
                logger.debug("Failed to extract run concept stems for graph check: %s", _ce)

            deferred_stems = set(fsm._chunk_ctx.get("deferred_stems", []))
            deferred_stems |= fsm._run_concept_stems

            # Exempt every path a phase of THIS chunk mutated, from the
            # rollback transaction (the single source of truth: SNAPSHOT
            # seeds it, every mutating phase appends). ops alone missed the
            # HUB_UPDATE/AUTOLINK/BACKLINK edits, so a pre-existing dangling
            # link in the hub's own planned-topic list read as "introduced"
            # and rolled back a healthy chunk (3 of the 6 rollbacks, run
            # 262e6847). validate deliberately keeps unresolved wikilinks in
            # run-authored content as forward-refs, so rule 2's business is
            # only notes this chunk never touched.
            patched_paths = frozenset(
                {p for op in ops
                 if op.op in (OpType.patch, OpType.overwrite) and (p := op.touched_ref())}
                | txn_touched_paths(fsm._txn)
            )

            success, errors = check_graph_regression(
                fsm._pre_graph, post_graph, created_paths, frozenset(deferred_stems),
                patched_paths, removed_paths=frozenset(txn_removed_paths(fsm._txn)),
            )

            if orch.CONFIG.verbose:
                logger.info(
                    "[DEBUG Graph Regression Gate]: Pre-write graph size: %d nodes | Post-write graph size: %d nodes | Rule: %s | Result: %s",
                    len(fsm._pre_graph.link_counts) if fsm._pre_graph and fsm._pre_graph.link_counts else 0,
                    len(post_graph.link_counts) if post_graph and post_graph.link_counts else 0,
                    regression_rule,
                    "PASSED" if success else f"FAILED: {errors}"
                )

            if not success:
                orphan_errors = [e for e in errors if e.startswith("Unplanned orphans")]
                drift_errors = [e for e in errors if e.startswith("Backlink drift")]
                blocking_errors = [
                    e for e in errors
                    if not e.startswith("Unplanned orphans") and not e.startswith("Backlink drift")
                ]
                if drift_errors:
                    logger.warning(
                        "[Graph Regression Gate]: Backlink drift (non-blocking): %s",
                        "; ".join(drift_errors),
                    )
                if orphan_errors:
                    logger.warning(
                        "[Graph Regression Gate]: Orphan warning (non-blocking): %s",
                        "; ".join(orphan_errors),
                    )
                    # Record run-created notes that ended this chunk orphaned.
                    # Acted on (if still orphaned) at end of run, not now —
                    # AUTOLINK/BACKLINK or a later chunk may yet connect them.
                    if fsm.warning_ledger is not None:
                        try:
                            from silica.kernel.graph_diff import normalize_ref
                            post_orphan_keys = {normalize_ref(r) for r in post_graph.orphans}
                            detail = "; ".join(orphan_errors)
                            for op in ops:
                                p = op.touched_ref()
                                if not p or op.op != OpType.write:
                                    continue
                                name = os.path.splitext(os.path.basename(p))[0]
                                if normalize_ref(NoteRef(name=name, path=p)) in post_orphan_keys:
                                    fsm.warning_ledger.add(p, "orphan", detail)
                        except Exception as _we:
                            logger.debug("orphan warning record failed (non-fatal): %s", _we)
                if blocking_errors:
                    reason = f"Graph regression gate failed: {'; '.join(blocking_errors)}"
                    logger.warning("[Graph Regression Gate]: Blocking errors (triggering rollback): %s", "; ".join(blocking_errors))
                    fsm._chunk_ctx["abort_reason"] = reason
                    fsm._progress_note(fsm._chunk_task_id("lint"), "lint", "failed", error=reason)
                    fsm.state = orch.InjectorState.ROLLBACK
                    return
        except Exception as e:
            logger.error("Failed to perform graph-diff check: %s", e)
            fsm._chunk_ctx["abort_reason"] = f"Graph regression gate error during check: {e}"
            fsm._progress_note(fsm._chunk_task_id("lint"), "lint", "failed", error=fsm._chunk_ctx["abort_reason"])
            fsm.state = orch.InjectorState.ROLLBACK
            return

    fsm._progress_note(fsm._chunk_task_id("lint"), "lint", "done")
    fsm._transition_success()


def _log_nucleate_completion(fsm: "InjectorFSM", fi: int, source_file: str) -> None:
    """Append one line to the vault's human journal (log.md).

    Pure projection of state WRITE/VALIDATE already recorded — the manifest
    (new/patch counts) and the deferred store (deferred count) — onto the
    log.md line shape. No new computation. Idempotent per (run_id, source
    file): a multi-file run shares one run_id and fires this once per file,
    so each file needs its own line, while a resume of the same run must not
    duplicate any (dedup_key). Best-effort and must never block CLEANUP.

    Called on every file conclusion — the success path (last chunk's CLEANUP)
    AND the failure path (last chunk rolled back, so CLEANUP never runs for it).
    A file whose earlier chunks committed real notes must still be accounted
    even when its last chunk fails; the `_files_logged` guard keeps the two
    entry points from double-recording.
    """
    try:
        from silica.kernel.recall.run_log import append_log_line, format_nucleate_event

        basename = os.path.basename(source_file)
        logged = getattr(fsm, "_files_logged", None)
        if logged is None:
            logged = fsm._files_logged = set()
        if fi in logged:
            return
        logged.add(fi)
        new_count = sum(
            1 for e in fsm.manifest.entries
            if e.source_basename == basename and e.op == "write"
        )
        patch_count = sum(
            1 for e in fsm.manifest.entries
            if e.source_basename == basename and e.op == "patch"
        )

        deferred_count = 0
        extractive_rejected = 0
        content_hashes = getattr(fsm, "_file_content_hashes", [])
        if fi < len(content_hashes):
            try:
                from silica.kernel.recall.deferred import get_deferred_store
                bundle = get_deferred_store().get(content_hashes[fi])
                if bundle:
                    deferred_count = len(bundle.get("rejected_ops", []))
                    extractive_rejected = sum(
                        1 for _op in bundle.get("rejected_ops", [])
                        if isinstance(_op, dict)
                        and str(_op.get("rejection_reason", "")).startswith("extractive:")
                    )
            except Exception as _de:
                logger.debug("CLEANUP: deferred count lookup failed (non-fatal): %s", _de)

        # Stash the grounded per-file outcome before any log I/O: this is what
        # the agent-facing tool result reports as actually written. Guarded so
        # a context-less fsm stub can't sink the log append below.
        ctx = getattr(fsm, "context", None)
        if isinstance(ctx, dict):
            entry = {"file": basename, "new": new_count, "patch": patch_count,
                     "deferred": deferred_count}
            if extractive_rejected:
                # W1 (survey-provenance spec): a claim the extractive span
                # gate dropped is declared in the run report, never silently
                # folded into the aggregate "deferred".
                entry["extractive_rejected"] = extractive_rejected
            prior = (ctx.get("renucleated") or {}).get(basename)
            if prior:
                # The source was nucleated before at another version: the
                # report says how many notes of that version stay beside these.
                entry["renucleated_prior_notes"] = prior
            ctx.setdefault("files_summary", []).append(entry)

        event = format_nucleate_event(basename, new_count, patch_count, deferred_count)
        append_log_line(event, fsm.progress.run_id, dedup_key=f"`{basename}`")
    except Exception as exc:
        logger.debug("CLEANUP: log.md append skipped (non-fatal): %s", exc)


def _drain_grounding(ctx: dict, basename: str) -> dict[str, dict]:
    """The per-note grounding score for `basename`, summed over the validate
    rounds DISTILL stacked in `ctx["grounding"]` (one row per accepted op: a
    note patched three times has three rows). Rows of this source leave the
    context so a resumed CLEANUP cannot count them twice; other sources' rows
    stay for their own record."""
    rows = list(ctx.get("grounding") or [])
    keep: list[Any] = []
    out: dict[str, dict] = {}
    for g in rows:
        if g.get("source_basename") != basename:
            keep.append(g)
            continue
        key = str(g.get("path") or "").removesuffix(".md")
        acc = out.setdefault(key, {"spans": 0, "ungrounded": 0, "profile": g.get("profile") or "default"})
        acc["spans"] += int(g.get("spans") or 0)
        acc["ungrounded"] += int(g.get("ungrounded") or 0)
    if rows and isinstance(ctx, dict):
        ctx["grounding"] = keep
    return out


def _source_text_after_cleanup(source_file: str) -> str:
    """The source's text once CLEANUP has archived it: same three candidates
    validate's `_full_source_text` tries, "" when none reads."""
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import active_done_dir, archive_path_for

    root = CONFIG.vault_path or ""
    for cand in (source_file, archive_path_for(source_file),
                 f"{active_done_dir()}/{os.path.basename(source_file)}"):
        if not cand:
            continue
        try:
            return open(os.path.join(root, cand), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
    return ""


def _archived_source_text(basename: str) -> str:
    """The archived text of a source the ledger knows only by basename: the
    archive mirrors the inbox tree, so the file can sit anywhere under the
    done root. First match wins; "" when none, and its spans then read as
    lost, which is the pre-2026-09-04 behaviour for that one source."""
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import active_done_dir

    root = os.path.join(CONFIG.vault_path or "", active_done_dir())
    for dirpath, _dirs, files in os.walk(root):
        if basename in files:
            try:
                return open(os.path.join(dirpath, basename), encoding="utf-8",
                            errors="replace").read()
            except OSError:
                return ""
    return ""


def _reverify_prior(fsm, basename: str, source_file: str, sha256: str,
                    notes: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Re-check the previous version's notes against the new source text
    (spec M6): `({note: grounding}, reverified)`. A note whose checkable
    spans all survive is re-verified and leaves the drifted list; one that
    lost a span stays drifted and the digest names the span. A note with
    nothing checkable (prose on the paraphrasing profile) is neither: it
    keeps its drift, because zero spans cannot vouch for anything (§0.5).
    Every prior note, the ones this run patched included: the op row scores
    only what the op proposed, and a patch can land as a relations-only
    append while the body keeps a v1 formula the new source dropped (seen
    2026-09-04: snippet 2/2 grounded, body still on the old constant).
    The whole body is what a reader gets, so its score wins."""
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter
    from silica.kernel.write.provenance import (
        grounding_counts, read_records, sources_of, ungrounded_spans)

    recs = read_records(basename)
    if not recs or recs[-1].get("sha256") == sha256:
        return {}, []
    prior = list(recs[-1].get("notes") or [])
    if not prior:
        return {}, []
    text = _source_text_after_cleanup(source_file)
    if not text.strip():
        return {}, []
    ledger = getattr(fsm, "warning_ledger", None)
    grounding: dict[str, dict] = {}
    reverified: list[str] = []
    other_texts: dict[str, str] = {}
    for note in prior:
        try:
            content = DRIVER.read_note(note if note.endswith(".md") else note + ".md").content or ""
        except Exception:
            continue  # gone or unreadable: nothing to re-verify, stays as the ledger has it
        _d, _r, body = frontmatter.split(content)
        # A note patched from several lessons carries spans of each; against
        # this source alone every span of the others read as lost (32% of the
        # test vault's notes sit under 2+ sources, counted 2026-09-04). The
        # ledger names them, their archived text is the rest of the corpus.
        corpus = text
        for other in sources_of(note):
            if other != basename:
                if other not in other_texts:
                    other_texts[other] = _archived_source_text(other)
                corpus += "\n" + other_texts[other]
        checked, lost = grounding_counts(body or "", corpus)
        grounding[note] = {"spans": checked, "ungrounded": lost, "profile": "reverify"}
        if lost and ledger is not None:
            ledger.add(note, "reverify_lost_span",
                       " | ".join(sp[:60] for sp in ungrounded_spans(body or "", corpus)))
        elif checked and not lost:
            reverified.append(note)
    return grounding, reverified


def _record_provenance(fsm: "InjectorFSM", fi: int, source_file: str) -> None:
    """Append one `<vault>/provenance.json` record (spec-hermes-coherence §3).

    Sibling projection to _log_nucleate_completion, at the same CLEANUP point:
    reuses fsm._file_content_hashes[fi] — the sha256 already computed once
    per file at RUN start (silica.router.orchestrator.InjectorFSM.run), the
    same value the /nucleate pre-check will later compare against. Recomputing
    it here would fail anyway: by CLEANUP the source file has already been
    archived (moved) out of its original inbox path. `notes` is the
    projection of this run's validated write/patch ops for this source,
    already recorded in fsm.manifest.entries — no new computation. Records
    even when notes is empty: a version change with zero touched notes still
    means every note derived from the prior version is now stale. Best-
    effort and must never block CLEANUP.
    """
    try:
        from silica.kernel.write.provenance import append_record, is_deriving_op

        basename = os.path.basename(source_file)
        content_hashes = getattr(fsm, "_file_content_hashes", [])
        sha256 = content_hashes[fi] if fi < len(content_hashes) else ""
        if not sha256:
            return

        notes = sorted({
            e.path for e in fsm.manifest.entries
            if e.source_basename == basename and is_deriving_op(e.op)
        })

        # getattr: the record must land even from a caller without a context
        # (a bookkeeping key must never cost the record; the except below
        # would swallow it silently).
        ctx = getattr(fsm, "context", None) or {}
        partial = bool(ctx.get("has_partial_failure") or ctx.get("failed_chunks"))
        grounding = _drain_grounding(ctx, basename)
        re_grounding, reverified = _reverify_prior(fsm, basename, source_file, sha256, notes)
        grounding.update(re_grounding)  # whole-body row wins over the op row
        append_record(basename, sha256, fsm.progress.run_id, notes, partial=partial,
                      grounding=grounding, reverified=reverified)
    except Exception as exc:
        logger.debug("CLEANUP: provenance append skipped (non-fatal): %s", exc)


from silica.kernel.recall.paths import SOURCES_MARKER as _SOURCES_MARKER


def _write_source_leaf(fsm: "InjectorFSM", source_file: str) -> None:
    """Verbatim source leaf + `## Sources` links (spec-harness-promotion §2).

    Runs at CLEANUP, before archiving — the one point where the source text
    and the produced notes are both in hand. A leaf is written when the run
    asked for it (`keep_sources`, the /nucleate --keep-sources flag) or when
    it is a conversation capture (`seen_override` set: the source is
    ephemeral, otherwise lost). Independently of who wrote the leaf — this
    run or web_research beside its own inbox note — every note this source
    produced (manifest write/patch entries, same derivation as provenance)
    gains a `## Sources` block linking the leaf. Idempotent on the link, so
    a re-ingest never double-links. Inverses ride fsm._run_inverses, so the
    undo journal covers leaf and block like any other write of the run.
    Best-effort and must never block CLEANUP.
    """
    try:
        from silica.kernel.write import frontmatter
        from silica.kernel.write.contested import append_before_superseded
        from silica.kernel.write.ops import InverseOp, InverseOpKind
        from silica.kernel.write.templates import append_source
        from silica.kernel.recall.paths import SOURCES_DIR
        from silica.kernel.vault_manifest import in_write_dir
        from silica.kernel.write.provenance import (
            attribute_lines,
            footnote_label,
            is_deriving_op,
            source_event_date,
        )

        basename = os.path.basename(source_file)
        # A segment of a one-node source (a Zotero paper, ADR-0055) names the
        # leaf the converter already wrote whole: link there, write nothing.
        # The label stays the segment's, because the patch executor already
        # cited the segment's prose with it.
        try:
            src_data, _, _ = frontmatter.split(orch.DRIVER.read_note(source_file).content or "")
        except Exception:
            src_data = {}
        owner = src_data.get("source_leaf") if isinstance(src_data, dict) else None
        leaf_name = str(owner) if owner else basename
        leaf_rel = f"{in_write_dir(SOURCES_DIR)}/{leaf_name}"
        leaf_stem = leaf_name.removesuffix(".md")

        try:
            orch.DRIVER.read_note(leaf_rel)
            leaf_exists = True
        except Exception:
            leaf_exists = False

        wants_leaf = getattr(fsm, "keep_sources", False) or bool(
            getattr(fsm, "seen_override", None)
        )
        if wants_leaf and not leaf_exists and not owner:
            source_text = orch.DRIVER.read_note(source_file).content or ""
            # Single frontmatter block: keep the source BODY verbatim; the date
            # is the source's event clock, resolved by the same function that
            # stamps it on every claim (kernel/provenance) so leaf and claim
            # can never disagree. An undated source leaves the leaf undated:
            # `date: <today>` here would be read back by _incoming_side as the
            # incoming clock of every future contest this leaf feeds.
            _data, _raw, body = frontmatter.split(source_text)
            date = source_event_date(source_text, getattr(fsm, "seen_override", None))
            # Carry `source_file:` (stamped by convert on every converted inbox
            # note) into the leaf: this block otherwise replaces the source's
            # frontmatter wholesale, and the leaf is the only copy left once
            # the inbox note is archived.
            # The citation fields convert stamps (doi/arxiv/authors/title) ride
            # along for the same reason: this leaf is their last copy.
            carry = ""
            for key in ("source_title", "authors", "doi", "arxiv", "source_file"):
                val = _data.get(key) if isinstance(_data, dict) else None
                if val:
                    quoted = str(val).replace("\\", "\\\\").replace('"', '\\"')
                    carry += f'{key}: "{quoted}"\n'
            date_line = f"date: {date}\n" if date else ""
            leaf = f"---\n{date_line}source_id: {basename}\n{carry}---\n\n{body.lstrip()}"
            orch.DRIVER.create(leaf_rel, leaf)
            fsm._run_inverses.append(
                (leaf_rel, InverseOp(kind=InverseOpKind.delete_created, path=leaf_rel), None)
            )
            leaf_exists = True

        notes = sorted({
            e.path for e in fsm.manifest.entries
            if e.source_basename == basename and is_deriving_op(e.op)
        })
        label = footnote_label(basename)
        marker = f"[^{label}]"

        if not leaf_exists:
            # No leaf, no `## Sources` (reliability_tier reads the marker, and
            # a block with nothing verbatim behind it would lie). A patch may
            # still have cited its prose with the label, and a marker without
            # a definition renders as literal text: define it as the source's
            # name, plain, at the end of the body.
            for rel in notes:
                note_path = f"{rel}.md"
                try:
                    prior = orch.DRIVER.read_note(note_path).content or ""
                except Exception:
                    continue
                if marker not in prior or f"{marker}:" in prior:
                    continue
                orch.DRIVER.overwrite(
                    note_path, append_before_superseded(prior, f"\n{marker}: {basename}\n"))
                fsm._run_inverses.append(
                    (note_path,
                     InverseOp(kind=InverseOpKind.restore_version, path=note_path,
                               prior_content=prior),
                     None)
                )
            return

        # Keyed per-claim attribution (OKF §5.1): the leaf body is the only copy
        # of what this source actually said, and here is the one place it sits
        # beside the notes it produced. Lines that are verbatim from it get the
        # leaf's own id as a footnote label, so a note fed by three transcripts
        # says which line came from which instead of just naming all three.
        leaf_body = ""
        try:
            _d, _r, leaf_body = frontmatter.split(orch.DRIVER.read_note(leaf_rel).content or "")
        except Exception:
            leaf_body = ""
        # Path-qualified, never the bare stem: the outline lane writes a spine
        # note carrying the source's own stem beside the concept notes, and a
        # bare `[[Lezione 3]]` resolved to that sibling (same folder wins), so
        # every block pointed at the spine and the leaf was unreachable
        # (2026-09-04, 231 notes in one vault).
        link = f"[[{leaf_rel.removesuffix('.md')}]]"

        for rel in notes:
            note_path = f"{rel}.md"  # manifest paths carry no .md
            try:
                prior = orch.DRIVER.read_note(note_path).content or ""
            except Exception:
                continue
            # Idempotency is about the BLOCK, not the link anywhere in the file.
            # The distiller names the source basename as a `related:` /
            # `parent note:` wikilink long before the leaf exists, so a
            # whole-file grep read those notes as "already linked": they never
            # got the block, and reliability_tier — which looks for the marker,
            # not the link — filed them as distilled. 10 of 30 notes on one
            # paper (2026-08-18). A block linking the legacy bare stem counts.
            already = prior.split(_SOURCES_MARKER, 1)[1] if _SOURCES_MARKER in prior else ""
            linked = link in already or f"[[{leaf_stem}]]" in already
            attributed = attribute_lines(prior, leaf_body, label)
            # The patch executor defined its marker as the plain source name
            # (it cannot know a leaf will exist); now one does, so the
            # definition is this block's and points at the leaf.
            plain = f"{marker}: {basename}"
            if any(l.rstrip() == plain for l in attributed.splitlines()):
                attributed = "\n".join(
                    l for l in attributed.splitlines() if l.rstrip() != plain
                ) + ("\n" if attributed.endswith("\n") else "")
            # The definition rides the Sources block and only when a line
            # carries the marker (verbatim, marked here; or distilled, cited
            # by the patch executor), so a note never carries a dangling
            # `[^id]` and never a definition nothing points at.
            cited = marker in attributed.split(_SOURCES_MARKER, 1)[0]
            add_def = cited and f"{marker}:" not in attributed
            if linked and not add_def and attributed == prior:
                continue  # re-ingest no-op
            # The definition carries the link, so it is the entry: a bare copy
            # beside it listed every leaf twice in reading view. The bare link
            # is the entry only while nothing in the note cites the leaf.
            # Inside the block, not at EOF: a patch appended after the block
            # used to push the next link under whatever section came last.
            entry = f"{marker}: {link}" if add_def else ("" if linked else link)
            new = append_source(attributed, entry) if entry else attributed
            from silica.kernel.write.preserve import Loss

            # CITE: verbatim lines gain their [^label] and the patch's own
            # definition gives way to the one that links the leaf.
            orch.DRIVER.overwrite(note_path, new, loss=Loss.CITE)
            fsm._run_inverses.append(
                (note_path,
                 InverseOp(kind=InverseOpKind.restore_version, path=note_path,
                           prior_content=prior),
                 None)
            )
    except Exception as exc:
        logger.debug("CLEANUP: source leaf skipped (non-fatal): %s", exc)


def txn_touched_paths(txn) -> set[str]:
    """Every path the chunk's transaction recorded a mutation for, any phase.

    Reads Txn.inverses (InverseOp .path / .to_path), tolerating the serialized
    dict form the persisted snapshot fallback carries.
    """
    out: set[str] = set()
    for inv in getattr(txn, "inverses", None) or []:
        for key in ("path", "to_path"):
            val = inv.get(key) if isinstance(inv, dict) else getattr(inv, key, None)
            if val:
                out.add(val)
    return out


def txn_removed_paths(txn) -> set[str]:
    """The paths the chunk's transaction deleted or moved away: the only notes
    whose absence the chunk can be blamed for (check_graph_regression rule 2).
    A delete's inverse recreates `path`; a move's moves `to_path` back to it."""
    out: set[str] = set()
    for inv in getattr(txn, "inverses", None) or []:
        kind = inv.get("kind") if isinstance(inv, dict) else getattr(inv, "kind", None)
        path = inv.get("path") if isinstance(inv, dict) else getattr(inv, "path", None)
        if path and str(getattr(kind, "value", kind)) in ("recreate_deleted", "move_back"):
            out.add(path)
    return out


def file_language(fsm: "InjectorFSM", fi: int, source_text: str) -> str | None:
    """Same precedence as render_prompt: the per-file pin, else the vault's
    declared language, else detection on the source. The pin is skipped
    whenever the manifest declares a language, so without the middle rung
    the outline claims came out in English on an Italian vault (2026-09-02)."""
    from silica.kernel.text import language as lang_mod
    from silica.kernel.vault_manifest import get_active_manifest
    return (fsm.context.get(f"file_{fi}_language")
            or get_active_manifest().conventions.language
            or lang_mod.display_name(lang_mod.detect(source_text[:4000])))


def handle_cleanup(fsm: "InjectorFSM") -> None:
    from silica.tools.wrapped import silica_cleanup

    fsm._get_chunks_from_context_if_empty()
    fi, ci = fsm._chunk_flat_to_fi_ci.get(fsm._current_chunk_idx, (0, fsm._current_chunk_idx))
    with orch.phase(fsm, fsm._chunk_task_id("cleanup"), "cleanup"):
        # Always write ledger for this chunk's ops (per chunk)
        fsm._write_ledger_for_file(fi, "committed")

        # Archive the physical file only on the last chunk of its file group
        file_group = fsm._file_chunks.get(fi, {})
        n_chunks_in_file = len(file_group.get("chunks", []))
        is_last_chunk_of_file = (ci + 1 >= n_chunks_in_file)

        fi_prefix = f"f{fi}_"
        file_has_failure = any(
            t.status == "failed" for t in fsm.progress.tasks
            if t.id.startswith(fi_prefix)
        )
        inbox_file_for_fi = file_group.get("source_file", fsm.inbox_file)

        if is_last_chunk_of_file:
            # Log the per-file outcome regardless of failures: a file that
            # committed notes in earlier chunks still deserves its log.md line
            # and files_summary entry. Only ARCHIVING is gated on no-failure
            # (a partial source stays in the inbox for retry).
            _log_nucleate_completion(fsm, fi, inbox_file_for_fi)
            if not file_has_failure:
                # Leaf before archive: the source text must still be readable
                # at its inbox path. On failure the source stays in the inbox
                # for retry, so nothing is lost by skipping the leaf here.
                _write_source_leaf(fsm, inbox_file_for_fi)
                res = silica_cleanup(inbox_file_for_fi)
                if "error" in res:
                    fsm.context["cleanup_warning"] = res["error"]
                # Title-index run cache: the archived source moved out of its
                # indexed path — drop its ref so AUTOLINK can't link to a stale
                # inbox title.
                _cached_refs = getattr(fsm, "_run_title_refs", None)
                if _cached_refs is not None:
                    _src_abs = os.path.abspath(inbox_file_for_fi)
                    _cached_refs[:] = [
                        r for r in _cached_refs
                        if not getattr(r, "path", None) or os.path.abspath(r.path) != _src_abs
                    ]
            else:
                logger.info(
                    "File %d (%s) had chunk failures — not archiving.",
                    fi, file_group.get("source_file", "?"),
                )
            # Provenance covers whatever DID commit: a partial file's validated
            # write/patch ops are real derived notes, and both session attribution
            # (eval session_recall) and re-ingest idempotence (note_authored_by)
            # must see them even while the source stays in inbox for retry.
            _record_provenance(fsm, fi, inbox_file_for_fi)
        else:
            logger.info(
                "Chunk f%d_c%d done. Archiving deferred until last chunk of file %d.",
                fi, ci, fi,
            )

        # Grounded outcome evidence: committed-chunk count lets the terminal
        # verdict (here and in ROLLBACK) tell "partial" from "failed".
        fsm.context["committed_chunks"] = fsm.context.get("committed_chunks", 0) + 1

        # Run-level verdict, recomputed each chunk's CLEANUP (last write wins).
        # no_ops is a whole-run property — it holds only when NO chunk had actionable
        # ops. A later chunk that had ops lifts a prior all-skip chunk's provisional
        # no_ops rather than staying stuck on it, in either order (A24).
        if fsm.context.get("has_partial_failure"):
            fsm.context["final_status"] = "partial"
        elif fsm.context.get("run_had_ops"):
            fsm.context["final_status"] = "Success"
        else:
            fsm.context["final_status"] = "no_ops"

        # Persist this run's inverses for /revert, with final content hash.
        if fsm._undo_run_id and fsm._run_inverses:
            import hashlib
            from silica.kernel.write.ops import InverseOpKind
            from silica.kernel.write.undo_journal import get_undo_journal
            journal = get_undo_journal()
            for path, inv, _ in fsm._run_inverses:
                try:
                    post = orch.DRIVER.read_note(path).content
                    post_hash = hashlib.sha256((post or "").encode("utf-8")).hexdigest()
                except Exception:
                    post_hash = None
                    if inv.kind != InverseOpKind.recreate_deleted:
                        # Note should exist after this write; without its hash the
                        # /revert "modified since inject" guard can't protect it.
                        logger.warning(
                            "finalize: could not hash %s post-write; /revert guard "
                            "disabled for it", path)
                journal.record(fsm._undo_run_id, inv, post_hash)
            fsm._run_inverses.clear()


def handle_rollback(fsm: "InjectorFSM") -> None:
    fsm._progress_note("rollback", "rollback", "running")
    snapshot_res = fsm._chunk_ctx.get("snapshot", {})
    # fsm._txn.inverses is the single source of truth for rollback (C3 /
    # ADR-0009): SNAPSHOT seeds it and every phase that mutates a pre-existing
    # note appends to it. Fall back to the persisted snapshot dict only when
    # no live transaction exists (defensive — both share the per-chunk lifetime).
    if fsm._txn is not None:
        txn_id = fsm._txn.id
        inverses = fsm._txn.inverses_serialized
    else:
        txn_id = snapshot_res.get("txn_id")
        inverses = snapshot_res.get("inverses", [])

    if txn_id and inverses:
        from silica.tools.wrapped import silica_restore
        try:
            res = silica_restore(txn_id=txn_id, inverses=inverses)
            if not res.get("success", False):
                err_msg = "; ".join(res.get("errors", []))
                logger.error("Rollback partially failed: %s", err_msg)
                fsm.context["rollback_error"] = err_msg
            else:
                logger.info("Rollback complete for txn %s", txn_id)
        except Exception as e:
            logger.error("Rollback failed: %s", e)
            fsm.context["rollback_error"] = str(e)
        fsm._write_ledger_rollback(txn_id)

    # Clean up the embedding index for notes that were created and then rolled back
    # to prevent stale phantom entries that would bias future candidate searches.
    created_paths: list[str] = []
    if fsm._txn is not None and fsm._txn.created_paths:
        created_paths = list(fsm._txn.created_paths)
    elif snapshot_res.get("created_paths"):
        created_paths = list(snapshot_res["created_paths"])
    if created_paths:
        try:
            from silica.kernel.recall.embed import get_store
            store = get_store()
            for cp in created_paths:
                store.delete(cp.removesuffix(".md"))
            store.save()
        except Exception as _ee:
            logger.debug("ROLLBACK: embed index cleanup failed (non-fatal): %s", _ee)

    fsm._progress_note("rollback", "rollback", "done")
    # Contain the failure at chunk level instead of aborting the whole run
    fsm._contain_chunk_failure()
