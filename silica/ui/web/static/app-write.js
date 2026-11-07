// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Alessandro Carosia

// A note written by hand at the centre of the app (ADR-0155): the drawer's
// editor in a second session, with the doors that create a note, the title, the
// format bar, `[[`, find, images and the properties form. No model is on this
// path: opening, typing, saving, creating, renaming, linking, finding and pasting
// all work with no key. The saving itself is app-notes.js's, one code for both
// surfaces, and what a surface adds to its editor is noteExtensions below.

const centreNote = noteSession();
noteSessions.push(centreNote);
Object.assign(centreNote, { foot: $("#nv-foot"), strip: $("#nv-check"), bar: $("#nv-selbar"), host: $("#view-note") });

const noteFolder = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");

// --- `[[` completes, over the composer's own index (ADR-0155 point 13) --------
function linkSource(cx) {
  const m = cx.matchBefore(LINK_OPENING);
  if (!m) return null;
  const q = m.text.slice(2).trim().toLowerCase();
  const hits = (q ? noteIdx.map((n) => [scoreNote(n, q), n]).filter((p) => p[0] > 0)
    .sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name)).map((p) => p[1])
    : [...noteIdx].sort((a, b) => a.name.localeCompare(b.name))).slice(0, 8);
  const closed = cx.state.sliceDoc(cx.pos, cx.pos + 2) === "]]";
  return {
    from: m.from + 2, filter: false,
    options: hits.map((n) => ({
      label: n.name, detail: noteFolder(n.path),
      apply: (view, _c, from, to) => {
        const link = mentionLink(n.path);
        view.dispatch({ changes: { from, to: closed ? to + 2 : to, insert: link + "]]" }, selection: { anchor: from + link.length + 2 } });
      },
    })),
  };
}

// --- an unresolved link under the caret is a door (ADR-0155 point 9) ----------
const linkTarget = (t) => t.split("|")[0].split("#")[0].trim();
function ghostAt(state, s) {
  const sel = state.selection.main;
  if (!sel.empty) return null;
  const line = state.doc.lineAt(sel.head);
  for (const m of line.text.matchAll(/(?<!!)\[\[([^[\]\n]+?)\]\]/g)) {
    const from = line.from + m.index, to = from + m[0].length, name = linkTarget(m[1]);
    if (sel.head < from || sel.head > to || !name || !window.LiveEditor.isMissing(state, name)) continue;
    return {
      pos: from, above: false, strictSide: false, arrow: false,
      create() {
        const dom = mkEl("div", "nv-ghost");
        const b = mkEl("button");
        b.type = "button";
        b.append(document.createTextNode(`Write "${name}"`));
        b.append(mkEl("span", "", linkedFolder(name, s.path || "", s.folder) || "vault root"));
        b.addEventListener("mousedown", (e) => { e.preventDefault(); writeLinked(name, s); });
        dom.append(b);
        return { dom };
      },
    };
  }
  return null;
}
const ghostField = (s) => window.CM.StateField.define({
  create: (st) => ghostAt(st, s),
  update: (v, tr) => (tr.docChanged || tr.selection || tr.effects.some((e) => e.is(window.LiveEditor.brokenEffect)) ? ghostAt(tr.state, s) : v),
  provide: (f) => window.CM.showTooltip.compute([f], (st) => st.field(f)),
});

// The folder a linked note is born in: the one the link spells, else the naming
// note's, and the root when that is one of Silica's own.
function linkedFolder(name, from, fallback = "") {
  if (name.includes("/")) return noteFolder(name);
  const f = from ? noteFolder(from) : fallback;
  return ownFolder(f) ? "" : f;
}

async function createLinked(name, from, fallback) {
  const folder = linkedFolder(name, from, fallback), base = name.split("/").pop();
  let r = null, d = {};
  try {
    r = await postJson("/note/new", { folder, name: base, text: "" });
    d = await r.json().catch(() => ({}));
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify(d.detail || `couldn't write ${base}`); return null; }
  notify(`Wrote ${d.name} in ${folder || "the vault root"}`, "info", { label: "undo", run: () => undoWrite(d) });
  await loadVaultInfo();
  return d;
}

async function writeLinked(name, s) {
  const d = await createLinked(name, s.path, s.folder);
  if (!d) return;
  s.broken = s.broken.filter((t) => t !== name);
  s.editor.setMissing(s.broken);
  paintFoot(s);
  await openNote(d.path); // as a link does: the text at the centre stays where it was
  if (drawerNote.editor && drawerNote.path === d.path) drawerNote.editor.focus();
}

// The node pane, the note's Links and Examine's unwritten list: `from` is the
// note that names it, as those panes spell it.
window.writeGhost = async (name, from) => {
  const first = Array.isArray(from) ? from[0] : from;
  const hit = first ? noteIdx.find((n) => n.path === first || n.path === first + ".md" || n.name === first) : null;
  const d = await createLinked(name, hit ? hit.path : "", "");
  if (d) await openNote(d.path);
};

