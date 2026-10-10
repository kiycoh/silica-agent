// --- zotero ------------------------------------------------------------------
// ADR-0055's interface: Settings › Zotero, Explore › Library, and the labels
// and strip a paper wears elsewhere. What each surface SAYS is decided by the
// pure functions below, which tests/test_zotero_web_ui.py runs under node; the
// DOM halves under them only place it.

// zotero-pure:start
const ZOTERO_ALLOW = "Zotero › Settings › Advanced › Allow other applications on this "
  + "computer to communicate with Zotero";

// The Status row, one line per state the server can name (zotero_web.status).
function zoteroStatus(s) {
  const st = (s && s.state) || "closed";
  if (st === "running" || st === "read_only") {
    const bits = [`Zotero ${s.version || ""}`.trim(), `${s.items || 0} items`];
    if (s.bbt) bits.push("Better BibTeX");
    const text = bits.join(" · ") + (st === "read_only" ? " · read only: writes need Zotero 10" : "");
    return { dot: "●", text, fix: "" };
  }
  if (st === "api_off") {
    return { dot: "○", text: "running, API off", fix: ZOTERO_ALLOW };
  }
  if (st === "other") {
    return { dot: "○", text: `another library answers: ${s.linked_library} is linked, `
      + `${s.library} is running`, fix: "open the linked library in Zotero, or relink here" };
  }
  return { dot: "○", text: "not running", fix: "start Zotero; if it is running, tick " + ZOTERO_ALLOW };
}

// A vault that does not hold the link reads the library and changes nothing
// in it (ADR-0066 decision 1), and the header says where the link is: that
// path is how a stray owner, an e2e vault left in /tmp, gets found.
function libraryHeader(lib) {
  if (lib.read_only) {
    return `${lib.total} in Zotero · read only here · `
      + (lib.holder ? `linked to ${lib.holder}` : "no vault linked");
  }
  return `${lib.total} in Zotero · ${lib.in_vault} in this vault · `
    + `${lib.new} new since last session`;
}

// ● in the vault, ○ not yet, ◐ the PDF changed in Zotero since import. A new
// paper is ○ too, in the accent: it is not in the vault either.
function rowMark(state) {
  return {
    in: { mark: "●", cls: "in", why: "in the vault" },
    changed: { mark: "◐", cls: "changed", why: "PDF changed in Zotero since import" },
    new: { mark: "○", cls: "new", why: "new in Zotero since the last session" },
    there: { mark: "●", cls: "there", why: "in the linked vault: click the title to read it by peek" },
  }[state] || { mark: "○", cls: "out", why: "not in the vault yet" };
}

// The inbox row's suggestion: the neighbours' first choice and their vote, or
// the second choice once the user switched (zotero_write.suggest_filing).
function suggestion(row, names, second) {
  const target = second ? row.second : row.first;
  return { target, text: names[target] || target,
    vote: second ? "2nd" : `${row.votes}/3` };
}

// A citekey when the paper has one, the item link otherwise: both are
// /nucleate targets (ADR-0055 decision 3).
function bringCommand(rows) {
  return "/nucleate " + rows.map((r) => (r.citekey
    ? "@" + r.citekey : "zotero://select/library/items/" + r.key)).join(" ");
}

function authorsOf(fm) {
  const a = fm.authors;
  return Array.isArray(a) ? a : (a ? [String(a)] : []);
}

// "Surname Year", the way a reference list names a paper; "" for a note that
// is not a paper, so callers fall back to the stem.
function paperLabel(fm) {
  if (!fm || !fm.zotero_key) return "";
  const first = authorsOf(fm)[0];
  const surname = first ? first.split(",")[0].trim() : String(fm.title || "?").split(/\s+/)[0];
  return `${surname} ${fm.year || "n.d."}`;
}

function paperStrip(fm) {
  const names = authorsOf(fm).map((a) => a.split(",")[0].trim());
  const who = names.length > 3 ? names.slice(0, 3).join(", ") + " et al." : names.join(", ");
  const byline = [who, fm.year, fm.venue].filter(Boolean).join(" · ");
  const links = [["Zotero", "zotero://select/library/items/" + fm.zotero_key]];
  if (fm.pdf_key) links.push(["PDF", "zotero://open-pdf/library/items/" + fm.pdf_key]);
  if (fm.doi) links.push(["DOI", "https://doi.org/" + fm.doi]);
  return { byline, links };
}
// A read chip's name: "Surname Year" for a paper leaf, the stem otherwise.
function chipLabelFor(path, labels) {
  return (labels && labels[path]) || String(path).split("/").pop().replace(/\.md$/, "");
}

