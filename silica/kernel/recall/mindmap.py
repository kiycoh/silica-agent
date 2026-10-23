# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L1 Mindmap — deterministic radial map rooted on one note (zero LLM).

Builds a `MapView` — a radial mind-map centred on a single note — from the vault's
wikilink graph plus the latent (embeddings + co-occurrence) relatedness leg. The
builder computes the 2D coordinates **server-side**, so the two surfaces that
materialise one (an Obsidian `.canvas` file, and the GUI graph's arrangement of
a clicked note, ADR-0088) share one geometry and cannot diverge.

Complementary to `graph_export` (which draws the flat whole-vault network): `/graph`
is the network, `/map <note>` is a rooted association field.

Both polar coordinates carry data (viewers read display distance as semantic
distance — the distance-similarity metaphor — so they had better agree):
radius is the association cost to the root (weighted shortest path: wikilink
hop = 1.0, latent tie = 1/normalised strength), and angular neighbours are
ordered by embedding similarity inside each community wedge.

Layout is deterministic (same input → same positions, no `random`, no physics)
and non-overlap is guaranteed by placement order: nodes are placed
closest-first and each slides outward along its own angle until it clears
every already-placed card (exact AABB check against the fixed box size).
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from silica.kernel.recall.cooccurrence import CooccurStore


class GraphLike(Protocol):
    """The slice of an nx.Graph this module actually uses.

    A Protocol, not an import: the mindmap is pure geometry and must not pull
    networkx in. Structural typing states the same contract the caller already
    satisfies, and now states it where a checker can see it.
    """

    def __contains__(self, node: object) -> bool: ...

    def neighbors(self, node: str) -> Iterable[str]: ...


# (node id, node id) -> cosine.
SimFn = Callable[[str, str], float]

# Fixed node-box size in canvas units. The non-overlap guarantee is stated in
# terms of these: distance ≥ hypot(W, H) ⇒ the two boxes' AABBs are disjoint.
# The .canvas draws a card this size; the GUI graph draws a dot and a name,
# and the box is the room that name is given (ADR-0069, ADR-0088).
BOX_W = 220.0
BOX_H = 64.0
# shrinking these is the ONLY compactness knob. _DIAG is the spacing
# unit every ring radius is a multiple of, so smaller boxes scale the whole
# layout tighter with wedge angles and ring count untouched; non-overlap still
# holds by construction (distance >= hypot(W,H), recomputed from the new size).
_DIAG = math.hypot(BOX_W, BOX_H)

# Muted slate for community-less nodes (group == -1) — never black/white.
_MUTED = "#5a6372"


@dataclass
class MapNode:
    id: str          # stable: vault-relative path WITH .md (e.g. "concetti/x.md")
    path: str        # same as id; the file the Canvas node points at
    title: str
    x: float
    y: float
    community: int   # global Louvain membership (reused from graph_export); -1 = none
    hop: int         # 0 = root, 1, 2
    cost: float = 0.0    # association cost to the root (radius input); 0 = root


@dataclass
class MapEdge:
    src: str          # node id
    dst: str          # node id
    kind: str         # "wikilink" | "latent"
    weight: float


@dataclass
class MapView:
    root: str
    nodes: list[MapNode]
    edges: list[MapEdge]


@dataclass
class MapMaterials:
    """Everything build_mapview needs, injectable so tests need no live vault.

    `graph` is an undirected view of the wikilink graph (ids carry `.md`).
    `latent` is the relatedness leg in fused-ranking order: (id_with_md, title,
    strength) where strength is a native 0-1 signal (embed cosine / edge
    Jaccard), NOT the RRF score — the radius consumes it as 1/strength.
    """
    graph: GraphLike
    titles: dict[str, str]                # id -> display title
    community_of: dict[str, int]          # id -> global community (missing ⇒ -1)
    latent: list[tuple[str, str, float]] = field(default_factory=list)
    sim: SimFn | None = None              # None to abstain


