"""Areas held on discs placed from the layout on screen, drawn as blobs (ADR-0074).

All of it is the frame's: the discs start where each area already is, so the
server has nothing to place. The pure functions run under node: where the discs
go, the force that holds a note on its disc, which lines pull, the field that
draws a blob, and the ribbons between two areas.
"""
from __future__ import annotations

import json

from silica.kernel.recall import graph_export as ge
from tests.test_zotero_graph_frame import _pure, needs_node


def test_the_frame_ships_the_areas_it_can_hold_and_no_placement():
    import silica.ui.web.graph_view as gv
    from tests.test_graph_frame import _island

    nodes = [{"id": f"{g}/{i}.md", "label": f"{g}{i}", "type": "note", "group": g, "path": f"{g}/{i}.md"}
             for g, s in ((0, 3), (1, 2), (2, 1)) for i in range(s)]
    comms = [ge.Community(id=g, label=str(g), color="#888", size=s) for g, s in ((0, 3), (1, 2), (2, 1))]
    html = gv.render_html(nodes, [], comms, lib_js="// x")
    data = json.loads(_island(html))
    assert set(data["comm_hubs"]) == {"0", "1"}   # a one-note area is no blob
    assert "area_discs" not in data
    assert 'id="cb-areas"' in html and 'id="cb-areas" checked' not in html   # off when it opens


# --- the frame half, run under node -------------------------------------------

_DISCS = """
  const gapOf = (P, a, b) => Math.hypot(P[a][0] - P[b][0], P[a][1] - P[b][1]) - P[a][2] - P[b][2];
  // The first build's golden spiral, as a start that is no one's layout.
  const spiral = sizes => {
    const ids = Object.keys(sizes).sort((a, b) => sizes[b] - sizes[a] || a - b), start = {}, radius = {};
    let acc = 0;
    ids.forEach((g, k) => {
      radius[g] = 0.62 * Math.sqrt(sizes[g]); acc += radius[g] ** 2;
      const rad = k ? 1.6 * Math.sqrt(acc) : 0, t = k * Math.PI * (3 - Math.sqrt(5));
      start[g] = [rad * Math.cos(t), rad * Math.sin(t)];
    });
    return [start, radius];
  };
"""


@needs_node
def test_no_two_discs_overlap_and_each_keeps_the_gap(tmp_path):
    v = _pure(tmp_path, _DISCS + """
      const [start, radius] = spiral({0: 40, 1: 30, 2: 22, 3: 12, 4: 9, 5: 5, 6: 3, 7: 2});
      const inter = [["0", "1", 2], ["0", "2", 2], ["1", "3", 2], ["4", "6", 1]];
      // On one line too, the start that settles slowest.
      const line = {};
      for (const g in start) line[g] = [0.1 * g, 0];
      const worst = P => {
        let w = Infinity;
        for (const a in P) for (const b in P) if (a < b) w = Math.min(w, gapOf(P, a, b));
        return w;
      };
      const P = placeDiscs(start, radius, inter);
      console.log(JSON.stringify([Object.keys(P).length, worst(P), worst(placeDiscs(line, radius, inter))]));
    """)
    n, spread, line = v
    assert n == 8 and spread >= 0.3 - 1e-6 and line >= 0.3 - 1e-6


@needs_node
def test_a_wikilink_between_two_areas_brings_their_discs_to_touching(tmp_path):
    """Every disc ends up packed against the big ones, so "closer than the
    unlinked" is not a property; touching where they otherwise would not is."""
    v = _pure(tmp_path, _DISCS + """
      const [start, radius] = spiral({0: 40, 1: 40, 2: 5, 3: 5});
      const apart = placeDiscs(start, radius, []), linked = placeDiscs(start, radius, [["2", "3", 3]]);
      console.log(JSON.stringify([gapOf(apart, "2", "3"), gapOf(linked, "2", "3")]));
    """)
    apart, linked = v
    assert linked < 0.3 * 1.05 + 0.05 < apart


