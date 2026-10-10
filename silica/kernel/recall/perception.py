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

from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from silica.kernel.recall.cooccurrence import CooccurStore

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Perception-grid winners as plain defaults (config promotion declined
# 2026-08-19; revisit only when a real vault needs different values).
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
TOP_WINDOWED_RANKS = 5  # ranks past this render one window; see perceive()
ALL_NON_ACTIVE_RULE = ("[every recalled note is contested, under review, or superseded: "
                       "do not state their content as fact; name the dispute, or abstain]")
FACTS_K = 10

# A [[link]] inside a note body is not evidence that its target exists. On the
# study-plan replay (docs/evaluation/study-plan-rubric-2026-09-22.md) the model
# copied five such links, seen 3 to 6 times each across the turn's recalls,
# into its answer, while the note that did exist for the concept sat at rank
# 15. The brackets stay: the model must learn that the NAME is not a note, not
# merely lose a link. Embeds (`![[...]]`) are attachments, never marked.
_WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
NO_NOTE = " (no such note)"
# A fenced block (to its close, or the end of an answer cut mid-fence) or an
# inline code span: text a reader runs or copies, never a link to mark.
_CODE_SPAN = re.compile(r"```.*?(?:```|\Z)|`[^`\n]+`", re.S)
LINK_LEGEND = ('[legend: a wikilink followed by "(no such note)" names a note that '
               "does not exist in this vault; link only notes you saw delivered]")


@dataclass
class NoteBlock:
    """One recalled note, ready for the prompt."""
    path: str       # store-keyspace rel path (no .md)
    date: str       # frontmatter `date`, '' when absent
    evidence: str   # joined per-leg provenance ("embed:0.83 cooccur:w9"), '' in --stuff
    body: str       # full body, frontmatter stripped
    excerpt: str    # query-densest window of the body
    contested: str | None = None  # correction reason when flagged, else None
    section: str = ""  # heading chain above the first window ("A > B"), '' at top
    builds_on: str = ""  # rendered prereqs ("A, B"), set only by study order (G6)
    origin: str = "vault"  # "memory" = personal-memory lane (ADR-0019): the path
    #                        resolves in ANOTHER vault, so read_note denies it
    documents: list[str] = field(default_factory=list)  # `documents:` frontmatter, repo-relative
    grounding: str = ""  # "grounded: 2/3 spans" | "grounding: n/a" | "" when the ledger has no row
    trust: str = ""  # contested.trust; "" from a stub. Header names it only when not "human"
    lifecycle: str = "active"  # contested.lifecycle. Header names it only when not active/contested
    non_active: bool = False  # lifecycle counts against it: demoted, one window, may trigger abstain
    whole: bool | None = None  # the excerpt covered the whole body BEFORE link marking:
    #                            the tool's `partial` reads this, never excerpt != body,
    #                            because the "(no such note)" marker edits the excerpt of a
    #                            note served whole. None = derive from the fields (a block
    #                            built by hand, as the tests and the flat arm do).
    title: str = ""  # frontmatter `title` when it differs from the path stem: the identity
    #                  a raw import (Inbox/giovanni_castelli) does not carry in its path.
    #                  Never the first H1: measured 2026-09-22 on the test vault, 372 of 728
    #                  notes open with a section H1 ("Definizione di …"), so that rule
    #                  would mislabel half the vault to name 15 imports.


    def __post_init__(self) -> None:
        if self.whole is None:
            self.whole = self.excerpt.strip() == self.body.strip()


@dataclass
class Perception:
    """perceive()'s result: render() is the prompt string, the rest is telemetry."""
    query: str
    orientation: str = ""  # G2: vault map block, filled only by orient=True
    facts_block: str = ""
    fact_hits: list = field(default_factory=list)    # episodic.FactHit
    fact_chains: list = field(default_factory=list)  # per-hit supersede chain (episodic.Fact)
    blocks: list[NoteBlock] = field(default_factory=list)
    filtered: int = 0  # blocks an epistemic filter (min_trust / lifecycle) dropped
    all_non_active: bool = False  # every block is contested/review/superseded (M3): abstain rule leads
    unresolved: int = 0  # wikilinks marked "(no such note)": the legend leads the render only when > 0

    def render(self, *, facts_first: bool = True, windowed: bool = True,
               stale: dict[str, str] | None = None,
               plain_headers: bool = False) -> str:
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
                # Path + section chain give the excerpt its place in the vault
                # (graft G1): a window that starts mid-section otherwise reaches
                # the model with no anchor at all — the header carried only rank
                # and score provenance. Chunk text is untouched (the query-side
                # variant of 2608.00824), so the embed index never rebuilds.
                if plain_headers:
                    # Pre-G1 header (rank + provenance only): the A/B legacy
                    # arm, same standing as flat_context/facts_last.
                    head = f"[#{rank}"
                else:
                    head = f"[#{rank} | {b.path}"
                    head += (f" | title: {b.title}" if b.title else "")
                    head += (f" | sec: {b.section}" if b.section else "")
                    head += (f" | builds-on: {b.builds_on}" if b.builds_on else "")
                head += (f" | {b.evidence}" if b.evidence else "")
                # The files this note documents, so a code question answered
                # from prose still names the file to open: code itself is not
                # in the recall index (955 vectors = 955 notes), and until now
                # the binding only ever fed `stale`.
                head += (f" | documents: {', '.join(b.documents)}" if b.documents else "")
                head += (f" | {b.grounding}" if b.grounding else "")
                head += (f" | dated {b.date}" if b.date else "")
                # A person's active note is the silent default: only the
                # deviation is a fact the model must weigh, and a hand-written
                # vault keeps the exact prompt the G1 header was measured on.
                head += (f" | trust: {b.trust}" if b.trust and b.trust != "human" else "")
                head += (f" | lifecycle: {b.lifecycle}"
                         if b.lifecycle not in ("active", "contested") else "")
                head += (f" | contested: {b.contested}" if b.contested else "")
                head += (f" | stale:{lvl}" if lvl else "") + "]"
                parts.append(f"{head}\n{b.excerpt}")
            else:
                marks = ([f"dated {b.date}"] if b.date else []) \
                    + ([f"trust: {b.trust}"] if b.trust and b.trust != "human" else []) \
                    + ([f"lifecycle: {b.lifecycle}"]
                       if b.lifecycle not in ("active", "contested") else []) \
                    + ([f"contested: {b.contested}"] if b.contested else []) \
                    + ([f"stale:{lvl}"] if lvl else [])
                head = f"[{' | '.join(marks)}]\n" if marks else ""
                parts.append(f"{head}{b.body}")
        ctx = "\n\n---\n\n".join(parts)
        if not self.facts_block or not ctx:
            out = self.facts_block or ctx
        else:
            out = (f"{self.facts_block}\n\n---\n\n{ctx}" if facts_first
                   else f"{ctx}\n\n---\n\n{self.facts_block}")
        # Orientation leads (G2): a map after the evidence reads as more
        # evidence; before it, it frames what the evidence is a sample OF.
        if self.orientation and out:
            out = f"{self.orientation}\n\n---\n\n{out}"
        if self.unresolved and windowed and out:
            out = f"{LINK_LEGEND}\n\n{out}"
        # The rule leads everything (M3): the model reads this string and
        # nothing else, and a note it never sees cannot be doubted. Same
        # branch as "not in the vault": the answer is the dispute, or none.
        if self.all_non_active and out:
            out = (ALL_NON_ACTIVE_RULE + "\n\n" + out)
        return out


def _peek_dir(vault: str | None) -> str | None:
    """The resolved folder a `vault=` peek reads, or None for a plain call: no
    vault named, or the active vault named (then the active path, with its
    sweep and its singletons, is the right one)."""
    if vault is None or not vault.strip():
        return None
    from silica.config import CONFIG

    p = Path(vault).expanduser().resolve()
    active = (getattr(CONFIG, "vault_path", "") or "").strip()
    if active and Path(active).resolve() == p:
        return None
    return str(p)


def facade_retrieve(query: str, *, k: int, use_embedder: bool = True,
                    use_rerank: bool = True, rerank_stats: dict | None = None,
                    use_memory: bool = True, vault: str | None = None,
                    recall_rank: list[tuple[str, float]] | None = None,
                    lexical_rank: list[tuple[str, float]] | None = None):
    """Fused first-stage retrieval + cross-encoder rerank for a fresh text query.

    The single retrieval path shared by the chat tools
    (silica_semantic_search) and perceive() — and therefore by
    the eval adapter. Both lanes (active vault + personal memory, ADR-0019) are
    queried; a down leg abstains to the survivor.

    Returns ``(results, query_vec)``: results is the RelatedNote list ([] for
    no hits), or None when no leg is available at all (no query embedding AND
    no co-occurrence index in either lane). query_vec is surfaced for reuse —
    episodic fact recall scores against the same vector.

    ``recall_rank`` and ``lexical_rank`` are extra pre-ranked legs the CALLER
    computed, fused as additional RRF rankings. The product never builds them:
    both are eval arms (`evals/recall_arms.py`), and passing the ranking rather
    than a boolean is what keeps their sources out of this module while the
    harness still measures this function and not a copy of it.

    ``rerank_stats`` is an optional out-dict filled with ``{"reranked": bool}``
    (see ``rerank_related``): the returned ``.score`` is a cross-encoder
    relevance when True and a first-stage fusion cosine when False, and the two
    are an order of magnitude apart. A caller that shows the number, or
    thresholds on it, has to know which one it got.

    ``vault`` (peek): the path of ANOTHER adopted vault whose stores stand in
    for the active legs, read-only and read as they lie on disk. The memory
    lane still applies under ``use_memory``; results carry that folder as
    their ``origin`` so body readers open the right files. The active vault's
    singletons, sweep and CONFIG are never touched — this is the memory lane
    (ADR-0019) pointed where the caller says, not a vault switch.
    """
    from silica.agent.providers import get_embedder, get_reranker
    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.recall.embed import get_store
    from silica.kernel.recall.memory_lane import memory_stores, memory_vault
    from silica.kernel.recall.relatedness import related_notes_for_query
    from silica.kernel.recall.rerank import rerank_related
    from silica.kernel.recall.sync import sweep

    peek = _peek_dir(vault)
    cooccur_store: CooccurStore | None
    if peek is None:
        # Out-of-band freshness: hand-edits (Obsidian, rm, git) land in the
        # indexes before this query reads them. Debounced, never raises.
        sweep()

        embed_store = get_store()
        try:
            loaded = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
            cooccur_store = loaded if len(loaded) else None  # empty store ⇒ abstain
        except Exception:
            cooccur_store = None
    else:
        # No sweep for a peek: the sweep reconciles the ACTIVE vault's indexes
        # with its disk and must not seed a foreign one from here. A cold
        # peeked index answers nothing; `vault_registry.coverage` says so.
        from silica.kernel.recall.memory_lane import stores_for

        embed_store, cooccur_store = stores_for(peek)
    # ADR-0032: lane scope is caller intent. False = "this vault only" — the
    # memory legs are never loaded, fusion is bit-identical to single-vault.
    mem_embed, mem_cooccur = memory_stores() if use_memory else (None, None)
    if peek is not None and memory_vault() == Path(peek):
        # Peeking AT the memory vault: it already fills the primary legs, and
        # the same store on both lanes would double every RRF term.
        mem_embed = mem_cooccur = None

    query_vec = None
    if use_embedder and ((embed_store is not None and len(embed_store) > 0)
                         or mem_embed is not None):
        try:
            query_vec = get_embedder(CONFIG).embed([query], query=True)[0]
        except Exception:
            query_vec = None  # embed leg abstains; co-occurrence may still carry

    if query_vec is None and cooccur_store is None and mem_cooccur is None:
        return None, None

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
    if peek is not None:
        # Fusion marks the primary legs "vault", which every body reader takes
        # to mean the ACTIVE vault. Stamp the peeked folder before rerank reads
        # bodies: a wrong-vault read scores as irrelevant and buries the peek.
        for r in results:
            if r.origin == "vault":
                r.origin = peek
    reranker = get_reranker(CONFIG) if use_rerank else None
    if rerank_stats is not None:
        rerank_stats["reranked"] = False  # no reranker configured ⇒ cosines stand
    if reranker:
        # Default document path: gate 2b sees full body lengths, the scored
        # docs are query-densest windows, memory-lane bodies resolve by origin.
        results = rerank_related(reranker, query, results, k=k, stats=rerank_stats)
    return results, query_vec


def _read_dated_body(path: str, origin: str = "vault") -> tuple[
        str, str | None, str | None, list[str], str, str, bool]:
    """(frontmatter date, contested reason, body, documents, trust, lifecycle,
    weak) for one note; ('', None, None, [], '', 'active', False) when
    unreadable. `weak` marks a contest raised only by agents' flags
    (contested.contested_by_agents_only): labelled, never decisive (M3).
    `contested` is the note's flag reason (first `contradictions` entry) or
    None; `documents` the repo files the note documents (`documents:`
    frontmatter, [] when none); `trust` and `lifecycle` the two derived
    epistemic axes (contested.trust / contested.lifecycle, spec M2), read off
    the same parse so the header costs no second read. origin='memory'
    resolves in the personal-memory vault (ADR-0019); an absolute-path origin
    resolves in that peeked vault."""
    if origin != "vault":
        from silica.kernel.recall.memory_lane import foreign_root

        root = foreign_root(origin)
        if root is None:
            return "", None, None, [], "", "active", False
        p = root / (path if path.endswith(".md") else path + ".md")
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", None, None, [], "", "active", False
    else:
        from silica.driver import DRIVER

        try:
            content = DRIVER.read_note(
                path if path.endswith(".md") else path + ".md").content or ""
        except Exception:
            return "", None, None, [], "", "active", False
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
    from silica.kernel.recall.paths import SOURCES_MARKER
    from silica.kernel.write.contested import lifecycle as note_lifecycle
    from silica.kernel.write.contested import trust as note_trust

    from silica.kernel.write.contested import contested_by_agents_only

    life = note_lifecycle(data)
    weak = life == "contested" and contested_by_agents_only(
        [str(r) for r in (data.get("contradictions") or [])])
    return (date, contested, body, frontmatter.documents_in(content),
            note_trust(content, has_source_leaf=SOURCES_MARKER in (body or "")),
            life, weak)


def _recall_facts(perception: Perception, query: str, query_vec, *, now: str,
                  facts_k: int, episodic_ttl_days: int | None,
                  use_embedder: bool, mark_used: bool = False) -> None:
    """Fill the Personal-memory side of `perception`. Best-effort: additive
    evidence must never block answering (mirror of capture_from_distill)."""
    try:
        from silica.kernel.recall.episodic import EpisodicStore, render as render_facts

        store = EpisodicStore()
        if not store.live_facts():
            return
        embedder = query_model = None
        if use_embedder:
            try:
                from silica.agent.providers import get_embedder
                from silica.config import CONFIG
                from silica.kernel.recall.embed import embedder_identity

                embedder = get_embedder(CONFIG)
                # The upstream query_vec came from this same configured embedder
                # (facade_retrieve), so its identity names that vector's space too.
                query_model = embedder_identity(embedder)
            except Exception:
                embedder = None  # no identity: recall falls back to the width check
        if query_vec is None and embedder is not None:
            try:
                query_vec = embedder.embed([query], query=True)[0]
            except Exception:
                query_vec = None  # lexical fact recall
        hits = store.recall(query, query_vec, k=facts_k, now=now,
                            ttl_days=episodic_ttl_days, query_model=query_model)
        if not hits:
            return
        perception.fact_hits = hits
        perception.fact_chains = [store.chain(h.fact) for h in hits]
        perception.facts_block = "Personal memory:\n" + render_facts(hits, store=store)
        if mark_used:
            store.mark_used([h.fact.id for h in hits], on=now)
    except Exception as e:
        logger.warning("perceive: episodic recall failed (context continues): %s", e)














def _grounding_labels() -> dict[str, str]:
    """`{note_key: header token}` from the provenance ledger, read once per
    perceive (the ledger read is memoized on mtime). A note the gate never
    scored has no token; a scored note with nothing checkable says `n/a`,
    because on the paraphrasing profile only math and code are gateable and
    a 100% over zero spans would be the false confidence the spec forbids.
    Vault lane only: a peeked or memory vault has its own ledger."""
    from silica.kernel.write.provenance import grounding_by_note

    out: dict[str, str] = {}
    for key, g in grounding_by_note().items():
        total = int(g.get("spans") or 0)
        if total <= 0:
            out[key] = "grounding: n/a"
        else:
            out[key] = f"grounded: {total - int(g.get('ungrounded') or 0)}/{total} spans"
    return out


def _resolver():
    """ref -> bool for the active vault: a path, a stem, a title (mindmap's
    resolver) or a frontmatter alias (the driver's alias index; the graph
    resolver alone would mark every alias link, which Obsidian resolves).
    None on any failure: a context without markers is the pre-2026-09-22
    context, never a crash on the answer path."""
    try:
        from silica.driver import DRIVER
        from silica.kernel.recall.mindmap import note_resolver

        base = note_resolver()
        aliases = {a.strip().lower() for _p, al in DRIVER.alias_index() for a in al}
    except Exception:
        return None
    return lambda ref: base(ref) is not None or ref.strip().lower() in aliases


def _declared_title(path: str) -> str:
    """Frontmatter `title` of an active-vault note, '' when absent or unreadable.
    One more frontmatter read per block; the body reader returns a fixed tuple
    that three test fakes and one eval build by hand."""
    try:
        from silica.driver import DRIVER

        props = DRIVER.props_of(path if path.endswith(".md") else path + ".md") or {}
    except Exception:
        return ""
    return str(props.get("title") or "").strip()


def _mark_unresolved(text: str, exists) -> tuple[str, int]:
    """Append NO_NOTE to every wikilink whose target does not exist; (text, n)."""
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        if exists(m.group(1)):
            return m.group(0)
        n += 1
        return m.group(0) + NO_NOTE
    return _WIKILINK.sub(sub, text), n


def declare_missing_links(text: str) -> str:
    """The answer-boundary twin of the context marker: every `[[link]]` in a
    chat answer that names no note gets "(no such note)". Transparency, not
    repair (Astra, 2026-09-22): the sentence still says what the model
    advised, and the reader sees that the note is not there. Fail-open."""
    exists = _resolver()
    if exists is None or "[[" not in text:
        return text
    # Code is not prose: a mermaid node `A[[Subroutine]]` reads as a wikilink,
    # and the mark broke the fence for mermaid's parser in 4 of 48 /diagram
    # answers (ADR-0082). Only the prose between code spans is marked.
    out, pos = [], 0
    for m in _CODE_SPAN.finditer(text):
        out += [_mark_unresolved(text[pos:m.start()], exists)[0], m.group(0)]
        pos = m.end()
    return "".join(out) + _mark_unresolved(text[pos:], exists)[0]


def _section_chain(body: str, offset: int, depth: int = 3) -> str:
    """Markdown heading chain still open at `offset` ("Training > Gradients").
    Deepest `depth` levels only — the nearest heading carries the most anchor
    per token, and a full chain on a deeply nested note is header bloat.
    '' above the first heading, so headingless notes cost zero tokens."""
    chain: list[tuple[int, str]] = []
    for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", body[:offset], re.MULTILINE):
        lvl = len(m.group(1))
        while chain and chain[-1][0] >= lvl:
            chain.pop()
        chain.append((lvl, m.group(2)))
    return " > ".join(t for _lvl, t in chain[-depth:])


def perceive(query: str, *, now: str, k: int = DEFAULT_K,
             window_chars: int = WINDOW_CHARS, windows: int = DEFAULT_WINDOWS,
             facts_k: int = FACTS_K,
             episodic_ttl_days: int | None = None, with_facts: bool = True,
             use_embedder: bool = True, use_rerank: bool = True,
             paths: list[str] | None = None,
             use_memory: bool = True,
             vault: str | None = None,
             rerank_stats: dict | None = None,
             recall_rank: list[tuple[str, float]] | None = None,
             lexical_rank: list[tuple[str, float]] | None = None,
             folder: str | None = None,
             min_trust: str | None = None,
             lifecycle: str | None = None,
             mark_used: bool = False) -> Perception:
    """Retrieve + assemble the answer-time context for `query`.

    ``mark_used`` is for the answer path only (silica_recall): the episodic
    facts this context hands the model count as used on ``now`` and their TTL
    restarts (ADR-0057). Off by default, so evals, probes and replays that call
    perceive() measure the store without keeping any fact alive.

    ``min_trust`` / ``lifecycle`` are the epistemic filters (spec M2): a
    trust floor (`contested.TRUST_ORDER`) and an exact lifecycle. Both are
    off by default and never reorder; a filter drops blocks AFTER retrieval,
    so what it costs is recall (the rank tail carried gold on the rank
    probe), and ``Perception.filtered`` says how much was dropped so the
    caller can tell the model it saw less.

    ``folder`` keeps only notes under that vault subtree. Retrieval over-fetches
    3k and cuts to k after the filter, so a scoped call still fills its slots;
    no index is rebuilt. This is the lever `memory=False` is not: on a repo
    question the market-research notes (35% of this vault) took 6 of 15 slots
    by co-occurrence, and they live in the ACTIVE vault.

    ``rerank_stats`` is `facade_retrieve`'s out-dict, forwarded untouched: the
    tool surface has to tell a reranked ordering from a fusion one, and this
    was the only hop where that bit got lost (measured 2026-09-03: recall
    answered for a day with :1235 down and nothing in the reply said so).

    ``recall_rank``/``lexical_rank`` are forwarded to `facade_retrieve`
    untouched: extra ranked legs the caller computed, never built here.

    ``paths`` skips retrieval and assembles the given notes in order (the eval
    adapter's --stuff arm, or a caller that already holds a shortlist);
    unreadable paths are skipped and ranks stay dense. ``episodic_ttl_days``:
    None = CONFIG default, 0 = never expire. ``vault`` (default None) peeks at another
    adopted vault — see `facade_retrieve`; blocks then carry that folder as
    ``origin``, and assembly stays off because it walks the ACTIVE vault's
    link graph.
    """
    from silica.kernel.recall.rerank import best_window_spans, window_weights

    query_vec = None
    if paths is not None:
        hits = [(p, "", "vault") for p in paths]
    else:
        results, query_vec = facade_retrieve(
            query, k=k * 3 if folder else k, use_embedder=use_embedder,
            use_rerank=use_rerank, use_memory=use_memory, vault=vault,
            rerank_stats=rerank_stats, recall_rank=recall_rank,
            lexical_rank=lexical_rank)
        if folder:
            from silica.kernel.recall.paths import in_folder

            # Memory-lane paths resolve in another vault, so a folder of THIS
            # vault cannot contain them.
            results = [r for r in (results or [])
                       if getattr(r, "origin", "vault") != "memory"
                       and in_folder(r.path, folder)][:k]
        hits = [(r.path, " ".join(r.evidence), getattr(r, "origin", "vault"))
                for r in (results or [])]

    # One idf map per query, shared by every note's window scan (graft G3);
    # {} — no lexical index — keeps the scan bit-identical to the unweighted one.
    wts = window_weights(query) if query else {}
    grounding = _grounding_labels()
    from silica.kernel.write.provenance import note_key
    blocks: list[NoteBlock] = []
    exists = _resolver()
    unresolved = 0
    seen_bodies: dict[int, int] = {}  # hash(body + epistemic state) -> index in blocks
    for rank, (path, evidence, origin) in enumerate(hits, 1):
        date, contested, body, documents, trust, life, weak = _read_dated_body(path, origin)
        if body is None:
            continue
        # A byte-identical body is the same file twice (a mineru intermediate
        # `x/hybrid_auto/x.md` beside `x.md` took 2 of 15 slots with the same
        # excerpt): one block, at the shallower path, in the rank it first
        # earned. The index keeps both on purpose: hiding one at the driver
        # would also hide it from the graph and from the links that name it.
        # The epistemic state and the origin are part of the key: a reviewed
        # copy and the active note it shadows share a body and are two pieces
        # of evidence, and a memory-lane twin lives in another vault, so its
        # path must never replace a vault path (silica_read_note would open
        # the wrong vault).
        if (key := hash((origin, body, date, contested, trust, life))) in seen_bodies:
            kept = blocks[seen_bodies[key]]
            if path.count("/") < kept.path.count("/"):
                kept.path = path
            continue
        non_active = life != "active" and not weak
        # Cutting k was refuted (ranks 9-15 carried 9 gold answers on the
        # rank probe), so the token saving comes from the tail's window count
        # instead: k=15 at 3 windows is 42k chars (~10.6k tokens, measured
        # 2026-09-03); one window past rank 5 keeps the tail and bounds the
        # context at ~25k. The head keeps its multi-window span, which is
        # where the multi-window spec measured its gain.
        # A non-active note is served with one window wherever it ranked
        # (M3): it is evidence of a dispute, not the answer's main text.
        n = 1 if non_active else (windows if rank <= TOP_WINDOWED_RANKS else 1)
        spans = (best_window_spans(body, query, window_chars, n, wts, snap=True)
                 if query else [(0, body[:window_chars])])
        excerpt = "\n[…]\n".join(s for _p, s in spans)
        if not excerpt.strip():
            continue  # empty body renders as a bare "[#n | evidence]" header, zero content
        whole = excerpt.strip() == body.strip()
        if exists is not None and origin == "vault":
            excerpt, n_marked = _mark_unresolved(excerpt, exists)
            unresolved += n_marked
        title = _declared_title(path) if origin == "vault" else ""
        if title.lower() == path.rsplit("/", 1)[-1].lower():
            title = ""  # the path already says it
        seen_bodies[key] = len(blocks)
        blocks.append(NoteBlock(path=path, date=date, evidence=evidence,
                                body=body, excerpt=excerpt, contested=contested,
                                section=_section_chain(body, spans[0][0]), title=title, whole=whole,
                                origin=origin, documents=documents,
                                grounding=(grounding.get(note_key(path), "")
                                           if origin == "vault" else ""),
                                trust=trust, lifecycle=life, non_active=non_active))
    filtered = 0
    if min_trust or lifecycle:
        from silica.kernel.write.contested import TRUST_ORDER

        floor = TRUST_ORDER.get(min_trust or "", 0)
        passing = [b for b in blocks
                   if TRUST_ORDER.get(b.trust, 0) >= floor
                   and (not lifecycle or b.lifecycle == lifecycle)]
        filtered, blocks = len(blocks) - len(passing), passing
    # Correction loop: non-active notes (contested by a person or the judge,
    # under review, superseded) are demoted behind active ones (stable), never
    # dropped — the render marks them so the answer step can distrust them.
    # An agent's own flag stays in place: labelled, not decisive (M3).
    blocks = [b for b in blocks if not b.non_active] + [b for b in blocks if b.non_active]
    perception = Perception(query=query, blocks=blocks, filtered=filtered, unresolved=unresolved,
                            all_non_active=bool(blocks) and all(b.non_active for b in blocks))
    # use_memory=False means "this vault only": the episodic store homes in the
    # memory vault with no abstain rule of its own (episodic.py), so the facts
    # block is the same foreign lane through a second door and goes dark with it.
    if with_facts and use_memory:
        _recall_facts(perception, query, query_vec, now=now, facts_k=facts_k,
                      episodic_ttl_days=episodic_ttl_days, use_embedder=use_embedder,
                      mark_used=mark_used)
    return perception
