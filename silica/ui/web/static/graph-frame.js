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
// What the 2D atlas draws (ADR-0136): "areas" (node.group, its provinces
// node.sub), "zones" (node.sgroup) on a vault with no area of 3 notes or more,
// or "" with neither. One partition per document, never both (ADR-0023).
const ATLAS_BY = DATA.atlas || "";
const SUBS = DATA.subs || [];
// The semantic partition rides only on a document whose atlas draws it.
// Disjoint from node.group in every way that matters: different edges,
// different ids, different colours.
const ZONES = DATA.zones || [];

const outDeg = {}, inDeg = {};
RAW_EDGES.forEach(e => {
  outDeg[e.from] = (outDeg[e.from] || 0) + 1;
  inDeg[e.to]   = (inDeg[e.to]   || 0) + 1;
});

const NODE_BY_ID = {};
ALL_NODES.forEach(n => { NODE_BY_ID[n.id] = n; });

// Every link a note has, whatever is on show: a note Silica runs on is placed
// by these (followNative), since its links pull nothing.
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
// The arrangement (ADR-0088): {kind, root, layout: {id: [x, y, cost]}, wedges,
// rIn, centre} while the notes stand somewhere other than the settled layout,
// else null. kind "map" is a single-note focus; "clusters" and "flow" are the
// tabs of those names (ADR-0152), which add `heads`, and `edges` for Flow.
let ARR = null;
// The tab the app is on: "network", "clusters" or "flow". A focus arranges the
// Map on the network only (arrangeFocus).
let LAYOUT = "network";
// The link clicked (ADR-0152), or null. It outranks hover and focus: the reader
// is looking at one line.
let LINK_SEL = null;
// {t, grow, t0, ring, end, ready, travelling, then} while the notes arrive on
// the circle (ADR-0152), else null. Nothing that reads a settled position runs
// meanwhile, so it waits in `then` or for arrivalDone.
let ARRIVAL = null;

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
  if (LINK_SEL) markOnly([LINK_SEL.from, LINK_SEL.to], LINK_SEL, ALL_NODES, ALL_EDGES);
  // Flow lights its ladder and nothing the ladder's notes happen to link to.
  else if (ARR && ARR.kind === "flow") markOnly(Object.keys(ARR.layout), null, ALL_NODES, ALL_EDGES);
  else markDim(hoverId ? [hoverId] : focusIds, incident, ALL_NODES, ALL_EDGES);
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
  if (SCOPE && SCOPE.kind === "area" && window.parent === window) {
    const n = RAW_NODES.filter(x => x.group === SCOPE.id).length;
    parts.push("area <b>" + escHtml(COMM_HUBS[SCOPE.id] || "#" + SCOPE.id) + "</b> · " + n + " notes");
  }
  if (focusIds.length && LAYOUT !== "flow") {
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
// Colour is the community, size is links and the rings are hub, orphan and cut
// vertex, so shape is the one channel left. The square keeps the AREA of the
// disc it replaces (side r * sqrt(pi)): size has to read as links across both
// shapes, and an inscribed square would read 36% smaller at the
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
const NATIVE = { plan: "Memorize plan", event: "event note", reading: "Memorize reading" };
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

// --- What a line pulls with (ADR-0136) ---------------------------------------
// Only the backbone's wikilinks lay the vault out: graph_export.backbone_links
// stamps each pair it keeps with the Jaccard of its two ends' neighbourhoods,
// and the pull is min(1, BACKBONE_K x J), floored for a spanning-tree tie whose
// ends share no neighbour. Every other wikilink is drawn and pulls nothing.
// Measured on the machine learning course (1,092 notes, 2026-10-28) by a
// replica of this frame's layout, purity being the share of each note's 10
// nearest that sit in its own area: 3D 0.639 with every line pulling, 0.758 on
// the backbone; the d3 layout in 2D 0.462 against 0.515, which is why 2D lays
// out with ForceAtlas2 (see "the layout in a worker"). J itself was too weak
// against the charge (2D 0.361), x6 unclamped collapsed to 0.507 in 3D, and
// clamped at 1 every factor from 4 to 10 held 0.745-0.758.
// An unresolved link places its ghost, at d3's default `dflt` (1 / the line
// count of its less busy end). A k-NN or citation line pulls only where one
// end has no resolved wikilink (`linked`), so a link-orphan still lands beside
// its semantic neighbours. The absent lines (GAP, PROPOSED, COUPLED) pull
// nothing, and nor does a Silica note's line (ADR-0070): followNative places it.
// `same` multiplies a backbone line whose two notes share an area, after the
// clamp: the areas are cut from every link and the layout reads only the
// backbone, so without it an area's notes land wherever their strongest ties
// pull them and the atlas draws each area as islands (AREA_PULL).
const BACKBONE_K = 4, BACKBONE_FLOOR = 0.05;
function pullOf(l, linked, dflt, same = 1) {
  const a = l.source, b = l.target;
  if (NATIVE[a.type] || NATIVE[b.type]) return 0;
  if (l.type === "EXTRACTED")
    return l.backbone == null ? 0 : Math.min(1, BACKBONE_K * Math.max(l.backbone, BACKBONE_FLOOR))
      * (a.group >= 0 && a.group === b.group ? same : 1);
  if (l.type === "AMBIGUOUS") return dflt;
  if (l.type === "SIMILAR" || l.type === "CITES") return linked.has(a.id) && linked.has(b.id) ? 0 : dflt;
  return 0;
}

// A note Silica runs on is placed, not laid out: each tick it moves FOLLOW of
// the way to the mean of the notes it links (_follow), and never moves them.
// With its lines pulling, d3 moved a plan's notes and not the plan (it has the
// most lines): under ADR-0074's held areas one plan carried an area 12.6 disc
// radii off its disc (test vault, 2026-10-07). The layout worker runs this by
// its source.
const FOLLOW = 0.1;
function followNative(nodes, alpha) {
  for (const n of nodes) {
    const f = n._follow;
    if (!f || !f.length) continue;
    let x = 0, y = 0, z = 0;
    for (const m of f) { x += m.x; y += m.y; z += m.z || 0; }
    n.vx += (x / f.length - n.x) * FOLLOW * alpha;
    n.vy += (y / f.length - n.y) * FOLLOW * alpha;
    if (n.vz !== undefined) n.vz += (z / f.length - (n.z || 0)) * FOLLOW * alpha;
  }
}

// --- The atlas's geometry (ADR-0136) ----------------------------------------
// Every note owns its Voronoi cell, clipped to a disc of radius R around it.
// A border between two notes is the part of their Voronoi edge inside both
// discs (the lens of the two): any other point of that edge lies outside every
// disc, so this is exactly the border that shows, with no clipping pass.
// P: the points, flat [x0, y0, x1, y1, ...]; country[i], prov[i]: point i's
// parts; del: a d3-delaunay Delaunay over P; cc: its Voronoi's circumcenters.
// Returns {country: [x0, y0, x1, y1, ...], province: {country: [...]}}.
// The hull's own edges count too: there the Voronoi edge is a ray, outward
// from the triangle's circumcenter, and its lens part shows like any other.
// keep(p, q), when given, is false for a pair whose border is left out: below
// the notes' zoom a small island draws none (atlasIslands, ATLAS_MIN_ISLAND).
function atlasBorders(P, country, prov, R, del, cc, keep) {
  const T = del.triangles, H = del.halfedges, out = { country: [], province: {} };
  const next = e => e % 3 === 2 ? e - 2 : e + 1;
  for (let e = 0; e < H.length; e++) {
    const j = H[e];
    if (j >= 0 && j < e) continue;   // each inner edge once, from its lower half
    const p = T[e], q = T[next(e)];
    if (country[p] === country[q] && prov[p] === prov[q]) continue;
    if (keep && !keep(p, q)) continue;
    const mx = (P[2 * p] + P[2 * q]) / 2, my = (P[2 * p + 1] + P[2 * q + 1]) / 2;
    const dx = P[2 * q] - P[2 * p], dy = P[2 * q + 1] - P[2 * p + 1], d = Math.hypot(dx, dy);
    if (d === 0 || d >= 2 * R) continue;   // the two discs do not meet
    const ux = -dy / d, uy = dx / d, h = Math.sqrt(R * R - d * d / 4);
    const along = (x, y) => (x - mx) * ux + (y - my) * uy;
    const t = Math.floor(e / 3), t1 = along(cc[2 * t], cc[2 * t + 1]);
    let lo, hi;
    if (j >= 0) {
      const u = Math.floor(j / 3), t2 = along(cc[2 * u], cc[2 * u + 1]);
      lo = Math.min(t1, t2); hi = Math.max(t1, t2);
    } else {
      // Outward is away from the triangle's third note.
      const w = T[next(next(e))];
      [lo, hi] = along(P[2 * w], P[2 * w + 1]) > 0 ? [-Infinity, t1] : [t1, Infinity];
    }
    lo = Math.max(lo, -h); hi = Math.min(hi, h);
    if (hi <= lo) continue;
    const seg = country[p] !== country[q] ? out.country
      : out.province[country[p]] || (out.province[country[p]] = []);
    seg.push(mx + ux * lo, my + uy * lo, mx + ux * hi, my + uy * hi);
  }
  return out;
}

// A note's ground: its Voronoi cell `poly` ([[x, y], ...], convex, closed or
// not, any winding) inside the disc of radius R around its note (sx, sy), the
// disc a regular `sides`-gon (at 24 the gon is within 0.9% of the circle).
// Returns {pts: [x0, y0, x1, y1, ...], arc: [edge i lies on the disc's rim]},
// convex: the gon clipped by each of the cell's half-planes (Sutherland and
// Hodgman), each edge remembering whether it came from the gon, which is the
// coast where no neighbour's ground meets it.
function cellInDisc(poly, sx, sy, R, sides) {
  let V = [], F = [];
  for (let k = 0; k < sides; k++) {
    const a = 2 * Math.PI * k / sides;
    V.push([sx + R * Math.cos(a), sy + R * Math.sin(a)]);
    F.push(true);
  }
  const n = poly.length - (poly.length > 1 && poly[0][0] === poly[poly.length - 1][0]
    && poly[0][1] === poly[poly.length - 1][1] ? 1 : 0);
  for (let e = 0; e < n && V.length; e++) {
    const A = poly[e], B = poly[(e + 1) % n];
    const ex = B[0] - A[0], ey = B[1] - A[1];
    // Inside is the note's side of the edge, whatever the winding.
    const side = Math.sign(ex * (sy - A[1]) - ey * (sx - A[0])) || 1;
    const d = P => side * (ex * (P[1] - A[1]) - ey * (P[0] - A[0]));
    const W = [], G = [];
    for (let i = 0; i < V.length; i++) {
      const S = V[i], E = V[(i + 1) % V.length], f = F[i], ds = d(S), de = d(E);
      const cut = () => { const t = ds / (ds - de); return [S[0] + (E[0] - S[0]) * t, S[1] + (E[1] - S[1]) * t]; };
      if (de >= 0) {
        if (ds < 0) { W.push(cut()); G.push(f); }
        W.push(E); G.push(F[(i + 1) % V.length]);
      } else if (ds >= 0) { W.push(cut()); G.push(false); }
    }
    V = W; F = G;
  }
  return { pts: V.flat(), arc: F };
}

// An area's islands: its notes whose grounds touch (the two discs meet),
// joined through each other, as the eye joins them. A separate pass from
// atlasBorders, which skips a pair of one province before it measures it.
// Returns {of: the island each point is in (a point index), size: per point,
// the size of its island, shore: per point, 1 where its island's ground meets
// another area's}. P, country, R, del as atlasBorders reads them.
function atlasIslands(P, country, R, del) {
  const n = P.length / 2, up = new Int32Array(n), meet = [];
  for (let i = 0; i < n; i++) up[i] = i;
  const root = i => { while (up[i] !== i) i = up[i] = up[up[i]]; return i; };
  const T = del.triangles, H = del.halfedges, next = e => e % 3 === 2 ? e - 2 : e + 1;
  for (let e = 0; e < H.length; e++) {
    if (H[e] >= 0 && H[e] < e) continue;
    const p = T[e], q = T[next(e)];
    if (Math.hypot(P[2 * q] - P[2 * p], P[2 * q + 1] - P[2 * p + 1]) >= 2 * R) continue;
    if (country[p] !== country[q]) meet.push(p, q);
    else up[root(p)] = root(q);
  }
  const of = new Int32Array(n), count = new Int32Array(n), size = new Int32Array(n);
  const wet = new Uint8Array(n), shore = new Uint8Array(n);
  for (let i = 0; i < n; i++) { of[i] = root(i); count[of[i]]++; }
  for (const p of meet) wet[of[p]] = 1;
  for (let i = 0; i < n; i++) { size[i] = count[of[i]]; shore[i] = wet[of[i]]; }
  return { of, size, shore };
}

// What the atlas names and draws at a zoom, by what fits on the screen and not
// by the zoom's ratio to the fit (ADR-0136, amendment 2026-10-29): the ratios
// 1.8x and 3.6x were set on the 1,092-note course, and on alex_second_brain
// (2,166 notes) the step at 3.6x put 1,167 notes up for a name at once, 24
// names on screen to 150. cellPx: one note's ground across on screen, 2R times
// the zoom. An area with sub-areas opens into them once its own footprint,
// cellPx x sqrt(its notes), is ATLAS_OPEN_PX across, about half the frame at
// 1440, so the big areas open first and a small one stays one name. Every area
// is open and the notes are named once one ground is ATLAS_NOTE_PX across,
// room for a dozen characters at 11px. The two values are the draft's
// (2026-10-29), where they kept 8 to 30 names on screen at every zoom.
// parts: [{c, size, split}], split: the area has sub-areas. Returns
// {notes, open: the set of c open}.
const ATLAS_OPEN_PX = 520, ATLAS_NOTE_PX = 90;
function atlasZoom(cellPx, parts) {
  const notes = cellPx >= ATLAS_NOTE_PX, open = new Set();
  for (const p of parts) if (p.split && (notes || cellPx * Math.sqrt(p.size) >= ATLAS_OPEN_PX)) open.add(p.c);
  return { notes, open };
}

// The notes a part's name may sit on: first the member nearest the part's
// centroid, so a part shaped like a crescent is not named over the hole in
// its middle, then up to k - 1 more spread from the centre outward, where the
// name goes when its spot is taken or out of sight (under the key, at fit).
// ids: point indices; P as atlasBorders reads it.
function atlasSpots(ids, P, k) {
  let x = 0, y = 0;
  for (const i of ids) { x += P[2 * i]; y += P[2 * i + 1]; }
  x /= ids.length; y /= ids.length;
  const d = i => (P[2 * i] - x) ** 2 + (P[2 * i + 1] - y) ** 2;
  const near = ids.slice().sort((a, b) => d(a) - d(b));
  if (near.length <= k || k < 2) return near.slice(0, k);
  const step = (near.length - 1) / (k - 1);
  return Array.from({ length: k }, (_, j) => near[Math.round(j * step)]);
}

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

// The same look for a set that is exactly what stays lit: its notes and the one
// edge `keep`, with no neighbours (a clicked link's two ends, Flow's ladder).
function markOnly(ids, keep, nodes, edges) {
  const on = new Set(ids);
  nodes.forEach(n => { n._dim = !on.has(n.id); });
  edges.forEach(e => { e._dim = e !== keep; });
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

// The link under a point, in graph units: the nearest one on show within
// `reach`. Asked at click time only, so the lib's link picking stays off
// (linkPointerAreaPaint). A written link beats an inferred one lying as close:
// the k-NN lines outnumber the wikilinks, and the line a reader aims at is
// usually the one they wrote. `at`: id -> note.
const LINK_WRITTEN = { EXTRACTED: 1, CITES: 1 };
function nearestLink(edges, p, reach, at) {
  let best = null, bd = Infinity;
  for (const l of edges) {
    if (l._hidden || l._dim) continue;
    const a = at(l.from), b = at(l.to);
    if (!a || !b || a.x == null || b.x == null) continue;
    const dx = b.x - a.x, dy = b.y - a.y, len = dx * dx + dy * dy;
    const u = len ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len)) : 0;
    const d = Math.hypot(p.x - a.x - u * dx, p.y - a.y - u * dy);
    if (d > reach) continue;
    const rank = LINK_WRITTEN[l.type] ? d : d * 1.5;
    if (rank < bd) { bd = rank; best = l; }
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
    n.__wx = NaN;   // put back where it stood, so simFlush tells the layout worker to stop it too
  }
  if (!keep) saved.clear();
}

