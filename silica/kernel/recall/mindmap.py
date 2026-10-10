# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L1 Mindmap — deterministic radial map rooted on one note (zero LLM).

Builds a `MapView` — a radial mind-map centred on a single note — from the vault's
wikilink graph plus the latent (embeddings + co-occurrence) relatedness leg. The
builder computes the 2D coordinates **server-side**, so the two surfaces that
materialise a MapView (an Obsidian `.canvas` file and the web GUI's static SVG)
show the *identical* map and cannot diverge.

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
import re
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
# (parent id, child id) -> the anchor line that justifies the link, or None.
LinkContextFn = Callable[[str, str], "str | None"]

# Fixed node-box size in canvas units. The non-overlap guarantee is stated in
# terms of these: distance ≥ hypot(W, H) ⇒ the two boxes' AABBs are disjoint.
# The .canvas draws a card this size; the GUI draws a dot and a label, and the
# box is the room that label is given (render_map_svg, ADR-0069).
BOX_W = 220.0
BOX_H = 64.0
# shrinking these is the ONLY compactness knob. _DIAG is the spacing
# unit every ring radius is a multiple of, so smaller boxes scale the whole
# layout tighter with wedge angles and ring count untouched; non-overlap still
# holds by construction (distance >= hypot(W,H), recomputed from the new size).
_DIAG = math.hypot(BOX_W, BOX_H)

# Muted slate for community-less nodes (group == -1) — never black/white.
_MUTED = "#5a6372"
_MUTED_PAPER = "#7d8496"   # the same no-community slate, read against paper


@dataclass
class MapNode:
    id: str          # stable: vault-relative path WITH .md (e.g. "concetti/x.md")
    path: str        # same as id; the file the Canvas node points at
    title: str
    x: float
    y: float
    community: int   # global Louvain membership (reused from graph_export); -1 = none
    hop: int         # 0 = root, 1, 2
    subtitle: str | None = None
    cost: float = 0.0    # association cost to the root (radius input); 0 = root
    degree: int = 0      # global wikilink degree — landmark salience, not layout


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
    areas: dict[int, str] = field(default_factory=dict)  # community -> its name, for the wedges


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
    latent_evidence: dict[str, str] = field(default_factory=dict)  # id -> display "why"
    sim: SimFn | None = None              # None to abstain
    link_context: LinkContextFn | None = None
    community_label: dict[int, str] = field(default_factory=dict)  # missing ⇒ the wedge goes unnamed


def node_color(community: int, on_paper: bool = False) -> str:
    """Community colour, shared with /graph; muted slate for -1 (no community).

    `on_paper` picks the light-floor band. Both are emitted onto every card so
    the map can switch theme without a rebuild — see render_map_svg.
    """
    if community < 0:
        return _MUTED_PAPER if on_paper else _MUTED
    from silica.kernel.recall.graph_export import _community_color
    return _community_color(community, on_paper=on_paper)


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
) -> tuple[dict[str, tuple[int, str | None]], set[str]]:
    """Pick the capped node set: root + wikilink BFS + latent, priority-ordered.

    Returns (selected, latent_only): selected is id -> (hop, parent);
    latent_only are the ids that entered through the latent leg alone.
    Priority tiers (kept top `max_nodes`): root, wikilink hop-1, deeper
    wikilink hops, then latent in list order — `materials.latent` arrives in
    fused-ranking order, so the index IS the selection priority (the per-item
    strength is a radius signal, not a ranking one).
    """
    reached = _bfs(root, materials.graph, hops=hops)
    candidates: list[tuple[int, float, str]] = []  # (tier, rank, id) sort key

    for nid, (hop, _parent) in reached.items():
        if nid == root:
            continue
        tier = 1 if hop == 1 else 2  # hop-1 outranks all deeper wikilink hops
        candidates.append((tier, 0.0, nid))

    for i, (lid, _title, _strength) in enumerate(materials.latent):
        if lid == root or lid in reached:
            continue
        candidates.append((3, float(i), lid))

    candidates.sort()
    kept = {root}
    for _tier, _rank, nid in candidates[: max(0, max_nodes - 1)]:
        kept.add(nid)

    selected: dict[str, tuple[int, str | None]] = {root: (0, None)}
    latent_only: set[str] = set()
    for nid in kept:
        if nid == root:
            continue
        if nid in reached:
            selected[nid] = reached[nid]
        else:
            selected[nid] = (1, root)  # latent neighbours hang off the root
            latent_only.add(nid)
    return selected, latent_only


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
    selected, latent_only = _select(root, materials, max_nodes=max_nodes, hops=hops)
    graph = materials.graph

    def _degree(nid: str) -> int:
        try:
            return len(list(graph.neighbors(nid))) if nid in graph else 0
        except Exception:
            return 0

    parent = {nid: p for nid, (_hop, p) in selected.items()}
    nodes: list[MapNode] = []
    for nid, (hop, _p) in selected.items():
        nodes.append(
            MapNode(
                id=nid,
                path=nid,
                title=materials.titles.get(nid, _stem_title(nid)),
                x=0.0,
                y=0.0,
                community=materials.community_of.get(nid, -1),
                hop=hop,
                subtitle=nid.rsplit("/", 1)[0] if "/" in nid else None,
                degree=_degree(nid),
            )
        )

    ids = {n.id for n in nodes}
    edges: list[MapEdge] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Wikilink edges: any graph edge among the selected nodes (tree + cross-branch).
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

    # Latent edges: root → each surviving latent neighbour not already wiki-linked.
    for lid, _title, strength in materials.latent:
        lid = _with_md(lid)
        if lid not in ids or lid == root:
            continue
        key = (root, lid) if root <= lid else (lid, root)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        edges.append(MapEdge(src=root, dst=lid, kind="latent", weight=float(strength)))

    costs = _association_costs(root, edges, ids, materials.sim)
    for n in nodes:
        n.cost = costs[n.id]

    # Subtitle = the WHY of the relation, when the materials can supply it:
    # the anchor line for wikilinks, the leg evidence for latent ties. The
    # folder path (set above) stays as the fallback and as the root's own.
    for n in nodes:
        if n.hop == 0:
            continue
        if n.id in latent_only:
            n.subtitle = materials.latent_evidence.get(n.id) or n.subtitle
        elif materials.link_context is not None:
            p = parent.get(n.id)
            ctx = materials.link_context(p, n.id) if p else None
            if ctx:
                n.subtitle = ctx

    _layout(nodes, costs=costs, sim=materials.sim)
    present = {n.community for n in nodes}
    areas = {c: name for c, name in materials.community_label.items() if c in present}
    return MapView(root=root, nodes=nodes, edges=edges, areas=areas)


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