// What a click on a link inside a rendered note or answer does. A footnote
// scrolls in place, because app state lives in the hash and `#fn1` would be
// routed; zotero: and obsidian: go to the OS protocol handler; http(s) opens a
// tab. Two schemes named, not "any scheme": javascript: must stay inert.
function linkAction(href) {
  const h = String(href || "").trim();
  if (/^#fn(ref)?\d/.test(h)) return "footnote";
  if (/^(zotero|obsidian):/i.test(h)) return "scheme";
  if (/^https?:/i.test(h)) return "external";
  return null;
}
// The scope picker's value back into the scope the frame reads. A Zotero
// collection is named, not numbered, and Papers only has no id at all. A
// collection scope is a set since ADR-0066: `collections:` alone is every
// collection the frame announced (`names`), `collections:` with a JSON list
// is the set the Library tab picked.
function parseScope(v, label, names) {
  const kind = v.slice(0, v.indexOf(":"));
  const rest = v.slice(v.indexOf(":") + 1);
  if (kind === "folder") return { kind, prefix: rest, label };
  if (kind === "collection") return { kind, ids: [rest], label };
  if (kind === "collections") {
    return rest ? { kind: "collection", ids: JSON.parse(rest), label }
      : { kind: "collection", ids: [...(names || [])], all: true, label };
  }
  if (kind === "papers") return { kind, label };
  return { kind, id: Number(rest), label };
}

// The other way: a scope to the value the pickers hold.
function scopeValue(sc) {
  if (!sc) return "";
  if (sc.kind === "papers") return "papers:";
  if (sc.kind === "collection") {
    if (sc.all) return "collections:";
    return sc.ids.length === 1 ? "collection:" + sc.ids[0] : "collections:" + JSON.stringify(sc.ids);
  }
  return sc.kind + ":" + (sc.kind === "folder" ? sc.prefix : sc.id);
}

// "name · covered of total" over the papers on the rim (ADR-0066 decision 5),
// "12-temporal · 0 of 12"; only the total before a pass has run, and the
// leaves this vault already holds after it, since those sit inside the graph.
function collectionCount(c, pending) {
  const bits = [];
  if (c.outside) bits.push(pending ? String(c.outside) : `${c.covered || 0} of ${c.outside}`);
  if (c.size) bits.push(c.outside ? `${c.size} in vault` : String(c.size));
  return bits.join(" · ");
}

// The Collections group, from what the frame says it can scope to (it read
// membership live at its build). Papers only and All collections belong to
// the scope picker: comparing "all papers" with a part of them says nothing.
function zoteroScopeOptions(groups, withPapers) {
  const out = [];
  const pending = !groups.covers || groups.covers.state === "none";
  if (withPapers && groups.papers) out.push(["papers:", `Papers only · ${groups.papers}`]);
  const cols = groups.collections || [];
  if (withPapers && cols.length > 1 && groups.rim && groups.rim.outside) {
    out.push(["collections:", "All collections · "
      + collectionCount({ outside: groups.rim.outside, covered: groups.rim.covered }, pending)]);
  }
  for (const c of cols) out.push(["collection:" + c.name, `${c.name} · ${collectionCount(c, pending)}`]);
  return out;
}

// The chip beside the tabs while a COVERS pass runs and once it is done
// (ADR-0066 decision 6): it counts, then offers the rebuild instead of taking
// it, for the reason it offers "vault changed": a rebuild throws away the
// camera, the zoom and the focused node. null when there is nothing to say.
function coversCue(pass) {
  if (!pass) return null;
  if (pass.running) {
    return { text: `covers · ${pass.done} of ${pass.total || "…"} papers`, busy: true,
             title: "Silica is searching this vault once per library paper; the graph is redrawn when you ask" };
  }
  if (pass.ready) {
    return { text: "⟳ covers ready", busy: false,
             title: "the covers pass finished: rebuild the graph to draw it" };
  }
  return null;
}
// zotero-pure:end

async function zPost(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) });
  let d = {};
  try { d = await r.json(); } catch { /* an empty answer is still an answer */ }
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

function zEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