// boxes in priority order -> keep[i]. Greedy, so an earlier name always wins
// a collision and a later one waits for room (ADR-0069 rule 3). `area`, when
// given, is what the reader can see: a name reaching past it would be cut, so
// it waits too.
function keepLabels(boxes, area) {
  return placeLabels(boxes.map(b => [b]), area).map(k => k === 0);
}
// The same, for a name with places to go: alts[i] is name i's boxes in the
// order it would rather sit, and the answer is the one it took, or -1.
// cls[i] and caps, when given: a budget per kind of name, spent on the names
// placed, so a name that waits for room leaves its turn to the next one.
function placeLabels(alts, area, cls, caps) {
  const kept = [], used = {};
  return alts.map((boxes, i) => {
    const kind = cls && cls[i];
    if (caps && kind in caps && (used[kind] || 0) >= caps[kind]) return -1;
    const k = boxes.findIndex(b => {
      const inside = !area || (b.x0 >= area.x0 && b.x1 <= area.x1 && b.y0 >= area.y0 && b.y1 <= area.y1);
      const clear = inside && kept.every(k => b.x0 >= k.x1 || b.x1 <= k.x0 || b.y0 >= k.y1 || b.y1 <= k.y0);
      if (clear) kept.push(b);
      return clear;
    });
    if (k >= 0 && kind) used[kind] = (used[kind] || 0) + 1;
    return k;
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

// --- what a layout that holds can afford (ADR-0152) -------------------------
// Worked out once, so a last pass can move overlapping discs apart by their
// drawn size, which ForceAtlas2 cannot (adjustSizes is off: it doubled the
// iterations and cost the purity ADR-0136 measured). In place. Pairs through a
// grid sized for nine discs in ten, and the larger of two is the one that looks
// for the other: on alex_second_brain one note holds 852 links, and a grid of
// the largest disc took 900 ms for 120 rounds. 600 rounds is what that vault
// needs to reach no overlap at all (2026-11-06).
const SPREAD_ROUNDS = 600, GOLDEN = 2.399963;
function spreadDiscs(x, y, r, gap) {
  const n = x.length, sorted = Float64Array.from(r).sort();
  const cell = 2 * (sorted[Math.floor(n * 0.9)] || 1) + gap;
  const key = (i, j) => i * 73856093 ^ j * 19349663;
  for (let round = 0; round < SPREAD_ROUNDS; round++) {
    const grid = new Map();
    for (let i = 0; i < n; i++) {
      const k = key(Math.floor(x[i] / cell), Math.floor(y[i] / cell));
      if (grid.has(k)) grid.get(k).push(i); else grid.set(k, [i]);
    }
    let moved = 0;
    for (let i = 0; i < n; i++) {
      const cx = Math.floor(x[i] / cell), cy = Math.floor(y[i] / cell);
      const span = Math.ceil((2 * r[i] + gap) / cell);
      for (let gx = cx - span; gx <= cx + span; gx++) for (let gy = cy - span; gy <= cy + span; gy++) {
        for (const j of grid.get(key(gx, gy)) || []) {
          if (r[j] > r[i] || (r[j] === r[i] && j <= i)) continue;
          let dx = x[j] - x[i], dy = y[j] - y[i], d = Math.hypot(dx, dy);
          const min = r[i] + r[j] + gap;
          if (d >= min) continue;
          // Two notes on one point have no bearing: the golden angle spreads them.
          if (d < 1e-6) { dx = Math.cos(i * GOLDEN); dy = Math.sin(i * GOLDEN); d = 1; }
          const push = (min - d) / 2 / d;
          x[i] -= dx * push; y[i] -= dy * push;
          x[j] += dx * push; y[j] += dy * push;
          moved++;
        }
      }
    }
    if (!moved) break;
  }
}

// The circle the notes arrive on: rows [{id, area, links, r}] in area order,
// biggest area first, most linked first inside one, clockwise from the top, so
// the ring reads as arcs of one colour. Each note gets an arc its own size.
// Returns id -> {x, y, rank}, rank 0 to 1 along the circle of radius R.
function arrivalRing(rows, R) {
  const size = new Map();
  rows.forEach(r => size.set(r.area, (size.get(r.area) || 0) + 1));
  const order = rows.slice().sort((a, b) => size.get(b.area) - size.get(a.area) ||
    (a.area < b.area ? -1 : a.area > b.area ? 1 : 0) || b.links - a.links || (a.id < b.id ? -1 : 1));
  const total = order.reduce((t, r) => t + r.r, 0) || 1, at = new Map();
  let done = 0;
  order.forEach((r, i) => {
    const th = -Math.PI / 2 + 2 * Math.PI * (done + r.r / 2) / total;
    at.set(r.id, { x: R * Math.cos(th), y: R * Math.sin(th), rank: i / Math.max(1, order.length - 1) });
    done += r.r;
  });
  return at;
}

// Clusters (ADR-0152): each group a disc of notes, most linked in the middle,
// the discs on one ring clockwise from the top, biggest first. groups:
// [{key, name, color, ids, r}], ids and r in the order the disc fills. Each
// note takes the area its drawn disc needs, so a big note never sits on a small
// one. The ring starts where its arcs add up, and grows until no two neighbours
// touch: a chord is shorter than its arc, so a big disc beside a small one
// overlaps on the first answer. `unit`: graph units to a pixel at the fit.
// Returns {layout, heads}.
function clusterRing(groups, unit) {
  const gap = 2 * unit, pad = 34 * unit;
  const G = groups.map(g => {
    let acc = 0, top = 0;
    const pos = g.r.map((r, j) => {
      const d = j ? Math.sqrt(acc / 0.7) : 0;
      acc += (r + gap) ** 2;
      top = Math.max(top, r);
      return [d * Math.cos(j * GOLDEN), d * Math.sin(j * GOLDEN)];
    });
    return { ...g, pos, R: Math.sqrt(acc / 0.7) + top };
  }).sort((a, b) => b.ids.length - a.ids.length || (a.name < b.name ? -1 : 1));
  const total = G.reduce((t, g) => t + 2 * g.R + pad, 0);
  let th = -Math.PI / 2;
  for (const g of G) {
    const span = 2 * Math.PI * (2 * g.R + pad) / total;
    g.th = th + span / 2;
    th += span;
  }
  let ring = G.length > 1 ? total / (2 * Math.PI) : 0;
  const clear = () => G.every((g, i) => {
    const h = G[(i + 1) % G.length];
    return g === h || 2 * ring * Math.abs(Math.sin((g.th - h.th) / 2)) >= g.R + h.R + pad;
  });
  while (!clear()) ring *= 1.05;
  const layout = {}, heads = [];
  for (const g of G) {
    const cx = ring * Math.cos(g.th), cy = ring * Math.sin(g.th);
    g.ids.forEach((id, j) => { layout[id] = [cx + g.pos[j][0], cy + g.pos[j][1]]; });
    heads.push({ key: g.key, name: g.name, color: g.color, n: g.ids.length, cx, cy, R: g.R, th: g.th });
  }
  return { layout, heads };
}

// Flow (ADR-0152): a ladder as columns, read first on the left, this note at
// (0, 0), what it unlocks on the right. levels: [{depth, ids}]. A level taller
// than FLOW_ROWS takes as many columns as it needs. `unit`: graph units to a
// pixel at the fit. Returns {layout, heads}, one head per level, at the top of
// its first column.
const FLOW_ROWS = 16;
function flowColumns(levels, unit) {
  const row = 21 * unit, col = 250 * unit, cols = [];
  for (const { depth, ids } of levels.filter(l => l.ids.length).sort((a, b) => a.depth - b.depth))
    for (let c = 0; c * FLOW_ROWS < ids.length; c++)
      cols.push({ depth, ids: ids.slice(c * FLOW_ROWS, (c + 1) * FLOW_ROWS), first: c === 0, n: ids.length });
  const at = cols.findIndex(c => c.depth === 0), layout = {}, heads = [];
  cols.forEach((c, i) => {
    const x = (i - at) * col;
    c.ids.forEach((id, j) => { layout[id] = [x, (j - (c.ids.length - 1) / 2) * row]; });
    if (c.first) heads.push({ depth: c.depth, n: c.n, x, y: -(Math.min(FLOW_ROWS, c.n) - 1) / 2 * row - 26 * unit });
  });
  return { layout, heads };
}

// What is left of an arrangement's side room on a frame too narrow for it: never
// more than half the free width, or the picture has none. At 390px 2 * 165 is
// more than the frame's 302, and the fit came out negative.
function layoutRoom(free, left, right) {
  const s = Math.min(1, free / (2 * (left + right)));
  return [left * s, right * s];
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
  // Clusters and Flow are arrangements already, where a focus only lights; and
  // on the circle there is no place yet to arrange from (arrivalDone asks again).
  if (LAYOUT !== "network" || ARRIVAL) return;
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
  ARR = { kind: "map", root: root.id, layout: fit.layout, wedges: fit.wedges, rIn: fit.rIn, centre };
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
// instead of jumping, for a return the reader asked for. `fit`: frame the
// layout the notes are going back to, which only this moment can measure.
function disarrange(refilter = true, play = false, fit = false) {
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
    if (fit) fitFree(800);
    playFrom(from, settle, fit ? LAYOUT_PLAY : undefined);
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
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const ARR_PLAY_MS = REDUCED ? 0 : 650;
const EASE_MAP = u => u < 0.5 ? 4 * u * u * u : 1 - Math.pow(2 - 2 * u, 3) / 2;
const EASE_OUT = u => 1 - Math.pow(1 - u, 4);
// A whole vault moves further than a ring does, and leaves in turn.
const LAYOUT_PLAY = { ms: REDUCED ? 0 : 900, spread: REDUCED ? 0 : 320, rank: n => (n.__layoutRank || 0), ease: EASE_OUT };
let playRaf = 0;
function positionsNow() {
  const at = new Map();
  for (const n of ALL_NODES) if (n.x != null) at.set(n.id, [n.x, n.y, n.z || 0]);
  return at;
}
function stopPlay() { cancelAnimationFrame(playRaf); playRaf = 0; }
// `spread`: the last note leaves that much after the first; `rank`: 0 to 1, when
// in that span a note leaves.
function playFrom(from, done, { ms = ARR_PLAY_MS, spread = 0, rank = () => 0, ease = EASE_MAP } = {}) {
  stopPlay();
  const moves = [];
  for (const n of ALL_NODES) {
    const a = from.get(n.id);
    if (!a || (a[0] === n.x && a[1] === n.y && a[2] === (n.z || 0))) continue;
    moves.push([n, a, { x: n.x, y: n.y, z: n.z, fx: n.fx, fy: n.fy, fz: n.fz }]);
  }
  // The camera frames over 700-900 ms while the notes slide: no hover until
  // both have stopped (see hoverNote).
  const total = ms + spread;
  hoverQuietUntil = performance.now() + Math.max(total, 900) + 100;
  const t0 = performance.now(), flat = is2D();
  const step = now => {
    const t = Math.max(0, now - t0), over = t >= total;
    for (const [n, a, b] of moves) {
      if (over) { Object.assign(n, b); continue; }
      const e = ease(ms ? Math.min(1, Math.max(0, t - spread * rank(n)) / ms) : 1);
      n.x = n.fx = a[0] + (b.x - a[0]) * e;
      n.y = n.fy = a[1] + (b.y - a[1]) * e;
      if (!flat) n.z = n.fz = a[2] + ((b.z || 0) - a[2]) * e;
    }
    pushMeshes();
    // force-graph pauses its redraw when nothing it knows of moved, and a note
    // sliding under a still camera is not something it knows of.
    if (flat && Graph) Graph.nodeColor(nodeColor);
    if (!over) { playRaf = requestAnimationFrame(step); return; }
    playRaf = 0;
    if (done) done();
  };
  // The first step now, not on the next frame: the caller has already put
  // every note at its end, and a paint before the step would show it there.
  step(t0);
  if (Graph) wake(total + 200);
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
  if (!ARR || ARR.kind !== "map") return;
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

function fitGraph() {
  if (ARR && ARR.kind !== "map") { frameLayout(); return; }
  fitFree(400);
}

// The lib clamps every zoom at 0.01, a number of graph units and not a share of
// the picture: alex_second_brain fits at 0.008 on a 1440 frame, and at 390 the fit
// asked 0.0085 and got 0.01. Half the fit leaves a wheel room to go on out.
function lowerZoomFloor(k) {
  Graph.minZoom(Math.min(0.01, k / 2));
}

// The lib's zoomToFit frames the canvas, and the key stands over its right edge
// (freeWidth): with a note counted in pixels (KREF) the outer ones went under it.
// The foot stands over the bottom edge the same way, 44px up against a pad of 40,
// and took Petrarch and Divine Comedy on silica_wiki at 1440.
const FOOT_GAP = 12;
function fitFree(ms, pad = 40, only = n => !n._hidden) {
  wake(ms + 200);
  if (!is2D()) { Graph.zoomToFit(ms, pad, only); return; }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of ALL_NODES) {
    if (n.x == null || !only(n)) continue;
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x);
    y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y);
  }
  if (x0 > x1) return;
  const W = Graph.width(), H = Graph.height(), free = freeWidth();
  const foot = Math.max(pad, H - document.getElementById("gstat").getBoundingClientRect().top + FOOT_GAP);
  const k = Math.min((free - 2 * pad) / Math.max(1, x1 - x0), (H - pad - foot) / Math.max(1, y1 - y0));
  if (!(k > 0)) return;
  lowerZoomFloor(k);
  Graph.centerAt((x0 + x1) / 2 + (W - free) / 2 / k, (y0 + y1) / 2 + (foot - pad) / 2 / k, ms);
  Graph.zoom(k, ms);
}

// --- the arrival (ADR-0152) -------------------------------------------------
// A layout that had to be computed is computed out of sight. The notes land
// whole around a circle in area order (DESIGN.md: a lighter print means a lower
// tier, so nothing fades), their links draw out, and the circle holds until the
// worker is done; each note then travels to its place, leaving in the circle's
// order. On a cache hit none of this runs. Reduced motion: the circle stands at
// once and the notes go to their places in one step.
const ARRIVE_RING = REDUCED ? 0 : 1000, ARRIVE_LINKS = REDUCED ? 0 : 650, ARRIVE_HOLD = REDUCED ? 0 : 200;
const ARRIVE_TRAVEL = REDUCED ? 0 : 950, ARRIVE_SPREAD = REDUCED ? 0 : 450;

function arrivalStart() {
  const A = ARRIVAL, W = Graph.width(), free = freeWidth();
  const at = arrivalRing(RAW_NODES.filter(n => !n._hidden).map(n =>
    ({ id: n.id, area: n.group == null ? -1 : n.group, links: n.links || 0, r: nodeRadius(n) })),
    // A frame loaded behind another surface has no size yet.
    0.42 * (Math.min(free, Graph.height()) || 800) / KREF);
  for (const n of RAW_NODES) {
    const p = at.get(n.id);
    n.__rank = p ? p.rank : 1;
    if (!p) continue;
    // __wx with x, or simFlush would hand the circle to the worker as a move.
    n.x = n.__wx = p.x; n.y = n.__wy = p.y;
  }
  A.ring = positionsNow();
  A.cx = (W - free) / 2 / KREF;
  Graph.centerAt(A.cx, 0);
  Graph.zoom(KREF);
  A.t0 = performance.now();
  requestAnimationFrame(arrivalStep);
}

function arrivalStep(now) {
  const A = ARRIVAL;
  if (!A || A.travelling) return;
  A.t = now - A.t0;
  const g = ARRIVE_LINKS ? Math.min(1, Math.max(0, (A.t - ARRIVE_RING) / ARRIVE_LINKS)) : 1;
  A.grow = EASE_OUT(g);
  Graph.nodeColor(nodeColor);   // the redraw (see playFrom)
  wake(300);
  if (A.t < ARRIVE_RING + ARRIVE_LINKS + ARRIVE_HOLD) { requestAnimationFrame(arrivalStep); return; }
  A.ready = true;
  if (A.end) arrivalTravel();
}

// The worker's last post. Its layout, spread and saved, is where the notes are
// going; they stay on the circle until the circle has finished drawing.
function landArrival(d) {
  const A = ARRIVAL, was = KREF;
  warmupRun = 0; simTicks = d.total;
  RAW_NODES.forEach((n, i) => { n.x = d.p[3 * i]; n.y = d.p[3 * i + 1]; });
  spreadLayout();
  saveLayout();
  dropSim();
  A.end = positionsNow();
  // The layout's fit is known now, and a disc is counted from it. The circle
  // goes over to the layout's units: the same picture, on the same pixels.
  const s = was / KREF;
  for (const p of A.ring.values()) { p[0] *= s; p[1] *= s; }
  for (const n of RAW_NODES) { const p = A.ring.get(n.id); if (p) { n.x = p[0]; n.y = p[1]; } }
  Graph.centerAt(A.cx * s, 0);
  Graph.zoom(KREF);
  if (A.ready) arrivalTravel();
}

function arrivalTravel() {
  const A = ARRIVAL;
  A.travelling = true; A.t = Infinity; A.grow = 1;
  // The notes at their ends for what measures them; playFrom takes them back.
  for (const n of RAW_NODES) { const p = A.end.get(n.id); if (p) { n.x = p[0]; n.y = p[1]; } }
  measureGraphRadius();
  if (RIM) placeRim();
  const ms = ARRIVE_TRAVEL + ARRIVE_SPREAD;
  fitFree(ms);
  fitPending = false;
  playFrom(A.ring, arrivalDone, { ms: ARRIVE_TRAVEL, spread: ARRIVE_SPREAD, rank: n => n.__rank, ease: EASE_OUT });
}

function arrivalDone() {
  const then = ARRIVAL.then;
  ARRIVAL = null;
  atlasMemo = null;
  paintStat("settled in " + gnum(warmupRun + simTicks) + " ticks");
  applyFilters();   // the atlas comes on, and the links go back under it
  if (then) then();
  if (focusIds.length) arrangeFocus();
}

// The layout as it settled gives the fit a disc is counted from (KREF), at the
// frame's size on a 1440px window so a layout does not depend on the window it
// was computed in, and the overlap pass moves the notes apart at that size (see
// spreadDiscs). A ghost is the far end of a link and hidden until asked for: it
// pushes nothing.
function spreadLayout() {
  const notes = RAW_NODES.filter(n => n.type !== "ghost");
  const x = notes.map(n => n.x || 0), y = notes.map(n => n.y || 0);
  const span = v => Math.max(1, Math.max(...v) - Math.min(...v));
  KREF = Math.min(880 / span(x), 820 / span(y));
  spreadDiscs(x, y, notes.map(nodeRadius), 1.4 / KREF);
  notes.forEach((n, i) => { n.x = x[i]; n.y = y[i]; });
}

// --- Clusters and Flow (ADR-0152) -------------------------------------------
// The tabs of those names: two more kinds of the ADR-0088 arrangement, whole-
// vault where the Map is one note's. 2D only, since what they draw around the
// notes is canvas; a tab picked in 3D goes to 2D without changing the choice
// the renderer remembers, and comes back.
let back3D = false;
function setLayout(kind, root) {
  if (ARRIVAL) { ARRIVAL.then = () => setLayout(kind, root); return; }
  if (kind === LAYOUT && kind !== "flow") return;
  if (kind !== "network" && !is2D()) {
    back3D = true;
    setMode("2d", false);
    if (ARRIVAL) { ARRIVAL.then = () => setLayout(kind, root); return; }
    fitPending = false;   // the arrangement frames itself
  }
  LAYOUT = kind;
  document.body.classList.toggle("arranged", kind !== "network");
  arrSeq++;   // a Map still on its way belongs to the tab just left
  flowSeq++;
  sayLayout("");
  selectLink(null);
  if (kind === "clusters") applyClusters();
  else if (kind === "flow") setFlow(root || (focusIds.length === 1 ? focusIds[0] : ""));
  else {
    disarrange(true, true, true);
    paintDim();
    refreshPaint();
    if (back3D) { back3D = false; setMode("3d", false); }
    else if (focusIds.length) arrangeFocus();
  }
}

// One line over the picture, for what an arrangement cannot draw.
function sayLayout(text) {
  const el = document.getElementById("layout-note");
  el.textContent = text;
  el.hidden = !text;
}

// Puts the notes where `arr` says and plays them there from `from`.
function placeLayout(arr, from) {
  restoreArranged(ALL_NODES, ARR_SAVED, true);
  ARR = arr;
  placeArranged(ALL_NODES, arr.layout, arr.centre, ARR_SAVED, false);
  const moving = RAW_NODES.filter(n => n.id in arr.layout);
  moving.forEach((n, i) => { n.__layoutRank = i / Math.max(1, moving.length - 1); });
  paintDim();
  applyFilters();
  refreshPaint();
  frameLayout();
  playFrom(from, null, LAYOUT_PLAY);
}

// Clusters and Flow leave room round their picture for their heads: a head
// stands outside its disc, a note's name to the right of its column.
const LAYOUT_ROOM = { clusters: [165, 70], flow: [40, 60] }, FLOW_NAME = 190;
function frameLayout() {
  if (!ARR || ARR.kind === "map") return;   // asked for on a frame: the tab may have changed
  const { centre: c, layout, heads, kind } = ARR;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const take = (x, y, r = 0) => {
    x0 = Math.min(x0, x - r); x1 = Math.max(x1, x + r);
    y0 = Math.min(y0, y - r); y1 = Math.max(y1, y + r);
  };
  for (const id in layout) take(c.x + layout[id][0], c.y + layout[id][1]);
  for (const h of heads) kind === "clusters" ? take(c.x + h.cx, c.y + h.cy, h.R) : take(c.x + h.x, c.y + h.y);
  const W = Graph.width(), H = Graph.height(), free = freeWidth(), [side, py] = LAYOUT_ROOM[kind];
  const [px, right] = layoutRoom(free, side, kind === "flow" ? FLOW_NAME : side);
  const k = Math.min((free - px - right) / Math.max(1, x1 - x0),
    Math.max(1, H - 2 * py) / Math.max(1, y1 - y0), kind === "flow" ? 4 * KREF : Infinity);
  ARR.k = k;
  lowerZoomFloor(k);
  // the picture's middle at the middle of what is free of the key and the names
  const dx = ((W - free) / 2 + (right - px) / 2) / k;
  Graph.centerAt((x0 + x1) / 2 + dx, (y0 + y1) / 2, 800);
  Graph.zoom(k, 800);
  wake(1000);
}

function applyClusters(from = positionsNow()) {
  const count = new Map(), by = new Map();
  const shown = RAW_NODES.filter(n => !n._hidden && n.x != null);
  for (const n of shown) count.set(n.group, (count.get(n.group) || 0) + 1);
  for (const n of shown) {
    // A note with no link is an area of one, and 39 of them on Cultura would
    // be 39 specks on the ring: they gather as one group.
    const key = n.group >= 0 && count.get(n.group) > 1 ? n.group : "none";
    if (!by.has(key)) by.set(key, []);
    by.get(key).push(n);
  }
  const groups = [...by].map(([key, ns]) => {
    ns.sort((a, b) => (b.links || 0) - (a.links || 0) || (a.id < b.id ? -1 : 1));
    return { key, name: key === "none" ? "No area" : COMM_HUBS[key] || COMM_LABELS[key] || "#" + key,
             color: key === "none" ? GP.line : baseColor(ns[0]),
             ids: ns.map(n => n.id), r: ns.map(nodeRadius) };
  });
  if (!groups.length) return;
  const { layout, heads } = clusterRing(groups, 1 / KREF);
  placeLayout({ kind: "clusters", root: null, layout, heads, wedges: [], rIn: 0, centre: { x: 0, y: 0, z: 0 } }, from);
}

// Each area's ground, the atlas's own mix (ATLAS_GROUND): an opaque ink under
// its notes, so a dim never shows through it.
function drawGround(ctx) {
  ctx.save();
  for (const h of ARR.heads) {
    ctx.beginPath();
    ctx.arc(h.cx, h.cy, h.R, 0, 2 * Math.PI);
    ctx.fillStyle = towardFloor(h.color, ATLAS_GROUND);
    ctx.fill();
  }
  ctx.restore();
}

// /path's answer, or null where there is no server to ask: the Flow tab is the
// app's, but a request can still fail, and the tab then says so.
const readPath = note => fetch("/path?note=" + encodeURIComponent(note || ""))
  .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
  .catch(err => { console.warn("reading order unavailable:", err.message); return null; });
const hasLadder = d => !!d && !!d.levels && d.levels.filter(l => l.notes.length).length > 1;

let flowSeq = 0;
async function setFlow(root) {
  const seq = ++flowSeq, flowing = ARR && ARR.kind === "flow";
  if (flowing && root === ARR.root) return;
  sayLayout("Reading the order");   // the first answer on a big vault takes seconds
  const asked = root ? await readPath(root) : null;
  if (seq !== flowSeq) return;
  let d = asked;
  if (root && !hasLadder(asked) && flowing) {   // the ladder on screen stays
    sayLayout("No reading order around " + linkName(root));
    return;
  }
  if (!hasLadder(d)) {
    // No note named, or one with no ladder on the way in: the note that shows
    // an order best. The tab is never an empty promise.
    const land = await readPath("");
    if (seq !== flowSeq) return;
    const pick = land && land.picks && land.picks[0];
    d = pick ? await readPath(pick.path) : null;
    if (seq !== flowSeq) return;
    if (!hasLadder(d)) {
      if (ARR) disarrange(true, true, true);
      sayLayout((land && land.hint) || "No note has a prerequisite yet.");
      return;
    }
  }
  const notes = {};
  const levels = d.levels.map(l => {
    const ids = l.notes.map(n => n.path).filter(id => NODE_BY_ID[id]);
    ids.forEach(id => { notes[id] = 1; });
    return { depth: l.depth, ids: ids.sort(byAreaThenLinks) };
  });
  const id = d.root.path, n = NODE_BY_ID[id];
  if (!n) return;
  const home = ARR_SAVED.get(id) || n;
  const { layout, heads } = flowColumns(levels, 1 / KREF);
  const from = positionsNow();
  placeLayout({ kind: "flow", root: id, layout, heads, wedges: [], rIn: 0,
    edges: (d.edges || []).filter(e => notes[e.from] && notes[e.to]),
    centre: { x: home.x || 0, y: home.y || 0, z: 0 } }, from);
  const foot = [];
  if (asked && !hasLadder(asked)) foot.push("No reading order around " + linkName(root));
  if (d.truncated) foot.push("Cut at 60 notes: the ladder continues past this");
  // A cycle is a disagreement the vault really contains, so it is reported; the
  // edges that close it are dropped from the layout.
  if (d.cycles) foot.push(d.cycles + (d.cycles === 1 ? " note is" : " notes are") + " in a prerequisite cycle: their edges are not drawn");
  sayLayout(foot.join(" · "));
  if (window.parent !== window) window.parent.postMessage({ type: "silica-flow-root", path: id }, "*");
}

function byAreaThenLinks(a, b) {
  const p = NODE_BY_ID[a], q = NODE_BY_ID[b];
  return (p.group ?? -1) - (q.group ?? -1) || (q.links || 0) - (p.links || 0) || (a < b ? -1 : 1);
}

// A prerequisite is not a wikilink, so Flow draws its own wires, left to right.
// No arrowhead: only a citation has one (graph invariant 12), and the columns
// already say which way a ladder reads.
function drawWires(ctx, scale) {
  ctx.save();
  ctx.lineWidth = 1.2 / scale;
  ctx.strokeStyle = GP.ghostLabel;
  ctx.beginPath();
  for (const e of ARR.edges) {
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    if (!a || !b) continue;
    const mx = (a.x + b.x) / 2;
    ctx.moveTo(a.x, a.y);
    ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y);
  }
  ctx.stroke();
  ctx.restore();
}

const flowRung = d => d === 0 ? "This note" : d === -1 ? "Read first" : d === 1 ? "Unlocks"
  : d < 0 ? (-d) + " steps before" : d + " steps after";
// Clusters' and Flow's names. Heads first, biggest first, in every names mode:
// they are what the arrangement says, not a name of a note. Then the notes:
// what a hover or a focus lights, then what the mode asks for. Auto climbs by
// zoom over the fit, as it does on the atlas.
function drawLayoutNames(ctx, scale) {
  const { kind, heads, centre: c, layout } = ARR, items = [], gap = 8 / scale;
  if (kind === "clusters") {
    for (const h of heads.slice().sort((a, b) => b.n - a.n)) {
      const cos = Math.cos(h.th), sin = Math.sin(h.th);
      items.push({ text: clip(h.name, 30) + " · " + gnum(h.n), px: 12.5, x: h.cx + (h.R + gap) * cos, y: h.cy + (h.R + gap) * sin,
        align: cos > 0.25 ? "left" : cos < -0.25 ? "right" : "center", ink: GP.label });
    }
  } else {
    for (const h of heads) items.push({ text: flowRung(h.depth) + (h.depth ? " · " + gnum(h.n) : ""), px: 12.5,
      x: c.x + h.x, y: c.y + h.y, align: "left", ink: GP.label });
  }
  const z = scale / (ARR.k || scale), lit = new Set(hoverId ? [hoverId] : focusIds);
  const wanted = n => kind === "flow" || lit.has(n.id) || namesMode === "all" || (namesMode === "key" && isKey(n)) ||
    (namesMode === "auto" && (z >= 3.6 || (z >= 1.8 && isKey(n))));
  if (namesMode !== "off") {
    const deg = n => (n.links || 0) + (lit.has(n.id) ? 1e6 : 0) + (n.id === ARR.root ? 2e6 : 0);
    Object.keys(layout).map(id => NODE_BY_ID[id]).filter(n => n && !n._dim && !n._hidden && wanted(n))
      .sort((p, q) => deg(q) - deg(p)).forEach(n => items.push({ text: clip(n.label, 34), px: 11.5,
        x: n.x + (kind === "flow" ? arrangedRadius(n, scale) : drawnRadius(n, scale)) + 4 / scale,
        y: n.y, align: "left", ink: GP.label }));
  }
  paintNames(ctx, scale, items);
}

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
  el.textContent = tail;
  const size = document.getElementById("gsize");
  if (size) size.textContent = gnum(RAW_NODES.length) + " nodes · " + gnum(RAW_EDGES.length) + " edges";
}

// Undim AND reframe — the background click means "show me everything again".
function clearFocus() {
  applyFocus(null);
  if (ARR && ARR.kind !== "map") { frameLayout(); return; }
  fitFree(600);
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
  if (!is2D()) repaintLinkSeg(); else repaintGLLinks();
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
  if (is2D()) { Graph.nodeColor(Graph.nodeColor()); repaintGLLinks(); }
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
  accent: tok('--accent'), line: tok('--line'), line2: tok('--line-2'),
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
let showRequires = true;   // a written requires: on, like cites (ADR-0145)
let showMissing = false;
// The part of the vault on show: {kind: "folder", prefix} | {kind: "area", id} |
// {kind: "collection", ids: [names]} | {kind: "papers"} | null. A visibility predicate beside the focus dim, never a second build:
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
  // is a substitution by definition. An atlas over the zones owns the ground
  // and the names; it does not get to repaint the notes.
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
// The counts are not absolute, since the cost of a tick is not. Every warmup
// tick is a frame the browser cannot paint, and a tick is roughly linear in
// links: measured 8.94ms at 12,232 links (link 2.5 + charge 6.4), so the 240
// tuned at ~4.5k links stopped being ~0.8s of freeze and became ~2.1s. Hold the
// freeze near what it was tuned to be, rather than the number of ticks.
const WARMUP_REF_EDGES = 4566;   // the graph the counts above were tuned on
function WARMUP_TICKS() {
  const base = is2D() ? 240 : 150;
  const scale = Math.min(1, WARMUP_REF_EDGES / Math.max(1, RAW_EDGES.length));
  return Math.max(30, Math.round(base * scale));
}

// --- Layout cache: the layout is worked out once (ADR-0152) -----------------
// A hit is the picture: the notes go where they were saved and no engine runs,
// neither the worker nor the lib's. A rerelaxation here would only undo the
// overlap pass, which the forces know nothing about.
//
// One slot per renderer, holding its own fingerprint, the node set and the
// engine.
// -2 since ADR-0136: a layout settled under every line pulling is not one the
// backbone would reach, and a fingerprint hit would seed it as if it were.
// -3 since the area pull (AREA_PULL): a 2D layout settled without it is not
// one ForceAtlas2 reaches with it.
// -4 since ADR-0152: a hit is now shown as it stands, and a layout saved before
// the overlap pass, or under a force slider, would be shown for good.
const LAYOUT_KEY = "silica-graph-layout-4";
const COLD_DECAY = 0.0228;   // d3's own: alpha 1 -> ALPHA_MIN in ~300 ticks
const NODE_FP = (() => {
  let h = RAW_NODES.length;
  for (const n of RAW_NODES)
    for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) | 0;
  return h;
})();
// The engine too: a 2D layout settled by ForceAtlas2 is not the one d3 settles
// in a frame with no worker, and either would seed the other as its own.
const layoutFp = () => NODE_FP + ":" + (is2D() ? ENGINE_2D : "d3");

// True when the positions came from the cache, i.e. nothing has to run.
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
  if (hit !== RAW_NODES.length) return false;
  KREF = saved.kref || DISC_PX;
  return true;
}

