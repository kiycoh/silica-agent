// --- path: the reading order around one note (V2, RefD) ----------------------
// The sixth surface, and the only directed one. Rungs are LONGEST-path depth,
// not hop count: on a ladder a shortcut edge would otherwise put a note on the
// same rung as its own prerequisite, and the rung is the whole reading.
//
// Wires are drawn after layout from the chips' own boxes rather than laid out
// in SVG: the chips are text of unpredictable width, and measuring what the
// browser already placed is the only way the two can agree. They are redrawn on
// resize for the same reason.
let pathRootedPath = null;
let pathWireObserver = null;
let pathEdges = [];

function rootPath(path) {
  pathRootedPath = path;
  if (graphMode !== "path") setGraphMode("path");
  else drawPath();
  closeNodeResults();
}

async function drawPath() {
  const pane = $("#shape-pane");
  pane.innerHTML = "";
  $("#shape-loading").hidden = false;
  let d;
  try {
    d = await (await fetch("/path?note=" + encodeURIComponent(pathRootedPath || ""))).json();
  } catch {
    d = { error: "couldn't read the reading order" };
  }
  $("#shape-loading").hidden = true;
  if (graphMode !== "path") return; // the reader switched surface mid-flight
  pane.innerHTML = "";
  // A re-root is a new reading of the vault, so it arrives like a surface does.
  pane.appendChild(renderPath(d)).classList.add("surface-in");
  drawPathWires();
}

function pathRungLabel(depth) {
  if (depth === 0) return "this note";
  if (depth === -1) return "read first";
  if (depth === 1) return "unlocks";
  return depth < 0 ? (-depth) + " steps before" : depth + " steps after";
}