def node_color(community: int) -> str:
    """Community colour, shared with /graph; muted slate for -1 (no community)."""
    if community < 0:
        return _MUTED
    from silica.kernel.recall.graph_export import _community_color
    return _community_color(community)


# ---------------------------------------------------------------------------
# Neighbourhood selection + cap
# ---------------------------------------------------------------------------

def _with_md(path: str) -> str:
    return path if path.endswith(".md") else path + ".md"


def _stem_title(node_id: str) -> str:
    return node_id.rsplit("/", 1)[-1].removesuffix(".md")


def _bfs(root: str, graph: GraphLike, hops: int) -> dict[str, tuple[int, str | None]]:
    """BFS up to `hops` on the undirected wikilink graph.

    Returns id -> (hop, parent_id); root maps to (0, None). Neighbours are
    visited in sorted order so the result is deterministic.
    """
    seen: dict[str, tuple[int, str | None]] = {root: (0, None)}
    q: deque[str] = deque([root])
    while q:
        u = q.popleft()
        hop, _ = seen[u]
        if hop >= hops or u not in graph:
            continue
        for v in sorted(graph.neighbors(u)):
            if v not in seen:
                seen[v] = (hop + 1, u)
                q.append(v)
    return seen


def _select(
    root: str,
    materials: MapMaterials,
    *,
    max_nodes: int,
    hops: int,
) -> dict[str, int]:
    """Pick the capped node set: root + wikilink BFS + latent, nearest first.

    Returns id -> hop, the root first; a latent neighbour hangs off the root
    at hop 1. The hop-1 wikilinks go first, then everything else together; inside each
    group the association cost to the root decides, the same cost the radius
    draws (ADR-0088). Ranking by tier alone kept every hop-2 note ahead of
    every related one, although the strongest related tie costs one wikilink
    and a hop-2 note at least two, and broke every tie inside a tier by path:
    "Machine learning" kept 12 of its 37 hop-2 notes alphabetically.
    """
    reached = _bfs(root, materials.graph, hops=hops)
    latent_ids = [_with_md(lid) for lid, _title, _strength in materials.latent]
    candidates = [nid for nid in reached if nid != root]
    candidates += [lid for lid in dict.fromkeys(latent_ids) if lid != root and lid not in reached]

    room = max(0, max_nodes - 1)
    if len(candidates) > room:
        # Costed over every candidate, before the cut: a cost depends on the
        # chain that reaches a note, and the chain may run through one the cut
        # is about to drop.
        ids = {root, *candidates}
        cost = _association_costs(root, _edges(root, ids, materials), ids, materials.sim)
        hop1 = sorted((n for n in candidates if reached.get(n, (0,))[0] == 1),
                      key=lambda n: (cost[n], n))
        rest = sorted((n for n in candidates if reached.get(n, (0,))[0] != 1),
                      key=lambda n: (cost[n], n))
        candidates = (hop1 + rest)[:room]
    return {root: 0, **{nid: reached[nid][0] if nid in reached else 1 for nid in candidates}}


def _edges(root: str, ids: set[str], materials: MapMaterials) -> list[MapEdge]:
    """Every wikilink among `ids` (tree and cross-branch), then root → each
    latent neighbour in `ids` that no wikilink already joins to the root."""
    graph = materials.graph
    edges: list[MapEdge] = []
    seen_pairs: set[tuple[str, str]] = set()
    for u in ids:
        if u not in graph:
            continue
        for v in graph.neighbors(u):
            if v not in ids:
                continue
            key = (u, v) if u <= v else (v, u)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            edges.append(MapEdge(src=key[0], dst=key[1], kind="wikilink", weight=1.0))
    for lid, _title, strength in materials.latent:
        # As given when it is one of the ids: the graph's lit set carries ids
        # that are not notes (an unresolved link), and `.md` would miss them.
        lid = lid if lid in ids else _with_md(lid)
        if lid not in ids or lid == root:
            continue
        key = (root, lid) if root <= lid else (lid, root)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        edges.append(MapEdge(src=root, dst=lid, kind="latent", weight=float(strength)))
    return edges


