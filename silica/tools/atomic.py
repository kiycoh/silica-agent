# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Atomic tools — L0 façades, 1:1 on Obsidian CLI commands.

From SILICA.md §4.2:
  Atomic tools are single Obsidian-native operations, 1:1 on a CLI command
  or a pure kernel function. They are the base vocabulary — called by both
  the agent and the pipeline.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from silica.driver import DRIVER
from silica.tools import tool


# ---------------------------------------------------------------------------
# Read / Discovery
# ---------------------------------------------------------------------------

# One cap for every listing that grows with the vault. ponytail: flat cap
# defends the context window (a 1000-note vault ≈ 20k tokens uncapped); no
# paging — narrowing by folder, or acting on a sample, covers the real uses.
_FILES_CAP = 200


class SearchArgs(BaseModel):
    query: str = Field(description="Text to search for in note names in the vault")

# Same unbounded shape as search_context, one level down: substring-over-names
# means a short query answers with the vault ("e" measured 575 paths / 41k chars
# on a 719-note vault). A name lookup that returns 500 names has answered
# nothing, so rank by how well the name matches and keep the head.
_SEARCH_CAP = 40


@tool(SearchArgs, cls="atomic")
def silica_search(query: str) -> dict:
    """Search for notes by NAME/title match. Returns the paths of matching notes.

    A note's wikilink name is its filename without the extension. For text
    inside note bodies use silica_search_context; for meaning-based search when
    you don't know the exact words use silica_semantic_search.

    Returns {paths, matched}: closest name match first, `truncated` when capped.
    """
    refs = DRIVER.search_names(query)
    q = query.casefold()

    def rank(ref) -> tuple:
        name = ref.name.casefold()
        # 0/1/2: exact, prefix, substring. Then shorter name (a 6-char hit on
        # an 8-char name is a better answer than on an 80-char one), then path.
        tier = 0 if name == q else (1 if name.startswith(q) else 2)
        return (tier, len(name), ref.path)

    ranked = sorted(refs, key=rank)
    out: dict = {"paths": [r.path for r in ranked[:_SEARCH_CAP]], "matched": len(ranked)}
    if len(ranked) > _SEARCH_CAP:
        out["truncated"] = (
            f"{len(ranked)} notes matched; showing the {_SEARCH_CAP} closest name "
            "matches. Narrow the query, or use silica_semantic_search to rank by meaning."
        )

    # Stale flags (spec-stale-triggers §3): read-only peek, so a search never
    # pays the git walk; at worst the first search after a commit has no flags.
    try:
        from silica.config import CONFIG
        from silica.kernel.code import codedocs

        m = codedocs.peek(CONFIG.vault_path)
        flagged = {p: lvl for p in out["paths"]
                   if (lvl := codedocs.peek_level(m, p))}
        if flagged:
            out["stale"] = flagged
    except Exception:
        pass  # flags are an aid, never a reason a search fails
    return out


class SearchContextArgs(BaseModel):
    query: str = Field(description="Text to search for within the content of vault notes")


# A literal grep over every body is unbounded by nature: one Hit per matching
# LINE, so a short query returns the vault. Measured on a 719-note vault:
# "OSI" → 529 hits / 170k chars in a single payload, "e" → 14535 hits. The
# window is the scarce resource, so rank by hit density (the note that mentions
# the term 40 times is the one meant; the 190 notes mentioning it once are not)
# and keep the top slice. ponytail: density, not the reranker — this is the
# deterministic literal leg, and putting a model in it buys ranking at the cost
# of reproducibility and a "reranker down" failure mode. Meaning-based ranking
# already has a tool: silica_semantic_search.
_CONTEXT_MAX_NOTES = 12
_CONTEXT_LINES_PER_NOTE = 3


