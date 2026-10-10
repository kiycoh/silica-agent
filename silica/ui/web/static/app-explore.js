// --- explore tab: network graph | radial map ---------------------------------
// Two modes in one view, one toolbar. "graph" is one build (wikilink structure
// + semantic k-NN overlay, layers toggled in the frame's HUD); "map" is a radial
// map rooted on one note (/map), which needs a root, so it opens on a hub-picker
// landing. Each mode owns its iframe so switching back doesn't rebuild the graph.
let graphMode = "graph";
// The surface a pasted #explore/<mode> names, read before the boot click: that
// click runs setGraphMode(graphMode) and rewrites the hash, so a reload of
// #explore/areas used to land on the graph. A mode that registers later (the
// Zotero Library, ADR-0055) waits in pendingGraphMode until it does.
{
  const { slug, arg } = parseHash();
  if (slug === "explore" && arg) {
    if (LAYOUT_MODES.some(([m]) => m === arg)) graphMode = arg;
    else pendingGraphMode = arg;
  }
}
let mapRootedPath = null; // note the radial map is rooted on, or null → picker

// Show one mode: toggle which frame/picker is visible, and rebuild /graph only
// when the vault changed under us. Also the entry point when switching INTO the
// explore tab, so it must be idempotent.
function setGraphMode(m) {
  // The surface arrives from the side of the row it was picked toward, the
  // same reading as the tabs above it (app-motion.css).
  const order = LAYOUT_MODES.map(([mode]) => mode);
  $("#view-graph").style.setProperty("--surface-from",
    String(Math.sign(order.indexOf(m) - order.indexOf(graphMode))));
  graphMode = m;
  syncRefreshCue(); // the offer belongs to the graph surface alone
  syncSurfaceTabs();
  buildSurfaceLine();
  const isMap = m === "map";
  // folders / areas / read render in-page. They take the whole pane, so both
  // iframes hide and the note search goes with them: it flies the graph camera
  // and roots the map, and neither means anything on a treemap or a matrix.
  const isShape = m in SHAPE_VIEWS;
  // path renders in the same pane as the three shape views and is NOT one of
  // them: it is rooted on a note, so it keeps the search the shape views hide.
  const isPath = m === "path";
  // The Zotero library (ADR-0055) is a pane of rows, not a picture of the
  // graph: no frame, and no note search, which would fly a camera it hides.
  const isLibrary = m === "library";
  $("#shape-pane").hidden = !(isShape || isPath || isLibrary);
  if (!isShape) delete $("#shape-pane").dataset.mode;   // see drawShape
  $("#node-search-wrap").hidden = isShape || isLibrary;
  // The right column (ADR-0072) holds the search on the graph, map and path,
  // and the bar at the left keeps out of its band wherever it shows.
  $("#graph-side").hidden = isShape || isLibrary;
  $("#graph-bar").classList.toggle("by-side", !(isShape || isLibrary));
  // Over a pane the bar is in the column's flow, so the pane starts under it
  // whatever its height: a fixed top padding let a key that wraps (a narrow
  // pane, the note column open) sit on the surface's own title.
  $("#graph-bar").classList.toggle("in-flow", isShape || isPath || isLibrary);
  $("#graph-frame").hidden = isMap || isShape || isPath || isLibrary;
  $("#map-frame").hidden = !isMap || !mapRootedPath;
  $("#map-picker").hidden = !isMap || !!mapRootedPath;
  closeNodeResults();
  // #explore/<mode>: the surface is state (P7). Before the pane views return,
  // not after: on the last line it ran for graph and map only, so
  // #explore/areas never appeared in the URL and Back skipped four of the
  // seven surfaces (measured 2026-09-22).
  if (activeTab === "graph") syncUrl(false);
  if (isShape || isPath || isLibrary) {
    $("#graph-loading").hidden = true;
    $("#map-loading").hidden = true;
    if (isLibrary) drawLibrary();
    else if (isPath) { drawPath(); $("#node-search").focus(); } else { drawShape(); }
    return;
  }
  $("#shape-loading").hidden = true;
  if (isMap) {
    $("#graph-loading").hidden = true;
    if (mapRootedPath) $("#map-loading").hidden = true;
    $("#node-search").focus();
  } else {
    $("#map-loading").hidden = true;
    if (graphStale) {
      $("#graph-loading").hidden = false;
      $("#graph-frame").src = "/graph?theme=" + liveTheme() + "&t=" + Date.now();
      graphStale = false;
      syncRefreshCue(); // …and this rebuild is what it was offering
    }
  }
}


$("#graph-refresh").addEventListener("click", () => {
  // A finished covers pass offers the same rebuild (ADR-0066 decision 6).
  if (window.coversTake && coversTake()) graphStale = true;
  setGraphMode("graph"); // graphStale is true now, so this is the rebuild
});

// --- the line under the tabs --------------------------------------------------
// What the marks in front of you MEAN, for the one surface you are on, placed
// where reading starts (ADR-0058). One line each: the second lines this held
// were instructions a hover or the surface itself already gives. The Graph has
// no key line: its key is its panel, and its line is scope and compare, which
// choose what you look at.
const SURFACE_KEYS = {
  // The radius is the association cost, not the hop count (mindmap._layout):
  // this line said "ring = hops" while the drawing measured something else.
  map: "nearer the centre = stronger tie · wedge = an area · solid line = wikilink · dashed, hollow dot = related, no link · pick another root from the search",
  folders: "tile area = notes in the folder · fill = how much the folder mixes areas · click a folder to descend",
  areas: "disc = an area, or a zone · dot = a note with a link out · line = one wikilink · cell = the links between two parts · neighbours clockwise from the top, fewest links first",
  read: "stops in area order, biggest first · each hub then what it opens onto",
  path: "− comes before this note · + is what it unlocks · ● is where you are · re-root from any chip",
  library: "your Zotero library · the count on the tab is what arrived since the last session",
};

function buildSurfaceLine() {
  const box = $("#surface-line");
  const picks = $("#graph-picks");
  box.innerHTML = "";
  picks.innerHTML = "";
  picks.hidden = graphMode !== "graph";
  if (graphMode !== "graph") {
    box.appendChild(mkEl("div", "sl-key", SURFACE_KEYS[graphMode] || ""));
    return;
  }
  // Scope and compare choose what the Graph shows, so they join the search in
  // the right column, over the key that reads it (ADR-0072).
  picks.appendChild(buildScopePicker());
  picks.appendChild(buildComparePicker());
  // One load for both pickers. Two callers racing for it used to leave the
  // second list empty; loadShape() now shares its fetch, and one is still one.
  (async () => {
    const s = shapeData || await loadShape();
    if (!s) return;
    for (const id of ["#graph-scope", "#graph-compare"]) { const sel = $(id); if (sel) fillPicker(sel, s); }
    if (window.fillZoteroScopes) fillZoteroScopes();   // ADR-0055: the frame's collections
    syncScopePicker();
  })();
}

// --- scope: one part of the vault on the graph --------------------------------
// A native <select>: folders, areas and zones are three ways of naming a set of
// notes, and one list with three groups says so. The frame hides the rest and
// keeps the layout, so the part is seen where it sits (the compare surface will
// take two of these).
let graphScope = null;   // {kind, prefix|id, label} or null = the whole vault

// scopeValue and parseScope live in app-zotero.js's pure block, where a test
// runs them: a collection scope is a set there (ADR-0066).

