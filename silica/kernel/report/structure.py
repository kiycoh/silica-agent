# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The seven variables read for ONE note, without paying for the whole report.

`compute_report(analytics, with_embeddings, with_cooccurrence)` already carries
every field this module returns, and it is memoized — but cold it measured
9.39 s on a 709-note vault (2026-08-22), and the surfaces that want a note's
structural position are drawer reads: /context is documented as "index lookup,
not a turn". The same figures cost 2.0 s cold here (graph 1.12, betweenness
0.14, k-core + cut vertices 0.01, RefD 0.74) because this pass skips every leg
that ranks the vault against itself: no duplicate detection, no embeddings, no
autolink delta.

What makes that affordable: the wikilink graph is `wikilink_graph_cached`,
which /context already pays for through `silica_related`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# (epoch,) -> StructureMap. One entry: unlike the graph memo there is no folder
# scope here, so a second key could only ever be a stale epoch.
_memo: dict[str, StructureMap] = {}


@dataclass(frozen=True)
class StructureMap:
    """Vault-wide, graph keyspace (ids carry `.md`) except where noted."""

    core: dict[str, int] = field(default_factory=dict)
    articulation: frozenset[str] = frozenset()
    surprise: dict[str, float] = field(default_factory=dict)
    betweenness: dict[str, float] = field(default_factory=dict)
    degree: dict[str, int] = field(default_factory=dict)
    # The two ranks `surprise` is the difference of, kept so a reader can be
    # shown both (ADR-0075): "+25" says nothing that "96 against 71" does not.
    betweenness_rank: dict[str, float] = field(default_factory=dict)
    degree_rank: dict[str, float] = field(default_factory=dict)
    # Store keyspace (no `.md`), like prereq_map on the report.
    prereq: dict[str, list[str]] = field(default_factory=dict)
    unlocks: dict[str, list[str]] = field(default_factory=dict)

    def cut_component(self, node: str) -> int:
        """How many notes fall off the giant component if `node` is removed.

        0 when the note is not a cut vertex. This is the number the curator's
        veto is about, and the only one that says how much a hold is worth.
        """
        if node not in self.articulation:
            return 0
        return self._cut_sizes().get(node, 0)

    def _cut_sizes(self) -> dict[str, int]:
        # Filled by build(); an empty map means nobody measured, and every cut
        # vertex then reads 0 rather than a made-up size.
        return getattr(self, "_sizes", {}) or {}


def cut_component_sizes(G, articulation) -> dict[str, int]:
    """{cut vertex: notes stranded by its removal}.

    Stranded is measured INSIDE the note's own component and against the
    largest piece the removal leaves: a hub that splits 400 notes into 399 + 1
    costs one note, not 400. Measured vault-wide instead, every cut vertex was
    charged for the vault's pre-existing fragmentation (159 notes on a vault
    whose orphan islands were already there before the removal).

    Subgraph VIEWS, not copies: one copy per cut vertex was 128 copies of a
    709-node graph on the dev vault.
    """
    import networkx as nx

    comp_of: dict[str, frozenset[str]] = {}
    for c in nx.connected_components(G):
        f = frozenset(c)
        for n in f:
            comp_of[n] = f
    sizes: dict[str, int] = {}
    for v in articulation:
        comp = comp_of.get(v)
        if not comp:
            continue
        pieces = cut_pieces(G, v, comp)
        sizes[v] = sum(len(p) for p in pieces[1:])
    return sizes


def cut_pieces(G, v: str, comp=None) -> list[set[str]]:
    """The pieces `v`'s removal leaves inside its component, largest first.

    pieces[0] is the rest of the vault as `cut_component_sizes` counts it; the
    others are the notes stranded. One function for both, so the count the
    drawer prints and the names it lists under it cannot disagree. Ties on size
    break on the smallest id, or which piece is "the rest" would change between
    two reads of the same graph.
    """
    import networkx as nx

    if comp is None:
        comp = nx.node_connected_component(G, v)
    rest = set(comp) - {v}
    return sorted((set(c) for c in nx.connected_components(G.subgraph(rest))),
                  key=lambda c: (-len(c), min(c)))