@tool(SearchContextArgs, cls="atomic")
def silica_search_context(query: str) -> dict:
    """Search note BODIES for exact text; returns snippets with line numbers.

    Use to find literal mentions of a term. When the exact wording is unknown,
    use silica_semantic_search instead; to match note titles use silica_search.

    Returns {hits, notes_matched}: densest notes first, 3 snippets each at most,
    `truncated` when capped.
    """
    by_note: dict[str, list] = {}
    for h in DRIVER.search_context(query):
        by_note.setdefault(h.ref.path or h.ref.name, []).append(h)

    q = query.casefold()
    ranked = sorted(
        by_note.values(),
        # note_matches is the note's TRUE occurrence count: the backend caps
        # materialized Hits per note, so len(hs) saturates at the cap and would
        # tie every heavy note. 0 means the backend didn't count — fall back.
        # path last: a stable tiebreak, so the same query answers the same way.
        key=lambda hs: (-(hs[0].note_matches or len(hs)),
                        q not in hs[0].ref.name.casefold(), hs[0].ref.path),
    )
    kept = ranked[:_CONTEXT_MAX_NOTES]

    # Stale flags (spec-stale-triggers §3): one peek for the whole call, read-only
    # so search never pays the git walk. `peek()` itself never raises by contract;
    # this try/except guards the imports and honors the soft-failure rule (§5)
    # regardless.
    try:
        from silica.config import CONFIG
        from silica.kernel.code import codedocs

        stale_map = codedocs.peek(CONFIG.vault_path)
    except Exception:
        stale_map = {}
        codedocs = None  # peek import itself failed: no flags this call

    out: dict = {
        "hits": [
            {"name": h.ref.name, "path": h.ref.path, "line": h.line,
             "snippet": h.snippet,
             **({"stale": lvl} if stale_map and codedocs
                and (lvl := codedocs.peek_level(stale_map, h.ref.path or ""))
                else {})}
            for hs in kept for h in hs[:_CONTEXT_LINES_PER_NOTE]
        ],
        "notes_matched": len(by_note),
    }
    if len(kept) < len(by_note) or any(len(hs) > _CONTEXT_LINES_PER_NOTE for hs in kept):
        out["truncated"] = (
            f"{len(by_note)} notes matched; showing the {len(kept)} densest, up to "
            f"{_CONTEXT_LINES_PER_NOTE} lines each. Narrow the query, or use "
            "silica_semantic_search to rank by meaning."
        )
    return out


class ReadNoteArgs(BaseModel):
    name: str = Field(description="Name of the note to read (wikilink style, without file extension)")

@tool(ReadNoteArgs, cls="atomic")
def silica_read_note(name: str) -> str:
    """Reads the complete content of a note in the vault by name (wikilink-style resolution). DO NOT use paths."""
    nc = DRIVER.read_note(name)
    return _with_stale_banner(nc.content)


def _with_stale_banner(content: str) -> str:
    """Prefix a code-doc note with its staleness warning, when it has one.

    A wiki note derived from source outlives the source: after a refactor it
    still reads as authoritative while naming files that have moved. The
    `code_ref`/`documents:` frontmatter always carried the answer, but only the
    `/stale` report ever asked — so the reader, the one acting on the note, was
    the one kept in the dark. Parsed from the content already in hand: no extra
    driver round-trip, and notes without `documents:` cost one dict lookup.
    """
    try:
        from silica.config import CONFIG
        from silica.kernel.code import codedocs
        from silica.kernel.write import frontmatter

        data, _, _ = frontmatter.split(content)
        if not data or not codedocs.documents_of(data):
            return content
        warning = codedocs.read_warning(CONFIG.vault_path, data)
        return f"> {warning}\n\n{content}" if warning else content
    except Exception:
        return content


class PropsArgs(BaseModel):
    name: str = Field(description="Name of the note to read the frontmatter properties from")

@tool(PropsArgs, cls="atomic")
def silica_props(name: str) -> dict:
    """Reads the frontmatter properties of a note (saves tokens, does not read the body)."""
    return DRIVER.props_of(name)


class OutlineArgs(BaseModel):
    name: str = Field(description="Name of the note to display the heading tree of")

@tool(OutlineArgs, cls="atomic")
def silica_outline(name: str) -> list:
    """Displays the heading tree (H1-H6) of a note."""
    headings = DRIVER.outline(name)
    return [{"level": h.level, "text": h.text} for h in headings]


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