// The picker shows the scope it holds. A set of collections picked in the
// Library tab is no option of the menu, so it gets one of its own, named by
// the scope's label, rather than a picker that reads "whole vault".
function syncScopePicker() {
  const sel = $("#graph-scope");
  if (!sel) return;
  const v = scopeValue(graphScope);
  sel.querySelector("option[data-adhoc]")?.remove();
  sel.value = v;
  if (sel.value === v) return;
  const o = mkEl("option", null, graphScope.label || v);
  o.value = v;
  o.dataset.adhoc = "1";
  sel.appendChild(o);
  sel.value = v;
}

function setGraphScope(scope) {
  graphScope = scope;
  postToViews({ type: "silica-scope", scope });
  syncScopePicker();
  setGraphCompare(null);   // a new A ends the comparison; B is picked against A
  // The first collection scope with no fresh pass starts one (ADR-0066 decision 6).
  if (scope && scope.kind === "collection" && window.coversNeeded) window.coversNeeded();
}

// --- compare: the scope against a second part ---------------------------------
// A is the scope, B the second picker. The frame shows both and fades the links
// inside each, so the ones between them stand (ADR-0058: not by width); the
// work panel says what characterises each and quotes the links (compare_sets).
let graphCompare = null;
function setGraphCompare(d) {
  graphCompare = d;
  postToViews({ type: "silica-compare", a: d ? d.a.paths : null, b: d ? d.b.paths : null });
  document.dispatchEvent(new CustomEvent("silica:compare", { detail: d }));
  const sel = $("#graph-compare");
  if (sel && !d) sel.value = "";
}

async function compareWith(v) {
  if (!v) { setGraphCompare(null); return; }
  if (!graphScope) { notify("pick a scope first: the comparison is scope against this", "info"); $("#graph-compare").value = ""; return; }
  try {
    const d = await (await fetch("/compare?a=" + encodeURIComponent(scopeValue(graphScope)) + "&b=" + encodeURIComponent(v))).json();
    if (d.error) { notify(d.error); return; }
    setGraphCompare(d);
    if (window.openNodeDrawer) window.openNodeDrawer();
  } catch { notify("couldn't compare those two"); }
}
function replayGraphScope() { if (graphScope) postToViews({ type: "silica-scope", scope: graphScope }); }

// Folders at depth one and two with at least two notes: deeper, the list is the
// file tree again, which the rail already is.
function folderPrefixes(notes) {
  const count = new Map();
  for (const n of notes) {
    const parts = n.path.split("/");
    for (let d = 1; d < Math.min(parts.length, 3); d++) {
      const pre = parts.slice(0, d).join("/") + "/";
      count.set(pre, (count.get(pre) || 0) + 1);
    }
  }
  return [...count].filter(([, c]) => c >= 2).sort((a, b) => a[0].localeCompare(b[0]));
}

function fillPicker(sel, s) {
  const groups = [
    ["Folders", folderPrefixes(s.notes || []).map(([pre, c]) => ["folder:" + pre, pre + " · " + c])],
    ["Areas", (s.areas || []).map((a) => ["area:" + a.id, a.label + " · " + a.size])],
    ["Zones", (s.zones || []).map((z) => ["zone:" + z.id, z.label + " · " + z.size])],
  ];
  for (const [name, opts] of groups) {
    if (!opts.length) continue;
    const g = mkEl("optgroup");
    g.label = name;
    for (const [v, t] of opts) { const o = mkEl("option", null, t); o.value = v; g.appendChild(o); }
    sel.appendChild(g);
  }
}

function buildComparePicker() {
  const row = mkEl("label", "sl-pick");
  row.appendChild(mkEl("span", null, "compare"));
  const sel = mkEl("select");
  sel.id = "graph-compare";
  sel.title = "a second folder, area or zone against the scope: both stay, the links inside each fade, the panel reads them";
  const none = mkEl("option", null, "nothing");
  none.value = "";
  sel.appendChild(none);
  row.appendChild(sel);
  sel.addEventListener("change", () => compareWith(sel.value));
  return row;
}

function buildScopePicker() {
  const row = mkEl("label", "sl-pick");
  row.appendChild(mkEl("span", null, "scope"));
  const sel = mkEl("select");
  sel.id = "graph-scope";
  sel.title = "show one folder, area, zone or collection where it sits; the rest is hidden, the layout stays, and a part keeps the notes its links reach. A collection's papers outside this vault sit on a rim around the notes";
  const all = mkEl("option", null, "whole vault");
  all.value = "";
  sel.appendChild(all);
  row.appendChild(sel);
  sel.addEventListener("change", () => {
    const v = sel.value;
    if (!v) { setGraphScope(null); return; }
    const label = sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : v;
    setGraphScope(parseScope(v, label, window.zoteroCollectionNames ? zoteroCollectionNames() : []));
  });
  return row;
}

// #graph-frame finishes loading only once the server is done building — drop the
// loader then and re-sync the focus dim state after a (re)load.
$("#graph-frame").addEventListener("load", () => {
  $("#graph-loading").hidden = true;
  replayGraphFocus(); // re-sync whatever is focused after a (re)load
  replayGraphScope();
  syncDrawerToViews(); // ditto for the drawer, which hides this frame's HUD
  syncDockToViews(); // and the dock's height, which its corner bars park above
  syncSideToViews(); // and the right column's, which its key starts under
});
$("#map-frame").addEventListener("load", () => {
  $("#map-loading").hidden = true;
  syncDockToViews(); // the map's corner bar parks above the dock, like the graph's
  syncBarToViews();
});
// The tab bar and its key line float over the map's top edge, and the key
// wraps on a narrow pane, so the map is told the height and fits below it.
function syncBarToViews() {
  if (typeof postToViews !== "function") return; // app-notes.js has not run yet
  postToViews({ type: "silica-host-bar", h: $("#graph-bar").getBoundingClientRect().bottom
    - $("#view-graph").getBoundingClientRect().top });
}
new ResizeObserver(syncBarToViews).observe($("#graph-bar"));

// --- explore: three surfaces that are not link-space -------------------------
// graph and map both lay notes out by how they CONNECT. Three questions that
// shape cannot answer: where does a note sit, how do two areas couple as a
// whole, and in what order could this be read. One /shape load feeds all three.
let shapeData = null;
// The fetch in flight, shared: a second caller used to get null back and draw
// nothing, which is what clicking Explore then a surface inside the first load
// did (the scope picker's load and the surface's raced; 2026-10-06).
let shapeLoading = null;
let folderPrefix = []; // drill state for the containment view

function loadShape() {
  if (shapeData) return Promise.resolve(shapeData);
  return shapeLoading || (shapeLoading = (async () => {
    $("#shape-loading").hidden = false;
    try {
      const d = await (await fetch("/shape")).json();
      if (d.error) { notify("couldn't read the vault shape: " + d.error); return null; }
      shapeData = d;
      return d;
    } catch { notify("couldn't read the vault shape"); return null; }
    finally { shapeLoading = null; $("#shape-loading").hidden = true; }
  })());
}

