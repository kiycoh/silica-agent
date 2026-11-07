// My Wikis (ADR-0143, ADR-0151): the view that holds the account's vaults, each a
// folder of its vault, Off, Public or Private; the globes on the tree and on the note; the strip
// over a saved note; the invitations to Private vaults; and the confirm a page's "Save to Silica"
// lands on, for one note or for the areas of a whole vault.

const PUB_PATHS = '<circle cx="12" cy="12" r="9"></circle><ellipse cx="12" cy="12" rx="4" ry="9"></ellipse><line x1="3" y1="12" x2="21" y2="12"></line>';
const LOCK_PATHS = '<rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V8a4 4 0 0 1 8 0v3"></path>';
const pubSvg = (size, struck, lock) => `<svg aria-hidden="true" focusable="false" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${lock ? LOCK_PATHS : PUB_PATHS}${struck ? '<line x1="4" y1="4" x2="20" y2="20"></line>' : ""}</svg>`;

let pubState = null;  // GET /public, the one source every globe draws from
let pubCur = "";      // the slug of the vault the view has open
let pubInvites = [];  // GET /vault/invitations
let pubAdding = false;
let pubResult = null; // the last Publish, for the line under the button

const pubLive = () => !!(pubState && pubState.available);
const pubOf = (path) => (pubState && pubState.notes[path]) || "off";
const pubVaultBy = (slug) => (pubState ? pubState.vaults.find((v) => v.slug === slug) : null);
const pubVaultAt = (folder) => (pubState ? pubState.vaults.find((v) => v.folder === folder) : null);
const pubVaultOfNote = (path) => pubVaultBy(pubState && pubState.of[path]);
const pubCount = (n) => `${fmtNum(n)} ${n === 1 ? "note" : "notes"}`;
const pubCurVault = () => pubVaultBy(pubCur) || (pubState && pubState.vaults[0]) || null;

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

async function loadInvitations() {
  try {
    const r = await fetch("/vault/invitations");
    pubInvites = r.ok ? (await r.json()).invitations : [];
  } catch {
    // No list is no invitation shown; nothing is lost, the relay keeps offering it.
    pubInvites = [];
  }
}

// A vault's mark: the globe in the signal when Public, a lock when Private, the globe dim when Off.
function vaultMark(vis, size) {
  const s = mkEl("span", `pg-mark pg-${vis}`);
  s.innerHTML = pubSvg(size, false, vis === "private");
  return s;
}

function paintGlobe(b, st, size, vault) {
  b.setAttribute("aria-pressed", String(st === "on"));
  b.classList.toggle("on", st === "on" && vault && vault.visibility === "public");
  b.classList.toggle("priv", st === "on" && vault && vault.visibility === "private");
  b.classList.toggle("struck", st === "struck");
  // on, but its vault is Off: lit in the dim ink, so the choice shows and nothing pretends to
  // be online
  b.classList.toggle("paused", st === "on" && (!vault || vault.visibility === "off"));
  b.innerHTML = pubSvg(size, st === "struck", false);
}

function pubTitle(st, name, vault) {
  const where = vault ? vault.title : "its wiki";
  if (st === "on") return `goes out with ${where}: leave ${name} out`;
  if (st === "struck") return `left out of ${where}: put ${name} back`;
  return `put ${name} in ${where}`;
}

function syncTreePublic() {
  const full = pubState && pubState.vaults.length >= pubState.max;
  for (const b of document.querySelectorAll("#tree .tree-pub")) {
    const folder = b.dataset.pubFolder;
    b.classList.remove("vault", "on", "priv", "struck", "paused");
    if (!pubLive() || pubNever(folder ? folder + "/_" : b.dataset.pub)) { b.hidden = true; continue; }
    if (folder) {
      const v = pubVaultAt(folder);
      if (v) {
        b.hidden = false;
        b.classList.add("vault", v.visibility === "public" ? "on" : v.visibility === "private" ? "priv" : "paused");
        b.innerHTML = pubSvg(11, false, v.visibility === "private");
        b.title = `the wiki ${v.title} in My Wikis, ${v.visibility === "off" ? "Off" : v.visibility === "public" ? "Public" : "Private"}: open it`;
        b.setAttribute("aria-pressed", String(v.visibility !== "off"));
        continue;
      }
      // a folder inside a vault, or around one, cannot be a vault itself (ADR-0151 point 1)
      const near = pubState.vaults.some((x) => folder.startsWith(x.folder + "/") || x.folder.startsWith(folder + "/"));
      b.hidden = near || full;
      if (b.hidden) continue;
      b.innerHTML = pubSvg(11, false, false);
      b.setAttribute("aria-pressed", "false");
      b.title = `make the folder ${folder} a wiki in My Wikis`;
      continue;
    }
    const v = pubVaultOfNote(b.dataset.pub);
    b.hidden = !v;
    if (!v) continue;
    const st = pubOf(b.dataset.pub);
    paintGlobe(b, st, 11, v);
    b.title = pubTitle(st, noteStem(b.dataset.pub), v);
  }
}

async function pubPost(url, body) {
  let data;
  try {
    data = await apiPost(url, body);
  } catch (e) {
    notify(!e.status ? "My Wikis could not be reached" : e.data?.detail || "My Wikis did not change");
    return null;
  }
  if (data.vaults) {
    pubState = data;
    syncTreePublic();
    syncNotePub();
    if (activeTab === "public") renderPublic();
  }
  return data;
}

function openVault(slug) {
  pubCur = slug;
  pubAdding = false;
  if (activeTab !== "public") showTab("public");
  else renderPublic(true);
}

async function togglePublicRow(b) {
  if (!pubLive()) return;
  const folder = b.dataset.pubFolder;
  if (folder) {
    const v = pubVaultAt(folder);
    if (v) return openVault(v.slug);
    const r = await pubPost("/public/vault", { folder });
    if (r && r.added) openVault(r.added);
    return;
  }
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
  const v = path && pubLive() ? pubVaultOfNote(path) : null;
  b.hidden = !v || pubNever(path);
  if (b.hidden) return;
  const st = pubOf(path);
  paintGlobe(b, st, 14, v);
  b.dataset.tip = pubTitle(st, "this note", v);
}

function askCredit(path) {
  const pop = $("#note-pub-pop");
  const who = pubCredit(path);
  pop.querySelector(".np-text").textContent =
    `This note is ${who}'s. Your wiki will name ${who} as its author and link the original.`;
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
  await Promise.all([loadPublicState(), loadInvitations()]);
  renderPublic(true);
}

// --- the cover (ADR-0146 point 4) ----------------------------------------------
// The open vault's own graph printed in one bit under its head: every note that goes out is a
// mark, every link between two of them a line, ink on the well and nothing in between. When a
// vault opens it builds as the landing's Fig. 1 does: notes from the middle of the layout
// outward, then each link drawn out from its first note, all at full ink, in 1.7 s.
let pubCheck = null;
let pubCover = null, pubCoverK = 1, pubCoverRun = 0, pubCoverHover = -1, pubCoverRank = null;
let pubCoverEl = null, pubCoverOf = "";

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

