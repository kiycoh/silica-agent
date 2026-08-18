# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Alias consolidation — propose the aliases nobody wrote down.

The alias door already exists: `aliases:` frontmatter → build_alias_map →
autolink. But it only opens for surfaces a human declared. This pass makes one
LLM call over the title index, receives {canonical title: [variant spellings]},
gates every variant with the same ambiguity rules the door itself enforces, and
records the survivors as frontmatter aliases of their canonical note. From then
on every autolink pass resolves those spellings for free.

Dry-run by default; apply=True commits each note through the write gate.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel, Field

from silica.tools import tool

logger = logging.getLogger(__name__)


def gate_alias_groups(
    proposals: dict,
    scoped_titles: list[str],
    all_title_lowers: set[str],
    taken_surfaces: set[str],
) -> dict[str, list[str]]:
    """Filter LLM-proposed alias groups down to what the alias door accepts.

    Mirrors build_alias_map's rules at write time instead of read time, so a
    variant the door would drop is never even written: the canonical must be a
    scoped, unambiguous title; a variant is dropped when shorter than 2 chars,
    equal to its canonical, colliding with ANY real note title, already a
    registered alias surface, or claimed by two canonicals in the proposal.
    """
    index = {t.lower(): t for t in scoped_titles}
    # surface(lower) → (canonical, first casing); contested surfaces drop whole
    claims: dict[str, tuple[str, str]] = {}
    contested: set[str] = set()

    for raw_canonical, raw_variants in (proposals or {}).items():
        canonical = index.get(str(raw_canonical or "").strip().lower())
        if canonical is None or not isinstance(raw_variants, (list, tuple)):
            continue
        for raw in raw_variants:
            if not isinstance(raw, str):
                continue  # model garbage: a number is never an alias surface
            surface = raw.strip()
            key = surface.lower()
            if (
                len(key) < 2
                or key == canonical.lower()
                or key in all_title_lowers
                or key in taken_surfaces
            ):
                continue
            prior = claims.get(key)
            if prior is not None:
                if prior[0] != canonical:
                    contested.add(key)
                continue
            claims[key] = (canonical, surface)

    out: dict[str, list[str]] = {}
    for key, (canonical, surface) in claims.items():
        if key not in contested:
            out.setdefault(canonical, []).append(surface)
    return {c: sorted(v) for c, v in out.items()}


def _propose_groups(titles: list[str], config: Any) -> dict:
    """One LLM call over the whole title list → {canonical: [variants]}."""
    from silica.agent.providers import get_provider
    from silica.kernel.text.sanitize import parse_json

    system_prompt = (
        "You maintain the alias vocabulary of a personal knowledge vault. "
        "You are precise: you only group surface forms that clearly denote "
        "the same concept."
    )
    user_prompt = (
        "Below is the list of note titles in a knowledge vault, one per line.\n"
        "Propose alias surface forms for these titles: alternative spellings, "
        "abbreviations, acronyms, expansions, or strict synonyms a reader would "
        "plausibly write in prose when referring to that exact note's concept.\n\n"
        "Rules:\n"
        "- Keys are EXACT titles copied from the list; values are arrays of "
        "variant surface forms.\n"
        "- Never propose a variant that is itself a title in the list — two "
        "existing notes are a merge question, not an alias.\n"
        "- Only high-confidence variants; most titles have none — skip them.\n"
        '- Return ONLY a JSON object: {"Title": ["variant", ...], ...}\n\n'
        "Titles:\n" + "\n".join(titles)
    )
    provider = get_provider(config, role="worker")
    response = provider.call_llm(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        tools=None,
        max_tokens=int(os.getenv("MAX_TOKENS", "32768")),
    )
    try:
        parsed, _ = parse_json(response.text or "", strict=False)
    except Exception as e:
        logger.warning("aliases: proposal parse failed: %s", e)
        return {}
    return parsed if isinstance(parsed, dict) else {}


class AliasesArgs(BaseModel):
    apply: bool = Field(
        default=False,
        description="If True, write the gated aliases into frontmatter; default is a dry-run that only returns the proposals.",
    )
    folder: str = Field(default="", description="Vault-relative folder to scope the pass (empty = whole vault)")


@tool(AliasesArgs, cls="composed")
def silica_aliases(apply: bool = False, folder: str = "", cancel_token: Any = None) -> dict[str, Any]:
    """Propose and record frontmatter aliases for existing note titles. One
    LLM call proposes variants; each passes the read-time ambiguity gate (no
    collision with a real title, single claimant, noise floor). Dry-run by
    default; apply=True writes `aliases:` through the write gate. Autolink
    resolves the new surfaces on future passes.
    """
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.link.autolink import build_title_index

    scoped_refs = DRIVER.list_files(folder)
    scoped_titles = build_title_index(scoped_refs)
    if not scoped_titles:
        return {"error": "no notes in scope"}
    # ponytail: one call over the whole scoped index; chunk-and-merge when a
    # vault beyond ~2k titles shows up here.
    all_refs = DRIVER.list_files("") if folder else scoped_refs
    all_title_lowers = {r.name.lower() for r in all_refs if getattr(r, "name", "")}

    taken_surfaces: set[str] = set()
    try:
        for _title, alias_list in DRIVER.alias_index():
            for a in alias_list or []:
                taken_surfaces.add(str(a).strip().lower())
    except Exception as e:
        logger.warning("aliases: alias_index unavailable (%s) — proceeding without", e)

    proposals = _propose_groups(scoped_titles, CONFIG)
    groups = gate_alias_groups(proposals, scoped_titles, all_title_lowers, taken_surfaces)

    proposed = sum(len(v) for v in proposals.values() if isinstance(v, (list, tuple)))
    accepted = sum(len(v) for v in groups.values())
    result: dict[str, Any] = {
        "apply": apply,
        "proposed": proposed,
        "accepted": accepted,
        "dropped": proposed - accepted,
        "groups": groups,
    }
    if not apply or not groups:
        return result

    from silica.agent.bounds import alias_consolidation_bounds
    from silica.agent.commit import commit_ops
    from silica.kernel.write.frontmatter import add_alias
    from silica.kernel.write.ops import Op, OpType

    path_by_title = {r.name.lower(): r.path for r in scoped_refs if getattr(r, "name", "")}
    written: dict[str, int] = {}
    skipped: list[dict] = []
    for canonical, variants in sorted(groups.items()):
        path = path_by_title.get(canonical.lower())
        if not path:
            skipped.append({"note": canonical, "reason": "path not found"})
            continue
        try:
            prior = DRIVER.read_note(path).content or ""
        except Exception as e:
            skipped.append({"note": canonical, "reason": f"unreadable: {e}"})
            continue
        content = prior
        for variant in variants:
            content = add_alias(content, variant)
        if content == prior:
            continue  # every variant already declared — idempotent re-run
        res = commit_ops(
            [Op(
                op=OpType.overwrite,
                heading=canonical,
                source_basename=os.path.basename(path),
                path=path,
                content=content,
                base_content=prior,
                reason=f"alias consolidation: {', '.join(variants)}",
            )],
            target_dir=os.path.dirname(path),
            bounds=alias_consolidation_bounds(path),
        )
        if res.get("status") == "committed":
            written[path] = len(variants)
        else:
            skipped.append({"note": canonical, "reason": str(res.get("status", "refused"))})

    result["written"] = written
    result["skipped"] = skipped
    return result
