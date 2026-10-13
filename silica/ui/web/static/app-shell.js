// --- unified sidebar (stats · search · files · history) ----------------------
if (localStorage.getItem("sidebar-collapsed") === "1")
  document.body.classList.add("sidebar-collapsed");
// Two buttons do this, one in the drawer's head and one at the top of the rail
// (ADR-0084): with the top strip gone, the folded drawer has to carry its own
// way back open.
function toggleSidebar() {
  // Below the floor the rail is already a strip of icons and there is nothing
  // left to collapse; the same button summons it over the transcript instead.
  if (isNarrow()) { toggleRail(railSection || "side-vault"); return; }
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
  sidebarYielded = false; // an explicit choice outranks the drawer's auto-yield
  syncNarrow();
}
for (const b of document.querySelectorAll(".side-toggle")) b.addEventListener("click", toggleSidebar);

// --- the 1120 floor: the deck folds, it does not reflow ----------------------
// Below the width that holds rail + reading measure + right sidebar side by
// side, the rail becomes five icons summoned over the transcript. It does not
// disappear: what a fold must never do is take a surface away and leave nothing
// where it was. (The work panel folded here too, until it stopped being a panel
// — as a mode of the sidebar it folds the way the sidebar does, which is why
// work.js no longer asks this question and the export it asked through is gone.)
//
// The threshold itself lives in ONE place, the media query in app-node.css that sets
// --narrow, read back through here rather than carried as a pixel count as well,
// because two constants that must match are two constants that eventually do not.
function isNarrow() {
  return getComputedStyle(document.body).getPropertyValue("--narrow").trim() === "1";
}

let railSection = null; // which compartment the summoned rail is showing

function toggleRail(sec) {
  const open = railSection !== sec;
  railSection = open ? sec : null;
  document.body.classList.toggle("rail-open", open);
  for (const b of document.querySelectorAll("#railmini .rm"))
    b.classList.toggle("on", open && b.dataset.sec === sec);
  if (!open) return;
  // The compartment you asked for is the one that opens; the others keep the
  // state you left them in, so summoning Files twice does not re-collapse the
  // tree you just expanded.
  const el = document.getElementById(sec);
  if (el) { el.open = true; el.scrollIntoView({ block: "nearest" }); }
}

function closeRail() {
  if (!railSection) return;
  railSection = null;
  document.body.classList.remove("rail-open");
  for (const b of document.querySelectorAll("#railmini .rm")) b.classList.remove("on");
}

$("#railmini").addEventListener("click", (e) => {
  const b = e.target.closest(".rm");
  if (!b) return;
  if (b.dataset.tab) { closeRail(); showTab(b.dataset.tab); return; } // a place picked, the summoned drawer goes
  if (!b.dataset.sec) return; // the toggle, New chat and the vault have their own handlers
  // Above the floor the rail is the drawer collapsed by choice: a compartment
  // asked for from it opens the drawer on that compartment, where below the
  // floor it is summoned over the transcript.
  if (!isNarrow()) {
    toggleSidebar();
    const el = document.getElementById(b.dataset.sec);
    if (el) el.open = true;
    return;
  }
  toggleRail(b.dataset.sec);
});
// The summoned rail sits over the transcript, so a click in the transcript is
// a dismissal. The rail itself and the strip that summoned it are not.
document.addEventListener("click", (e) => {
  if (!railSection) return;
  if (e.target.closest("#sidebar, #railmini")) return;
  closeRail();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRail(); });

// No compartment hides any more (audit P17: each has a resting state), so this
// only keeps an icon honest about a section that is missing altogether.
function syncRailIcons() {
  for (const b of document.querySelectorAll("#railmini .rm[data-sec]")) {
    const sec = document.getElementById(b.dataset.sec);
    b.hidden = !sec || sec.hidden;
  }
}
new MutationObserver(syncRailIcons).observe($("#sidebar"),
  { attributes: true, attributeFilter: ["hidden"], subtree: true });

function syncNarrow() {
  const narrow = isNarrow();
  $("#railmini").hidden = !narrow && !document.body.classList.contains("sidebar-collapsed");
  if (!narrow) closeRail();
  syncRailIcons();
}
syncNarrow();

// --- sequence guards (audit P11) ---------------------------------------------
// Every list loader is called from several places, and a vault switch calls
// all of them at once: a slow response from the PREVIOUS vault used to paint
// over the fast one from the new. Each loader takes a ticket and an epoch
// before its await and paints only if both still hold after it.
const loadSeq = { vault: 0, sessions: 0, changes: 0, messages: 0 };
let vaultEpoch = 0;
function ticket(name) {
  const my = ++loadSeq[name], epoch = vaultEpoch;
  return () => my === loadSeq[name] && epoch === vaultEpoch;
}

// Vault stats + file tree, from /vault_info. Best-effort: on error the placeholders stay.
async function loadVaultInfo() {
  const still = ticket("vault");
  try {
    const r = await fetch("/vault_info");
    const data = await r.json();
    if (!still()) return;
    if (data.error) return;
    if (data.path) setVaultPath(data.path); // follows a /vault switch
    $("#tree").innerHTML = data.tree || "";
    if (!$("#tree .tree-note")) {
      // An empty vault has one next step (ADR-0098 point 1), and on the server
      // the + is the only door besides a drop: the line's words open its menu.
      // Not `!data.tree`: an empty vault's tree is still its <div id=file-tree>,
      // which is also why `#tree:empty` never matched.
      const p = document.createElement("p");
      p.className = "vault-empty";
      p.innerHTML = 'Nothing here yet: <button type="button" class="linklike">add files or a folder</button>, or drop them anywhere.';
      p.querySelector("button").addEventListener("click", (e) => { e.stopPropagation(); $("#vault-add").click(); });
      $("#tree").append(p);
    }
    renderVaultHealth(data);          // the chip's dot, the panel's link count, the compartment's size
    syncTreePins();          // a fresh tree knows nothing about the pins yet
    markOpenNote();          // …nor about the note the drawer is on
    renderVaultFacts(data);           // the chat landing's counted line
    buildNoteIndex();                 // explore note search reads the fresh tree
    applySidebarFilter();
    applyHashNote();                  // a ?note= deep link can open now that the tree knows it
  } catch { notify("couldn't refresh vault stats"); }
}

