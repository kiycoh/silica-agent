// The public page (ADR-0143): the view that says what goes out before it
// goes, the globes on the tree and on the note, the strip over a saved note,
// and the confirm a public page's "Save to Silica" lands on.

const PUB_PATHS = '<circle cx="12" cy="12" r="9"></circle><ellipse cx="12" cy="12" rx="4" ry="9"></ellipse><line x1="3" y1="12" x2="21" y2="12"></line>';
const pubSvg = (size, struck) => `<svg aria-hidden="true" focusable="false" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${PUB_PATHS}${struck ? '<line x1="4" y1="4" x2="20" y2="20"></line>' : ""}</svg>`;

let pubState = null;  // GET /public, the one source every globe draws from
let pubResult = null; // the last Publish, for the line under the button

const pubName = (path) => path.split("/").pop().replace(/\.md$/, "");
const pubLive = () => !!(pubState && pubState.available);
const pubOf = (path) => (pubState && pubState.notes[path]) || "off";
const pubFolderOn = (f) => !!(pubState && pubState.folders.includes(f));

// Silica's own folders and every dot-folder have no globe (ADR-0143 point 3);
// the server refuses them too, this only keeps the button from being offered.
function pubNever(path) {
  if (path.split("/").slice(0, -1).some((d) => d.startsWith("."))) return true;
  return ((pubState && pubState.never) || []).some((d) => path === d || path.startsWith(d + "/"));
}

// The handle a note credits when it is someone else's, "" for the person's own.
function pubCredit(path) {
  const a = ((pubState && pubState.authors[path]) || "").replace(/^@/, "");
  return a && a !== pubState.handle ? "@" + a : "";
}

async function loadPublicState() {
  if (!viewServed("public")) pubState = null;
  else {
    try {
      const r = await fetch("/public");
      pubState = r.ok ? await r.json() : null;
    } catch {
      // A view that failed to load offers no globe: a toggle that cannot say
      // what it did is worse than none, and the next tree refresh asks again.
      pubState = null;
    }
  }
  syncTreePublic();
  syncNotePub();
  if (activeTab === "public") renderPublic();
  return pubState;
}

function paintGlobe(b, st, size) {
  b.setAttribute("aria-pressed", String(st === "on"));
  b.classList.toggle("on", st === "on");
  b.classList.toggle("struck", st === "struck");
  // on, but the whole page is off: lit in the dim ink, so the choice shows
  // and nothing pretends to be online
  b.classList.toggle("paused", st === "on" && !pubState.page);
  b.innerHTML = pubSvg(size, st === "struck");
}

function pubTitle(st, name) {
  if (st === "on") return `on your public page: take ${name} off`;
  if (st === "struck") return `left out of a public folder: put ${name} back`;
  return `put ${name} on your public page`;
}

function syncTreePublic() {
  for (const b of document.querySelectorAll("#tree .tree-pub")) {
    const folder = b.dataset.pubFolder;
    b.hidden = !pubLive() || pubNever(folder ? folder + "/_" : b.dataset.pub);
    if (b.hidden) continue;
    const st = folder ? (pubFolderOn(folder) ? "on" : "off") : pubOf(b.dataset.pub);
    paintGlobe(b, st, 11);
    b.title = pubTitle(st, folder ? `the folder ${folder}` : pubName(b.dataset.pub));
  }
}

async function pubPost(url, body) {
  let r;
  try {
    r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  } catch {
    notify("the public page could not be reached");
    return null;
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    notify(data.detail || "the public page did not change");
    return null;
  }
  pubState = data;
  syncTreePublic();
  syncNotePub();
  if (activeTab === "public") renderPublic();
  return data;
}