function renderPath(d) {
  const wrap = mkEl("div", "pl-wrap");
  if (d.error) {
    wrap.appendChild(mkEl("p", "pl-empty", d.error));
    return wrap;
  }
  // The landing. A picker of notes to start from, ranked differently: a note
  // roots a ladder by what reads around it, which the file tree cannot say.
  if (d.picks) {
    wrap.appendChild(mkEl("div", "pl-h", "Pick a note to place in its reading order"));
    if (d.hint) wrap.appendChild(mkEl("p", "pl-empty", d.hint));
    if (!d.picks.length && !d.hint) {
      wrap.appendChild(mkEl("p", "pl-empty",
        "no note has a prerequisite yet. RefD only speaks where the co-occurrence "
        + "index has enough related notes to compare."));
    }
    const list = mkEl("div", "pl-picks");
    for (const pk of d.picks) {
      const row = mkEl("button", "pl-pick");
      row.type = "button";
      row.dataset.root = pk.path;
      row.appendChild(mkEl("span", "pl-pick-n", pk.name));
      row.appendChild(mkEl("span", "pl-pick-d",
        pk.before + " before · " + pk.after + " after"));
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  const head = mkEl("div", "pl-h");
  head.appendChild(mkEl("span", "pl-h-t", (d.root && d.root.name) || "reading order"));
  wrap.appendChild(head);
  if (d.hint) {
    wrap.appendChild(mkEl("p", "pl-empty", d.hint));
    return wrap;
  }

  const lad = mkEl("div", "pl");
  const wires = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  wires.setAttribute("class", "pl-wires");
  wires.setAttribute("aria-hidden", "true");
  lad.appendChild(wires);
  for (const lv of d.levels) {
    const rung = mkEl("div", "pl-rung" + (lv.depth === 0 ? " here" : ""));
    rung.appendChild(mkEl("div", "pl-lbl", pathRungLabel(lv.depth)));
    const notes = mkEl("div", "pl-notes");
    for (const n of lv.notes) {
      const chip = mkEl("div", "pl-n" + (n.root ? " root" : "") + (n.cyclic ? " cyc" : ""));
      chip.dataset.path = n.path;
      chip.dataset.plId = n.path;
      chip.appendChild(mkEl("span", "pl-n-t", n.name));
      if (!n.root) {
        const re = mkEl("button", "pl-re", "⤴");
        re.type = "button";
        re.dataset.root = n.path;
        re.title = "place this note in its own reading order";
        notes.appendChild(chip);
        chip.appendChild(re);
      } else {
        notes.appendChild(chip);
      }
    }
    rung.appendChild(notes);
    lad.appendChild(rung);
  }
  wrap.appendChild(lad);

  const foot = [];
  if (d.truncated) foot.push("cut at 60 notes: the ladder continues past this");
  // A cycle is a disagreement the vault really contains, not corrupt data, so
  // it is reported rather than hidden. The edges that close it are dropped from
  // the layout, which is why the count is worth stating at all.
  if (d.cycles) {
    foot.push(d.cycles + (d.cycles === 1 ? " note is" : " notes are")
      + " in a prerequisite cycle: their edges are not drawn");
  }
  if (foot.length) wrap.appendChild(mkEl("p", "pl-foot", foot.join(" · ")));
  pathEdges = d.edges || [];
  return wrap;
}

function drawPathWires() {
  const lad = $("#shape-pane").querySelector(".pl");
  const svg = lad && lad.querySelector(".pl-wires");
  if (!svg) return;
  const box = lad.getBoundingClientRect();
  svg.setAttribute("viewBox", "0 0 " + Math.round(box.width) + " " + Math.round(box.height));
  svg.setAttribute("width", Math.round(box.width));
  svg.setAttribute("height", Math.round(box.height));
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const at = (path) => {
    const el = lad.querySelector('[data-pl-id="' + CSS.escape(path) + '"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left - box.left + r.width / 2, top: r.top - box.top, bot: r.bottom - box.top };
  };
  for (const e of pathEdges) {
    const a = at(e.from), b = at(e.to);
    if (!a || !b) continue;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const y1 = a.bot, y2 = b.top;
    const mid = (y1 + y2) / 2;
    path.setAttribute("d", "M" + a.x + " " + y1 + " C" + a.x + " " + mid
      + " " + b.x + " " + mid + " " + b.x + " " + y2);
    path.setAttribute("class", "pl-wire");
    svg.appendChild(path);
  }
  if (!pathWireObserver && window.ResizeObserver) {
    pathWireObserver = new ResizeObserver(() => {
      if (graphMode === "path") drawPathWires();
    });
    pathWireObserver.observe($("#shape-pane"));
  }
}

$("#shape-pane").addEventListener("click", (e) => {
  const b = e.target.closest("[data-root]");
  if (b) rootPath(b.dataset.root);
});

// --- explore note search (graph: fly the camera · path: root the ladder) -------
// A fuzzy ranked picker over the vault's notes, indexed from the sidebar tree —
// same title→prefix→substring→path ranking the graph viewer's own search uses.
let noteIdx = [];      // [{name, path, ln, lp}]
let nodeResults = [];  // current ranked matches
let nodeSel = -1;

function buildNoteIndex() {
  noteIdx = Array.from($("#tree").querySelectorAll(".tree-note")).map((el) => {
    const name = el.textContent, path = el.dataset.id || "";
    return { name, path, ln: name.toLowerCase(), lp: path.toLowerCase() };
  });
}

function scoreNote(n, q) {
  if (n.ln === q) return 5;
  if (n.ln.startsWith(q)) return 4;
  if (n.ln.includes(q)) return 3;
  if (n.lp.includes(q)) return 2;
  return 0;
}

function renderNodeResults(raw) {
  const q = raw.trim().toLowerCase();
  const box = $("#node-results");
  if (!q) { closeNodeResults(); return; }
  nodeResults = noteIdx
    .map((n) => [scoreNote(n, q), n])
    .filter((p) => p[0] > 0)
    .sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name))
    .slice(0, 12)
    .map((p) => p[1]);
  nodeSel = nodeResults.length ? 0 : -1;
  box.innerHTML = "";
  nodeResults.forEach((n, i) => {
    const el = document.createElement("div");
    el.className = "node-result" + (i === nodeSel ? " sel" : "");
    el.innerHTML = '<span class="nr-name"></span><span class="nr-path"></span>';
    el.querySelector(".nr-name").textContent = n.name;
    el.querySelector(".nr-path").textContent = n.path;
    el.addEventListener("click", () => pickNote(n.path));
    box.appendChild(el);
  });
  box.hidden = nodeResults.length === 0;
}

function closeNodeResults() {
  $("#node-results").hidden = true;
  nodeResults = [];
  nodeSel = -1;
}

function moveNodeSel(d) {
  nodeSel = (nodeSel + d + nodeResults.length) % nodeResults.length;
  document.querySelectorAll("#node-results .node-result").forEach((el, i) => el.classList.toggle("sel", i === nodeSel));
}

function pickNote(path) {
  if (graphMode === "path") {
    rootPath(path);
  } else { // network: locate the note and fly the graph camera to it
    const f = $("#graph-frame");
    if (f.contentWindow) f.contentWindow.postMessage({ type: "silica-goto-path", path }, "*");
  }
  $("#node-search").value = "";
  closeNodeResults();
}

$("#node-search").addEventListener("input", (e) => renderNodeResults(e.target.value));
$("#node-search").addEventListener("keydown", (e) => {
  if (!nodeResults.length) return;
  if (e.key === "ArrowDown") { e.preventDefault(); moveNodeSel(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveNodeSel(-1); }
  else if (e.key === "Enter") { e.preventDefault(); if (nodeSel >= 0) pickNote(nodeResults[nodeSel].path); }
  else if (e.key === "Escape") { $("#node-search").value = ""; closeNodeResults(); }
});


// --- attachments: drop / "+" accumulate files as chips above the input; they
// are NOT nucleated on drop. The next composer submit uploads them together with
// the typed message, so the agent acts on the files per the user's instruction.
let staged = []; // File objects awaiting the next submit
const attachEls = $("#attachments");

function renderAttachments() {
  attachEls.innerHTML = "";
  attachEls.hidden = staged.length === 0;
  staged.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span class="chip-name"></span><button type="button" class="chip-x" title="remove">✕</button>`;
    chip.querySelector(".chip-name").textContent = f.name;
    chip.querySelector(".chip-x").addEventListener("click", () => { staged.splice(i, 1); renderAttachments(); });
    attachEls.appendChild(chip);
  });
}
function addFiles(fileList) {
  // A mail export is not a chip: the import has a dry run and keeps only the
  // threads the user took part in (ADR-0090 point 5), and the composer has
  // neither. Dropped here it wrote a note /revert could not find (2026-10-12).
  const mail = [];
  for (const f of withinUploadCap(fileList)) (/\.(eml|mbox)$/i.test(f.name) ? mail : staged).push(f);
  if (mail.length) {
    notify(`${mail.map((f) => f.name).join(", ")}: mail is imported in Settings › Connectors › Mail, `
      + "or with /mail import <file>, which show a dry run first", "warn");
  }
  renderAttachments();
}

// The edge's cap on one request, on the server only (/supported_types): a file
// over it never reaches silica, and the edge answers with a page of its own
// (ADR-0099 Consequences). Refused here, before a byte is sent, with the
// reason; 0 is a laptop, where nothing stands in between.
let uploadMaxMb = 0;
function withinUploadCap(files) {
  const list = [...files];
  if (!uploadMaxMb) return list;
  const big = list.filter((f) => f.size > uploadMaxMb * 1024 * 1024);
  if (big.length) {
    notify(`${big.map((f) => f.name).join(", ")}: over the ${uploadMaxMb} MB a file can be to reach the `
      + "server. Split a long recording into parts, or export it at a lower bitrate.", "error");
  }
  return list.filter((f) => !big.includes(f));
}

// Upload every staged file + the typed text as one turn (server stages them in
// the chat's own folder, ADR-0121; a file to convert becomes a job, and the
// question waits for it when there is one).
function nucleateStaged(text) {
  if (streaming || !staged.length) return;
  const names = staged.map((f) => f.name);
  const body = bubble("user");
  body.textContent = text.trim();
  body.dataset.words = text.trim();
  userFiles(body, names.map((name) => ({ name })));
  const fd = new FormData();
  for (const f of staged) fd.append("files", f);
  fd.append("text", text);
  staged = [];
  renderAttachments();
  // The turn names the files still converting; their rows come from the list,
  // read now rather than waiting on the stream's first event (ADR-0099).
  const sent = fetch("/nucleate", { method: "POST", body: fd }).then((r) => {
    if (r.headers.get("X-Silica-Jobs")) jobsFetch();
    return r;
  });
  runTurn(sent, "staging " + names.length + (names.length === 1 ? " file" : " files"));
}

// --- the files a message carried (ADR-0121 point 4) --------------------------
// A chip per file under the words, live and replayed: the name opens the file's
// note, and "add to vault" copies it into the Inbox (point 5). Until the turn's
// `files` event names the file's place in the chat, a live chip is a name only.
function userFiles(body, files) {
  if (!body || !files || !files.length) return;
  const msg = body.closest(".msg");
  msg.querySelector(":scope > .msg-files")?.remove();
  const row = document.createElement("div");
  row.className = "msg-files";
  for (const f of files) {
    const chip = document.createElement("span");
    chip.className = "mf";
    const name = document.createElement("button");
    name.type = "button";
    name.className = "mf-name";
    name.textContent = f.name;
    const notes = f.notes || [];
    name.disabled = !notes.length;
    name.title = notes.length ? "open " + f.name : f.name;
    name.addEventListener("click", () => notes.length && openNote(notes[0]));
    chip.appendChild(name);
    if (f.in_vault) chip.appendChild(mkEl("span", "mf-in", "in the vault"));
    else if (f.source && notes.length) chip.appendChild(addToVaultButton(f.source, chip));
    row.appendChild(chip);
  }
  body.after(row);
}

function addToVaultButton(source, chip) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "mf-add linklike";
  b.textContent = "add to vault";
  b.title = "copy this file and its notes into the Inbox; the chat keeps its own";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await fetch("/chat/files/add", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { notify(typeof d.detail === "string" ? d.detail : `the server answered ${r.status}`); b.disabled = false; return; }
      b.replaceWith(mkEl("span", "mf-in", "in the vault"));
      announce(`${source.split("/").pop()} added to the Inbox`);
      loadVaultInfo();
      markVaultChanged();
    } catch { notify("the server could not be reached"); b.disabled = false; }
  });
  return b;
}

// The formats the chat model's projector takes (image_readings._MIME).
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"];
function stagedAreImages() {
  return staged.length > 0 && staged.every((f) => IMAGE_TYPES.includes(f.type));
}

// Images alone go with the message itself, as data URLs the server saves outside
// the vault (ADR-0077 point 3): asking about a screenshot must not file it.
async function lookStaged(text) {
  if (streaming || !staged.length) return;
  const files = staged;
  staged = [];
  renderAttachments();
  const images = await Promise.all(files.map((f) => new Promise((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok({ name: f.name, data: r.result });
    r.onerror = () => fail(r.error);
    r.readAsDataURL(f);
  })));
  send(text, false, images);
}

// A pasted screenshot is an attachment like a dropped one.
input.addEventListener("paste", (e) => {
  const files = Array.from(e.clipboardData?.files || []).filter((f) => IMAGE_TYPES.includes(f.type));
  if (files.length) { e.preventDefault(); addFiles(files); }
});

// Only files over the chat view are staged: the rail and the skill rows take
// drops of their own, and a tree row dragged anywhere carries no files, so the
// hint would promise a nucleate that drop never does. dragover decides the hint
// because it names the element under the cursor; the depth count only notices
// the drag leaving the window, where dragover stops firing.
const chatView = $("#view-chat");
const filesOverChat = (e) => e.dataTransfer.types.includes("Files") && chatView.contains(e.target);
// Tree rows over the chat (ADR-0126 point 5): notes write their links into the
// composer, a folder has no token there and shows no drop.
const rowsOverChat = (e) => e.dataTransfer.types.includes(NOTE_DRAG) && chatView.contains(e.target);
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; });
window.addEventListener("dragover", (e) => {
  // Cancelled everywhere, or a file let go off the chat opens in the tab. A
  // target that took a tree row (a folder, a workspace row, the editor) set
  // its own effect first and is left alone.
  const taken = e.defaultPrevented;
  e.preventDefault();
  document.body.classList.toggle("dragging", filesOverChat(e));
  if (!taken && e.dataTransfer.types.includes(WS_DRAG)) e.dataTransfer.dropEffect = rowsOverChat(e) ? "copy" : "none";
});
window.addEventListener("dragleave", (e) => { e.preventDefault(); if (--dragDepth <= 0) document.body.classList.remove("dragging"); });
window.addEventListener("drop", (e) => {
  // A target that took the files (a workspace row, a skill row) prevented the
  // default first: staging them for the chat too would import them twice.
  const taken = e.defaultPrevented;
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (taken) return;
  if (filesOverChat(e) && e.dataTransfer.files.length) { addFiles(e.dataTransfer.files); return; }
  if (rowsOverChat(e)) {
    let it = null;
    try { it = JSON.parse(e.dataTransfer.getData(WS_DRAG) || "null"); } catch { /* not ours */ }
    if (it && Array.isArray(it.notes)) insertLinks(it.notes);
  }
});

// "+" opens what the next message can do (ADR-0083). Its first entry is the
// native picker, constrained to what the nucleate lanes accept.
const nucleateInput = $("#nucleate-file");
fetch("/supported_types")
  .then((r) => r.json())
  // Images are offered even when no converter reads them: they go to the chat.
  .then((d) => {
    nucleateInput.accept = [...new Set([...(d.extensions || []), ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"])].join(",");
    uploadMaxMb = d.upload_max_mb || 0;
  })
  .catch(() => {}); // accept="" just means the picker shows all files
const attachMenu = $("#attach-menu");
const closeAttachMenu = menuButton(attachMenu, $("#attach"));
attachMenu.addEventListener("click", (e) => {
  const item = e.target.closest('[role="menuitem"]');
  if (!item) return;
  if (item.id === "am-files") { nucleateInput.click(); return; }
  // ADR-0127: the words in the box become the new skill's instructions, written
  // and previewed in Settings › Skills, where an installed one is managed.
  if (item.id === "am-skill") { skWriteSkill(input.value); return; }
  if (item.id === "am-plan") {
    // lrPlanFolder's three calls with nothing filled in: Create plan opens on
    // its sentence (ADR-0081 §10), and no note is carried over (ADR-0083).
    lrFormReset();
    showTab("learn");
    lrGo({ view: "new" });
    return;
  }
  // Swap a leading command and keep the text. The input event is what runs
  // the band, the ghost, autosize and the draft, so the box reads as typed.
  input.value = item.dataset.action + " " + input.value.replace(/^\/[a-z-]*\s*/, "");
  input.focus();
  input.dispatchEvent(new Event("input"));
});
nucleateInput.addEventListener("change", () => {
  addFiles(nucleateInput.files);
  nucleateInput.value = ""; // reset so re-picking the same file fires change again
});

// --- the vault's + (ADR-0098) ---------------------------------------------------
// Files, a folder or a link into the Inbox, each through its lane, and no
// agent turn: a folder the student only wants searchable should not spend a
// turn. Two picker entries because one browser picker takes files or a folder,
// never both; the link is typed (ADR-0120).
const vaultAddBtn = $("#vault-add");
const vaultAddMenu = $("#vault-add-menu");
const vaultFilesInput = $("#vault-files");
const vaultFolderInput = $("#vault-folder");
const vaultImportBox = $("#vault-import");
// The server's cap per request (_UPLOAD_MAX_FILES), and ADR-0098 H2's cap per
// import, the free tier's note count: past it a folder is a job, not a pick.
const VAULT_IMPORT_BATCH = 32;
// The workspace whose + opened the picker (ADR-0112 point 6): each imported
// note joins it. Cleared by the import that uses it and by a click on the
// vault's own +, which imports into no workspace.
let vaultImportWorkspace = "";
$("#vault-add").addEventListener("click", (e) => { if (e.isTrusted) vaultImportWorkspace = ""; }, true);
const VAULT_IMPORT_MAX = 500;
// From /supported_types. A folder picker ignores `accept`, so the same list
// filters what a folder hands over before anything is sent; while it is empty
// nothing is filtered here, and the server's own check still holds.
let vaultImportTypes = [];
fetch("/supported_types")
  .then((r) => r.json())
  .then((d) => { vaultImportTypes = d.import || []; vaultFilesInput.accept = vaultImportTypes.join(","); })
  .catch(() => {});

// Fixed under the +, not inside the drawer: the drawer scrolls and clips, and
// a shut Vault hides everything but its title.
function placeVaultAddMenu() {
  const r = vaultAddBtn.getBoundingClientRect();
  vaultAddMenu.style.top = `${Math.round(r.bottom + 4)}px`;
  vaultAddMenu.style.right = `${Math.max(8, Math.round(innerWidth - r.right))}px`;
}
const closeVaultAddMenu = menuButton(vaultAddMenu, vaultAddBtn, placeVaultAddMenu);
// No folder picker in this browser: the entry would open a file picker.
if (!("webkitdirectory" in HTMLInputElement.prototype)) $("#vam-folder").hidden = true;
vaultAddMenu.addEventListener("click", (e) => {
  const item = e.target.closest('[role="menuitem"]');
  if (item?.id === "vam-files") vaultFilesInput.click();
  if (item?.id === "vam-folder") vaultFolderInput.click();
  if (item?.id === "vam-link") askVaultLink();
});

// A web page into the Inbox, as /fetch does (ADR-0120). The address is typed
// in the import's own box, which then shows the outcome as it shows a file's.
// Text, not type=url: `en.wikipedia.org/wiki/X` is how people type an
// address, and the server adds the https.
function askVaultLink() {
  const intoWorkspace = vaultImportWorkspace;
  vaultImportWorkspace = "";
  closeVaultAddMenu(true);
  showSidePane("side-vault"); // the import reports in the notes
  vaultImportBox.hidden = false;
  vaultImportBox.replaceChildren();
  const form = document.createElement("form");
  form.className = "vi-link";
  const field = document.createElement("input");
  Object.assign(field, { type: "text", inputMode: "url", autocomplete: "off", placeholder: "https://…" });
  field.setAttribute("aria-label", "web address of the page to add");
  const go = document.createElement("button");
  Object.assign(go, { type: "submit", className: "linklike", textContent: "add" });
  const x = document.createElement("button");
  Object.assign(x, { type: "button", className: "vi-x", textContent: "✕" });
  x.setAttribute("aria-label", "dismiss");
  x.addEventListener("click", () => { vaultImportBox.hidden = true; });
  field.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    e.stopPropagation(); // the app's Escape would go on to close the drawer
    vaultImportBox.hidden = true;
  });
  form.append(field, go, x);
  vaultImportBox.append(form);
  field.focus();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = field.value.trim();
    if (!url) return;
    renderVaultImport({ progress: "reading the page…" });
    let rows = [];
    let failed = "";
    try {
      const r = await fetch("/vault/fetch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, workspace: intoWorkspace }),
      });
      if (r.ok) rows = (await r.json()).files;
      else failed = (await r.json().catch(() => ({}))).detail || `the server answered ${r.status}`;
    } catch {
      failed = "the server could not be reached";
    }
    vaultImportLast = { rows, skipped: [], failed, folder: "", big: 0 };
    renderVaultImport(vaultImportLast);
    if (rows.some((r) => r.status === "note")) {
      loadVaultInfo();
      if (intoWorkspace) loadWorkspaces();
    }
  });
}
// Its own Escape: the app's one list (app-settings.js) would go on to close
// the drawer, and the student only meant the menu.
vaultAddMenu.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  e.stopPropagation();
  closeVaultAddMenu(true);
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#vault-add-menu, #vault-add")) closeVaultAddMenu();
});
vaultFilesInput.addEventListener("change", () => {
  const files = [...vaultFilesInput.files];
  vaultFilesInput.value = ""; // the same pick again must fire change again
  importToVault(files, false);
});
vaultFolderInput.addEventListener("change", () => {
  const files = [...vaultFolderInput.files];
  vaultFolderInput.value = "";
  importToVault(files, true);
});

async function importToVault(files, fromFolder) {
  // A File from a folder carries its path under the picked folder; Safari can
  // hand over single files in folder mode, and those go in as files.
  const relOf = (f) => (fromFolder && f.webkitRelativePath) || "";
  const extOf = (name) => (name.match(/\.[^./]+$/) || [""])[0].toLowerCase();
  const intoWorkspace = vaultImportWorkspace;
  vaultImportWorkspace = "";
  const skipped = [];
  const wanted = files.filter((f) => {
    const path = relOf(f) || f.name;
    // .git, .obsidian, .silica: the folders the index walk skips too
    const ok = !path.split("/").some((s) => s.startsWith("."))
      && (!vaultImportTypes.length || vaultImportTypes.includes(extOf(f.name)));
    if (!ok) skipped.push(path);
    return ok;
  });
  const fits = withinUploadCap(wanted);
  const big = wanted.length - fits.length;
  wanted.splice(0, wanted.length, ...fits);
  const folder = fromFolder && wanted[0] ? relOf(wanted[0]).split("/")[0] : "";
  showSidePane("side-vault"); // the import reports in the notes
  if (wanted.length > VAULT_IMPORT_MAX) {
    renderVaultImport({ failed: `${wanted.length} files Silica reads are in it, and one import takes at most ${VAULT_IMPORT_MAX}: pick a smaller folder` });
    return;
  }
  const rows = [];
  let failed = "";
  for (let i = 0; i < wanted.length; i += VAULT_IMPORT_BATCH) {
    renderVaultImport({ progress: `importing ${i} of ${wanted.length}…` });
    const fd = new FormData();
    for (const f of wanted.slice(i, i + VAULT_IMPORT_BATCH)) {
      fd.append("files", f, f.name);
      if (fromFolder) fd.append("paths", relOf(f));
    }
    if (i + VAULT_IMPORT_BATCH >= wanted.length) fd.append("last", "1");
    if (intoWorkspace) fd.append("workspace", intoWorkspace);
    let r;
    try {
      r = await fetch("/vault/import", { method: "POST", body: fd });
    } catch {
      failed = "the server could not be reached";
      break;
    }
    if (!r.ok) {
      failed = (await r.json().catch(() => ({}))).detail || `the server answered ${r.status}`;
      break;
    }
    rows.push(...(await r.json()).files);
  }
  vaultImportLast = { rows, skipped, failed, folder, big };
  renderVaultImport(vaultImportLast);
  if (intoWorkspace) loadWorkspaces();
  // A PDF converts in milliseconds: its job's last event can arrive before this
  // answer did, so each row takes the state the page already holds for it.
  for (const r of rows) if (r.job && jobsById.has(r.job)) vaultImportJob(jobsById.get(r.job));
  if (rows.length) loadVaultInfo();
}

// A file that needs converting answers `queued` with its job (ADR-0099); its
// row turns into a note, or a refusal, when the job ends. The last import's
// box is the one kept up to date: an earlier one was dismissed or replaced.
let vaultImportLast = null;
function vaultImportJob(j) {
  const row = vaultImportLast && vaultImportLast.rows.find((r) => r.job === j.id);
  if (!row || (j.state !== "done" && j.state !== "failed" && j.state !== "interrupted")) return;
  if (j.state === "done") Object.assign(row, { status: "note", notes: j.notes, hidden: j.hidden && j.hidden.length ? j.hidden : undefined,
    pages_not_read: j.pages_not_read || undefined });
  else Object.assign(row, { status: "refused", reason: j.error });
  if (!vaultImportBox.hidden) renderVaultImport(vaultImportLast);
  loadVaultInfo();
}

// One line of counts with its two actions, then the files that need a word:
// refused with their reason, hidden text left out (ADR-0098 H6), and pages
// with no text that no model read (ADR-0087 point 3).
function renderVaultImport({ progress = "", rows = [], skipped = [], failed = "", folder = "", big = 0 }) {
  vaultImportBox.hidden = false;
  vaultImportBox.replaceChildren();
  const head = document.createElement("div");
  head.className = "vi-head";
  vaultImportBox.append(head);
  if (progress) {
    head.textContent = progress;
    return;
  }
  const done = rows.filter((r) => r.status === "note");
  const refused = rows.filter((r) => r.status === "refused");
  const converting = rows.filter((r) => r.status === "queued");
  const notTaken = skipped.length + rows.filter((r) => r.status === "skipped").length;
  const where = folder ? `${(done[0]?.path || converting[0]?.path || "").split("/")[0] || "Inbox"}/${folder}` : "the Inbox";
  const parts = [done.length ? `${done.length} imported into ${where}` : converting.length ? `into ${where}` : "nothing imported"];
  if (converting.length) parts.push(`${converting.length} converting`);
  if (notTaken) parts.push(`${notTaken} not a type Silica reads`);
  if (big) parts.push(`${big} over ${uploadMaxMb} MB`);
  if (refused.length) parts.push(`${refused.length} refused`);
  const text = document.createElement("span");
  text.textContent = (failed ? `import stopped: ${failed}. ` : "") + parts.join(" · ");
  head.append(text);
  // Offered once nothing converts: a /nucleate of a folder still converting
  // would convert its files a second time, inside the turn.
  if (done.length && !converting.length) {
    const go = document.createElement("button");
    go.type = "button";
    go.className = "linklike";
    go.textContent = "nucleate them";
    go.title = "prefill /nucleate in the chat with what was imported";
    go.addEventListener("click", () => {
      const target = folder ? where : done.flatMap((r) => r.notes || []).join(" ");
      showTab("chat");
      input.value = `/nucleate ${target}`;
      input.focus();
      input.dispatchEvent(new Event("input"));
    });
    head.append(go);
  }
  const x = document.createElement("button");
  x.type = "button";
  x.className = "vi-x";
  x.setAttribute("aria-label", "dismiss");
  x.textContent = "✕";
  x.addEventListener("click", () => { vaultImportBox.hidden = true; vaultImportLast = null; });
  head.append(x);
  const notes = [
    ...refused.map((r) => `${r.name}: ${r.reason}${r.detail ? ` (${r.detail})` : ""}`),
    ...rows.filter((r) => r.hidden).flatMap((r) => r.hidden.map((h) =>
      `${r.name}, page ${h.page}: ${h.characters} characters of hidden text left out`)),
    ...rows.filter((r) => r.pages_not_read).map((r) => `${r.name}: ${r.pages_not_read}`),
  ];
  if (notes.length) {
    const list = document.createElement("ul");
    for (const n of notes) {
      const li = document.createElement("li");
      li.textContent = n;
      list.append(li);
    }
    vaultImportBox.append(list);
  }
}

// --- note panel (right overlay drawer; opens from .note-link and the graph) ---
const notePanel = $("#note-panel");
let lastNotePath = null;   // note currently open in the drawer
let lastViewedPath = null; // survives close — feeds the corner toggle

// The dock inset and the drawer width must agree; CSS reads it as --note-w.
function setNoteW(w) {
  document.documentElement.style.setProperty("--note-w", w + "px");
  syncDrawerToViews(); // the frame parks its focus bar against the drawer's edge
}

// Mirror the open note onto the graph iframe, which arranges it and what it
// lights as the Map (ADR-0088). No-op harmlessly if
// a tab was never opened (contentWindow still exists, message just has no
// listener yet).
function postToViews(msg) {
  const frame = $("#graph-frame");
  if (frame.contentWindow) frame.contentWindow.postMessage(msg, "*");
}
// The last focus INTENT, replayed whenever a view (re)loads. A frame that is
// still loading drops the message, and /graph is rebuilt on every entry into
// the explore tab — so "light these notes" issued from the chat tab used to be
// posted into a loading iframe and then overwritten by the load handler.
let graphFocus = [];
function focusGraphNode(path) {
  graphFocus = path ? [path] : [];
  replayGraphFocus();
}
// Same, for a SET: a concept lights every note that carries it.
function focusGraphNodes(paths) {
  graphFocus = paths || [];
  replayGraphFocus();
}
// Both shapes, in order: the set arrives second, so it wins.
function replayGraphFocus() {
  postToViews({ type: "silica-focus-path", path: graphFocus[0] || null });
  if (graphFocus.length > 1) postToViews({ type: "silica-focus-paths", paths: graphFocus });
}

// Explore does not inset for the drawer the way chat does — the graph keeps its
// full width and the drawer overlays it, so the frame's own HUD sits under a
// translucent panel and reads through the note. The frame cannot see the
// drawer, so tell it. Replayed on frame load like the focus state, because
// /graph is rebuilt on every entry into the tab.
function syncDrawerToViews() {
  // The view ends a gutter before the note now (app-shell.css body.note-open
  // .view), so the frame is never under the drawer and has nothing to yield:
  // it is told "closed" whatever the drawer does. The message stays because an
  // exported graph.html still listens for it, and a stale `open` from before
  // this change would hide its HUD for good.
  postToViews({ type: "silica-host-drawer", open: false, width: 0 });
}

// The dock floats over the foot of the graph frame, which parks its
// corner bars above it (graph-frame.css --foot). Its height is not one number:
// a run line, the answer card and a multi-row draft stack it taller during a
// turn. Folded and open it is the same height (app-panels.css), so a hover
// posts nothing. Replayed on frame load with the drawer, for the same reason.
function syncDockToViews() {
  // `lift`: under 912px of dock the composer is not capped between the frame's
  // foot groups (app-panels.css, ADR-0072), so they park above it. Decided here,
  // on the dock's own width, the box the container query measures: the frame
  // measuring its own width disagreed with it by the rail's width at 1000px.
  const dock = $("#dock");
  postToViews({ type: "silica-host-dock", h: dock.offsetHeight, lift: dock.offsetWidth < 912 });
}
new ResizeObserver(syncDockToViews).observe($("#dock"));

// The explore view's right column (search, scope, compare) stands over the
// frame's key, which starts under it (ADR-0072); its height changes with the
// surface and with the pickers, so the frame is told, as it is of the dock.
function syncSideToViews() {
  const side = $("#graph-side");
  postToViews({ type: "silica-host-side", h: side.hidden ? 0 : side.offsetHeight });
}
new ResizeObserver(syncSideToViews).observe($("#graph-side"));

// Mermaid is a 3.5MB vendored bundle, so it loads on demand — only the first
// time an opened note actually contains a ```mermaid fence. Render failures
// leave the fence as plain text (suppressErrorRendering).
let mermaidLoad = null;

// Read the palette instead of restating it. The old block carried five hex
// literals copied out of app-base.css, and by the time anyone looked they were two
// revisions stale — which is the failure mode a second theme turns from a
// blemish into a wrong-coloured diagram on a white page. getComputedStyle on
// :root returns whichever ramp is live, so this cannot drift from either.
// `base` in both themes, never "dark"/"default". Mermaid derives a theme object
// on the first initialize() and a later one does not rebuild it: switching
// theme:"dark" -> theme:"default" re-rendered every diagram in mermaid's own
// lavender defaults with our themeVariables sitting in the live config, ignored.
// `base` is the theme that exists to be driven entirely by those variables, so
// nothing has to be re-derived and there is one code path instead of two.
//
// The look is the diagram theme of ADR-0069, the same one the graph's Map wears:
// a diagram sits on the reading surface with no frame of its own, a node is a
// raised fill with a crisp edge, a line is a neutral the eye can follow, and
// colour is spent on the few marks a reader starts from. Names are set in the
// prose face: one left-to-right flowchart of the test vault drew 1409 units
// wide in Martian Mono at 16px and 1064 in Archivo at 14px, and the 465px
// drawer had shrunk the first to 5.3px type (measured 2026-10-06).
function mermaidConfig() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  const light = document.documentElement.dataset.theme === "light";
  // The surface a diagram lands on (the view and the drawer), and the step a
  // node rises to. The old pair was --slate-2 nodes on that --sheet surface,
  // 1.00:1 apart, so every box vanished and only its label was left; --bubble
  // is 1.18:1 off it and the --ash-dim edge (5.44:1) draws the box.
  const surface = v("--sheet"), node = v("--bubble");
  // The ordinal chart ramp, emphatic end first: a pie's biggest slice and a
  // mindmap's root carry the signal hue, the rest step down the same hue.
  // Beyond five a slice is a neutral, never a sixth generated hue.
  const ramp = ["--c-ord-4", "--c-ord-3", "--c-ord-2", "--c-ord-1", "--c-track"].map(v);
  const tail = [v("--ash-dim"), v("--line-2"), v("--ash"), v("--line")];
  const pie = Object.fromEntries([...ramp, ...tail, ...tail].slice(0, 12).map((c, i) => ["pie" + (i + 1), c]));
  // mindmap and timeline sections: two quiet steps of the hue, alternating,
  // under the ink that clears both (computed 2026-10-06: --frost on them 5.33
  // and 7.51:1 on graphite, 7.18 and 13.34:1 on paper).
  const sect = [v("--c-ord-1"), v("--c-track")];
  const cScale = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [
    ["cScale" + i, sect[i % 2]], ["cScaleLabel" + i, v("--frost")]]).flat());
  return {
    startOnLoad: false, suppressErrorRendering: true, theme: "base",
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif",
    fontSize: 14,
    flowchart: { curve: "basis", padding: 14 },
    themeVariables: {
      darkMode: !light,
      fontSize: "14px",
      background: surface,
      primaryColor: node,
      primaryTextColor: v("--frost"),
      primaryBorderColor: v("--ash-dim"),
      lineColor: v("--ash-dim"),
      // base derives the rest from these four, and derives them wrong when the
      // ramp is not a neutral gray: naming them is cheaper than correcting it.
      secondaryColor: node,
      tertiaryColor: surface,
      mainBkg: node,
      nodeBorder: v("--ash-dim"),
      textColor: v("--text"),
      titleColor: v("--frost"),
      edgeLabelBackground: surface,
      clusterBkg: surface,
      clusterBorder: v("--line-2"),
      // sequence: the lifeline's own activity in the signal hue's deep step,
      // notes as a quiet aside instead of mermaid's sticky-note yellow
      actorBkg: node, actorBorder: v("--ash-dim"), actorTextColor: v("--frost"),
      actorLineColor: v("--line-2"), signalColor: v("--ash"), signalTextColor: v("--text"),
      labelBoxBkgColor: node, labelBoxBorderColor: v("--line-2"), labelTextColor: v("--text"),
      loopTextColor: v("--ash"), noteBkgColor: v("--slate-2"), noteBorderColor: v("--line-2"),
      noteTextColor: v("--text"), activationBkgColor: v("--c-track"), activationBorderColor: v("--c-ord-2"),
      sequenceNumberColor: v("--on-vivid"),
      // state + ER
      labelBackgroundColor: surface, transitionColor: v("--ash-dim"), transitionLabelColor: v("--ash"),
      stateBkg: node, stateBorder: v("--ash-dim"), specialStateColor: v("--accent"),
      innerEndBackground: v("--accent"), compositeBackground: surface, altBackground: v("--slate-2"),
      // base falls back to the FILL for a state's label colour, which is why
      // every state name vanished; and ER's rows read rowOdd/rowEven, not the
      // attribute pair, in mermaid 11
      stateLabelColor: v("--frost"),
      attributeBackgroundColorOdd: surface, attributeBackgroundColorEven: node,
      rowOdd: surface, rowEven: node,
      // pie: a 2px surface gap between slices instead of a black outline
      ...pie, pieStrokeColor: surface, pieStrokeWidth: "2px", pieOuterStrokeWidth: "0px",
      pieTitleTextColor: v("--frost"), pieSectionTextColor: v("--frost"),
      pieLegendTextColor: v("--text"), pieOpacity: "1",
      ...cScale,
      // gantt
      sectionBkgColor: node, altSectionBkgColor: surface, sectionBkgColor2: node,
      taskBkgColor: v("--c-ord-1"), taskBorderColor: v("--c-ord-2"), taskTextColor: v("--frost"),
      activeTaskBkgColor: v("--c-ord-3"), activeTaskBorderColor: v("--c-ord-4"),
      doneTaskBkgColor: v("--line-2"), doneTaskBorderColor: v("--ash-dim"),
      critBkgColor: v("--c-amber"), critBorderColor: v("--warn"), todayLineColor: v("--accent"),
      gridColor: v("--line"),
    },
    // Per-shape treatment, as CSS so it reads the live tokens. Mermaid scopes
    // every rule here to the one diagram's id, which outranks its own sheet.
    // Shapes are told apart by element, which is the only handle mermaid 11
    // gives: a decision is a polygon, a store a path, a terminal a stadium.
    themeCSS: `
      .node rect { rx: 6px; ry: 6px; }
      .node rect, .node polygon, .node path, .node circle { stroke-width: 1px; }
      .node polygon { fill: color-mix(in srgb, var(--accent) 12%, var(--sheet)); stroke: var(--accent); }
      .node path.outer-path { fill: color-mix(in srgb, var(--frost) 6%, var(--sheet)); }
      .node g.label-container.outer-path path { stroke: var(--accent); }
      .node circle.state-start { fill: var(--accent); stroke: none; }
      .flowchart-link, .transition, .relationshipLine, .messageLine0, .messageLine1 { stroke-width: 1.2px; }
      .edgeLabel, .edgeLabel p, .labelBkg { background-color: var(--sheet); color: var(--ash); font-size: 12px; }
      .cluster rect { fill: color-mix(in srgb, var(--frost) 3%, transparent); stroke: var(--line-2);
                      stroke-dasharray: 4 3; rx: 10px; ry: 10px; }
      .cluster-label .nodeLabel, .cluster-label span { color: var(--ash-dim); font-size: 12px; font-weight: 600; }
      .nodeLabel, .label { font-weight: 500; }
      [class*="section-edge-"] { stroke: var(--line-2); stroke-width: 2px; }
      .mindmap-node .node-line- { stroke: none; }
      .section-root circle, .section--1 circle { fill: var(--accent); }
      .section-root .nodeLabel, .section--1 .nodeLabel { color: var(--on-vivid); }
      .pieTitleText { font-size: 15px; font-weight: 600; }
      .slice { fill: var(--frost); paint-order: stroke; stroke: var(--sheet); stroke-width: 3px; }
      .legend text { font-size: 13px; }
    `,
  };
}