// --- find, in a bar of the house's (ADR-0155 point 14) -------------------------
// CodeMirror keeps only the lines near the viewport in the page, so the
// browser's own find cannot stand in: 25 of 800 matches on a 1,600-line note.
function findPanel(view) {
  const { SearchQuery, setSearchQuery, findNext, findPrevious, replaceNext, replaceAll, closeSearchPanel, getSearchQuery } = window.CM;
  const dom = mkEl("div", "nv-find");
  const row = mkEl("div", "row"), row2 = mkEl("div", "row");
  const find = mkEl("input"), repl = mkEl("input"), n = mkEl("span", "n");
  find.placeholder = "Find in this note";
  find.setAttribute("main-field", "true");
  find.setAttribute("aria-label", "find in this note");
  repl.placeholder = "Replace with";
  repl.setAttribute("aria-label", "replace with");
  const glyph = (d, label, fn) => {
    const b = mkEl("button", "nv-b");
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.innerHTML = `<svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
    b.addEventListener("click", fn);
    return b;
  };
  const word = (t, fn) => { const b = mkEl("button", "nv-b txt", t); b.type = "button"; b.addEventListener("click", fn); return b; };
  const count = () => {
    const q = getSearchQuery(view.state);
    if (!q.search) { n.textContent = ""; return; }
    let total = 0, at = 0;
    const cur = q.getCursor(view.state), head = view.state.selection.main.from;
    for (let r = cur.next(); !r.done && total < 999; r = cur.next()) { total++; if (r.value.from <= head) at = total; }
    n.textContent = total ? `${at || 1} of ${total >= 999 ? "999+" : total}` : "no match";
  };
  const commit = () => {
    view.dispatch({ effects: setSearchQuery.of(new SearchQuery({ search: find.value, replace: repl.value })) });
    count();
  };
  find.addEventListener("input", commit);
  repl.addEventListener("input", commit);
  // The caret in the find bar leaves the text unfocused, and a drawn block (a
  // table, the frontmatter under the form) unfolds only for a focused caret, so a
  // match inside one was counted and never seen. Moving to a match opens its block.
  const go = (fn) => {
    fn(view);
    view.dispatch({ effects: window.LiveEditor.focusEffect.of(true) });
    count();
  };
  find.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); go(e.shiftKey ? findPrevious : findNext); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSearchPanel(view); view.focus(); }
  });
  row2.hidden = true;
  const close = glyph('<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>', "close · Esc", () => { closeSearchPanel(view); view.focus(); });
  row.append(find, n,
    glyph('<polyline points="18 15 12 9 6 15"></polyline>', "previous · Shift+Enter", () => go(findPrevious)),
    glyph('<polyline points="6 9 12 15 18 9"></polyline>', "next · Enter", () => go(findNext)),
    word("Replace…", () => { row2.hidden = !row2.hidden; if (!row2.hidden) repl.focus(); }), mkEl("span", "sp"), close);
  row2.append(repl, word("Replace", () => { replaceNext(view); count(); }), word("All", () => { replaceAll(view); count(); }));
  dom.append(row, row2);
  return {
    dom, top: true,
    mount() { const q = getSearchQuery(view.state); find.value = q.search; find.select(); count(); },
    update(u) { if (u.docChanged || u.selectionSet) count(); },
  };
}
function findInNote(s) {
  if (s.editor) { s.editor.focus(); window.CM.openSearchPanel(s.editor.view); }
}

// --- an image pasted or dropped lands in the vault (ADR-0155 point 15) ---------
async function putImage(s, file) {
  if (s.fresh) await saveEdit(s); // the file lands beside its note, so the note comes first
  if (!s.path) { notify("write a line first: an image lands beside its note"); return; }
  const form = new FormData();
  form.append("note", s.path);
  form.append("file", file, file.name || "image");
  let r = null, d = {};
  try {
    r = await fetch("/note/asset", { method: "POST", body: form });
    d = await r.json().catch(() => ({}));
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify(d.detail || "couldn't add that image"); return; }
  const v = s.editor.view, at = v.state.selection.main.head, text = d.embed + "\n";
  v.dispatch({ changes: { from: at, insert: text }, selection: { anchor: at + text.length } });
  v.focus();
}
// In turn: each goes in at the caret the last one left.
async function putImages(s, files) {
  for (const f of files) await putImage(s, f);
}
const imageDrop = (s) => window.CM.EditorView.domEventHandlers({
  paste(e) {
    const files = [...(e.clipboardData?.files || [])].filter((x) => VOCAB.formats.note_images.includes(x.type));
    if (!files.length) return false;
    e.preventDefault();
    putImages(s, files);
    return true;
  },
  drop(e, view) {
    const files = [...(e.dataTransfer?.files || [])].filter((x) => VOCAB.formats.note_images.includes(x.type));
    if (!files.length) return false;
    e.preventDefault();
    const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
    if (pos != null) view.dispatch({ selection: { anchor: pos } });
    putImages(s, files);
    return true;
  },
});

// What both surfaces add to the editor. Built once per session: a fresh state per
// note asks for them again, and a new StateField each time would be a new field.
function noteExtensions(s) {
  if (s.extensions) return s.extensions;
  const { tooltips, autocompletion, search, placeholder, keymap, Prec, searchKeymap } = window.CM;
  s.extensions = [
    // on the body: the view and the drawer clip what hangs out of them
    tooltips({ parent: document.body }),
    autocompletion({ override: [linkSource], icons: false }),
    search({ top: true, createPanel: findPanel }),
    ghostField(s),
    imageDrop(s),
    ...(s === centreNote ? [placeholder("Write here. [[ links a note")] : []),
    Prec.high(keymap.of([
      ...searchKeymap.filter((k) => k.key === "Mod-f"),
      { key: "Mod-k", run: (v) => { fmtLink(v); return true; } },
    ])),
  ];
  return s.extensions;
}

// --- the title: the file's name, not the note's bytes (ADR-0155 point 6) --------
const nvTop = mkEl("div", "nv-top");
nvTop.innerHTML = '<textarea id="nv-title" rows="1" placeholder="Title" spellcheck="true" aria-label="title, which is the name of the file"></textarea>'
  + '<div id="nv-msg" hidden></div><div id="nv-strip"></div><div id="nv-props"></div>';
const nvTitle = nvTop.querySelector("#nv-title"), nvMsg = nvTop.querySelector("#nv-msg");
const nvStrip = nvTop.querySelector("#nv-strip"), nvProps = nvTop.querySelector("#nv-props");
const growTitle = () => { nvTitle.style.height = "auto"; nvTitle.style.height = nvTitle.scrollHeight + "px"; };
const titleSay = (text, bad = false) => { nvMsg.hidden = !text; nvMsg.textContent = text || ""; nvMsg.classList.toggle("bad", bad); };
const freshName = () => nvTitle.value.trim().replace(/\s+/g, " ");

nvTitle.addEventListener("input", () => {
  growTitle();
  titleSay("");
  centreNote.nameRefused = "";
  if (centreNote.phase === "refused") setPhase(centreNote, "saving");
  $("#nv-handle span").textContent = nvTitle.value.trim() || "New note";
});
nvTitle.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== "ArrowDown") return;
  e.preventDefault();
  // The caret moves now and the write runs behind it: at typing speed the next
  // keys are the text's, and waiting for the answer sent them into the file's name.
  centreNote.editor.focus();
  // A refusal brings the caret back only to a text not yet begun: mid-sentence, the
  // keys would go on into the title and name the note after them.
  commitTitle().then((ok) => { if (!ok && !centreNote.editor.getText().trim()) nvTitle.focus(); });
});
nvTitle.addEventListener("blur", () => { if (nvTitle.value.trim()) commitTitle(); });

// The name leaves the field only on Enter or a blur, so a half-typed title never
// renames a file. False keeps the caret here, with the reason under it.
async function commitTitle() {
  const s = centreNote, name = freshName();
  if (!name) return true;
  if (s.fresh) { await createFresh(s); return !s.fresh; }
  if (!s.path || name === noteStem(s.path)) return true;
  // Enter moves the caret, which blurs the title: one press, one rename.
  s.renaming ||= renameCentre(s, name).finally(() => { s.renaming = null; });
  return s.renaming;
}

async function renameCentre(s, name) {
  await saveEdit(s);
  let r = null, d = {};
  try {
    r = await postJson("/tree/move", { path: s.path, name });
    d = await r.json().catch(() => ({}));
  } catch { /* reported below */ }
  if (!r || !r.ok) { titleSay(d.detail || "couldn't rename this note", true); return false; }
  followMoves(d.moved, d.rewritten);
  paintCentre();
  loadVaultInfo();
  syncUrl(false);
  notify(`Renamed to ${name}. The links that named it follow`, "info",
         d.moved.length ? { label: "undo", run: () => undoTreeMove(d.run, d.moved) } : null);
  return true;
}

// The first save of a note with something in it (ADR-0155 point 7): text with no
// title is named by the server. One run, and the toast's undo sends it to the trash.
// `leaving`: the field is about to be let go, so a name the vault refused gives
// way to Untitled rather than take the text with it. Otherwise a refused name is
// asked once, not once per keystroke, until the person changes it.
function createFresh(s = centreNote, leaving = false) {
  if (s.creating) return s.creating;
  const text = s.editor.getText(), name = freshName();
  if (!text.trim() && !name) { setPhase(s, ""); return Promise.resolve(); }
  const refused = !!name && name === s.nameRefused;
  if (refused && !leaving) { setPhase(s, "refused"); return Promise.resolve(); }
  if (refused && !text.trim()) return Promise.resolve();
  const asked = refused ? "" : name;
  s.creating = (async () => {
    let r = null, d = {};
    try {
      r = await postJson("/note/new", { folder: s.folder, name: asked, text });
      d = await r.json().catch(() => ({}));
    } catch { /* retried below */ }
    if (!r) {
      setPhase(s, "retry");
      clearTimeout(s.retry);
      s.retry = setTimeout(() => saveEdit(s), 5000);
      return;
    }
    if (!r.ok) {
      titleSay(d.detail || "couldn't write this note", true);
      s.nameRefused = name;
      setPhase(s, "refused");
      return;
    }
    s.fresh = false;
    s.nameRefused = "";
    holdNote(s, d);
    s.editor.setMissing(s.broken);
    s.touched = true;
    s.at = new Date();
    if (document.activeElement !== nvTitle) nvTitle.value = d.name; // not over keys still going in
    growTitle();
    titleSay(d.same_name ? `${noteFolder(d.same_name) || "The vault root"} also holds a note called ${d.name}. A bare link reaches the nearer one.` : "");
    paintCentre();
    setPhase(s, s.editor.getText() === d.text ? "saved" : "saving");
    if (s.editor.getText() !== d.text) scheduleSave(s);
    notify(`Wrote ${d.name} in ${s.folder || "the vault root"}` + (refused ? `: ${name} was taken` : ""), "info",
           { label: "undo", run: () => undoWrite(d) });
    loadVaultInfo();
    syncUrl(false);
  })().finally(() => { s.creating = null; });
  return s.creating;
}

// Free the field for another note: true when nothing typed stays behind. A new
// note that cannot be written yet keeps the field, as a held note does.
async function settleField(s, leaving = false) {
  // Back before the answer came: the person is in the field again and it stays theirs.
  const back = leaving ? () => activeTab === "note" : null;
  if (s.fresh) {
    await createFresh(s, true);
    if (back && back()) return false;
    if (s.fresh && s.editor.getText().trim()) {
      notify("Not saved yet: your text stays in the note until it has a place", "error",
             activeTab === "note" ? null : { label: "back to it", run: () => { showTab("note"); syncUrl(true); } });
      return false;
    }
    s.fresh = false;
  }
  return releaseNote(s, back);
}

async function undoWrite(d) {
  let r = null, data = {};
  try {
    r = await postJson("/tree/undo", { run: d.run });
    data = await r.json().catch(() => ({}));
  } catch { /* reported below */ }
  if (!r || !r.ok) { notify(data.detail || "couldn't take that back"); return; }
  // Renamed since it was born, the note is not where the run left it, and stays.
  if (!(data.restored || []).length) { notify("This note has moved since: delete it from the tree", "info"); return; }
  notify("In the trash, with what it held", "info");
  if (centreNote.path === d.path) {
    centreNote.path = ""; // its bytes are in the trash: nothing is left to save
    clearTimeout(centreNote.timer);
    writeNote({ folder: noteFolder(d.path) });
  }
  if (drawerNote.path === d.path) { drawerNote.path = ""; clearTimeout(drawerNote.timer); closeNote(); }
  loadVaultInfo();
}

// --- the surface ---------------------------------------------------------------
function paintCentre() {
  const s = centreNote, folder = s.path ? noteFolder(s.path) : s.folder;
  $("#nv-crumb").textContent = folder || "Vault";
  $("#nv-handle span").textContent = s.path ? noteStem(s.path) : (nvTitle.value.trim() || "New note");
  markOpenNote();
}

function centreEditor() {
  return sessionEditor(centreNote, $("#nv-edit"), {
    blur: () => { saveEdit(centreNote); recheck(false, centreNote); },
    select: (view) => { syncSelBar(view, centreNote); syncFormat(view); recheck(false, centreNote); },
    openAt: (path, line) => openNote(path, line || undefined),
  });
}
// A save that landed after the person left the view: nothing holds the note now.
centreNote.onSaved = () => { if (activeTab !== "note") setTimeout(() => releaseNote(centreNote)); };
centreNote.onChange = () => {
  renderProps();
  syncFormat(centreNote.editor.view);
};
centreNote.onRelease = () => {
  if (centreNote.ck.run) centreNote.ck.run.abort();
  hideSelBar();
  closeCompare(centreNote);
};
// A move from the tree: the name, the crumb and the address follow the note.
centreNote.onMoved = () => {
  nvTitle.value = noteStem(centreNote.path);
  growTitle();
  paintCentre();
  syncUrl(false);
};
// A press on a note the centre holds lands here instead of loading twice.
centreNote.reveal = (line) => {
  if (activeTab !== "note") showTab("note");
  if (line) centreNote.editor.revealLine(line);
  centreNote.editor.focus();
};

let centreSeq = 0;
async function openCentre(path, pos, { fromUrl = false } = {}) {
  const seq = ++centreSeq, s = centreNote;
  // Held since the view was left, with text only the field has: it is what comes back.
  if (s.path === path) {
    if (activeTab !== "note") { showTab("note"); if (!fromUrl) syncUrl(true); }
    s.editor.focus();
    return;
  }
  const fromDrawer = drawerNote.path === path;
  if (fromDrawer && !(await releaseNote(drawerNote))) return;
  if (!(await settleField(s))) {
    if (activeTab !== "note") { showTab("note"); syncUrl(!fromUrl); }
    return;
  }
  let data, ed;
  try {
    const [r, loaded] = await Promise.all([fetch("/note/raw?path=" + encodeURIComponent(path)), centreEditor()]);
    if (r.status === 404) { if (seq === centreSeq) { notify(`no note named "${path}" in this vault`); if (activeTab === "note") showTab("chat"); } return; }
    if (!r.ok) throw new Error(String(r.status));
    data = await r.json();
    ed = loaded;
  } catch { if (seq === centreSeq) notify("couldn't open that note"); return; }
  if (seq !== centreSeq) return;
  if (!ed) { notify("couldn't load the editor"); return; }
  // One note, one session (ADR-0155 point 1): it leaves the drawer first.
  // One note, one session (ADR-0155 point 1): the drawer shuts on the note it let
  // go, or stays open on a field no save reaches.
  if (drawerNote.path === data.path && !(await releaseNote(drawerNote))) return;
  if (fromDrawer || lastNotePath === data.path) closeNote();
  holdNote(s, data);
  s.folder = noteFolder(data.path);
  nvStrip.replaceChildren();
  const strip = noteTopEl(data);
  if (strip) nvStrip.append(strip);
  ed.load(data.text, { top: nvTop, missing: data.broken || [] });
  nvTitle.value = noteStem(data.path);
  titleSay("");
  formOn = true;
  renderProps();
  if (activeTab !== "note") { showTab("note"); if (!fromUrl) syncUrl(true); }
  else syncUrl(!fromUrl);
  paintCentre();
  paintFoot(s);
  growTitle();
  if (checkedPaths.has(data.path)) recheck(true, s);
  else drawCheckStrip(s);
  if (pos != null) {
    ed.view.dispatch({ selection: { anchor: Math.min(pos, data.text.length) }, scrollIntoView: true });
    ed.focus();
  }
  syncFormat(ed.view);
}

// Pressing another view saves the note and lets it go (ADR-0155 point 2). A new
// note with nothing in it leaves no trace: there was never a file.
function leaveCentre() {
  $("#dock").hidden = true;
  closeNvMenus();
  settleField(centreNote, true);
}

// The note and the drawer trade places, the caret kept.
function moveToDrawer() {
  const s = centreNote, path = s.path;
  if (!path) return;
  const head = s.editor.view.state.selection.main.head, line = s.editor.view.state.doc.lineAt(head).number;
  releaseNote(s).then((free) => {
    if (!free) return;
    s.fresh = false;
    if (activeTab === "note") showTab("chat");
    openNote(path, line);
  });
}
$("#note-centre").addEventListener("click", () => {
  const path = drawerNote.path;
  if (path) openCentre(path, drawerNote.editor.view.state.selection.main.head);
});
$("#nv-todrawer").addEventListener("click", moveToDrawer);
$("#nv-close").addEventListener("click", () => { showTab("chat"); });

// The drawer's details are the home of Links, Place and History. For the note at
// the centre they open there with no text under them: `read` closes them.
async function centreDetails(tab) {
  const path = centreNote.path;
  if (!path || !(await releaseNote(drawerNote))) return;
  lastNotePath = path;
  lastViewedPath = path;
  drawerMode = "note";
  detailsOn = true;
  detailsLent = true;
  showDrawer(noteStem(path));
  syncDrawerMode();
  await loadContext(path);
  if (tab === "history" && window.showNoteHistory) window.showNoteHistory();
}
$("#nv-info").addEventListener("click", () => centreDetails());

// The folder in view (ADR-0155 point 5): Silica's own folders count as none, so
// a note open in sources/ does not send a new note there.
function folderInView() {
  for (const s of [centreNote, drawerNote]) {
    const f = s.path ? noteFolder(s.path) : "";
    // Saved/ holds other people's copies: a person's own note does not start there.
    if (s.path && !ownFolder(f) && !/^Saved(\/|$)/.test(f)) return f;
  }
  return centreNote.fresh ? centreNote.folder : "";
}

async function writeNote({ folder } = {}) {
  const s = centreNote, f = folder ?? folderInView(), seq = ++centreSeq;
  closeNvMenus();
  closeTreeMenu();
  if (!(await settleField(s))) {
    if (activeTab !== "note") { showTab("note"); syncUrl(true); }
    return;
  }
  const ed = await centreEditor();
  if (seq !== centreSeq) return;
  if (!ed) { notify("couldn't load the editor"); return; }
  s.fresh = true;
  s.nameRefused = "";
  s.folder = f;
  s.clean = "";
  s.phase = ""; s.at = null; s.broken = [];
  nvStrip.replaceChildren();
  ed.load("", { top: nvTop });
  nvTitle.value = "";
  titleSay("");
  formOn = true;
  renderProps();
  if (activeTab !== "note") { showTab("note"); syncUrl(true); } else syncUrl(false);
  paintCentre();
  paintFoot(s);
  drawCheckStrip(s);
  growTitle();
  syncFormat(ed.view);
  setTimeout(() => nvTitle.focus(), 30);
}

// --- the format bar: each control is a transaction on the bytes (point 12) ------
const wrapMark = (v, mark) => { window.LiveEditor.toggleWrap(mark)(v); v.focus(); };
const LINE_LEAD = /^(\s*)(#{1,6}\s+|>\s?|[-*+]\s+\[[ xX]\]\s+|[-*+]\s+|\d+[.)]\s+)?/;
function setLead(v, make) {
  const st = v.state, changes = [];
  const a = st.doc.lineAt(st.selection.main.from).number, z = st.doc.lineAt(st.selection.main.to).number;
  let i = 0;
  for (let n = a; n <= z; n++) {
    const line = st.doc.line(n), m = LINE_LEAD.exec(line.text), had = m[2] || "";
    if (a !== z && !line.text.trim()) continue; // a blank line inside a selection stays blank
    changes.push({ from: line.from + m[1].length, to: line.from + m[1].length + had.length, insert: make(had, i++) });
  }
  // Mapped forward: a caret at the marker's place lands after it, so what is
  // typed next goes after the `- ` and not before it.
  const set = st.changes(changes);
  v.dispatch({ changes: set, selection: st.selection.map(set, 1) });
  v.focus();
}
const toggleLead = (test, text) => (had, i) => (test.test(had) ? "" : (typeof text === "function" ? text(i) : text));
function insertBlock(v, text, caret) {
  const st = v.state, line = st.doc.lineAt(st.selection.main.head);
  const blank = !line.text.trim(), at = blank ? line.from : line.to, pre = blank ? "" : "\n\n";
  v.dispatch({ changes: { from: at, to: blank ? line.to : at, insert: pre + text + "\n" }, selection: { anchor: at + pre.length + caret } });
  v.focus();
}
function fmtLink(v) {
  const st = v.state, r = st.selection.main;
  if (r.empty) {
    v.dispatch({ changes: { from: r.from, insert: "[[" }, selection: { anchor: r.from + 2 } });
    window.CM.startCompletion(v);
  } else {
    const t = st.sliceDoc(r.from, r.to);
    v.dispatch({ changes: { from: r.from, to: r.to, insert: `[[${t}]]` }, selection: { anchor: r.from + t.length + 4 } });
  }
  v.focus();
}
const FORMAT = {
  bold: (v) => wrapMark(v, "**"),
  italic: (v) => wrapMark(v, "*"),
  mark: (v) => wrapMark(v, "=="),
  bullets: (v) => setLead(v, toggleLead(/^[-*+]\s+(?!\[)/, "- ")),
  numbers: (v) => setLead(v, toggleLead(/^\d/, (i) => `${i + 1}. `)),
  tasks: (v) => setLead(v, toggleLead(/\[[ xX]\]/, "- [ ] ")),
  link: fmtLink,
  para: (v) => setLead(v, () => ""),
  h1: (v) => setLead(v, () => "# "),
  h2: (v) => setLead(v, () => "## "),
  h3: (v) => setLead(v, () => "### "),
  quote: (v) => setLead(v, toggleLead(/^>/, "> ")),
  code: (v) => insertBlock(v, "```\n\n```", 4),
  table: (v) => insertBlock(v, "| Column | Column |\n| --- | --- |\n|  |  |", 2),
  callout: (v) => insertBlock(v, "> [!note] Title\n> ", 18),
  math: (v) => insertBlock(v, "$$\n\n$$", 3),
  diagram: (v) => insertBlock(v, "```mermaid\nflowchart LR\n  A --> B\n```", 11),
  footnote: (v) => {
    const st = v.state, n = (st.doc.toString().match(/\[\^\d+\]:/g) || []).length + 1, at = st.selection.main.head;
    v.dispatch({ changes: [{ from: at, insert: `[^${n}]` }, { from: st.doc.length, insert: `\n\n[^${n}]: ` }], selection: { anchor: st.doc.length + `[^${n}]`.length * 2 + 4 } });
    v.focus();
  },
  // On the line under it, since a key on the rule would break it; `***` on a
  // note's first line, where `---` would open a frontmatter.
  divider: (v) => insertBlock(v, v.state.doc.lineAt(v.state.selection.main.head).number === 1 ? "***" : "---", 4),
  image: () => $("#nv-file").click(),
};
// What the caret is in, read from the syntax tree.
function syncFormat(v) {
  const st = v.state, head = st.selection.main.head, on = new Set();
  for (let n = window.CM.syntaxTree(st).resolveInner(head, -1); n; n = n.parent) on.add(n.name);
  const set = (k, val) => { const b = $(`#nv-format [data-fmt="${k}"]`); if (b) b.setAttribute("aria-pressed", String(!!val)); };
  set("bold", on.has("StrongEmphasis"));
  set("italic", on.has("Emphasis"));
  set("mark", on.has("Highlight"));
  set("bullets", on.has("BulletList") && !on.has("Task"));
  set("numbers", on.has("OrderedList"));
  const line = st.doc.lineAt(head).text;
  set("tasks", /^\s*[-*+]\s+\[[ xX]\]/.test(line));
  const h = /^(#{1,3})\s/.exec(line);
  $("#nv-kind").textContent = h ? `Heading ${h[1].length}` : on.has("Blockquote") ? "Quote" : on.has("FencedCode") ? "Code" : "Text";
}
function closeNvMenus() {
  document.querySelectorAll(".nv-pop").forEach((m) => { m.hidden = true; });
  $("#nv-menu").hidden = true;
  $("#nv-handle").setAttribute("aria-expanded", "false");
}
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#nv-format, #nv-menu, #nv-handle")) closeNvMenus();
});
$("#nv-format").addEventListener("mousedown", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  e.preventDefault(); // the caret stays in the text
  if (b.dataset.pop) {
    const m = $("#" + b.dataset.pop), was = m.hidden;
    closeNvMenus();
    m.hidden = !was;
    m.style.left = b.offsetLeft + "px";
    m.style.top = (b.offsetTop + b.offsetHeight + 4) + "px";
    return;
  }
  closeNvMenus();
  const v = centreNote.editor && centreNote.editor.view;
  if (v && FORMAT[b.dataset.fmt]) { FORMAT[b.dataset.fmt](v); syncFormat(v); }
});
$("#nv-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) putImage(centreNote, f);
  e.target.value = "";
});

