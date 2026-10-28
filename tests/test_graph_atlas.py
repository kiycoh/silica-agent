# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0136's half in the frame: what a line pulls with, the atlas's geometry
and its names. The pure block runs under node with the d3-delaunay the frame
is inlined with, as in test_graph_arrange; the drawing is checked in a browser."""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
FRAME_JS = (STATIC / "graph-frame.js").read_text(encoding="utf-8")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _pure(tmp_path, body: str):
    pure = (STATIC / "d3-delaunay.min.js").read_text(encoding="utf-8") + "\n" \
        + FRAME_JS[FRAME_JS.index("// frame-pure:start"):FRAME_JS.index("// frame-pure:end")]
    script = tmp_path / "pure.js"
    script.write_text(pure + "\n" + body + "\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_only_the_backbone_lays_out_and_a_link_orphan_is_still_placed(tmp_path):
    out = _pure(tmp_path, """
      const n = (id, type = "note") => ({id, type});
      const a = n("a"), b = n("b"), c = n("c"), plan = n("p", "plan"), ghost = n("g", "ghost");
      const linked = new Set(["a", "b"]);
      const l = (source, target, type, extra = {}) => ({source, target, type, ...extra});
      console.log(JSON.stringify([
        pullOf(l(a, b, "EXTRACTED", {backbone: 0.2}), linked, 0.1),   // 4 x J
        pullOf(l(a, b, "EXTRACTED", {backbone: 0.6}), linked, 0.1),   // clamped at 1
        pullOf(l(a, b, "EXTRACTED", {backbone: 0}), linked, 0.1),     // a tree tie, floored
        pullOf(l(a, b, "EXTRACTED"), linked, 0.1),                    // drawn, not pulling
        pullOf(l(a, ghost, "AMBIGUOUS"), linked, 0.1),                // places its ghost
        pullOf(l(a, b, "SIMILAR"), linked, 0.1),                      // both linked: nothing
        pullOf(l(a, c, "SIMILAR"), linked, 0.1),                      // c has no wikilink
        pullOf(l(a, b, "GAP"), linked, 0.1),                          // an absence
        pullOf(l(plan, a, "EXTRACTED", {backbone: 0.5}), linked, 0.1),  // a Silica note
      ]));
    """)
    assert out == pytest.approx([0.8, 1, 0.2, 0, 0.1, 0, 0.1, 0, 0])


@needs_node
def test_a_silica_note_follows_its_notes_and_moves_none_of_them(tmp_path):
    out = _pure(tmp_path, """
      const a = {x: 0, y: 0, vx: 0, vy: 0}, b = {x: 10, y: 0, vx: 0, vy: 0};
      const p = {x: 100, y: 50, vx: 0, vy: 0, _follow: [a, b]};
      followNative([a, b, p], 1);
      console.log(JSON.stringify([p.vx, p.vy, a.vx, b.vx]));
    """)
    assert out == pytest.approx([(5 - 100) * 0.1, -50 * 0.1, 0, 0])


@needs_node
def test_a_note_s_ground_is_its_cell_inside_its_disc(tmp_path):
    out = _pure(tmp_path, """
      const sq = [[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]];
      const r = (cell, x, y, R) => { const k = cellInDisc(cell, x, y, R, 24); return [k.pts.length / 2, k.arc.filter(Boolean).length,
        Math.max(...k.pts.filter((v, i) => i % 2 === 0))]; };
      console.log(JSON.stringify([r(sq, 0, 0, 2), r(sq, 0, 0, 0.5), r(sq.slice().reverse(), 0.9, 0, 0.5)]));
    """)
    assert out[0][:2] == [4, 0], "a cell inside the disc is the cell, no coast"
    assert out[1][:2] == [24, 24], "a disc inside the cell is the disc, all coast"
    # cut by the cell's edge at x = 1, whatever the winding
    assert out[2][1] == out[2][0] - 1 and out[2][2] == pytest.approx(1)


@needs_node
def test_a_border_is_the_lens_of_two_notes_and_says_which_border_it_is(tmp_path):
    """Three notes at distance 1, R = 1: each lens chord is sqrt(R^2 - d^2/4)
    either side of the midpoint, cut where the Voronoi edge stops."""
    out = _pure(tmp_path, """
      const P = new Float64Array([0, 0, 1, 0, 0.5, Math.sqrt(3) / 2]);
      const del = new d3Delaunay.Delaunay(P), vor = del.voronoi([-5, -5, 5, 5]);
      const run = (country, prov, R) => atlasBorders(P, Int32Array.from(country), Int32Array.from(prov), R, del, vor.circumcenters);
      const len = f => { let s = 0; for (let k = 0; k < f.length; k += 4) s += Math.hypot(f[k + 2] - f[k], f[k + 3] - f[k + 1]); return s; };
      const all = run([0, 1, 2], [0, 1, 2], 1);
      const prov = run([0, 0, 0], [0, 1, 1], 1);
      const none = run([0, 0, 0], [0, 0, 0], 1);
      const apart = run([0, 1, 2], [0, 1, 2], 0.4);
      console.log(JSON.stringify([all.country.length / 4, len(all.country), Object.keys(prov.province),
        prov.province[0].length / 4, prov.country.length, none.country.length + Object.keys(none.province).length,
        apart.country.length]));
    """)
    n, total, prov_keys, prov_segs, prov_country, none, apart = out
    assert n == 3
    # Each Voronoi edge runs from the circumcentre outward: the inner half of
    # each chord is cut at the centre, the outer half is the hull's ray.
    half = math.sqrt(1 - 0.25)
    inner = 1 / (2 * math.sqrt(3))   # midpoint to circumcentre
    assert total == pytest.approx(3 * (inner + half))
    assert prov_keys == ["0"] and prov_segs == 2 and prov_country == 0
    assert none == 0, "two notes of one province share no border"
    assert apart == 0, "discs that do not meet draw no border"


@needs_node
def test_a_name_takes_the_first_free_spot_and_waits_when_there_is_none(tmp_path):
    out = _pure(tmp_path, """
      const b = (x0, y0, x1, y1) => ({x0, y0, x1, y1});
      const area = b(0, 0, 100, 100);
      console.log(JSON.stringify([
        placeLabels([[b(10, 10, 30, 20)], [b(15, 12, 35, 22), b(10, 30, 30, 40)], [b(12, 11, 20, 19), b(90, 90, 120, 95)]], area),
        keepLabels([b(10, 10, 30, 20), b(15, 12, 35, 22)], area),
      ]));
    """)
    assert out == [[0, 1, -1], [True, False]]


@needs_node
def test_the_zoom_names_areas_then_sub_areas_then_notes(tmp_path):
    out = _pure(tmp_path, """
      const z = [0.5, 1, 1.79, 1.8, 3.59, 3.6, 9];
      console.log(JSON.stringify([z.map(k => atlasTier(k)), z.map(k => atlasTier(k, false))]));
    """)
    assert out[0] == [0, 0, 0, 1, 1, 2, 2]
    # no province to name: the areas hold the middle of the zoom
    assert out[1] == [0, 0, 0, 0, 0, 2, 2]


@needs_node
def test_a_name_tries_notes_spread_across_its_part(tmp_path):
    out = _pure(tmp_path, """
      const P = new Float64Array(Array.from({length: 20}, (_, i) => [i, 0]).flat());
      const ids = Array.from({length: 20}, (_, i) => i);
      console.log(JSON.stringify([atlasSpots(ids, P, 1), atlasSpots(ids, P, 3), atlasSpots([4, 7], P, 9)]));
    """)
    one, three, few = out
    assert one in ([9], [10]), "first the note nearest the centre"
    assert three[0] == one[0] and {0, 19} & set(three[1:]), "then spread out to the ends"
    assert sorted(few) == [4, 7]


def test_2d_lays_out_with_forceatlas2_and_only_in_a_worker():
    """d3 keeps 3D and a 2D frame with no worker (ADR-0136, step b in 2D)."""
    assert 'const ENGINE_2D = NO_WORKER || typeof Worker === "undefined" ? "d3" : "fa2";' in FRAME_JS
    assert 'engine: is2D() ? ENGINE_2D : "d3", unit: layoutUnit,' in FRAME_JS
    assert 'if (m.init.engine === "fa2") { faInit(m.init.unit); faForces(m.init.forces, links); }' in FRAME_JS
    # A 2D layout settled by one engine never seeds the other as its own,
    # and a cached FA2 layout restarts at the unit it stopped at.
    assert '":" + (is2D() ? ENGINE_2D : "d3")' in FRAME_JS
    assert "unit: SIM && SIM.last ? SIM.last.unit : 1" in FRAME_JS


def test_the_renderer_and_the_layout_cache_start_over_once():
    """The first build wrote the old default, 3D, into every profile, and a
    layout settled with every line pulling is not the backbone's."""
    assert re.search(r'const MODE_KEY = "silica-graph-mode-2";\nlet mode = "2d";', FRAME_JS)
    assert 'if (localStorage.getItem(MODE_KEY) === "3d") mode = "3d";' in FRAME_JS
    assert "if (rebuild) try { localStorage.setItem(MODE_KEY, m); }" in FRAME_JS
    assert 'const LAYOUT_KEY = "silica-graph-layout-2";' in FRAME_JS


def test_with_the_atlas_on_a_line_shows_for_the_note_in_hand_only():
    quiet = FRAME_JS[FRAME_JS.index("const quietNow = l =>"):].split("\n")[0]
    assert "atlasOn()" in quiet and "!AB" in quiet and 'l.type !== "COVERS"' in quiet
    assert "(hoverId || focusIds.length) && !l._dim" in quiet
    # the atlas names its notes; drawNode's own labels stand down
    node = FRAME_JS[FRAME_JS.index("function drawNode("):FRAME_JS.index("// --- an arrangement, drawn")]
    assert "if (atlas || (scale < 1.5" in node


def test_a_drag_holds_forceatlas2_running_from_a_settled_layout():
    """A settled run has spent its budget: the hold has to open it again, or
    the notes around a dragged one never answer the drag (2026-10-28)."""
    assert "if (fa) { fa.hold = m.target > 0; faRestart(); }" in FRAME_JS
    step = FRAME_JS[FRAME_JS.index("function faStep() {"):FRAME_JS.index("function faRestart()")]
    assert step.index("if (!fa.left)") < step.index("if (fa.hold) return true;")
