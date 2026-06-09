"""Golden tests for the betweenness hub-protection gate (Tier 2, Item 6).

Graph topologies:

  Path: A - B - C - D
    bc(A) = bc(D) = 0.0   (endpoints)
    bc(B) = bc(C) = 2/3 ≈ 0.667

  Diamond: A - B - D  and  A - C - D  (no B-C edge)
    All four nodes: bc ≈ 1/6 ≈ 0.167
    After removing B-D: B becomes a leaf → bc(B) = 0.0  (Δ ≈ 0.167)

The diamond scenario gives a controlled Δ between 0.1 and 0.25, useful for
testing custom thresholds.
"""
import pytest
from silica.kernel.graph_diff import check_hub_protection


def _nodes(*ids: str) -> list[dict]:
    return [{"id": n, "type": "note", "group": 0} for n in ids]


def _edges(*pairs: tuple[str, str]) -> list[dict]:
    return [{"from": a, "to": b, "type": "EXTRACTED"} for a, b in pairs]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _path_abcd():
    nodes = _nodes("A", "B", "C", "D")
    edges = _edges(("A", "B"), ("B", "C"), ("C", "D"))
    return nodes, edges


def _path_abcde():
    nodes = _nodes("A", "B", "C", "D", "E")
    edges = _edges(("A", "B"), ("B", "C"), ("C", "D"), ("D", "E"))
    return nodes, edges


def _diamond():
    """Diamond A-B-D, A-C-D (no B-C edge). Each node: bc ≈ 0.167."""
    nodes = _nodes("A", "B", "C", "D")
    edges = _edges(("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"))
    return nodes, edges


def _diamond_minus_bd():
    """Diamond with B-D removed → B becomes a leaf → bc(B) = 0.0."""
    nodes = _nodes("A", "B", "C", "D")
    edges = _edges(("A", "B"), ("A", "C"), ("C", "D"))
    return nodes, edges


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHubProtectionGolden:
    def test_severing_bridge_blocks(self):
        """Removing B-C severs the graph; B and C lose all betweenness → Δ ≈ 0.667 > 0.25."""
        pre = _path_abcd()
        # Post: A-B  C-D (B-C edge removed — B and C become disconnected endpoints)
        post_nodes = _nodes("A", "B", "C", "D")
        post_edges = _edges(("A", "B"), ("C", "D"))

        ok, errors = check_hub_protection(pre, (post_nodes, post_edges))
        assert not ok
        assert errors
        assert any("B" in e or "C" in e for e in errors)

    def test_adding_leaf_passes(self):
        """E is a new node; shared subgraph {A,B,C,D} is unchanged → Δ = 0 for all → passes."""
        pre = _path_abcd()
        post = _path_abcde()

        ok, errors = check_hub_protection(pre, post)
        assert ok
        assert not errors

    def test_no_change_passes(self):
        """Identical pre/post graphs → no hub change → gate passes."""
        graph = _path_abcd()
        ok, errors = check_hub_protection(graph, graph)
        assert ok
        assert not errors

    def test_custom_threshold_tighter(self):
        """Diamond: removing B-D makes B a leaf → Δbc(B) ≈ 0.167. Fires at threshold=0.1."""
        pre = _diamond()
        post = _diamond_minus_bd()

        # Default threshold (0.25): Δ ≈ 0.167 < 0.25 → passes
        ok_default, _ = check_hub_protection(pre, post)
        assert ok_default

        # Tighter threshold (0.1): Δ ≈ 0.167 > 0.1 → blocks
        ok_tight, errors_tight = check_hub_protection(pre, post, threshold=0.1)
        assert not ok_tight
        assert errors_tight

    def test_empty_graphs_pass(self):
        ok, errors = check_hub_protection(([], []), ([], []))
        assert ok
        assert not errors

    def test_adding_isolated_cluster_passes(self):
        """Adding an isolated F-G pair does not change existing nodes' betweenness."""
        pre = _path_abcd()
        post_nodes = _nodes("A", "B", "C", "D", "F", "G")
        post_edges = _edges(("A", "B"), ("B", "C"), ("C", "D"), ("F", "G"))

        ok, errors = check_hub_protection(pre, (post_nodes, post_edges))
        assert ok
        assert not errors

    def test_betweenness_increase_allowed(self):
        """A write that *creates* a new hub (bc increases) is not blocked."""
        # Pre: A, B, C, D all disconnected (no edges)
        pre_nodes = _nodes("A", "B", "C", "D")
        pre = (pre_nodes, [])

        # Post: A-B-C-D (B and C become hubs)
        post = _path_abcd()

        ok, errors = check_hub_protection(pre, post)
        assert ok
        assert not errors

    def test_error_message_names_affected_nodes(self):
        """Error messages must identify which hub nodes triggered the gate."""
        pre = _path_abcd()
        post_nodes = _nodes("A", "B", "C", "D")
        post_edges = _edges(("A", "B"), ("C", "D"))

        _, errors = check_hub_protection(pre, (post_nodes, post_edges))
        combined = " ".join(errors)
        # At least one of the bridges should be named
        assert "B" in combined or "C" in combined
