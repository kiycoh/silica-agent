# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Answer-time perception — the one assembly of recalled memory into context.

Validated on the LongMemEval perception grid (frozen corpus A, 2026-07-14):
facts-first episodic block + per-note query-densest window + rank/evidence/date
headers. The LME harness consumes perceive() directly, so the eval and the
product cannot diverge on this seam — the measured number belongs to Silica.

Kernel rule: no ``datetime.now()`` here — ``now`` is supplied by the caller
(the tool layer passes today, the eval adapter passes the simulated question
date).

Failure behavior: the episodic lane is additive and best-effort (a broken
store never blocks answering); retrieval errors propagate — a silently empty
context would score as a memory miss with no signal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ponytail: perception-grid winners as plain defaults; promote to CONFIG only
# when a real vault needs different values.
DEFAULT_K = 15
# Window grid decided 2026-07-30 (bench/window_sweep_150.json + the paired A/B in
# bench/ab_win_*.metrics.json). 3x1000 beats the old 1x3000 on answer accuracy:
# 0.520 vs 0.427 over the same 150 LME questions and the SAME retrieved blocks
# (rerank carries its own _WINDOW_CHARS, so the render window cannot move
# ranking), McNemar exact p=0.0336, 26 questions won against 12 lost. Uniform
# NARROWING was the losing move — every 1xN cell below 3000 lost gold and cost
# 12-28pp on some question type; splitting the same budget across more windows
# is what wins. k stays 15: probe_recall_rank showed the rank tail carries gold.
WINDOW_CHARS = 1000
DEFAULT_WINDOWS = 3
FACTS_K = 10


@dataclass
class NoteBlock:
    """One recalled note, ready for the prompt."""
    path: str       # store-keyspace rel path (no .md)
    date: str       # frontmatter `date`, '' when absent
    evidence: str   # joined per-leg provenance ("embed:0.83 cooccur:w9"), '' in --stuff
    body: str       # full body, frontmatter stripped
    excerpt: str    # query-densest window of the body
    contested: str | None = None  # correction reason when flagged, else None
    abstract: bool = False  # served at the L0 tier (rank tail / already served)


# L0 abstract shape (OpenViking doc_overview, leaner): enough headings to show
# the note's skeleton, one paragraph to say what it is about.
L0_CAP_CHARS = 400
L0_MAX_HEADINGS = 8


def l0_excerpt(body: str, *, cap_chars: int = L0_CAP_CHARS,
               max_headings: int = L0_MAX_HEADINGS) -> str:
    """Extractive L0 abstract of a note body: heading tree + first paragraph.

    Deterministic and LLM-free by verdict, not convenience — distill-vs-verbatim
    and the extractive-ingest arm both scored extractive over generated text.
    Serves the rank tail (and already-served notes) so a hit keeps its slot at
    a fraction of the window cost; the model re-reads on demand via `partial`.
    """
    import re

    if not body or not body.strip():
        return ""
    headings = re.findall(r"^#{1,6}[ \t].+$", body, flags=re.MULTILINE)
    lines = headings[:max_headings]
    paragraph = ""
    for block in re.split(r"\n\s*\n", body):
        candidate = "\n".join(
            ln for ln in block.strip().splitlines()
            if not ln.lstrip().startswith("#")
        ).strip()
        if candidate:
            paragraph = candidate
            break
    out = "\n".join(part for part in ("\n".join(lines), paragraph) if part)
    return out[:cap_chars].strip()


def _apply_tiers(blocks: list[NoteBlock], *, deep_ranks: int | None,
                 served: set[str]) -> None:
    """Degrade the rank tail and already-served notes to the L0 tier in place.

    Rank counts the FINAL order (what the model sees as #n). A block whose L0
    comes back empty keeps its window — an empty excerpt would be dropped as
    contentless, and the slot exists precisely because the tail carries gold.
    """
    for rank, b in enumerate(blocks, 1):
        beyond = deep_ranks is not None and rank > deep_ranks
        if not beyond and b.path not in served:
            continue
        l0 = l0_excerpt(b.body)
        if l0:
            b.excerpt = l0
            b.abstract = True


