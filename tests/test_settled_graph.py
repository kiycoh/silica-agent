# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0152: the 2D layout settles once and holds, notes arrive on a circle, a
note's size is its links, a clicked link says what it is, and Clusters and Flow
are tabs of the same frame.

The frame's drawing is verified by opening it; what is pinned here is what the
server answers and the frame's pure functions, run under node.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
FRAME = (STATIC / "graph-frame.js").read_text()
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@pytest.fixture
def client(tmp_vault):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    SESSION.reset()
    return TestClient(server.app)


def _pure(body: str):
    """The frame's pure block, then `body`, whose value is printed as JSON."""
    block = FRAME[FRAME.index("// frame-pure:start"):FRAME.index("// frame-pure:end")]
    out = subprocess.run(["node", "-e", block + f"\nconsole.log(JSON.stringify((() => {{ {body} }})()));"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_the_overlap_pass_leaves_no_two_discs_overlapping():
    x, y, r = _pure("""
      const x = [], y = [], r = [];
      for (let i = 0; i < 300; i++) { x.push((i * 37 % 41) * 2); y.push((i * 53 % 43) * 2); r.push(3 + i % 7); }
      x[1] = x[0]; y[1] = y[0];   // two notes on one point have no bearing
      spreadDiscs(x, y, r, 1.5);
      return [x, y, r];
    """)
    for i in range(len(x)):
        for j in range(i):
            assert math.hypot(x[i] - x[j], y[i] - y[j]) >= r[i] + r[j]


def _spiral(n: int, radius: float, cx: float = 0.0) -> tuple[list[float], list[float]]:
    """n points spread evenly over a disc, the way a settled part fills its room."""
    xs, ys = [], []
    for i in range(n):
        d, a = radius * math.sqrt((i + 0.5) / n), i * 2.399963
        xs.append(cx + d * math.cos(a))
        ys.append(d * math.sin(a))
    return xs, ys


@needs_node
def test_discs_shrink_until_they_fill_half_the_room_the_forces_left_a_part():
    """ADR-0152, amended 2026-11-13: discs counted in pixels at the fit needed
    1.98 times the circle ForceAtlas2 left alex_second_brain's notes, and the
    overlap pass mixed the areas. The most crowded part brings every disc down
    to half its circle, and a sparse part does not lend it room; a sparse vault
    keeps its discs; a part under 21 notes says nothing."""
    xs, ys = _spiral(100, 100.0)
    sx, sy = _spiral(5, 3.0, cx=1000.0)
    fx, fy = _spiral(100, 100.0, cx=-1000.0)
    got = _pure(f"""
      const x = {json.dumps(xs)}, y = {json.dumps(ys)}, part = x.map(() => 0);
      const dense = discScale(x, y, x.map(() => 10), part);
      const sparse = discScale(x, y, x.map(() => 2), part);
      const withSmall = discScale(x.concat({json.dumps(sx)}), y.concat({json.dumps(sy)}),
        x.map(() => 10).concat([50, 50, 50, 50, 50]), part.concat([1, 1, 1, 1, 1]));
      const withSparse = discScale(x.concat({json.dumps(fx)}), y.concat({json.dumps(fy)}),
        x.map(() => 10).concat(x.map(() => 2)), part.concat(part.map(() => 2)));
      const nowhere = discScale(x.map(() => 0), y.map(() => 0), x.map(() => 5), part);
      return [dense, sparse, withSmall, withSparse, nowhere, partsOf(5, [[0, 1], [1, 2], [3, 4]])];
    """)
    dense, sparse, with_small, with_sparse, nowhere, parts = got
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    r95 = sorted(math.hypot(a - cx, b - cy) for a, b in zip(xs, ys))[95]
    assert dense < 1
    assert dense ** 2 * 100 * 10 ** 2 / r95 ** 2 == pytest.approx(0.5)
    assert sparse == 1
    assert with_small == pytest.approx(dense)
    assert with_sparse == pytest.approx(dense)
    assert nowhere == 1
    assert parts[0] == parts[1] == parts[2] and parts[3] == parts[4] and parts[0] != parts[3]


def test_a_crowded_vault_is_spread_by_areas():
    """The pass to no overlap mixed alex_second_brain's areas at every size a
    reader sees (0.63 to 0.28 at the 0.5 factor), so a scaled vault is spread by
    areas first and the pass is left a last nudge; what links to nothing big
    goes beside its folder where the areas put it (ADR-0152 and ADR-0156,
    amendments 2026-11-13)."""
    body = FRAME[FRAME.index("function spreadLayout() {"):]
    body = body[:body.index("\n}\n")]
    areas = body.index("if (DISC_SCALE < 1) spreadAreas(x, y, r, adoptAreas(")
    assert areas < body.index("nearFolder(") < body.index("spreadDiscs(x, y, r, gap);")


@needs_node
def test_areas_laid_on_each_other_come_apart():
    """Two areas laid on top of each other come out apart, a note of no area
    and all, and a node outside a big part does not move; the overlap pass after
    finds nothing to mix."""
    xs, ys = _spiral(61, 10.0)
    got = _pure(f"""
      const x = {json.dumps(xs)}.concat([40]), y = {json.dumps(ys)}.concat([0]);
      const area = x.map((_, i) => i < 60 ? i % 2 : -1);
      const held = x.map((_, i) => i <= 60);
      spreadAreas(x, y, x.map(() => 2), area, held, 0.5);
      const loose = [x[61], y[61]];
      spreadDiscs(x, y, x.map(() => 2), 0.5);
      return [x, y, loose];
    """)
    x, y, loose = got
    assert loose == [40, 0]
    for i in range(len(x)):
        for j in range(i):
            assert math.hypot(x[i] - x[j], y[i] - y[j]) >= 4 - 1e-9
    cen = [(sum(x[i] for i in range(k, 60, 2)) / 30, sum(y[i] for i in range(k, 60, 2)) / 30) for k in (0, 1)]
    for i in range(60):
        own, other = cen[i % 2], cen[1 - i % 2]
        assert math.hypot(x[i] - own[0], y[i] - own[1]) < math.hypot(x[i] - other[0], y[i] - other[1])


@needs_node
def test_a_note_in_no_area_joins_the_area_of_its_ties():
    """A note that is an area of one, or in none, is spread with the area most
    of its ties are in, through such notes too; with no tie it stays alone
    (the owner, 2026-11-13: "le note non linkate vanno nel loro vicino")."""
    got = _pure("""
      const area = [0, 0, 1, 1, -1, 5, -1, -1];
      return adoptAreas(area, [[4, 0], [4, 1], [4, 2], [5, 2], [5, 3], [6, 4]]);
    """)
    assert got == [0, 0, 1, 1, 0, 1, 0, -1]


@needs_node
def test_what_links_to_nothing_big_sits_beside_its_folder():
    """ADR-0156, amended 2026-11-13: the files with no link sat on a ring the
    forces push out, and the fit framed it. Each goes beside the nearest folder
    up its path that holds a node of a big part; with none, beside them all.
    The big parts do not move, and two loose nodes do not overlap."""
    a_x, a_y = _spiral(30, 20.0, cx=100.0)
    b_x, b_y = _spiral(30, 20.0, cx=-100.0)
    got = _pure(f"""
      const x = {json.dumps(a_x + b_x)}.concat([900, 905, -900, 0]);
      const y = {json.dumps(a_y + b_y)}.concat([900, 900, 900, -900]);
      const big = x.map((_, i) => i < 60);
      const dirs = Array(30).fill("A").concat(Array(30).fill("B/C"), ["A/x/y", "A/x/y", "B/C", "Z"]);
      const before = [x.slice(0, 60), y.slice(0, 60)];
      nearFolder(x, y, x.map(() => 2), big, dirs, 1);
      return [x, y, before];
    """)
    x, y, (bx, by) = got
    assert x[:60] == bx and y[:60] == by
    near = lambda i, cx: math.hypot(x[i] - cx, y[i]) < 20
    assert near(60, 100.0) and near(61, 100.0)
    assert math.hypot(x[60] - x[61], y[60] - y[61]) >= 2 + 2
    assert near(62, -100.0)
    assert near(63, 0.0)


@needs_node
def test_with_no_big_part_nothing_moves():
    got = _pure("""
      const x = [1, 2, 3], y = [4, 5, 6];
      nearFolder(x, y, [1, 1, 1], [false, false, false], ["A", "A", "B"], 1);
      return [x, y];
    """)
    assert got == [[1, 2, 3], [4, 5, 6]]


@needs_node
def test_the_circle_runs_in_area_order_clockwise_from_the_top():
    at = _pure("""
      const rows = [{id: "small", area: 2, links: 9, r: 1}, {id: "b", area: 1, links: 1, r: 1},
                    {id: "a", area: 1, links: 5, r: 1}, {id: "c", area: 1, links: 0, r: 1}];
      return Object.fromEntries(arrivalRing(rows, 100));
    """)
    assert [k for k, _ in sorted(at.items(), key=lambda kv: kv[1]["rank"])] == ["a", "b", "c", "small"]
    assert all(math.isclose(math.hypot(p["x"], p["y"]), 100) for p in at.values())
    # the first arc starts at twelve and goes right: y grows downward on a canvas
    assert at["a"]["x"] > 0 and at["a"]["y"] < 0


@needs_node
def test_clusters_sit_on_one_ring_biggest_first_and_never_touch():
    out = _pure("""
      const g = (key, n) => ({ key, name: key, color: "#123456", ids: Array.from({length: n}, (_, i) => key + i),
                               r: Array.from({length: n}, (_, i) => 3 + i % 5) });
      return clusterRing([g("s", 2), g("big", 120), g("m", 30), g("t", 1), g("n", 12)], 1);
    """)
    heads = out["heads"]
    assert [h["key"] for h in heads] == ["big", "m", "n", "s", "t"]
    assert heads[0]["th"] > -math.pi / 2 and all(a["th"] < b["th"] for a, b in zip(heads, heads[1:]))
    ring = [math.hypot(h["cx"], h["cy"]) for h in heads]
    assert max(ring) - min(ring) < 1e-6, "one ring"
    for i, a in enumerate(heads):
        for b in heads[:i]:
            assert math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"]) >= a["R"] + b["R"]
    assert len(out["layout"]) == 165


@needs_node
def test_an_arrangement_keeps_room_for_its_picture_on_a_narrow_frame():
    wide, phone, flow = _pure("""
      return [layoutRoom(912, 165, 165), layoutRoom(302, 165, 165), layoutRoom(302, 40, 190)];
    """)
    assert wide == [165, 165]   # room to spare: the heads keep all of it
    assert sum(phone) == pytest.approx(151) and phone[0] == phone[1]
    assert sum(flow) == pytest.approx(151) and flow[0] < flow[1]   # the names keep their share


@needs_node
def test_a_head_too_long_for_its_side_is_cut_and_not_dropped():
    short, cut, floor = _pure("""
      const w = t => t.length * 7;   // a text 7px a character
      const long = "Deep Learning (Goodfellow, Bengio, Courville)";
      return [fitHead("Statistica", " · 89", 165, w), fitHead(long, " · 149", 165, w), fitHead(long, " · 149", 10, w)];
    """)
    assert short == "Statistica · 89"
    assert cut.endswith("… · 149") and len(cut) * 7 <= 165
    assert floor == "Deep …" + " · 149"   # past the floor it stands as it is, and paintNames decides


@needs_node
def test_a_flow_reads_left_to_right_and_a_tall_level_takes_more_columns():
    out = _pure("""
      const ids = n => Array.from({length: n}, (_, i) => "u" + i);
      return flowColumns([{depth: 1, ids: ids(20)}, {depth: 0, ids: ["root"]}, {depth: -1, ids: ["a", "b"]}], 1);
    """)
    lay = out["layout"]
    assert lay["root"] == [0, 0]
    assert lay["a"][0] < 0 < lay["u0"][0] < lay["u19"][0], "20 notes take two columns"
    assert [h["depth"] for h in out["heads"]] == [-1, 0, 1]
    assert out["heads"][2]["n"] == 20


@needs_node
def test_a_click_finds_the_nearest_drawn_link_and_prefers_a_written_one():
    got = _pure("""
      const at = id => ({ a: {x: 0, y: 0}, b: {x: 100, y: 0}, c: {x: 0, y: 2}, d: {x: 100, y: 2} })[id];
      const written = {from: "a", to: "b", type: "EXTRACTED"}, near = {from: "c", to: "d", type: "SIMILAR"};
      const hit = (e, y) => { const l = nearestLink(e, {x: 50, y}, 5, at); return l && l.type; };
      return [hit([written, near], 1.2), hit([near], 1.2), hit([{...written, _hidden: true}], 0),
              hit([{...written, _dim: true}], 0), hit([written], 9)];
    """)
    assert got == ["EXTRACTED", "SIMILAR", None, None, None]


@needs_node
def test_a_lit_set_lights_itself_and_no_neighbours():
    got = _pure("""
      const nodes = [{id: "a"}, {id: "b"}, {id: "c"}], keep = {from: "a", to: "b"}, other = {from: "b", to: "c"};
      markOnly(["a", "b"], keep, nodes, [keep, other]);
      return [nodes.map(n => n._dim), keep._dim, other._dim];
    """)
    assert got == [[False, False, True], False, True]


def test_a_cold_3d_layout_does_not_start_flat_on_the_2d_one():
    """A 2D cache hit writes z = 0, and d3 reseeds a note only when a coordinate
    is missing: the 3D layout stayed flat, and a layout that holds kept it so."""
    assert "if (!seeded && !is2D()) RAW_NODES.forEach(n => { delete n.z; });" in FRAME


def test_the_key_is_what_the_size_says():
    assert "const isKey = n => (n.links || 0) >= KEY_MIN;" in FRAME
    assert "_links[Math.floor(_links.length * 0.9)]" in FRAME


def test_size_is_links_counted_once_and_a_native_note_counts_for_nothing():
    import networkx as nx

    from silica.kernel.report.overlays import size_by_links

    G = nx.Graph([("a", "b"), ("a", "c"), ("a", "p"), ("a", "a")])
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "p", "type": "plan", "size": 16},
             {"id": "g", "type": "ghost", "size": 10}, {"id": "lone"}]
    size_by_links(nodes, G)
    by = {n["id"]: n for n in nodes}
    assert by["a"]["links"] == 2 and by["a"]["size"] == 23
    assert by["b"]["links"] == 1 and by["lone"]["links"] == 0 and by["lone"]["size"] == 16
    assert "links" not in by["p"] and by["g"]["size"] == 10
    # the radius goes with the root: 51 links draw 2.9 times the radius of 2
    assert math.isclose(math.sqrt(16 + 3.5 * 51) / math.sqrt(16 + 3.5 * 2), 2.9, abs_tol=0.05)