function saveLayout() {
  // Never an arrangement (ADR-0088): it is a state of one focus, put back by
  // Esc, and an engine stop lands inside it whenever a tick moves its pins.
  if (ARR) return;
  const pos = {};
  // Rounded to whole graph units: the layout is thousands of units across, the
  // decimals are noise, and they triple the size of what goes into storage.
  RAW_NODES.forEach(n => {
    pos[n.id] = [Math.round(n.x || 0), Math.round(n.y || 0), Math.round(n.z || 0)];
  });
  try {
    localStorage.setItem(LAYOUT_KEY + "-" + mode, JSON.stringify(
      { fp: layoutFp(), pos: pos, kref: KREF }));
  } catch (e) { /* quota or blocked storage -> next load just re-settles */ }
}

// --- 2D labels: node radius + zoom LOD --------------------------------------
// Base size is whatever the smallest real note got (16 with no links, plus 3.5
// a link: graph_view._size_by_links); anything above it is a node that stands
// out, and those are the ones worth a label before you have zoomed in.
const BASE_SIZE = RAW_NODES.reduce(
  (m, n) => n.type === "ghost" ? m : Math.min(m, n.size || 16), Infinity);
const NODE_REL_SIZE = 1.5;  // r = sqrt(val) * this — 16 => 6px, a 56 hub => 11px
// A layout is thousands of units across and a note a dozen: at the fit of a
// vault this size a disc was under a pixel, and the grey dots the reader took
// for notes were their atlas cells (2026-11-06, Cultura, 0.05x). So a radius is
// counted in pixels at the settled layout's fit: KREF is that fit's zoom, kept
// with the layout, and DISC_PX puts 2 links at 3.4px and 51 at 10px, the sizes
// the draft showed. Past the fit a disc grows with the root of the zoom, so a
// note stays a note and the room round it opens up.
const DISC_PX = 0.477;
let KREF = DISC_PX;
const nodeRadius = n => Math.sqrt(Math.max(0, n.size || 16)) * NODE_REL_SIZE * DISC_PX / KREF;
const drawnRadius = (n, k) => nodeRadius(n) * (k > KREF ? Math.sqrt(KREF / k) : 1);

// --- which names show (ADR-0152) --------------------------------------------
// Auto is the climb with the zoom (atlasZoom), and the default. Off, Key and All
// override it: nothing, the most linked tenth, every name that fits. Key reads
// the size the reader sees, not the hub ring, so a big disc is a named one.
const NAMES_KEY = "silica-graph-names";
let namesMode = "auto";
try {
  const v = localStorage.getItem(NAMES_KEY);
  if (v === "off" || v === "key" || v === "all") namesMode = v;
} catch (e) { /* blocked storage -> the default */ }
const _links = RAW_NODES.filter(n => n.type !== "ghost").map(n => n.links || 0).sort((a, b) => a - b);
const KEY_MIN = Math.max(1, _links.length ? _links[Math.floor(_links.length * 0.9)] : 1);
const isKey = n => (n.links || 0) >= KEY_MIN;
function setNames(m) {
  namesMode = m;
  try { localStorage.setItem(NAMES_KEY, m); } catch (e) {}
  syncNames();
  if (Graph) refreshPaint();
}
function syncNames() {
  document.querySelectorAll("#names-toggle button").forEach(b => {
    b.classList.toggle("active", b.dataset.names === namesMode);
    b.setAttribute("aria-pressed", String(b.dataset.names === namesMode));
  });
}

// Circle + text. Below RING_ZOOM times the fit only dots (at that scale the text
// is a smear and the labels outnumber the pixels), then the standouts, and from
// twice that all of them. Font size divides by the zoom so text keeps a constant
// SCREEN size.
const RING_ZOOM = 1.6;
function drawNode(n, ctx, scale) {
  // On the circle a note is not there until its turn, then there whole.
  if (ARRIVAL && !(ARRIVAL.t >= (n.__rank || 0) * ARRIVE_RING)) return;
  // The vault around an arrangement is drawn as it always is, dimmed. Clusters
  // holds every note at its own size: a floor would pile them up in a disc.
  if (ARR && ARR.kind !== "clusters" && ARR.layout[n.id]) { drawArrangedNode(n, ctx, scale); return; }
  if (isRim(n)) { drawRimPaper(n, ctx, scale); return; }
  // A note a scoped paper's line ends on is what the reader came to the rim
  // for, and at the zoom that fits the rim it is a pixel with no name: it
  // keeps a disc of 7px on screen and its label, as the rim keeps its squares.
  const covering = COVERING && COVERING.has(n.id);
  const r0 = drawnRadius(n, scale);
  const r = covering ? Math.max(r0, 3.5 / scale) : n._ctx ? Math.max(r0, 1 / scale) : r0;
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
  if (n._dim || n._ctx || scale < RING_ZOOM * KREF) return;   // dimmed or context: dot only
  const atlas = atlasOn();
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
  // The atlas names its notes by what fits on screen (atlasZoom), all at once and never
  // overlapping (drawAtlasNames), which also draws Off, Key and All.
  if (atlas || ARR || namesMode !== "auto" || (scale < 2 * RING_ZOOM * KREF && (n.size || 16) <= BASE_SIZE)) return;  // mid zoom: standouts
  drawNodeLabel(n, ctx, scale, r);
}

// --- an arrangement, drawn (ADR-0088, after ADR-0069) -----------------------
// The Map fits a ring of dozens of notes into one screen, where a node at its
// graph size is a pixel: a dot keeps 4px on screen, the root 7px and the
// signal ring (ADR-0069 rule 1). The names are drawn after, all together,
// because holding one back depends on every name before it.
const ARR_DOT_PX = 4, ARR_ROOT_PX = 7;
const arrangedRadius = (n, scale) =>
  Math.max(nodeRadius(n) * KREF, n.id === ARR.root ? ARR_ROOT_PX : ARR_DOT_PX) / scale;
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
  if (namesMode === "off") return;
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
  paintNames(ctx, scale, items);
}

