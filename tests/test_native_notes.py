# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0070: a note a Silica feature runs on (a Learn plan, an event note) is
a diamond in the graph, drawn with its links, and none of them is counted:
no orphan, hub, betweenness, area or area name moves because of one.
"""
from __future__ import annotations

import math

from silica.kernel.recall import daily_layer, embed, graph_export
from tests.test_daily_layer import _Store
from tests.test_zotero_graph_frame import STATIC, _pure, needs_node

PLAN = "---\ntype: plan\nscope: folder\ntarget: Psicologia/\n---\n\n# Prova Learn\n\n- [[A]] · [[B]] · [[C]]\n"
SYLLABUS = "---\ntype: syllabus\n---\n\n# Old plan\n"
EVENT = "---\ntitle: Esame\nevent_start: 2026-11-10 09:00\nAI: true\n---\n\nRipassare [[A]].\n"
CONCEPT = "---\nAI: true\ntags:\n  - x\n---\n\n# A\n"


def _vault(tmp_path, monkeypatch):
    for name, text in {"Plans/Prova Learn.md": PLAN, "Plans/Old.md": SYLLABUS,
                       "calendar/Esame.md": EVENT, "A.md": CONCEPT, "B.md": "b",
                       "C.md": "c"}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(tmp_path))
    return tmp_path


def test_a_plan_and_an_event_are_typed_and_a_note_silica_wrote_is_not(tmp_path, monkeypatch):
    vault = _vault(tmp_path, monkeypatch)
    nodes = [{"id": p, "type": "note"} for p in
             ("Plans/Prova Learn.md", "Plans/Old.md", "calendar/Esame.md", "A.md", "B.md")]
    graph_export.stamp_native(nodes, vault)
    got = {n["id"]: n["type"] for n in nodes}
    assert got == {"Plans/Prova Learn.md": "plan", "Plans/Old.md": "plan",
                   "calendar/Esame.md": "event", "A.md": "note", "B.md": "note"}


def test_a_native_note_and_its_links_are_not_in_the_counted_graph():
    nodes = [{"id": "p.md", "type": "plan"}, {"id": "e.md", "type": "event"},
             {"id": "a.md", "type": "note"}, {"id": "b.md", "type": "note"}]
    edges = [{"from": "p.md", "to": "a.md", "type": "EXTRACTED"},
             {"from": "e.md", "to": "b.md", "type": "EXTRACTED"},
             {"from": "a.md", "to": "b.md", "type": "EXTRACTED"}]
    G = graph_export.edge_graph(nodes, edges)
    assert set(G.nodes()) == {"a.md", "b.md"}
    assert set(G.edges()) == {("a.md", "b.md")}


def _plan_star():
    """A plan linking every note of a vault whose own links form two pairs."""
    grey = graph_export._NODE_DEFAULT_COLOR   # what build_graph_data gives every node
    notes = [{"id": f"{x}.md", "label": x, "type": "note", "path": f"{x}.md", "size": 16,
              "color": dict(grey)} for x in "abcd"]
    plan = {"id": "Plans/P.md", "label": "P", "type": "plan", "path": "Plans/P.md", "size": 16,
            "color": dict(grey)}
    edges = [{"id": f"p{x}", "from": "Plans/P.md", "to": f"{x}.md", "type": "EXTRACTED"}
             for x in "abcd"]
    edges += [{"id": "e0", "from": "a.md", "to": "b.md", "type": "EXTRACTED"},
              {"id": "e1", "from": "c.md", "to": "d.md", "type": "EXTRACTED"}]
    return [plan, *notes], edges


def test_the_export_draws_the_plan_and_counts_none_of_its_links(monkeypatch, tmp_path):
    import silica.ui.web.graph_view as gv

    nodes, edges = _plan_star()
    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    monkeypatch.setattr(graph_export, "build_graph_data", lambda folder="": (nodes, edges))
    monkeypatch.setattr(graph_export, "knn_edges", lambda nodes, k=6: [])
    seen = {}
    real_render = gv.render_html

    def spy(nodes, edges, *a, **kw):
        seen["nodes"] = {n["id"]: n for n in nodes}
        seen["edges"] = edges
        return real_render(nodes, edges, *a, **kw)

    monkeypatch.setattr(gv, "render_html", spy)
    gv.export_graph(output_path=str(tmp_path / "g.html"))
    plan = seen["nodes"]["Plans/P.md"]
    # Drawn, with every link it writes...
    assert sum(1 for e in seen["edges"] if e.get("from") == "Plans/P.md") == 4
    # ...in no area, at the base size, and joining no two areas: without the
    # plan, a-b and c-d are two areas, and with it counted they were one.
    assert plan["group"] == -1 and plan["size"] == 16
    assert plan["color"] == graph_export._NODE_DEFAULT_COLOR
    groups = {seen["nodes"][f"{x}.md"]["group"] for x in "abcd"}
    assert len(groups) == 2 and -1 not in groups
    # The area is named after one of its notes, never after the plan.
    hubs = graph_export.area_hubs(graph_export.area_tally(list(seen["nodes"].values()), seen["edges"]))
    assert hubs and "Plans/P.md" not in hubs.values()


def test_the_report_counts_no_plan_and_no_note_it_alone_links(monkeypatch):
    from silica.kernel.report.graph_report.compute import compute_report

    nodes, edges = _plan_star()
    r = compute_report(_nodes_edges_override=(nodes, edges), _mtimes_override={})
    # a and c have no in-link of their own: the plan's do not rescue them, and
    # the plan, which nobody links to, is not an orphan either.
    assert sorted(r.orphans) == ["a.md", "c.md"]
    assert r.totals["notes"] == 4


def test_whole_note_knn_never_offers_a_native_note(monkeypatch):
    monkeypatch.setattr(embed, "get_store", lambda: _Store({
        "Plans/P": [1.0, 0.0, 0.0], "e": [1.0, 0.0, 0.0],
        "a": [1.0, 0.1, 0.0], "b": [0.0, 1.0, 0.0],
    }))
    monkeypatch.setattr(daily_layer, "block_store", lambda: _Store({}))
    nodes = [{"id": "Plans/P.md", "type": "plan"}, {"id": "e.md", "type": "event"},
             {"id": "a.md", "type": "note"}, {"id": "b.md", "type": "note"}]
    edges = graph_export.knn_edges(nodes, k=3)
    assert edges and not [e for e in edges if {e["from"], e["to"]} & {"Plans/P.md", "e.md"}]
    # ...and is not an unembedded note the zone gate should count against.
    assert graph_export.semantic_coverage(nodes, edges) == (2, 2)


@needs_node
def test_a_native_note_is_a_diamond_of_the_disc_area(tmp_path):
    out = _pure(tmp_path, """
      const log = [];
      const ctx = new Proxy({}, {get: (_, k) => (...a) => log.push([k, ...a])});
      const res = {};
      for (const type of ["plan", "event"]) {
        log.length = 0;
        markPath(ctx, {type, x: 0, y: 0}, 6);
        res[type] = log.slice();
      }
      res.edge = [markEdge({type: "plan"}, 6, 1, 0), markEdge({type: "plan"}, 6, Math.SQRT1_2, Math.SQRT1_2)];
      res.shape = [shapeOf({type: "plan"}), shapeOf({type: "event"}), shapeOf({type: "note"})];
      console.log(JSON.stringify(res));
    """)
    for type in ("plan", "event"):
        pts = [c[1:3] for c in out[type] if c[0] in ("moveTo", "lineTo")]
        assert len(pts) == 4 and any(c[0] == "closePath" for c in out[type])
        # A square on its corner: four equal sides, the disc's area, centred.
        sides = [math.dist(pts[i], pts[(i + 1) % 4]) for i in range(4)]
        assert max(sides) - min(sides) < 1e-9
        assert math.isclose(sides[0] ** 2, math.pi * 36, rel_tol=1e-9)
        assert math.isclose(sum(p[0] for p in pts), 0, abs_tol=1e-9)
        assert math.isclose(sum(p[1] for p in pts), 0, abs_tol=1e-9)
        assert any(p[0] == 0 and p[1] < 0 for p in pts), "a vertex points up"
    d = 6 * math.sqrt(math.pi / 2)
    assert math.isclose(out["edge"][0], d, rel_tol=1e-9)              # to the vertex
    assert math.isclose(out["edge"][1], d / math.sqrt(2), rel_tol=1e-9)  # to the side
    assert out["shape"][0] == out["shape"][1] == 4 and out["shape"][2] == 0


@needs_node
def test_a_plans_links_rescue_no_orphan_and_ring_no_plan(tmp_path):
    got = _pure(tmp_path, """
      const byId = {"P.md": {type: "plan"}, "a.md": {type: "note"}};
      const linkInDeg = countLinkIns([
        {from: "P.md", to: "a.md", type: "EXTRACTED"},
        {from: "a.md", to: "b.md", type: "EXTRACTED"},
        {from: "a.md", to: "b.md", type: "SIMILAR"},
      ], byId);
      const HUB_MIN = 0.5;
      console.log(JSON.stringify([linkInDeg,
        nodeState({id: "P.md", type: "plan"}),
        nodeState({id: "E.md", type: "event"}),
        nodeState({id: "a.md", type: "note"})]));
    """, "nodeState")
    assert got == [{"b.md": 1}, "note", "note", "orphan"]


def _js() -> str:
    return (STATIC / "graph-frame.js").read_text(encoding="utf-8")


def test_3d_cuts_the_diamond_from_the_squares_sphere():
    js = _js()
    start = js.index("const SHAPE_VAL")
    shape_val = js[start:js.index("};", start)]
    assert "plan: SQUARE_VAL" in shape_val and "event: SQUARE_VAL" in shape_val
    assert "uSilShape.x > 3.5" in js, "the shader has no diamond branch"


def test_the_key_names_the_diamond_only_where_a_native_note_is():
    from silica.ui.web.graph_view import render_html

    plan = {"id": "Plans/P.md", "label": "P", "type": "plan", "path": "Plans/P.md",
            "size": 16, "color": {"background": "#545d67"}}
    note = {"id": "a.md", "label": "a", "type": "note", "path": "a.md", "size": 16,
            "color": {"background": "#545d67"}}
    with_plan = render_html([plan, note], [], [], title="g", lib_js="/*JS*/")
    without = render_html([note], [], [], title="g", lib_js="/*JS*/")
    assert 'class="dia"' in with_plan and "Silica notes" in with_plan
    assert 'class="dia"' not in without


def test_a_source_leaf_is_typed_source_and_still_counted(monkeypatch):
    """A leaf is a document Silica converted, not a note Silica runs on: the
    paper's square, which already means "a source", and every count as before."""
    monkeypatch.setattr("silica.config.CONFIG.write_dir", "", raising=False)
    assert graph_export._infer_type("sources/Kafka.md") == "source"
    assert graph_export._infer_type("Inbox/Kafka.md") == "inbox"
    G = graph_export.edge_graph([{"id": "sources/K.md", "type": "source"},
                                 {"id": "a.md", "type": "note"}],
                                [{"from": "a.md", "to": "sources/K.md", "type": "EXTRACTED"}])
    assert set(G.edges()) == {("a.md", "sources/K.md")}


@needs_node
def test_a_source_leaf_is_the_papers_square(tmp_path):
    got = _pure(tmp_path, """
      const log = [];
      const ctx = new Proxy({}, {get: (_, k) => (...a) => log.push([k, ...a])});
      markPath(ctx, {type: "source", x: 0, y: 0}, 6);
      console.log(JSON.stringify([shapeOf({type: "source"}), log.map(c => c[0])]));
    """)
    assert got == [1, ["beginPath", "rect"]]
