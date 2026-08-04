"""edge_graph must hand Louvain the same node order in every process, and the
community NUMBERING must come from the partition rather than from luck.

Louvain is a greedy local-move heuristic: it shuffles `G.nodes()` with its
seeded RNG, so the insertion order decides which local optimum it lands on.
Building the node set and calling `add_nodes_from(<set>)` made that order
hash-dependent, so E(vault) drifted run to run on an unchanged vault.

Freezing the order fixed WHICH partition we land on but not what each part is
CALLED: `louvain_communities` returns its list of sets in a per-process order,
so three runs on an unchanged vault numbered the same 78-member community 60,
61, 60. The id is not cosmetic — `_community_color` is keyed on it (a community
changed colour between runs) and `clusters_ctx.json` persists it for readers in
other processes.
"""
from __future__ import annotations

from silica.kernel.recall.graph_export import detect_communities, edge_graph


def _nodes(*ids):
    return [{"id": i, "type": "note"} for i in ids]


def test_edge_graph_inserts_nodes_sorted():
    ids = ["zeta.md", "alpha.md", "mid/beta.md", "gamma.md", "10.md", "2.md"]
    G = edge_graph(_nodes(*ids), [{"from": "zeta.md", "to": "alpha.md", "type": "EXTRACTED"}])
    assert list(G.nodes()) == sorted(ids)


def test_edge_graph_order_independent_of_input_order():
    ids = ["zeta.md", "alpha.md", "mid/beta.md", "gamma.md"]
    edges = [{"from": "zeta.md", "to": "alpha.md", "type": "EXTRACTED"}]
    assert list(edge_graph(_nodes(*ids), edges).nodes()) == list(
        edge_graph(_nodes(*reversed(ids)), edges).nodes()
    )


def _clique(*ids):
    return [{"from": a, "to": b, "type": "EXTRACTED"}
            for i, a in enumerate(ids) for b in ids[i + 1:]]


def test_community_ids_are_ordered_by_size():
    """Largest partition is 0. Content-derived, so every process agrees."""
    nodes = _nodes("a.md", "b.md", "c.md", "d.md", "x.md", "y.md")
    comms = detect_communities(nodes, _clique("a.md", "b.md", "c.md", "d.md")
                               + _clique("x.md", "y.md"))
    assert [(c.id, c.size) for c in comms] == [(0, 4), (1, 2)]
    group = {n["id"]: n["group"] for n in nodes}
    assert group["a.md"] == 0 and group["x.md"] == 1


def test_equal_sized_communities_break_ties_on_member_id():
    """Size alone leaves ties, and a tie is where the per-process order used to
    leak back in. The smallest member id decides, so the rule is total."""
    nodes = _nodes("a.md", "b.md", "x.md", "y.md")
    comms = detect_communities(nodes, _clique("a.md", "b.md") + _clique("x.md", "y.md"))
    assert [c.size for c in comms] == [2, 2]
    group = {n["id"]: n["group"] for n in nodes}
    assert group["a.md"] == 0 and group["x.md"] == 1


def test_ghost_nodes_stay_out():
    nodes = _nodes("a.md", "b.md") + [{"id": "__unresolved__x", "type": "ghost"}]
    G = edge_graph(nodes, [{"from": "a.md", "to": "__unresolved__x", "type": "EXTRACTED"}])
    assert list(G.nodes()) == ["a.md", "b.md"]
    assert G.number_of_edges() == 0