class LinksArgs(BaseModel):
    name: str = Field(description="Name of the note to list outgoing links from")

@tool(LinksArgs, cls="atomic")
def silica_links(name: str) -> list:
    """Lists outgoing links from a note (connected notes)."""
    refs = DRIVER.links(name)
    return [r.path for r in refs]


class BacklinksArgs(BaseModel):
    name: str = Field(description="Name of the note to list incoming links (backlinks) for")

@tool(BacklinksArgs, cls="atomic")
def silica_backlinks(name: str) -> list:
    """Lists incoming links (backlinks) pointing to a note."""
    refs = DRIVER.backlinks(name)
    return [r.path for r in refs]


class EmptyArgs(BaseModel):
    pass

@tool(EmptyArgs, cls="atomic")
def silica_orphans() -> dict:
    """Lists orphan notes (notes with no incoming links) in the vault.

    Returns {"total": N, "orphans": [path, ...]} capped at 200 entries — a
    neglected vault is mostly orphans, so the count is the answer to "how many"
    and the sample is enough to start linking.
    """
    refs = DRIVER.orphans()
    paths = [r.path for r in refs]
    out: dict = {"total": len(paths), "orphans": paths[:_FILES_CAP]}
    if len(paths) > _FILES_CAP:
        out["truncated"] = True
    return out


@tool(EmptyArgs, cls="atomic")
def silica_unresolved() -> list:
    """Lists unresolved wikilinks in the vault (links pointing to non-existent notes)."""
    links = DRIVER.unresolved()
    return [{"target": l.target} for l in links]


# ---------------------------------------------------------------------------
# List files
# ---------------------------------------------------------------------------

def _natural_key(path: str) -> list:
    """Sort key that reads embedded runs of digits as numbers.

    Plain lexicographic order interleaves "Lezione 10" between 1 and 2, and the
    injector ingests a folder in listing order: lesson 10's concepts would land
    before lesson 2 defines them. re.split alternates non-digit/digit, so the
    element at a given index always has the same type across paths.
    """
    import re

    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path)]


def notes_under(folder: str) -> list[str]:
    """Vault-relative `.md` paths under `folder`, natural-sorted. "" ⇒ the vault.

    The one answer to "which notes does this folder hold?", because neither
    half of the codebase could give it for an inbox folder: the note index used
    to skip the inbox entirely, and `sources.registry.expand_folder` answers
    only for the code lane off a git-backed census — a plain Obsidian vault is
    no repo, so it returns [] for every folder in it. Between the two a folder
    argument had no listing at all, and the only caller left to produce one was
    an LLM guessing filenames.

    The index now walks the inbox, so `list_files` alone answers. The
    `list_inbox_files` fallback below stays as belt-and-braces: its failure mode
    was a model inventing filenames, which is not a failure worth re-earning to
    save four lines.
    """
    from silica.kernel.recall.paths import in_folder
    from silica.kernel.vault_manifest import active_inbox_dir

    scope = _vault_rel(folder)
    # list_files(folder) pre-filters loosely (startswith); in_folder tightens it
    # so a Foo/ argument never leaks into a sibling FooBar/.
    paths = [r.path for r in DRIVER.list_files(scope) if in_folder(r.path, scope)]
    if not paths and scope:
        inbox = active_inbox_dir()
        if inbox and in_folder(scope, inbox):
            # .md only: the unconverted files (PDFs) are silica_inbox_ls's job,
            # and a converted folder's Images/ leaves outnumber its notes 70:1.
            paths = [
                r.path for r in DRIVER.list_inbox_files()
                if r.path.endswith(".md") and in_folder(r.path, scope)
            ]
    return sorted(paths, key=_natural_key)


def _vault_rel(path: str) -> str:
    """`path` as a vault-relative posix path; already-relative input passes through.

    Users do paste absolute vault paths at /nucleate, and every listing below is
    keyed vault-relative. A path outside the vault is returned as given: it
    matches nothing, which is the honest answer for it.
    """
    from silica.config import CONFIG

    p = Path(path.strip())
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(CONFIG.vault_path).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.replace("\\", "/").strip("/")