# ---------------------------------------------------------------------------
# Static SVG render (GUI surface; native, zero deps, positions precomputed)
# ---------------------------------------------------------------------------

_TAU = 2 * math.pi


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _sector_path(a0: float, a1: float, r_in: float, r_out: float) -> str:
    """An annular sector from angle a0 to a1 (radians, a1 > a0)."""
    if a1 - a0 >= _TAU - 1e-9:   # one area holds the whole map: a full ring
        return (f"M {r_out:.1f} 0 A {r_out:.1f} {r_out:.1f} 0 1 1 {-r_out:.1f} 0 "
                f"A {r_out:.1f} {r_out:.1f} 0 1 1 {r_out:.1f} 0 Z "
                f"M {r_in:.1f} 0 A {r_in:.1f} {r_in:.1f} 0 1 0 {-r_in:.1f} 0 "
                f"A {r_in:.1f} {r_in:.1f} 0 1 0 {r_in:.1f} 0 Z")
    large = 1 if a1 - a0 > math.pi else 0
    c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
    return (f"M {r_in * c0:.1f} {r_in * s0:.1f} L {r_out * c0:.1f} {r_out * s0:.1f} "
            f"A {r_out:.1f} {r_out:.1f} 0 {large} 1 {r_out * c1:.1f} {r_out * s1:.1f} "
            f"L {r_in * c1:.1f} {r_in * s1:.1f} "
            f"A {r_in:.1f} {r_in:.1f} 0 {large} 0 {r_in * c0:.1f} {r_in * s0:.1f} Z")


def _arc_path(a0: float, a1: float, r: float) -> str:
    """The outer edge of a wedge alone, which is what draws the map's outline."""
    if a1 - a0 >= _TAU - 1e-9:
        return f"M {r:.1f} 0 A {r:.1f} {r:.1f} 0 1 1 {-r:.1f} 0 A {r:.1f} {r:.1f} 0 1 1 {r:.1f} 0"
    large = 1 if a1 - a0 > math.pi else 0
    return (f"M {r * math.cos(a0):.1f} {r * math.sin(a0):.1f} "
            f"A {r:.1f} {r:.1f} 0 {large} 1 {r * math.cos(a1):.1f} {r * math.sin(a1):.1f}")


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


