# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Graph viewer — the force-graph HTML emitter for the vault wikilink graph.

Split out of `silica.kernel.graph.graph_export` (which keeps the deterministic *data*
role: build_graph_data / detect_communities) and `overlays` (the proposal layers and
the node readings). This module turns nodes/edges/communities into a fully self-contained
HTML file, and `export_graph` is the build behind the tool, which is why it sits with the
tools and above `sources`: it reads Zotero and the citation layer, and a tool reaches it.

The document is three files under silica/ui/web/static/, each in the language
a browser and a linter read it in: graph-frame.html is the string.Template skeleton,
graph-frame.css the sheet, graph-frame.js the frame's code. This module fills the skeleton
with the counts, the legend and tree fragments, and one JSON island carrying
everything graph-frame.js needs for this document. All three are inlined at render
and never referenced by URL, because the emitted file opens from file:// with
no server behind it (ADR-0026); the renderer bundles and the Archivo face are
inlined for the same reason.

TWO renderers ship in every document: `3d-force-graph` (WebGL) and `force-graph`
(2D canvas), switched at runtime from the HUD. Not `numDimensions(2)` on the 3D
bundle — the point of 2D is readable node text, which canvas gives and a
flattened WebGL scene does not. They are kapsule siblings and share nearly the
whole chainable API, so `buildGraph()` is one builder with four branches.