// Squarified treemap. ~30 lines instead of d3-hierarchy: the vendored bundles
// are the graph renderers, and pulling a layout library in for one rect split
// would be the largest dependency on the page by a wide margin.
// Returns [{...item, x, y, w, h}] in the given rect.
function squarify(items, x, y, w, h) {
  const out = [];
  let rest = items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value);
  while (rest.length) {
    const total = rest.reduce((s, i) => s + i.value, 0);
    const vertical = w < h;          // lay the next row along the shorter side
    const side = vertical ? w : h;
    // Grow the row while the worst aspect ratio in it keeps improving.
    let row = [], best = Infinity, sum = 0;
    for (const it of rest) {
      const trial = sum + it.value;
      const thickness = (trial / total) * (vertical ? h : w);
      const worst = Math.max(
        ...[...row, it].map((r) => {
          const len = (r.value / trial) * side;
          return Math.max(thickness / len, len / thickness);
        }));
      if (row.length && worst > best) break;
      row.push(it); sum = trial; best = worst;
    }
    const thickness = (sum / total) * (vertical ? h : w);
    let off = 0;
    for (const it of row) {
      const len = (it.value / sum) * side;
      out.push(vertical
        ? { ...it, x: x + off, y, w: len, h: thickness }
        : { ...it, x, y: y + off, w: thickness, h: len });
      off += len;
    }
    if (vertical) { y += thickness; h -= thickness; } else { x += thickness; w -= thickness; }
    rest = rest.slice(row.length);
  }
  return out;
}

// One level of the containment tree at `prefix`: immediate children, each with
// its note count and how much of it belongs to a single area.
function folderLevel(notes, prefix, real) {
  const pre = prefix.length ? prefix.join("/") + "/" : "";
  const kids = new Map();
  for (const n of notes) {
    if (!n.path.startsWith(pre)) continue;
    const tail = n.path.slice(pre.length);
    const cut = tail.indexOf("/");
    const name = cut === -1 ? tail : tail.slice(0, cut);
    let k = kids.get(name);
    if (!k) kids.set(name, k = { name, folder: cut !== -1, count: 0, areas: new Map() });
    k.count++;
    // Only multi-note areas count toward purity. A singleton carries a group id
    // like any other community, so counting it would let a folder of six
    // unrelated notes report six areas and a purity of 1/6 — a number about the
    // clustering's tail, not about the filing. `real` is the same set the
    // matrix draws, so the two surfaces agree on what an area is.
    if (real.has(n.area)) k.areas.set(n.area, (k.areas.get(n.area) || 0) + 1);
  }
  return [...kids.values()].map((k) => {
    const placed = [...k.areas.values()].reduce((s, v) => s + v, 0);
    const top = Math.max(0, ...k.areas.values());
    return { ...k, value: k.count, placed,
             purity: placed ? top / placed : null, spread: k.areas.size };
  });
}

// The containment view. Area is note count; the fill is IMPURITY, so a folder
// whose notes all belong to one area is nearly blank and one that mixes nine
// areas is solid. That direction is deliberate: the question this surface
// answers is where filing and meaning disagree, so the disagreements are the
// ones that should be loud. Colouring by area instead would need a 26-hue
// categorical palette, which is exactly what the viz tokens exist to avoid.
function renderFolders(s) {
  const pane = mkEl("div", "shape-body");
  const head = mkEl("div", "shape-head");
  const crumbs = mkEl("div", "fcrumb");
  const mk = (label, depth) => {
    const b = mkEl("button", "fcrumb-b", label);
    b.type = "button";
    b.addEventListener("click", () => { folderPrefix = folderPrefix.slice(0, depth); drawShape(); });
    return b;
  };
  crumbs.appendChild(mk("vault", 0));
  folderPrefix.forEach((p, i) => {
    crumbs.appendChild(mkEl("span", "fcrumb-sep", "/"));
    crumbs.appendChild(mk(p, i + 1));
  });
  head.appendChild(crumbs);
  pane.appendChild(head);

  const rows = folderLevel(s.notes, folderPrefix, new Set(s.areas.map((a) => a.id)));
  if (!rows.length) { pane.appendChild(mkEl("p", "mempty", "Nothing here.")); return pane; }

  const box = mkEl("div", "tmap");
  // A fixed viewBox with percentage-positioned tiles: the layout is computed in
  // an abstract 100x100 box and the container decides the pixels, so a resize
  // needs no relayout and no observer.
  for (const t of squarify(rows, 0, 0, 100, 100)) {
    const tile = mkEl("div", "tmap-tile" + (t.folder ? " folder" : ""));
    tile.style.cssText = `left:${t.x}%;top:${t.y}%;width:${t.w}%;height:${t.h}%`;
    const impurity = t.purity === null ? 0 : 1 - t.purity;
    tile.style.setProperty("--i", impurity.toFixed(3));
    const pur = t.purity === null ? "no area" : `${Math.round(t.purity * 100)}% one area`;
    tile.title = `${t.name} · ${t.count} notes · ${pur}`
      + (t.spread > 1 ? ` · spans ${t.spread} areas` : "");
    const lbl = mkEl("div", "tmap-lbl");
    lbl.appendChild(mkEl("span", "tmap-name", t.name));
    lbl.appendChild(mkEl("span", "tmap-n", nfmt(t.count)));
    tile.appendChild(lbl);
    if (t.folder) {
      tile.addEventListener("click", () => { folderPrefix = [...folderPrefix, t.name]; drawShape(); });
    } else {
      tile.dataset.path = (folderPrefix.length ? folderPrefix.join("/") + "/" : "") + t.name;
      tile.classList.add("clickable");
    }
    box.appendChild(tile);
  }
  pane.appendChild(box);

  const worst = rows.filter((r) => r.purity !== null && r.placed >= 3)
    .sort((a, b) => a.purity - b.purity)[0];
  pane.appendChild(mkEl("p", "mnote", worst
    ? `Most mixed here: ${worst.name}, ${Math.round(worst.purity * 100)}% in its biggest area across ${worst.spread}.`
    : "Too few placed notes here to read purity."));
  return pane;
}