@dataclass
class Perception:
    """perceive()'s result: render() is the prompt string, the rest is telemetry."""
    query: str
    facts_block: str = ""
    fact_hits: list = field(default_factory=list)    # episodic.FactHit
    fact_chains: list = field(default_factory=list)  # per-hit supersede chain (episodic.Fact)
    blocks: list[NoteBlock] = field(default_factory=list)

    def render(self, *, facts_first: bool = True, windowed: bool = True,
               stale: dict[str, str] | None = None) -> str:
        """The context string. Defaults are the validated perception; the flags
        exist as A/B arms for the eval harness (legacy layouts).

        `stale` maps note_path (.md-suffixed, codedocs.peek's shape) to change
        level; a matching block's header gains a stale:<level> token, because
        the model answers from this string and a side map alone never reaches
        it."""
        parts: list[str] = []
        for rank, b in enumerate(self.blocks, 1):
            # block paths are store-keyspace (no .md); peek keys carry .md
            lvl = (stale.get(b.path) or stale.get(b.path + ".md")) if stale else None
            if windowed:
                head = f"[#{rank}" + (f" | {b.evidence}" if b.evidence else "")
                head += (f" | dated {b.date}" if b.date else "")
                head += (f" | contested: {b.contested}" if b.contested else "")
                head += (f" | stale:{lvl}" if lvl else "")
                # L0-tier block: the marker tells the model this is a summary
                # and the note holds more (re-read via `partial`).
                head += (" | abstract" if b.abstract else "") + "]"
                parts.append(f"{head}\n{b.excerpt}")
            else:
                marks = ([f"dated {b.date}"] if b.date else []) \
                    + ([f"contested: {b.contested}"] if b.contested else []) \
                    + ([f"stale:{lvl}"] if lvl else [])
                head = f"[{' | '.join(marks)}]\n" if marks else ""
                parts.append(f"{head}{b.body}")
        ctx = "\n\n---\n\n".join(parts)
        if not self.facts_block or not ctx:
            return self.facts_block or ctx
        return (f"{self.facts_block}\n\n---\n\n{ctx}" if facts_first
                else f"{ctx}\n\n---\n\n{self.facts_block}")


def facade_retrieve(query: str, *, k: int, use_embedder: bool = True,
                    use_rerank: bool = True, use_recall_weights: bool = False,
                    use_lexical: bool = False):
    """Fused first-stage retrieval + cross-encoder rerank for a fresh text query.

    The single retrieval path shared by the chat tools
    (silica_semantic_search) and perceive() — and therefore by
    the eval adapter. Both lanes (active vault + personal memory, ADR-0019) are
    queried; a down leg abstains to the survivor.

    Returns ``(results, query_vec)``: results is the RelatedNote list ([] for
    no hits), or None when no leg is available at all (no query embedding AND
    no co-occurrence index in either lane). query_vec is surfaced for reuse —
    episodic fact recall scores against the same vector.

    ``use_recall_weights`` (phase 1 of `improve`, LoCoMo eval-only): when True,
    folds the vault's recall-outcome weights in as an extra fusion leg. False
    (the default) leaves the retrieval path byte-identical for every other
    caller.

    ``use_lexical`` (default off, opt-in like ``use_recall_weights``): when
    True, folds the hand-written BM25/fuzzy leg into fusion as an extra leg.
    Abstains when the lexical index is absent or empty.
    """
    from silica.agent.providers import get_embedder, get_reranker
    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.recall.embed import get_store
    from silica.kernel.recall.memory_lane import memory_stores
    from silica.kernel.recall.relatedness import related_notes_for_query
    from silica.kernel.recall.rerank import rerank_related
    from silica.kernel.recall.sync import sweep

    # Out-of-band freshness: hand-edits (Obsidian, rm, git) land in the
    # indexes before this query reads them. Debounced, never raises.
    sweep()

    embed_store = get_store()
    try:
        cooccur_store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        if len(cooccur_store) == 0:
            cooccur_store = None
    except Exception:
        cooccur_store = None
    mem_embed, mem_cooccur = memory_stores()

    query_vec = None
    if use_embedder and (len(embed_store) > 0 or mem_embed is not None):
        try:
            query_vec = get_embedder(CONFIG).embed([query])[0]
        except Exception:
            query_vec = None  # embed leg abstains; co-occurrence may still carry

    if query_vec is None and cooccur_store is None and mem_cooccur is None:
        return None, None

    recall_rank = None
    if use_recall_weights:
        from silica.kernel.recall.recall_weights import ranking

        recall_rank = ranking()

    lexical_rank = None
    if use_lexical:
        from silica.kernel.recall.lexical import get_lexical_store

        lexical_rank = get_lexical_store().rank(query, k=k) or None

    results = related_notes_for_query(
        query_vec=query_vec,
        query_text=query,
        embed_store=embed_store,
        cooccur_store=cooccur_store,
        memory_embed_store=mem_embed,
        memory_cooccur_store=mem_cooccur,
        k=k,
        recall_rank=recall_rank,
        lexical_rank=lexical_rank,
    ) or []
    reranker = get_reranker(CONFIG) if use_rerank else None
    if reranker:
        # Default document path: gate 2b sees full body lengths, the scored
        # docs are query-densest windows, memory-lane bodies resolve by origin.
        results = rerank_related(reranker, query, results, k=k)
    return results, query_vec


