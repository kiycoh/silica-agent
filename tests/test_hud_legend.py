# tests/test_hud_legend.py
"""The graph HUD answers three questions, and hides its machinery under them.

It used to stack six panels: renderer, edge types, semantic zones, node state, a
91-row community list and three force sliders, with a rebuild button under all
of it. Two of those (the list, the sliders) held most of the height, and neither
is legend -- they are controls you reach for once. So the reading you actually
came for ("what is this line, what is this dot, what does the colour mean") was
buried in its own machinery.

Nothing was deleted, which is the part these tests exist to keep true: the list
opens from the row that counts it, the forces are one fold down, and every id
the JS drives is still there.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME_JS = ROOT / "silica" / "ui" / "web" / "static" / "graph-frame.js"


def _hud(html: str) -> str:
    start = html.index('<div id="hud">')
    return html[start:html.index('<div id="drawer">', start)]


def _render() -> str:
    from silica.ui.web.graph_view import render_html
    nodes = [
        {"id": "a.md", "label": "A", "type": "note", "group": 0, "size": 3,
         "betweenness": 0.4, "path": "a.md"},
        {"id": "b.md", "label": "B", "type": "note", "group": 0, "size": 2,
         "betweenness": 0.0, "path": "b.md"},
    ]
    edges = [{"from": "a.md", "to": "b.md", "type": "EXTRACTED"},
             {"from": "b.md", "to": "a.md", "type": "SIMILAR"}]
    return render_html(nodes, edges, communities=[], lib_js="// x")


def test_the_panel_asks_three_questions_in_reading_order():
    """Lines, dots, colour: the order you ask them of a picture of a graph."""
    hud = _hud(_render())
    heads = [hud.index(">Edges<"), hud.index(">Nodes <"), hud.index(">Colour<")]
    assert heads == sorted(heads), "the three headings are out of reading order"
    # the old names are gone, not merely re-styled
    for dead in (">Edge types<", ">Node state<", ">Communities<", ">Semantic zones<"):
        assert dead not in hud, f"{dead} survived the regrouping"


def _areas_render() -> str:
    from silica.ui.web.graph_view import Community, render_html
    nodes = [{"id": f"{g}{i}.md", "label": f"{g.upper()}{i}", "type": "note", "group": n, "path": f"{g}{i}.md"}
             for n, (g, k) in enumerate([("a", 3), ("b", 2), ("s", 1)]) for i in range(k)]
    edges = [{"from": "a0.md", "to": "a1.md", "type": "EXTRACTED"},
             {"from": "a0.md", "to": "a2.md", "type": "EXTRACTED"},
             {"from": "b0.md", "to": "b1.md", "type": "EXTRACTED"}]
    comms = [Community(id=0, label="alpha · topics", color="#111", size=3),
             Community(id=1, label="beta · topics", color="#222", size=2),
             Community(id=2, label="solo · topics", color="#333", size=1)]
    return render_html(nodes, edges, communities=comms, lib_js="// x")


def test_the_area_list_is_the_answer_to_the_row_that_counts_it():
    """Closed, the row states how many areas there are, which IS the legend.
    Open, an area is a scope (ADR-0058): one isolate, the app's, so the picker
    shows it and compare is measured against it."""
    hud = _hud(_areas_render())
    fold = hud[hud.index('<details id="areas-fold">'):hud.index("</details>")]
    assert "structural areas" in fold
    assert 'id="legend-box"' in fold and 'id="legend-all"' not in fold
    assert 'id="sort-communities"' in fold and "toggleCommunitySort()" in fold
    assert 'onclick="scopeArea(0)"' in fold and "filterCommunity" not in hud
    # it counts what the matrix counts: the one-note area is the trailing row
    assert '<span class="ct">2</span>' in fold.split("</summary>")[0]
    assert "scopeArea(2)" not in fold and "single-note areas<span class=\"ct\">1</span>" in fold
    # named by the hub, the topics on the tooltip: the zones keep the topics
    assert '<span class="an">A0</span>' in fold and 'title="A0 · topics: alpha · topics"' in fold
    # and it is closed on arrival: an open list is the height problem
    assert "<details id=\"areas-fold\" open" not in hud
    js = FRAME_JS.read_text()
    assert "activeCommunity" not in js, "a second isolate beside the scope is back"
    assert 'window.parent.postMessage({ type: "silica-pick-scope", scope }, "*");' in js


def test_the_mechanics_are_on_the_foot_bar_and_not_in_the_key():
    """The panel is the Graph's key and nothing else (ADR-0058): the forces and
    the rebuild are what you reach for once, when the simulation itself is
    wrong, so they open from the bar that runs it."""
    html = _render()
    hud = _hud(html)
    assert "forces-fold" not in hud
    foot = html[html.index('<div id="gstat">'):html.index('<div id="hud">')]
    fold = foot[foot.index('<details id="forces-fold">'):]
    for control in ("sl-repel", "sl-dist", "sl-center", "resetForces()",
                    "onForceSlider()", "location.reload()"):
        assert control in fold, f"{control} is not under the Forces fold"
    assert '<details id="forces-fold" open' not in foot


def test_nothing_the_frame_drives_lost_its_id():
    """The regrouping moved markup, not behaviour. Every id below is read or
    written by a function in this same file, and a rename here fails silently in
    the browser: the handler just stops finding its element."""
    html = _render()
    for el in ("cb-extracted", "cb-ambiguous", "cb-gaps", "cb-similar",
               "state-legend", "st-hub", "st-orphan", "st-ghost",
               "st-ghost-row", "legend-box", "sort-communities", "gstat",
               "mode-toggle",
               "sl-repel", "sl-dist", "sl-center", "fv-repel", "fv-dist", "fv-center"):
        assert f'id="{el}"' in html, f"{el} is gone from the frame"


def test_the_nodes_rows_hide_with_the_rings_and_the_heading_keeps_size():
    """The rows under Nodes are a 2D-only channel and setMode hides their
    block. The heading stays outside it: it carries the one mark no row names,
    size, and 3D draws size too."""
    hud = _hud(_render())
    assert hud.index("size = how much the vault routes through it") < hud.index('<div id="state-legend">')
    block = hud[hud.index('<div id="state-legend">'):]
    assert "hub<span" in block and "load-bearing" not in hud.split('<div id="state-legend">')[0]
    js = FRAME_JS.read_text()
    assert 'document.getElementById("state-legend").style.display = is2D()' in js


def test_a_ghost_follows_unresolved():
    """466 of 1,194 nodes floated with no line to explain them while the
    unresolved row was off. A ghost is the far end of an unresolved link."""
    js = FRAME_JS.read_text()
    filt = js[js.index("function computeFilters("):js.index("function applyFilters(")]
    assert '(n.type === "ghost" && !showAmbiguous)' in filt
    assert 'getElementById("st-ghost-row").classList.toggle("off", !showAmbiguous)' in js


def test_the_two_partitions_are_two_rows_of_one_section():
    """Structural areas and semantic zones are both answers to "what does this
    colour mean", so they read together. The zone rows only exist where the
    vault has vectors, which is why they are a separate f-string."""
    from silica.ui.web.graph_view import Zone, render_html
    nodes = [{"id": "a.md", "label": "A", "type": "note", "group": 0, "sgroup": 0,
              "size": 3, "betweenness": 0.4, "path": "a.md"}]
    zones = [Zone(id=0, label="z", color="#888", color_paper="#444", size=1)]
    hud = _hud(render_html(nodes, [], communities=[], lib_js="// x", zones=zones))
    colour = hud[hud.index(">Colour<"):]
    assert "structural areas" in colour and "semantic zones" in colour
    assert 'id="cb-zones"' in colour and 'id="cb-zone-nodes"' in colour
    # "notes" means "the zones alone": hidden until there are zones on, or it
    # blanks the frame
    assert '<label class="filter-row" id="zone-nodes-row" style="display:none' in colour
    upd = FRAME_JS.read_text().split("function updateZoneFilter() {")[1].split("\n}")[0]
    assert "if (cbNodes && !showZones) cbNodes.checked = true;" in upd
    # no vectors, no rows: an empty checkbox promises a layer with nothing behind it
    plain = _hud(_render())
    assert 'id="cb-zones"' not in plain


def test_the_edges_are_ordered_by_the_rule_they_are_drawn_by():
    """Present links first, then after a gap the absent ones, the three the
    dash draws (ADR-0058). No Highlight group: width carried it and width does
    not read."""
    hud = _hud(_render())
    assert ">Highlight<" not in hud
    edges = hud[hud.index(">Edges<"):hud.index(">Nodes")]
    absent = edges[edges.index('<div class="edge-absent">'):]
    present = edges[:edges.index('<div class="edge-absent">')]
    for row in ('id="cb-extracted"', 'id="cb-ambiguous"', 'id="cb-similar"'):
        assert row in present, row
    assert 'id="cb-gaps"' in absent and 'id="cb-gaps"' not in present