// A reading order, derived and not authored. The one surface here that is not a
// layout: it answers "where do I start and what next", which no arrangement of
// nodes in space can, because space has no order.
function renderReading(s) {
  const pane = mkEl("div", "shape-body read");
  const head = mkEl("div", "shape-head");
  const r = s.reading;
  head.appendChild(mkEl("strong", null, "A way through"));
  head.appendChild(mkEl("span", "shape-sub", `${r.stops.length} stops`));
  pane.appendChild(head);
  // The path is a roll on a reading measure, so on a wide screen it cannot fill
  // the pane and should not try. What the width is worth here is a way back:
  // 24 stops across 8 areas scroll past the top, and the rail is the only place
  // that can say where in the path you are. Rail and roll centre as one cluster.
  const rail = mkEl("nav", "rpath-rail");
  rail.setAttribute("aria-label", "areas in this path");
  pane.appendChild(rail);
  // The vault holds notes that share a name across folders, so a path can list
  // the same label twice for two different files. Where that happens the parent
  // folder rides along: two identical rows pointing at different notes is worse
  // than a longer label, and silently dropping one would be worse still.
  const seenLabel = new Map();
  for (const st of r.stops) seenLabel.set(st.label, (seenLabel.get(st.label) || 0) + 1);
  const ol = mkEl("ol", "rpath");
  let area = null;
  let ai = 0;
  let ordinal = 0;
  const marks = [];
  for (const stop of r.stops) {
    if (stop.area !== area) {
      area = stop.area;
      const id = "rp-a" + (ai++);
      const h = mkEl("li", "rpath-area", area);
      h.id = id;
      ol.appendChild(h);
      const link = mkEl("button", "rpath-rail-i", area);
      link.type = "button";
      link.title = area;
      link.addEventListener("click", () => h.scrollIntoView({ block: "start", behavior: "smooth" }));
      rail.appendChild(link);
      marks.push([h, link]);
    }
    const li = mkEl("li", "rpath-stop clickable");
    li.dataset.path = stop.path;
    // The number is not decoration on this one surface: the whole claim of a
    // reading path is its order, and without the ordinal the roll reads as a
    // grouped list of hubs, which is what the folders view already is.
    li.appendChild(mkEl("span", "rpath-n", String(++ordinal)));
    // The full path, not the parent folder: the notes that collide here are
    // forks of each other under `silica/`, so they share every segment except
    // the first, and one parent segment disambiguated nothing.
    const name = seenLabel.get(stop.label) > 1
      ? stop.path.replace(/\.md$/, "") : stop.label;
    const n = mkEl("span", "rpath-name", name);
    n.title = stop.path;
    li.appendChild(n);
    li.appendChild(mkEl("span", "rpath-why", stop.why));
    ol.appendChild(li);
  }
  pane.appendChild(ol);
  // The cut is stated rather than left to be inferred from a heading count: the
  // path stops at a readable length, and 18 unmentioned areas would make this
  // read as a tour of the whole vault.
  const cut = r.areas_total - r.areas_covered;
  pane.appendChild(mkEl("p", "mnote",
    "Derived from link structure alone, so it promises adjacency and not importance: "
    + "each stop is linked to something already read."
    + (cut > 0 ? ` ${nfmt(cut)} smaller areas are past the end of the path.` : "")));
  // Which area you are in, marked on the rail. Observed rather than measured on
  // scroll: this pane holds a few hundred rows and a scroll handler reading
  // offsets off all of them is the one thing that could make a list janky.
  if (marks.length) {
    const io = new IntersectionObserver((entries) => {
      for (const en of entries) {
        const pair = marks.find(([h]) => h === en.target);
        if (pair) pair[1].classList.toggle("here", en.isIntersecting);
      }
      // Nothing intersecting means every heading is above the fold: keep the
      // last one that was, so the rail never goes blank mid-area.
      if (!rail.querySelector(".here")) {
        const above = marks.filter(([h]) => h.getBoundingClientRect().top < 200).pop();
        if (above) above[1].classList.add("here");
      }
    }, { root: $("#shape-pane"), rootMargin: "-56px 0px -70% 0px" });
    for (const [h] of marks) io.observe(h);
  }
  return pane;
}

// Areas: one part and its neighbours (ADR-0060), over the coupling of every
// pair (ADR-0064). The matrix says HOW MANY links join two areas; the drawing
// says WHICH, one part at a time, because the
// question a reader brings is about a part ("what does NLP reach, and through
// which notes"), and a flat list of every bridge (74 rows, 7 screens on the
// test vault, ordered by pair) made them scan for one name. The centre is the
// scope (ADR-0058 Decision 6): picking it here scopes the Graph, and the
// reverse. The ring keeps ADR-0045's order clockwise from twelve: fewest links
// between the two parts first, then the pair holding more notes, so rarity
// reads as position.
let bridgePartition = "areas";  // "areas" | "zones": the switch beside the picker
let bridgeSpoke = null;         // the neighbour whose group is open, or null
let bridgeLink = null;          // the selected link's key (egoKey), or null
let bridgeCentre = null;        // {kind, id} last drawn at the centre, scope or default

// A fixed drawing, not a simulation: 9 spokes and 48 lines are the largest
// case measured (2026-10-06), which needs no force layout, and a layout that
// moved between loads would throw the ring's order away. The labels are HTML
// over the SVG so their type does not scale with the viewBox.
const EGO = { W: 760, H: 620, CX: 380, CY: 300, R0: 96, R1: 232, RD: 34 };
const GOLDEN = 2.39996;   // the golden angle: dots inside a disc never line up
const egoKey = (r) => r.a.path + "→" + r.b.path;
const egoCount = (n, one, many) => `${nfmt(n)} ${n === 1 ? one : many}`;

