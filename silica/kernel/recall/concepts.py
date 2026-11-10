# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's concepts as the co-occurrence graph holds them: a term's
neighbourhood and the notes that carry it, or a note's own top concepts.

silica_concepts' payload, said here and not in the tool because Explore's drawer
reads it too, and a surface reaches the domain, never a tool shaped for the model.
"""
from __future__ import annotations

from typing import Any


def lookup(term: str = "", note: str = "", k: int = 10) -> dict[str, Any]:
    """`note=` for its concepts by weight, else `term=` for its canonical label,
    centrality, neighbours and carriers; `k` bounds both lists. An `error` key
    when the store is missing or empty, or neither is given."""
    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.text.text import stem_word

    try:
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
    except Exception as e:
        return {"error": f"co-occurrence store unavailable ({e}) — run silica_cooccurrence_refresh"}
    if len(store) == 0:
        return {"error": "co-occurrence index empty. Run silica_cooccurrence_refresh first."}

    if note:
        # The note's own concept vector is already stored per note by the index —
        # no extraction, no LLM. note_nodes() applies store_key to the path itself.
        from silica.driver import DRIVER

        try:
            path = DRIVER.read_note(note).ref.path or note
        except Exception:
            path = note  # unresolved: the index may still know the reader's spelling
        nodes = store.note_nodes(path)
        top = sorted(nodes.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        out: dict[str, Any] = {
            "note": note,
            "concepts": [{"concept": store.node_label(s), "weight": c} for s, c in top],
        }
        if not top:
            out["hint"] = (
                f"'{note}' has no entry in the co-occurrence index — check the name/path, "
                "or run silica_cooccurrence_refresh if the note is new."
            )
        return out

    if not term.strip():
        return {"error": "pass either term= (concept neighbourhood) or note= (a note's concepts)."}

    stem = stem_word(term.strip().lower(), lang=store.lang)
    neighbors = store.neighbors(term, k=k)

    # Concept -> notes inverted lookup, ranked by contribution count. Read the
    # cached stem postings directly (O(df log df)) instead of an O(N) vault scan.
    from silica.kernel.workspaces import in_scope

    posting = {p: c for p, c in store.stem_postings().get(stem, {}).items() if in_scope(p)}
    notes = sorted(posting.items(), key=lambda kv: (-kv[1], kv[0]))[:k]

    out = {
        "term": term,
        "concept": store.node_label(stem),
        "centrality": round(sum(store.adjacency().get(stem, {}).values()), 1),
        "neighbors": neighbors,
        "notes": [{"path": p, "count": c} for p, c in notes],
    }
    if not neighbors and not notes:
        out["hint"] = (
            f"'{term}' is not a concept node in the co-occurrence graph — "
            "concepts are single content words; try another word, or run "
            "silica_cooccurrence_refresh if the index is stale."
        )
    return out
