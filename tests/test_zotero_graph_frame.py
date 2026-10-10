# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 "The graph": a paper is a square, a citation an arrow, a missing
paper a layer that is off until asked for, and a Zotero collection a scope.

The frame's own drawing is verified by opening it; what is pinned here is what
the server hands the frame, the HUD rows, and the frame's pure functions (the
block between the `frame-pure` markers), run under node.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from silica.sources import zotero as z
from tests.zotero_fake import FakeZotero

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _island(html: str) -> dict:
    start = html.index('<script id="graph-data"')
    start = html.index(">", start) + 1
    return json.loads(html[start:html.index("</script>", start)])


def _input(html: str, cb: str) -> str:
    """The <input> tag of one HUD checkbox."""
    at = html.index(f'id="{cb}"')
    return html[html.rindex("<input", 0, at):html.index(">", at)]


def _count(html: str, cb: str) -> str:
    row = html[html.index(f'id="{cb}"'):]
    row = row[row.index('<span class="ct">') + len('<span class="ct">'):]
    return row[:row.index("<")]


def _paper(i: int, **extra) -> dict:
    return {"id": f"sources/@p{i}.md", "label": f"Author{i} 2020", "type": "paper", "group": 0,
            "path": f"sources/@p{i}.md",
            "paper": {"zotero_key": f"PPPP000{i}", "title": f"Paper {i}", **extra}}


MISSING = {"id": "__missing__arxiv:2303.08774", "type": "missing", "group": -1, "path": "",
           "label": "arXiv 2303.08774", "title": "GPT-4 technical report",
           "quote": "GPT-4 technical report", "href": "https://arxiv.org/abs/2303.08774",
           "cited_by": 5}
NOTE = {"id": "Concepts/Memory.md", "label": "Memory", "type": "note", "group": 0,
        "path": "Concepts/Memory.md"}


def _cites(a: str, b: str) -> dict:
    return {"id": f"c{a}{b}", "from": a, "to": b, "type": "CITES"}


# --- the HUD -------------------------------------------------------------------

def test_a_paper_vault_shows_citations_on_and_missing_papers_off():
    """Citations are in the source, so they are on; a missing paper is not in
    the vault at all, so its layer has to be asked for."""
    import silica.ui.web.graph_view as gv

    edges = [_cites("sources/@p0.md", "sources/@p1.md"), _cites("sources/@p0.md", MISSING["id"])]
    html = gv.render_html([_paper(0), _paper(1), MISSING, NOTE], edges, lib_js="// x")

    assert "checked" in _input(html, "cb-cites")
    assert "checked" not in _input(html, "cb-missing")
    assert _count(html, "cb-cites") == "1"       # paper to paper; the other reaches a missing one
    assert _count(html, "cb-missing") == "1"


def test_a_vault_without_papers_has_neither_row():
    import silica.ui.web.graph_view as gv

    html = gv.render_html([NOTE], [], lib_js="// x")

    assert 'id="cb-cites"' not in html and 'id="cb-missing"' not in html


def test_a_missing_paper_is_not_counted_as_a_note():
    import silica.ui.web.graph_view as gv

    html = gv.render_html([_paper(0), _paper(1), MISSING, NOTE], [], lib_js="// x")

    assert '<div class="val">3</div><div class="lbl">Notes</div>' in html


# --- what export_graph hands the frame -------------------------------------------

def _leaves(vault) -> None:
    for i in range(2):
        vault.note(f"sources/@p{i}.md",
                   f"---\nzotero_key: PPPP000{i}\ntitle: \"Paper {i}\"\nauthors:\n- \"Author{i}, A\"\n"
                   f"year: 2020\narxiv: \"2001.0000{i}\"\n---\n\n# Paper {i}\n\n## References\n\n"
                   + ("[1] arXiv:2001.00001\n" if i == 0 else ""))
    vault.note("Concepts/Memory.md", "# Memory\n\n## Sources\n\n[[sources/@p0]]\n")