@needs_node
def test_each_area_gathers_where_it_already_is(tmp_path):
    """Switching the areas on gathers each area in place: the discs keep the
    layout's arrangement, the left area stays left and the top one on top."""
    v = _pure(tmp_path, """
      const start = {0: [-20, 0], 1: [20, 0], 2: [0, -20], 3: [0, 20]};
      const radius = {0: 2, 1: 2, 2: 2, 3: 2};
      const P = placeDiscs(start, radius, []);
      console.log(JSON.stringify([P[0][0] < P[1][0], P[2][1] < P[3][1],
                                  Math.max(...Object.values(P).map(p => Math.hypot(p[0], p[1])))]));
    """)
    left_left, top_top, reach = v
    assert left_left and top_top
    assert reach < 10   # the gravity packed what started 20 out


@needs_node
def test_hold_pulls_a_member_to_its_disc_and_a_loose_note_beside_its_neighbours(tmp_path):
    v = _pure(tmp_path, """
      const discs = {"3": [2, 0, 1]};
      const m = {x: 0, y: 0, vx: 0, vy: 0, group: 3};
      const far = {x: 40, y: 0}, near = {x: 60, y: 0};
      const loose = {x: 0, y: 30, vx: 0, vy: 0, group: -1, _loose: true, _follow: [far, near]};
      const inside = {x: 20, y: 3, vx: 0, vy: 0, group: -1, _loose: true, _follow: [{x: 20, y: 3}]};
      const ghost = {x: 0, y: 0, vx: 0, vy: 0, group: -1};
      holdAreas([m, loose, inside, ghost], discs, 10, 1);
      console.log(JSON.stringify([m.vx, m.vy, loose.vx, loose.vy, inside.vx, inside.vy, ghost.vx]));
    """)
    m_vx, m_vy, l_vx, l_vy, in_vx, in_vy, ghost = v
    assert m_vx > 0 and m_vy == 0                  # toward x = 2 * 10
    assert l_vx > 0 and l_vy < 0                   # toward (50, 0), the mean of its links
    assert in_vy > 0 and abs(in_vx) < in_vy        # out of the disc at (20, 0), radially
    assert ghost == 0                              # its links place it


_IN_FILL = """
  const inFill = (geo, x, y) => geo.fill.some(p => {
    let c = false;
    for (let i = 0, j = p.length - 2; i < p.length; j = i, i += 2) {
      const yi = p[i + 1], yj = p[j + 1];
      if ((yi > y) !== (yj > y) && x < (p[j] - p[i]) * (y - yi) / (yj - yi) + p[i]) c = !c;
    }
    return c;
  });
"""


@needs_node
def test_a_lone_note_s_blob_ends_where_its_field_falls_to_tau(tmp_path):
    """One note: f = (1 - d^2/R^2)^2 meets tau at d = R sqrt(1 - sqrt(tau))."""
    v = _pure(tmp_path, _IN_FILL + """
      const R = 100, tau = 0.35, d = R * Math.sqrt(1 - Math.sqrt(tau));
      const [geo] = blobGeometry([{x: 0, y: 0, k: 0}], R, tau);
      console.log(JSON.stringify([inFill(geo, 0.9 * d, 0), inFill(geo, 0, -0.9 * d),
                                  inFill(geo, 1.1 * d, 0), geo.line.length > 0]));
    """)
    assert v == [True, True, False, True]