# The page around the drawing. Plain strings, not f-strings, so the CSS and the
# script read as the CSS and the script; the per-map data rides on the markup.
# Tokens restate static/app-base.css (graphite + paper, the 189deg signal) the
# way graph-frame.css does, because this document is served on its own and has
# no sheet to link. Keep the two in step: the old block here was still the
# blue-black #0A0D14 ramp with a #00A5E1 signal a whole identity after the app
# moved off it (measured 2026-10-06).
_MAP_CSS = """
@font-face{font-family:"Archivo";src:url("/static/archivo-latin.woff2") format("woff2");
  font-weight:100 900;font-stretch:62% 125%;font-display:swap}
:root{color-scheme:dark;
  --void:#0D0E0F;--slate-2:#24272B;--line:#2F343A;--line-2:#3F454D;
  --frost:#F4F5F7;--text:#C5CAD2;--ash:#AEB4BE;--ash-dim:#969CA6;--accent:#22B4CC;
  --g-fallback:#545D67;--hud-fill:rgba(13,14,15,.92);
  --glass:rgba(35,39,43,.78);--glass-blur:blur(20px) saturate(1.3);
  --glass-edge:inset 0 1px 0 rgba(244,245,247,.07);--sel-hover:rgb(244 245 247 / .06);
  --edge:rgb(150 156 166 / .34);--edge-soft:rgb(150 156 166 / .16);--wash:.035;
  --sans:"Archivo",ui-sans-serif,system-ui,sans-serif}
:root[data-theme="light"]{color-scheme:light;
  --void:#EFEAE0;--slate-2:#E9E2D4;--line:#D9D1C0;--line-2:#C6BCA8;
  --frost:#1A1815;--text:#3A362F;--ash:#5B554B;--ash-dim:#655D4F;--accent:#0A6070;
  --g-fallback:#79705F;--hud-fill:rgba(245,241,233,.95);
  --glass:rgba(255,255,255,.86);--sel-hover:rgb(26 24 21 / .05);
  --glass-edge:inset 0 1px 0 rgba(255,255,255,.9),inset 0 -1px 0 rgba(58,44,20,.05);
  --edge:rgb(101 93 79 / .40);--edge-soft:rgb(101 93 79 / .20);--wash:.04}
*{box-sizing:border-box;margin:0}
html,body{height:100%;background:var(--void);overflow:hidden;font-family:var(--sans);
  -webkit-user-select:none;user-select:none;-webkit-font-smoothing:antialiased}
svg{width:100%;height:100vh;display:block;cursor:grab;touch-action:none;--inv:1}
svg.panning{cursor:grabbing}
/* Every stroke and every glyph is sized in screen pixels: the scene scales,
   the ink does not. Before, a 1.6-unit edge and 12.5-unit type both shrank
   with the fit: at 1100x800 a 35-note map drew its titles at 4.5px
   (Statistica) and 6.1px (NLP), measured 2026-10-06. */
.edge,.ring,.spoke,.dot{vector-effect:non-scaling-stroke}
.sector{fill:var(--c,var(--g-fallback));fill-opacity:var(--wash);stroke:none}
:root[data-theme="light"] .sector{fill:var(--cp,var(--g-fallback))}
/* The wash only says "inside"; the rim in the area's colour is what draws
   each area's reach. One area holding most of a map made a filled wedge a
   grey disc (measured on Statistica, 32 of 34 notes). */
.rim{fill:none;stroke:var(--c,var(--g-fallback));stroke-opacity:.55;stroke-width:1.25;vector-effect:non-scaling-stroke}
:root[data-theme="light"] .rim{stroke:var(--cp,var(--g-fallback))}
.spoke{stroke:var(--line);stroke-width:1}
.ring{fill:none;stroke:var(--line-2);stroke-width:1;stroke-dasharray:2 5}
.edge{fill:none;stroke:var(--edge);stroke-width:1;transition:opacity .18s cubic-bezier(.2,.8,.2,1)}
.edge.lateral{stroke:var(--edge-soft)}
.edge.latent{stroke-dasharray:3 4}
.dot{r:calc(var(--r,4) * var(--inv) * 1px);fill:var(--c,var(--g-fallback));
  stroke:var(--void);stroke-width:2;cursor:pointer;transition:opacity .18s cubic-bezier(.2,.8,.2,1)}
:root[data-theme="light"] .dot{fill:var(--cp,var(--c,var(--g-fallback)))}
/* related, no link: the dashed edge's note is hollow, so the mark says it too */
.dot.latent{fill:var(--void);stroke:var(--c,var(--g-fallback));stroke-width:1.6}
:root[data-theme="light"] .dot.latent{stroke:var(--cp,var(--c,var(--g-fallback)))}
:root .dot.root{fill:var(--accent)}
.halo{r:calc(15 * var(--inv) * 1px);fill:none;stroke:var(--accent);stroke-opacity:.35;stroke-width:1;
  vector-effect:non-scaling-stroke;pointer-events:none}
/* Labels carry a halo in the floor colour instead of a box: the cartographer's
   mask, so a line passing under a name never cuts it. */
.lbl,.slbl{font-size:calc(var(--fs,12) * var(--inv) * 1px);paint-order:stroke;stroke:var(--void);
  stroke-width:calc(4 * var(--inv) * 1px);stroke-linejoin:round;
  transition:opacity .18s cubic-bezier(.2,.8,.2,1)}
.lbl{fill:var(--text);cursor:pointer;--fs:12}
.lbl.hop1{fill:var(--frost);font-weight:500;--fs:12.5}
.lbl.root{fill:var(--frost);font-weight:600;--fs:15}
.lbl .sub{fill:var(--ash-dim);font-weight:400;font-size:.88em;display:none}
svg.near .lbl .sub{display:inline}
.slbl{fill:var(--ash);font-weight:600;--fs:11.5;pointer-events:none}
.slbl .n{fill:var(--ash-dim);font-weight:400}
.culled{visibility:hidden}
/* hover and focus: the note, its neighbours and the lines between them light;
   the signal colour is the only thing that ever says "this one" */
.edge.hot,.edge.on{stroke:var(--accent);stroke-opacity:.95;stroke-width:1.5}
:is(.lbl,.dot).hot,:is(.lbl,.dot).on{visibility:visible}
svg.focusing :is(.dot,.lbl):not(.on){opacity:.22}
svg.focusing .edge:not(.on){opacity:.07}
svg.focusing .slbl{opacity:.4}
/* The two bars sit where the graph frame's sit, on the frame's bottom edge
   beside the dock's resting stone, in the same material (graph-frame.css
   #gstat, ADR-0072). Under 912px of dock the app cannot narrow its composer
   between them, says so (`lift`), and they park above the dock as before. */
body{--foot:12px}
body.embedded{--foot:16px}
body.embedded.foot-lift{--foot:calc(var(--dock-h,72px) + 12px)}
#mstat{position:absolute;left:12px;bottom:var(--foot);display:flex;align-items:center;gap:2px;
  padding:2px;border-radius:10px;background:var(--glass);backdrop-filter:var(--glass-blur);
  box-shadow:var(--glass-edge);font-size:12px;color:var(--ash-dim);font-variant-numeric:tabular-nums;
  transition:bottom .22s cubic-bezier(.2,.8,.2,1)}
#mstat>span:last-child{padding:0 10px 0 8px}
#mstat button{min-width:26px;min-height:24px;padding:5px 10px;background:none;border:0;border-radius:8px;
  color:var(--ash);font:inherit;cursor:pointer}
#mstat button:hover{background:var(--sel-hover);color:var(--frost)}
#mstat .seg{display:flex;gap:2px}
#mkey{position:absolute;right:12px;bottom:var(--foot);display:flex;gap:14px;align-items:center;
  font-size:10.5px;color:var(--ash-dim)}
body.embedded #mkey{display:none}  /* the app's key line under the tabs says it */
#mkey i{display:inline-block;width:18px;vertical-align:middle;border-top:1px solid var(--ash);margin-right:5px}
#mkey i.dash{border-top-style:dashed}
"""