def _read_dated_body(path: str, origin: str = "vault") -> tuple[str, str | None, str | None]:
    """(frontmatter date, contested reason, body) for one note; ('', None, None)
    when unreadable. `contested` is the note's flag reason (first `contradictions`
    entry) or None. origin='memory' resolves in the personal-memory vault (ADR-0019)."""
    if origin == "memory":
        from silica.kernel.recall.memory_lane import memory_vault

        mv = memory_vault()
        if mv is None:
            return "", None, None
        p = mv / (path if path.endswith(".md") else path + ".md")
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", None, None
    else:
        from silica.driver import DRIVER

        try:
            content = DRIVER.read_note(
                path if path.endswith(".md") else path + ".md").content or ""
        except Exception:
            return "", None, None
    from silica.kernel.write import frontmatter

    data, _raw, body = frontmatter.split(content)
    # data is None for a body-only note (no frontmatter) or a YAML error —
    # product notes from the FSM write path can lack frontmatter entirely.
    data = data or {}
    date = str(data.get("date") or "").strip()
    contested = None
    if data.get("contested"):
        refs = data.get("contradictions") or []
        contested = str(refs[0]) if refs else "contested"
    # `body` is the frontmatter-stripped text; for a body-only note split()
    # already returns the whole content as body. The old `or content` fallback
    # leaked YAML frontmatter into context whenever the body was empty (A7).
    return date, contested, body


def _recall_facts(perception: Perception, query: str, query_vec, *, now: str,
                  facts_k: int, episodic_ttl_days: int | None,
                  use_embedder: bool) -> None:
    """Fill the Personal-memory side of `perception`. Best-effort: additive
    evidence must never block answering (mirror of capture_from_distill)."""
    try:
        from silica.kernel.recall.episodic import EpisodicStore, render as render_facts

        store = EpisodicStore()
        if not store.live_facts():
            return
        if query_vec is None and use_embedder:
            try:
                from silica.agent.providers import get_embedder
                from silica.config import CONFIG

                query_vec = get_embedder(CONFIG).embed([query])[0]
            except Exception:
                query_vec = None  # lexical fact recall
        hits = store.recall(query, query_vec, k=facts_k, now=now,
                            ttl_days=episodic_ttl_days)
        if not hits:
            return
        perception.fact_hits = hits
        perception.fact_chains = [store.chain(h.fact) for h in hits]
        perception.facts_block = "Personal memory:\n" + render_facts(hits, store=store)
    except Exception as e:
        logger.warning("perceive: episodic recall failed (context continues): %s", e)


def _maybe_assemble(blocks: list[NoteBlock], *, assemble: bool, query: str) -> list[NoteBlock]:
    """Gate: assemble=False returns blocks untouched (bit-identical default)."""
    if not assemble or not blocks:
        return blocks
    return _assemble_blocks(blocks, query)


def _driver_neighbors(path: str):
    """`assembly.Neighbors` for one note, read live from DRIVER + cooccurrence.

    Keyspace note: seeds and `body_of`/`by_path` live in the store keyspace
    (no ".md"); `NoteRef.path` (children via backlinks, related via links)
    carries ".md", so it is stripped here to match. `parent` is transcribed
    as the raw `parent note` prop value (a NAME, not necessarily a store
    path) and `edges` as raw cooccurrence-store keys — both may not resolve
    through `body_of`; see the caller's keyspace concerns.
    """
    from silica.driver import DRIVER
    from silica.kernel.recall import assembly
    from silica.kernel.recall.cooccurrence import cooccur_key, get_cooccur_store
    from silica.config import CONFIG

    parent = None
    try:
        raw = (DRIVER.props_of(path) or {}).get("parent note") or ""
        parent = str(raw).strip().strip("[]").strip() or None
    except Exception:
        parent = None
    try:
        related = [r.path.removesuffix(".md") for r in DRIVER.links(path)]
    except Exception:
        related = []
    children: list[str] = []
    try:
        for b in DRIVER.backlinks(path):
            bp = (DRIVER.props_of(b.path) or {}).get("parent note") or ""
            if str(bp).strip().strip("[]").strip().lower() == _name_of(path).lower():
                children.append(b.path.removesuffix(".md"))
    except Exception:
        children = []
    edges: list[str] = []
    try:
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        row = store.note_edges_for(cooccur_key(path))
        edges = [p for p, _w in sorted(row.items(), key=lambda kv: (-kv[1], kv[0]))]
    except Exception:
        edges = []
    return assembly.Neighbors(parent=parent, children=children,
                              related=related, edges=edges)


def _name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _assembly_body(path: str) -> str:
    _date, _contested, body = _read_dated_body(path)
    return body or ""


