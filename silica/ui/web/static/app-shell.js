// --- unified sidebar (stats · search · files · history) ----------------------
if (localStorage.getItem("sidebar-collapsed") === "1")
  document.body.classList.add("sidebar-collapsed");
// Two buttons do this, one in the drawer's head and one at the top of the rail
// (ADR-0084): with the top strip gone, the folded drawer has to carry its own
// way back open.
function toggleSidebar() {
  // Below the floor the rail is already a strip of icons and there is nothing
  // left to collapse; the same button summons it over the transcript instead.
  if (isNarrow()) { toggleRail(railSection || sidePane); return; }
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

let railSection = null; // which list the summoned rail is showing

// Stamped on <html> by the server (index()), so the views a hosted account
// does not get are gone before the first paint rather than after a probe.
const HOSTED = document.documentElement.hasAttribute("data-hosted");

function toggleRail(sec) {
  const open = railSection !== sec;
  railSection = open ? sec : null;
  document.body.classList.toggle("rail-open", open);
  for (const b of document.querySelectorAll("#railmini .rm"))
    b.classList.toggle("on", open && b.dataset.sec === sec);
  if (!open) return;
  // The list you asked for is the one shown (ADR-0122); the tree keeps the
  // folders you left open, so summoning the notes twice does not re-collapse
  // what you just expanded.
  showSidePane(sec);
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
  // Above the floor the rail is the drawer collapsed by choice: a list asked
  // for from it opens the drawer on that list, where below the floor it is
  // summoned over the transcript.
  if (!isNarrow()) {
    toggleSidebar();
    showSidePane(b.dataset.sec);
    return;
  }
  toggleRail(b.dataset.sec);
});
// The summoned rail sits over the transcript, so a click in the transcript is
// a dismissal. The rail itself and the strip that summoned it are not.
document.addEventListener("click", (e) => {
  if (!railSection) return;
  // A target a click handler already re-rendered away (a workspace row that
  // opened) is no longer inside anything; it was inside the rail.
  if (!e.target.isConnected || e.target.closest("#sidebar, #railmini")) return;
  closeRail();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // The tree's selection goes first (ADR-0126 point 11), and only when nothing
  // else took the key: the composer's picker prevents its Escape, a menu holds
  // focus, a field is being typed in. The next Escape closes the summoned rail.
  if (treeSel.size && !e.defaultPrevented && !treeMenu
      && !e.target.closest("input, textarea, [contenteditable], .cm-editor")) {
    e.preventDefault();
    clearTreeSel();
    return;
  }
  closeRail();
});

// No rail icon hides with its list (ADR-0122): the list not in view is hidden
// by design, and its icon is the way back to it. syncRailIcons, which hid the
// icon of a hidden compartment, went with the compartments.
function syncNarrow() {
  const narrow = isNarrow();
  $("#railmini").hidden = !narrow && !document.body.classList.contains("sidebar-collapsed");
  if (!narrow) closeRail();
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
  // A rename or a new folder being typed in the tree would die with the
  // redraw (ADR-0126 point 8): it waits, and the field's close runs it.
  if (treeField) { treeRedrawDue = true; return; }
  treeRedrawDue = false;
  const still = ticket("vault");
  try {
    const r = await fetch("/vault_info");
    const data = await r.json();
    if (!still()) return;
    if (data.error) return;
    if (data.path) setVaultPath(data.path); // follows a /vault switch
    $("#tree").innerHTML = data.tree || "";
    applyTreeOpen();         // the folders the person opened or closed (ADR-0126 point 9)
    if (!$("#tree .tree-note")) {
      // An empty vault has one next step (ADR-0098 point 1), and on the server
      // the + is the only door besides a drop: the line's words open its menu.
      // Not `!data.tree`: an empty vault's tree is still its <div id=file-tree>,
      // which is also why `#tree:empty` never matched.
      const p = document.createElement("p");
      p.className = "vault-empty";
      p.innerHTML = 'Nothing here yet: <button type="button" class="linklike">add files, a folder or a link</button>, or drop them anywhere.';
      p.querySelector("button").addEventListener("click", (e) => { e.stopPropagation(); $("#vault-add").click(); });
      $("#tree").append(p);
    }
    loadWorkspaces();                 // the members follow the files (ADR-0112)
    loadTrash();                      // a delete or a restore moved notes across
    renderVaultHealth(data);          // the chip's dot, the panel's link count
    syncTreePins();          // a fresh tree knows nothing about the pins yet
    markOpenNote();          // …nor about the note the drawer is on
    renderLandingIdeas(data);         // the landing's ways to start, from the hubs
    buildNoteIndex();                 // explore note search reads the fresh tree
    applySidebarFilter();
    trimTreeSel();           // the selection, less what the fresh tree no longer shows
    applyHashNote();                  // a ?note= deep link can open now that the tree knows it
    document.dispatchEvent(new CustomEvent("silica:vault-info")); // the tour reads the fresh tree (ADR-0105)
  } catch { notify("couldn't refresh vault stats"); }
}

// --- the vault chip: identity and health, stated once (audit P16) ------------
// The strip used to carry the vault's size beside every conversation: three
// numbers that never change during a session, and two amber ones the reader
// learned to ignore. The chip carries the name, a dot for whether anything
// needs answering, and the count of notices; the panel behind it holds the
// notices and the broken-link count with its way to metrics. The size lives
// on the landing and in metrics, which is where a size is read.