// --- the vault chip: identity and health, stated once (audit P16) ------------
// The strip used to carry the vault's size beside every conversation: three
// numbers that never change during a session, and two amber ones the reader
// learned to ignore. The chip carries the name, a dot for whether anything
// needs answering, and the count of notices; the panel behind it holds the
// notices and the broken-link count with its way to metrics. The size lives
// on the landing and in metrics, which is where a size is read.

// The folder's NAME in the drawer's foot, its path on hover and in full at the
// head of the vault's panel, which is where "which of my two vaults is this
// window" is answered while reading something else.
function setVaultPath(path) {
  const el = $("#top-vname");
  const was = el.title;
  const name = String(path || "").replace(/\/+$/, "").split("/").pop();
  el.textContent = name || "";
  el.title = path || "";
  const full = $("#vp-path");
  full.textContent = path ? "\u200e" + path + "\u200e" : "";
  full.title = path || "";
  $("#vault-chip").hidden = !path;
  if (path && path !== was) loadPins(); // pins are per vault, and this is the switch
  if (path && path !== was) { landingPresence?.seed(path); composerPresence?.seed(path); } // and so is the crystal, at both its sites
}

// A cluster count made entirely of singletons names nothing, so it is stated
// only when there are areas worth naming. One function, because the strip and
// the chat landing both state this number and two copies of the rule are two
// numbers that can disagree about the same vault.
function areaCount(data) {
  return (data.topics || []).length ? data.clusters : 0;
}

let vaultBroken = 0;
function renderVaultHealth(data) {
  vaultBroken = data.unresolved || 0;
  const broken = $("#top-broken");
  broken.hidden = !vaultBroken;
  broken.textContent = nfmt(vaultBroken) + " unresolved wikilink" + (vaultBroken === 1 ? "" : "s")
    + " · targets that do not exist yet · open metrics";
  $("#vault-links").hidden = !vaultBroken;
  // the compartment's own count, which is the rail's reading of the size;
  // the folder count rides the hover, the summary row is 264px wide
  const folders = $("#tree").querySelectorAll("details").length;
  const count = $("#vault-count");
  count.textContent = data.notes ? nfmt(data.notes) + (data.notes === 1 ? " note" : " notes") : "";
  count.dataset.tip = folders ? `${nfmt(data.notes || 0)} notes in ${nfmt(folders)} folder${folders === 1 ? "" : "s"}` : "";
  syncVaultDot();
}

// The dot is amber while anything needs answering: a notice, or links that
// point nowhere. Both surfaces set their half and this derives the colour.
function syncVaultDot() {
  const warn = vaultBroken > 0 || noticeCount > 0;
  const label = "vault " + ($("#top-vname").textContent || "") + (warn ? ", needs attention" : ", healthy");
  // The drawer's foot and the rail's foot are one vault said twice, and the
  // panel colours its count by the same state.
  for (const el of [$("#vault-chip"), $("#rail-vault"), $("#vault-panel")]) el.dataset.health = warn ? "warn" : "ok";
  $("#vault-chip").setAttribute("aria-label", label);
  $("#rail-vault").setAttribute("aria-label", label);
}

// A number you cannot act on from where it is stated is a number you read once.
$("#top-broken").addEventListener("click", () => {
  closeVaultPanel();
  document.querySelector('.tab[data-tab="metrics"]').click();
});

// The panel names the vault; its one action on it is to change it, which is a
// setting, so it opens settings on that row rather than growing a picker.
$("#vault-switch").addEventListener("click", async () => {
  closeVaultPanel(false);
  await openSettings();
  stSearch.value = "vault";
  stFilter();
});

// --- the explore tabs ---------------------------------------------------------
// The ONE place the surfaces are named. They were a row of tabs in the graph
// toolbar, then a list in the left rail, then a closed Layout fold in a legend
// against the picture; a reader learned the surfaces existed by opening it. They
// are the view's primary navigation, so they are tabs at the top of the pane
// (ADR-0058). The list is data rather than markup because there is no second
// DOM to read it back off.
//
// [mode, label, key, group]. The key is the surface's one line under the tabs,
// what the marks in front of you mean (SURFACE_KEYS holds it per mode; the third
// field is the tab's tooltip). The group splits the row by space, not by
// labels: pictures of the links, readings of the shape, then the library.
const LAYOUT_MODES = [
  ["graph", "Graph", "wikilink structure + semantic k-NN overlay; the panel at the right is its key", 0],
  // The only surface that is not undirected: the others lay notes out by how
  // they connect, where they are filed, or how two groups couple, and none of
  // them can answer "what do I read BEFORE this".
  ["path", "Path", "reading order around one note: what RefD says comes before it, and what it unlocks", 0],
  // Three surfaces over the same graph that are NOT link-space: the graph
  // lays notes out by how they connect, so it cannot show where a note
  // SITS, how two areas couple as a whole, or what order the vault could be
  // read in.
  ["folders", "Folders", "the vault as folders, shaded by how much each folder mixes areas", 1],
  // One surface for how areas couple (ADR-0064): the drawing says WHICH links
  // join one area to the others, the matrix under its index HOW MANY join
  // every pair. They were two tabs, Areas and Bridges, and a reader crossed
  // between them on every cell they opened.
  ["areas", "Areas", "one area and the wikilinks to its neighbours, over the coupling of every pair", 1],
  ["read", "Read", "a reading order derived from hubs and their links", 1],
];

