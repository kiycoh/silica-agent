# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bridges: the wikilinks that join two areas, ranked by how rare their pair of
areas is (ADR-0045). One ranking feeds the report row and the explore surface."""
from silica.kernel.recall.graph_export import area_bridges
from silica.kernel.report.graph_report.compute import compute_report


def _clique(prefix: str, n: int, group: int, sgroup: int | None = None):
    nodes = [
        {"id": f"{prefix}{i}", "label": f"{prefix}{i}", "type": "note", "group": group,
         **({"sgroup": sgroup} if sgroup is not None else {})}
        for i in range(n)
    ]
    edges = [
        {"type": "EXTRACTED", "from": f"{prefix}{i}", "to": f"{prefix}{j}"}
        for i in range(n)
        for j in range(i + 1, n)
    ]
    return nodes, edges


def _link(a: str, b: str, **extra):
    return {"type": "EXTRACTED", "from": a, "to": b, **extra}


def test_the_lone_link_between_two_small_areas_outranks_two_links_between_big_ones():
    """Rarity first, size second. The shared gap score size_a * size_b /
    (1 + links) put a 2-link pair of 6-note areas (36 / 3 = 12) above the only
    link between two 3-note areas (9 / 2 = 4.5); on the 728-note test vault 7
    of its top ten touched the 113-note area. A bridge is the link that, taken
    away, separates two areas, and that is the count of links, not the size."""
    na, ea = _clique("a", 6, 0)
    nb, eb = _clique("b", 6, 1)
    nc, ec = _clique("c", 3, 2)
    nd, ed = _clique("d", 3, 3)
    rows = area_bridges(
        na + nb + nc + nd,
        ea + eb + ec + ed + [_link("a0", "b0"), _link("a1", "b1"), _link("c0", "d0")],
    )
    assert [(r["a"], r["b"]) for r in rows] == [("c0", "d0"), ("a0", "b0"), ("a1", "b1")]
    assert rows[0]["links_between"] == 1 and rows[0]["joined"] == 9
    assert rows[1]["links_between"] == 2 and rows[1]["joined"] == 36
    assert "score" not in rows[0], "no scalar: two keys, two questions"


def test_at_equal_rarity_the_pair_of_areas_holding_more_notes_comes_first():
    na, ea = _clique("a", 4, 0)
    nb, eb = _clique("b", 4, 1)
    nc, ec = _clique("c", 2, 2)
    nd, ed = _clique("d", 2, 3)
    rows = area_bridges(na + nb + nc + nd, ea + eb + ec + ed + [_link("c0", "d0"), _link("a0", "b0")])
    assert [(r["a"], r["joined"]) for r in rows] == [("a0", 16), ("c0", 4)]


def test_a_prose_link_outranks_a_frontmatter_scaffold_at_equal_rarity():
    na, ea = _clique("a", 3, 0)
    nb, eb = _clique("b", 3, 1)
    nc, ec = _clique("c", 3, 2)
    rows = area_bridges(
        na + nb + nc,
        ea + eb + ec + [_link("a0", "b0", scaffold=True), _link("a0", "c0")],
    )
    assert [(r["b"], r["scaffold"]) for r in rows] == [("c0", False), ("b0", True)]


def test_a_mutual_pair_is_one_bridge():
    na, ea = _clique("a", 3, 0)
    nb, eb = _clique("b", 3, 1)
    rows = area_bridges(na + nb, ea + eb + [_link("a0", "b0"), _link("b0", "a0")])
    assert len(rows) == 1


def test_a_row_says_whether_the_two_notes_share_a_semantic_zone():
    na, ea = _clique("a", 2, 0, sgroup=7)
    nb, eb = _clique("b", 2, 1, sgroup=7)
    nc, ec = _clique("c", 2, 2, sgroup=9)
    nd, ed = _clique("d", 2, 3)              # no vector: sgroup absent
    rows = area_bridges(
        na + nb + nc + nd,
        ea + eb + ec + ed + [_link("a0", "b0"), _link("a1", "c0"), _link("a0", "d0")],
    )
    zone = {r["b"]: r["zone"] for r in rows}
    assert zone == {"b0": "same", "c0": "different", "d0": None}


def test_the_zones_are_ranked_by_the_same_producer_and_the_row_reads_the_areas():
    """ADR-0060 Decision 5: the zone reading is the area reading with the
    partitions swapped, not a second producer. a0-b0 crosses two zones inside
    one area (the case 114 of 155 zone pairs were on the test vault), a1-c0
    crosses both partitions, and a link inside one zone is no row."""
    na, ea = _clique("a", 2, 0, sgroup=5)
    nb, eb = [{"id": "b0", "label": "b0", "type": "note", "group": 0, "sgroup": 6}], []
    nc, ec = _clique("c", 2, 1, sgroup=7)
    nd, ed = _clique("d", 2, 2, sgroup=5)
    rows = area_bridges(
        na + nb + nc + nd,
        ea + eb + ec + ed + [_link("a0", "b0"), _link("a1", "c0"), _link("a0", "d0")],
        key="sgroup",
    )
    assert [(r["a"], r["b"], r["area_a"], r["area_b"], r["zone"]) for r in rows] == [
        ("a1", "c0", 5, 7, "different"), ("a0", "b0", 5, 6, "same")]   # one link each: 4x2 joined before 4x1


def test_a_row_carries_the_areas_it_joins():
    na, ea = _clique("a", 3, 0)
    nb, eb = _clique("b", 2, 1)
    rows = area_bridges(na + nb, ea + eb + [_link("a0", "b1")])
    assert rows[0]["area_a"] == 0 and rows[0]["area_b"] == 1
    assert rows[0]["size_a"] == 3 and rows[0]["size_b"] == 2


def test_the_report_row_is_the_same_ranking():
    """Two triangles joined once: the hub-sum score printed 6 here (deg 3 + deg
    3, no shared neighbour) and put hub-to-hub links first on a real vault.
    The row now says what it measured: one link, 3 x 3 notes joined."""
    nodes = [{"id": x, "label": x, "type": "note"} for x in "ABCDEF"]
    edges = [_link("A", "B"), _link("B", "C"), _link("A", "C"),
             _link("D", "E"), _link("E", "F"), _link("D", "F"), _link("C", "D")]
    r = compute_report(analytics=True, _nodes_edges_override=(nodes, edges), _mtimes_override={})
    assert [(b.source, b.target, b.links_between, b.joined) for b in r.bridges] == [("C", "D", 1, 9)]


# --- /shape ships the rows the explore surface draws --------------------------

import pytest


@pytest.fixture
def client(tmp_vault):
    _fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def _two_triangles(tmp_vault, c_body: str) -> None:
    tmp_vault.note("A.md", "[[B]] [[C]]")
    tmp_vault.note("B.md", "[[A]] [[C]]")
    tmp_vault.note("C.md", c_body)
    tmp_vault.note("D.md", "[[E]] [[F]]")
    tmp_vault.note("E.md", "[[D]] [[F]]")
    tmp_vault.note("F.md", "[[D]] [[E]]")


def test_shape_ships_the_bridges_with_the_sentence_that_carries_the_link(client, tmp_vault):
    _two_triangles(tmp_vault, "[[A]] [[B]]\n\nThe proof in C leans on [[D]] for its lemma. A second sentence.\n")
    rows = client.get("/shape").json()["bridges"]
    assert [(r["a"]["name"], r["b"]["name"]) for r in rows] == [("C", "D")]
    row = rows[0]
    assert row["links_between"] == 1 and row["joined"] == 9 and "score" not in row
    assert row["scaffold"] is False and row["other"] is None
    assert row["a"]["part"] and row["b"]["part"] and row["a"]["part"] != row["b"]["part"]
    assert row["a"]["path"] == "C.md" and row["b"]["path"] == "D.md"
    assert row["passage"] == "The proof in C leans on D for its lemma."
    assert "concepts" not in row   # measured generic on 74/74 rows of the test vault: not shipped


def test_a_link_carried_by_a_bullet_or_a_quote_still_has_its_passage(client, tmp_vault):
    """31 of 55 prose bridges on the test vault had no passage because the
    snippet rule refuses list items and quotes. Here they ARE the prose:
    `- **Hazard**: [[Funzione matematica]] chiave per...` is the sentence."""
    _two_triangles(tmp_vault, "[[A]] [[B]]\n\n- **Lemma**: rests on [[D]] for the bound.\n")
    row = client.get("/shape").json()["bridges"][0]
    assert row["passage"] == "Lemma: rests on D for the bound."
    tmp_vault.write(tmp_vault.note("C.md"), "[[A]] [[B]]\n\n> To do it, [[D|derive]] the log-likelihood.\n")
    row = client.get("/shape").json()["bridges"][0]
    assert row["passage"] == "To do it, derive the log-likelihood."


def test_a_frontmatter_bridge_has_no_sentence_to_show(client, tmp_vault):
    _two_triangles(tmp_vault, "---\nrelated:\n  - \"[[D]]\"\n---\n[[A]] [[B]]\n")
    row = client.get("/shape").json()["bridges"][0]
    assert row["scaffold"] is True and row["passage"] == ""


def test_shape_ships_both_readings_and_the_colours_bridges_paints_with(client, tmp_vault):
    """ADR-0060: the zone rows ride on the same load (empty without an embed
    index, never missing), and an area carries the colour its notes wear on the
    graph, so a disc and the dots it stands for agree."""
    from silica.kernel.recall.graph_export import _community_color

    _two_triangles(tmp_vault, "[[A]] [[B]] [[D]]")
    s = client.get("/shape").json()
    assert s["zone_bridges"] == [] and s["zones"] == []
    assert [(a["color"], a["color_paper"]) for a in s["areas"]] == [
        (_community_color(a["id"]), _community_color(a["id"], on_paper=True)) for a in s["areas"]]
    assert {r["a"]["part"] for r in s["bridges"]} <= {a["label"] for a in s["areas"]}