// The folder's name, and its path in full, head the menu under SILICA (ADR-0122,
// amendment 2026-10-22), which is where "which of my two vaults is this window"
// is answered while reading something else.
function setVaultPath(path) {
  const el = $("#vp-name");
  const was = el.title;
  const name = String(path || "").replace(/\/+$/, "").split("/").pop();
  el.textContent = name || "";
  el.title = path || "";
  const full = $("#vp-path");
  full.textContent = path ? "\u200e" + path + "\u200e" : "";
  full.title = path || "";
  if (path && path !== was) loadPins(); // pins are per vault, and this is the switch
}

let vaultBroken = 0;
function renderVaultHealth(data) {
  // The count's one action is Examine, which a hosted account does not have
  // (ADR-0111 point 3): there it would be an amber dot nobody can answer.
  vaultBroken = HOSTED ? 0 : data.unresolved || 0;
  const broken = $("#top-broken");
  broken.hidden = !vaultBroken;
  broken.textContent = nfmt(vaultBroken) + " unresolved wikilink" + (vaultBroken === 1 ? "" : "s")
    + " · targets that do not exist yet · open Examine";
  $("#vault-links").hidden = !vaultBroken;
  // No size beside the list's name since ADR-0122, which has no title row to
  // carry one: the size is read on the landing and in Examine (P16).
  syncVaultDot();
}