@needs_node
def test_two_areas_share_a_border_at_most_and_never_overlap(tmp_path):
    v = _pure(tmp_path, _IN_FILL + """
      const R = 100, pts = [];
      for (let i = 0; i < 6; i++) pts.push({x: 20 * i, y: 10 * (i % 2), k: 0});
      for (let i = 0; i < 6; i++) pts.push({x: 150 + 20 * i, y: 10 * (i % 2), k: 1});
      const [a, b] = blobGeometry(pts, R, 0.35);
      let both = 0, onlyA = 0, onlyB = 0;
      for (let x = -150; x <= 400; x += 7) for (let y = -120; y <= 120; y += 7) {
        const ia = inFill(a, x, y), ib = inFill(b, x, y);
        if (ia && ib) both++; else if (ia) onlyA++; else if (ib) onlyB++;
      }
      console.log(JSON.stringify([both, onlyA, onlyB, inFill(a, 40, 5), inFill(b, 200, 5)]));
    """)
    both, only_a, only_b, a_home, b_home = v
    assert both == 0 and only_a > 0 and only_b > 0 and a_home and b_home


@needs_node
def test_held_a_similar_line_pulls_only_inside_one_area(tmp_path):
    """Measured on the test vault 2026-10-07: with every k-NN line pulling,
    5 of 23 areas settled 11-20 disc radii off their disc. With the areas off
    (no discs) every line keeps d3's default."""
    v = _pure(tmp_path, """
      const discs = {"0": [0, 0, 1], "1": [5, 0, 1]};
      const a = {group: 0}, b = {group: 0}, c = {group: 1}, loose = {group: -1};
      const links = [
        {type: "SIMILAR", source: a, target: b}, {type: "SIMILAR", source: a, target: c},
        {type: "SIMILAR", source: c, target: loose}, {type: "EXTRACTED", source: a, target: c},
      ];
      const held = heldLinkStrength(discs), free = heldLinkStrength(null);
      console.log(JSON.stringify([links.map((l, i) => held(l, i, links)), links.map((l, i) => free(l, i, links))]));
    """)
    (inside, across, to_loose, wikilink), free = v
    # Every other line keeps d3's default, 1 / the line count of its less busy end.
    assert inside == 1 and across == 0 and to_loose == 0   # min(a: 3, b: 1)
    assert wikilink == 1 / 3                                # min(a: 3, c: 3)
    assert free == [1, 1 / 3, 1, 1 / 3]


@needs_node
def test_held_a_plan_moves_toward_its_notes_and_never_moves_them(tmp_path):
    """ADR-0070 draws a plan's links and counts none of them. Pulling, they
    carried the 3 notes of one area 12.6 disc radii off their disc (test
    vault, 2026-10-07): the plan has the most lines, so d3 moves the notes."""
    v = _pure(tmp_path, """
      const note = {type: "note", group: 0, x: 10, y: 0}, other = {type: "note", group: 0, x: 30, y: 0};
      const plan = {type: "plan", group: -1, x: 0, y: 0, vx: 0, vy: 0, _follow: [note, other]};
      const links = [{type: "EXTRACTED", source: plan, target: note}];
      const s = heldLinkStrength({"0": [0, 0, 1]})(links[0], 0, links);
      holdAreas([plan], {"0": [0, 0, 1]}, 10, 1);
      console.log(JSON.stringify([s, plan.vx, plan.vy]));
    """)
    strength, vx, vy = v
    assert strength == 0
    assert vx > 0 and vy == 0   # toward x = 20, the mean of the two notes


@needs_node
def test_a_ribbon_stands_for_three_lines_or_more_between_two_areas(tmp_path):
    v = _pure(tmp_path, """
      const pairs = [[0, 1], [1, 0], [0, 1], [2, 3], [2, 3], [4, 4], [4, 4], [4, 4]];
      console.log(JSON.stringify([ribbonPairs(pairs), keptInside({"0": [0, 0, 1]}, "EXTRACTED", 0, 0),
                                  keptInside({"0": [0, 0, 1]}, "SIMILAR", 0, 0),
                                  keptInside({"0": [0, 0, 1]}, "EXTRACTED", 0, 1)]))
    """)
    ribbons, wikilink_inside, knn_inside, wikilink_across = v
    assert ribbons == [["0", "1", 3]]   # both directions count; 2 lines and one area make none
    assert wikilink_inside and not knn_inside and not wikilink_across