// --- the properties form (ADR-0155 point 16) -------------------------------------
// String-level, as stamp_type is: a row writes its own bytes and every other byte
// of the frontmatter stays. What it cannot draw shows as source, with no field.
// A YAML scalar as it reads, its quoting and escapes undone.
function yamlText(raw) {
  const v = raw.trim();
  if (v.length > 1 && v[0] === '"' && v.endsWith('"')) {
    try { return JSON.parse(v); } catch { return v.slice(1, -1); }
  }
  if (v.length > 1 && v[0] === "'" && v.endsWith("'")) return v.slice(1, -1).replace(/''/g, "'");
  return v;
}
function parseFrontmatter(text) {
  const m = /^---[ \t]*\n((?:.*\n)*?)---[ \t]*(?:\n|$)/.exec(text);
  if (!m) return null;
  const rows = [], lines = m[1].split("\n");
  if (lines.at(-1) === "") lines.pop();
  let at = m[0].indexOf("\n") + 1;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], kv = /^([^\s:#][^:#]*):(?:[ \t]+(.*))?$/.exec(line);
    const from = at;
    at += line.length + 1;
    if (!kv) { if (line.trim()) rows.push({ key: "", from, to: from + line.length, kind: "raw", value: line }); continue; }
    const value = (kv[2] || "").trim();
    const row = { key: kv[1], from, to: from + line.length, kind: "scalar", value: yamlText(value), quoted: /^["']/.test(value) };
    const more = [];
    while (i + 1 < lines.length && /^\s/.test(lines[i + 1])) {
      more.push({ text: lines[++i], from: at, to: at + lines[i].length + 1 });
      at += lines[i].length + 1;
    }
    if (more.length) row.to = more.at(-1).to - 1;
    if (!value && more.length && more.every((l) => /^\s+-\s/.test(l.text))) {
      row.kind = "list";
      row.block = true;
      row.items = more.map((l) => yamlText(l.text.replace(/^\s+-\s+/, "")));
      row.spans = more;
    } else if (more.length || /^[|>{]/.test(value) || (!row.quoted && /\s#/.test(value))) {
      row.kind = "raw"; // a block scalar, a map, a comment: shown, and written by Edit as YAML
      row.value = [value, ...more.map((l) => l.text.trim())].filter(Boolean).join(" ");
    } else if (/^\[.*\]$/.test(value)) {
      row.kind = "list";
      row.items = [];
      row.spans = [];
      row.end = from + line.lastIndexOf("]");
      const inner = value.slice(1, -1), base = from + line.indexOf("[", kv[1].length) + 1;
      // A comma inside quotes is the item's own.
      for (const x of inner.matchAll(/\s*"(?:[^"\\]|\\.)*"\s*|\s*'(?:[^']|'')*'\s*|[^,]+/g)) {
        const raw = x[0].trim();
        if (!raw) continue;
        const start = base + x.index + x[0].indexOf(raw);
        row.items.push(yamlText(raw));
        row.spans.push({ from: start, to: start + raw.length });
      }
    }
    rows.push(row);
  }
  return { rows, end: m[0].length };
}
const FIXED_KEYS = new Set(["AI", "type"]);
// Unquoted ": " empties the whole frontmatter for a YAML reader, so anything
// that is not plainly a word, a number or a date goes in quotes.
const yamlScalar = (v, quoted) => (!quoted && /^[\p{L}\p{N}][\p{L}\p{N} ._/-]*$/u.test(v) && !/^(true|false|null|yes|no|on|off)$/i.test(v) ? v : JSON.stringify(v));
function setRow(key, write) {
  const v = centreNote.editor.view, fm = parseFrontmatter(v.state.doc.toString()), row = fm && fm.rows.find((r) => r.key === key);
  if (row) v.dispatch({ changes: { from: row.from, to: row.to, insert: write(row) } });
}
// A chip takes its own bytes and no others': the rest of the list stays as written.
function removeItem(key, i) {
  const v = centreNote.editor.view, fm = parseFrontmatter(v.state.doc.toString()), row = fm && fm.rows.find((r) => r.key === key);
  const s = row && row.spans, at = s && s[i];
  if (!at) return;
  const span = row.block || s.length === 1 ? at
    : i < s.length - 1 ? { from: at.from, to: s[i + 1].from } : { from: s[i - 1].to, to: at.to };
  v.dispatch({ changes: span });
}
function addItem(key, val) {
  const v = centreNote.editor.view, fm = parseFrontmatter(v.state.doc.toString()), row = fm && fm.rows.find((r) => r.key === key);
  if (!row) return;
  if (row.block) {
    const indent = /^\s*/.exec(row.spans.at(-1).text)[0];
    v.dispatch({ changes: { from: row.to, insert: `\n${indent}- ${JSON.stringify(val)}` } });
  } else {
    const item = /[,[\]{}:#"']/.test(val) ? JSON.stringify(val) : val;
    v.dispatch({ changes: { from: row.end, insert: (row.items.length ? ", " : "") + item } });
  }
}
let formOn = true;

function addPropertyRow() {
  const row = mkEl("div", "nv-prow"), k = mkEl("input"), val = mkEl("input");
  k.placeholder = "name";
  k.setAttribute("aria-label", "the property's name");
  val.placeholder = "value";
  val.setAttribute("aria-label", "its value");
  let done = false; // the row's removal blurs its field, which would commit it twice
  const commit = () => {
    const key = k.value.trim().replace(/[:#]/g, ""), v = centreNote.editor.view;
    if (!key || done) return;
    // Two rows under one key: PyYAML keeps the last and strict readers refuse it.
    if (parseFrontmatter(v.state.doc.toString())?.rows.some((r) => r.key === key)) {
      notify(`${key} is already a property of this note`, "info");
      k.focus();
      k.select();
      return;
    }
    done = true;
    const line = `${key}: ${val.value.trim() ? yamlScalar(val.value.trim()) : '""'}\n`;
    const cur = parseFrontmatter(v.state.doc.toString());
    if (cur) v.dispatch({ changes: { from: cur.end - fenceLength(v, cur), insert: line } });
    else v.dispatch({ changes: { from: 0, insert: `---\n${line}---\n` } });
    row.remove();
    renderProps();
  };
  for (const f of [k, val]) f.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done = true; row.remove(); }
    if (e.key === "Enter") { e.preventDefault(); if (f === k) val.focus(); else commit(); }
  });
  row.addEventListener("focusout", (e) => {
    if (done || row.contains(e.relatedTarget)) return;
    if (!k.value.trim()) { row.remove(); return; }
    commit();
    if (!done) row.remove(); // a key already here, left behind by a click elsewhere
  });
  row.append(k, val);
  nvProps.querySelector(".nv-pacts").before(row);
  k.focus();
}
// How many characters the closing fence takes at the end of the block.
const fenceLength = (v, cur) => {
  const closing = v.state.sliceDoc(0, cur.end).match(/---[ \t]*\n?$/);
  return closing ? closing[0].length : 0;
};

// `force`: a press inside the form changed it. Otherwise a focused field stays as
// it is, since a redraw would take the caret out of it.
function renderProps(force = false) {
  if (!force && document.activeElement && nvProps.contains(document.activeElement)) return;
  const s = centreNote, text = s.editor ? s.editor.getText() : "", fm = parseFrontmatter(text);
  $("#nv-edit").classList.toggle("form-on", formOn);
  nvProps.textContent = "";
  if (!formOn) {
    const back = mkEl("button", "nv-link", "Back to the form");
    back.type = "button";
    back.addEventListener("click", () => { formOn = true; renderProps(true); });
    nvProps.append(back);
    return;
  }
  for (const r of fm ? fm.rows : []) {
    const row = mkEl("div", "nv-prow");
    row.append(mkEl("div", "k", r.key));
    if (FIXED_KEYS.has(r.key) || r.kind === "raw") row.append(mkEl("div", r.kind === "raw" ? "fixed src" : "fixed", r.value));
    else if (r.kind === "list") {
      const box = mkEl("div", "nv-chips");
      r.items.forEach((it, i) => {
        const c = mkEl("span", "nv-chip", it), x = mkEl("button", "", "×");
        x.type = "button";
        x.setAttribute("aria-label", `remove ${it}`);
        x.addEventListener("click", () => { removeItem(r.key, i); renderProps(true); });
        c.append(x);
        box.append(c);
      });
      const add = mkEl("input");
      add.placeholder = "add";
      add.setAttribute("aria-label", `add to ${r.key}`);
      add.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || !add.value.trim()) return;
        e.preventDefault();
        const val = add.value.trim();
        add.blur();
        addItem(r.key, val);
        renderProps(true);
      });
      box.append(add);
      row.append(box);
    } else {
      const inp = mkEl("input");
      inp.value = r.value;
      inp.setAttribute("aria-label", r.key);
      inp.addEventListener("change", () => { setRow(r.key, (now) => `${r.key}: ${yamlScalar(inp.value, now.quoted)}`); });
      row.append(inp);
    }
    nvProps.append(row);
  }
  const acts = mkEl("div", "nv-pacts");
  const add = mkEl("button", "nv-link", "Add a property");
  add.type = "button";
  add.addEventListener("click", addPropertyRow);
  acts.append(add);
  if (fm) {
    const y = mkEl("button", "nv-link", "Edit as YAML");
    y.type = "button";
    y.addEventListener("click", () => {
      formOn = false;
      renderProps(true);
      s.editor.view.dispatch({ selection: { anchor: 4 } });
      s.editor.focus();
    });
    acts.append(y);
  }
  nvProps.append(acts);
}

