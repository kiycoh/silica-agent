// Workspaces (ADR-0112): a note that gathers notes and folders from anywhere in
// the vault without moving them. Here: the group in the Vault compartment, the
// ◇ menu on a tree row and beside the reader's pin, and the chip of a chat
// opened in one. Membership is resolved on the server by a walk; this file only
// draws what /workspaces says and sends the user's edits back.

let workspaces = [];       // /workspaces rows: {path, name, count, members, dead, ...}
let chatWorkspace = "";    // the workspace this chat is opened in; "" is the whole vault
let wsBooted = false;      // the server's `current` is adopted once, then on a reopened chat
const wsOpen = new Set();  // rows shown expanded, by path
const WS_REASON = { listed: "listed", folder: "folder", where: "where", derived: "derived" };
const WS_REASON_TIP = {
  listed: "listed in the workspace's notes",
  folder: "inside a listed folder",
  where: "carries a listed frontmatter value",
  derived: "nucleated from a member",
};

// `adopt` takes the server's scope for this chat. Only at boot and on a
// reopened chat: the server learns a closed or opened chip at the next turn,
// so adopting it on every refresh brought a closed chip back, or dropped an
// open one, whenever a workspace was edited in between.
async function loadWorkspaces({ adopt = false } = {}) {
  try {
    const d = await (await fetch("/workspaces")).json();
    if (d.error) return;
    workspaces = d.workspaces || [];
    if (adopt || !wsBooted) chatWorkspace = d.current || "";
    wsBooted = true;
    if (chatWorkspace && !workspaces.some((w) => w.path === chatWorkspace)) {
      // Renamed or deleted since the chat was opened in it: said, not silent,
      // and the server refuses a turn that still names it (404).
      chatWorkspace = "";
      notify("the workspace this chat was opened in is gone: the next message searches the whole vault");
    }
  } catch { return; } // the group keeps what it drew; the next vault refresh retries
  renderWorkspaces();
  renderWsChip();
  syncNoteWs();
  // A workspace note open in the reader was rewritten under it (a ◇ edit, a
  // note the chat wrote): the field takes the new bytes unless it holds
  // unsaved typing, and the next keystroke no longer meets a 409.
  if (typeof refreshOpenNote === "function" && workspaces.some((w) => w.path === editPath)) refreshOpenNote();
}

