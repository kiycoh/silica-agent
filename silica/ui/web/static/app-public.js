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

// a dialog in the one focus trap (app-settings.js DIALOGS), so Escape is its way out
$("#note-pub-pop").addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  e.stopPropagation();
  $("#note-pub-pop").hidden = true;
  $("#note-pub").focus();
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
  // the page builds itself each time the view opens; a later render only redraws it
  loadPubCover(true);
}

// --- the cover (ADR-0146 point 4) ----------------------------------------------
// The page's own graph printed in one bit above the boxes: every note that goes out is a mark,
// every link between two of them a line, ink on the well and nothing in between. When the view
// opens it builds as the landing's Fig. 1 does: notes from the middle of the layout outward,
// then each link drawn out from its first note, all at full ink, in 1.7 s.
let pubCover = null, pubCoverK = 1, pubCoverRun = 0, pubCoverHover = -1, pubCoverRank = null;
let pubCoverEl = null;

function pubCoverFigure() {
  if (pubCoverEl) return pubCoverEl;
  pubCoverEl = mkEl("figure", "pp-cover");
  const box = mkEl("div", "pp-cover-box");
  const cv = document.createElement("canvas");
  cv.setAttribute("role", "img");
  const tip = mkEl("div", "pp-ctip");
  tip.hidden = true;
  box.append(cv, tip);
  pubCoverEl.append(box, mkEl("figcaption", "", ""));
  box.addEventListener("pointermove", pubCoverPoint);
  box.addEventListener("pointerleave", () => { pubCoverHover = -1; tip.hidden = true; drawPubCover(); });
  new ResizeObserver(() => drawPubCover()).observe(box);
  return pubCoverEl;
}

async function loadPubCover(build) {
  let c = null;
  try {
    const r = await fetch("/public/cover");
    c = r.ok ? await r.json() : null;
  } catch {
    // A page without its picture still says what goes out: the boxes do not need it.
    c = null;
  }
  pubCover = c && c.n.length ? c : null;
  pubCoverRank = null;
  if (pubCoverEl) pubCoverEl.hidden = !(pubCover || (pubState && pubState.out.length));
  if (!pubCover) return;
  const cap = pubCoverEl.querySelector("figcaption");
  cap.textContent = `${pubCount(pubCover.n.length)} · ${pubCover.e.length.toLocaleString("en")} ${pubCover.e.length === 1 ? "link" : "links"}`;
  pubCoverEl.querySelector("canvas").setAttribute("aria-label", `The page as a graph: ${cap.textContent}`);
  const run = ++pubCoverRun;
  if (!build || matchMedia("(prefers-reduced-motion: reduce)").matches) { pubCoverK = 1; drawPubCover(); return; }
  pubCoverK = 0;
  const t0 = performance.now();
  (function step(now) {
    if (run !== pubCoverRun) return;
    pubCoverK = Math.min(1, (now - t0) / 1700);
    drawPubCover();
    if (pubCoverK < 1) requestAnimationFrame(step);
  })(t0);
}

const pubCss = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const pubRgb = (hex) => { const h = hex.replace("#", ""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); };
const pubU32 = (c) => ((255 << 24) | (c[2] << 16) | (c[1] << 8) | c[0]) >>> 0;