// --- the bars and menus of the note view ------------------------------------------
$("#nv-selbar").addEventListener("mousedown", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  e.preventDefault(); // the selection stays
  if (b.dataset.check !== undefined) checkFromBar(b, centreNote);
  else if (PASSAGE_VERBS[b.dataset.verb]) PASSAGE_VERBS[b.dataset.verb](centreNote);
});
watchSelection(centreNote, $("#nv-edit"));

// ADR-0155 point 19: the drawer's bar quotes the source into the note at the
// centre, a blockquote at its caret ending in a link back. No model.
function syncQuoteButton() {
  const on = activeTab === "note" && !!(centreNote.path || centreNote.fresh) && !!centreNote.editor;
  const b = $("#sel-quote");
  b.hidden = !on;
  if (on) b.textContent = `quote in ${centreNote.path ? noteStem(centreNote.path) : "the note"}`;
}
$("#sel-quote").addEventListener("mousedown", (e) => {
  e.preventDefault();
  const text = passage(drawerNote);
  if (!text) return;
  const c = centreNote.editor.view, line = c.state.doc.lineAt(c.state.selection.main.head);
  // A blank line after it: the next words are not a lazy line of the quote.
  const block = `\n\n${quoted(text)}\n>\n> [[${mentionLink(drawerNote.path)}]]\n\n`;
  c.dispatch({ changes: { from: line.to, insert: block }, selection: { anchor: line.to + block.length }, scrollIntoView: true });
  hideSelBar();
});

