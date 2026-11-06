// --- flow: the reading order around one note (ADR-0152) ----------------------
// The ladder RefD gives a note, drawn by the frame with the graph's own marks.
// This is the one place that knows how to enter it rooted somewhere.
function rootFlow(path) {
  flowRoot = path;
  if (graphMode !== "flow") setGraphMode("flow");
  else replayGraphArrange();
  closeNodeResults();
}

// --- explore note search (graph: fly the camera · flow: root the ladder) -------
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
  if (graphMode === "flow") {
    rootFlow(path);
    openNote(path);
  } else { // network: fly the graph camera to the note and open it, as a click on its node does
    const f = $("#graph-frame");
    if (f.contentWindow) f.contentWindow.postMessage({ type: "silica-goto-path", path }, "*");
    openNote(path);
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
  if (DEMO) return; // an upload is personal data (ADR-0139 point 6): a drop or a paste stages nothing
  // A pasted screenshot rides the turn; any other file may convert for minutes.
  if ([...fileList].some((f) => !(f.type || "").startsWith("image/"))) askNotify();
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
      const r = await postJson("/chat/files/add", { source });
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
  if (DEMO && e.dataTransfer.types.includes("Files")) { e.dataTransfer.dropEffect = "none"; return; }
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
// Files or a folder at the top of the vault, a link into the Inbox (web text
// waits there for the person's word), each through its lane, and no
// agent turn: a folder the student only wants searchable should not spend a
// turn. Two picker entries because one browser picker takes files or a folder,
// never both; the link is typed (ADR-0120).
const vaultAddBtn = $("#vault-add");
const vaultAddMenu = $("#vault-add-menu");
const vaultFilesInput = $("#vault-files");
const vaultFolderInput = $("#vault-folder");
const vaultImportBox = $("#vault-import");
// The server's cap per request (_UPLOAD_MAX_FILES), and ADR-0098 H2's cap per
// import: past it a folder is a job, not a pick.
const VAULT_IMPORT_BATCH = 32;
// And a cap in bytes. The edge cuts a request whose origin sends no byte for
// 125 s, and the origin cannot answer before the whole body is in: on
// 2026-11-02 t18's one batch of 53.4 MB died at 125.0 s twice, while the
// tenant socket reads 53 MB in 0.3 s, so the time went on the person's uplink
// (under 0.43 MB/s). 8 MB is 19 s at that rate; a larger file goes alone.
const VAULT_IMPORT_BATCH_BYTES = 8 * 1024 * 1024;
// import-batches:start
// The files in order, cut where either cap would be passed.
function importBatches(files, maxFiles, maxBytes) {
  const batches = [];
  let bytes = 0;
  for (const f of files) {
    const cur = batches[batches.length - 1];
    if (!cur || cur.length >= maxFiles || bytes + f.size > maxBytes) {
      batches.push([f]);
      bytes = f.size;
    } else {
      cur.push(f);
      bytes += f.size;
    }
  }
  return batches;
}
// import-batches:end
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
      const r = await postJson("/vault/fetch", { url, workspace: intoWorkspace });
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
  if (DEMO) return; // a workspace row's drop is the one way here a demo leaves open
  askNotify(); // an import converts in the background (ADR-0147)
  // A File from a folder carries its path under the picked folder; Safari can
  // hand over single files in folder mode, and those go in as files.
  const relOf = (f) => (fromFolder && f.webkitRelativePath) || "";
  const extOf = (name) => (name.match(/\.[^./]+$/) || [""])[0].toLowerCase();
  const intoWorkspace = vaultImportWorkspace;
  vaultImportWorkspace = "";
  // A picked folder holding .obsidian/ is an Obsidian vault: the server
  // remembers it, so what Silica adds can link to the person's own files
  // (ADR-0142 point 5). Read before the filter below drops hidden paths.
  const obsidian = fromFolder && files.some((f) => relOf(f).split("/")[1] === ".obsidian")
    ? relOf(files[0]).split("/")[0] : "";
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
  const batches = importBatches(wanted, VAULT_IMPORT_BATCH, VAULT_IMPORT_BATCH_BYTES);
  let sent = 0;
  for (const [n, batch] of batches.entries()) {
    renderVaultImport({ progress: `importing ${sent} of ${wanted.length}…` });
    const fd = new FormData();
    for (const f of batch) {
      fd.append("files", f, f.name);
      if (fromFolder) fd.append("paths", relOf(f));
    }
    if (n === batches.length - 1) fd.append("last", "1");
    if (intoWorkspace) fd.append("workspace", intoWorkspace);
    if (obsidian) fd.append("obsidian", obsidian);
    let r;
    try {
      r = await fetch("/vault/import", { method: "POST", body: fd });
    } catch {
      failed = "the server could not be reached";
      break;
    }
    if (!r.ok) {
      // 502 and 524 are the edge's cut at 125 s, with a page of its own and no detail.
      failed = (await r.json().catch(() => ({}))).detail || (r.status === 502 || r.status === 524
        ? `${batch.map((f) => f.name).join(", ")} did not finish uploading in the 125 s the server waits: try it again on a faster connection`
        : `the server answered ${r.status}`);
      break;
    }
    rows.push(...(await r.json()).files);
    sent += batch.length;
  }
  vaultImportLast = { rows, skipped, failed, folder, big, obsidian };
  renderVaultImport(vaultImportLast);
  if (intoWorkspace) loadWorkspaces();
  for (const r of rows) if (r.attached && (r.notes || []).includes(drawerNote.path)) refreshRaw(drawerNote.path);
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
function renderVaultImport({ progress = "", rows = [], skipped = [], failed = "", folder = "", big = 0, obsidian = "" }) {
  vaultImportBox.hidden = false;
  vaultImportBox.replaceChildren();
  const head = document.createElement("div");
  head.className = "vi-head";
  vaultImportBox.append(head);
  if (progress) {
    head.textContent = progress;
    return;
  }
  // A file its notes lost and got back (ADR-0129, amended 2026-11-04) was
  // not imported as new: it is counted apart, and offers no /nucleate.
  const attached = rows.filter((r) => r.status === "note" && r.attached);
  const done = rows.filter((r) => r.status === "note" && !r.attached);
  const refused = rows.filter((r) => r.status === "refused");
  const converting = rows.filter((r) => r.status === "queued");
  const notTaken = skipped.length + rows.filter((r) => r.status === "skipped").length;
  // The + lands at the top of the vault (or of a confined write folder), so a
  // folder's place is its rows' path up to its own name.
  const first = done[0]?.path || converting[0]?.path || "";
  const at = folder ? first.indexOf(`${folder}/`) : -1;
  const where = folder ? (at >= 0 ? first.slice(0, at + folder.length) : folder) : "the top of the vault";
  const lead = done.length ? `${done.length} imported into ${where}` : converting.length ? `into ${where}`
    : attached.length ? "" : "nothing imported";
  const parts = lead ? [lead] : [];
  if (attached.length) parts.push(`${attached.length} attached to the notes ${attached.length === 1 ? "it" : "they"} became`);
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
    // The + puts it at the top of the vault, not beside its notes: it is linked, not moved.
    ...attached.map((r) => `${r.name}: the file ${r.notes.length} ${r.notes.length === 1 ? "note" : "notes"} came from, linked to ${r.notes.length === 1 ? "it" : "them"} again and not converted`),
    ...rows.filter((r) => r.hidden).flatMap((r) => r.hidden.map((h) =>
      `${r.name}, page ${h.page}: ${h.characters} characters of hidden text left out`)),
    ...rows.filter((r) => r.pages_not_read).map((r) => `${r.name}: ${r.pages_not_read}`),
    // The way back is in another menu: said where the vault came in (ADR-0142).
    ...(obsidian && HOSTED ? ["To take the notes Silica writes back to Obsidian: SILICA menu, Download what Silica added."] : []),
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
  const view = $("#view-graph");
  // `key`: folded and shut, the frame hides its key too (syncSideFold).
  const key = !view.classList.contains("side-folded") || view.classList.contains("key-open");
  postToViews({ type: "silica-host-side", h: side.hidden ? 0 : side.offsetHeight, key });
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
let detailsLent = false; // open for the note at the centre, so they do not stick to the next note
// The PDF a source came from is its third view (ADR-0129, amended 2026-11-04),
// held the same way: once it is asked for, every source opened after it opens
// on its PDF, at the page the note starts, until the text is asked for again.
// The text stays the first view, because a citation leads to a line of it.
// A PDF opened from the rail is shown for that open alone (`rawFor`).
let rawOn = false;
let rawFor = null;
let drawerRaw = null; // the open note's raw, as /note/raw describes it
let rawTop = null;    // the strip heading its text, refilled when the raw comes back
const rawShowing = () => drawerMode === "note" && !!drawerRaw && drawerRaw.open
  && (rawOn || rawFor === drawerNote.path);

function syncDrawerMode() {
  const open = notePanel.classList.contains("open");
  const raw = rawShowing();
  const details = drawerMode === "note" && detailsOn && !raw;
  // The title belongs to a note in every view of it, in the same place; the
  // two chips to what has no title. Beside the title, the info button and
  // the pin on the text, the read glyph in details, which leads back; the
  // raw's glyph on the text and on the PDF, lit on the PDF.
  $("#note-title").hidden = drawerMode === "node";
  $("#note-info").hidden = drawerMode !== "note" || details || raw;
  $("#note-read").hidden = !details;
  $("#note-raw").hidden = drawerMode !== "note" || details || !drawerRaw || !drawerRaw.open;
  $("#note-raw").setAttribute("aria-pressed", String(raw));
  $("#note-raw").setAttribute("aria-label", raw ? "back to the text" : "the PDF this note came from");
  $("#note-raw").dataset.tip = raw ? "back to the text" : "the PDF this note came from";
  $("#note-pin").hidden = drawerMode === "node" || details;
  $("#note-changes").hidden = drawerMode !== "diff";
  $("#node-scope").hidden = drawerMode !== "node";
  $("#node-state").hidden = drawerMode !== "node";
  $("#note-edit").hidden = drawerMode !== "note" || details || raw;
  $("#note-foot").hidden = $("#note-edit").hidden;
  $("#note-centre").hidden = $("#note-edit").hidden;
  $("#note-diff").hidden = drawerMode !== "diff";
  $("#node-pane").hidden = drawerMode !== "node" && !details;
  syncRawView(raw);
  // A diff of a note this session never touched has nothing in it, so the
  // menu lists it only when there is one, and not while it is showing.
  $("#nm-read").hidden = drawerMode !== "note" || !changedPaths.has(lastNotePath);
  if (drawerMode !== "note" || details || raw) hideSelBar();
  // The corner toggle (ADR-0084) exists only while the drawer is shut AND has
  // something to reopen: what reopenDrawer restores, a note read or a node
  // pointed at. Open, the drawer's own close holds the same corner. It names
  // what it reopens, because an icon alone cannot say which note.
  const toggle = $("#drawer-toggle");
  toggle.hidden = open || !(lastViewedPath || nodePicked);
  const what = drawerMode !== "node" && lastViewedPath ? noteStem(lastViewedPath) : "";
  const label = what ? "show " + what : "show the note sidebar";
  toggle.setAttribute("aria-label", label);
  toggle.dataset.tip = label;
  document.body.classList.toggle("drawer-recall", !toggle.hidden);
  renderNoteChip(); // the composers' chip follows the note the drawer holds
  drawCheckStrip(); // the check's strip heads the text, so it goes where the text goes
}

// A vault switch leaves nothing of the old vault to reopen.
function forgetDrawer() {
  if (fieldHeld(centreNote)) dropField(centreNote); // its path means nothing in the new vault
  if (activeTab === "note") showTab("chat"); // lets the note at the centre go, into the vault it came from
  if (notePanel.classList.contains("open")) closeNote(true);
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
  if (detailsLent) { detailsLent = false; detailsOn = false; } // they were the note at the centre's
  // The note already open, on its text or its details: clicking its node again
  // or a row naming it has nothing new to load, and a reload would drop the
  // caret and the scroll for nothing.
  if (path === drawerNote.path && drawerMode === "note" && notePanel.classList.contains("open")) {
    if (line && drawerNote.editor) drawerNote.editor.revealLine(line);
    if (ctxPath !== path) loadContext(path, facts);
    return true;
  }
  // A note another editor holds (Learn's write view, ADR-0134 point 6) goes
  // there instead of loading twice.
  const held = heldElsewhere(path, drawerNote);
  if (held) { if (held.reveal) held.reveal(line); return false; }
  closeNoteMenu();
  if (!(await releaseNote())) return false; // what was typed into the last note reaches the disk first
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
  // Named by a bare title, the note turned out to be the other editor's: the
  // drawer has already let its own go, so it shuts rather than show bytes it
  // no longer saves.
  const holder = heldElsewhere(data.path, drawerNote);
  if (holder) { closeNote(); if (holder.reveal) holder.reveal(line); return false; }
  lastNotePath = data.path;
  lastViewedPath = data.path;
  drawerMode = "note";
  holdNote(drawerNote, data);
  drawerRaw = data.raw || null;
  if (rawFor !== data.path) rawFor = null;
  const top = noteTopEl(data);
  rawTop = data.raw && !data.paper && !data.saved ? top : null;
  ed.load(data.text, { top, missing: data.broken || [] });
  paintFoot(drawerNote);
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
  if (on) { rawOn = false; rawFor = null; } // details are asked for over the PDF too: Alt+Enter
  syncDrawerMode();
  if (on && ctxPath !== lastNotePath) loadContext(lastNotePath);
  // Details of the note at the centre (centreDetails) have no text here to go back to.
  if (!on && drawerNote.path !== lastNotePath) { closeNote(); return true; }
  if (!on && drawerNote.editor) drawerNote.editor.view.requestMeasure(); // it was measured while hidden
  // The control that was pressed is hidden now: focus goes to the one that
  // took its place, rather than to the page.
  const f = document.activeElement;
  if (inPanel && (!f || !f.offsetParent)) $(on ? "#note-read" : "#note-info").focus();
  return true;
}
$("#note-info").addEventListener("click", () => setDetails(true));
$("#note-read").addEventListener("click", () => setDetails(false));
$("#note-raw").addEventListener("click", () => setRaw(!rawShowing()));

// The text and the PDF of one source. False when nothing changed, so the
// Escape list can stop at a press that took the reader back to the text.
function setRaw(on) {
  if (drawerMode !== "note" || !drawerRaw || !drawerRaw.open || rawShowing() === on) return false;
  rawOn = on;
  rawFor = null;
  syncDrawerMode();
  markOpenNote();
  if (!on && drawerNote.editor) drawerNote.editor.view.requestMeasure(); // it was measured while hidden
  return true;
}

// The rail's PDF row: the note that names it, on its PDF, for this open.
async function openRaw(path) {
  rawFor = path;
  const ok = await openNote(path);
  if (ok === true) { rawFor = drawerNote.path; syncDrawerMode(); markOpenNote(); }
}
window.openRaw = openRaw;

const rawUrl = (path, extra = "") => "/raw?path=" + encodeURIComponent(path) + extra;

// What a source says of its raw, over its text and over its PDF: the file,
// the pages the note spans, the other view, the download. A raw the server
// deleted before 2026-11-04 is named as gone, with the way to bring it back
// when its hash lets the vault's + recognise it. Built from text nodes only:
// every word here is a file name from the person's vault.
function fillRawStrip(el, raw, path, inRaw) {
  el.className = raw.kept ? "note-paper raw-strip" : "note-saved raw-gone";
  el.replaceChildren();
  const line = mkEl("span", "");
  const name = mkEl("b", "", raw.name);
  let where = "";
  if (raw.from && raw.to) where = raw.from === raw.to ? `page ${raw.from}` : `pages ${raw.from} to ${raw.to}`;
  if (where && raw.pages) where += ` of ${raw.pages}`;
  if (!where && raw.pages) where = `${raw.pages} pages`;
  if (raw.parts) where += `${where ? ", " : ""}in ${raw.parts} parts`;
  line.append("From ", name, where ? `, ${where}` : "");
  if (!raw.kept) {
    line.append(raw.gone === "removed" ? ". The server removed the file after converting it."
      : ". The file is not in the vault any more.");
    el.append(line);
    if (raw.again && !DEMO) {
      const again = mkEl("button", "raw-again", "Import it again");
      again.type = "button";
      again.title = "the same file is attached to this note, not converted again";
      again.addEventListener("click", () => vaultFilesInput.click());
      el.append(again);
    }
    return el;
  }
  el.append(line);
  if (raw.open) {
    const b = mkEl("button", "wk-paper-a", inRaw ? "Text" : "PDF");
    b.type = "button";
    b.addEventListener("click", () => setRaw(!inRaw));
    el.append(b);
  }
  el.append(Object.assign(document.createElement("a"),
    { className: "wk-paper-a", textContent: "Download", href: rawUrl(path, "&download=1") }));
  return el;
}

// The PDF in the browser's own viewer, at the page the note starts. A new
// note is a new address, so the frame loads it; the same address is left
// alone, or the reader's place in it would be lost.
function syncRawView(on) {
  const view = $("#raw-view");
  view.hidden = !on;
  if (!on) return;
  fillRawStrip($("#raw-head"), drawerRaw, drawerNote.path, true);
  const url = rawUrl(drawerNote.path) + (drawerRaw.from ? `#page=${drawerRaw.from}` : "");
  const frame = $("#raw-frame");
  if (frame.dataset.url !== url) {
    frame.dataset.url = url;
    frame.title = `${drawerRaw.name}, the PDF of this note`;
    frame.src = url;
  }
}

// The raw came back (an import recognised it): the strip says so without a
// reload of the note, which would cost the caret and the scroll.
async function refreshRaw(path) {
  if (!rawTop || drawerNote.path !== path) return;
  try {
    const data = await (await fetch("/note/raw?path=" + encodeURIComponent(path))).json();
    if (drawerNote.path !== path || !data.raw) return;
    drawerRaw = data.raw;
    fillRawStrip(rawTop, data.raw, path, false);
    if (drawerNote.editor) drawerNote.editor.view.requestMeasure();
    syncDrawerMode();
  } catch { /* the strip keeps saying the file is gone; the next open reads it again */ }
}
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

// What stands over a note's text, the same in the drawer and at the centre: a
// paper's byline, a saved note's author (ADR-0143 point 11), or the file a
// source came from (ADR-0129).
function noteTopEl(data) {
  return data.paper ? paperStripEl(data.paper)
    : data.saved && typeof savedStripEl === "function" ? savedStripEl(data.saved)
    : data.raw ? fillRawStrip(mkEl("div", ""), data.raw, data.path, false) : null;
}

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
  if (!(await releaseNote())) return; // the diff has to include what was just typed
  lastNotePath = path;
  lastViewedPath = path;
  drawerMode = "diff";
  syncDrawerMode();
  focusGraphNode(path);
  const box = $("#note-diff");
  box.className = "dl-wait";
  box.textContent = "reading the diff…";
  showDrawer(noteStem(path));
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

function closeNote(force = false) {
  // Text only the field holds keeps the panel open: shutting it would drop the text.
  if (fieldHeld(drawerNote)) {
    if (!force) { sayHeld(drawerNote); return; }
    dropField(drawerNote); // the note itself is gone, or its vault is
  }
  if (detailsLent) { detailsLent = false; detailsOn = false; }
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
// hands off to: the drawer knows which note it is on and nothing else about
// explore's modes.
window.openLadder = (note) => {
  if (!note) return;
  rootFlow(note);
  document.querySelector('.tab[data-tab="graph"]').click();
};

// The selection bar's two asks dispatch a turn about the open note. The
// drawer stays open (the peek dock tucks under it and mirrors the turn), so
// the note you launched from is never lost.
// Backslashes too: shlex reads `\"` inside quotes as a quote, so a passage
// ending in `\` would otherwise swallow its own closing quote.
const shellQuote = (s) => '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
function drawerReader(makeCmd, peekTitle, path = lastNotePath) {
  if (!path || streaming) return; // streaming: send() would no-op — no peek either
  const cmd = makeCmd(path, $("#note-title-text").textContent.trim());
  if (activeTab !== "chat") openPeek(peekTitle || cmd); // on chat the stream is already visible
  send(cmd);
}
// The text in the composer that is showing: the chat's on Chat, the dock on
// Explore and Learn. Calendar and Metrics show none, so there it goes to Chat.
// Nothing is sent: the reader finishes the question.
function draftInComposer(draft) {
  if (activeTab === "note") $("#dock").hidden = false; // the view's writing room, lent for this one question
  const box = activeTab === "chat" ? $("#input") : !$("#dock").hidden ? $("#dock-input") : null;
  if (!box) { prefillChat(draft); return; }
  box.value = draft + box.value;
  box.focus();
  box.setSelectionRange(draft.length, draft.length);
  box.dispatchEvent(new Event("input")); // let the autosize/palette hooks see it
}
// The menu's ask (ADR-0075): a question about the note. The composer already
// carries the note as its chip, so this only puts the caret there; it used to
// write an English sentence naming the note, which an Italian vault then
// had to delete (ADR-0097, amendment 2026-10-25).
$("#note-chat").addEventListener("click", () => {
  if (!lastNotePath) return;
  closeNoteMenu();
  noteChipSkip = "";
  renderNoteChip();
  if (activeTab !== "chat" && $("#dock").hidden) showTab("chat");
  (activeTab === "chat" ? $("#input") : $("#dock-input")).focus();
});

// --- the open note as the next message's context (ADR-0097, 2026-10-25) ----
// A note open beside the chat is what "this article" means, so the composer
// shows it as a chip and the turn sends its path (server: named_notes'
// open_note). The chip stays from message to message while the note is open,
// since the reader is still looking at it; its x leaves the note out of the
// next message only, and the "+ title" left in its place puts it back.
let noteChipSkip = "";   // the path the next message leaves out

function chatNote() {
  if (activeTab === "note" && centreNote.path) return centreNote.path;
  const open = notePanel.classList.contains("open") && drawerMode !== "node";
  return open && lastNotePath && lastNotePath !== noteChipSkip ? lastNotePath : "";
}

// Sent: the x was for that one message.
function noteSent() {
  if (!noteChipSkip) return;
  noteChipSkip = "";
  renderNoteChip();
}

const NOTE_GLYPH = '<svg aria-hidden="true" focusable="false" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>';

function renderNoteChip() {
  const open = notePanel.classList.contains("open") && drawerMode !== "node" && !!lastNotePath;
  // The x was for this note and the next message: another note, or the same
  // one closed and reopened, starts with its chip.
  if (!open || noteChipSkip !== lastNotePath) noteChipSkip = "";
  const title = open ? ($("#note-title-text").textContent.trim() || noteStem(lastNotePath)) : "";
  for (const row of document.querySelectorAll(".note-chip-row")) {
    const field = row.closest("form").querySelector("textarea");
    row.innerHTML = "";
    row.hidden = !open;
    if (!open) continue;
    if (lastNotePath === noteChipSkip) {
      const add = mkEl("button", "note-chip-add", "+ " + title);
      add.type = "button";
      add.title = "read " + title + " with the next message";
      add.addEventListener("click", () => { noteChipSkip = ""; renderNoteChip(); field.focus(); });
      row.appendChild(add);
      continue;
    }
    const chip = mkEl("span", "chip note-chip");
    chip.innerHTML = NOTE_GLYPH;
    chip.appendChild(mkEl("span", "chip-name", title));
    chip.title = "the note open beside the chat: each message reads it while it stays open";
    const x = mkEl("button", "chip-x");
    x.type = "button";
    x.setAttribute("aria-label", "send the next message without " + title);
    x.title = "the next message without this note";
    x.innerHTML = '<svg aria-hidden="true" focusable="false" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"></line><line x1="18" y1="6" x2="6" y2="18"></line></svg>';
    x.addEventListener("click", () => { noteChipSkip = lastNotePath; renderNoteChip(); field.focus(); });
    chip.appendChild(x);
    row.appendChild(chip);
  }
}

// Under the question, as its files are: the note the turn read because it was
// open, live and on a reopened chat (`note` of /messages).
function userNote(body, path) {
  if (!body || !path) return;
  const row = mkEl("div", "msg-note");
  const chip = mkEl("span", "mf");
  chip.appendChild(mkEl("span", "mf-in", "with"));
  const name = mkEl("button", "mf-name", noteStem(path));
  name.type = "button";
  name.title = "open " + path;
  name.addEventListener("click", () => openNote(path));
  chip.appendChild(name);
  row.appendChild(chip);
  body.after(row);
}
// Test me and Explain it (ADR-0119): Learn's home no longer lists every
// note, so a test on this one starts here. Learn's own functions run them.
$("#note-find").addEventListener("click", () => { closeNoteMenu(); findInNote(drawerNote); });
$("#note-test").addEventListener("click", () => { closeNoteMenu(); if (lastNotePath) learnTestNote(lastNotePath); });
$("#note-teach").addEventListener("click", () => { closeNoteMenu(); if (lastNotePath) learnExplain(lastNotePath); });
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
let editorLoad = null;  // the drawerNote.editor's two scripts, fetched once for every drawerNote.editor

// One note in one editor (ADR-0134 point 6). The drawer holds one session and
// Learn's write view another; saving, releasing and flushing take the one
// they act on, and everything else acts on the drawer's. A note is held by
// one session at a time, so two editors never race on one file's etag.
function noteSession() {
  return {
    editor: null,
    path: "",       // the note the field holds; "" when it holds none
    etag: "",       // the disk's bytes as last seen, for the save to check
    clean: "",      // the text the disk holds as far as the field knows
    conflict: false,
    timer: 0,
    saving: null,   // the in-flight save; a second one waits for it
    again: false,   // a save asked for while one was in flight
    onRelease: null, // what the place does when its note is let go
    // ADR-0155 point 10: what the foot line says, from the server's answers.
    phase: "",      // "" | "saving" | "saved" | "retry" | "conflict"
    at: null,       // when the server last said saved
    disk: null,     // a 409's own bytes, for Take the disk's and Compare
    retry: 0,       // the 5 s timer of a save that failed
    touched: false, // a save landed since the note was held: the index is owed a refresh
    broken: [],     // the links that resolve nowhere, as the server last said
    // a note not yet on disk (ADR-0155 point 7): `folder` is where it will be born
    fresh: false,
    folder: "",
    // the surface: its foot line, its strip, its bar, the box they sit in
    foot: null,
    strip: null,
    bar: null,
    host: null,
    // the source check on this session's note (ADR-0110)
    ck: { path: "", run: null, view: null },
  };
}
const noteSessions = [];
const drawerNote = noteSession();
noteSessions.push(drawerNote);
Object.assign(drawerNote, { foot: $("#note-foot"), strip: $("#note-check"), bar: $("#note-selbar"), host: notePanel });

// The session other than `s` that holds this note, if any.
const heldElsewhere = (path, s) => noteSessions.find((x) => x !== s && x.path && x.path === path) || null;

// The field takes a note it read from /note/raw: false when another editor
// holds that note, which then keeps it.
function holdNote(s, data) {
  if (heldElsewhere(data.path, s)) return false;
  s.path = data.path; s.etag = data.etag; s.clean = data.text;
  s.conflict = false;
  s.phase = ""; s.at = null; s.disk = null; s.touched = false; s.broken = data.broken || [];
  clearTimeout(s.retry);
  return true;
}

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
function loadEditor() {
  editorLoad ||= loadScript("/static/codemirror.min.js")
    .then(() => loadScript("/static/app-editor.js"))
    .catch((e) => { editorLoad = null; throw e; });
  return editorLoad;
}

// The hooks every note editor takes, bound to its session; `extra` is what
// one place does differently.
function sessionHooks(s, extra = {}) {
  return {
    render: (text) => postJson("/note/render", { text }).then((r) => (r.ok ? r.json() : Promise.reject(r.status))).then((d) => d.html || ""),
    openNote: openLinkedNote,
    change: () => typed(s),
    blur: () => { saveEdit(s); },
    save: () => { saveEdit(s); },
    keep: (x) => keepWebSource(s, x),
    extra: () => noteExtensions(s),
    decorate: decorateRendered,
    openAt: (path, line) => openNote(path, line || undefined),
    links: (paths) => paths.map((p) => `[[${mentionLink(p)}]]`), // a tree row dropped on the note
    ...extra,
  };
}

// A session's editor in `parent`, built once; null when the scripts failed.
function sessionEditor(s, parent, extra) {
  return loadEditor().then(() => (s.editor ||= window.LiveEditor.create(parent, sessionHooks(s, extra))), () => null);
}

function ensureEditor() {
  return sessionEditor(drawerNote, noteEdit, {
    blur: () => { saveEdit(drawerNote); recheck(); },
    select: (view) => { syncSelBar(view, drawerNote); recheck(); },
  });
}
drawerNote.onRelease = () => {
  if (drawerNote.ck.run) drawerNote.ck.run.abort(); // the verdicts paid for are cached, and come back on reopening
  hideSelBar();
};

// What the server drew inside the editor gets what the reader's copy got:
// diagrams run, and the properties box opens as it was last left. Diagrams go
// through the chat's drawDiagrams, not renderMermaid alone: on 2026-10-28 a
// mind map the chat wrote into a note drew here as an empty bar, and quoting
// its labels, which a reply already gets, is all it took to draw it.
function decorateRendered(el, view) {
  drawDiagrams(el, { keepStyles: true });
  const fm = el.querySelector("details.fm");
  if (fm) {
    fm.open = localStorage.getItem("note-props") === "open";
    fm.addEventListener("toggle", () => {
      localStorage.setItem("note-props", fm.open ? "open" : "closed");
      view.requestMeasure(); // the box changed height under the drawerNote.editor
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
// `unless`, read once the saves are done: the person came back, so it stays held.
async function releaseNote(s = drawerNote, unless = null) {
  if (!s.path) return true;
  if (s.onRelease) s.onRelease();
  await saveEdit(s);
  while (s.saving) await s.saving;
  if (unless && unless()) return false;
  // A conflict or a failed save leaves the person's text in the field and
  // nowhere else, so the field keeps its note: a false here stops the caller
  // from loading anything over it. Text is never dropped to make room.
  if (fieldHeld(s)) { sayHeld(s); return false; }
  // The vectors and the co-occurrence follow the person (ADR-0155 point 18):
  // once per let-go, here, and not per save. The server's own 60 s timer is what
  // a page that never lets go falls back on, so a lost call costs a minute.
  if (s.touched) {
    fetch("/note/let-go", {
      method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
      body: JSON.stringify({ path: s.path }),
    }).catch(() => { /* the 60 s timer still runs */ });
  }
  s.path = ""; s.etag = ""; s.clean = ""; s.conflict = false;
  s.phase = ""; s.at = null; s.disk = null; s.touched = false; s.broken = [];
  clearTimeout(s.timer);
  clearTimeout(s.retry);
  return true;
}

// The field holds text the disk does not, and saving cannot put it there now.
const fieldHeld = (s) => !!s.editor && (s.fresh
  ? (s.phase === "retry" || s.phase === "refused") && !!s.editor.getText().trim()
  : !!s.path && (s.conflict || s.phase === "retry" || s.phase === "gone"));

function sayHeld(s) {
  const away = s === centreNote && activeTab !== "note";
  notify(s.conflict ? "This note changed on disk: pick Keep mine or Take the disk's first"
    : s.phase === "gone" ? "This note's file is gone: pick Write it back or Let it go first"
    : "Not saved yet: your text stays in the note until the save lands", "error",
         away ? { label: "back to it", run: () => { showTab("note"); syncUrl(true); } } : null);
}

// For a note that is gone with its file or its vault: no save can land.
function dropField(s) {
  s.conflict = false;
  s.phase = "";
  s.disk = null;
  if (s.editor) s.clean = s.editor.getText();
  clearTimeout(s.retry);
}

// An agent's write or an /undo can land on a note an editor holds. While
// nothing typed is waiting to be saved, the field takes the disk's bytes as one
// mapped change, so the caret stays where it was; with typing in it, the
// next save's 409 says so instead, as it did before the editor was always
// open. Called from loadChanges, which runs after every turn and every undo.
function refreshOpenNote() {
  return Promise.all(noteSessions.map(refreshSession));
}

async function refreshSession(s) {
  const path = s.path, ed = s.editor;
  if (!path || !ed || s.saving || s.conflict || ed.getText() !== s.clean) return;
  const r = await fetch("/note/raw?path=" + encodeURIComponent(path)).catch(() => null);
  const d = r && r.ok ? await r.json().catch(() => null) : null;
  if (!d || s.path !== path || s.saving || ed.getText() !== s.clean || d.etag === s.etag) return;
  s.etag = d.etag;
  s.clean = d.text;
  s.broken = d.broken || [];
  ed.adopt(d.text);
  ed.setMissing(s.broken);
  paintFoot(s);
}

function scheduleSave(s = drawerNote) {
  clearTimeout(s.timer);
  s.timer = setTimeout(() => saveEdit(s), 800);
}

// A move from the tree (ADR-0126 point 3): the panel's note follows its path,
// reopened so the title and the URL say the new one; a note whose links the
// move rewrote takes the disk's bytes, or the next save would write the old
// link back and meet a 409. `to` maps old paths to new. Another editor's
// note keeps its place and takes the new path.
function followNote(to, rewritten) {
  for (const s of noteSessions) {
    if (s === drawerNote || !s.path) continue;
    if (to.has(s.path)) { s.path = to.get(s.path); if (s.onMoved) s.onMoved(); }
    else if (rewritten.includes(s.path)) refreshSession(s);
  }
  if (!drawerNote.path) return;
  if (to.has(drawerNote.path)) {
    const path = to.get(drawerNote.path);
    if (lastViewedPath === drawerNote.path) lastViewedPath = path;
    drawerNote.path = ""; // or openNote would take it for the note already open
    openNote(path);
  } else if (rewritten.includes(drawerNote.path)) {
    refreshSession(drawerNote);
  }
}

async function saveEdit(s = drawerNote) {
  clearTimeout(s.timer);
  if (s.fresh) return createFresh(s);
  if (!s.path || s.conflict || s.phase === "gone" || !s.editor) return;
  if (s.saving) { s.again = true; return s.saving; }
  const path = s.path, ed = s.editor, text = ed.getText();
  if (text === s.clean) {
    // typed and taken back before the save fired: the field is the disk's again
    if (s.phase === "saving") setPhase(s, s.at ? "saved" : "");
    return;
  }
  s.saving = (async () => {
    try {
      const r = await postJson("/note/save", { path, text, etag: s.etag });
      const data = await r.json().catch(() => ({}));
      if (s.path !== path) return; // the field moved on; this save is history
      if (r.status === 409) {
        // A choice and not a race (ADR-0155 point 11): the typed text stays in
        // the field, no further keystroke writes, and the foot line asks.
        s.etag = data.etag || s.etag;
        s.conflict = true;
        s.disk = data.text;
        setPhase(s, "conflict");
        return;
      }
      // The file left behind the app: no retry can land, so the person is asked.
      if (r.status === 404) { setPhase(s, "gone"); return; }
      if (!r.ok) throw new Error(String(r.status));
      s.etag = data.etag;
      // What landed may differ from what was sent (overwrite stamps a `type:`
      // into frontmatter that lacks it). If nothing was typed meanwhile the
      // field takes the disk's bytes as one mapped change, so the two agree;
      // otherwise the next save carries the newer text anyway. Either way the
      // clean text is the disk's, the one the new etag names.
      if (data.text !== text && ed.getText() === text) ed.adopt(data.text);
      s.clean = data.text;
      s.touched = true;
      s.at = new Date();
      if (data.broken) { s.broken = data.broken; ed.setMissing(data.broken); }
      setPhase(s, ed.getText() === data.text ? "saved" : "saving");
      if (s.onSaved) s.onSaved();
      loadChanges(); // the menu's diff and the write cards see the edit
      // a workspace note's row reads its name, lines and instructions (ADR-0112)
      if (typeof workspaces !== "undefined" && workspaces.some((w) => w.path === path)) loadWorkspaces();
    } catch {
      // The text stays in the field and the foot line says so: a toast leaves,
      // and a person writing for an hour may not be looking at it.
      if (s.path === path) {
        setPhase(s, "retry");
        clearTimeout(s.retry);
        s.retry = setTimeout(() => saveEdit(s), 5000);
      }
    } finally {
      s.saving = null;
      if (s.again) { s.again = false; saveEdit(s); }
    }
  })();
  return s.saving;
}

// Typing: the foot line says Saving… at once, and only the server's answer says
// Saved. A note in conflict waits for the person's choice, and typing is not one.
function typed(s) {
  if (s.conflict || s.phase === "gone") { paintFoot(s); return; }
  // A field with no note under it must not swallow words: at the centre they
  // become a new note, and the drawer shuts on a note it no longer holds, so
  // reaching this there is a defect that a toast at least says out loud.
  if (!s.path && !s.fresh) {
    if (s === centreNote) s.fresh = true;
    else { notify("This field holds no note, so what you type here is not saved"); return; }
  }
  setPhase(s, "saving");
  scheduleSave(s);
  if (s.onChange) s.onChange();
}

function setPhase(s, phase) {
  s.phase = phase;
  paintFoot(s);
}

const fmEnd = (text) => { const m = /^---[ \t]*\n[\s\S]*?\n---[ \t]*(?:\n|$)/.exec(text); return m ? m[0].length : 0; };

// The save state now; the counts a beat later, since they read the whole text.
function paintFoot(s) {
  const foot = s.foot;
  if (!foot || !s.editor) return;
  const st = foot.querySelector(".nv-st"), text = s.editor.getText();
  st.className = "nv-st";
  st.textContent = "";
  if (s.fresh && !text.trim()) st.textContent = "Nothing written yet";
  else if (s.phase === "saving") st.textContent = "Saving…";
  else if (s.phase === "refused") {
    st.classList.add("warn");
    st.append(mkEl("span", "dot"), document.createTextNode("Not saved: the reason is under the title"));
  } else if (s.phase === "gone") {
    st.classList.add("warn");
    st.append(mkEl("span", "dot"), document.createTextNode("Its file is gone"));
    const act = (label, fn) => { const b = mkEl("button", "act", label); b.type = "button"; b.addEventListener("click", fn); st.append(b); };
    act("Write it back", () => writeBack(s));
    act("Let it go", () => letItGo(s));
  } else if (s.phase === "retry") {
    st.classList.add("warn");
    st.append(mkEl("span", "dot"), document.createTextNode("Not saved: retrying. Your text stays here"));
  } else if (s.phase === "conflict") {
    st.classList.add("warn");
    st.append(mkEl("span", "dot"), document.createTextNode("Changed on disk"));
    const act = (label, fn) => { const b = mkEl("button", "act", label); b.type = "button"; b.addEventListener("click", fn); st.append(b); };
    act("Keep mine", () => keepMine(s));
    act("Take the disk's", () => takeDisk(s));
    act("Compare", () => openCompare(s));
  } else if (s.at) st.textContent = `Saved ${s.at.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
  clearTimeout(s.countTimer);
  s.countTimer = setTimeout(() => paintCounts(s), 250);
}

function paintCounts(s) {
  const ct = s.foot.querySelector(".nv-ct"), text = s.editor.getText(), body = text.slice(fmEnd(text));
  const words = (body.replace(/[#>*_`=|[\]-]/g, " ").match(/\S+/g) || []).length;
  const links = [...body.matchAll(/(?<!!)\[\[[^[\]\n]+?\]\]/g)].length;
  ct.textContent = "";
  ct.append(mkEl("span", "", `${words} ${words === 1 ? "word" : "words"}`));
  if (s !== drawerNote || links) ct.append(mkEl("span", "opt", `${links} ${links === 1 ? "link" : "links"}`));
  if (s.broken.length) ct.append(mkEl("span", "opt", `${s.broken.length} unresolved`));
  if (s.path) {
    const h = mkEl("button", "nv-link", "History");
    h.type = "button";
    h.addEventListener("click", () => noteHistory(s));
    ct.append(h);
  }
}

// The drawer's details are the one home of a note's history, whichever
// surface holds its text.
function noteHistory(s) {
  if (s !== drawerNote) { centreDetails("history"); return; }
  setDetails(true);
  if (window.showNoteHistory) window.showNoteHistory();
}

function keepMine(s) {
  s.conflict = false;
  s.clean = s.disk; // what the fresh etag names, so the field reads as changed
  s.disk = null;
  closeCompare(s);
  setPhase(s, "saving");
  saveEdit(s);
}

// The file went behind the app: the field's text is the only copy, so it goes back
// where it was, as a new note. The vault may refuse (the name taken since, a folder
// of Silica's), and then the field stays as it is and the toast says why.
async function writeBack(s) {
  const path = s.path, at = path.lastIndexOf("/");
  let r = null, d = {};
  try {
    r = await postJson("/note/new", { folder: at < 0 ? "" : path.slice(0, at), name: noteStem(path), text: s.editor.getText() });
    d = await r.json().catch(() => ({}));
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify(d.detail || "couldn't write the note back"); return; }
  s.etag = d.etag;
  s.clean = d.text;
  s.broken = d.broken || [];
  s.editor.setMissing(s.broken);
  s.at = new Date();
  s.touched = true;
  setPhase(s, "saved");
  loadVaultInfo();
}

function letItGo(s) {
  dropField(s);
  if (s === drawerNote) { closeNote(); return; }
  releaseNote(s).then(() => { if (activeTab === "note") showTab("chat"); });
}

// Read again on the press: the bytes of the 409 may be old by now, and only the
// disk's own bytes can honestly read as Saved.
async function takeDisk(s) {
  const r = await fetch("/note/raw?path=" + encodeURIComponent(s.path)).catch(() => null);
  const now = r && r.ok ? await r.json().catch(() => null) : null;
  if (now) { s.disk = now.text; s.etag = now.etag; }
  const disk = s.disk;
  s.conflict = false;
  s.disk = null;
  s.clean = disk;
  s.at = new Date();
  closeCompare(s);
  s.editor.take(disk);
  setPhase(s, "saved");
}

// Line by line, what the field holds against what the disk holds. The two share
// their head and tail in the usual case, so the table is for the middle; past
// 2,000 by 2,000 lines it shows the two middles whole rather than stall the page.
function lineDiff(mine, disk) {
  const a = mine.split("\n"), b = disk.split("\n");
  let i = 0, z = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  while (z < a.length - i && z < b.length - i && a[a.length - 1 - z] === b[b.length - 1 - z]) z++;
  const x = a.slice(i, a.length - z), y = b.slice(i, b.length - z), out = [];
  if (x.length * y.length > 4e6) {
    for (const l of x) out.push(["del", l]);
    for (const l of y) out.push(["add", l]);
    return out;
  }
  const t = Array.from({ length: x.length + 1 }, () => new Uint32Array(y.length + 1));
  for (let p = x.length - 1; p >= 0; p--) for (let q = y.length - 1; q >= 0; q--) {
    t[p][q] = x[p] === y[q] ? t[p + 1][q + 1] + 1 : Math.max(t[p + 1][q], t[p][q + 1]);
  }
  let p = 0, q = 0;
  while (p < x.length || q < y.length) {
    if (p < x.length && q < y.length && x[p] === y[q]) { out.push(["same", x[p]]); p++; q++; }
    else if (q >= y.length || (p < x.length && t[p + 1][q] >= t[p][q + 1])) out.push(["del", x[p++]]);
    else out.push(["add", y[q++]]);
  }
  return out;
}

function closeCompare(s) {
  if (s.compare) { s.compare.remove(); s.compare = null; }
}

// Rows are DOM nodes, never innerHTML: every line is a line of the vault.
function openCompare(s) {
  closeCompare(s);
  const box = mkEl("div", "nv-compare");
  box.setAttribute("role", "region");
  box.setAttribute("aria-label", "your text against the disk's");
  const head = mkEl("div", "nv-compare-head");
  head.append(mkEl("span", "", "Red is what you typed, green is what the disk holds"));
  const x = mkEl("button", "nc-close", "\u00D7");
  x.type = "button";
  x.setAttribute("aria-label", "close the comparison");
  x.addEventListener("click", () => closeCompare(s));
  head.append(x);
  const rows = mkEl("div", "nv-compare-rows");
  for (const [kind, line] of lineDiff(s.editor.getText(), s.disk ?? "")) {
    if (kind === "same") continue;
    rows.append(mkEl("div", kind === "add" ? "dl-add" : "dl-del", line || " "));
  }
  if (!rows.childElementCount) rows.append(mkEl("div", "", "The two are the same."));
  box.append(head, rows);
  s.host.append(box);
  s.compare = box;
  x.focus();
}

// A reload or a closed tab with an unsaved edit: the beacon outlives the page.
addEventListener("pagehide", () => {
  for (const s of noteSessions) {
    if (s.fresh && s.editor && s.editor.getText().trim()) {
      navigator.sendBeacon("/note/new", new Blob(
        [JSON.stringify({ folder: s.folder, name: freshName() === s.nameRefused ? "" : freshName(), text: s.editor.getText() })],
        { type: "application/json" }));
      continue;
    }
    if (!s.path || s.conflict || !s.editor || s.editor.getText() === s.clean) continue;
    navigator.sendBeacon("/note/save", new Blob(
      [JSON.stringify({ path: s.path, text: s.editor.getText(), etag: s.etag })],
      { type: "application/json" }));
  }
});

// The beacon cannot carry a conflict or a save that is failing, so the browser's
// own question is the last thing standing between that text and a closed tab.
addEventListener("beforeunload", (e) => {
  if (noteSessions.some(fieldHeld)) e.preventDefault();
});

// --- checking the note's sources (ADR-0110 step 3) ----------------------------
// `check sources` reads the note on disk against the vault: the rule findings
// land at once, the model's block by block, as marks on the text
// (app-editor.js) and counts on the strip above it. A request of its own, so
// the chat stays free; cancel drops it. A note checked in this page comes
// back with its marks when it is reopened, from the server's cache and with
// no model call, so following a box's passage and coming back loses nothing.
const CHECK_SHOW_KEY = "check-show";
const CHECK_GRADES = ["error", "warning", "nit"];
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
async function flushForCheck(s = drawerNote) {
  await saveEdit(s);
  while (s.saving) await s.saving;
  return !s.conflict && s.editor.getText() === s.clean;
}

function countFindings(findings) {
  const c = { error: 0, warning: 0, nit: 0 };
  for (const f of findings) c[f.grade]++;
  return c;
}

async function checkSources(confirm = false, s = drawerNote) {
  if (!s.path || !s.editor) return;
  const c = s.ck;
  if (c.run) c.run.abort();
  if (!(await flushForCheck(s))) { notify("this note changed on disk; reopen it before checking"); return; }
  const path = s.path, ctl = new AbortController();
  c.run = ctl;
  c.path = path;
  checkedPaths.add(path);
  const seen = [];
  c.view = { counts: countFindings(seen), pool: null, done: 0, of: 0, notChecked: 0 };
  s.editor.startCheck();
  s.editor.showCheck(checkShown());
  drawCheckStrip(s);
  const live = () => c.run === ctl && s.path === path;
  try {
    const r = await fetch("/note/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, confirm }), signal: ctl.signal,
    });
    if (!r.ok) throw new Error(String(r.status));
    await readLines(r, (ev) => {
      if (!live()) return;
      if (ev.event === "rules" || ev.event === "confirm" || ev.event === "block") {
        s.editor.addCheck(ev.findings, ev.states);
        seen.push(...ev.findings);
        c.view.counts = countFindings(seen);
      }
      if (ev.event === "rules") Object.assign(c.view, { pool: ev.pool, of: ev.waiting });
      else if (ev.event === "confirm") Object.assign(c.view, { pool: ev.pool, held: ev.calls });
      else if (ev.event === "block") Object.assign(c.view, { done: ev.done, of: ev.of });
      else if (ev.event === "done") {
        // The report's counts are the cache file's (point 14, step 3).
        Object.assign(c.view, { counts: ev.counts, pool: ev.pool, notChecked: ev.states.filter((x) => x.state === "not checked").length });
      } else if (ev.event === "error") notify(ev.say);
      drawCheckStrip(s);
    });
  } catch (e) {
    if (e.name === "AbortError" && s.path === path && c.view) {
      // The blocks it never reached; the one in flight lands in the cache.
      Object.assign(c.view, { cancelled: true, notChecked: c.view.notChecked + Math.max(0, c.view.of - c.view.done) });
    } else if (live()) notify("couldn't check this note");
  } finally {
    if (c.run === ctl) c.run = null;
    if (s.path === path) drawCheckStrip(s);
  }
}

// What a selection asked, for the strip's second line: a supported block carries
// no mark, and a person who asked about one sentence is owed an answer.
function vaultVerdict(findings, states) {
  const f = findings.find((x) => x.grade !== "nit") || findings[0];
  if (f) return f.kind === "not found" ? "not found in the vault" : f.kind;
  const ok = states.find((x) => x.state === "supported");
  if (ok) return ok.at ? `supported · ${ok.at}` : "supported";
  const no = states.find((x) => x.state === "not checked");
  return no ? `not checked · ${no.say}` : "nothing here to check";
}

// A selection, against the vault or against the web (ADR-0110, amendment of
// 2026-11-06). The range's marks replace its own and no others: a check of one
// sentence must not wipe what a check of the note found elsewhere.
async function checkSelection(s, web) {
  const ed = s.editor, c = s.ck;
  if (!ed || ed.view.state.selection.main.empty) return;
  if (c.run) c.run.abort();
  hideSelBar();
  if (!(await flushForCheck(s))) { notify("this note changed on disk; reopen it before checking"); return; }
  const v = ed.view, sel = v.state.selection.main, doc = v.state.doc;
  const claim = v.state.sliceDoc(sel.from, sel.to).trim();
  const first = doc.lineAt(sel.from).number, last = doc.lineAt(sel.to).number;
  const path = s.path, ctl = new AbortController();
  c.run = ctl;
  c.path = path;
  if (ed.hasCheck()) ed.rebaseCheck(); else ed.startCheck();
  ed.dropCheck(first, last);
  ed.showCheck(checkShown());
  checkedPaths.add(path);
  c.view = { counts: countFindings(ed.checkFindings()), sub: "reading the vault" };
  drawCheckStrip(s);
  const live = () => c.run === ctl && s.path === path;
  const vault = { findings: [], states: [] };
  let line = 0, blocks = -1, webSaid = "";
  try {
    const r = await fetch("/note/check", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: ctl.signal,
      body: JSON.stringify({ path, lines: [first, last], confirm: true, ...(web ? { claim, web: true } : {}) }),
    });
    if (r.status === 400) {
      notify((await r.json().catch(() => ({}))).detail || "couldn't check that");
      c.view = null;
      drawCheckStrip(s);
      return;
    }
    if (!r.ok) throw new Error(String(r.status));
    await readLines(r, (ev) => {
      if (!live()) return;
      if (ev.event === "rules" || ev.event === "block") {
        vault.findings.push(...ev.findings);
        vault.states.push(...ev.states);
        // the box prints the vault's verdict first, so a supported block says who said so
        ed.addCheck(ev.findings, ev.states.flatMap((x) => (x.state === "supported" && x.at
          ? [x, { ...x, state: "vault", say: `supported by ${x.at}` }] : [x])));
        line ||= (ev.findings[0] || {}).line || 0;
      } else if (ev.event === "web") {
        c.view.sub = ev.step === "reading" ? `reading ${ev.pages} ${ev.pages === 1 ? "page" : "pages"}` : "asking the web";
      } else if (ev.event === "web_verdict") {
        ed.addCheck(ev.findings, ev.states);
        const said = ev.findings[0] || ev.states[0];
        webSaid = said ? said.say : "";
        line = (ev.findings[0] || ev.states.find((x) => x.host) || {}).line || line;
      } else if (ev.event === "done") {
        blocks = ev.blocks ?? -1;
        vault.states.push(...(ev.states || []).filter((x) => !vault.states.some((y) => y.line === x.line && y.state === x.state)));
      } else if (ev.event === "error") notify(ev.say);
      c.view.counts = countFindings(ed.checkFindings());
      drawCheckStrip(s);
    });
    if (!live()) return;
    if (blocks === 0) notify("Nothing here to check", "info");
    c.view.sub = webSaid || vaultVerdict(vault.findings, vault.states);
    if (line) ed.openCheck(line);
  } catch (e) {
    if (e.name === "AbortError" && s.path === path && c.view) c.view.sub = "cancelled";
    else if (live()) notify("couldn't check that");
  } finally {
    if (c.run === ctl) c.run = null;
    if (s.path === path) drawCheckStrip(s);
  }
}

// A page the web check read, staged in the Inbox with its citation: /nucleate
// stays the only way on (ADR-0015).
async function keepWebSource(s, x) {
  let r = null, d = {};
  try {
    r = await postJson("/note/check/keep", { path: s.path, url: x.url, quote: x.passage || "", claim: x.mark || "" });
    d = await r.json().catch(() => ({}));
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify(d.detail || "couldn't keep that page"); return; }
  notify(`Kept in Inbox, cited to ${x.host}`, "info", { label: "open", run: () => openNote(d.path) });
  loadVaultInfo();
}

// The rules again on a block typed into, once the caret has left it (Open 4),
// and the marks of a note checked earlier in this page when it is reopened:
// served from the cache, so no model call. The model's verdicts on an edited
// block stay hollow until the next check.
async function recheck(reopened = false, s = drawerNote) {
  const c = s.ck;
  if (!s.editor || recheckBusy || c.run || !s.path) return;
  if (!reopened && (c.path !== s.path || !s.editor.checkEdited())) return;
  recheckBusy = true;
  const path = s.path;
  try {
    if (!(await flushForCheck(s)) || s.path !== path) return;
    if (reopened) { s.editor.startCheck(); s.editor.showCheck(checkShown()); }
    const carried = s.editor.rebaseCheck();
    const r = await postJson("/note/check", { path, rules: true });
    if (!r.ok) return;
    let report = null;
    await readLines(r, (ev) => { if (ev.event === "done") report = ev; });
    if (!report || s.path !== path || c.run) return;
    if (report.unchecked) { checkedPaths.delete(path); s.editor.endCheck(); return; }
    s.editor.replaceCheck(report, carried);
    c.path = path;
    c.view = { counts: report.counts, pool: report.pool, done: 0, of: 0,
               notChecked: report.states.filter((x) => x.state === "not checked").length };
    drawCheckStrip(s);
  } catch {
    /* a rerun that failed leaves the marks hollow, which is what they are */
  } finally {
    recheckBusy = false;
  }
}

function endCheck(s = drawerNote) {
  const c = s.ck;
  if (c.run) c.run.abort();
  c.run = null;
  if (c.path) checkedPaths.delete(c.path);
  c.path = "";
  c.view = null;
  if (s.editor) s.editor.endCheck();
  drawCheckStrip(s);
}

// The strip: the counts by grade, each a toggle for its marks, and under them
// the run's progress or what was searched. At most two lines (point 14).
function drawCheckStrip(s = drawerNote) {
  const c = s.ck, strip = s.strip;
  const on = !!c.view && c.path === s.path && (s !== drawerNote || (drawerMode === "note" && !detailsOn && !rawShowing()));
  strip.hidden = !on;
  if (!on) return;
  const v = c.view, shown = checkShown();
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
      s.editor.showCheck(next);
      drawCheckStrip(s);
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
  close.addEventListener("click", () => endCheck(s));
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
  if (v.held && !c.run) {
    // Cancel keeps the rule marks: only the model's pass waited for a go.
    sub.append(`${v.held} blocks for the model · `, act("go", () => checkSources(true, s)), " · ",
      act("cancel", () => { v.held = 0; drawCheckStrip(s); }));
  } else if (c.run) {
    sub.append(v.sub || (v.of ? `judging ${v.done} of ${v.of}` : "reading the vault"), " · ", act("cancel", () => c.run && c.run.abort()));
  } else if (v.sub) {
    sub.textContent = v.sub;
    sub.title = v.sub;
  } else if (v.pool) {
    const p = v.pool;
    const where = p.scope === "whole" ? "searched the whole vault"
      : p.scope === "party" ? `${p.size} of ${p.of} notes · ${p.values.join(", ")}`
      : `${p.size} of ${p.of} notes · no party value here, so other parties' notes are out`;
    sub.textContent = where + (v.cancelled ? " · cancelled" : "") + (v.notChecked ? ` · ${v.notChecked} not checked` : "");
    sub.title = sub.textContent;
  }
  strip.replaceChildren(row, sub);
}

$("#note-check-open").addEventListener("click", () => checkSources());

// --- a selected passage ------------------------------------------------------
// The whole-note asks cannot aim at a sentence; a selection can (ADR-0059).
// The bar reads CodeMirror's selection, not the DOM's, since the text is the
// editor, and shows once the pointer is released, so it never jumps about
// under a drag. It does not close on blur, or a press on its own buttons
// (and a Tab into them) would take it away first; an empty selection, a
// press anywhere else, Escape and leaving the note close it.
let selDragging = false;

function hideSelBar() {
  let was = false;
  for (const s of noteSessions) if (s.bar && !s.bar.hidden) { s.bar.hidden = true; was = true; }
  return was;
}

function syncSelBar(view, s = drawerNote) {
  if (treeMenu && treeMenu.dataset.sel) return; // an open list owns the selection until it closes
  const sel = view.state.selection.main;
  if (sel.empty || (s === drawerNote && (drawerMode !== "note" || !drawerNote.path))) { if (s.bar) s.bar.hidden = true; return; }
  if (selDragging || !view.hasFocus) return; // shown on release, kept on blur
  placeSelBar(view, s);
}

// Over the selection's first line, inside the surface; under its last when the
// top of the text has no room above it. Off screen, it hides.
function placeSelBar(view, s = drawerNote) {
  const sel = view.state.selection.main, bar = s.bar;
  const a = view.coordsAtPos(sel.from), b = view.coordsAtPos(sel.to);
  const box = view.scrollDOM.getBoundingClientRect();
  if (!a || !b || b.bottom < box.top || a.top > box.bottom) { bar.hidden = true; return; }
  if (s === drawerNote) syncQuoteButton();
  bar.hidden = false;
  const p = s.host.getBoundingClientRect(), w = bar.offsetWidth, h = bar.offsetHeight;
  const above = a.top - h - 6;
  const top = above >= box.top ? above : b.bottom + 6;
  const left = Math.min(Math.max(a.left - p.left, 8), p.width - w - 8);
  bar.style.top = Math.round(top - p.top) + "px";
  bar.style.left = Math.round(left) + "px";
}

// One press, one drag, whichever editor it starts in. The second button is the
// right click's, which raises no bar: its list opens instead.
function watchSelection(s, field) {
  field.addEventListener("pointerdown", (e) => {
    if (e.button === 0) selDragging = true;
    hideSelBar();
  });
  field.addEventListener("scroll", () => {
    if (s.bar && !s.bar.hidden && s.editor) placeSelBar(s.editor.view, s);
  }, true);
  field.addEventListener("contextmenu", (e) => {
    const v = s.editor && s.editor.view, sel = v && v.state.selection.main;
    if (!sel || sel.empty) return; // nothing selected: the browser's own menu
    const pos = v.posAtCoords({ x: e.clientX, y: e.clientY });
    if (pos == null || pos < sel.from || pos > sel.to) return;
    e.preventDefault();
    selMenu(s, e.clientX, e.clientY);
  });
}
watchSelection(drawerNote, noteEdit);
document.addEventListener("pointerup", () => {
  if (!selDragging) return;
  selDragging = false;
  for (const s of noteSessions) if (s.editor && s.editor.view.hasFocus) syncSelBar(s.editor.view, s);
});
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#note-selbar, #nv-selbar, #note-edit, #nv-edit, #tree-menu")) hideSelBar();
});

