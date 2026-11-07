# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""BoundedSubAgent — a small, tightly-bounded worker that runs on the worker model.

A bounded sub-agent consumes one WorkItem at a time and dispatches it to the
capability registered under ``item.kind``. Each capability (see
``silica/capabilities/``) is a self-contained ``run(item, config) -> dict``
function that writes only through its CapabilityBounds + the commit_ops micro-gate.
The sub-agent runs on the *worker* model (role="worker"), concurrently with the
Injector.

``BoundedSubAgent`` itself is just the dispatch seam: it owns the worker config,
catches capability errors so a single item never crashes the pool, and returns a
status dict. Adding a behaviour means adding a capability module + one registry
line — never editing this file.
"""
from __future__ import annotations

import logging
from typing import Any

from silica.agent.run_context import RunContext
from silica.config import CONFIG
from silica.capabilities import CAPABILITIES, Capability
from silica.kernel.workqueue import DedupFamily, DedupJob, ExpandJob, WorkItem

logger = logging.getLogger(__name__)


def _item_provenance(item: Any) -> tuple[str, str, str] | None:
    """(source key, content sha, ledger run id) of the work item.

    dedup carries the inbox path, so its key is the path under the inbox
    (ADR-0114); expand carries only the op's source_basename, which the ledger
    reads as that file when the vault holds one of the name. The run id
    is the FSM's own `progress.run_id` — the key CLEANUP stamps and the key the
    dangling-link sweep and check_renucleate read back. It is NOT the undo
    journal's run id, which is a separate uuid4 keyspace.
    """
    job = item.context
    if isinstance(job, DedupFamily):
        # One batch is always one file — the sha it carries is
        # fsm.current_content_hash — so the first member answers for all.
        job = next((m for m in job.members if m.inbox_file), job.members[0])
    if isinstance(job, ExpandJob):
        src = job.inbox_file or job.op.get("source_basename") or ""
    elif isinstance(job, DedupJob):
        src = job.inbox_file
    else:
        return None
    from silica.kernel.base.manifest import source_key

    return (source_key(src), job.content_hash, job.run_id) if src and job.content_hash else None


def consume(wq: Any, agent: "BoundedSubAgent", stop: Any = None, undo_run: Any = None) -> None:
    """One consumer thread: claim → handle → complete until the queue closes.

    THE consumer loop — shared by the Coordinator's in-run pool and the ad-hoc
    ``run_subagent_batch`` path, so cancel semantics, bookkeeping, and bus
    events live in exactly one place.  Blocks at OS level on ``wq.claim()`` —
    no polling; the sentinel injected by ``wq.close()`` cascades through all
    consumers so they all wake and exit cleanly.  ``stop`` (optional Event) is
    checked before each item so a producer crash or user cancel causes pending
    items to be marked cancelled rather than dispatched.
    """
    from silica.obs.bus import BUS
    from silica.kernel.write.gate import in_run
    from silica.obs.events import WorkCancelledEvent
    from silica.obs import narration as _narr_mod

    while True:
        item = wq.claim()               # blocks; no timeout, no polling
        if item is None:
            return                      # sentinel received — queue fully drained
        if (stop is not None and stop.is_set()) or item.cancel_token.is_set():
            wq.complete(item, "cancelled", {"status": "cancelled", "reason": "cancel_token"})
            BUS.publish(
                "work/cancelled",
                WorkCancelledEvent(item.id, item.kind, "pre_handle"),
            )
            _narr_mod.NARRATOR.narrate(
                "subagent", "cancelled", f"{item.kind} cancelled at pre_handle",
                {"kind": item.kind, "phase": "pre_handle"}, id=item.id)
            continue
        # The subagent span (spec §4): this worker thread starts with an empty
        # context, so attach makes every inner beat (calls, writes) carry
        # item.id as parent — the attribution that keeps parallel appends
        # unambiguous.
        _narr_mod.NARRATOR.span_open(
            "subagent", item.id, f"{item.kind} {item.target_path}".strip(),
            {"kind": item.kind}, attach=True)
        try:
            with in_run(provenance=_item_provenance(item),
                        undo=undo_run() if undo_run is not None else None):
                res = agent.handle(item)
        except BaseException:
            _narr_mod.NARRATOR.span_close("subagent", item.id, "failed",
                                          f"{item.kind} crashed", {"kind": item.kind})
            raise
        wq.complete(item, res.get("status", "done"), res)
        status = res.get("status", "done")
        mapped = {"error": "failed", "cancelled": "cancelled"}.get(status, "done")
        # `done` carries the queue's richer verdict (no_merge/skipped/…) in the
        # summary: the axis stays three-valued, the truth stays visible.
        _narr_mod.NARRATOR.narrate(
            "work", mapped, f"{item.kind} {status}",
            {"kind": item.kind, "status": status}, id=f"wk-{item.id}")
        _narr_mod.NARRATOR.span_close(
            "subagent", item.id, mapped,
            f"{item.kind} {status}", {"kind": item.kind, "status": status})


def run_subagent_batch(
    items: list[WorkItem],
    config: Any = CONFIG,
    *,
    max_workers: int | None = None,
    cancel_token: Any = None,
) -> dict[str, Any]:
    """Run a batch of WorkItems through leashed sub-agents in parallel.

    Used by the ad-hoc /dedup, /enrich commands
    (out of the inject pipeline).  A pre-closed WorkQueue drained by the shared
    ``consume`` loop — the exact engine the Coordinator runs in-pipeline — so
    both paths get identical cancel/bookkeeping semantics.  BoundedSubAgent is
    stateless beyond its config, so one instance is safely shared across
    threads; commit_ops serialises same-note writes via path_lease.
    """
    from concurrent.futures import ThreadPoolExecutor

    from silica.kernel.workqueue import WorkQueue

    if not items:
        return {"items": 0, "summary": {}, "results": []}

    if cancel_token is not None:
        for it in items:
            it.cancel_token = cancel_token

    mw = max(1, int(max_workers or config.subagent_max_concurrent or 3))
    agent = BoundedSubAgent(config)

    wq = WorkQueue()
    for it in items:
        wq.enqueue(it)
    wq.close()

    # One undo-journal run for the whole batch so /revert can undo the entire
    # /enrich or /dedup.
    from silica.kernel.write.gate import open_run
    worker = RunContext.of_run(undo=open_run(f"{items[0].kind}-batch")).spawn(consume)

    with ThreadPoolExecutor(max_workers=mw, thread_name_prefix="subagent") as ex:
        futures = [ex.submit(worker, wq, agent, cancel_token) for _ in range(mw)]
    for f in futures:
        exc = f.exception()
        if exc:
            logger.warning("sub-agent consumer crashed: %s", exc)

    return {
        "items": len(items),
        "summary": wq.summary(),
        "results": [
            {"target": it.target_path, **(it.result or {"status": it.status})}
            for it in items
        ],
    }


class BoundedSubAgent:
    """Dispatches a WorkItem to the capability registered under its kind."""

    def __init__(
        self,
        config: Any = CONFIG,
        capabilities: dict[str, Capability] | None = None,
    ):
        self.config = config
        # Injected registry defaults to the global one, so production is
        # unchanged while tests supply a fake registry without mutating state.
        self._capabilities = capabilities if capabilities is not None else CAPABILITIES

    def handle(self, item: WorkItem) -> dict[str, Any]:
        run = self._capabilities.get(item.kind)
        if run is None:
            return {"status": "skipped", "reason": f"no capability for kind '{item.kind}'"}
        try:
            return run(item, self.config)
        except Exception as e:  # never let a sub-agent crash the pool
            logger.warning("Capability '%s' error on item %s: %s", item.kind, item.id, e)
            return {"status": "error", "error": str(e)}