# One script for every map: it reads the drawing it is handed and nothing else.
_MAP_JS = r"""
(() => {
const svg = document.getElementById("stage"), scene = document.getElementById("scene");
const embedded = window.parent !== window;
if (embedded) document.body.classList.add("embedded");
const [bx0, by0, bx1, by1] = svg.dataset.bbox.split(" ").map(Number);
let k = 1, tx = 0, ty = 0, kFit = 1, moved = false;
// The app's tab bar floats over this frame's top; it posts its height
// (silica-host-bar). 96 is that bar with one key line, for the first paint.
let topInset = embedded ? 96 : 12;

function apply() {
  scene.setAttribute("transform", "translate(" + tx + "," + ty + ") scale(" + k + ")");
  svg.style.setProperty("--inv", (1 / k).toFixed(5));
  // The why under each name needs room the overview does not have; at 0.85
  // a 220-unit label box is 187px wide, which a clipped subtitle fits.
  svg.classList.toggle("near", k >= 0.85);
  scheduleCull();
}
function fit() {
  const W = innerWidth, H = innerHeight;
  if (!W || !H) return;   // a hidden frame has no box; the resize on show refits
  const foot = H - document.getElementById("mstat").getBoundingClientRect().top + 10;
  const top = topInset + 26, bottom = Math.max(foot, 40), side = 128;
  k = Math.min((W - 2 * side) / (bx1 - bx0), (H - top - bottom) / (by1 - by0), 1.25);
  k = Math.max(k, 0.04);
  tx = W / 2 - k * (bx0 + bx1) / 2;
  ty = top + (H - top - bottom) / 2 - k * (by0 + by1) / 2;
  kFit = k; moved = false; apply();
}
function zoomAt(f, cx, cy) {
  const nk = Math.min(3, Math.max(kFit * 0.6, k * f));
  tx = cx - (cx - tx) * nk / k; ty = cy - (cy - ty) * nk / k; k = nk; moved = true; apply();
}

// Labels in priority order (root, the hop-1 ring, the area names, the rest),
// each kept only if it clears every label kept before it and every other dot.
// visibility, not display, so a culled label still has a box to measure.
const labels = [...document.querySelectorAll(".lbl,.slbl")].sort((a, b) => a.dataset.p - b.dataset.p);
const dots = [...document.querySelectorAll(".dot")];
let queued = false;
function scheduleCull() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => { queued = false; cull(); });
}
function cull() {
  const boxes = labels.map((el) => el.getBoundingClientRect());
  const marks = dots.map((d) => [d.dataset.id, d.getBoundingClientRect()]);
  const kept = [], W = innerWidth;
  labels.forEach((el, i) => {
    const r = boxes[i], id = el.dataset.id;
    const hit = (b) => r.left < b.right + 4 && b.left < r.right + 4 && r.top < b.bottom && b.top < r.bottom;
    // At the fit, a name the frame edge cuts is held back too: half a title
    // reads as a different title (measured at 900px, "Principi di Asilomar per
    // un'IA b"). Zoomed in, the edge is where you pan to, as on any map.
    const lose = (i > 0 && k <= kFit * 1.05 && (r.left < 2 || r.right > W - 2))
      || kept.some(hit) || marks.some(([d, b]) => d !== id && hit(b));
    el.classList.toggle("culled", lose);
    if (!lose) kept.push(r);
  });
}

let drag = null;
svg.addEventListener("pointerdown", (e) => {
  if (e.target.closest("[data-id]")) return;
  drag = { x: e.clientX, y: e.clientY };
  svg.classList.add("panning");
  svg.setPointerCapture(e.pointerId);
});
svg.addEventListener("pointermove", (e) => {
  if (!drag) return;
  tx += e.clientX - drag.x; ty += e.clientY - drag.y;
  drag = { x: e.clientX, y: e.clientY }; moved = true;
  scene.setAttribute("transform", "translate(" + tx + "," + ty + ") scale(" + k + ")");
});
const endDrag = () => { drag = null; svg.classList.remove("panning"); };
svg.addEventListener("pointerup", endDrag);
svg.addEventListener("pointercancel", endDrag);
svg.addEventListener("wheel", (e) => {
  e.preventDefault();
  zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
}, { passive: false });
const mid = () => [innerWidth / 2, (innerHeight + topInset) / 2];
document.getElementById("fit").addEventListener("click", fit);
document.getElementById("zin").addEventListener("click", () => zoomAt(1.3, ...mid()));
document.getElementById("zout").addEventListener("click", () => zoomAt(1 / 1.3, ...mid()));
addEventListener("keydown", (e) => {
  if (e.key === "0") fit();
  else if (e.key === "+" || e.key === "=") zoomAt(1.3, ...mid());
  else if (e.key === "-") zoomAt(1 / 1.3, ...mid());
});
addEventListener("resize", () => (moved ? apply() : fit()));

// A note, its neighbours and the lines between them: "hot" while hovered,
// "on" once clicked or named by the app.
const nb = {};
const edges = [...document.querySelectorAll(".edge")];
edges.forEach((el) => {
  const a = el.dataset.src, b = el.dataset.dst;
  (nb[a] = nb[a] || new Set()).add(b);
  (nb[b] = nb[b] || new Set()).add(a);
});
const marked = [...document.querySelectorAll("[data-id]")];
function light(id, cls) {
  if (cls === "on") svg.classList.toggle("focusing", id != null);
  document.querySelectorAll("." + cls).forEach((el) => el.classList.remove(cls));
  if (id == null) return;
  const near = nb[id] || new Set();
  marked.forEach((el) => { if (el.dataset.id === id || near.has(el.dataset.id)) el.classList.add(cls); });
  edges.forEach((el) => { if (el.dataset.src === id || el.dataset.dst === id) el.classList.add(cls); });
}
scene.addEventListener("pointerover", (e) => {
  const t = e.target.closest("[data-id]");
  light(t ? t.dataset.id : null, "hot");
});
scene.addEventListener("pointerleave", () => light(null, "hot"));
svg.addEventListener("click", (e) => {
  const t = e.target.closest("[data-id]");
  if (!t) { light(null, "on"); return; }
  light(t.dataset.id, "on");
  // A click means "what is this, and what is around it": the host's context
  // drawer, the same contract as a graph node click. Naming a note (a
  // wikilink, the file tree) is what opens the reader.
  if (embedded) window.parent.postMessage({ type: "silica-open-context", path: t.dataset.id }, "*");
});
const known = new Set(marked.map((el) => el.dataset.id));
addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "silica-focus-path") light(known.has(d.path) ? d.path : null, "on");
  else if (d.type === "silica-host-dock") {
    document.body.style.setProperty("--dock-h", (+d.h || 0) + "px");
    document.body.classList.toggle("foot-lift", !!d.lift);
  }
  else if (d.type === "silica-host-bar") { topInset = +d.h || 0; if (!moved) fit(); }
});
fit();
document.fonts.ready.then(() => (moved ? scheduleCull() : fit()));
})();
"""


