"""Layout invariants for kernel/mindmap — deterministic radial wedge placement."""
from __future__ import annotations

import math

import networkx as nx

from silica.kernel.recall.mindmap import (
    BOX_H,
    BOX_W,
    MapMaterials,
    build_mapview,
)


def _materials(latent=()):
    """root — a,b (hop1); a — c, b — d (hop2). Two communities.  Plus latent."""
    g = nx.Graph()
    g.add_edges_from([
        ("root.md", "a.md"), ("root.md", "b.md"),
        ("a.md", "c.md"), ("b.md", "d.md"),
    ])
    return MapMaterials(
        graph=g,
        titles={p: p[:-3].upper() for p in g.nodes},
        community_of={"root.md": 0, "a.md": 0, "c.md": 0, "b.md": 1, "d.md": 1},
        latent=list(latent),
    )


def _boxes_overlap(a, b) -> bool:
    # Equal axis-aligned boxes centred at a, b overlap iff both axes are within a box.
    return abs(a.x - b.x) < BOX_W and abs(a.y - b.y) < BOX_H


def test_root_at_origin():
    mv = build_mapview("root.md", _materials())
    root = next(n for n in mv.nodes if n.id == "root.md")
    assert (root.x, root.y) == (0.0, 0.0)
    assert root.hop == 0


def test_no_box_overlap():
    mv = build_mapview("root.md", _materials([("e.md", "E", 0.9), ("f.md", "F", 0.8)]))
    ns = mv.nodes
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            assert not _boxes_overlap(ns[i], ns[j]), f"{ns[i].id} overlaps {ns[j].id}"


def test_communities_occupy_contiguous_wedges():
    # Each community must own a contiguous angular arc — no interleaving.
    mv = build_mapview("root.md", _materials())
    non_root = [n for n in mv.nodes if n.hop > 0]
    ordered = sorted(non_root, key=lambda n: math.atan2(n.y, n.x) % (2 * math.pi))
    runs = [n.community for n in ordered]
    # Number of contiguous community-runs equals the number of distinct communities.
    changes = sum(1 for k in range(1, len(runs)) if runs[k] != runs[k - 1])
    assert changes == len(set(runs)) - 1


def test_deterministic():
    m = _materials([("e.md", "E", 0.9)])
    a = build_mapview("root.md", m)
    b = build_mapview("root.md", _materials([("e.md", "E", 0.9)]))
    assert [(n.id, n.x, n.y) for n in a.nodes] == [(n.id, n.x, n.y) for n in b.nodes]


def test_root_argument_normalized_to_md():
    mv = build_mapview("root", _materials())  # no .md suffix
    assert mv.root == "root.md"
    assert any(n.id == "root.md" and n.hop == 0 for n in mv.nodes)


def test_radius_tracks_association_cost():
    # Radius IS data: a direct wikilink sits closer than a 2-hop one, a strong
    # latent tie closer than a weak one, and the strongest latent tie costs
    # exactly one wikilink hop (strengths are normalised to the map's best).
    mv = build_mapview("root.md", _materials([("e.md", "E", 0.9), ("f.md", "F", 0.45)]))
    r = {n.id: math.hypot(n.x, n.y) for n in mv.nodes}
    assert r["a.md"] < r["c.md"]
    assert r["e.md"] < r["f.md"]
    assert abs(r["a.md"] - r["e.md"]) < 1e-6
    costs = {n.id: n.cost for n in mv.nodes}
    assert costs["root.md"] == 0.0
    assert costs["a.md"] == 1.0 and costs["c.md"] == 2.0


def test_hub_root_radius_is_not_degenerate():
    # Regression: when every selected node is a direct wikilink, cost used to
    # tie at 1.0 for all of them and the radius spread was pure collision
    # slide — i.e. alphabetical. Similarity must break that tie.
    g = nx.Graph()
    for x in "abcd":
        g.add_edge("root.md", f"{x}.md")
    sims = {"a.md": 0.9, "b.md": 0.6, "c.md": 0.4, "d.md": 0.2}
    m = MapMaterials(
        graph=g,
        titles={p: p for p in g.nodes},
        community_of={p: 0 for p in g.nodes},
        sim=lambda p, q: sims.get(q if p == "root.md" else p, 0.0),
    )
    mv = build_mapview("root.md", m)
    cost = {n.id: n.cost for n in mv.nodes}
    assert cost["a.md"] < cost["b.md"] < cost["c.md"] < cost["d.md"]
    assert cost["a.md"] == 1.0            # the tightest tie anchors the map
    assert cost["d.md"] < 2.0             # one wikilink never costs two


def test_sibling_order_follows_similarity():
    # With a similarity signal, angular neighbours must be semantic neighbours:
    # a~z high beats the alphabetical a,b,z order.
    g = nx.Graph()
    for x in ("a.md", "b.md", "z.md"):
        g.add_edge("root.md", x)
    sims = {
        frozenset({"a.md", "z.md"}): 0.95,
        frozenset({"a.md", "b.md"}): 0.10,
        frozenset({"b.md", "z.md"}): 0.20,
    }
    m = MapMaterials(
        graph=g,
        titles={p: p for p in g.nodes},
        community_of={p: 0 for p in g.nodes},
        sim=lambda p, q: sims.get(frozenset({p, q}), 0.0),
    )
    mv = build_mapview("root.md", m)
    ordered = sorted(
        (n for n in mv.nodes if n.hop > 0),
        key=lambda n: math.atan2(n.y, n.x) % (2 * math.pi),
    )
    assert [n.id for n in ordered] == ["a.md", "z.md", "b.md"]


