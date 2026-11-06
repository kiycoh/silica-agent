# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A search by meaning, and what a retrieval reply says about itself: the legs it
ran without and the notes it should doubt.

Below the tools, because /find, Explore and the Zotero rim search too, and they
were importing the tool module for a private function.
"""
from __future__ import annotations

from typing import Any

# The degraded line, one per leg: `<prefix>: the <role> <phrase><fix>` (ADR-0094
# point 5). The prefix says what the answer lost, not which server failed: the
# reader judges the answer, and "lexical only" is what changes how to read it.
_LEG = {"embed": ("lexical only", "embedder", "SILICA_EMBEDDING_TIMEOUT", "embedding_timeout"),
        "rerank": ("not reranked", "reranker", "SILICA_RERANK_TIMEOUT", "rerank_timeout")}
_PHRASE = {"unreachable": "is unreachable", "loading": "is still loading",
           "rejected": "refused the model name", "failed": "failed"}
# Where to fix it, per surface. The GUI names its own panels; the REPL has none,
# so both Settings pointers become the doctor; a tool result can be read by
# either, so it names both. Loading has no fix: it ends.
_FIX = {
    "gui": {"unreachable": "start it in Settings, Endpoints",
            "diagnose": "Settings, Diagnostics names the cause"},
    "repl": {"unreachable": "run silica doctor", "diagnose": "run silica doctor"},
    "tool": {"unreachable": "start it in Settings, Endpoints, or run silica doctor",
             "diagnose": "Settings, Diagnostics names the cause, or run silica doctor"},
}


def degraded_lines(degraded: dict, surface: str = "gui") -> list[str]:
    """One line per leg this call or turn ran without, the embedder first."""
    from silica.config import CONFIG

    lines = []
    for leg in ("embed", "rerank"):
        cause = degraded.get(leg)
        if cause is None:
            continue
        prefix, role, knob, timeout_attr = _LEG[leg]
        if cause == "slow":
            phrase, fix = f"did not answer in {getattr(CONFIG, timeout_attr):g} s", f"raise {knob}"
        else:
            phrase = _PHRASE.get(cause, "failed")
            fix = {"unreachable": _FIX[surface]["unreachable"],
                   "loading": ""}.get(cause, _FIX[surface]["diagnose"])
        lines.append(f"{prefix}: the {role} {phrase}" + (f" · {fix}" if fix else ""))
    return lines


# The third stale level, beside codedocs' "cosmetic" and "structural": the
# note was distilled from a source version that has since been re-nucleated.
_STALE_SOURCE = "source"


def stale_map() -> dict[str, str]:
    """`note_path.md -> level` for every note a reader should doubt.

    Two ledgers, one flag. codedocs.peek answers for code notes after a commit;
    the provenance drift map answers for notes whose source was re-nucleated
    at another version. The code level wins when a note carries both: it is
    the more specific claim. Each leg is guarded on its own, so a ledger that
    cannot be read costs its flags and nothing else.
    """
    from silica.config import CONFIG

    out: dict[str, str] = {}
    try:
        from silica.kernel.write import provenance

        out.update({p: _STALE_SOURCE
                    for p in provenance.drift_map(vault_path=CONFIG.vault_path)})
    except Exception:
        pass  # flags are an aid, never a reason a search fails
    try:
        from silica.kernel.code import codedocs

        out.update(codedocs.peek(CONFIG.vault_path))
    except Exception:
        pass  # the code ledger's flags go, the search and the provenance flags stay
    return out


def stale_entry(stale_map: dict[str, str], r) -> dict:
    """{"stale": level} for a flagged result, {} otherwise. Memory-lane
    results are another vault (ADR-0019): never annotated, like cluster."""
    if r.origin == "memory" or not stale_map:
        return {}
    from silica.kernel.code import codedocs

    lvl = codedocs.peek_level(stale_map, r.path)
    return {"stale": lvl} if lvl else {}


def code_stale_map() -> dict[str, str]:
    """`stale_map` without its provenance leg: the stale peek map for the active
    vault; {} on any failure (spec §5). What silica_semantic_search, silica_recall
    and silica_related flag, so a note whose source was re-nucleated reads stale
    in silica_search_context alone. Named and not closed: one map would change
    what those tools return."""
    try:
        from silica.config import CONFIG
        from silica.kernel.code import codedocs

        return codedocs.peek(CONFIG.vault_path)
    except Exception:
        return {}


def degraded_fields(leg_stats: dict) -> dict[str, Any]:
    """`degraded` and its `hint` for a retrieval reply, {} when no leg failed.

    Spread right after `query` (ADR-0094 point 3): the GUI card keeps the first
    1,200 characters of a result and the median recall result is 22,567 long
    (2026-10-11), so a flag after `context` never reached the screen; a
    compaction stub keeps the head too.
    """
    degraded = leg_stats.get("degraded")
    if not degraded:
        return {}
    return {"degraded": dict(degraded), "hint": " · ".join(degraded_lines(degraded, "tool"))}


# First-stage pool for facade_search: over-fetch so dropping staging entries
# still leaves k notes. 20 is the rerank pool the gate is calibrated on.
_NOTE_POOL = 20


def facade_search(text: str, k: int, memory: bool = True, folder: str = "") -> dict[str, Any]:
    """Fused embeddings + co-occurrence search for a fresh text, then reranked.

    Shared core of silica_semantic_search, now routed
    through perception.facade_retrieve — the same retrieval path perceive()
    and the memory eval use. Returns ``{"results": [{path, name, score}, ...]}``
    or ``{"error": ...}`` when no index is available at all. The two legs
    abstain independently: an empty embedding index (or an offline embedder)
    still serves co-occurrence results, and vice versa — mirroring how
    perception and the run substrate consume the facade. (COLLISION does not:
    it routes on the plain cosine search, ADR-0030.)
    """
    from silica.kernel.recall.perception import facade_retrieve

    from silica.kernel.base.layout import is_inbox_path

    # Staging outranks the notes distilled from it: a raw lecture repeats every
    # term its notes split up, so it wins first-stage retrieval and then eats the
    # top-k. Measured on the ML vault: asking whether a concept had a note, the
    # inbox copy of the ASKING document answered its own question at score 1.0,
    # three times. The inbox stays indexed (staging is source material and
    # `silica_recall` still reaches it) — it just stops crowding the ranked list
    # this tool returns, so over-fetch, drop staging, then cut to k.
    from silica.model.providers import get_reranker
    from silica.config import CONFIG
    from silica.kernel.recall.rerank import rerank_related

    # Filter BEFORE the cross-encoder, not after: reranking the wide pool and
    # then dropping staging pays for documents that get thrown away, and on a
    # local GPU a 20-document batch OOMs outright. This way the reranker still
    # sees exactly k candidates, and they are all notes.
    # One record for both calls (ADR-0094): the query embed marks `embed` in
    # facade_retrieve, the cross-encoder marks `rerank` below. Kept apart, a
    # search that lost its embedder read `reranked: true` and looked healthy.
    from silica.kernel.workspaces import allow_keys

    rr: dict = {"reranked": False}
    # A chat opened in a workspace searches only its members (ADR-0112 point
    # 4), and the personal-memory lane is another vault, so it sits out.
    allow = allow_keys()
    results, _query_vec = facade_retrieve(text, k=max(k, _NOTE_POOL), use_rerank=False,
                                          use_memory=memory and allow is None, leg_stats=rr,
                                          allow=allow)
    if results is None:
        return {**degraded_fields(rr),
                "error": "No index available. Run silica_embed_refresh or silica_cooccurrence_refresh first."}
    # is_inbox_path answers for the ACTIVE vault's staging roots; a memory-lane
    # note whose vault happens to use the same folder name is not this vault's
    # staging, and dropping it here silently shrank the guest lane (ADR-0019).
    notes = [r for r in results
             if r.origin == "memory" or not is_inbox_path(r.path or "")]
    if folder:
        from silica.kernel.base.layout import in_folder

        # A folder of THIS vault never contains a memory-lane path.
        notes = [r for r in notes if r.origin != "memory" and in_folder(r.path or "", folder)]
    # Never answer empty because everything relevant was staged: if the vault has
    # nothing else to say, staging IS the answer. A folder scope is different:
    # empty there is the true answer, not a staging artefact.
    results = (notes or (results if not folder else []))[:k]
    reranker = get_reranker(CONFIG)
    if reranker:
        results = rerank_related(reranker, text, results, k=k, stats=rr)
    stale_map = code_stale_map()
    return {
        **degraded_fields(rr),
        # Which scale `score` is on. The cross-encoder abstains on pools it
        # cannot read, and the surviving first-stage cosines live around 0.03
        # where a rerank probability lives around 0.99 — same field, two scales.
        # Without this a caller reads 0.03 as "nothing here" when it means
        # "not scored". Never threshold across calls; compare within one.
        "reranked": bool(rr.get("reranked", False)),
        "results": [
            {
                "path": r.path,
                "name": r.name,
                "score": round(r.score, 4),
                # Origin marker (ADR-0019): only when the note is NOT in the
                # active vault, so single-vault payloads stay unchanged.
                **({"origin": "memory"} if r.origin == "memory" else {}),
                **stale_entry(stale_map, r),
            }
            for r in results
        ]
    }