// items in priority order: {text, px, x, y, align, ink}. Names that fit, each
// on a halo of the floor; the rest wait for zoom.
function paintNames(ctx, scale, items) {
  ctx.save();
  ctx.textBaseline = "middle";
  const boxes = items.map(it => {
    ctx.font = (it.px / scale) + "px " + ATLAS_FONT;
    const w = ctx.measureText(it.text).width, h = it.px * 1.2 / scale;
    const x0 = it.align === "left" ? it.x : it.align === "right" ? it.x - w : it.x - w / 2;
    return { x0, y0: it.y - h / 2, x1: x0 + w, y1: it.y + h / 2 };
  });
  const tl = Graph.screen2GraphCoords(0, 0), br = Graph.screen2GraphCoords(freeWidth(), Graph.height());
  const keep = keepLabels(boxes, { x0: tl.x, y0: tl.y, x1: br.x, y1: br.y });
  ctx.lineJoin = "round";
  items.forEach((it, i) => {
    if (!keep[i]) return;
    ctx.font = (it.px / scale) + "px " + ATLAS_FONT;
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

// The arcs and their names, under the papers (2D only, like the atlas).
function drawRim(ctx, scale) {
  if (!RIM || AB) return;
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
  if (l.type !== "CITES") return;   // the photon carriers pass here too, with the layer up
  const a = l.source, b = l.target;
  if (!a || !b || a.x == null || b.x == null) return;
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy);
  if (!len) return;
  const ux = dx / len, uy = dy / len;
  const tip = markEdge(b, drawnRadius(b, Graph.zoom()), ux, uy);
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

// A written requires (ADR-0145, B2): open chevrons every 16 screen px along
// the line, pointing at the prerequisite (the edge's target). In screen pixels
// so they read alike at any zoom, and no head, so cites keeps the frame's only
// arrowhead. 2D only: 3D draws the plain frost line.
function drawRequires(l, ctx, k) {
  const a = l.source, b = l.target;
  if (!a || !b || a.x == null || b.x == null) return;
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy);
  if (!len) return;
  const ux = dx / len, uy = dy / len, px = 1 / (k || 1), arm = 4 * px;
  const end = len - markEdge(b, drawnRadius(b, Graph.zoom()), ux, uy) - 8 * px;
  ctx.beginPath();
  for (let d = markEdge(a, drawnRadius(a, Graph.zoom()), ux, uy) + 10 * px; d <= end; d += 16 * px) {
    const x = a.x + ux * d, y = a.y + uy * d;
    ctx.moveTo(x - ux * arm - uy * arm, y - uy * arm + ux * arm);
    ctx.lineTo(x, y);
    ctx.lineTo(x - ux * arm + uy * arm, y - uy * arm - ux * arm);
  }
  ctx.setLineDash([]);
  ctx.lineWidth = px;
  ctx.strokeStyle = linkPaint(l);
  ctx.stroke();
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
    : COVERING && COVERING.has(n.id) ? Math.max(drawnRadius(n, k), 3.5 / k) + 2 / k : drawnRadius(n, k) + 2 / k;
}

// 2D's hover, picked off the positions with the click's own hit radius rather
// than read from force-graph's picking canvas. The lib re-reads that canvas
// every frame, and a resolution drop (setRes) blanks it until its 800 ms
// redraw: with the pointer resting on a note the hover flipped to nothing and
// back. A relight is a repaint stream, which drops the resolution, so each
// flip set off the next (looping in the owner's tab, 2026-10-11). Asked on a
// pointer move only, so a note sliding under a still pointer relights nothing.
function pickHover2D(e) {
  if (!Graph || !is2D() || ARRIVAL) return;
  if (e.target !== document.querySelector("#graph canvas")) { hoverNote(null); return; }   // over the key, not the graph
  hoverNote(noteAt2D(e));
}
function noteAt2D(e) {
  const c = document.querySelector("#graph canvas").getBoundingClientRect(), k = Graph.zoom();
  const p = Graph.screen2GraphCoords(e.clientX - c.left, e.clientY - c.top);
  return nearestHit(ALL_NODES, p, n => hitRadius(n, k));
}

// The click, off the positions in 2D for the same reason: force-graph calls
// onNodeClick for the note its picking canvas last showed under the pointer,
// else onBackgroundClick. After a relight that canvas is blank, and after a
// played move it still holds the old layout, so a click on a ring note landed
// as an empty click: the Map played back and the hover left the note lit over
// the force layout (owner, 2026-10-11). 3D raycasts and keeps the lib's note.
function clickNote(node, e) {
  if (ARRIVAL) return;
  if (is2D() && e) node = noteAt2D(e);
  if (node) {
    selectLink(null);
    selectNode(node);
    if (LAYOUT === "flow" && arrangeable(node)) setFlow(node.id);
    else applyFocus(node.id);
    return;
  }
  const link = is2D() && e ? linkAt2D(e) : null;
  if (link) { selectLink(link); return; }
  selectLink(null);
  closeDrawer();
  clearFocus();
}

// --- a clicked link (ADR-0152) ----------------------------------------------
// It prints in frost, its two notes stay lit, the rest dims, and a tag in the
// chrome's material stands at its middle and says what it is. The direction is
// written, A → B, because only a citation draws an arrowhead.
const LINK_HIT_PX = 5;
function linkAt2D(e) {
  const c = document.querySelector("#graph canvas").getBoundingClientRect();
  const p = Graph.screen2GraphCoords(e.clientX - c.left, e.clientY - c.top);
  return nearestLink(ALL_EDGES, p, LINK_HIT_PX / Graph.zoom(), id => NODE_BY_ID[id]);
}

function selectLink(l) {
  if (l === LINK_SEL || (!l && !LINK_SEL)) return;
  LINK_SEL = l;
  paintDim();
  if (Graph) refreshPaint();
  const tag = document.getElementById("link-tag");
  tag.hidden = !l;
  if (l) { fillLinkTag(tag, l); placeLinkTag(); }
}

const linkName = (id) => { const n = NODE_BY_ID[id]; return n ? n.label : id; };
function linkKind(l) {
  if (l.requires) return "Written prerequisite, in the requires of " + linkName(l.from);
  return { EXTRACTED: "Wikilink, written in " + linkName(l.from),
    CITES: "Citation: this note cites the paper",
    SIMILAR: "Similar notes: neighbours by what they say, never written",
    PROPOSED: "Proposed link: the two notes share a concept and are not linked",
    GAP: "Structural gap: a bridge between two areas that could exist and does not",
    COUPLED: "Coupled notes: linked to the same notes, not to each other",
    COVERS: "Covered: this note covers the paper" }[l.type] || l.type;
}
function fillLinkTag(tag, l) {
  const a = NODE_BY_ID[l.from], b = NODE_BY_ID[l.to];
  let more = "";
  if (l.type === "PROPOSED" && (l.shared || []).length)
    more = '<div class="ln">Shares ' + escHtml(l.shared.join(", ")) + "</div>";
  if (l.type === "GAP" && a && b)
    more = '<div class="ln">' + escHtml(areaOf(a) || "No area") + " and " + escHtml(areaOf(b) || "No area") + "</div>";
  tag.innerHTML = '<div class="ends">' + escHtml(linkName(l.from)) + "<i>→</i>" + escHtml(linkName(l.to)) +
    '</div><div class="kind"></div>' + more;
  tag.querySelector(".kind").textContent = linkKind(l);
  if (l.type !== "EXTRACTED") return;
  // Asked on click: the export carries no line. Tolerated, as the Map is: a
  // graph exported to a file has no server to ask, and the tag still says what
  // the link is.
  fetch("/link?from=" + encodeURIComponent(l.from) + "&to=" + encodeURIComponent(l.to))
    .then(r => r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)))
    .then(d => { if (LINK_SEL === l) fillLinkLine(tag, l, d); })
    .catch(err => console.warn("link line unavailable:", err.message));
}
function fillLinkLine(tag, l, d) {
  const box = document.createElement("div");
  box.className = "ln";
  if (!d.line) box.textContent = "No line names it: it resolves through another name.";
  else {
    tag.querySelector(".kind").textContent = (l.requires ? "Written prerequisite, in " : "Wikilink, written in ") + linkName(d.path);
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = "line " + d.line;
    const q = document.createElement("q");
    q.textContent = d.text;
    box.append(n, q);
  }
  tag.appendChild(box);
  if (d.line && window.parent !== window) {
    const open = document.createElement("button");
    open.type = "button";
    open.className = "open";
    open.textContent = "Open " + linkName(d.path) + " at line " + d.line;
    open.addEventListener("click", () => {
      selectLink(null);   // the note's own focus takes over the picture
      window.parent.postMessage({ type: "silica-open-line", path: d.path, line: d.line }, "*");
    });
    tag.appendChild(open);
  }
  placeLinkTag();
}
// Every painted frame, so it stays on its link through a pan or a zoom.
function placeLinkTag() {
  if (!LINK_SEL) return;
  const tag = document.getElementById("link-tag");
  const a = NODE_BY_ID[LINK_SEL.from], b = NODE_BY_ID[LINK_SEL.to];
  const s = Graph.graph2ScreenCoords((a.x + b.x) / 2, (a.y + b.y) / 2);
  const w = tag.offsetWidth, h = tag.offsetHeight, free = freeWidth();
  tag.style.left = Math.max(8, Math.min(free - w - 8, s.x - w / 2)) + "px";
  tag.style.top = Math.max(8, Math.min(Graph.height() - h - 8, s.y - h - 12)) + "px";
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
// Since ADR-0146 the 2D view carries this division as a screen (SCREEN_2D
// below); the dash is 3D's and the WebGL2-less canvas's. Weight ranks the layers; it does not SEPARATE them, and seven kinds on the
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
// the atlas's discs do (ATLAS_R): a constant would be wrong the moment the
// distance slider moved, so the period rides baseDist().
//
// A dash still lands on each end. A broken line that stopped short of its
// target would read as a link that does not arrive, which is a different claim
// from the one this channel makes. With k dashes and k-1 gaps filling [0,1] at
// gap ratio r, the period is 1/(k-r) and the ink (1-r)/(k-r), which puts the
// last dash's far end exactly on 1.
// 1:3 gap to ink, which is DASH_2D's 2px against 6px and the legend swatch's
// too: one ratio, so the two views and the key under them draw one pattern.
const DASH_GAP = 0.25;                                     // of the period
const DASH_PERIOD = () => 0.10 * baseDist();
// Two, so even the shortest link still reads as broken; the ceiling is what
// keeps the buffer a fixed allocation. 24 covers a link 2.4x the layout's own
// spacing - past that the dashes lengthen again, which is the old behaviour and
// only for the outliers. Raise it (6 floats per dashed edge per step) if a
// vault ever draws an absent edge that reads solid.
const DASH_MIN = 2, DASH_CAP = 24;
// The canvas painter's pair, in the graph units the period above works in. 2D
// prints a screen instead (SCREEN_2D); this dash is left to the frame with no
// WebGL2, whose canvas has no screen to print.
const DASH_2D = [6, 2];

// --- 2D: every link in a spot ink (ADR-0146 point 5) ------------------------
// Translucent lines darkened where they crossed, and a dash beside an
// antialiased line clashed with the dithered tiers the landing prints. So in
// 2D each kind is one opaque ink and an inferred link is a band printed
// through the landing's screens: [Bayer side, band width in CSS px]. A
// proposal (a shared concept; COVERS, which Silica drew) takes the 4x4 screen,
// a question (an association only) the 8x8 ghost. Every band is 3 px since
// 2026-11-01, when the owner asked for them narrower and all one size (they were
// 3 and 5 to 6 px): 3 is the floor because at .14 to .16 ink an 8x8 strip under
// 3 rows is a dotted hairline, not a screen. The key order is the draw order,
// gap under coupled under proposed, as the approved draft stacked them.
const SCREEN_2D = { GAP: [8, 3], COUPLED: [8, 3], PROPOSED: [4, 3], COVERS: [4, 3] };
// The share of a screen's cells inked. Paper keeps the landing's .55 and .16;
// light dots on graphite spread to the eye, so the draft thinned them to .42
// and .14, by eye and not yet measured (DESIGN.md, Tier key).
const SCREEN_INK = LIGHT ? { 4: 0.55, 8: 0.16 } : { 4: 0.42, 8: 0.14 };
// Every written link is 1 px, so width says one thing: a band is inferred.
// Hover, focus and Compare dim; none of them reach this.
// The clicked link is the one drawn wider, while it is clicked (ADR-0152).
const width2D = l => l === LINK_SEL ? 2 : SCREEN_2D[l.type] ? SCREEN_2D[l.type][1] : 1;
// With the atlas on, every link the atlas used to hide prints as a mat over
// the cells at this share, and what is being read prints over it whole (the
// owner, 2026-11-01: "in atlas si potessero vedere i link, ma meno opachi").
// Chosen by eye on alex_second_brain beside .25, which kept a little more of
// the cells' hue and showed the links less, and seeing them was the ask.
const ATLAS_MAT = 0.35;

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
  const sel = l === LINK_SEL;
  const key = (sel ? "S" : "") + (q ? "d" + q : "") + (l._ctx ? "x" : "") + (is2D() ? "2" : "3") +
    (AB ? (l._ab ? "C" : "c") : "");
  if (l.__paintKey === key) return l.__paint;
  // The share of the ground the 2D ink covers: what it was mixed onto the
  // floor at, so over an atlas cell it can be laid at that share (GL_INK_FS).
  let out, cover = 1;
  // A context wikilink under the rim (ADR-0066 amendment) is drawn as the
  // focus dims one: the structure is there, and it is not what is being read.
  if (sel) out = GP.label;   // frost, at full ink
  else if (q >= FADE_STEPS || l._ctx || (AB && !on)) out = GP.linkDim;
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
    // A written requires prints at full ink in both views: the 2D scale-down
    // is for the mat of plain links, and this one is meant to stand out of it.
    const a = l.requires ? 0.95
      : (on ? 0.95 : (c.opacity == null ? 0.6 : c.opacity)) * (is2D() ? LINK_ALPHA_2D : 1);
    // 2D prints the spot ink (ADR-0146): the colour mixed onto the floor once
    // at that alpha, so it reads as it did and a crossing cannot darken. A
    // band takes its hue whole; its screen is the density.
    const g = parseInt(GP.bg.slice(1), 16), k = SCREEN_2D[l.type] ? 1 : a;
    const ink = sh => is2D() ? ((g >> sh) & 255) + (((n >> sh) & 255) - ((g >> sh) & 255)) * k : (n >> sh) & 255;
    // Partway dimmed: toward the dim's colour and toward its full opacity.
    const t = q / FADE_STEPS, d = parseInt(GP.linkDim.slice(1), 16);
    const ch = sh => Math.round(ink(sh) + (((d >> sh) & 255) - ink(sh)) * t);
    cover = k + (1 - k) * t;
    out = "rgba(" + ch(16) + "," + ch(8) + "," + ch(0) + "," + (is2D() ? 1 : a + (1 - a) * t).toFixed(3) + ")";
  }
  l.__paintKey = key; l.__paint = out; l.__cover = cover;
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

// --- 2D: the solid links in WebGL, under the canvas -------------------------
// The canvas stroke was the whole cost of a 2D frame on a big vault. force-graph
// already strokes one path per (colour, width, dash), but Skia tessellates a
// path 1px or wider, and a wikilink is 1.6px: on the 23,836-edge vault
// (2026-10-26, Intel iGPU, 1600x913 backing) that was ~93ms of raster a frame
// against 6.5ms of script, 9.7fps on a pan. Under 1px Skia takes its hairline
// path instead, which is what the half ratio of the dynamic resolution below
// was buying all along: it made a 1.6px line a 0.8px one.
//
// So every link is one instanced quad, on a WebGL2 canvas stacked under the
// lib's, positions read from a texture of the nodes' x/y, the colours
// rewritten only when the paint changes (the same three callers as
// repaintLinkSeg). All of the line is the layer's, so a band can be drawn
// after every written link and nothing the lib strokes crosses it; the lib
// keeps what it adds to a line, the citation's arrowhead and the photons.
//
// Every fragment is the link's spot ink or nothing (ADR-0146 point 5): no
// coverage ramp and no translucent paint, so two links crossing cannot darken
// whatever order they land in. That is what the MAX-blended buffer for the
// wikilinks used to buy. Over the bare floor that is the whole story. An
// atlas cell is not the floor, and an ink pre-mixed onto the floor all but
// vanished over one (a focused wikilink at luma 65 on a cell of 49, against
// 86 before). So with the atlas on the links are written into a buffer with
// blending off, each at the share of the ground it covers, the last one
// winning where they cross, and the buffer is laid over the cells: the mat at
// ATLAS_MAT, then what is being read at full strength. The lib's pre-paint
// layers (the atlas, the rim, the wedges) sat under its links, so they are
// drawn on a 2D canvas under this one.
let GLLinks = null;   // the layer; false where WebGL2 is unavailable or was lost

const GL_LINK_VS = `#version 300 es
in vec2 corner;            // x: 0 at the source, 1 at the target; y: -1|1 across
in uvec2 ends;             // the two nodes, as indices into ALL_NODES
in vec4 color;             // opaque: the spot ink
in float width;            // CSS px, width2D
in vec2 screen;            // Bayer side (0: solid) and the share of its cells inked
uniform highp sampler2D pos;
uniform int posW;
uniform vec3 m0, m1;       // graph units -> device px: the canvas's own transform
uniform vec2 size;         // device px
uniform float dpr;
uniform float grow;        // 0 to 1: how far a link reaches from its first note (the arrival)
out vec4 vColor; out float vD; out float vHalf; flat out vec2 vScreen;
vec2 at(uint i) { return texelFetch(pos, ivec2(int(i) % posW, int(i) / posW), 0).xy; }
vec2 dev(vec2 p) { return vec2(dot(m0, vec3(p, 1.)), dot(m1, vec3(p, 1.))); }
void main() {
  vec2 a = dev(at(ends.x)), b = mix(a, dev(at(ends.y)), grow), d = b - a;
  float L = length(d);
  vec2 u = L > 0. ? d / L : vec2(1., 0.);
  vHalf = width * dpr * .5;
  vD = corner.y * (vHalf + 1.);   // a px of slack, so a sub-pixel width still finds its centre
  vec2 p = mix(a, b, corner.x) + vec2(-u.y, u.x) * vD;
  vColor = color;
  vScreen = screen;
  gl_Position = vec4(p.x / size.x * 2. - 1., 1. - p.y / size.y * 2., 0., 1.);
}`;
// Coverage is the box filter of the stroke across one pixel: a 1.6px line is
// solid over its middle and ramps over a pixel at each edge, as Skia's AA
// stroke does; a thinner one never reaches full cover, as its hairline does.
// The atlas's coast and borders keep it; the links print (GL_INK_FS).
const GL_LINK_FS = `#version 300 es
precision mediump float;
in vec4 vColor; in float vD; in float vHalf;
out vec4 o;
void main() {
  float a = vColor.a * clamp(vHalf + .5 - abs(vD), 0., 1.);
  o = vec4(vColor.rgb * a, a);
}`;
// A link is ink inside its width, never thinner than a device pixel, and a
// band inks only the cells its screen admits, so the floor casing under it
// shows through the rest. The screen is the landing's 8x8 Bayer (its even
// cells are the 4x4), thresholded on gl_FragCoord in whole device pixels per
// CSS pixel: it never scales with zoom and a dot stays a dot at any ratio.
// highp because a device pixel column past 2048 is not an integer in fp16.
const GL_INK_FS = `#version 300 es
precision highp float;
precision highp int;
in vec4 vColor; in float vD; in float vHalf; flat in vec2 vScreen;
uniform float dpr;
uniform float over;        // 0: print the spot ink (atlas off); 1: lay it at its cover (atlas on)
uniform vec3 ground;       // the floor the spot ink was mixed onto
out vec4 o;
const int B8[64] = int[64](0,32,8,40,2,34,10,42,48,16,56,24,50,18,58,26,12,44,4,36,14,46,6,38,
  60,28,52,20,62,30,54,22,3,35,11,43,1,33,9,41,51,19,59,27,49,17,57,25,15,47,7,39,13,45,5,37,63,31,55,23,61,29,53,21);
void main() {
  if (abs(vD) > max(vHalf, .5)) discard;
  if (vScreen.x > 0.) {
    ivec2 c = ivec2(gl_FragCoord.xy / max(1., floor(dpr + .5)));
    float t = vScreen.x > 4. ? float(B8[(c.y & 7) * 8 + (c.x & 7)]) / 64.
                             : float(B8[(c.y & 3) * 16 + (c.x & 3) * 2]) / 16.;
    if (vScreen.y <= t) discard;
  }
  // The spot ink less the floor it was mixed onto is the ink at its cover,
  // premultiplied: over the floor it composites back to the spot ink, over a
  // cell to the line the cell had before the reshade.
  float c = over > .5 ? vColor.a : 1.;
  o = vec4(vColor.rgb - ground * (1. - c), c);
}`;
// u32 source, u32 target, rgba8 (the spot ink, its cover in alpha), f32 width,
// f32 screen side, f32 screen ink
const GL_STRIDE = 24;
// The buffer laid over the canvas, at a share: the mat's, or whole.
const GL_LAY_VS = `#version 300 es
void main() { gl_Position = vec4(gl_VertexID == 1 ? 3. : -1., gl_VertexID == 2 ? 3. : -1., 0., 1.); }`;
const GL_LAY_FS = `#version 300 es
precision mediump float;
uniform sampler2D t;
uniform float share;
out vec4 o;
void main() { o = texelFetch(t, ivec2(gl_FragCoord.xy), 0) * share; }`;

// The atlas (ADR-0136) on the same layer, under the links: its ground as
// triangles, its coast and borders as quads with the links' coverage
// (GL_LINK_FS), their ends given in graph units rather than as notes.
const GL_FILL_VS = `#version 300 es
in vec2 p;
in vec4 color;
uniform vec3 m0, m1;
uniform vec2 size;
out vec4 vColor;
void main() {
  vec2 d = vec2(dot(m0, vec3(p, 1.)), dot(m1, vec3(p, 1.)));
  vColor = color;
  gl_Position = vec4(d.x / size.x * 2. - 1., 1. - d.y / size.y * 2., 0., 1.);
}`;
const GL_FILL_FS = `#version 300 es
precision mediump float;
in vec4 vColor;
out vec4 o;
void main() { o = vec4(vColor.rgb * vColor.a, vColor.a); }`;
const GL_SEG_VS = `#version 300 es
in vec2 corner;
in vec4 ends;              // x0, y0, x1, y1 in graph units
in vec4 color;
in float width;            // CSS px
uniform vec3 m0, m1;
uniform vec2 size;
uniform float dpr;
out vec4 vColor; out float vD; out float vHalf;
vec2 dev(vec2 p) { return vec2(dot(m0, vec3(p, 1.)), dot(m1, vec3(p, 1.))); }
void main() {
  vec2 a = dev(ends.xy), b = dev(ends.zw), d = b - a;
  float L = length(d);
  vec2 u = L > 0. ? d / L : vec2(1., 0.);
  vHalf = width * dpr * .5;
  vD = corner.y * (vHalf + 1.);
  // Square ends, half a width past each end, so a coast of short segments
  // meets itself without a notch at every joint.
  vec2 p = mix(a - u * vHalf, b + u * vHalf, corner.x) + vec2(-u.y, u.x) * vD;
  vColor = color;
  gl_Position = vec4(p.x / size.x * 2. - 1., 1. - p.y / size.y * 2., 0., 1.);
}`;
const GL_SEG_STRIDE = 24;   // f32 x0, y0, x1, y1, rgba8, f32 width

function glProgram(gl, vs, fs) {
  const p = gl.createProgram();
  for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    gl.attachShader(p, s);
  }
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  return p;
}