const passage = (s = drawerNote) => {
  const v = s.editor && s.editor.view, sel = v && v.state.selection.main;
  return sel && !sel.empty ? v.state.sliceDoc(sel.from, sel.to).trim() : "";
};
const quoted = (text) => text.split("\n").map((l) => "> " + l).join("\n");
// The peek's header names the ask and the start of the passage, not the
// whole turn: a quoted paragraph does not fit on one line.
const passageTitle = (verb, text) => verb + ": “" + (text.length > 48 ? text.slice(0, 47) + "…" : text) + "”";

// Not /explain, which takes a concept (its skill, ADR-0063): a passage is explained
// in the note it sits in, so the turn names the note.
function explainPassage(s) {
  const text = passage(s);
  if (!text) return;
  hideSelBar();
  drawerReader((p) => "Explain this passage from the note " + shellQuote(p)
    + ", in the context of that note:\n\n" + quoted(text), passageTitle("explain this", text), s.path);
}
// /quiz on the note with the passage as its focus, so the round is still a
// round on the note and records through silica_record_quiz.
function quizPassage(s) {
  const text = passage(s);
  if (!text) return;
  hideSelBar();
  drawerReader((p) => "/quiz " + shellQuote(p) + " --focus=" + shellQuote(text), passageTitle("quiz me on this", text), s.path);
}
// The passage, quoted, in the composer that is showing, for a question in
// the reader's own words: the chat's on Chat, the dock on Explore and Learn.
// Calendar and Metrics show none, so there it goes to Chat.
function askPassage(s) {
  const text = passage(s);
  if (!text || !s.path) return;
  hideSelBar();
  draftInComposer("From " + shellQuote(s.path) + ":\n" + quoted(text) + "\n\n");
}
const PASSAGE_VERBS = { explain: explainPassage, quiz: quizPassage, ask: askPassage };
$("#sel-explain").addEventListener("click", () => explainPassage(drawerNote));
$("#sel-quiz").addEventListener("click", () => quizPassage(drawerNote));
$("#sel-ask").addEventListener("click", () => askPassage(drawerNote));

