# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The walk under the phase-0 numbers: if it is wrong, the timings are noise."""
from __future__ import annotations

from evals.probe_ppr_phase0 import bfs_levels, ppr


def _graph() -> dict[str, dict[str, float]]:
    """A hub with 300 leaves + a rare pair, both seeded. This is the shape the
    spec's hypothesis is about: flat expansion hands the hub 300x the mass."""
    adj: dict[str, dict[str, float]] = {"hub": {}, "rare": {"twin": 1.0}, "twin": {"rare": 1.0}}
    for i in range(300):
        adj["hub"][f"leaf{i}"] = 1.0
        adj[f"leaf{i}"] = {"hub": 1.0}
    return adj


def test_degree_normalisation_starves_the_hub_leaves():
    r = ppr(_graph(), {"hub": 1.0, "rare": 1.0}, hops=1, alpha=0.5)
    assert r["twin"] > 100 * r["leaf0"]  # 0.25 vs 0.25/300


def test_mass_is_conserved_on_a_graph_without_dangling_nodes():
    for hops in (1, 2, 3):
        r = ppr(_graph(), {"hub": 1.0, "rare": 1.0}, hops=hops, alpha=0.5)
        assert abs(sum(r.values()) - 1.0) < 1e-9


def test_bfs_levels_are_hop_distances_capped_at_max_hops():
    adj = {"a": {"b": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"b": 1.0}, "far": {}}
    assert bfs_levels(adj, ["a"], 2) == {"a": 0, "b": 1, "c": 2}
    assert bfs_levels(adj, ["a"], 1) == {"a": 0, "b": 1}