const wsByPath = (p) => workspaces.find((w) => w.path === p);
const wsSvg = (d, w = 12) =>
  `<svg aria-hidden="true" focusable="false" width="${w}" height="${w}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const WS_ICON = {
  chev: '<polyline points="9 6 15 12 9 18"></polyline>',
  ws: '<path d="M12 3l9 9-9 9-9-9z"></path>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"></path>',
  plan: '<path d="M2 5h6a4 4 0 0 1 4 4v11a3 3 0 0 0-3-3H2z"></path><path d="M22 5h-6a4 4 0 0 0-4 4v11a3 3 0 0 1 3-3h7z"></path>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line>',
  x: '<line x1="6" y1="6" x2="18" y2="18"></line><line x1="18" y1="6" x2="6" y2="18"></line>',
};

function renderWorkspaces() {
  const box = $("#workspaces");
  box.innerHTML = "";
  if (!workspaces.length) {
    // The one line that says what the group is for (Open 1): a workspace is not
    // a pane layout, as the same word means in Obsidian.
    box.appendChild(mkEl("p", "ws-empty", "Groups notes from any folder without moving them. Start one with +, with ◇ on a note, or by dragging a note here."));
  }
  for (const w of workspaces) {
    const open = wsOpen.has(w.path);
    const row = mkEl("div", "ws-row" + (w.path === chatWorkspace ? " current" : ""));
    row.dataset.path = w.path;
    row.dataset.name = w.name;
    const head = mkEl("div", "ws-head");
    const tg = mkEl("button", "ws-toggle");
    tg.type = "button";
    tg.setAttribute("aria-expanded", String(open));
    tg.innerHTML = wsSvg(WS_ICON.chev, 10) + wsSvg(WS_ICON.ws, 11);
    tg.querySelector("svg").classList.add("ws-chev");
    tg.querySelectorAll("svg")[1].classList.add("ws-glyph");
    tg.append(mkEl("span", "t", w.name), mkEl("span", "tree-n", String(w.count)));
    if (w.dead.notes.length || w.dead.folders.length) {
      const warn = mkEl("span", "ws-warn", "!");
      warn.title = "a line points at nothing";
      tg.appendChild(warn);
    }
    head.appendChild(tg);
    const acts = mkEl("div", "ws-acts");
    for (const [act, icon, tip] of [["chat", WS_ICON.chat, `new chat in ${w.name}`],
                                   ["plan", WS_ICON.plan, `plan from ${w.name} in Learn`],
                                   ["add", WS_ICON.plus, `add files to ${w.name}`]]) {
      const b = mkEl("button", "ws-act");
      b.type = "button";
      b.dataset.act = act;
      b.title = tip;
      b.setAttribute("aria-label", tip);
      b.innerHTML = wsSvg(icon, 13);
      acts.appendChild(b);
    }
    head.appendChild(acts);
    row.appendChild(head);
    if (open) row.appendChild(renderWsMembers(w));
    box.appendChild(row);
  }
  syncTreeWs();
  applySidebarFilter();
  placeWsMenu();
}

// The members under their own folder path, so a member reads as the same note
// the tree below shows (ADR-0112 point 6), each with why it is in.
function renderWsMembers(w) {
  const list = mkEl("div", "ws-members");
  const groups = new Map();
  for (const m of w.members) {
    const f = m.path.includes("/") ? m.path.slice(0, m.path.lastIndexOf("/")) : "";
    if (!groups.has(f)) groups.set(f, []);
    groups.get(f).push(m);
  }
  for (const [f, ms] of groups) {
    list.appendChild(mkEl("div", "ws-folder", (f || "vault root") + "/"));
    for (const m of ms) {
      const a = mkEl("a", "ws-note");
      a.href = "#chat?note=" + encodeURIComponent(m.path);
      a.dataset.id = m.path;
      a.appendChild(mkEl("span", "t", m.path.split("/").pop().replace(/\.md$/, "")));
      const bs = mkEl("span", "ws-badges");
      for (const r of m.reasons) {
        const b = mkEl("span", "ws-b " + r.kind, WS_REASON[r.kind] || r.kind);
        b.title = `${WS_REASON_TIP[r.kind] || r.kind}: ${r.via}`;
        bs.appendChild(b);
      }
      a.appendChild(bs);
      list.appendChild(a);
    }
  }
  for (const f of w.dead.folders) list.appendChild(mkEl("div", "ws-dead", `folder not found: ${f}`));
  for (const n of w.dead.notes) list.appendChild(mkEl("div", "ws-dead", `no note named ${n}`));
  if (!w.members.length && !w.dead.folders.length && !w.dead.notes.length) {
    list.appendChild(mkEl("div", "ws-hint", "empty: drag notes or folders here, or use ◇ in the tree"));
  }
  if (w.has_instructions) {
    const p = mkEl("div", "ws-instr", "a chat here follows the note's own words");
    p.title = "the workspace note's text is read as standing instructions";
    list.appendChild(p);
  }
  return list;
}

$("#workspaces").addEventListener("click", (e) => {
  const row = e.target.closest(".ws-row");
  if (!row) return;
  const w = wsByPath(row.dataset.path);
  if (!w) return;
  const act = e.target.closest(".ws-act");
  if (act) {
    if (act.dataset.act === "chat") openWorkspaceChat(w.path);
    else if (act.dataset.act === "plan") learnPlanWorkspace(w.name);
    else if (act.dataset.act === "add") {
      // The vault's own menu, with this workspace as destination. Stopped here:
      // the document's outside-click closer would shut the menu this opens.
      e.stopPropagation();
      vaultImportWorkspace = w.path;
      $("#vault-add").click();
    }
    return;
  }
  const note = e.target.closest(".ws-note");
  if (note) {
    if (!plainClick(e)) return;
    e.preventDefault();
    openNote(note.dataset.id);
    return;
  }
  if (e.target.closest(".ws-toggle")) {
    if (wsOpen.has(w.path)) wsOpen.delete(w.path); else wsOpen.add(w.path);
    renderWorkspaces();
    focusWsRow(w.path); // the re-render replaced the button that had focus
  }
});

function focusWsRow(path) {
  $(`#workspaces .ws-row[data-path="${CSS.escape(path)}"] .ws-toggle`)?.focus();
}