# ---------------------------------------------------------------------------
# Radial association layout (deterministic; the only new algorithmic piece)
# ---------------------------------------------------------------------------

def _similarity_chain(members: list[MapNode], sim: SimFn | None) -> list[MapNode]:
    """Greedy nearest-neighbour ordering: each next node is the one most
    similar to the last placed, so angular neighbours are semantic ones.
    Falls back to id order when there is no similarity signal."""
    rest = sorted(members, key=lambda n: n.id)
    if sim is None or len(rest) < 3:
        return rest
    out = [rest.pop(0)]
    while rest:
        rest.sort(key=lambda n: (-sim(out[-1].id, n.id), n.id))
        out.append(rest.pop(0))
    return out


def _wedge_order(by_comm: dict[int, list[MapNode]], sim: SimFn | None) -> list[int]:
    """Communities as an angular sequence: largest first, then greedy by mean
    cross-similarity, so related communities share a wedge border."""
    comms = sorted(by_comm)
    if sim is None or len(comms) < 3:
        return comms

    def cross(a: int, b: int) -> float:
        vals = [sim(x.id, y.id) for x in by_comm[a] for y in by_comm[b]]
        return sum(vals) / len(vals) if vals else 0.0

    rest = sorted(comms, key=lambda c: (-len(by_comm[c]), c))
    out = [rest.pop(0)]
    while rest:
        rest.sort(key=lambda c: (-cross(out[-1], c), c))
        out.append(rest.pop(0))
    return out


# A wikilink's cost band. The floor is the map's anchor (the tightest tie of
# either kind costs exactly 1.0); the ceiling stays under 2.0 so ONE wikilink,
# however loose, never costs more than two tight ones — the written link keeps
# its structural rank over any chain that replaces it.
_WIKI_COST_SPAN = 0.9


def _association_costs(
    root: str, edges: list[MapEdge], ids: set[str], sim: SimFn | None = None
) -> dict[str, float]:
    """Weighted shortest-path cost root → every selected node (Dijkstra).

    Edge costs. Latent: s_max/s, where s_max is the best latent strength on the
    map — the strongest inferred tie costs exactly one tight wikilink. Wikilink:
    1.0 + span·(1 − t), where t is the pair's embedding similarity min-max
    normalised over the map's own wikilinks. Both legs are normalised WITHIN the
    map because embed cosine and edge Jaccard share a 0-1 range but not a
    calibration; only their ratios are trusted.

    Flat 1.0 without a similarity signal — which is also what kept hub roots
    degenerate: a root whose neighbours fill the node cap put every one of them
    at cost 1.0, so radius carried nothing and the collision slide (alphabetical)
    decided the spread. Similarity is what makes the band continuous there.
    """
    latent_s = [e.weight for e in edges if e.kind == "latent" and e.weight > 0]
    s_max = max(latent_s) if latent_s else 1.0

    wiki_sim: dict[int, float] = {}
    if sim is not None:
        for i, e in enumerate(edges):
            if e.kind == "wikilink":
                wiki_sim[i] = sim(e.src, e.dst)
    lo, hi = (min(wiki_sim.values()), max(wiki_sim.values())) if wiki_sim else (0.0, 0.0)

    def wiki_cost(i: int) -> float:
        if i not in wiki_sim or hi <= lo:
            return 1.0
        t = (wiki_sim[i] - lo) / (hi - lo)
        return 1.0 + _WIKI_COST_SPAN * (1.0 - t)

    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for i, e in enumerate(edges):
        w = wiki_cost(i) if e.kind == "wikilink" else s_max / max(float(e.weight), 1e-6)
        adj[e.src].append((e.dst, w))
        adj[e.dst].append((e.src, w))

    dist = {root: 0.0}
    heap: list[tuple[float, str]] = [(0.0, root)]   # node id breaks ties ⇒ deterministic
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w in sorted(adj[u]):
            nd = d + w
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                heapq.heappush(heap, (nd, v))

    far = max(dist.values(), default=0.0) + 1.0     # disconnected safety: park at the rim
    for nid in ids:
        dist.setdefault(nid, far)
    return dist