// Two clicks for anything that writes into the user's own Zotero tree, the
// same confirm "Revert all N" asks for (ADR-0055 decision 7): the first click
// says what will happen, the second does it.
function zTwoClick(btn, label, armedLabel, run) {
  btn.textContent = label;
  btn.addEventListener("click", async () => {
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = armedLabel();
      btn.classList.add("armed");
      return;
    }
    btn.dataset.armed = "";
    btn.classList.remove("armed");
    btn.disabled = true;
    try { await run(); } finally { btn.disabled = false; btn.textContent = label; }
  });
}

// --- Settings › Zotero ---------------------------------------------------------
function zSettingRow(host, label, help) {
  const row = zEl("div", "st-row");
  row.dataset.search = `zotero ${label} ${help || ""}`.toLowerCase();
  const lab = zEl("div", "st-label");
  lab.appendChild(zEl("span", "st-name", label));
  if (help) lab.appendChild(zEl("div", "st-help", help));
  row.appendChild(lab);
  const ctl = zEl("div", "st-ctl");
  row.appendChild(ctl);
  const note = zEl("div", "st-note");
  row.appendChild(note);
  host.appendChild(row);
  return { row, ctl, note };
}

function zSay(note, cls, text) { note.className = "st-note " + cls; note.textContent = text; }

async function renderZotero(host) {
  host.querySelectorAll(".st-row, .st-note-line").forEach((e) => e.remove());
  let s;
  try { s = await (await fetch("/zotero/status")).json(); }
  catch { host.appendChild(zEl("div", "st-note-line", "could not ask the server about Zotero")); return; }
  const up = s.state === "running" || s.state === "read_only";
  const line = zoteroStatus(s);

  const st = zSettingRow(host, "status", "probed live on 127.0.0.1:23119");
  st.ctl.appendChild(zEl("span", "st-ctl-text " + (up ? "up" : "down"), `${line.dot} ${line.text}`));
  const again = zEl("button", "st-btn st-safe", "recheck");
  again.type = "button";
  again.addEventListener("click", () => renderZotero(host));
  st.ctl.appendChild(again);
  if (line.fix) st.note.textContent = line.fix;

  const use = zSettingRow(host, "use in this vault", "writes the zotero: block in vault.yaml");
  const box = zEl("input");
  box.type = "checkbox";
  box.checked = !!s.linked;
  box.disabled = !s.linked && (!up || !!s.elsewhere);
  box.setAttribute("aria-label", "use Zotero in this vault");
  box.addEventListener("change", async () => {
    try {
      await zPost("/zotero/link", { on: box.checked });
      renderZotero(host);
      zoteroBoot();
    } catch (err) {
      box.checked = !box.checked;
      zSay(use.note, "bad", err.message);
    }
  });
  use.ctl.appendChild(box);
  if (s.linked) use.note.textContent = `linked to library ${s.linked_library}`;
  else if (s.elsewhere) zSay(use.note, "bad", `this library is linked to ${s.elsewhere}; reach its papers by peek`);
  if (!s.linked) return;

  const wa = zSettingRow(host, "write access", "Zotero asks you in its own dialog");
  if (s.write) {
    wa.ctl.appendChild(zEl("span", "st-ctl-text up", "● granted"));
    wa.note.textContent = "key kept in your user .env, outside the vault";
  } else {
    const grant = zEl("button", "st-btn", "grant");
    grant.type = "button";
    grant.disabled = s.state !== "running";
    grant.addEventListener("click", async () => {
      grant.disabled = true;
      zSay(wa.note, "pending", "waiting for Zotero: answer its dialog (Always Allow keeps the key)");
      try {
        const out = await zPost("/zotero/grant");
        if (out.kept) renderZotero(host);
        else zSay(wa.note, "bad", "allowed once: the key is not kept; Silica asks again before the next write");
      } catch (err) {
        zSay(wa.note, "bad", "denied: Silica reads the library and writes nothing to it");
      } finally { grant.disabled = false; }
    });
    wa.ctl.appendChild(grant);
    if (s.state === "read_only") wa.note.textContent = "writes need Zotero 10";
  }

  const bl = zSettingRow(host, "back-links in Zotero",
    "importing a paper adds a child note under it, tagged silica, linking back to its notes");
  const blBox = zEl("input");
  blBox.type = "checkbox";
  blBox.checked = !!s.backlinks;
  blBox.setAttribute("aria-label", "write back-link notes in Zotero");
  blBox.addEventListener("change", async () => {
    try { await zPost("/zotero/setting", { key: "backlinks", value: blBox.checked }); zSay(bl.note, "ok", "saved"); }
    catch (err) { blBox.checked = !blBox.checked; zSay(bl.note, "bad", err.message); }
  });
  bl.ctl.appendChild(blBox);

  const ib = zSettingRow(host, "inbox collection", "filing proposes moves out of this collection");
  const pick = zEl("select");
  pick.setAttribute("aria-label", "inbox collection");
  pick.appendChild(new Option("none", ""));
  for (const c of s.collections || []) pick.appendChild(new Option(c, c));
  pick.value = s.inbox || "";
  pick.addEventListener("change", async () => {
    try { await zPost("/zotero/setting", { key: "inbox", value: pick.value }); zSay(ib.note, "ok", "saved"); }
    catch (err) { zSay(ib.note, "bad", err.message); }
  });
  ib.ctl.appendChild(pick);

  const gr = zSettingRow(host, "groups collection", "created by the first groups write");
  gr.ctl.appendChild(zEl("span", "st-ctl-text", s.groups ? `Silica groups · ${s.groups}` : "not created yet"));

  const un = zSettingRow(host, "unlink", "removes Silica's notes and groups from Zotero; items are never touched");
  const unBtn = zEl("button", "st-btn st-safe");
  unBtn.type = "button";
  zTwoClick(unBtn, "remove from Zotero", () => "remove them? click again", async () => {
    try {
      const out = await zPost("/zotero/unlink");
      zSay(un.note, "ok", `removed ${out.notes} note(s) and ${out.collections} collection(s)`);
    } catch (err) { zSay(un.note, "bad", err.message); }
  });
  un.ctl.appendChild(unBtn);
}

