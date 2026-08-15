# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Orphan-connector capability — make an offered neighbour link to a lonely note.

The model may only choose among candidates that were actually offered; an
invented target is filtered out so we never create another dangling link.

Direction matters: an orphan is a note with in-degree 0, so the wikilink is
written INTO the chosen neighbour, pointing at the orphan. Writing it into the
orphan instead (what this did until 2026-08-14) only adds out-degree and leaves
the note orphaned — the undirected edge is the same either way, but only this
direction clears the metric the report and E(vault) actually read.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel

from silica.agent.commit import commit_ops
from silica.agent.bounds import orphan_bounds
from silica.kernel.vault_manifest import active_write_dir, within
from silica.kernel.write.ops import Op, OpType
from silica.kernel.workqueue import WorkItem
from silica.capabilities._base import emit_feedback, load_prompt, read_or_skip

logger = logging.getLogger(__name__)


class OrphanLinkDecision(BaseModel):
    links: list[str] = []
    rationale: str = ""


def run_orphan(item: WorkItem, config: Any) -> dict[str, Any]:
    target = item.target_path
    candidates = item.context.get("candidates", [])  # [{"name":..., "path":...}]
    if not candidates:
        return {"status": "no_candidates"}

    emit_feedback(item, "reading")
    body, skip = read_or_skip(target)
    if skip is not None:
        return skip

    if item.cancel_token.is_set():
        return {"status": "cancelled"}

    emit_feedback(item, "calling_llm")
    decision = _decide_links(config, target, body[:8000], candidates)
    # Only keep links that were actually offered as candidates — never let the
    # model invent a target (which would just create another dangling link).
    by_name = {c.get("name", ""): c.get("path", "") for c in candidates}
    valid = [n for n in dict.fromkeys(decision.links) if by_name.get(n)]
    if not valid:
        return {"status": "no_link", "rationale": decision.rationale}

    if item.cancel_token.is_set():
        return {"status": "cancelled"}

    emit_feedback(item, "committing")
    title = os.path.splitext(os.path.basename(target))[0]
    hub = item.context.get("hub")
    # The candidate paths come from the relatedness facade, whose keyspace is
    # cooccur_key: '.md'-stripped. The driver reads either spelling, but the op
    # path is what gets written, so it has to be the real note.
    neighbours = list(dict.fromkeys(
        p if p.endswith(".md") else p + ".md" for n in valid if (p := by_name[n])
    ))
    # This patches pre-existing notes anywhere in the vault (hence the empty
    # target_dir below, which lifts validate's landing-folder gate), so it
    # enforces the write boundary itself exactly as backlink_pass does: on a
    # vault that reads a whole source tree, a de-orphaning patch must never
    # edit its README.
    write_root = active_write_dir()
    if write_root:
        neighbours = [p for p in neighbours if within(p, write_root)]
    if not neighbours:
        return {"status": "no_link", "rationale": "no candidate inside the write boundary"}

    snippet = f"## Related\n\n- [[{title}]]\n"
    ops = [
        Op(
            op=OpType.patch,
            heading="Related",
            # Per-orphan, not a flat "orphan": the provenance block keyed by
            # (heading, source) is what makes a re-patch idempotent, and a
            # popular neighbour is the best candidate for many orphans. A
            # shared key would let the first one land and silently skip the
            # rest as duplicates.
            source_basename=f"orphan:{title}",
            path=p,
            snippet=snippet,
            hub=hub,
            reason=f"orphan connect: {decision.rationale[:120]}",
        )
        for p in neighbours
    ]
    bounds = orphan_bounds(neighbours, orphan_title=title, hub=hub)
    result = commit_ops(ops, target_dir="", hub=hub, bounds=bounds)
    result.setdefault("linked", valid)
    return result


def _decide_links(
    config: Any,
    target_path: str,
    body: str,
    candidates: list[dict],
) -> OrphanLinkDecision:
    from silica.agent.providers import get_provider
    from silica.kernel.text.sanitize import parse_json

    prompt = load_prompt("orphan_prompt.txt")
    cand_block = "\n".join(
        f"{i+1}. {c.get('name', c.get('path', '?'))}"
        for i, c in enumerate(candidates)
    )
    user_message = (
        f"{prompt}\n\n---\nORPHAN NOTE ({target_path}):\n{body}\n\n"
        f"---\nCANDIDATES:\n{cand_block}\n"
    )
    provider = get_provider(config, role="worker")
    response = provider.call_llm(
        messages=[{"role": "user", "content": user_message}],
        tools=None,
        response_schema=OrphanLinkDecision,
        max_tokens=512,
    )
    raw = response.text or ""
    try:
        parsed, _ = parse_json(raw, strict=False)
        if isinstance(parsed, dict):
            links = parsed.get("links", [])
            return OrphanLinkDecision(
                links=[str(x) for x in links] if isinstance(links, list) else [],
                rationale=str(parsed.get("rationale", "")),
            )
    except Exception as e:
        logger.debug("orphan link decision parse failed: %s", e)
    return OrphanLinkDecision(links=[], rationale="unparseable decision")
