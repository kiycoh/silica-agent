// Embedded in the web app's iframe: the app's own sidebar (stats/search/tree)
// replaces the internal one; only the graph + HUD legend remain.
if (window.parent !== window) document.body.classList.add("embedded");

// Everything the server decided for this document rides one JSON island, read
// here and nowhere else. Not JS literals spliced into this file: the code is
// then a static file a linter can parse and a test can run, and the data is the
// one thing per document that is not (ADR-0026). The island is parsed, not
// evaluated, so a value can never be code.
const DATA = JSON.parse(document.getElementById("graph-data").textContent);

// The papers on the rim and their COVERS edges (ADR-0066) are kept apart from
// the notes. Every count, the warmup budget, the layout cache, the search and
// the state legend below read RAW_NODES and RAW_EDGES, and none of them may
// move because a library was read; only the renderer, the scope and the focus
// see the rim. The rim rides at the END of ALL_NODES, which is what lets
// rimless() hand the forces the notes alone without renumbering them.
const isRim = n => n.type === "outside";
const RAW_NODES = DATA.nodes.filter(n => !isRim(n));
const RAW_EDGES = DATA.edges.filter(e => e.type !== "COVERS");
const RIM_NODES = DATA.nodes.filter(isRim);
const COVER_EDGES = DATA.edges.filter(e => e.type === "COVERS");
const ALL_NODES = RAW_NODES.concat(RIM_NODES);
const ALL_EDGES = RAW_EDGES.concat(COVER_EDGES);
// Hidden, a rim paper waits pinned at the notes' centre, inside any fit.
const pinHome = n => { n.x = n.fx = 0; n.y = n.fy = 0; n.z = n.fz = 0; };
RIM_NODES.forEach(pinHome);
const COMM_LABELS = DATA.comm_labels;
const COMM_HUBS = DATA.comm_hubs || {};   // area -> hub note: the name the /shape surfaces use
// The semantic partition: node.sgroup -> zone. Disjoint from node.group in every
// way that matters — different edges, different ids, different colours.
const ZONES = DATA.zones;
// Only the label is indexed. There was a ZONE_COLOR map beside it, read by
// nodeColor alone; now that the notes keep their community colour it has no
// reader left — the hulls and the zone names take z.color straight off ZONES.
const ZONE_LABEL = {};
ZONES.forEach(z => { ZONE_LABEL[z.id] = z.label; });
// The zone's hue for the live floor. Both rides on the zone (see Zone in
// graph_export) because the phase shift that keeps zone i off community i is
// declared there, and recomputing it here would be a second place to break it.
const zoneColor = z => (LIGHT && z.color_paper) || z.color;

const outDeg = {}, inDeg = {};
RAW_EDGES.forEach(e => {
  outDeg[e.from] = (outDeg[e.from] || 0) + 1;
  inDeg[e.to]   = (inDeg[e.to]   || 0) + 1;
});

const NODE_BY_ID = {};
ALL_NODES.forEach(n => { NODE_BY_ID[n.id] = n; });

// Every link a note has, whatever is on show: the held layout's follow force
// (_follow) reads this, since a hidden link still places a note in its area.
const neighbors = {};
ALL_EDGES.forEach(e => {
  (neighbors[e.from] = neighbors[e.from] || new Set()).add(e.to);
  (neighbors[e.to]   = neighbors[e.to]   || new Set()).add(e.from);
});
// What a focus reads: the edges themselves, so litSet can ask each one whether
// the filters draw it (ADR-0088). Covers included: focusing a paper lights the
// notes that cover it while the rim shows (ADR-0066 "Consequences").
const incident = {};
ALL_EDGES.forEach(e => {
  (incident[e.from] = incident[e.from] || []).push(e);
  (incident[e.to]   = incident[e.to]   || []).push(e);
});

let focusIds = [];  // the focused set; [] = nothing focused
// The arrangement (ADR-0088): {root, layout: {id: [x, y, cost]}, wedges, rIn,
// centre} while a single-note focus is laid out as the Map, else null.
let ARR = null;

// Highlight a SET of nodes and their 1-hop neighbours; dim everything else.
// A set, not one id: the context drawer lights every note carrying a concept,
// and a single-node focus is just the one-element case.
//
// Split in two on purpose: computeFocus only writes the _dim flags onto the
// shared node/link objects, applyFocus also repaints. Those objects outlive the
// renderer instance, so a fresh one picks the flags up in its first graphData()
// digest — which is what lets a rebuild compute and skip the repaint. The
// repaint is not free (see refreshPaint).
function computeFocus(ids) {
  focusIds = (ids == null ? [] : [].concat(ids)).filter(id => NODE_BY_ID[id]);
  paintDim();
  updateFocusBar();
}

// The lit set on screen: the hovered note's while the pointer rests on one,
// else the focus's (ADR-0088 amendment). Since a click arranges, the hover is
// where the old click's look went, and it is that look exactly (markDim).
function paintDim() {
  markDim(hoverId ? [hoverId] : focusIds, incident, ALL_NODES, ALL_EDGES);
}

// A hover is a look, not a focus: focusIds, the banner and an arrangement stay
// as they are, and the pointer leaving puts the focus's own lighting back.
// While notes or the camera move (an arrangement opening or closing) a note
// slides under a still pointer every frame, and each one would relight the
// graph: the hover waits until the motion is over (hoverQuietUntil).
let hoverId = null, hoverWant = null, hoverTimer = 0, hoverQuietUntil = 0;
// 3D re-runs the node digest on every repaint (refreshPaint), so passing over a
// cluster would pay it once per note crossed: there the pointer has to rest.
const HOVER_REST_3D = 90;
function hoverNote(n) {
  hoverWant = n && !n._hidden ? n.id : null;
  clearTimeout(hoverTimer);
  const wait = Math.max(hoverQuietUntil - performance.now(), is2D() ? 0 : HOVER_REST_3D);
  if (wait > 0) hoverTimer = setTimeout(paintHover, wait);
  else paintHover();
}
function paintHover() {
  if (hoverWant === hoverId) return;
  hoverId = hoverWant;
  paintDim();
  if (Graph) refreshPaint();
}

// --- The banner: say out loud that you are not looking at the whole vault ---
// Two mechanisms hide notes, the scope and the focus dim, and neither used
// to announce itself. Both land here, so one line covers both, and it names the
// way out (Esc) next to the reason it is needed. Embedded, the app's scope
// picker already names the scope above the picture, so only the standalone
// frame says it here.
function updateFocusBar() {
  const bar = document.getElementById("focus-bar");
  const parts = [];
  // Notes off is the loudest filter of the three: with the zones off too it
  // empties the frame outright, and an empty frame that says nothing reads as a
  // broken view rather than a chosen one.
  if (!showNotes) parts.push(showZones ? "<b>zones only</b>" : "<b>notes hidden</b>");
  if (SCOPE && SCOPE.kind === "area" && window.parent === window) {
    const n = RAW_NODES.filter(x => x.group === SCOPE.id).length;
    parts.push("area <b>" + escHtml(COMM_HUBS[SCOPE.id] || "#" + SCOPE.id) + "</b> · " + n + " notes");
  }
  if (focusIds.length) {
    const lit = litSet(focusIds, incident);
    const nearby = Math.max(0, lit.size - focusIds.length);
    // One note focused: its name is already on screen above this bar, in the
    // drawer header. Repeating it here is noise, so only a set says what it is.
    const head = focusIds.length === 1 ? "" : "<b>" + focusIds.length + " notes</b> ";
    parts.push(head + "+ " + nearby + " neighbour" + (nearby === 1 ? "" : "s"));
  }
  bar.innerHTML = parts.length
    ? parts.join(" · ") + ' <span class="esc">· Esc to clear</span>' : "";
  bar.style.display = parts.length ? "block" : "none";
}

// frame-pure:start
// Everything from here to frame-pure:end reads its arguments and nothing else,
// so tests/test_zotero_graph_frame.py runs it under node, where there is no DOM.
const escHtml = s => String(s).replace(/[&<>]/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

// --- A paper is a square (ADR-0055) -----------------------------------------
// Colour is the community, size is betweenness and the rings are hub, orphan
// and cut vertex, so shape is the one channel left. The square keeps the AREA
// of the disc it replaces (side r * sqrt(pi)): size has to read as betweenness
// across both shapes, and an inscribed square would read 36% smaller at the
// same value. A missing paper is the same square broken, the frame's mark for
// an absence. The values are what the 3D shader reads as uSilShape.x.
// An outside paper (ADR-0066) is a paper too, drawn hollow on the rim. A
// source leaf Zotero does not hold is the same square (ADR-0070): a document
// Silica converted, which is what the square says.
const SQUARE = { paper: 1, missing: 2, outside: 1, source: 1 };
const SQUARE_HALF = Math.sqrt(Math.PI) / 2;   // half-side over the disc radius
// Two dashes a side with a wide gap: a bracket at each corner. Three at the
// 1:3 ratio the absent edges use (DASH_GAP) would leave a 0.96-unit gap on a
// size-16 node, under a pixel at zoom 1, and that reads as a solid outline.
const MISSING_DASHES = 2, MISSING_GAP = 0.4;   // gap as a fraction of the period

// --- A daily note is a triangle (ADR-0062) ----------------------------------
// The shape channel's third mark, for the square's reason: equilateral with
// the AREA of the disc it replaces (side r * sqrt(4 pi / sqrt 3)), so size
// still reads as betweenness. Apex up, centred on the node. TRI_R is its
// circumradius over the disc radius: the sphere the 3D cut is taken from.
const TRIANGLE = 3;   // after the square's two values of uSilShape.x
const TRI_R = Math.sqrt(4 * Math.PI / (3 * Math.sqrt(3)));

// --- A note Silica runs on is a diamond (ADR-0070) --------------------------
// A Learn plan or an event note: drawn with its links, none of them counted.
// The square on its corner, so the disc's area again and the square's own
// circumsphere in 3D; DIA_R is its half-diagonal over the disc radius. The
// value is the hover's word for it.
const NATIVE = { plan: "Learn plan", event: "event note" };
const DIAMOND = 4;
const DIA_R = Math.sqrt(Math.PI / 2);
const shapeOf = n => SQUARE[n.type] || (n.type === "daily" ? TRIANGLE : NATIVE[n.type] ? DIAMOND : 0);

// The outline of n's mark for a disc of radius r: the fill, and each ring
// around it at a larger r. A broken square is four subpaths because a dash
// pattern restarts on every subpath, so each corner gets ink; one closed rect
// carries the phase round the corners and breaks some of them and not others.
function markPath(ctx, n, r) {
  ctx.beginPath();
  if (n.type === "daily") {
    const R = r * TRI_R, w = R * Math.sqrt(3) / 2;
    ctx.moveTo(n.x, n.y - R);
    ctx.lineTo(n.x + w, n.y + R / 2);
    ctx.lineTo(n.x - w, n.y + R / 2);
    ctx.closePath();
    return;
  }
  if (NATIVE[n.type]) {
    const d = r * DIA_R;
    ctx.moveTo(n.x, n.y - d);
    ctx.lineTo(n.x + d, n.y);
    ctx.lineTo(n.x, n.y + d);
    ctx.lineTo(n.x - d, n.y);
    ctx.closePath();
    return;
  }
  if (!SQUARE[n.type]) { ctx.arc(n.x, n.y, r, 0, 2 * Math.PI); return; }
  const h = r * SQUARE_HALF;
  if (n.type !== "missing") { ctx.rect(n.x - h, n.y - h, 2 * h, 2 * h); return; }
  const c = [[-h, -h], [h, -h], [h, h], [-h, h]];
  for (let i = 0; i < 4; i++) {
    ctx.moveTo(n.x + c[i][0], n.y + c[i][1]);
    ctx.lineTo(n.x + c[(i + 1) % 4][0], n.y + c[(i + 1) % 4][1]);
  }
}

// How far from a mark's centre its edge lies along the unit direction (ux,
// uy): where a citation's arrowhead puts its tip. Exact for the square, whose
// edge is nearer on an axis than at a corner, so the tip touches the side it
// arrives at instead of stopping short of it or sinking into a corner.
function markEdge(n, r, ux, uy) {
  if (NATIVE[n.type]) return r * DIA_R / Math.max(Math.abs(ux) + Math.abs(uy), 1e-9);
  return SQUARE[n.type] ? r * SQUARE_HALF / Math.max(Math.abs(ux), Math.abs(uy), 1e-9) : r;
}

// What a paper or a missing paper adds to its hover. The label is the
// reference-list form ("Xu 2025"), so the title has to live somewhere, and
// here is where a reader looks for it. A missing paper has no title offline,
// only the reference as the first citing paper wrote it.
function paperTip(n) {
  if (n.type === "paper") return n.paper && n.paper.title ? "<i>" + escHtml(n.paper.title) + "</i>" : "";
  // A rim paper names its collections here: an arc too short for its name
  // leaves this as the only place they are said (ADR-0066 decision 3).
  if (n.type === "outside") {
    const p = n.paper || {};
    const k = (n.covers || []).length;
    const said = n.covered == null ? "covers not computed yet"
      : k ? "covered by " + new Set(n.covers.map(c => c[0])).size + " notes" : "no note here covers it";
    return (p.title ? "<i>" + escHtml(p.title) + "</i>" : "") +
      "<i>" + escHtml((p.collections || []).join(" · ")) + " · " + said + "</i>";
  }
  // A diamond says which of Silica's notes it is, and why its links are drawn
  // and not counted (ADR-0070).
  if (n.type === "source") return "<i>source document, as converted</i>";
  if (NATIVE[n.type]) return "<i>" + NATIVE[n.type] + " · its links count toward no area, size or orphan</i>";
  if (n.type !== "missing") return "";
  return (n.quote ? "<i>" + escHtml(n.quote) + "</i>" : "") +
    "<i>cited by " + (n.cited_by || 0) + " library papers · " +
    escHtml((n.href || "").replace(/^https:\/\//, "")) + "</i>";
}

// --- The rim (ADR-0066 decision 3) ------------------------------------------
// One arc per scoped collection, in the menu's order (a plain sort, as the
// picker's), clockwise from twelve o'clock with a gap between arcs. A paper in
// several scoped collections sits in the first of them, and inside an arc the
// covered papers come first, so the lines leave from one end of it. Angles are
// in radians from twelve o'clock: x = cx + R sin t, y = cy - R cos t.
const RIM_GAP = 0.05;   // between two arcs: a break, wider than any paper's slot at 271
function rimLayout(papers, ids) {
  const order = [...ids].sort();
  const groups = new Map(order.map(c => [c, []]));
  const covered = p => !!(p.covers && p.covers.length);
  for (const p of papers) {
    const cols = (p.paper && p.paper.collections) || [];
    const c = order.find(x => cols.includes(x));
    if (c !== undefined) groups.get(c).push(p);
  }
  const keys = order.filter(c => groups.get(c).length);
  const total = keys.reduce((n, c) => n + groups.get(c).length, 0);
  const gap = keys.length > 1 ? RIM_GAP : 0;
  const usable = 2 * Math.PI - gap * keys.length;
  const at = {}, arcs = [];
  let a = 0;
  for (const c of keys) {
    const g = groups.get(c).sort((x, y) => (covered(y) - covered(x)) ||
      String(x.label).localeCompare(String(y.label)));
    const span = usable * g.length / total;
    g.forEach((p, j) => { at[p.id] = a + span * (j + 0.5) / g.length; });
    arcs.push({ name: c, a0: a, a1: a + span, n: g.length, covered: g.filter(covered).length });
    a += span + gap;
  }
  return { at, arcs };
}

// The arc's name, and its count once a pass has said which papers are covered.
function arcLabel(arc, pending) {
  return arc.name + " · " + (pending ? arc.n : arc.covered + " of " + arc.n);
}
// Orphan's count (nodeState): in-links over RESOLVED wikilinks, and none from
// a plan or an event (ADR-0070), whose links are drawn and not counted. The
// tree of "Prova Learn" was the only in-link of 10 notes on the test vault.
function countLinkIns(edges, byId) {
  const deg = {};
  edges.forEach(e => {
    if (e.type === "EXTRACTED" && !NATIVE[(byId[e.from] || {}).type]) deg[e.to] = (deg[e.to] || 0) + 1;
  });
  return deg;
}

// --- Areas held on their discs (ADR-0074) -----------------------------------
// The areas are a view asked for, like the zones, and they start from the
// layout on screen: each area's disc is placed where its notes already are
// (placeDiscs), so switching the areas on gathers each area in place instead of
// laying the vault out again. A note is pulled toward its area's centre: the
// charge already spreads an area to about the disc's size, which is what the
// radius was sized on. A plan or an event follows the mean of the notes it
// links (_follow), since its links move nothing (heldLinkStrength). A note in
// no area follows its own neighbours the same way, and is sent to the rim of
// any disc it lands in, so it waits beside the notes it links and outside every
// blob. A ghost is none of these: its links place it.
// Measured on the test vault 2026-10-07 against the first build, which placed
// the discs on a golden spiral and sent the notes in no area to a ring past
// them: 81,419 line crossings against 164,212, and a note moves 7.7 rest
// lengths when the areas come on against 18.5. The ring's 513 lines ran from
// the edge of the frame into the middle.
const HOLD = 0.1, LOOSE_HOLD = 0.06, LOOSE_RIM = 0.35, LOOSE_PUSH = 0.3;
function holdAreas(nodes, discs, unit, alpha) {
  const all = Object.values(discs);
  for (const n of nodes) {
    const d = discs[n.group];
    if (d) {
      n.vx += (d[0] * unit - n.x) * HOLD * alpha;
      n.vy += (d[1] * unit - n.y) * HOLD * alpha;
      continue;
    }
    if (!n._follow || !n._follow.length) continue;
    let x = 0, y = 0;
    for (const m of n._follow) { x += m.x; y += m.y; }
    const k = (n._loose ? LOOSE_HOLD : HOLD) * alpha;
    n.vx += (x / n._follow.length - n.x) * k;
    n.vy += (y / n._follow.length - n.y) * k;
    if (n._loose) for (const q of all) {
      const dx = n.x - q[0] * unit, dy = n.y - q[1] * unit, r = Math.hypot(dx, dy) || 1e-6;
      const rim = (q[2] + LOOSE_RIM) * unit;
      if (r < rim) {
        n.vx += dx / r * (rim - r) * LOOSE_PUSH * alpha;
        n.vy += dy / r * (rim - r) * LOOSE_PUSH * alpha;
      }
    }
  }
}

// Where each area is held, in link rest lengths: graph_export.area_discs's
// relaxation, started from `start` (each area's centroid on screen) instead of
// a golden spiral. Areas a wikilink joins drift to touching, a gravity packs
// the mass toward the origin, and no two discs overlap, `gap` apart. Without
// the gravity the discs kept the free layout's spread and the mass read sparse,
// which is the separation the owner turned down (2.5 rest lengths, 2026-10-07).
// start: {area: [x, y]}, radius: {area: r}, inter: [[a, b, pairs]].
// O(iters x areas^2) on the positions alone: 23 areas run in about a millisecond.
const AREA_GAP = 0.3;
function placeDiscs(start, radius, inter, gap = AREA_GAP, iters = 240) {
  const ids = Object.keys(start), P = {};
  ids.forEach(g => { P[g] = [start[g][0], start[g][1], radius[g]]; });
  const links = inter.filter(([a, b]) => P[a] && P[b]);
  const wmax = Math.max(1, ...links.map(l => l[2]));
  // f > 0 draws the two together by f of their distance, f < 0 parts them;
  // the smaller disc takes the larger share of the move.
  const nudge = (a, b, f) => {
    const dx = b[0] - a[0], dy = b[1] - a[1], wa = b[2] ** 2 / (a[2] ** 2 + b[2] ** 2);
    a[0] += dx * f * wa; a[1] += dy * f * wa;
    b[0] -= dx * f * (1 - wa); b[1] -= dy * f * (1 - wa);
  };
  const separate = () => {
    let moved = false;
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
      const a = P[ids[i]], b = P[ids[j]];
      if (a[0] === b[0] && a[1] === b[1]) b[0] += 1e-3;
      const d = Math.hypot(b[0] - a[0], b[1] - a[1]), need = a[2] + b[2] + gap;
      if (d < need) { nudge(a, b, -(need - d) / d); moved = true; }
    }
    return moved;
  };
  for (let it = 0; it < iters; it++) {
    const cool = 1 - it / iters;
    for (const [ga, gb, w] of links) {
      const a = P[ga], b = P[gb];
      const d = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1e-9, need = a[2] + b[2] + gap;
      if (d > need) nudge(a, b, 0.08 * (0.3 + w / wmax) * (d - need) / d * cool);
    }
    for (const g of ids) { P[g][0] *= 1 - 0.02 * cool; P[g][1] *= 1 - 0.02 * cool; }
    separate();
  }
  // One pass can push a disc into a third one; more passes settle it. Areas
  // starting on one line settle slowest, a chain each pass shifts by a little:
  // 8 of them needed more than 100 passes (2026-10-07). A pass ends the loop
  // as soon as nothing overlaps, so the bound costs nothing on a real layout.
  for (let pass = 0; pass < 1000 && separate(); pass++);
  return P;
}

// Held, a k-NN line pulls only between two notes of one area. Measured on the
// test vault 2026-10-07 with every line pulling: 5 of 23 areas settled 11-20
// disc radii off their disc. A plan's or an event's line pulls nothing either,
// the forces' half of ADR-0070's "drawn and not counted": a plan has the most
// lines, so d3 moved its notes and not the plan, and carried one area 12.6
// radii off its disc. Every other line keeps d3's default, 1 / the line count
// of its less busy end, which a custom strength otherwise replaces; with no
// discs (the areas off) every line keeps it, so the same force serves both.
const pullsInside = (discs, a, b) => !!discs[a] && a === b;
function heldLinkStrength(discs) {
  let seen = null, count = null;
  return (l, i, links) => {
    if (links !== seen) {
      seen = links;
      count = new Map();
      for (const k of links) for (const n of [k.source, k.target]) count.set(n, (count.get(n) || 0) + 1);
    }
    if (discs && l.type === "SIMILAR" && !pullsInside(discs, l.source.group, l.target.group)) return 0;
    if (discs && (NATIVE[l.source.type] || NATIVE[l.target.type])) return 0;
    return 1 / Math.min(count.get(l.source), count.get(l.target));
  };
}

// With the areas on, the blob says which notes belong together, so a line
// inside an area is kept only if it is a wikilink, and every other line is
// quiet: drawn for a focused note only. Between two areas one ribbon stands for
// all their lines. 4,377 lines crossed 164,212 times on the test vault
// (2026-10-07); the wikilinks inside the areas and 50 ribbons cross 2,870
// times. A pair under RIBBON_MIN lines gets no ribbon: its lines show on the
// note, and 87 ribbons at full count read as a second mat.
const RIBBON_MIN = 3;
const keptInside = (discs, type, a, b) => type !== "SIMILAR" && pullsInside(discs, a, b);
// pairs: [[area, area]] of the lines on show. Returns [[a, b, lines]], a < b.
function ribbonPairs(pairs, min = RIBBON_MIN) {
  const m = new Map();
  for (const [a, b] of pairs) {
    if (a === b) continue;
    const key = a < b ? a + "|" + b : b + "|" + a;
    m.set(key, (m.get(key) || 0) + 1);
  }
  return [...m].filter(([, n]) => n >= min).map(([key, n]) => [...key.split("|"), n]);
}

// An area's blob (ADR-0074) is blobGeometry, in blob-geometry.js: the drawer's
// cut figure draws the same blobs (ADR-0075), and graph_view inlines the file
// ahead of this one.

// --- what a focus lights, and the Map it is arranged as (ADR-0088) ----------
// incident: id -> the edges that touch it. An edge lights its far end only
// while the filters draw it, so the lit set is what is on screen. Before the
// first filter pass no edge carries the flag, and absent reads as shown.
function litSet(ids, incident) {
  const lit = new Set(ids);
  for (const id of ids)
    for (const e of incident[id] || [])
      if (!e._hidden) lit.add(e.from === id ? e.to : e.from);
  return lit;
}

// The focus look, on the shared objects: the notes `ids` lights stay, the rest
// dims, and an edge stays lit when EITHER end is in `ids` (neighbour to
// neighbour dims). A click's focus and a hover's look both land here.
function markDim(ids, incident, nodes, edges) {
  const on = new Set(ids);
  const lit = litSet(ids, incident);
  nodes.forEach(n => { n._dim = on.size > 0 && !lit.has(n.id); });
  edges.forEach(e => { e._dim = on.size > 0 && !on.has(e.from) && !on.has(e.to); });
}

// The note under a point, in graph units: the nearest visible one whose hit
// radius (`radius(n)`) reaches it, or null.
function nearestHit(nodes, p, radius) {
  let best = null, bd = Infinity;
  for (const n of nodes) {
    if (n._hidden || n.x == null) continue;
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    if (d <= radius(n) && d < bd) { bd = d; best = n; }
  }
  return best;
}

// One frame of the dim's fade: `f` moves `k` toward 1 (dim) or 0 (lit) and
// stops there. Absent reads as lit, which is where every note starts.
function fadeToward(f, dim, k) {
  const v = f || 0;
  return dim ? Math.min(1, v + k) : Math.max(0, v - k);
}

// `a` taken `t` of the way to `b`, both "#rrggbb". Anything else comes back as
// it was: a colour this cannot mix shows lit until the fade ends, never wrong.
function mixHex(a, b, t) {
  const hex = /^#[0-9a-f]{6}$/i;
  if (!hex.test(a) || !hex.test(b)) return a;
  const p = parseInt(a.slice(1), 16), q = parseInt(b.slice(1), 16);
  const ch = sh => {
    const x = (p >> sh) & 255;
    return Math.round(x + (((q >> sh) & 255) - x) * t);
  };
  return "#" + ((ch(16) << 16) | (ch(8) << 8) | ch(0)).toString(16).padStart(6, "0");
}

// Pins every note the layout names at `centre` plus its offset, on the plane
// through the centre in 3D. `saved` holds where every note stood before the
// FIRST arrangement: re-rooting on an arranged note must not make the arranged
// positions the ones to come back to. Notes the layout does not name are saved
// too, since a running simulation moves them while they are hidden.
function placeArranged(nodes, layout, centre, saved, threeD) {
  for (const n of nodes) {
    if (!saved.has(n.id))
      saved.set(n.id, { x: n.x, y: n.y, z: n.z, fx: n.fx, fy: n.fy, fz: n.fz });
    const p = layout[n.id];
    if (!p) continue;
    n.x = n.fx = centre.x + p[0];
    n.y = n.fy = centre.y + p[1];
    if (threeD) n.z = n.fz = centre.z || 0;
  }
}

// Velocities are zeroed with the positions: a note let go with the speed the
// simulation gave it while pinned would fly off the spot it was put back on.
// `keep`: the force layout stays saved, for a re-root or a return still to play.
function restoreArranged(nodes, saved, keep = false) {
  for (const n of nodes) {
    const s = saved.get(n.id);
    if (!s) continue;
    n.x = s.x; n.y = s.y; n.z = s.z;
    n.fx = s.fx; n.fy = s.fy; n.fz = s.fz;
    n.vx = n.vy = n.vz = 0;
  }
  if (!keep) saved.clear();
}

// boxes in priority order -> keep[i]. Greedy, so an earlier name always wins
// a collision and a later one waits for room (ADR-0069 rule 3). `area`, when
// given, is what the reader can see: a name reaching past it would be cut, so
// it waits too.
function keepLabels(boxes, area) {
  const kept = [];
  return boxes.map(b => {
    const inside = !area || (b.x0 >= area.x0 && b.x1 <= area.x1 && b.y0 >= area.y0 && b.y1 <= area.y1);
    const clear = inside && kept.every(k => b.x0 >= k.x1 || b.x1 <= k.x0 || b.y0 >= k.y1 || b.y1 <= k.y0);
    if (clear) kept.push(b);
    return clear;
  });
}

// The server's Map, made to sit in the graph it opens in (ADR-0088 amendment).
// `home`: id -> where the note stands in the force layout.
// Scale: the server's unit makes the ring about twice the spread its notes came
// from (radius 1013 against a median 470 on "Machine learning"), and laid over
// the graph at that size it covered 195 grey notes. So the ring is ARR_FIT
// times the median distance its notes stood at, between ARR_FIT_MIN and 1 of
// its own size; the framing zooms to it, so on screen it is as large as before.
// Turn: the server's angles say nothing about the picture on screen, and the
// one rotation that best matches where the notes stood (Procrustes, closed
// form) cuts the median trip on "Machine learning" from 681 units to 414.
const ARR_FIT = 1.1, ARR_FIT_MIN = 0.3;
function fitArrangement(layout, wedges, rIn, home, root) {
  const o = home[root] || { x: 0, y: 0 };
  let R = Math.max(rIn, 1, ...wedges.map(w => w.r));
  const dist = [];
  let dot = 0, cross = 0;
  for (const id in layout) {
    const p = layout[id], h = home[id];
    R = Math.max(R, Math.hypot(p[0], p[1]));
    if (id === root || !h) continue;
    const ax = h.x - o.x, ay = h.y - o.y;
    dist.push(Math.hypot(ax, ay, (h.z || 0) - (o.z || 0)));
    dot += p[0] * ax + p[1] * ay;
    cross += p[0] * ay - p[1] * ax;
  }
  dist.sort((a, b) => a - b);
  const med = dist.length ? dist[dist.length >> 1] : R;
  const s = Math.min(R, Math.max(ARR_FIT_MIN * R, ARR_FIT * med)) / R;
  const th = Math.atan2(cross, dot), co = Math.cos(th), si = Math.sin(th);
  const out = {};
  for (const id in layout) {
    const [x, y, ...rest] = layout[id];
    out[id] = [s * (x * co - y * si), s * (x * si + y * co), ...rest];
  }
  return { layout: out, s, th, rIn: rIn * s,
           wedges: wedges.map(w => ({ ...w, r: w.r * s, a0: w.a0 + th, a1: w.a1 + th })) };
}

// The clearing: a note within ARR_CLEAR_OUT radii of the centre is pressed into
// the band from ARR_CLEAR_IN to ARR_CLEAR_OUT, on its own bearing and in its
// own order, so the ring stands on empty ground and the vault around it keeps
// its shape. Past the band nothing moves: measured on the test vault
// (2026-10-11), 25 notes for the median Map, 77 to 122 of 698 around "Machine
// learning" across three 2D settles, 183 in 3D, where more of the vault stands
// in front of the plane. In the plane only: 3D looks at the ring down z, so a
// note in front of it is cleared like one beside it. Returns id -> [x, y].
const ARR_CLEAR_IN = 1.08, ARR_CLEAR_OUT = 1.6;
function clearRing(points, centre, R) {
  const inner = ARR_CLEAR_IN * R, outer = ARR_CLEAR_OUT * R, out = {};
  points.forEach((p, i) => {
    const dx = p.x - centre.x, dy = p.y - centre.y, d = Math.hypot(dx, dy);
    if (d >= outer) return;
    // A note on the centre itself has no bearing: the golden angle spreads them.
    const a = d > 1e-9 ? Math.atan2(dy, dx) : i * 2.399963;
    const r = inner + (outer - inner) * d / outer;
    out[p.id] = [centre.x + r * Math.cos(a), centre.y + r * Math.sin(a)];
  });
  return out;
}

// frame-pure:end

function applyFocus(ids) { computeFocus(ids); refreshPaint(); arrangeFocus(); }

// --- the arrangement (ADR-0088) ---------------------------------------------
// A single-note focus lays out what it lights as the Map. The geometry is the
// server's (POST /map/layout, the builder the .canvas export uses), so it has
// one implementation; the frame fits it to the neighbourhood, pins the notes
// there and clears the ground under it, with the rest of the vault left in
// place and dimmed (the amendment: it used to be hidden). Esc, an empty click
// or closing the note plays the force layout back; a scope, a compare or a mode
// switch puts it back at once. A set focus (a concept's notes) has no centre
// and only dims.
const ARR_SAVED = new Map();   // where every note stood before the first one
let arrSeq = 0;                // a newer focus drops the answer to an older one

const arrangeable = n => !!n && !!n.path && !isRim(n) && n.type !== "ghost" && n.type !== "missing";

function arrangeFocus() {
  const seq = ++arrSeq;
  const root = focusIds.length === 1 ? NODE_BY_ID[focusIds[0]] : null;
  if (!arrangeable(root)) { disarrange(true, true); return; }
  const lit = [...litSet(focusIds, incident)].filter(id => id !== root.id);
  // The same note lit the same way is already on screen (the app echoes a
  // click back as silica-focus-path): a second answer would only refit.
  if (ARR && ARR.root === root.id && Object.keys(ARR.layout).length === lit.length + 1 &&
      lit.every(id => id in ARR.layout)) return;
  const groups = {};
  for (const id of [root.id, ...lit]) {
    const g = NODE_BY_ID[id] && NODE_BY_ID[id].group;
    if (Number.isInteger(g) && g >= 0) groups[id] = g;
  }
  fetch("/map/layout", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root: root.id, ids: lit, groups }),
  })
    .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
    .then(out => { if (seq === arrSeq) applyArrangement(root, out); })
    // Tolerated: a graph exported to a file (silica_graph_export) has no
    // server to ask, and a failed request leaves the focus as it was before
    // ADR-0088, lit and dimmed in place. That is the graph without the Map,
    // not a broken view, so it is said in the console and nowhere else.
    .catch(err => console.warn("arrangement unavailable:", err.message));
}