def test_a_link_says_the_line_it_is_written_on(client, tmp_vault):
    tmp_vault.note("A.md", "---\nrelated:\n  - \"[[C]]\"\n---\n# A\n\nThe proof leans on [[B]] for its lemma. A second one.\n")
    tmp_vault.note("B.md", "Nothing here.\n")
    tmp_vault.note("C.md", "Nothing here.\n")
    tmp_vault.note("D.md", "| x |\n|---|\n| [[A]] |\n")
    get = lambda a, b: client.get("/link", params={"from": a, "to": b}).json()  # noqa: E731
    assert get("A.md", "B.md") == {"path": "A.md", "line": 7, "text": "The proof leans on B for its lemma."}
    # a frontmatter link has a line too, counted in the file the drawer opens
    assert get("A.md", "C.md")["line"] == 3 and get("A.md", "C.md")["text"].endswith("C")
    # one edge per pair: the line may be in the other end
    assert get("B.md", "A.md")["path"] == "A.md"
    # a table is not a passage and it is still where the link is written
    assert get("D.md", "A.md")["line"] == 3


def test_a_body_line_outranks_the_same_link_in_the_frontmatter(client, tmp_vault):
    tmp_vault.note("A.md", "---\nrelated:\n  - \"[[B]]\"\n---\nIt rests on [[B]].\n")
    tmp_vault.note("B.md", "x\n")
    out = client.get("/link", params={"from": "A.md", "to": "B.md"}).json()
    assert out == {"path": "A.md", "line": 5, "text": "It rests on B."}


def test_a_link_no_line_names_and_a_path_out_of_the_vault_answer_empty(client, tmp_vault):
    tmp_vault.note("A.md", "No link at all.\n")
    tmp_vault.note("B.md", "None here either.\n")
    empty = {"path": "", "line": 0, "text": ""}
    assert client.get("/link", params={"from": "A.md", "to": "B.md"}).json() == empty
    assert client.get("/link", params={"from": "../../etc/passwd", "to": "B.md"}).json() == empty
    assert client.get("/link").json() == empty