// --- Explore › Library ------------------------------------------------------------
const zLib = { linked: false, lib: null, col: "", data: null, sel: new Set(), alt: {}, side: null,
  show: new Set() };

// The surface exists wherever Zotero answers (ADR-0066 decision 1), and in a
// linked vault even while Zotero is closed, where it says so. Only the linked
// vault writes: anywhere else it is read-only.
async function zoteroBoot() {
  let s = null;
  try { s = await (await fetch("/zotero/status")).json(); } catch { /* no server, no surface */ }
  const linked = !!(s && s.linked);
  const on = linked || !!(s && (s.state === "running" || s.state === "read_only"));
  const at = LAYOUT_MODES.findIndex(([m]) => m === "library");
  if (at >= 0) LAYOUT_MODES.splice(at, 1);
  if (on) {
    LAYOUT_MODES.push(["library", "Library", linked
      ? "your Zotero library: bring papers in, file the inbox, write groups"
      : "your Zotero library, read only here: the linked vault brings papers in", 2]);
  }
  zLib.linked = linked;
  zCoversResume();
  buildSurfaceTabs();
  // The route ran at load, before this mode existed: #explore/library on a
  // reload would otherwise land on the graph.
  if (on && pendingGraphMode === "library") { pendingGraphMode = ""; setGraphMode("library"); }
  if (!on) return;
  try {
    const r = await fetch("/zotero/library");
    zLib.lib = r.ok ? await r.json() : null;
  } catch { zLib.lib = null; }
  zBadge();
}

function zBadge() {
  const b = document.querySelector('#surface-tabs .stab[data-gmode="library"]');
  if (!b) return;
  b.querySelector(".lay-count")?.remove();
  if (zLib.lib && zLib.lib.new) b.appendChild(zEl("span", "lay-count", String(zLib.lib.new)));
}