// The dot is amber while anything needs answering: a notice, or links that
// point nowhere. Both surfaces set their half and this derives the colour.
function syncVaultDot() {
  const warn = vaultBroken > 0 || noticeCount > 0;
  const label = "vault " + ($("#vp-name").textContent || "") + (warn ? ", needs attention" : ", healthy");
  // The drawer's head and the rail's dot are one vault said twice, and the
  // panel colours its count by the same state.
  for (const el of [$("#vault-chip"), $("#rail-vault"), $("#vault-panel")]) el.dataset.health = warn ? "warn" : "ok";
  $("#vault-chip").setAttribute("aria-label", "Silica menu, " + label);
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

function pinKey() { return "pinned:" + ($("#vp-name").title || ""); }

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
  if (typeof syncNoteWs === "function") syncNoteWs(); // its neighbour states the same note
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
    row.draggable = true; // onto a workspace, a folder or the composer (ADR-0126 point 10)
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

// --- the chat landing (ADR-0048, amendment 2026-10-22) -----------------------
// The empty chat greets you and writes it the way a reply streams, the mark
// beside it on the run line's beat until the sentence is down; then both rest.
// The name is the one given in settings (SILICA_USER_NAME), which index()
// stamps on the page: the address an account signs in with is not a name, and
// a name guessed from one greets somebody else.
let greeted = ""; // the sentence on the landing, so a second call writes nothing
let greetTimer = 0;

function greet(again = false) {
  const who = (document.documentElement.dataset.name || "").trim();
  const vault = $("#vp-name").textContent.trim();
  // [words, whether they are the person's or the vault's own]
  const parts = who ? [["Hi ", false], [who, true], [". ", false]] : [];
  if (vault) parts.push(["What do you want to know about ", false], [vault, true], ["?", false]);
  else parts.push(["What do you want to know?", false]);
  const said = parts.map((p) => p[0]).join("");
  if (said === greeted && !again) return;
  greeted = said;
  $("#vh-said").textContent = said;
  const typed = $("#vh-typed"), mark = $("#vault-hero .vh-mark");
  const paint = (n) => {
    typed.replaceChildren();
    for (const [words, own] of parts) {
      if (n <= 0) break;
      const bit = words.slice(0, n);
      n -= words.length;
      typed.append(own ? mkEl("b", "", bit) : bit);
    }
  };
  clearInterval(greetTimer);
  // Written at once where nobody would watch it being written: under a
  // transcript, or for a reader who asked the page to keep still.
  if (log.childElementCount || matchMedia("(prefers-reduced-motion: reduce)").matches) {
    mark.classList.remove("rl-mark");
    paint(said.length);
    return;
  }
  const caret = mkEl("span", "caret", "\u258d");
  let n = 0;
  paint(0);
  typed.append(caret);
  mark.classList.add("rl-mark");
  greetTimer = setInterval(() => {
    paint(++n);
    if (n < said.length) typed.append(caret);
    else { clearInterval(greetTimer); mark.classList.remove("rl-mark"); }
  }, 32);
}

// Three ways to start, from the notes the vault links most (/vault_info's
// hubs), each through a command that already exists and filling the field the
// way the + menu does. Dated notes are left out: a daily note is a hub because
// every day links to it, and "quiz me on 2026-10-21" asks about nothing.
// /quiz is a shortcut, split with shlex, so a title with spaces is quoted; the
// two skills read their line as written.
const LANDING_IDEAS = [
  ["Explain ", "/explain", (link) => `/explain [[${link}]]`],
  ["Quiz me on ", "/quiz", (link) => `/quiz ${/\s/.test(link) ? `"${link}"` : link}`],
  ["Summarize ", "/summarize", (link) => `/summarize [[${link}]]`],
];

function renderLandingIdeas(data) {
  const box = $("#vh-ideas");
  box.replaceChildren();
  const hubs = (data.hubs || []).filter((h) => !/^\d{4}-\d{2}-\d{2}/.test(h.name));
  hubs.slice(0, LANDING_IDEAS.length).forEach((h, i) => {
    const [verb, cmd, line] = LANDING_IDEAS[i];
    const say = mkEl("span", "vh-say", verb);
    say.append(mkEl("b", "", h.name));
    const b = mkEl("button", "vh-idea");
    b.type = "button";
    b.append(say, mkEl("span", "vh-cmd", cmd));
    b.addEventListener("click", () => {
      input.value = line(mentionLink(h.path)) + " ";
      input.focus();
      input.dispatchEvent(new Event("input"));
    });
    box.append(b);
  });
}

// The sentence the worker writes about the vault left the landing with the
// greeting, and is still asked for: silica_vaults hands it to the agent when
// it picks among vaults (vault_registry._brief), and nothing else writes it.
function loadVaultBrief() {
  fetch("/vault_brief").catch(() => {}); // the agent describes a vault without it, from its name and coverage
}

// --- the tree's selection (ADR-0126 point 1) ---------------------------------
// A set of paths and not of rows: the tree is redrawn by innerHTML after every
// write, so rows die and paths live. Shift selects the range from the anchor
// over the rows the eye can see, cmd on macOS or ctrl elsewhere toggles one,
// and a plain click opens the note and clears the selection, as a click in
// Finder selects that one row and nothing else. The count lives in #tree-sel:
// aria-selected is not a link's to carry, and the page already keeps it to
// real ARIA widgets (setActive). Trimmed to the rows shown after a redraw, a
// filter change and a collapse, so a drag never carries a note no one sees.
const treeSel = new Set();
let treeAnchor = null;
const TREE_MOD = /Mac|iPhone|iPad/.test(navigator.platform) ? "metaKey" : "ctrlKey";
let treeField = null;      // the rename or new-folder field being typed, or null
let treeRedrawDue = false; // a loadVaultInfo that waited for the field

function visibleTreeRows() {
  return [...$("#tree").querySelectorAll(".tree-note")].filter((el) => el.offsetParent !== null);
}
function syncTreeSel() {
  for (const el of $("#tree").querySelectorAll(".tree-note")) el.classList.toggle("sel", treeSel.has(el.dataset.id));
  const line = $("#tree-sel");
  line.hidden = !treeSel.size;
  line.querySelector(".t").textContent = `${treeSel.size} selected`;
}
function clearTreeSel() {
  if (!treeSel.size) return false;
  treeSel.clear();
  syncTreeSel();
  return true;
}
function trimTreeSel() {
  const shown = new Set(visibleTreeRows().map((el) => el.dataset.id));
  for (const p of treeSel) if (!shown.has(p)) treeSel.delete(p);
  syncTreeSel();
}
$("#tree-sel button").addEventListener("click", () => clearTreeSel());
$("#side-search").addEventListener("input", () => trimTreeSel()); // after applySidebarFilter, registered first

// --- open folders survive a redraw (ADR-0126 point 9) -------------------------
// What the person opened or closed, by folder path, from the toggle events;
// the filter's forced opening is not theirs, so a toggle under a non-empty
// filter is not written. Applied over the server's default (top level open)
// after each innerHTML. toggle does not bubble: captured.
const treeOpen = new Map();
const folderOf = (d) => d.querySelector(":scope > summary .tree-more")?.dataset.folder;
$("#tree").addEventListener("toggle", (e) => {
  const d = e.target;
  if (!(d instanceof HTMLDetailsElement) || $("#side-search").value.trim()) return;
  const f = folderOf(d);
  if (f) treeOpen.set(f, d.open);
}, true);
function applyTreeOpen() {
  for (const d of $("#tree").querySelectorAll("details")) {
    const f = folderOf(d);
    if (f && treeOpen.has(f)) d.open = treeOpen.get(f);
  }
}
// a folder a drop landed in opens, with every folder above it
function openTreeFolder(folder) {
  const parts = folder.split("/");
  for (let i = 1; i <= parts.length; i++) treeOpen.set(parts.slice(0, i).join("/"), true);
}

// --- a drop on a folder is a move (ADR-0126 point 3) --------------------------
// The payload is app-workspaces.js's; a folder lights only under notes, since
// a folder does not move (Open 2). "New folder…" at the top of the pane takes
// the same drop and asks for the name.
const newFolderLine = $("#tree-newfolder");
let treeOver = null;
function treeDropMark(el) {
  if (treeOver === el) return;
  treeOver?.classList.remove("drop-over");
  treeOver = el;
  el?.classList.add("drop-over");
}
const treeDropTarget = (e) => e.target.closest("#tree summary") || (newFolderLine.contains(e.target) ? newFolderLine : null);
for (const host of [$("#tree"), newFolderLine]) {
  host.addEventListener("dragover", (e) => {
    const t = treeDropTarget(e);
    if (!t || !e.dataTransfer.types.includes(NOTE_DRAG)) { treeDropMark(null); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    treeDropMark(t);
  });
  host.addEventListener("dragleave", (e) => { if (!host.contains(e.relatedTarget)) treeDropMark(null); });
  host.addEventListener("drop", (e) => {
    const t = treeDropTarget(e);
    treeDropMark(null);
    if (!t || !e.dataTransfer.types.includes(NOTE_DRAG)) return;
    let it = null;
    try { it = JSON.parse(e.dataTransfer.getData(WS_DRAG) || "null"); } catch { /* not ours: the window's handler decides */ }
    if (!it || !Array.isArray(it.notes) || !it.notes.length) return;
    e.preventDefault();
    if (t === newFolderLine) { newFolderInline(it.notes); return; }
    moveFromTree(it.notes, t.querySelector(".tree-more")?.dataset.folder || "");
  });
}
document.addEventListener("dragend", () => treeDropMark(null));

// The name of the folder the notes go into, typed where the drop landed or
// where the menu's "new folder…" was picked. Under the vault root: a folder
// is only ever made by moving notes into it, since the tree is drawn from
// notes and an empty directory is invisible to it (ADR-0126 point 4).
function newFolderInline(paths) {
  closeTreeMenu();
  if (treeField) return;
  const n = paths.length;
  newFolderLine.classList.add("on");
  newFolderLine.textContent = "";
  const field = document.createElement("input");
  field.type = "text";
  field.className = "s-rename";
  field.placeholder = `New folder with ${n} ${n === 1 ? "note" : "notes"}`;
  field.setAttribute("aria-label", field.placeholder);
  newFolderLine.appendChild(field);
  treeField = field;
  field.focus();
  treeFieldKeys(field, (name) => {
    newFolderLine.classList.remove("on");
    newFolderLine.textContent = "New folder…";
    if (name) moveFromTree(paths, name);
  });
}

// Enter commits, Escape and a click away cancel: a move rewrites links across
// the vault, where a chat's title (renameSessionInline) commits on blur and
// rewrites nothing. The redraw that waited runs once the field is gone.
function treeFieldKeys(field, done) {
  let over = false;
  const finish = (save) => {
    if (over) return;
    over = true;
    treeField = null;
    const name = save ? field.value.trim() : "";
    done(name);
    if (treeRedrawDue) loadVaultInfo();
  };
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
  });
  field.addEventListener("blur", () => finish(false));
  field.addEventListener("click", (e) => e.stopPropagation()); // not a row click
}