// Built with the 2D instance, before its first frame; false (and the lib draws
// every link, as before this layer) where there is no WebGL2 to build it on.
function buildGLLinks() {
  const cv = document.querySelector("#graph canvas");
  const box = cv && cv.parentElement;
  const glc = document.createElement("canvas"), under = document.createElement("canvas");
  const gl = box && glc.getContext("webgl2",
    { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false });
  if (!gl) return false;
  let prog, lay;
  try { prog = glProgram(gl, GL_LINK_VS, GL_INK_FS); lay = glProgram(gl, GL_LAY_VS, GL_LAY_FS); }
  catch (e) { console.warn("graph: WebGL links unavailable, the canvas draws them", e); return false; }
  // After the lib's canvas in the DOM, because "#graph canvas" means the lib's
  // everywhere in this file, and under it on screen: z-index -1 inside a box
  // that isolates its own stacking, so they paint over its (empty) background
  // and below its in-flow canvas.
  box.style.isolation = "isolate";
  for (const c of [under, glc]) {
    c.style.cssText = "position:absolute;left:0;top:0;z-index:-1;pointer-events:none";
    box.appendChild(c);
  }
  under.style.background = GP.bg;
  const index = new Map(ALL_NODES.map((n, i) => [n.id, i]));
  const posW = Math.min(4096, ALL_NODES.length || 1);
  const posH = Math.ceil((ALL_NODES.length || 1) / posW);
  const L = {
    gl, glc, under, uctx: under.getContext("2d"), prog, index, posW, posH,
    pos: new Float32Array(posW * posH * 2), posTex: gl.createTexture(),
    // a band is two instances, its casing and itself
    inst: new ArrayBuffer((ALL_EDGES.length + ALL_EDGES.filter(l => SCREEN_2D[l.type]).length) * GL_STRIDE || GL_STRIDE),
    instBuf: gl.createBuffer(), cornerBuf: gl.createBuffer(), vao: gl.createVertexArray(),
    count: 0, mat: 0, over: false, dirty: true, rgba: new Map(),
    lay, fbo: gl.createFramebuffer(), fboTex: gl.createTexture(),
    u: Object.fromEntries(["pos", "posW", "m0", "m1", "size", "dpr", "over", "ground", "grow"]
      .map(k => [k, gl.getUniformLocation(prog, k)])),
    uLay: { t: gl.getUniformLocation(lay, "t"), share: gl.getUniformLocation(lay, "share") },
  };
  gl.bindTexture(gl.TEXTURE_2D, L.posTex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, posW, posH, 0, gl.RG, gl.FLOAT, null);
  for (const t of [L.posTex, L.fboTex]) {
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  }
  gl.bindVertexArray(L.vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, L.cornerBuf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, -1, 1, -1, 1, 1, 0, -1, 1, 1, 0, 1]), gl.STATIC_DRAW);
  const at = k => gl.getAttribLocation(prog, k);
  gl.enableVertexAttribArray(at("corner"));
  gl.vertexAttribPointer(at("corner"), 2, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, L.instBuf);
  gl.bufferData(gl.ARRAY_BUFFER, L.inst.byteLength, gl.DYNAMIC_DRAW);
  L.attr = { ends: at("ends"), color: at("color"), width: at("width"), screen: at("screen") };
  for (const k in L.attr) {
    gl.enableVertexAttribArray(L.attr[k]);
    gl.vertexAttribDivisor(L.attr[k], 1);
  }
  gl.bindVertexArray(null);
  L.atlas = buildGLAtlas(gl, L.cornerBuf);
  // A lost context (driver reset, the tab's GPU process gone) hands every link
  // back to the lib rather than leaving the frame without them.
  glc.addEventListener("webglcontextlost", () => {
    if (GLLinks !== L) return;
    GLLinks = false;
    under.remove(); glc.remove();
    if (Graph) Graph.backgroundColor(GP.bg).linkVisibility(Graph.linkVisibility());
  });
  return L;
}

// The atlas's two programs and their buffers; null, and the canvas draws the
// atlas (drawAtlas), where they do not build.
function buildGLAtlas(gl, cornerBuf) {
  let fill, seg;
  try { fill = glProgram(gl, GL_FILL_VS, GL_FILL_FS); seg = glProgram(gl, GL_SEG_VS, GL_LINK_FS); }
  catch (e) { console.warn("graph: the atlas draws on the canvas", e); return null; }
  const A = { fill, seg, fvao: gl.createVertexArray(), fbuf: gl.createBuffer(),
              svao: gl.createVertexArray(), sbuf: gl.createBuffer(), fn: 0, sn: 0, of: null };
  const loc = (prog, names) => Object.fromEntries(names.map(k => [k, gl.getUniformLocation(prog, k)]));
  A.uf = loc(fill, ["m0", "m1", "size"]);
  A.us = loc(seg, ["m0", "m1", "size", "dpr"]);
  gl.bindVertexArray(A.fvao);
  gl.bindBuffer(gl.ARRAY_BUFFER, A.fbuf);
  const fp = gl.getAttribLocation(fill, "p"), fc = gl.getAttribLocation(fill, "color");
  gl.enableVertexAttribArray(fp); gl.vertexAttribPointer(fp, 2, gl.FLOAT, false, 12, 0);
  gl.enableVertexAttribArray(fc); gl.vertexAttribPointer(fc, 4, gl.UNSIGNED_BYTE, true, 12, 8);
  gl.bindVertexArray(A.svao);
  gl.bindBuffer(gl.ARRAY_BUFFER, cornerBuf);
  const at = k => gl.getAttribLocation(seg, k);
  gl.enableVertexAttribArray(at("corner"));
  gl.vertexAttribPointer(at("corner"), 2, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, A.sbuf);
  for (const [k, n, type, norm, off] of [["ends", 4, gl.FLOAT, false, 0], ["color", 4, gl.UNSIGNED_BYTE, true, 16],
                                         ["width", 1, gl.FLOAT, false, 20]]) {
    gl.enableVertexAttribArray(at(k));
    gl.vertexAttribPointer(at(k), n, type, norm, GL_SEG_STRIDE, off);
    gl.vertexAttribDivisor(at(k), 1);
  }
  gl.bindVertexArray(null);
  return A;
}

// The geometry atlasGeometry built, into the two buffers: each cell a fan of
// triangles (it is convex), each segment one instance, in ATLAS_LINES' order.
// What the zoom draws of it is atlasLayer's, so a zoom that opens an area or
// reaches the notes repacks (drawGLAtlas, the key).
function packGLAtlas(L, g) {
  const A = L.atlas, gl = L.gl, layer = atlasLayer(g);
  let tris = 0;
  for (const { pts } of g.polys) tris += pts.length / 2 - 2;
  const fb = new ArrayBuffer(tris * 3 * 12), ff = new Float32Array(fb), fu = new Uint8Array(fb);
  let v = 0;
  const vert = (x, y, rgba) => { ff[v * 3] = x; ff[v * 3 + 1] = y; fu.set(rgba, v * 12 + 8); v++; };
  for (const { c, i: at, pts } of g.polys) {
    const rgba = glRGBA(layer.ground(c, at));
    for (let k = 2; k + 3 < pts.length; k += 2) {
      vert(pts[0], pts[1], rgba); vert(pts[k], pts[k + 1], rgba); vert(pts[k + 2], pts[k + 3], rgba);
    }
  }
  let segs = g.coast.length / 4 + layer.borders.length / 4;
  for (const f of layer.provinces.values()) segs += f.length / 4;
  const sb = new ArrayBuffer(segs * GL_SEG_STRIDE), sf = new Float32Array(sb), su = new Uint8Array(sb);
  let i = 0;
  const put = (flat, rgba, w) => {
    for (let k = 0; k < flat.length; k += 4, i++) {
      sf.set(flat.slice(k, k + 4), i * 6);
      su.set(rgba, i * GL_SEG_STRIDE + 16);
      sf[i * 6 + 5] = w;
    }
  };
  const alpha = (hex, a) => { const c = glRGBA(hex); return [c[0], c[1], c[2], Math.round(255 * a)]; };
  for (const [part, w, a] of ATLAS_LINES) {
    if (part === "provinces") for (const [c, f] of layer.provinces) put(f, alpha(g.hue.get(c), a), w);
    else put(part === "coast" ? g.coast : layer.borders, alpha(part === "coast" ? GP.line2 : GP.label, a), w);
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, A.fbuf);
  gl.bufferData(gl.ARRAY_BUFFER, fb, gl.DYNAMIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, A.sbuf);
  gl.bufferData(gl.ARRAY_BUFFER, sb, gl.DYNAMIC_DRAW);
  A.fn = v; A.sn = i; A.of = g; A.key = g.zoom ? g.zoom.key : "";
}

// After the clear and before the links: the ground, then the lines.
function drawGLAtlas(L, g, m, W, H, dpr) {
  const A = L.atlas, gl = L.gl;
  if (A.of !== g || A.key !== (g.zoom ? g.zoom.key : "")) packGLAtlas(L, g);
  gl.blendEquation(gl.FUNC_ADD);
  gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
  gl.useProgram(A.fill);
  gl.uniform3f(A.uf.m0, m.a, m.c, m.e); gl.uniform3f(A.uf.m1, m.b, m.d, m.f);
  gl.uniform2f(A.uf.size, W, H);
  gl.bindVertexArray(A.fvao);
  gl.drawArrays(gl.TRIANGLES, 0, A.fn);
  gl.useProgram(A.seg);
  gl.uniform3f(A.us.m0, m.a, m.c, m.e); gl.uniform3f(A.us.m1, m.b, m.d, m.f);
  gl.uniform2f(A.us.size, W, H); gl.uniform1f(A.us.dpr, dpr);
  gl.bindVertexArray(A.svao);
  gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, A.sn);
  gl.bindVertexArray(null);
}

function dropGLLinks() {
  if (!GLLinks) { GLLinks = null; return; }
  const ext = GLLinks.gl.getExtension("WEBGL_lose_context");
  GLLinks = null;   // first, so the loss event below finds no layer to hand back
  if (ext) ext.loseContext();   // the browser caps live WebGL contexts at ~16
}

// The paint changed: colours, the visible set, or both. Picked up on the next
// frame, which every one of these callers also asks for.
function repaintGLLinks() { if (GLLinks) GLLinks.dirty = true; }

function glRGBA(s) {
  let c = GLLinks.rgba.get(s);
  if (c) return c;
  if (s[0] === "#") {
    const n = parseInt(s.slice(1), 16);
    c = [(n >> 16) & 255, (n >> 8) & 255, n & 255, 255];
  } else {
    const p = s.match(/rgba?\(([^)]+)\)/)[1].split(",").map(Number);
    c = [p[0], p[1], p[2], Math.round((p.length > 3 ? p[3] : 1) * 255)];
  }
  GLLinks.rgba.set(s, c);
  return c;
}

// The mat holds a link as it stands at rest, whatever is in focus: the focus
// is told apart over the atlas by strength, its links whole over the mat's
// share. In the dim ink the mat darkened every cell and the focused links over
// it fell to luma 80 against 86; faded out instead, it would flash the whole
// structure each time the pointer crossed a note. Cached per link, since the
// mat is most of the vault and is repacked on every fade step.
function restPaint(l) {
  if (l.__restOf !== !!l._ctx) {
    const p = { type: l.type, color: l.color, _ctx: l._ctx, requires: l.requires };
    l.__rest = linkPaint(p); l.__restCover = p.__cover; l.__restOf = !!l._ctx;
  }
  return l.__rest;
}

// Two ranges, the mat and then what is being read. The mat is what the atlas
// used to hide (quietNow), and is empty with the atlas off; a band whose layer
// is on is read wherever it is. In each range the written links are grouped by
// colour in the order the lib met them, which is the order its paths stacked
// in, then every band after them (ADR-0146: drawn last), each as its casing, a
// pixel of floor either side, and then itself. A band out of focus or outside
// a comparison is not read, so it goes first and uncased: drawn last, its
// casing cut every lit link that crossed it. A band in the mat goes the same
// way: a casing laid at the mat's share is a dark stripe through a cell.
function packGLLinks() {
  const L = GLLinks, kinds = Object.keys(SCREEN_2D), mat = [], read = [];
  for (const l of ALL_EDGES) {
    if (l._hidden) continue;
    const live = SCREEN_2D[l.type] && linkPaint(l) !== GP.linkDim;
    (quietNow(l) && !live ? mat : read).push(l);
  }
  const u32 = new Uint32Array(L.inst), u8 = new Uint8Array(L.inst), f32 = new Float32Array(L.inst);
  const floor = glRGBA(GP.bg);
  let k = 0;
  const put = (l, rgba, w, side, cover) => {
    const q = k++ * GL_STRIDE;
    u32[q >> 2] = L.index.get(l.from); u32[(q >> 2) + 1] = L.index.get(l.to);
    u8.set(rgba, q + 8); u8[q + 11] = Math.round(cover * 255);
    f32[(q >> 2) + 3] = w; f32[(q >> 2) + 4] = side; f32[(q >> 2) + 5] = side ? SCREEN_INK[side] : 0;
  };
  const pack = (list, rest) => {
    const by = new Map(), bands = kinds.map(() => []), under = [];
    for (const l of list) {
      const s = kinds.indexOf(l.type), c = rest ? restPaint(l) : linkPaint(l);
      if (s >= 0) { (rest || c === GP.linkDim ? under : bands[s]).push(l); continue; }
      const g = by.get(c);
      if (g) g.push(l); else by.set(c, [l]);
    }
    for (const l of under) put(l, glRGBA(rest ? restPaint(l) : linkPaint(l)), width2D(l), SCREEN_2D[l.type][0], 1);
    for (const [c, links] of by) {
      const rgba = glRGBA(c);
      for (const l of links) put(l, rgba, width2D(l), 0, rest ? l.__restCover : l.__cover);
    }
    for (const list of bands) for (const l of list) {
      put(l, floor, width2D(l) + 2, 0, 1);
      put(l, glRGBA(linkPaint(l)), width2D(l), SCREEN_2D[l.type][0], 1);
    }
  };
  pack(mat, true);
  L.mat = k;
  pack(read, false);
  L.count = k; L.over = atlasOn();
  const gl = L.gl;
  gl.bindBuffer(gl.ARRAY_BUFFER, L.instBuf);
  gl.bufferSubData(gl.ARRAY_BUFFER, 0, new Uint8Array(L.inst, 0, k * GL_STRIDE));
  L.dirty = false;
}

// The canvas the lib's pre-paint layers draw on: the one under the links when
// the layer is up, the lib's own when it is not. Same transform either way.
function underCtx(ctx) {
  const L = GLLinks;
  if (!L) return ctx;
  const cv = ctx.canvas, u = L.under;
  if (u.width !== cv.width || u.height !== cv.height) {
    u.width = cv.width; u.height = cv.height;
    u.style.width = cv.style.width; u.style.height = cv.style.height;
  }
  L.uctx.setTransform(1, 0, 0, 1, 0, 0);
  L.uctx.clearRect(0, 0, u.width, u.height);
  L.uctx.setTransform(ctx.getTransform());
  return L.uctx;
}

// After the lib's own frame, so the links follow the positions this frame's
// tick just wrote, the ones the nodes were drawn at.
function drawGLLinks(ctx, scale) {
  const L = GLLinks;
  if (!L) return;
  const gl = L.gl, cv = ctx.canvas, W = cv.width, H = cv.height;
  if (L.glc.width !== W || L.glc.height !== H) {
    L.glc.width = W; L.glc.height = H;
    L.glc.style.width = cv.style.width; L.glc.style.height = cv.style.height;
  }
  if (L.fboW !== W || L.fboH !== H) {
    L.fboW = W; L.fboH = H;
    gl.bindTexture(gl.TEXTURE_2D, L.fboTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, W, H, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, L.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, L.fboTex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }
  if (L.dirty) packGLLinks();
  for (let i = 0; i < ALL_NODES.length; i++) {
    L.pos[2 * i] = ALL_NODES[i].x || 0; L.pos[2 * i + 1] = ALL_NODES[i].y || 0;
  }
  gl.bindTexture(gl.TEXTURE_2D, L.posTex);
  gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, L.posW, L.posH, gl.RG, gl.FLOAT, L.pos);
  const m = ctx.getTransform();
  gl.viewport(0, 0, W, H);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.enable(gl.BLEND);
  if (L.atlas && atlasNow && atlasNow.n) drawGLAtlas(L, atlasNow, m, W, H, m.a / scale);
  if (!L.count) return;
  const links = () => {
    gl.useProgram(L.prog);
    gl.uniform1i(L.u.pos, 0); gl.uniform1i(L.u.posW, L.posW);
    gl.uniform3f(L.u.m0, m.a, m.c, m.e); gl.uniform3f(L.u.m1, m.b, m.d, m.f);
    gl.uniform2f(L.u.size, W, H);
    gl.uniform1f(L.u.dpr, m.a / scale);   // the lib's lineWidth is width / scale, under dpr * scale
    gl.uniform1f(L.u.grow, ARRIVAL ? ARRIVAL.grow : 1);
    gl.uniform1f(L.u.over, L.over ? 1 : 0);
    gl.uniform3fv(L.u.ground, glRGBA(GP.bg).slice(0, 3).map(v => v / 255));
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, L.posTex);
    gl.bindVertexArray(L.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, L.instBuf);
  };
  // No base instance in WebGL2: a range's offset rides on the pointers.
  const draw = (from, n) => {
    const o = from * GL_STRIDE;
    gl.vertexAttribIPointer(L.attr.ends, 2, gl.UNSIGNED_INT, GL_STRIDE, o);
    gl.vertexAttribPointer(L.attr.color, 4, gl.UNSIGNED_BYTE, true, GL_STRIDE, o + 8);
    gl.vertexAttribPointer(L.attr.width, 1, gl.FLOAT, false, GL_STRIDE, o + 12);
    gl.vertexAttribPointer(L.attr.screen, 2, gl.FLOAT, false, GL_STRIDE, o + 16);
    gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, n);
    gl.bindVertexArray(null);
  };
  // Atlas off: the spot inks straight onto the canvas, opaque, over the floor.
  if (!L.over) { links(); draw(0, L.count); return; }
  for (const [from, n, share] of [[0, L.mat, ATLAS_MAT], [L.mat, L.count - L.mat, 1]]) {
    if (!n) continue;
    gl.bindFramebuffer(gl.FRAMEBUFFER, L.fbo);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.BLEND);   // the last link wins where two cross: nothing stacks
    links();
    draw(from, n);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(L.lay);
    gl.bindTexture(gl.TEXTURE_2D, L.fboTex);
    gl.uniform1i(L.uLay.t, 0); gl.uniform1f(L.uLay.share, share);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
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
// moves. Revisit at 5k notes, or if a weak iGPU ever drops frames on an orbit.
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
// 2D unless this browser chose 3D (ADR-0136, the owner's "default view 2d",
// 2026-10-28): the atlas exists only in 2D, and 3D was the hardest picture of
// the course to read. -2 because the first build wrote the old default, 3D,
// into every profile, so the old key cannot tell a choice from that default.
const MODE_KEY = "silica-graph-mode-2";
let mode = "2d";
try { if (localStorage.getItem(MODE_KEY) === "3d") mode = "3d"; } catch (e) {}
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
  // A cold build's warmup runs in the worker: paint nothing until it lands,
  // which is what the lib's synchronous warmup showed too.
  if (SIM && !SIM.ready && !ARRIVAL) { Graph.pauseAnimation(); return; }
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
  // The links on WebGL took the fill with them: at full ratio a pan holds 60fps
  // at 1600x913 and at 3200x2000 (2026-10-26), so going soft buys nothing.
  if (GLLinks && scale !== 1) return;
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
  requestAnimationFrame(() => requestAnimationFrame(() => fitFree(400)));
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

// --- the layout in a worker ---------------------------------------------------
// Both bundles tick the layout once per painted frame, on the main thread, so
// a frame paid for the whole tick before it could paint. On the 23,836-edge
// vault (2026-10-26) that was 13ms of a 13.9ms 2D frame once the links went to
// WebGL, and in 3D 40.7ms of script a frame, 13.7fps and 19.8s to settle cold.
//
// The tick now runs in a worker, on the same d3-force-3d both bundles carry
// (vendored as d3-force-3d.min.js) and its own copy of the notes. The lib's
// engine stays the frame clock: its alpha is held, its forces are inert
// (`inert`: they still take their settings and still resolve the link ends,
// which is where the worker reads them from), and each of its ticks copies the
// worker's latest positions in. So everything that listens to that engine -
// onEngineStop, the 3D meshes, simRunning - runs as before. It works once, on
// a cold build, and is dropped when it is done (ADR-0152).
//
// What the main thread writes wins, and reaches the worker on the next frame:
// a pin (the Map, on a 3D layout still settling) by its fx, a free note put back
// by its x differing from the last x the worker gave it.
// No Worker, or ?noworker in the address, and the frame lays out as it always
// did; a worker that fails reloads the frame with ?noworker.
let SIM = null;
const NO_WORKER = new URLSearchParams(location.search).has("noworker");

