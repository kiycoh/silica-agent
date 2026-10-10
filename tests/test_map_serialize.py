"""Serializer + bounding invariants for kernel/mindmap."""
from __future__ import annotations

import json

import networkx as nx

from silica.kernel.recall.mindmap import (
    MapMaterials,
    _resolve_in,
    build_mapview,
    mapview_to_canvas,
    render_map_svg,
)

_MUTED = "#5a6372"


def _materials(latent=(), n_wiki=2):
    g = nx.Graph()
    for i in range(n_wiki):
        g.add_edge("root.md", f"n{i}.md")
    return MapMaterials(
        graph=g,
        titles={p: p[:-3] for p in g.nodes},
        community_of={p: 0 for p in g.nodes},
        latent=list(latent),
    )


def test_canvas_is_valid_and_conformant():
    mv = build_mapview("root.md", _materials([("lat.md", "Lat", 0.9)]))
    canvas = mapview_to_canvas(mv)
    # Round-trips as JSON.
    assert json.loads(json.dumps(canvas)) == canvas

    ids = {n["id"] for n in canvas["nodes"]}
    for node in canvas["nodes"]:
        assert node["type"] == "file"
        assert node["file"].endswith(".md")
    for edge in canvas["edges"]:
        assert edge["fromNode"] in ids
        assert edge["toNode"] in ids


def test_latent_edges_muted_with_tilde_label():
    mv = build_mapview("root.md", _materials([("lat.md", "Lat", 0.9)]))
    canvas = mapview_to_canvas(mv)
    kinds = {(e.src, e.dst): e.kind for e in mv.edges}
    for edge in canvas["edges"]:
        kind = kinds.get((edge["fromNode"], edge["toNode"])) or kinds.get(
            (edge["toNode"], edge["fromNode"])
        )
        if kind == "latent":
            assert edge["label"] == "≈"
            assert edge["color"] == _MUTED
        else:
            assert edge["label"] == ""
            assert edge["color"] != _MUTED


def test_cap_respected():
    mv = build_mapview("root.md", _materials(n_wiki=20), max_nodes=5)
    assert len(mv.nodes) <= 5


def test_both_legs_abstain_yields_wikilink_tree_only():
    mv = build_mapview("root.md", _materials(latent=[], n_wiki=3))
    assert mv.edges
    assert all(e.kind == "wikilink" for e in mv.edges)
    assert {n.id for n in mv.nodes} == {"root.md", "n0.md", "n1.md", "n2.md"}


def test_resolve_accepts_title_and_path_in_subfolder():
    # The GUI bug: a note in a subfolder given by its TITLE must resolve to the
    # full graph key (path with .md), not stay unmatched.
    paths = ["AI/Storia dell'IA moderna.md", "concetti/grafo.md"]
    titles = {
        "AI/Storia dell'IA moderna.md": "Storia dell'IA moderna",
        "concetti/grafo.md": "Grafo",
    }
    assert _resolve_in("Storia dell'IA moderna", paths, titles) == "AI/Storia dell'IA moderna.md"
    assert _resolve_in("AI/Storia dell'IA moderna", paths, titles) == "AI/Storia dell'IA moderna.md"
    assert _resolve_in("AI/Storia dell'IA moderna.md", paths, titles) == "AI/Storia dell'IA moderna.md"
    assert _resolve_in("nope", paths, titles) is None


def test_svg_renders_and_dashes_latent():
    mv = build_mapview("root.md", _materials([("lat.md", "Lat", 0.9)]))
    svg = render_map_svg(mv)
    assert "<svg" in svg and "</svg>" in svg
    assert "stroke-dasharray" in svg  # the latent edge is dashed


def _two_area_view():
    g = nx.Graph()
    for i in range(4):
        g.add_edge("root.md", f"a{i}.md")
    g.add_edge("root.md", "b0.md")
    community_of = {p: 0 for p in g.nodes}
    community_of["b0.md"] = 1
    return build_mapview("root.md", MapMaterials(
        graph=g,
        titles={p: p[:-3] for p in g.nodes},
        community_of=community_of,
        latent=[("lat.md", "Lat", 0.9)],
        community_label={0: "Area zero", 1: "Area one", 7: "Not on this map"},
    ))


def test_wedges_tile_the_circle_and_hold_their_notes():
    import math

    from silica.kernel.recall.mindmap import _wedges

    mv = _two_area_view()
    rest = [n for n in mv.nodes if n.hop > 0]
    wedges = _wedges(rest)
    assert math.isclose(sum(a1 - a0 for _c, a0, a1, _m in wedges), 2 * math.pi)
    for _c, a0, a1, members in wedges:
        for n in members:
            a = math.atan2(n.y, n.x) % (2 * math.pi)
            assert a0 - 1e-9 <= a <= a1 + 1e-9 or a0 - 1e-9 <= a - 2 * math.pi <= a1 + 1e-9


def test_map_names_its_areas_and_marks_related_notes():
    mv = _two_area_view()
    assert mv.areas == {0: "Area zero", 1: "Area one"}   # only areas on the map
    page = render_map_svg(mv)
    assert "Area zero" in page                 # a wedge of several notes is named
    assert "Area one" not in page              # a one-note wedge is named by its note
    assert 'class="dot root"' in page
    assert 'class="dot hop1 latent"' in page   # related, no link: the hollow dot