function applyArrangement(root, out) {
  // Around the root where it stands now, so re-rooting on an arranged note
  // moves the picture as little as it can.
  const centre = { x: root.x || 0, y: root.y || 0, z: root.z || 0 };
  // Sized and turned by where its notes stand in the force layout, not by
  // where the last ring put them.
  const home = {};
  for (const id in out.nodes) {
    const h = ARR_SAVED.get(id) || NODE_BY_ID[id];
    if (h) home[id] = h;
  }
  const fit = fitArrangement(out.nodes, out.wedges || [], out.r_in || 0, home, root.id);
  ARR = { root: root.id, layout: fit.layout, wedges: fit.wedges, rIn: fit.rIn, centre };
  const from = positionsNow();
  restoreArranged(ALL_NODES, ARR_SAVED, true);   // a re-root lets the last clearing go
  placeArranged(ALL_NODES, ARR.layout, centre, ARR_SAVED, !is2D());
  const around = ALL_NODES.filter(n => !(n.id in ARR.layout) && !n._hidden && n.x != null);
  const cleared = clearRing(around, centre, arrRadius());
  for (const n of around) {
    const p = cleared[n.id];
    if (!p) continue;
    n.x = n.fx = p[0]; n.y = n.fy = p[1];
    if (!is2D()) n.fz = n.z;
  }
  applyFilters();
  playFrom(from);
  frameArrangement();
}

// `refilter` false: the caller runs the filters itself right after (a scope,
// a compare) or a rebuild reads the flags (a mode switch). `play`: slide back
// instead of jumping, for a return the reader asked for.
function disarrange(refilter = true, play = false) {
  arrSeq++;   // an answer still in flight is for a focus that is gone
  // The saved layout outlives ARR while a return is playing: a scope arriving
  // mid-slide still has to put it back.
  if (!ARR && !ARR_SAVED.size) return;
  ARR = null;
  ALL_NODES.forEach(n => { if (n.__threeObj) n.__threeObj.scale.setScalar(1); });
  const settle = () => {
    restoreArranged(ALL_NODES, ARR_SAVED);
    measureGraphRadius();
    if (Graph) pushPositions();
  };
  if (play && Graph) {
    const from = positionsNow();
    restoreArranged(ALL_NODES, ARR_SAVED, true);
    playFrom(from, settle);
  } else {
    stopPlay();
    settle();
  }
  if (refilter) applyFilters();
}

// The move, played: every note slides from where it stood (`from`) to where
// it stands now. On the way it is pinned at each step, so a simulation still
// running cannot pull it off the path; at the end it gets the pins it was
// given. A jump read as the picture being swapped for another one.
const ARR_PLAY_MS = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 650;
let playRaf = 0;
function positionsNow() {
  const at = new Map();
  for (const n of ALL_NODES) if (n.x != null) at.set(n.id, [n.x, n.y, n.z || 0]);
  return at;
}
function stopPlay() { cancelAnimationFrame(playRaf); playRaf = 0; }
function playFrom(from, done) {
  stopPlay();
  const moves = [];
  for (const n of ALL_NODES) {
    const a = from.get(n.id);
    if (!a || (a[0] === n.x && a[1] === n.y && a[2] === (n.z || 0))) continue;
    moves.push([n, a, { x: n.x, y: n.y, z: n.z, fx: n.fx, fy: n.fy, fz: n.fz }]);
  }
  // The camera frames over 700-900 ms while the notes slide: no hover until
  // both have stopped (see hoverNote).
  hoverQuietUntil = performance.now() + Math.max(ARR_PLAY_MS, 900) + 100;
  const t0 = performance.now(), flat = is2D();
  const step = now => {
    const u = ARR_PLAY_MS ? Math.min(1, Math.max(0, now - t0) / ARR_PLAY_MS) : 1;
    const e = u < 0.5 ? 4 * u * u * u : 1 - Math.pow(2 - 2 * u, 3) / 2;
    for (const [n, a, b] of moves) {
      if (u === 1) { Object.assign(n, b); continue; }
      n.x = n.fx = a[0] + (b.x - a[0]) * e;
      n.y = n.fy = a[1] + (b.y - a[1]) * e;
      if (!flat) n.z = n.fz = a[2] + ((b.z || 0) - a[2]) * e;
    }
    pushMeshes();
    if (u < 1) { playRaf = requestAnimationFrame(step); return; }
    playRaf = 0;
    if (done) done();
  };
  // The first step now, not on the next frame: the caller has already put
  // every note at its end, and a paint before the step would show it there.
  step(t0);
  if (Graph) wake(ARR_PLAY_MS + 200);
}

// 3D moves a mesh only inside an engine tick, and a settled graph runs none;
// the 3D wrapper exposes no resetCountdown to give it one (measured: undefined
// on the bundled 3d-force-graph), and a reheat would run the whole layout. So
// the meshes are moved here, through __threeObj, the lib's handle on each
// node's object. The merged links follow n.x on their own (linkSegStep), and
// 2D paints straight from n.x: both need only the frames.
function pushMeshes() {
  if (Graph && !is2D()) ALL_NODES.forEach(n => {
    if (n.__threeObj) n.__threeObj.position.set(n.x || 0, n.y || 0, n.z || 0);
  });
}
function pushPositions() { pushMeshes(); wake(1000); }

// How far the ring reaches from its centre, in graph units: the farthest of
// its notes, its wedges and its hole.
function arrRadius() {
  const { layout, wedges, rIn } = ARR;
  return Math.max(rIn, 1, ...wedges.map(w => w.r),
    ...Object.values(layout).map(p => Math.hypot(p[0], p[1])));
}

// The canvas the reader can see: the key (#hud) and the app's column above it
// stand over the frame's right edge (ADR-0072), so the Map is framed in what
// is left of it. In canvas pixels, from the left edge.
function freeWidth() {
  const W = Graph.width(), hud = document.getElementById("hud");
  const r = hud && hud.offsetParent ? hud.getBoundingClientRect() : null;
  const left = document.querySelector("#graph canvas").getBoundingClientRect().left;
  return r && r.width ? Math.max(1, Math.min(W, r.left - left)) : W;
}

// The ring takes this share of the free width and the names the rest: a name
// is up to ~170px at 12.5px, and at 1400px with the drawer shut the free width
// is ~880px, which leaves 176px a side. Narrower, the outer names wait.
const ARR_RING_W = 0.6, ARR_RING_H = 0.84;
function frameArrangement() {
  if (!ARR) return;
  const { centre: c, layout } = ARR;
  const R = arrRadius();
  const W = Graph.width(), H = Graph.height(), free = freeWidth();
  // pixels per graph unit
  const k = Math.min(free * ARR_RING_W, H * ARR_RING_H) / (2 * R);
  // centre c sits at the middle of the free width, left of the canvas middle
  const dx = (W - free) / 2 / k;
  if (is2D()) {
    Graph.centerAt(c.x + dx, c.y, 700);
    Graph.zoom(k, 700);
  } else {
    // The same scale at the plane's distance: H pixels span 2 D tan(fov/2).
    const tan = Math.tan(((Graph.camera().fov || 50) * Math.PI) / 360);
    const D = H / (2 * k * tan);
    const at = { x: c.x + dx, y: c.y, z: c.z };
    Graph.cameraPosition({ x: at.x, y: at.y, z: c.z + D }, at, 900);
    // A sphere keeps its graph size (cube root of its value, times the
    // bundle's nodeRelSize of 4), a pixel or two from this far: it takes the
    // 2D floor instead, and disarrange gives every mesh its scale back.
    for (const id in layout) {
      const n = NODE_BY_ID[id], o = n && n.__threeObj;
      if (!o) continue;
      const px = id === ARR.root ? ARR_ROOT_PX : ARR_DOT_PX;
      o.scale.setScalar(Math.max(1, px / (k * Math.cbrt(nodeVal3D(n)) * 4)));
    }
  }
  wake(1000);
}

function fitGraph() { Graph.zoomToFit(400, 40); wake(600); }

// --- what the layout is doing, bottom-left ----------------------------------
// Ticks and not seconds: a tick is the layout's own unit, so the same graph
// settles in the same number of them on any machine, while the seconds say more
// about the laptop than about the vault.
//
// The animated tail is counted here; the synchronous warmup is ADDED rather
// than observed, because the bundle runs it as a plain forceLayout.tick() loop
// with no onEngineTick call. Adding the count we passed is exact in both paths
// and not an estimate: a seeded build warms up 0 ticks, and a cold one runs at
// most 240 against a schedule that only reaches ALPHA_MIN at ~300, so the loop's
// own alpha break can never cut it short.
let simTicks = 0, warmupRun = 0;
const gnum = n => n.toLocaleString("en-US");

function paintStat(tail) {
  const el = document.getElementById("gstat-n");
  if (!el) return;
  // The corner keeps the live word; the size moved into the head of Forces
  // (ADR-0072), where the forces it sizes are.
  el.textContent = tail;
  const size = document.getElementById("gsize");
  if (size) size.textContent = gnum(RAW_NODES.length) + " nodes · " + gnum(RAW_EDGES.length) + " edges";
}

// The chip beside fit. It had a reheat chip beside it until 2026-10-06, when
// the user removed it: a reheat is applyForces(true), the perturbation a force
// slider still makes, and this chip finishes what it only starts.
// "settled" above means COLD, not at rest: the alpha
// gate stops the sim at tick ~300 whatever the forces still say, and a reheat
// exposes that residual for 66 ticks and freezes it again a quarter of the way
// down (integrated push 1/FAST_DECAY = 10 against 1/0.0228 = 44 for a cold
// build). Watching a reheat, the nodes visibly "want" to go somewhere and stop.
// This finishes the descent instead of stirring: alpha is HELD where the
// reheat put it, and the tick rule v' = 0.6 (v + alpha F) then walks toward
// its fixed point v = 1.5 alpha F, so the per-node step IS the residual force.
// Same basin, same mental map: this is not a new layout, it is the one the
// reheat kept pointing at.
//
// Measured 2026-09-21 on the 1,195-node test vault. 3D: a reheat after the
// cold build moved every node 100 units on average (max 412); after one
// 600-tick hold the same reheat moved 21, after two 13, after three 14 and a
// second reheat 10: that is the floor, Barnes-Hut noise the reheat itself
// adds. 2D: 117 after the cold build, 15 after one hold, 15 again on a
// second reheat. The hold at alpha ~1 was stable (largest step bounded at 10-20 units,
// no ringing) and descended 3x faster than a hold at 0.3; the value d3's drag
// uses was the safe guess, not the right one. Pairwise distances changed 9%
// (median) across a hold, so the motion is a rearrangement, not a rotation.
//
// The budget is the terminator on a vault this size, not the residual: the
// median step plateaus at ~3 units by tick 300 and stays there while the
// nodes keep sliding along a flat valley (540 units each per 600 ticks, and
// the reheat floor no longer improves). The eps fires only where a layout
// truly converges. One click is one budget; the chip shows the step, and a
// second click continues from where the first left off.
// ponytail: every tick is painted, so a budget costs what the renderer costs:
// measured 11 s in 3D and 13 s in 2D (the half-ratio backing store under
// motion, see the dynamic resolution note, is what keeps 2D near 3D). The
// synchronous path is warmupTicks at graphData() time, or d3-force in a
// worker; take it when the wait is a complaint, the layout is the same.
const RESIDUAL_EPS = 0.5;      // graph units per tick; saveLayout rounds to 1 anyway
const RESIDUAL_BUDGET = 600;   // 2x the 300-tick cold schedule, at 3x its alpha
let residualTicks = 0;         // > 0 while a descent is running

// Median per-node step of the last tick, i.e. the residual force (see above).
// The median and not the max: the max is one hub's Barnes-Hut noise, measured
// 3-10x the median, and would report a settled graph as moving. 2D nodes have
// no vz; `|| 0` keeps the two renderers on one function.
function residual() {
  const st = RAW_NODES.map(n => Math.hypot(n.vx || 0, n.vy || 0, n.vz || 0));
  st.sort((a, b) => a - b);
  return st[st.length >> 1] || 0;
}

function autoLayout() {
  simTicks = 0; warmupRun = 0;
  residualTicks = RESIDUAL_BUDGET;
  paintStat("descending");   // the click answers before the first tick does
  // Same deadlock as a drag (see onNodeDrag): the gate would stop the engine
  // on the settled alpha before the reheat could lift it. descendTick puts it
  // back. Neither bundle links d3AlphaTarget out of its inner kapsule, so the
  // hold is the decay itself: the reheat sets alpha to 1 on the FAST_DECAY
  // schedule and a zero decay then never runs it. (renderBudget paints one
  // frame synchronously inside applyForces, so the hold lands at 0.9 when
  // that frame ticked; both were measured stable.)
  Graph.d3AlphaMin(0);
  applyForces(true);
  Graph.d3AlphaDecay(0);
}

// One tick of a running descent: paint the residual, and when it is under eps
// (or the budget is spent) let alpha cool so the usual settle lands in
// onEngineStop, which caches the polished positions like any other.
function descendTick() {
  if (!residualTicks) return;
  const r = residual();
  paintStat("descending, step " + r.toFixed(2));
  if (r < RESIDUAL_EPS || --residualTicks === 0) {
    residualTicks = 0;
    Graph.d3AlphaDecay(FAST_DECAY).d3AlphaMin(ALPHA_MIN);
  }
}

// Undim AND reframe — the background click means "show me everything again".
function clearFocus() {
  applyFocus(null);
  Graph.zoomToFit(600, 40);
  wake(700);   // the refit is a camera tween; it needs frames to run
}

// Re-pass the colour accessors so the renderer repaints without touching the
// simulation. 2D also needs nodeCanvasObject re-passed — the canvas draw reads
// _dim itself, and force-graph caches nothing per node between frames, so a
// plain redraw suffices there; the re-pass is what schedules it.
//
// Cheap in 2D only. force-graph declares nodeColor/linkColor with
// `triggerUpdate:false`, so the re-pass just raises needsRedraw. 3d-force-graph
// does NOT: it lists nodeColor/nodeVisibility/linkColor/linkVisibility among the
// props that re-run the whole node and link digest, so every re-pass rebuilds
// the material of every node and every link. Never call this when a rebuild is
// about to happen anyway.
function refreshPaint() {
  Graph.nodeColor(Graph.nodeColor());
  // 3D links live in the merged LineSegments: one buffer rewrite against the
  // full link digest (9k material rebuilds) the accessor re-pass would cost.
  // With PARTICLES on the lib still owns the photon carriers, so those keep
  // the re-pass beside the merge.
  if (is2D() || PARTICLES) Graph.linkColor(Graph.linkColor());
  if (!is2D()) repaintLinkSeg();
  wake(120);
  startFade();
}

// The dim, faded (the owner, 2026-10-11): a note leaving the lit set goes dark
// over FADE_MS instead of at once, and one coming back lights the same way.
// `_fade` is what the colours paint; `_dim` stays the decision, and everything
// that is not a colour (names, rings, particles) still reads it at once.
const FADE_MS = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 320;
// A link's colour string is cached per fade step (linkPaint), so the steps are
// what a fade costs in strings: 16 over 320 ms is one per frame at 50 fps.
const FADE_STEPS = 16;
let fadeRaf = 0, fadeAt = 0;
function startFade() {
  if (fadeRaf || !Graph) return;
  fadeAt = performance.now();
  fadeRaf = requestAnimationFrame(fadeStep);
}
function fadeStep(now) {
  const k = FADE_MS ? Math.max(0, now - fadeAt) / FADE_MS : 1;
  fadeAt = now;
  let moving = false;
  for (const list of [ALL_NODES, ALL_EDGES]) for (const o of list) {
    if ((o._fade || 0) === (o._dim ? 1 : 0)) continue;
    o._fade = fadeToward(o._fade, o._dim, k);
    moving = true;
  }
  fadeRaf = 0;
  if (!Graph) return;
  // 2D paints from the accessors and only has to be told; 3D reads a note's
  // fade in its shader on every draw, and the merged links from their buffer.
  if (is2D()) Graph.nodeColor(Graph.nodeColor());
  else repaintLinkSeg();
  wake(200);
  if (moving) fadeRaf = requestAnimationFrame(fadeStep);
}
// A rebuild reads the flags in its first digest with no repaint to fade from.
function snapFade() {
  for (const list of [ALL_NODES, ALL_EDGES]) for (const o of list) o._fade = o._dim ? 1 : 0;
}

// --- the canvas's own palette ----------------------------------------------
// CSS tokens stop at the edge of the canvas: a WebGL material and a 2D
// fillStyle both need a literal, and both are set on a hot path where reading
// getComputedStyle per node is not an option.
//
// So read the token block ONCE, here, into the literals that hot path wants.
// The per-node constraint is unchanged, nothing below this line calls
// getComputedStyle. What changes is who owns the values: graph-frame.css said
// its --g-* neutrals were "declared here so the legend swatches and the
// renderer read one source", and that was simply not true, the renderer kept a
// second set. The second set then missed the whole identity consolidation (see
// docs/specs/visual-identity.md): the canvas was still painting the deleted
// crystal floor #0D0917 while every surface around it had moved to graphite,
// the two neutrals were still blue-violet on a warm paper ground, and
// ghostLabel was still the ash-dim that measured 3.83:1 and got replaced for it.
// A theme fork here is also redundant: the CSS forks on [data-theme="light"]
// already, so one read serves both grounds and there is nothing left to keep in
// sync by hand.
const LIGHT = document.documentElement.dataset.theme === "light";
const TOK = getComputedStyle(document.documentElement);
const tok = (name) => TOK.getPropertyValue(name).trim();
const GP = {
  dim: tok('--g-dim'),           // a node filtered out of focus: toward the floor
  ghost: tok('--g-ghost'),       // unresolved link — the faintest thing the floor holds
  fallback: tok('--g-fallback'), // no community
  label: tok('--frost'), ghostLabel: tok('--ash-dim'),
  linkDim: tok('--g-link-dim'),
  bg: tok('--void'),
  ringHub: tok('--accent'), ringOrphan: tok('--ash-dim'), ringCut: tok('--warn'),
  accent: tok('--accent'), line: tok('--line'),
};
// three.js wants the floor as an int, and only here: one parse of the value
// above rather than a second literal that can disagree with it.
GP.bgHex = parseInt(GP.bg.slice(1), 16);

let showExtracted = true;
let showAmbiguous = false;
let showGaps = true;
let showSimilar = true;
// The three inferred layers, all off: the frame opens on what you WROTE, and
// every layer that is a claim about what you did not write has to be asked for.
let showProposed = false;
// Citations are read off the paper's own references, so they are on; missing
// papers are ids no file carries, so they wait to be asked for (ADR-0055).
let showCites = true;
let showMissing = false;
// The part of the vault on show: {kind: "folder", prefix} | {kind: "area", id} |
// {kind: "zone", id} | {kind: "collection", ids: [names]} | {kind: "papers"} |
// null. A visibility predicate beside the focus dim, never a second build:
// positions stay global, so a folder is seen where it sits among the rest. The
// HUD's area list sets it too (ADR-0058): it used to set a filter of its own
// with the same predicate, and two different areas set there and here
// composed to an empty frame.
let SCOPE = null;
// {cx, cy, R, arcs} while a collection scope draws the rim in 2D, else null.
let RIM = null;
// {note id -> COVERS edges reaching it from the scoped papers} while the rim is
// up: the notes a reader came to the rim to find (see drawNode).
let COVERING = null;
// Two parts side by side: {a: Set, b: Set} or null. Both visible, the rest
// hidden, and the links inside each part faded, so the wikilinks between the
// two (`_ab` on the edge) are the ones left at full weight. Fading and not
// thickening: 2.6 against 1.6 px did not read on a graph that zooms (ADR-0058).
let AB = null;
let showCoupled = false;
let showZones = false;   // the semantic layer is asked for, never assumed
let showNotes = true;    // the macro read: zones alone in the frame