class ListFilesArgs(BaseModel):
    folder: str = Field(default="", description="Optional folder path to filter results")

@tool(ListFilesArgs, cls="atomic")
def silica_files(folder: str = "") -> dict:
    """Lists the notes in the vault and the source files under a folder.

    Returns {"total": N, "files": [vault-relative path, ...]} for markdown
    notes, plus "code": [repo-relative path, ...] with the ingestible source
    files under `folder` (empty folder= lists notes only). An inbox folder is
    listed too — its notes are kept out of the vault index, not missing — but
    only its .md: unconverted files there (PDFs etc.) need `/convert` first.
    A note's wikilink name is its filename without the extension. A folder of code is NOT empty
    just because it holds no .md — feed the "code" paths to /nucleate to stage
    a skeleton stub per file. Both listings are capped at 200 entries: when
    "truncated" is true, narrow with folder= instead of re-calling. For a bare
    count ("how many notes?") use the returned "total" — or the '## Vault map'
    block already in context, without any call.
    """
    # bare paths, not {name, path} dicts — NoteRef.name is the
    # filename without its extension, so the dict shipped every note's name
    # twice (48% of this payload, ~2.5k tokens at the 200-entry cap).
    files = notes_under(folder)
    result: dict = {"total": len(files), "files": files[:_FILES_CAP]}
    if len(files) > _FILES_CAP:
        result["truncated"] = True
        result["hint"] = "Listing capped at 200 entries; pass folder= to narrow."
    # folder-scoped only — a bare call would dump a whole repo into
    # the context window, and the vault map already covers "what is here".
    if folder:
        from silica.sources.registry import expand_folder

        code = expand_folder(folder)
        if code:
            result["code"] = code[:_FILES_CAP]
            result["code_total"] = len(code)
    return result


class ExistsArgs(BaseModel):
    path: str = Field(description="Relative path of the note in the vault")

@tool(ExistsArgs, cls="atomic")
def silica_exists(path: str) -> bool:
    """Verifies if a note exists in the vault (including the inbox) given its relative path."""
    try:
        DRIVER.read_note(path)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Deferred Op Store
# ---------------------------------------------------------------------------

@tool(EmptyArgs, cls="atomic")
def silica_deferred_list() -> list:
    """List all pending deferred op bundles (concepts rejected by the validator in previous runs).

    Returns summary rows — use silica_deferred_retry(content_hash) to attempt
    writing them, or silica_deferred_flush(content_hash) to discard them.
    """
    from silica.kernel.recall.deferred import get_deferred_store
    return get_deferred_store().list_all()


class DeferredFlushArgs(BaseModel):
    content_hash: str = Field(description="Content hash of the deferred bundle to permanently discard")

@tool(DeferredFlushArgs, cls="atomic", collapse="eager")
def silica_deferred_flush(content_hash: str) -> dict:
    """Discard a deferred op bundle — marks those rejected ops as permanently skipped."""
    from silica.kernel.recall.deferred import get_deferred_store
    removed = get_deferred_store().remove(content_hash)
    if removed:
        return {"removed": True, "content_hash": content_hash}
    return {"removed": False, "error": f"No deferred bundle found for {content_hash[:8]}…"}


@tool(EmptyArgs, cls="atomic")
def silica_inbox_ls() -> list:
    """Lists all files in the Inbox folder (inbox_dir), including non-markdown
    files (PDFs etc.). Non-markdown files cannot be read or nucleated directly:
    ask the user to run `/convert <path>` first, then work on the resulting .md.
    """
    refs = DRIVER.list_inbox_files()
    return [r.path for r in refs]


# ---------------------------------------------------------------------------
# Graph path / explain
# ---------------------------------------------------------------------------

class GraphPathArgs(BaseModel):
    source: str = Field(description="Source note name or vault-relative path")
    target: str = Field(description="Target note name or vault-relative path")
    max_paths: int = Field(default=1, description="Maximum number of shortest paths to return")