def _layout(nodes: list[MapNode], *, costs: dict[str, float], sim: SimFn | None = None) -> None:
    """Place nodes radially, mutating each node's x/y. Root stays at (0, 0).

    Angle: 360° is partitioned into one contiguous wedge per community, width
    ∝ the community's node count; wedges and the siblings inside them are
    ordered by similarity chains (see _wedge_order/_similarity_chain), so
    angular proximity reads as semantic proximity.

    Radius: association cost mapped into a radial band — closer to the root ⇒
    more strongly tied. Non-overlap is guaranteed by placement order: nodes
    are placed cheapest-first and each slides outward along its own angle
    until it clears every already-placed card (exact AABB test), so a node
    only ever moves AWAY from its data-given radius, never inward.
    """
    non_root = [n for n in nodes if n.hop > 0]
    if not non_root:
        return

    by_comm: dict[int, list[MapNode]] = defaultdict(list)
    for n in non_root:
        by_comm[n.community].append(n)

    total = len(non_root)
    order = _wedge_order(by_comm, sim)
    angle: dict[str, float] = {}
    cursor = 0.0
    for c in order:
        width = 2 * math.pi * len(by_comm[c]) / total
        members = _similarity_chain(by_comm[c], sim)
        slot = width / len(members)
        for i, n in enumerate(members):
            angle[n.id] = cursor + (i + 0.5) * slot
        cursor += width

    cmin = min(costs[n.id] for n in non_root)
    cmax = max(costs[n.id] for n in non_root)
    # More nodes ⇒ a wider band, capped: the band buys radial fidelity and pays
    # in card legibility (the viewBox fits the whole map, so a wider band shrinks
    # every card). Measured on a 34-node hub map, cost↔radius correlation vs the
    # map's outer radius: ×2.5→.88/892, ×3→.91/958, ×4→.97/1146, ×6.8→1.00/1787.
    # ×3 is the knee — the text is still readable and the gradient still reads.
    spread = _DIAG * min(3.0, max(2.0, total / 5.0))

    def desired(c: float) -> float:
        t = (c - cmin) / (cmax - cmin) if cmax > cmin else 0.0
        return _DIAG + t * spread                   # _DIAG floor clears the root box

    placed = [n for n in nodes if n.hop == 0]       # the root blocks the centre
    step = _DIAG / 3
    for n in sorted(non_root, key=lambda m: (costs[m.id], m.id)):
        a = angle[n.id]
        ca, sa = math.cos(a), math.sin(a)
        r = desired(costs[n.id])
        guard = 0
        while guard < 2000 and any(
            abs(r * ca - p.x) < BOX_W and abs(r * sa - p.y) < BOX_H for p in placed
        ):
            r += step
            guard += 1
        n.x, n.y = r * ca, r * sa
        placed.append(n)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_mapview(
    root: str,
    materials: MapMaterials,
    *,
    max_nodes: int = 35,
    hops: int = 2,
) -> MapView:
    """Build a MapView rooted on `root` from injected materials (pure)."""
    root = _with_md(root)
    selected = _select(root, materials, max_nodes=max_nodes, hops=hops)
    nodes = [
        MapNode(id=nid, path=nid, title=materials.titles.get(nid, _stem_title(nid)),
                x=0.0, y=0.0, community=materials.community_of.get(nid, -1), hop=hop)
        for nid, hop in selected.items()
    ]
    ids = {n.id for n in nodes}
    edges = _edges(root, ids, materials)
    costs = _association_costs(root, edges, ids, materials.sim)
    for n in nodes:
        n.cost = costs[n.id]
    _layout(nodes, costs=costs, sim=materials.sim)
    return MapView(root=root, nodes=nodes, edges=edges)