// --- Node color = its community color, flat -------------------------------
// One hue per community: every node in a community shares the exact color,
// hub or leaf. Degree is shown by size, never by washing the hue out.
function nodeColor(n) {
  // Solid darken-to-background dim, decided: rgba() only if visual
  // verification ever shows 3d-force-graph honours per-node alpha. It fades
  // in (fadeStep): 2D mixes toward it by `_fade`. 3D with the flat shader mixes
  // in the shader instead (U_DIM), because a colour per frame here would
  // rebuild the node materials per frame; without that shader the note takes
  // the dim at once, as it always did.
  const c = litColor(n);
  const f = is2D() ? n._fade || 0 : SHADING ? 0 : n._dim ? 1 : 0;
  return f >= 1 ? GP.dim : f > 0 ? mixHex(c, GP.dim, f) : c;
}

function litColor(n) {
  // Neutrals are blue-violet, never gray: the mascot has no gray facet, only
  // unlit ones. Each value below holds the luminance of the gray it replaces.
  if (n.type === 'ghost') return GP.ghost;
  // The vault around the rim (ADR-0066 amendment): the area's hue mixed
  // CTX_MIX of the way toward the floor, solid because 3D takes no alpha.
  if (n._ctx) return n.__ctx || (n.__ctx = towardFloor(baseColor(n), CTX_MIX));
  // The node's colour is the STRUCTURAL community, always, whatever else is on
  // screen. It used to hand the channel over to the semantic partition while
  // the zone layer was up, which read as a bug and was one: ADR-0023 says the
  // two partitions "coexist and never substitute for each other" and "share
  // neither colour key nor id space", and a channel that swaps between them
  // is a substitution by definition. The semantic layer owns the hulls and the
  // names; it does not get to repaint the notes.
  return baseColor(n);
}

function baseColor(n) {
  const c = n.color || {};
  return (LIGHT ? c.background_paper || c.background : c.background) || GP.fallback;
}

// The mock drew the context at 14% opacity on discs of 2.2px or more; at the
// frame's fit a note is under a pixel, where 14% of a hue is nothing. A 2px
// floor (drawNode) and 30% of the hue read as the mock did.
const CTX_MIX = 0.3;
function towardFloor(hex, t) {
  if (!/^#[0-9a-f]{6}$/i.test(hex || "")) return GP.dim;   // no hue to keep a trace of
  const h = parseInt(hex.slice(1), 16), f = GP.bgHex;
  const ch = s => Math.round(((h >> s) & 255) * t + ((f >> s) & 255) * (1 - t));
  return "#" + ((ch(16) << 16) | (ch(8) << 8) | ch(0)).toString(16).padStart(6, "0");
}

// --- Node state = a ring, on a channel colour is not already using ----------
// Nothing new is computed here: betweenness rides on every node already (it is
// what sizes them) and the in-degree comes from the edge list. The states are
// the ones the vault report already names, so the view and the report agree.
//
// Orphan matches graph_report's definition exactly — in-degree zero over
// RESOLVED wikilinks only. Counting SIMILAR would erase the state entirely
// (k-NN gives almost every note an in-edge), which is the opposite of the
// question "is anything in the vault pointing here".
const linkInDeg = countLinkIns(RAW_EDGES, NODE_BY_ID);

// Hub = top decile of the nodes that have any betweenness at all. A fixed
// threshold would call half a dense vault a hub and none of a sparse one;
// the decile asks the same question of both.
const _bets = RAW_NODES.map(n => n.betweenness || 0).filter(b => b > 0).sort((a, b) => a - b);
const HUB_MIN = _bets.length ? _bets[Math.floor(_bets.length * 0.9)] : Infinity;

// Priority is deliberate: a crossing with no backlinks is worth reading as a
// hub, not as an orphan.
function nodeState(n) {
  if (n.type === "ghost") return "ghost";
  // An id, not a note: no wikilink can reach one, so the orphan rule below
  // would ring every missing paper, 73 of them on the founder's library.
  if (n.type === "missing") return "missing";
  // Not counted, so no reading of the counts (ADR-0070): nobody links to a
  // plan, and an orphan ring on one would be an alarm about how Learn works.
  if (NATIVE[n.type]) return "note";
  if ((n.betweenness || 0) >= HUB_MIN) return "hub";
  // Nobody links to a day (ADR-0062): an orphan ring on every daily note would
  // be an alarm about how a journal works, not about the vault.
  if (n.type === "daily") return "note";
  if (!linkInDeg[n.id]) return "orphan";
  return "note";
}

// Only the states with NO channel of their own get a ring. "note" is the
// default, so ringing it would say nothing; ghost already has three markers
// (its own unlit colour, a smaller radius, a dimmer label) and there are 468 of
// them in a 682-note vault — a fourth marker on the most numerous state turns
// the whole view into an alarm about links you have not written yet.
const STATE_RING = { hub: GP.ringHub, orphan: GP.ringOrphan };

// Counts never change (the states come from the exported data), so this runs
// once at load, not per repaint.
function syncStateLegend() {
  const c = { hub: 0, orphan: 0, ghost: 0, note: 0, missing: 0 };
  RAW_NODES.forEach(n => c[nodeState(n)]++);
  Object.keys(c).forEach(k => {
    const el = document.getElementById("st-" + k);
    if (el) el.textContent = c[k];
  });
}
syncStateLegend();

// --- Density-aware forces ---------------------------------------------------
// The lib's d3 defaults (charge -60 in 3D, link distance 30) collapse dense
// graphs into a hairball: equilibrium spacing must grow with avg degree or
// neighborhoods overlap. sqrt keeps sparse graphs (k<=2) exactly as before
// (scale=1) and opens dense ones up to 4x. Sliders multiply on top of this
// baseline, so the auto-scaling stays authoritative as the vault grows.
const AVG_DEG = RAW_NODES.length ? 2 * RAW_EDGES.length / RAW_NODES.length : 0;
const FORCE_SCALE = Math.min(4, Math.max(1, Math.sqrt(AVG_DEG / 2)));
// Same auto-scaled baseline in both modes; only the per-mode constants differ.
// 2D has one dimension fewer to disperse into, so at the same charge the plane
// packs tighter than the sphere: repulsion and rest length are opened up until
// x1 reads the same in both. (Tuned by eye — that IS what "looks right" means.)
const CHARGE_2D_K = 1.8, DIST_2D_K = 1.5;
const baseCharge = () => -60 * FORCE_SCALE * FORCE_SCALE * (is2D() ? CHARGE_2D_K : 1);
const baseDist   = () => 30 * FORCE_SCALE * (is2D() ? DIST_2D_K : 1);
// --- When the layout is allowed to stop ------------------------------------
// A tick COUNT is the wrong gate and was measurably too small. d3 decays alpha
// by a fixed 2.28% per tick regardless of graph size, so it reaches the 0.001
// that d3 itself calls converged at tick 300, always. The old budget
// (100 + min(200, N/10)) gave 155 ticks on a 550-note vault: alpha 0.028, still
// 28x above convergence. The view froze on a layout that was genuinely half
// unfolded — not a perception problem, an arithmetic one.
//
// So hand the gate to the physics. Both bundles already check it and neither
// had it switched on (d3AlphaMin defaults to 0 = disabled):
//
//   ++cntTicks > cooldownTicks || now - startTickTime > cooldownTime
//     || d3AlphaMin > 0 && forceLayout.alpha() < d3AlphaMin   ->  onEngineStop
//
// cooldownTicks and cooldownTime both go to Infinity on purpose. Alpha decays
// deterministically, so it ALWAYS converges; a tick or wall-clock ceiling can
// no longer protect against anything, it can only re-introduce the early cut
// through a second door. (cooldownTime's 15s default would have done exactly
// that on the slower renderer.)
const ALPHA_MIN = 0.001;      // d3's own convergence point, reached at tick ~300

// warmupTicks runs the same tick() in a plain loop with nothing painted, and
// consumes the SAME alpha schedule — the bundle's warmup loop carries the
// d3AlphaMin check too. So warmup and the animated tail split one 300-tick
// budget, and the split is what you actually watch.
//
// Split per renderer, because the two pay completely different prices for a
// painted tick: both libs advance the layout exactly one tick per RENDERED
// frame, and the 2D canvas (553 arcs + labels + 4566 strokes per frame, all
// CPU) runs that at roughly 19 ticks/s against WebGL's 60. An even split makes
// 2D take three times as long to settle for the identical layout. Weighting the
// warmup by the renderer buys back a comparable settle time in both.
//
// Two things the plain `() => is2D() ? 240 : 150` got wrong on a big vault.
//
// It ran a warmup even when the layout was SEEDED. Seeded means FAST_DECAY, and
// FAST_DECAY reaches ALPHA_MIN at tick 66 — inside either count. So the engine
// stopped in the middle of the synchronous warmup, before the renderer had ever
// drawn a frame, and the fit deferred to onEngineStop then measured a scene
// whose meshes were all still sitting at the origin. Camera 41 units from the
// centre of a 3,900-unit graph: the 2D -> 3D switch opening fully zoomed in.
// A cached layout does not need a warmup. It IS the warmup.
//
// And the counts were absolute, while the cost of a tick is not. Every warmup
// tick is a frame the browser cannot paint, and a tick is roughly linear in
// links: measured 8.94ms at 12,232 links (link 2.5 + charge 6.4), so the 240
// tuned at ~4.5k links stopped being ~0.8s of freeze and became ~2.1s. Hold the
// freeze near what it was tuned to be, rather than the number of ticks.
const WARMUP_REF_EDGES = 4566;   // the graph the counts above were tuned on
function WARMUP_TICKS(seeded) {
  if (seeded) return 0;
  const base = is2D() ? 240 : 150;
  const scale = Math.min(1, WARMUP_REF_EDGES / Math.max(1, RAW_EDGES.length));
  return Math.max(30, Math.round(base * scale));
}

// --- Layout cache: pay the 300 ticks once, not once per load ----------------
// The honest gate above costs about twice the ticks of the wrong one. Cached
// positions hand that back from the second load on: seed x/y/z from the last
// settled layout and raise the decay so the sim only has to rerelax, ~66 ticks,
// which the warmup then swallows whole. The graph opens already settled.
//
// One slot per renderer, holding its own fingerprint — a slot per fingerprint
// would accumulate a dead entry for every force-slider position ever dragged.
// The fingerprint covers the node set AND the force multipliers: a layout
// settled under different forces is not this layout.
const LAYOUT_KEY = "silica-graph-layout";
const FAST_DECAY = 0.1;   // alpha 1 -> ALPHA_MIN in ~66 ticks
const COLD_DECAY = 0.0228;   // d3's own: alpha 1 -> ALPHA_MIN in ~300 ticks
const NODE_FP = (() => {
  let h = RAW_NODES.length;
  for (const n of RAW_NODES)
    for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) | 0;
  return h;
})();
const layoutFp = () => NODE_FP + ":" + forceMul.repel.toFixed(3) + ":" +
  forceMul.dist.toFixed(3) + ":" + forceMul.center.toFixed(2);

// True when the positions were seeded, i.e. the sim only has to rerelax.
function loadLayout() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY + "-" + mode)); } catch (e) {}
  if (!saved || saved.fp !== layoutFp() || !saved.pos) return false;
  // Positions are keyed by id, not by index: a vault that gained a note between
  // two loads has a different fingerprint anyway, but by-index would silently
  // scatter every node past the insertion point if that ever stopped holding.
  let hit = 0;
  RAW_NODES.forEach(n => {
    const p = saved.pos[n.id];
    if (!p) return;
    n.x = p[0]; n.y = p[1]; n.z = p[2];
    hit++;
  });
  return hit === RAW_NODES.length;
}

function saveLayout() {
  // The cache holds the layout the frame opens on, and it opens with the areas
  // off: a held layout saved here would open as the free one (ADR-0074).
  if (areasOn()) return;
  // Nor an arrangement (ADR-0088): it is a state of one focus, put back by
  // Esc, and an engine stop lands inside it whenever a tick moves its pins.
  if (ARR) return;
  const pos = {};
  // Rounded to whole graph units: the layout is thousands of units across, the
  // decimals are noise, and they triple the size of what goes into storage.
  RAW_NODES.forEach(n => {
    pos[n.id] = [Math.round(n.x || 0), Math.round(n.y || 0), Math.round(n.z || 0)];
  });
  try {
    localStorage.setItem(LAYOUT_KEY + "-" + mode,
                         JSON.stringify({ fp: layoutFp(), pos: pos }));
  } catch (e) { /* quota or blocked storage -> next load just re-settles */ }
}

// --- 2D labels: node radius + zoom LOD --------------------------------------
// Base size is whatever the smallest real note got (16 today, or 16+40*b once
// betweenness sizing runs); anything above it is a node that stands out, and
// those are the ones worth a label before you have zoomed in.
const BASE_SIZE = RAW_NODES.reduce(
  (m, n) => n.type === "ghost" ? m : Math.min(m, n.size || 16), Infinity);
const NODE_REL_SIZE = 1.5;  // r = sqrt(val) * this — 16 => 6px, a 56 hub => 11px
const nodeRadius = n => Math.sqrt(Math.max(0, n.size || 16)) * NODE_REL_SIZE;

// Circle + text. Below ~0.6 zoom only dots (at that scale the text is a smear
// and the labels outnumber the pixels); 0.6-1.5 the standouts; above 1.5 all of
// them. Font size divides by the zoom so text keeps a constant SCREEN size.
function drawNode(n, ctx, scale) {
  // The vault around an arrangement is drawn as it always is, dimmed.
  if (ARR && ARR.layout[n.id]) { drawArrangedNode(n, ctx, scale); return; }
  if (isRim(n)) { drawRimPaper(n, ctx, scale); return; }
  // A note a scoped paper's line ends on is what the reader came to the rim
  // for, and at the zoom that fits the rim it is a pixel with no name: it
  // keeps a disc of 7px on screen and its label, as the rim keeps its squares.
  const covering = COVERING && COVERING.has(n.id);
  const r = covering ? Math.max(nodeRadius(n), 3.5 / scale)
    : n._ctx ? Math.max(nodeRadius(n), 1 / scale) : nodeRadius(n);
  markPath(ctx, n, r);
  if (n.type === "missing") {
    // Hollow, since nothing is there to fill, and in graph units like the
    // fill it stands in for, so it grows with the zoom as a disc does.
    const p = 2 * r * SQUARE_HALF / (MISSING_DASHES - MISSING_GAP);
    ctx.setLineDash([p * (1 - MISSING_GAP), p * MISSING_GAP]);
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = nodeColor(n);
    ctx.stroke();
    ctx.setLineDash([]);
  } else {
    ctx.fillStyle = nodeColor(n);
    ctx.fill();
  }
  if (covering && !n._dim) { drawNodeLabel(n, ctx, scale, r); return; }
  if (n._dim || n._ctx || scale < 0.6) return;             // dimmed or context: dot only
  // The ring is detail, so it lives behind the same zoom gate as the labels.
  // Drawing it at every scale flooded the zoomed-out view: rings outnumbered
  // the pixels between them and the whole graph read as one alarm.
  // It sits OUTSIDE the disc so the community hue keeps its full area, and both
  // offset and width divide by the zoom to stay constant on screen.
  const ring = STATE_RING[nodeState(n)];
  if (ring) {
    markPath(ctx, n, r + 2 / scale);   // a paper's rings are square like the paper
    ctx.lineWidth = 1.5 / scale;
    ctx.strokeStyle = ring;
    ctx.stroke();
  }
  // A second node channel rather than a fourth state: a cut vertex is usually a
  // hub as well, and nodeState() can only answer with one word. The ring sits
  // OUTSIDE the state ring, so a note that is both shows both readings instead
  // of one overwriting the other.
  //
  // Always on, and it has no toggle. It was off by default on the argument that
  // 128 amber rings on a 709-note vault is an alarm about a normal graph, and
  // that traded the wrong thing away: a reading nobody switches on is a reading
  // nobody has. The ring is 1.2px at the node's own scale and carries no fill,
  // which is what keeps 128 of them a texture rather than an alarm.
  if (n.cut) {
    markPath(ctx, n, r + 4.5 / scale);
    ctx.lineWidth = 1.2 / scale;
    ctx.strokeStyle = GP.ringCut;
    ctx.stroke();
  }
  if (scale < 1.5 && (n.size || 16) <= BASE_SIZE) return;  // mid zoom: standouts
  drawNodeLabel(n, ctx, scale, r);
}

// --- an arrangement, drawn (ADR-0088, after ADR-0069) -----------------------
// The Map fits a ring of dozens of notes into one screen, where a node at its
// graph size is a pixel: a dot keeps 4px on screen, the root 7px and the
// signal ring (ADR-0069 rule 1). The names are drawn after, all together,
// because holding one back depends on every name before it.
const ARR_DOT_PX = 4, ARR_ROOT_PX = 7;
const arrangedRadius = (n, scale) =>
  Math.max(nodeRadius(n), (n.id === ARR.root ? ARR_ROOT_PX : ARR_DOT_PX) / scale);
function drawArrangedNode(n, ctx, scale) {
  const root = n.id === ARR.root;
  const r = arrangedRadius(n, scale);
  markPath(ctx, n, r);
  ctx.fillStyle = nodeColor(n);
  ctx.fill();
  if (!root) return;
  markPath(ctx, n, r + 3 / scale);
  ctx.lineWidth = 2 / scale;
  ctx.strokeStyle = GP.accent;
  ctx.stroke();
}

// An area's hue for its wedge: the colour its notes are painted with, read off
// the first arranged one, so a wedge and its dots cannot disagree.
function wedgeColor(area) {
  if (area < 0) return GP.line;
  for (const id in ARR.layout) {
    const n = NODE_BY_ID[id];
    if (n && n.group === area) return baseColor(n);
  }
  return GP.line;
}

// Each area is a wedge reaching as far as its farthest note, outlined in its
// colour (ADR-0069 decision 5); the spokes divide them. Canvas and the layout
// measure angles the same way (from +x toward +y), so they are used as given.
function drawWedges(ctx, scale) {
  const { centre: c, wedges, rIn } = ARR;
  ctx.save();
  for (const w of wedges) {
    const col = wedgeColor(w.area);
    ctx.beginPath();
    ctx.arc(c.x, c.y, w.r, w.a0, w.a1);
    ctx.arc(c.x, c.y, rIn, w.a1, w.a0, true);
    ctx.closePath();
    ctx.globalAlpha = 0.06;
    ctx.fillStyle = col;
    ctx.fill();
    ctx.globalAlpha = 0.75;
    ctx.lineWidth = 1.5 / scale;
    ctx.strokeStyle = col;
    ctx.beginPath();
    ctx.arc(c.x, c.y, w.r, w.a0, w.a1);
    ctx.stroke();
    if (wedges.length > 1) {
      ctx.globalAlpha = 0.4;
      ctx.lineWidth = 1 / scale;
      ctx.strokeStyle = GP.line;
      ctx.beginPath();
      ctx.moveTo(c.x + rIn * Math.cos(w.a0), c.y + rIn * Math.sin(w.a0));
      ctx.lineTo(c.x + w.r * Math.cos(w.a0), c.y + w.r * Math.sin(w.a0));
      ctx.stroke();
    }
  }
  ctx.restore();
}

// Priority as on the Map: the root, then the ring by how linked each note is,
// then the area names. A name that would collide waits for zoom (keepLabels).
function drawArrangedNames(ctx, scale) {
  const { centre: c, wedges, root } = ARR;
  const items = [];
  const deg = id => (outDeg[id] || 0) + (inDeg[id] || 0);
  const ids = Object.keys(ARR.layout).filter(id => NODE_BY_ID[id])
    .sort((a, b) => (b === root) - (a === root) || deg(b) - deg(a) || (a < b ? -1 : 1));
  for (const id of ids) {
    const n = NODE_BY_ID[id], isRoot = id === root;
    const px = isRoot ? 15 : 12.5;
    const gap = (isRoot ? ARR_ROOT_PX + 6 : ARR_DOT_PX + 4) / scale;
    // The root's name sits under it, centred: every other name is read
    // against it. The rest face outward, away from the root.
    const align = isRoot ? "center" : n.x >= c.x ? "left" : "right";
    const x = isRoot ? n.x : n.x + (align === "left" ? gap : -gap);
    const y = isRoot ? n.y + gap + px / 2 / scale : n.y;
    items.push({ text: n.label, px, x, y, align,
      // A name a hover does not light stays, quieter: the ring keeps its
      // words while the pointer reads one of its notes.
      ink: n._dim || n.type === "ghost" || n.type === "missing" ? GP.ghostLabel : GP.label });
  }
  for (const w of wedges) {
    const name = w.n > 1 && w.area >= 0 ? COMM_HUBS[w.area] : "";
    if (!name) continue;   // a one-note wedge is named by its note already
    const a = (w.a0 + w.a1) / 2, cos = Math.cos(a);
    const align = cos > 0.25 ? "left" : cos < -0.25 ? "right" : "center";
    items.push({ text: name + " · " + w.n, px: 12, x: c.x + (w.r + 8 / scale) * cos,
      y: c.y + (w.r + 8 / scale) * Math.sin(a), align, ink: GP.ghostLabel });
  }
  ctx.save();
  ctx.textBaseline = "middle";
  const boxes = items.map(it => {
    ctx.font = (it.px / scale) + "px Lexend, system-ui, sans-serif";
    const w = ctx.measureText(it.text).width, h = it.px * 1.2 / scale;
    const x0 = it.align === "left" ? it.x : it.align === "right" ? it.x - w : it.x - w / 2;
    return { x0, y0: it.y - h / 2, x1: x0 + w, y1: it.y + h / 2 };
  });
  const tl = Graph.screen2GraphCoords(0, 0), br = Graph.screen2GraphCoords(freeWidth(), Graph.height());
  const keep = keepLabels(boxes, { x0: tl.x, y0: tl.y, x1: br.x, y1: br.y });
  ctx.lineJoin = "round";
  items.forEach((it, i) => {
    if (!keep[i]) return;
    ctx.font = (it.px / scale) + "px Lexend, system-ui, sans-serif";
    ctx.textAlign = it.align;
    // A halo in the floor's colour masks the lines under a name (rule 4).
    ctx.lineWidth = 3 / scale;
    ctx.strokeStyle = GP.bg;
    ctx.strokeText(it.text, it.x, it.y);
    ctx.fillStyle = it.ink;
    ctx.fillText(it.text, it.x, it.y);
  });
  ctx.restore();
}

function drawNodeLabel(n, ctx, scale, r) {
  ctx.font = (11 / scale) + 'px Lexend, system-ui, sans-serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  // A missing paper is named like a ghost: both are names nothing carries.
  ctx.fillStyle = (n.type === "ghost" || n.type === "missing") ? GP.ghostLabel : GP.label;
  ctx.fillText(n.label, n.x, n.y + r + 2 / scale);
}

// A paper on the rim (ADR-0066): a hollow square, frost when a note covers it,
// ash-dim at reduced opacity when none does. Before any pass every paper is
// frost, because "not covered" is a verdict and nothing has been tested yet.
// Never under 6px a side on screen: the rim is read at the zoom that fits the
// whole vault, where a note-sized square is a pixel.
const RIM_SIDE_PX = 6;
const rimHalf = (n, scale) => Math.max(nodeRadius(n) * SQUARE_HALF, RIM_SIDE_PX / 2 / scale);
function drawRimPaper(n, ctx, scale) {
  const h = rimHalf(n, scale);
  const none = n.covered === false;
  ctx.globalAlpha = (n._dim ? 0.2 : 1) * (none ? 0.45 : 1);
  ctx.beginPath();
  ctx.rect(n.x - h, n.y - h, 2 * h, 2 * h);
  ctx.fillStyle = GP.bg;
  ctx.fill();
  ctx.lineWidth = 1.2 / scale;
  ctx.strokeStyle = none ? GP.ghostLabel : GP.label;
  ctx.stroke();
  ctx.globalAlpha = 1;
  // Named when it is the one focused, or once the zoom names every note.
  if (n._dim || (scale < 1.5 && !focusIds.includes(n.id))) return;
  ctx.font = (11 / scale) + 'px Lexend, system-ui, sans-serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = GP.label;
  ctx.fillText(n.label, n.x, n.y + h + 2 / scale);
}

// The arcs and their names, under the papers (2D only, like the hulls).
function drawRim(ctx, scale) {
  if (!RIM || AB || !showNotes) return;
  const { cx, cy, R, arcs } = RIM;
  const pending = !DATA.covers || DATA.covers.state === "none";
  ctx.lineWidth = 1 / scale;
  ctx.strokeStyle = GP.line;
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, 2 * Math.PI);
  ctx.stroke();
  const band = R + 9 / scale, at = R + 22 / scale;
  ctx.font = (12 / scale) + 'px Lexend, system-ui, sans-serif';
  ctx.textBaseline = "middle";
  for (const a of arcs) {
    const lit = pending || a.covered > 0;
    ctx.lineWidth = 3 / scale;
    ctx.strokeStyle = lit ? GP.accent : GP.ghostLabel;
    ctx.globalAlpha = lit ? 0.8 : 0.5;
    ctx.beginPath();
    ctx.arc(cx, cy, band, a.a0 - Math.PI / 2, a.a1 - Math.PI / 2);   // canvas 0 is three o'clock
    ctx.stroke();
    ctx.globalAlpha = 1;
    // An arc too short to carry its name stays unlabelled: its papers name
    // their collection on hover, and two names on one spot read as neither.
    if ((a.a1 - a.a0) * (R * scale + 22) < 28) continue;
    // One arc is the whole circle, and its middle is six o'clock, under the
    // dock; twelve is under the toolbar and three under the key. Nine is the
    // side nothing floats over.
    const m = arcs.length === 1 ? 1.5 * Math.PI : (a.a0 + a.a1) / 2;
    const x = cx + at * Math.sin(m), y = cy - at * Math.cos(m);
    const text = arcLabel(a, pending);
    ctx.textAlign = Math.abs(Math.sin(m)) < 0.05 ? "center" : Math.sin(m) > 0 ? "left" : "right";
    ctx.lineWidth = 3 / scale;
    ctx.strokeStyle = GP.bg;
    ctx.strokeText(text, x, y);
    ctx.fillStyle = lit ? GP.label : GP.ghostLabel;
    ctx.fillText(text, x, y);
  }
}

