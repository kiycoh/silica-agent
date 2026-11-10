# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Pipeline tools: what the model can reach of the deferred-ops path.

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


@tool(cls="composed", collapse="eager", ingress=False, label=("deferred retry", None))
def silica_deferred_retry(content_hash: Annotated[str, Field(description='Content hash of the deferred bundle to retry (from silica_deferred_list)')]) -> dict[str, Any]:
    """Retry writing a deferred op bundle: re-validates against the current vault,
    snapshots, writes the ops that now pass, and updates the bundle.

    - Ops that pass validation are written immediately.
    - Ops that still fail remain in the deferred store.
    - If the bundle is fully cleared, it is removed from the deferred store.
    """
    return anneal.retry_bundle(content_hash)


@tool(cls="composed", collapse="eager", ingress=False, label=("anneal", None))
def silica_anneal(steer: Annotated[bool, Field(description="After the mechanical pass, hand each bundle's still-failing ops to the escalation model (a bounded repair loop per bundle)")] = False, limit: Annotated[int, Field(description='Max bundles to process (0 = all)')] = 0) -> dict[str, Any]:
    """Boundary annealing: sweep EVERY deferred bundle through the mechanical
    retry (re-validate against the current vault, write what now passes), then
    with steer=True hand each bundle's still-failing ops to the escalation
    model in a bounded repair loop per bundle, steered by the per-op
    ``rejection_reason`` stamps and the validator's live verdicts.
    """
    return anneal.sweep_deferred(steer=steer, limit=limit)