// A note renamed in its row (ADR-0126 point 8): its folder is kept.
function renameNoteInline(row) {
  closeTreeMenu();
  const a = row?.querySelector(".tree-note");
  if (!a || treeField) return;
  const path = a.dataset.id;
  const field = document.createElement("input");
  field.type = "text";
  field.className = "s-rename";
  field.value = a.textContent;
  field.setAttribute("aria-label", "new name for this note");
  a.replaceWith(field);
  treeField = field;
  field.focus();
  field.select();
  treeFieldKeys(field, (name) => {
    field.replaceWith(a);
    if (name && name !== a.textContent) moveFromTree([path], undefined, name);
  });
}
$("#tree").addEventListener("keydown", (e) => {
  if (e.key !== "F2") return;
  const row = e.target.closest(".tree-row");
  if (row) { e.preventDefault(); renameNoteInline(row); }
});

// POST /tree/move: a folder for the set, or a name for one note. The editor's
// pending save lands first, or a note typed into and then moved would save
// under its old path. The page then follows: the pins, the open note, the
// folder dropped into opens, and the toast's undo brings the set back.
async function moveFromTree(paths, folder, name) {
  closeTreeMenu();
  if (typeof saveEdit === "function") await saveEdit();
  let r = null, data = null;
  try {
    r = await fetch("/tree/move", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(name !== undefined ? { path: paths[0], name } : { paths, folder }),
    });
    data = await r.json();
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify((data && data.detail) || "couldn't move that"); return; }
  followMoves(data.moved, data.rewritten);
  if (name === undefined && folder) openTreeFolder(folder);
  clearTreeSel();
  loadVaultInfo();
  const n = data.moved.length;
  let msg = name !== undefined
    ? (n ? `Renamed to ${name}.` : "Nothing to rename.")
    : `Moved ${n} ${n === 1 ? "note" : "notes"} to ${folder ? folder.split("/").pop() : "the vault root"}.`;
  if (data.skipped.length && name === undefined) msg += ` ${data.skipped.length} already there.`;
  if (data.error) msg += ` ${data.error}.`;
  const action = n ? { label: "undo", run: () => undoTreeMove(data.run, data.moved) } : null;
  notify(msg, data.error ? "error" : "info", action);
}

// Pins live in the browser, so only a move made from the page carries them
// (ADR-0126 point 3); the note panel follows its note (app-notes.js).
function followMoves(moved, rewritten) {
  const to = new Map(moved.map((m) => [m.from, m.to]));
  let repin = false;
  pinnedPaths = pinnedPaths.map((p) => { if (to.has(p)) { repin = true; return to.get(p); } return p; });
  if (repin) savePins();
  if (typeof followNote === "function") followNote(to, rewritten || []);
}

async function undoTreeMove(run, moved) {
  const data = await undoTreeRun(run);
  if (!data) return;
  const back = new Set(data.restored);
  followMoves(moved.filter((m) => back.has(m.from)).map((m) => ({ from: m.to, to: m.from })), []);
  loadVaultInfo();
  const n = data.restored.length, s = data.skipped.length;
  notify(`Moved back ${n} ${n === 1 ? "note" : "notes"}.`
    + (s ? ` ${s} not moved back: a note has taken the place since.` : ""), s ? "error" : "info");
}