// --- A citation's arrowhead (ADR-0055) --------------------------------------
// The frame's only one: direction is the whole of what a citation says. Drawn
// here and in the merged 3D buffer rather than by the bundles' own directional
// arrows, which in 3D are a cone mesh and a draw call each
// (TestBigVaultPerfKnobs keeps them out). At the cited end, on the mark's
// edge, because a mid-link arrow in a dense cluster points at nothing.
const ARROW_2D = 4, ARROW_3D = 8;   // length in graph units: 2/3 of a size-16 disc's radius in 2D
const ARROW_W = 0.45;               // half-width over length

function drawCiteArrow(l, ctx) {
  const a = l.source, b = l.target;
  if (!a || !b || a.x == null || b.x == null) return;
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy);
  if (!len) return;
  const ux = dx / len, uy = dy / len;
  const tip = markEdge(b, nodeRadius(b), ux, uy);
  if (len < tip + ARROW_2D) return;   // the two marks overlap: no line to point along
  const tx = b.x - ux * tip, ty = b.y - uy * tip;
  const bx = tx - ux * ARROW_2D, by = ty - uy * ARROW_2D, w = ARROW_2D * ARROW_W;
  ctx.beginPath();
  ctx.moveTo(tx, ty);
  ctx.lineTo(bx - uy * w, by + ux * w);
  ctx.lineTo(bx + uy * w, by - ux * w);
  ctx.closePath();
  ctx.fillStyle = linkPaint(l);
  ctx.fill();
}

// Hit area follows the circle, not the label: clicking the text of a dense
// cluster would otherwise pick whichever node's label happened to be on top.
function paintNodeArea(n, color, ctx, scale) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(n.x, n.y, hitRadius(n, scale || 1), 0, 2 * Math.PI);
  ctx.fill();
}
// In graph units at zoom k. A rim paper is drawn at a screen size, so it is
// hit at one too; an arranged note too (ADR-0088), with 3px of slack round
// its dot.
function hitRadius(n, k) {
  return ARR && ARR.layout[n.id] ? arrangedRadius(n, k) + 3 / k
    : isRim(n) ? rimHalf(n, k) * 1.5
    : COVERING && COVERING.has(n.id) ? Math.max(nodeRadius(n), 3.5 / k) + 2 : nodeRadius(n) + 2;
}

// 2D's hover, picked off the positions with the click's own hit radius rather
// than read from force-graph's picking canvas. The lib re-reads that canvas
// every frame, and a resolution drop (setRes) blanks it until its 800 ms
// redraw: with the pointer resting on a note the hover flipped to nothing and
// back. A relight is a repaint stream, which drops the resolution, so each
// flip set off the next (looping in the owner's tab, 2026-10-11). Asked on a
// pointer move only, so a note sliding under a still pointer relights nothing.
function pickHover2D(e) {
  if (!Graph || !is2D()) return;
  const cv = document.querySelector("#graph canvas");
  if (e.target !== cv) { hoverNote(null); return; }   // over the key, not the graph
  const c = cv.getBoundingClientRect(), k = Graph.zoom();
  const p = Graph.screen2GraphCoords(e.clientX - c.left, e.clientY - c.top);
  hoverNote(nearestHit(ALL_NODES, p, n => hitRadius(n, k)));
}

// --- Edge weight: what you wrote outranks what was inferred -----------------
// Every edge already declares its own opacity and width (graph_export), and
// both were dead: the 3D branch drew every link at the bundle's flat 0.2 and
// the 2D branch at a flat width of 1. So the two layers that matter most —
// 1341 wikilinks you wrote and 2718 similarities a model guessed — arrived on
// screen at identical weight, separated by hue alone.
//
// The alpha lives in the colour string because that is the only per-link seam
// the 3D bundle has: linkOpacity is global, but it MULTIPLIES the alpha it
// parses out of an rgba(), so setting it to 1 makes the per-edge alpha the
// final word. 2D reads the same string straight into strokeStyle.
const LINK_ALPHA_2D = 0.55;   // see the comment on its use below
const EDGE_FALLBACK = LIGHT ? DATA.colors.extracted_paper : DATA.colors.extracted;

// --- Edge family: a broken line is a link that is not there ------------------
// Weight ranks the layers; it does not SEPARATE them, and seven kinds on the
// hue channel alone is more than a 1px antialiased line can carry. So a second
// channel, carrying the one division that survives being drawn: GAP is a bridge
// between two areas that does not exist, PROPOSED is a link the shape of the
// vault predicts and nobody wrote, COUPLED is two notes from one source "that
// never came to link each other". All three draw an absence. Everything else on
// the canvas is a relation that HOLDS - a wikilink you wrote, the discord
// recolour of those same wikilinks, and the k-NN similarity, which is measured
// and not proposed - so all of it stays solid. It is the dashed line's oldest
// meaning on a map: the road that is planned, the border nobody agreed.
//
// SIMILAR was in this set for one build, on the wider rule "solid is what you
// wrote". Wrong twice. Wrong by meaning, because a similarity is not a missing
// link, it is a different KIND of link. And wrong by weight: it is 2901 of the
// 4246 visible edges, so dashing it turned the layer that has to read as ground
// into 8703 dots and made it the subject - which is the same mistake the
// particle budget below already refused for the same layer, in the same words.
// The dash is worth having on the three rows you can COUNT; on a field it is
// texture. SIMILAR was never the layer anyone confused: it owns the only
// saturated cool hue, the lowest opacity and the thinnest width already.
const ABSENT = new Set(["GAP", "PROPOSED", "COUPLED"]);
// COVERS is dashed for the reason it is inferred (ADR-0066 decision 4): Silica
// drew it, the reader did not write it. It is not an absence, so it stays out
// of ABSENT, and it only exists in 2D, so the 3D buffer never meets it.
const isDashed = l => ABSENT.has(l.type) || l.type === "COVERS";

// A dash is a LENGTH, not a fraction of the link. Three segments spread over
// the whole span made the pattern scale with the edge - the two gaps were 11%
// of it each - so a long link drew three long strokes with two furrows cut in
// them rather than a broken line. This is the upgrade the note that stood here
// named, a period in world units with a per-edge count, and it obeys the rule
// the zone geometry already does (ZONE_BULB): a constant would be wrong the
// moment the distance slider moved, so the period rides baseDist().
//
// A dash still lands on each end. A broken line that stopped short of its
// target would read as a link that does not arrive, which is a different claim
// from the one this channel makes. With k dashes and k-1 gaps filling [0,1] at
// gap ratio r, the period is 1/(k-r) and the ink (1-r)/(k-r), which puts the
// last dash's far end exactly on 1.
// 1:3 gap to ink, which is DASH_2D's 2px against 6px and the legend swatch's
// too: one ratio, so the two views and the key under them draw one pattern.
const DASH_GAP = 0.25;                                     // of the period
const DASH_PERIOD = () => 0.10 * baseDist() * forceMul.dist;
// Two, so even the shortest link still reads as broken; the ceiling is what
// keeps the buffer a fixed allocation. 24 covers a link 2.4x the layout's own
// spacing - past that the dashes lengthen again, which is the old behaviour and
// only for the outliers. Raise it (6 floats per dashed edge per step) if a
// vault ever draws an absent edge that reads solid.
const DASH_MIN = 2, DASH_CAP = 24;
// 2D keeps its own pair: the canvas painter runs inside the zoom transform, so
// the pattern is already in the same graph units the period above works in, and
// nothing there scales with the link.
const DASH_2D = [6, 2];

// Memoised because 2D calls this for every visible link on every frame, and
// building the string costs ~1ms per frame across 4.6k edges — measured, not
// assumed. An edge's colour depends on nothing but its own (fixed) data, its
// dim state and the renderer, so those two are the whole cache key.
function linkPaint(l) {
  // The comparison is part of the key, not a branch after the lookup: it
  // changes the paint of an edge whose data is otherwise fixed, so a cache that
  // ignored it would keep painting the last answer after the pick. With a
  // comparison on, a link inside either part paints as dimmed and one between
  // them at full weight; nothing is told apart by width (ADR-0058).
  const on = !!AB && !!l._ab;
  // The dim fades (fadeStep), in steps so the cache still holds. A 3D link the
  // lib owns (a particle carrier) takes a colour only on a full link digest,
  // so it takes the dim at once.
  const f = !is2D() && libOwnsLink(l) ? (l._dim ? 1 : 0) : l._fade || 0;
  const q = Math.round(f * FADE_STEPS);
  const key = (q ? "d" + q : "") + (l._ctx ? "x" : "") + (is2D() ? "2" : "3") +
    (AB ? (l._ab ? "C" : "c") : "");
  if (l.__paintKey === key) return l.__paint;
  let out;
  // A context wikilink under the rim (ADR-0066 amendment) is drawn as the
  // focus dims one: the structure is there, and it is not what is being read.
  if (q >= FADE_STEPS || l._ctx || (AB && !on)) out = GP.linkDim;
  else {
    const c = l.color || {};
    // A covers line is the accent, read from the theme's own token.
    const hex = l.type === "COVERS" ? GP.accent
      : (LIGHT ? c.paper || c.color : c.color) || EDGE_FALLBACK;
    const n = parseInt(hex.slice(1), 16);
    // 2D stacks every edge on one flat plane with no depth to thin the far ones
    // out, so the same alphas that read as structure in 3D read as a mat there.
    // One scale factor rather than a second table: the RANK is what is worth
    // preserving between the two views, not the absolute values.
    const a = (on ? 0.95 : (c.opacity == null ? 0.6 : c.opacity)) * (is2D() ? LINK_ALPHA_2D : 1);
    // Partway dimmed: toward the dim's colour and toward its full opacity.
    const t = q / FADE_STEPS, d = parseInt(GP.linkDim.slice(1), 16);
    const ch = sh => Math.round(((n >> sh) & 255) + (((d >> sh) & 255) - ((n >> sh) & 255)) * t);
    out = "rgba(" + ch(16) + "," + ch(8) + "," + ch(0) + "," + (a + (1 - a) * t).toFixed(3) + ")";
  }
  l.__paintKey = key; l.__paint = out;
  return out;
}

// --- 3D: every link in ONE LineSegments -------------------------------------
// The bundle builds a separate THREE.Line per link: one draw call and one
// per-tick geometry write each, which on this vault is ~9k of either and
// ~36ms a frame before a single sphere is drawn — measured, and the reason
// the 3D view lagged. So the lib's lines are never shown (linkVisibility
// false) and never positioned (linkPositionUpdate returns true), and this
// layer draws every visible link as one LineSegments: one object, one draw
// call, two vertices per link, RGBA per vertex so the per-edge alpha rank
// survives the merge. Fog applies to the shared material like it did to the
// per-link ones.
//
// No THREE global exists (same constraint as flattenNodes below), and no
// LineSegments instance exists to steal a constructor from. A Line instance
// does — and the renderer branches on the isLineSegments FLAG, not the class,
// so a Line wearing the flag renders as GL_LINES. The donor is the ONE line
// the lib is allowed to build: its visibility accessor admits RAW_EDGES[0]
// only, and an accessor that returns false makes the lib skip creating the
// object outright (verified, not assumed) — so the whole per-link fleet, its
// 9k materials included, is simply never born. The donor itself stays
// degenerate at the origin (linkPositionUpdate never lets it be positioned)
// and draws nothing.
//
// Particles are the one carve-out. A photon group is only created for a link
// whose line object exists (verified: admit the link, photons appear; skip
// it, they never do), so the links that may carry photons — GAP always,
// SIMILAR for the drift — stay lib-owned whenever PARTICLES is on: real
// lines, lib-positioned, excluded from the merge. PARTICLES off (the
// default) merges everything behind the one donor.
const libOwnsLink = l => PARTICLES && (l.type === "GAP" || l.type === "SIMILAR");
let LinkSeg = null;   // { obj, pos, col, colorCls, edges }

// The same conversion Color.setStyle applies (sRGB into the working space),
// without setStyle's per-call "alpha will be ignored" console warning.
function segRGBA(Color, s) {
  let r, g, b, a = 1;
  if (s[0] === "#") {
    const n = parseInt(s.slice(1), 16);
    r = (n >> 16) & 255; g = (n >> 8) & 255; b = n & 255;
  } else {
    const p = s.match(/rgba?\(([^)]+)\)/)[1].split(",").map(Number);
    r = p[0]; g = p[1]; b = p[2];
    if (p.length > 3) a = p[3];
  }
  const c = new Color().setRGB(r / 255, g / 255, b / 255, "srgb");
  return [c.r, c.g, c.b, a];
}

function buildLinkSeg() {
  const line = RAW_EDGES.length && RAW_EDGES[0].__lineObj;
  if (!line) return;               // digest not run yet; next frame retries
  // Segments, not links: a dashed edge is up to DASH_CAP disjoint pairs in the
  // same buffer. Allocated at the DASHED rows' worst case rather than the whole
  // graph's, because those rows are the three SMALL ones (18 edges of 4938 on
  // the reference vault), so the true size is E+432 where DASH_CAP * E would
  // have been 24 times the buffer to draw the same picture.
  // A citation is its line plus the two barbs of its arrowhead.
  const S = RAW_EDGES.reduce((n, e) => n + (isDashed(e) ? DASH_CAP : e.type === "CITES" ? 3 : 1), 0);
  const pos = new Float32Array(S * 6);
  const col = new Float32Array(S * 8);
  const Attr = line.geometry.getAttribute("position").constructor;
  const geom = new (line.geometry.constructor)();
  geom.setAttribute("position", new Attr(pos, 3));
  geom.setAttribute("color", new Attr(col, 4));
  const mat = new (line.material.constructor)({ vertexColors: true, transparent: true });
  const obj = new (line.constructor)(geom, mat);
  obj.isLineSegments = true;       // the renderer reads the flag, not the class
  obj.type = "LineSegments";
  obj.frustumCulled = false;       // positions churn every tick; skip bounds
  obj.raycast = () => {};        // the pointer belongs to the nodes
  (line.parent || Graph.scene()).add(obj);
  // Solid and dashed are kept apart, and the dashed ones ride at the END of the
  // buffer. Their segment COUNT follows the layout - a period in world units
  // over a length that moves every tick - so any edge stored after them would
  // have its colours slide out from under it on every frame. Behind them,
  // nothing does: the solid colours are written once per repaint and never
  // touched again, which is what keeps a per-tick recount off the wire.
  LinkSeg = { obj, pos, col, colorCls: line.material.color.constructor,
              solid: [], dashed: [], dashCol: new Float32Array(0),
              dashK: new Uint8Array(0) };
  repaintLinkSeg();
}

// Colours + the visible set — only when they change (filters, focus, dim),
// which is what refreshPaint/applyFilters call in place of the accessor
// re-pass that used to rebuild 9k materials.
function repaintLinkSeg() {
  if (!LinkSeg) return;
  const solid = [], dashed = [];
  for (const e of RAW_EDGES) {
    if (e._hidden || libOwnsLink(e)) continue;
    (isDashed(e) ? dashed : solid).push(e);
  }
  LinkSeg.solid = solid; LinkSeg.dashed = dashed;
  for (let i = 0; i < solid.length; i++) {
    const c = segRGBA(LinkSeg.colorCls, linkPaint(solid[i]));
    LinkSeg.col.set(c, i * 8);
    LinkSeg.col.set(c, i * 8 + 4);
  }
  // The arrowheads sit between the solid lines and the dashed ones: their
  // count is fixed between repaints, so their colours are written once here
  // and nothing behind them slides (see the dashed note above).
  const heads = LinkSeg.heads = solid.filter(e => e.type === "CITES");
  for (let j = 0, q = solid.length * 8; j < heads.length; j++, q += 16) {
    const c = segRGBA(LinkSeg.colorCls, linkPaint(heads[j]));
    for (let v = 0; v < 16; v += 4) LinkSeg.col.set(c, q + v);
  }
  // The dashed colours are RESOLVED here, because linkPaint and the sRGB
  // conversion are the expensive half, and WRITTEN in the position pass, which
  // is the only place that knows where each one landed.
  LinkSeg.dashCol = new Float32Array(dashed.length * 4);
  for (let i = 0; i < dashed.length; i++)
    LinkSeg.dashCol.set(segRGBA(LinkSeg.colorCls, linkPaint(dashed[i])), i * 4);
  // Zeroed rather than kept: 0 is not a legal dash count, so the pass below
  // sees every dashed edge as changed and lays its colours down at least once.
  LinkSeg.dashK = new Uint8Array(dashed.length);
  LinkSeg.obj.geometry.getAttribute("color").needsUpdate = true;
  writeLinkSegPositions();
}

function writeLinkSegPositions() {
  const { pos, col, solid, dashed, dashCol, dashK, obj } = LinkSeg;
  // The solid case keeps its straight 1:1 write. It is most of the buffer on
  // any vault, and it runs every tick a node can still move.
  for (let i = 0; i < solid.length; i++) {
    const a = NODE_BY_ID[solid[i].from], b = NODE_BY_ID[solid[i].to], o = i * 6;
    pos[o]     = a.x || 0; pos[o + 1] = a.y || 0; pos[o + 2] = a.z || 0;
    pos[o + 3] = b.x || 0; pos[o + 4] = b.y || 0; pos[o + 5] = b.z || 0;
  }
  // Two barbs from the tip, spread across the line in the plane that faces
  // the camera, so the head reads as a V from any angle. That plane moves with
  // the camera, which is why this rides the per-frame write and not the
  // repaint. The tip sits on the disc of the target's area (cbrt of its size,
  // times the bundle's nodeRelSize of 4), a paper's included.
  const heads = LinkSeg.heads, cam = Graph.camera().position;
  for (let j = 0, q = solid.length * 6; j < heads.length; j++, q += 12) {
    const a = NODE_BY_ID[heads[j].from], b = NODE_BY_ID[heads[j].to];
    const bx = b.x || 0, by = b.y || 0, bz = b.z || 0;
    let ux = bx - (a.x || 0), uy = by - (a.y || 0), uz = bz - (a.z || 0);
    const len = Math.hypot(ux, uy, uz) || 1;
    ux /= len; uy /= len; uz /= len;
    const rb = Math.cbrt(Math.round(b.size || 16)) * 4;
    const room = len > rb + ARROW_3D;   // overlapping marks: the barbs fold to the tip
    const tx = bx - ux * rb, ty = by - uy * rb, tz = bz - uz * rb;
    const vx = cam.x - tx, vy = cam.y - ty, vz = cam.z - tz;
    let px = uy * vz - uz * vy, py = uz * vx - ux * vz, pz = ux * vy - uy * vx;
    const pl = Math.hypot(px, py, pz);
    const w = room && pl > 1e-9 ? ARROW_3D * ARROW_W / pl : 0, back = room ? ARROW_3D : 0;
    px *= w; py *= w; pz *= w;
    for (const sgn of [1, -1]) {
      const o = q + (sgn > 0 ? 0 : 6);
      pos[o] = tx; pos[o + 1] = ty; pos[o + 2] = tz;
      pos[o + 3] = tx - ux * back + sgn * px;
      pos[o + 4] = ty - uy * back + sgn * py;
      pos[o + 5] = tz - uz * back + sgn * pz;
    }
  }
  const period = DASH_PERIOD();
  let s = solid.length + 2 * heads.length, moved = false;
  for (let i = 0; i < dashed.length; i++) {
    const a = NODE_BY_ID[dashed[i].from], b = NODE_BY_ID[dashed[i].to];
    const ax = a.x || 0, ay = a.y || 0, az = a.z || 0;
    const dx = (b.x || 0) - ax, dy = (b.y || 0) - ay, dz = (b.z || 0) - az;
    const k = Math.max(DASH_MIN, Math.min(DASH_CAP,
      Math.round(Math.sqrt(dx * dx + dy * dy + dz * dz) / period)));
    if (dashK[i] !== k) { dashK[i] = k; moved = true; }
    const p = 1 / (k - DASH_GAP), ink = p * (1 - DASH_GAP);
    for (let j = 0; j < k; j++) {
      const q = (s + j) * 6, t0 = j * p, t1 = t0 + ink;
      pos[q]     = ax + dx * t0; pos[q + 1] = ay + dy * t0; pos[q + 2] = az + dz * t0;
      pos[q + 3] = ax + dx * t1; pos[q + 4] = ay + dy * t1; pos[q + 5] = az + dz * t1;
    }
    s += k;
  }
  // Only when the packing actually moved. The colour buffer is 158KB on the
  // reference vault and a needsUpdate re-uploads all of it; the dash counts
  // change while the layout settles and then stop, so this pays once per
  // change instead of 60 times a second forever.
  if (moved) {
    let t = solid.length + 2 * heads.length;
    for (let i = 0; i < dashed.length; i++) {
      const c0 = dashCol[i * 4], c1 = dashCol[i * 4 + 1],
            c2 = dashCol[i * 4 + 2], c3 = dashCol[i * 4 + 3];
      for (let j = 0, k = dashK[i]; j < k; j++) {
        const q = (t + j) * 8;
        col[q]     = c0; col[q + 1] = c1; col[q + 2] = c2; col[q + 3] = c3;
        col[q + 4] = c0; col[q + 5] = c1; col[q + 6] = c2; col[q + 7] = c3;
      }
      t += dashK[i];
    }
    obj.geometry.getAttribute("color").needsUpdate = true;
  }
  obj.geometry.setDrawRange(0, s * 2);
  obj.geometry.getAttribute("position").needsUpdate = true;
}

// Per frame beside the label layers (the loop always runs in 3D): build once
// the lib's digest has produced a carrier, then follow the nodes — but only
// while they can move. A settled, unwoken graph skips the write entirely.
function linkSegStep() {
  if (is2D()) return;
  if (!LinkSeg) { buildLinkSeg(); return; }
  if (!simRunning && performance.now() >= awakeUntil) return;
  writeLinkSegPositions();
}

// --- 3D: the same crystal the mark is cut from ------------------------------
// PARTICLES/SHADING come from the settings panel (Display). Both off is the
// bundle's own look: smooth lit spheres, no fog, still edges.
//
// The two lights below stopped lighting the NOTES when those became flat marks
// (see flattenNodes). They are kept, and kept tuned, because the particle and
// arrow meshes are still MeshLambert and still the bundle's: with PARTICLES on
// they are the only lit things in the scene, and a neutral office rig on them
// would read as another app's dots crossing this one's ground. With PARTICLES
// off the rig lights nothing, and the fog and the lens are the whole of what
// SHADING still buys.
const PARTICLES = DATA.particles;
const SHADING = DATA.shading;
// Everything here is a bundle default undone. Out of the box the scene is lit
// by a GRAY ambient at full strength plus a white key, the spheres are smooth-
// shaded, and there is no fog — which is a fine neutral rig for a demo and the
// wrong one for this palette. A substrate built as blue-black crystal, lit flat
// by an office light, renders as plastic beads: that, not the library, is what
// made this view read as a stock 3D graph while the rest of the app did not.
//
// No THREE global exists to build with (see the DOM label layers below for the
// same constraint). It is not needed: the bundle hands out the scene, and every
// constructor this wants is reachable from an object already inside it.
// Vendored+pinned bundles are what makes that safe to lean on.
//
// Two rigs, because light is not the same scene with the background swapped.
// The community colours already dropped ~28 lightness points to survive a paper
// floor (_COMMUNITY_LIGHTNESS_ON_PAPER), and a multiplying ambient at crystal
// strength takes them the rest of the way to mud. So on paper the ambient rises
// toward white and the key comes down: the facets still split, but the light
// falling on them is a room's, not a lamp inside the stone. Fog travels with
// the floor either way — it is the same depth cue, sold into white instead of
// out of black.
const CRYSTAL = LIGHT ? {
  ambient: 0xEFEAE0, ambientI: 1.55,
  key: 0xFFF6E4, keyI: 1.35,      // warm key, matching the paper it lands on
  fogNear: 0.55, fogFar: 2.30,    // paper fog swallows less: white on white
  fov: 42,
} : {
  // Ambient MULTIPLIES each node's own colour, so this is the one value that
  // can silently destroy the community channel: a saturated violet here turned
  // every community violet, and a bright one flattened the facets back into
  // spheres. Low enough that lit and unlit facets separate, unsaturated enough
  // that the hue survives into the unlit half.
  ambient: 0x9B93C6, ambientI: 1.05,
  key: 0xDCE8FF, keyI: 2.40,      // cool key, off-axis so facets split
  fogNear: 0.45, fogFar: 2.10,    // as fractions of the camera's distance
  fov: 42,                        // 50 is the wide-angle look; this is a lens
};