// A diagram shrinks to its column, but not past three quarters of its own
// size: the face change alone took the flowchart above from 5.3px to 6.1px,
// and the floor takes it to 10.5px. Past the floor it scrolls sideways, the
// way a wide code block already does in the same <pre>.
function floorMermaid(blocks) {
  blocks.forEach((b) => {
    const svg = b.querySelector(":scope > svg");
    const w = svg && svg.viewBox && svg.viewBox.baseVal ? svg.viewBox.baseVal.width : 0;
    if (w) svg.style.minWidth = Math.round(w * 0.75) + "px";
  });
}

function loadMermaid() {
  return (mermaidLoad ||= new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = "/static/mermaid.min.js";
    s.onload = () => { mermaid.initialize(mermaidConfig()); resolve(); };
    document.head.appendChild(s);
  }));
}

function renderMermaid(root) {
  const blocks = root.querySelectorAll("pre.mermaid");
  if (!blocks.length) return Promise.resolve();
  // Keep the source: mermaid.run() replaces the fence's text with an <svg>, so
  // without this a repaint has nothing left to re-render from.
  blocks.forEach((b) => { if (!b.dataset.mmd) b.dataset.mmd = b.textContent; });
  return loadMermaid().then(() => mermaid.run({ nodes: blocks }).catch(() => {}).then(() => floorMermaid(blocks)));
}