def render_map_svg(mv: MapView, title: str = "Mindmap") -> str:
    """Render a MapView as a self-contained, interactive SVG page.

    Consumes the precomputed positions (no force layout ⇒ cannot diverge from the
    canvas). Each note is a dot in its area's colour with its name beside it;
    the backdrop is one wedge per area, reaching as far out as that area's
    notes do, so the shape of the map says which parts of the vault the root
    touches and how far. Radius is association strength (closer = stronger
    tie) and the dashed guides mark the nearest, middle and farthest tie on the
    map. Wikilinks are solid neutral curves, related-without-a-link ties dashed
    and their notes hollow; only the root, and whatever is hovered or focused,
    wears the signal colour. Type and strokes hold their screen size through
    the zoom, and labels that would collide are held back until there is room
    (ADR-0069).
    """
    import html

    by_id = {n.id: n for n in mv.nodes}
    rest = [n for n in mv.nodes if n.hop > 0]
    rs = sorted(math.hypot(n.x, n.y) for n in rest) or [_DIAG]
    guide_rs = sorted({rs[0], (rs[0] + rs[-1]) / 2, rs[-1]})
    r_in = _DIAG * 0.3
    reach = _DIAG * 0.42          # how far a wedge runs past its farthest note

    wiki_touch = {x for e in mv.edges if e.kind == "wikilink" for x in (e.src, e.dst)}
    deg_max = max((n.degree for n in mv.nodes), default=0) or 1
    esc = lambda s: html.escape(s, quote=True)  # noqa: E731

    def colours(c: int) -> str:
        if c < 0:
            return ""   # no area: the CSS falls back to the canvas's own neutral
        return f"--c:{node_color(c)};--cp:{node_color(c, on_paper=True)};"

    sectors, rims, spokes, slabels = [], [], [], []
    xs, ys = [n.x for n in mv.nodes], [n.y for n in mv.nodes]

    wedges = _wedges(rest) if rest else []
    r_outs = [max(math.hypot(m.x, m.y) for m in members) + reach for _c, _a0, _a1, members in wedges]
    for i, (c, a0, a1, members) in enumerate(wedges):
        r_out = r_outs[i]
        sectors.append(f'<path class="sector" style="{colours(c)}" d="{_sector_path(a0, a1, r_in, r_out)}"/>')
        if len(wedges) > 1:
            # Run to the longer of the two wedges it divides: that closes the
            # step in the outline where a short wedge meets a long one.
            r_sp = max(r_out, r_outs[i - 1])
            spokes.append(f'<line class="spoke" x1="{r_in * math.cos(a0):.1f}" y1="{r_in * math.sin(a0):.1f}" '
                          f'x2="{r_sp * math.cos(a0):.1f}" y2="{r_sp * math.sin(a0):.1f}"/>')
        rims.append(f'<path class="rim" style="{colours(c)}" d="{_arc_path(a0, a1, r_out)}"/>')
        steps = max(2, math.ceil((a1 - a0) / (math.pi / 18)))   # every 10deg: the arc bulges past its ends
        for i in range(steps + 1):
            t = a0 + (a1 - a0) * i / steps
            xs.append(r_out * math.cos(t))
            ys.append(r_out * math.sin(t))
        name = mv.areas.get(c)
        # A one-note wedge is named by that note's own label already.
        if name and len(members) > 1:
            am = (a0 + a1) / 2
            ca, sa = math.cos(am), math.sin(am)
            anchor = "start" if ca > 0.25 else ("end" if ca < -0.25 else "middle")
            label_dy = "0.35em" if anchor != "middle" else ("1em" if sa > 0 else "-0.4em")
            slabels.append(
                f'<text class="slbl" data-p="{{p}}" x="{r_out * ca:.1f}" y="{r_out * sa:.1f}" '
                f'text-anchor="{anchor}" dx="{0.5 if anchor == "start" else -0.5 if anchor == "end" else 0}em" '
                f'dy="{label_dy}">{html.escape(_clip(name, 30))}<tspan class="n"> · {len(members)}</tspan></text>'
            )

    rings = "".join(f'<circle class="ring" cx="0" cy="0" r="{r:.1f}"/>' for r in guide_rs)

    edge_svg = []
    for e in mv.edges:
        s, d = by_id[e.src], by_id[e.dst]
        parent, child = (s, d) if s.hop <= d.hop else (d, s)
        x1, y1, x2, y2 = parent.x, parent.y, child.x, child.y
        # A deterministic bow (perpendicular offset from the midpoint): an arc
        # rather than a ruler line, reproducible because nothing is random.
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        bow = min(36.0, length * 0.16)
        cx, cy = (x1 + x2) / 2 - dy / length * bow, (y1 + y2) / 2 + dx / length * bow
        kind = "latent" if e.kind == "latent" else ("tree" if parent.hop != child.hop else "lateral")
        edge_svg.append(
            f'<path class="edge {kind}" data-src="{esc(e.src)}" data-dst="{esc(e.dst)}" '
            f'd="M {x1:.1f} {y1:.1f} Q {cx:.1f} {cy:.1f} {x2:.1f} {y2:.1f}"/>'
        )

    # Label priority: the root, then the first ring by degree, then the area
    # names, then everything else by degree. Earlier wins a collision.
    order = sorted(mv.nodes, key=lambda n: (n.hop, -n.degree, n.cost, n.id))
    rank = {n.id: i for i, n in enumerate(order)}
    hop1 = sum(1 for n in mv.nodes if n.hop <= 1)
    slabels = [s.replace("{p}", str(hop1 + i)) for i, s in enumerate(slabels)]
    prio = hop1 + len(slabels)

    dot_svg, label_svg = [], []
    for n in mv.nodes:
        t = n.degree / deg_max
        r_px = 8.0 if n.hop == 0 else 3.2 + 3.6 * math.sqrt(t)
        latent = n.hop > 0 and n.id not in wiki_touch
        cls = "root" if n.hop == 0 else ("hop1" if n.hop == 1 else "hop2")
        tip = "\n".join(s for s in (n.title, n.subtitle or "", f"{n.degree} links") if s)
        dot_svg.append(
            f'<circle class="dot {cls}{" latent" if latent else ""}" data-id="{esc(n.id)}" '
            f'cx="{n.x:.1f}" cy="{n.y:.1f}" style="{colours(n.community)}--r:{r_px:.1f}">'
            f'<title>{html.escape(tip)}</title></circle>'
        )
        if n.hop == 0:
            dot_svg.append(f'<circle class="halo" cx="{n.x:.1f}" cy="{n.y:.1f}"/>')
        fs = 15.0 if n.hop == 0 else (12.5 if n.hop == 1 else 12.0)
        p = rank[n.id] if n.hop <= 1 else prio + rank[n.id]
        name = html.escape(_clip(n.title, 34))
        sub = n.subtitle or ""
        if n.path.rsplit("/", 1)[0] == sub:
            # No anchor line and no evidence, only the folder it is filed in:
            # its last step says where, and nothing when that is the note's
            # own name (a note filed in a folder named after it).
            sub = sub.rsplit("/", 1)[-1]
            sub = "" if sub.casefold() == n.title.casefold() else "in " + sub
        sub = html.escape(_clip(sub, 56)) if n.hop > 0 else ""
        if n.hop == 0:
            # The root's name sits under its dot, centred: it is the one label
            # every other one is read against.
            label_svg.append(
                f'<text class="lbl root" data-id="{esc(n.id)}" data-p="0" x="{n.x:.1f}" y="{n.y:.1f}" '
                f'text-anchor="middle" dy="{(r_px + 9) / fs + 0.75:.2f}em">{name}</text>'
            )
            continue
        right = n.x >= 0
        off = (r_px + 6) / fs
        sub_off = (r_px + 6) / (fs * 0.88)
        sub_svg = (f'<tspan class="sub" x="{n.x:.1f}" dx="{sub_off if right else -sub_off:.2f}em" '
                   f'dy="1.45em">{sub}</tspan>') if sub else ""
        label_svg.append(
            f'<text class="lbl {cls}" data-id="{esc(n.id)}" data-p="{p}" x="{n.x:.1f}" y="{n.y:.1f}" '
            f'text-anchor="{"start" if right else "end"}">'
            f'<tspan dx="{off if right else -off:.2f}em" dy="0.35em">{name}</tspan>{sub_svg}</text>'
        )

    bbox = f"{min(xs) - 40:.0f} {min(ys) - 40:.0f} {max(xs) + 40:.0f} {max(ys) + 40:.0f}"
    root = by_id.get(mv.root)
    root_title = root.title if root else title
    n_wiki = sum(1 for e in mv.edges if e.kind == "wikilink")
    n_latent = len(mv.edges) - n_wiki
    count = f"{len(mv.nodes)} notes · {n_wiki} links" + (f" · {n_latent} related" if n_latent else "")
    desc = (f"Notes around {root_title}: the nearer a note sits to the centre, the more "
            f"strongly it ties to it, and each wedge is one area of the vault.")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<!-- ?theme= when the app embeds this; the OS when it is opened on its own.
     Same contract as /graph — see graph_view.render_html. -->