// --- 3D nodes: a flat mark, not a lit bead ----------------------------------
// A note is a 2D mark. 2D draws it as one - a filled disc, and rings that hold
// a constant SCREEN width whatever the zoom (drawNode). 3D drew a lit sphere,
// and a lit sphere is an OBJECT: the gradient running across it is the whole
// reason it reads as one. So the notes here stop being lit. The fill is the
// community colour flat, edge to edge, and the silhouette is the only shape
// information the mark carries - which is what makes it the same mark in both
// views. Nothing has to be turned toward the camera: a uniformly coloured
// sphere IS a camera-facing disc, identical from every angle, so the geometry
// does the billboarding for free and there is no orientation to keep in step.
// Size still falls off with distance; that and the fog are all that is left of
// depth once the shading is gone, which is why the fog stays.
//
// A fragment-shader replacement, not material properties, and that is not a
// preference: the bundle's node digest keeps a material only while
// material.color.equals(nodeColor(n)), so zeroing the diffuse to kill the light
// would make it discard and rebuild every node material on every focus change.
// Reading diffuseColor in the shader leaves the colour where the bundle expects
// to find it, and the rebuild never fires.
//
// The rim is the constant-width ring, and it is exact rather than fitted: on a
// sphere dot(normal, view) reaches 0 at the silhouette from every angle, and
// dividing it by its own screen-space derivative turns that into a distance in
// PIXELS from the edge. So the ring is RIM_PX wide on a hub, on a leaf, near
// and far - the property that makes it read as drawn on the screen rather than
// modelled in the scene, and the same reason drawNode divides its own ring by
// the zoom. Painted in the background colour, so two overlapping discs separate
// instead of fusing into one blob: the reading the lit version used to get from
// its gradient for free.
const RIM_PX = 1.2;
// The two rings 2D draws around the disc (drawNode): hub/orphan on the state
// channel, load-bearing on its own outside it. Until now the 3D view had
// neither, so the legend's four NODE rows promised a reading half of them could
// not deliver here and the tooltip was the only place it landed.
//
// One deviation from 2D, and it is forced: 2D puts its rings OUTSIDE the disc
// so the community hue keeps its full area, and a sphere has no outside - the
// silhouette is where the geometry ends. So the bands are laid INWARD from the
// edge and the hue gives up its outer 6.5px on a ringed note. The ORDER is 2D's
// and that is what the reading rests on: load-bearing outside the state ring,
// state ring outside the hue.
//
// The widths are pixels, which is the whole point of the metric below, and the
// floor is what 2D spends its zoom gate on: under RING_MIN_PX of disc there is
// no room for 6.5px of ring and no note left under it, so the mark goes back to
// being a plain dot rather than an alarm made of rings.
const RING_S_PX = 1.6, RING_C_PX = 1.3, RING_GAP_PX = 1.2, RING_MIN_PX = 13;
// The silhouette IS the mark now, so it cannot be a hexagon - resolution 6 was
// right only while the facets were the point. 32 rather than 20, and the ring
// is what decided it: the bands are laid out from the INTERPOLATED normal,
// which sweeps non-linearly across a chord, so a coarse sphere makes the ring
// trace a visible polygon and vary in width around it. Measured on a node
// filling ~120px: at 20 the chords and the width wobble both read, at 32
// neither does. 2048 triangles a node against 72, ~2.4M a frame on the
// reference vault - and the render budget only pays for them while something
// moves. Revisit at 5k notes, or if a weak iGPU ever drops frames on a drag.
const NODE_RES = 32;
// The bundle caches one sphere geometry per node VALUE, and size is
// round(16 + 40*betweenness, 2) - a near-continuous float, so it was caching
// one geometry per note. Rounded it is at most 41 of them, and cbrt() flattens
// the half-unit into a ~1% radius error nobody can see. Worth doing at any
// resolution; unaffordable at 20.
// A paper's square is cut from the sphere it is inscribed in (see FLAT), so
// that sphere's radius is the square's half-diagonal: sqrt(pi/2) of the disc
// radius, which is (pi/2)^1.5 in value since the bundle takes the cube root.
// Only with SHADING, the one config that installs the cut; without it a paper
// stays the bundle's lit sphere at its own size.
// A daily note's triangle is cut the same way, from the sphere of its
// circumradius, TRI_R of the disc radius. A diamond is the square turned, so
// its sphere is the square's.
const SQUARE_VAL = Math.pow(Math.PI / 2, 1.5);
const SHAPE_VAL = { paper: SQUARE_VAL, missing: SQUARE_VAL, source: SQUARE_VAL, daily: Math.pow(TRI_R, 3),
                    plan: SQUARE_VAL, event: SQUARE_VAL };
const nodeVal3D = n => Math.round((n.size || 16) * ((SHADING && SHAPE_VAL[n.type]) || 1));

// The per-node channel, and the only one this view has: the bundle shares one
// material per COLOUR and one geometry per size, so neither can carry a value
// that belongs to a single note. These three arrays are shared by every node
// material, and the object about to be drawn rewrites them - three uploads
// uniforms inside setProgram, which runs AFTER Object3D.onBeforeRender, so what
// is written there lands on that node's draw and on nothing else's. Every node
// material also compiles to ONE program (the colours are uniforms, not
// defines), so there is one uniform cache and it sees every change.
const U_STATE = new Float32Array(4);   // rgb + width in px; 0 = no ring
const U_CUT   = new Float32Array(4);
const U_BANDS = new Float32Array(2);   // knockout width, gap width
const U_SHAPE = new Float32Array(2);   // shapeOf value (0 = disc), outline px of a broken one
// The focus dim (fadeStep): its colour in the working space, and how far this
// note has faded toward it. Per draw, so a fade costs no material rebuild.
const U_DIM   = new Float32Array(4);
const MISSING_PX = 1.5;                // the broken square's outline: 2D's 1.5 graph units at zoom 1

// Built once, from the first Color the scene hands over. The injection lands
// BEFORE the colorspace conversion, so the rim has to be in the working space -
// which is exactly the conversion Color applies on the way in, so the value is
// read off the instance instead of converted a second time by hand.
//
// ONE patch object for every material, and it has to stay one: the program
// cache key is onBeforeCompile.toString(), so a second closure with different
// GLSL but the same source text would silently be handed this one's program.
let flatPatch = null;
function buildFlatPatch(Color) {
  const c = new Color(GP.bgHex);
  const bg = "vec3(" + c.r.toFixed(5) + "," + c.g.toFixed(5) + "," +
             c.b.toFixed(5) + ")";
  const LIT = "vec3 outgoingLight = reflectedLight.directDiffuse + " +
              "reflectedLight.indirectDiffuse + totalEmissiveRadiance;";
  // rho is the disc's radial coordinate, 0 at the centre and 1 at the
  // silhouette, and unlike dot(normal, view) it is LINEAR in screen radius - so
  // its gradient is the same everywhere on the disc and (1-rho)/|grad rho| is a
  // distance in pixels, not merely something proportional to one. That is what
  // lets the bands below be specified as widths and mean it. dFdx/dFdy rather
  // than fwidth() because fwidth sums the two axes and would make the ring up
  // to 41% thinner on the diagonals than on the axes - invisible on the 1.2px
  // knockout alone, a wobble once there are three bands stacked.
  const FLAT = "vec3 outgoingLight = mix(diffuseColor.rgb, uSilDim.rgb, uSilDim.w);\n" +
    "float _f = abs(dot(normalize(normal), normalize(vViewPosition)));\n" +
    "float _rho = sqrt(max(0.0, 1.0 - _f * _f));\n" +
    "float _d = (1.0 - _rho) / max(length(vec2(dFdx(_rho), dFdy(_rho))), 1e-6);\n" +
    // A paper (ADR-0055). The sphere is the square's circumcircle (nodeVal3D),
    // and the disc coordinate is the normal's component across the view ray,
    // which has length rho. Measured on axes taken across THIS fragment's ray
    // and not view-space x/y: off the screen's centre the ray leans, and plain
    // x/y would let the corners run past the silhouette. Scaled by sqrt(2) so
    // the inscribed square's edge sits at 1, then the same pixel distance as
    // the disc's, so every band below works on a square unchanged. The branch
    // is on a uniform, so the derivatives inside it stay defined.
    "if (uSilShape.x > 0.5) {\n" +
    "  vec3 _v = normalize(vViewPosition), _n = normalize(normal);\n" +
    "  vec3 _ax = normalize(cross(vec3(0.0, 1.0, 0.0), _v));\n" +
    "  vec2 _q = vec2(dot(_n, _ax), dot(_n, cross(_v, _ax)));\n" +
    "  float _m;\n" +
    // A plan or an event (ADR-0070): the diamond inscribed in the unit circle
    // has its vertices on the axes, so its edge is where |x| + |y| reaches 1.
    "  if (uSilShape.x > 3.5) {\n" +
    "    _m = abs(_q.x) + abs(_q.y);\n" +
    // A daily note (ADR-0062): the triangle inscribed in the unit circle has
    // inradius 1/2, so twice the largest projection on its three edge normals
    // (down, and 30 degrees above either side) is 1 on its edge. _q.y is up on
    // screen, so the apex is up, as in 2D.
    "  } else if (uSilShape.x > 2.5) {\n" +
    "    _m = 2.0 * max(-_q.y, max(dot(_q, vec2(0.8660254, 0.5)), dot(_q, vec2(-0.8660254, 0.5))));\n" +
    "  } else {\n" +
    "    _q *= 1.41421356;\n" +
    "    _m = max(abs(_q.x), abs(_q.y));\n" +
    "  }\n" +
    "  _d = (1.0 - _m) / max(length(vec2(dFdx(_m), dFdy(_m))), 1e-6);\n" +
    "  if (_d < 0.0) discard;\n" +
    // A missing paper keeps only a broken outline: MISSING_DASHES per side,
    // ink at both corners, the period that 2D's setLineDash gets.
    "  float _t = 0.5 * ((abs(_q.x) > abs(_q.y) ? _q.y : _q.x) + 1.0);\n" +
    "  if (uSilShape.x > 1.5 && uSilShape.x < 2.5 && (_d > uSilShape.y || fract(_t * " +
    (MISSING_DASHES - MISSING_GAP).toFixed(4) + ") > " + (1 - MISSING_GAP).toFixed(4) +
    ")) discard;\n" +
    "}\n" +
    // A broken square is the node colour edge to edge: a knockout band would
    // eat the whole of a 1.5px outline.
    "if (uSilShape.x < 1.5 || uSilShape.x > 2.5) {\n" +
    // Every boundary is an offset from the one outside it, and a ring of width
    // 0 collapses its band AND the gap after it - so a note with no ring lays
    // down exactly the knockout this started as. The mix target is the
    // background rather than the ring colour when the width is 0, because two
    // smoothsteps at the same edge do not cancel: they would leave a 1px ghost
    // of a ring that is not there.
    "float _e0 = uSilBands.x;\n" +
    "float _e1 = _e0 + uSilCut.w;\n" +
    "float _e2 = _e1 + (uSilCut.w > 0.0 ? uSilBands.y : 0.0);\n" +
    "float _e3 = _e2 + uSilState.w;\n" +
    "float _e4 = _e3 + (uSilState.w > 0.0 ? uSilBands.y : 0.0);\n" +
    "vec3 _bg = " + bg + ";\n" +
    "vec3 _c = _bg;\n" +
    "_c = mix(_c, uSilCut.w   > 0.0 ? uSilCut.rgb   : _bg, smoothstep(_e0 - 0.5, _e0 + 0.5, _d));\n" +
    "_c = mix(_c, _bg, smoothstep(_e1 - 0.5, _e1 + 0.5, _d));\n" +
    "_c = mix(_c, uSilState.w > 0.0 ? uSilState.rgb : _bg, smoothstep(_e2 - 0.5, _e2 + 0.5, _d));\n" +
    "_c = mix(_c, _bg, smoothstep(_e3 - 0.5, _e3 + 0.5, _d));\n" +
    "outgoingLight = mix(_c, outgoingLight, smoothstep(_e4 - 0.5, _e4 + 0.5, _d));\n" +
    "}";
  const DECL = "uniform vec4 uSilState;\nuniform vec4 uSilCut;\n" +
               "uniform vec2 uSilBands;\nuniform vec2 uSilShape;\nuniform vec4 uSilDim;\n";
  return sh => {
    // Not a swallowed failure: a bundle upgrade that rewrites the lambert
    // shader leaves the notes lit, which is a LOOK and not a break, and this
    // view has no other way to say the patch stopped landing. The focus dim
    // rides this patch too (nodeColor), so it would stop showing in 3D.
    if (sh.fragmentShader.indexOf(LIT) < 0) {
      console.warn("node flat patch: lambert outgoingLight anchor is gone; 3D focus dim is off");
      return;
    }
    sh.fragmentShader = DECL + sh.fragmentShader.replace(LIT, FLAT);
    sh.uniforms.uSilState = { value: U_STATE };
    sh.uniforms.uSilCut = { value: U_CUT };
    sh.uniforms.uSilBands = { value: U_BANDS };
    sh.uniforms.uSilShape = { value: U_SHAPE };
    sh.uniforms.uSilDim = { value: U_DIM };
  };
}

// The three ring colours, once, in the working space - the same conversion the
// merged link buffer does for the same reason (segRGBA).
let RING_RGB = null;
function buildRingRGB(Color) {
  const rgb = s => segRGBA(Color, s).slice(0, 3);
  return { hub: rgb(GP.ringHub), orphan: rgb(GP.ringOrphan), cut: rgb(GP.ringCut) };
}

// Resolved once per node, because neither answer can change: both come off the
// exported data, which is also why 2D's own legend counts run once at load.
function nodeRings(n) {
  // STATE_RING is the gate, not a second list: the states that get a ring are
  // decided once, in that table, with the reason for each omission. A ghost
  // therefore gets no state ring but keeps the load-bearing one, as in 2D.
  const st = nodeState(n);
  const s = STATE_RING[st] ? RING_RGB[st] : null;
  return (s || n.cut) ? { s, c: n.cut ? RING_RGB.cut : null } : null;
}

// Per object, per frame, immediately before its draw. Costs one distance and
// eight writes on 1150 nodes; the arrays are shared, so a node with no ring
// still has to zero the widths or it would inherit the last node's.
function nodeUniforms(o, renderer, camera) {
  const dpr = renderer.getPixelRatio();
  U_BANDS[0] = RIM_PX * dpr; U_BANDS[1] = RING_GAP_PX * dpr;
  U_STATE[3] = U_CUT[3] = 0;
  const n = o.__data, r = o.geometry.parameters;
  // Before the ring gates below: a dimmed or distant paper is still a square.
  U_SHAPE[0] = (n && shapeOf(n)) || 0; U_SHAPE[1] = MISSING_PX * dpr;
  U_DIM[3] = (n && n._fade) || 0;
  // __data is the bundle's own back-pointer from the mesh to the note, the same
  // one its click and tooltip paths read. No ring rather than a throw if a
  // future bundle stops setting it.
  if (!n || !n.__rings || n._dim || !r) return;
  // The derivative above measures in DEVICE pixels, so the projection has to as
  // well: domElement.height is the drawing buffer, not the CSS box. A paper's
  // sphere is its square's circumcircle, so the floor is held against the disc
  // of the same area, not against the larger sphere.
  const e = o.matrixWorld.elements, c = camera.matrixWorld.elements;
  const dist = Math.hypot(e[12] - c[12], e[13] - c[13], e[14] - c[14]);
  const rpx = r.radius * (0.5 * renderer.domElement.height /
                          Math.tan(camera.fov * Math.PI / 360)) / dist /
              (SHAPE_VAL[n.type] ? Math.cbrt(SHAPE_VAL[n.type]) : 1);
  if (rpx < RING_MIN_PX * dpr) return;
  const s = n.__rings.s, k = n.__rings.c;
  if (s) { U_STATE[0] = s[0]; U_STATE[1] = s[1]; U_STATE[2] = s[2];
           U_STATE[3] = RING_S_PX * dpr; }
  if (k) { U_CUT[0] = k[0]; U_CUT[1] = k[1]; U_CUT[2] = k[2];
           U_CUT[3] = RING_C_PX * dpr; }
}

// three uploads a material's uniforms only when the MATERIAL changes between
// two draws (setProgram compares material.id), and the bundle shares one
// material per colour, so every note of an area shares one. Two of them drawn
// back to back kept the first one's values: a paper drawn after a note of its
// own area came out round, and a ring leaked to its neighbour the same way.
// Unbinding the program makes setProgram rebind it and upload in full, and it
// is paid only when the values differ from what the program last received.
const U_ALL = [U_STATE, U_CUT, U_BANDS, U_SHAPE, U_DIM];
const U_HELD = new Float32Array(16);
let U_HELD_MAT = null;
function nodeRingStep(renderer, scene, camera) {
  nodeUniforms(this, renderer, camera);
  let k = 0, changed = false;
  for (const u of U_ALL) for (let i = 0; i < u.length; i++, k++)
    if (U_HELD[k] !== u[i]) { U_HELD[k] = u[i]; changed = true; }
  if (changed && this.material === U_HELD_MAT) renderer.state.useProgram(null);
  U_HELD_MAT = this.material;
}

// Re-patch after every wholesale material rebuild. refreshPaint triggers one on
// every focus change (see its comment), so this cannot be a one-shot. The flag
// rides on the material itself: a rebuilt material simply arrives without one,
// which makes the repeat cost a 1150-entry loop that writes nothing.
// __threeObj is the bundle's own per-node handle — 18x cheaper than walking the
// scene, which also carries ~2700 particle meshes that want none of this.
function flattenNodes() {
  let Color = null;
  for (const n of RAW_NODES) {
    const o = n.__threeObj, m = o && o.material;
    if (!m) continue;
    if (!Color) Color = m.color.constructor;
    if (!RING_RGB) { RING_RGB = buildRingRGB(Color); U_DIM.set(segRGBA(Color, GP.dim).slice(0, 3)); }
    if (!flatPatch) flatPatch = buildFlatPatch(Color);
    if (n.__rings === undefined) n.__rings = nodeRings(n);
    // Outside the material guard: the hook rides the MESH, and a mesh can be
    // rebuilt while its (shared, already patched) material is not.
    if (o.onBeforeRender !== nodeRingStep) o.onBeforeRender = nodeRingStep;
    if (m.__silica) continue;
    // The material arrives smooth-shaded and stays that way: the bands read the
    // normal FIELD, and a faceted one would draw 20-sided rings inside a round
    // silhouette.
    m.onBeforeCompile = flatPatch;
    m.needsUpdate = true;
    m.__silica = 1;
  }
  return Color;
}

function styleScene() {
  if (!SHADING || is2D() || !Graph || !Graph.scene) return;
  const sc = Graph.scene();
  const Color = flattenNodes();
  if (!Color) return;
  // Per-light flags, not one scene-level one. The bundle populates its scene
  // lazily: at construction and immediately after graphData it holds only the
  // background mesh, and the lights and the node Group appear some frames
  // later. A single "already styled" mark set the moment the nodes showed up
  // could therefore land in a window where the lights had not, and then said
  // done forever — which is exactly what it did. Flagging each light drops the
  // ordering assumption: one that arrives late, or gets replaced, is styled on
  // the next frame instead of never. Direct children only, so this is four
  // entries rather than a walk over ~3900 meshes.
  for (const o of sc.children) {
    if (!o.isLight || o.__silica) continue;
    if (o.type === "AmbientLight") {
      o.color = new Color(CRYSTAL.ambient); o.intensity = CRYSTAL.ambientI;
    } else if (o.type === "DirectionalLight") {
      o.color = new Color(CRYSTAL.key); o.intensity = CRYSTAL.keyI;
      o.position.set(0.55, 1, 0.75);
    } else continue;
    o.__silica = 1;
  }
  if (sc.__silicaLit) return;
  // Linear fog, not exponential. Exponential density is anchored to world units,
  // and the camera here travels three orders of magnitude between a fitted vault
  // and a single note: one density either does nothing up close or swallows the
  // whole graph the moment you pull back — which is exactly what it did. Linear
  // near/far ride the camera instead, so the depth cue reads the same at every
  // zoom. No Fog constructor is reachable (no instance exists to borrow one
  // from), but the renderer reads the shape, not the class.
  sc.fog = { isFog: true, isFogExp2: false, name: "",
             color: new Color(GP.bgHex), near: 1, far: 2 };
  const cam = Graph.camera();
  cam.fov = CRYSTAL.fov;
  cam.updateProjectionMatrix();
  sc.__silicaLit = 1;
}

// Per frame, beside the label layers: the fog slab has to follow the camera or
// it is just a fixed band the graph flies through.
function fogStep() {
  if (!SHADING || is2D() || !Graph || !Graph.scene) return;
  const sc = Graph.scene();
  if (!sc.fog) return;
  const p = Graph.camera().position;
  const d = Math.hypot(p.x, p.y, p.z);
  sc.fog.near = d * CRYSTAL.fogNear;
  sc.fog.far = d * CRYSTAL.fogFar;
}

// --- the renderer, either dimension ----------------------------------------
// /graph regenerates the whole document per request (cooccurrence refresh + kNN
// + Louvain), so the switch must never reload: it destroys the instance and
// rebuilds from the RAW_NODES/RAW_EDGES already in the page. The two libs are
// kapsule siblings — one builder, four branches (fly-to, sphere detail, link
// width, labels), everything else shared.
const MODE_KEY = "silica-graph-mode";
let mode = "3d";
try { if (localStorage.getItem(MODE_KEY) === "2d") mode = "2d"; } catch (e) {}
const is2D = () => mode === "2d";

let Graph = null;
let fitPending = false;  // one-shot zoomToFit after a rebuild

// --- render budget: stop paying 60fps for a picture that stopped moving -----
// Neither bundle idles on its own here. 3d-force-graph's _animationCycle is
// unconditional: tickFrame + render + requestAnimationFrame, every frame,
// forever. force-graph DOES have autoPauseRedraw (default on), but its wake
// condition includes `links.some(l => l.__photons.length)` — so the five GAP
// particle links hold the whole canvas awake, repainting all 553 nodes at 60Hz
// to move ten dots. On a settled graph that is the single largest cost in this
// view, and it is pure waste: nothing on screen changes.
//
// So: full rate while the layout settles, the camera tweens or the pointer is
// over the graph; IDLE_FPS when only particles still move; nothing when even
// those are off. The failure mode is chosen — a missed wake signal leaves the
// loop running, i.e. exactly today's behaviour, never a frozen view.
const IDLE_FPS = 20;
const WAKE_MS = 1200;   // trackball inertia keeps the camera moving after pointerup
let awakeUntil = 0, simRunning = true, idleTick = null, sleepTimer = null;

// Both particle layers keep the canvas awake, so both have to be asked. The
// similarity layer is on by default, which means the idle tick now effectively
// always runs — that is the price of the drift, and it is the reason the drift
// rides the 20fps budget instead of the full frame rate.
const particlesMoving = () => PARTICLES && RAW_EDGES.some(e =>
  !e._dim && !e._hidden &&
  ((e.type === "GAP" && showGaps) || (e.type === "SIMILAR" && showSimilar)));

function renderBudget() {
  if (!Graph) return;
  clearInterval(idleTick); idleTick = null;
  if (simRunning || performance.now() < awakeUntil) { Graph.resumeAnimation(); return; }
  Graph.pauseAnimation();
  // resumeAnimation() runs one cycle synchronously and re-arms rAF;
  // pauseAnimation() cancels the re-arm. Net effect: exactly one frame.
  if (particlesMoving()) idleTick = setInterval(
    () => { Graph.resumeAnimation(); Graph.pauseAnimation(); }, 1000 / IDLE_FPS);
}

// Keep rendering at full rate for `ms`. Every mutation and every camera tween
// calls this; the tail covers control inertia and tween duration.
function wake(ms = WAKE_MS) {
  awakeUntil = Math.max(awakeUntil, performance.now() + ms);
  clearTimeout(sleepTimer);
  sleepTimer = setTimeout(renderBudget, awakeUntil - performance.now() + 30);
  renderBudget();
}

// --- dynamic resolution: motion may be soft, stillness never ----------------
// The 2D canvas is fill-bound: a full repaint costs ~15ms per megapixel of
// backing store on software raster, so a hot simulation on a 2263x1339 canvas
// delivers 15-22fps with the main thread mostly idle — measured ~8ms of script
// against 45-90ms between frames, rAF starved by raster back-pressure, which
// is why no JS profile ever showed it. Pixels are the cost: the same scene at
// half ratio (a quarter of the pixels) measured ~240fps. So while repaints are
// STREAMING in full-rate mode the backing store drops to half ratio — softness
// is invisible in motion — and when the stream ends the full ratio returns
// with one crisp repaint. A still frame is never soft, and the idle particle
// tick (parked, 20fps) never triggers this: what it paints IS stillness.
//
// One overridden getter keeps every consumer consistent: the lib reads
// window.devicePixelRatio live for the backing store (resize path), the
// per-frame canvas transform, and the shadow-canvas picking coords. 2D only —
// the WebGL renderer is not fill-bound and keeps its native ratio.
// Captured BEFORE the override, and re-read on resize: a browser zoom or a drag
// to a HiDPI monitor changes the real ratio, and once the getter below is in
// place no later read can see it — a one-shot snapshot left the canvas at the
// old device ratio for the rest of the session.
const dprNative = (() => {
  const d = Object.getOwnPropertyDescriptor(window, "devicePixelRatio");
  const get = d && d.get;
  const seed = window.devicePixelRatio || 1;
  return () => (get ? get.call(window) : seed) || 1;
})();
let DPR_REAL = dprNative();
const DRS_SCALE = 0.5;
let dprScale = 1;
Object.defineProperty(window, "devicePixelRatio",
  { get: () => DPR_REAL * dprScale, configurable: true });
window.addEventListener("resize", () => { DPR_REAL = dprNative(); });

function setRes(scale) {
  if (dprScale === scale || !Graph || !is2D()) return;
  dprScale = scale;
  // A same-value set still runs the lib's resize path: it rebuilds both
  // canvases at the new ratio (verified live) — and leaves them BLANK, so a
  // repaint must follow. Going low, the stream's next frame is that repaint;
  // going crisp, paint the one frame here and hand the loop to the budget.
  //
  // The resize path also re-centers: it estimates the old CSS size as
  // canvas.width / devicePixelRatio and shifts the zoom transform by half the
  // difference. That estimate divides OLD pixels by the NEW ratio, so on a
  // ratio change (the only reason we are here) it is wrong by exactly the
  // ratio step and the camera lurches half a screen per transition. The CSS
  // size never changes in this resize, so the correct shift is zero: save the
  // camera, let the lib do its arithmetic, put the camera back.
  const z = Graph.zoom(), c = Graph.centerAt();
  Graph.width(Graph.width()).height(Graph.height());
  Graph.zoom(z);
  Graph.centerAt(c.x, c.y);
  // Both directions paint one frame right here: the resize left the canvases
  // blank, and a repaint may not otherwise be due (a grab that has not moved
  // yet, a restore on a parked loop). That paint re-enters drsOnPaint, which
  // re-arms the restore timer — an engage with no follow-up stream still finds
  // its way back to crisp. renderBudget re-derives the loop state the pause
  // just clobbered.
  Graph.resumeAnimation(); Graph.pauseAnimation(); renderBudget();
}

// The burst detector below covers streams that no pointer announces (layout
// settle, slider reheat, camera tweens): three rapid repaints in full-rate
// mode prove a stream. Pointer-driven camera work does NOT wait for that
// warm-up — its three full-res paints cost 150-270ms at the START of every
// pan stroke, and with pan-pause-pan exploration each resume paid it again,
// which read as "panning the settled graph is slow". A drag or a wheel notch
// is motion by declaration: the graph-wrap listener at the bottom of this
// file calls setRes(DRS_SCALE) directly, before the first heavy paint.
let drsTimer = null, drsLast = 0, drsBurst = 0;
function drsOnPaint() {
  const now = performance.now();
  const streaming = simRunning || now < awakeUntil;
  if (streaming) {
    drsBurst = now - drsLast < 250 ? drsBurst + 1 : 0;
    // Deferred: resizing from inside the paint would clear the canvas mid-frame.
    if (drsBurst >= 3 && dprScale === 1) setTimeout(() => setRes(DRS_SCALE));
  } else drsBurst = 0;
  drsLast = now;
  clearTimeout(drsTimer);
  // Only a STREAMING paint arms the restore timer. The idle particle tick paints
  // every 1000/IDLE_FPS = 50ms, which is shorter than this window — so letting it
  // re-arm meant the timer could never elapse and one pan left the settled graph
  // permanently soft, inverting this block's whole premise. What the idle tick
  // paints IS stillness, so it restores immediately instead. The restore paint
  // re-enters here with dprScale already 1, which arms nothing: no oscillation.
  if (streaming) drsTimer = setTimeout(() => setRes(1), 250);
  else if (dprScale !== 1) drsTimer = setTimeout(() => setRes(1));
}