async function drawLibrary() {
  const pane = $("#shape-pane");
  pane.innerHTML = "";
  const body = zEl("div", "shape-body zl surface-in");
  pane.appendChild(body);
  try {
    const r = await fetch("/zotero/library");
    zLib.lib = r.ok ? await r.json() : null;
  } catch { zLib.lib = null; }
  zBadge();
  if (!zLib.lib) {
    body.appendChild(zEl("p", "zl-empty", "Zotero is not answering. Start it, then open Library again."));
    return;
  }
  const head = zEl("div", "shape-head");
  head.appendChild(zEl("h2", "zl-title", "Library"));
  head.appendChild(zEl("span", "shape-sub", libraryHeader(zLib.lib)));
  if (!zLib.lib.read_only) {
    const groupsBtn = zEl("button", "zl-btn", "Write groups to Zotero");
    groupsBtn.type = "button";
    groupsBtn.addEventListener("click", () => zShowGroups());
    head.appendChild(groupsBtn);
  }
  body.appendChild(head);

  const grid = zEl("div", "zl-grid");
  const tree = zEl("nav", "zl-tree");
  tree.setAttribute("aria-label", "Zotero collections");
  const main = zEl("section", "zl-main");
  const side = zEl("aside", "zl-side");
  grid.append(tree, main, side);
  body.appendChild(grid);
  zLib.side = side;

  const cols = zLib.lib.collections;
  const kids = (parent) => cols.filter((c) => (c.parent || "") === parent);
  // A tick per collection and one button: the graph's scope is a set of
  // collections, and this list is where a set is picked (ADR-0066 decision 5);
  // the picker above the graph holds one value.
  const showBtn = zEl("button", "zl-btn", "Show on graph");
  showBtn.type = "button";
  const syncShow = () => {
    showBtn.disabled = !zLib.show.size;
    showBtn.textContent = zLib.show.size > 1 ? `Show ${zLib.show.size} on graph` : "Show on graph";
  };
  const add = (c, depth) => {
    const row = zEl("div", "zl-col-row");
    const tick = zEl("input");
    tick.type = "checkbox";
    tick.checked = zLib.show.has(c.name);
    tick.disabled = !c.total;
    tick.setAttribute("aria-label", "show " + c.name + " on the graph");
    tick.style.marginLeft = (0.6 + depth * 0.9) + "rem";
    tick.addEventListener("change", () => {
      if (tick.checked) zLib.show.add(c.name); else zLib.show.delete(c.name);
      syncShow();
    });
    const b = zEl("button", "zl-col");
    b.type = "button";
    b.dataset.key = c.key;
    b.title = c.name;   // the tick narrows the row: a cut name reads whole on hover
    b.appendChild(zEl("span", "zl-col-name", c.name));
    b.appendChild(zEl("span", "zl-col-count" + (c.in_vault && c.in_vault === c.total ? " full" : ""),
      zLib.lib.read_only ? String(c.total) : `${c.in_vault} / ${c.total}`));
    b.addEventListener("click", () => { zLib.col = c.key; zLib.sel.clear(); zLib.alt = {}; zLoadCollection(); });
    row.append(tick, b);
    tree.appendChild(row);
    for (const k of kids(c.key)) add(k, depth + 1);
  };
  for (const c of kids("")) add(c, 0);
  showBtn.addEventListener("click", () => {
    const ids = [...zLib.show].sort();
    if (!ids.length) return;
    setGraphScope({ kind: "collection", ids,
      label: ids.length === 1 ? ids[0] : `${ids.length} collections` });
    setGraphMode("graph");
  });
  syncShow();
  tree.appendChild(showBtn);
  tree.appendChild(zEl("p", "zl-hint", "Read live from Zotero. Collections are edited in Zotero."));
  if (!zLib.col || !cols.some((c) => c.key === zLib.col)) {
    zLib.col = zLib.lib.inbox || (cols.find((c) => c.total) || cols[0] || {}).key || "";
  }
  zSideHint();
  if (zLib.col) zLoadCollection();
}

// Empty until a preview or the groups need it: an empty card is hidden by CSS,
// so on a narrow pane the table is not pushed down by a hint.
function zSideHint() {
  if (zLib.side) zLib.side.innerHTML = "";
}

async function zLoadCollection() {
  document.querySelectorAll(".zl-col").forEach((b) => b.classList.toggle("on", b.dataset.key === zLib.col));
  const main = document.querySelector(".zl-main");
  if (!main) return;
  main.innerHTML = "";
  main.appendChild(zEl("p", "zl-hint", "reading Zotero…"));
  try {
    const r = await fetch("/zotero/collection?key=" + encodeURIComponent(zLib.col));
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
    zLib.data = d;
  } catch (err) {
    main.innerHTML = "";
    main.appendChild(zEl("p", "zl-empty", "could not read this collection: " + err.message));
    return;
  }
  if (zLib.data.inbox) {
    for (const r of zLib.data.rows) if (r.votes === 3 && r.first) zLib.sel.add(r.key);
  }
  zRenderTable();
}

function zNames() {
  const out = {};
  for (const c of (zLib.lib && zLib.lib.collections) || []) out[c.key] = c.name;
  return out;
}