Both bundles are *vendored* (silica/ui/web/static/, pinned to
3d-force-graph@1.80.0 and force-graph@1.51.2) and inlined into every emitted
file — the artifact opens offline, with no network at render time. There is no
CDN fallback anywhere: `lib_js=""` from a direct caller emits an empty script
(the tests pass a dummy), and `export_graph` (the production path) always
inlines the vendored bundles and raises loudly if either asset is missing.
"""
from __future__ import annotations

import html
import importlib.resources
import json
import logging
import string
from pathlib import Path

# The edge colours ride along so each legend swatch and the edges it stands for
# cannot drift apart: they were two literals of the same hex until one moved.
from silica.kernel.base.noteshape import NATIVE_TYPES
from silica.kernel.graph import file_tree, overlays
from silica.kernel.graph.overlays import (
    _EDGE_COLOR_COUPLED,
    _EDGE_COLOR_COUPLED_PAPER,
    _EDGE_COLOR_GAP,
    _EDGE_COLOR_GAP_PAPER,
    _EDGE_COLOR_PROPOSED,
    _EDGE_COLOR_PROPOSED_PAPER,
)
from silica.kernel.graph.graph_export import (
    _EDGE_COLOR_AMBIGUOUS,
    _EDGE_COLOR_AMBIGUOUS_PAPER,
    _EDGE_COLOR_EXTRACTED,
    _EDGE_COLOR_EXTRACTED_PAPER,
    _EDGE_COLOR_SIMILAR,
    _EDGE_COLOR_SIMILAR_PAPER,
    Community,
    SubArea,
    Zone,
)

logger = logging.getLogger(__name__)

# By the top package: the frame's files sit with the app's static tree, and naming
# `silica.ui.web` would import the layer above this one to read a folder.
_STATIC = importlib.resources.files("silica") / "ui" / "web" / "static"

# Both renderers, in load order: WebGL first (the default mode), canvas second.
_VENDORED_BUNDLES = ("3d-force-graph.min.js", "force-graph.min.js")


def _vendored_lib_js() -> str:
    """Both renderer bundles, concatenated: they are independent UMD modules
    exporting `ForceGraph3D` and `ForceGraph`, so callers keep one `lib_js`
    string. A missing bundle raises (see _asset) and nothing falls back to a
    CDN `<script src>`: that would silently reintroduce the network dependency
    this split removed and hide the packaging bug."""
    return ";\n".join(_asset(name) for name in _VENDORED_BUNDLES)


# The layout worker's d3-force-3d (graph-frame.js, "the layout in a worker"),
# pinned 3.0.6, the line both bundles carry it from. It reaches the page as the
# TEXT the worker is built from, in a type="text/plain" script that never runs,
# so the frame still opens from file:// with nothing to fetch (ADR-0026).
# Rebuilt from npm d3-force-3d@3.0.6 with an entry of
#   export { forceSimulation, forceManyBody, forceLink, forceCenter } from "d3-force-3d";
# through esbuild --bundle --format=iife --global-name=d3 --minify --legal-comments=none.
# ForceAtlas2 lays out 2D since ADR-0136, in the same worker: npm
# graphology-layout-forceatlas2@0.10.1, an entry of
#   export { default as iterate } from "graphology-layout-forceatlas2/iterate.js";
# through the same esbuild flags with --global-name=fa2. Only `iterate`: it
# works on flat arrays, so graphology itself never ships.
_SIM_BUNDLES = ("d3-force-3d.min.js", "forceatlas2.min.js")


def _sim_js() -> str:
    """The worker's bundles, refused if one could end its own script element.

    The parser ends script data at the first `</script` whatever the type, so
    that string inside a bundle would cut the worker short and paint the rest
    of it into the page as text."""
    out = []
    for name in _SIM_BUNDLES:
        src = _asset(name)
        if "</script" in src.lower():
            raise RuntimeError(f"graph_export: {name} contains '</script' and cannot be inlined")
        out.append(src)
    return ";\n".join(out)


def _vendored_font_face() -> str:
    """@font-face rule with the Archivo woff2 inlined as a data: URI, so the
    exported HTML stays fully self-contained (it is opened from file:// too).
    Cosmetic asset: if missing, degrade to the system-ui fallback, not a raise.

    Archivo's subset is 90KB against Lexend's 40KB, so every export it lands in
    grew by ~50KB when the identity consolidated on one sans
    (docs/specs/visual-identity.md). Subset to the glyphs an export actually
    uses if that ever shows up in a size complaint."""
    import base64

    res = _STATIC / "archivo-latin.woff2"
    if not res.is_file():
        return ""
    b64 = base64.b64encode(res.read_bytes()).decode("ascii")
    return (
        '@font-face{font-family:"Archivo";'
        f'src:url("data:font/woff2;base64,{b64}") format("woff2");'
        "font-weight:100 900;font-stretch:62% 125%;"
        "font-style:normal;font-display:swap}"
    )


# The frame's own files, beside the bundles and inlined the same way. Not
# `<script src>` as index.html does it: the emitted document is opened from
# file:// with no server behind it, so a URL is a reference to nothing there
# (ADR-0026). The cost is one read per render, which /graph already pays
# fifteen-fold for the bundles. d3-delaunay is the atlas's Voronoi (ADR-0136):
# npm d3-delaunay@6.0.4, an entry of `export { Delaunay } from "d3-delaunay";`
# through the worker bundles' esbuild flags with --global-name=d3Delaunay.
_FRAME_ASSETS = ("graph-frame.html", "graph-frame.css", "d3-delaunay.min.js", "graph-frame.js")


def _asset(name: str) -> str:
    """One file under ui/web/static/, or a RuntimeError that names it.

    Loud on purpose: a missing asset is a packaging bug, and the alternative
    (an empty string in its slot) renders a frame that looks like a vault bug.
    """
    res = _STATIC / name
    if not res.is_file():
        raise RuntimeError(
            f"graph_export: vendored {name} is missing from silica/ui/web/static/ "
            "This is a packaging bug. Reinstall silica or re-vendor the assets (pinned "
            "3d-force-graph@1.80.0, force-graph@1.51.2)."
        )
    return res.read_text(encoding="utf-8")


def _json_island(payload: dict) -> str:
    """The document's data as the body of a <script type="application/json">.

    `<` becomes \\u003c, which is still JSON and the same string again after
    JSON.parse. Every `<`, not only `</`: the parser ends script data at the
    first `</script`, but a `<!--` followed by a `<script` inside the data
    moves it to the double-escaped state, where the real `</script>` no
    longer ends the element. One character closes both holes.

    allow_nan=False because NaN is not JSON: a non-finite score would reach
    JSON.parse as a syntax error and a blank frame, and this names the defect
    on the server side instead, where the log and the /graph error can show it.
    """
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")


# The atlas draws the areas once one of them holds this many notes; under it
# the vault is link-free for the atlas's purpose (converted PDFs, a folder per
# client) and the atlas draws the semantic zones instead, saying so (ADR-0136,
# Open 4, the owner's "ok" 2026-10-28). Two notes are a link, not a country.
ATLAS_MIN_AREA = 3


def _frame_context(
    nodes: list[dict],
    edges: list[dict],
    communities: "list[Community]",
    title: str,
    lib_js: str,
    discourse: str,
    zones: "list[Zone]",
    covers: dict | None = None,
    subs: "list[SubArea]" = (),  # type: ignore[assignment]
) -> dict:
    """Every value graph-frame.html asks for, under the name it asks for it by.

    A dict rather than locals(): a test holds its keys and the template's
    identifiers equal in both directions, so a slot nobody fills and a value
    nobody reads both fail in the suite, not as a blank in the sidebar.
    """
    # The title is a tool argument the model chooses, so it is untrusted markup
    # in both slots it lands in (<title> and the sidebar <h1>).
    title = html.escape(title)

    # A missing paper rides the payload (ADR-0055) and is an id, not a file:
    # every count below reads the notes without it. An
    # outside paper (ADR-0066) is the same kind of node, a paper with no file
    # in this vault, and stays out of them for the same reason.
    missing = {n["id"] for n in nodes if n.get("type") == "missing"}
    outside = {n["id"] for n in nodes if n.get("type") == "outside"}
    nodes_all, nodes = nodes, [n for n in nodes if n["id"] not in missing | outside]
    n_notes      = sum(1 for n in nodes if n.get("type") != "ghost")
    n_ghost      = sum(1 for n in nodes if n.get("type") == "ghost")
    # A written requires is drawn and switched as its own row (ADR-0145), so
    # "resolved" counts the plain wikilinks it no longer draws as.
    n_requires   = sum(1 for e in edges if e.get("type") == "EXTRACTED" and e.get("requires"))
    n_extracted  = sum(1 for e in edges if e.get("type") == "EXTRACTED") - n_requires
    n_ambiguous  = sum(1 for e in edges if e.get("type") == "AMBIGUOUS")
    n_gaps       = sum(1 for e in edges if e.get("type") == "GAP")
    n_similar    = sum(1 for e in edges if e.get("type") == "SIMILAR")
    n_proposed   = sum(1 for e in edges if e.get("type") == "PROPOSED")
    n_coupled    = sum(1 for e in edges if e.get("type") == "COUPLED")
    n_cut        = sum(1 for n in nodes if n.get("cut"))
    n_communities = len(communities)
    # Semantic-map edges: only surface the row when present (the links view
    # have none, so the row would just read 0 and confuse).
    similar_row = (
        f'<label class="filter-row" style="margin-top:4px" title="Embedding k-NN: notes pulled together by semantic similarity">'
        f'<input type="checkbox" id="cb-similar" checked onchange="updateEdgeFilter()">'
        f'<div class="dot-edge" style="--c:{_EDGE_COLOR_SIMILAR};--cp:{_EDGE_COLOR_SIMILAR_PAPER}"></div>similar'
        f'<span class="ct">{n_similar}</span>'
        f'</label>'
    ) if n_similar else ""

    # Three rows that only exist when their layer does. Each states the rule it
    # obeys in the tooltip, because none of the three is a thing the reader
    # wrote: they are what the vault INFERS, and a layer you did not author has
    # to say where it came from before it is worth ticking.
    proposed_row = (
        f'<label class="filter-row" style="margin-top:4px" title="Pairs two hops apart that share '
        f'neighbours (Adamic-Adar) AND share a concept in the co-occurrence index. Corroborated '
        f'only: on its own this signal lost every golden pair it was tested on, so an uncorroborated '
        f'proposal is not drawn.">'
        f'<input type="checkbox" id="cb-proposed" onchange="updateEdgeFilter()">'
        f'<div class="dot-edge dashed" style="--c:{_EDGE_COLOR_PROPOSED};--cp:{_EDGE_COLOR_PROPOSED_PAPER}"></div>'
        f'proposed links'
        f'<span class="ct">{n_proposed}</span>'
        f'</label>'
    ) if n_proposed else ""
    coupled_row = (
        f'<label class="filter-row" style="margin-top:4px" title="Notes written from the same source '
        f'or in the same nucleate run, that never came to link each other. Provenance, not topology.">'
        f'<input type="checkbox" id="cb-coupled" onchange="updateEdgeFilter()">'
        f'<div class="dot-edge dashed" style="--c:{_EDGE_COLOR_COUPLED};--cp:{_EDGE_COLOR_COUPLED_PAPER}"></div>'
        f'coupled'
        f'<span class="ct">{n_coupled}</span>'
        f'</label>'
    ) if n_coupled else ""
    # The citation pair (ADR-0055). Cites is ON, unlike the three inferred rows
    # above: the citation is in the paper's own reference list, so it is a
    # relation that holds, read and not guessed. It exists only where a paper
    # does, which is what "on by default in a Zotero vault" comes to. The
    # missing papers are the opposite case, ids no file carries, so they stay
    # off until asked for and take their citations with them. The count is the
    # lines between library papers; the arrows into missing papers belong to
    # the second row.
    # On by default, like cites: the person wrote it, so it holds. Chevrons
    # point at the prerequisite (2D); the swatch draws them the same way.
    requires_row = (
        '<label class="filter-row" style="margin-top:4px" title="A note\'s requires: in its '
        'frontmatter, accepted by you. The chevrons point at the note to read first.">'
        '<input type="checkbox" id="cb-requires" checked onchange="updateEdgeFilter()">'
        '<svg class="dot-edge req" width="24" height="9" viewBox="0 0 24 9" aria-hidden="true" '
        'shape-rendering="crispEdges"><rect x="0" y="4" width="24" height="1" fill="currentColor"/>'
        '<path d="M6 1 L9 4 L6 7 M15 1 L18 4 L15 7" stroke="currentColor" stroke-width="1" fill="none"/></svg>'
        'requires'
        f'<span class="ct">{n_requires}</span>'
        '</label>'
    ) if n_requires else ""
    n_papers = sum(1 for n in nodes if n.get("type") == "paper")
    n_cites = sum(1 for e in edges if e.get("type") == "CITES" and e.get("to") not in missing)
    cites_row = (
        f'<label class="filter-row" style="margin-top:4px" title="A paper of the library citing '
        f'another, read from the DOI and arXiv ids in its reference list. Derived at every build '
        f'and never written into a note; the arrow points at the cited paper.">'
        f'<input type="checkbox" id="cb-cites" checked onchange="updateEdgeFilter()">'
        f'<div class="dot-edge cites" style="--c:{_EDGE_COLOR_EXTRACTED};--cp:{_EDGE_COLOR_EXTRACTED_PAPER}"></div>'
        f'cites'
        f'<span class="ct">{n_cites}</span>'
        f'</label>'
    ) if n_papers else ""
    missing_row = ""
    if missing:
        from silica.sources.citations import MISSING_MIN

        missing_row = (
            f'<label class="filter-row" style="margin-top:4px" title="Ids cited by at least '
            f'{MISSING_MIN} library papers and absent from the library. Not in the vault, so off '
            f'until asked for. Click one for the reference as a citing paper wrote it; adding it '
            f'is a Zotero action, Add Item by Identifier.">'
            f'<input type="checkbox" id="cb-missing" onchange="updateEdgeFilter()">'
            f'<span class="sq dashed"></span>'
            f'missing papers'
            f'<span class="ct">{len(missing)}</span>'
            f'</label>'
        )
    # The rim's key (ADR-0066 decision 5): rows with no switch, because a
    # collection is a scope and never a layer. Hidden until a collection is
    # scoped, and counted by the frame, which is the one that knows the scope.
    # Only where the library has a paper outside this vault to draw.
    covers_row = (
        '<div class="filter-row" id="covers-row" style="display:none;margin-top:4px" '
        'title="A note among the paper\'s 5 nearest notes, which the papers of its collection '
        'meet more often than the rest of the library does, beyond chance. Inferred by Silica, '
        'so screened in 2D and dashed in 3D (ADR-0146); no direction, so no arrowhead.">'
        '<div class="dot-edge dashed" style="--c:var(--accent);--cp:var(--accent)"></div>'
        '<span id="covers-name">covers</span><span class="ct" id="ct-covers"></span>'
        '</div>'
    ) if outside else ""
    rim_rows = (
        '<div class="filter-row" id="rim-row" style="display:none" title="A paper of the scoped '
        'collections with no leaf in this vault, on its collection\'s arc around the notes. '
        'Click one for its covering notes and their k/K.">'
        '<span class="sq rim"></span>paper on the rim<span class="ct" id="ct-rim"></span>'
        '</div>'
        '<div class="filter-row" id="uncovered-row" style="display:none" title="No note of this '
        'vault passes the test for this paper: no evidence of a relation, which is not proof '
        'of its absence.">'
        '<span class="sq rim none"></span>not covered<span class="ct" id="ct-uncovered"></span>'
        '</div>'
    ) if outside else ""

    # The fragility reading rides the Nodes section, not Edges: coreness and cut
    # vertices are facts about a note. 2D only, like the other two rings - in 3D
    # a ring means rebuilding every node's geometry.
    #
    # No checkbox, unlike the three rows above: this ring is always drawn. Those
    # three are claims about what you did NOT write, so each has to be asked for;
    # a cut vertex is a fact about the graph in front of you, on the same footing
    # as hub and orphan, which have no checkbox either. The row is here to say
    # what the amber ring means, and it aligns with its two neighbours because it
    # carries the same swatch-then-word shape they do.
    cut_row = (
        f'<div class="filter-row" title="Removing this note disconnects the graph: every path '
        f'between the notes it strands and the rest goes through it. Always ringed in 2D and '
        f'named on hover in 3D; the Work panel says what each one carries.">'
        f'<span class="ring" style="border-color:var(--warn)"></span>load-bearing'
        f'<span class="ct">{n_cut}</span>'
        f'</div>'
    ) if n_cut else ""

    # The triangle's key (ADR-0062). Outside #state-legend, which 3D hides:
    # the shape is drawn in both modes, unlike the rings.
    n_daily = sum(1 for n in nodes if n.get("type") == "daily")
    daily_row = (
        f'<div class="filter-row" style="margin-top:4px" title="The note Obsidian opens as that '
        f'day\'s note, read from its Daily notes settings. Related by its blocks, one task or '
        f'paragraph at a time, never by the template every day shares.">'
        f'<span class="tri"></span>daily notes'
        f'<span class="ct">{n_daily}</span>'
        f'</div>'
    ) if n_daily else ""
    # The diamond's key (ADR-0070), on the triangle's terms: both modes draw it.
    n_native = sum(1 for n in nodes if n.get("type") in NATIVE_TYPES)
    native_row = (
        f'<div class="filter-row" style="margin-top:4px" title="A note a Silica feature runs '
        f'on: a Memorize plan (type: plan) or an event note (event_start). Drawn with its links, '
        f'and none of them is counted: no area, size, hub or orphan moves because of one.">'
        f'<span class="dia"></span>Silica notes'
        f'<span class="ct">{n_native}</span>'
        f'</div>'
    ) if n_native else ""

    discourse_badge = (
        f'<div style="font-size:11px;color:var(--ash);letter-spacing:.04em" '
        f'title="Shape of the wikilink graph: how much of the vault sits in the largest connected '
        f'component and how evenly the clusters split it.">'
        f'discourse: <span style="color:var(--warn);font-weight:600">{html.escape(discourse)}</span></div>'
        if discourse else ""
    )

    # The gap list used to live here, under Edge types. It is a vault-level
    # worklist, not a key to what the canvas is painting, and in a legend it read
    # as neither. It now sits on the vault-level surface that already measured it
    # -- the Structural gaps card in metrics -- where each row carries the
    # bridging action. The amber GAP overlay and its checkbox stay: that IS a key.
    #
    # The one list of areas (ADR-0058). Named by the hub note, the name the
    # Clusters tab and the scope picker use; the two c-TF-IDF topics ride the
    # tooltip, and stay the zones' names, so an area and a zone can no longer
    # share one ("variabile · statistica" was both, over 113 and 106 notes).
    # It counts what the matrix counts: a one-note area is a row of zeroes
    # there and a row of noise here, so the 81 of them are one trailing row.
    # The bar is the rail list's reason to exist, the shape of the distribution.
    from silica.kernel.graph.graph_export import area_hubs, area_tally

    label_by_id = {n["id"]: n.get("label") or n["id"].rsplit("/", 1)[-1] for n in nodes}
    areas = sorted((c for c in communities if c.size > 1), key=lambda c: (-c.size, c.id))
    # Only the areas the list names: an area of one note named after its hub
    # is the note's own name again, which is what the frame says nothing for.
    listed = {c.id for c in areas}
    tally = area_tally(nodes, edges)
    hub_name = {g: label_by_id.get(h, h) for g, h in area_hubs(tally).items() if g in listed}
    n_single = sum(1 for c in communities if c.size == 1)
    top = areas[0].size if areas else 1

    def area_row(c) -> str:
        name = html.escape(hub_name.get(c.id) or c.label)
        return (
            f'<button type="button" class="legend-item" data-community="{c.id}" data-size="{c.size}" '
            f'style="--c:{c.color};--cp:{c.color_paper or c.color}" '
            f'title="{name} · topics: {html.escape(c.label)}" onclick="scopeArea({c.id})">'
            f'<span class="dot"></span><span class="an">{name}</span><span class="ct">{c.size}</span>'
            f'<span class="abar"><i style="width:{max(4, round(100 * c.size / top))}%"></i></span>'
            f'</button>\n'
        )

    legend_items = "".join(area_row(c) for c in areas) + (
        f'<div class="legend-item rest" title="Notes Louvain left in an area of their own. '
        f'Left out of the list as they are left out of the areas matrix: each would be a row '
        f'of one.">single-note areas<span class="ct">{n_single}</span></div>\n'
        if n_single else "")

    # The atlas (ADR-0136) is the one row that replaced two, the area blobs and
    # the semantic zones: every note owns the ground nearest it, an area's
    # ground is a country, a sub-area's a province. It draws the areas, or the
    # zones where no area reaches ATLAS_MIN_AREA notes, and the row says which
    # (ADR-0023: a surface names the partition it shows). On in 2D unless the
    # reader turned it off; the frame hides the row in 3D, which has no atlas.
    atlas_by = "zones" if zones else ("areas" if areas else "")
    atlas_tip = (
        "No area holds 3 notes or more, so the countries are the semantic zones: Louvain over "
        "the embedding k-NN, notes grouped by what they are about. Every note owns the ground "
        "nearest to it. Links show for the note under the pointer or in focus. 2D only."
        if zones else
        "Every note owns the ground nearest to it: an area's ground is a country, a sub-area's "
        "a province. Zoom in for the sub-areas, then the notes. Links show for the note under "
        "the pointer or in focus. 2D only."
    )
    atlas_row = (
        f'<label class="filter-row" id="atlas-row" style="margin-top:4px" title="{atlas_tip}">'
        f'<input type="checkbox" id="cb-atlas" checked onchange="updateAtlas()">'
        f'<span class="dot all"></span>atlas'
        + (f' · semantic zones<span class="ct">{len(zones)}</span>' if zones else "")
        + '</label>'
    ) if atlas_by else ""

    tree_html = file_tree.frame_tree(nodes)

    # Two display settings, baked in at render time rather than read from the
    # frame: /graph regenerates the whole document per request anyway, so there
    # is nothing here for a live toggle to be live *against*.
    from silica.config import CONFIG

    data = {
        "nodes": nodes_all,
        "edges": edges,
        "comm_labels": {c.id: c.label for c in communities},
        # The area's hub note, the name /shape's matrix and pair detail use. The
        # bridge tip named an area by its c-TF-IDF label ("python · stringa")
        # where the detail said "Python"; the legend list keeps the label as a
        # description of the colour, the tip names the area (ADR-0045). Its
        # keys are the listed areas, multi-note only: the countries the 2D atlas
        # names (ADR-0136).
        "comm_hubs": hub_name,
        # What the atlas draws: "areas" (node.group, provinces node.sub), or
        # "zones" (node.sgroup) on a vault with no area of ATLAS_MIN_AREA notes,
        # or "" with neither. One partition per document, never both (ADR-0023).
        "atlas": atlas_by,
        # The provinces, named by hub_of's rule over each one's notes. A
        # one-note sub-area is its note, and a province is drawn from two.
        "subs": [
            {"id": s.id, "area": s.area, "name": label_by_id.get(s.hub, s.hub), "size": s.size}
            for s in subs if s.size > 1
        ],
        # The semantic partition rides as its own list (ADR-0023), and only on
        # the vault whose atlas draws it: the JS keys it by node.sgroup, never
        # by node.group.
        "zones": [
            {"id": z.id, "label": z.label, "color": z.color,
             "color_paper": z.color_paper or z.color, "size": z.size}
            for z in zones
        ],
        # The last COVERS pass (ADR-0066 decision 6): none, fresh or stale,
        # and how many papers it searched. The frame passes it to the app,
        # which starts a pass on the first collection scope that needs one.
        "covers": covers or {"state": "none"},
        "particles": bool(CONFIG.graph_particles),
        "shading": bool(CONFIG.graph_shading),
        # The same constants paint the legend swatches below, so the JS reads
        # the edge colours from here and not from literals of its own.
        "colors": {
            "extracted": _EDGE_COLOR_EXTRACTED,
            "extracted_paper": _EDGE_COLOR_EXTRACTED_PAPER,
            "similar_particle": _EDGE_COLOR_SIMILAR_PARTICLE,
            "similar_particle_paper": _EDGE_COLOR_SIMILAR_PARTICLE_PAPER,
            "gap": _EDGE_COLOR_GAP,
            "gap_paper": _EDGE_COLOR_GAP_PAPER,
        },
    }

    return {
        "title": title,
        "lib_js": lib_js,
        "font_face": _vendored_font_face(),
        "graph_css": _asset("graph-frame.css"),
        "n_notes": n_notes,
        "n_ghost": n_ghost,
        "n_extracted": n_extracted,
        "n_ambiguous": n_ambiguous,
        "n_gaps": n_gaps,
        "n_communities": n_communities,
        "n_areas": len(areas),
        "color_extracted": _EDGE_COLOR_EXTRACTED,
        "color_extracted_paper": _EDGE_COLOR_EXTRACTED_PAPER,
        "color_ambiguous": _EDGE_COLOR_AMBIGUOUS,
        "color_ambiguous_paper": _EDGE_COLOR_AMBIGUOUS_PAPER,
        "color_gap": _EDGE_COLOR_GAP,
        "color_gap_paper": _EDGE_COLOR_GAP_PAPER,
        "tree_html": tree_html,
        "similar_row": similar_row,
        "proposed_row": proposed_row,
        "coupled_row": coupled_row,
        "cites_row": cites_row,
        "requires_row": requires_row,
        "missing_row": missing_row,
        "covers_row": covers_row,
        "rim_rows": rim_rows,
        "cut_row": cut_row,
        "daily_row": daily_row,
        "native_row": native_row,
        "discourse_badge": discourse_badge,
        "legend_items": legend_items,
        "atlas_row": atlas_row,
        "graph_data": _json_island(data),
        # The atlas's Delaunay first, which the frame calls: one script
        # element, so no URL is involved (ADR-0026).
        "graph_js": _asset("d3-delaunay.min.js") + "\n" + _asset("graph-frame.js"),
        "sim_js": _sim_js(),
    }


def render_html(
    nodes: list[dict],
    edges: list[dict],
    communities: "list[Community]" = (),  # type: ignore[assignment]
    title: str = "Vault Graph",
    lib_js: str = "",
    discourse: str = "",
    zones: "list[Zone]" = (),  # type: ignore[assignment]
    covers: dict | None = None,
    subs: "list[SubArea]" = (),  # type: ignore[assignment]
) -> str:
    """Produce a fully self-contained force-graph HTML string.

    lib_js is the vendored renderer bundle, embedded inline (offline-capable;
    export_graph always supplies it via _vendored_lib_js).
    communities is a list of Community objects; legend is built from it.
    zones is the semantic partition (node["sgroup"]), passed only where the
    atlas draws it in place of the areas (ATLAS_MIN_AREA); subs are the areas'
    provinces (detect_sub_areas, node["sub"]).

    string.Template and not an f-string: the skeleton is a file the browser
    and a linter read as HTML, and its `$name` slots need no brace doubling,
    which is what had every `{` in 2,200 lines of CSS and JS written twice.
    substitute() is the strict form, so a slot with no value is a KeyError at
    render and never an empty sidebar.
    """
    ctx = _frame_context(nodes, edges, communities, title, lib_js, discourse, zones, covers, subs)
    return string.Template(_asset("graph-frame.html")).substitute(ctx)


# The similarity particles, pre-dimmed rather than made transparent. There are
# five gap links and roughly two thousand similar ones, so the same particle
# treatment would drown the frame; the effect has to survive at a fortieth of
# the weight. Alpha is not available to do that dimming: 3d-force-graph builds
# each photon as a mesh whose material takes a THREE.Color, which discards the
# alpha channel outright. So the colour is blended against the void here, once,
# and both renderers get an opaque colour that already looks faint.
#
# It is derived UPWARD from the line, not downward. The line is _EDGE_COLOR_SIMILAR
# at opacity 0.35, which over --void lands at about #08405E; a particle at or
# below that is a moving dot you cannot see. This sits a step above it — the same
# azure blended at 0.65 — which is the whole budget the effect gets: enough that
# the drift registers, not enough that two thousand of them become the subject.
_EDGE_COLOR_SIMILAR_PARTICLE = "#056E9A"  # one step up from the line's apparent #08405E

# The paper pair. Same derivation, opposite direction: a photon that cannot use
# alpha has to be blended against the floor it will sit on, and on paper that
# blend goes toward white. The similar particle is _EDGE_COLOR_SIMILAR_PAPER
# blended one step DOWN from its line's apparent value, because on a light floor
# a faint dot is a dark one — the reverse of the crystal case above and the
# reason this is a second constant rather than the same value reused.
_EDGE_COLOR_SIMILAR_PARTICLE_PAPER = "#5CA8C4"

def _stamp_collections(nodes: list[dict]) -> None:
    """Zotero collection names onto the paper nodes, for the scope picker's
    Collections group (ADR-0055 "Scoping").

    Read live at every build and never stored, because Silica's own filing
    moves items between collections and a copy in the vault would go stale
    (decision 10). Zotero closed, its API off or another library answering is
    the ordinary case of a laptop where Zotero is not open: the papers carry
    no membership and the group is simply absent, while the graph itself does
    not depend on Zotero at all.
    """
    if not any(n.get("type") == "paper" for n in nodes):
        return
    import httpx

    from silica.sources import zotero

    try:
        member = zotero.memberships(zotero.client())
    except (zotero.ZoteroError, httpx.HTTPError) as exc:
        logger.info("graph_view: no Zotero collections for this build (%s)", exc)
        return
    for n in nodes:
        paper = n.get("paper")
        if paper and paper.get("zotero_key") in member:
            paper["collections"] = sorted(member[paper["zotero_key"]])


def _zotero_layer(nodes: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """The library as this frame draws it (ADR-0066): collection names stamped
    on the paper leaves, one `outside` node per paper of a collection with no
    leaf in this vault, the `COVERS` edges of the last pass, and that pass's
    state. One read of the library serves all four.

    Every vault reads the library (decision 1), so this asks Zotero whether or
    not the vault is linked. Zotero closed, its API off or another library
    answering is the ordinary case of a laptop where Zotero is not open: the
    papers carry no membership, nothing is outside, and the graph itself does
    not depend on Zotero at all.
    """
    import httpx

    from silica.config import CONFIG
    from silica.kernel.recall.sync import vault_version
    from silica.sources import zotero, zotero_covers

    try:
        lib = zotero_covers.library(zotero.client())
    except (zotero.ZoteroError, httpx.HTTPError) as exc:
        logger.info("graph_view: no Zotero library for this build (%s)", exc)
        return [], [], {"state": "none"}
    by_key = {p["key"]: p for p in lib["papers"]}
    held = set()
    for n in nodes:
        paper = n.get("paper")
        if paper and paper.get("zotero_key") in by_key:
            paper["collections"] = by_key[paper["zotero_key"]]["collections"]
            held.add(paper["zotero_key"])
    vault = (CONFIG.vault_path or "").strip()
    cache = zotero_covers.cached(vault)
    state = zotero_covers.freshness(cache, lib["version"], vault_version())
    covers = (cache or {}).get("covers", {}) if state != "none" else {}
    ids = {n["id"] for n in nodes}
    outside: list[dict] = []
    edges: list[dict] = []
    for p in lib["papers"]:
        # A paper in no collection can never be scoped, so it is never drawn;
        # a paper with a leaf here is a file of this vault and stays one.
        if not p["collections"] or p["key"] in held:
            continue
        nid = f"zotero:{p['key']}"
        rows = [r for r in covers.get(p["key"], []) if r[0] in ids]
        outside.append({
            "id": nid, "type": "outside", "path": "", "label": p["label"], "group": -1,
            "size": 16, "covers": rows, "covered": bool(rows) if state != "none" else None,
            "paper": {"zotero_key": p["key"], "title": p["title"],
                      "collections": p["collections"]},
        })
        for note in dict.fromkeys(r[0] for r in rows):
            edges.append({"from": nid, "to": note, "type": "COVERS", "width": 1.1,
                          "color": {"opacity": 1.0}})
    meta = {"state": state, "papers": (cache or {}).get("papers", 0),
            "at": (cache or {}).get("at", "")}
    return outside, edges, meta


def export_graph(
    output_path: str,
    folder: str = "",
    title: str = "Vault Graph",
    knn_k: int = 6,
) -> dict:
    """Build and write the unified vault-graph HTML to output_path.

    One build, two edge layers on a shared force layout:
      - the wikilink graph (EXTRACTED/AMBIGUOUS) — the explicit structure;
      - the embedding k-NN overlay (SIMILAR) — meaning-space proximity.
    Communities are Louvain on the WIKILINKS; the SIMILAR layer is a toggleable
    HUD overlay whose forces pull link-orphans (e.g. book extracts with no
    wikilinks) next to their semantic neighbours instead of leaving them
    floating. Structural-gap particles ride the wikilink layer.

    Reads the vendored JS first (fail fast on a packaging bug) and always inlines
    it, so the emitted file is self-contained/offline. Returns dict with keys:
    success, path, nodes, edges (wikilinks), similar (k-NN), communities,
    unresolved, gaps.
    """
    from silica.kernel.graph.graph_export import (
        build_graph_data,
        canvas_metrics,
        backbone_links,
        detect_communities,
        detect_semantic_partition,
        detect_sub_areas,
        discourse_shape,
        edge_graph,
        knn_edges,
    )

    lib_js = _vendored_lib_js()  # fail fast before the graph build
    nodes, edges = build_graph_data(folder=folder)   # wikilink edges (the structure)
    # Paper leaves become `paper` nodes, and their citations a derived layer
    # (ADR-0055 decision 6). The missing papers join the payload only at render,
    # after every partition and count below: they are ids, not notes.
    cites: list[dict] = []
    missing: list[dict] = []
    try:
        from silica.sources.citations import paper_layer
        cites, missing = paper_layer(nodes)
    except Exception as exc:
        logger.warning("graph_view: citation layer skipped (%s)", exc)
    # The outside papers join the payload at render too, after the missing
    # ones and for their reason (ADR-0066 decision 2): no area, zone or size
    # may move because a library was read.
    outside, covers, covers_meta = _zotero_layer(nodes)
    sim = knn_edges(nodes, k=knn_k)                  # embedding k-NN overlay
    communities = detect_communities(nodes, edges)   # Louvain on the wikilinks
    from silica.kernel.graph.daily_layer import neutral_lone_dailies

    neutral_lone_dailies(nodes)   # a day alone in its community is not a hue
    # The provinces of the atlas, and the ties the layout pulls on (ADR-0136).
    subs = detect_sub_areas(nodes, edges)
    n_backbone = backbone_links(nodes, edges)
    # The second partition only where the atlas draws it, a vault with no area
    # of ATLAS_MIN_AREA notes: since ADR-0136 the graph reads it nowhere else.
    # Louvain on the k-NN, writing node["sgroup"] only, so the structural
    # colours above stand.
    linked = any(c.size >= ATLAS_MIN_AREA for c in communities)
    zones = [] if linked else detect_semantic_partition(nodes, sim)

    # Betweenness for the hub ring + discourse-shape badge, from one shared nx
    # build over the wikilinks.
    bet, giant = canvas_metrics(nodes, edges)
    if bet:
        for n in nodes:
            if n.get("type") != "ghost":
                n["betweenness"] = round(bet.get(n["id"], 0.0), 4)
    discourse = discourse_shape(
        sum(1 for n in nodes if n.get("type") != "ghost"),
        giant, [c.size for c in communities],
    )

    G_und = edge_graph(nodes, edges)
    overlays.size_by_links(nodes, G_und)
    overlays.stamp_load_bearing(nodes, G_und, bet)

    # Gap particles ride the wikilink layer (they answer a linking question).
    gaps = overlays.gap_edges(nodes, edges)
    # The two note-level proposal layers. Both are best-effort: a vault with no
    # co-occurrence index gets no layer and the same graph it always got, which
    # is why neither is allowed to raise into the export.
    proposed: list[dict] = []
    coupled: list[dict] = []
    try:
        proposed = overlays.proposed_edges(G_und, nodes)
    except Exception as exc:
        logger.warning("graph_view: proposed layer skipped (%s)", exc)
    try:
        coupled = overlays.coupled_edges(G_und, nodes)
    except Exception as exc:
        logger.warning("graph_view: coupled layer skipped (%s)", exc)

    html_out = render_html(
        nodes + missing + outside, edges + sim + gaps + proposed + coupled + cites + covers,
        communities, title=title, lib_js=lib_js, discourse=discourse, zones=zones,
        covers=covers_meta, subs=subs,
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_out, encoding="utf-8")

    n_notes       = sum(1 for n in nodes if n.get("type") != "ghost")
    n_ghost       = sum(1 for n in nodes if n.get("type") == "ghost")
    n_links       = sum(1 for e in edges if e.get("type") == "EXTRACTED")
    n_similar     = len(sim)
    n_communities = len(communities)

    logger.info(
        "graph_export: wrote %s — %d notes, %d links (%d pairs on the backbone), %d similar, "
        "%d clusters, %d unresolved, atlas over the %s",
        out, n_notes, n_links, n_backbone, n_similar, n_communities, n_ghost,
        "areas" if linked else "zones",
    )
    return {
        "success":     True,
        "path":        str(out.resolve()),
        "nodes":       n_notes,
        "edges":       n_links,
        "similar":     n_similar,
        # Two counts, two names: `communities` is the structural partition,
        # `zones` the semantic one, counted only where the atlas draws it
        # (ATLAS_MIN_AREA) and None elsewhere. Never summed, never swapped
        # (ADR-0023).
        "communities": n_communities,
        "zones":       None if linked else len(zones),
        "sub_areas":   sum(1 for s in subs if s.size > 1),
        "backbone":    n_backbone,
        "unresolved":  n_ghost,
        "gaps":        len(gaps),
    }