// zoomToFit measures the SCENE, not the data. 3d-force-graph's getGraphBbox
// unions the node meshes' world boxes, and a mesh only takes its node's
// position on a rendered frame — so fitting before the first frame measures
// 1,445 meshes stacked at the origin and returns the largest node RADIUS as the
// graph's extent. Measured: bbox +/-14 against positions spanning +/-2,300, and
// a camera parked 41 units from the centre of a 3,900-unit graph, which is what
// "the 3D view opens fully zoomed in" actually is.
//
// So the fit waits for a frame it can trust. wake() is first because the budget
// loop may be parked, and a paused loop paints nothing to wait for; the two
// frames then cover our own callback landing ahead of the library's render on
// the same tick.
function fitWhenPainted(G) {
  wake(1200);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    G.zoomToFit(400, 40);
    wake(600);
  }));
}

// Every force the layout runs sees the notes alone (ADR-0066 decision 3: the
// rim's papers are pinned and never move a note). Weight zero was not enough:
// d3's link force splits each pull by the degree of its two ends, so a COVERS
// edge counted there shifts every other link of the note it reaches, and a
// pinned paper in the many-body quadtree regroups the notes around it. So the
// rim nodes never reach initialize() and the COVERS edges never reach links().
// Filtering the nodes keeps each note's index because the rim rides at the end
// of ALL_NODES; the COVERS ends are resolved here, which d3 would otherwise do.
function rimless(real) {
  const f = alpha => real(alpha);
  f.initialize = (nodes, ...rest) => real.initialize(nodes.filter(n => !isRim(n)), ...rest);
  f.links = arr => {
    if (arr === undefined) return real.links();
    arr.forEach(l => {
      if (l.type === "COVERS") { l.source = NODE_BY_ID[l.from]; l.target = NODE_BY_ID[l.to]; }
    });
    real.links(arr.filter(l => l.type !== "COVERS"));
    return proxy;
  };
  const proxy = new Proxy(f, {
    get(t, k) {
      if (k === "initialize" || (k === "links" && real.links)) return t[k];
      const v = real[k];
      if (typeof v !== "function") return v;
      return (...a) => { const r = v.apply(real, a); return r === real ? proxy : r; };
    },
  });
  return proxy;
}

function buildGraph() {
  // Direct write, not setRes: the old instance is about to die, and the new
  // one (either renderer) must initialize its canvas at the native ratio.
  dprScale = 1;
  const el = document.getElementById("graph");
  if (Graph) {
    // Loud on failure: a swallowed teardown leaks the WebGL context, and the
    // browser caps them at ~16 — the graph would go black after enough mode
    // switches with nothing in the console to say why.
    try { Graph._destructor(); } catch (e) { console.warn("graph teardown failed", e); }
    el.innerHTML = "";
    LinkSeg = null;   // died with the scene; the next 3D build remakes it
  }
  const G = is2D() ? new ForceGraph(el) : new ForceGraph3D(el);
  syncCanvasSize(G);   // the bundle's default is the size at its load, not now
  // Seeded positions and the decay that goes with them, decided before the data
  // lands: on a cache hit the whole rerelaxation fits inside the warmup, so the
  // graph appears already settled instead of unfolding a layout you have
  // watched unfold before.
  const seeded = loadLayout();
  const warm = WARMUP_TICKS(seeded);
  warmupRun = warm; simTicks = 0;
  paintStat("settling");
  G.warmupTicks(warm)
    .d3AlphaMin(ALPHA_MIN)
    .d3AlphaDecay(seeded ? FAST_DECAY : COLD_DECAY)
    .cooldownTicks(Infinity)   // alpha is the gate; see ALPHA_MIN
    .cooldownTime(Infinity)
    .backgroundColor(GP.bg)
    .linkSource("from").linkTarget("to")
    .nodeVal("size")
    .nodeColor(nodeColor)
    .linkColor(linkPaint)
    // Motion. The line above this one used to read "structural gaps have no dash
    // in WebGL, mark them by motion instead", which was true of the per-link
    // THREE.Lines this frame drew before the merge and is not true of what the
    // merge left behind: LineSegments draws disjoint vertex pairs, so a dash is
    // what that buffer natively IS (see writeLinkSegPositions). GAP now carries
    // both, and needs to - with PARTICLES on it is lib-owned (libOwnsLink),
    // which puts it outside the merge and out of reach of the dash, so motion is
    // the only channel it keeps in that config. SIMILAR is solid by design (see
    // ABSENT) and motion is the whole of what marks it. Amber particles stream
    // along the absent bridge. The similarity layer gets the
    // same treatment at a fraction of the weight: one particle instead of two,
    // pre-dimmed almost into the background, and six times the speed, so two
    // thousand of them read as a faint drift through the semantic neighbourhood
    // rather than as two thousand moving dots. Both stop when the link is dimmed
    // or filtered out, so focus mode stays quiet and unticking Similar takes its
    // meshes with it. Same binding in both libs, so the layers read the same
    // either way.
    .linkDirectionalParticles(l => (!PARTICLES || l._dim || l._hidden) ? 0
      : l.type === "GAP" ? 2 : l.type === "SIMILAR" ? 1 : 0)
    .linkDirectionalParticleSpeed(l => l.type === "SIMILAR" ? 0.06 : 0.01)
    .linkDirectionalParticleColor(l => l.type === "SIMILAR"
      ? (LIGHT ? DATA.colors.similar_particle_paper : DATA.colors.similar_particle)
      : (LIGHT ? DATA.colors.gap_paper : DATA.colors.gap))
    .linkDirectionalParticleWidth(l => l.type === "SIMILAR" ? 1.5 : 2)
    .nodeVisibility(n => !n._hidden)
    .linkVisibility(l => !l._hidden && !quietNow(l))
    .onNodeClick((node) => { selectNode(node); applyFocus(node.id); })
    // 3D only: 2D picks its hover itself (pickHover2D). One argument, since
    // the lib's second is the previous node.
    .onNodeHover(n => { if (!is2D()) hoverNote(n); })
    .onBackgroundClick(() => { closeDrawer(); clearFocus(); })
    // The alpha gate and a drag deadlock each other. Both bundles reheat a drag
    // the d3 way — d3AlphaTarget(0.3).resetCountdown() on every drag event — but
    // alpha only climbs toward that target INSIDE tick(), and tick() is exactly
    // what the gate refuses to run while alpha still sits at the settled value
    // it stopped on. So the engine stops again on the same frame, forever. The
    // grabbed node still follows the pointer (both libs write its position
    // directly), which is why the symptom is one node moving through a graph
    // that has stopped answering. Lift the gate for the drag and let alpha do
    // what it was going to do; dragend puts the gate back, alphaTarget returns
    // to 0, and the layout settles into onEngineStop as usual.
    .onNodeDrag(() => {
      if (simRunning) return;
      simRunning = true;
      G.d3AlphaMin(0);
      renderBudget();   // a still pointer stops waking the loop mid-drag
    })
    .onNodeDragEnd(() => G.d3AlphaMin(ALPHA_MIN))
    .onEngineTick(() => { simTicks++; descendTick(); })
    .onEngineStop(() => {
      simRunning = false;
      residualTicks = 0;
      paintStat("settled in " + gnum(warmupRun + simTicks) + " ticks");
      for (const k in mstMemo) delete mstMemo[k];  // positions just moved
      blobMemo = null;
      saveLayout();
      measureGraphRadius();   // the label thresholds are a fraction of it
      if (RIM) placeRim();
      if (fitPending) { fitPending = false; fitWhenPainted(G); }
      else renderBudget();
    });

  if (is2D()) {
    // Width 0 is invisible on canvas (GL draws a 1px line for it, 2D draws
    // nothing), and the labels ARE the point here, so pay for them. Width is
    // per-edge because on canvas it is free, and it is the second half of the
    // rank the alpha starts: a wikilink is both stronger and thicker than the
    // similarity that a model proposed beside it.
    G.linkWidth(l => l.width || 1)
      // The dash the 3D side has to build by hand (see LinkSeg), free here:
      // the canvas painter ends on setLineDash(accessor || []). Paint-time and
      // untriggered, so a family that costs one buffer rewrite over there costs
      // nothing at all on this side.
      //
      // The pattern is in graph units, and the painter runs inside
      // the zoom transform, so dashes lengthen as you zoom in. It reads as the
      // whole picture growing rather than as a pattern changing, which is why
      // it stays. Screen-space would mean owning the link painter outright
      // (linkCanvasObject) to divide by scale the way drawNode does its font.
      .linkLineDash(l => isDashed(l) ? DASH_2D : null)
      // After the lib's own stroke, on the citations only: any other mode
      // value leaves a link to the default painter alone.
      .linkCanvasObjectMode(l => l.type === "CITES" ? "after" : null)
      .linkCanvasObject(drawCiteArrow)
      .nodeRelSize(NODE_REL_SIZE)
      // 2D names every note on the canvas and hovers nothing, except a paper
      // or a missing paper: its canvas name is the short form ("Xu 2025"),
      // and the title or the reference has nowhere else to go.
      .nodeLabel(n => SQUARE[n.type] || NATIVE[n.type]
        ? '<div class="g3d-tip"><b>' + escHtml(n.label) + '</b>' + paperTip(n) + '</div>' : "")
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint(paintNodeArea)
      // The picking canvas repaints on a ~800ms debounce, and its link pass
      // strokes every edge at width+4px — 40-80ms on this vault, landing as a
      // visible hitch once or twice a second through any pan. Nothing hovers
      // or clicks a link here (no linkLabel, no onLinkHover), so the pass
      // buys nothing: paint no link areas at all. Node picking keeps its own
      // painter above. Measured: worst 2D frame 78ms -> 9ms.
      .linkPointerAreaPaint(() => {})
      // Pre, not Post: a zone is the ground the notes stand on. 2D only — the
      // 3D bundle hands out no THREE, so there the zones are colour and name.
      // Every real 2D repaint passes here, which is what makes it the seam
      // where the resolution governor watches for streams.
      // An arrangement is its own picture: its wedges replace the areas, the
      // zones and the rim, which are all drawings of the free layout.
      .onRenderFramePre((ctx, scale) => {
        drsOnPaint();
        if (ARR) { drawWedges(ctx, scale); return; }
        drawAreaBlobs(ctx, scale); drawAreaRibbons(ctx, scale); drawZones(ctx, scale);
        drawRim(ctx, scale);
      })
      .onRenderFramePost((ctx, scale) => ARR ? drawArrangedNames(ctx, scale) : drawAreaNames(ctx, scale));
  } else {
    // Perf on big vaults (1200+ notes): the bundle gives every link its own
    // THREE.Line — a draw call and a per-tick buffer write each. The links are
    // drawn merged instead (see LinkSeg above), and the visibility accessor
    // admits exactly one lib line into existence: the constructor donor. A
    // falsy accessor result skips object creation entirely, so the other 9k
    // Lines and their materials are never built at all. linkWidth 0 keeps the
    // donor a cheap Line rather than a cylinder mesh. The sphere detail and the
    // bucketed value belong to the flat mark, not to perf (see NODE_RES).
    G.linkWidth(0)
      .nodeResolution(SHADING ? NODE_RES : 6).nodeVal(nodeVal3D)
      .linkVisibility(l => l === RAW_EDGES[0] || (libOwnsLink(l) && !l._hidden))
      // "handled" for merged links and for the donor, which stays degenerate;
      // lib-owned particle carriers keep the default update and really move.
      .linkPositionUpdate((o, coords, l) => !libOwnsLink(l))
      // 1 so the per-edge alpha in linkPaint is the final opacity rather than
      // being scaled by a second global. The bundle's default 0.2 is what put
      // every link at the same weight in the first place.
      .linkOpacity(1)
      // The bundle's own onboarding line, bottom centre of every scene it has
      // ever rendered. It is the single most recognisable thing about the
      // library, it teaches three mouse bindings nobody needed taught, and it
      // is not written in this app's voice.
      .showNavInfo(false)
      // 0.75 is the default, and at 0.75 every node shows through every other
      // one: the cluster reads as gas. Solid nodes plus the fog below carry the
      // depth instead, which is the reading that was wanted all along.
      .nodeOpacity(0.96)
      // The default tooltip is the bare label in the library's own black
      // rounded box. This one is a Silica compartment (see .float-tooltip-kap)
      // and it answers the question the hover is actually asking at a distance
      // the note labels do not reach: which cluster is this, and is it special.
      //
      // The community comes first because that is what the node's colour means,
      // always. The zone is APPENDED when its layer is up, never substituted:
      // in 3D there are no hulls to carry it (onRenderFramePre is 2D-only), so
      // without this the semantic layer would be floating names and nothing an
      // individual note could be checked against.
      // The zone gets its OWN line rather than joining the first: community
      // labels already contain " · " inside themselves, so appending to them
      // produced "etica · sistemi · zone: etica · morale", one run with no seam
      // where the second partition starts.
      .nodeLabel(n => {
        const bits = [];
        if (areaOf(n)) bits.push(areaOf(n));
        const st = nodeState(n);
        if (st !== "note") bits.push(st);
        const zone = (showZones && n.sgroup >= 0) ? ZONE_LABEL[n.sgroup] : null;
        // Appended on the same terms as the zone. The mark carries the ring now
        // (RING_S_PX), so this line is no longer the only place the reading
        // lands - but the ring says load-bearing and cannot say how MANY
        // strands, and it is gone below RING_MIN_PX of disc, which is most of a
        // zoomed-out frame.
        const cut = n.cut
          ? "load-bearing · strands " + (n.strands || 0) : null;
        return '<div class="g3d-tip"><b>' + escHtml(n.label) + '</b>' + paperTip(n) +
          (bits.length ? '<i>' + escHtml(bits.join(" · ")) + '</i>' : '') +
          (zone ? '<i>zone ' + escHtml(zone) + '</i>' : '') +
          (cut ? '<i>' + escHtml(cut) + '</i>' : '') + '</div>';
      });
  }
  if (RIM_NODES.length)
    for (const name of ["link", "charge", "center"]) G.d3Force(name, rimless(G.d3Force(name)));
  // Forces before the data, and the data last of all. The warmup loop runs the
  // moment graphData lands, so anything set afterwards shapes only the animated
  // tail: with the tuned forces arriving late, the bulk of the layout was being
  // built by d3's untouched defaults and then nudged at low alpha. Widening the
  // warmup made that worse, which is how it surfaced.
  applyForces(false, G);
  residualTicks = 0;   // a rebuild is a cold start, whatever a descent was doing
  G.graphData({ nodes: ALL_NODES, links: ALL_EDGES });
  return G;
}

// Switching preserves everything that is not the camera: edge filters, the
// community filter, the focused set and the search box all live outside the
// instance. The camera cannot be preserved — there is no sane mapping from a 3D
// camera to a 2D pan/zoom — so it refits once the new layout settles.
function setMode(m) {
  if (m === mode && Graph) return;
  const rebuild = Graph !== null;   // a switch always refits; a first build may not
  // The plane of a 3D arrangement is not a 2D one: put the layout back, build,
  // and arrange the same focus again in the new mode.
  const rearrange = !!ARR;
  disarrange(false);
  leaveAreas();
  mode = m;
  try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
  document.querySelectorAll("#mode-toggle button")
    .forEach(b => b.classList.toggle("active", b.dataset.mode === m));
  // The rings are a canvas draw. Leaving their legend up in 3D would promise a
  // channel that mode does not paint.
  document.getElementById("state-legend").style.display = is2D() ? "" : "none";
  const areasRow = document.getElementById("areas-row");
  if (areasRow) areasRow.style.display = is2D() ? "" : "none";
  placeRim();   // the rim is 2D only: built on the way in, sent home on the way out
  fitPending = fitPending || rebuild;
  // Flags BEFORE the build, repaint never: the new instance reads _hidden/_dim
  // in its first graphData() digest. Recomputing them afterwards used to cost
  // four more full digests to arrive at the state the build already had.
  computeFilters();
  computeFocus(focusIds);
  snapFade();
  simRunning = true;
  Graph = buildGraph();    // owns the forces now: they must precede graphData
  window.__G = Graph;      // console/harness handle: frame-cost probes in hidden tabs
  styleScene();            // first frame already lit; a no-op if 2D
  syncZoneLoop();          // the note-name layer is 3D-only, so the mode owns it
  renderBudget();
  if (rearrange) arrangeFocus();
}

// The scope picker is the app's, and its Folders, Areas and Zones come from
// /shape. Zotero collections cannot: membership is read live at THIS build
// and never stored (graph_view._stamp_collections), so the frame says which
// collections it can scope to, and how many papers it holds for "Papers
// only". A frame built while Zotero was closed announces no collection.
//
// Since ADR-0066 the rim's papers count too: `outside` is a collection's papers
// with no leaf here and `covered` those a note covers, which is what the
// picker's "name · covered of total" reads; `covers` is the last pass's state,
// which is how the app knows whether a collection scope has to start one.
function announceScopes() {
  if (window.parent === window) return;
  const size = {}, outside = {}, covered = {};
  let papers = 0, hitAll = 0;
  const cols = n => (n.paper && n.paper.collections) || [];
  RAW_NODES.forEach(n => {
    if (n.type === "paper") papers++;
    cols(n).forEach(c => { size[c] = (size[c] || 0) + 1; });
  });
  RIM_NODES.forEach(n => {
    const hit = !!(n.covers && n.covers.length);
    if (hit) hitAll++;
    cols(n).forEach(c => {
      outside[c] = (outside[c] || 0) + 1;
      if (hit) covered[c] = (covered[c] || 0) + 1;
    });
  });
  const names = [...new Set(Object.keys(size).concat(Object.keys(outside)))].sort();
  window.parent.postMessage({
    type: "silica-scope-groups", papers,
    collections: names.map(name => ({ name, size: size[name] || 0,
      outside: outside[name] || 0, covered: covered[name] || 0 })),
    rim: { outside: RIM_NODES.length, covered: hitAll },
    covers: DATA.covers || { state: "none" },
  }, "*");
}

// Slider multipliers persist across sessions; the baseline is never persisted
// (recomputed from the current graph each load).
const FORCES_KEY = "silica-graph-forces";
let forceMul = { repel: 1, dist: 1, center: 1 };
try {
  Object.assign(forceMul, JSON.parse(localStorage.getItem(FORCES_KEY)) || {});
} catch (e) { /* corrupt or blocked storage -> auto defaults */ }

// G is passed explicitly during a build, where the instance is not yet the
// global one: the forces have to be in place before graphData runs the warmup.
function applyForces(reheat, G, decay = FAST_DECAY) {
  G = G || Graph;
  const held = areasOn();
  // distanceMax bounds both over-dispersion and per-tick cost on big graphs.
  // Held, it only spaces neighbours: the discs place the areas, and a charge
  // reaching across them inflated every area into a string of beads on the
  // prototype (ADR-0074).
  G.d3Force("charge").strength(baseCharge() * forceMul.repel)
    .distanceMax(held ? HOLD_REACH * baseDist() * forceMul.dist : 600 * FORCE_SCALE);
  G.d3Force("areas", held ? holdForce : null);
  // heldLinkStrength(null) is d3's default rule: the areas going off on the same
  // instance have to put it back, which d3 offers no accessor for.
  G.d3Force("link").distance(baseDist() * forceMul.dist)
    .strength(heldLinkStrength(held ? AREA_DISCS : null));
  // Center capped at 1: d3 forceCenter shifts positions directly, >1 oscillates.
  G.d3Force("center").strength(Math.min(1, forceMul.center));
  // A slider moves an already-settled layout, so the reheat is a perturbation,
  // not a cold start: the fast decay is the right budget for it, and without it
  // dragging a slider would cost the full 300-tick schedule every time. The
  // areas coming on move every area to its disc, which is a cold start.
  if (reheat) {
    G.d3AlphaDecay(decay).d3ReheatSimulation();
    simRunning = true;
    renderBudget();
  }
}

// Log-scale track for the multiplier sliders: x1 sits mid-track and the
// useful 0.2-1 range gets half the travel instead of a sliver.
const fromSlider = v => Math.pow(10, +v);
const toSlider = m => Math.log10(m);

function syncForceUI() {
  document.getElementById("sl-repel").value = toSlider(forceMul.repel);
  document.getElementById("sl-dist").value = toSlider(forceMul.dist);
  document.getElementById("sl-center").value = forceMul.center;
  document.getElementById("fv-repel").textContent = forceMul.repel.toFixed(1) + "\u00d7";
  document.getElementById("fv-dist").textContent = forceMul.dist.toFixed(1) + "\u00d7";
  document.getElementById("fv-center").textContent = (+forceMul.center).toFixed(2);
}

function onForceSlider() {
  forceMul.repel = fromSlider(document.getElementById("sl-repel").value);
  forceMul.dist = fromSlider(document.getElementById("sl-dist").value);
  forceMul.center = +document.getElementById("sl-center").value;
  try { localStorage.setItem(FORCES_KEY, JSON.stringify(forceMul)); } catch (e) {}
  syncForceUI();
  applyForces(true);
}

function resetForces() {
  forceMul = { repel: 1, dist: 1, center: 1 };
  try { localStorage.removeItem(FORCES_KEY); } catch (e) {}
  syncForceUI();
  applyForces(true);
}

syncForceUI();

// --- Areas held and drawn as blobs (ADR-0074), 2D only ----------------------
// 3D keeps its layout as it was: a blob is a 2D drawing, like a zone's hull,
// and holding the areas without one only flattens the cloud. Off when the frame
// opens, like the zones, and off again on every mode switch: the areas are a
// view of the layout on screen, and the frame always opens on the free one.
let showAreas = false;
// The areas the legend lists, which are the ones with a hub to be named by:
// graph_view keeps comm_hubs to the multi-note areas.
const HELD_AREAS = Object.keys(COMM_HUBS);
const AREA_INDEX = {};
HELD_AREAS.forEach((g, k) => { AREA_INDEX[g] = k; });
const areasOn = () => showAreas && is2D() && HELD_AREAS.length > 0;
// area -> [x, y, r] in link rest lengths, placed when the areas come on.
let AREA_DISCS = {};
// One unit, a link rest length, in graph units, scaled so a blob's edge lands
// on its disc's rim. Measured in the app on the test vault 2026-10-07 at 1.15:
// the edge (the 90th percentile note's distance from the centroid, plus the
// blob's reach) sat at 0.62-0.93 of the disc for every area of 9 notes or
// more, median 0.77, which left a moat around each blob on top of AREA_GAP.
// 0.9 is 1.15 x 0.78; the smallest areas overflow their discs and meet their
// neighbours at a shared border, the dense mass the owner asked for.
const discUnit = () => 0.9 * baseDist() * forceMul.dist;
const HOLD_REACH = 3;   // the held charge, in rest lengths
const holdForce = Object.assign(
  alpha => holdAreas(RAW_NODES, AREA_DISCS, discUnit(), alpha),
  { initialize() {} });   // reads RAW_NODES, so the rim never reaches it (ADR-0066)

// What placeDiscs needs from the vault, fixed for the life of the frame: a disc
// whose area is its note count (0.62 sqrt(notes), the radius area_discs used),
// and the linked pairs between two areas, counted as area_tally counts them.
const AREA_RADIUS = {}, AREA_INTER = [];
(() => {
  const size = {}, pairs = new Map(), seen = new Set();
  RAW_NODES.forEach(n => {
    if (AREA_INDEX[n.group] !== undefined && n.type !== "ghost") size[n.group] = (size[n.group] || 0) + 1;
  });
  HELD_AREAS.forEach(g => { AREA_RADIUS[g] = 0.62 * Math.sqrt(size[g] || 1); });
  RAW_EDGES.forEach(e => {
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    if (e.type !== "EXTRACTED" || !a || !b || a.group === b.group) return;
    if (AREA_INDEX[a.group] === undefined || AREA_INDEX[b.group] === undefined) return;
    const pair = e.from < e.to ? e.from + "\n" + e.to : e.to + "\n" + e.from;
    if (seen.has(pair)) return;   // a mutual link is one pair, as area_tally has it
    seen.add(pair);
    const key = a.group < b.group ? a.group + "|" + b.group : b.group + "|" + a.group;
    pairs.set(key, (pairs.get(key) || 0) + 1);
  });
  pairs.forEach((w, key) => AREA_INTER.push([...key.split("|"), w]));
})();
RAW_NODES.forEach(n => {
  n._loose = AREA_INDEX[n.group] === undefined && n.type !== "ghost" && n.type !== "missing" && !NATIVE[n.type];
  if (NATIVE[n.type] || n._loose)
    n._follow = [...(neighbors[n.id] || [])].map(id => NODE_BY_ID[id])
      .filter(m => m && !isRim(m) && (NATIVE[n.type] || (m.type !== "ghost" && m.type !== "missing")));
});

// Each area's centroid on screen, in disc units, is where its disc starts.
function discsFromLayout() {
  const acc = {}, u = discUnit(), start = {};
  for (const n of RAW_NODES) {
    if (AREA_INDEX[n.group] === undefined || n.type === "ghost" || typeof n.x !== "number") continue;
    const c = acc[n.group] || (acc[n.group] = [0, 0, 0]);
    c[0] += n.x; c[1] += n.y; c[2]++;
  }
  for (const g in acc) start[g] = [acc[g][0] / acc[g][2] / u, acc[g][1] / acc[g][2] / u];
  return placeDiscs(start, AREA_RADIUS, AREA_INTER);
}