# How much of a cut the drawer draws (ADR-0075, Place). Measured 2026-10-07 on
# the test vault: Backward chaining's 13 stranded notes, its 2 links on the
# vault's side and their 4 neighbours named 8 of 19 notes at a 320px drawer and
# 15 at 528. Past ~40 dots the names are all held back and the figure is a
# texture, so a piece bigger than BEHIND_CAP shows its nearest notes and the
# links it hides as stubs.
CUT_BEHIND_CAP, CUT_NEAR_CAP, CUT_FAR_CAP = 24, 6, 8
STRESS_STEPS = 300


def _stress(D):
    """2D positions whose distances match `D` (hops): stress majorization.

    Kamada-Kawai's objective (each pair pulled toward its graph distance,
    weighted 1/d^2), solved by SMACOF from a classical-MDS start, in numpy:
    networkx's kamada_kawai_layout imports scipy, which the product does not
    declare (it arrives only through sentence-transformers in the `all` extra).
    """
    import numpy as np

    n = len(D)
    if n < 3:
        return np.array([[0.0, 0.0], [float(D[0][1]), 0.0]][:n])
    J = np.eye(n) - 1.0 / n
    w, v = np.linalg.eigh(-0.5 * J @ (D ** 2) @ J)
    X = v[:, -2:] * np.sqrt(np.maximum(w[-2:], 1e-9))
    # Two leaves of one note have the same row of distances, so MDS puts them
    # on one point and no step can part them (8 of 127 cut vertices on the
    # test vault). A fixed nudge per index splits every such pair.
    i = np.arange(n)
    X = X + 1e-3 * np.stack([np.cos(2.4 * i), np.sin(2.4 * i)], 1)
    W = np.where(D > 0, 1.0 / np.where(D > 0, D, 1.0) ** 2, 0.0)
    Vp = np.linalg.pinv(np.diag(W.sum(1)) - W)
    for _ in range(STRESS_STEPS):
        dist = np.sqrt(((X[:, None] - X[None]) ** 2).sum(-1))
        # Guttman's convention: a pair on one point pulls nothing this step.
        Bz = np.where(dist > 1e-9, -W * D / np.maximum(dist, 1e-9), 0.0)
        np.fill_diagonal(Bz, 0.0)
        np.fill_diagonal(Bz, -Bz.sum(1))
        X = Vp @ Bz @ X
    return X


