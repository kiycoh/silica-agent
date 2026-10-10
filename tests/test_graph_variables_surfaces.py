# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The seven variables where a reader meets them.

The kernel side is pinned by tests/test_graph_variables.py. This file pins the
SURFACES: the per-note facade behind the Work panel, the reading-order pane,
the three graph layers, the metrics rows and the calendar strip. One rule runs
through all of them and is asserted rather than assumed: a variable that failed
its gate (docs/adr/0027) is allowed to state what it measured and is never
allowed to accuse a note.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import networkx as nx
import pytest

from silica.kernel.report import structure

WEB = Path(__file__).resolve().parent.parent / "silica" / "ui" / "web"
from tests.webassets import app_css, app_js
WORK_JS = WEB / "static" / "work.js"
FRAME_JS = WEB / "static" / "graph-frame.js"
FRAME_HTML = WEB / "static" / "graph-frame.html"


# ---------------------------------------------------------------------------
# the facade: cut components, and the ladder's rungs
# ---------------------------------------------------------------------------

def test_cut_component_is_measured_inside_its_own_component():
    """A vault is already in pieces before anything is removed. Counting every
    piece as "stranded by this note" charged the first cut vertex on a 709-note
    vault with 159 notes it had nothing to do with; the honest number is what
    falls off ITS component, against the largest piece the removal leaves."""
    G = nx.Graph()
    G.add_edges_from([("a", "cut"), ("cut", "b"), ("b", "c")])   # cut splits 1 | 2
    G.add_edges_from([("x", "y"), ("y", "z")])                   # a separate island
    sizes = structure.cut_component_sizes(G, {"cut", "b"})
    assert sizes["cut"] == 1     # 'a' alone: the {b,c} side is the larger piece
    assert sizes["b"] == 1       # 'c' alone
    assert "x" not in sizes      # the island was already an island


def test_cut_component_size_is_zero_for_a_node_that_is_not_a_cut_vertex():
    G = nx.Graph([("a", "b"), ("b", "c"), ("c", "a")])
    assert structure.cut_component_sizes(G, set()) == {}


def test_the_count_and_the_names_of_a_cut_come_from_one_split():
    """The drawer prints "Only bridge for N notes" and names the N under it
    (ADR-0075). Two computations of one split are two answers that drift."""
    G = nx.Graph([("a", "cut"), ("cut", "b"), ("b", "c"), ("cut", "d")])
    pieces = structure.cut_pieces(G, "cut")
    assert pieces[0] == {"b", "c"}                      # the rest: the largest piece
    assert sorted(sorted(p) for p in pieces[1:]) == [["a"], ["d"]]
    assert structure.cut_component_sizes(G, {"cut"})["cut"] == sum(len(p) for p in pieces[1:])


def test_note_structure_carries_the_scales_and_names_what_a_bridge_strands(monkeypatch):
    """A - CUT - B, and B in a triangle with C and B2: CUT strands A alone."""
    G = nx.Graph([("A.md", "CUT.md"), ("CUT.md", "B.md"), ("B.md", "C.md"),
                  ("C.md", "B2.md"), ("B.md", "B2.md")])
    monkeypatch.setattr("silica.kernel.recall.graph_export.wikilink_graph_cached", lambda folder="": G)
    monkeypatch.setattr("silica.kernel.report.learner.prerequisites_map", lambda: {})
    m = structure.build()
    monkeypatch.setattr(structure, "structure_map", lambda: m)
    st = structure.note_structure("CUT.md")
    assert st["articulation"] is True and st["strands"] == 1
    assert st["stranded"] == [["A.md"]] and st["component"] == 5
    # depth 1 of the vault's 2 (the triangle), and every note reaches 1
    assert (st["coreness"], st["core_max"], st["core_reach"], st["notes"]) == (1, 2, 5, 5)
    # the two ranks surprise is the difference of, not a second ranking
    assert round(st["betweenness_rank"] - st["degree_rank"], 4) == st["surprise"]
    assert "stranded" not in structure.note_structure("C.md")    # not a cut vertex