// The free layout the areas were switched on from, put back when they go off:
// the areas gather each area in place, so the way back is where it started.
// Put back as it was and not rerelaxed, when it had settled: a fast reheat
// from it still carried one note 6 rest lengths (test vault, 2026-10-07).
let freePos = null, freeSettled = false;
function setAreas(on) {
  const cb = document.getElementById("cb-areas");
  if (cb) cb.checked = on;
  if (on === showAreas) return;
  showAreas = on;
  if (!Graph || !is2D()) return;
  blobMemo = null;
  if (on) {
    freePos = RAW_NODES.map(n => [n.x, n.y]);
    freeSettled = !simRunning;
    AREA_DISCS = discsFromLayout();
    fitPending = true;
    applyForces(true, Graph, COLD_DECAY);
  } else {
    const back = !!freePos;
    leaveAreas();
    if (back && freeSettled) { applyForces(false); fitGraph(); }
    else { fitPending = true; applyForces(true); }
  }
  applyFilters();
}
function updateAreaFilter() {
  disarrange();   // the areas hold the free layout, not an arrangement of it
  const cb = document.getElementById("cb-areas");
  setAreas(!!(cb && cb.checked));
}
// A mode switch leaves the areas: the positions go back to the free layout
// before the new instance reads them, with no reheat on the old one.
function leaveAreas() {
  if (freePos) RAW_NODES.forEach((n, i) => { n.x = freePos[i][0]; n.y = freePos[i][1]; n.vx = n.vy = 0; });
  freePos = null;
  showAreas = false;
  const cb = document.getElementById("cb-areas");
  if (cb) cb.checked = false;
}
// A quiet line (computeFilters) shows only as a line of the focused note, and
// under a focus the areas show that note's lines alone: a dimmed line is
// painted in the floor's colour, a dark scratch across every blob it crosses.
const quietNow = l => (l._quiet || (l._dim && areasOn())) && !(focusIds.length && !l._dim);

// The blob's reach, in rest lengths, and the field level its edge sits on. The
// owner's values from the prototype, 2026-10-07; at TAU a lone note's blob is
// 0.64 R across and two notes up to 1.5 R apart fuse.
const BLOB_PAD = 0.85, BLOB_TAU = 0.35;
const BLOB_ALPHA = LIGHT ? 0.13 : 0.17;   // a ground under the notes, as ZONE_ALPHA is
const RIBBON_ALPHA = LIGHT ? 0.4 : 0.34;

// Recomputed every frame while the layout moves, served from here once it
// settles; onEngineStop and computeFilters clear it. Each area also gets a rim,
// 1.25 x the RMS distance of its notes from their centroid plus a quarter rest
// length, where its ribbons leave it.
// ponytail: blobGeometry allocates its grids per call. 25 runs on the test
// vault 2026-10-07: median 3.2 ms, max 24.7 ms, the max a collection. Keep
// the buffers between frames when a settle's max passes 33 ms (a frame at 30 fps).
let blobMemo = null, blobsNow = null;
function areaBlobs() {
  if (blobMemo && !simRunning) return blobMemo;
  const pts = [], color = [];
  for (const n of RAW_NODES) {
    const k = AREA_INDEX[n.group];
    if (k === undefined || n._hidden || n._ctx || typeof n.x !== "number") continue;
    pts.push({ x: n.x, y: n.y, k });
    if (!color[k]) color[k] = baseColor(n);
  }
  const R = BLOB_PAD * baseDist() * forceMul.dist;
  const geo = blobGeometry(pts, R, BLOB_TAU), rim = geo.map(() => 0);
  for (const p of pts) rim[p.k] += (p.x - geo[p.k].x) ** 2 + (p.y - geo[p.k].y) ** 2;
  geo.forEach((g, k) => { rim[k] = g.n ? 1.25 * Math.sqrt(rim[k] / g.n) + 0.25 * baseDist() * forceMul.dist : 0; });
  const ribbons = ribbonPairs(RAW_EDGES.filter(e => e._ribbon && !e._hidden)
    .map(e => [NODE_BY_ID[e.from].group, NODE_BY_ID[e.to].group]));
  return (blobMemo = { geo, color, rim, ribbons });
}

// Under the notes, and not with the zones up: the zone layer owns the ground
// then, and two partitions' regions on one floor read as neither (ADR-0023).
function drawAreaBlobs(ctx, scale) {
  blobsNow = null;
  if (!areasOn() || showZones) return;
  blobsNow = areaBlobs();
  blobsNow.geo.forEach((g, k) => {
    if (!g.n) return;
    ctx.save();
    ctx.fillStyle = ctx.strokeStyle = blobsNow.color[k];
    ctx.beginPath();
    for (const p of g.fill) {
      ctx.moveTo(p[0], p[1]);
      for (let i = 2; i < p.length; i += 2) ctx.lineTo(p[i], p[i + 1]);
      ctx.closePath();
    }
    ctx.globalAlpha = BLOB_ALPHA;
    ctx.fill();
    ctx.beginPath();
    for (const l of g.line) { ctx.moveTo(l[0], l[1]); ctx.lineTo(l[2], l[3]); }
    ctx.globalAlpha = 0.85;
    ctx.lineWidth = 1.3 / scale;
    ctx.stroke();
    ctx.restore();
  });
}

// One ribbon per pair of areas that RIBBON_MIN lines or more join, from rim
// to rim, each end facing the other area, bowed a tenth of its length so two
// ribbons between neighbours do not lie on one line. Its width grows with the
// square root of its lines, in screen pixels. Faded under a focus, where the
// focused note's own lines are the reading.
function drawAreaRibbons(ctx, scale) {
  if (!blobsNow) return;
  const { geo, color, rim, ribbons } = blobsNow;
  const port = (k, o) => {
    const dx = geo[o].x - geo[k].x, dy = geo[o].y - geo[k].y, d = Math.hypot(dx, dy) || 1;
    return [geo[k].x + dx / d * rim[k], geo[k].y + dy / d * rim[k]];
  };
  ctx.save();
  ctx.lineCap = "round";
  ctx.globalAlpha = RIBBON_ALPHA * (focusIds.length ? 0.35 : 1);
  for (const [a, b, n] of ribbons) {
    const i = AREA_INDEX[a], j = AREA_INDEX[b];
    if (!geo[i].n || !geo[j].n) continue;
    const p = port(i, j), q = port(j, i), dx = q[0] - p[0], dy = q[1] - p[1];
    const grad = ctx.createLinearGradient(p[0], p[1], q[0], q[1]);
    grad.addColorStop(0, color[i]);
    grad.addColorStop(1, color[j]);
    ctx.strokeStyle = grad;
    ctx.lineWidth = (0.6 + 0.55 * Math.sqrt(n)) / scale;
    ctx.beginPath();
    ctx.moveTo(p[0], p[1]);
    ctx.quadraticCurveTo((p[0] + q[0]) / 2 - dy * 0.1, (p[1] + q[1]) / 2 + dx * 0.1, q[0], q[1]);
    ctx.stroke();
  }
  ctx.restore();
}

// The area's name over its blob, only below the zoom where the canvas starts
// naming notes (drawNode, 0.6): above it the hub's own label says it again.
// Held areas nearly touch, so two names can land on each other; the bigger
// area keeps its name. Keys are area ids, which run in size order
// (detect_communities), and an object walks integer keys ascending.
function drawAreaNames(ctx, scale) {
  if (!blobsNow || scale >= 0.6) return;
  const taken = [];
  ctx.save();
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  blobsNow.geo.forEach((g, k) => {
    if (!(g.n >= 9 || (g.n >= 4 && scale > 0.35))) return;
    const hub = COMM_HUBS[HELD_AREAS[k]] || "";
    const name = hub.length > 30 ? hub.slice(0, 29) + "\u2026" : hub;
    const sub = g.n + " notes";
    ctx.font = "600 " + (13 / scale) + "px Archivo, system-ui, sans-serif";
    const hw = ctx.measureText(name).width / 2, hh = 17 / scale;
    if (taken.some(t => Math.abs(t[0] - g.x) < t[2] + hw && Math.abs(t[1] - g.y) < 2 * hh)) return;
    taken.push([g.x, g.y, hw]);
    ctx.lineWidth = 4 / scale;
    ctx.strokeStyle = GP.bg;
    ctx.strokeText(name, g.x, g.y - 7 / scale);
    ctx.fillStyle = GP.label;
    ctx.fillText(name, g.x, g.y - 7 / scale);
    ctx.font = (12 / scale) + "px Archivo, system-ui, sans-serif";
    ctx.strokeText(sub, g.x, g.y + 9 / scale);
    ctx.fillStyle = GP.ghostLabel;
    ctx.fillText(sub, g.x, g.y + 9 / scale);
  });
  ctx.restore();
}

// Same split as computeFocus/applyFocus: flags on the shared objects here,
// repaint only when there is no rebuild coming to read them for free.
function inScope(n) {
  if (!SCOPE) return true;
  if (SCOPE.kind === "folder") return (n.id || "").startsWith(SCOPE.prefix);
  if (SCOPE.kind === "area") return n.group === SCOPE.id;
  if (SCOPE.kind === "zone") return n.sgroup === SCOPE.id;
  // The two Zotero kinds (ADR-0055) read the paper itself: its collections
  // are stamped on it live at build (graph_view._stamp_collections), so a
  // frame built while Zotero was closed has none to match.
  // A set since ADR-0066: one collection, several from the Library tab, or all.
  if (SCOPE.kind === "collection") {
    const cols = (n.paper && n.paper.collections) || [];
    return (SCOPE.ids || []).some(c => cols.includes(c));
  }
  // The library's citation graph: leaves and missing papers, never the rim.
  if (SCOPE.kind === "papers") return n.type === "paper" || n.type === "missing";
  return true;
}

function computeFilters() {
  // A ghost is the far end of an unresolved link, so it is drawn with those
  // links and only with them: 466 of 1,194 nodes floated with no line to
  // explain them while the row was off, which is its default (ADR-0058).
  const toggledOff = n => !showNotes || (n.type === "missing" && !showMissing)
    || (n.type === "ghost" && !showAmbiguous);
  RAW_NODES.forEach(n => {
    n._hidden = toggledOff(n) || (AB ? !(AB.a.has(n.id) || AB.b.has(n.id)) : !inScope(n));
    n._ctx = false;
  });
  // "Papers only" is the library's citation graph. Hiding the concept notes
  // takes their wikilinks along, but the k-NN joins two papers directly and
  // often, so the rule has to be on the edge as well as on the node.
  const citesOnly = !AB && SCOPE && SCOPE.kind === "papers";
  // An area or a zone on show keeps the far end of every wikilink that leaves
  // it: what a part reaches is the note across the link, and with the Highlight
  // group gone (ADR-0058) this is where the Graph still says it. A far end
  // shows only the links that reach it from the part, never its own.
  // A collection keeps the far ends of its leaves' wikilinks the same way
  // (ADR-0066 decision 5), and the far end of every COVERS edge below.
  const reach = !AB && showNotes && SCOPE &&
    (SCOPE.kind === "area" || SCOPE.kind === "zone" || SCOPE.kind === "collection");
  if (reach) {
    RAW_EDGES.forEach(e => {
      if (!((e.type === "EXTRACTED" && showExtracted) || (e.type === "AMBIGUOUS" && showAmbiguous))) return;
      const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
      if (a && b && inScope(a) !== inScope(b)) { a._hidden = false; b._hidden = false; }
    });
  }
  // The rim: under a collection scope only, in 2D only. The note at the far end
  // of a COVERS edge stays in both modes, so 3D still shows which notes cover
  // the scoped papers and leaves the papers themselves to the 2D drawing.
  const rimOn = !AB && showNotes && !!SCOPE && SCOPE.kind === "collection";
  RIM_NODES.forEach(n => { n._hidden = !(rimOn && is2D() && inScope(n)); });
  COVERING = rimOn ? new Map() : null;
  if (rimOn) {
    COVER_EDGES.forEach(e => {
      const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
      if (!a || !b || !inScope(a)) return;
      b._hidden = false;
      COVERING.set(b.id, (COVERING.get(b.id) || 0) + 1);
    });
    // The rest of the vault stays under the rim, faint and unnamed (ADR-0066
    // amendment): which notes the papers reach reads against where every
    // other note sits, as the mock drew it. Only what the scope hid comes
    // back; what a row of the key switched off stays off.
    RAW_NODES.forEach(n => {
      if (n._hidden && !toggledOff(n)) { n._hidden = false; n._ctx = true; }
    });
  }
  RAW_EDGES.forEach(e => {
    // Notes off takes the edges with it: an edge between two invisible nodes is
    // a line to nowhere, and the macro read is exactly the one that cannot
    // afford 2718 of them. Same for ONE invisible end: both renderers filter
    // links on linkVisibility alone and never look at the nodes', so a folder
    // scope hid its outsiders and kept every line to them (1385 wikilinks
    // around 171 notes). Read after the node pass and the far-end rule above.
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    const off = !showNotes || (a && a._hidden) || (b && b._hidden) ||
                (e.type === "EXTRACTED" && !showExtracted) ||
                (e.type === "AMBIGUOUS" && !showAmbiguous) ||
                (e.type === "GAP" && !showGaps) ||
                (e.type === "SIMILAR" && !showSimilar) ||
                (e.type === "PROPOSED" && !showProposed) ||
                (e.type === "COUPLED" && !showCoupled) ||
                (e.type === "CITES" && !showCites) ||
                (citesOnly && e.type !== "CITES");
    const beyond = reach && a && b && !inScope(a) && !inScope(b);
    // Under the rim the context keeps its wikilinks alone, faint: the k-NN
    // layer is 2,998 lines against 105 covers, and drawn even faint it is the
    // mat the covers would be read through. The mock drew the same.
    e._ctx = rimOn && beyond && !off && (e.type === "EXTRACTED" || e.type === "AMBIGUOUS");
    e._hidden = off || (beyond && !e._ctx);
  });
  // With the areas on (ADR-0074) only a wikilink inside one area is drawn
  // as it is; every other line is quiet (quietNow), and a line between two
  // areas counts into their ribbon.
  const held = areasOn();
  RAW_EDGES.forEach(e => {
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    e._quiet = held && !(a && b && keptInside(AREA_DISCS, e.type, a.group, b.group));
    e._ribbon = held && !!a && !!b && a.group !== b.group &&
      AREA_INDEX[a.group] !== undefined && AREA_INDEX[b.group] !== undefined;
  });
  COVER_EDGES.forEach(e => {
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    e._hidden = !rimOn || !a || !b || a._hidden || b._hidden;
  });
  blobMemo = null;   // a blob is drawn around the notes on show
  syncRimKey();
}

// The rim's key rows (ADR-0066 decision 5): present only while a collection is
// scoped, each with its count in scope. Before any pass every rim paper is
// drawn frost, so the first row counts them all and "not covered" waits for a
// verdict; after it, the two rows split the rim by how each square is drawn.
function syncRimKey() {
  if (!document.getElementById("covers-row")) return;
  const on = !AB && !!SCOPE && SCOPE.kind === "collection";
  const pending = !DATA.covers || DATA.covers.state === "none";
  const rim = on ? RIM_NODES.filter(inScope) : [];
  const hit = rim.filter(n => n.covers && n.covers.length).length;
  const reached = new Set();
  if (on) COVER_EDGES.forEach(e => { if (inScope(NODE_BY_ID[e.from])) reached.add(e.to); });
  const show = (id, yes) => { document.getElementById(id).style.display = yes ? "" : "none"; };
  const put = (id, v) => { document.getElementById(id).textContent = v; };
  show("covers-row", on);
  // In 3D the papers are not drawn: the row counts the notes that cover them
  // and says where the papers are (decision 3).
  put("covers-name", is2D() ? "covers" : "covering notes · rim in 2D");
  put("ct-covers", pending ? "…" : is2D() ? COVER_EDGES.filter(e => !e._hidden).length : reached.size);
  show("rim-row", on);
  put("ct-rim", pending ? rim.length : hit);
  show("uncovered-row", on && !pending);
  put("ct-uncovered", rim.length - hit);
}

// Pins the scoped papers on the rim, or sends them home when no collection
// scope draws one. The radius is 1.08 times the farthest note from the centre
// of the notes' box, measured now (decision 3), so it follows the layout the
// reader is looking at; fx/fy keep the simulation off the papers. A scope
// applied while the layout still settles is placed again when it stops.
function placeRim() {
  const on = !!SCOPE && SCOPE.kind === "collection" && is2D() && RIM_NODES.length > 0;
  if (!on) {
    if (RIM) { RIM_NODES.forEach(pinHome); RIM = null; }
    return;
  }
  const notes = RAW_NODES.filter(n => n.x != null && n.type !== "ghost" && n.type !== "missing");
  if (!notes.length) return;
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const n of notes) {
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x);
    y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y);
  }
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  let far = 0;
  for (const n of notes) far = Math.max(far, Math.hypot(n.x - cx, n.y - cy));
  const R = 1.08 * far;
  const { at, arcs } = rimLayout(RIM_NODES.filter(inScope), SCOPE.ids || []);
  RIM_NODES.forEach(n => {
    const t = at[n.id];
    if (t == null) { pinHome(n); return; }
    n.x = n.fx = cx + R * Math.sin(t);
    n.y = n.fy = cy - R * Math.cos(t);
  });
  RIM = { cx, cy, R, arcs };
  if (Graph) wake(120);   // pinned positions move nothing until a frame paints them
}

function applyFilters() {
  computeFilters();
  // Re-pass the current accessor to force a visibility refresh without resetting the physics layout
  Graph.nodeVisibility(Graph.nodeVisibility());
  // 3D link visibility lives in the merged buffer; the accessor re-pass stays
  // for 2D and, with PARTICLES on, for the lib-owned photon carriers.
  if (is2D() || PARTICLES) Graph.linkVisibility(Graph.linkVisibility());
  if (!is2D()) repaintLinkSeg();
  wake(120);   // and re-evaluate the idle tick: gaps may have just been toggled
}

function updateEdgeFilter() {
  showExtracted = document.getElementById("cb-extracted").checked;
  showAmbiguous = document.getElementById("cb-ambiguous").checked;
  showGaps = document.getElementById("cb-gaps").checked;
  const cbSim = document.getElementById("cb-similar");
  if (cbSim) showSimilar = cbSim.checked;
  // Rendered only where a paper or a missing paper exists (graph_view).
  const cbCites = document.getElementById("cb-cites");
  if (cbCites) showCites = cbCites.checked;
  const cbMissing = document.getElementById("cb-missing");
  if (cbMissing) showMissing = cbMissing.checked;
  // Each of the four is rendered only where its layer has something in it, so
  // each is read defensively rather than assumed into existence.
  const cbProp = document.getElementById("cb-proposed");
  if (cbProp) showProposed = cbProp.checked;
  const cbCoup = document.getElementById("cb-coupled");
  if (cbCoup) showCoupled = cbCoup.checked;
  // The ghost row has no switch of its own; it states that it follows this one.
  document.getElementById("st-ghost-row").classList.toggle("off", !showAmbiguous);
  applyFilters();
  // A focus lights what is on show (ADR-0088), so a switch changes what it
  // lights, and a single focus is arranged again over the new set.
  if (focusIds.length) applyFocus(focusIds);
}

// --- areas: named by their hub note ----------------------------------------
// The hub note's name, not the community's c-TF-IDF label: it is what the
// Areas surface and the scope picker call the area, and a title the reader
// wrote (ADR-0058). graph_view sends a name only for the areas of more than
// one note; for the rest this is "", which says nothing rather than repeating
// the note's own name as its area.
function areaOf(n) {
  return (Number.isInteger(n.group) && n.group >= 0 && COMM_HUBS[n.group]) || "";
}

// --- The semantic zone layer ------------------------------------------------
// Hull + name around the members of each k-NN Louvain cluster. Not a second
// view and not a second graph: the same frame, one more layer, so the two
// partitions are read against each other instead of one after the other.
//
// The k-NN FORCES are not toggled here and never were — d3's link force reads
// every link in graphData, and linkVisibility is a render-time accessor, so the
// SIMILAR edges pull whether or not they are drawn. That decoupling is what
// makes a hull honest: on a pure-wikilink layout the members of a semantic
// cluster sit all over the frame and its hull would be a lie about space.
// The region is a CORRIDOR OF CONSTANT WIDTH along the zone's minimum spanning
// tree, not a hull around it.
// A zone has to LOOK like one region — Louvain hands back a partition, and a
// grouping drawn as scattered islands reads as several. But a CONVEX hull buys
// that continuity by claiming the members are contiguous in SPACE, and they are
// not: the layout is driven by the wikilinks too, so a semantic zone is
// routinely scattered and its hull swallows whatever lies between the pieces.
//
// Measured on the 682-note vault, one settled layout, counting foreign notes
// that fall inside a zone's drawn region:
//
//   convex hull            675   1.88 regions over the average note   max 6
//   plain discs r=0.7·d    110   1.16                                 max 3   NOT continuous
//   MST corridor r=0.35·d  119   1.17                                 max 3   continuous
//
// So continuity is nearly free as long as the corridors are the SHORTEST set
// that joins the members — which is what a minimum spanning tree is. Striping
// every intra-zone k-NN edge instead was measured too: 2406 edges, many of them
// long sweeps across the frame, 300 foreign notes at a third of this width.
//
// What overlap survives is the real thing (spec §3): wikilink pull dragging a
// note into another zone's neighbourhood. The gaps stay gaps, which is the
// information the tessellation was rejected to keep.
//
// Width follows the layout, never a constant: baseDist() IS the link force's
// rest length, and it landed within 15% of this vault's measured median
// nearest-neighbour distance (84 vs 73). So the zones breathe with the vault's
// density and with the Link-distance slider instead of going blobby or grainy.
// Two widths, because one does not read: at a single width wide enough to fuse
// neighbouring branches the corridors claim the whole neighbourhood, and at one
// narrow enough to be honest the zone reads as a tangle of tubes rather than a
// territory. So the members get a BULB and the tree gets an ISTHMUS, and the
// isthmus only shows where the bulbs do not already touch.
const ZONE_BULB = () => 0.70 * baseDist() * forceMul.dist;   // fuses at typical spacing
const ZONE_LINK = () => 0.22 * baseDist() * forceMul.dist;   // half-width of a bridge
const ZONE_ALPHA = 0.13;   // a backdrop; the notes stay the figure

// Prim, O(m²) — the textbook array form, not the naive rescan: with the tree
// scanned per candidate it is O(m³) and cost 7.9ms a frame on the largest zone
// here, against 1.0ms for all 18 zones this way.
//
// While the engine is stopped the positions are frozen, so drawZones serves
// each zone's tree from mstMemo (cleared on every onEngineStop, bypassed
// while simRunning): a pan or zoom over a settled layout stops paying Prim
// per frame. Member count guards a same-id zone whose membership changed.
const mstMemo = {};
function zoneMST(ms) {
  if (ms.length < 2) return [];
  const n = ms.length, used = new Array(n).fill(false),
        best = new Array(n).fill(Infinity), from = new Array(n).fill(0), out = [];
  used[0] = true;
  for (let j = 1; j < n; j++) best[j] = (ms[0].x - ms[j].x) ** 2 + (ms[0].y - ms[j].y) ** 2;
  for (let k = 1; k < n; k++) {
    let b = -1;
    for (let j = 0; j < n; j++) if (!used[j] && (b < 0 || best[j] < best[b])) b = j;
    used[b] = true;
    out.push([ms[from[b]], ms[b]]);
    for (let j = 0; j < n; j++) if (!used[j]) {
      const d = (ms[b].x - ms[j].x) ** 2 + (ms[b].y - ms[j].y) ** 2;
      if (d < best[j]) { best[j] = d; from[j] = b; }
    }
  }
  return out;
}

// Members per zone, positions only — recomputed per frame because the layout
// is still moving for most of the time a zone is on screen.
function zoneMembers() {
  const by = {};
  RAW_NODES.forEach(n => {
    if (!(n.sgroup >= 0) || typeof n.x !== "number") return;
    (by[n.sgroup] = by[n.sgroup] || []).push(n);
  });
  return by;
}

// 2D only: onRenderFramePre draws under the nodes.
//
// ONE fill per zone, never a fill plus a stroke: the two widths would have to be
// two passes, and two passes double-composite where they meet — a darker lozenge
// ringing every member, an accidental density map. So the isthmus goes into the
// SAME path as the bulbs, as a quad rather than a stroked segment, and nonzero
// winding unions the lot. Everything inside one fill() composites exactly once,
// however much of it overlaps, which is what lets dense areas fuse flat.
//
// The quad is wound consistently (left normal, so its handedness does not follow
// the edge's direction) and the arcs are drawn anticlockwise to match: mixed
// winding would subtract the overlaps and punch holes where a bridge meets a bulb.
function drawZones(ctx) {
  if (!showZones || !ZONES.length) return;
  const by = zoneMembers();
  const R = ZONE_BULB(), w = ZONE_LINK();
  ZONES.forEach(z => {
    const members = by[z.id];
    if (!members) return;
    ctx.save();
    ctx.globalAlpha = ZONE_ALPHA;
    ctx.fillStyle = zoneColor(z);
    ctx.beginPath();
    members.forEach(n => {
      ctx.moveTo(n.x + R, n.y);
      ctx.arc(n.x, n.y, R, 0, 2 * Math.PI, true);
    });
    let mst = !simRunning && mstMemo[z.id];
    if (!mst || mst.n !== members.length) {
      mst = { n: members.length, seg: zoneMST(members) };
      if (!simRunning) mstMemo[z.id] = mst;
    }
    mst.seg.forEach(([a, b]) => {
      const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len * w, ny = dx / len * w;
      ctx.moveTo(a.x + nx, a.y + ny);
      ctx.lineTo(b.x + nx, b.y + ny);
      ctx.lineTo(b.x - nx, b.y - ny);
      ctx.lineTo(a.x - nx, a.y - ny);
      ctx.closePath();
    });
    ctx.fill();
    ctx.restore();
  });
}

const zoneEls = {};
function buildZoneLabels() {
  const layer = document.getElementById("zone-labels");
  if (!layer || !ZONES.length) return;
  layer.innerHTML = ZONES.map(z =>
    '<div class="zone-label" data-id="' + z.id + '" style="color:' + zoneColor(z) + '">' +
    escHtml(z.label) + '</div>').join("");
  layer.querySelectorAll(".zone-label").forEach(el => { zoneEls[el.dataset.id] = el; });
}

// In 3D a centroid behind the camera still projects to a point on screen — a
// mirrored one. A zone name planted over the wrong cluster is worse than no
// name, so ask which side of the camera it is on first. Plain arithmetic on
// camera.position and the controls' target: the bundle exposes no THREE.
function inFrontOfCamera(c) {
  if (is2D()) return true;
  const cam = Graph.camera && Graph.camera();
  const ctr = Graph.controls && Graph.controls();
  if (!cam || !cam.position || !ctr || !ctr.target) return true;
  const p = cam.position, t = ctr.target;
  return (c.x - p.x) * (t.x - p.x) + (c.y - p.y) * (t.y - p.y) + (c.z - p.z) * (t.z - p.z) > 0;
}

