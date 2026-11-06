# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Capability registry — THE dispatch seam for background work.

A *capability* is a self-contained background behaviour: a plain
``run(item, config) -> dict`` function that claims a WorkItem of one ``kind`` and
executes it under that behaviour's bounds. One ``kind`` is owned by exactly one
capability, so dispatch is a keyed lookup — the same shape as the ``TOOLS``
table — not a scan. Adding a behaviour is: drop one module here and add one
line to ``CAPABILITIES``.

Everything that runs in the background flows through this registry:

  * in-run WorkItems produced by the Injector/Coordinator (dedup, orphan),
  * ad-hoc batches from /dedup, /enrich (via ``run_subagent_batch``).

The registry is the ONLY dispatch table. The execution engine is always
``BoundedSubAgent`` + the shared consumer loop in ``silica/agent/subagent.py``;
FSM pipelines (injector/refiner/organizer) are deterministic foreground flows
and intentionally stay outside this seam.
"""
from __future__ import annotations

from typing import Any, Callable

from silica.kernel.workqueue import WorkItem
from silica.capabilities.dedup import run_dedup
from silica.capabilities.expand import run_expand
from silica.capabilities.enrich import run_enrich
from silica.capabilities.orphan import run_orphan

# A capability runs one WorkItem under its leash and returns a status dict.
Capability = Callable[[WorkItem, Any], dict]


CAPABILITIES: dict[str, Capability] = {
    "dedup": run_dedup,
    "expand": run_expand,
    "enrich": run_enrich,
    "orphan": run_orphan,
}
