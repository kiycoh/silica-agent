# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bridges are read on the Bridges surface, not highlighted on the Graph
(ADR-0058 Decision 11, superseding ADR-0045 Decision 6).

The Highlight group told "between areas" apart by width (2.6 against 1.6 px in
2D, 1.2 against 1px in 3D) and "between zones" by a second neutral. In review
both read as resolved: a one-pixel difference on a graph that zooms is not a
channel. What the group answered survives in two places: scoping an area or a
zone keeps the far end of every wikilink that leaves it, and the Bridges
surface draws one part and its neighbours (ADR-0060)."""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
FRAME_JS = WEB / "graph-frame.js"
FRAME_HTML = WEB / "graph-frame.html"


def _ctx(edges, communities=()):
    from silica.ui.web.graph_view import _frame_context

    nodes = [{"id": "A", "label": "A", "type": "note", "group": 0},
             {"id": "A2", "label": "A2", "type": "note", "group": 0},
             {"id": "B", "label": "B", "type": "note", "group": 1},
             {"id": "B2", "label": "B2", "type": "note", "group": 1}]
    return _frame_context(nodes, edges, list(communities), "t", "", "", [])


def test_the_graph_offers_no_highlight():
    edges = [{"id": "e0", "from": "A", "to": "B", "type": "EXTRACTED"}]
    ctx = _ctx(edges)
    assert "highlight_section" not in ctx
    rendered = "".join(str(v) for v in ctx.values())
    assert 'id="cb-bridges"' not in rendered and 'id="cb-discord"' not in rendered
    assert "${highlight_section}" not in FRAME_HTML.read_text()
    js = FRAME_JS.read_text()
    for gone in ("showBridges", "showDiscord", "hoverBridge", "selectBridge", "bridgeTip",
                 "silica-open-bridge", ".onLinkHover(", ".onLinkClick(", ".linkLabel("):
        assert gone not in js, f"{gone} is back in the frame"


def test_no_edge_is_told_apart_by_width():
    js = FRAME_JS.read_text()
    assert "G.linkWidth(l => l.width || 1)" in js     # 2D: the layer's own rank, nothing more
    assert "G.linkWidth(0)" in js                     # 3D: the merged 1px lines
    assert "G.linkWidth(l => ((" not in js and "linkWidth(Graph.linkWidth())" not in js
    # nothing is lib-owned to be hovered: only the particle carriers are
    own = js.split("const libOwnsLink = ")[1].split("\n")[0]
    assert own == 'l => PARTICLES && (l.type === "GAP" || l.type === "SIMILAR");'


def test_compare_fades_the_links_inside_each_part():
    """The links between the two parts are the ones left standing: everything
    else a comparison shows paints as dimmed, at the same width."""
    js = FRAME_JS.read_text()
    paint = js[js.index("function linkPaint("):js.index("// --- 3D: every link")]
    assert "if (q >= FADE_STEPS || l._ctx || (AB && !on)) out = GP.linkDim;" in paint
    assert "const on = !!AB && !!l._ab;" in paint


def test_a_scoped_area_or_zone_keeps_the_far_end_of_its_links():
    """What a part reaches is the note across the link; a far end shows only
    the links that reach it from the part, never its own."""
    js = FRAME_JS.read_text()
    filt = js[js.index("function computeFilters("):js.index("function applyFilters(")]
    # A collection keeps them too since ADR-0066 decision 5.
    assert '(SCOPE.kind === "area" || SCOPE.kind === "zone" || SCOPE.kind === "collection")' in filt
    assert "inScope(a) !== inScope(b)" in filt
    # A link between two far ends stays hidden, unless it is a context
    # wikilink under the rim (ADR-0066 amendment).
    assert "const beyond = reach && a && b && !inScope(a) && !inScope(b);" in filt
    assert "(beyond && !e._ctx)" in filt


def test_the_work_panel_still_reads_a_bridge():
    """The Bridges surface is where a bridge is read now; the work panel keeps
    the reading, and the pair detail keeps its way into the list."""
    work = (WEB / "work.js").read_text()
    explore = (WEB / "app-explore.js").read_text()
    assert 'addEventListener("silica:bridge"' in work and "function renderBridge()" in work
    assert "window.openBridges = " in explore and "window.openBridges" in work


def test_the_frame_names_an_area_by_its_hub_like_the_shape_surfaces_do():
    """One name per area: its hub, a title the reader wrote, and the one the
    Areas surface and the scope picker use. Only areas of more than one note
    are named; a one-note area named after its hub is the note's name again."""
    import json

    from silica.ui.web.graph_view import Community

    comms = [Community(id=0, label="a · topics", color="#111", size=2),
             Community(id=1, label="b · topics", color="#222", size=2)]
    ctx = _ctx([{"id": "e0", "from": "A", "to": "A2", "type": "EXTRACTED"},
                {"id": "e1", "from": "A", "to": "B", "type": "EXTRACTED"},
                {"id": "e2", "from": "B", "to": "B2", "type": "EXTRACTED"}], comms)
    assert json.loads(ctx["graph_data"])["comm_hubs"] == {"0": "A", "1": "B"}
    js = FRAME_JS.read_text()
    assert "const COMM_HUBS = DATA.comm_hubs" in js
    area = js[js.index("function areaOf("):js.index("// --- The semantic zone layer")]
    assert "COMM_HUBS[" in area and "COMM_LABELS" not in area