function positionZoneLabels() {
  const by = zoneMembers();
  ZONES.forEach(z => {
    const el = zoneEls[z.id];
    if (!el) return;
    const members = by[z.id];
    if (!showZones || !members) { el.style.display = "none"; return; }
    let x = 0, y = 0, zc = 0;
    members.forEach(n => { x += n.x; y += n.y; zc += n.z || 0; });
    x /= members.length; y /= members.length; zc /= members.length;
    // Snap to the member nearest the centroid. A scattered zone's centroid sits
    // in the hole between its pieces — now that the region no longer fills that
    // hole, a name parked there labels empty space, or worse, someone else's.
    // O(n) per zone per frame, n <= 92 here.
    let c = null, best = Infinity;
    members.forEach(n => {
      const d = (n.x - x) ** 2 + (n.y - y) ** 2 + ((n.z || 0) - zc) ** 2;
      if (d < best) { best = d; c = { x: n.x, y: n.y, z: n.z || 0 }; }
    });
    if (!c || !inFrontOfCamera(c)) { el.style.display = "none"; return; }
    const s = Graph.graph2ScreenCoords(c.x, c.y, c.z);
    el.style.display = "block";
    el.style.transform =
      "translate(" + s.x + "px," + s.y + "px) translate(-50%,-50%)";
  });
}

// --- Note names in 3D -------------------------------------------------------
// 2D paints its labels on the canvas and gates them on the ZOOM: dots below
// 0.6, the standouts up to 1.5, everything above it. 3D has no zoom scalar, so
// the same idea rides the only distance it does have — camera to node. Move in,
// names appear; the reading is identical, the quantity is not a guess.
//
// DOM, like the zone names above, and for the same two reasons: the bundle
// hands out no THREE to build a sprite with, and one positioning routine is
// enough. The cost is bounded by a fixed pool of divs rather than by the vault:
// 553 absolutely-positioned elements written every frame is a real bill, and
// past the first few dozen the names overlap into a smear anyway.
const LABEL_POOL = 60;
const labelEls = [];
function buildNodeLabels() {
  const layer = document.getElementById("node-labels");
  if (!layer || labelEls.length) return;
  for (let i = 0; i < LABEL_POOL; i++) {
    const el = document.createElement("div");
    el.className = "node-label";
    layer.appendChild(el);
    labelEls.push(el);
  }
}

// The thresholds are in graph units and have to scale with the layout, which is
// thousands of units across on a big vault and hundreds on a small one. Mean
// distance from the centroid is that scale, recomputed whenever the layout
// settles rather than per frame.
let GRAPH_R = 0;
function measureGraphRadius() {
  let x = 0, y = 0, z = 0, n = 0;
  RAW_NODES.forEach(p => { if (p.x != null) { x += p.x; y += p.y; z += p.z || 0; n++; } });
  if (!n) return;
  x /= n; y /= n; z /= n;
  let sum = 0;
  RAW_NODES.forEach(p => {
    if (p.x == null) return;
    sum += Math.hypot(p.x - x, p.y - y, (p.z || 0) - z);
  });
  GRAPH_R = sum / n;
}

function positionNodeLabels() {
  if (!labelEls.length) return;
  const cam = !is2D() && Graph.camera && Graph.camera();
  // 2D draws its own labels on the canvas; showing these there would double
  // every name. Same for notes-off, where there is nothing to name.
  if (!cam || !cam.position || !showNotes || !GRAPH_R) {
    labelEls.forEach(el => { el.style.display = "none"; });
    return;
  }
  const far = 3.0 * GRAPH_R;    // beyond this, no name at all
  const near = 1.2 * GRAPH_R;   // inside this, every note gets one
  const p = cam.position;
  const cand = [];
  for (const n of RAW_NODES) {
    if (n._hidden || n.x == null) continue;
    // An arrangement names its own notes, lit or not (the 2D ring does the
    // same); the vault around it stays unnamed, as the focus left it.
    if (ARR ? !(n.id in ARR.layout) : n._dim) continue;
    const d = Math.hypot(n.x - p.x, n.y - p.y, (n.z || 0) - p.z);
    // An arrangement (ADR-0088) is looked at from as far as it takes to hold
    // it, and every note on its plane is one the reader asked for: no
    // distance rule, only the pool.
    if (!ARR && d > far) continue;
    // Between near and far only the standouts, exactly the rule 2D applies
    // between zoom 0.6 and 1.5. Ghosts are names nothing carries; they are
    // already the most numerous thing in the frame and stay unnamed.
    if (!ARR && d > near && ((n.size || 16) <= BASE_SIZE || n.type === "ghost")) continue;
    if (!inFrontOfCamera(n)) continue;
    cand.push([d, n]);
  }
  // Nearest first, then the pool cuts the tail: when more notes qualify than
  // there are slots, the ones you flew towards are the ones that get named.
  // An arrangement ranks its names as the 2D one does (the root, then by how
  // linked each note is), since every note on its plane is equally near.
  const deg = id => (outDeg[id] || 0) + (inDeg[id] || 0);
  cand.sort(ARR
    ? (a, b) => (b[1].id === ARR.root) - (a[1].id === ARR.root) || deg(b[1].id) - deg(a[1].id)
    : (a, b) => a[0] - b[0]);
  const shown = Math.min(cand.length, LABEL_POOL);
  const boxes = [];
  for (let i = 0; i < shown; i++) {
    const d = cand[i][0], n = cand[i][1];
    const el = labelEls[i];
    const s = Graph.graph2ScreenCoords(n.x, n.y, n.z || 0);
    if (el._id !== n.id) { el.textContent = n.label; el._id = n.id; el._w = 0; }
    el.style.display = "block";
    // Fade with distance so names arrive instead of popping. Floored, because a
    // label at 5% is a smudge, not a word. Not on an arrangement's one plane.
    el.style.opacity = ARR ? 1 : Math.max(0.35, Math.min(1, 1.15 - d / far));
    el.style.color = (n._dim || n.type === "ghost" || n.type === "missing") ? GP.ghostLabel : GP.label;
    el.style.transform =
      "translate(" + s.x + "px," + s.y + "px) translate(-50%,6px)";
    if (!ARR) continue;
    // Measured once per name, not per frame: the width does not move with the
    // transform, and a read after every write would lay the page out 60 times
    // a second.
    if (!el._w) { el._w = el.offsetWidth; el._h = el.offsetHeight; }
    boxes.push({ x0: s.x - el._w / 2, y0: s.y + 6, x1: s.x + el._w / 2, y1: s.y + 6 + el._h });
  }
  // A name that would collide or run under the key waits, as in 2D.
  if (ARR) keepLabels(boxes, { x0: 0, y0: 0, x1: freeWidth(), y1: Graph.height() })
    .forEach((k, i) => { if (!k) labelEls[i].style.display = "none"; });
  for (let i = shown; i < labelEls.length; i++) labelEls[i].style.display = "none";
}

// Its own rAF, not the render budget's: a camera orbit moves the labels without
// the simulation running, and this loop only writes transforms on a bounded set
// of divs — nothing is rendered. It exists only while a layer that needs it is
// on. One loop drives both layers, so a 3D graph with zones up pays for one.
let zoneRaf = null;
const labelsWanted = () => showZones || !is2D();
function syncZoneLoop() {
  if (labelsWanted() && zoneRaf === null) {
    const step = () => {
      positionZoneLabels();
      positionNodeLabels();
      // All 3D-only and all cheap. styleScene is here rather than wired to
      // each rebuild site because the rebuilds are the bundle's, not ours, and
      // land whenever it decides: a frame is the one moment we know they have.
      // linkSegStep rides the same fact — the link carriers it builds from
      // appear whenever the digest does.
      linkSegStep();
      styleScene();
      fogStep();
      zoneRaf = requestAnimationFrame(step);
    };
    zoneRaf = requestAnimationFrame(step);
  } else if (!labelsWanted() && zoneRaf !== null) {
    cancelAnimationFrame(zoneRaf);
    zoneRaf = null;
    positionZoneLabels();   // one last pass to hide them
    positionNodeLabels();
  }
}

function updateZoneFilter() {
  const cbZones = document.getElementById("cb-zones");
  const cbNodes = document.getElementById("cb-zone-nodes");
  if (cbZones) showZones = cbZones.checked;
  // "notes" means "the zones alone", so it exists only while there are zones
  // to leave alone: with them off it blanked the frame (ADR-0058). Leaving the
  // zones takes the notes back with it.
  if (cbNodes && !showZones) cbNodes.checked = true;
  const row = document.getElementById("zone-nodes-row");
  if (row) row.style.display = showZones ? "" : "none";
  if (cbNodes) showNotes = cbNodes.checked;
  // No refreshPaint: toggling zones no longer touches a single node's colour,
  // and in 3D that call rebuilds the material of every node and every link (see
  // its comment). applyFilters already re-passes the visibility accessors and
  // wakes the renderer, which is all the hull layer needs to appear.
  applyFilters();
  syncZoneLoop();
  updateFocusBar();
}

buildZoneLabels();
buildNodeLabels();

// First build: the mode comes from localStorage (3D on a fresh profile), and
// setMode owns the whole bring-up — instance, forces, filters, focus. A 2D
// first paint has no default camera worth keeping, so it refits; 3D keeps the
// lib's own initial framing.
//
// It runs down here, after both label layers exist, because setMode starts the
// label loop: the loop's own state is declared above in this section, and a
// bring-up from further up the file would read it before it is initialised.
fitPending = is2D();
setMode(mode);
announceScopes();
document.getElementById("st-ghost-row").classList.toggle("off", !showAmbiguous);

// --- the area list is a scope picker ----------------------------------------
// One isolate (ADR-0058): a click on an area makes it the scope. Embedded, the
// scope is the app's (its picker shows it, and compare is measured against
// it), so the frame asks and applies what comes back in "silica-scope", the
// one path both pickers take. Standalone there is no app, so it sets its own.
// The same area again gives the whole vault back.
function scopeArea(cid) {
  const on = SCOPE && SCOPE.kind === "area" && SCOPE.id === cid;
  pickScope(on ? null : { kind: "area", id: cid, label: COMM_HUBS[cid] || "#" + cid });
}

function pickScope(scope) {
  if (window.parent !== window) {
    window.parent.postMessage({ type: "silica-pick-scope", scope }, "*");
    return;
  }
  setScope(scope);
}

function setScope(scope) {
  // Into or out of a collection the context colours change, and 3D reads a
  // node's colour only on an accessor re-pass (refreshPaint).
  const repaint = (SCOPE && SCOPE.kind === "collection") || (scope && scope.kind === "collection");
  disarrange(false);   // the filters and the fit below read the free layout
  SCOPE = scope || null;
  placeRim();   // before the filters and the fit, which both read its positions
  document.querySelectorAll(".legend-item[data-community]").forEach(el =>
    el.classList.toggle("active", !!SCOPE && SCOPE.kind === "area" && +el.dataset.community === SCOPE.id));
  applyFilters();
  if (repaint) refreshPaint();
  updateFocusBar();
  fitScope();
}

// A scope set while this frame is hidden (the Library tab's "Show on graph",
// the Areas surface's way to the Graph: both scope, then switch) fitted a
// canvas of no size, and the zoom clamped to its floor of 0.01, an empty
// picture (seen 2026-10-06). So the fit waits for the frame to have a size.
let scopeFitPending = false;
function fitScope() {
  const el = document.getElementById("graph");
  if (!el.clientWidth || !el.clientHeight || !Graph.width()) { scopeFitPending = true; return; }
  scopeFitPending = false;
  if (SCOPE) { Graph.zoomToFit(400, 50, n => !n._hidden); wake(600); } else fitGraph();
}
// Both bundles take the window's size once, when their script runs, as the
// default of `width` and `height`, and never follow it. A frame loaded while
// the app showed another surface (Library opens after the Graph has started
// loading) got a 0 x 0 canvas for good: a blank Graph and a fit clamped to
// zoom 0.01 (2026-10-06). So the canvas follows the window, which here is the
// iframe, and a hidden frame's zero size is skipped rather than copied.
//
// A live resize (the note drawer opening narrows this iframe) keeps the camera
// where it was, since the lib's resize path re-centres by its own estimate, and
// paints one frame, since it leaves the canvases blank: the same two repairs
// setRes makes for the same call.
function syncCanvasSize(G) {
  const live = !G;
  G = G || Graph;
  if (!G || !innerWidth || !innerHeight) return;
  if (G.width() === innerWidth && G.height() === innerHeight) return;
  const cam = live && is2D() ? { z: G.zoom(), c: G.centerAt() } : null;
  G.width(innerWidth).height(innerHeight);
  if (cam) { G.zoom(cam.z); G.centerAt(cam.c.x, cam.c.y); }
  if (live) { G.resumeAnimation(); G.pauseAnimation(); renderBudget(); }
}
window.addEventListener("resize", () => {
  syncCanvasSize();
  if (scopeFitPending) requestAnimationFrame(fitScope);
  // The click that arranges also opens the note drawer, which narrows this
  // frame a moment later: the arrangement is framed again for the new size.
  if (ARR) requestAnimationFrame(frameArrangement);
});

// --- Communities legend: sort by size, toggling ascending <-> descending ----
let communitySortAsc = true;
function toggleCommunitySort() {
  const box = document.getElementById("legend-box");
  const rest = box.querySelector(".legend-item.rest");   // stays last, whichever way
  const items = Array.from(box.querySelectorAll(".legend-item[data-community]"));
  items.sort((a, b) => (+a.dataset.size - +b.dataset.size) * (communitySortAsc ? 1 : -1));
  items.forEach(el => box.insertBefore(el, rest));
  document.getElementById("sort-communities").textContent = communitySortAsc ? "size ↑" : "size ↓";
  communitySortAsc = !communitySortAsc;
}

// --- Search → ranked results → fly-to-focus -------------------------------
// Search by what people actually remember: title first, then path, then the
// cluster they were browsing. Choosing a result flies the
// camera to the node and selects it — the graph answers "where is it", not
// just "is it somewhere in this cloud".
let results = [], selIdx = -1;

function scoreNode(n, q) {
  // A missing paper is findable only while its layer is drawn: a hit on a
  // hidden node flies the camera to an empty spot.
  if (n.type === 'ghost' || (n.type === 'missing' && !showMissing)) return 0;
  const label = (n.label || '').toLowerCase();
  if (label === q)            return 5;
  if (label.startsWith(q))    return 4;
  if (label.includes(q))      return 3;
  if ((n.path || '').toLowerCase().includes(q)) return 2;
  const cl = (areaOf(n) + " " + (COMM_LABELS[n.group] || "")).toLowerCase();
  if (cl.trim() && cl.includes(q)) return 1;
  return 0;
}

function renderResults(q) {
  const box = document.getElementById("search-results");
  if (!q) { box.className = ""; box.innerHTML = ""; results = []; selIdx = -1; return; }
  results = RAW_NODES
    .map(n => [scoreNode(n, q), n])
    .filter(p => p[0] > 0)
    .sort((a, b) => b[0] - a[0] || a[1].label.localeCompare(b[1].label))
    .slice(0, 12)
    .map(p => p[1]);
  selIdx = results.length ? 0 : -1;

  const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const sub = n => {
    const cl = areaOf(n);
    return cl ? '<em>' + esc(cl) + '</em>' : esc(n.path || n.type);
  };
  box.innerHTML =
    '<div id="search-count">' + (results.length || 'no') +
      ' result' + (results.length === 1 ? '' : 's') + '</div>' +
    results.map((n, i) =>
      '<div class="result-item' + (i === selIdx ? ' sel' : '') +
        '" onclick="chooseResult(' + i + ')">' +
        '<span class="result-name">' + esc(n.label) + '</span>' +
        '<span class="result-sub">' + sub(n) + '</span>' +
      '</div>').join("");
  box.className = "open";
}

// Shared selection path for tree clicks and search results: open the note view
// and fly the camera. Task 3 adds neighbour dimming here.
function chooseNode(node) {
  if (!node) return;
  selectNode(node);
  focusNode(node);
  applyFocus(node.id);
}

function chooseResult(i) {
  const n = results[i];
  if (!n) return;
  selIdx = i;
  chooseNode(n);
}

function moveSel(d) {
  if (!results.length) return;
  selIdx = (selIdx + d + results.length) % results.length;
  document.querySelectorAll("#search-results .result-item")
    .forEach((el, i) => el.classList.toggle("sel", i === selIdx));
}

function onSearch(q) { renderResults(q.trim().toLowerCase()); }

function onSearchKey(e) {
  if (e.key === "Enter")          { e.preventDefault(); chooseResult(selIdx); }
  else if (e.key === "ArrowDown") { e.preventDefault(); moveSel(1); }
  else if (e.key === "ArrowUp")   { e.preventDefault(); moveSel(-1); }
  else if (e.key === "Escape")    { document.getElementById("search").value = ""; renderResults(""); }
}

// Fly to a node: a camera move in 3D, a pan + zoom in 2D. Coords (node.x/y/z)
// exist once the layout has run (cooldownTicks); before that they default to 0
// and the view simply recentres — harmless.
function focusNode(node) {
  wake(1100);   // 900ms tween, either mode — it needs frames to actually fly
  if (is2D()) {
    Graph.centerAt(node.x || 0, node.y || 0, 900);
    Graph.zoom(2.5, 900);
    return;
  }
  const r = Math.hypot(node.x || 0, node.y || 0, node.z || 0) || 1;
  const k = 1 + 90 * 3 / r;
  Graph.cameraPosition(
    { x: (node.x || 0) * k, y: (node.y || 0) * k, z: (node.z || 0) * k },
    node, 900
  );
}

// One click, one message. The second click of a double click used to mean
// "read it" and raise the drawer over the panel the first one filled; since
// ADR-0075 the first click opens the note itself, so a second has nothing
// left to ask for and the frame no longer sends one.
function selectNode(node) {
  // Embedded in the web-UI iframe: hand off to the parent instead of opening
  // this internal metadata drawer (avoids two stacked drawers). The parent
  // opens the note on its text or its details (ADR-0075). Ghost nodes ride the
  // same message: they have no path, and the parent's pane is the only
  // surface that can say anything about an unresolved link.
  if (window.parent !== window) {
    // The head facts ride along because this frame HAD to compute all three to
    // draw the node you just clicked: degree from the edge list, state from
    // nodeState (the same rule the HUD's Node state legend counts), area from
    // the community it is coloured by. Asking the server for them again would
    // be a second answer that can disagree with the picture on screen.
    window.parent.postMessage({
      type:  "silica-open-context",
      path:  node.path || "",
      name:  node.label || "",
      ghost: node.type === "ghost",
      links: isRim(node) ? null : (outDeg[node.id] || 0) + (inDeg[node.id] || 0),
      state: isRim(node) ? "" : nodeState(node),
      area:  areaOf(node),
      // A missing paper has no path, so the panel reads it as a ghost; these
      // two are what it can say instead (ADR-0055 "Missing papers").
      quote: node.quote || "",
      href:  node.href || "",
      // A paper on the rim (ADR-0066 decision 4): its covering notes with the
      // k/K a reader judges a systematic association by.
      outside: isRim(node) ? rimContext(node) : null,
    }, "*");
    return;
  }
  document.getElementById("drawer-title").textContent = node.label;
  // A missing paper has no file to name: its line is the reference as the
  // first citing paper wrote it, and its way out is the id's own page, since
  // adding it to the library stays a Zotero action (ADR-0055).
  document.getElementById("drawer-path").textContent  = node.path || node.quote ||
    (isRim(node) ? "not in this vault" : "(ghost node)");
  const commText = areaOf(node) ? ` · ${areaOf(node)}` : "";
  const betwText = node.betweenness ? ` · betweenness ${node.betweenness}` : "";
  const what = node.paper && node.paper.title ? `${node.type} · ${node.paper.title}` : node.type;
  const meta = document.getElementById("drawer-meta");
  meta.textContent = `${what}${commText}${betwText}`;
  if (node.href) {
    const a = document.createElement("a");
    a.href = node.href; a.target = "_blank"; a.rel = "noopener";
    a.textContent = node.href.replace(/^https:\/\//, "");
    meta.append(" · ", a);
  }
  document.getElementById("drawer-out").textContent = outDeg[node.id] || 0;
  document.getElementById("drawer-in").textContent  = inDeg[node.id]  || 0;

  document.getElementById("drawer").classList.add("open");
}

// (Direct clicks in the view get the same dim-non-neighbours treatment as
// tree/search picks, but skip focusNode's fly — the user is already looking at
// this spot, recentring would just be jarring. Bound in buildGraph, so a mode
// switch rebinds them.)

// The embedding page (chat + note-panel) tells us which note is open
// elsewhere — e.g. a link followed inside the note panel itself — so the
// graph mirrors it. Dim only, no camera move (same reasoning as above).
window.addEventListener("message", e => {
  if (e.data && e.data.type === "silica-focus-path") {
    applyFocus(NODE_BY_ID[e.data.path] ? e.data.path : null);
  }
  // Same, for a SET of notes: the context drawer's concept cloud lights every
  // note carrying the clicked concept at once.
  if (e.data && e.data.type === "silica-focus-paths") {
    applyFocus(e.data.paths || []);
  }
  // The app's compare picker: two parts on show, the links inside each faded.
  if (e.data && e.data.type === "silica-compare") {
    disarrange(false);   // two parts where they sit, not around one note
    const d = e.data.a && e.data.b ? { a: new Set(e.data.a), b: new Set(e.data.b) } : null;
    AB = d;
    RAW_EDGES.forEach(l => {
      l._ab = !!d && l.type === "EXTRACTED" &&
        ((d.a.has(l.from) && d.b.has(l.to)) || (d.b.has(l.from) && d.a.has(l.to)));
    });
    refreshPaint();
    applyFilters();
    if (d) { Graph.zoomToFit(400, 50, n => !n._hidden); wake(600); } else fitGraph();
  }
  // The app's scope picker, or this frame's area list by way of the app: show
  // one part of the vault where it sits.
  if (e.data && e.data.type === "silica-scope") setScope(e.data.scope);
  // The explore toolbar's note search asks us to *locate* a note: fly the
  // camera to it and dim to its neighbourhood, without opening the drawer
  // (selectNode would) — the user is searching the cloud, not inspecting yet.
  if (e.data && e.data.type === "silica-goto-path") {
    const n = NODE_BY_ID[e.data.path];
    if (n) { focusNode(n); applyFocus(n.id); }
  }
  // The note drawer covers this frame's right edge, which is where the HUD is.
  // Its width comes with the message because the drawer is resizable — the
  // focus bar parks against its edge, not against a constant.
  if (e.data && e.data.type === "silica-host-drawer") {
    document.body.classList.toggle("host-drawer-open", !!e.data.open);
    document.body.style.setProperty("--drawer-w", (e.data.width || 0) + "px");
  }
  // The app's dock floats over this frame's foot and grows during a turn; the
  // corner bars park above it (graph-frame.css --foot).
  if (e.data && e.data.type === "silica-host-dock") {
    document.body.style.setProperty("--dock-h", (+e.data.h || 0) + "px");
    document.body.classList.toggle("foot-lift", !!e.data.lift);
  }
  // The app's right column stands over the key's corner (ADR-0072); the key
  // starts 8px under it, or at the corner when the column is hidden.
  if (e.data && e.data.type === "silica-host-side") {
    const h = +e.data.h || 0;
    document.body.style.setProperty("--side-h", (h ? h + 8 : 0) + "px");
  }
});

function rimContext(n) {
  const p = n.paper || {};
  return {
    key: p.zotero_key || "", title: p.title || "", collections: p.collections || [],
    covered: n.covered == null ? null : !!n.covered,
    covers: (n.covers || []).map(([id, collection, k, K]) => {
      const note = NODE_BY_ID[id] || {};
      return { path: note.path || id, name: note.label || id, collection, k, K };
    }),
  };
}

function closeDrawer() {
  document.getElementById("drawer").classList.remove("open");
  // Embedded there is no drawer here to close: this is the deselect, and the
  // app's Node panel is the surface that was showing what got deselected. A
  // panel still describing the node you just dropped is the app disagreeing
  // with the view it sits beside.
  if (window.parent !== window) {
    window.parent.postMessage({ type: "silica-clear-node" }, "*");
  }
}

document.getElementById("file-tree").addEventListener("click", e => {
  const leaf = e.target.closest(".tree-note");
  if (leaf) chooseNode(NODE_BY_ID[leaf.dataset.id]);
});

// --- Esc: back to the whole vault -------------------------------------------
// Undone in the order it was applied, focus first and then the scope,
// so one press never throws away two decisions at once. The search box owns its
// own Escape (it clears the query), so it is skipped here.
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (e.target && e.target.id === "search") return;
  if (focusIds.length) { closeDrawer(); clearFocus(); }
  else if (SCOPE) pickScope(null);
  // Last rung, because it is the one the banner promises Esc will undo: the
  // zone layer stays on, only the notes come back.
  else if (!showNotes) {
    const cb = document.getElementById("cb-zone-nodes");
    if (cb) { cb.checked = true; updateZoneFilter(); }
  }
});

// The loop sleeps between interactions, so anything the renderer itself
// services on a frame — the hover raycast, the cursor, drag, wheel zoom,
// control inertia — has to wake it first. Capture phase because force-graph
// reads its hover target at click time and only a rendered frame refreshes it:
// on touch there is no pointermove before the tap.
//
// The same events also cancel the pending auto-fit. The layout settles about a
// second and a half after the view opens, and until now the fit fired then
// regardless: if you had already grabbed the graph and moved somewhere, it
// yanked the camera back. Taking hold of the view means you have chosen your
// framing, so the fit stands down. Hovering is not taking hold, so pointermove
// only wakes the loop.
["pointerdown", "pointermove", "wheel", "touchstart"].forEach(t =>
  document.getElementById("graph-wrap").addEventListener(t, e => {
    wake();
    if (e.type !== "pointermove") fitPending = false;
    else if (!e.buttons) pickHover2D(e);   // a held button is a pan or a drag
    // Camera manipulation drops the resolution BEFORE the first heavy paint:
    // capture phase runs ahead of d3-zoom's own handlers, so the very frame
    // this gesture dirties already paints at the motion ratio. A move with a
    // button held is a drag (camera or node), a wheel notch is a zoom; hover
    // and plain clicks never blur. Restore rides the usual no-paints timer.
    if (e.type === "wheel" || (e.type === "pointermove" && e.buttons))
      setRes(DRS_SCALE);
  }, { capture: true, passive: true }));

// The renderer reports a hover only from a frame under the pointer, so a
// pointer that leaves the frame over a note would keep it lit until it came back.
document.getElementById("graph-wrap").addEventListener("pointerleave", () => hoverNote(null));