function buildSurfaceTabs() {
  const box = $("#surface-tabs");
  box.innerHTML = "";
  let group = null;
  for (const [mode, label, why, g] of LAYOUT_MODES) {
    const b = mkEl("button", "stab" + (group !== null && g !== group ? " stab-gap" : ""), label);
    group = g;
    b.type = "button";
    b.dataset.gmode = mode;
    b.title = why;
    b.addEventListener("click", () => setGraphMode(mode));
    box.appendChild(b);
  }
  syncSurfaceTabs();
}

function syncSurfaceTabs() {
  document.querySelectorAll("#surface-tabs .stab").forEach((b) => {
    setActive(b, b.dataset.gmode === graphMode);
    b.setAttribute("aria-current", b.dataset.gmode === graphMode ? "page" : "false");
  });
}

// --- pinned notes ------------------------------------------------------------
// The one thing in the rail the vault cannot derive. Files is alphabetical,
// History is chronological, Areas is computed: none of them can say "this one
// matters". Stored per vault, because a pin is a statement about the folder you
// are reading and switching vaults must not carry one vault's pins into another.
let pinnedPaths = [];

function pinKey() { return "pinned:" + ($("#top-vname").title || ""); }

function loadPins() {
  try {
    const raw = JSON.parse(localStorage.getItem(pinKey()) || "[]");
    pinnedPaths = Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : [];
  } catch { pinnedPaths = []; } // a hand-edited key is not an error to report
  renderPins();
}

function savePins() {
  // Quota is the one failure worth saying out loud: silently, the rail forgets
  // a pin the moment the tab closes and the user never learns why.
  try { localStorage.setItem(pinKey(), JSON.stringify(pinnedPaths)); }
  catch { notify("couldn't save that pin"); }
  renderPins();
}

function togglePin(path) {
  if (!path) return;
  const i = pinnedPaths.indexOf(path);
  if (i >= 0) pinnedPaths.splice(i, 1);
  else pinnedPaths.unshift(path); // newest first: the pin you just made is visible
  savePins();
}

function syncPinButton() {
  const btn = $("#note-pin");
  const on = !!lastViewedPath && pinnedPaths.includes(lastViewedPath);
  btn.setAttribute("aria-pressed", String(on));
  btn.classList.toggle("on", on);
  // A glyph alone, so the tip carries the state; the accessible name stays
  // put, since aria-pressed already says whether it is on.
  btn.dataset.tip = on ? "pinned: unpin this note" : "keep this note in the rail";
}

// The same pin, stated on the row it came from. Pinned is a state of the NOTE,
// so the tree has to show it: a toggle that only ever looks unpressed is a
// button the reader has to click twice to learn what it did.
function syncTreePins() {
  const on = new Set(pinnedPaths);
  for (const b of $("#tree").querySelectorAll(".tree-pin")) {
    const lit = on.has(b.dataset.pin);
    b.setAttribute("aria-pressed", String(lit));
    b.classList.toggle("on", lit);
    b.title = lit ? "unpin this note" : "keep this note in the rail";
  }
}

function renderPins() {
  const box = $("#pinned");
  box.innerHTML = "";
  for (const path of pinnedPaths) {
    const row = mkEl("div", "pin-row");
    row.dataset.path = path;
    row.title = path;
    row.appendChild(mkEl("span", "sw"));
    row.appendChild(mkEl("span", "t", path.replace(/\.md$/, "").split("/").pop()));
    const x = mkEl("button", "pin-x", "✕");
    x.type = "button";
    x.setAttribute("aria-label", "unpin " + path);
    row.appendChild(x);
    box.appendChild(row);
  }
  $("#pinned-group").hidden = !pinnedPaths.length;
  syncPinButton();
  syncTreePins();
  applySidebarFilter();
}

$("#pinned").addEventListener("click", (e) => {
  const row = e.target.closest(".pin-row");
  if (!row) return;
  if (e.target.closest(".pin-x")) { togglePin(row.dataset.path); return; }
  openNote(row.dataset.path);
});
$("#note-pin").addEventListener("click", () => togglePin(lastViewedPath));

// --- the chat landing --------------------------------------------------------
// Two lines about the folder you opened, over the wordmark. The counted one is
// this function and costs nothing; the written one is loadVaultBrief below and
// costs a call, so it is the one that can be switched off. When it is off the
// prose slot falls back to naming the topics — the landing must never describe
// a vault by its size alone, which is the one thing size cannot say.
let vaultTopics = [];

function renderVaultFacts(data) {
  vaultTopics = (data.topics || []).map((t) => t.label).filter(Boolean);
  const bits = [];
  if (data.notes) bits.push(nfmt(data.notes) + (data.notes === 1 ? " note" : " notes"));
  if (data.links) bits.push(nfmt(data.links) + " links");
  const areas = areaCount(data);
  if (areas) bits.push(nfmt(areas) + " areas");
  $("#vh-facts").textContent = bits.join("  ·  ");
  if (!$("#vh-brief").dataset.written) renderTopicLine();
}

// The fallback sentence. The labels are the vault's own words and carry the
// content colour; everything around them is chrome, which is what keeps a
// label like "mystery · sophia" readable inside a sentence that also joins
// with punctuation.
function renderTopicLine() {
  const el = $("#vh-brief");
  el.replaceChildren();
  const t = vaultTopics.slice(0, 3);
  if (!t.length) return;
  el.appendChild(document.createTextNode("Densest around "));
  t.forEach((label, i) => {
    if (i) el.appendChild(document.createTextNode(i === t.length - 1 ? " and " : ", "));
    el.appendChild(mkEl("b", "", label));
  });
  el.appendChild(document.createTextNode("."));
}

async function loadVaultBrief() {
  // The presence thinks only if the answer is slow enough to be the worker
  // writing: a cached sentence came back in 0.25-0.35 s on the 728-note test
  // vault (2026-09-22), a fresh one is a model call measured in seconds.
  const thinking = setTimeout(() => landingPresence?.state("thinking"), 700);
  let wrote = false;
  try {
    const d = await (await fetch("/vault_brief")).json();
    if (!d.enabled || !d.text) return; // off, or the worker had nothing to say
    const el = $("#vh-brief");
    el.dataset.written = "1";
    el.textContent = d.text;
    wrote = !d.cached;
  } catch { /* the counted line already answered the question */ } finally {
    // A failure is never an error on the landing (server.py vault_brief), so
    // the presence goes back to rest rather than into its error state.
    clearTimeout(thinking);
    landingPresence?.state(wrote ? "success" : "idle");
  }
}