<script>
  (function () {{
    var q = new URLSearchParams(location.search).get("theme");
    var mq = window.matchMedia("(prefers-color-scheme: light)");
    var pinned = q === "light" || q === "dark";
    document.documentElement.dataset.theme = pinned ? q : (mq.matches ? "light" : "dark");
    if (!pinned) mq.addEventListener("change", function () {{ location.reload(); }});
  }})();
</script>
<style>{_MAP_CSS}</style>
</head>
<body>
<svg id="stage" data-bbox="{bbox}" role="img" aria-labelledby="map-title map-desc" xmlns="http://www.w3.org/2000/svg">
  <title id="map-title">Map of {html.escape(root_title)}</title>
  <desc id="map-desc">{html.escape(desc)}</desc>
  <g id="scene">
    <g id="sectors">{"".join(sectors)}{"".join(spokes)}{"".join(rims)}</g>
    <g id="rings">{rings}</g>
    <g id="edges">{"".join(edge_svg)}</g>
    <g id="dots">{"".join(dot_svg)}</g>
    <g id="labels">{"".join(slabels)}{"".join(label_svg)}</g>
  </g>
</svg>
<div id="mstat">
  <button id="fit" type="button" title="fit the map (0)">fit</button>
  <span class="seg"><button id="zout" type="button" title="zoom out (-)">−</button><button id="zin" type="button" title="zoom in (+)">+</button></span>
  <span>{count}</span>