// One undo for a delete and a move the person made from the tree (/tree/undo).
async function undoTreeRun(run) {
  let r = null, data = null;
  try {
    r = await fetch("/tree/undo", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run }),
    });
    data = await r.json();
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify((data && data.detail) || "couldn't undo that"); return null; }
  return data;
}

// A tree click opens the note drawer, which also mirrors the focus into the
// graph iframe (focusGraphNode), where it is arranged as the Map (ADR-0088).
$("#tree").addEventListener("click", (e) => {
  // The pin sits inside the row, so it is tested first: otherwise the row's own
  // click wins and pinning a note would also open it, which is the opposite of
  // what a pin is for (naming it without going there).
  const pin = e.target.closest(".tree-pin");
  if (pin) { togglePin(pin.dataset.pin); return; }
  // the row's menu (ADR-0123), also inside a folder's summary, where the
  // details must not toggle under it
  const more = e.target.closest(".tree-more");
  if (more) { e.preventDefault(); openTreeMenu(more); return; }
  // inside the folder's summary: the details must not toggle under it
  const plan = e.target.closest(".tree-plan");
  if (plan) { e.preventDefault(); learnPlanFolder(plan.dataset.folder); return; }
  const leaf = e.target.closest(".tree-note");
  if (!leaf) {
    if (e.target.id === "tree" || e.target.id === "file-tree") clearTreeSel(); // the tree's empty space
    return;
  }
  const path = leaf.dataset.id;
  // Shift and cmd/ctrl are the tree's (ADR-0126 point 1): a range from the
  // anchor, or one row toggled. The anchor is the last row clicked alone.
  if (e.button === 0 && (e.shiftKey || e[TREE_MOD]) && !e.altKey) {
    e.preventDefault();
    if (e.shiftKey) {
      const rows = visibleTreeRows().map((el) => el.dataset.id);
      const a = rows.indexOf(treeAnchor), b = rows.indexOf(path);
      if (a < 0) { treeSel.add(path); treeAnchor = path; }
      else for (const p of rows.slice(Math.min(a, b), Math.max(a, b) + 1)) treeSel.add(p);
    } else {
      if (treeSel.has(path)) treeSel.delete(path); else treeSel.add(path);
      treeAnchor = path;
    }
    syncTreeSel();
    return;
  }
  // Rows are links (audit P7): a plain left click is handled here; the middle
  // button and alt are left to the browser, which opens the note in a new tab
  // through the same URL.
  if (!plainClick(e)) return;
  e.preventDefault();
  treeAnchor = path;
  clearTreeSel();
  openNote(path);
});

// --- a tree row's menu (ADR-0123) --------------------------------------------
// The chat row's "…" menu, on a note or a folder: its workspaces, and its
// delete, armed as a chat's is. One menu for the whole tree, fixed to the
// viewport: a summary clips what it holds and toggles its <details> on any
// click inside, so the menu cannot live in the row as a chat's does.
let treeMenu = null;
let treeMenuFor = null;

function closeTreeMenu(refocus = false) {
  if (!treeMenu) return;
  treeMenu.remove();
  treeMenu = null;
  treeMenuFor?.setAttribute("aria-expanded", "false");
  if (refocus && treeMenuFor?.isConnected) treeMenuFor.focus();
  treeMenuFor = null;
}

function placeTreeMenu(x, y) {
  if (!treeMenu) return;
  if (!treeMenuFor.isConnected) { closeTreeMenu(); return; } // a tree refresh took its row
  if (x === undefined) {
    const r = treeMenuFor.getBoundingClientRect();
    x = r.right; y = r.bottom + 4;
  }
  const w = treeMenu.offsetWidth, h = treeMenu.offsetHeight;
  treeMenu.style.left = `${Math.round(Math.min(Math.max(8, x - w), innerWidth - w - 8))}px`;
  treeMenu.style.top = `${Math.round(y + h <= innerHeight - 8 ? y : Math.max(8, y - h - 8))}px`;
}

function openTreeMenu(btn, x, y) {
  const again = treeMenuFor === btn;
  closeTreeMenu();
  if (again) { btn.focus(); return; }
  const note = btn.dataset.note, folder = btn.dataset.folder;
  // the selection, when the row is in it and holds more (ADR-0126 point 7)
  const sel = note && treeSel.size > 1 && treeSel.has(note) ? [...treeSel] : null;
  // the count the summary shows is the count the server deletes: both come
  // from the notes the tree is drawn with (tree_delete)
  const n = sel ? sel.length : note ? 1 : Number(btn.closest("summary")?.querySelector(".tree-n")?.textContent || 0);
  const notes = `${n} ${n === 1 ? "note" : "notes"}`;
  // Not class s-menu: closeSessionMenus removes every .s-menu on any click
  // outside a chat row, this one included. The id takes its look (app-shell.css).
  treeMenu = mkEl("div");
  treeMenu.id = "tree-menu";
  treeMenu.setAttribute("role", "menu");
  treeMenu.setAttribute("aria-label", sel ? notes : note ? "this note" : `the folder ${folder}`);
  const item = (label, fn, cls) => {
    const b = mkEl("button", "s-item" + (cls ? " " + cls : ""), label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fn(b); });
    treeMenu.appendChild(b);
    return b;
  };
  // No "workspaces…" for a selection: ADR-0112's menu states one note's
  // membership per workspace, which three notes have no honest state for,
  // and the drag onto a workspace row adds them all.
  if (!sel) item("workspaces…", () => { closeTreeMenu(); openWsMenu(btn, note || folder, !note); });
  if (note) item("move to…", () => treeMoveMenu(sel || [note]));
  if (note && !sel) item("rename", () => renameNoteInline(btn.closest(".tree-row")));
  // A folder's delete names how many notes go, because that number is the
  // consequence: the files that are not notes, and the folder, stay.
  const label = note && !sel ? "delete" : `delete ${notes}`;
  const del = item(label, () => {}, "danger");
  armThenRun(del, label, note && !sel ? "click again to delete" : `click again · ${notes} go`,
             () => deleteFromTree(sel ? null : note, folder, sel));
  treeMenuFor = btn;
  btn.setAttribute("aria-expanded", "true");
  document.body.appendChild(treeMenu);
  placeTreeMenu(x, y);
  treeMenu.querySelector("button").focus();
}