// A tree click opens the note drawer, which also mirrors the focus into the
// graph iframe (focusGraphNode), where it is arranged as the Map (ADR-0088).
$("#tree").addEventListener("click", (e) => {
  // The pin sits inside the row, so it is tested first: otherwise the row's own
  // click wins and pinning a note would also open it, which is the opposite of
  // what a pin is for (naming it without going there).
  const pin = e.target.closest(".tree-pin");
  if (pin) { togglePin(pin.dataset.pin); return; }
  // inside the folder's summary: the details must not toggle under it
  const plan = e.target.closest(".tree-plan");
  if (plan) { e.preventDefault(); learnPlanFolder(plan.dataset.folder); return; }
  const leaf = e.target.closest(".tree-note");
  if (!leaf) return;
  // Rows are links (audit P7): a plain left click is handled here, and a
  // middle click or a modifier click is left to the browser, which opens the
  // note in a new tab through the same URL.
  if (!plainClick(e)) return;
  e.preventDefault();
  const path = leaf.dataset.id;
  openNote(path);
});

// A left click with no modifier is the app's to handle; anything else is the
// browser's (new tab, new window, save link).
function plainClick(e) {
  return e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey;
}

// The note open in the drawer is marked in the tree with the same inset the
// active tab and the current chat carry: cyan says "you are here".
function markOpenNote() {
  const open = notePanel.classList.contains("open") ? (lastNotePath || "") : "";
  for (const el of $("#tree").querySelectorAll(".tree-note"))
    el.classList.toggle("open", !!open && el.dataset.id === open);
}

// --- changes (what this session did to the vault) ----------------------------
// The list is the server's, not the transcript's: it survives a reload, folds
// five writes to one note into one row, and empties a row when /undo puts the
// bytes back. A row opens the drawer on the diff, which is the only place in the
// app where you can read what actually changed rather than what was claimed.
const changedPaths = new Set();
const KIND_MARK = { created: "+", deleted: "−", moved: "→", modified: "±" };

// A turn-end refresh is enough for a one-note write, and wrong for a run: an
// injector writes for minutes before its tool returns, so the notes were on disk
// — Obsidian showing them — while this list stayed empty and read as broken.
// Every phase bumps it, throttled: a run emits one every few hundred ms, and
// /changes re-reads each tracked note off disk. The trailing edge is covered by
// the turn-end call, so a leading-edge throttle drops nothing.
let lastBump = 0;
function bumpChanges() {
  const now = performance.now();
  if (now - lastBump < 2000) return;
  lastBump = now;
  loadChanges();
}

// path → the judge's sentence for a write under review (audit P4), read off
// the note's own `review:` frontmatter by /changes. Empty when the gate was
// sure. The write cards read it to draw the one amber border in the app.
const reviewOf = new Map();
let changeRows = []; // the last /changes payload, for cards drawn after it landed

async function loadChanges() {
  const still = ticket("changes");
  let rows = [];
  try {
    rows = await (await fetch("/changes")).json();
  } catch { return; } // ambient, not an errand: a failed poll says nothing
  if (!still()) return;
  changeRows = rows;
  changedPaths.clear();
  reviewOf.clear();
  const box = $("#changes");
  box.innerHTML = "";
  for (const r of rows) {
    changedPaths.add(r.path);
    if (r.review) reviewOf.set(r.path, r.review);
    const row = mkEl("div", "chg-row " + r.kind + (r.review ? " review" : ""));
    row.dataset.path = r.path;
    row.title = r.from ? `${r.from} → ${r.path}` : r.path;
    row.appendChild(mkEl("span", "chg-mark", KIND_MARK[r.kind] || "±"));
    row.appendChild(mkEl("span", "chg-name", r.name));
    const tally = mkEl("span", "chg-tally");
    if (r.review) tally.appendChild(mkEl("span", "chg-review", "to review"));
    if (r.added) tally.appendChild(mkEl("span", "chg-add", "+" + r.added));
    if (r.removed) tally.appendChild(mkEl("span", "chg-del", "−" + r.removed));
    if (!r.added && !r.removed && !r.review) tally.appendChild(mkEl("span", "chg-quiet", r.kind));
    row.appendChild(tally);
    box.appendChild(row);
  }
  // The compartment's resting state says what to expect, and its filled state
  // counts what needs an eye: "2 changed · 1 to review" is the sentence the
  // reader opens this compartment for.
  const toReview = rows.filter((r) => r.review).length;
  const head = $("#chat-head");
  head.textContent = rows.length
    ? `${rows.length} changed` + (toReview ? ` · ${toReview} to review` : "")
    : "nothing changed yet";
  head.classList.toggle("review", toReview > 0);
  // The folded rail's one state: an amber bar on the first icon while a write
  // waits for a verdict, and a label that says so rather than a title.
  const rm = document.querySelector('#railmini .rm[data-sec="side-chat"]');
  rm.classList.toggle("review", toReview > 0);
  rm.setAttribute("aria-label", toReview ? `this chat · ${toReview} to review` : "this chat");
  stampWriteTallies(rows);
  stampReviewCards();
  applySidebarFilter();
  syncDrawerMode(); // the menu's diff may have just become available for the open note
  refreshOpenNote(); // a turn or an undo may have rewritten the note in the drawer
}