function zRenderTable() {
  const main = document.querySelector(".zl-main");
  if (!main || !zLib.data) return;
  main.innerHTML = "";
  const col = zNames()[zLib.col] || "";
  const inbox = zLib.data.inbox;
  const rows = zLib.data.rows;
  const bar = zEl("div", "zl-bar");
  bar.appendChild(zEl("h3", "zl-col-title", col));
  const chosen = rows.filter((r) => zLib.sel.has(r.key));
  const readOnly = zLib.lib && zLib.lib.read_only;
  if (readOnly) {
    bar.appendChild(zEl("span", "zl-hint", zLib.lib.holder
      ? `papers come in through ${zLib.lib.holder}, the vault linked to this library`
      : "papers come in through the vault linked to this library: link one in Settings › Zotero"));
  } else if (inbox) {
    const file = zEl("button", "zl-btn primary");
    file.type = "button";
    file.disabled = !chosen.length;
    zTwoClick(file, `File ${chosen.length}`, () => `Move ${chosen.length} item(s)? Click again`, async () => {
      const names = zNames();
      const moves = chosen.filter((r) => r.first)
        .map((r) => [r.key, suggestion(r, names, !!zLib.alt[r.key]).target]);
      try {
        const out = await zPost("/zotero/file", { moves });
        notify(`Filed ${out.moved} item(s) out of ${col}. /revert undoes it.`, "info");
        zLib.sel.clear();
        drawLibrary();
      } catch (err) { notify("Zotero: " + err.message); }
    });
    bar.appendChild(file);
    bar.appendChild(zEl("span", "zl-hint",
      "each suggestion is the vote of the 3 nearest filed papers; click it for the second choice"));
  } else {
    const out = rows.filter((r) => r.state !== "in");
    const bring = zEl("button", "zl-btn primary", `Bring in ${chosen.length}`);
    bring.type = "button";
    bring.disabled = !chosen.length;
    bring.addEventListener("click", () => zBring(chosen));
    const all = zEl("button", "zl-btn", `Bring in the ${out.length} not in the vault`);
    all.type = "button";
    all.disabled = !out.length;
    all.addEventListener("click", () => zBring(out));
    bar.append(bring, all);
  }
  main.appendChild(bar);

  const table = zEl("div", "zl-table");
  table.setAttribute("role", "table");
  const head = zEl("div", "zl-row zl-th" + (inbox ? " inbox" : ""));
  head.setAttribute("role", "row");
  for (const t of ["", "", "First author", "Year", "Title", inbox ? "Suggested" : "Cited by"]) {
    const c = zEl("span", "", t);
    c.setAttribute("role", "columnheader");
    head.appendChild(c);
  }
  table.appendChild(head);
  const names = zNames();
  for (const r of rows) {
    const tr = zEl("div", "zl-row" + (inbox ? " inbox" : ""));
    tr.setAttribute("role", "row");
    const pickCell = zEl("span");
    if (!readOnly) {
      const box = zEl("input");
      box.type = "checkbox";
      box.checked = zLib.sel.has(r.key);
      box.disabled = !inbox && r.state === "in";
      box.setAttribute("aria-label", "select " + r.title);
      box.addEventListener("change", () => {
        if (box.checked) zLib.sel.add(r.key); else zLib.sel.delete(r.key);
        zRenderTable();
      });
      pickCell.appendChild(box);
    }
    const m = rowMark(r.state);
    const mark = zEl("span", "zl-mark " + m.cls, m.mark);
    mark.title = m.why;
    const title = zEl("button", "zl-open", r.title);
    title.type = "button";
    title.addEventListener("click", () => zOpen(r));
    let last;
    if (inbox && r.first) {
      const sg = suggestion(r, names, !!zLib.alt[r.key]);
      last = zEl("button", "zl-sug");
      last.type = "button";
      last.title = zLib.alt[r.key] ? `second choice; first was ${names[r.first] || r.first}`
        : `${r.votes} of its 3 nearest filed papers are in ${sg.text}; click for the second choice`;
      last.appendChild(zEl("span", "zl-sug-to", "→ " + sg.text));
      last.appendChild(zEl("span", "zl-sug-vote" + (r.votes === 3 && !zLib.alt[r.key] ? " sure" : ""), sg.vote));
      last.addEventListener("click", () => {
        zLib.alt[r.key] = !zLib.alt[r.key];
        zRenderTable();
      });
    } else {
      last = zEl("span", "zl-num", inbox ? "no suggestion" : String(r.cited_by));
    }
    for (const c of [pickCell, mark, zEl("span", "zl-sur", r.sur), zEl("span", "zl-num", r.year), title, last]) {
      c.setAttribute("role", "cell");
      tr.appendChild(c);
    }
    table.appendChild(tr);
  }
  main.appendChild(table);
  main.appendChild(zEl("p", "zl-hint", readOnly
    ? "● in the linked vault · ○ not imported anywhere. Click a title: a paper in the linked vault "
      + "is read from there by peek, one not imported shows its abstract."
    : "● in the vault · ○ not yet · ◐ PDF changed in Zotero since import. Click a title: a paper "
      + "in the vault opens in the Node panel, one not yet shows its abstract."));
}