// A diagram is baked SVG, so it does not follow a token swap the way the rest
// of the page does — every rendered fence has to be rewound to its source and
// drawn again. Nothing to do if the bundle was never loaded.
function repaintMermaid() {
  if (!mermaidLoad) return;
  mermaidLoad.then(() => {
    mermaid.initialize(mermaidConfig());
    const done = document.querySelectorAll("pre.mermaid[data-processed]");
    if (!done.length) return;
    done.forEach((b) => {
      b.removeAttribute("data-processed");
      b.textContent = b.dataset.mmd || "";
    });
    mermaid.run({ nodes: done }).catch(() => {}).then(() => floorMermaid(done));
  });
}

// The transcript is what the drawer exists to serve, so it gets a floor and the
// two panes beside it negotiate around it.
//
// The old rule was a fixed 1100px breakpoint, and it made the layout NON-MONOTONIC:
// below 1100 the sidebar yielded, but at 1200 it stayed (264px) while the drawer
// kept its full 630, leaving 306px of log — measured at 21 characters of prose per
// line, worse than at the 900px floor. The window got bigger and the transcript got
// smaller. 1280 and 1366 are the two commonest laptop widths after 1440, so the
// broken band was the common case.
const MIN_PROSE = 560;   // px of transcript that must survive, ~47ch of prose once
                         // the sheet's and the row's padding come out of it
const MIN_DRAWER = 320;  // below this the drawer stops being worth its own pane
let sidebarYielded = false;

// The rail is draggable, so its width is read and never assumed: a constant
// here would go stale the first time anyone touched the handle, and the drawer
// would size itself against a rail that is no longer that wide.
function sideW() {
  return parseInt(getComputedStyle(document.documentElement)
    .getPropertyValue("--side-w"), 10) || 264;
}

// The canvas gutters between and around the three surfaces (app-base.css
// --gutter). Four of them stand between the window's edges and the prose when
// rail and note are both open, so the budget below has to give them up too.
function gutters() {
  return 4 * (parseInt(getComputedStyle(document.documentElement)
    .getPropertyValue("--gutter"), 10) || 12);
}

// What the drawer may take before the transcript drops below its floor.
// Collapsed, the drawer is still the 52px rail (ADR-0084), and its gutter stays.
function drawerBudget(sidebarOn) {
  return window.innerWidth - (sidebarOn ? sideW() : 52) - MIN_PROSE - gutters();
}

// Single owner of the open-drawer layout: decides whether the sidebar can stay,
// then sizes the drawer to whatever is left over the floor. Runs on open, on
// resize and after a drag, so the same window can no longer sit in two different
// layouts depending on the order those happened in.
function fitPanes() {
  if (!document.body.classList.contains("note-open")) return;
  const userCollapsed = document.body.classList.contains("sidebar-collapsed") && !sidebarYielded;
  if (!userCollapsed) {
    if (drawerBudget(true) >= MIN_DRAWER) restoreYieldedSidebar();
    else yieldSidebarToDrawer();
  }
  const sidebarOn = !document.body.classList.contains("sidebar-collapsed");
  const want = parseInt(localStorage.getItem("note-width"), 10) || 630;
  const w = Math.max(MIN_DRAWER, Math.min(want, drawerBudget(sidebarOn)));
  notePanel.style.width = w + "px";
  setNoteW(w);
}

function yieldSidebarToDrawer() {
  if (document.body.classList.contains("sidebar-collapsed")) return;
  document.body.classList.add("sidebar-collapsed");
  sidebarYielded = true;
  syncNarrow(); // the rail stands where the drawer was
}

function restoreYieldedSidebar() {
  if (!sidebarYielded) return;
  document.body.classList.remove("sidebar-collapsed");
  sidebarYielded = false;
  syncNarrow();
}

// This drawer is the app's ONE right-edge surface, in three modes. Two are the
// same file read two ways: `note` is its text, `diff` is that file against how
// it stood before this session touched it. The third, `node`, is what you
// POINTED at, which is a different subject from either.
//
// That third mode was called `work` and drew two unrelated things behind one
// name: the run on chat, the node on explore. A mode cannot be named after
// one of its two subjects. The run moved to the left rail's Work compartment,
// where nothing competes with it and it stays readable WHILE a note is open
// (and from there to the transcript's ledger line with ADR-0122); this mode
// kept the node, and now carries the name of what it draws.
//
// (It also once carried a `context` mode, which drew the /context payload the
// work panel's node scope also drew: two panels at the same edge, the same
// sections, neither header saying which you were reading. That one was deleted
// rather than merged - the panel took the payload, this drawer kept the prose.)
//
// No segment switches them (ADR-0059). Since ADR-0075 the drawer opens on the
// note's TEXT for every gesture with a note behind it - a wikilink, the file
// tree, a search hit, a calendar task, a graph node, a metrics or
// shape row - so naming a note and pointing at one land on the same view, and
// "a second click reads it" is gone. The note's second view, its DETAILS
// (Content, Links, Place), is behind the info button beside the title: on for
// the session once turned on, so every note opened after it opens there,
// the way a details pane follows the selection in Drive or the Finder.
// `node` is left to what has no text to read: an unresolved link, a bridge, a
// set of concepts, a comparison, the report, a paper on the rim or a missing
// one. The diff is entered from the note's menu or a write card, and the
// `changes` chip is the way back from it to the text.
let drawerMode = "note";
let detailsOn = false; // the note's second view, held for the session (ADR-0075)

function syncDrawerMode() {
  const open = notePanel.classList.contains("open");
  const details = drawerMode === "note" && detailsOn;
  // The title belongs to a note in every view of it, in the same place; the
  // two chips to what has no title. Beside the title, the info button and
  // the pin on the text, the read glyph in details, which leads back.
  $("#note-title").hidden = drawerMode === "node";
  $("#note-info").hidden = drawerMode !== "note" || details;
  $("#note-read").hidden = !details;
  $("#note-pin").hidden = drawerMode === "node" || details;
  $("#note-changes").hidden = drawerMode !== "diff";
  $("#node-scope").hidden = drawerMode !== "node";
  $("#node-state").hidden = drawerMode !== "node";
  $("#note-edit").hidden = drawerMode !== "note" || details;
  $("#note-diff").hidden = drawerMode !== "diff";
  $("#node-pane").hidden = drawerMode !== "node" && !details;
  // A diff of a note this session never touched has nothing in it, so the
  // menu lists it only when there is one, and not while it is showing.
  $("#nm-read").hidden = drawerMode !== "note" || !changedPaths.has(lastNotePath);
  if (drawerMode !== "note" || details) hideSelBar();
  // The corner toggle (ADR-0084) exists only while the drawer is shut AND has
  // something to reopen: what reopenDrawer restores, a note read or a node
  // pointed at. Open, the drawer's own close holds the same corner. It names
  // what it reopens, because an icon alone cannot say which note.
  const toggle = $("#drawer-toggle");
  toggle.hidden = open || !(lastViewedPath || nodePicked);
  const what = drawerMode !== "node" && lastViewedPath ? noExt(lastViewedPath.split("/").pop()) : "";
  const label = what ? "show " + what : "show the note sidebar";
  toggle.setAttribute("aria-label", label);
  toggle.dataset.tip = label;
  document.body.classList.toggle("drawer-recall", !toggle.hidden);
  drawCheckStrip(); // the check's strip heads the text, so it goes where the text goes
}

// A vault switch leaves nothing of the old vault to reopen.
function forgetDrawer() {
  if (notePanel.classList.contains("open")) closeNote();
  lastViewedPath = null;
  nodePicked = false;
  syncDrawerMode();
}

// Shared tail of both openers: raise the panel and let fitPanes negotiate the
// widths. Kept in one place so the two modes cannot drift apart on layout.
function showDrawer(title) {
  $("#note-title-text").textContent = title || "";
  // The whole title on hover, for the one the header's width cuts.
  $("#note-title").title = title || "";
  notePanel.classList.add("open");
  notePanel.setAttribute("aria-hidden", "false");
  document.body.classList.add("note-open"); // dock + chat inset to the drawer's edge
  syncDrawerToViews();
  fitPanes(); // owns the sidebar decision AND the drawer width, in that order
  syncPinButton(); // the toggle states THIS note, so it is re-read per open
  syncDrawerMode(); // the segments and the corner toggle key on `open`, set above
  notePanel.inert = false;
  markOpenNote();   // the tree marks the note the drawer is on (P19)
  syncUrl(false);   // ?note=<path> rides the hash (P7)
}

// Node opens without a path, which is the whole difference between it and the
// other two: it is about what you pointed at, which may be a ghost with no file
// behind it at all, so the header shows the node's two chips and no title.
let nodePicked = false; // has anything been pointed at this session

function openNodePane() {
  drawerMode = "node";
  showDrawer("");
}
// The flag is set HERE and not in openNodePane, because this is the only entry
// that is an actual pick. The segment above calls the same opener and is
// disabled until this has fired, and the corner toggle exists only once it has
// (ADR-0084): neither may be summoned by a node nobody chose.
window.openNodeDrawer = () => { nodePicked = true; openNodePane(); };

