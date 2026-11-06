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

    server._reset_session()
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

    from silica.ui.web.graph_view import _size_by_links

    G = nx.Graph([("a", "b"), ("a", "c"), ("a", "p"), ("a", "a")])
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "p", "type": "plan", "size": 16},
             {"id": "g", "type": "ghost", "size": 10}, {"id": "lone"}]
    _size_by_links(nodes, G)
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