def test_a_cut_is_laid_out_with_its_two_sides_apart():
    """Place draws the cut as a line through the note (ADR-0075): every note it
    strands to the right of it, its side of the vault to the left, and the
    links that leave the drawing counted as stubs."""
    # v strands a chain a1-a2-a3 and a lone b; the vault's side is a star, so
    # v sits in its middle, the case one layout over both sides got wrong.
    G = nx.Graph([("v", "a1"), ("a1", "a2"), ("a2", "a3"), ("v", "b"),
                  ("v", "r1"), ("v", "r2"), ("v", "r3"), ("r1", "f1"), ("r1", "f2"), ("r2", "f3"),
                  ("f1", "x"), ("f2", "x"), ("f3", "x"), ("r3", "f3")])
    pieces = structure.cut_pieces(G, "v")
    out = structure.cut_layout(G, "v", pieces, dict(G.degree()))
    at = {n["id"]: n for n in out["nodes"]}
    assert (at["v"]["x"], at["v"]["y"], at["v"]["side"]) == (0.0, 0.0, "note")
    assert {n for n, a in at.items() if a["side"] == "behind"} == {"a1", "a2", "a3", "b"}
    assert all(a["x"] > 0 for a in at.values() if a["side"] == "behind")
    assert all(a["x"] < 0 for a in at.values() if a["side"] == "rest")
    assert (at["a3"]["hop"], at["b"]["piece"], at["a1"]["piece"]) == (3, 1, 0)
    # x is two steps out, past the drawing: each note linking it shows a stub
    assert "x" not in at and [at[f]["stubs"] for f in ("f1", "f2", "f3")] == [1, 1, 1]
    pts = [(a["x"], a["y"]) for a in at.values()]
    assert min(math.dist(p, q) for i, p in enumerate(pts) for q in pts[i + 1:]) > 0.2
    ids = [n["id"] for n in out["nodes"]]
    assert sorted(tuple(sorted((ids[i], ids[j]))) for i, j in out["edges"]) == \
        sorted(tuple(sorted(e)) for e in G.subgraph(ids).edges())
    assert out == structure.cut_layout(G, "v", pieces, dict(G.degree()))    # same graph, same drawing


def test_two_leaves_of_one_note_are_not_drawn_on_one_point():
    """Same distances to everything, so classical MDS stacks them; the nudge in
    _stress parts them (8 of 127 cut vertices on the test vault)."""
    import numpy as np

    D = np.array([[0, 1, 1, 1], [1, 0, 2, 2], [1, 2, 0, 2], [1, 2, 2, 0]], float)
    X = structure._stress(D)
    assert not np.isnan(X).any()
    assert min(math.dist(X[i], X[j]) for i in range(4) for j in range(i + 1, 4)) > 1.0


def test_a_bridge_ships_its_layout_with_the_areas_the_graph_colours():
    """One Louvain, one seed, one graph: the drawer's blobs are the frame's."""
    from silica.kernel.recall.graph_export import louvain_sorted

    G = nx.Graph([("A.md", "CUT.md"), ("CUT.md", "B.md"), ("B.md", "C.md"),
                  ("C.md", "B2.md"), ("B.md", "B2.md")])
    m = structure.StructureMap(degree=dict(G.degree()))
    import unittest.mock as um
    with um.patch("silica.kernel.recall.graph_export.wikilink_graph_cached", lambda folder="": G):
        out = structure._cut_detail("CUT.md", m)
    area = {n: i for i, c in enumerate(louvain_sorted(G)) for n in c}
    assert {n["id"]: n["area"] for n in out["layout"]["nodes"]} == {n: area[n] for n in G}


def test_note_structure_counts_the_notes_at_each_depth():
    m = structure.StructureMap(core={"a": 0, "b": 1, "c": 1, "d": 2}, degree={"a": 0, "b": 1, "c": 2, "d": 2})
    import unittest.mock as um
    with um.patch.object(structure, "structure_map", lambda: m):
        st = structure.note_structure("c")
    assert st["core_hist"] == [1, 2, 1]
    assert st["core_reach"] == sum(st["core_hist"][st["coreness"]:])


def _map(prereq, degree=None) -> structure.StructureMap:
    """A StructureMap with only the fields the ladder reads."""
    deg = degree or {n + ".md": 1 for n in {k for k in prereq} | {
        p for ps in prereq.values() for p in ps}}
    m = structure.StructureMap(degree=deg, prereq=prereq)
    object.__setattr__(m, "_sizes", {})
    return m


def test_ladder_depth_is_the_longest_path_not_the_hop_count(monkeypatch):
    """A -> B -> C with a shortcut A -> C. By hops, C sits one step from A and
    lands on B's rung; by longest path it is two, which is the reading: you can
    skip B, but B still comes first."""
    monkeypatch.setattr(structure, "structure_map",
                        lambda: _map({"B": ["A"], "C": ["B", "A"]}))
    lad = structure.ladder("A.md")
    depth = {n["path"]: n["depth"] for n in lad["nodes"]}
    assert depth == {"A.md": 0, "B.md": 1, "C.md": 2}
    assert lad["root"] == "A.md"
    assert lad["cycles"] == 0


def test_ladder_places_prerequisites_below_zero_and_dependents_above(monkeypatch):
    monkeypatch.setattr(structure, "structure_map",
                        lambda: _map({"ROOT": ["BASE"], "TIP": ["ROOT"]}))
    lad = structure.ladder("ROOT.md")
    depth = {n["path"]: n["depth"] for n in lad["nodes"]}
    assert depth["BASE.md"] == -1 and depth["ROOT.md"] == 0 and depth["TIP.md"] == 1


def test_ladder_reports_a_cycle_and_does_not_lay_its_edges_out(monkeypatch):
    """RefD can say A needs B, B needs C and C needs A. That is a real
    disagreement in the vault, not corrupt data, so it is counted and the
    edges that close it are left undrawn rather than making the rungs
    unassignable."""
    monkeypatch.setattr(structure, "structure_map",
                        lambda: _map({"A": ["C"], "B": ["A"], "C": ["B"]}))
    lad = structure.ladder("A.md")
    assert lad["cycles"] == 3
    assert lad["edges"] == []           # nothing settled, so nothing is drawn
    assert lad["root"] == "A.md"