def arrange_mapview(
    root: str,
    ids: Iterable[str],
    *,
    graph: GraphLike,
    community_of: dict[str, int],
    sim: SimFn | None,
) -> MapView:
    """Lay out a set the caller chose: the graph's lit notes (ADR-0088).

    No selection and no cap, since the graph already draws every lit note; the
    geometry is build_mapview's. A note no wikilink joins to the root (lit by
    the k-NN, a gap, a proposal) is a latent tie with the strength of its
    similarity to the root, which is how the Map prices its related notes. One
    with nothing to price it by (an unresolved link, a note with no vector)
    parks on the rim, as _association_costs does with a disconnected node.
    """
    given = [i for i in dict.fromkeys(ids) if i != root]
    linked = set(graph.neighbors(root)) if root in graph else set()
    latent: list[tuple[str, str, float]] = []
    if sim is not None:
        for nid in given:
            s = 0.0 if nid in linked else sim(root, nid)
            if s > 0:
                latent.append((nid, "", s))
    materials = MapMaterials(graph=graph, titles={}, community_of=community_of,
                             latent=latent, sim=sim)
    ids_all = {root, *given}
    edges = _edges(root, ids_all, materials)
    costs = _association_costs(root, edges, ids_all, sim)
    nodes = [
        MapNode(id=nid, path=nid, title=_stem_title(nid), x=0.0, y=0.0,
                community=community_of.get(nid, -1), hop=0 if nid == root else 1,
                cost=costs[nid])
        for nid in [root, *given]
    ]
    _layout(nodes, costs=costs, sim=sim)
    return MapView(root=root, nodes=nodes, edges=edges)


_TAU = 2 * math.pi


def _wedges(nodes: list[MapNode]) -> list[tuple[int, float, float, list[MapNode]]]:
    """(community, a0, a1, members) per wedge, read back off the positions.

    _layout gives each community one contiguous arc and moves a note only
    outward along its own angle, so sorting by angle recovers the wedges
    exactly; a boundary sits halfway between two neighbouring wedges' notes.
    Read here rather than returned by _layout so the .canvas path, which has no
    wedges to draw, keeps the MapView it always had.
    """
    ring = sorted(nodes, key=lambda n: math.atan2(n.y, n.x) % _TAU)
    runs: list[list] = []
    for n in ring:
        a = math.atan2(n.y, n.x) % _TAU
        if runs and runs[-1][0] == n.community:
            runs[-1][2] = a
            runs[-1][3].append(n)
        else:
            runs.append([n.community, a, a, [n]])
    if len(runs) == 1:
        c, _a, _b, members = runs[0]
        return [(c, 0.0, _TAU, members)]
    out = []
    for i, (c, first, last, members) in enumerate(runs):
        prev_last = runs[i - 1][2] - (_TAU if i == 0 else 0.0)
        nxt_first = runs[(i + 1) % len(runs)][1] + (_TAU if i == len(runs) - 1 else 0.0)
        out.append((c, (prev_last + first) / 2, (last + nxt_first) / 2, members))
    return out


# Where an area's wedge starts (clear of the root's own name) and how far it
# runs past its farthest note, in the layout's units. Read by the graph frame,
# which draws the wedges of an arrangement (ADR-0069 decision 5).
_WEDGE_IN = _DIAG * 0.3
_WEDGE_REACH = _DIAG * 0.42