$("#nv-handle").addEventListener("click", () => {
  const m = $("#nv-menu"), was = m.hidden;
  closeNvMenus();
  m.hidden = !was;
  $("#nv-handle").setAttribute("aria-expanded", String(was));
  if (was) m.querySelector("button").focus();
});
$("#nv-menu").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  closeNvMenus();
  const path = centreNote.path, act = b.dataset.act;
  if (act === "find") { findInNote(centreNote); return; }
  if (!path) { notify("write a line first: this note is not on disk yet", "info"); return; }
  if (act === "ask") { $("#dock").hidden = false; $("#dock-input").focus(); }
  else if (act === "check") checkSources(false, centreNote);
  else if (act === "test") learnTestNote(path);
  else if (act === "teach") learnExplain(path);
  else if (act === "map") { graphMode = "graph"; document.querySelector('.tab[data-tab="graph"]').click(); focusGraphNode(path); }
});
// The note's menu and the bar's two lists take the keys a menu takes.
document.addEventListener("keydown", (e) => {
  const open = document.activeElement?.closest("#nv-menu, .nv-pop");
  if (!open) return;
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeNvMenus(); $("#nv-handle").focus(); return; }
  const go = { ArrowDown: 1, ArrowUp: -1 }[e.key];
  if (!go) return;
  e.preventDefault();
  const items = [...open.querySelectorAll("button")];
  items[(items.indexOf(document.activeElement) + go + items.length) % items.length]?.focus();
});
// The composer lent to the view goes back when it is left empty.
$("#dock-input").addEventListener("blur", () => {
  if (activeTab === "note" && !$("#dock-input").value.trim() && $("#peek").hidden) setTimeout(() => {
    if (activeTab === "note" && document.activeElement !== $("#dock-input") && $("#peek").hidden) $("#dock").hidden = true;
  }, 150);
});