async function loadPubCover(slug, build) {
  let c = null;
  try {
    const r = await fetch("/public/cover?vault=" + encodeURIComponent(slug));
    c = r.ok ? await r.json() : null;
  } catch {
    // A vault without its picture still says what goes out: the boxes do not need it.
    c = null;
  }
  // the person moved to another vault while this one's picture was on its way
  if (pubCurVault() && pubCurVault().slug !== slug) return;
  pubCoverOf = slug;
  pubCover = c && c.n.length ? c : null;
  pubCoverRank = null;
  if (!pubCoverEl) return;
  pubCoverEl.hidden = !pubCover;
  const areas = $("#public-body .pg-areas");
  if (areas) areas.textContent = c && c.areas ? `${c.areas.length} ${c.areas.length === 1 ? "area" : "areas"}` : "";
  if (!pubCover) return;
  const cap = pubCoverEl.querySelector("figcaption");
  const unl = pubCover.unlinked || 0;
  cap.textContent = `${fmtNum(pubCover.n.length)} notes drawn · ${fmtNum(pubCover.e.length)} ${pubCover.e.length === 1 ? "link" : "links"}`
    + (unl ? ` · ${fmtNum(unl)} with no link, in the wiki but not in the picture` : "");
  pubCoverEl.querySelector("canvas").setAttribute("aria-label", `This wiki as a graph: ${cap.textContent}`);
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
    // the order the vault builds in: from the middle of the layout outward
    const cx = N.reduce((t, n) => t + n[0], 0) / N.length, cy = N.reduce((t, n) => t + n[1], 0) / N.length;
    const d = N.map((n) => Math.hypot(n[0] - cx, n[1] - cy));
    pubCoverRank = new Float32Array(N.length);
    [...d.keys()].sort((a, b) => d[a] - d[b]).forEach((i, k) => { pubCoverRank[i] = k / N.length; });
  }
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  // cover-ink:start The press this cover shares with pages.js, which ships alone to the public
  // host and cannot load a file of the app's: tests/test_design_copies.py holds the two to one text.
  const X = (i) => Math.round(pad + N[i][0] * (w - 2 * pad)), Y = (i) => Math.round(pad + N[i][1] * (h - 2 * pad));
  const rad = (i) => (N[i][3] >= 12 ? 2 : 1);
  const x = cv.getContext("2d"), img = x.createImageData(w, h), px = new Uint32Array(img.data.buffer);
  const put = (a, b, c) => { if (a >= 0 && b >= 0 && a < w && b < h) px[b * w + a] = c; };
  const seg = (x0, y0, x1, y1, c) => {
    const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
    for (let k = 0; k <= n; k++) put(Math.round(x0 + (x1 - x0) * k / n), Math.round(y0 + (y1 - y0) * k / n), c);
  };
  const disc = (cx, cy, rr, c) => { for (let a = -rr; a <= rr; a++) for (let b = -rr; b <= rr; b++) if (a * a + b * b <= rr * rr + rr) put(cx + a, cy + b, c); };
  // cover-ink:end
  const ground = pubU32(pubRgb(pubCss("--void"))), ink = pubU32(pubRgb(pubCss("--frost")));
  px.fill(ground);
  // a note lands at at(i) and its links grow from the note that landed first
  const K = pubCoverK, rk = pubCoverRank, at = (i) => rk[i] * 0.55;
  for (const [a0, b0] of E) {
    const [a, b] = rk[a0] <= rk[b0] ? [a0, b0] : [b0, a0];
    const f = Math.min(1, Math.max(0, (K - at(b) - 0.05) / 0.35));
    if (f > 0) seg(X(a), Y(a), Math.round(X(a) + (X(b) - X(a)) * f), Math.round(Y(a) + (Y(b) - Y(a)) * f), ink);
  }
  // a clear ring of the well first, so a note reads as a mark and not as a crossing
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
  tip.textContent = n[4];
  tip.hidden = false;
  tip.style.left = `${tx}px`;
  tip.style.top = `${pad + n[1] * (h - 2 * pad)}px`;
  tip.classList.toggle("left", tx > w * 0.62);
}

new MutationObserver(() => drawPubCover()).observe(document.documentElement, { attributeFilter: ["data-theme"] });

function ppBox(title, count, { wide = false } = {}) {
  const box = mkEl("section", "pp-box" + (wide ? " wide" : ""));
  const h = mkEl("h3", "", "");
  h.append(mkEl("span", "", title));
  if (count !== null) h.append(mkEl("b", "", count));
  box.append(h);
  return box;
}

function pgSeg(label, items, current, onPick) {
  const seg = mkEl("div", "pg-seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", label);
  for (const [value, text, extra] of items) {
    const b = mkEl("button", "", text);
    b.type = "button";
    b.dataset.v = value;
    b.setAttribute("aria-pressed", String(value === current));
    if (extra) b.append(extra);
    b.addEventListener("click", () => onPick(value));
    seg.append(b);
  }
  return seg;
}

const pubDay = (iso) => {
  if (!iso) return "not yet";
  const d = new Date(iso.length <= 10 ? iso + "T12:00:00" : iso);
  return isNaN(d) ? iso : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
};

// The one window a Free account meets at what a paid account adds (ADR-0140,
// amendment of 2026-11-06). The list is the host's: on a host that sets none,
// and for an account with nothing left to buy, nothing here opens.
let upgradeFrom = null;
const upgradeOn = () => !!pubState?.plan?.length;

function upgradeOpen(head, why) {
  upgradeFrom = document.activeElement;
  $("#upgrade-h").textContent = head;
  $("#upgrade-why").textContent = why;
  $("#upgrade-list").replaceChildren(...pubState.plan.map((line) => mkEl("li", "", line)));
  $("#upgrade-sheet").hidden = false;
  $("#upgrade-go").focus();
}

function upgradeClose() {
  $("#upgrade-sheet").hidden = true;
  upgradeFrom?.focus?.();
  upgradeFrom = null;
}

// The month only: a window reopens within hours and its sentence says when, and
// a sale at every stop would be noise to the account that meets them most.
function upgradeAtLimit(ev, said) {
  if (ev.limit !== "month" || !upgradeOn()) return;
  const [head, ...rest] = said.split(/(?<=\.)\s+/);
  upgradeOpen(head.replace(/\.$/, ""), [...rest, "Reading, search and the graph keep working."].join(" "));
}

function upsellText(why) {
  if (why === "hosted") return [document.createTextNode("A Private wiki is read only by the accounts you name, which needs Silica hosted: on this computer there is no one to deliver it to.")];
  const a = Object.assign(document.createElement("a"), { href: "https://silica-agent.dev/#plans", target: "_blank", rel: "noopener", textContent: "What a paid account includes" });
  return [document.createTextNode("A Private wiki is read only by the accounts you name. It comes with a paid account: "), a, document.createTextNode(".")];
}

// --- the handle, asked on the first visit (ADR-0143 point 7, amended 2026-11-02) ---------
// An overlay on this view alone: the drawer stays live, so the person can leave and come back,
// and the view behind it is inert. The format and the reserved names are checked as typed with
// the server's own list; whether a name is taken only the host knows, within a minute.
const PUB_HANDLE = /^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$/;
// How long a claim may wait before the field opens again: the host's pass runs every minute.
const PUB_ASK_STALE_MS = 3 * 60 * 1000;
let pubAskTimer = 0, pubAskSince = 0;

function pubHandleWhy(h, reserved) {
  if (!h) return "";
  if (!PUB_HANDLE.test(h) || h.includes("--")) return "Use 1 to 30 of a-z, 0-9 and single dashes, not at either end.";
  if (reserved.includes(h)) return `@${h} is reserved.`;
  return "";
}

