# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0062 Decision 7: a daily note is a triangle of the disc's area, it never
rings as orphan, and the key names it."""
from __future__ import annotations

import math

from tests.test_zotero_graph_frame import STATIC, _pure, needs_node


@needs_node
def test_a_daily_note_is_an_equilateral_triangle_of_the_disc_area(tmp_path):
    calls = _pure(tmp_path, """
      const log = [];
      const ctx = new Proxy({}, {get: (_, k) => (...a) => log.push([k, ...a])});
      markPath(ctx, {type: "daily", x: 0, y: 0}, 6);
      console.log(JSON.stringify(log));
    """)
    pts = [c[1:3] for c in calls if c[0] in ("moveTo", "lineTo")]
    assert len(pts) == 3 and any(c[0] == "closePath" for c in calls)
    (ax, ay), (bx, by), (cx, cy) = pts
    area = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / 2
    assert math.isclose(area, math.pi * 36, rel_tol=1e-9)
    sides = [math.dist(pts[i], pts[(i + 1) % 3]) for i in range(3)]
    assert max(sides) - min(sides) < 1e-9
    # Centred on the node, apex up (canvas y grows downward).
    assert math.isclose(sum(p[0] for p in pts), 0, abs_tol=1e-9)
    assert math.isclose(sum(p[1] for p in pts), 0, abs_tol=1e-9)
    assert min(pts, key=lambda p: p[1])[0] == 0


@needs_node
def test_nobody_links_to_a_day_so_it_never_rings_as_orphan(tmp_path):
    got = _pure(tmp_path, """
      const linkInDeg = {};
      const HUB_MIN = 0.5;
      console.log(JSON.stringify([
        nodeState({id: "daily/2026-09-18.md", type: "daily"}),
        nodeState({id: "daily/2026-09-19.md", type: "daily", betweenness: 0.9}),
        nodeState({id: "x.md", type: "note"}),
      ]));
    """, "nodeState")
    assert got == ["note", "hub", "orphan"]


def _js() -> str:
    return (STATIC / "graph-frame.js").read_text(encoding="utf-8")


def test_3d_cuts_the_triangle_from_its_circumsphere():
    js = _js()
    v3 = next(ln for ln in js.splitlines() if ln.startswith("const nodeVal3D"))
    assert "SHAPE_VAL" in v3, "the triangle's sphere has to grow to its circumradius"
    assert "uSilShape.x > 2.5" in js, "the shader has no triangle branch"


def test_the_key_names_the_triangle_only_where_a_day_is():
    from silica.ui.web.graph_view import render_html

    day = {"id": "daily/2026-09-18.md", "label": "2026-09-18", "type": "daily",
           "path": "daily/2026-09-18.md", "size": 16, "color": {"background": "#545d67"}}
    note = {"id": "a.md", "label": "a", "type": "note", "path": "a.md", "size": 16,
            "color": {"background": "#545d67"}}
    with_day = render_html([day, note], [], [], title="g", lib_js="/*JS*/")
    without = render_html([note], [], [], title="g", lib_js="/*JS*/")
    assert 'class="tri"' in with_day and "daily notes" in with_day
    assert 'class="tri"' not in without