// A note opens in its editor and nowhere else (ADR-0059): its bytes, its
// title and a paper's identity come in one read, and the field takes no
// focus, so a keystroke meant for the composer or a shortcut never lands in
// the note. `path` may also be a bare title, a wikilink's, which the read
// resolves; the canonical path is what the pin, the diff and the tree key on.
let openSeq = 0; // two fast opens: only the last one may land

// `facts` are what the graph frame knew about the node it was clicked as
// (degree, state, area): handed on to the pane rather than asked for again.
// True once the note is the one on screen, so a caller can act on its text.
async function openNote(path, line, facts) {
  if (!path) return;
  const seq = ++openSeq;
  // The note already open, on its text or its details: clicking its node again
  // or a row naming it has nothing new to load, and a reload would drop the
  // caret and the scroll for nothing.
  if (path === editPath && drawerMode === "note" && notePanel.classList.contains("open")) {
    if (line && editor) editor.revealLine(line);
    if (ctxPath !== path) loadContext(path, facts);
    return true;
  }
  closeNoteMenu();
  await releaseNote(); // what was typed into the last note reaches the disk first
  let data, ed;
  try {
    const [r, loaded] = await Promise.all([fetch("/note/raw?path=" + encodeURIComponent(path)), ensureEditor()]);
    if (r.status === 404) { if (seq === openSeq) notify(`no note named "${path}" in this vault`); return; }
    if (!r.ok) throw new Error(String(r.status));
    data = await r.json();
    ed = loaded;
  } catch { if (seq === openSeq) notify("couldn't open that note"); return; }
  if (seq !== openSeq) return;
  if (!ed) { notify("couldn't load the editor"); return; }
  lastNotePath = data.path;
  lastViewedPath = data.path;
  drawerMode = "note";
  editPath = data.path; editEtag = data.etag; editClean = data.text;
  editConflict = false;
  ed.load(data.text, { top: data.paper ? paperStripEl(data.paper) : null, missing: data.broken || [] });
  focusGraphNode(data.path);
  showDrawer(data.title || data.path);
  ed.view.requestMeasure(); // it was measured while hidden
  // A calendar task row lands on its line (ADR-0062); the doc is the raw file,
  // so a line the scan counted is a line of this doc.
  if (line) ed.revealLine(line);
  // Every open reads the note's context, on every view: its details are one
  // button away wherever the note was opened, and the info button's dot needs
  // to know about a warning before anyone asks.
  loadContext(data.path, facts);
  if (checkedPaths.has(data.path)) recheck(true);
  return true;
}

// Whose context the pane holds, so turning details on never shows another's.
let ctxPath = null;

async function loadContext(path, facts) {
  ctxPath = path;
  setInfoDot(false);
  const seq = ++nodeSeq;
  const target = { ...(facts || {}), path };
  announceNode(target, null);   // the pane says the note's name now; the tabs follow
  try {
    const data = await (await fetch("/context?path=" + encodeURIComponent(path))).json();
    if (seq !== nodeSeq) return;
    announceNode(target, data);
    setInfoDot(!!(data.structure && data.structure.articulation));
  } catch {
    if (seq === nodeSeq) announceNode(target, { error: "couldn't read that note's context" });
  }
}

// The one warning a note can carry, on the button that leads to it, so it
// shows while the text is being read (ADR-0075).
function setInfoDot(on) {
  $("#note-info .ic-dot").hidden = !on;
  $("#note-info").setAttribute("aria-label",
    on ? "details: links and place; this note is the only bridge for some notes" : "details: links and place");
}

// The two views of one note. False when nothing changed, so the Escape list
// can stop at a press that took the reader back to the text.
function setDetails(on) {
  if (drawerMode !== "note" || detailsOn === on || !notePanel.classList.contains("open")) return false;
  const inPanel = notePanel.contains(document.activeElement);
  detailsOn = on;
  syncDrawerMode();
  if (on && ctxPath !== lastNotePath) loadContext(lastNotePath);
  if (!on && editor) editor.view.requestMeasure(); // it was measured while hidden
  // The control that was pressed is hidden now: focus goes to the one that
  // took its place, rather than to the page.
  const f = document.activeElement;
  if (inPanel && (!f || !f.offsetParent)) $(on ? "#note-read" : "#note-info").focus();
  return true;
}
$("#note-info").addEventListener("click", () => setDetails(true));
$("#note-read").addEventListener("click", () => setDetails(false));
// Alt+Enter toggles them, the Properties key in Windows. Mod-i is the
// editor's italic (app-editor.js) and Ctrl+Shift+I Chrome's DevTools. Not
// from a text field: in the composer Enter sends, and a send is not undone.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if (e.target.closest && e.target.closest("input, textarea, select")) return;
  if (drawerMode !== "note" || !notePanel.classList.contains("open")) return;
  e.preventDefault();
  e.stopPropagation();
  setDetails(!detailsOn);
}, true);

// A paper's byline and its doors to Zotero, the PDF and the DOI, over the
// text (ADR-0055); the body below is the verbatim paper.
function paperStripEl(paper) {
  const strip = mkEl("div", "note-paper");
  if (!window.paperStrip) return strip;
  const s = paperStrip(paper);
  if (s.byline) strip.appendChild(mkEl("span", "", s.byline));
  for (const [label, href] of s.links) {
    strip.appendChild(Object.assign(document.createElement("a"), { className: "wk-paper-a", textContent: label, href }));
  }
  return strip;
}

// --- the Node panel: what you pointed at, in the third column ----------------
// Pointing at a node fills the third column instead of opening a drawer over
// the surface you are reading. The click contract itself does not move: naming
// a note still means "read it" and still lands in the drawer; pointing at one
// still means "what is this". What moved is the ANSWER, which used to be drawn
// twice - here for explore and in the drawer's context mode for every other
// view - off the one /context call. One reader now, so a node answers the same
// on the graph, a metrics row and a shape row.
//
// The head facts (degree, state, area) ride in on the frame's message rather
// than being asked for again: the frame computed all three to draw the node you
// clicked, and a second source for them is a second answer that can disagree
// with the picture in front of you.
const announceNode = (node, context) =>
  document.dispatchEvent(new CustomEvent("silica:node",
    { detail: node ? { node: node, context: context } : null }));

let nodeSeq = 0; // two fast clicks: only the last one's context may land

async function showNode(target) {
  const path = target.path || "";
  const ghost = !!target.ghost || !path;
  if (!path && !target.name) return;
  // A note opens, on its text or its details (ADR-0075). Only what has no
  // text to read raises node mode, below.
  if (!ghost && !target.href && !target.outside) { openNote(path, null, target); return; }
  ctxPath = null;
  const seq = ++nodeSeq;
  // A missing paper carries its own reference and has nothing behind it to ask
  // /context about (ADR-0055); asking would describe it as an unresolved link.
  if (target.href) {
    focusGraphNode(null);
    announceNode(target, { title: target.name });
    window.openNodeDrawer();
    return;
  }
  // A paper on the rim (ADR-0066) has no file either: the frame sends its
  // covering notes, and Zotero its byline and abstract. The frame keeps its
  // own focus on the paper, which is what fades every other line.
  if (target.outside) {
    const title = target.outside.title || target.name;
    announceNode(target, { title });
    window.openNodeDrawer();
    try {
      const r = await fetch("/zotero/item?key=" + encodeURIComponent(target.outside.key));
      const item = r.ok ? await r.json() : null;
      if (seq === nodeSeq) announceNode(target, { title, item });
    } catch { /* Zotero closed since the build: the covers still stand without the abstract */ }
    return;
  }
  // An unresolved link: no file, so the pane is the only thing that can say
  // anything about it - who links it, and what they are about.
  focusGraphNode(null);
  announceNode(target, null);   // the head paints now; the sections follow
  window.openNodeDrawer();
  try {
    const data = await (await fetch("/context?ghost=1&name=" + encodeURIComponent(target.name || ""))).json();
    if (seq === nodeSeq) announceNode(target, data);
  } catch {
    if (seq === nodeSeq) announceNode(target, { error: "couldn't read that link's context" });
  }
}
window.showNode = showNode;

// --- diff mode ---------------------------------------------------------------
// The note against how it stood before this session touched it. Red is what left
// the file, green is what arrived; the gaps are the unchanged stretches the
// server left out. Rows are DOM nodes, never innerHTML: every line here is a line
// of the user's own vault.
async function openDiff(path) {
  if (!path) return;
  closeNoteMenu();
  await releaseNote(); // the diff has to include what was just typed
  lastNotePath = path;
  lastViewedPath = path;
  drawerMode = "diff";
  syncDrawerMode();
  focusGraphNode(path);
  const box = $("#note-diff");
  box.className = "dl-wait";
  box.textContent = "reading the diff…";
  showDrawer(path.split("/").pop().replace(/\.md$/, ""));
  let d;
  try {
    d = await (await fetch("/changes/diff?path=" + encodeURIComponent(path))).json();
  } catch {
    box.className = "dl-wait";
    box.textContent = "couldn't read that diff";
    return;
  }
  // The session never touched this note, so there is nothing to compare it
  // against. Show the note rather than an empty diff pretending to be one.
  if (d.baseline === false) return openNote(path);
  box.className = "";
  box.innerHTML = "";
  const head = mkEl("div", "dl-head");
  head.appendChild(mkEl("span", "dl-kind " + d.kind, d.kind));
  if (d.from) head.appendChild(mkEl("span", "dl-from", d.from + " →"));
  if (d.added) head.appendChild(mkEl("span", "chg-add", "+" + d.added));
  if (d.removed) head.appendChild(mkEl("span", "chg-del", "−" + d.removed));
  box.appendChild(head);
  if (!d.lines.length) {
    box.appendChild(mkEl("div", "dl-empty", d.kind === "moved"
      ? "moved, and the bytes are unchanged"
      : "no difference left: this note is back to how it started"));
  }
  const track = mkEl("div", "dl-track");
  const CLS = { "+": "dl-add", "-": "dl-del", "@": "dl-gap" };
  for (const l of d.lines) {
    const cls = CLS[l.op] || "dl-ctx";
    // A gap is the elision itself, not a line of the note.
    track.appendChild(mkEl("div", "dl-line " + cls, l.op === "@" ? "⋯" : l.op + l.text));
  }
  box.appendChild(track);
  if (d.clipped)
    box.appendChild(mkEl("div", "dl-empty",
      `${d.clipped} more lines. Open the note for the rest`));
  box.scrollTop = 0;
  showDrawer(d.name || path);
}

function closeNote() {
  closeNoteMenu();
  releaseNote(); // the save rides on after the panel is shut
  notePanel.classList.remove("open");
  notePanel.setAttribute("aria-hidden", "true");
  // Inert while shut (audit P9): a closed drawer's buttons used to sit in the
  // tab order, six stops a keyboard user walked through into nothing.
  notePanel.inert = true;
  document.body.classList.remove("note-open");
  syncDrawerToViews();
  restoreYieldedSidebar();
  lastNotePath = null; // lastViewedPath survives: the corner toggle reopens it
  focusGraphNode(null);
  syncDrawerMode(); // the corner toggle keys on `open`, cleared above
  markOpenNote();
  syncUrl(false);
}


// The corner toggle's one act: reopen the drawer. It does not
// pick a mode, it RESTORES one — whichever you left it on, degraded to the next
// one that has something behind it, because a segment can go dead between two
// opens (a diff needs this session to have touched the file).
function reopenDrawer() {
  const path = lastNotePath || lastViewedPath;
  if (drawerMode === "diff" && changedPaths.has(path)) { openDiff(path); return; }
  if (drawerMode !== "node" && path) { openNote(path); return; }
  if (drawerMode === "node" && nodePicked) { openNodePane(); return; }
  if (path) openNote(path);
  // Nothing read and nothing pointed at: there is no toggle to press (ADR-0084).
}

$("#drawer-toggle").addEventListener("click", () => {
  if (notePanel.classList.contains("open")) closeNote();
  else reopenDrawer();
});
// The two turns a suggestion drafts. They live here and not in work.js, which
// is the only surface that offers them, because a prompt written twice is two
// turns that drift the first time one of them is reworded - the same rule the
// metrics view's bulk prompts already follow.
function writeGhostPrompt(name, from) {
  return 'Write the note "' + name + '", which "' + from + '" already links to.';
}

function linkNotesPrompt(a, b) {
  return 'Check whether "' + a + '" and "' + b + '" belong linked, and if they do, '
       + "add the wikilink in whichever direction reads right.";
}

// The unresolved link as a subject of its own: it has no body, and the notes
// that already point at it are the only material there is to write it from.
// Three callers hand it three shapes of the same list (backlink rows, ghost
// source names, a metrics row's `from`), so it takes either and quotes both:
// an unquoted list of names ending in a comma is a sentence the model has to
// guess the boundaries of.
function ghostWritePrompt(title, sources) {
  const from = (sources || []).map((s) => '"' + noExt(s.name || s) + '"').join(", ");
  return 'Write the note "' + title + '".'
       + (from ? " It is already linked from " + from
                 + ", so ground it in what those notes already say." : "");
}
window.writeGhostPrompt = writeGhostPrompt;
window.linkNotesPrompt = linkNotesPrompt;
window.ghostWritePrompt = ghostWritePrompt;

