"""The graph frame's half of ADR-0088: what a click lights, and the Map it is
arranged as. The frame's pure block runs under node, as in
test_zotero_graph_frame; the glue around it is driven in a browser."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
FRAME_JS = (STATIC / "graph-frame.js").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _pure(tmp_path, body: str):
    pure = (STATIC / "blob-geometry.js").read_text(encoding="utf-8") + "\n" \
        + FRAME_JS[FRAME_JS.index("// frame-pure:start"):FRAME_JS.index("// frame-pure:end")]
    script = tmp_path / "pure.js"
    script.write_text(pure + "\n" + body + "\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_a_neighbour_is_lit_only_through_an_edge_on_show(tmp_path):
    """The index used to be every edge of the payload, so a note reached by a
    hidden proposal was lit with no line to say why, and "Machine learning"
    counted 38 neighbours with 31 drawn."""
    lit = _pure(tmp_path, """
      const e = (from, to, shown) => ({from, to, _hidden: !shown});
      const incident = {r: [e("r", "a", true), e("b", "r", true), e("r", "g", false)]};
      console.log(JSON.stringify([...litSet(["r"], incident)].sort()));
    """)
    assert lit == ["a", "b", "r"]


@needs_node
def test_an_edge_the_filters_have_not_read_yet_still_lights(tmp_path):
    """Before the first filter pass no edge carries the flag: absent is shown."""
    lit = _pure(tmp_path, """
      console.log(JSON.stringify([...litSet(["r"], {r: [{from: "r", to: "a"}]})].sort()));
    """)
    assert lit == ["a", "r"]


@needs_node
def test_arranging_pins_each_note_around_the_root_and_restoring_gives_every_pin_back(tmp_path):
    """The arrangement is laid over the force layout, never into it: what was
    pinned before (a rim paper) is pinned the same way after."""
    out = _pure(tmp_path, """
      const nodes = [
        {id: "r", x: 100, y: 50, z: 0},
        {id: "a", x: 7, y: 8, z: 9, vx: 3},
        {id: "rim", x: 1, y: 2, fx: 1, fy: 2},
      ];
      const saved = new Map();
      placeArranged(nodes, {r: [0, 0, 0], a: [30, -40, 1], rim: [0, 90, 2]},
                    {x: 100, y: 50, z: 5}, saved, true);
      const placed = nodes.map(n => [n.id, n.x, n.y, n.z, n.fx, n.fy, n.fz]);
      restoreArranged(nodes, saved);
      const back = nodes.map(n => [n.id, n.x, n.y, n.z, n.fx, n.fy, n.fz, n.vx || 0]);
      console.log(JSON.stringify({placed, back}));
    """)
    assert out["placed"] == [
        ["r", 100, 50, 5, 100, 50, 5],
        ["a", 130, 10, 5, 130, 10, 5],
        ["rim", 100, 140, 5, 100, 140, 5],
    ]
    assert out["back"] == [
        ["r", 100, 50, 0, None, None, None, 0],
        ["a", 7, 8, 9, None, None, None, 0],
        ["rim", 1, 2, None, 1, 2, None, 0],
    ]


@needs_node
def test_a_second_arrangement_keeps_the_first_saved_positions(tmp_path):
    """Re-rooting on an arranged note must not save the arranged positions as
    the ones to come back to."""
    out = _pure(tmp_path, """
      const nodes = [{id: "r", x: 0, y: 0}, {id: "a", x: 5, y: 5}];
      const saved = new Map();
      placeArranged(nodes, {r: [0, 0, 0], a: [10, 0, 1]}, {x: 0, y: 0}, saved, false);
      placeArranged(nodes, {a: [0, 0, 0], r: [-10, 0, 1]}, {x: 10, y: 0}, saved, false);
      restoreArranged(nodes, saved);
      console.log(JSON.stringify(nodes.map(n => [n.id, n.x, n.y])));
    """)
    assert out == [["r", 0, 0], ["a", 5, 5]]


@needs_node
def test_names_are_kept_by_priority_and_a_later_one_that_collides_is_held_back(tmp_path):
    """ADR-0069 rule 3: nothing is shrunk to fit; the lower-priority name of a
    colliding pair waits for room."""
    kept = _pure(tmp_path, """
      console.log(JSON.stringify(keepLabels([
        {x0: 0, y0: 0, x1: 10, y1: 4},     // the root
        {x0: 8, y0: 2, x1: 20, y1: 6},     // overlaps the root: held back
        {x0: 30, y0: 0, x1: 40, y1: 4},    // clear
        {x0: 12, y0: 0, x1: 18, y1: 4},    // clear of the root and of 30-40
      ])));
    """)
    assert kept == [True, False, True, True]


# --- the wiring, read off the source (driven for real in a browser) ---------

def _fn(name: str) -> str:
    start = FRAME_JS.index(f"function {name}(")
    return FRAME_JS[start:FRAME_JS.index("\n}\n", start)]


def test_the_focus_and_its_banner_read_the_edges_on_show():
    for name in ("markDim", "updateFocusBar"):
        body = _fn(name)
        assert "litSet(" in body and "neighbors[" not in body, name
    assert "markDim(" in _fn("paintDim")


def test_a_layer_switch_relights_the_focus():
    """The lit set follows the switches, so flipping one recomputes it, and a
    single focus is arranged again over what it now lights."""
    assert "applyFocus(focusIds)" in _fn("updateEdgeFilter")


def test_a_single_focus_arranges_and_everything_else_dims():
    assert "arrangeFocus()" in _fn("applyFocus")


def test_the_layout_cache_never_holds_an_arrangement():
    """The cache is the layout the frame opens on; an arrangement is a state
    of one focus and comes back by Esc, not on the next load."""
    assert "if (ARR) return;" in _fn("saveLayout")


def test_a_scope_a_compare_and_a_mode_switch_each_put_the_layout_back():
    assert "disarrange(" in _fn("setScope")
    assert "disarrange(" in _fn("setMode")
    block = FRAME_JS[FRAME_JS.index('e.data.type === "silica-compare"'):]
    assert "disarrange(" in block[:block.index("\n  }\n")]


@needs_node
def test_a_name_outside_the_free_area_is_held_back_not_cut(tmp_path):
    """The canvas runs under the key and past the frame's edge: a name there
    would be drawn and cut, so it waits like a colliding one."""
    kept = _pure(tmp_path, """
      const area = {x0: 0, y0: 0, x1: 100, y1: 50};
      console.log(JSON.stringify(keepLabels([
        {x0: 10, y0: 10, x1: 40, y1: 20},     // inside
        {x0: -5, y0: 10, x1: 30, y1: 20},     // past the left edge
        {x0: 90, y0: 30, x1: 120, y1: 40},    // under the key
      ], area)));
    """)
    assert kept == [True, False, False]


def test_an_arranged_note_is_hit_where_it_is_drawn():
    """A dot keeps 4px on screen in an arrangement; the hit area was the graph
    radius, 0.6px at the zoom that fits "Machine learning", so a click on a
    neighbour missed it and cleared the focus instead of re-rooting."""
    assert "arrangedRadius(" in _fn("hitRadius")
    assert "arrangedRadius(" in _fn("drawArrangedNode")


# --- the arrangement keeps the vault (ADR-0088 amendment) --------------------

@needs_node
def test_the_ring_is_as_wide_as_the_neighbourhood_it_came_from(tmp_path):
    """The server lays the Map out in its own unit (a radius near 1000 on the
    test vault), twice the spread of the neighbours it came from: laid over the
    graph at that size it covered 195 grey notes around "Machine learning".
    So it is scaled to 1.1 times the median distance the neighbours stood at,
    between 0.3 and 1 of its own size."""
    out = _pure(tmp_path, """
      const layout = {r: [0, 0, 0], a: [100, 0, 1], b: [0, 50, 1], c: [-60, 0, 1]};
      const at = d => ({r: {x: 0, y: 0}, a: {x: d, y: 0}, b: {x: 0, y: d}, c: {x: -d, y: 0}});
      const fit = d => fitArrangement(layout, [{a0: 0, a1: 1, r: 100}], 10, at(d), "r");
      const mid = fit(40), small = fit(5), big = fit(500);
      console.log(JSON.stringify({s: [mid.s, small.s, big.s], a: mid.layout.a, rIn: mid.rIn,
                                  w: mid.wedges[0].r}));
    """)
    assert [round(v, 3) for v in out["s"]] == [0.44, 0.3, 1.0]
    assert [round(v, 1) for v in out["a"][:2]] == [44.0, 0.0] and out["a"][2] == 1
    assert round(out["rIn"], 2) == 4.4 and round(out["w"], 1) == 44.0


@needs_node
def test_the_ring_is_turned_to_face_the_way_its_neighbours_stood(tmp_path):
    """The server's angles say nothing about the picture the reader is looking
    at, so a neighbour above the root could land below it and cross the whole
    map to get there. One rotation (Procrustes, closed form) turns the ring to
    match: on "Machine learning" the median trip falls from 681 to 414."""
    out = _pure(tmp_path, """
      // The neighbours stood a quarter turn from where the server put them.
      const layout = {r: [0, 0, 0], a: [10, 0, 1], b: [0, 10, 1]};
      const home = {r: {x: 5, y: 5}, a: {x: 5, y: 15}, b: {x: -5, y: 5}};
      const f = fitArrangement(layout, [{a0: 0, a1: 0.5, r: 10}], 0, home, "r");
      console.log(JSON.stringify({a: f.layout.a, b: f.layout.b, th: f.th, w: f.wedges[0]}));
    """)
    # 1.1 times the median distance (10) is past the ring's own size: kept at 1.
    assert [round(v, 6) for v in out["a"][:2]] == [0, 10]
    assert [round(v, 6) for v in out["b"][:2]] == [-10, 0]
    assert round(out["th"], 6) == round(3.141592653589793 / 2, 6)
    assert round(out["w"]["a0"], 6) == round(3.141592653589793 / 2, 6)
    assert round(out["w"]["a1"], 6) == round(0.5 + 3.141592653589793 / 2, 6)


@needs_node
def test_the_notes_under_the_ring_step_out_to_its_edge_and_the_rest_stay(tmp_path):
    """The clearing: what stands within 1.6 radii of the root is pressed into
    the band between 1.08 and 1.6 radii, each note on its own bearing and in
    its own order; past 1.6 nothing moves. Across the plane only, so in 3D the
    ring is cleared all the way to the camera."""
    out = _pure(tmp_path, """
      const pts = [{id: "in", x: 10 + 50, y: 0}, {id: "mid", x: 10, y: 120},
                   {id: "out", x: 10 + 170, y: 0}, {id: "on", x: 10, y: 0}];
      console.log(JSON.stringify(clearRing(pts, {x: 10, y: 0}, 100)));
    """)
    assert "out" not in out
    x, y = out["in"]
    assert round(y, 6) == 0 and 108 < x - 10 < 160
    mx, my = out["mid"]
    assert round(mx, 6) == 10 and x - 10 < my < 160
    assert 108 <= abs(complex(*out["on"]) - 10) < 109


@needs_node
def test_a_hover_lights_its_own_neighbourhood(tmp_path):
    """The look a click used to give, now on a hover: the note, its neighbours
    over the edges on show and the links that touch it are lit; the rest dims."""
    out = _pure(tmp_path, """
      const E = (from, to) => ({from, to});
      const ra = E("r", "a"), ab = E("a", "b"), bc = E("b", "c");
      const nodes = ["r", "a", "b", "c"].map(id => ({id}));
      const incident = {r: [ra], a: [ra, ab], b: [ab, bc], c: [bc]};
      markDim(["b"], incident, nodes, [ra, ab, bc]);
      const hover = [nodes.map(n => n._dim), [ra, ab, bc].map(e => e._dim)];
      markDim([], incident, nodes, [ra, ab, bc]);
      console.log(JSON.stringify({hover, none: nodes.map(n => n._dim)}));
    """)
    assert out["hover"] == [[True, False, False, False], [True, False, False]]
    assert out["none"] == [False, False, False, False]


def test_a_hover_is_a_look_and_leaves_the_focus_where_it_was():
    """focusIds, the banner and an arrangement belong to the click; the hover
    only chooses whose set is lit while the pointer rests."""
    assert ".onNodeHover(n => { if (!is2D()) hoverNote(n); })" in FRAME_JS
    body = _fn("hoverNote") + _fn("paintHover")
    assert "focusIds =" not in body and "updateFocusBar" not in body and "arrangeFocus" not in body
    assert "hoverId ? [hoverId] : focusIds" in _fn("paintDim")
    assert "paintDim()" in _fn("computeFocus")


def test_the_arrangement_dims_the_vault_instead_of_hiding_it():
    assert "ARR" not in _fn("computeFilters")
    assert "ARR && ARR.layout[n.id]" in _fn("drawNode")


def test_leaving_the_arrangement_by_hand_plays_it_back():
    """Esc, an empty click and closing the note animate home; a scope, a
    compare and a mode switch put the layout back at once."""
    assert "disarrange(true, true)" in _fn("arrangeFocus")


# --- the dim, faded in (owner, 2026-10-11) ------------------------------------

@needs_node
def test_a_dim_moves_toward_its_target_by_the_step_and_stops_there(tmp_path):
    """Notes leaving the focus go dark over a few frames instead of at once,
    and a note coming back into it lights the same way."""
    out = _pure(tmp_path, """
      console.log(JSON.stringify([fadeToward(0, true, 0.25), fadeToward(0.9, true, 0.25),
        fadeToward(1, false, 0.5), fadeToward(0.2, false, 0.5), fadeToward(undefined, true, 1)]));
    """)
    assert out == [0.25, 1, 0.5, 0, 1]


@needs_node
def test_a_colour_halfway_to_the_dim_is_the_mix_of_the_two(tmp_path):
    out = _pure(tmp_path, """
      console.log(JSON.stringify([mixHex("#000000", "#ffffff", 0.5), mixHex("#80afe5", "#1A1D20", 0),
        mixHex("#80afe5", "#1A1D20", 1), mixHex("rgba(1,2,3,0.5)", "#1A1D20", 0.4)]));
    """)
    assert out == ["#808080", "#80afe5", "#1a1d20", "rgba(1,2,3,0.5)"]


def test_every_relight_fades_and_every_paint_reads_the_fade():
    """refreshPaint is where every change of the dim lands (a click, a hover, a
    scope, a compare), so the fade starts there; the renderers read `_fade`."""
    assert "startFade()" in _fn("refreshPaint")
    assert "_fade" in _fn("nodeColor") and "_fade" in _fn("linkPaint")
    # A rebuild reads the flags without a repaint: it takes them as they stand.
    assert "snapFade()" in _fn("setMode")


def test_3d_fades_a_note_in_its_shader_not_by_rebuilding_its_material():
    """The bundle shares one material per colour and rebuilds them on every
    colour re-pass, so a colour per frame would rebuild the node digest per
    frame. The dim rides the per-node uniforms the rings already use."""
    assert "uSilDim" in FRAME_JS and "U_DIM" in _fn("nodeUniforms")
    assert "U_DIM" in FRAME_JS[FRAME_JS.index("const U_ALL"):FRAME_JS.index("const U_HELD")]


@needs_node
def test_the_note_under_the_pointer_is_the_nearest_one_it_reaches(tmp_path):
    """2D picks its own hover off the positions: the nearest visible note whose
    hit radius reaches the point, or none."""
    out = _pure(tmp_path, """
      const nodes = [{id: "a", x: 0, y: 0}, {id: "b", x: 10, y: 0},
                     {id: "h", x: 4, y: 0, _hidden: true}, {id: "u"}];
      const r = () => 6;
      const at = (x, y) => { const n = nearestHit(nodes, {x, y}, r); return n && n.id; };
      console.log(JSON.stringify([at(4, 0), at(6, 0), at(30, 0)]));
    """)
    assert out == ["a", "b", None]


def test_2d_hovers_off_the_positions_not_the_picking_canvas():
    """force-graph re-reads its hover every frame from a picking canvas that a
    resolution drop (setRes) blanks until its 800 ms redraw. A relight is a
    repaint stream, which drops the resolution, so with a resting pointer the
    hover flipped to nothing and back and each flip set off the next (looping
    in the owner's tab, 2026-10-11). 2D now asks the positions, on a pointer
    move only; 3D raycasts, which nothing blanks."""
    wrap = FRAME_JS[FRAME_JS.index('["pointerdown", "pointermove", "wheel", "touchstart"]'):]
    assert "pickHover2D(e)" in wrap[:wrap.index("}, { capture: true")]
    assert "noteAt2D(e)" in _fn("pickHover2D")
    assert "nearestHit(" in _fn("noteAt2D") and "hitRadius(" in _fn("noteAt2D")
    assert "hitRadius(" in _fn("paintNodeArea")
    assert ".onNodeHover(n => { if (!is2D()) hoverNote(n); })" in FRAME_JS


def test_2d_clicks_off_the_positions_not_the_picking_canvas():
    """force-graph takes its click from the same picking canvas: at pointer-up
    it calls onNodeClick for the note it last read there, else
    onBackgroundClick. That canvas is blank after a relight and lags a played
    move by up to 800 ms, so a click on a ring note landed as an empty click:
    the Map played back, and the hover left the clicked note lit with its
    neighbours over the force layout (owner, 2026-10-11). 2D asks the positions;
    3D keeps the lib's raycast."""
    assert ".onNodeClick((n, e) => clickNote(n, e))" in FRAME_JS
    assert ".onBackgroundClick(e => clickNote(null, e))" in FRAME_JS
    assert "is2D()" in _fn("clickNote") and "noteAt2D(e)" in _fn("clickNote")