@tool(GraphPathArgs, cls="atomic")
def silica_graph_path(source: str, target: str, max_paths: int = 1) -> dict:
    """Shortest connection(s) between two notes over the resolved wikilink graph.

    Returns path(s) as lists of note ids, or an error dict if no path exists.
    Uses the undirected view of the resolved (EXTRACTED) wikilink graph.
    """
    import networkx as nx
    from silica.kernel.recall.graph_export import build_graph_data, edge_graph

    try:
        nodes, edges = build_graph_data(folder="")
    except Exception as exc:
        return {"error": f"Failed to build graph: {exc}"}

    # edge_graph is the shared builder; it also inserts nodes sorted, so tied
    # shortest paths come back in the same order every process.
    G = edge_graph(nodes, edges)
    real_ids: set[str] = set(G.nodes())

    # Resolve source/target: accept path or name substring match
    def _resolve(query: str) -> str | None:
        if query in real_ids:
            return query
        q_lower = query.lower().removesuffix(".md")
        for nid in real_ids:
            stem = nid.rsplit("/", 1)[-1].removesuffix(".md").lower()
            if stem == q_lower:
                return nid
        return None

    src_id = _resolve(source)
    tgt_id = _resolve(target)

    if src_id is None:
        return {"error": f"Source note not found in graph: '{source}'"}
    if tgt_id is None:
        return {"error": f"Target note not found in graph: '{target}'"}
    if src_id == tgt_id:
        return {"paths": [[src_id]], "length": 0}

    try:
        if max_paths == 1:
            path = nx.shortest_path(G, src_id, tgt_id)
            return {"paths": [path], "length": len(path) - 1}
        else:
            import itertools
            gen = nx.all_shortest_paths(G, src_id, tgt_id)
            paths = list(itertools.islice(gen, max_paths))
            return {"paths": paths, "length": len(paths[0]) - 1 if paths else 0}
    except nx.NetworkXNoPath:
        return {"error": f"No path between '{source}' and '{target}'"}
    except nx.NodeNotFound as exc:
        return {"error": f"Node not found: {exc}"}


class GraphExplainArgs(BaseModel):
    note: str = Field(description="Note name or vault-relative path to explain")
    depth: int = Field(default=1, description="Neighbourhood depth (1=direct links only)")