// Every write card in the transcript, including ones a page reload replayed,
// takes its tally from the payload above rather than counting anything itself.
function stampWriteTallies(rows) {
  // The two halves are keyed differently and have to be made to meet. A card is
  // stamped with the tool's own argument — silica_patch_note(name:"Photosynthesis")
  // — while /changes reports the path the tool resolved that to,
  // "Biology/Photosynthesis.md". Exact path first, then the same path without
  // .md, then the bare basename but only when it is unambiguous across the
  // payload. Without this the tally never filled and every card click fell
  // through openDiff's baseline lookup to openNote, so the diff the card exists
  // to open was never the thing that opened.
  const by = new Map(), byBase = new Map();
  for (const r of rows) {
    const stem = r.path.replace(/\.md$/, "");
    by.set(r.path, r);
    by.set(stem, r);
    const base = stem.split("/").pop();
    byBase.set(base, byBase.has(base) ? null : r); // null = ambiguous, never used
  }
  for (const el of document.querySelectorAll(".wc-tally")) {
    const key = el.dataset.for;
    const r = by.get(key) || byBase.get(key) || null;
    el.textContent = "";
    if (!r) continue;
    // Re-point the card at the resolved path so its click opens the diff.
    const open = el.parentElement && el.parentElement.querySelector(".wc-open");
    if (open) open.dataset.path = r.path;
    if (r.added) el.appendChild(mkEl("span", "chg-add", "+" + r.added));
    if (r.removed) el.appendChild(mkEl("span", "chg-del", "−" + r.removed));
  }
}

$("#changes").addEventListener("click", (e) => {
  const row = e.target.closest(".chg-row");
  if (row) openDiff(row.dataset.path);
});

// One box, no scope: the rail's three compartments ARE the scope, so one
// typed string narrows every list at once and the status line counts per kind.
// Two chips (notes, chats) used to pick one list; a note and a chat sharing a
// substring both show, each under its own title, which is the disambiguation.
function applySidebarFilter() {
  const q = $("#side-search").value.trim().toLowerCase();
  // notes: substring on name or full path
  $("#tree").querySelectorAll(".tree-note").forEach((el) => {
    const off = !!q && !el.textContent.toLowerCase().includes(q) &&
                !(el.dataset.id || "").toLowerCase().includes(q);
    el.hidden = off;
    // The pin rides in a wrapper, so hiding the label alone would leave a bare
    // pin on an empty line. Both, because every count below still asks the
    // label whether it is hidden.
    const row = el.closest(".tree-row");
    if (row) row.hidden = off;
  });
  // folders: hide if nothing visible remains inside; reveal matches while searching
  $("#tree").querySelectorAll("details").forEach((d) => {
    const any = Array.from(d.querySelectorAll(".tree-note")).some((n) => !n.hidden);
    d.hidden = !!q && !any;
    if (q && any) d.open = true;
  });
  // pinned notes: same substring rule again, so a filtered rail is filtered
  // everywhere and not just below the fold
  $("#pinned").querySelectorAll(".pin-row").forEach((el) => {
    el.hidden = !!q && !el.textContent.toLowerCase().includes(q) &&
                !(el.dataset.path || "").toLowerCase().includes(q);
  });
  // changed notes: same substring rule as the tree, on the same names
  $("#changes").querySelectorAll(".chg-row").forEach((el) => {
    el.hidden = !!q && !el.textContent.toLowerCase().includes(q) &&
                !(el.dataset.path || "").toLowerCase().includes(q);
  });
  // sessions: substring on the TITLE span, not the row: the row also carries
  // "2h ago" and the "…" menu, so "ago" would light every chat. While
  // searching, the expand cap is lifted.
  $("#sessions").querySelectorAll(".session").forEach((el) => {
    const title = (el.querySelector(".t") || el).textContent.toLowerCase();
    el.hidden = (!!q && !title.includes(q)) ||
                (!q && !sessionsExpanded && +el.dataset.idx >= SESSION_CAP);
  });
  // a bucket with nothing left under it is a label over nothing
  $("#sessions").querySelectorAll(".side-bucket").forEach((b) => {
    let n = b.nextElementSibling, any = false;
    while (n && !n.classList.contains("side-bucket")) { if (!n.hidden) any = true; n = n.nextElementSibling; }
    b.hidden = !any;
  });
  $("#sessions-more").hidden = !!q || sessionsExpanded || sessionCount <= SESSION_CAP;

  // Say what the filter did. It used to empty the list in silence, so a real
  // zero and a typo rendered pixel-identical and the only way to learn which was
  // to clear the field. It also only ever matched names, never note bodies, and
  // the placeholder never said so, hence the offer to ask the vault instead,
  // which stands beside the count and not only behind an empty result.
  const box = $("#side-search-status");
  box.hidden = !q;
  if (!q) return;
  box.innerHTML = "";
  const typed = $("#side-search").value.trim();
  const notes = $("#tree").querySelectorAll(".tree-note:not([hidden])").length;
  const chats = $("#sessions").querySelectorAll(".session:not([hidden])").length;
  const n = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
  const parts = [];
  if (notes) parts.push(n(notes, "note"));
  if (chats) parts.push(n(chats, "chat"));
  box.appendChild(document.createTextNode(
    parts.length ? `${parts.join(" · ")} match · ` : `nothing is named "${typed}" · `));
  box.classList.toggle("empty", !parts.length);
  const ask = document.createElement("button");
  ask.type = "button";
  ask.className = "linklike";
  ask.textContent = "search inside the notes";
  ask.addEventListener("click", () => {
    input.value = "/find " + typed;
    input.focus();
    autoGrow(input);
  });
  box.appendChild(ask);
}
$("#side-search").addEventListener("input", applySidebarFilter);

// A button inside a <summary> (collapse, + new) would also toggle the whole
// compartment. Cancelling on the summary itself is what reliably suppresses
// that: preventDefault from the button's own listener is order-dependent and
// stopPropagation does not consistently reach summary's activation behaviour.
for (const s of document.querySelectorAll("#sidebar details.side-section > summary")) {
  s.addEventListener("click", (e) => { if (e.target.closest("button")) e.preventDefault(); });
}

$("#tree-collapse").addEventListener("click", () => {
  for (const d of $("#tree").querySelectorAll("details[open]")) d.open = false;
});