def arrangement_payload(mv: MapView) -> dict:
    """What the graph frame needs to draw an arrangement: each id's position
    and cost, and each area's wedge with the radius its outline runs to."""
    rest = [n for n in mv.nodes if n.hop > 0]
    wedges = [
        {"area": c, "a0": a0, "a1": a1, "n": len(members),
         "r": round(max(math.hypot(m.x, m.y) for m in members) + _WEDGE_REACH, 1)}
        for c, a0, a1, members in (_wedges(rest) if rest else [])
    ]
    return {
        "nodes": {n.id: [*_xy(n), round(n.cost, 3)] for n in mv.nodes},
        "wedges": wedges,
        "r_in": round(_WEDGE_IN, 1),
    }


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _xy(n: MapNode) -> tuple[int, int]:
    """Rounded integer coordinates — shared by both serializers so they agree."""
    return round(n.x), round(n.y)


def _side(dx: float, dy: float) -> str:
    """Nearest box side for an edge leaving toward (dx, dy). Canvas y grows down."""
    if abs(dx) >= abs(dy):
        return "right" if dx >= 0 else "left"
    return "bottom" if dy >= 0 else "top"


# Wikilink edges keep a full colour; latent edges degrade to a muted colour +
# the "≈" label, because JSON Canvas has no dashed-edge style (only colour+label).
_CANVAS_WIKI_COLOR = "#22d3ee"
_CANVAS_LATENT_COLOR = _MUTED


def mapview_to_canvas(mv: MapView) -> dict:
    """Serialize a MapView to a JSON Canvas dict (jsoncanvas.org)."""
    nodes = []
    for n in mv.nodes:
        x, y = _xy(n)
        node: dict = {
            "id": n.id,
            "type": "file",
            "file": n.path,
            "x": x - round(BOX_W / 2),
            "y": y - round(BOX_H / 2),
            "width": round(BOX_W),
            "height": round(BOX_H),
        }
        if n.community >= 0:
            node["color"] = node_color(n.community)
        nodes.append(node)

    by_id = {n.id: n for n in mv.nodes}
    edges = []
    for i, e in enumerate(mv.edges):
        s, d = by_id[e.src], by_id[e.dst]
        latent = e.kind == "latent"
        edges.append({
            "id": f"e{i}",
            "fromNode": e.src,
            "toNode": e.dst,
            "fromSide": _side(d.x - s.x, d.y - s.y),
            "toSide": _side(s.x - d.x, s.y - d.y),
            "color": _CANVAS_LATENT_COLOR if latent else _CANVAS_WIKI_COLOR,
            "label": "≈" if latent else "",
        })
    return {"nodes": nodes, "edges": edges}


def _matches_in(note: str, note_paths: list[str], titles: dict[str, str]) -> list[str]:
    """Every graph key `note` (a path OR a title) can mean, sorted. Pure.

    Graph keys are full vault-relative paths WITH .md; a user (or the GUI input)
    may give a bare title or a path with/without .md. An exact path is the one
    answer; otherwise every note whose basename or title matches,
    case-insensitively.
    """
    keys = sorted(p.replace("\\", "/") for p in note_paths)
    key_set = set(keys)
    cand = note.replace("\\", "/").strip()
    for c in (cand, cand + ".md", cand.removesuffix(".md")):
        if c in key_set:
            return [c]
    target = cand.removesuffix(".md").rsplit("/", 1)[-1].lower()
    return [path for path in keys
            if path.removesuffix(".md").rsplit("/", 1)[-1].lower() == target
            or (titles.get(path, "") or "").lower() == target]


def _resolve_in(note: str, note_paths: list[str], titles: dict[str, str]) -> str | None:
    """Resolve `note` (a path OR a title) to a graph key; the first sorted
    match when several notes share the name, so it is stable."""
    hits = _matches_in(note, note_paths, titles)
    return hits[0] if hits else None


def _driver_graph():
    """One driver read → (raw notes map, {path: title}, undirected wikilink graph)."""
    from silica.driver import get_driver

    notes, _unresolved, g = get_driver().graph_data()
    return (
        notes,
        {p.replace("\\", "/"): ref.name for p, ref in notes.items()},
        g.to_undirected(as_view=True) if hasattr(g, "to_undirected") else g,
    )