// `fed`: called when the lib hands this force its links, which its digest does
// after it has placed the notes (see simInit).
function inert(real, fed) {
  const f = () => {};
  f.initialize = (...a) => real.initialize(...a);
  const proxy = new Proxy(f, {
    get(t, k) {
      if (k === "initialize") return t[k];
      const v = real[k];
      if (typeof v !== "function") return v;
      return (...a) => {
        const r = v.apply(real, a);
        if (k === "links" && a.length && fed) fed();
        return r === real ? proxy : r;
      };
    },
  });
  return proxy;
}

// --- ForceAtlas2, the 2D layout (ADR-0136) ------------------------------------
// Laid out on the backbone (pullOf), d3 in 2D reached 0.515 purity on the
// machine learning course and ForceAtlas2 0.68-0.715 by its seed, in a replica
// of this frame (2026-10-28); today's 2D was 0.462. ForceAtlas2 is Jacomy,
// Venturini, Heymann and Bastian (PLoS ONE 2014), graphology's iterate()
// (vendored as forceatlas2.min.js), run in the layout worker; d3 keeps 3D and
// a 2D frame with no worker. Settings, measured on the course: Gephi's mass
// (1 + the pulling degree) and outbound attraction distribution, scalingRatio
// 2, gravity 1, slowDown 1 (8, graphology's default for this size, stood at
// 0.561 after 600 iterations against 0.678), Barnes-Hut at theta 1 (0.5 bought
// nothing and cost twice the time). The weights are pullOf's strengths, one
// rule for both engines: 0.702 after 1,000 iterations, against 0.685 at the
// Jaccard itself.
// FA2 has no alpha. A run stops when the median note moves less than FA2_EPS
// graph units an iteration, after FA2_MIN of them, or after FA2_BUDGET. There is
// one run: nothing starts it again (ADR-0152).
const FA2_SCALING = 2, FA2_THETA = 1, FA2_MIN = 100, FA2_BUDGET = 1500;
const FA2_EPS = 0.5;   // graph units per iteration; saveLayout rounds to 1 anyway
// No worker, no FA2: the lib's own engine is d3.
const ENGINE_2D = NO_WORKER || typeof Worker === "undefined" ? "d3" : "fa2";

// The worker's half: one protocol, two engines. followNative is the same
// function the lib runs without a worker, by its source.
const simWorkerSource = () => `
const FOLLOW = ${FOLLOW};
${followNative}
const PPN = 10, FA2_SCALING = ${FA2_SCALING}, FA2_THETA = ${FA2_THETA};
const FA2_MIN = ${FA2_MIN}, FA2_BUDGET = ${FA2_BUDGET}, FA2_EPS = ${FA2_EPS};
let sim = null, nodes = [], S = null, min = 0, running = false;
let ticks = 0, total = 0, fa = null;
const pinned = new Set();
const follow = Object.assign(a => followNative(nodes, a), { initialize() {} });
// A message, not setTimeout(0): nested timeouts clamp to 4ms, a third of a tick.
const ch = new MessageChannel();
ch.port1.onmessage = run;
function kick() { if (!running) { running = true; ch.port2.postMessage(0); } }
// The lib's gate, checked where the lib checks it: before the tick.
function step() {
  if (fa) return faStep();
  if (min > 0 && sim.alpha() < min) { running = false; return false; }
  sim.tick(); ticks++; total++;
  return true;
}
function run() {
  if (!running) return;
  const t0 = performance.now();
  while (performance.now() - t0 < 12 && step());
  post();
  if (running) ch.port2.postMessage(0);
}
function post() {
  const p = new Float64Array(nodes.length * 3);
  if (fa) faUnit();
  for (let i = 0; i < nodes.length; i++) {
    if (fa) { p[3 * i] = fa.N[i * PPN] * fa.U; p[3 * i + 1] = fa.N[i * PPN + 1] * fa.U; continue; }
    const n = nodes[i];
    p[3 * i] = n.x; p[3 * i + 1] = n.y; p[3 * i + 2] = n.z || 0;
  }
  postMessage({ p, ticks, total, done: !running }, [p.buffer]);
  ticks = 0;
}

// ForceAtlas2: the notes in a node matrix, the pulling lines in an edge matrix,
// flat as fa2.iterate takes them. It works in its own unit, since its speed is
// not scale-free (scaled up 2x by its scalingRatio, the course stood at 0.643
// after 1,000 iterations against 0.702); a post multiplies it by U, which puts
// the median pulling line at one rest length, the frame's unit, and a pin or a
// note put back comes in divided by U.
function faInit() {
  const n = nodes.length, N = new Float32Array(n * PPN);
  fa = { N, E: new Float32Array(0), U: 1, rest: 1, opt: null, prev: new Float32Array(2 * n),
         st: new Float32Array(n), left: FA2_BUDGET, iters: 0 };
  for (let i = 0; i < n; i++) {
    N[i * PPN] = nodes[i].x / fa.U; N[i * PPN + 1] = nodes[i].y / fa.U;
    N[i * PPN + 7] = 1; N[i * PPN + 8] = 1;   // convergence, size
    // A note Silica runs on is placed beside its notes (faFollow), never by
    // the forces: fixed, so iterate leaves it where faFollow puts it.
    if (nodes[i]._follow) N[i * PPN + 9] = 1;
  }
}
function faForces(f, links) {
  const N = fa.N, keep = [];
  for (let i = 0; i < f.strengths.length; i++) if (f.strengths[i] > 0) keep.push(i);
  const E = new Float32Array(keep.length * 3);
  for (let i = 0; i < nodes.length; i++) N[i * PPN + 6] = 1;
  keep.forEach((li, k) => {
    const s = links[2 * li], t = links[2 * li + 1];
    E[3 * k] = s * PPN; E[3 * k + 1] = t * PPN; E[3 * k + 2] = f.strengths[li];
    N[s * PPN + 6]++; N[t * PPN + 6]++;
  });
  fa.E = E; fa.rest = f.distance;
  fa.opt = { linLogMode: false, outboundAttractionDistribution: true, adjustSizes: false,
    edgeWeightInfluence: 1, scalingRatio: FA2_SCALING, strongGravityMode: false,
    gravity: f.center, slowDown: 1, barnesHutOptimize: true, barnesHutTheta: FA2_THETA };
}
function faFollow() {
  const N = fa.N;
  for (let i = 0; i < nodes.length; i++) {
    const f = nodes[i]._follow;
    if (!f || !f.length || pinned.has(i)) continue;
    let x = 0, y = 0;
    for (const m of f) { x += N[m.index * PPN]; y += N[m.index * PPN + 1]; }
    N[i * PPN] = x / f.length; N[i * PPN + 1] = y / f.length;
  }
}
function faUnit() {
  const N = fa.N, E = fa.E, k = E.length / 3;
  if (!k) return;
  const L = new Float32Array(k);
  for (let j = 0; j < k; j++) {
    const s = E[3 * j], t = E[3 * j + 1];
    L[j] = Math.hypot(N[s] - N[t], N[s + 1] - N[t + 1]);
  }
  L.sort();
  if (L[k >> 1] > 0) fa.U = fa.rest / L[k >> 1];
}
function faStep() {
  if (!fa.left) { running = false; return false; }
  const N = fa.N, P = fa.prev, st = fa.st, n = nodes.length;
  for (let i = 0; i < n; i++) { P[2 * i] = N[i * PPN]; P[2 * i + 1] = N[i * PPN + 1]; }
  fa2.iterate(fa.opt, N, fa.E);
  faFollow();
  for (let i = 0; i < n; i++) st[i] = Math.hypot(N[i * PPN] - P[2 * i], N[i * PPN + 1] - P[2 * i + 1]);
  st.sort();
  const residual = (st[n >> 1] || 0) * fa.U;
  ticks++; total++; fa.iters++;
  if (--fa.left > 0 && fa.iters >= FA2_MIN && residual < FA2_EPS) fa.left = 0;
  if (!fa.left) running = false;
  return running;
}

function forces(f) {
  S = f.strengths;
  sim.force("link").distance(f.distance).strength((l, i) => S[i]);
  sim.force("charge").strength(f.charge).distanceMax(f.chargeMax);
  sim.force("center").strength(f.center);
}
onmessage = ({ data: m }) => {
  if (m.init) {
    const { dim, x, y, z, links, follow: near } = m.init;
    nodes = Array.from({ length: x.length }, (_, i) =>
      ({ index: i, x: x[i], y: y[i], z: z[i], vx: 0, vy: 0, vz: 0 }));
    near.forEach((f, i) => { if (f) nodes[i]._follow = f.map(j => nodes[j]); });
    const L = [];
    for (let i = 0; i < links.length; i += 2) L.push({ source: links[i], target: links[i + 1] });
    // The lib's force order (link, charge, center, then the native notes'
    // follow): the same sums in the same order, so the same layout.
    sim = d3.forceSimulation().numDimensions(dim).stop().nodes(nodes)
      .force("link", d3.forceLink(L)).force("charge", d3.forceManyBody())
      .force("center", d3.forceCenter()).force("follow", follow)
      .alphaDecay(m.init.decay);
    forces(m.init.forces);
    if (m.init.engine === "fa2") { faInit(); faForces(m.init.forces, links); }
    min = m.init.min;
    // The warmup, unposted as the lib's synchronous loop was unpainted.
    if (fa) {
      running = true;
      for (let i = 0; i < m.init.warm && faStep(); i++);
      running = false;
      if (!fa.left) { post(); return; }   // settled inside the warmup
    } else {
      for (let i = 0; i < m.init.warm && !(min > 0 && sim.alpha() < min); i++) { sim.tick(); total++; }
    }
    kick();
    return;
  }
  if (sim) for (const op of m.ops) apply(op);
};
function apply(m) {
  if (m.moved) for (let k = 0; k < m.moved.length; k += 4) {
    const i = m.moved[k], n = nodes[i];
    n.x = m.moved[k + 1]; n.y = m.moved[k + 2]; n.z = m.moved[k + 3];
    n.vx = n.vy = n.vz = 0;
    if (fa) { fa.N[i * PPN] = n.x / fa.U; fa.N[i * PPN + 1] = n.y / fa.U; fa.N[i * PPN + 2] = fa.N[i * PPN + 3] = 0; }
  }
  if (m.pins) {
    for (const i of pinned) {
      nodes[i].fx = nodes[i].fy = nodes[i].fz = null;
      if (fa) fa.N[i * PPN + 9] = nodes[i]._follow ? 1 : 0;
    }
    pinned.clear();
    for (let k = 0; k < m.pins.length; k += 4) {
      const i = m.pins[k], n = nodes[i];
      n.fx = m.pins[k + 1]; n.fy = m.pins[k + 2]; n.fz = m.pins[k + 3];
      if (fa) { fa.N[i * PPN] = n.fx / fa.U; fa.N[i * PPN + 1] = n.fy / fa.U; fa.N[i * PPN + 9] = 1; }
      pinned.add(i);
    }
  }
}`;

// Built with the instance and before its data: it takes the lib's schedule
// over, so the warmup and the decay the build set are the worker's to run.
function buildSim(G) {
  const lib = document.getElementById("sim-lib");
  if (NO_WORKER || typeof Worker === "undefined" || !lib || !lib.textContent.trim()) return null;
  let w, url;
  try {
    url = URL.createObjectURL(new Blob([lib.textContent, "\n", simWorkerSource()], { type: "text/javascript" }));
    w = new Worker(url);
  } catch (e) {
    console.warn("graph: no layout worker, the frame lays out on its own thread", e);
    return null;
  }
  const S = {
    w, url, G, ready: false, inited: false, last: null, applied: null, pins: [],
    min: G.d3AlphaMin(), decay: G.d3AlphaDecay(), warm: G.warmupTicks(),
    // One message per turn of the main thread, its writes in order.
    box: [], queued: false,
    post(m) {
      if (!this.inited) return;
      this.box.push(m);
      if (this.queued) return;
      this.queued = true;
      queueMicrotask(() => { this.queued = false; const ops = this.box; this.box = []; w.postMessage({ ops }); });
    },
  };
  w.onmessage = ({ data }) => {
    if (SIM !== S) return;
    S.last = data;
    // On the circle nothing reads a post but the last: the lib's engine, which
    // would copy them onto the notes, is standing still.
    if (ARRIVAL) { if (data.done) landArrival(data); return; }
    if (!S.ready) { S.ready = true; renderBudget(); }
  };
  // Loud and recovered: the same frame again, on the lib's engine. Reloaded
  // rather than rebuilt in place: rebuilt while the failed instance was still
  // settling in, the next layout came out at half the spread (2026-10-26),
  // and a fresh document carries nothing over to explain.
  w.onerror = e => {
    e.preventDefault();   // reported once, here, instead of again as uncaught
    console.error("graph: the layout worker failed, reloading on the main thread", e.message || e);
    if (SIM !== S) return;
    const u = new URL(location.href);
    u.searchParams.set("noworker", "1");
    location.replace(u.href);
  };
  G.warmupTicks(0).d3AlphaDecay(0).d3AlphaMin(0);   // the lib's alpha holds: its engine only keeps time
  return S;
}

// What the forces are set to, read off the lib's own (inert) force objects:
// applyForces writes them, and the strengths are d3's accessors run over
// the same link array the lib resolved, in float64 so the sums do not drift.
function simForces(G) {
  const link = G.d3Force("link"), charge = G.d3Force("charge"), center = G.d3Force("center");
  const links = link.links(), sf = link.strength();
  const strengths = new Float64Array(links.length);
  links.forEach((l, i) => { strengths[i] = sf(l, i, links); });
  return {
    strengths, distance: link.distance()(links[0] || {}, 0, links),
    charge: charge.strength()(RAW_NODES[0] || {}, 0, RAW_NODES), chargeMax: charge.distanceMax(),
    center: center.strength(),
  };
}

// From the lib's digest of graphData, which kapsule defers past the call: the
// digest places the notes, then hands the link force its links (inert's
// `fed`). Nothing read off the shared notes and edges can say that: a seeded
// layout has its positions before the digest, a rebuild after another mode
// has its ends resolved by the instance before, and a worker started on either
// laid the vault out with no links at all, flat in 3D (2026-10-26).
function simInit(S) {
  if (SIM !== S || S.inited) return;
  const G = S.G, links = G.d3Force("link").links();
  const index = new Map(RAW_NODES.map((n, i) => [n, i]));
  const pairs = new Uint32Array(links.length * 2);
  links.forEach((l, i) => { pairs[2 * i] = index.get(l.source); pairs[2 * i + 1] = index.get(l.target); });
  const N = RAW_NODES.length, x = new Float64Array(N), y = new Float64Array(N), z = new Float64Array(N);
  RAW_NODES.forEach((n, i) => {
    x[i] = n.__wx = n.x || 0; y[i] = n.__wy = n.y || 0; z[i] = n.__wz = n.z || 0;
  });
  S.inited = true;
  S.w.postMessage({ init: {
    dim: is2D() ? 2 : 3, x, y, z, links: pairs, forces: simForces(G),
    decay: S.decay, min: S.min, warm: S.warm,
    engine: is2D() ? ENGINE_2D : "d3",
    follow: RAW_NODES.map(n => n._follow ? n._follow.map(m => index.get(m)) : null),
  } }, [x.buffer, y.buffer, z.buffer, pairs.buffer]);
  simFlush(S);
  if (ARRIVAL) arrivalStart();   // after the worker has its seed: the circle is no place to start from
}

// The main thread's writes since the last frame: every pin, when the set
// changed, and every free note whose position is not the one the worker gave.
function simFlush(S) {
  const pins = [], moved = [], flat = is2D();
  for (let i = 0; i < RAW_NODES.length; i++) {
    const n = RAW_NODES[i];
    if (n.fx != null) { pins.push(i, n.fx, n.fy, flat ? 0 : n.fz || 0); continue; }
    if (n.x !== n.__wx || n.y !== n.__wy || (!flat && n.z !== n.__wz)) {
      moved.push(i, n.x || 0, n.y || 0, n.z || 0);
      n.__wx = n.x; n.__wy = n.y; n.__wz = n.z;
    }
  }
  const m = {};
  if (pins.length !== S.pins.length || pins.some((v, k) => v !== S.pins[k])) { m.pins = Float64Array.from(pins); S.pins = pins; }
  if (moved.length) m.moved = Float64Array.from(moved);
  if (m.pins || m.moved) S.post(m);
}

// One tick of the lib's engine: the worker's latest positions in, a pinned
// note left where the main thread holds it, and no velocity for the lib to
// integrate. The worker's done is the lib's stop, one tick later.
function simSync(S) {
  simFlush(S);
  const d = S.last, flat = is2D();
  if (d && d !== S.applied) {
    S.applied = d;
    const p = d.p;
    for (let i = 0; i < RAW_NODES.length; i++) {
      const n = RAW_NODES[i];
      if (n.fx != null) continue;
      n.x = n.__wx = p[3 * i]; n.y = n.__wy = p[3 * i + 1];
      if (!flat) n.z = n.__wz = p[3 * i + 2];
    }
    simTicks = Math.max(0, d.total - warmupRun);
    if (d.done) S.G.cooldownTicks(0);
  }
  for (const n of RAW_NODES) { n.vx = 0; n.vy = 0; if (!flat) n.vz = 0; }
}