// A concept is a set of notes, so it focuses a set. Clicked from chat it also
// switches to explore first — there is nothing to see otherwise.
// Exported: the explore panel's concept pills light the same notes through this
// one function, rather than growing a second copy of "resolve the term, then
// focus whatever carries it".
// The selection is a SET of concepts and the lit notes are their intersection:
// one click lights where a concept lives, the second where two are treated
// together, and the work panel quotes the lines that carry each. Clicking a
// lit pill drops that one concept; the last drop shows the graph again.
let litConcepts = [];
window.isConceptLit = (term) => litConcepts.includes(term);
async function lightConcept(term, btn) {
  const i = litConcepts.indexOf(term);
  if (i >= 0) litConcepts.splice(i, 1); else litConcepts.push(term);
  document.querySelectorAll(".wk-pill").forEach((e) => e.classList.toggle("lit", litConcepts.includes(e.textContent)));
  if (btn) btn.classList.toggle("lit", i < 0);
  if (!litConcepts.length) {
    focusGraphNodes([]);
    document.dispatchEvent(new CustomEvent("silica:concepts", { detail: null }));
    return;
  }
  if (activeTab !== "graph") showTab("graph");
  try {
    const d = await (await fetch("/concepts?terms=" + encodeURIComponent(litConcepts.join(",")))).json();
    focusGraphNodes((d.notes || []).map((n) => n.path));
    document.dispatchEvent(new CustomEvent("silica:concepts", { detail: d }));
    if (window.openNodeDrawer) window.openNodeDrawer();
    if (!d.notes || !d.notes.length) notify(litConcepts.length > 1
      ? "no note carries all of: " + litConcepts.join(", ") : "no notes carry “" + term + "”");
  } catch { notify("couldn't resolve that concept"); }
}
window.lightConcept = lightConcept;

// Suggested rows never write. They prefill a chat turn and hand it back to you,
// so the write still goes through the agent's gate — validate, checkpoint, undo
// journal — instead of a drawer button reaching the disk on its own.
function prefillChat(text) {
  showTab("chat");
  const box = $("#input");
  box.value = text;
  box.focus();
  box.dispatchEvent(new Event("input")); // let the autosize/palette hooks see it
}

// "the map" in the drawer's menu: the Map is the graph arranged on the note
// (ADR-0088). Capture the path FIRST: the programmatic tab .click() bubbles to
// the document's outside-click handler, which closes the drawer, nulls
// lastNotePath and clears the graph focus, so the focus is set again after it.
$("#note-map").addEventListener("click", () => {
  const note = lastNotePath;
  if (!note) return;
  graphMode = "graph";
  document.querySelector('.tab[data-tab="graph"]').click();
  focusGraphNode(note);
});

// The same jump for the reading order, which the node panel's ladder footer
// hands off to. Exported rather than duplicated: the drawer knows which note it
// is on and nothing else about explore's modes, and rootPath is the one place
// that knows how to enter Path rooted somewhere.
window.openLadder = (note) => {
  if (!note) return;
  // rootPath BEFORE the tab, and no pre-set graphMode: path has no replay on
  // tab-enter - showTab's setGraphMode(graphMode) draws off pathRootedPath
  // and nothing else. Pre-setting the mode therefore started an UNROOTED draw
  // first, and the landing measures forty ladders, so it also finished last
  // and overwrote the rooted one: Path opened on the picker every time.
  rootPath(note);
  document.querySelector('.tab[data-tab="graph"]').click();
};

// The selection bar's two asks dispatch a turn about the open note. The
// drawer stays open (the peek dock tucks under it and mirrors the turn), so
// the note you launched from is never lost.
// Backslashes too: shlex reads `\"` inside quotes as a quote, so a passage
// ending in `\` would otherwise swallow its own closing quote.
const shellQuote = (s) => '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
function drawerReader(makeCmd, peekTitle) {
  if (!lastNotePath || streaming) return; // streaming: send() would no-op — no peek either
  const cmd = makeCmd(lastNotePath, $("#note-title-text").textContent.trim());
  if (activeTab !== "chat") openPeek(peekTitle || cmd); // on chat the stream is already visible
  send(cmd);
}
// The text in the composer that is showing: the chat's on Chat, the dock on
// Explore and Learn. Calendar and Metrics show none, so there it goes to Chat.
// Nothing is sent: the reader finishes the question.
function draftInComposer(draft) {
  const box = activeTab === "chat" ? $("#input") : !$("#dock").hidden ? $("#dock-input") : null;
  if (!box) { prefillChat(draft); return; }
  box.value = draft + box.value;
  box.focus();
  box.setSelectionRange(draft.length, draft.length);
  box.dispatchEvent(new Event("input")); // let the autosize/palette hooks see it
}
// The menu's chat (ADR-0075): a question about the note, where a passage's
// ask… puts the passage. It replaced four asks that are still typed commands.
$("#note-chat").addEventListener("click", () => {
  if (!lastNotePath) return;
  const title = $("#note-title-text").textContent.trim() || lastNotePath.split("/").pop().replace(/\.md$/, "");
  draftInComposer('Tell me about "' + title + '", grounded in what the vault already says. ');
});
// Test me and Explain it (ADR-0119): Learn's home no longer lists every
// note, so a test on this one starts here. Learn's own functions run them.
$("#note-test").addEventListener("click", () => { closeNoteMenu(); if (lastNotePath && typeof learnTestNote === "function") learnTestNote(lastNotePath); });
$("#note-teach").addEventListener("click", () => { closeNoteMenu(); if (lastNotePath && typeof learnExplain === "function") learnExplain(lastNotePath); });
$("#note-diff-open").addEventListener("click", () => openDiff(lastNotePath));
$("#note-changes").addEventListener("click", () => openNote(lastNotePath));

// --- the note's menu ---------------------------------------------------------
// A menu button in the WAI-ARIA sense, shared by the note's title and the
// composer's + (ADR-0083): opening it puts focus on an item, the arrows walk
// the items, Escape (in the app's one list, app-settings.js) and Tab close it
// and hand focus back to the button. It returns the close, which is true when
// the menu was open, so the Escape list can stop there.
function menuButton(menu, button, onOpen = () => {}) {
  const items = () => [...menu.querySelectorAll('[role="menuitem"]')].filter((b) => b.offsetParent);
  function open(at = 0) {
    onOpen();
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    const all = items();
    (all.at(at) || all[0]).focus();
  }
  function close(refocus = false) {
    if (menu.hidden) return false;
    const inside = menu.contains(document.activeElement);
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (refocus || inside) button.focus();
    return true;
  }
  button.addEventListener("click", () => { if (!close(true)) open(0); });
  button.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    open(e.key === "ArrowDown" ? 0 : -1);
  });
  menu.addEventListener("keydown", (e) => {
    const all = items(), i = all.indexOf(document.activeElement);
    const go = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: all.length - 1 }[e.key];
    if (go !== undefined) { e.preventDefault(); all[(go + all.length) % all.length].focus(); return; }
    // Tab leaves from the button, so it moves on to whatever follows the button.
    if (e.key === "Tab") close(true);
  });
  // An item that was chosen closes the menu. Registered here, before any
  // handler the caller adds on the menu, so a caller that moves focus wins.
  menu.addEventListener("click", (e) => { if (e.target.closest('[role="menuitem"]')) close(); });
  return close;
}

// The title is the note's handle (ADR-0059): what can be done with the note
// lives behind its name, grouped by where the answer arrives.
const noteMenu = $("#note-menu");
const noteTitleBtn = $("#note-title");
// The read group follows the ledger, which may have moved since it was drawn.
const closeNoteMenu = menuButton(noteMenu, noteTitleBtn, () => syncDrawerMode());

// --- the note's text ---------------------------------------------------------
// Obsidian's Live Preview, the note's only view in the drawer (ADR-0059; the
// editor is ADR-0051's): its own bytes in an editor that draws the markdown
// everywhere except where the caret is, and shows it as typed there (the rule
// lives in static/app-editor.js). There is no reader beside it and no edit
// mode to enter: a click in the text is a caret, and typing saves. A stray
// keystroke is a session change like any other, so the diff shows it and
// /undo puts the bytes back. Saves are debounced off typing and flushed on
// blur, on opening another note or the diff, on closing the drawer and on
// pagehide, so the disk is never more than a moment behind the field and
// there is no save button to forget. Each save goes through /note/save, which
// is the driver's overwrite: the session ledger takes its baseline on the
// first keystroke, so the diff enables and /undo can put the bytes back.
//
// The etag from /note/raw rides with every save. A 409 means an agent (or
// Obsidian) rewrote the note under the editor; the field keeps the typed text,
// says so once, and stops flushing on its own - only MORE typing writes again,
// and then it wins, which is Obsidian's rule stated instead of silent.
const noteEdit = $("#note-edit");
let editor = null;      // the one LiveEditor, built with the first note
let editorLoad = null;  // its two scripts, fetched once
let editPath = "";      // the note the field holds; "" when it holds none
let editEtag = "";      // the disk's bytes as last seen, for the save to check
let editClean = "";     // the text the disk holds as far as the field knows
let editConflict = false;
let editTimer = 0;
let editSaving = null;  // the in-flight save; a second one waits for it
let editAgain = false;  // a save asked for while one was in flight

const loadScript = (src) => new Promise((resolve, reject) => {
  const s = document.createElement("script");
  s.src = src;
  s.onload = resolve;
  s.onerror = reject;
  document.head.appendChild(s);
});

// With the first note and not with the page: 320 KB of editor is dead weight
// for a session that never opens one. A failed load is forgotten, so the next
// open tries again rather than inheriting the failure.
function ensureEditor() {
  editorLoad ||= loadScript("/static/codemirror.min.js")
    .then(() => loadScript("/static/app-editor.js"))
    .then(() => {
      editor = window.LiveEditor.create(noteEdit, {
        render: (text) => fetch("/note/render", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        }).then((r) => (r.ok ? r.json() : Promise.reject(r.status))).then((d) => d.html || ""),
        openNote: openLinkedNote,
        change: () => { editConflict = false; scheduleSave(); },
        blur: () => { saveEdit(); recheck(); },
        save: () => { saveEdit(); },
        decorate: decorateRendered,
        select: (view) => { syncSelBar(view); recheck(); },
        openAt: (path, line) => openNote(path, line || undefined),
        links: (paths) => paths.map((p) => `[[${mentionLink(p)}]]`), // a tree row dropped on the note
      });
    })
    .catch(() => { editorLoad = null; });
  return editorLoad.then(() => editor);
}

// What the server drew inside the editor gets what the reader's copy got:
// diagrams run, and the properties box opens as it was last left.
function decorateRendered(el, view) {
  renderMermaid(el);
  const fm = el.querySelector("details.fm");
  if (fm) {
    fm.open = localStorage.getItem("note-props") === "open";
    fm.addEventListener("toggle", () => {
      localStorage.setItem("note-props", fm.open ? "open" : "closed");
      view.requestMeasure(); // the box changed height under the editor
    });
  }
}

// A link followed from the editor lands in the editor, as in Obsidian. The
// raw read resolves a wikilink's bare title to its path.
function openLinkedNote(target) {
  return openNote(target.split("#")[0].trim());
}

// The field lets go of its note: whatever was typed is on disk first,
// including a save that was asked for while another was in flight.
async function releaseNote() {
  if (!editPath) return;
  if (checkRun) checkRun.abort(); // the verdicts paid for are cached, and come back on reopening
  await saveEdit();
  while (editSaving) await editSaving;
  editPath = ""; editEtag = ""; editClean = ""; editConflict = false;
  clearTimeout(editTimer);
  hideSelBar();
}

// An agent's write or an /undo can land on the note the field holds. While
// nothing typed is waiting to be saved, the field takes the disk's bytes as one
// mapped change, so the caret stays where it was; with typing in it, the
// next save's 409 says so instead, as it did before the editor was always
// open. Called from loadChanges, which runs after every turn and every undo.
async function refreshOpenNote() {
  const path = editPath;
  if (!path || !editor || editSaving || editor.getText() !== editClean) return;
  const r = await fetch("/note/raw?path=" + encodeURIComponent(path)).catch(() => null);
  const d = r && r.ok ? await r.json().catch(() => null) : null;
  if (!d || editPath !== path || editSaving || editor.getText() !== editClean || d.etag === editEtag) return;
  editEtag = d.etag;
  editClean = d.text;
  editor.adopt(d.text);
  editor.setMissing(d.broken || []);
}

function scheduleSave() {
  clearTimeout(editTimer);
  editTimer = setTimeout(saveEdit, 800);
}

// A move from the tree (ADR-0126 point 3): the panel's note follows its path,
// reopened so the title and the URL say the new one; a note whose links the
// move rewrote takes the disk's bytes, or the next save would write the old
// link back and meet a 409. `to` maps old paths to new.
function followNote(to, rewritten) {
  if (!editPath) return;
  if (to.has(editPath)) {
    const path = to.get(editPath);
    if (lastViewedPath === editPath) lastViewedPath = path;
    editPath = ""; // or openNote would take it for the note already open
    openNote(path);
  } else if (rewritten.includes(editPath)) {
    refreshOpenNote();
  }
}