function drawPubCover() {
  if (!pubCover || !pubCoverEl || activeTab !== "public") return;
  const box = pubCoverEl.querySelector(".pp-cover-box"), cv = box.querySelector("canvas");
  const r = box.getBoundingClientRect(), w = Math.floor(r.width), h = Math.floor(r.height);
  if (w < 40 || h < 40) return;
  const N = pubCover.n, E = pubCover.e, pad = 14;
  if (!pubCoverRank) {
    // the order the page builds in: from the middle of the layout outward
    const cx = N.reduce((t, n) => t + n[0], 0) / N.length, cy = N.reduce((t, n) => t + n[1], 0) / N.length;
    const d = N.map((n) => Math.hypot(n[0] - cx, n[1] - cy));
    pubCoverRank = new Float32Array(N.length);
    [...d.keys()].sort((a, b) => d[a] - d[b]).forEach((i, k) => { pubCoverRank[i] = k / N.length; });
  }
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const X = (i) => Math.round(pad + N[i][0] * (w - 2 * pad)), Y = (i) => Math.round(pad + N[i][1] * (h - 2 * pad));
  const x = cv.getContext("2d"), img = x.createImageData(w, h), px = new Uint32Array(img.data.buffer);
  const ground = pubU32(pubRgb(pubCss("--void"))), ink = pubU32(pubRgb(pubCss("--frost")));
  px.fill(ground);
  const put = (a, b, c) => { if (a >= 0 && b >= 0 && a < w && b < h) px[b * w + a] = c; };
  const seg = (x0, y0, x1, y1, c) => {
    const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
    for (let k = 0; k <= n; k++) put(Math.round(x0 + (x1 - x0) * k / n), Math.round(y0 + (y1 - y0) * k / n), c);
  };
  const disc = (cx, cy, rr, c) => { for (let a = -rr; a <= rr; a++) for (let b = -rr; b <= rr; b++) if (a * a + b * b <= rr * rr + rr) put(cx + a, cy + b, c); };
  // a note lands at at(i) and its links grow from the note that landed first
  const K = pubCoverK, rk = pubCoverRank, at = (i) => rk[i] * 0.55;
  for (const [a0, b0] of E) {
    const [a, b] = rk[a0] <= rk[b0] ? [a0, b0] : [b0, a0];
    const f = Math.min(1, Math.max(0, (K - at(b) - 0.05) / 0.35));
    if (f > 0) seg(X(a), Y(a), Math.round(X(a) + (X(b) - X(a)) * f), Math.round(Y(a) + (Y(b) - Y(a)) * f), ink);
  }
  // a clear ring of the well first, so a note reads as a mark and not as a crossing
  // a mark's width comes with the cover (public_site.mark_px), so a sparse page prints
  // bigger marks here and on the published page alike
  const rad = (i) => ((N[i][4] || 3) - 1) / 2;
  for (let i = 0; i < N.length; i++) {
    if (K < at(i)) continue;
    disc(X(i), Y(i), rad(i) + 1, ground); disc(X(i), Y(i), rad(i), ink);
  }
  if (pubCoverHover >= 0 && K >= 1) {
    // the pointed note rings in ink: the print keeps its two inks (DESIGN.md, the Two Inks
    // Rule), where it once lit the note and its links in teal
    const i = pubCoverHover, r0 = rad(i);
    disc(X(i), Y(i), r0 + 4, ground); disc(X(i), Y(i), r0 + 3, ink);
    disc(X(i), Y(i), r0 + 1, ground); disc(X(i), Y(i), r0, ink);
  }
  x.putImageData(img, 0, 0);
}

function pubCoverPoint(ev) {
  if (!pubCover || pubCoverK < 1) return;
  const box = ev.currentTarget, r = box.getBoundingClientRect(), w = Math.floor(r.width), h = Math.floor(r.height), pad = 14;
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  let best = -1, bd = 10 * 10;
  pubCover.n.forEach((n, i) => {
    const d = (pad + n[0] * (w - 2 * pad) - mx) ** 2 + (pad + n[1] * (h - 2 * pad) - my) ** 2;
    if (d < bd) { bd = d; best = i; }
  });
  if (best === pubCoverHover) return;
  pubCoverHover = best;
  drawPubCover();
  const tip = box.querySelector(".pp-ctip");
  if (best < 0) { tip.hidden = true; return; }
  const n = pubCover.n[best], tx = pad + n[0] * (w - 2 * pad);
  tip.textContent = n[3];
  tip.hidden = false;
  tip.style.left = `${tx}px`;
  tip.style.top = `${pad + n[1] * (h - 2 * pad)}px`;
  tip.classList.toggle("left", tx > w * 0.62);
}

new MutationObserver(() => drawPubCover()).observe(document.documentElement, { attributeFilter: ["data-theme"] });

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

const pubCount = (n) => `${n.toLocaleString("en")} ${n === 1 ? "note" : "notes"}`;

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

  // the stamp Examine prints (2026-10-31 01:08), in the reader's own time: toLocaleString gave a
  // US date with seconds and AM/PM, unlike every other date in the app
  const d = st.published_at ? new Date(st.published_at) : null, p2 = (x) => String(x).padStart(2, "0");
  const when = d ? `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}` : "";
  const where = st.dir ? `, in ${st.dir}` : "";
  const status = !st.published_at
    ? "Not published yet. A note goes out when its globe, or its folder's, is on; Publish puts the page online."
    : st.page
      ? `Online since ${when}${where}. Changes below go out when you publish.`
      : "Off: the address says there is no page. Your folders and notes keep their globes for when you turn it on.";
  const n = st.out.length;
  const frag = document.createDocumentFragment();
  // the same figure across renders, so a toggle redraws the page without building it again
  const cover = pubCoverFigure();
  // the well holds its place from the first render, so the boxes do not jump when the picture
  // arrives
  cover.hidden = !(pubCover || n);
  frag.append(cover);
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
  // the boxes after the action: at the 900 px floor the cover and five boxes had pushed Publish
  // under the fold (measured 2026-11-01), and the status line above it is what it answers
  frag.append(grid);
  body.replaceChildren(frag);
  if (pubCover && pubCover.n.length !== n) loadPubCover(false);
  else drawPubCover();
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
  await loadVaultInfo(); // the tree shows the copy now, not at the next poll
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