function dropSim() {
  if (!SIM) return;
  SIM.w.terminate();
  URL.revokeObjectURL(SIM.url);
  SIM = null;
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
    dropGLLinks();
    dropSim();
    el.innerHTML = "";
    LinkSeg = null;   // died with the scene; the next 3D build remakes it
  }
  // 3D without MSAA, which the bundle turns on. The 3D frame's cost was the
  // merged LineSegments blended under 4x MSAA, not the notes (2026-10-26, the
  // 23,836-edge vault, Intel iGPU, dpr 2 at fit): 4.5fps as built, 5.3 with no
  // nodes at all, 60 with no links, 60 with MSAA off; 8.3 -> 142fps uncapped at
  // dpr 1.33. A disc's edge is the RIM_PX knockout the flat patch already
  // smooths in pixels, so what MSAA was buying is the links' edges alone, and
  // they now step. Lit spheres (SILICA_GRAPH_SHADING=0) step at the silhouette
  // too. The AA way back is the links as quads with their own coverage, as 2D
  // draws them (GL_LINK_FS), not MSAA over the whole scene.
  const G = is2D() ? new ForceGraph(el)
    : new ForceGraph3D(el, { rendererConfig: { antialias: false } });
  if (is2D()) GLLinks = buildGLLinks();
  syncCanvasSize(G);   // the bundle's default is the size at its load, not now
  // Decided before the data lands: a cache hit is the picture, and the lib's
  // engine stops on its first frame without a tick, its cooldown already spent.
  const seeded = loadLayout();
  if (!seeded) KREF = DISC_PX;
  // A cold 3D build starts from the lib's own seed. With z left at the 0 a 2D
  // cache hit writes, it started flat on the 2D layout, stayed flat (median z 4
  // against a radius of 2,325) and was then kept for good. No z reseeds all three.
  if (!seeded && !is2D()) RAW_NODES.forEach(n => { delete n.z; });
  const warm = seeded ? 0 : WARMUP_TICKS();
  warmupRun = warm; simTicks = 0;
  paintStat(seeded ? "settled" : "settling");
  G.warmupTicks(warm)
    .d3AlphaMin(ALPHA_MIN)
    .d3AlphaDecay(COLD_DECAY)
    .cooldownTicks(0)   // a cold build lifts it below, where it knows who settles
    .cooldownTime(Infinity)
    // The layout is the picture, so a note stays where it was put (ADR-0152).
    .enableNodeDrag(false)
    // Under the WebGL links the floor is the canvas below them (buildGLLinks).
    .backgroundColor(GLLinks ? "transparent" : GP.bg)
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
    .linkVisibility(l => !l._hidden && !quietNow(l) && !(ARRIVAL && !ARRIVAL.grow) &&
      (!GLLinks || l.type === "CITES" || l.requires || libOwnsLink(l)))
    .onNodeClick((n, e) => clickNote(n, e))
    // 3D only: 2D picks its hover itself (pickHover2D). One argument, since
    // the lib's second is the previous node.
    .onNodeHover(n => { if (!is2D()) hoverNote(n); })
    .onBackgroundClick(e => clickNote(null, e))
    .onEngineTick(() => { if (SIM) return simSync(SIM); simTicks++; })
    .onEngineStop(() => {
      // It holds from here (ADR-0152): a cooldown of 0 leaves the lib's engine
      // no tick to run if anything starts it again.
      G.cooldownTicks(0);
      simRunning = false;
      if (ARRIVAL) { renderBudget(); return; }   // it lands when the worker is done (landArrival)
      if (!seeded) {
        if (is2D()) spreadLayout();
        paintStat("settled in " + gnum(warmupRun + simTicks) + " ticks");
        saveLayout();
        dropSim();
      }
      pushMeshes();   // 3D moves a mesh only in a tick, and a hit runs none
      atlasMemo = null;   // positions just moved
      measureGraphRadius();   // the label thresholds are a fraction of it
      if (RIM) placeRim();
      if (fitPending) { fitPending = false; fitWhenPainted(G); }
      else renderBudget();
    });

  if (is2D()) {
    // The layer draws every line at width2D (ADR-0146). These two reach the
    // lib's own stroke only in the frame with no WebGL2, which cannot print a
    // screen: there every link is 1 px, as a written link is, and the absent
    // ones keep the dash they had, in graph units under the zoom transform.
    G.linkWidth(1)
      .linkLineDash(l => isDashed(l) ? DASH_2D : null)
      // With the layer up the lib strokes no line ("replace" with a painter
      // that draws only a citation's arrowhead or a written requires'
      // chevrons, ADR-0145), and keeps the links it must count as visible for
      // those marks and the photons. Without it the marks go after the lib's
      // own stroke, on those two kinds only.
      .linkCanvasObjectMode(l => GLLinks ? "replace" : l.type === "CITES" || l.requires ? "after" : null)
      .linkCanvasObject((l, ctx, k) => l.requires ? drawRequires(l, ctx, k) : drawCiteArrow(l, ctx))
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
      // visible hitch once or twice a second through any pan. A clicked link
      // is found at click time (nearestLink), so the pass buys nothing: paint
      // no link areas at all. Node picking keeps its own painter above.
      // Measured: worst 2D frame 78ms -> 9ms.
      .linkPointerAreaPaint(() => {})
      // Pre, not Post: the atlas is the ground the notes stand on. 2D only:
      // the 3D bundle hands out no THREE, so there an area is a colour and a
      // name. Every real 2D repaint passes here, which is what makes it the
      // seam where the resolution governor watches for streams.
      // An arrangement is its own picture: its wedges replace the atlas and
      // the rim, which are drawings of the free layout.
      .onRenderFramePre((ctx, scale) => {
        drsOnPaint();
        const under = underCtx(ctx);
        drawAtlas(under, scale);   // first, so an arrangement clears it too
        if (ARRIVAL) return;
        if (ARR) { ({ map: drawWedges, clusters: drawGround, flow: drawWires })[ARR.kind](under, scale); return; }
        drawRim(under, scale);
      })
      .onRenderFramePost((ctx, scale) => {
        drawGLLinks(ctx, scale);
        if (ARRIVAL) return;
        placeLinkTag();
        if (!ARR) drawAtlasNames(ctx, scale);
        else if (ARR.kind === "map") drawArrangedNames(ctx, scale);
        else drawLayoutNames(ctx, scale);
      });
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
      // always.
      .nodeLabel(n => {
        const bits = [];
        if (areaOf(n)) bits.push(areaOf(n));
        const st = nodeState(n);
        if (st !== "note") bits.push(st);
        // A line of its own. The mark carries the ring now (RING_S_PX), so
        // this line is no longer the only place the reading lands - but the
        // ring says load-bearing and cannot say how MANY strands, and it is
        // gone below RING_MIN_PX of disc, which is most of a zoomed-out frame.
        const cut = n.cut
          ? "load-bearing · strands " + (n.strands || 0) : null;
        return '<div class="g3d-tip"><b>' + escHtml(n.label) + '</b>' + paperTip(n) +
          (bits.length ? '<i>' + escHtml(bits.join(" · ")) + '</i>' : '') +
          (cut ? '<i>' + escHtml(cut) + '</i>' : '') + '</div>';
      });
  }
  if (RIM_NODES.length)
    for (const name of ["link", "charge", "center"]) G.d3Force(name, rimless(G.d3Force(name)));
  // A hit needs no worker, and its forces go inert all the same: a tick here
  // would be the lib's own d3 walking a picture the reader is looking at.
  SIM = seeded ? null : buildSim(G);
  const S = SIM;
  // A cold 2D build arrives on the circle, the lib's engine standing still with
  // the worker running beside it. Without a worker the layout has nowhere to
  // settle out of sight, and it settles in view, as a cold 3D build does.
  ARRIVAL = S && is2D() && RAW_NODES.length > 1 ? { t: -1, grow: 0 } : null;
  if (!seeded && !ARRIVAL) G.cooldownTicks(Infinity);
  if (seeded || S)
    for (const name of ["link", "charge", "center"])
      G.d3Force(name, inert(G.d3Force(name), S && name === "link" ? () => setTimeout(() => simInit(S)) : null));
  // Forces before the data, and the data last of all. The warmup loop runs the
  // moment graphData lands, so anything set afterwards shapes only the animated
  // tail: with the tuned forces arriving late, the bulk of the layout was being
  // built by d3's untouched defaults and then nudged at low alpha. Widening the
  // warmup made that worse, which is how it surfaced.
  applyForces(G);
  G.graphData({ nodes: ALL_NODES, links: ALL_EDGES });
  return G;
}

// Switching preserves everything that is not the camera: edge filters, the
// community filter, the focused set and the search box all live outside the
// instance. The camera cannot be preserved — there is no sane mapping from a 3D
// camera to a 2D pan/zoom — so it refits once the new layout settles.
function setMode(m, remember = true) {
  if (m === mode && Graph) return;
  if (ARRIVAL) return;   // the circle ends in a few seconds, and it owns the notes
  const rebuild = Graph !== null;   // a switch always refits; a first build may not
  // The plane of a 3D arrangement is not a 2D one: put the layout back, build,
  // and arrange the same focus again in the new mode.
  const rearrange = !!ARR;
  disarrange(false);
  mode = m;
  // Only a switch is a choice: the first build writing its default here is
  // what made the old key unable to tell the two apart (MODE_KEY).
  if (rebuild && remember) try { localStorage.setItem(MODE_KEY, m); } catch (e) {}
  document.querySelectorAll("#mode-toggle button")
    .forEach(b => b.classList.toggle("active", b.dataset.mode === m));
  // The rings are a canvas draw. Leaving their legend up in 3D would promise a
  // channel that mode does not paint.
  document.getElementById("state-legend").style.display = is2D() ? "" : "none";
  // The absent rows' swatch is the band in 2D and the dash in 3D (graph-frame.css).
  document.body.classList.toggle("mode-2d", is2D());
  const atlasRow = document.getElementById("atlas-row");
  if (atlasRow) atlasRow.style.display = is2D() ? "" : "none";
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
  syncLabelLoop();         // the note-name layer is 3D-only, so the mode owns it
  renderBudget();
  if (rearrange) arrangeFocus();
}

// The scope picker is the app's, and its Folders and Areas come from /shape. Zotero collections cannot: membership is read live at THIS build
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
// G is passed explicitly during a build, where the instance is not yet the
// global one: the forces have to be in place before graphData runs the warmup.
function applyForces(G) {
  // distanceMax bounds both over-dispersion and per-tick cost on big graphs.
  G.d3Force("charge").strength(baseCharge()).distanceMax(600 * FORCE_SCALE);
  // The worker runs its own follow; the lib's engine needs one only alone.
  G.d3Force("follow", SIM ? null : followForce);
  G.d3Force("link").distance(baseDist()).strength(linkStrength());
  G.d3Force("center").strength(1);
}

// --- What pulls (ADR-0136) ---------------------------------------------------
// The notes a resolved wikilink reaches, over the edges Louvain and the
// backbone read (no ghost, no Silica note): a k-NN or citation line pulls a
// note outside this set only (pullOf).
const LINKED = new Set();
RAW_EDGES.forEach(e => {
  const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
  if (e.type !== "EXTRACTED" || !a || !b || e.from === e.to || NATIVE[a.type] || NATIVE[b.type]) return;
  LINKED.add(e.from); LINKED.add(e.to);
});
// What a backbone line inside one area pulls with in ForceAtlas2 (pullOf's
// `same`), the atlas being 2D. Measured in this frame on alex_second_brain
// (2,164 notes, 2026-10-29), purity 0.740 -> 0.817, the notes off their area's
// largest island 28.9% -> 13.9%, borders drawn 2,290 -> 1,722, settled in 915
// ticks against 862, and the unrelated areas stood further from the core
// (OSINT 6.13 -> 6.86 vault radii). x4 reached 0.895 but no longer settled
// inside FA2_BUDGET; x8 pulled OSINT in to 4.54 (ADR-0136, amendment
// 2026-10-29). d3 keeps 1: a link strength over 1 overshoots its integrator,
// and 3D draws no atlas.
const AREA_PULL = 2;
// d3's link strength under pullOf's rule. d3 offers no accessor for its own
// default, so it is counted here, once per link array the force is handed.
function linkStrength() {
  let seen = null, count = null;
  return (l, i, links) => {
    if (links !== seen) {
      seen = links;
      count = new Map();
      for (const k of links) for (const n of [k.source, k.target]) count.set(n, (count.get(n) || 0) + 1);
    }
    return pullOf(l, LINKED, 1 / Math.min(count.get(l.source), count.get(l.target)),
      is2D() && ENGINE_2D === "fa2" ? AREA_PULL : 1);
  };
}
// A note Silica runs on follows the notes it links (followNative). The lib's
// engine runs it only without a worker (applyForces); RAW_NODES, so the rim
// never reaches it (ADR-0066).
RAW_NODES.forEach(n => {
  if (NATIVE[n.type])
    n._follow = [...(neighbors[n.id] || [])].map(id => NODE_BY_ID[id]).filter(m => m && !isRim(m));
});
const followForce = Object.assign(alpha => followNative(RAW_NODES, alpha), { initialize() {} });

// --- The atlas (ADR-0136), 2D only ------------------------------------------
// The areas drawn as a map under the notes: every note owns its Voronoi cell,
// clipped to a disc of ATLAS_R rest lengths around it, an area's cells are a
// country and a sub-area's a province (graph_export.detect_sub_areas). The
// ground is the area's colour most of the way to the floor; a country's border
// is a line in the frame's ink, a province's a thinner one in its area's
// colour. What it names and how much of it it draws follow what fits on the
// screen (atlasZoom). It replaced the area blobs of
// ADR-0074 and the semantic zones' hulls, and on a vault with no area of 3
// notes its countries are the zones, as the key row says (ATLAS_BY).
// On unless the reader turned it off, remembered per browser: the owner's
// atlas is the 2D view, the row is the way out of it.
const ATLAS_KEY = "silica-graph-atlas";
let showAtlas = true;
try { if (localStorage.getItem(ATLAS_KEY) === "off") showAtlas = false; } catch (e) {}
{ const cb = document.getElementById("cb-atlas"); if (cb) cb.checked = showAtlas; }
const atlasOn = () => showAtlas && is2D() && !!ATLAS_BY;
// The draft's disc, 13px at fit on the course, was 0.74 of the median
// backbone link there, and a link's rest length is the unit (ForceAtlas2's
// posts are scaled to it, faUnit). The coast is a line of constant width
// (ATLAS_LINES), where the draft filled a 1.15 R ring under the cells: that
// ring was a 15px band at 5x fit.
const ATLAS_R = 0.74;
// Never less than this much ground round a note at the fit: a cell inside its
// own disc is no ground at all, now that a disc is counted in pixels (KREF).
const ATLAS_PX = 10.5;
// The colour's share of a cell's ground, over the floor: the draft's 0.28 on
// graphite and 0.2 on paper, where a tint reads stronger.
const ATLAS_GROUND = LIGHT ? 0.2 : 0.28;
// An island of fewer than ATLAS_MIN_ISLAND notes whose ground meets another
// area's, a note or two of one area standing among another's, draws under the
// notes' zoom as unclaimed ground with no border: otherwise each is a hole
// ringed in ink, and on alex_second_brain 168 of 284 islands were a single
// note (2026-10-29). One standing alone keeps its ground, having no border to
// take away: taking its colour too made 500 of 2,164 cells unclaimed at fit.
// Its dot keeps its area's colour, and ground and border come back with the
// notes' names. The ground is the coast's line most of the way to the floor,
// so it reads as land, not as an area.
const ATLAS_MIN_ISLAND = 3;
const ATLAS_UNCLAIMED = towardFloor(GP.line2, 0.4);
const ZONE_BY_ID = {};
ZONES.forEach(z => { ZONE_BY_ID[z.id] = z; });
const SUB_BY_ID = {};
SUBS.forEach(s => { SUB_BY_ID[s.id] = s; });
const countryOf = n => ATLAS_BY === "zones" ? n.sgroup : n.group;
const provinceOf = n => ATLAS_BY === "zones" ? -1 : n.sub;
const countryName = c => ATLAS_BY === "zones" ? (ZONE_BY_ID[c] || {}).label || "" : COMM_HUBS[c] || "";
// A zone's hue rides on the zone (graph_export.Zone), off any area's key; an
// area's is the colour its notes are painted with, so a country and its dots
// cannot disagree.
function countryHue(c, n) {
  if (ATLAS_BY !== "zones") return baseColor(n);
  const z = ZONE_BY_ID[c];
  return z ? (LIGHT && z.color_paper) || z.color : GP.fallback;
}

// The geometry of the notes on show, rebuilt when a note moved, a filter
// changed what is on show, or the unit changed; a pan or a zoom over a still
// layout serves it from here. While the layout settles it is rebuilt at most
// every ATLAS_SETTLE_MS, a few frames behind the notes, which on 2,165 notes
// is a Delaunay, a cell each and one pass over the edges.
// Every part is plain polygons and segments, drawn by WebGL under the links
// (drawGLAtlas) or by the canvas where there is no WebGL2 (drawAtlas): as
// Canvas2D paths with a clip to the discs the atlas held 8-11 fps at fit on
// alex_second_brain (Intel iGPU, 2026-10-28), against 106 without it.
const ATLAS_SIDES = 24, ATLAS_SETTLE_MS = 66;
let atlasMemo = null, atlasNow = null;
function atlasGeometry() {
  const R = Math.max(ATLAS_R * baseDist(), ATLAS_PX / KREF);
  if (atlasMemo && simRunning && atlasMemo.R === R && performance.now() - atlasMemo.at < ATLAS_SETTLE_MS) return atlasMemo;
  const on = [];
  let sum = 0;
  for (const n of RAW_NODES) {
    if (n._hidden || n._ctx || typeof n.x !== "number" || !(countryOf(n) >= 0)) continue;
    on.push(n);
    sum += n.x * 1.000003 + n.y;
  }
  if (atlasMemo && atlasMemo.sum === sum && atlasMemo.n === on.length && atlasMemo.R === R) return atlasMemo;
  // polys: [{c, pts}]; coast and borders: [x0, y0, x1, y1, ...]; provinces: country -> the same.
  const g = { sum, n: on.length, R, at: performance.now(), on, polys: [], coast: [], borders: [],
              provinces: new Map(), ground: new Map(), hue: new Map(), countries: [], subs: [], box: null, paths: null };
  atlasMemo = g;
  if (!on.length) return g;
  const P = new Float64Array(2 * on.length);
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  on.forEach((n, i) => {
    P[2 * i] = n.x; P[2 * i + 1] = n.y;
    x0 = Math.min(x0, n.x); x1 = Math.max(x1, n.x); y0 = Math.min(y0, n.y); y1 = Math.max(y1, n.y);
  });
  g.box = { x0, y0, x1, y1 };
  const del = new d3Delaunay.Delaunay(P);
  const vor = del.voronoi([x0 - 2 * R, y0 - 2 * R, x1 + 2 * R, y1 + 2 * R]);
  const country = Int32Array.from(on, countryOf), prov = Int32Array.from(on, provinceOf);
  const byCountry = new Map(), byProv = new Map();
  on.forEach((n, i) => {
    const c = country[i];
    if (!g.hue.has(c)) {
      const hue = countryHue(c, n);
      g.hue.set(c, hue);
      g.ground.set(c, towardFloor(hue, ATLAS_GROUND));
    }
    (byCountry.get(c) || byCountry.set(c, []).get(c)).push(i);
    if (prov[i] >= 0) (byProv.get(prov[i]) || byProv.set(prov[i], []).get(prov[i])).push(i);
    // A note on another's very spot has no cell of its own (d3-delaunay
    // leaves a coincident point out): the first one's ground is theirs.
    const cell = vor.cellPolygon(i);
    if (!cell) return;
    const k = cellInDisc(cell, n.x, n.y, R, ATLAS_SIDES), q = k.pts;
    if (q.length < 6) return;
    g.polys.push({ c, i, pts: q });
    for (let e = 0; e < k.arc.length; e++) if (k.arc[e]) {
      const f = (e + 1) % k.arc.length;
      g.coast.push(q[2 * e], q[2 * e + 1], q[2 * f], q[2 * f + 1]);
    }
  });
  const b = atlasBorders(P, country, prov, R, del, vor.circumcenters);
  g.borders = b.country;
  for (const c in b.province) g.provinces.set(+c, b.province[c]);
  // The same lines less a small island's, for the zooms under the notes'.
  const isl = atlasIslands(P, country, R, del);
  g.bare = Uint8Array.from(isl.size, (s, i) => s < ATLAS_MIN_ISLAND && isl.shore[i]);
  const gb = atlasBorders(P, country, prov, R, del, vor.circumcenters, (p, q) => !g.bare[p] && !g.bare[q]);
  g.gBorders = gb.country;
  g.gProvinces = new Map(Object.entries(gb.province).map(([c, f]) => [+c, f]));
  // What the names stand on: an area of three notes or more with a name (two
  // notes are a link, graph_view.ATLAS_MIN_AREA), on its largest island, so
  // its name never sits on another area's ground; a province of three or more
  // in a country it splits. `split` is what atlasZoom may open.
  for (const [c, ids] of byCountry) {
    const name = countryName(c);
    if (ids.length < 3 || !name) continue;
    const count = new Map();
    for (const i of ids) count.set(isl.of[i], (count.get(isl.of[i]) || 0) + 1);
    let main = -1, most = 0;
    for (const [o, k] of count) if (k > most) { main = o; most = k; }
    g.countries.push({ c, name, size: ids.length, split: g.provinces.has(c),
                       at: atlasSpots(ids.filter(i => isl.of[i] === main), P, 1) });
  }
  g.countries.sort((p, q) => q.size - p.size);
  for (const [s, ids] of byProv) {
    const sub = SUB_BY_ID[s];
    if (sub && ids.length > 2 && g.provinces.has(sub.area))
      g.subs.push({ name: sub.name, area: sub.area, size: ids.length, at: atlasSpots(ids, P, 1) });
  }
  g.subs.sort((p, q) => q.size - p.size);
  return g;
}

// The atlas's three lines, in the order they are laid: the coast in the
// floor's line colour, a province's border in its area's colour, a country's
// in the frame's ink. Widths in CSS px, constant on screen at any zoom.
const ATLAS_LINES = [["coast", 1.5, 1], ["provinces", 0.8, 0.5], ["borders", 1.4, 0.7]];

// The atlas at the zoom atlasZoom read: under the notes' zoom a small island
// is unclaimed ground with no border, and only an open area draws its
// provinces; at the notes' zoom, every line. ground(c, i): point i's colour.
function atlasLayer(g) {
  const z = g.zoom, all = !z || z.notes;
  return {
    ground: (c, i) => !all && g.bare[i] ? ATLAS_UNCLAIMED : g.ground.get(c),
    borders: all ? g.borders : g.gBorders,
    provinces: all ? g.provinces : new Map([...g.gProvinces].filter(([c]) => z.open.has(c))),
  };
}