async function saveEdit() {
  clearTimeout(editTimer);
  if (!editPath || editConflict || !editor) return;
  if (editSaving) { editAgain = true; return editSaving; }
  const path = editPath, text = editor.getText();
  if (text === editClean) return;
  editSaving = (async () => {
    try {
      const r = await fetch("/note/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, text, etag: editEtag }),
      });
      const data = await r.json().catch(() => ({}));
      if (editPath !== path) return; // the field moved on; this save is history
      if (r.status === 409) {
        editEtag = data.etag || editEtag;
        editConflict = true;
        notify("this note changed on disk while you were editing; keep typing to overwrite it");
        return;
      }
      if (!r.ok) { notify("couldn't save the note"); return; }
      editEtag = data.etag;
      // What landed may differ from what was sent (overwrite stamps a `type:`
      // into frontmatter that lacks it). If nothing was typed meanwhile the
      // field takes the disk's bytes as one mapped change, so the two agree;
      // otherwise the next save carries the newer text anyway. Either way the
      // clean text is the disk's, the one the new etag names.
      if (data.text !== text && editor.getText() === text) editor.adopt(data.text);
      editClean = data.text;
      if (data.broken) editor.setMissing(data.broken);
      loadChanges(); // the menu's diff and the write cards see the edit
      // a workspace note's row reads its name, lines and instructions (ADR-0112)
      if (typeof workspaces !== "undefined" && workspaces.some((w) => w.path === path)) loadWorkspaces();
    } catch {
      notify("couldn't save the note");
    } finally {
      editSaving = null;
      if (editAgain) { editAgain = false; saveEdit(); }
    }
  })();
  return editSaving;
}

// A reload or a closed tab with an unsaved edit: the beacon outlives the page.
addEventListener("pagehide", () => {
  if (!editPath || editConflict || !editor || editor.getText() === editClean) return;
  navigator.sendBeacon("/note/save", new Blob(
    [JSON.stringify({ path: editPath, text: editor.getText(), etag: editEtag })],
    { type: "application/json" }));
});

// --- checking the note's sources (ADR-0110 step 3) ----------------------------
// `check sources` reads the note on disk against the vault: the rule findings
// land at once, the model's block by block, as marks on the text
// (app-editor.js) and counts on the strip above it. A request of its own, so
// the chat stays free; cancel drops it. A note checked in this page comes
// back with its marks when it is reopened, from the server's cache and with
// no model call, so following a box's passage and coming back loses nothing.
const checkStrip = $("#note-check");
const CHECK_SHOW_KEY = "check-show";
const CHECK_GRADES = ["error", "warning", "nit"];
let checkPath = "";       // the note the marks belong to; "" when none
let checkRun = null;      // the AbortController of the check in flight
let checkView = null;     // what the strip shows: {counts, pool, done, of, calls, held, notChecked}
let recheckBusy = false;
const checkedPaths = new Set();

// Per viewer (Open 9): a lecture-notes reader hides *not found*, an auditor
// keeps it. Nits start hidden, as the ADR has them until pressed.
function checkShown() {
  let list = null;
  try { list = JSON.parse(localStorage.getItem(CHECK_SHOW_KEY) || "null"); } catch { /* unreadable: the defaults */ }
  return new Set(Array.isArray(list) ? list : ["error", "warning", "not found"]);
}

async function readLines(resp, each) {
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n");
    buf = parts.pop();
    for (const line of parts) if (line.trim()) each(JSON.parse(line));
  }
  if (buf.trim()) each(JSON.parse(buf)); // a one-line answer may end without its newline
}

// The disk is what the server checks, so the field goes there first; a
// field the disk refused (409) is not checked against bytes it does not hold.
async function flushForCheck() {
  await saveEdit();
  while (editSaving) await editSaving;
  return !editConflict && editor.getText() === editClean;
}

function countFindings(findings) {
  const c = { error: 0, warning: 0, nit: 0 };
  for (const f of findings) c[f.grade]++;
  return c;
}

async function checkSources(confirm = false) {
  if (!editPath || !editor) return;
  if (checkRun) checkRun.abort();
  if (!(await flushForCheck())) { notify("this note changed on disk; reopen it before checking"); return; }
  const path = editPath, ctl = new AbortController();
  checkRun = ctl;
  checkPath = path;
  checkedPaths.add(path);
  const seen = [];
  checkView = { counts: countFindings(seen), pool: null, done: 0, of: 0, notChecked: 0 };
  editor.startCheck();
  editor.showCheck(checkShown());
  drawCheckStrip();
  const live = () => checkRun === ctl && editPath === path;
  try {
    const r = await fetch("/note/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, confirm }), signal: ctl.signal,
    });
    if (!r.ok) throw new Error(String(r.status));
    await readLines(r, (ev) => {
      if (!live()) return;
      if (ev.event === "rules" || ev.event === "confirm" || ev.event === "block") {
        editor.addCheck(ev.findings, ev.states);
        seen.push(...ev.findings);
        checkView.counts = countFindings(seen);
      }
      if (ev.event === "rules") Object.assign(checkView, { pool: ev.pool, of: ev.waiting });
      else if (ev.event === "confirm") Object.assign(checkView, { pool: ev.pool, held: ev.calls });
      else if (ev.event === "block") Object.assign(checkView, { done: ev.done, of: ev.of });
      else if (ev.event === "done") {
        // The report's counts are the cache file's (point 14, step 3).
        Object.assign(checkView, { counts: ev.counts, pool: ev.pool, notChecked: ev.states.filter((s) => s.state === "not checked").length });
      } else if (ev.event === "error") notify(ev.say);
      drawCheckStrip();
    });
  } catch (e) {
    if (e.name === "AbortError" && editPath === path && checkView) {
      // The blocks it never reached; the one in flight lands in the cache.
      Object.assign(checkView, { cancelled: true, notChecked: checkView.notChecked + Math.max(0, checkView.of - checkView.done) });
    } else if (live()) notify("couldn't check this note");
  } finally {
    if (checkRun === ctl) checkRun = null;
    if (editPath === path) drawCheckStrip();
  }
}

// The rules again on a block typed into, once the caret has left it (Open 4),
// and the marks of a note checked earlier in this page when it is reopened:
// served from the cache, so no model call. The model's verdicts on an edited
// block stay hollow until the next check.
async function recheck(reopened = false) {
  if (!editor || recheckBusy || checkRun || !editPath) return;
  if (!reopened && (checkPath !== editPath || !editor.checkEdited())) return;
  recheckBusy = true;
  const path = editPath;
  try {
    if (!(await flushForCheck()) || editPath !== path) return;
    if (reopened) { editor.startCheck(); editor.showCheck(checkShown()); }
    const carried = editor.rebaseCheck();
    const r = await fetch("/note/check", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, rules: true }),
    });
    if (!r.ok) return;
    let report = null;
    await readLines(r, (ev) => { if (ev.event === "done") report = ev; });
    if (!report || editPath !== path || checkRun) return;
    if (report.unchecked) { checkedPaths.delete(path); editor.endCheck(); return; }
    editor.replaceCheck(report, carried);
    checkPath = path;
    checkView = { counts: report.counts, pool: report.pool, done: 0, of: 0,
                  notChecked: report.states.filter((s) => s.state === "not checked").length };
    drawCheckStrip();
  } catch {
    /* a rerun that failed leaves the marks hollow, which is what they are */
  } finally {
    recheckBusy = false;
  }
}

function endCheck() {
  if (checkRun) checkRun.abort();
  checkRun = null;
  if (checkPath) checkedPaths.delete(checkPath);
  checkPath = "";
  checkView = null;
  if (editor) editor.endCheck();
  drawCheckStrip();
}

// The strip: the counts by grade, each a toggle for its marks, and under them
// the run's progress or what was searched. At most two lines (point 14).
function drawCheckStrip() {
  const on = !!checkView && checkPath === editPath && drawerMode === "note" && !detailsOn;
  checkStrip.hidden = !on;
  if (!on) return;
  const v = checkView, shown = checkShown();
  const toggle = (key, label, n) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "nc-t" + (key in v.counts ? " nc-" + key : "");
    b.setAttribute("aria-pressed", String(shown.has(key)));
    b.textContent = label;
    b.addEventListener("click", () => {
      const next = checkShown();
      next.has(key) ? next.delete(key) : next.add(key);
      try { localStorage.setItem(CHECK_SHOW_KEY, JSON.stringify([...next])); } catch { /* private window: this page only */ }
      editor.showCheck(next);
      drawCheckStrip();
    });
    if (n !== undefined) b.dataset.n = String(n);
    return b;
  };
  const glyph = { error: "■", warning: "▲", nit: "●" };
  const row = document.createElement("div");
  row.className = "nc-row";
  for (const g of CHECK_GRADES) {
    const n = v.counts[g];
    row.append(toggle(g, `${glyph[g]} ${n} ${g}${n === 1 ? "" : "s"}`, n));
  }
  row.append(toggle("not found", "not found"));
  const close = document.createElement("button");
  close.type = "button";
  close.className = "nc-close";
  close.setAttribute("aria-label", "clear the check");
  close.textContent = "×";
  close.addEventListener("click", endCheck);
  row.append(close);
  const sub = document.createElement("div");
  sub.className = "nc-sub";
  const act = (label, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "nc-act";
    b.textContent = label;
    b.addEventListener("click", fn);
    return b;
  };
  if (v.held && !checkRun) {
    // Cancel keeps the rule marks: only the model's pass waited for a go.
    sub.append(`${v.held} blocks for the model · `, act("go", () => checkSources(true)), " · ",
      act("cancel", () => { v.held = 0; drawCheckStrip(); }));
  } else if (checkRun) {
    sub.append(v.of ? `judging ${v.done} of ${v.of} · ` : "reading the vault · ", act("cancel", () => checkRun && checkRun.abort()));
  } else if (v.pool) {
    const p = v.pool;
    const where = p.scope === "whole" ? "searched the whole vault"
      : p.scope === "party" ? `${p.size} of ${p.of} notes · ${p.values.join(", ")}`
      : `${p.size} of ${p.of} notes · no party value here, so other parties' notes are out`;
    sub.textContent = where + (v.cancelled ? " · cancelled" : "") + (v.notChecked ? ` · ${v.notChecked} not checked` : "");
    sub.title = sub.textContent;
  }
  checkStrip.replaceChildren(row, sub);
}

$("#note-check-open").addEventListener("click", () => checkSources());

// --- a selected passage ------------------------------------------------------
// The whole-note asks cannot aim at a sentence; a selection can (ADR-0059).
// The bar reads CodeMirror's selection, not the DOM's, since the text is the
// editor, and shows once the pointer is released, so it never jumps about
// under a drag. It does not close on blur, or a press on its own buttons
// (and a Tab into them) would take it away first; an empty selection, a
// press anywhere else, Escape and leaving the note close it.
const selBar = $("#note-selbar");
let selDragging = false;

function hideSelBar() {
  if (selBar.hidden) return false;
  selBar.hidden = true;
  return true;
}

function syncSelBar(view) {
  const sel = view.state.selection.main;
  if (sel.empty || drawerMode !== "note" || !editPath) { hideSelBar(); return; }
  if (selDragging || !view.hasFocus) return; // shown on release, kept on blur
  placeSelBar(view);
}

// Over the selection's first line, inside the drawer; under its last when the
// top of the text has no room above it. Off screen, it hides.
function placeSelBar(view) {
  const sel = view.state.selection.main;
  const a = view.coordsAtPos(sel.from), b = view.coordsAtPos(sel.to);
  const box = view.scrollDOM.getBoundingClientRect();
  if (!a || !b || b.bottom < box.top || a.top > box.bottom) { hideSelBar(); return; }
  selBar.hidden = false;
  const p = notePanel.getBoundingClientRect(), w = selBar.offsetWidth, h = selBar.offsetHeight;
  const above = a.top - h - 6;
  const top = above >= box.top ? above : b.bottom + 6;
  const left = Math.min(Math.max(a.left - p.left, 8), p.width - w - 8);
  selBar.style.top = Math.round(top - p.top) + "px";
  selBar.style.left = Math.round(left) + "px";
}

noteEdit.addEventListener("pointerdown", () => { selDragging = true; hideSelBar(); });
document.addEventListener("pointerup", () => {
  if (!selDragging) return;
  selDragging = false;
  if (editor) syncSelBar(editor.view);
});
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#note-selbar, #note-edit")) hideSelBar();
});
noteEdit.addEventListener("scroll", () => {
  if (!selBar.hidden && editor) placeSelBar(editor.view);
}, true);

const passage = () => {
  const v = editor && editor.view, sel = v && v.state.selection.main;
  return sel && !sel.empty ? v.state.sliceDoc(sel.from, sel.to).trim() : "";
};
const quoted = (text) => text.split("\n").map((l) => "> " + l).join("\n");
// The peek's header names the ask and the start of the passage, not the
// whole turn: a quoted paragraph does not fit on one line.
const passageTitle = (verb, text) => verb + ": “" + (text.length > 48 ? text.slice(0, 47) + "…" : text) + "”";

