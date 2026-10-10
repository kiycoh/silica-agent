# SPDX-License-Identifier: AGPL-3.0-or-later
"""One tally of links between areas (ADR-0045, third pass). The report's
inter_cluster, the /shape matrix and the gap/bridge ranking used to count the
same contingency table in three loops that agreed by hand."""
import pytest

from silica.kernel.recall.graph_export import area_hubs, area_tally


def _clique(prefix, n, group):
    nodes = [{"id": f"{prefix}{i}", "label": f"{prefix}{i}", "type": "note", "group": group}
             for i in range(n)]
    edges = [{"type": "EXTRACTED", "from": f"{prefix}{i}", "to": f"{prefix}{j}"}
             for i in range(n) for j in range(i + 1, n)]
    return nodes, edges


def test_the_tally_counts_linked_pairs_inside_and_between_areas_once():
    na, ea = _clique("a", 3, 0)
    nb, eb = _clique("b", 3, 1)
    mutual = [{"type": "EXTRACTED", "from": "a0", "to": "b0"},
              {"type": "EXTRACTED", "from": "b0", "to": "a0"}]
    t = area_tally(na + nb, ea + eb + mutual)
    assert t.sizes == {0: 3, 1: 3}
    assert t.intra == {0: 3, 1: 3}
    assert t.inter == {(0, 1): 1}, "a mutual pair is one unit of coupling"
    assert t.deg["a0"] == 3 and t.deg["b1"] == 2


def test_the_hub_is_the_highest_degree_member_and_a_tie_goes_to_the_larger_id():
    """compute_report and structural_gaps already broke the tie this way;
    /shape took the first member in file order, so the matrix could name one
    note and the report another for the same area."""
    nodes = [{"id": x, "label": x, "type": "note", "group": 0} for x in ("b", "a", "c")]
    edges = [{"type": "EXTRACTED", "from": "a", "to": "c"},
             {"type": "EXTRACTED", "from": "b", "to": "c"}]
    t = area_tally(nodes, edges)
    assert area_hubs(t) == {0: "c"}
    t = area_tally(nodes[:2] + [{"id": "d", "label": "d", "type": "note", "group": 1}],
                   [{"type": "EXTRACTED", "from": "a", "to": "d"},
                    {"type": "EXTRACTED", "from": "b", "to": "d"}])
    assert area_hubs(t)[0] == "b"


def test_the_report_names_the_hub_the_tally_does_not_the_directed_degree_one(monkeypatch):
    """The report chose an area's hub on in+out degree, where a mutual pair
    counts twice; the matrix and the gap overlay on linked pairs. On the
    728-note vault 6 of 103 areas got two names. Here f has four mutual links
    (directed 9, pairs 5) and a six plain ones (directed 6, pairs 6). One area
    by fiat: Louvain splits every small graph that shows the difference."""
    from silica.kernel.recall import graph_export as ge
    from silica.kernel.report.graph_report import compute as cm

    def one_area(nodes, edges):
        for n in nodes:
            n["group"] = 0
        return []
    # compute_report imports the name inside the function, so the patch goes
    # on the module it reads it from.
    monkeypatch.setattr(ge, "detect_communities", one_area)

    def E(x, y):
        return {"type": "EXTRACTED", "from": x, "to": y}
    edges = ([E("a", x) for x in "bcdefg"] + [E("f", x) for x in "bcde"]
             + [E(x, "f") for x in "bcde"] + [E("g", "b"), E("g", "c")])
    nodes = [{"id": x, "label": x, "type": "note"} for x in "abcdefg"]
    r = cm.compute_report(analytics=False, _nodes_edges_override=(nodes, edges), _mtimes_override={})
    hubs = area_hubs(area_tally(nodes, edges))
    assert hubs == {0: "a"}
    assert {c.cluster_id: c.hub for c in r.clusters} == hubs


@pytest.fixture
def client(tmp_vault):
    _fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def test_the_report_the_matrix_and_the_ranking_read_one_tally(client, tmp_vault):
    tmp_vault.note("A.md", "[[B]] [[C]]")
    tmp_vault.note("B.md", "[[A]] [[C]]")
    tmp_vault.note("C.md", "[[A]] [[B]] [[D]]")
    tmp_vault.note("D.md", "[[E]] [[F]] [[C]]")
    tmp_vault.note("E.md", "[[D]] [[F]]")
    tmp_vault.note("F.md", "[[D]] [[E]]")
    from silica.kernel.recall.graph_export import build_graph_data, detect_communities
    from silica.kernel.report.graph_report.compute import compute_report

    nodes, edges = build_graph_data(folder="")
    detect_communities(nodes, edges)
    t = area_tally(nodes, edges)
    r = compute_report(analytics=True, _nodes_edges_override=(nodes, edges), _mtimes_override={})
    s = client.get("/shape").json()

    ids = [a["id"] for a in s["areas"]]
    assert sorted(ids) == sorted(t.sizes)
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            want = t.intra[a] if a == b else t.inter.get((min(a, b), max(a, b)), 0)
            assert s["matrix"][i][j] == want
            assert r.inter_cluster.get(f"{min(a, b)}|{max(a, b)}", 0) == want
    assert s["matrix"] == [[3, 1], [1, 3]]

    hubs = area_hubs(t)
    assert {a["id"]: a["path"] for a in s["areas"]} == hubs
    assert {c.cluster_id: c.hub for c in r.clusters} == hubs
    assert set(hubs.values()) == {"C.md", "D.md"}