def test_ladder_abstains_for_a_note_with_no_direction(monkeypatch):
    m = _map({"B": ["A"]})
    object.__setattr__(m, "degree", {**m.degree, "LONE.md": 3})
    monkeypatch.setattr(structure, "structure_map", lambda: m)
    lad = structure.ladder("LONE.md")
    assert lad["nodes"] == [] and lad["root"] == "LONE.md"


def test_ladder_is_empty_without_a_prerequisite_index(monkeypatch):
    monkeypatch.setattr(structure, "structure_map", lambda: structure.StructureMap())
    assert structure.ladder("anything.md") == {
        "root": "", "nodes": [], "edges": [], "cycles": 0, "truncated": False,
    }


def test_note_structure_is_empty_for_a_note_the_graph_has_never_seen(monkeypatch):
    monkeypatch.setattr(structure, "structure_map", lambda: structure.StructureMap())
    assert structure.note_structure("ghost.md") == {}


# ---------------------------------------------------------------------------
# the graph frame: three layers and one node channel
# ---------------------------------------------------------------------------

def _n(nid, sgroup=-1, ntype="note"):
    return {"id": nid, "label": nid, "type": ntype, "sgroup": sgroup}


def test_load_bearing_is_stamped_on_the_nodes_with_what_a_cut_costs():
    from silica.ui.web.graph_view import _stamp_load_bearing

    nodes = [_n("a"), _n("cut"), _n("b"), _n("c"), _n("G", ntype="ghost")]
    G = nx.Graph([("a", "cut"), ("cut", "b"), ("b", "c")])
    _stamp_load_bearing(nodes, G, {n: 0.0 for n in G})
    by_id = {n["id"]: n for n in nodes}
    assert by_id["cut"]["cut"] is True and by_id["cut"]["strands"] == 1
    assert "cut" not in by_id["a"]           # not a cut vertex: no flag at all
    assert by_id["a"]["coreness"] == 1
    assert "coreness" not in by_id["G"]      # a ghost is not a note


def test_proposed_layer_keeps_only_pairs_a_shared_concept_corroborates(monkeypatch):
    """Uncorroborated, V1 lost every golden pair it was tested on (ADR-0027).
    The layer is allowed to exist because the corroboration is the filter, so
    a pair the index cannot vouch for is not drawn."""
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(
        "silica.kernel.recall.graph_export.shared_concepts",
        lambda pairs, k=3: {("A", "D"): ["gradient"]},
    )
    out = gv._corroborate(
        [("A", "D", 2.0), ("B", "E", 9.0)], [_n("A"), _n("B"), _n("D"), _n("E")], 10,
        kind="PROPOSED", color="#000", paper="#fff",
    )
    assert [(e["from"], e["to"]) for e in out] == [("A", "D")]
    assert out[0]["type"] == "PROPOSED" and out[0]["shared"] == ["gradient"]


def test_proposed_layer_is_empty_without_an_index(monkeypatch):
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(
        "silica.kernel.recall.graph_export.shared_concepts", lambda pairs, k=3: {})
    assert gv._corroborate([("A", "D", 2.0)], [_n("A"), _n("D")], 10,
                           kind="COUPLED", color="#000", paper="#fff") == []


def test_the_frame_hides_a_layer_that_has_nothing_in_it():
    """A checkbox for an empty layer promises something behind it. Each of the
    four rows is rendered only where its own count is non-zero."""
    from silica.ui.web.graph_view import _frame_context

    ctx = _frame_context([_n("A"), _n("B")],
                         [{"id": "e0", "from": "A", "to": "B", "type": "EXTRACTED"}],
                         [], "t", "", "", [])
    assert ctx["proposed_row"] == "" and ctx["coupled_row"] == ""
    assert ctx["cut_row"] == ""


def test_the_frame_renders_each_layer_when_it_has_something(tmp_path):
    from silica.ui.web.graph_view import _frame_context

    nodes = [_n("A", 0), _n("B", 1)]
    nodes[0]["cut"] = True
    nodes[0]["strands"] = 4
    edges = [
        {"id": "e0", "from": "A", "to": "B", "type": "EXTRACTED"},
        {"id": "p0", "from": "A", "to": "B", "type": "PROPOSED"},
        {"id": "c0", "from": "A", "to": "B", "type": "COUPLED"},
    ]
    ctx = _frame_context(nodes, edges, [], "t", "", "", [])
    assert 'id="cb-proposed"' in ctx["proposed_row"]
    assert 'id="cb-coupled"' in ctx["coupled_row"]
    # The load-bearing row is a legend entry, not a control: the ring it names
    # is always drawn, so it carries the swatch and the count and no input.
    assert "load-bearing" in ctx["cut_row"] and ">1<" in ctx["cut_row"]
    assert "<input" not in ctx["cut_row"]