// The crumb moves the note, the tree's own second page; a note not yet on disk
// just changes the folder it will be born in.
$("#nv-crumb").addEventListener("click", () => {
  const s = centreNote, r = $("#nv-crumb").getBoundingClientRect();
  closeTreeMenu();
  const m = mkEl("div");
  m.id = "tree-menu";
  m.setAttribute("role", "menu");
  treeMenu = m;
  document.body.append(m);
  treeMenuFor = { isConnected: true, setAttribute() {}, focus: () => $("#nv-crumb").focus(),
                  getBoundingClientRect: () => ({ right: r.left + m.offsetWidth, bottom: r.bottom }) };
  if (s.fresh) {
    const item = (label, f, depth) => {
      const b = mkEl("button", "s-item", label);
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.style.paddingLeft = `${0.6 + 0.7 * depth}rem`;
      b.addEventListener("click", () => { closeTreeMenu(); s.folder = f; paintCentre(); });
      m.append(b);
    };
    item("vault root", "", 0);
    for (const more of $("#tree").querySelectorAll(".tree-more[data-folder]")) {
      const f = more.dataset.folder;
      if (!ownFolder(f)) item(f.split("/").pop(), f, f.split("/").length - 1);
    }
  } else if (s.path) treeMoveMenu([s.path]);
  placeTreeMenu();
  m.querySelector("button")?.focus();
});

// --- the doors (ADR-0155 point 4) ---------------------------------------------------
$("#write-note").addEventListener("click", () => writeNote());
$("#rail-write").addEventListener("click", () => writeNote());
$("#vam-write").addEventListener("click", () => { $("#vault-add-menu").hidden = true; writeNote(); });
// Alt+N, since Ctrl+N is the browser's.
document.addEventListener("keydown", (e) => {
  if (e.altKey && !e.ctrlKey && !e.metaKey && e.code === "KeyN") { e.preventDefault(); writeNote(); }
});