function renderAreas(s) {
  const zones = bridgePartition === "zones";
  const kind = zones ? "zone" : "area";
  const unit = zones ? "zones" : "areas";
  const parts = (zones ? s.zones : s.areas) || [];
  const rows = (zones ? s.zone_bridges : s.bridges) || [];
  const byId = new Map(parts.map((p) => [p.id, p]));
  const light = document.documentElement.dataset.theme === "light";
  const col = (id) => { const p = byId.get(id); return (p && (light ? p.color_paper : p.color)) || "var(--ash)"; };
  const pname = (id) => (byId.get(id) || {}).label || ("#" + id);
  const scopeOf = (id) => ({ kind, id, label: `${pname(id)} · ${nfmt((byId.get(id) || {}).size || 0)}` });
  const scoped = graphScope && graphScope.kind === kind && byId.has(graphScope.id);
  // No scope: the source part of the link ADR-0045 ranks first, so the first
  // screen still opens on the rarest link in the vault.
  const ego = scoped ? graphScope.id : rows.length ? rows[0].a.part_id : parts.length ? parts[0].id : null;
  const pickEgo = (id) => { bridgeSpoke = null; bridgeLink = null; setGraphScope(scopeOf(id)); drawShape(); };
  bridgeCentre = ego == null ? null : { kind, id: ego };

  const pane = mkEl("div", "shape-body ego-body");
  // What morphEgo matches the next drawing against: a disc is the same disc
  // only inside one partition, because area 3 and zone 3 are different parts.
  pane.dataset.partition = bridgePartition;
  pane.dataset.ego = String(ego);
  const head = mkEl("div", "shape-head ego-head");
  head.appendChild(mkEl("strong", null, "Areas"));
  const pairs = new Set(rows.map((r) => Math.min(r.a.part_id, r.b.part_id) + ":" + Math.max(r.a.part_id, r.b.part_id)));
  head.appendChild(mkEl("span", "shape-sub",
    `${nfmt(rows.length)} links join ${nfmt(pairs.size)} pairs of ${unit} · `
    + (zones ? `${nfmt(rows.filter((r) => r.other === "same").length)} inside one area`
             : `${nfmt(rows.filter((r) => r.scaffold).length)} from frontmatter`)));
  const tools = mkEl("div", "ego-tools");
  const lab = mkEl("label", "ego-pick");
  lab.appendChild(mkEl("span", null, "around"));
  const sel = mkEl("select");
  for (const p of parts) {
    const o = mkEl("option", null, `${p.label} · ${nfmt(p.size)}`);
    o.value = String(p.id);
    sel.appendChild(o);
  }
  if (ego != null) sel.value = String(ego);
  sel.addEventListener("change", () => pickEgo(Number(sel.value)));
  lab.appendChild(sel);
  tools.appendChild(lab);
  const seg = mkEl("div", "ego-seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "partition");
  for (const m of ["areas", "zones"]) {
    const b = mkEl("button", null, m);
    b.type = "button";
    b.setAttribute("aria-pressed", String(m === bridgePartition));
    // Without an embed index there is no semantic partition to draw over: the
    // switch says why instead of opening onto an empty picture.
    if (m === "zones" && !(s.zones || []).length) {
      b.disabled = true;
      b.title = "no semantic zones: the embed index does not cover the vault";
    }
    b.addEventListener("click", () => {
      if (m === bridgePartition) return;
      bridgePartition = m; bridgeSpoke = null; bridgeLink = null;
      drawShape();
    });
    seg.appendChild(b);
  }
  tools.appendChild(seg);
  const toGraph = mkEl("button", "ego-btn", "show in graph");
  toGraph.type = "button";
  toGraph.title = "the Graph with this part as its scope; the notes its links reach stay lit";
  toGraph.disabled = ego == null;
  toGraph.addEventListener("click", () => { if (!scoped) setGraphScope(scopeOf(ego)); setGraphMode("graph"); });
  tools.appendChild(toGraph);
  head.appendChild(tools);
  pane.appendChild(head);
  if (ego == null) { pane.appendChild(mkEl("p", "mempty", `No ${unit} to draw.`)); return pane; }

  // Every link of the ego, turned so its own note comes first: e is the end
  // inside the ego, f the far end. Grouped by the far part, in ring order.
  const mine = rows.filter((r) => r.a.part_id === ego || r.b.part_id === ego)
    .map((r) => (r.a.part_id === ego ? { r, e: r.a, f: r.b } : { r, e: r.b, f: r.a }));
  const nb = new Map();
  for (const m of mine) (nb.get(m.f.part_id) || nb.set(m.f.part_id, []).get(m.f.part_id)).push(m);
  const order = [...nb.keys()].sort((x, y) => nb.get(x).length - nb.get(y).length
    || nb.get(y)[0].r.joined - nb.get(x)[0].r.joined || pname(x).localeCompare(pname(y)));

  const { W, H, CX, CY, R0, R1, RD } = EGO;
  const ang = new Map(order.map((id, k) => [id, -Math.PI / 2 + k * 2 * Math.PI / order.length]));
  const at = (a, rad, cx = CX, cy = CY) => [cx + rad * Math.cos(a), cy + rad * Math.sin(a)];
  // A note on the ego's side that reaches several neighbours is one dot, at the
  // circular mean of their angles, pushed apart along the rim where two land
  // on one spot.
  const reach = new Map();
  for (const m of mine) {
    if (!reach.has(m.e.path)) reach.set(m.e.path, { end: m.e, as: [] });
    reach.get(m.e.path).as.push(ang.get(m.f.part_id));
  }
  const eList = [...reach.values()].map((d) => ({
    ...d, a: Math.atan2(d.as.reduce((t, x) => t + Math.sin(x), 0), d.as.reduce((t, x) => t + Math.cos(x), 0)),
  })).sort((x, y) => x.a - y.a);
  const gap = Math.min(0.13, 2 * Math.PI / Math.max(1, eList.length));
  const want = eList.map((d) => d.a);
  for (let k = 1; k < eList.length; k++) if (eList[k].a - eList[k - 1].a < gap) eList[k].a = eList[k - 1].a + gap;
  // The push above only runs clockwise, so a run of 15 notes all reaching one
  // neighbour trailed half the rim past it (the busiest zone, 2026-10-06).
  // Each run moves back until it is centred on where its notes point.
  for (let i = 0, j; i < eList.length; i = j + 1) {
    for (j = i; j + 1 < eList.length && eList[j + 1].a - eList[j].a <= gap + 1e-9; j++);
    let shift = 0;
    for (let k = i; k <= j; k++) shift += want[k] - eList[k].a;
    for (let k = i; k <= j; k++) eList[k].a += shift / (j - i + 1);
  }
  const pos = new Map(eList.map((d) => ["e|" + d.end.path, at(d.a, R0 - 14)]));
  // The far notes on a sunflower inside their disc, the first facing the ego:
  // one neighbour holds up to 15 notes over the zones of the test vault
  // (2026-10-06), past what one arc of a 34px rim can space.
  for (const id of order) {
    const fars = [...new Map(nb.get(id).map((m) => [m.f.path, m.f])).values()];
    const c = at(ang.get(id), R1);
    const step = Math.min(7, (RD - 7) / Math.sqrt(fars.length));
    fars.forEach((f, j) => pos.set(id + "|" + f.path, fars.length === 1 ? c
      : at(ang.get(id) + Math.PI + j * GOLDEN, step * Math.sqrt(j + 0.5), c[0], c[1])));
  }

  const main = mkEl("div", "ego-main");
  const fig = mkEl("div", "ego-fig");
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "ego-svg" });
  svg.setAttribute("aria-hidden", "true");   // the index is the accessible reading
  const tip = mkEl("div", "ego-tip");
  tip.hidden = true;
  const showTip = (ev, title, text) => {
    tip.textContent = "";
    tip.appendChild(mkEl("b", null, title));
    if (text) tip.appendChild(mkEl("span", null, text));
    tip.hidden = false;
    const box = fig.getBoundingClientRect();
    const x = ev.clientX - box.left, y = ev.clientY - box.top;
    tip.style.left = Math.min(x + 14, box.width - tip.offsetWidth - 4) + "px";
    tip.style.top = (y + 16 + tip.offsetHeight > box.height ? y - tip.offsetHeight - 10 : y + 16) + "px";
  };
  const hideTip = () => { tip.hidden = true; };
  const pct = (v, of) => (100 * v / of).toFixed(3) + "%";
  const sentence = (r) => r.passage || (r.scaffold ? "a frontmatter link: no sentence carries it"
                                                   : "no sentence quotes this link");

  const ring = svgEl("circle", { cx: CX, cy: CY, r: R0, class: "ego-disc", "data-part": ego });
  ring.style.setProperty("--c", col(ego));
  svg.appendChild(ring);
  const discs = new Map(), labs = new Map();
  for (const id of order) {
    const [x, y] = at(ang.get(id), R1);
    const d = svgEl("circle", { cx: x, cy: y, r: RD, class: "ego-nb", "data-part": id });
    d.style.setProperty("--c", col(id));
    const n = nb.get(id).length;
    d.addEventListener("pointermove", (ev) => showTip(ev, pname(id), egoCount(n, "link", "links") + " · click to put it at the centre"));
    d.addEventListener("pointerenter", () => hot(id, true));
    d.addEventListener("pointerleave", () => { hideTip(); hot(id, false); });
    d.addEventListener("click", (ev) => { ev.stopPropagation(); pickEgo(id); });
    svg.appendChild(d);
    discs.set(id, d);
    const up = Math.sin(ang.get(id)) < -0.25;
    const l = mkEl("div", "ego-lab" + (up ? " up" : ""));
    l.dataset.part = id;
    l.style.left = pct(x, W);
    l.style.top = pct(up ? y - RD - 6 : y + RD + 6, H);
    l.appendChild(mkEl("span", "ego-lab-n", pname(id)));
    l.appendChild(mkEl("span", "ego-lab-c", " · " + nfmt(n)));
    labs.set(id, l);
  }
  const lines = [];
  for (const m of mine) {
    const [x1, y1] = pos.get("e|" + m.e.path), [x2, y2] = pos.get(m.f.part_id + "|" + m.f.path);
    const g = svgEl("g", { class: "ego-ln" + (m.r.scaffold ? " fm" : ""), "data-k": order.indexOf(m.f.part_id) });
    g.appendChild(svgEl("line", { x1, y1, x2, y2, class: "ego-ln-hit" }));
    g.appendChild(svgEl("line", { x1, y1, x2, y2, class: "ego-ln-ink" }));
    g.addEventListener("pointermove", (ev) => showTip(ev, `${m.e.name} ↔ ${m.f.name}`, sentence(m.r)));
    g.addEventListener("pointerleave", hideTip);
    g.addEventListener("click", (ev) => { ev.stopPropagation(); pickLink(m); });
    svg.appendChild(g);
    lines.push([m, g]);
  }
  const dots = [];
  const dot = (key, end, part) => {
    const [x, y] = pos.get(key);
    const c = svgEl("circle", { cx: x, cy: y, r: 4, class: "ego-dot", "data-k": order.indexOf(part) });
    c.style.setProperty("--c", col(part));
    c.addEventListener("pointermove", (ev) => showTip(ev, end.name, "click to open the note"));
    c.addEventListener("pointerleave", hideTip);
    c.addEventListener("click", (ev) => { ev.stopPropagation(); hideTip(); openNote(end.path); });
    svg.appendChild(c);
    dots.push([key, c]);
  };
  for (const d of eList) dot("e|" + d.end.path, d.end, ego);
  for (const id of order) for (const f of new Map(nb.get(id).map((m) => [m.f.path, m.f])).values()) dot(id + "|" + f.path, f, id);
  fig.appendChild(svg);
  for (const l of labs.values()) fig.appendChild(l);
  const centre = mkEl("div", "ego-c");
  centre.style.left = pct(CX, W);
  centre.style.top = pct(CY, H);
  centre.appendChild(mkEl("b", null, pname(ego)));
  centre.appendChild(mkEl("span", null, order.length
    ? `${egoCount(mine.length, "link", "links")} · ${egoCount(order.length, kind, unit)}`
    : `${nfmt((byId.get(ego) || {}).size || 0)} notes`));
  fig.appendChild(centre);
  // 8 of the 22 areas reach no other on the test vault: the picture says so in
  // one line and draws the disc alone.
  if (!order.length) fig.appendChild(mkEl("p", "ego-none", `No wikilink leaves ${pname(ego)}: its links stay inside it.`));
  fig.appendChild(tip);
  // The drawing's background clears the selection; the click carries on to the
  // pane, which clears the work panel the same way.
  fig.addEventListener("click", () => { bridgeSpoke = null; bridgeLink = null; paint(); });
  main.appendChild(fig);

  // The right column (ADR-0064): the index on top, the matrix at its foot. The
  // index is a layer over the column, so a group opens in place and grows down
  // over the matrix instead of pushing it. The reserve holds the closed index
  // of the busiest centre in this partition (8 areas, 9 zones on the test
  // vault), so a recentre does not move the matrix either.
  const side = mkEl("div", "ego-side");
  const nbrs = new Map();
  for (const r of rows) {
    for (const [x, y] of [[r.a.part_id, r.b.part_id], [r.b.part_id, r.a.part_id]]) {
      (nbrs.get(x) || nbrs.set(x, new Set()).get(x)).add(y);
    }
  }
  const reserve = mkEl("div", "ego-reserve");
  reserve.style.setProperty("--slots", Math.max(0, ...[...nbrs.values()].map((v) => v.size)));
  side.appendChild(reserve);

  // The index: the list, grouped by neighbour in the ring's order. It is the
  // keyboard's way through the picture (ADR-0045 Decision 6 kept the list for
  // that access), so the arrows walk its buttons.
  const index = mkEl("div", "ego-index");
  index.setAttribute("aria-label", `links of ${pname(ego)}, by neighbour`);
  const groups = [];
  for (const id of order) {
    const grp = mkEl("div", "ego-grp");
    const h = mkEl("button", "ego-gh");
    h.type = "button";
    const sw = mkEl("span", "ego-sw");
    sw.style.setProperty("--c", col(id));
    h.appendChild(sw);
    h.appendChild(mkEl("span", "ego-gh-n", pname(id)));
    h.appendChild(mkEl("span", "ego-gh-c", nfmt(nb.get(id).length)));
    h.addEventListener("click", (ev) => {
      ev.stopPropagation();
      bridgeSpoke = bridgeSpoke === id ? null : id;
      bridgeLink = null;
      paint();
    });
    h.addEventListener("pointerenter", () => hot(id, true));
    h.addEventListener("pointerleave", () => hot(id, false));
    grp.appendChild(h);
    const body = mkEl("div", "ego-rows");
    const rowEls = [];
    for (const m of nb.get(id)) {
      const b = mkEl("button", "ego-row");
      b.type = "button";
      b.appendChild(mkEl("span", "ego-row-t", `${m.e.name} ↔ ${m.f.name}`));
      b.appendChild(mkEl("span", "ego-row-s" + (m.r.passage ? "" : " none"), sentence(m.r)));
      b.addEventListener("click", (ev) => { ev.stopPropagation(); pickLink(m); });
      body.appendChild(b);
      rowEls.push([m, b]);
    }
    grp.appendChild(body);
    index.appendChild(grp);
    groups.push({ id, h, body, rowEls });
  }
  index.addEventListener("keydown", (ev) => {
    // Escape folds the open group back onto the matrix and keeps the focus on
    // the head that opened it.
    if (ev.key === "Escape" && bridgeSpoke != null) {
      const g = groups.find((x) => x.id === bridgeSpoke);
      bridgeSpoke = null; bridgeLink = null; paint();
      if (g) g.h.focus();
      return;
    }
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    const walk = [...index.querySelectorAll("button")].filter((b) => b.offsetParent);
    const i = walk.indexOf(document.activeElement);
    const next = walk[i + (ev.key === "ArrowDown" ? 1 : -1)];
    if (next) { ev.preventDefault(); next.focus(); }
  });
  side.appendChild(index);

  // The coupling of every pair, in couplingMatrix's swatch form. A part with no
  // link to another is a row and a column of empty cells (8 of 22 areas on the
  // test vault, 2026-10-06), and the drawing already says so by drawing its
  // disc alone, so the grid leaves it out. Zones ship no matrix: theirs is
  // summed from the rows, whose links_between is the tally the area matrix
  // reads (area_bridges), so the two partitions count the same thing.
  const posOf = new Map(parts.map((p, i) => [p.id, i]));
  let grid = s.matrix;
  if (zones) {
    grid = parts.map(() => parts.map(() => 0));
    for (const r of rows) {
      const i = posOf.get(r.a.part_id), j = posOf.get(r.b.part_id);
      if (i != null && j != null) grid[i][j] = grid[j][i] = r.links_between;
    }
  }
  const keep = parts.map((p, i) => i).filter((i) => grid[i].some((v, j) => j !== i && v));
  const links = (x, y) => grid[posOf.get(x)][posOf.get(y)];
  // A cell on the centre opens the other end's group in place; off it, the
  // row's part comes to the centre with the column's open (openBridges).
  const openPair = (x, y) => {
    if (x !== ego && y !== ego) { openBridges(x, y, bridgePartition); return; }
    const other = x === ego ? y : x;
    bridgeSpoke = bridgeSpoke === other ? null : other;
    bridgeLink = null;
    paint();
  };
  const cmx = mkEl("div", "ego-cmx");
  cmx.style.setProperty("--cols", keep.length);
  const mx = couplingMatrix(keep.map((i) => parts[i]), keep.map((i) => keep.map((j) => grid[i][j])), {
    swatch: (p) => col(p.id), onPart: pickEgo, onPair: openPair,
  });
  cmx.appendChild(mx);
  const cellOf = (x, y) => mx.querySelector(`.smx-cell[data-i="${x}"][data-j="${y}"]`);
  for (const c of mx.querySelectorAll(`.smx-cell[data-i="${ego}"], .smx-cell[data-j="${ego}"]`)) c.classList.add("band");
  for (const h of mx.querySelectorAll(`.smx-sw[data-part="${ego}"]`)) h.classList.add("ego");

  // The names live here, not on the heads: one line for whatever the pointer
  // is on, the centre when it is on nothing.
  const readout = mkEl("div", "ego-read");
  readout.setAttribute("aria-live", "polite");
  function read(x = null, y = null) {
    readout.textContent = "";
    const line = mkEl("div", "ego-read-p");
    const name = (id) => {
      const sw = mkEl("i");
      sw.style.setProperty("--c", col(id));
      line.appendChild(sw);
      const b = mkEl("b", null, pname(id));
      b.title = pname(id);
      line.appendChild(b);
    };
    let what;
    if (x == null) {
      name(ego);
      what = order.length
        ? `reaches ${nfmt(order.length)} of ${egoCount(parts.length - 1, kind, unit)} · ${egoCount(mine.length, "link", "links")}`
        : `reaches none of the other ${egoCount(parts.length - 1, kind, unit)} · not in the grid`;
    } else if (y == null || x === y) {
      const p = byId.get(x) || {};
      const k = (nbrs.get(x) || new Set()).size;
      name(x);
      what = `${egoCount(p.size || 0, "note", "notes")}`
        + (p.cohesion != null ? ` · cohesion ${p.cohesion}` : "")
        + ` · reaches ${egoCount(k, kind, unit)}` + (x === ego ? "" : " · click to put it at the centre");
    } else {
      const v = links(x, y);
      name(x);
      line.appendChild(mkEl("span", "ego-read-x", v ? "↔" : "↮"));
      name(y);
      what = !v ? "nothing links them"
        : `${egoCount(v, "link", "links")} · ` + (x === ego || y === ego ? "click to open them" : `click to centre ${pname(x)}`);
    }
    readout.appendChild(line);
    readout.appendChild(mkEl("span", "ego-read-n", what));
  }
  read();
  // The crosshair: the hovered cell's row and column, so a cell 12 columns
  // from its heads can still be read back to both.
  let crossAt = null;
  const cross = (on) => {
    if (!crossAt) return;
    const [x, y] = crossAt;
    for (const el of mx.querySelectorAll(`.smx-cell[data-i="${x}"], .smx-cell[data-j="${y}"], `
      + `.smx-sw.smx-row[data-part="${x}"], .smx-sw.smx-col[data-part="${y}"]`)) el.classList.toggle("hx", on);
    if ((x === ego) !== (y === ego)) hotDraw(x === ego ? y : x, on);
  };
  mx.addEventListener("pointerover", (ev) => {
    const c = ev.target.closest(".smx-cell, .smx-sw");
    if (!c) return;
    const x = Number(c.dataset.i ?? c.dataset.part), y = c.dataset.j != null ? Number(c.dataset.j) : null;
    if (crossAt && crossAt[0] === x && crossAt[1] === y) return;
    cross(false);
    crossAt = y == null ? null : [x, y];
    cross(true);
    read(x, y);
  });
  mx.addEventListener("pointerleave", () => { cross(false); crossAt = null; read(); });
  cmx.appendChild(readout);
  const pairsAll = keep.length * (keep.length - 1) / 2;
  let linked = 0;
  for (const i of keep) for (const j of keep) if (i < j && grid[i][j]) linked++;
  cmx.appendChild(mkEl("p", "mnote",
    `${egoCount(keep.length, kind, unit)} with a link`
    + (parts.length > keep.length ? ` · ${nfmt(parts.length - keep.length)} left out` : "")
    + ` · ${nfmt(linked)} of ${nfmt(pairsAll)} pairs linked`));
  side.appendChild(cmx);
  main.appendChild(side);
  pane.appendChild(main);

  // One hover lights the same pair in the three places that draw it: the
  // disc, its group in the index, and its cell.
  function hotDraw(id, on) {
    const d = discs.get(id);
    if (d) d.classList.toggle("hot", on);
    const l = labs.get(id);
    if (l) l.classList.toggle("hot", on);
    const g = groups.find((x) => x.id === id);
    if (g) g.h.classList.toggle("hot", on);
  }
  function hot(id, on) {
    hotDraw(id, on);
    for (const c of [cellOf(ego, id), cellOf(id, ego)]) if (c) c.classList.toggle("hot", on);
    if (on) read(ego, id); else read();
  }

  // Selection is painted in place, not redrawn: a redraw would drop the focus
  // the keyboard is walking the index with.
  function pickLink(m) {
    hideTip();
    bridgeLink = egoKey(m.r);
    bridgeSpoke = m.f.part_id;
    paint();
    document.dispatchEvent(new CustomEvent("silica:bridge", { detail: { ...m.r, partition: bridgePartition } }));
    if (window.openNodeDrawer) window.openNodeDrawer();
  }
  function paint() {
    const focus = bridgeLink != null || bridgeSpoke != null;
    const lit = (m) => (bridgeLink != null ? egoKey(m.r) === bridgeLink : bridgeSpoke == null || m.f.part_id === bridgeSpoke);
    const litKeys = new Set(), litParts = new Set();
    for (const [m, g] of lines) {
      const on = lit(m);
      g.classList.toggle("off", focus && !on);
      g.classList.toggle("on", egoKey(m.r) === bridgeLink);
      if (on) { litKeys.add("e|" + m.e.path); litKeys.add(m.f.part_id + "|" + m.f.path); litParts.add(m.f.part_id); }
    }
    for (const [key, c] of dots) c.classList.toggle("off", focus && !litKeys.has(key));
    for (const [id, d] of discs) {
      d.classList.toggle("off", focus && !litParts.has(id));
      labs.get(id).classList.toggle("off", focus && !litParts.has(id));
    }
    for (const g of groups) {
      const open = g.id === bridgeSpoke;
      g.h.setAttribute("aria-expanded", String(open));
      g.h.classList.toggle("open", open);
      g.body.hidden = !open;
      for (const [m, b] of g.rowEls) b.classList.toggle("on", egoKey(m.r) === bridgeLink);
    }
    for (const c of mx.querySelectorAll(".smx-cell.pin")) c.classList.remove("pin");
    if (bridgeSpoke != null) for (const c of [cellOf(ego, bridgeSpoke), cellOf(bridgeSpoke, ego)]) if (c) c.classList.add("pin");
  }
  paint();
  return pane;
}

