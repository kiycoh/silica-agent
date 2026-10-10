# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Orphan-connector capability — make an offered neighbour link to a lonely note.

The model may only choose among candidates that were actually offered; an
invented target is filtered out so we never create another dangling link.

A typed decision sits in front of the LLM call (ADR-0044, second site): one
noul per candidate when `SILICA_DECISION_MODEL` is set, the LLM call on any
non-answer.

Direction matters: an orphan is a note with in-degree 0, so the wikilink is
written INTO the chosen neighbour, pointing at the orphan. Writing it into the
orphan instead (what this did until 2026-08-14) only adds out-degree and leaves
the note orphaned — the undirected edge is the same either way, but only this
direction clears the metric the report and E(vault) actually read.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel

from silica.agent.commit import commit_ops
from silica.agent.decide import decide
from silica.agent.bounds import orphan_bounds
from silica.kernel.vault_manifest import active_write_dir, within
from silica.kernel.write.ops import Op, OpType
from silica.kernel.workqueue import WorkItem
from silica.capabilities._base import emit_feedback, load_prompt, read_or_skip

logger = logging.getLogger(__name__)


class OrphanLinkDecision(BaseModel):
    links: list[str] = []
    rationale: str = ""


# Strictly above this the typed verdict links a candidate. Measured 2026-09-27
# (docs/evaluation/decision-sites-jev-kev-2026-09-27.md: 40 orphans x 5
# candidates on the test vault, jev-1.13), at most 3 links per orphan: 0.7
# gives precision 0.986 / recall 0.545, 0.6 gives 0.942 / 0.604, the LLM call
# 0.881 / 0.388. The stricter cut because a wrong yes patches a note the owner
# wrote, while a wrong no only leaves the orphan for the next report.
LINK_ACCEPT = 0.7
LINK_QUESTION = {
    "link": {
        "type": "noul",
        "instructions": ("Should the candidate note link to the orphan note? Yes only when the two "
                         "are genuinely, topically related: a reader of the candidate would want "
                         "the orphan next."),
    },
}
# The state the gate measured: the orphan's head, the candidate's opening.
_ORPHAN_HEAD = 2000
_CANDIDATE_OPENING = 600
_WIKILINK = re.compile(r"\[\[([^\]|]+\|)?([^\]]+)\]\]")


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def typed_state(orphan_path: str, orphan_text: str, candidate_path: str,
                candidate_text: str) -> dict:
    """One (orphan, candidate) state. The candidate's wikilinks are flattened
    to their text: on the gate a bracketed link to the orphan would have
    answered the question, and flattening all of them keeps the state the one
    that was measured (evals/probe_decision_sites.py builds it here)."""
    return {
        "orphan_note": {"title": _stem(orphan_path),
                        "body": _strip_frontmatter(orphan_text)[:_ORPHAN_HEAD]},
        "candidate_note": {"title": _stem(candidate_path),
                           "opening": _WIKILINK.sub(r"\2", _strip_frontmatter(candidate_text))[:_CANDIDATE_OPENING]},
    }


def run_orphan(item: WorkItem, config: Any) -> dict[str, Any]:
    from silica.kernel.recall.paths import is_staging_path

    target = item.target_path
    # A staging note (inbox, done/) is bookkeeping, not knowledge: the report
    # does not count it as an orphan, and the owner ruled on 2026-09-27 that
    # the orphan pass leaves staging alone both ways. The candidate side is
    # filtered where candidates are made (curate.link_candidates).
    if is_staging_path(target):
        return {"status": "skipped", "reason": "staging note"}
    candidates = item.context.get("candidates", [])  # [{"name":..., "path":...}]
    if not candidates:
        return {"status": "no_candidates"}

    emit_feedback(item, "reading")
    body, skip = read_or_skip(target)
    if skip is not None:
        return skip

    if item.cancel_token.is_set():
        return {"status": "cancelled"}

    decision = _decide_typed(target, body, candidates)
    if decision is None:
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


def _decide_typed(target: str, body: str, candidates: list[dict]) -> OrphanLinkDecision | None:
    """The typed verdicts, or None and the LLM call runs (ADR-0044).

    One noul per candidate, each on its own (orphan, candidate) state, in
    parallel: the shape the gate measured. A non-answer on any candidate hands
    the whole orphan to the LLM; an uncertain answer does not. On the gate,
    routing orphans with a p in (0.4, 0.6] to the LLM sent 24 of 40 there and
    cut the right pairs from 142 to 127 of 200: the LLM is the weaker arm here,
    unlike the form sniff, so a valid low p is a no.
    """
    from silica.config import CONFIG

    if not CONFIG.decision_model:
        return None  # off: no candidate reads for a verdict nobody asks for

    def ask(c: dict) -> float | None:
        path = c.get("path", "")
        text, skip = read_or_skip(path)
        if skip is not None:
            return None
        try:
            answers = decide(typed_state(target, body, path, text), LINK_QUESTION)
        except Exception as exc:
            # decide() never raises by contract. Guarded anyway: an exception
            # escaping a worker would kill the orphan's repair instead of
            # letting the LLM call decide it.
            logger.warning("orphan decision failed (%s): LLM path", exc)
            return None
        return answers["link"]["noul"] if answers else None

    with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as ex:
        ps = list(ex.map(ask, candidates))
    verdicts = " ".join(f"{c.get('name', '?')}={'-' if p is None else f'{p:.2f}'}"
                        for c, p in zip(candidates, ps))
    if any(p is None for p in ps):
        logger.info("orphan decision %s: %s, non-answer, LLM path", target, verdicts)
        return None
    # `p is not None` holds for every p here (a None returned above); it is
    # spelled out so the scores type as floats
    chosen = sorted(((p, c) for p, c in zip(ps, candidates) if p is not None and p > LINK_ACCEPT),
                    key=lambda pc: -pc[0])[:3]
    logger.info("orphan decision %s: %s, taken", target, verdicts)
    return OrphanLinkDecision(
        links=[c.get("name", "") for _, c in chosen],
        rationale=f"typed decision over {LINK_ACCEPT}: "
                  + (", ".join(f"{c.get('name', '?')} {p:.2f}" for p, c in chosen) or "no candidate"),
    )


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