// The menu's second page (ADR-0126 point 7): the tree's folders, the root, a
// new one. The same element, so its placement and keys hold; on touch, where
// no drag fires, this is how a note moves.
function treeMoveMenu(paths) {
  if (!treeMenu) return;
  treeMenu.innerHTML = "";
  treeMenu.setAttribute("aria-label", "move to");
  const item = (label, fn, cls) => {
    const b = mkEl("button", "s-item" + (cls ? " " + cls : ""), label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fn(); });
    treeMenu.appendChild(b);
    return b;
  };
  item("vault root", () => moveFromTree(paths, ""));
  for (const more of $("#tree").querySelectorAll(".tree-more[data-folder]")) {
    const f = more.dataset.folder;
    const b = item(f.split("/").pop(), () => moveFromTree(paths, f));
    b.style.paddingLeft = `${0.6 + 0.7 * (f.split("/").length - 1)}rem`;
    b.title = f;
  }
  item("new folder…", () => newFolderInline(paths));
  placeTreeMenu();
  treeMenu.querySelector("button").focus();
}

$("#tree").addEventListener("contextmenu", (e) => {
  const more = e.target.closest(".tree-row, #tree summary")?.querySelector(".tree-more");
  if (!more) return;
  e.preventDefault();
  openTreeMenu(more, e.clientX, e.clientY);
});
document.addEventListener("click", (e) => { if (!e.target.closest("#tree-menu, .tree-more")) closeTreeMenu(); });
document.addEventListener("keydown", (e) => {
  if (!treeMenu || !treeMenu.contains(document.activeElement)) return;
  if (e.key === "Escape") { e.preventDefault(); closeTreeMenu(true); return; }
  const items = [...treeMenu.querySelectorAll("button:not([disabled])")];
  const go = { ArrowDown: 1, ArrowUp: -1 }[e.key];
  if (go) {
    e.preventDefault();
    items[(items.indexOf(document.activeElement) + go + items.length) % items.length]?.focus();
  }
});
addEventListener("resize", () => placeTreeMenu());
// the rail scrolls under a menu opened on one of its rows: it follows the row
addEventListener("scroll", () => placeTreeMenu(), true);

async function deleteFromTree(note, folder, paths) {
  closeTreeMenu();
  let r = null, data = null;
  try {
    r = await fetch("/tree/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(paths ? { paths } : note ? { path: note } : { folder }),
    });
    data = await r.json();
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify((data && data.detail) || "couldn't delete that"); return; }
  const gone = new Set(data.deleted);
  // A pin on a deleted note opens nothing; the undo puts it back with the note.
  const unpinned = pinnedPaths.filter((p) => gone.has(p));
  if (unpinned.length) { pinnedPaths = pinnedPaths.filter((p) => !gone.has(p)); savePins(); }
  if (lastNotePath && gone.has(lastNotePath)) closeNote();
  if (lastViewedPath && gone.has(lastViewedPath)) lastViewedPath = null; // the corner toggle would reopen it
  loadVaultInfo();
  clearTreeSel();
  const what = note ? note.replace(/\.md$/, "").split("/").pop()
    : `${gone.size} ${gone.size === 1 ? "note" : "notes"}` + (folder ? ` in ${folder.split("/").pop()}` : "");
  const d = data.dangling.length;
  const msg = `Moved ${what} to the trash.`
    + (d ? ` ${d} ${d === 1 ? "link now points" : "links now point"} to no note.` : "")
    + (data.error ? ` ${data.error}.` : "");
  notify(msg, data.error ? "error" : "info", { label: "undo", run: () => undoTreeDelete(data.run, unpinned) });
}

async function undoTreeDelete(run, unpinned) {
  const data = await undoTreeRun(run);
  if (!data) return;
  const back = new Set(data.restored);
  const repin = unpinned.filter((p) => back.has(p) && !pinnedPaths.includes(p));
  if (repin.length) { pinnedPaths.unshift(...repin); savePins(); }
  loadVaultInfo();
  const n = data.restored.length, s = data.skipped.length;
  notify(`Restored ${n} ${n === 1 ? "note" : "notes"}.`
    + (s ? ` ${s} not restored: changed since the delete.` : ""), s ? "error" : "info");
}

