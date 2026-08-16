# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Coordinator — runs the Injector and a pool of bounded sub-agents concurrently.

Producer/consumer model:
  * the InjectorFSM (router model) runs on the calling thread and *produces*
    WorkItems as it commits batches — it never blocks on a sub-agent;
  * a ThreadPoolExecutor of BoundedSubAgents (worker model) *consumes* the queue
    in parallel, writing only through their CapabilityBounds + the commit_ops micro-gate;
  * after the Injector finishes, the Coordinator closes the queue and joins the
    pool (drains remaining items) before returning an aggregated status.

When no items are ever produced this collapses to single-FSM behaviour with
negligible overhead.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from silica.config import CONFIG

logger = logging.getLogger(__name__)

# Ceiling on end-of-run orphan repairs: each one is a worker LLM call carrying
# an 8000-char note body, so a run that warned on a whole folder must not turn
# CLEANUP into an unbounded token spend. Residual orphans past the cap stay in
# the warning ledger and the vault report — dropped from THIS run's repair
# pass, never from visibility. ponytail: fixed cap, promote to Config if a
# real run ever needs a different ceiling.
MAX_ORPHAN_REPAIRS_PER_RUN = 16


class Coordinator:
    def __init__(
        self,
        inbox_files: list[str] | None = None,
        target_dir: str = "",
        hub: str | None = None,
        *,
        resume_run_id: str | None = None,
        seen_override: str | None = None,
        keep_sources: bool = False,
        episodic_capture: bool = True,
        distill_profile: str | None = None,
        config: Any = CONFIG,
        cancel_token: "threading.Event | None" = None,
    ):
        # Lazy import keeps construction patchable at the orchestrator boundary
        # and avoids import-time coupling.
        from silica.router.orchestrator import InjectorFSM

        self.config = config
        self._stop = cancel_token if cancel_token is not None else threading.Event()
        self.fsm = InjectorFSM(
            inbox_files=inbox_files,
            target_dir=target_dir,
            hub=hub,
            resume_run_id=resume_run_id,
            seen_override=seen_override,
            keep_sources=keep_sources,
            episodic_capture=episodic_capture,
            distill_profile=distill_profile,
        )

    def run(self) -> dict[str, Any]:
        import time

        from silica.kernel.workqueue import WorkQueue
        from silica.router.warning_ledger import WarningLedger
        from silica.agent.bus import BUS

        # Sweep scope marker: files under target_dir modified from here on are
        # candidates for the end-of-run dangling-link sweep.
        self._run_started: float | None = time.time()

        run_dir = getattr(self.fsm.progress, "run_dir", None)
        wq = WorkQueue(run_dir=run_dir)
        self.fsm.work_queue = wq
        self.fsm.warning_ledger = WarningLedger(run_dir=run_dir)

        # Named, not a lambda, so it can be unsubscribed: this used to leak one
        # subscriber per run, and "work/*" now also matches the per-phase stream
        # (work/phase), so run N was fanning every phase transition out to N-1
        # dead debug closures.
        def _log_work_event(e) -> None:
            logger.debug("work event: %s", e)
        BUS.subscribe("work/*", _log_work_event)

        max_workers = max(1, int(getattr(self.config, "subagent_max_concurrent", 3)))
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="subagent")
        futures = [pool.submit(self._consume, wq) for _ in range(max_workers)]

        try:
            result = self.fsm.run()  # producer; runs to completion on this thread
            # End-of-run repair: only the warnings still unresolved after the whole
            # run (incl. AUTOLINK/BACKLINK) become priority work for the sub-agents.
            self._enqueue_orphan_repairs(wq, result)
        except BaseException:
            # Interrupt or unexpected crash: signal consumers to exit immediately
            # so pool.shutdown() doesn't block on in-flight LLM calls.
            self._stop.set()
            raise
        finally:
            BUS.unsubscribe("work/*", _log_work_event)
            wq.close()
            if self._stop.is_set():
                # Best-effort: cancel queued futures and return without joining threads
                # that are mid-LLM-call. The process is shutting down anyway.
                pool.shutdown(wait=False, cancel_futures=True)
            else:
                pool.shutdown(wait=True)

        # Re-verify: recompute orphans after the repairs committed.
        self._reverify_orphans(result)

        # Zero dangling links on run exit: forward-refs that never materialized
        # are unlinked back to plain text (deterministic; only this run's notes).
        self._sweep_dangling_links(result)

        # Surface any consumer-thread crashes (handle() already swallows per-item
        # errors, so this only catches unexpected pool failures).
        for f in futures:
            exc = f.exception()
            if exc:
                logger.warning("sub-agent consumer crashed: %s", exc)

        result["subagents"] = wq.summary()
        logger.info("Coordinator: sub-agent outcomes %s", result["subagents"])

        # The passes above (sub-agent repairs, link sweep) edit the run's notes
        # AFTER finalize hashed them, so /revert's "modified since inject" guard
        # refused the run's own writes. Re-hash now: the run is truly over, and
        # this state is the one the guard should protect.
        undo_run_id = getattr(self.fsm, "_undo_run_id", None)
        if undo_run_id:
            try:
                from silica.kernel.write.undo_journal import get_undo_journal

                get_undo_journal().refresh_post_hashes(undo_run_id)
            except Exception as e:
                logger.debug("post-run hash refresh failed (non-fatal): %s", e)
        return result

    # --- end-of-run orphan resolution -------------------------------------

    def _current_orphans(self) -> set[str]:
        """Normalized set of notes currently orphaned, from the driver graph.

        Fix B: orphan status is just in-degree==0 — it needs no Louvain/PageRank.
        ``DRIVER.orphans()`` reads the maintained graph directly (~sub-ms), where
        the old ``compute_report`` rebuilt the whole report (~3.8s at 10k notes)
        and this fires up to twice per run (enqueue + reverify).
        """
        from silica.agent.bounds import _norm_path
        try:
            from silica.driver import DRIVER
            return {_norm_path(o.path) for o in DRIVER.orphans()}
        except Exception as e:
            logger.debug("orphan recompute failed (non-fatal): %s", e)
            return set()

    def _orphan_candidates(self, path: str, k: int = 3) -> list[dict]:
        """Related notes for an orphan, via the relatedness facade.

        Fuses embeddings + co-occurrence (RRF): pure candidate generation, no
        cosine thresholding, so the facade is a clean drop-in. Still produces
        link targets when the embedding index is empty — the co-occurrence leg
        carries the routing on its own.
        """
        try:
            from silica.config import CONFIG
            from silica.kernel.recall.cooccurrence import cooccur_key, get_cooccur_store
            from silica.kernel.recall.embed import get_store
            from silica.kernel.recall.relatedness import related_notes

            from silica.agent.providers import get_reranker
            from silica.kernel.recall.rerank import link_query, rerank_related

            # cooccur_key (case-PRESERVED, .md-stripped) is the store keyspace; _norm_path
            # would lowercase and miss the case-preserving stored keys -> empty results.
            key = cooccur_key(path)
            reranker = get_reranker(CONFIG)
            pool = max(k, 20) if reranker else k
            results = related_notes(
                key,
                embed_store=get_store(),
                cooccur_store=get_cooccur_store(lang=CONFIG.cooccurrence_lang),
                k=pool,
            )
            if reranker:
                results = rerank_related(reranker, link_query(key), results, k=k)
            return [{"name": r.name, "path": r.path} for r in results]
        except Exception as e:
            logger.debug("orphan candidate lookup failed (non-fatal): %s", e)
            return []

    def _enqueue_orphan_repairs(self, wq: Any, result: dict) -> None:
        from silica.agent.bounds import _norm_path
        from silica.kernel.workqueue import WorkItem

        ledger = getattr(self.fsm, "warning_ledger", None)
        if ledger is None or len(ledger) == 0:
            return

        warned = ledger.paths("orphan")
        current = self._current_orphans()
        # Residual = warned notes that are STILL orphaned after the full run.
        residual = [p for p in warned if not current or _norm_path(p) in current]

        if len(residual) > MAX_ORPHAN_REPAIRS_PER_RUN:
            logger.warning(
                "orphan repair: %d residual orphans, repairing the first %d — "
                "the rest stay in the warning ledger / vault report.",
                len(residual), MAX_ORPHAN_REPAIRS_PER_RUN,
            )

        enqueued = 0
        for path in residual[:MAX_ORPHAN_REPAIRS_PER_RUN]:
            candidates = self._orphan_candidates(path)
            if not candidates:
                continue
            wq.enqueue(WorkItem(
                kind="orphan",
                target_path=path,
                context={"candidates": candidates, "hub": getattr(self.fsm, "hub", None)},
                reason="residual_orphan",
            ))
            enqueued += 1

        result["orphan_warnings"] = {
            "warned": len(warned),
            "residual": len(residual),
            "enqueued": enqueued,
        }

    def _sweep_dangling_links(self, result: dict) -> None:
        """Unlink wikilinks still dangling after the whole run committed.

        The run's notes come from the provenance ledger (written at CLEANUP,
        keyed by this run_id), plus AI-authored notes under the run's target
        folder written since the run started — deferred retries and sub-agent
        repairs commit outside the FSM manifest, and their notes were escaping
        the sweep (observed: [[KnowledGPT]] left dangling in a recovered note).
        Notes without `AI: true` are never touched, so a human's own deliberate
        forward-references survive. A run-level /revert deletes the run's
        notes, which subsumes these edits.
        """
        try:
            from silica.kernel.link.sweep import sweep_dangling_links
            from silica.kernel.write.provenance import read_records

            run_id = getattr(getattr(self.fsm, "progress", None), "run_id", None)
            if not run_id:
                return
            notes = {
                n
                for r in read_records()
                if r.get("run_id") == run_id
                for n in (r.get("notes") or [])
            }
            notes.update(self._run_written_under_target())
            notes = sorted(notes)
            if not notes:
                return
            summary = sweep_dangling_links(notes)
            if summary["links_stripped"] or summary["links_relinked"]:
                result["link_sweep"] = {
                    "notes_edited": summary["notes_edited"],
                    "links_stripped": summary["links_stripped"],
                    "links_relinked": summary["links_relinked"],
                }
        except Exception as e:
            logger.warning("dangling-link sweep failed (non-fatal): %s", e)

    def _run_written_under_target(self) -> set[str]:
        """AI-authored notes under the run's target folder modified since the
        run started — the sweep scope for writes that bypass the FSM manifest
        (deferred retries, sub-agent repairs). Vault-relative, no `.md`."""
        out: set[str] = set()
        try:
            from pathlib import Path

            started = getattr(self, "_run_started", None)
            target = getattr(self.fsm, "target_dir", "") or ""
            vault = (getattr(self.config, "vault_path", "") or "").strip()
            if not target or not vault or started is None:
                return out
            base = Path(vault) / target
            for p in base.rglob("*.md"):
                try:
                    if p.stat().st_mtime < started:
                        continue
                    head = p.read_text(encoding="utf-8", errors="replace")[:2048]
                    if "\nAI: true" not in head:
                        continue
                except OSError:
                    continue
                out.add(str(p.relative_to(vault))[:-3])
        except Exception as e:
            logger.debug("run-written scan failed (non-fatal): %s", e)
        return out

    def _reverify_orphans(self, result: dict) -> None:
        """After repairs commit, recompute how many warned notes are still orphaned."""
        ow = result.get("orphan_warnings")
        if not ow or not ow.get("enqueued"):
            return
        ledger = getattr(self.fsm, "warning_ledger", None)
        if ledger is None:
            return
        from silica.agent.bounds import _norm_path
        warned_keys = {_norm_path(p) for p in ledger.paths("orphan")}
        still = self._current_orphans() & warned_keys
        ow["residual_after"] = len(still)
        logger.info(
            "Coordinator: orphan repair — %d warned, %d enqueued, %d still orphaned after",
            ow.get("warned", 0), ow.get("enqueued", 0), len(still),
        )

    def _consume(self, wq: Any) -> None:
        """One consumer thread — delegates to the shared ``consume`` loop in
        silica/agent/subagent.py (the same engine ad-hoc batches run on)."""
        from silica.agent.subagent import BoundedSubAgent, consume

        consume(wq, BoundedSubAgent(self.config), self._stop)