def test_collections_are_read_live_from_zotero_at_build(tmp_vault, monkeypatch, tmp_path):
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    fake = FakeZotero()
    item, _ = fake.add_paper()
    mem = fake.collection("03-agents-and-memory")
    graphs = fake.collection("02-graphs")
    fake.clone(item, "PPPP0000", collections=[mem["key"], graphs["key"]])
    fake.clone(item, "PPPP0001", collections=[mem["key"]])
    monkeypatch.setattr(z, "client", lambda: z.Zotero(transport=fake.transport()))
    _leaves(tmp_vault)

    gv.export_graph(output_path=str(tmp_path / "g.html"))
    papers = {n["id"]: n["paper"] for n in _island((tmp_path / "g.html").read_text())["nodes"]
              if n.get("type") == "paper"}

    assert papers["sources/@p0.md"]["collections"] == ["02-graphs", "03-agents-and-memory"]
    assert papers["sources/@p1.md"]["collections"] == ["03-agents-and-memory"]


def test_a_closed_zotero_leaves_the_collections_out(tmp_vault, monkeypatch, tmp_path):
    """conftest points the client at a closed port: the build still succeeds
    and no paper carries a membership it could not read."""
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    _leaves(tmp_vault)

    res = gv.export_graph(output_path=str(tmp_path / "g.html"))
    papers = [n for n in _island((tmp_path / "g.html").read_text())["nodes"] if n.get("type") == "paper"]

    assert res["success"] and len(papers) == 2
    assert all("collections" not in n["paper"] for n in papers)


def test_citations_stay_out_of_the_partitions_and_betweenness(tmp_vault, monkeypatch, tmp_path):
    import silica.kernel.recall.graph_export as ge
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    seen: dict[str, set] = {}
    real_detect, real_metrics = ge.detect_communities, ge.canvas_metrics

    def detect(nodes, edges, *a, **k):
        seen["communities"] = {e.get("type") for e in edges}
        return real_detect(nodes, edges, *a, **k)

    def metrics(nodes, edges, *a, **k):
        seen["betweenness"] = {e.get("type") for e in edges}
        return real_metrics(nodes, edges, *a, **k)

    monkeypatch.setattr(ge, "detect_communities", detect)
    monkeypatch.setattr(ge, "canvas_metrics", metrics)
    _leaves(tmp_vault)

    gv.export_graph(output_path=str(tmp_path / "g.html"))
    data = _island((tmp_path / "g.html").read_text())

    assert any(e["type"] == "CITES" for e in data["edges"])
    assert "CITES" not in seen["communities"] and "CITES" not in seen["betweenness"]


# --- the frame's pure functions, run --------------------------------------------