// One statement per web check (ADR-0110, amendment of 2026-11-06, point 5): one
// verdict over several claims would hide which of them failed.
const WEB_CLAIM_MAX = 600;
// The server says whether the web entry is open: its gate failed on 2026-11-06, so
// by default it is not, and the bar's button is then the one check (web.py).
let checkWebOpen = false;
function setCheckWeb(on) {
  checkWebOpen = !!on;
  for (const b of document.querySelectorAll("#sel-check, #nv-selbar [data-check]")) {
    b.textContent = checkWebOpen ? "check…" : "check this";
    if (checkWebOpen) b.setAttribute("aria-haspopup", "menu"); else b.removeAttribute("aria-haspopup");
  }
}

// The right click's list, and the bar's `check…`, which shows only its checks.
// It wears the tree's menu (ADR-0123): same element, same keys, same placing.
function selMenu(s, x, y, checksOnly = false) {
  closeTreeMenu();
  if (!checksOnly) hideSelBar();
  const m = mkEl("div");
  m.id = "tree-menu";
  m.dataset.sel = "1";
  m.setAttribute("role", "menu");
  m.setAttribute("aria-label", "this passage");
  treeMenu = m;
  const item = (label, fn, off) => {
    const b = mkEl("button", "s-item", label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    if (off) { b.disabled = true; b.title = off; }
    b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); closeTreeMenu(); s.editor.focus(); fn(); });
    m.append(b);
  };
  const sep = () => { const d = mkEl("div", "vam-sep"); d.setAttribute("role", "separator"); m.append(d); };
  if (!checksOnly) {
    item("copy", () => { navigator.clipboard?.writeText(passage(s)); });
    sep();
    item("explain this", () => explainPassage(s));
    if (viewServed("learn")) item("quiz me on this", () => quizPassage(s));
    item("ask…", () => askPassage(s));
    if (!DEMO) sep();
  }
  if (!DEMO) {
    item("check against the vault", () => checkSelection(s, false));
    if (checkWebOpen) item("check against the web", () => checkSelection(s, true), passage(s).length > WEB_CLAIM_MAX ? "select one statement" : "");
  }
  document.body.append(m);
  const w = m.offsetWidth;
  // Stands in for the tree's row: placeTreeMenu re-reads it on a resize or a
  // scroll, and Escape gives the caret back to the text it came from.
  treeMenuFor = { isConnected: true, setAttribute() {}, focus: () => s.editor.focus(),
                  getBoundingClientRect: () => ({ right: x + w, bottom: y - 4 }) };
  placeTreeMenu(x + w, y);
  m.querySelector("button:not([disabled])").focus();
}
function checkFromBar(b, s) {
  if (!checkWebOpen) { checkSelection(s, false); return; } // one check: nothing to choose between
  const r = b.getBoundingClientRect();
  selMenu(s, r.left, r.bottom + 4, true);
}
$("#sel-check").addEventListener("mousedown", (e) => { e.preventDefault(); checkFromBar(e.currentTarget, drawerNote); });