def cut_layout(G, v: str, pieces: list[set[str]], degree: dict[str, int]) -> dict:
    """The neighbourhood of a cut, placed: `v` at the origin, the notes it
    strands to the right of it, its side of the vault to the left.

    Each side is laid out on its own, `v` included, then turned so its mass
    lies on its side and anything still across the line is mirrored back. One
    layout over both sides put Forward chaining, which sits behind Backward
    chaining, on the vault's side of the line (2026-10-07), and the line is the
    figure's claim. Units are hops. Deterministic: the order is sorted and the
    mirror is fixed by the farthest note.
    """
    import networkx as nx
    import numpy as np

    rest = pieces[0] if pieces else set()
    piece_of = {n: k for k, p in enumerate(pieces[1:]) for n in p}
    deg = lambda n: (-degree.get(n, 0), n)      # noqa: E731

    # behind: nearest first, the best-linked first within a hop
    hop_b = nx.single_source_shortest_path_length(G.subgraph({v} | set(piece_of)), v)
    behind = sorted((n for n in hop_b if n != v), key=lambda n: (hop_b[n], *deg(n)))[:CUT_BEHIND_CAP]
    near = sorted((n for n in G[v] if n in rest), key=deg)[:CUT_NEAR_CAP]
    far = sorted({m for n in near for m in G[n] if m in rest} - set(near), key=deg)[:CUT_FAR_CAP]

    pos: dict[str, tuple[float, float]] = {v: (0.0, 0.0)}
    for side, members, sign in (("behind", behind, 1.0), ("rest", near + far, -1.0)):
        if not members:
            continue
        order = [v, *members]
        H = G.subgraph(order)
        sp = dict(nx.all_pairs_shortest_path_length(H))
        D = np.array([[sp[a].get(b, len(order)) for b in order] for a in order], float)
        X = _stress(D)
        X = X - X[0]
        cx, cy = X[1:].mean(0)
        a = -np.arctan2(cy, cx)
        X = X @ np.array([[np.cos(a), np.sin(a)], [-np.sin(a), np.cos(a)]])
        # A side whose notes wrap round `v` (the vault's side of Backward
        # chaining is a star: v sits in its middle) is folded into the half
        # plane by narrowing every angle by one factor, so the order round v
        # and each distance from it survive. Mirroring the far ones instead
        # landed Backtracking on Base di conoscenza, 0.07 hops apart.
        r, t = np.hypot(X[:, 0], X[:, 1]), np.arctan2(X[:, 1], X[:, 0])
        span = np.abs(t[1:]).max()
        if span > 0.42 * np.pi:
            t = t * (0.42 * np.pi / span)
        X = np.stack([np.maximum(r * np.cos(t), 0.35), r * np.sin(t)], 1)
        X[0] = 0.0
        if X[1:, 1][np.argmax(np.abs(X[1:, 1]))] > 0:       # the farthest note above
            X[:, 1] = -X[:, 1]
        for n, (x, y) in zip(order[1:], X[1:]):
            pos[n] = (round(sign * float(x), 3), round(float(y), 3))

    shown = [v, *behind, *near, *far]
    idx = {n: i for i, n in enumerate(shown)}
    hop = nx.single_source_shortest_path_length(G.subgraph(shown), v)
    return {
        "nodes": [{"id": n, "side": "note" if n == v else "behind" if n in piece_of else "rest",
                   "piece": piece_of.get(n), "hop": hop.get(n, 0),
                   "x": pos[n][0], "y": pos[n][1],
                   # links that leave the drawing, drawn as stubs
                   "stubs": sum(1 for m in G[n] if m not in idx)}
                  for n in shown],
        "edges": sorted((idx[a], idx[b]) if idx[a] < idx[b] else (idx[b], idx[a])
                        for a, b in G.subgraph(shown).edges()),
    }


def build() -> StructureMap:
    """Compute the map from scratch. `structure_map()` is the memoized door."""
    import networkx as nx

    from silica.kernel.recall import signals
    from silica.kernel.recall.graph_export import wikilink_graph_cached

    G = wikilink_graph_cached()
    if G.number_of_nodes() == 0:
        return StructureMap()

    bet: dict[str, float] = {}
    if G.number_of_edges():
        try:
            # Same sampling as canvas_metrics and compute_report, same seed, so
            # a note's betweenness reads identically on all three surfaces.
            bet = nx.betweenness_centrality(
                G, k=min(G.number_of_nodes(), 400), seed=42, normalized=True,
            )
        except Exception as exc:
            logger.warning("structure: betweenness failed (%s)", exc)
    deg = dict(G.degree())
    core, articulation, surprise = signals.load_bearing(G, betweenness=bet, degree=deg)
    # The same ranking load_bearing takes the difference of, over the same nodes.
    b_rank = signals._pct_rank({n: float(bet.get(n, 0.0)) for n in G.nodes})
    d_rank = signals._pct_rank({n: float(deg.get(n, 0)) for n in G.nodes})

    prereq: dict[str, list[str]] = {}
    try:
        from silica.kernel.report.learner import prerequisites_map

        prereq = prerequisites_map()
    except Exception as exc:
        # The reading order is one of seven variables: a store that cannot
        # answer must not cost the reader the other six.
        logger.debug("structure: prerequisites unavailable (%s)", exc)
    unlocks: dict[str, list[str]] = {}
    for dependent, ps in prereq.items():
        for p in ps:
            unlocks.setdefault(p, []).append(dependent)
    for v in unlocks.values():
        v.sort()

    m = StructureMap(
        core=core, articulation=frozenset(articulation), surprise=surprise,
        betweenness={n: round(b, 4) for n, b in bet.items()}, degree=deg,
        betweenness_rank=b_rank, degree_rank=d_rank,
        prereq=prereq, unlocks=unlocks,
    )
    # frozen dataclass: the sizes ride as a private attribute rather than a
    # field, because they are a lazy detail of `cut_component` and nothing
    # serialises them.
    object.__setattr__(m, "_sizes", cut_component_sizes(G, articulation))
    return m