</div>
<div id="mkey"><span><i></i>wikilink</span><span><i class="dash"></i>related, no link</span><span>nearer the centre = stronger tie</span><span>wedge = an area</span></div>
<script>{_MAP_JS}</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Live-vault material gathering (IO; the tool/endpoint call this)
# ---------------------------------------------------------------------------

def _resolve_in(note: str, note_paths: list[str], titles: dict[str, str]) -> str | None:
    """Resolve `note` (a path OR a title) to a graph key. Pure; sorted ⇒ stable.

    Graph keys are full vault-relative paths WITH .md; a user (or the GUI input)
    may give a bare title or a path with/without .md. Try exact path first, then
    match by basename/title case-insensitively.
    """
    keys = sorted(p.replace("\\", "/") for p in note_paths)
    key_set = set(keys)
    cand = note.replace("\\", "/").strip()
    for c in (cand, cand + ".md", cand.removesuffix(".md")):
        if c in key_set:
            return c
    target = cand.removesuffix(".md").rsplit("/", 1)[-1].lower()
    for path in keys:
        stem = path.removesuffix(".md").rsplit("/", 1)[-1].lower()
        if stem == target or (titles.get(path, "") or "").lower() == target:
            return path
    return None


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


_WIKILINK_ALIAS = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]")
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def _unwrap_line(line: str) -> str:
    """A body line stripped to prose: wikilinks unwrapped (alias wins), bold
    and backtick markers removed, list/heading markers dropped, whitespace
    collapsed."""
    s = _WIKILINK_ALIAS.sub(r"\2", line)
    s = _WIKILINK.sub(r"\1", s)
    s = re.sub(r"\*\*|`", "", s)
    return " ".join(s.strip().lstrip("#>*- \t").split())