def _pure(tmp_path, body: str, *functions: str):
    """The pure block, plus any named top-level function of the frame (cut at
    its closing brace), then `body`."""
    src = (STATIC / "graph-frame.js").read_text(encoding="utf-8")
    # blob-geometry.js is the frame's too: graph_view inlines it ahead of it.
    pure = (STATIC / "blob-geometry.js").read_text(encoding="utf-8") + "\n" \
        + src[src.index("// frame-pure:start"):src.index("// frame-pure:end")]
    for fn in functions:
        start = src.index(f"function {fn}(")
        pure += "\n" + src[start:src.index("\n}\n", start) + 3]
    script = tmp_path / "pure.js"
    script.write_text(pure + "\n" + body + "\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_a_paper_is_a_square_of_the_disc_area_it_replaces(tmp_path):
    """Size is betweenness on both shapes, so the square keeps the disc's
    area: an inscribed one would read 36% smaller at the same value."""
    calls = _pure(tmp_path, """
      const log = [];
      const ctx = new Proxy({}, {get: (_, k) => (...a) => log.push([k, ...a])});
      const out = {};
      for (const type of ["note", "paper", "missing"]) {
        log.length = 0;
        markPath(ctx, {type, x: 0, y: 0}, 6);
        out[type] = log.slice();
      }
      console.log(JSON.stringify(out));
    """)
    assert [c[0] for c in calls["note"]] == ["beginPath", "arc"]
    [rect] = [c for c in calls["paper"] if c[0] == "rect"]
    assert math.isclose(rect[3] * rect[4], math.pi * 6 ** 2, rel_tol=1e-9)
    # A broken square restarts its dash on every side, so each corner is drawn.
    assert [c[0] for c in calls["missing"]].count("moveTo") == 4


@needs_node
def test_the_scope_picker_reaches_collections_and_papers_only(tmp_path):
    got = _pure(tmp_path, """
      const p = {type: "paper", group: 0, paper: {collections: ["02-graphs"]}};
      const bare = {type: "paper", group: 0, paper: {}};
      const m = {type: "missing", group: -1};
      const n = {id: "Concepts/x.md", type: "note", group: 0};
      const under = (scope, node) => { SCOPE = scope; return inScope(node); };
      const col = {kind: "collection", ids: ["02-graphs"]};
      const pap = {kind: "papers"};
      console.log(JSON.stringify([
        under(col, p), under(col, bare), under(col, m), under(col, n),
        under(pap, p), under(pap, m), under(pap, n),
        under({kind: "folder", prefix: "Concepts/"}, n), under(null, n),
      ]));
    """.replace("const p =", "let SCOPE = null;\n      const p ="), "inScope")
    assert got == [True, False, False, False, True, True, False, True, True]


@needs_node
def test_an_arrowhead_touches_the_side_of_the_square_it_arrives_at(tmp_path):
    """On an axis a square's edge is nearer than the disc's, at a corner
    farther: a tip at the disc radius would float off one and sink into the
    other."""
    got = _pure(tmp_path, """
      const d = Math.SQRT1_2;
      console.log(JSON.stringify([
        markEdge({type: "note"}, 6, 1, 0), markEdge({type: "paper"}, 6, 1, 0),
        markEdge({type: "paper"}, 6, d, d), markEdge({type: "missing"}, 6, 0, -1),
      ]));
    """)
    half = 6 * math.sqrt(math.pi) / 2
    assert got[0] == 6
    assert math.isclose(got[1], half) and math.isclose(got[3], half)
    assert math.isclose(got[2], half * math.sqrt(2))


@needs_node
def test_the_tooltip_carries_the_full_title_and_a_missing_papers_reference(tmp_path):
    got = _pure(tmp_path, """
      console.log(JSON.stringify([
        paperTip({type: "paper", label: "Xu 2025", paper: {title: "A-MEM <agentic> memory"}}),
        paperTip({type: "missing", quote: "OpenAI. GPT-4", href: "https://arxiv.org/abs/2303.08774",
                  cited_by: 5}),
        paperTip({type: "note"}),
      ]));
    """)
    paper, missing, note = got
    assert "A-MEM &lt;agentic&gt; memory" in paper
    assert "OpenAI. GPT-4" in missing and "arxiv.org/abs/2303.08774" in missing and "5" in missing
    assert note == ""


# --- the frame's wiring ----------------------------------------------------------

def _js() -> str:
    return (STATIC / "graph-frame.js").read_text(encoding="utf-8")


def test_both_renderers_draw_the_square():
    js = _js()
    assert "markPath(ctx, n, r" in js[js.index("function drawNode"):], "2D paints the disc only"
    assert "uSilShape" in js and "discard" in js, "3D has no square cut"
    v3 = next(ln for ln in js.splitlines() if ln.startswith("const nodeVal3D"))
    assert "SHAPE_VAL" in v3, (
        "the 3D square is cut from a sphere it is inscribed in, so a paper's sphere has to grow")


def test_a_per_node_uniform_reaches_the_node_drawn_after_one_of_its_colour():
    """three re-uploads a material's uniforms only when the material changes,
    and a paper shares its community's material with every note of that area."""
    js = _js()
    step = js[js.index("function nodeRingStep"):]
    assert "renderer.state.useProgram(null)" in step[:step.index("\n}\n")]


def test_citations_carry_an_arrowhead_in_both_renderers():
    """Drawn by the frame, not by the bundles: 3d-force-graph's arrow is a cone
    mesh and a draw call each, which TestBigVaultPerfKnobs keeps out."""
    js = _js()
    assert '.linkCanvasObjectMode(l => l.type === "CITES" ? "after" : null)' in js
    assert ".linkCanvasObject(drawCiteArrow)" in js
    write = js[js.index("function writeLinkSegPositions"):js.index("function linkSegStep")]
    assert "LinkSeg.heads" in write and "Graph.camera()" in write, (
        "the 3D head rides the merged buffer and turns with the camera")
    assert "linkDirectionalArrowLength" not in js


def test_the_frame_reads_both_rows_and_hides_a_missing_papers_citations_with_it():
    js = _js()
    flt = js[js.index("function updateEdgeFilter"):]
    assert 'getElementById("cb-cites")' in flt and 'getElementById("cb-missing")' in flt
    comp = js[js.index("function computeFilters"):js.index("function applyFilters")]
    assert '"missing"' in comp and "showMissing" in comp
    assert '"CITES"' in comp and "showCites" in comp