// The + on the bucket: a workspace with nothing in it yet, named in place. A
// row dropped on the group but on no workspace opens it too, holding that row.
let wsNewWith = null; // {key, value} of the dropped row, or null
function wsNewReset() {
  wsNewWith = null;
  $("#ws-new-name").placeholder = "Name the workspace";
}
$("#ws-new").addEventListener("click", () => {
  const f = $("#ws-new-form");
  f.hidden = !f.hidden;
  wsNewReset();
  if (!f.hidden) $("#ws-new-name").focus();
});
$("#ws-new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#ws-new-name").value.trim();
  if (!name) return;
  const w = wsNewWith;
  const made = await createWorkspace(name, w?.key === "notes" ? [w.value] : [], w?.key === "folders" ? [w.value] : []);
  if (made) {
    $("#ws-new-name").value = "";
    $("#ws-new-form").hidden = true;
    wsNewReset();
    focusWsRow(made.path);
  }
});
$("#ws-new-name").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.stopPropagation(); $("#ws-new-form").hidden = true; wsNewReset(); $("#ws-new").focus(); }
});

// --- drag a tree row onto a workspace -------------------------------------------
// The same line the ◇ menu writes, for the student who reaches for the mouse.
// Its own type, not text/plain: the window's handler stages dropped Files for
// the chat, and a dragged link carries none. Files from the desktop dropped on
// a row import into that workspace, as its + does.
const WS_DRAG = "application/x-silica-ws";
const wsGroup = $("#ws-group");
const wsShown = (it) => it.value.split("/").pop().replace(/\.md$/, "") + (it.key === "folders" ? "/" : "");

document.addEventListener("dragstart", (e) => {
  const note = e.target.closest?.("#tree .tree-note, #workspaces .ws-note");
  const folder = !note && e.target.closest?.("#tree summary")?.querySelector(".tree-ws")?.dataset.wsFolder;
  if (!note && !folder) return;
  e.dataTransfer.setData(WS_DRAG, JSON.stringify(note ? { key: "notes", value: note.dataset.id } : { key: "folders", value: folder }));
  e.dataTransfer.effectAllowed = "copy";
});

let wsOver = null;
function wsDropMark(el) {
  if (wsOver === el) return;
  wsOver?.classList.remove("ws-over");
  wsOver = el;
  el?.classList.add("ws-over");
  // the window's "nucleate into Inbox/" hint is not what a row does with files
  document.body.classList.toggle("ws-drop", !!el);
}
wsGroup.addEventListener("dragover", (e) => {
  const row = e.target.closest(".ws-row");
  const t = e.dataTransfer.types;
  if (!t.includes(WS_DRAG) && !(row && t.includes("Files"))) { wsDropMark(null); return; }
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  wsDropMark(row || wsGroup);
});
// relatedTarget is null where a browser does not report it: the mark goes, and
// the next dragover over the group draws it again.
wsGroup.addEventListener("dragleave", (e) => { if (!wsGroup.contains(e.relatedTarget)) wsDropMark(null); });
document.addEventListener("dragend", () => wsDropMark(null));

wsGroup.addEventListener("drop", async (e) => {
  wsDropMark(null);
  const row = e.target.closest(".ws-row");
  const w = row && wsByPath(row.dataset.path);
  let it = null;
  try { it = JSON.parse(e.dataTransfer.getData(WS_DRAG) || "null"); } catch { /* not ours: the window's handler decides */ }
  if (!it && w && e.dataTransfer.files.length) {
    e.preventDefault(); // the window's handler sees it taken and stages nothing
    vaultImportWorkspace = w.path;
    importToVault([...e.dataTransfer.files], false);
    return;
  }
  if (!it || !["notes", "folders"].includes(it.key) || typeof it.value !== "string") return;
  e.preventDefault();
  if (!w) {
    $("#ws-new-form").hidden = false;
    wsNewWith = it;
    $("#ws-new-name").placeholder = `New workspace with ${wsShown(it)}`;
    $("#ws-new-name").focus();
    return;
  }
  const already = it.key === "folders" ? w.folders.includes(it.value) : w.members.some((m) => m.path === it.value);
  if (already) { notify(`${wsShown(it)} is already in ${w.name}`, "info"); return; }
  if (!(await wsSetLine(w.path, it.key, it.value, true))) return;
  notify(`${wsShown(it)} added to ${w.name}`, "info");
  await loadWorkspaces();
});