def _assemble_blocks(blocks: list[NoteBlock], query: str) -> list[NoteBlock]:
    from silica.kernel.recall import assembly

    by_path = {b.path: b for b in blocks}

    def _body(p: str) -> str:
        # Seeds already carry the correctly-fetched body (right origin, memory
        # or vault, per _read_dated_body) on NoteBlock.body — assemble() calls
        # body_of() for every unit including seeds, and a re-read here would
        # default to origin="vault" and silently drop memory-lane seed bodies.
        # Only genuine periphery paths (not in by_path) fall back to a fresh read.
        seed = by_path.get(p)
        return seed.body if seed is not None else _assembly_body(p)

    res = assembly.assemble(
        [b.path for b in blocks],
        neighbors_of=_driver_neighbors,
        body_of=_body,
    )
    out: list[NoteBlock] = []
    for ab in res.blocks:
        head = by_path.get(ab.members[0])
        out.append(NoteBlock(
            path=ab.members[0],
            date=head.date if head else "",
            evidence=head.evidence if head else "",
            body=ab.text,
            excerpt=ab.text,   # assembled text is already budgeted
        ))
    return out


def perceive(query: str, *, now: str, k: int = DEFAULT_K,
             window_chars: int = WINDOW_CHARS, windows: int = DEFAULT_WINDOWS,
             facts_k: int = FACTS_K,
             episodic_ttl_days: int | None = None, with_facts: bool = True,
             use_embedder: bool = True, use_rerank: bool = True,
             paths: list[str] | None = None,
             use_recall_weights: bool = False,
             assemble: bool = False,
             use_lexical: bool = False,
             deep_ranks: int | None = None,
             served_before: set[str] | None = None) -> Perception:
    """Retrieve + assemble the answer-time context for `query`.

    ``paths`` skips retrieval and assembles the given notes in order (the eval
    adapter's --stuff arm, or a caller that already holds a shortlist);
    unreadable paths are skipped and ranks stay dense. ``episodic_ttl_days``:
    None = CONFIG default, 0 = never expire. ``use_recall_weights`` (phase 1 of
    `improve`, eval-only, default off) is forwarded to `facade_retrieve`; it
    has no effect when ``paths`` is set, since that bypasses retrieval.
    ``assemble`` (default off) folds each seed's 1-hop neighbours into a
    squashed, breadcrumbed block; no effect when ``paths`` is set (that
    bypasses retrieval).
    ``use_lexical`` (default off) forwards to `facade_retrieve`'s lexical leg;
    no effect when ``paths`` is set.
    ``deep_ranks`` (default None = off, byte-identical): ranks beyond it are
    served as an extractive L0 abstract instead of the query windows — the
    tail keeps its slot (the rank probe showed it carries gold) at a fraction
    of the cost. ``served_before``: paths degraded to L0 whatever their rank,
    because the reader already holds their body (cross-turn dedup). Both are
    gate-pending A/B arms; --assemble overrides them (it re-budgets blocks).
    """
    from silica.kernel.recall.rerank import best_windows

    query_vec = None
    if paths is not None:
        hits = [(p, "", "vault") for p in paths]
    else:
        results, query_vec = facade_retrieve(
            query, k=k, use_embedder=use_embedder, use_rerank=use_rerank,
            use_recall_weights=use_recall_weights, use_lexical=use_lexical)
        hits = [(r.path, " ".join(r.evidence), getattr(r, "origin", "vault"))
                for r in (results or [])]

    blocks: list[NoteBlock] = []
    for path, evidence, origin in hits:
        date, contested, body = _read_dated_body(path, origin)
        if body is None:
            continue
        excerpt = ("\n[…]\n".join(best_windows(body, query, window_chars, windows))
                   if query else body[:window_chars])
        if not excerpt.strip():
            continue  # empty body renders as a bare "[#n | evidence]" header, zero content
        blocks.append(NoteBlock(path=path, date=date, evidence=evidence,
                                body=body, excerpt=excerpt, contested=contested))
    # Correction loop: contested notes are demoted behind clean ones (stable),
    # never dropped — the render marks them so the answer step can distrust them.
    blocks = [b for b in blocks if not b.contested] + [b for b in blocks if b.contested]

    if deep_ranks is not None or served_before:
        _apply_tiers(blocks, deep_ranks=deep_ranks, served=served_before or set())

    if paths is None:
        blocks = _maybe_assemble(blocks, assemble=assemble, query=query)

    perception = Perception(query=query, blocks=blocks)
    if with_facts:
        _recall_facts(perception, query, query_vec, now=now, facts_k=facts_k,
                      episodic_ttl_days=episodic_ttl_days, use_embedder=use_embedder)
    return perception