def test_the_three_inferred_layers_are_off_until_asked_for():
    """The frame opens on what you WROTE. Every layer that is a claim about
    what you did not write starts unticked."""
    from silica.ui.web.graph_view import _frame_context

    js = FRAME_JS.read_text()
    for flag in ("showProposed", "showCoupled"):
        assert re.search(rf"let {flag} = false;", js), flag
    nodes = [_n("A", 0), _n("B", 1)]
    nodes[0]["cut"] = True
    ctx = _frame_context(
        nodes,
        [{"id": "e0", "from": "A", "to": "B", "type": "EXTRACTED"},
         {"id": "p0", "from": "A", "to": "B", "type": "PROPOSED"},
         {"id": "c0", "from": "A", "to": "B", "type": "COUPLED"}],
        [], "t", "", "", [],
    )
    rows = {slot: ctx[slot] for slot in ("proposed_row", "coupled_row")}
    for slot, row in rows.items():
        # The <input> tag itself, not the row: a tooltip may say "checked".
        tag = row.split("<input", 1)[1].split(">", 1)[0]
        assert "checked" not in tag, slot


def test_the_load_bearing_ring_sits_outside_the_state_ring():
    """A cut vertex is usually a hub too. One ring would have had the two
    readings overwrite each other, which is why this is a second channel and
    not a fourth nodeState."""
    js = FRAME_JS.read_text()
    draw = js.split("function drawNode(n, ctx, scale) {")[1].split("\n}")[0]
    state_r = draw.index("r + 2 / scale")
    cut_r = draw.index("r + 4.5 / scale")
    assert state_r < cut_r


def test_the_load_bearing_ring_has_no_switch_at_all():
    """It shipped off by default, on the argument that 128 amber rings on a
    709-note vault is an alarm about a normal graph. A reading nobody switches
    on is a reading nobody has, and unlike the three inferred layers this one
    is a fact about the graph in front of you, not a claim about what you did
    not write. So: no flag, no checkbox, no handler, in either renderer."""
    js = FRAME_JS.read_text()
    assert "showCut" not in js and "updateNodeFilter" not in js
    assert 'id="cb-cut"' not in FRAME_HTML.read_text()
    draw = js.split("function drawNode(n, ctx, scale) {")[1].split("\n}")[0]
    assert "if (n.cut) {" in draw
    tip = js.split(".nodeLabel(n => {")[1].split("});")[0]
    assert "const cut = n.cut" in tip, "3D has no ring: the tooltip is the reading"


def test_every_edge_layer_has_a_colour_of_its_own():
    """The zone-crossing highlight once reused the similar layer's azure - the
    same hex - so ticking it changed nothing anyone could see. Every edge
    colour the frame can draw at once has to differ from every other."""
    from silica.kernel.recall import graph_export as ge
    from silica.ui.web import graph_view as gv

    # By NAME first: graph_view re-imports three of graph_export's constants, and
    # a constant seen twice under its own name is one colour, not two.
    by_name: dict[str, str] = {
        name: getattr(mod, name).lower()
        for mod in (ge, gv)
        for name in dir(mod)
        if name.startswith("_EDGE_COLOR_") and not name.endswith("_PAPER")
    }
    seen: dict[str, str] = {}
    for name, hexv in sorted(by_name.items()):
        assert hexv not in seen, f"{name} wears {seen[hexv]}'s colour ({hexv})"
        seen[hexv] = name


# ---------------------------------------------------------------------------
# the Work panel: what each number is allowed to claim
# ---------------------------------------------------------------------------