def note_resolver():
    """One driver read → a pure closure: ref (path or title) -> graph key | None.

    Reuse when resolving many refs per render (e.g. linkifying a message): the
    driver graph is read once, the returned callable does no further IO.
    """
    notes, titles, _g = _driver_graph()
    paths = list(notes)
    return lambda ref: _resolve_in(ref, paths, titles)


def answer_resolver(read_refs=(), graph=None) -> tuple:
    """(resolve, read) for a chat answer, from one driver read (ADR-0127);
    `graph`, a `_driver_graph()` already read, spares it for many answers.

    `read` is the notes the turn read, as graph keys: each of `read_refs` that
    names exactly one note. `resolve` differs from note_resolver's on a name
    several notes share (ADR-0114: never resolved silently): it is the one of
    them the turn read, or "" when that is not exactly one. An answer that
    named "Lezione 16" from one course used to open the other course's
    lecture, whichever sorted first. "" and not None, because None draws a
    broken link, and the note is there, twice.
    """
    notes, titles, _g = graph or _driver_graph()
    paths = list(notes)
    read = sorted({hits[0] for r in read_refs
                   if len(hits := _matches_in(str(r), paths, titles)) == 1})
    mine = set(read)

    def resolve(ref: str) -> str | None:
        hits = _matches_in(ref, paths, titles)
        if len(hits) < 2:
            return hits[0] if hits else None
        ours = [h for h in hits if h in mine]
        return ours[0] if len(ours) == 1 else ""

    return resolve, read


def reading_path(
    src: str,
    dst: str,
    *,
    graph: GraphLike | None = None,
    cooccur_store: "CooccurStore | None" = None,
    weighted: bool = False,
) -> list[tuple[str, str]] | None:
    """Shortest reading path src → dst: BFS over wikilinks + latent cooccur edges.

    Endpoints are resolved graph keys (vault-relative, with .md). Returns
    [(path, leg), ...] where leg says how the node was reached from the
    previous one ("start" | "wikilink" | "cooccur"); None when the two notes
    are not connected. Read-only. Pass graph/cooccur_store to skip loading
    the live vault (tests).

    With `weighted=True` the hop count gives way to association strength:
    edge cost = 1/strength (wikilink strength 1.0, cooccur strength = its
    Jaccard score, so a weak 0.25 bridge costs 4 wikilink hops) and Dijkstra
    picks the strongest chain instead of the fewest hops.
    """
    if graph is None:
        _notes, _titles, graph = _driver_graph()
    if cooccur_store is None:  # embed leg unused here — load only the cooccur half
        cooccur_store = _cooccur_store()

    def neighbors(u: str) -> list[tuple[str, tuple[str, float]]]:
        out: dict[str, tuple[str, float]] = {}
        if cooccur_store is not None:
            # O(deg) per node: the store serves this from a two-way adjacency
            # built once per mutation, so the walk is O(V+E), not O(V·E).
            for nb, score in cooccur_store.note_edges_for(u).items():
                out[nb + ".md"] = ("cooccur", max(float(score), 1e-6))
        if u in graph:
            for nb in graph.neighbors(u):
                out[nb] = ("wikilink", 1.0)  # wikilink wins when both legs share an edge
        return sorted(out.items())

    prev: dict[str, tuple[str | None, str]] = {src: (None, "start")}
    if weighted:
        dist = {src: 0.0}
        heap: list[tuple[float, str]] = [(0.0, src)]  # node id breaks ties ⇒ deterministic
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, math.inf):
                continue
            if u == dst:
                break
            for v, (leg, strength) in neighbors(u):
                nd = d + 1.0 / strength
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, leg)
                    heapq.heappush(heap, (nd, v))
    else:
        q: deque[str] = deque([src])
        while q:
            u = q.popleft()
            if u == dst:
                break
            for v, (leg, _strength) in neighbors(u):
                if v not in prev:
                    prev[v] = (u, leg)
                    q.append(v)
    if dst not in prev:
        return None
    steps: list[tuple[str, str]] = []
    node: str | None = dst
    while node is not None:
        parent, leg = prev[node]
        steps.append((node, leg))
        node = parent
    steps.reverse()
    return steps