function zBring(rows) {
  if (!rows.length) return;
  showTab("chat");
  send(bringCommand(rows));
}

async function zOpen(r) {
  if (r.leaf) { showNode({ path: r.leaf }); return; }
  if (r.state === "there") { zPeek(r); return; }
  const side = zLib.side;
  if (!side) return;
  side.innerHTML = "";
  side.appendChild(zEl("p", "zl-hint", "reading Zotero…"));
  let p;
  try {
    const res = await fetch("/zotero/item?key=" + encodeURIComponent(r.key));
    p = await res.json();
    if (!res.ok) throw new Error(p.detail || "HTTP " + res.status);
  } catch (err) { side.innerHTML = ""; side.appendChild(zEl("p", "zl-empty", err.message)); return; }
  side.innerHTML = "";
  const readOnly = zLib.lib && zLib.lib.read_only;
  side.appendChild(zEl("div", "zl-kicker", readOnly ? "Not imported in any vault"
    : r.state === "new" ? "New in Zotero · not in the vault" : "Not in the vault"));
  side.appendChild(zEl("h3", "zl-ptitle", p.title));
  const who = (p.authors || []).map((a) => a.split(",")[0]);
  side.appendChild(zEl("div", "zl-by", [who.length > 3 ? who.slice(0, 3).join(", ") + " et al." : who.join(", "),
    p.year, p.venue].filter(Boolean).join(" · ")));
  if (p.abstract) side.appendChild(zEl("p", "zl-abs", p.abstract));
  const acts = zEl("div", "zl-acts");
  if (!readOnly) {
    const bring = zEl("button", "zl-btn primary", "Bring in");
    bring.type = "button";
    bring.addEventListener("click", () => zBring([r]));
    acts.appendChild(bring);
  }
  const open = zEl("a", "zl-btn", "Open in Zotero");
  open.href = p.zotero;
  acts.appendChild(open);
  side.appendChild(acts);
  if (!p.pdf && !readOnly) side.appendChild(zEl("p", "zl-hint", "No PDF in Zotero: Silica brings in its title, authors and abstract."));
}

// A paper whose leaf lives in the linked vault, read from there and never
// copied here (ADR-0066 decision 1, ADR-0035: peek, not switch).
async function zPeek(r) {
  const side = zLib.side;
  if (!side) return;
  side.innerHTML = "";
  side.appendChild(zEl("p", "zl-hint", "reading the linked vault…"));
  let d;
  try {
    const res = await fetch("/zotero/peek?key=" + encodeURIComponent(r.key));
    d = await res.json();
    if (!res.ok) throw new Error(d.detail || "HTTP " + res.status);
  } catch (err) { side.innerHTML = ""; side.appendChild(zEl("p", "zl-empty", err.message)); return; }
  side.innerHTML = "";
  side.appendChild(zEl("div", "zl-kicker", "In " + d.vault.split("/").pop() + " · read by peek"));
  side.appendChild(zEl("h3", "zl-ptitle", r.title));
  side.appendChild(zEl("div", "zl-by", d.vault + "/…/" + d.leaf));
  side.appendChild(zEl("pre", "zl-peek", d.text));
}

async function zShowGroups() {
  const side = zLib.side;
  if (!side) return;
  side.innerHTML = "";
  side.appendChild(zEl("div", "zl-kicker", "Write groups to Zotero"));
  side.appendChild(zEl("p", "zl-hint", "computing the zones of the papers in this vault…"));
  let d;
  try {
    const r = await fetch("/zotero/groups");
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || "HTTP " + r.status);
  } catch (err) { side.lastChild.textContent = err.message; return; }
  side.lastChild.remove();
  side.appendChild(zEl("p", "", "The semantic zones of the papers in this vault, as subcollections of Silica groups. "
    + "Your own collections are not touched; running it again replaces only what Silica put there."));
  if (!d.groups.length) side.appendChild(zEl("p", "zl-hint", "No paper has notes in a zone yet."));
  for (const g of d.groups) {
    const row = zEl("div", "zl-group");
    row.appendChild(zEl("span", "", g.label));
    row.appendChild(zEl("span", "zl-num", g.now ? `${g.papers} (now ${g.now})` : `+${g.papers}`));
    side.appendChild(row);
  }
  const write = zEl("button", "zl-btn primary");
  write.type = "button";
  write.disabled = !d.groups.length;
  const n = d.groups.reduce((s, g) => s + g.papers, 0);
  zTwoClick(write, "Write groups", () => `Write ${n} membership(s)? Click again`, async () => {
    try {
      const out = await zPost("/zotero/groups");
      notify(`Wrote ${out.groups} group(s) under Silica groups. /revert undoes it.`, "info");
      zSideHint();
    } catch (err) { notify("Zotero: " + err.message); }
  });
  side.appendChild(write);
}