def test_the_cap_keeps_a_strong_related_note_over_a_hop_two_note():
    # ADR-0088 point 7. The strongest related tie costs exactly one wikilink
    # (1.0) and any hop-2 note at least two, so with one slot left after hop 1
    # the related note is the nearer one and has to be the one kept. The old
    # tier order kept every hop-2 note ahead of every related one.
    mv = build_mapview("root.md", _materials([("e.md", "E", 0.9)]), max_nodes=4)
    assert {n.id for n in mv.nodes} == {"root.md", "a.md", "b.md", "e.md"}


def test_hop_one_beyond_the_cap_is_cut_by_cost_not_by_path():
    # A hub with more wikilinks than the cap: the old sort fell back to the
    # path, so "a" and "b" stayed whatever they cost. The cheapest stay now.
    g = nx.Graph()
    for x in ("a.md", "b.md", "z.md"):
        g.add_edge("root.md", x)
    sims = {"z.md": 0.9, "b.md": 0.5, "a.md": 0.1}
    m = MapMaterials(
        graph=g,
        titles={p: p for p in g.nodes},
        community_of={p: 0 for p in g.nodes},
        sim=lambda p, q: sims.get(q if p == "root.md" else p, 0.0),
    )
    mv = build_mapview("root.md", m, max_nodes=3)
    assert {n.id for n in mv.nodes} == {"root.md", "z.md", "b.md"}


# --- the graph's lit set, arranged as it is given (ADR-0088) -------------------

def _star(n: int) -> nx.Graph:
    g = nx.Graph()
    for i in range(n):
        g.add_edge("root.md", f"n{i:02d}.md")
    return g


def test_arrange_keeps_every_given_note_and_adds_none():
    # No cap: the graph already draws the whole lit set, so all of it is laid
    # out, and a note of the vault the caller did not light stays out.
    from silica.kernel.recall.mindmap import arrange_mapview

    g = _star(40)
    g.add_edge("n00.md", "elsewhere.md")
    ids = [f"n{i:02d}.md" for i in range(40)]
    mv = arrange_mapview("root.md", ids, graph=g, community_of={}, sim=None)
    assert {n.id for n in mv.nodes} == {"root.md", *ids}


def test_arrange_reads_the_community_it_is_given():
    # The frame sends the group each dot is painted with, so a wedge and the
    # dots inside it cannot disagree on colour.
    from silica.kernel.recall.mindmap import arrange_mapview

    mv = arrange_mapview("root.md", ["n00.md", "n01.md"], graph=_star(2),
                         community_of={"root.md": 3, "n00.md": 7, "n01.md": 9}, sim=None)
    assert {n.id: n.community for n in mv.nodes} == {"root.md": 3, "n00.md": 7, "n01.md": 9}


def test_arrange_prices_an_unlinked_note_by_its_similarity_to_the_root():
    # A note lit by the k-NN, a gap or a proposal has no wikilink to the root;
    # it is a latent tie with the strength of its similarity, as on the Map.
    from silica.kernel.recall.mindmap import arrange_mapview

    sims = {"x.md": 0.9, "y.md": 0.45}
    mv = arrange_mapview("root.md", ["x.md", "y.md"], graph=nx.Graph(), community_of={},
                         sim=lambda p, q: sims.get(q if p == "root.md" else p, 0.0))
    cost = {n.id: n.cost for n in mv.nodes}
    assert cost["x.md"] == 1.0 and cost["x.md"] < cost["y.md"]


def test_arrange_parks_what_it_cannot_price_on_the_rim():
    # An unresolved link has no file and no vector: nothing says how near it
    # is, so it sits past every note that has a price.
    from silica.kernel.recall.mindmap import arrange_mapview

    ghost = "__unresolved__Pattern recognition"
    mv = arrange_mapview("root.md", ["n00.md", "n01.md", ghost], graph=_star(2),
                         community_of={}, sim=lambda p, q: 0.0)
    r = {n.id: math.hypot(n.x, n.y) for n in mv.nodes}
    cost = {n.id: n.cost for n in mv.nodes}
    assert cost[ghost] > max(cost["n00.md"], cost["n01.md"])
    assert r[ghost] >= max(r["n00.md"], r["n01.md"])


def test_the_arrangement_payload_carries_positions_and_wedges_that_tile_the_circle():
    from silica.kernel.recall.mindmap import arrange_mapview, arrangement_payload

    g = _star(6)
    groups = {f"n{i:02d}.md": i % 3 for i in range(6)}
    mv = arrange_mapview("root.md", list(groups), graph=g, community_of=groups, sim=None)
    out = arrangement_payload(mv)
    assert set(out["nodes"]) == {"root.md", *groups}
    assert out["nodes"]["root.md"] == [0, 0, 0.0]
    spans = sum(w["a1"] - w["a0"] for w in out["wedges"])
    assert abs(spans - 2 * math.pi) < 1e-6
    for w in out["wedges"]:
        members = [n for n in mv.nodes if n.hop > 0 and n.community == w["area"]]
        assert w["n"] == len(members)
        assert w["r"] > max(math.hypot(n.x, n.y) for n in members)