def _embed_sim():
    """(id, id) -> stored-vector cosine, or None when the embed index is empty.

    Ids carry .md, store keys don't — bridged through cooccur_key like
    knn_edges. Vectors are cached per id; a missing vector abstains with 0.0.
    """
    try:
        from silica.kernel.recall.cooccurrence import cooccur_key
        from silica.kernel.recall.embed import _cosine, get_store

        store = get_store()
        if len(store) == 0:
            return None
    except Exception:
        return None

    cache: dict[str, list[float] | None] = {}

    def _vec(nid: str):
        if nid not in cache:
            cache[nid] = store.get_vec(cooccur_key(nid))
        return cache[nid]

    def sim(a: str, b: str) -> float:
        va, vb = _vec(a), _vec(b)
        return _cosine(va, vb) if va is not None and vb is not None else 0.0

    return sim


def gather_materials(root: str, *, latent_k: int = 10) -> MapMaterials:
    """Collect wikilink graph, titles, global communities, the latent leg and
    the embedding similarity closure."""
    from silica.kernel.recall.graph_export import build_graph_data, detect_communities
    from silica.kernel.recall.relatedness import related_notes

    _notes, titles, undirected = _driver_graph()

    nodes, edges = build_graph_data()
    detect_communities(nodes, edges)  # assigns node["group"] in place (global, seed=42)
    community_of = {n["id"]: n.get("group", -1) for n in nodes}

    latent: list[tuple[str, str, float]] = []
    try:
        embed_store, cooccur_store = _load_stores()
        for r in related_notes(
            _with_md(root), embed_store=embed_store, cooccur_store=cooccur_store, k=latent_k
        ):
            # Radius wants a native 0-1 strength, not the RRF score (ordering
            # only). Cooccur-only rows carry no such signal: park them just
            # under the weakest scored one — they are the ranking's tail anyway.
            latent.append((_with_md(r.path), r.name, r.embed_score or 0.0))
    except Exception:
        # The latent leg abstains when its stores cannot be read: the map is
        # then the wikilink BFS alone, which is still a correct map.
        latent = []
    if latent:
        floor = min((s for _, _, s in latent if s > 0), default=0.3) * 0.9
        latent = [(lid, t, s if s > 0 else floor) for lid, t, s in latent]

    return MapMaterials(
        graph=undirected,
        titles=titles,
        community_of=community_of,
        latent=latent,
        sim=_embed_sim(),
    )


def arrange_in_vault(root: str, ids: list[str], community_of: dict[str, int]) -> dict:
    """arrange_mapview over the active vault's wikilinks and stored vectors.

    The frame sends the communities, so nothing here runs build_graph_data or
    Louvain: the driver graph and the similarity closure measured 0.003 s on
    the test vault, against 0.23-0.58 s for gather_materials (ADR-0088).
    """
    _notes, _titles, graph = _driver_graph()
    mv = arrange_mapview(root, ids, graph=graph, community_of=community_of, sim=_embed_sim())
    return arrangement_payload(mv)


def _cooccur_store() -> "CooccurStore | None":
    """The cooccur store, or None when empty/unavailable ⇒ that leg abstains."""
    try:
        from silica.config import CONFIG
        from silica.kernel.recall.cooccurrence import get_cooccur_store

        cs = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        return cs if len(cs) > 0 else None
    except Exception:
        return None


def _load_stores():
    """(embed_store, cooccur_store), each None when empty/unavailable ⇒ leg abstains."""
    embed_store = None
    try:
        from silica.kernel.recall.embed import get_store
        es = get_store()
        embed_store = es if len(es) > 0 else None
    except Exception:
        embed_store = None

    return embed_store, _cooccur_store()