// --- the COVERS pass (ADR-0066 decision 6) -----------------------------------
// Started by the first collection scope whose frame has no fresh pass, polled
// while it runs, and offered as a rebuild once it is done: the chip beside the
// tabs says which (coversCue). `ready` survives until the rebuild is taken.
let zPass = null;
async function coversNeeded() {
  const st = zScopes && zScopes.covers ? zScopes.covers.state : "none";
  if (st === "fresh" || (zPass && (zPass.running || zPass.ready))) return;
  try { zPass = await zPost("/zotero/covers"); } catch (err) { notify("covers: " + err.message); return; }
  zCoversPoll();
}
async function zCoversPoll() {
  while (zPass && zPass.running) {
    if (window.syncRefreshCue) syncRefreshCue();
    await new Promise((ok) => setTimeout(ok, 1500));
    try { zPass = await (await fetch("/zotero/covers")).json(); } catch { /* next tick asks again */ }
  }
  if (zPass && zPass.error) { notify("covers: " + zPass.error); zPass = null; }
  else if (zPass) zPass.ready = true;
  if (window.syncRefreshCue) syncRefreshCue();
}
// A reload during a pass picks the count back up instead of starting another.
async function zCoversResume() {
  try {
    const st = await (await fetch("/zotero/covers")).json();
    if (st.running && !(zPass && zPass.running)) { zPass = st; zCoversPoll(); }
  } catch { /* no server: nothing to resume */ }
}
window.coversNeeded = coversNeeded;
window.currentCoversCue = () => coversCue(zPass);
window.coversTake = () => {
  if (!(zPass && zPass.ready)) return false;
  zPass = null;
  return true;
};

// The frame announces its Zotero scopes on every build; the pickers are rebuilt
// per surface, so the last announcement is kept and replayed into new ones.
let zScopes = null;
window.zoteroCollectionNames = () => ((zScopes && zScopes.collections) || []).map((c) => c.name);
function fillZoteroScopes(groups) {
  if (groups) zScopes = groups;
  if (!zScopes) return;
  // A frame built after the pass draws it, whichever chip rebuilt it.
  if (zPass && zPass.ready && zScopes.covers && zScopes.covers.state === "fresh") {
    zPass = null;
    if (window.syncRefreshCue) syncRefreshCue();
  }
  for (const [id, withPapers] of [["#graph-scope", true], ["#graph-compare", false]]) {
    const sel = document.querySelector(id);
    if (!sel) continue;
    sel.querySelector("optgroup[data-zotero]")?.remove();
    const opts = zoteroScopeOptions(zScopes, withPapers);
    if (!opts.length) continue;
    const g = zEl("optgroup");
    g.label = "Collections";
    g.dataset.zotero = "1";
    for (const [v, t] of opts) { const o = zEl("option", null, t); o.value = v; g.appendChild(o); }
    sel.appendChild(g);
  }
  if (window.syncScopePicker) syncScopePicker();
}
window.addEventListener("message", (e) => {
  if (e.data && e.data.type === "silica-scope-groups") fillZoteroScopes(e.data);
});
window.fillZoteroScopes = fillZoteroScopes;
window.parseScope = parseScope;
window.scopeValue = scopeValue;
window.paperStrip = paperStrip;
window.linkAction = linkAction;
window.paperLabel = paperLabel;
window.paperLabels = {};
// Not named like the pure function: a top-level `function chipLabel` is this
// very property, so the wrapper called itself until the stack overflowed.
window.chipLabel = (path) => chipLabelFor(path, window.paperLabels);
// Labels are read once per page: a paper imported mid-session reads by its
// stem until the next load, and the run's own result card names it anyway.
fetch("/zotero/labels").then((r) => r.json()).then((d) => { window.paperLabels = d || {}; })
  .catch(() => { /* no papers, or no server: chips keep their stems */ });
zoteroBoot();