// Not /explain, which takes a concept (its skill, ADR-0063): a passage is explained
// in the note it sits in, so the turn names the note.
$("#sel-explain").addEventListener("click", () => {
  const text = passage();
  if (!text) return;
  hideSelBar();
  drawerReader((p) => "Explain this passage from the note " + shellQuote(p)
    + ", in the context of that note:\n\n" + quoted(text), passageTitle("explain this", text));
});
// /quiz on the note with the passage as its focus, so the round is still a
// round on the note and records through silica_record_quiz.
$("#sel-quiz").addEventListener("click", () => {
  const text = passage();
  if (!text) return;
  hideSelBar();
  drawerReader((p) => "/quiz " + shellQuote(p) + " --focus=" + shellQuote(text), passageTitle("quiz me on this", text));
});
// The passage, quoted, in the composer that is showing, for a question in
// the reader's own words: the chat's on Chat, the dock on Explore and Learn.
// Calendar and Metrics show none, so there it goes to Chat.
$("#sel-ask").addEventListener("click", () => {
  const text = passage();
  if (!text || !lastNotePath) return;
  hideSelBar();
  draftInComposer("From " + shellQuote(lastNotePath) + ":\n" + quoted(text) + "\n\n");
});

// --- dock card (rendered answer for a dock- or drawer-launched turn) ---------
// Not a re-implementation of the chat flow: no tools, no thinking text. Title =
// the dispatched prompt; body = pulsing "thinking", then the answer as live
// markdown (mdLite), upgraded to the canonical OFM render on `done` — so
// wikilinks in the card open the note drawer and focus the graph. One exchange
// only; the next one replaces it. "open in chat" → the full transcript.
const peekEl = $("#peek");
let peek = null; // { body, caret, raw } while a turn is being mirrored
function openPeek(title) {
  const body = $("#peek-body");
  body.className = "";
  body.textContent = "thinking";
  const caret = document.createElement("span"); // own instance: the chat caret is a
  caret.className = "caret";                    // single element, re-parented live
  caret.textContent = "▍";
  body.appendChild(caret);
  $("#peek-title").textContent = title;
  peekEl.hidden = false;
  peek = { body, caret, raw: "", mark: 0 };
}
function closePeek() {
  peekEl.hidden = true;
  peek = null;
}
// Freeze: stop mirroring, drop the caret, leave the card up until dismissed.
function freezePeek() {
  if (!peek) return;
  peek.caret.remove();
  peek = null;
}
function peekDelta(text) {
  if (!peek) return;
  peek.raw += text;
  peek.body.innerHTML = mdLite(peek.raw);
  (peek.body.lastElementChild || peek.body).appendChild(peek.caret);
  peek.body.scrollTop = peek.body.scrollHeight;
}
// The dock mirrors the same text deltas as one flat string, so a retraction has
// to cut it back too — to the last tool block, not to zero: text the chat pane
// already committed above one still stands.
function peekMark() { if (peek) peek.mark = peek.raw.length; }
function peekRollback() {
  if (!peek) return;
  peek.raw = peek.raw.slice(0, peek.mark);
  peek.body.innerHTML = mdLite(peek.raw);
  (peek.body.lastElementChild || peek.body).appendChild(peek.caret);
}
// `done` upgrade: the server's canonical OFM render (wikilinks, callouts, math),
// same swap the chat pane does. Also covers no-delta turns (raw still empty).
function peekDone(ev) {
  if (!peek) return;
  if (ev.html || ev.answer) peek.body.innerHTML = ev.html || escapeHtml(ev.answer);
  freezePeek();
}
function peekError(msg) {

  if (!peek) return;
  peek.body.classList.add("error");
  peek.body.textContent = "error: " + msg;
  peek = null; // frozen; card stays until dismissed
}
$("#peek-open-chat").addEventListener("click", () => showTab("chat")); // showTab closes the peek
$("#peek-close").addEventListener("click", closePeek);

// --- rail resize (drag right edge, clamped) ---------------------------------
// The mirror of #note-resize below, and it exists for the same reason: the one
// line that says WHICH vault you are reading sat at the foot of a 264px column
// and was clipped there, so the answer was reachable only by hovering it.
const SIDE_MIN_W = 200, SIDE_MAX_W = 520;

function setSideW(w) {
  document.documentElement.style.setProperty("--side-w", w + "px");
}

const savedSideWidth = parseInt(localStorage.getItem("side-width"), 10);
if (savedSideWidth) setSideW(Math.min(SIDE_MAX_W, Math.max(SIDE_MIN_W, savedSideWidth)));

$("#side-resize").addEventListener("mousedown", (e) => {
  e.preventDefault();
  const startX = e.clientX, startWidth = sideW();
  const onMove = (e2) => {
    // The same prose floor the drawer obeys: widening the rail must not be a
    // way around it, or the two panes together squeeze the transcript to
    // nothing from opposite edges.
    const open = document.body.classList.contains("note-open");
    const cap = open ? window.innerWidth - MIN_PROSE - MIN_DRAWER - gutters() : SIDE_MAX_W;
    setSideW(Math.min(SIDE_MAX_W, Math.max(SIDE_MIN_W,
      Math.min(cap, startWidth + (e2.clientX - startX)))));
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    localStorage.setItem("side-width", sideW());
    fitPanes(); // the drawer's budget just changed under it
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
});

// --- note panel resize (drag left edge, clamped) ----------------------------
const NOTE_MIN_W = 280, NOTE_MAX_W = 800;
const savedNoteWidth = parseInt(localStorage.getItem("note-width"), 10);
if (savedNoteWidth) notePanel.style.width = Math.min(NOTE_MAX_W, Math.max(NOTE_MIN_W, savedNoteWidth)) + "px";
// Read the rendered width, not the inline style: with no saved width the inline
// style is "" and the old `|| 420` fallback set --note-w to 420 while the panel
// rendered at its stylesheet width, so the header and dock reserved 210px too
// little and the drawer covered #stop and #dock-send on every fresh profile.
const syncNoteW = () => setNoteW(Math.round(notePanel.getBoundingClientRect().width));
syncNoteW();
syncDrawerMode(); // the corner toggle starts hidden: nothing has been read yet
// The drawer's width is viewport-relative, so it changes with the window.
// --note-w drives the header and dock insets, so it has to follow or they reserve
// the wrong gap and the drawer covers #stop / #dock-send again. fitPanes() is the
// one that re-negotiates against the prose floor; syncNoteW is the fallback for a
// resize with the drawer closed.
window.addEventListener("resize", () => { syncNarrow(); fitPanes(); syncNoteW(); });
let resizingNote = false; // guards the outside-click-closes handler below: a drag
                           // that ends outside #note-panel fires a "click" there too
$("#note-resize").addEventListener("mousedown", (e) => {
  e.preventDefault();
  resizingNote = true;
  const startX = e.clientX, startWidth = notePanel.getBoundingClientRect().width;
  const onMove = (e2) => {
    // The drag is clamped by the same prose floor the automatic fit obeys, so a
    // user cannot hand-drag the transcript down to four words a line either.
    const cap = Math.max(MIN_DRAWER, drawerBudget(!document.body.classList.contains("sidebar-collapsed")));
    const w = Math.min(NOTE_MAX_W, cap, Math.max(NOTE_MIN_W, startWidth + (startX - e2.clientX)));
    notePanel.style.width = w + "px";
    // Read the rendered width, not the requested one: max-width can clamp the
    // drawer on a narrow window, and --note-w must never disagree with it.
    setNoteW(Math.round(notePanel.getBoundingClientRect().width));
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    localStorage.setItem("note-width", Math.round(notePanel.getBoundingClientRect().width));
    setTimeout(() => { resizingNote = false; }, 0); // clear after this click event finishes
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
});
// What the sidebar was showing BEFORE this click reached anything: the mode, or
// null for closed. Capture phase, so it is sampled before any element handler
// can change it. The outside-click rule below closes the sidebar, and the three
// modes made that rule wrong in two directions at once: a metrics or shape row
// POINTS at a note, which raises the sidebar on `work` from its own listener,
// and the delegated handler running after it on the way up would read the panel
// as open and close what the click just asked for — whether the click OPENED the
// sidebar or SWUNG it from `note`. So the rule is not "was it open" but "did this
// click leave it alone": a click that opened or moved the sidebar never closes it.
let drawerBefore = null, openBefore = 0;
const drawerNow = () => (notePanel.classList.contains("open") ? drawerMode : null);
// openSeq too: a row that opens a note does it after a fetch, so the drawer
// has not moved yet when this rule runs, and without it the click that asked
// for the note closed the drawer it was about to open in.
document.addEventListener("click", () => { drawerBefore = drawerNow(); openBefore = openSeq; }, true);
// One delegated handler: .note-link (chat OR in-panel → in-place nav) opens the
// drawer; a click outside an open drawer closes it. The sidebar and the dock
// are persistent instruments — picking a note, toggling a folder, or typing a
// question about the open note must not close the drawer or reset the graph
// focus, so they never count as "outside", except its view rows and wordmark.
document.addEventListener("click", (e) => {
  if (resizingNote) return;
  if (!e.target.closest("#note-menu, #note-title")) closeNoteMenu();
  if (!e.target.closest("#attach-menu, #attach")) closeAttachMenu();
  // dismiss the explore note-search dropdown on any click outside it (a result
  // click runs its own handler first, so pickNote still fires)
  if (!e.target.closest("#node-search-wrap")) closeNodeResults();
  // A citation and a change are two different questions, so they get two
  // different answers. `.note-link` asks what a note says, and opens the note.
  // `.wc-open` is the path on a write card, where the only thing worth reading
  // is what actually changed rather than what was claimed — so it opens the
  // diff, which falls back to the note when the session holds no baseline.
  const changed = e.target.closest(".wc-open");
  if (changed) { e.preventDefault(); openDiff(changed.dataset.path); return; }
  // A numbered citation in an answer opens at the line it stands on (openCite,
  // app-chat.js); every other link opens the note at its top.
  const link = e.target.closest(".note-link, .cite-mark");
  if (link) { e.preventDefault(); link.dataset.cite ? openCite(link) : openNote(link.dataset.path); return; }
  // An external link opens in its own tab. The app has no internal <a href> of
  // its own — every in-app move is JS — so any href in the flow came out of a
  // rendered answer or note, and following it in place tore down the SPA: the
  // turn, the open drawer and the graph focus all went with it. Scoped to
  // http(s) on purpose: a `[text](nota.md)` still resolves against the origin
  // the way it does today, rather than opening a new tab on a 404.
  const ext = e.target.closest('a[href^="http:"], a[href^="https:"]');
  if (ext) { e.preventDefault(); window.open(ext.href, "_blank", "noopener"); return; }
  // Footnotes and the two app schemes (ADR-0055); linkAction says which.
  const a = e.target.closest("a[href]");
  const act = a && window.linkAction ? window.linkAction(a.getAttribute("href")) : null;
  if (act === "footnote") {
    e.preventDefault();
    const scope = a.closest(".msg") || document;
    const id = a.getAttribute("href").slice(1);
    const target = scope.querySelector(`[id="${CSS.escape(id)}"]`);
    if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
  if (act === "scheme") { e.preventDefault(); window.location.href = a.href; return; }
  // isConnected FIRST, and not as one more `closest` — a row inside the drawer
  // whose own handler re-renders the list has already been detached by the time
  // this runs, and closest() on a detached node answers "outside" for every
  // selector below. That is what closed the sidebar on every Read-first click:
  // the panel the click asked to walk was the panel it took away.
  if (drawerBefore && drawerBefore === drawerNow() && openBefore === openSeq &&  // untouched by this click
      e.target.isConnected &&
      !e.target.closest("#note-panel") &&
      // A view row moved into the sidebar with ADR-0084, and a view switch
      // still puts the note away as the strip's tabs did: the corner toggle
      // brings it back on the new view.
      (!e.target.closest("#sidebar") || e.target.closest("#views .tab")) &&
      !e.target.closest("#dock")) closeNote();
});
$("#note-close").addEventListener("click", closeNote);
// (Escape is handled once, at the bottom of this file, in priority order.)
// Graph node clicks (in the iframe) post a message up when embedded.
window.addEventListener("message", (e) => {
  if (!e.data) return;
  // A graph node or a map card opens its note, with the facts the frame drew
  // it from (ADR-0075). A ghost node arrives with no path at all, and the
  // pane is the only surface that can say anything about an unresolved link.
  if (e.data.type === "silica-open-context") showNode(e.data);
  // Clicking the background is a deselection, and a pane still showing the
  // node you just dropped is the app disagreeing with the view. Not while the
  // drawer is on a note: that is the note being read, not a selection.
  if (e.data.type === "silica-clear-node" && !(drawerNow() === "note")) announceNode(null, null);
  // An area clicked in the frame's Colour list is a scope (ADR-0058): the
  // scope is the app's, so the picker shows it and compare is measured
  // against it, and the frame applies it when setGraphScope posts it back.
  if (e.data.type === "silica-pick-scope") setGraphScope(e.data.scope || null);
});