def structure_map() -> StructureMap:
    """The map for the current vault, memoized on its file-state epoch."""
    from silica.kernel.recall.paths import vault_epoch

    try:
        epoch = vault_epoch()
    except Exception:
        epoch = ""
    if epoch and (hit := _memo.get(epoch)) is not None:
        return hit
    try:
        m = build()
    except Exception as exc:
        # Every consumer treats an empty map as "not measured" and prints
        # nothing, so a broken index costs a section, never a 500.
        logger.warning("structure: map unavailable (%s)", exc)
        return StructureMap()
    if epoch:
        _memo.clear()
        _memo[epoch] = m
    return m


def note_structure(node_id: str) -> dict:
    """The seven variables for one note, JSON-ready, graph keyspace in.

    `prerequisites` and `unlocks` come back in the GRAPH keyspace so a caller
    can link them; RefD works in the store keyspace, so the `.md` is put back
    here and an entry that no longer resolves to a node is dropped rather than
    shipped as a dead row.
    """
    from silica.kernel.recall.cooccurrence import cooccur_key

    m = structure_map()
    if not m.degree:
        return {}
    key = cooccur_key(node_id)
    known = set(m.degree)

    def _gid(store_key: str) -> str | None:
        for cand in (store_key + ".md", store_key):
            if cand in known:
                return cand
        return None

    k = m.core.get(node_id, 0)
    out = {
        "degree": m.degree.get(node_id, 0),
        "betweenness": m.betweenness.get(node_id, 0.0),
        "coreness": k,
        # The scale "core 2" never had (ADR-0075): 2 of how many, and how common.
        # A pass over the core map per read; it is one dict of ints per note.
        "core_max": max(m.core.values(), default=0),
        "core_reach": sum(1 for c in m.core.values() if c >= k),
        # How many notes sit at each depth, 0 to core_max: the drawer's
        # histogram, so "2 of 4" is seen against where the vault is.
        "core_hist": _core_hist(m.core),
        "notes": len(m.degree),
        "degree_rank": m.degree_rank.get(node_id, 0.0),
        "betweenness_rank": m.betweenness_rank.get(node_id, 0.0),
        "articulation": node_id in m.articulation,
        "strands": m.cut_component(node_id),
        "surprise": m.surprise.get(node_id, 0.0),
        "prerequisites": [g for p in m.prereq.get(key, []) if (g := _gid(p))],
        "unlocks": [g for d in m.unlocks.get(key, []) if (g := _gid(d))],
        "in_graph": node_id in known,
    }
    if out["articulation"] and out["in_graph"]:
        out.update(_cut_detail(node_id, m))
    return out


def _core_hist(core: dict[str, int]) -> list[int]:
    hist = [0] * (max(core.values(), default=0) + 1)
    for c in core.values():
        hist[c] += 1
    return hist