// --- the trash (ADR-0128) ----------------------------------------------------
// Each item restores alone, not with the run it was deleted in, which is what
// the toast's undo brings back: a folder deleted at once is one item per note.

async function loadTrash() {
  let items;
  try { items = (await (await fetch("/trash")).json()).items; } catch { return; }
  if (!Array.isArray(items)) return;
  $("#trash-group").hidden = !items.length;
  $("#trash-n").textContent = items.length;
  const list = $("#trash");
  list.innerHTML = "";
  for (const t of items) {
    const row = mkEl("div", "trash-row");
    const name = mkEl("span", "trash-name", t.path.replace(/\.md$/, "").split("/").pop());
    const who = t.source === "user:/delete" ? "you" : "the chat";
    name.title = `${t.path}\ndeleted ${new Date(t.deleted_at * 1000).toLocaleString()} by ${who}`;
    const back = mkEl("button", "linklike", "restore");
    back.type = "button";
    back.setAttribute("aria-label", `restore ${t.path}`);
    if (t.taken) {
      // a restore would overwrite the note written there since
      back.disabled = true;
      back.title = "a note is at this path now";
    } else back.addEventListener("click", () => trashAct("/trash/restore", t, `Restored ${name.textContent}.`));
    const gone = mkEl("button", "linklike danger", "delete");
    gone.type = "button";
    gone.setAttribute("aria-label", `delete ${t.path} for good`);
    armThenRun(gone, "delete", "again: for good", () => trashAct("/trash/erase", t, `Deleted ${name.textContent} for good.`));
    row.append(name, back, gone);
    list.append(row);
  }
  const all = mkEl("button", "linklike danger trash-empty", "empty the trash");
  all.type = "button";
  armThenRun(all, "empty the trash", `click again · ${items.length} go for good`,
             () => trashAct("/trash/erase", { all: true }, "The trash is empty."));
  list.append(all);
}

async function trashAct(url, item, done) {
  let r = null, data = null;
  try {
    r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item.all ? { all: true } : { run: item.run, path: item.path }),
    });
    data = await r.json();
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify((data && data.detail) || "couldn't do that", "error"); loadTrash(); return; }
  notify(done);
  loadVaultInfo(); // a restored note joins the tree, and the trash redraws with it
}

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
// bytes back. It was drawn as rows of "This chat" until ADR-0122; it still
// feeds the write cards' tallies and review borders, the drawer's diff mode,
// and the review mark on the chat it belongs to.
const changedPaths = new Set();

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
  for (const r of rows) {
    changedPaths.add(r.path);
    if (r.review) reviewOf.set(r.path, r.review);
  }
  chatsReview = rows.filter((r) => r.review).length;
  syncChatsMark();
  stampWriteTallies(rows);
  stampReviewCards();
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
    const card = el.parentElement;
    const open = card && card.querySelector(".wc-open");
    if (open) open.dataset.path = r.path;
    const act = card && card.classList.contains("written") && card.querySelector(".wc-act");
    if (act && r.kind === "created") {
      card.querySelector(".wc-op").textContent = WRITE_COPY.created.label;
      act.textContent = WRITE_COPY.created.act;
      act.dataset.tip = WRITE_COPY.created.hint;
    }
    if (r.added) el.appendChild(mkEl("span", "chg-add", "+" + r.added));
    if (r.removed) el.appendChild(mkEl("span", "chg-del", "−" + r.removed));
  }
}