// The recentre. Picking a neighbour redraws the whole picture around it, and
// drawn cold that is a new picture: the reader has to find the part they just
// clicked again, and cannot see that the old centre is now one of its
// neighbours. So a disc that is in both drawings travels from where it was to
// where it is (the clicked one into the middle, the old centre out to its
// spoke), its name travels with it, and only then do the links draw outward
// from the centre, spoke by spoke in ring order: the ring is read clockwise
// from twelve and the drawing arrives in the same order. The previous drawing
// stays over the new one as a ghost for the length of its fade, which is what
// takes the parts that dropped out of the ring off screen.
//
// FLIP on transforms only; nothing here touches cx/cy/r, so the picture the
// handlers and the index read is final from the first frame. One run per
// click, ~60 elements on the largest ring measured (9 spokes, 48 lines,
// 2026-10-06). Interrupted mid-flight, the next morph starts from where the
// last one was headed, not from where its discs were drawn at that instant.
const EGO_MS = 420;
function morphEgo(prev, next) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const fig = next.querySelector(".ego-fig");
  const svg = fig && fig.querySelector(".ego-svg");
  if (!svg) return;
  const same = !!prev && prev.dataset.partition === next.dataset.partition;
  // A repaint (theme, a selection, the same centre picked again) is not a move.
  if (same && prev.dataset.ego === next.dataset.ego) return;
  const was = new Map();
  if (same) for (const c of prev.querySelectorAll(".ego-svg [data-part]")) was.set(c.dataset.part, c);
  const ease = getComputedStyle(document.documentElement).getPropertyValue("--ease").trim();
  const run = (el, from, delay, dur = EGO_MS) =>
    el.animate([{ ...from, offset: 0 }], { duration: dur, delay, easing: ease, fill: "backwards" });
  const geo = (c) => ["cx", "cy", "r"].map((a) => +c.getAttribute(a));
  const px = fig.clientWidth / EGO.W;   // the labels are HTML, in CSS px

  for (const d of svg.querySelectorAll("[data-part]")) {
    const lab = d.classList.contains("ego-disc") ? fig.querySelector(".ego-c")
      : fig.querySelector(`.ego-lab[data-part="${d.dataset.part}"]`);
    const o = was.get(d.dataset.part);
    if (!o) {
      run(d, { opacity: 0, transform: "scale(0.6)" }, 90, 320);
      if (lab) run(lab, { opacity: 0 }, 200, 220);
      continue;
    }
    const [x, y, r] = geo(d), [x0, y0, r0] = geo(o);
    // The scale would scale the stroke with it: a 34px disc grown from the
    // 96px centre opened on a 4px ring. Counter-scaled, it stays the drawn width.
    const sw = parseFloat(getComputedStyle(d).strokeWidth) * r / r0;
    run(d, { transform: `translate(${x0 - x}px, ${y0 - y}px) scale(${r0 / r})`, strokeWidth: sw }, 0);
    // The new centre's name comes into focus as it lands; a spoke's just moves.
    if (lab) run(lab, { translate: `${(x0 - x) * px}px ${(y0 - y) * px}px`,
                        ...(lab.classList.contains("ego-c") ? { filter: "blur(3px)", opacity: 0.4 } : {}) }, 0);
  }
  // Explicit both ends: a dash list does not interpolate to `none`, and after
  // the run the line goes back to having no dash at all, which draws the same.
  const lag = (k) => 150 + Math.min(Math.max(k, 0) * 28, 160);
  for (const g of svg.querySelectorAll(".ego-ln")) {
    const ink = g.querySelector(".ego-ln-ink");
    const [x1, y1, x2, y2] = ["x1", "y1", "x2", "y2"].map((a) => +ink.getAttribute(a));
    const len = Math.hypot(x2 - x1, y2 - y1);
    ink.animate([{ strokeDasharray: `0 ${len}` }, { strokeDasharray: `${len} 0` }],
      { duration: 340, delay: lag(+g.dataset.k), easing: ease, fill: "backwards" });
  }
  // A far note lights when its line reaches it; the centre's own rim first.
  for (const c of svg.querySelectorAll(".ego-dot")) {
    const k = +c.dataset.k;
    run(c, { opacity: 0, transform: "scale(0)" }, k < 0 ? 120 : lag(k) + 220, 200);
  }

  // The index is a different list around a different centre; on an arrival
  // the whole surface is already fading in, so only a move fades it alone.
  const idx = prev && next.querySelector(".ego-index");
  if (idx) run(idx, { opacity: 0, translate: "0 4px" }, 140, 260);

  const ghost = prev && prev.querySelector(".ego-fig");
  if (!ghost) return;
  // What the new drawing already carries leaves the ghost now, or it would be
  // seen twice: once fading where it was, once travelling from there.
  for (const el of ghost.querySelectorAll(".ego-ghost, .ego-c, .ego-tip, .ego-none")) el.remove();
  for (const el of ghost.querySelectorAll("[data-part]")) if (was.has(el.dataset.part)
      && svg.querySelector(`[data-part="${el.dataset.part}"]`)) el.remove();
  ghost.classList.add("ego-ghost");
  ghost.inert = true;
  fig.prepend(ghost);
  ghost.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 200, easing: ease, fill: "forwards" })
    .onfinish = () => ghost.remove();
}