// Under the notes. With WebGL the layer under the links draws it
// (drawGLAtlas) and this only hands it the geometry and the zoom's reading
// (atlasZoom, keyed so a layer repacks when it changes); without, the canvas
// draws the same polygons and segments, one path per colour.
function drawAtlas(ctx, scale) {
  atlasNow = null;   // what the WebGL layer draws this frame, if anything
  // On the circle the links print as the mat they will rest as, over no ground:
  // at full ink 14,563 chords filled alex_second_brain's circle solid.
  if (!atlasOn() || ARR || ARRIVAL) return;
  const g = atlasNow = atlasGeometry();
  if (!g.n) return;
  const z = g.zoom = atlasZoom(2 * g.R * scale, g.countries);
  z.key = z.notes ? "notes" : [...z.open].join(",");
  if (GLLinks && GLLinks.atlas) return;
  if (!g.paths || g.paths.key !== z.key) {
    const seg = flat => { const p = new Path2D(); for (let k = 0; k < flat.length; k += 4) { p.moveTo(flat[k], flat[k + 1]); p.lineTo(flat[k + 2], flat[k + 3]); } return p; };
    const layer = atlasLayer(g), cells = new Map();
    for (const { c, i, pts } of g.polys) {
      const col = layer.ground(c, i), p = cells.get(col) || cells.set(col, new Path2D()).get(col);
      p.moveTo(pts[0], pts[1]);
      for (let k = 2; k < pts.length; k += 2) p.lineTo(pts[k], pts[k + 1]);
      p.closePath();
    }
    g.paths = { key: z.key, cells, coast: seg(g.coast), borders: seg(layer.borders),
                provinces: new Map([...layer.provinces].map(([c, f]) => [c, seg(f)])) };
  }
  ctx.save();
  for (const [col, p] of g.paths.cells) { ctx.fillStyle = col; ctx.fill(p); }
  ctx.lineCap = "round";
  for (const [part, w, alpha] of ATLAS_LINES) {
    ctx.globalAlpha = alpha;
    ctx.lineWidth = w / scale;
    if (part === "provinces") for (const [c, p] of g.paths.provinces) { ctx.strokeStyle = g.hue.get(c); ctx.stroke(p); }
    else { ctx.strokeStyle = part === "coast" ? GP.line2 : GP.label; ctx.stroke(g.paths[part]); }
  }
  ctx.restore();
}

// The names, after the notes, all together: whether one is drawn depends on
// every one before it (placeLabels), largest part or busiest note first. With
// a note under the pointer or in focus, its name and its neighbours' are the
// only ones, as its lines are the only ones drawn.
const ATLAS_FONT = "Archivo, system-ui, sans-serif";
// Where a part's name tries to sit, in screen px off its anchor (x in its
// own widths): on it, then four spots around it, as the draft did.
const ATLAS_SPOTS = [[0, 0], [0, -30], [0, 30], [0.6, 0], [-0.6, 0]];
// Names per screen, by kind, spent on the names placed (placeLabels): with
// the zoom rule (atlasZoom) the draft kept 8 to 30 names on screen at every
// zoom where the frame drew 19 to 150 (2026-10-29). ctx: the quiet names of
// what the reader is inside of, an open area once ATLAS_CONTEXT_MIN of its
// notes are on screen, the sub-areas once the notes are named.
const ATLAS_CAPS = { area: 12, sub: 20, note: 30, ctx: 4 };
const ATLAS_CONTEXT_MIN = 20, ATLAS_CONTEXT_ALPHA = 0.45;
const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
function drawAtlasNames(ctx, scale) {
  if (namesMode === "off") return;
  const g = atlasNow, auto = namesMode === "auto";
  // Auto climbs the atlas, so without one it is drawNode's. Key and All read
  // the notes themselves, atlas or not.
  if (auto && (!g || !g.n)) return;
  const tl = Graph.screen2GraphCoords(0, 0), br = Graph.screen2GraphCoords(freeWidth(), Graph.height());
  const view = { x0: tl.x, y0: tl.y, x1: br.x, y1: br.y };
  const seen = p => p.x >= view.x0 && p.x <= view.x1 && p.y >= view.y0 && p.y <= view.y1;
  const deg = n => (outDeg[n.id] || 0) + (inDeg[n.id] || 0);
  // A name: its lines, [text, px, weight, ink], over a point, and where else
  // it may sit; its kind is the budget it spends (ATLAS_CAPS).
  // spots: [x, y, dx in its own widths, dy in screen px], in the order tried.
  const items = [];
  const note = (n, px, weight, ink, kind) =>
    items.push({ kind, lines: [[clip(n.label, 30), px, weight, ink]], spots: [[n.x, n.y - (px + 4) / scale, 0, 0]] });
  const around = p => { const a = g.on[p.at[0]]; return ATLAS_SPOTS.map(([dx, dy]) => [a.x, a.y, dx, dy]); };
  const part = (p, px, small, cap, kind) =>
    items.push({ kind, lines: [[clip(p.name, cap), px, 600, GP.label], [gnum(p.size) + " notes", small, 400, GP.ghostLabel]],
                 spots: around(p) });
  const context = p =>
    items.push({ kind: "ctx", quiet: true, lines: [[clip(p.name, 32).toUpperCase(), 11, 600, GP.label]], spots: around(p) });
  const lit = LINK_SEL ? [LINK_SEL.from, LINK_SEL.to] : hoverId ? [hoverId] : focusIds;
  if (lit.length) {
    const roots = new Set(lit);
    for (const id of lit) { const n = NODE_BY_ID[id]; if (n && !n._hidden) note(n, 13, 600, GP.accent); }
    RAW_NODES.filter(n => !n._dim && !n._hidden && !roots.has(n.id) && seen(n))
      .sort((p, q) => deg(q) - deg(p)).forEach(n => note(n, 11, 500, GP.label));
  } else if (!auto) {
    RAW_NODES.filter(n => !n._hidden && n.type !== "ghost" && seen(n) && (namesMode === "all" || isKey(n)))
      .sort((p, q) => deg(q) - deg(p)).forEach(n => note(n, 11.5, 500, GP.label, "note"));
  } else {
    const z = g.zoom || atlasZoom(2 * g.R * scale, g.countries);
    const here = new Map();
    for (const n of g.on) if (seen(n)) here.set(countryOf(n), (here.get(countryOf(n)) || 0) + 1);
    const top = g.countries.length ? g.countries[0].size : 1;
    for (const c of g.countries)
      if (!z.open.has(c.c) && here.get(c.c)) part(c, Math.round(12 + 6 * Math.sqrt(c.size / top)), 10, 40, "area");
    const subs = g.subs.filter(p => z.open.has(p.area) && seen(g.on[p.at[0]]));
    if (!z.notes) subs.forEach(p => part(p, 12.5, 9.5, 28, "sub"));
    else g.on.filter(seen).sort((p, q) => deg(q) - deg(p)).forEach(n => note(n, 11.5, 500, GP.label, "note"));
    (z.notes ? subs : g.countries.filter(c => z.open.has(c.c) && here.get(c.c) >= ATLAS_CONTEXT_MIN)
      .sort((p, q) => here.get(q.c) - here.get(p.c))).forEach(context);
  }
  ctx.save();
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  // Each name's boxes, in graph units: its lines stacked, its widest line wide.
  const alts = items.map(it => {
    let w = 0, h = 0;
    ctx.letterSpacing = it.quiet ? "0.12em" : "0px";
    for (const [t, px, weight] of it.lines) {
      ctx.font = weight + " " + px / scale + "px " + ATLAS_FONT;
      w = Math.max(w, ctx.measureText(t).width);
      h += px * 1.25 / scale;
    }
    it.h = h;
    return it.spots.map(([x0, y0, dx, dy]) => {
      const x = x0 + dx * w, y = y0 + dy / scale;
      return { x, y, x0: x - w / 2 - 3 / scale, y0: y - h / 2, x1: x + w / 2 + 3 / scale, y1: y + h / 2 };
    });
  });
  const took = placeLabels(alts, view, items.map(it => it.kind), lit.length || !auto ? null : ATLAS_CAPS);
  // The quiet names first, so the names they frame are drawn over them.
  for (const quiet of [true, false]) took.forEach((k, i) => {
    if (k < 0 || !!items[i].quiet !== quiet) return;
    const at = alts[i][k];
    ctx.globalAlpha = quiet ? ATLAS_CONTEXT_ALPHA : 1;
    ctx.letterSpacing = quiet ? "0.12em" : "0px";
    let y = at.y - items[i].h / 2;
    for (const [t, px, weight, ink] of items[i].lines) {
      y += px * 0.625 / scale;
      ctx.font = weight + " " + px / scale + "px " + ATLAS_FONT;
      ctx.lineWidth = 3.5 / scale;
      ctx.strokeStyle = GP.bg;
      ctx.strokeText(t, at.x, y);
      ctx.fillStyle = ink;
      ctx.fillText(t, at.x, y);
      y += px * 0.625 / scale;
    }
  });
  ctx.restore();
}

function updateAtlas() {
  const cb = document.getElementById("cb-atlas");
  showAtlas = !cb || cb.checked;
  try { localStorage.setItem(ATLAS_KEY, showAtlas ? "on" : "off"); } catch (e) {}
  if (Graph) applyFilters();
}

// With the atlas on, a line shows only as a line of the note under the pointer
// or in focus, the ones markDim leaves lit. Not under a comparison, whose
// reading is the lines between its two parts, nor a collection's covers, the
// lines the rim is drawn for.
const quietNow = l => atlasOn() && !AB && l.type !== "COVERS" && !((hoverId || focusIds.length || LINK_SEL) && !l._dim);

// Same split as computeFocus/applyFocus: flags on the shared objects here,
// repaint only when there is no rebuild coming to read them for free.
function inScope(n) {
  if (!SCOPE) return true;
  if (SCOPE.kind === "folder") return (n.id || "").startsWith(SCOPE.prefix);
  if (SCOPE.kind === "area") return n.group === SCOPE.id;
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
  const toggledOff = n => (n.type === "missing" && !showMissing)
    || (n.type === "ghost" && !showAmbiguous);
  RAW_NODES.forEach(n => {
    n._hidden = toggledOff(n) || (AB ? !(AB.a.has(n.id) || AB.b.has(n.id)) : !inScope(n));
    n._ctx = false;
  });
  // "Papers only" is the library's citation graph. Hiding the concept notes
  // takes their wikilinks along, but the k-NN joins two papers directly and
  // often, so the rule has to be on the edge as well as on the node.
  const citesOnly = !AB && SCOPE && SCOPE.kind === "papers";
  // An area on show keeps the far end of every wikilink that leaves it: what a
  // part reaches is the note across the link, and with the Highlight group
  // gone (ADR-0058) this is where the Graph still says it. A far end shows
  // only the links that reach it from the part, never its own.
  // A collection keeps the far ends of its leaves' wikilinks the same way
  // (ADR-0066 decision 5), and the far end of every COVERS edge below.
  const reach = !AB && SCOPE && (SCOPE.kind === "area" || SCOPE.kind === "collection");
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
  const rimOn = !AB && !!SCOPE && SCOPE.kind === "collection";
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
    // A hidden end takes the edge with it: both renderers filter links on
    // linkVisibility alone and never look at the nodes', so a folder scope hid
    // its outsiders and kept every line to them (1385 wikilinks around 171
    // notes). Read after the node pass and the far-end rule above.
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    const off = (a && a._hidden) || (b && b._hidden) ||
                (e.type === "EXTRACTED" && (e.requires ? !showRequires : !showExtracted)) ||
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
  COVER_EDGES.forEach(e => {
    const a = NODE_BY_ID[e.from], b = NODE_BY_ID[e.to];
    e._hidden = !rimOn || !a || !b || a._hidden || b._hidden;
  });
  atlasMemo = null;   // the atlas is drawn around the notes on show
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
  if (!is2D()) repaintLinkSeg(); else repaintGLLinks();
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
  // Rendered only where a note has a written requires (graph_view).
  const cbReq = document.getElementById("cb-requires");
  if (cbReq) showRequires = cbReq.checked;
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
// Clusters tab and the scope picker call the area, and a title the reader
// wrote (ADR-0058). graph_view sends a name only for the areas of more than
// one note; for the rest this is "", which says nothing rather than repeating
// the note's own name as its area.
function areaOf(n) {
  return (Number.isInteger(n.group) && n.group >= 0 && COMM_HUBS[n.group]) || "";
}

// In 3D a point behind the camera still projects to a point on screen, a
// mirrored one, and a name planted over the wrong cluster is worse than no
// name: ask which side of the camera it is on first. Plain arithmetic on
// camera.position and the controls' target: the bundle exposes no THREE.
function inFrontOfCamera(c) {
  if (is2D()) return true;
  const cam = Graph.camera && Graph.camera();
  const ctr = Graph.controls && Graph.controls();
  if (!cam || !cam.position || !ctr || !ctr.target) return true;
  const p = cam.position, t = ctr.target;
  return (c.x - p.x) * (t.x - p.x) + (c.y - p.y) * (t.y - p.y) + (c.z - p.z) * (t.z - p.z) > 0;
}

// --- Note names in 3D -------------------------------------------------------
// 2D paints its labels on the canvas and gates them on the ZOOM: dots below
// 0.6, the standouts up to 1.5, everything above it. 3D has no zoom scalar, so
// the same idea rides the only distance it does have — camera to node. Move in,
// names appear; the reading is identical, the quantity is not a guess.
//
// DOM, because the bundle hands out no THREE to build a sprite with. The cost
// is bounded by a fixed pool of divs rather than by the vault:
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
  // every name.
  if (!cam || !cam.position || !GRAPH_R) {
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
    if (namesMode === "off" || (namesMode === "key" && !isKey(n))) continue;
    const d = Math.hypot(n.x - p.x, n.y - p.y, (n.z || 0) - p.z);
    // An arrangement (ADR-0088) is looked at from as far as it takes to hold
    // it, and every note on its plane is one the reader asked for: no
    // distance rule, only the pool. Key and All were asked for the same way.
    const asked = ARR || namesMode !== "auto";
    if (!asked && d > far) continue;
    // Between near and far only the standouts, exactly the rule 2D applies
    // between zoom 0.6 and 1.5. Ghosts are names nothing carries; they are
    // already the most numerous thing in the frame and stay unnamed.
    if (!asked && d > near && ((n.size || 16) <= BASE_SIZE || n.type === "ghost")) continue;
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
// of divs - nothing is rendered. It runs only in 3D, the one mode that names
// its notes in the DOM.
let labelRaf = null;
function syncLabelLoop() {
  if (!is2D() && labelRaf === null) {
    const step = () => {
      positionNodeLabels();
      // All 3D-only and all cheap. styleScene is here rather than wired to
      // each rebuild site because the rebuilds are the bundle's, not ours, and
      // land whenever it decides: a frame is the one moment we know they have.
      // linkSegStep rides the same fact - the link carriers it builds from
      // appear whenever the digest does.
      linkSegStep();
      styleScene();
      fogStep();
      labelRaf = requestAnimationFrame(step);
    };
    labelRaf = requestAnimationFrame(step);
  } else if (is2D() && labelRaf !== null) {
    cancelAnimationFrame(labelRaf);
    labelRaf = null;
    positionNodeLabels();   // one last pass to hide them
  }
}

buildNodeLabels();

// First build: the mode comes from localStorage (2D on a fresh profile), and
// setMode owns the whole bring-up — instance, forces, filters, focus. A 2D
// first paint has no default camera worth keeping, so it refits; 3D keeps the
// lib's own initial framing.
//
// It runs down here, after both label layers exist, because setMode starts the
// label loop: the loop's own state is declared above in this section, and a
// bring-up from further up the file would read it before it is initialised.
fitPending = is2D();
syncNames();
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
  const from = positionsNow();   // Clusters plays from where its notes stand
  disarrange(false);   // the filters and the fit below read the free layout
  SCOPE = scope || null;
  placeRim();   // before the filters and the fit, which both read its positions
  document.querySelectorAll(".legend-item[data-community]").forEach(el =>
    el.classList.toggle("active", !!SCOPE && SCOPE.kind === "area" && +el.dataset.community === SCOPE.id));
  applyFilters();
  if (repaint) refreshPaint();
  updateFocusBar();
  if (LAYOUT === "clusters" && !ARRIVAL) applyClusters(from);
  else fitScope();
}

// A scope set while this frame is hidden (the Library tab's "Show on graph"
// scopes, then switches) fitted a canvas of no size, and the zoom clamped to its floor of 0.01, an empty
// picture (seen 2026-10-06). So the fit waits for the frame to have a size.
let scopeFitPending = false;
function fitScope() {
  const el = document.getElementById("graph");
  if (!el.clientWidth || !el.clientHeight || !Graph.width()) { scopeFitPending = true; return; }
  scopeFitPending = false;
  if (SCOPE) fitFree(400, 50); else fitGraph();
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
  reframe();
});
function reframe() {
  if (ARR) requestAnimationFrame(ARR.kind === "map" ? frameArrangement : frameLayout);
}

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
    Graph.zoom(Math.max(Graph.zoom(), 3.2 * KREF), 900);
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
    if (LAYOUT === "network") disarrange(false);   // two parts where they sit, not around one note
    const d = e.data.a && e.data.b ? { a: new Set(e.data.a), b: new Set(e.data.b) } : null;
    AB = d;
    RAW_EDGES.forEach(l => {
      l._ab = !!d && l.type === "EXTRACTED" &&
        ((d.a.has(l.from) && d.b.has(l.to)) || (d.b.has(l.from) && d.a.has(l.to)));
    });
    refreshPaint();
    applyFilters();
    if (LAYOUT !== "network") return;
    if (d) fitFree(400, 50); else fitGraph();
  }
  // The app's tabs: the arrangement the frame shows (ADR-0152).
  if (e.data && e.data.type === "silica-arrange") setLayout(e.data.kind, e.data.root);
  // The app's scope picker, or this frame's area list by way of the app: show
  // one part of the vault where it sits.
  if (e.data && e.data.type === "silica-scope") setScope(e.data.scope);
  // The explore toolbar's note search asks us to *locate* a note: fly the
  // camera to it and dim to its neighbourhood, without opening the drawer
  // (selectNode would) — the user is searching the cloud, not inspecting yet.
  if (e.data && e.data.type === "silica-goto-path") {
    const n = NODE_BY_ID[e.data.path];
    if (n && !ARRIVAL) { focusNode(n); applyFocus(n.id); }
  }
  // The note drawer covers this frame's right edge, which is where the HUD is.
  // Its width comes with the message because the drawer is resizable — the
  // focus bar parks against its edge, not against a constant.
  if (e.data && e.data.type === "silica-host-drawer") {
    document.body.classList.toggle("host-drawer-open", !!e.data.open);
    document.body.style.setProperty("--drawer-w", (e.data.width || 0) + "px");
    // The key comes back with the drawer gone, after the resize that framed for
    // a width with no key in it.
    reframe();
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
    // false only from a folded column with Key shut; an older host sends none
    document.body.classList.toggle("key-folded", e.data.key === false);
    reframe();   // the same, from the folded column
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
  if (LINK_SEL) selectLink(null);
  else if (focusIds.length) { closeDrawer(); clearFocus(); }
  else if (SCOPE) pickScope(null);
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
    else if (!e.buttons) pickHover2D(e);   // a held button is a pan
    // Camera manipulation drops the resolution BEFORE the first heavy paint:
    // capture phase runs ahead of d3-zoom's own handlers, so the very frame
    // this gesture dirties already paints at the motion ratio. A move with a
    // button held is a pan, a wheel notch is a zoom; hover
    // and plain clicks never blur. Restore rides the usual no-paints timer.
    if (e.type === "wheel" || (e.type === "pointermove" && e.buttons))
      setRes(DRS_SCALE);
  }, { capture: true, passive: true }));

// The renderer reports a hover only from a frame under the pointer, so a
// pointer that leaves the frame over a note would keep it lit until it came back.
document.getElementById("graph-wrap").addEventListener("pointerleave", () => hoverNote(null));
