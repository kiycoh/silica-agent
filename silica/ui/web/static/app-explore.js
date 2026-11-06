// --- explore tab: the graph and the surfaces beside it -----------------------
// "graph" is one build (wikilink structure + semantic k-NN overlay, layers
// toggled in the frame's HUD); a click on a note there arranges it as the Map
// (ADR-0088). "flow" and "clusters" are that same frame arranged (ADR-0152).
// The other surfaces render in-page and keep the frame alive, so switching back
// doesn't rebuild the graph.
let graphMode = "graph";
// The note Flow is rooted on; null lets the frame take the vault's first pick.
let flowRoot = null;
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
  // folders / read render in-page. They take the whole pane, so the frame hides
  // and the note search goes with it: it flies the graph camera, which means
  // nothing on a treemap.
  const isShape = m in SHAPE_VIEWS;
  // The Zotero library (ADR-0055) is a pane of rows, not a picture of the
  // graph: no frame, and no note search, which would fly a camera it hides.
  const isLibrary = m === "library";
  const inPane = isShape || isLibrary;
  $("#shape-pane").hidden = !inPane;
  if (!isShape) delete $("#shape-pane").dataset.mode;   // see drawShape
  $("#node-search-wrap").hidden = inPane;
  // The right column (ADR-0072) holds the search over the frame, and the bar
  // at the left keeps out of its band wherever it shows.
  $("#graph-side").hidden = inPane;
  syncSideFold(); // Key belongs to the frame alone
  $("#graph-bar").classList.toggle("by-side", !inPane);
  // Over a pane the bar is in the column's flow, so the pane starts under it
  // whatever its height: a fixed top padding let a key that wraps (a narrow
  // pane, the note column open) sit on the surface's own title.
  $("#graph-bar").classList.toggle("in-flow", inPane);
  $("#graph-frame").hidden = inPane;
  closeNodeResults();
  // #explore/<mode>: the surface is state (P7). Before the pane views return,
  // not after: on the last line it ran for the graph only, so
  // #explore/folders never appeared in the URL and Back skipped the surfaces
  // (measured 2026-09-22).
  if (activeTab === "graph") syncUrl(false);
  if (inPane) {
    $("#graph-loading").hidden = true;
    if (isLibrary) drawLibrary();
    else drawShape();
    return;
  }
  $("#shape-loading").hidden = true;
  if (graphStale) {
    $("#graph-loading").hidden = false;
    $("#graph-frame").src = "/graph?theme=" + liveTheme() + "&t=" + Date.now();
    graphStale = false;
    syncRefreshCue(); // …and this rebuild is what it was offering
  }
  replayGraphArrange();
}

// Which arrangement the frame shows. A frame still loading drops this, so the
// load handler sends it again, as it does the scope.
function replayGraphArrange() {
  if (inFrame(graphMode)) postToViews({
    type: "silica-arrange", kind: graphMode === "graph" ? "network" : graphMode, root: flowRoot,
  });
}
const inFrame = (m) => m === "graph" || m === "flow" || m === "clusters";


// --- the right column folded (ADR-0072, amendment 2026-10-25) --------------
// A note open beside the graph leaves the view 560px at 1440, and the column
// (find, scope, compare) with the frame's key under it took 216 of them. Under
// SIDE_FOLD_W the column is one row, the find and Key, beside the tabs; Key
// opens scope, compare and the frame's key together, where they stood.
const SIDE_FOLD_W = 760;
// Under this the folded row (216px) and the tabs (12rem at least) no longer fit
// side by side: at 390 the find covered Flow and Clusters and Key covered Folders,
// so only Graph could be pressed. The row takes the top and the tabs stand under it.
const SIDE_STACK_W = 460;
let graphKeyOpen = false;