const SHAPE_VIEWS = { folders: renderFolders, areas: renderAreas, read: renderReading };
// A pair of parts, from a matrix cell or the work panel's bridge: Areas on the
// first with the second's spoke open.
function openBridges(x, y, partition = "areas") {
  // Already centred on one end of the pair: stay there and open the other's
  // spoke, rather than flipping the picture to the link's source part.
  const kind = partition === "zones" ? "zone" : "area";
  if (bridgeCentre && bridgeCentre.kind === kind && bridgeCentre.id === y) [x, y] = [y, x];
  bridgePartition = partition;
  bridgeSpoke = y;
  bridgeLink = null;
  const parts = (shapeData && (partition === "zones" ? shapeData.zones : shapeData.areas)) || [];
  const p = parts.find((q) => q.id === x);
  setGraphScope({ kind, id: x,
                  label: p ? `${p.label} · ${nfmt(p.size)}` : "#" + x });
  setGraphMode("areas");
}
window.openBridges = openBridges;

async function drawShape() {
  const pane = $("#shape-pane");
  const render = SHAPE_VIEWS[graphMode];
  if (!render) return;
  const s = shapeData || await loadShape();
  if (!s || !SHAPE_VIEWS[graphMode]) return; // mode may have changed while loading
  // A surface you just came to arrives (app-motion.css .surface-in); a redraw
  // of the one you are on does not, and on Areas it morphs instead. The pane
  // forgets its mode whenever it is hidden (setGraphMode), so coming back to a
  // surface is an arrival too.
  const prev = pane.firstElementChild;
  const fresh = pane.dataset.mode !== graphMode;
  pane.innerHTML = "";
  const body = SHAPE_VIEWS[graphMode](s);
  pane.appendChild(body);
  pane.dataset.mode = graphMode;
  if (fresh) body.classList.add("surface-in");
  if (graphMode === "areas") morphEgo(fresh ? null : prev, body);
}

// Same convention the metrics rows use: a shape row opens its note, on its
// text or its details, like every other gesture with a note behind it
// (ADR-0075).
$("#shape-pane").addEventListener("click", (e) => {
  // The path surface puts a re-root button INSIDE its chips, so the chip's own
  // "select this note" verb has to stand aside for it. Registration order on
  // one node cannot be relied on, so the generic handler declines rather than
  // the specific one shouting.
  if (e.target.closest("[data-root]")) return;
  const el = e.target.closest("[data-path]");
  // Off a row is a deselection, the same way the graph's background is one:
  // without it a row click is a one-way door and the panel keeps stating a note
  // you have stopped looking at.
  if (el && el.dataset.path) showNode({ path: el.dataset.path });
  else announceNode(null, null);
});