function pubAsk(st) {
  const view = $("#view-public");
  let over = $("#pg-ask");
  const ask = st && st.ask_handle && st.ask_handle.ask ? st.ask_handle : null;
  for (const el of [$("#public-bar"), $("#public-body")]) el.inert = !!ask;
  if (!ask) {
    if (over) over.remove();
    clearTimeout(pubAskTimer);
    pubAskSince = 0;
    return;
  }
  if (!over) {
    over = mkEl("div", "pg-ask");
    over.id = "pg-ask";
    const card = mkEl("form", "pg-ask-card");
    card.noValidate = true;
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-labelledby", "pg-ask-h");
    const h = mkEl("h2", "pg-ask-h", "Choose your handle");
    h.id = "pg-ask-h";
    const field = mkEl("label", "pg-ask-field");
    const input = Object.assign(document.createElement("input"), {
      id: "pg-ask-in", autocomplete: "off", spellcheck: false, maxLength: 31, placeholder: "your-name",
    });
    input.setAttribute("aria-describedby", "pg-ask-addr pg-ask-msg");
    field.append(mkEl("span", "", "@"), input);
    const addr = mkEl("div", "pg-ask-addr");
    addr.id = "pg-ask-addr";
    const msg = mkEl("div", "pg-ask-msg");
    msg.id = "pg-ask-msg";
    msg.setAttribute("role", "status");
    const go = mkEl("button", "pp-go", "Take this handle");
    go.type = "submit";
    const row = mkEl("div", "pg-ask-row");
    row.append(go);
    card.append(h, mkEl("p", "pg-ask-p", ""), field, addr, msg, row);
    over.append(card);
    view.append(over);
    input.addEventListener("input", () => pubAskPaint(pubState));
    card.addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = input.value.trim().replace(/^@/, "").toLowerCase();
      if (pubHandleWhy(v, pubState.ask_handle.reserved || []) || !v) return;
      go.disabled = true;
      // asked again after the stale line: the wait is this request's, or the line never leaves
      pubAskSince = 0;
      // the reply is the view again: pubPost repaints it, and a laptop's overlay is gone with it
      if (!(await pubPost("/public/handle", { handle: v }))) go.disabled = false;
    });
    input.value = ask.suggest || (ask.refused && ask.refused.handle) || "";
    if (!ask.pending) setTimeout(() => input.focus(), 0);
  }
  pubAskPaint(st);
}