@tool(GraphExplainArgs, cls="atomic")
def silica_graph_explain(note: str, depth: int = 1) -> dict:
    """Explain a note's structural position: cluster, degree rank, betweenness,
    out-links, backlinks, and any cross-cluster bridges it participates in.

    `betweenness` is the fraction of shortest paths running through the note — a
    bottleneck signal distinct from degree. A note with LOW degree but HIGH
    betweenness is a bridge whose removal fragments the vault: worth reinforcing
    even though it has few links.

    `diagnosis` answers "how well is THIS note integrated" by reading every
    vault-wide coherence signal for this one note: orphan/hub status, the
    cohesion of its own cluster, whether it is contested or built on a drifted
    source, and its rank in the attention / integration-deficit lists. A `null`
    in a ranked field means the note did not make that list's top-k, which is
    "not among the worst", not "clean".
    """
    from silica.kernel.report.graph_report import compute_report

    try:
        report = compute_report(analytics=True)  # on-demand: needs god_nodes/bridges
    except Exception as exc:
        return {"error": f"Failed to compute graph report: {exc}"}

    # Find the node in god_nodes or clusters
    q_lower = note.lower().removesuffix(".md")
    node_stat = None
    for n in report.god_nodes:
        if n.id.lower() == q_lower or n.id.rsplit("/", 1)[-1].removesuffix(".md").lower() == q_lower:
            node_stat = n
            break

    # Resolve via cluster members if not in god_nodes
    resolved_id: str | None = None
    if node_stat:
        resolved_id = node_stat.id
    else:
        for c in report.clusters:
            for m in c.members:
                if m.lower() == q_lower or m.rsplit("/", 1)[-1].removesuffix(".md").lower() == q_lower:
                    resolved_id = m
                    break
            if resolved_id:
                break

    if resolved_id is None:
        # last attempt: check orphans
        for o in report.orphans:
            if o.lower() == q_lower or o.rsplit("/", 1)[-1].removesuffix(".md").lower() == q_lower:
                resolved_id = o
                break

    if resolved_id is None:
        return {"error": f"Note '{note}' not found in the graph"}

    # Degree rank (rank among all nodes by degree)
    try:
        out_links = [r.path or r.name for r in DRIVER.links(resolved_id)]
        backlinks = [r.path or r.name for r in DRIVER.backlinks(resolved_id)]
    except Exception:
        out_links = []
        backlinks = []

    bridges_involving = [
        {"source": b.source, "target": b.target, "clusters": f"{b.source_cluster}↔{b.target_cluster}", "weight": b.weight}
        for b in report.bridges
        if b.source == resolved_id or b.target == resolved_id
    ]

    cluster_id = -1
    for c in report.clusters:
        if resolved_id in c.members:
            cluster_id = c.cluster_id
            break

    # Degree rank
    all_degrees = sorted(
        [(n.id, n.degree) for n in report.god_nodes],
        key=lambda x: -x[1],
    )
    degree_rank = next(
        (i + 1 for i, (nid, _) in enumerate(all_degrees) if nid == resolved_id),
        None,
    )

    degree = (node_stat.degree if node_stat else len(out_links) + len(backlinks))

    # Per-note diagnosis: every vault-wide coherence signal, read for THIS note.
    # All of it is already on the report — the signals existed only as vault
    # aggregates or as rows in GRAPH_REPORT.md, with no way to ask "how well is
    # this one note integrated". Composition only, no extra computation.
    # ponytail: the co-occurrence signals (integration deficit, stale links) need
    # with_cooccurrence, which this tool does not pay for; they read `null` here.
    # Flip the compute_report call above if the diagnosis ever needs them.
    cluster_stat = next((c for c in report.clusters if c.cluster_id == cluster_id), None)
    contested_note = next((c for c in report.contested if c.path == resolved_id), None)
    attention = next((a for a in report.attention_candidates if a.path == resolved_id), None)
    deficit = next((d for d in report.integration_deficits if d.path == resolved_id), None)
    stale = [
        {"source": s.source, "target": s.target}
        for s in report.stale_links
        if resolved_id in (s.source, s.target)
    ]
    diagnosis = {
        # Structural position
        "is_orphan": resolved_id in report.orphans,
        "is_hub": bool(cluster_stat and cluster_stat.hub == resolved_id),
        "cluster_size": (cluster_stat.size if cluster_stat else 0),
        # How tightly its own area holds together: a well-linked note in a loose
        # cluster is integrated; the same note in a dense cluster is ordinary.
        "cluster_cohesion": (cluster_stat.cohesion if cluster_stat else 0.0),
        # Authority / freshness
        "contested": bool(contested_note),
        "contradictions": (contested_note.refs if contested_note else []),
        "drifted_source": any(
            sd.note == resolved_id.removesuffix(".md") for sd in report.source_drift
        ),
        # Ranked signals: present only when the note made the report's top-k, so
        # `null` means "not among the worst", NOT "clean".
        "attention_score": (attention.score if attention else None),
        "days_idle": (attention.days_idle if attention else None),
        "integration_deficit": (deficit.score if deficit else None),
        "stale_links": stale,
    }

    return {
        "note": resolved_id,
        "cluster": cluster_id,
        "degree": degree,
        "degree_rank": degree_rank,
        "betweenness": report.betweenness_map.get(resolved_id, 0.0),
        "out_links": out_links[:depth * 10],
        "backlinks": backlinks[:depth * 10],
        "bridges": bridges_involving,
        "diagnosis": diagnosis,
    }


# ---------------------------------------------------------------------------
# Ledger steering — silica_ledger_next / silica_ledger_update
# ---------------------------------------------------------------------------

class LedgerNextArgs(BaseModel):
    run_id: str = Field(description="Run ID returned by silica_vault_report")