async function togglePublicRow(b) {
  if (!pubLive()) return;
  const folder = b.dataset.pubFolder;
  if (folder) return pubPost("/public/folder", { folder, on: !pubFolderOn(folder) });
  const path = b.dataset.pub;
  const on = pubOf(path) !== "on";
  // Someone else's note asks first, naming them (ADR-0143 point 10), and the
  // question is asked where the note is read.
  if (on && pubCredit(path)) {
    if (lastNotePath !== path) await openNote(path);
    askCredit(path);
    return;
  }
  return pubPost("/public/note", { path, on });
}

// --- the note's globe --------------------------------------------------------

function syncNotePub() {
  const b = $("#note-pub");
  if (!b) return;
  const path = lastViewedPath;
  $("#note-pub-pop").hidden = true;
  b.hidden = !pubLive() || !path || pubNever(path);
  if (b.hidden) return;
  const st = pubOf(path);
  paintGlobe(b, st, 14);
  b.dataset.tip = pubTitle(st, "this note");
}

function askCredit(path) {
  const pop = $("#note-pub-pop");
  const who = pubCredit(path);
  pop.querySelector(".np-text").textContent =
    `This note is ${who}'s. Your page will list it under Saved from others, name ${who} as its author and link the original.`;
  pop.dataset.path = path;
  pop.hidden = false;
  pop.querySelector('[data-np="yes"]').focus();
}

$("#note-pub").addEventListener("click", (e) => {
  e.stopPropagation();
  const path = lastViewedPath;
  if (!path || !pubLive()) return;
  const on = pubOf(path) !== "on";
  if (on && pubCredit(path)) askCredit(path);
  else pubPost("/public/note", { path, on });
});

$("#note-pub-pop").addEventListener("click", (e) => {
  e.stopPropagation();
  const pick = e.target.closest("[data-np]");
  if (!pick) return;
  const pop = $("#note-pub-pop");
  pop.hidden = true;
  if (pick.dataset.np === "yes") pubPost("/public/note", { path: pop.dataset.path, on: true });
});

// --- the view ----------------------------------------------------------------

async function loadPublicView() {
  if (!pubState) $("#public-body").replaceChildren(mkEl("p", "pp-dim", "Reading the vault…"));
  await loadPublicState();
  renderPublic();
}

function ppBox(title, count, items, { wide = false, none = "" } = {}) {
  const box = mkEl("section", "pp-box" + (wide ? " wide" : ""));
  const h = mkEl("h3", "", "");
  h.append(mkEl("span", "", title));
  if (count !== null) h.append(mkEl("b", "", count));
  box.append(h);
  const ul = mkEl("ul", "");
  for (const [a, b] of items) {
    const li = mkEl("li", "");
    li.append(mkEl("span", "", a));
    if (b) li.append(mkEl("span", "pp-aside", b));
    ul.append(li);
  }
  if (!items.length && none) ul.append(mkEl("li", "pp-none", none));
  box.append(ul);
  return box;
}

const pubCount = (n) => `${n} ${n === 1 ? "note" : "notes"}`;