def _cut_detail(node_id: str, m: StructureMap) -> dict:
    """Which notes a cut vertex strands, by piece, and how big its part is.

    Built per read from the memoized graph and not stored on the map: the map
    keeps one size per cut vertex, and the pieces of all of them (128 on the dev
    vault) are sets nobody but this one drawer read ever asks for. One
    component walk per read of a bridge, measured in milliseconds at 729 notes.
    """
    import networkx as nx

    from silica.kernel.recall.graph_export import wikilink_graph_cached

    try:
        G = wikilink_graph_cached()
        comp = nx.node_connected_component(G, node_id)
        pieces = cut_pieces(G, node_id, comp)
    except Exception as exc:
        # The count still stands from the map; only the names are lost.
        logger.debug("structure: cut pieces failed for %s (%s)", node_id, exc)
        return {}
    # Best-linked first: the head of a piece is the name a reader knows it by.
    stranded = [sorted(p, key=lambda n: (-m.degree.get(n, 0), n)) for p in pieces[1:]]
    out = {"component": len(comp), "stranded": stranded}
    try:
        layout = cut_layout(G, node_id, pieces, m.degree)
        area = _areas(m, G)
        for n in layout["nodes"]:
            n["area"] = area.get(n["id"], -1)
        out["layout"] = layout
    except Exception as exc:
        # The drawing is lost, the sentence and the names under it are not.
        logger.debug("structure: cut layout failed for %s (%s)", node_id, exc)
    return out


def _areas(m: StructureMap, G) -> dict[str, int]:
    """Each note's area id, the one the graph frame colours it by.

    Louvain on the same cached graph with the frame's seed and order
    (`louvain_sorted`), so the ids agree. Lazy and kept on the epoch's map, like
    `_sizes`: 552 ms at 729 notes (ADR-0074), paid by the first bridge read of
    an epoch and by no other read.
    """
    hit = getattr(m, "_areas", None)
    if hit is None:
        from silica.kernel.recall.graph_export import louvain_sorted

        hit = {n: i for i, c in enumerate(louvain_sorted(G)) for n in c}
        object.__setattr__(m, "_areas", hit)
    return hit