@tool(LedgerNextArgs, cls="atomic")
def silica_ledger_next(run_id: str) -> dict:
    """Return the next actionable task for a run: capability (tool name), validated
    payload, and reason. Returns {"done": true} when the plan is exhausted.

    The agent should call the named tool with the payload, then call
    silica_ledger_update to record the outcome.
    """
    import orjson
    from pathlib import Path
    from silica.kernel.progress import ProgressLedger

    try:
        progress = ProgressLedger.load(run_id)
    except FileNotFoundError:
        return {"error": f"Run '{run_id}' not found"}
    except Exception as exc:
        return {"error": f"Failed to load ledger: {exc}"}

    t = progress.next_pending()
    if t is None:
        return {"done": True}

    # Load payload from disk if available
    payload: dict = {}
    if t.input_ref:
        try:
            payload = orjson.loads(Path(t.input_ref).read_bytes())
        except Exception:
            pass

    return {
        "task_id": t.id,
        "capability": t.capability_name,
        "payload": payload,
        "reason": payload.get("_reason", ""),
        "needs_confirmation": payload.get("needs_confirmation", False),
        "attempts": t.attempts,
    }


class LedgerUpdateArgs(BaseModel):
    run_id: str = Field(description="Run ID")
    task_id: str = Field(description="Task ID returned by silica_ledger_next")
    status: str = Field(description="Outcome: done | failed | skipped | blocked")
    error: str = Field(default="", description="Error message if status is 'failed'")

@tool(LedgerUpdateArgs, cls="atomic")
def silica_ledger_update(run_id: str, task_id: str, status: str, error: str = "") -> dict:
    """Mark a task's outcome on the run's ProgressLedger and persist it.

    Returns {"ok": true, "digest": ...} so the agent has the updated state
    for the next iteration.
    """
    from silica.kernel.progress import ProgressLedger

    try:
        progress = ProgressLedger.load(run_id)
    except FileNotFoundError:
        return {"error": f"Run '{run_id}' not found"}
    except Exception as exc:
        return {"error": f"Failed to load ledger: {exc}"}

    try:
        if status == "done":
            progress.mark_done(task_id)
        elif status == "failed":
            progress.mark_failed(task_id, error=error)
        else:
            progress.set_status(task_id, status, error=error or None)  # type: ignore[arg-type]
        progress.save()
    except KeyError:
        return {"error": f"Task '{task_id}' not found in run '{run_id}'"}
    except Exception as exc:
        return {"error": f"Failed to update ledger: {exc}"}

    return {"ok": True, "digest": progress.digest()}



# ---------------------------------------------------------------------------
# Study loop
# ---------------------------------------------------------------------------

class QuizResult(BaseModel):
    path: str = Field(description="Note the question was drawn from (wikilink name or vault-relative path)")
    correct: bool = Field(description="True if the reader's answer was right")


class RecordQuizArgs(BaseModel):
    results: list[QuizResult] = Field(description="One entry per graded question")


@tool(RecordQuizArgs, cls="atomic")
def silica_record_quiz(results: list) -> dict:
    """Record graded quiz answers so the notes the reader failed resurface later.

    Call once after grading a round of questions, one entry per question. This
    writes no note: the log is derived state, and it is what makes
    silica_weak_notes and the report's attention list rank by what the reader
    actually got wrong instead of by file age.
    """
    from silica.kernel.report import quiz

    entries = []
    for r in results:
        r = r if isinstance(r, dict) else r.model_dump()
        name = str(r.get("path") or "").strip()
        if not name:
            continue
        try:  # resolve wikilink names to the path the report keys on
            name = DRIVER.read_note(name).ref.path or name
        except Exception:
            pass  # unresolvable: log the reader's spelling rather than drop the answer
        entries.append({"path": name, "correct": bool(r.get("correct"))})

    written = quiz.record(entries)
    return {"recorded": written, "wrong": sum(1 for e in entries if not e["correct"])}


class WeakNotesArgs(BaseModel):
    limit: int = Field(default=10, description="How many notes to return")


@tool(WeakNotesArgs, cls="atomic")
def silica_weak_notes(limit: int = 10) -> list:
    """Notes the reader has answered wrong, worst first — the review queue.

    Reads the graded-quiz log written by silica_record_quiz. Empty until at
    least one round has been graded, which means "nothing measured yet", not
    "nothing to review".
    """
    from silica.kernel.report import quiz

    return quiz.weakest(limit)
