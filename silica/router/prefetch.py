# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Bounded prefetch pool for distiller LLM calls (Tier 1 speed).

The FSM stays single-threaded; only ``run_distiller`` (a pure network call
whose inputs are snapshotted at dispatch time) runs on this pool. An idx is
submittable exactly once for the lifetime of the pool: the DELEGATE handler
re-enters on steer retries, and a retry must run inline with steer context,
never as a re-dispatched fresh call.
"""
from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger(__name__)


class DistillPrefetcher:
    def __init__(self, max_workers: int):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="distill-prefetch"
        )
        self._futures: dict[int, Future] = {}
        self._started: set[int] = set()

    def submit(self, idx: int, fn: Callable[[], dict]) -> None:
        """Dispatch chunk ``idx``. Permanent no-op if idx was ever submitted."""
        if idx in self._started:
            return
        self._started.add(idx)
        self._futures[idx] = self._executor.submit(fn)

    def pop(self, idx: int) -> Future | None:
        """Take ownership of idx's future (or None if never/already popped)."""
        return self._futures.pop(idx, None)

    def __contains__(self, idx: int) -> bool:
        return idx in self._started

    def shutdown(self) -> None:
        """Cancel queued work; don't wait for in-flight calls.

        An in-flight HTTP call cannot be interrupted — it ends on its own
        deadline (DISTILLER_TIMEOUT caps it at 300 s worst case).
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._futures.clear()
