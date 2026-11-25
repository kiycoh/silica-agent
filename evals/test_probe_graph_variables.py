# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Adamic-Adar leg of evals/probe_graph_variables: these cases came from
tests/test_graph_variables.py with the function on 2026-11-25, when it left the
product for the one probe that calls it."""
from __future__ import annotations

import math

import networkx as nx
import pytest

from evals.probe_graph_variables import adamic_adar_ranking


def _path_graph() -> nx.Graph:
    """A - B - C - D  plus hub H linked to A, B, C (so A,C share B and H)."""
    G = nx.Graph()
    G.add_edges_from([("A", "B"), ("B", "C"), ("C", "D"), ("H", "A"), ("H", "B"), ("H", "C")])
    return G


def test_adamic_adar_ranks_non_adjacent_by_rare_common_neighbours():
    G = _path_graph()
    ranking = adamic_adar_ranking(G, "A", k=5)
    assert ranking is not None
    paths = [p for p, _ in ranking]
    # A and C share B (deg 3) and H (deg 3): the only distance-2 candidate
    # besides D (no common neighbour) -> C first, B/H excluded (adjacent).
    assert paths[0] == "C"
    assert "B" not in paths and "H" not in paths and "A" not in paths
    score = dict(ranking)["C"]
    assert math.isclose(score, 2 / math.log(3), rel_tol=1e-9)


def test_adamic_adar_abstains_for_isolated_or_unknown_node():
    G = _path_graph()
    G.add_node("Z")
    assert adamic_adar_ranking(G, "Z", k=5) is None
    assert adamic_adar_ranking(G, "nope", k=5) is None


def test_adamic_adar_ignores_degree_one_common_neighbour_without_crashing():
    # log(1) == 0: a common neighbour of degree 1 cannot exist (it would have
    # to touch both endpoints), but a degree-2 neighbour is the minimum and
    # must not divide by zero.
    G = nx.Graph([("A", "M"), ("M", "B")])
    ranking = adamic_adar_ranking(G, "A", k=5)
    assert ranking == [("B", pytest.approx(1 / math.log(2)))]