function syncSideFold() {
  const view = $("#view-graph");
  const w = view.clientWidth;
  if (!w) return;   // hidden: the width is 0, not narrow; the next show re-reads it
  const folded = w < SIDE_FOLD_W;
  if (!folded) graphKeyOpen = false;   // the next fold starts shut
  view.classList.toggle("side-folded", folded);
  view.classList.toggle("side-stacked", w < SIDE_STACK_W);
  view.classList.toggle("key-open", folded && graphKeyOpen);
  const key = $("#graph-key");
  key.hidden = !folded || !inFrame(graphMode);
  key.setAttribute("aria-expanded", String(folded && graphKeyOpen));
  // app-notes.js loads after this cut, and the observer can fire between the two
  if (typeof syncSideToViews === "function") syncSideToViews();
}
new ResizeObserver(syncSideFold).observe($("#view-graph"));
$("#graph-key").addEventListener("click", () => { graphKeyOpen = !graphKeyOpen; syncSideFold(); });

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
  flow: "read first, this note, unlocks · a wire is a prerequisite · click a note to place it · the rest stays dimmed where it stands",
  clusters: "one group per area, clockwise from the top, biggest first · a line is a link you wrote · click a note to open it",
  folders: "tile area = notes in the folder · fill = how much the folder mixes areas · click a folder to descend",
  read: "stops in area order, biggest first · each hub then what it opens onto",
  library: "your Zotero library · the count on the tab is what arrived since the last session",
};

function buildSurfaceLine() {
  const box = $("#surface-line");
  const picks = $("#graph-picks");
  box.innerHTML = "";
  picks.innerHTML = "";
  // Clusters arranges what the scope leaves, so its picker stays; Flow is
  // rooted on one note and has nothing for it to choose.
  picks.hidden = !(graphMode === "graph" || graphMode === "clusters");
  if (graphMode !== "graph") box.appendChild(mkEl("div", "sl-key", SURFACE_KEYS[graphMode] || ""));
  if (picks.hidden) return;
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
// A native <select>: folders and areas are two ways of naming a set of notes,
// and one list with two groups says so (the semantic zones were a third until
// ADR-0136). The frame hides the rest and
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
  sel.title = "a second folder or area against the scope: both stay, the links inside each fade, the panel reads them";
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
  sel.title = "show one folder, area or collection where it sits; the rest is hidden, the layout stays, and a part keeps the notes its links reach. A collection's papers outside this vault sit on a rim around the notes";
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
  replayGraphArrange(); // after the scope: Clusters arranges what the scope leaves
  syncDrawerToViews(); // ditto for the drawer, which hides this frame's HUD
  syncDockToViews(); // and the dock's height, which its corner bars park above
  syncSideToViews(); // and the right column's, which its key starts under
});

// --- explore: two surfaces that are not link-space ---------------------------
// the graph lays notes out by how they CONNECT. Two questions that shape cannot
// answer: where does a note sit, and in what order could this be read. One
// /shape load feeds both, and the scope and compare pickers.
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
      ? noExt(stop.path) : stop.label;
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

const SHAPE_VIEWS = { folders: renderFolders, read: renderReading };
async function drawShape() {
  const pane = $("#shape-pane");
  const render = SHAPE_VIEWS[graphMode];
  if (!render) return;
  const s = shapeData || await loadShape();
  if (!s || !SHAPE_VIEWS[graphMode]) return; // mode may have changed while loading
  // A surface you just came to arrives (app-motion.css .surface-in); a redraw
  // of the one you are on does not. The pane forgets its mode whenever it is
  // hidden (setGraphMode), so coming back to a surface is an arrival too.
  const fresh = pane.dataset.mode !== graphMode;
  pane.innerHTML = "";
  const body = SHAPE_VIEWS[graphMode](s);
  pane.appendChild(body);
  pane.dataset.mode = graphMode;
  if (fresh) body.classList.add("surface-in");
}

// Same convention the metrics rows use: a shape row opens its note, on its
// text or its details, like every other gesture with a note behind it
// (ADR-0075).
$("#shape-pane").addEventListener("click", (e) => {
  const el = e.target.closest("[data-path]");
  // Off a row is a deselection, the same way the graph's background is one:
  // without it a row click is a one-way door and the panel keeps stating a note
  // you have stopped looking at.
  if (el && el.dataset.path) showNode({ path: el.dataset.path });
  else announceNode(null, null);
});