def _clean_snippet(line: str, limit: int = 96) -> str:
    s = _unwrap_line(line)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _anchor_snippet(line: str, names: set[str]) -> str:
    """The learnable part of an anchor line. When the line opens by restating
    the linked title ("X (…): why…"), keep the part after the colon — the card
    already shows the title; the explanation is the payload."""
    full = _unwrap_line(line)
    head, sep, tail = full.partition(":")
    if sep and tail.strip() and any(nm in head.lower() for nm in names):
        full = tail.strip()
    return _clean_snippet(full)


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


def _link_context_fn(titles: dict[str, str]):
    """(parent_id, child_id) -> the cleaned body line where one wikilinks the
    other, or None. Checks the parent's body first, then the child's (the BFS
    parent is traversal order, not authorship — either side may hold the link).
    Bodies are read lazily through the driver, one read per note per map.
    """
    from silica.driver import get_driver

    drv = get_driver()
    bodies: dict[str, str] = {}

    def _body(nid: str) -> str:
        if nid not in bodies:
            try:
                bodies[nid] = drv.read_note(nid).content
            except Exception:
                bodies[nid] = ""
        return bodies[nid]

    def ctx(parent_id: str, child_id: str) -> str | None:
        for host, other in ((parent_id, child_id), (child_id, parent_id)):
            text = _body(host)
            if not text:
                continue
            stem = other.rsplit("/", 1)[-1].removesuffix(".md").lower()
            names = {stem, (titles.get(other) or "").lower()} - {""}
            for line in text.splitlines():
                low = line.lower()
                if "[[" in low and any(nm in low for nm in names):
                    snippet = _anchor_snippet(line, names)
                    if snippet:
                        return snippet
        return None

    return ctx


def gather_materials(root: str, *, latent_k: int = 10) -> MapMaterials:
    """Collect wikilink graph, titles, global communities, the latent leg, and
    the two evidence closures (embedding similarity, wikilink anchor lines)."""
    from silica.kernel.recall.graph_export import (
        area_hubs,
        area_tally,
        build_graph_data,
        detect_communities,
    )
    from silica.kernel.recall.relatedness import related_notes

    _notes, titles, undirected = _driver_graph()

    nodes, edges = build_graph_data()
    detect_communities(nodes, edges)  # assigns node["group"] in place (global, seed=42)
    community_of = {n["id"]: n.get("group", -1) for n in nodes}
    # A wedge is named the way Areas names an area, by its hub note's title
    # (ADR-0058 retired the co-occurrence topics as a name). The same tally
    # /shape reads, so a wedge here and a disc there cannot name two notes.
    real = [n for n in nodes if n.get("type") != "ghost"]
    tally = area_tally(real, edges)
    label_of = {n["id"]: n.get("label") or _stem_title(n["id"]) for n in real}
    community_label = {
        g: label_of.get(hub, _stem_title(hub))
        for g, hub in area_hubs(tally).items()
        if tally.sizes.get(g, 0) > 1      # a one-note area is its own label already
    }

    latent: list[tuple[str, str, float]] = []
    latent_evidence: dict[str, str] = {}
    try:
        embed_store, cooccur_store = _load_stores()
        for r in related_notes(
            _with_md(root), embed_store=embed_store, cooccur_store=cooccur_store, k=latent_k
        ):
            lid = _with_md(r.path)
            # Radius wants a native 0-1 strength, not the RRF score (ordering
            # only). Cooccur-only rows carry no such signal: park them just
            # under the weakest scored one — they are the ranking's tail anyway.
            latent.append((lid, r.name, r.embed_score or 0.0))
            latent_evidence[lid] = "≈ " + " ".join(r.evidence)
    except Exception:
        latent = []
    if latent:
        floor = min((s for _, _, s in latent if s > 0), default=0.3) * 0.9
        latent = [(lid, t, s if s > 0 else floor) for lid, t, s in latent]

    return MapMaterials(
        graph=undirected,
        titles=titles,
        community_of=community_of,
        latent=latent,
        latent_evidence=latent_evidence,
        sim=_embed_sim(),
        link_context=_link_context_fn(titles),
        community_label=community_label,
    )


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
