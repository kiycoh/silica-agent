# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Pipeline tools — what the model can reach of the deferred-ops path.

The bodies are silica.router.anneal's, below the tools because the FSM's
boundary anneal calls them; the injector's own stages are plain functions the
FSM calls. The full run lives in silica.tools.runners, exposed as
silica_run_injector.
"""
from __future__ import annotations

from typing import Annotated

from typing import Any

from pydantic import Field

from silica.router import anneal
from silica.tools import tool


@tool(cls="composed", collapse="eager")
def silica_deferred_retry(content_hash: Annotated[str, Field(description='Content hash of the deferred bundle to retry (from silica_deferred_list)')]) -> dict[str, Any]:
    """Retry writing a deferred op bundle: re-validates against the current vault,
    snapshots, writes the ops that now pass, and updates the bundle.

    - Ops that pass validation are written immediately.
    - Ops that still fail remain in the deferred store.
    - If the bundle is fully cleared, it is removed from the deferred store.
    """
    return anneal.retry_bundle(content_hash)


# collapse stays "lazy" (the default) on purpose: the verdict IS the feedback,
# and an eager stub would erase the rejection reasons from the very history the
# steer loop iterates on.
@tool(cls="composed", internal=True)
def submit_repaired_ops(content_hash: Annotated[str, Field(description='Hash of the deferred bundle being repaired (given in the repair task)')], ops: Annotated[list[dict], Field(description='Corrected ops in the standard op schema. Bodies are plain strings: real line breaks, single backslashes.')]) -> dict[str, Any]:
    """Validate and write repaired ops for a deferred bundle. Ops that pass the
    validator are written to the vault immediately and leave the bundle; ops
    that fail come back under "rejected" with the exact validator reason.
    Correct the rejected ops and resubmit them; never resubmit an op listed
    under "written" or "renamed" (renamed = written, but under a heading no
    parked op carries, so the original stays deferred).
    """
    return anneal.repair_bundle(content_hash, ops)


@tool(cls="composed", collapse="eager")
def silica_anneal(steer: Annotated[bool, Field(description="After the mechanical pass, hand each bundle's still-failing ops to the escalation model (a bounded repair loop per bundle)")] = False, limit: Annotated[int, Field(description='Max bundles to process (0 = all)')] = 0) -> dict[str, Any]:
    """Boundary annealing: sweep EVERY deferred bundle through the mechanical
    retry (re-validate against the current vault, write what now passes), then
    with steer=True hand each bundle's still-failing ops to the escalation
    model in a bounded repair loop per bundle, steered by the per-op
    ``rejection_reason`` stamps and the validator's live verdicts.
    """
    return anneal.sweep_deferred(steer=steer, limit=limit)