def _run_projection(tmp_path, call: str, arg):
    """Run one function of work.js's pure block under node, on `arg`."""
    src = WORK_JS.read_text()
    m = re.search(r"// --- projection:begin.*?// --- projection:end", src, re.S)
    assert m, "projection markers not found in work.js"
    script = tmp_path / "run.js"
    script.write_text(
        m.group(0)
        + f"\nconsole.log(JSON.stringify({call}(JSON.parse(process.argv[2]))));\n"
    )
    out = subprocess.run(["node", str(script), json.dumps(arg)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


NODE = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# Backward chaining on the test vault, the case ADR-0075 was measured on.
BRIDGE = {
    "articulation": True, "strands": 13, "component": 576, "coreness": 2,
    "core_max": 4, "core_reach": 399, "notes": 729, "surprise": 25,
    "degree_pct": 71, "betweenness_pct": 96,
    "pieces": [{"size": 12, "head": {"name": "Ragionamento (KRR)", "path": "K/R.md"}},
               {"size": 1, "head": {"name": "Forward chaining", "path": "K/F.md"}}],
    "behind": ["K/R.md", "K/F.md"],
}


@NODE
def test_a_cut_vertex_is_the_one_reading_allowed_to_warn(tmp_path):
    p = _run_projection(tmp_path, "placeReadings", BRIDGE)
    assert p["warn"] is True
    assert p["bridge"]["title"] == "Only bridge for 13 notes"
    assert p["bridge"]["why"] == "They reach the other 562 notes only through this one."
    assert p["depth"]["title"] == "Mesh depth 2 of 4"
    assert p["depth"]["scale"] == "399 of 729 notes reach depth 2."
    assert p["routing"]["title"] == "Busier than its links"
    assert "96%" in p["routing"]["why"] and "71%" in p["routing"]["why"]
    # the paper's words stay out of what a reader reads, and come last as a term
    for r in (p["bridge"], p["depth"], p["routing"]):
        text = r["title"] + " " + r["why"]
        for jargon in ("strands", "load-bearing", "core 2", "+25", "cut vertex"):
            assert jargon not in text, (jargon, text)
    assert p["bridge"]["term"] == "cut vertex" and p["depth"]["term"] == "k-core"


@NODE
def test_the_place_readings_carry_what_the_figures_draw(tmp_path):
    cut = {"nodes": [{"name": "v", "side": "note", "x": 0, "y": 0}], "edges": [], "colors": {}}
    p = _run_projection(tmp_path, "placeReadings", {**BRIDGE, "cut": cut, "core_hist": [91, 239, 205, 148, 46]})
    assert p["bridge"]["cut"] == cut
    assert p["depth"]["hist"] == [91, 239, 205, 148, 46] and p["depth"]["k"] == 2
    assert p["routing"]["focus"] == "paths"
    low = _run_projection(tmp_path, "placeReadings", {"articulation": False, "strands": 0,
                          "coreness": 0, "surprise": -41, "degree_pct": 60, "betweenness_pct": 19})
    assert low["routing"]["focus"] == "links"     # the rank the reading is about
    # an older server: no layout, no histogram, and the readings still stand
    old = _run_projection(tmp_path, "placeReadings", BRIDGE)
    assert old["bridge"]["cut"] is None and old["depth"]["hist"] == []


@NODE
def test_a_bridge_for_one_note_speaks_in_the_singular(tmp_path):
    p = _run_projection(tmp_path, "placeReadings",
                        {**BRIDGE, "strands": 1, "component": 6, "pieces": [], "behind": []})
    assert p["bridge"]["title"] == "Only bridge for 1 note"
    assert p["bridge"]["why"] == "It reaches the other 4 notes only through this one."


@NODE
def test_routing_reads_as_a_comparison_in_both_directions(tmp_path):
    high = _run_projection(tmp_path, "placeReadings", {"articulation": False, "strands": 0,
                           "coreness": 0, "surprise": 41, "degree_pct": 10, "betweenness_pct": 51})
    low = _run_projection(tmp_path, "placeReadings", {"articulation": False, "strands": 0,
                          "coreness": 0, "surprise": -41, "degree_pct": 60, "betweenness_pct": 19})
    assert high["routing"]["title"] == "Busier than its links"
    assert low["routing"]["title"] == "Linked more than crossed"
    assert (low["routing"]["links"], low["routing"]["paths"]) == (60, 19)
    assert high["warn"] is False and low["warn"] is False


@NODE
def test_an_ordinary_note_draws_no_tile_at_all(tmp_path):
    """Three numbers about a note in the middle of its own area is three lines
    saying "normal". Each reading is absent, not zeroed."""
    p = _run_projection(tmp_path, "placeReadings", {
        "articulation": False, "strands": 0, "coreness": 1, "surprise": 4})
    assert p == {"warn": False, "bridge": None, "depth": None, "routing": None}
    # never measured is not the same answer as ordinary
    assert _run_projection(tmp_path, "placeReadings", None) is None
    assert _run_projection(tmp_path, "placeReadings", {}) is None


def test_the_work_panel_reads_the_structure_block_through_the_projection():
    js = WORK_JS.read_text()
    proj = re.search(r"// --- projection:begin.*?// --- projection:end", js, re.S).group(0)
    assert "structure: c.structure || null," in proj
    assert "function placeReadings(st)" in proj
    assert "d.place = placeReadings(d.structure);" in proj
    # The direction is a spine, not two lists: a rung is a DEPTH, so "read
    # first" and "unlocks" are -1 and +1 of one section and the chain behind
    # them keeps going. The projection carries it through whole, like the
    # structure block, so the server stays the only place that decides which
    # rungs are near enough to name.
    assert "ladder: c.ladder || null," in proj
    assert "function ladderSection(d, into = body)" in js
    assert "nodeSection(\"Read first\"" not in js and "nodeSection(\"Unlocks\"" not in js


def test_only_the_cut_is_painted_as_a_warning():
    """Amber is an accusation, and only the cut vertex passed a gate that
    allows one (ADR-0027): its tag, its pieces and the dot that announces it.
    The Structure rows it replaced are gone, rules and all."""
    css = app_css()
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for warn in (".nb-tag.w {", ".pl-cutfig .pl-cutln {", ".pl-cutfig .pl-ring {", ".dt-dot {",
                 "#note-info .ic-dot {"):
        assert warn in rules, warn
    assert ".wk-str" not in rules


# ---------------------------------------------------------------------------
# the path surface, the metrics rows and the calendar strip
# ---------------------------------------------------------------------------

def test_path_is_a_pane_surface_that_keeps_the_root_search():
    """It renders where the shape views render and is not one of them: they are
    three readings of one /shape load, this one is rooted on a note and needs
    the search they hide."""
    app = app_js()
    body = app.split("function setGraphMode(m) {")[1].split("\n}")[0]
    assert 'const isPath = m === "path";' in body
    # The Zotero Library (ADR-0055) shares the pane too, and hides the search.
    assert '$("#shape-pane").hidden = !(isShape || isPath || isLibrary);' in body
    assert '$("#node-search-wrap").hidden = isShape || isLibrary;' in body
    assert "drawPath();" in body


def test_the_shape_click_defers_to_the_re_root_button():
    """Both handlers live on #shape-pane, and registration order on one node is
    not a contract. The generic one declines rather than the specific one
    shouting."""
    app = app_js()
    assert 'if (e.target.closest("[data-root]")) return;' in app
    assert "rootPath(b.dataset.root)" in app


def test_the_ladder_wires_are_measured_from_the_chips_not_laid_out():
    app = app_js()
    assert "function drawPathWires()" in app
    assert "getBoundingClientRect()" in app.split("function drawPathWires()")[1][:900]
    # and they redraw on resize, because the chips are text of unknown width
    assert "ResizeObserver" in app.split("function drawPathWires()")[1][:2000]


def test_bursting_is_a_card_and_not_a_worklist_row():
    """A worklist is what needs attention. A burst needs none: it is what the
    fortnight turned out to be about, so it is a card in Activity."""
    app = app_js()
    assert '"bursting"' not in app.split("].sort((a, b) => (b[1] || 0) - (a[1] || 0));")[0]
    assert 'mCard("Bursting concepts"' in app


def test_the_calendar_carries_the_burst_strip_above_its_days():
    app = app_js()
    assert "function calBurstStrip()" in app
    body = app.split("function renderCalAgenda() {")[1].split("\n}")[0]
    assert body.index("calBurstStrip()") < body.index('head.className = "cal-ag-head"')


# ---------------------------------------------------------------------------
# the endpoints
# ---------------------------------------------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_vault, monkeypatch):
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def test_context_carries_the_structure_block(client, tmp_vault, monkeypatch):
    tmp_vault.note("A.md", "alpha [[B]]")
    tmp_vault.note("B.md", "beta")
    monkeypatch.setattr(
        "silica.kernel.report.structure.note_structure",
        lambda nid: {"in_graph": True, "degree": 3, "betweenness": 0.4, "coreness": 2,
                     "articulation": True, "strands": 7, "surprise": 0.4123,
                     "prerequisites": ["B.md"], "unlocks": []},
    )
    st = client.get("/context", params={"path": "A.md"}).json()["structure"]
    assert st["coreness"] == 2 and st["articulation"] is True and st["strands"] == 7
    # Rounded to whole percent at the seam: the raw pct-rank difference carries
    # four decimals of noise from a betweenness sampled at 400 pivots.
    assert st["surprise"] == 41
    # The direction is NOT in this block. prerequisites/unlocks are the +-1
    # rungs of `ladder`, which carries the same relation two hops further, and
    # the panel draws that: shipping both spellings in one payload is how the
    # two would have drifted.
    assert "prerequisites" not in st and "unlocks" not in st


def test_context_ships_the_scales_and_the_notes_behind_a_bridge(client, tmp_vault, monkeypatch):
    """What the drawer's labels carry (ADR-0075): depth k OF K, the two ranks
    the surprise is taken of, and the stranded notes piece by piece, each piece
    with the note it is known by."""
    tmp_vault.note("A.md", "alpha [[B]]")
    tmp_vault.note("B.md", "beta")
    monkeypatch.setattr(
        "silica.kernel.report.structure.note_structure",
        lambda nid: {"in_graph": True, "degree": 3, "betweenness": 0.4, "coreness": 2,
                     "core_max": 4, "core_reach": 399, "notes": 729,
                     "degree_rank": 0.7111, "betweenness_rank": 0.9649,
                     "articulation": True, "strands": 3, "surprise": 0.2538,
                     "component": 20, "stranded": [["B.md", "C.md"], ["D.md"]],
                     "core_hist": [91, 239, 205, 148, 46],
                     "layout": {"nodes": [
                         {"id": "A.md", "side": "note", "piece": None, "hop": 0, "x": 0.0, "y": 0.0, "stubs": 0, "area": 3},
                         {"id": "B.md", "side": "behind", "piece": 0, "hop": 1, "x": 1.0, "y": 0.0, "stubs": 1, "area": 3}],
                         "edges": [[0, 1]]},
                     "prerequisites": [], "unlocks": []},
    )
    st = client.get("/context", params={"path": "A.md"}).json()["structure"]
    assert (st["core_max"], st["core_reach"], st["notes"]) == (4, 399, 729)
    assert (st["degree_pct"], st["betweenness_pct"]) == (71, 96)
    assert st["component"] == 20
    assert st["behind"] == ["B.md", "C.md", "D.md"]
    assert [(p["size"], p["head"]["path"]) for p in st["pieces"]] == [(2, "B.md"), (1, "D.md")]
    assert st["core_hist"] == [91, 239, 205, 148, 46]
    cut = st["cut"]
    assert [(n["name"], n["path"], n["side"]) for n in cut["nodes"]] == [("A", "A.md", "note"), ("B", "B.md", "behind")]
    assert "id" not in cut["nodes"][0] and cut["edges"] == [[0, 1]]
    from silica.kernel.recall.graph_export import area_color
    assert cut["colors"] == {"3": [area_color(3), area_color(3, on_paper=True)]}


def test_context_omits_the_block_for_a_note_the_graph_does_not_hold(client, tmp_vault,
                                                                   monkeypatch):
    tmp_vault.note("A.md", "alpha")
    monkeypatch.setattr("silica.kernel.report.structure.note_structure", lambda nid: {})
    assert client.get("/context", params={"path": "A.md"}).json()["structure"] == {}


def test_context_survives_a_structure_pass_that_raises(client, tmp_vault, monkeypatch):
    """A reading, never the drawer: the seven variables are the last section,
    and a broken index must cost that section and nothing else."""
    tmp_vault.note("A.md", "alpha")

    def _boom(nid):
        raise RuntimeError("index on fire")

    monkeypatch.setattr("silica.kernel.report.structure.note_structure", _boom)
    body = client.get("/context", params={"path": "A.md"}).json()
    assert body["structure"] == {} and body["title"] == "A"


def _fake_map(monkeypatch, **kw):
    m = structure.StructureMap(**kw)
    object.__setattr__(m, "_sizes", {})
    monkeypatch.setattr(structure, "structure_map", lambda: m)
    return m


def test_context_carries_the_reading_order_as_rungs_around_this_note(client, tmp_vault,
                                                                    monkeypatch):
    """BASE -> MID -> TIP, asked from the middle: the drawer has to see both
    ends, which is the whole reason the two flat lists were not enough. MID's
    prerequisite list said BASE and stopped; the rung at -1 says BASE and the
    rung at +1 says TIP, off one walk."""
    for n in ("BASE", "MID", "TIP"):
        tmp_vault.note(f"{n}.md", n.lower())
    _fake_map(
        monkeypatch,
        degree={"BASE.md": 1, "MID.md": 1, "TIP.md": 1},
        prereq={"MID": ["BASE"], "TIP": ["MID"]},
        unlocks={"BASE": ["MID"], "MID": ["TIP"]},
    )
    lad = client.get("/context", params={"path": "MID.md"}).json()["ladder"]
    assert [r["depth"] for r in lad["rungs"]] == [-1, 0, 1]
    assert [n["name"] for r in lad["rungs"] for n in r["notes"]] == ["BASE", "MID", "TIP"]
    assert lad["root"] == "MID.md"


def test_a_rung_past_the_near_ones_is_a_count_and_not_a_name(client, tmp_vault, monkeypatch):
    """A 310px column that prints every hop of a four-deep chain buries the
    sections under it. Depth 3 is `further`, and the footer hands it to Path."""
    for i in range(5):
        tmp_vault.note(f"L{i}.md", f"level {i}")
    _fake_map(
        monkeypatch,
        degree={f"L{i}.md": 1 for i in range(5)},
        prereq={f"L{i}": [f"L{i - 1}"] for i in range(1, 5)},
        unlocks={f"L{i}": [f"L{i + 1}"] for i in range(4)},
    )
    lad = client.get("/context", params={"path": "L0.md"}).json()["ladder"]
    assert [r["depth"] for r in lad["rungs"]] == [0, 1, 2]
    # L3 only: `further` counts what the WALK found and the drawer declined to
    # name, and the walk stops at three hops, so L4 was never in it. The footer
    # says "1 further out" and Path, which walks the same three, draws it.
    assert lad["further"] == 1


def test_a_note_with_no_direction_around_it_gets_no_reading_order(client, tmp_vault,
                                                                  monkeypatch):
    """Not a rung of one. A section holding only the note you are already on is
    a heading with nothing under it."""
    tmp_vault.note("A.md", "a")
    _fake_map(monkeypatch, degree={"A.md": 1, "B.md": 1, "C.md": 1}, prereq={"C": ["B"]},
              unlocks={"B": ["C"]})
    assert client.get("/context", params={"path": "A.md"}).json()["ladder"] == {}


def test_path_landing_ranks_two_sided_ladders_first(client, tmp_vault, monkeypatch):
    """A hub with 59 dependents and no prerequisites makes one rung of 59, which
    is a list with a title. The instructive root has notes on both sides."""
    tmp_vault.note("A.md", "a")
    _fake_map(
        monkeypatch,
        degree={"HUB.md": 1, "MID.md": 1, "BASE.md": 1, "TIP.md": 1, "F1.md": 1, "F2.md": 1},
        prereq={"F1": ["HUB"], "F2": ["HUB"], "MID": ["BASE"], "TIP": ["MID"]},
        unlocks={"HUB": ["F1", "F2"], "BASE": ["MID"], "MID": ["TIP"]},
    )
    picks = client.get("/path").json()["picks"]
    assert picks[0]["name"] == "MID"           # 1 before, 1 after
    assert picks[0]["before"] == 1 and picks[0]["after"] == 1


def test_path_says_why_it_is_empty_rather_than_showing_a_blank_pane(client, tmp_vault,
                                                                   monkeypatch):
    tmp_vault.note("A.md", "a")
    monkeypatch.setattr(structure, "structure_map", lambda: structure.StructureMap())
    body = client.get("/path").json()
    assert body["picks"] == []
    assert "co-occurrence index" in body["hint"]


def test_path_groups_the_ladder_into_rungs(client, tmp_vault, monkeypatch):
    tmp_vault.note("A.md", "a")
    _fake_map(monkeypatch, degree={"BASE.md": 1, "ROOT.md": 1, "TIP.md": 1},
              prereq={"ROOT": ["BASE"], "TIP": ["ROOT"]})
    body = client.get("/path", params={"note": "ROOT.md"}).json()
    assert body["root"] == {"name": "ROOT", "path": "ROOT.md"}
    assert [(lv["depth"], [n["name"] for n in lv["notes"]]) for lv in body["levels"]] == [
        (-1, ["BASE"]), (0, ["ROOT"]), (1, ["TIP"]),
    ]
    assert [n for lv in body["levels"] for n in lv["notes"] if n["root"]][0]["name"] == "ROOT"
    assert {"from": "BASE.md", "to": "ROOT.md"} in body["edges"]


def test_path_names_a_note_with_no_direction_instead_of_erroring(client, tmp_vault,
                                                                 monkeypatch):
    tmp_vault.note("A.md", "a")
    _fake_map(monkeypatch, degree={"A.md": 2, "B.md": 1, "C.md": 1}, prereq={"B": ["C"]})
    body = client.get("/path", params={"note": "A.md"}).json()
    assert body["levels"] == [] and "RefD found no direction" in body["hint"]


def test_calendar_carries_the_burst_and_survives_it_failing(client, tmp_vault, monkeypatch):
    rows = [{"concept": "gradient", "z": 3.1, "recent": 4, "total": 5}]
    monkeypatch.setattr("silica.kernel.report.structure.bursting", lambda: rows)
    assert client.get("/calendar", params={"days": 7}).json()["bursting"] == rows

    def _boom():
        raise RuntimeError("no index")

    monkeypatch.setattr("silica.kernel.report.structure.bursting", _boom)
    body = client.get("/calendar", params={"days": 7}).json()
    assert "bursting" not in body and "days" in body


def test_metrics_ships_the_two_store_derived_rows(client, tmp_vault):
    """It rides the co-occurrence depth, so at structural depth it comes back
    empty with its total present, and the client is what decides how to say
    "this leg did not run"."""
    tmp_vault.note("A.md", "alpha [[B]]")
    tmp_vault.note("B.md", "beta")
    body = client.get("/metrics").json()
    assert body["bursting"] == []
    assert "bursting" in body["totals"]


def test_the_calendar_burst_and_the_report_burst_are_the_same_reading(tmp_vault,
                                                                     tmp_path,
                                                                     monkeypatch):
    """Two paths compute V6 because they cost two different amounts: the report
    pays 9.39 s for its co-occurrence depth, the calendar 0.33 s for this one
    variable. Two paths is a licence to disagree, so this is the test that
    revokes it. Both must call signals.burst over the same window.
    """
    from silica.kernel.recall.cooccurrence import CooccurStore, build_contribution
    from silica.kernel.report.graph_report import compute_report

    # Every note carries a wikilink: the structure map is built from the
    # wikilink graph, and a vault of unlinked files has no graph to be in.
    #
    # The proportions are what make the fixture work, not the words. burst()
    # fires at z >= 2, and z is a one-proportion score: four recent notes all
    # carrying a stem that eight older ones never do lands at 2.83. Three of
    # six lands at 1.73 and nothing burst at all, which is the correct answer
    # to a weaker contrast and a useless fixture.
    old, new = "2020-01-01", "2026-08-20"
    names = [f"R{i}" for i in range(4)] + [f"O{i}" for i in range(8)]
    text = {}
    for i, name in enumerate(names):
        nxt = names[(i + 1) % len(names)]
        recent = name.startswith("R")
        body = ("gradient descent tuning" if recent else "sailing boat harbour")
        text[name] = (f"{body} [[{nxt}]]", new if recent else old)
    for name, (body, date) in text.items():
        tmp_vault.note(f"{name}.md", f"---\ndate: {date}\n---\n{body}\n")
    store = CooccurStore(path=tmp_path / "c.json", lang="english")
    for name, (body, _d) in text.items():
        store.upsert_note(name, build_contribution(name, body))
    monkeypatch.setattr(
        "silica.kernel.recall.cooccurrence.get_cooccur_store", lambda *a, **k: store)

    structure._memo.clear()
    structure._burst_memo.clear()
    cheap = structure.bursting()

    report = compute_report(analytics=True, with_cooccurrence=True,
                            _cooccur_store_override=store)
    rich = [{"concept": b.concept, "z": b.z, "recent": b.recent, "total": b.total}
            for b in report.bursting_concepts]
    assert cheap, "the fixture has a real contrast: something must burst"
    assert cheap == rich[:len(cheap)]
