# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Recon of one inbox file against the vault: its concepts, and which notes
they already collide with.

Apart from kernel.text.recon, whose scoring keyphrase imports: this module
reaches the driver and the embedder, and keyphrase is held to deterministic
structure by the P2/DKB import contract.
"""
from __future__ import annotations

from silica.driver import DRIVER
from silica.kernel.text.recon import (
    collision_priority,
    is_title_match,
    mentions_whole_word,
    rank_hits,
)


def _recon_embedder():
    """Pool ranker for recon; None (=> mined-rank fallback) when unavailable.

    Module-level seam so tests can disable the network embedder (see conftest)
    and keep recon deterministic; production uses the real embedder.
    """
    try:
        from silica.model.providers import get_embedder
        from silica.config import CONFIG
        return get_embedder(CONFIG)
    except Exception:
        return None


def _same_note(ref_a, ref_b) -> bool:
    """One note, whichever way the two refs spell it: store keys, under the
    filesystem's own idea of case (normcase folds on Windows, nowhere else)."""
    import os

    from silica.kernel.base.notekey import store_key

    def norm(r):
        return os.path.normcase(store_key(r.path or r.name))
    return norm(ref_a) == norm(ref_b)


def _prefers(candidate, incumbent) -> bool:
    """True when `candidate` is the vault original and `incumbent` its mirror copy.

    Only under safe mode is a note ever present twice; everywhere else this
    answers False and grouping keeps its first hit exactly as before.
    """
    from silica.kernel.base.manifest import active_write_dir, within
    from silica.kernel.base.manifest import SAFE_WRITE_DIR

    if active_write_dir() != SAFE_WRITE_DIR:
        return False
    a, b = candidate.path or "", incumbent.path or ""
    return bool(a and b) and within(b, SAFE_WRITE_DIR) and not within(a, SAFE_WRITE_DIR)


def recon_file(inbox_file: str) -> dict:
    """Mechanical extraction of concepts from an inbox file and searching for collisions in the vault."""
    from silica.config import CONFIG
    from silica.kernel.text.keyphrase import extract_keyphrases

    try:
        nc = DRIVER.read_note(inbox_file)
    except RuntimeError:
        return {"error": f"File not found: {inbox_file}"}

    embedder = _recon_embedder()
    cands = extract_keyphrases(
        nc.content,
        lang=CONFIG.cooccurrence_lang, embedder=embedder,
    )

    concepts = [c.phrase for c in cands]
    if not concepts:
        return {"file": inbox_file, "collisions": [], "new_concepts": []}

    collisions = []
    new_concepts = []
    
    batch = DRIVER.search_context_batch(concepts)   # one eval instead of N
    for c in concepts:
        hits = batch.get(c, [])
        if not hits:
            new_concepts.append(c)
            continue
            
        # Group hits by ref
        from silica.kernel.base.layout import is_inbox_path
        grouped = {}
        for h in hits:
            # Casefolded: the archive is `Done` since 2026-08-23 while a vault
            # that predates the rename keeps its `done`, and an archived source
            # registered as a collision target dooms every downstream op.
            _p = (h.ref.path or '').replace('\\', '/').casefold()
            if '/done/' in _p or _p.startswith('done/'):
                continue
            # Inbox notes are staging, never collision targets: registering one
            # as vault_collision dooms every downstream op (patch-the-inbox is
            # forbidden, patch-the-right-note mismatches the expected collision).
            if h.ref.path and is_inbox_path(h.ref.path):
                continue
            if _same_note(h.ref, nc.ref):
                continue
            # Whole-word only (see mentions_whole_word): the driver matched a
            # substring, which is its contract, not this one's. ponytail: the
            # driver caps materialized hits at 20 per note BEFORE this filter,
            # so a note whose first 20 matching lines are all fragments hides a
            # later whole-word line; reopen if a recon run misses a collision
            # that `silica_search_context` shows on line 21 or beyond.
            if not mentions_whole_word(c, h.snippet):
                continue

            name = h.ref.name
            if name not in grouped:
                grouped[name] = {"ref": h.ref, "count": 0}
            elif _prefers(h.ref, grouped[name]["ref"]):
                # Safe mode puts a preview copy of a note beside the note. They
                # group under one name, so WHICH path answered was down to scan
                # order — and the model was shown its own unmerged draft as the
                # vault's state. The original always wins; the copy exists to be
                # pasted over it, not to stand in for it.
                grouped[name]["ref"] = h.ref
            grouped[name]["count"] += 1
            
        if not grouped:
            new_concepts.append(c)
            continue
            
        raw_hits = []
        for name, data in grouped.items():
            in_t = is_title_match(c, name)
            raw_hits.append({
                "path": data["ref"].path or data["ref"].name,
                "count": data["count"],
                "in_title": in_t
            })
            
        ranked = rank_hits(raw_hits)
        collisions.append({
            "name": c,
            "total_hits": sum(h["count"] for h in raw_hits),
            "best_match": "title" if ranked[0]["in_title"] else "body",
            "hits": ranked
        })
        
    collisions.sort(key=collision_priority)
    new_concepts.sort()
    
    return {
        "file": inbox_file,
        "collisions": collisions,
        "new_concepts": new_concepts,
    }