// --- chats (audit P18): by day, newest first, the current one marked --------
const SESSION_CAP = 8;
let sessionsExpanded = false;
let sessionCount = 0;
let currentSessionId = "";
let currentTitle = "";

$("#sessions-more").addEventListener("click", () => {
  sessionsExpanded = true;
  applySidebarFilter();
});

// today / yesterday / earlier, by the calendar day of the machine the vault is
// on, which is also the machine the person is on.
function dayBucket(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  return diff <= 0 ? "today" : diff === 1 ? "yesterday" : "earlier";
}
// Relative while it is short, a date once it is not; the absolute time rides
// the title for the hover.
function relTime(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// The document title follows the open chat, so a tab strip of three silica
// windows says which is which.
function setDocumentTitle() {
  document.title = currentTitle ? currentTitle + " · silica" : "silica";
}

async function loadSessions() {
  const still = ticket("sessions");
  try {
    const r = await fetch("/sessions");
    const current = r.headers.get("X-Silica-Session") || "";
    const sessions = await r.json();
    if (!still()) return;
    const box = $("#sessions");
    box.innerHTML = "";
    sessionCount = sessions.length;
    currentSessionId = current;
    currentTitle = "";
    restoreDraft(current); // the composer's draft is per chat
    $("#sessions-count").textContent = sessionCount || "";
    let bucket = "";
    sessions.forEach((s, i) => {
      const b = dayBucket(s.updated || 0);
      if (b !== bucket) { bucket = b; box.appendChild(mkEl("div", "side-bucket", b)); }
      const el = document.createElement("a");
      el.className = "session" + (s.id === current ? " active" : "");
      el.href = "#chat/" + encodeURIComponent(s.id);
      el.dataset.idx = i;
      el.dataset.id = s.id;
      if (s.id === current) { el.setAttribute("aria-current", "page"); currentTitle = s.title || ""; }
      if (s.live) el.appendChild(mkEl("span", "s-dot", ""));
      // Title ladder: the first user line (the server's title), which for a
      // slash command IS the command, then "untitled".
      el.appendChild(mkEl("span", "t", s.title || "untitled"));
      const when = mkEl("span", "m", relTime(s.updated || 0));
      when.title = s.updated ? new Date(s.updated * 1000).toLocaleString() : "";
      el.appendChild(when);
      const more = mkEl("button", "s-more", "…");
      more.type = "button";
      more.setAttribute("aria-label", "more actions for this chat");
      more.setAttribute("aria-haspopup", "menu");
      el.appendChild(more);
      box.appendChild(el);
    });
    setDocumentTitle();
    $("#sessions-more").textContent = "show all " + sessionCount;
    applySidebarFilter();
    applyHashSession(); // a #chat/<id> deep link names a chat that may not be the current one
    // The boot wrote "#chat" before the id was known; stamp the id in place so
    // Back from the next chat has somewhere to return to.
    if (activeTab === "chat" && currentSessionId) syncUrl(false);
  } catch { notify("couldn't load chat history"); }
}

// Rows are links: a plain click opens the chat here, the rest is the
// browser's. The "…" opens the row's menu; the rename field and the armed
// delete are the two things in it that take a second gesture.
$("#sessions").addEventListener("click", (e) => {
  const row = e.target.closest(".session");
  if (!row) return;
  if (e.target.closest(".s-more")) { e.preventDefault(); openSessionMenu(row); return; }
  if (e.target.closest(".s-menu, .s-rename")) { e.preventDefault(); return; }
  if (!plainClick(e)) return;
  e.preventDefault();
  openSession(row.dataset.id);
});

function closeSessionMenus() {
  for (const m of document.querySelectorAll(".s-menu")) m.remove();
  for (const r of document.querySelectorAll(".session.menu-open")) r.classList.remove("menu-open");
}
document.addEventListener("click", (e) => { if (!e.target.closest(".s-menu, .s-more")) closeSessionMenus(); });

function openSessionMenu(row) {
  const was = row.querySelector(".s-menu");
  closeSessionMenus();
  if (was) return;
  const id = row.dataset.id;
  const menu = mkEl("div", "s-menu");
  menu.setAttribute("role", "menu");
  const item = (label, fn, cls) => {
    const b = mkEl("button", "s-item" + (cls ? " " + cls : ""), label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fn(b); });
    menu.appendChild(b);
    return b;
  };
  item("rename", () => renameSessionInline(row));
  item("open in a new tab", () => { window.open("#chat/" + encodeURIComponent(id), "_blank"); closeSessionMenus(); });
  // Delete asks first and names the consequence: the second click is the
  // decision, and doing nothing disarms it (the same arm the bulk revert has).
  const del = item("delete", () => {}, "danger");
  armThenRun(del, "delete", "click again · this chat and its record are gone", async () => {
    try {
      const r = await fetch("/session/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
      if (!r.ok) { notify(r.status === 409 ? "that chat is running; stop it first" : "couldn't delete that chat"); return; }
    } catch { notify("couldn't delete that chat"); return; }
    closeSessionMenus();
    if (id === currentSessionId) { log.innerHTML = ""; currentSessionId = ""; syncUrl(false); announceSession(); loadVault(); loadChanges(); }
    loadSessions();
  });
  row.classList.add("menu-open");
  row.appendChild(menu);
  menu.querySelector("button").focus();
}

function renameSessionInline(row) {
  closeSessionMenus();
  const t = row.querySelector(".t");
  const field = document.createElement("input");
  field.type = "text";
  field.className = "s-rename";
  field.value = t.textContent === "untitled" ? "" : t.textContent;
  field.setAttribute("aria-label", "new title for this chat");
  t.replaceWith(field);
  field.focus();
  field.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const title = field.value.trim();
    field.replaceWith(t);
    if (!save || !title || title === t.textContent) return;
    try {
      const r = await fetch("/session/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: row.dataset.id, title }) });
      if (!r.ok) { notify("couldn't rename that chat"); return; }
    } catch { notify("couldn't rename that chat"); return; }
    loadSessions();
  };
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  field.addEventListener("blur", () => finish(true));
}

// The work panel projects whichever narration session is current, and the two
// places that switch it are the only ones that know. A load replays no beats
// onto the BUS — read_beats() walks the file — so a live subscriber cannot see
// the switch happen and has to be told.
const announceSession = () => document.dispatchEvent(new CustomEvent("silica:session"));

// `fromUrl`: the hash already names this chat (Back, a pasted link, a reload),
// so the history entry is replaced rather than pushed; a click on a row pushes
// one, which is what makes Back return to the previous chat (audit P7).
async function openSession(id, fromUrl = false) {
  if (streaming) return;
  saveDraft();      // what was typed for THIS chat stays with it…
  input.value = ""; // …and the next chat opens on its own draft
  autoGrow(input);
  renderHighlight();
  try {
    const r = await fetch("/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    if (!r.ok) {
      // A stale id is a notice over a working page, never a blank one.
      notify(r.status === 404 ? "That chat wasn't found. It may have been deleted." : "couldn't load that chat");
      if (fromUrl) { currentSessionId = ""; syncUrl(false); }
      return;
    }
  } catch { notify("couldn't load that chat"); return; }
  currentSessionId = id;
  // The push happens BEFORE showTab: showTab replaces the hash from rendered
  // state, and a push after it would find the hash already written and
  // push nothing, so Back would leave the app instead of the chat.
  activeTab = "chat";
  syncUrl(!fromUrl);
  showTab("chat"); // surface the loaded chat
  announceSession();
  await loadVault();
  loadSessions();
  loadChanges();
}

// --- the URL is the state (audit P7) -----------------------------------------
// #chat/<session>, #explore/<mode>, #learn/… (learn owns its own), plus
// ?note=<path> while the drawer is open. Rendered state writes the hash; the
// hash, when it changes under us (Back, a paste, a new tab), drives the state.
// A view's address is its label, not its id: the ids stayed when Graph became
// Explore, Calendar Plan and Metrics Examine (2026-10-13), because renaming
// them would touch every element and test that names the view. The id still
// routes, so a bookmark made before a rename lands where it did.
const VIEW_SLUG = { graph: "explore", calendar: "plan", metrics: "examine" };
const viewOfSlug = (s) => Object.keys(VIEW_SLUG).find((v) => VIEW_SLUG[v] === s) || s;

function parseHash() {
  const raw = (location.hash || "").replace(/^#/, "");
  const [path, query] = raw.split("?");
  const parts = path.split("/");
  const params = new URLSearchParams(query || "");
  const arg = decodeURIComponent(parts[1] || "");
  // #explore/bridges was this surface's address until ADR-0064 took the name
  // Areas: a bookmark of it lands on what it became, not on the Graph.
  return { slug: parts[0] || "", arg: parts[0] === "explore" && arg === "bridges" ? "areas" : arg,
           note: params.get("note") || "" };
}

function syncUrl(push) {
  if (activeTab === "learn" && location.hash.startsWith("#learn")) return; // learn's own sub-states
  let h = "#" + (VIEW_SLUG[activeTab] || activeTab);
  if (activeTab === "chat" && currentSessionId) h += "/" + encodeURIComponent(currentSessionId);
  if (activeTab === "graph" && typeof graphMode === "string") h += "/" + graphMode;
  // #examine/<reading> (ADR-0072); Structure is the tab's own address.
  if (activeTab === "metrics" && typeof metricsTab === "string" && metricsTab !== "structure") h += "/" + metricsTab;
  if (notePanel.classList.contains("open") && lastNotePath) h += "?note=" + encodeURIComponent(lastNotePath);
  if (location.hash === h) return;
  if (push) history.pushState(null, "", h);
  else history.replaceState(null, "", h);
}

// The two halves of a deep link that need data before they can act: the chat
// list must know the current id, and the tree must know the note.
function applyHashSession() {
  const { slug, arg } = parseHash();
  if (slug === "chat" && arg && arg !== currentSessionId && !streaming) openSession(arg, true);
}
function applyHashNote() {
  const { note } = parseHash();
  if (note && note !== lastNotePath) openNote(note);
}

// --- tabs -------------------------------------------------------------------
// Rebuilding the graph (Louvain + cooccurrence labels) is not free — only do it
// when the vault might actually have changed (graphStale), not on every switch
// back into the tab. A turn that writes notes sets graphStale = true.
let graphStale = true;
// One vocabulary for "which of these is showing". `.active` paints it and this
// says it out loud, so a segmented control is not a state only a sighted user
// can read. `aria-pressed` and not `aria-selected`, because these are groups of
// buttons rather than an ARIA tablist with its roving tabindex — and because it
// is what the quick actions already carry, so the app keeps one convention
// instead of gaining a second.
function setActive(btn, on) {
  btn.classList.toggle("active", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
}

// The --sel box of a segment travels instead of blinking (app-motion.css
// .glide). It watches the buttons rather than being called, because the
// segments are switched from several places (syncSurfaceTabs, the calendar's
// mode, the hash router, the Zotero tab registering) and the next would forget
// the call. The header's .tabs left this list on 2026-10-07: its view is lit,
// not boxed (app-shell.css), so there is no pill to travel. A resize travels too, and has to: the active tab is
// set at weight 500, so selecting one resizes the strip in the same frame, and
// a resize that snapped cancelled every switch it was part of (seen live,
// 2026-10-06). Only a pill with nowhere to travel from lands without moving.
function glide(seg) {
  const place = () => {
    const on = seg.querySelector(":scope > .active");
    // Hidden before (no selection, or the segment out of view): growing out of
    // the corner reads as a glitch.
    const snap = !parseFloat(seg.style.getPropertyValue("--gw"));
    seg.classList.toggle("glide-snap", snap);
    const vis = on && on.offsetWidth;
    seg.style.setProperty("--gw", vis ? on.offsetWidth + "px" : "0px");
    seg.style.setProperty("--gh", vis ? on.offsetHeight + "px" : "0px");
    if (vis) {
      seg.style.setProperty("--gx", on.offsetLeft + "px");
      seg.style.setProperty("--gy", on.offsetTop + "px");
    }
    // Commit the snapped values before the transition comes back, or the
    // removal below is the style change that makes them travel after all.
    if (snap) { void getComputedStyle(seg, "::before").width; seg.classList.remove("glide-snap"); }
  };
  seg.classList.add("glide");
  place();
  // Only the buttons' classes: the segment's own class is glide-snap toggling.
  new MutationObserver((recs) => { if (recs.some((r) => r.target !== seg)) place(); })
    .observe(seg, { subtree: true, attributeFilter: ["class"] });
  new ResizeObserver(place).observe(seg);
}
for (const id of ["#surface-tabs", "#cal-mode", "#metrics-tabs"]) glide($(id));

// Switching tabs is a function, not only a click: a synthetic .click() bubbles
// to the document's outside-click handler, which closes the note drawer. Every
// caller that needs the drawer to survive the switch (the context drawer's
// concept cloud, its suggested rows) calls this instead.
function showTab(tab) {
  // The view arrives from the side of the strip it was picked toward
  // (app-motion.css .view.active), read off the strip so a reordered tab
  // cannot disagree with its own motion.
  const order = ["chat", ...[...document.querySelectorAll("#views .tab")].map((b) => b.dataset.tab)];
  const step = order.indexOf(tab) - order.indexOf(activeTab);
  $("main").style.setProperty("--tab-from", String(Math.sign(step)));
  activeTab = tab;
  if (tab === "chat") closePeek(); // stream visible → card redundant
  // ask-from-here lives on the graph, and on learn, where the peek
  // card mirrors a session the drawer sent to chat (learn-suite §5.3).
  $("#dock").hidden = !["graph", "learn"].includes(tab);
  // The views are navigation now (ADR-0084), so the one showing is the
  // current page rather than a pressed button; the rail's icons say it too.
  for (const b of document.querySelectorAll(".tab")) {
    b.classList.toggle("active", b.dataset.tab === tab);
    if (b.dataset.tab === tab) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  $("#view-chat").classList.toggle("active", tab === "chat");
  $("#view-graph").classList.toggle("active", tab === "graph");
  $("#view-learn").classList.toggle("active", tab === "learn");
  $("#view-calendar").classList.toggle("active", tab === "calendar");
  $("#view-metrics").classList.toggle("active", tab === "metrics");
  if (tab === "graph") setGraphMode(graphMode); // load the active mode's content
  if (tab === "learn") loadLearn();
  if (tab === "calendar") loadCalendar();
  if (tab === "metrics") loadMetrics();
  // Deep-linkable views: the hash names the tab, the chat, the surface and the
  // open note (audit P7). Learn keeps its own sub-states in the hash
  // (#learn/plan/…, #learn/session/…) so Back and Forward restore them
  // (learn-suite §5.4); syncUrl never flattens one of those to "#learn".
  syncUrl(false);
  // The node pane reads this: a selection on one view is not a subject on
  // another. It is a separate file and has no other way to know which view is up.
  document.dispatchEvent(new CustomEvent("silica:view", { detail: tab }));
}
$(".tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (tab) showTab(tab);
});
// #explore / #examine / #chat on the URL select the tab, at load and on manual
// hash edits alike. Unknown hashes are left alone (note anchors, etc.).
// A surface that registers after load (Library exists only for a vault linked
// to Zotero, ADR-0055) is not in LAYOUT_MODES when the route first runs, and the
// boot rewrites the hash before it registers: the asked-for mode waits here.
let pendingGraphMode = "";

function tabFromHash() {
  const { slug, arg, note } = parseHash();
  const tab = viewOfSlug(slug);
  if (["chat", "graph", "learn", "calendar", "metrics"].includes(tab) && tab !== activeTab) showTab(tab);
  // Back and Forward walk the chats and the surfaces, not only the tabs.
  if (tab === "chat" && arg && arg !== currentSessionId && !streaming) openSession(arg, true);
  if (tab === "graph" && arg && arg !== graphMode) {
    if (LAYOUT_MODES.some(([m]) => m === arg)) setGraphMode(arg);
    else pendingGraphMode = arg;
  }
  if (tab === "metrics" && typeof setMetricsTab === "function") setMetricsTab(arg || "structure", false);
  if (note && note !== lastNotePath) openNote(note);
  else if (!note && notePanel.classList.contains("open") && tab !== "learn") closeNote();
}
window.addEventListener("hashchange", tabFromHash);


// --- theme ------------------------------------------------------------------
// The palette itself is CSS, and the <head> script owns resolving "auto" — this
// is only the two things CSS cannot reach. The iframes get the resolved value
// on their URL rather than inheriting it, because they are separate documents
// with their own <head> script and no way to read this one's :root. Mermaid
// gets a repaint because a rendered diagram is baked SVG.
const liveTheme = () => document.documentElement.dataset.theme || "dark";

function applyThemePref(pref) {
  const root = document.documentElement;
  const paint = () => { root.dataset.themePref = pref; root.__silicaPaintTheme?.(); };
  // A crossfade of the whole document, graphite into paper. Only when the
  // preference actually moves: settings re-saves the same value, and while a
  // transition runs its overlay takes every click on the page, which is a
  // click lost for nothing.
  if (pref === root.dataset.themePref || !document.startViewTransition
      || matchMedia("(prefers-reduced-motion: reduce)").matches) paint();
  else document.startViewTransition(paint);
}

// One watcher for both ways the theme moves: the settings row writes the
// preference, and the OS moving under an "auto" preference fires the <head>
// script's own listener. Both land on data-theme, so that is the only thing
// worth watching — and watching the result rather than the causes is what keeps
// this from needing a second call site every time a new one appears.
new MutationObserver(() => {
  repaintMermaid();
  graphStale = true;
  if (activeTab === "graph") setGraphMode(graphMode);
}).observe(document.documentElement, { attributeFilter: ["data-theme"] });

