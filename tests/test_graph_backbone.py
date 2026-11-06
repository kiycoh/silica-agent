# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0136's half in the export: the backbone the layout pulls on, the
sub-areas the atlas draws as provinces, the one rule that names an area and a
sub-area, and the partition the atlas draws."""
from __future__ import annotations

import math

from silica.kernel.graph.graph_export import (
    _is_file_slug,
    area_hubs,
    area_tally,
    backbone_links,
    detect_sub_areas,
    hub_of,
)


def _note(nid, group=0):
    return {"id": nid, "label": nid.removesuffix(".md"), "type": "note", "group": group}


def _link(a, b, **extra):
    return {"from": a, "to": b, "type": "EXTRACTED", **extra}


def _clique(prefix, n, group=0):
    nodes = [_note(f"{prefix}{i}.md", group) for i in range(n)]
    edges = [_link(f"{prefix}{i}.md", f"{prefix}{j}.md") for i in range(n) for j in range(i + 1, n)]
    return nodes, edges


def test_the_backbone_keeps_every_linked_note_and_drops_the_hubs_spokes():
    """Four cliques of five and a hub linked to all twenty: the hub's
    neighbourhood dwarfs any leaf's, so its spokes score low and it keeps few
    of them, while the spanning tree still reaches every note."""
    cliques = [_clique(p, 5) for p in "abcd"]
    leaves = [n for ns, _ in cliques for n in ns]
    hub = [_note("hub.md")]
    spokes = [_link("hub.md", n["id"]) for n in leaves]
    edges = [e for _, es in cliques for e in es] + spokes
    kept = backbone_links(leaves + hub, edges)
    stamped = [e for e in edges if "backbone" in e]
    assert kept == len(stamped)
    reached = {x for e in stamped for x in (e["from"], e["to"])}
    assert reached == {n["id"] for n in leaves + hub}
    hub_kept = [e for e in stamped if "hub.md" in (e["from"], e["to"])]
    # The hub picks ceil(sqrt(20)) = 5 and the spanning tree adds a spoke per
    # clique its picks missed; every leaf picks inside its clique (Jaccard
    # 0.67 against 0.19 for a spoke).
    assert len(hub_kept) <= 8 < len(spokes) / 2
    assert all(0 <= e["backbone"] <= 1 for e in stamped)


def test_a_backbone_pair_pulls_once_and_carries_its_jaccard():
    nodes = [_note(f"{c}.md") for c in "abc"]
    edges = [_link("a.md", "b.md"), _link("b.md", "a.md"), _link("b.md", "c.md"), _link("a.md", "c.md")]
    backbone_links(nodes, edges)
    ab = [e for e in edges if {e["from"], e["to"]} == {"a.md", "b.md"} and "backbone" in e]
    assert len(ab) == 1, "a mutual link is two edges and pulls as one"
    # N(a) = {b, c}, N(b) = {a, c}: one shared neighbour of three.
    assert math.isclose(ab[0]["backbone"], round(1 / 3, 4))


def test_a_ghost_or_a_silica_note_is_never_on_the_backbone():
    nodes = [_note("a.md"), _note("b.md"), {"id": "plan.md", "label": "plan", "type": "plan", "group": -1},
             {"id": "__unresolved__x", "label": "x", "type": "ghost", "group": -1}]
    edges = [_link("a.md", "b.md"), _link("plan.md", "a.md"),
             {"from": "a.md", "to": "__unresolved__x", "type": "AMBIGUOUS"}]
    backbone_links(nodes, edges)
    assert [("backbone" in e) for e in edges] == [True, False, False]


def test_sub_areas_split_an_area_along_its_own_links():
    na, ea = _clique("a", 5)
    nb, eb = _clique("b", 5)
    other, eo = _clique("c", 3, group=1)
    ghost = {"id": "__unresolved__g", "label": "g", "type": "ghost", "group": -1}
    nodes = na + nb + other + [ghost]
    subs = detect_sub_areas(nodes, ea + eb + eo + [_link("a0.md", "b0.md")])
    assert [(s.area, s.size) for s in subs] == [(0, 5), (0, 5), (1, 3)]
    assert [s.id for s in subs] == [0, 1, 2]
    by = {n["id"]: n["sub"] for n in nodes}
    assert len({by[n["id"]] for n in na}) == 1 and by["a0.md"] != by["b0.md"]
    assert ghost["sub"] == -1
    assert all(s.hub.startswith(("a", "b", "c")) for s in subs)


def test_a_file_slug_is_a_title_made_of_parts_and_a_number():
    for slug in ("Goodfellow-38-15-2-transfer-learning.md", "src/2026-10-28.md", "slide_cap_03.md"):
        assert _is_file_slug(slug), slug
    for name in ("GPT-4.md", "Cross-validation.md", "Machine Learning (corso).md", "RNN-RBM.md",
                 "Lezione 30.md"):
        assert not _is_file_slug(name), name


def test_a_group_is_named_by_its_busiest_note_that_is_not_a_file_slug():
    deg = {"Goodfellow-01-part.md": 9, "Backpropagation.md": 4, "Adam.md": 4}
    assert hub_of(deg, deg) == "Backpropagation.md", "slug skipped, tie to the larger id"
    assert hub_of(["Goodfellow-01-part.md", "Goodfellow-02-x.md"], {"Goodfellow-01-part.md": 1,
                                                                    "Goodfellow-02-x.md": 3}) \
        == "Goodfellow-02-x.md", "all slugs: the busiest slug still names it"


def test_an_area_and_its_sub_areas_are_named_by_one_rule():
    nodes = [_note("Goodfellow-01-part.md"), _note("Backpropagation.md"), _note("Adam.md")]
    edges = [_link("Goodfellow-01-part.md", "Backpropagation.md"),
             _link("Goodfellow-01-part.md", "Adam.md"), _link("Backpropagation.md", "Adam.md")]
    assert area_hubs(area_tally(nodes, edges)) == {0: "Backpropagation.md"}
    assert [s.hub for s in detect_sub_areas(nodes, edges)] == ["Backpropagation.md"]


def _export(monkeypatch, tmp_path, nodes, edges, zones):
    from silica.kernel.graph import graph_export as ge
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    monkeypatch.setattr(ge, "build_graph_data", lambda folder="": (nodes, edges))
    monkeypatch.setattr(ge, "knn_edges", lambda nodes, k=6: [])
    called = []

    def semantic(nodes, edges):
        called.append(1)
        return zones

    monkeypatch.setattr(ge, "detect_semantic_partition", semantic)
    return gv.export_graph(output_path=str(tmp_path / "g.html")), called


def test_the_atlas_draws_the_areas_and_the_semantic_partition_is_not_computed(monkeypatch, tmp_path):
    """ADR-0136, the owner's answer to Open 2: the graph reads the zones only
    where the atlas draws them."""
    nodes, edges = _clique("a", 4, group=-1)
    res, called = _export(monkeypatch, tmp_path, nodes, edges, [])
    assert res["zones"] is None and called == []
    assert res["backbone"] >= 3 and res["sub_areas"] == 1
    assert '"atlas":"areas"' in (tmp_path / "g.html").read_text(encoding="utf-8").replace(": ", ":")


def test_a_vault_with_no_area_of_three_notes_draws_the_zones(monkeypatch, tmp_path):
    """Open 4: a link-free vault's atlas is its semantic zones, and says so."""
    from silica.kernel.graph.graph_export import Zone

    nodes = [_note(f"n{i}.md", group=-1) for i in range(4)]
    zone = Zone(id=0, label="topic", color="#888", color_paper="#444", size=4)
    res, called = _export(monkeypatch, tmp_path, nodes, [], [zone])
    html = (tmp_path / "g.html").read_text(encoding="utf-8")
    assert called == [1] and res["zones"] == 1
    assert '"atlas":"zones"' in html.replace(": ", ":") and "atlas · semantic zones" in html