async function createWorkspace(name, notes, folders) {
  try {
    const r = await fetch("/workspaces", { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify({ name, notes, folders }) });
    const d = await r.json();
    if (!r.ok) { notify(d.detail || "couldn't create that workspace"); return null; }
    wsOpen.add(d.path);
    await loadWorkspaces();
    return d;
  } catch { notify("couldn't create that workspace"); return null; }
}

// --- a chat opened in a workspace (ADR-0112 point 4) --------------------------

async function openWorkspaceChat(path) {
  await newChat();
  chatWorkspace = path;
  renderWorkspaces();
  renderWsChip();
  input.focus();
}

function renderWsChip() {
  const row = $("#ws-chip-row");
  const w = chatWorkspace && wsByPath(chatWorkspace);
  row.innerHTML = "";
  row.hidden = !w;
  if (!w) return;
  const chip = mkEl("span", "chip ws-chip");
  chip.innerHTML = wsSvg(WS_ICON.ws, 11);
  chip.appendChild(mkEl("span", "chip-name", `in ${w.name} · ${w.count} ${w.count === 1 ? "note" : "notes"}`));
  chip.title = "searches in this chat return only this workspace's notes; a note you name with @ is read anyway";
  const x = mkEl("button", "chip-x");
  x.type = "button";
  x.title = "search the whole vault from the next message";
  x.setAttribute("aria-label", "stop limiting this chat to " + w.name);
  x.innerHTML = wsSvg(WS_ICON.x, 11);
  x.addEventListener("click", () => {
    chatWorkspace = "";
    renderWsChip();
    renderWorkspaces();
    input.focus(); // the chip that had focus is gone
    notify("the next message searches the whole vault", "info");
  });
  chip.appendChild(x);
  row.appendChild(chip);
}

// --- the ◇ menu: which workspaces list this note or folder --------------------

const wsMenu = $("#ws-menu");
let wsMenuFor = null; // {path, folder, button}

async function openWsMenu(button, path, folder) {
  if (!path) return;
  if (wsMenuFor && wsMenuFor.button === button && !wsMenu.hidden) { closeWsMenu(true); return; }
  wsMenuFor = { path, folder, button };
  button.setAttribute("aria-expanded", "true");
  wsMenu.innerHTML = "";
  wsMenu.appendChild(mkEl("div", "ws-mh", folder ? `${path}/ in workspaces` : "this note in workspaces"));
  wsMenu.hidden = false;
  placeWsMenu();
  let rows = [];
  try {
    const q = new URLSearchParams({ path, folder: folder ? "true" : "false" });
    rows = (await (await fetch("/workspaces/membership?" + q)).json()).workspaces || [];
  } catch { notify("couldn't read the workspaces"); }
  if (!wsMenuFor || wsMenuFor.button !== button) return; // another row's menu opened meanwhile
  for (const r of rows) {
    const via = r.reasons.map((x) => `${WS_REASON_TIP[x.kind] || x.kind}: ${x.via}`).join("; ");
    const item = mkEl("button", "ws-item");
    item.type = "button";
    item.setAttribute("role", "menuitemcheckbox");
    item.setAttribute("aria-checked", String(r.listed || !!via));
    item.dataset.ws = r.path;
    item.innerHTML = wsSvg(r.listed || via ? '<polyline points="5 12 10 17 19 7"></polyline>' : "", 12);
    item.appendChild(mkEl("span", "t", r.name));
    if (via && !r.listed) {
      // A member for another reason: shown, not toggled, and the tip says why.
      item.disabled = true;
      item.title = via;
      item.appendChild(mkEl("span", "why", "member"));
    }
    wsMenu.appendChild(item);
  }
  const form = mkEl("form", "ws-menu-new");
  const inp = mkEl("input");
  inp.type = "text";
  inp.placeholder = "New workspace with it";
  inp.setAttribute("aria-label", "new workspace with this " + (folder ? "folder" : "note"));
  form.appendChild(inp);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = inp.value.trim();
    if (!name) return;
    const made = await createWorkspace(name, folder ? [] : [path], folder ? [path] : []);
    if (made) closeWsMenu(true);
  });
  wsMenu.appendChild(form);
  placeWsMenu();
  (wsMenu.querySelector(".ws-item:not([disabled])") || inp).focus();
}