def ladder(root: str, *, hops: int = 3, cap: int = 60) -> dict:
    """The prerequisite DAG around one note: what to read before it, and after.

    Graph keyspace in and out. `depth` is the LONGEST path from a source, not
    the hop count: on a ladder, shortest-hop puts a note one link from the root
    on the same rung as its own prerequisite whenever a shortcut edge exists,
    and the rung is the whole point. Depths are shifted so the root reads 0,
    negatives are what it needs and positives what it unlocks.

    RefD can produce a cycle (A needs B, B needs C, C needs A): those are real
    disagreements in the vault, not corrupt data, so the edge that closes a
    cycle is DROPPED for the layout and reported in `cycles` rather than
    silently kept (which would make the depth assignment non-terminating).

    `root` comes back resolved to the graph id the ladder was actually built
    around, so a caller never has to translate between the two keyspaces to
    find its own root in the answer. Empty string when nothing was built.
    """
    from silica.kernel.recall.cooccurrence import cooccur_key

    m = structure_map()
    if not m.prereq:
        return {"root": "", "nodes": [], "edges": [], "cycles": 0, "truncated": False}
    known = set(m.degree)

    def _gid(store_key: str) -> str | None:
        for cand in (store_key + ".md", store_key):
            if cand in known:
                return cand
        return None

    # prereq -> {dependents}, graph keyspace, restricted to notes still in the graph
    fwd: dict[str, set[str]] = {}
    back: dict[str, set[str]] = {}
    for dependent, ps in m.prereq.items():
        d = _gid(dependent)
        if d is None:
            continue
        for p in ps:
            g = _gid(p)
            if g is None or g == d:
                continue
            fwd.setdefault(g, set()).add(d)
            back.setdefault(d, set()).add(g)

    root_key = cooccur_key(root)
    root_gid = _gid(root_key) or (root if root in known else None)
    if root_gid is None:
        return {"root": "", "nodes": [], "edges": [], "cycles": 0, "truncated": False}
    if root_gid not in fwd and root_gid not in back:
        return {"root": root_gid, "nodes": [], "edges": [], "cycles": 0, "truncated": False}

    # Two bounded walks, one per direction. The cap is shared, so a note with
    # 200 dependents cannot crowd out its own prerequisites.
    def walk(adj: dict[str, set[str]]) -> set[str]:
        seen = {root_gid}
        frontier = {root_gid}
        for _ in range(max(0, hops)):
            nxt = {q for n in frontier for q in adj.get(n, ()) if q not in seen}
            if not nxt:
                break
            for q in sorted(nxt):
                if len(seen) >= cap:
                    break
                seen.add(q)
            frontier = nxt & seen
        return seen

    keep = walk(back) | walk(fwd)
    truncated = len(keep) >= cap
    edges = [(u, v) for u in keep for v in sorted(fwd.get(u, ())) if v in keep]

    # Kahn over the induced subgraph. Whatever is left when the queue empties is
    # inside a cycle: those nodes keep the depth their resolved predecessors gave
    # them, and the edges among them are dropped from the layout.
    indeg = {n: 0 for n in keep}
    for _u, v in edges:
        indeg[v] += 1
    depth = {n: 0 for n in keep}
    queue = sorted(n for n, d in indeg.items() if d == 0)
    settled: set[str] = set()
    while queue:
        n = queue.pop(0)
        settled.add(n)
        for v in sorted(fwd.get(n, ())):
            if v not in indeg:
                continue
            depth[v] = max(depth[v], depth[n] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    cyclic = keep - settled
    laid = [(u, v) for u, v in edges if u in settled and v in settled]

    shift = depth.get(root_gid, 0)
    return {
        "root": root_gid,
        "nodes": [
            {"path": n, "depth": depth[n] - shift, "cyclic": n in cyclic}
            for n in sorted(keep, key=lambda n: (depth[n], n))
        ],
        "edges": [{"from": u, "to": v} for u, v in sorted(laid)],
        "cycles": len(cyclic),
        "truncated": truncated,
    }


# (epoch,) -> bursting rows. Separate from StructureMap because it is the one
# variable that needs the note TEXT (a creation stamp), and the map is built
# from the graph and the indexes alone.
_burst_memo: dict[str, list[dict]] = {}


def bursting(*, k: int = 12) -> list[dict]:
    """Concepts over-represented in the last fortnight of WRITING (V6).

    Rebuilt here rather than read off the report because the report's
    co-occurrence depth measured 9.39 s cold and the calendar is a tab you
    flip to. This pass needs the store's note -> stems map and one creation
    timestamp per note, which is the same `_created_and_ai` the report reads,
    falling back to the file's mtime exactly as it does: the two surfaces then
    burst on the same window or neither does.
    """
    from silica.kernel.recall.paths import vault_epoch

    try:
        epoch = vault_epoch()
    except Exception:
        epoch = ""
    if epoch and (hit := _burst_memo.get(epoch)) is not None:
        return hit
    try:
        rows = _build_burst(k)
    except Exception as exc:
        logger.warning("structure: burst unavailable (%s)", exc)
        return []
    if epoch:
        _burst_memo.clear()
        _burst_memo[epoch] = rows
    return rows


def _build_burst(k: int) -> list[dict]:
    from silica.driver import DRIVER
    from silica.driver.base import NoteRef
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.recall.signals import burst
    from silica.kernel.report.learner import _created_and_ai

    store = get_cooccur_store()
    if len(store) == 0:
        return []
    m = structure_map()
    in_graph = [p for p in store.paths() if p + ".md" in m.degree or p in m.degree]
    if not in_graph:
        return []

    # DRIVER.mtime_of, not the note object: the same accessor compute_report's
    # _note_mtimes uses, so the fallback clock is identical on both surfaces.
    mtime_of = getattr(DRIVER, "mtime_of", None)
    created: dict[str, float] = {}
    for key in in_graph:
        gid = key + ".md" if key + ".md" in m.degree else key
        try:
            content = DRIVER.read_note(
                NoteRef(name=gid.rsplit("/", 1)[-1].removesuffix(".md"), path=gid)
            ).content
            mt = mtime_of(gid) if mtime_of else None
        except Exception:
            continue  # a note the index knows and the disk no longer has
        ts, _ai = _created_and_ai(content, mt if mt is not None else 0.0)
        if mt is not None or ts:
            created[key] = ts
    stems = {p: dict(store.note_nodes(p)) for p in in_graph}
    return [
        {"concept": store.node_label(s), "z": z, "recent": nr, "total": na}
        for s, z, nr, na in burst(created, stems)
    ][:k]


_shift_memo: dict[str, list[dict]] = {}


def semantic_shift(*, k: int = 12) -> list[dict]:
    """Notes whose paragraphs pull their single embedding apart (graft G5).

    Mean pairwise cosine distance between a note's paragraph embeddings:
    pooling semantically diverse content provably drags the pooled vector
    toward the centroid (2603.21437, "Pooling and Semantic Shift"), so a
    high-MPD note is one the embed leg represents worse than its length
    suggests. This REPLACES Shannon flatness as the dilution statistic —
    V7's judge gate failed because token entropy does not measure the
    quantity the theorem names (project_graph_variables, 2026-08-22).

    Report row ONLY (surface rule: an unmeasured statistic may state what it
    measured, never accuse a note). Cost is bounded, not free: the 60
    paragraph-richest notes, <= 8 paragraphs each, embedded once per vault
    epoch — one cold pass is ~480 embedder calls' worth of texts, batched.
    """
    from silica.kernel.recall.paths import vault_epoch

    try:
        epoch = vault_epoch()
    except Exception:
        epoch = ""
    if epoch and (hit := _shift_memo.get(epoch)) is not None:
        return hit
    try:
        rows = _build_shift(k)
    except Exception as exc:
        logger.warning("structure: semantic shift unavailable (%s)", exc)
        return []  # embedder down / no driver: the row is absent, never fake
    if epoch:
        _shift_memo.clear()
        _shift_memo[epoch] = rows
    return rows


def _paragraphs(body: str, *, min_chars: int = 200, cap: int = 8) -> list[str]:
    """Blank-line paragraphs long enough to embed meaningfully. Short ones
    (headings, list stubs) would make every note look diverse."""
    paras = [p.strip() for p in body.split("\n\n")]
    return [p for p in paras if len(p) >= min_chars][:cap]


def _build_shift(k: int) -> list[dict]:
    from silica.agent.providers import get_embedder
    from silica.config import CONFIG
    from silica.driver import DRIVER

    per_note: list[tuple[str, list[str]]] = []
    for ref in DRIVER.list_files(None):
        body = DRIVER.read_note(ref.path).content or ""
        paras = _paragraphs(body)
        if len(paras) >= 3:  # MPD over fewer vectors is noise, not dilution
            per_note.append((ref.path.removesuffix(".md"), paras))
    # Dilution needs length: rank candidates by paragraph count and embed only
    # the richest 60, so a cold pass stays one bounded batch per epoch.
    per_note.sort(key=lambda t: -len(t[1]))
    per_note = per_note[:60]
    if not per_note:
        return []

    texts = [t for _p, ps in per_note for t in ps]
    embedder = get_embedder(CONFIG)
    vecs: list[list[float]] = []
    for i in range(0, len(texts), 32):
        vecs.extend(embedder.embed(texts[i:i + 32]))

    rows: list[dict] = []
    at = 0
    for path, paras in per_note:
        vs = vecs[at:at + len(paras)]
        at += len(paras)
        dists: list[float] = []
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                num = sum(a * b for a, b in zip(vs[i], vs[j]))
                na = sum(a * a for a in vs[i]) ** 0.5
                nb = sum(b * b for b in vs[j]) ** 0.5
                if na and nb:
                    dists.append(1.0 - num / (na * nb))
        if dists:
            rows.append({"path": path, "paras": len(paras),
                         "mpd": round(sum(dists) / len(dists), 4)})
    rows.sort(key=lambda r: -r["mpd"])
    return rows[:k]