// One box for both lists: a note and a chat sharing a substring both match,
// each in its own list, and the status line counts per kind. Two chips (notes,
// chats) used to pick what it read; the segment above it picks what is SHOWN
// (ADR-0122), which is a different question.
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
  // workspaces: on the name, or on any member's name or path
  $("#workspaces").querySelectorAll(".ws-row").forEach((el) => {
    const w = workspaces.find((x) => x.path === el.dataset.path);
    el.hidden = !!q && !el.dataset.name.toLowerCase().includes(q) &&
                !(w && w.members.some((m) => m.path.toLowerCase().includes(q)));
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
  // The list not in view says its matches as the way to it (ADR-0122 point
  // 4): a chat named like the typed word is otherwise a match nobody sees.
  const kinds = [[notes, "note", "side-vault"], [chats, "chat", "side-chats"]].filter(([k]) => k);
  kinds.forEach(([k, w, pane], i) => {
    if (i) box.appendChild(document.createTextNode(" · "));
    if (pane === sidePane) { box.appendChild(document.createTextNode(n(k, w))); return; }
    const go = document.createElement("button");
    go.type = "button";
    go.className = "linklike sf-other";
    go.textContent = n(k, w);
    go.addEventListener("click", () => showSidePane(pane));
    box.appendChild(go);
  });
  box.appendChild(document.createTextNode(kinds.length ? " match · " : `nothing is named "${typed}" · `));
  box.classList.toggle("empty", !kinds.length);
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

$("#tree-collapse").addEventListener("click", () => {
  for (const d of $("#tree").querySelectorAll("details")) {
    d.open = false;
    const f = folderOf(d);
    if (f) treeOpen.set(f, false); // written here too: the toggle is not recorded under a filter
  }
  trimTreeSel();
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
    syncChatsMark(); // the review mark rides the current chat's row, rebuilt just now
    $("#sessions-more").textContent = "show all " + sessionCount;
    applySidebarFilter();
    applyHashSession(); // a #chat/<id> deep link names a chat that may not be the current one
    // The boot wrote "#chat" before the id was known; stamp the id in place so
    // Back from the next chat has somewhere to return to.
    if (activeTab === "chat" && currentSessionId) syncUrl(false);
    document.dispatchEvent(new CustomEvent("silica:sessions")); // the tour's "no chat yet" (ADR-0105)
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

// --- the two lists (ADR-0122) -------------------------------------------------
// One shown at a time under the segment, the other one click away: the tree and
// the chats used to split the column 3:2 and neither had room. Remembered per
// browser; a first visit shows the notes, the list every view reads (ADR-0072).
let sidePane = localStorage.getItem("side-pane") === "side-chats" ? "side-chats" : "side-vault";

function showSidePane(id) {
  if (id !== "side-vault" && id !== "side-chats") return;
  sidePane = id;
  for (const p of document.querySelectorAll("#sidebar .side-pane")) p.hidden = p.id !== id;
  for (const b of document.querySelectorAll("#side-tabs button")) setActive(b, b.dataset.pane === id);
  $("#sidebar").dataset.pane = id; // the notes' tools leave with the notes (app-shell.css)
  localStorage.setItem("side-pane", id);
  syncChatsMark();
  if ($("#side-search").value.trim()) applySidebarFilter(); // the link in the status line swaps lists
}
$("#side-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-pane]");
  if (b) showSidePane(b.dataset.pane);
});

// The states "This chat" carried while it was shut (audit P17), on the list
// that holds the chat now: amber while a write of the current chat waits for a
// verdict, the accent dot while a turn runs. The segment shows the dot only
// while the chats are out of view, where the running row's own dot is not; the
// rail's icon shows it always, since folded nothing else does.
let chatsReview = 0; // writes of the current chat under review, from /changes
let runLive = false; // set by work.js from the narration, the one reader of it

function markRun(live) {
  if (live === runLive) return;
  runLive = live;
  syncChatsMark();
}

function syncChatsMark() {
  const mark = (live) => (chatsReview ? "review" : live ? "live" : "");
  const seg = $('#side-tabs [data-pane="side-chats"]');
  const rm = $('#railmini [data-sec="side-chats"]');
  seg.dataset.mark = mark(runLive && sidePane !== "side-chats");
  rm.dataset.mark = mark(runLive);
  const said = chatsReview ? ` · ${chatsReview} to review` : runLive ? " · a turn running" : "";
  seg.setAttribute("aria-label", "chats" + said);
  rm.setAttribute("aria-label", "chats" + said);
  const row = $("#sessions .session.active");
  if (row) {
    row.classList.toggle("review", chatsReview > 0);
    row.title = chatsReview ? `${chatsReview} write${chatsReview === 1 ? "" : "s"} to review` : "";
  }
}
showSidePane(sidePane);

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
  loadWorkspaces({ adopt: true }); // the scope its last turn ran in comes back with it (ADR-0112)
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
// Explore and Metrics Examine (2026-10-13), because renaming them would touch
// every element and test that names the view. The id still routes, so a
// bookmark made before a rename lands where it did. Calendar was Plan from
// 2026-10-13 to 2026-10-15 and is Calendar again (ADR-0111 point 2): plans are
// made in Learn, and two pilot users looked for them under Plan the same day.
const VIEW_SLUG = { graph: "explore", metrics: "examine" };
const OLD_SLUG = { plan: "calendar" };
// Examine is off the hosted surface (ADR-0111 point 3): the address no longer
// opens it there, so a bookmark lands on the chat like any unknown one.
const viewOfSlug = (s) => {
  const v = Object.keys(VIEW_SLUG).find((k) => VIEW_SLUG[k] === s) || OLD_SLUG[s] || s;
  return v === "metrics" && HOSTED ? "" : v;
};

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

// The ?note= the page was loaded with, until the tree can open it. The first
// showTab rewrites the hash long before the tree has loaded, and until
// 2026-10-25 that rewrite dropped the note: a reload, or a pasted link, opened
// the view without the note it named. syncUrl keeps it while it waits.
let pendingHashNote = parseHash().note;

function syncUrl(push) {
  if (activeTab === "learn" && location.hash.startsWith("#learn")) return; // learn's own sub-states
  let h = "#" + (VIEW_SLUG[activeTab] || activeTab);
  if (activeTab === "chat" && currentSessionId) h += "/" + encodeURIComponent(currentSessionId);
  if (activeTab === "graph" && typeof graphMode === "string") h += "/" + graphMode;
  // #examine/<reading> (ADR-0072); Structure is the tab's own address.
  if (activeTab === "metrics" && typeof metricsTab === "string" && metricsTab !== "structure") h += "/" + metricsTab;
  const note = notePanel.classList.contains("open") && lastNotePath ? lastNotePath : pendingHashNote;
  if (note) h += "?note=" + encodeURIComponent(note);
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
  const note = parseHash().note || pendingHashNote;
  pendingHashNote = "";
  if (!note || note === lastNotePath) return;
  // A note the vault no longer has leaves the address with the failed open.
  openNote(note).then((ok) => { if (!ok) syncUrl(false); });
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
for (const id of ["#surface-tabs", "#cal-mode", "#metrics-tabs", "#side-tabs"]) glide($(id));

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