function placeWsMenu() {
  if (!wsMenuFor || wsMenu.hidden) return;
  // A tree refresh replaces the row the menu was opened on: nothing to follow.
  if (!wsMenuFor.button.isConnected) { closeWsMenu(); return; }
  const r = wsMenuFor.button.getBoundingClientRect();
  const h = wsMenu.offsetHeight || 0;
  const below = r.bottom + 4 + h <= innerHeight - 8;
  wsMenu.style.top = `${Math.round(below ? r.bottom + 4 : Math.max(8, r.top - 4 - h))}px`;
  wsMenu.style.left = `${Math.round(Math.min(Math.max(8, r.left - 8), innerWidth - wsMenu.offsetWidth - 8))}px`;
}

function closeWsMenu(refocus = false) {
  if (wsMenu.hidden) return;
  wsMenu.hidden = true;
  const b = wsMenuFor?.button;
  b?.setAttribute("aria-expanded", "false");
  wsMenuFor = null;
  if (refocus) b?.focus();
}

// The ◇ menu's toggle and a drop on a row: one line added or removed.
async function wsSetLine(workspace, key, value, on) {
  try {
    const r = await fetch("/workspaces/line", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace, key, value, on }),
    });
    if (!r.ok) throw new Error();
    return true;
  } catch { notify("couldn't change that workspace"); return false; }
}

wsMenu.addEventListener("click", async (e) => {
  const item = e.target.closest(".ws-item");
  if (!item || item.disabled || !wsMenuFor) return;
  const on = item.getAttribute("aria-checked") !== "true";
  const { path, folder } = wsMenuFor;
  if (!(await wsSetLine(item.dataset.ws, folder ? "folders" : "notes", path, on))) return;
  item.setAttribute("aria-checked", String(on));
  item.querySelector("svg").innerHTML = on ? '<polyline points="5 12 10 17 19 7"></polyline>' : "";
  await loadWorkspaces();
});
wsMenu.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.stopPropagation(); closeWsMenu(true); return; }
  const items = [...wsMenu.querySelectorAll(".ws-item:not([disabled]), input")];
  const i = items.indexOf(document.activeElement);
  const go = { ArrowDown: i + 1, ArrowUp: i - 1 }[e.key];
  if (go !== undefined && document.activeElement.tagName !== "INPUT") {
    e.preventDefault();
    items[(go + items.length) % items.length]?.focus();
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#ws-menu, .tree-ws, #note-ws")) closeWsMenu();
});
addEventListener("resize", placeWsMenu);
// The drawer scrolls, and the list grows when a workspace opens: the menu
// follows its ◇ instead of staying where it was first drawn.
addEventListener("scroll", placeWsMenu, true);
$("#note-ws").addEventListener("click", () => openWsMenu($("#note-ws"), lastViewedPath, false));

// The ◇ on a tree row and beside the reader's pin lights when the note or the
// folder is in a workspace, as the pin lights when it is pinned.
function syncTreeWs() {
  const notes = new Set(), folders = new Set();
  for (const w of workspaces) {
    for (const m of w.members) notes.add(m.path);
    for (const f of w.folders) folders.add(f);
  }
  for (const b of $("#tree").querySelectorAll(".tree-ws")) {
    const on = b.dataset.wsNote ? notes.has(b.dataset.wsNote) : folders.has(b.dataset.wsFolder);
    b.classList.toggle("on", on);
  }
  syncNoteWs();
}

function syncNoteWs() {
  const b = $("#note-ws");
  const n = lastViewedPath ? workspaces.filter((w) => w.members.some((m) => m.path === lastViewedPath)).length : 0;
  b.classList.toggle("on", n > 0);
  b.dataset.tip = n ? `in ${n} ${n === 1 ? "workspace" : "workspaces"}` : "add this note to a workspace";
}