function pubAskPaint(st) {
  const ask = st.ask_handle;
  const input = $("#pg-ask-in"), msg = $("#pg-ask-msg"), go = $("#pg-ask button[type=submit]");
  if (ask.pending && !input.value) input.value = ask.pending;
  const v = input.value.trim().replace(/^@/, "").toLowerCase();
  $(".pg-ask-p").textContent = st.hosted
    ? "It is your public page's address and the name readers see on every wiki you publish. You choose it once."
    : "It names your public page and is shown on every wiki you publish.";
  const base = (st.base || "").replace(/^https?:\/\//, "");
  $("#pg-ask-addr").textContent = base ? `${base}/@${v || "your-name"}/` : "";
  const pending = ask.pending || "";
  const stale = pending && pubAskSince && Date.now() - pubAskSince > PUB_ASK_STALE_MS;
  msg.className = "pg-ask-msg";
  let ok = false;
  if (pending && !stale) {
    msg.className += " ok";
    msg.textContent = `Taking @${pending}. My Wikis opens in about a minute.`;
  } else if (stale) {
    msg.textContent = "The server has not answered yet. You can try again.";
    ok = !pubHandleWhy(v, ask.reserved || []) && !!v;
  } else if (ask.refused && ask.refused.handle && ask.refused.handle === v) {
    msg.className += " err";
    msg.textContent = ask.refused.why === "taken" ? `@${v} is taken. Try another.` : `@${v} cannot be a handle.`;
  } else {
    const why = pubHandleWhy(v, ask.reserved || []);
    if (why) msg.className += " err";
    msg.textContent = why || "1 to 30 of a-z, 0-9 and single dashes.";
    ok = !why && !!v;
  }
  input.disabled = !!pending && !stale;
  go.disabled = !ok;
  go.textContent = ok ? `Take @${v}` : "Take this handle";
  clearTimeout(pubAskTimer);
  if (pending && !stale) {
    if (!pubAskSince) pubAskSince = Date.now();
    // the host answers into a file this account reads; the view asks again until it has
    pubAskTimer = setTimeout(() => { if (activeTab === "public") loadPublicState(); }, 5000);
  }
}

function renderPublic(fresh) {
  const body = $("#public-body");
  // a render that a save of another field set off keeps the field being typed in as it was
  const ae = document.activeElement;
  const keep = ae && body.contains(ae) && ae.dataset.keep
    ? { k: ae.dataset.keep, v: ae.value, s: ae.selectionStart, e: ae.selectionEnd, slug: pubCur } : null;
  const link = $("#public-url");
  const count = $("#public-count");
  const st = pubState;
  pubAsk(st);
  if (st && st.ask_handle && st.ask_handle.ask && !st.available) {
    link.hidden = true;
    count.textContent = "";
    body.replaceChildren();
    return;
  }
  if (!st || !st.available) {
    link.hidden = true;
    count.textContent = "";
    body.replaceChildren(mkEl("p", "pp-dim", !st
      ? "My Wikis could not be read."
      : "This account has no public address yet: the person who runs Silica gives each account its own handle."));
    return;
  }
  link.hidden = !st.url;
  if (st.url) {
    link.href = st.url + "/";
    link.textContent = st.url.replace(/^https?:\/\//, "") + " ↗";
  }
  count.textContent = `${st.vaults.length} of ${st.max} wikis`;
  const v = pubCurVault();
  if (v) pubCur = v.slug;
  if (pubCheck && (!v || pubCheck.slug !== v.slug)) pubCheck = null;
  if (pubCheck) {
    paintCheck();
    return;
  }
  const frag = document.createDocumentFragment();

  // the vaults of this Page, one tab each, and the way to add one
  const tabs = mkEl("div", "pg-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Your wikis");
  for (const x of st.vaults) {
    const b = mkEl("button", "pg-tab");
    b.type = "button";
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(!!v && x.slug === v.slug));
    b.append(mkEl("span", "pg-tab-name", x.title || "Untitled"), vaultMark(x.visibility, 12));
    b.addEventListener("click", () => openVault(x.slug));
    tabs.append(b);
  }
  const add = mkEl("button", "pg-tab add", "+ New wiki");
  add.type = "button";
  const full = st.vaults.length >= st.max;
  add.disabled = full && !upgradeOn();
  if (add.disabled) add.title = `you can publish ${st.max} wikis at most`;
  add.addEventListener("click", () => {
    if (full) { upgradeOpen(`A free account publishes ${st.max} wikis`, "Remove one to add another, or move to a paid account."); return; }
    pubAdding = !pubAdding;
    renderPublic();
  });
  tabs.append(add);
  frag.append(tabs);

  if (pubAdding || !v) {
    const panel = mkEl("div", "pg-panel");
    panel.append(mkEl("p", "pg-pt", "Publish a folder of your vault as a wiki"));
    const row = mkEl("div", "pg-row");
    const sel = document.createElement("select");
    sel.className = "pg-sel";
    sel.setAttribute("aria-label", "Folder");
    for (const f of st.folders) sel.append(Object.assign(document.createElement("option"), { value: f, textContent: f + "/" }));
    const go = mkEl("button", "pp-go small", "Add wiki");
    go.type = "button";
    go.disabled = !st.folders.length || add.disabled;
    go.addEventListener("click", async () => {
      const r = await pubPost("/public/vault", { folder: sel.value });
      if (r && r.added) openVault(r.added);
    });
    row.append(sel, go);
    if (v) {
      const no = mkEl("button", "st-btn", "Cancel");
      no.type = "button";
      no.addEventListener("click", () => { pubAdding = false; renderPublic(); });
      row.append(no);
    }
    panel.append(row, mkEl("p", "pp-dim", st.folders.length
      ? "It starts Off. Inbox, sources/, Plans, Workspaces and templates cannot be wikis, nor a folder inside another wiki."
      : "No folder is left that could be a wiki."));
    frag.append(panel);
  }
  if (!v) {
    frag.append(mkEl("p", "pp-status", `You can publish up to ${st.max} wikis, each a folder of your vault. Readers find them at your address, and in Pages if you list them.`));
    frag.append(invitesBox(true));
    body.replaceChildren(frag);
    return;
  }

  // a status line only when there is something to say: Off, or the lock
  const status = mkEl("p", "pp-status");
  status.hidden = v.visibility !== "off";
  if (v.visibility === "off") {
    status.textContent = v.downgraded
      ? "Off since this account became Free: a Private wiki goes Off. Its readers keep the copies they saved."
      : st.hosted || st.url
        ? "Off: this wiki's address says there is nothing there, and your public page does not show it. Its notes keep their globes for when you turn it on."
        : `Off. Publish writes it${st.dir ? ` into ${st.dir}` : ""}, as files any static host can serve; a wiki on this computer is not listed in Pages.`;
  }

  // the head of the vault in hand: its title and line, edited in place
  const head = mkEl("div", "pg-head");
  const tl = mkEl("div", "pg-tl");
  const title = Object.assign(document.createElement("input"), { className: "pg-ed pg-ed-t", value: v.title, maxLength: 80 });
  title.setAttribute("aria-label", "Title of this wiki");
  title.dataset.keep = "title";
  title.title = "Edit the title";
  const save = (key, el) => {
    const val = el.value.trim();
    if (val === (v[key] || "")) return;
    pubPost(`/public/vault/${encodeURIComponent(v.slug)}`, { [key]: val });
  };
  title.addEventListener("change", () => save("title", title));
  title.addEventListener("keydown", (e) => { if (e.key === "Enter") title.blur(); });
  const ctl = mkEl("div", "pg-ctl");
  const lock = st.private === "ok" ? null : mkEl("span", "pg-lock");
  if (lock) lock.innerHTML = pubSvg(12, false, true);
  ctl.append(pgSeg("Who can read this wiki", [["off", "Off"], ["public", "Public"], ["private", "Private", lock]], v.visibility, (x) => {
    if (x === v.visibility) return;
    if (x === "private" && upgradeOn()) { upgradeOpen("Private wikis come with a paid account", "A Private wiki is read only by the accounts you name."); return; }
    if (x === "private" && st.private !== "ok") { status.hidden = false; status.replaceChildren(...upsellText(st.private)); return; }
    pubPost(`/public/vault/${encodeURIComponent(v.slug)}`, { visibility: x });
  }));
  tl.append(title, ctl);
  const line = Object.assign(document.createElement("input"), { className: "pg-ed pg-ed-l", value: v.line, maxLength: 160, placeholder: "One line about this wiki" });
  line.setAttribute("aria-label", "One line about this wiki");
  line.dataset.keep = "line";
  line.title = "Edit the line";
  line.addEventListener("change", () => save("line", line));
  line.addEventListener("keydown", (e) => { if (e.key === "Enter") line.blur(); });
  const meta = mkEl("p", "pg-meta");
  meta.append(mkEl("span", "", "@" + st.handle), mkEl("span", "pg-path", v.folder + "/"), mkEl("span", "", pubCount(v.out)));
  const areaN = pubCover && pubCoverOf === v.slug && pubCover.areas ? pubCover.areas.length : null;
  meta.append(mkEl("span", "pg-areas", areaN === null ? "" : `${areaN} ${areaN === 1 ? "area" : "areas"}`));
  const lic = mkEl("label", "pg-lic", "License: ");
  const ls = document.createElement("select");
  for (const l of st.licenses) ls.append(Object.assign(document.createElement("option"), { value: l, textContent: l, selected: l === v.license }));
  ls.addEventListener("change", () => pubPost(`/public/vault/${encodeURIComponent(v.slug)}`, { license: ls.value }));
  lic.append(ls);
  meta.append(lic, mkEl("span", "", "Published: " + pubDay(v.published)));
  head.append(tl, line, meta);
  frag.append(head);

  // the cover: the same figure across renders, so a toggle redraws it without building it again
  const cover = pubCoverFigure();
  cover.hidden = !(pubCover && pubCoverOf === v.slug);
  frag.append(cover, status);

  const grid = mkEl("div", "pp-grid");
  const goes = ppBox(v.visibility === "off" ? "Would go out" : "Goes out", `${pubCount(v.out)} · ${v.folder}/`, { wide: true });
  const chips = mkEl("ul", "pg-chips");
  for (const t of v.top) chips.append(mkEl("li", "", t));
  if (!v.out) chips.append(mkEl("li", "more", v.held ? "Nothing yet: its notes wait for the check before Publish." : "Nothing yet: this folder has no note that can go out."));
  else if (v.out > v.top.length) chips.append(mkEl("li", "more", `and ${fmtNum(v.out - v.top.length)} more`));
  goes.append(chips);
  const notes = [];
  if (v.held) notes.push(`${pubCount(v.held)} waiting for an answer`);
  if (v.struck) notes.push(`${pubCount(v.struck)} left out by ${v.struck === 1 ? "its" : "their"} own globe`);
  if (v.new) notes.push(`${pubCount(v.new)} new since the last publish`);
  if (st.outside) notes.push(`${pubCount(st.outside)} with publish: true outside every wiki ${st.outside === 1 ? "stays" : "stay"} in your vault`);
  if (notes.length) goes.append(mkEl("p", "pp-dim", notes.join(" · ")));
  grid.append(goes);
  if (v.visibility === "public" && st.url) grid.append(listingBox(v));
  if (v.visibility === "private") grid.append(readersBox(v));
  grid.append(invitesBox(false));
  frag.append(grid);

  const row = mkEl("div", "pp-actions");
  const go = mkEl("button", "pp-go", !v.out && v.held ? "Check before publishing"
    : v.visibility === "off" ? `Turn on and publish ${pubCount(v.out)}` : `Publish ${pubCount(v.out)}`);
  go.type = "button";
  // the check is the only way to the answers that free a held note, so it stays one press away
  go.disabled = !v.out && !v.held;
  go.addEventListener("click", () => startPublish(v, go));
  row.append(go, mkEl("span", "pp-dim", inWikis(v) ? LEAVES_WIKIS : v.visibility === "private"
    ? "Your readers get the wiki as it is when you publish, as an invitation to save it."
    : "Readers see the wiki as it is when you publish; a note written later in its folder goes out the next time."));
  if (v.visibility === "off") {
    const rm = mkEl("button", "st-btn", "Remove from My Wikis");
    rm.type = "button";
    rm.title = "the folder and its notes stay as they are";
    rm.addEventListener("click", async () => { const r = await pubPost(`/public/vault/${encodeURIComponent(v.slug)}/remove`); if (r) { pubCur = ""; renderPublic(true); } });
    row.append(rm);
  }
  frag.append(row);
  if (pubResult && pubResult.vault === v.slug) {
    const r = pubResult;
    frag.append(mkEl("p", "pp-dim", `Last publish: ${pubCount(r.notes)}, ${r.links} ${r.links === 1 ? "link" : "links"}, ${r.images} ${r.images === 1 ? "image" : "images"}.`));
  }
  body.replaceChildren(frag);
  if (keep && keep.slug === v.slug) {
    const el = body.querySelector(`[data-keep="${keep.k}"]`);
    if (el) { el.value = keep.v; el.focus({ preventScroll: true }); el.setSelectionRange(keep.s, keep.e); }
  }
  if (fresh || pubCoverOf !== v.slug || (pubCover && pubCover.n.length + (pubCover.unlinked || 0) !== v.out)) loadPubCover(v.slug, !!fresh);
  else drawPubCover();
}

// --- the listing (ADR-0153 points 7, 8 and 13) -------------------------------------
// `v.listed` is the author's request; `v.review` is the host's record of what became of it. The
// host answers within a minute, so a request it has not read yet already reads as waiting.
const LEAVES_WIKIS = "Publishing takes this wiki out of Wikis until it is reviewed again.";
const inWikis = (v) => v.listed && v.review.state === "in";

function listingRow(text, label, listed, v) {
  const row = mkEl("div", "lst-row");
  row.append(mkEl("p", "", text));
  if (label) {
    const b = mkEl("button", "st-btn", label);
    b.type = "button";
    b.addEventListener("click", () => pubPost(`/public/vault/${encodeURIComponent(v.slug)}`, { listed }));
    row.append(b);
  }
  return row;
}

function listingNotice(r) {
  const dl = mkEl("dl", "lst-say");
  for (const [k, t] of [["Rule", r.rule], ["What was seen", r.seen], ["How it began", r.began],
    ["Automated tools", r.tools], ["What you can do", r.remedy]]) dl.append(mkEl("dt", "", k), mkEl("dd", "", t));
  return dl;
}

function listingBox(v) {
  const r = v.review;
  const since = r.since ? ` since ${pubDay(r.since)}` : "";
  const box = (status, body) => { const b = ppBox("In Wikis", status, { wide: true }); b.append(body); return b; };
  if (v.blocked.length) return box("Not listed", listingRow(v.blocked.join(" ")));
  if (!v.listed) return box("Not listed", listingRow("Your wiki is reviewed before it enters Wikis. Until then it is reachable at its address and search engines leave it alone.", "Ask to list", true, v));
  if (r.state === "in") return box("Listed", listingRow(`In Wikis${since}. A Publish takes it out until it is reviewed again.`, "Leave Wikis", false, v));
  if (r.state === "out") return box("Changed", listingRow("Out of Wikis: it was published again after its review. It is waiting for review again, and it stays public at its address."));
  if (r.state === "no") return box("Refused", listingNotice(r));
  return box("Waiting", listingRow(`Waiting for review${since}. Your wiki stays public at its address meanwhile.`, "Withdraw", false, v));
}

// --- the check before Publish (ADR-0153 points 9, 20 and 26) -----------------------
// One question, whose is it, for each file, folder of images and site this wiki shows. The server
// decides and the build enforces; the screen draws what /public/check answers, so the count at
// its foot is the count Publish will honour.
const PRE_ANSWERS = [["mine", "Mine, or I have permission"], ["open", "Open or public domain"], ["else", "Someone else's"]];
const PRE_SHEET = 10;
const prePlural = (n, w) => `${fmtNum(n)} ${w}${n === 1 ? "" : "s"}`;
const preOpen = (o) => !o.answer || (o.answer === "open" && !o.free && o.license !== "CC BY-ND");

async function publishNow(v) {
  const r = await pubPost("/public/publish", { vault: v.slug });
  if (r) pubCheck = null;
  if (r && r.result) {
    pubResult = r.result;
    notify(`Published ${pubCount(r.result.notes)}${r.result.private ? ` for ${v.readers.length} ${v.readers.length === 1 ? "reader" : "readers"}` : ""}`, "info");
  }
  renderPublic();
}

async function startPublish(v, go) {
  go.disabled = true;
  go.textContent = "checking…";
  let c = null;
  try {
    const r = await fetch(`/public/check?vault=${encodeURIComponent(v.slug)}`);
    c = await r.json().catch(() => null);
    if (!r.ok) { notify(c?.detail || "The check could not run"); renderPublic(); return; }
  } catch {
    notify("My Wikis could not be reached");
    renderPublic();
    return;
  }
  if (c.ask) {
    pubCheck = { slug: v.slug, data: c, open: new Set(), all: new Set(), old: false, personal: false };
    $("#public-body").scrollTop = 0;
    renderPublic();
    return;
  }
  go.textContent = "publishing…";
  await publishNow(v);
}

async function preSend(url, body) {
  const d = await pubPost(url, { vault: pubCheck.slug, ...body });
  if (d && pubCheck) {
    pubCheck.data = d;
    paintCheck();
  }
}
const preAnswer = (row, change) => preSend("/public/check/answer", { key: row.key, answer: row.answer, license: row.license, credit: row.credit, off: row.off || [], ...change });

// a redraw keeps the reader where they were: the control they used, and the place on the page
function paintCheck() {
  const body = $("#public-body");
  const key = document.activeElement?.dataset?.key;
  const top = body.scrollTop;
  body.replaceChildren(checkSheet(pubCurVault()));
  body.scrollTop = top;
  if (key) body.querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
}

function preLink(text, key, onClick) {
  const b = mkEl("button", "pre-link", text);
  b.type = "button";
  b.dataset.key = key;
  b.addEventListener("click", onClick);
  return b;
}

function preSeg(row) {
  const seg = pgSeg(`Whose is ${row.name}`, PRE_ANSWERS, row.answer, (x) => preAnswer(row, { answer: row.answer === x ? "" : x }));
  for (const b of seg.querySelectorAll("button")) b.dataset.key = `${row.key}|${b.dataset.v}`;
  return seg;
}

function preLicense(row) {
  if (row.answer !== "open") return null;
  const box = mkEl("div", "pre-lic");
  const lic = mkEl("label", "", "License ");
  const s = document.createElement("select");
  s.dataset.key = row.key + "|lic";
  s.append(Object.assign(document.createElement("option"), { value: "", textContent: "Choose" }));
  for (const l of pubCheck.data.licenses) s.append(Object.assign(document.createElement("option"), { value: l, textContent: l, selected: l === row.license }));
  s.addEventListener("change", () => preAnswer(row, { license: s.value }));
  lic.append(s);
  const credit = mkEl("label", "wide", "Credit ");
  const i = Object.assign(document.createElement("input"), { value: row.credit, placeholder: "Author, title", maxLength: 200 });
  i.dataset.key = row.key + "|credit";
  i.addEventListener("change", () => preAnswer(row, { credit: i.value }));
  credit.append(i);
  box.append(lic, credit);
  return box;
}

function preOpenHint(o) {
  if (!o.license) return "Choose its license, or it stays out.";
  if (o.license === "CC BY-ND") return "CC BY-ND allows nothing made from it: it stays out.";
  if (!o.free) return "Write its credit, or it stays out.";
  return (o.credit ? "Goes out with this credit." : "Goes out.") + (o.license === "CC BY-SA" ? " This wiki must then be CC BY-SA too." : "");
}

function preFileHint(f) {
  const base = f.answer === "mine" ? "Goes out. You are saying you wrote it or have permission."
    : f.answer === "open" ? preOpenHint(f)
    : f.answer === "else" ? "Not published, images included. It stays yours to use in the chat."
    : "No answer yet: not published, images included.";
  const stuck = f.free ? f.notes.filter((n) => !n.goes).length : 0;
  const wrote = f.free ? 0 : f.notes.filter((n) => n.mine && n.goes).length;
  return base
    + (stuck ? ` ${stuck} of its notes also ${stuck === 1 ? "comes" : "come"} from a file that stays, so ${stuck === 1 ? "it stays" : "they stay"}.` : "")
    + (wrote ? ` ${prePlural(wrote, "note")} you wrote yourself ${wrote === 1 ? "goes" : "go"} out.` : "");
}

function preNotes(f) {
  const box = mkEl("div", "pre-notes");
  if (!f.free) box.append(mkEl("p", "", "Tick only the notes you wrote yourself. Copied or translated notes do not count, and the review reads what you tick."));
  for (const n of f.notes) {
    if (f.free) {
      const row = mkEl("div", "pre-note" + (n.goes ? "" : " off"));
      row.append(mkEl("span"), mkEl("span", "t", n.title), mkEl("span", "st", n.goes ? "Goes out" : `Not published: also from ${n.held}`));
      box.append(row);
    } else if (n.can) {
      const row = mkEl("label", "pre-note");
      const c = Object.assign(document.createElement("input"), { type: "checkbox", checked: n.mine });
      c.dataset.key = "note|" + n.path;
      c.addEventListener("change", () => preSend("/public/check/note", { path: n.path, on: c.checked }));
      row.append(c, mkEl("span", "t", n.title), mkEl("span", "st", n.status));
      box.append(row);
    } else {
      const row = mkEl("div", "pre-note off");
      row.append(mkEl("span"), mkEl("span", "t", n.title), mkEl("span", "st", n.status));
      box.append(row);
    }
  }
  if (f.images.length) {
    const sheet = mkEl("div", "pre-sheet");
    for (const img of f.images) sheet.append(preTile(null, img));
    box.append(sheet);
  }
  return box;
}

function preFileRow(f) {
  const row = mkEl("div", "pre-row");
  const open = pubCheck.open.has(f.key);
  const flip = () => { open ? pubCheck.open.delete(f.key) : pubCheck.open.add(f.key); paintCheck(); };
  const nm = mkEl("div", "nm");
  const count = preLink(`${prePlural(f.notes.length, "note")}${f.images.length ? ` · ${prePlural(f.images.length, "image")}` : ""}`, f.key + "|o", flip);
  count.className = "ct";
  count.setAttribute("aria-expanded", String(open));
  nm.append(mkEl("span", "fn", f.name), count);
  const hint = mkEl("span", "pre-hint" + (f.free ? " go" : ""));
  hint.append(mkEl("span", "", preFileHint(f)));
  if (!f.free) {
    const wrote = preLink("I wrote some of these", f.key + "|w", flip);
    wrote.setAttribute("aria-expanded", String(open));
    hint.append(wrote);
  }
  row.append(nm, preSeg(f));
  const lic = preLicense(f);
  if (lic) row.append(lic);
  row.append(hint);
  if (open) row.append(preNotes(f));
  return row;
}

function preTile(g, img) {
  const free = !!g && g.free;
  const t = mkEl(free ? "label" : "div", "pre-th" + (free ? "" : " still") + (free && !img.on ? " off" : ""));
  t.title = img.name;
  if (free) {
    const c = Object.assign(document.createElement("input"), { type: "checkbox", checked: img.on });
    c.dataset.key = `${g.key}|${img.name}`;
    c.setAttribute("aria-label", `Publish ${img.name}`);
    c.addEventListener("change", () => {
      const off = new Set(g.off);
      if (c.checked) off.delete(img.name); else off.add(img.name);
      preAnswer(g, { off: [...off] });
    });
    t.append(c);
  }
  t.append(Object.assign(document.createElement("img"), { src: img.src, alt: img.name, loading: "lazy" }), mkEl("span", "", img.note || img.name));
  return t;
}

function preGroupHint(g) {
  const site = g.kind === "site";
  if (g.answer === "open") return preOpenHint(g);
  if (g.answer === "mine") return site ? `Copied into your wiki, credited "from ${g.name}". Untick any that should stay a link.` : "Goes out. Untick any you cannot vouch for.";
  if (g.answer === "else") return site ? `Stays a link to ${g.name}.` : "Not published.";
  return site ? `No answer yet: stays a link to ${g.name}.` : "No answer yet: not published.";
}

function preGroupRow(g) {
  const row = mkEl("div", "pre-row");
  const nm = mkEl("div", "nm");
  nm.append(mkEl("span", "fn" + (g.kind === "site" ? " site" : ""), g.name),
    mkEl("span", "ct", g.images.length ? prePlural(g.images.length, "image") : prePlural(g.bad.length, "link")));
  row.append(nm);
  if (g.images.length) {
    row.append(preSeg(g));
    const lic = preLicense(g);
    if (lic) row.append(lic);
    const hint = mkEl("span", "pre-hint" + (g.free ? " go" : ""));
    hint.append(mkEl("span", "", preGroupHint(g)));
    row.append(hint);
    const all = pubCheck.all.has(g.key);
    const sheet = mkEl("div", "pre-sheet");
    for (const img of all ? g.images : g.images.slice(0, PRE_SHEET)) sheet.append(preTile(g, img));
    row.append(sheet);
    const more = mkEl("div", "pre-more");
    if (g.images.length > PRE_SHEET) {
      more.append(preLink(all ? "Show fewer" : `Show all ${g.images.length}`, g.key + "|m", () => {
        if (all) pubCheck.all.delete(g.key); else pubCheck.all.add(g.key);
        paintCheck();
      }));
    }
    if (g.free && g.images.length > 1) {
      const allOn = g.images.every((i) => i.on);
      more.append(preLink(allOn ? "Untick all" : "Tick all", g.key + "|t", () => preAnswer(g, { off: allOn ? g.images.map((i) => i.name) : [] })));
    }
    if (more.children.length) row.append(more);
  }
  for (const b of g.bad) {
    const line = mkEl("div", "pre-bad");
    line.append(mkEl("span", "fn", b.name), mkEl("span", "st", `${b.why}: stays a link`));
    row.append(line);
  }
  return row;
}

function preCheckRow(n, text, what, stop, extra) {
  const li = mkEl("li");
  const words = mkEl("span", "", text);
  if (extra) words.append(" ", extra);
  li.append(mkEl("span", "n", fmtNum(n)), words, mkEl("span", "what" + (stop ? " stop" : ""), what));
  return li;
}

function preAnsweredBefore(n, word) {
  if (!n) return null;
  const p = mkEl("p", "pp-dim pre-hand", `Answered before: ${prePlural(n, word)}. `);
  p.append(preLink(pubCheck.old ? "Hide" : "Change", "old|" + word, () => { pubCheck.old = !pubCheck.old; paintCheck(); }));
  return p;
}

function checkSheet(v) {
  const c = pubCheck.data;
  const frag = document.createDocumentFragment();
  const top = mkEl("div", "pre-top");
  // the view's counts follow the answers, so Back reads them again
  top.append(preLink("Back", "back", () => { pubCheck = null; loadPublicView(); }), mkEl("h2", "pre-h", "Before you publish"),
    mkEl("p", "pre-lead", "Say once whose each thing is. What nobody answers for stays out."));
  frag.append(top);

  const files = c.files.filter((f) => !f.old || pubCheck.old);
  if (c.files.length) {
    const open = files.filter(preOpen).length;
    const box = ppBox("Notes made from files", open ? `${open} with no answer` : "All answered", { wide: true });
    box.append(mkEl("p", "pre-p", "Whose is each file? The answer holds for every note and image made from it. A note of two files follows the stricter answer."));
    for (const f of files) box.append(preFileRow(f));
    const said = preAnsweredBefore(c.files.filter((f) => f.old).length, "file");
    if (said) box.append(said);
    frag.append(box);
  }

  if (c.plain) {
    const box = ppBox("Notes with no known source", fmtNum(c.plain), { wide: true });
    box.append(mkEl("p", "pre-p", "Silica cannot tell where these come from: you may have written them, pasted them, or made them in the chat. They go out, and publishing them says they are yours to publish."));
    frag.append(box);
  }

  const groups = c.groups.filter((g) => !g.old || pubCheck.old);
  if (c.groups.length || c.from_files) {
    const open = groups.filter((g) => g.images.length && preOpen(g)).length;
    const box = ppBox("Images", open ? `${open} with no answer` : "All answered", { wide: true });
    box.append(mkEl("p", "pre-p", 'Nothing goes out until you say whose it is. An image with no answer stays out, and readers see "Image not published". One from another site stays a link.'));
    if (c.from_files) {
      const row = mkEl("div", "pre-row");
      const nm = mkEl("div", "nm");
      nm.append(mkEl("span", "fn", "From your files"), mkEl("span", "ct", prePlural(c.from_files, "image")));
      row.append(nm, mkEl("span", "pre-hint", "They follow the file they came from."));
      box.append(row);
    }
    for (const g of groups) box.append(preGroupRow(g));
    const said = preAnsweredBefore(c.groups.filter((g) => g.old).length, "group");
    if (said) box.append(said);
    frag.append(box);
  }

  const k = c.checks;
  if (k.personal.length || k.location || k.lost || k.nowhere) {
    const box = ppBox("Other checks", null, { wide: true });
    const ul = mkEl("ul", "pre-checks");
    if (k.personal.length) {
      const see = preLink("See them", "personal", () => { pubCheck.personal = !pubCheck.personal; paintCheck(); });
      see.setAttribute("aria-expanded", String(pubCheck.personal));
      ul.append(preCheckRow(k.personal_notes, k.personal_notes === 1 ? "note shows someone's personal data" : "notes show someone's personal data", "Keeps it out of Wikis", true, see));
    }
    if (k.location) ul.append(preCheckRow(k.location, k.location === 1 ? "image carries a location" : "images carry a location", "Removed at Publish"));
    if (k.lost) ul.append(preCheckRow(k.lost, k.lost === 1 ? "link leads to a note that is not published" : "links lead to notes that are not published", "Become plain words"));
    if (k.nowhere) ul.append(preCheckRow(k.nowhere, k.nowhere === 1 ? "link leads nowhere" : "links lead nowhere", "Become plain words"));
    box.append(ul);
    if (pubCheck.personal) {
      const list = mkEl("ul", "pre-found");
      for (const [title, what] of k.personal) list.append(mkEl("li", "", `${title}: ${what}`));
      box.append(list);
    }
    frag.append(box);
  }

  const foot = mkEl("div", "pre-foot");
  foot.setAttribute("role", "status");
  const sum = mkEl("div");
  const line = mkEl("p", "pre-sum", `Goes out: ${prePlural(c.count.notes, "note")}, ${prePlural(c.count.images, "image")}. `);
  const links = c.count.links ? ` ${prePlural(c.count.links, "image")} ${c.count.links === 1 ? "stays a link" : "stay links"}.` : "";
  line.append(mkEl("span", "q", `Not published: ${prePlural(c.count.stay, "note")}, ${prePlural(c.count.kept, "image")}.${links}`));
  sum.append(line);
  const wikis = inWikis(v) ? LEAVES_WIKIS : c.wikis;
  if (wikis) sum.append(mkEl("p", "pp-dim", wikis));
  const go = mkEl("button", "pp-go", `${v.visibility === "off" ? "Turn on and publish" : "Publish"} ${prePlural(c.count.notes, "note")}`);
  go.type = "button";
  go.disabled = !c.count.notes;
  go.addEventListener("click", () => { go.disabled = true; go.textContent = "publishing…"; publishNow(v); });
  foot.append(sum, go);
  frag.append(foot);
  return frag;
}

function readersBox(v) {
  const box = ppBox("Readers", `${v.readers.length} ${v.readers.length === 1 ? "reader" : "readers"}`, { wide: true });
  const row = mkEl("div", "pg-row");
  const input = Object.assign(document.createElement("input"), { className: "pg-sel pg-grow", placeholder: "Add a reader by their handle" });
  input.setAttribute("aria-label", "A reader's handle");
  input.dataset.keep = "reader";
  const go = mkEl("button", "pp-go small", "Invite");
  go.type = "button";
  const invite = () => {
    const h = input.value.trim().replace(/^@/, "").toLowerCase();
    if (!h) return;
    input.value = ""; // the render the invite sets off keeps what the field holds, which is none
    pubPost(`/public/vault/${encodeURIComponent(v.slug)}`, { readers: [...v.readers, h] });
  };
  go.addEventListener("click", invite);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") invite(); });
  row.append(input, go);
  const ul = mkEl("ul", "pg-rows");
  for (const r of v.readers) {
    const li = mkEl("li", "");
    li.append(mkEl("span", "pg-who", "@" + r), mkEl("span", "pp-dim", v.published ? "gets it at each publish" : "gets it at the first publish"));
    const rm = mkEl("button", "st-btn", "Remove");
    rm.type = "button";
    rm.addEventListener("click", () => pubPost(`/public/vault/${encodeURIComponent(v.slug)}`, { readers: v.readers.filter((x) => x !== r) }));
    li.append(rm);
    ul.append(li);
  }
  if (!v.readers.length) ul.append(mkEl("li", "pp-dim", "No readers yet: invite one by their handle."));
  box.append(row, ul, mkEl("p", "pp-dim", "Each reader gets an invitation in their own Silica and saves the wiki there; nothing is written in their vault until they do. Removing a reader cancels an invitation not yet saved. A copy already saved stays theirs, with your name on every note, and cannot go in their own wikis."));
  return box;
}

function invitesBox(alone) {
  const n = pubInvites.length;
  const box = ppBox("Shared with you", `${n} ${n === 1 ? "invitation" : "invitations"}`, { wide: true });
  const ul = mkEl("ul", "pg-rows");
  for (const inv of pubInvites) {
    const li = mkEl("li", "");
    const what = mkEl("span", "pg-grow");
    what.append(mkEl("span", "pg-who", "@" + inv.author), document.createTextNode(` shared ${inv.title}`));
    li.append(what, mkEl("span", "pp-dim", `${pubCount(inv.notes)}${inv.published ? " · " + pubDay(inv.published) : ""}`));
    const no = mkEl("button", "st-btn", "Decline");
    no.type = "button";
    no.addEventListener("click", async () => {
      const r = await postJson("/vault/invitations/decline", { author: inv.author, vault: inv.vault }).catch(() => null);
      if (r && r.ok) { pubInvites = (await r.json()).invitations; renderPublic(); }
    });
    const yes = mkEl("button", "st-btn primary", "Save to Silica");
    yes.type = "button";
    yes.addEventListener("click", () => openSaveSheet({ invite: inv, areas: null }));
    li.append(no, yes);
    ul.append(li);
  }
  if (!n) ul.append(mkEl("li", "pp-dim", "No invitations. When an account names you as a reader of a Private wiki, it appears here."));
  box.append(ul, mkEl("p", "pp-dim", "Free and paid accounts both receive. A saved copy lands in Saved/@handle/ and is a write like any other: it can be taken back."));
  box.hidden = alone ? false : !n;
  return box;
}

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
// One note, the areas of a public vault, or an invitation to a Private one (ADR-0151 points 8, 9).

let saveWhat = null;   // {url} or {invite}
let saveAreas = null;  // the area indexes ticked, null for all of them
let saveNotes = null;  // the notes a page named beside its whole areas, null for none

function saveAreasQuery() {
  const named = saveNotes ? "&notes=" + saveNotes.join(",") : "";
  if (saveAreas === null) return named;
  return (saveAreas.length ? saveAreas.map((a) => "&areas=" + a).join("") : "&areas=none") + named;
}

async function openSaveSheet(what) {
  const sheet = $("#save-sheet");
  const card = $("#save-card");
  const go = $("#save-go");
  if (what !== saveWhat) { saveAreas = what.areas === undefined ? null : what.areas; saveNotes = what.notes || null; }
  saveWhat = what;
  go.disabled = true;
  go.textContent = "save";
  if (!card.childElementCount) card.replaceChildren(mkEl("p", "st-sheet-text", what.invite ? "Reading the invitation…" : "Reading the page…"));
  sheet.hidden = false;
  let r, p;
  try {
    r = await fetch(what.invite
      ? `/vault/invitations/preview?author=${encodeURIComponent(what.invite.author)}&vault=${encodeURIComponent(what.invite.vault)}${saveAreasQuery()}`
      : "/vault/save/preview?url=" + encodeURIComponent(what.url) + saveAreasQuery());
    p = await r.json();
  } catch {
    card.replaceChildren(mkEl("p", "st-sheet-text", "That page could not be read right now."));
    return;
  }
  if (saveWhat !== what) return;
  if (!r.ok) {
    card.replaceChildren(mkEl("p", "st-sheet-text", p.detail || "That page could not be read."));
    return;
  }
  const head = mkEl("div", "sv-card");
  head.append(mkEl("b", "", p.title), mkEl("span", "", `by ${p.author}`), mkEl("code", "", what.invite ? "a Private wiki shared with you" : (p.source || p.url || "").replace(/^https:\/\//, "")));
  const parts = [head];
  if (p.kind === "note") {
    parts.push(mkEl("p", "st-sheet-text", `Into ${p.folder}.`));
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
    return;
  }
  // a whole vault: the areas to take, and the room they need
  const areas = mkEl("ul", "sv-areas");
  const named = p.areas.reduce((t, a) => t + a[1], 0);
  const rows = p.areas.map((a, i) => [i, a[0], a[1]]);
  if (p.of > named) rows.push([-1, "In no area: notes with no link", p.of - named]);
  for (const [i, name, size] of rows) {
    const li = mkEl("li", "");
    const lab = mkEl("label", "");
    const box = Object.assign(document.createElement("input"), { type: "checkbox", checked: saveAreas === null || saveAreas.includes(i) });
    box.addEventListener("change", () => {
      const all = rows.map((x) => x[0]);
      const now = saveAreas === null ? all : saveAreas;
      saveAreas = box.checked ? [...new Set([...now, i])] : now.filter((x) => x !== i);
      if (saveAreas.length === all.length) saveAreas = null;
      openSaveSheet(saveWhat);
    });
    lab.append(box, mkEl("span", "pg-grow", name), mkEl("span", "pp-dim", fmtNum(size)));
    li.append(lab);
    areas.append(li);
  }
  parts.push(mkEl("p", "st-sheet-text", `${pubCount(p.notes + p.already)} of ${fmtNum(p.of)}, into ${p.folder}/. Links between them stay links${what.invite ? "; a link to a note you leave out keeps its words" : `; a link to a note you leave out opens ${p.author}'s page`}.`));
  if (p.already) parts.push(mkEl("p", "st-sheet-text", `${fmtNum(p.already)} of these ${p.already === 1 ? "is already in your vault and stays as it is" : "are already in your vault and stay as they are"}.`));
  // a selection was made on the page, note by note: the sheet confirms it, areas could not say it
  if (!saveNotes) parts.push(areas);
  const room = p.room;
  if (room.max_notes || room.max_mb) {
    parts.push(mkEl("p", "st-sheet-text" + (room.short ? " sv-over" : ""), room.short
      ? `This save does not fit: ${room.short}. Choose fewer ${saveNotes ? "notes" : "areas"}.`
      : `Your vault holds ${fmtNum(room.notes)} notes${room.max_notes ? ` of ${fmtNum(room.max_notes)}` : ""}; after saving, ${fmtNum(room.notes + p.notes)}.`));
  }
  if (p.license) parts.push(mkEl("p", "st-sheet-text", `License: ${p.license === "not stated" ? "not stated by the author" : p.license}.`));
  parts.push(mkEl("p", "sv-snap", `This saves the wiki as it is today, as one write you can take back. If ${p.author} edits or removes a note later, your copy stays as saved.`));
  card.replaceChildren(...parts);
  go.textContent = p.notes ? `save ${pubCount(p.notes)}` : "nothing new to save";
  go.disabled = !p.notes || !!room.short;
  if (!go.disabled) go.focus();
}

function closeSaveSheet() {
  $("#save-sheet").hidden = true;
  $("#save-card").replaceChildren();
  saveWhat = null;
  saveAreas = null;
  saveNotes = null;
}

$("#upgrade-not").addEventListener("click", upgradeClose);
// stopped here, or the one Escape handler closes whatever lies under the window too
$("#upgrade-sheet").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); upgradeClose(); } });
$("#upgrade-sheet").addEventListener("click", (e) => { if (e.target === e.currentTarget) upgradeClose(); });
$("#save-cancel").addEventListener("click", closeSaveSheet);
$("#save-sheet").addEventListener("keydown", (e) => { if (e.key === "Escape") closeSaveSheet(); });
$("#save-go").addEventListener("click", async () => {
  const go = $("#save-go");
  go.disabled = true;
  const what = saveWhat;
  let r, d;
  try {
    r = await postJson(what.invite ? "/vault/invitations/save" : "/vault/save", what.invite ? { author: what.invite.author, vault: what.invite.vault, areas: saveAreas } : { url: what.url, areas: saveAreas, notes: saveNotes });
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
  notify(d.notes ? `Saved ${pubCount(d.notes)} to ${d.folder}` : `Saved to ${d.path}`, "info");
  if (what.invite) { await loadInvitations(); if (activeTab === "public") renderPublic(); }
  await loadVaultInfo(); // the tree shows the copy now, not at the next poll
  openNote(d.path);
});

// A page's button opens the app on ?save=<page>[&areas=..][&notes=..]; the address leaves the bar at once,
// so a reload does not ask again.
(() => {
  const qs = new URLSearchParams(location.search);
  const q = qs.get("save");
  if (!q) return;
  const areas = qs.getAll("areas").flatMap((a) => a.split(",")).filter((a) => /^-?\d+$/.test(a)).map(Number);
  const notes = qs.getAll("notes").flatMap((n) => n.split(",")).filter((n) => /^[a-z0-9-]+$/.test(n));
  history.replaceState(null, "", location.pathname + location.hash);
  openSaveSheet({ url: q, areas: areas.length ? areas : null, notes: notes.length ? notes : null });
})();