// --- dock card (rendered answer for a dock- or drawer-launched turn) ---------
// Not a re-implementation of the chat flow: no tools, no thinking text. Title =
// the dispatched prompt; body = pulsing "thinking", then the answer as live
// markdown (mdLite), upgraded to the canonical OFM render on `done` — so
// wikilinks in the card open the note drawer and focus the graph. One exchange
// only; the next one replaces it. "open in chat" → the full transcript.
const peekEl = $("#peek");
let peek = null; // { body, caret, raw } while a turn is being mirrored
function openPeek(title) {
  if (activeTab === "note") $("#dock").hidden = false; // as for the composer: only while it answers
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
  if (activeTab === "note" && !$("#dock-input").value.trim()) $("#dock").hidden = true;
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
  // a reply like the chat's, so its diagrams draw as the chat's do: until
  // 2026-10-29 the card styled pre.mermaid and never ran it, showing the source
  drawDiagrams(peek.body);
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
      // The root is no place anyone clicks: it is what a view transition's
      // overlay hands a click to while the landing moves onto the first turn.
      e.target !== document.documentElement &&
      !e.target.closest("#note-panel") &&
      // A view row moved into the sidebar with ADR-0084, and a view switch
      // still puts the note away as the strip's tabs did: the corner toggle
      // brings it back on the new view.
      (!e.target.closest("#sidebar") || e.target.closest("#views .tab")) &&
      // The chat's composer joined the dock on 2026-10-25: its chip sends the
      // open note with the question, and a click into the field closed it.
      // Explore's column too (find, Key, scope, compare): it picks what the
      // graph shows, and closing the note unfolded the column under the
      // pointer that had just reached for it.
      // And the note at the centre: the drawer is the source beside it (ADR-0155
      // point 3), and a click in the text must not take the source away.
      !e.target.closest("#dock, #composer, #graph-side, #view-note")) closeNote();
});
$("#note-close").addEventListener("click", () => closeNote()); // not as the handler: the click would pass as `force`
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
  // The tag of a clicked link opens its note at the line it is written on.
  if (e.data.type === "silica-open-line") openNote(e.data.path, e.data.line);
  // Flow re-rooted in the frame: the next load of it starts where the reader is.
  if (e.data.type === "silica-flow-root") flowRoot = e.data.path;
});