function renderPublic() {
  const body = $("#public-body");
  const sw = $("#public-switch");
  const link = $("#public-url");
  const st = pubState;
  if (!st || !st.available) {
    sw.hidden = true;
    link.hidden = true;
    body.replaceChildren(mkEl("p", "pp-dim", !st
      ? "The public page could not be read."
      : "This account has no public address yet: the person who runs Silica gives each account its own."));
    return;
  }
  sw.hidden = false;
  for (const b of sw.querySelectorAll("button")) {
    const on = (b.dataset.page === "on") === st.page;
    b.setAttribute("aria-pressed", String(on));
    b.classList.toggle("active", on);
  }
  link.hidden = !st.url;
  if (st.url) {
    link.href = st.url;
    link.textContent = st.url.replace(/^https?:\/\//, "") + " ↗";
  }

  const when = st.published_at ? new Date(st.published_at).toLocaleString() : "";
  const where = st.dir ? `, in ${st.dir}` : "";
  const status = !st.published_at
    ? "Not published yet. A note goes out when its globe, or its folder's, is on; Publish puts the page online."
    : st.page
      ? `Online since ${when}${where}. Changes below go out when you publish.`
      : "Off: the address says there is no page. Your folders and notes keep their globes for when you turn it on.";
  const n = st.out.length;
  const frag = document.createDocumentFragment();
  frag.append(mkEl("p", "pp-status", status));

  const grid = mkEl("div", "pp-grid");
  const goes = ppBox(st.page || !st.published_at ? "Goes out" : "Would go out", pubCount(n),
    st.out.slice(0, 60).map((t) => [t, ""]), { wide: true, none: "Nothing yet: turn on the globe of a folder or a note in the tree." });
  const how = [...st.by_folder.map((f) => `${f.folder}: ${f.on} of ${f.of}`), ...(st.single.length ? [`${st.single.length} single`] : [])];
  if (how.length) goes.querySelector("h3 span").textContent += " · " + how.join(" · ");
  if (n > 60) goes.querySelector("ul").append(mkEl("li", "pp-none", `and ${n - 60} more`));
  grid.append(goes);
  grid.append(ppBox("Stays private", pubCount(st.private_count),
    st.private.map((p) => [p.title, p.why]), { none: "Every note outside the public folders" }));
  grid.append(ppBox("Shown as plain text: the note it names is private", st.plain_count || null,
    st.plain.map((p) => [p.words, "in " + p.in]), { none: "None" }));
  grid.append(ppBox("Copied with the notes", st.images.length || null,
    st.images.map((i) => [i.name, "cited by " + i.in]), { none: "No images" }));
  grid.append(ppBox("Never on the page", null, [
    ["Inbox, sources/, Plans/, Workspaces and Silica's own files", ""],
    ["Folder names; properties other than title, tags and date", ""],
    ["Similar-note lines, proposed links, your Zotero library", ""],
  ]));
  if (st.new.length) grid.append(ppBox("New since the last publish", pubCount(st.new.length), st.new.map((t) => [t, ""])));
  frag.append(grid);

  const row = mkEl("div", "pp-actions");
  const go = mkEl("button", "pp-go", st.page ? `Publish ${pubCount(n)}` : `Turn on and publish ${pubCount(n)}`);
  go.type = "button";
  go.disabled = !n;
  go.addEventListener("click", async () => {
    go.disabled = true;
    go.textContent = "publishing…";
    const r = await pubPost("/public/publish");
    if (r && r.result) {
      pubResult = r.result;
      notify(`Published ${pubCount(r.result.notes)}`, "info");
    }
    renderPublic();
  });
  row.append(go, mkEl("span", "pp-dim", "Readers see the page as it is when you publish; a note written later in a public folder goes out the next time."));
  frag.append(row);
  if (pubResult) {
    const r = pubResult;
    frag.append(mkEl("p", "pp-dim", `Last publish: ${pubCount(r.notes)}, ${r.links} ${r.links === 1 ? "link" : "links"}, ${r.images} ${r.images === 1 ? "image" : "images"}.`));
  }
  body.replaceChildren(frag);
}

$("#public-switch").addEventListener("click", (e) => {
  const b = e.target.closest("[data-page]");
  if (b && pubState && (b.dataset.page === "on") !== pubState.page) pubPost("/public/page", { on: b.dataset.page === "on" });
});

document.addEventListener("silica:vault-info", () => loadPublicState()); // a fresh tree has bare globes

// --- a saved note --------------------------------------------------------------

// Over the text, where a paper names its authors (ADR-0055): whose it is,
// and that it is a copy.
function savedStripEl(saved) {
  const strip = mkEl("div", "note-saved");
  const when = saved.saved ? ` on ${saved.saved}` : "";
  strip.append(mkEl("span", "", `Saved from ${saved.author}${when}. A copy: it does not follow their edits.`));
  if (saved.source) {
    strip.append(Object.assign(document.createElement("a"),
      { textContent: "original ↗", href: saved.source, target: "_blank", rel: "noopener" }));
  }
  return strip;
}

// The chat cites a saved note as that author's, live and replayed alike: a
// note under Saved/@x/ is stamped where it is linked, and the sheet says
// "from @x" (ADR-0143 point 11).
const SAVED_PATH = /(?:^|\/)Saved\/(@[a-z0-9-]+)\//;
function stampSaved(root) {
  for (const a of root.querySelectorAll(".note-link[data-path]:not([data-from]), .wc-open[data-path]:not([data-from])")) {
    const m = SAVED_PATH.exec(a.dataset.path);
    if (m) a.dataset.from = m[1];
  }
}
new MutationObserver(() => stampSaved($("#log"))).observe($("#log"), { childList: true, subtree: true });

// --- Save to Silica ------------------------------------------------------------

let saveUrl = "";

async function openSaveSheet(url) {
  const sheet = $("#save-sheet");
  const card = $("#save-card");
  const go = $("#save-go");
  saveUrl = url;
  go.disabled = true;
  card.replaceChildren(mkEl("p", "st-sheet-text", "Reading the page…"));
  sheet.hidden = false;
  let r, p;
  try {
    r = await fetch("/vault/save/preview?url=" + encodeURIComponent(url));
    p = await r.json();
  } catch {
    card.replaceChildren(mkEl("p", "st-sheet-text", "That page could not be read right now."));
    return;
  }
  if (!r.ok) {
    card.replaceChildren(mkEl("p", "st-sheet-text", p.detail || "That page could not be read."));
    return;
  }
  const head = mkEl("div", "sv-card");
  head.append(mkEl("b", "", p.title), mkEl("span", "", `by ${p.author}`), mkEl("code", "", p.source));
  const parts = [head, mkEl("p", "st-sheet-text", `Into ${p.folder}.`)];
  if (p.links.length) {
    const ul = mkEl("ul", "sv-links");
    for (const l of p.links) {
      const li = mkEl("li", "");
      li.append(mkEl("span", "", l.text), mkEl("span", "pp-aside", l.to === "copy" ? "opens the copy you saved" : `opens ${p.author}'s page`));
      ul.append(li);
    }
    parts.push(mkEl("p", "st-sheet-text", "Links in your copy:"), ul);
  }
  if (p.images) parts.push(mkEl("p", "st-sheet-text", `${p.images} ${p.images === 1 ? "image stays" : "images stay"} on ${p.author}'s page.`));
  if (p.existing) parts.push(mkEl("p", "st-sheet-text", `You saved this page before (${p.existing}); this makes a second copy beside it.`));
  parts.push(mkEl("p", "sv-snap", `This saves the note as it is today. If ${p.author} edits or removes it later, your copy stays as saved.`));
  card.replaceChildren(...parts);
  go.disabled = false;
  go.focus();
}

function closeSaveSheet() {
  $("#save-sheet").hidden = true;
  saveUrl = "";
}

$("#save-cancel").addEventListener("click", closeSaveSheet);
$("#save-sheet").addEventListener("keydown", (e) => { if (e.key === "Escape") closeSaveSheet(); });
$("#save-go").addEventListener("click", async () => {
  const go = $("#save-go");
  go.disabled = true;
  let r, d;
  try {
    r = await fetch("/vault/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: saveUrl }) });
    d = await r.json().catch(() => ({}));
  } catch {
    notify("the page could not be saved");
    go.disabled = false;
    return;
  }
  if (!r.ok) {
    notify(d.detail || "the page could not be saved");
    go.disabled = false;
    return;
  }
  closeSaveSheet();
  notify(`Saved to ${d.path}`, "info");
  openNote(d.path);
});

// A public page's button opens the app on ?save=<page>; the address leaves the
// bar at once, so a reload does not ask again.
(() => {
  const q = new URLSearchParams(location.search).get("save");
  if (!q) return;
  history.replaceState(null, "", location.pathname + location.hash);
  openSaveSheet(q);
})();
