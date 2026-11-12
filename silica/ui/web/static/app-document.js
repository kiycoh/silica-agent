// --- a long document written from the vault (ADR-0158 points 3 and 11) ---------
// The + menu's "long document…" opens a form in the conversation: the type, the
// notes it is written from (folders and workspaces), the parameters. "Plan the
// outline" starts a job that reads every chosen note and plans; the outline
// comes back for the person to keep or cut, with the chapters the notes do not
// cover, which a deep research can fill through the Inbox before the writing.
// "Write it" writes the sections in order into a new note, stamped as written
// from the vault. A prototype: point 7's gate has not run.
const dcTray = $("#documents");
const dcById = new Map();
const dcTold = new Set();
let dcTypes = {};

// The rail's folders, less the two the Read skips anyway (the leaves under
// sources/ and the Inbox's staging): offering them would promise a read that
// does not happen.
function dcFolders() {
  return [...new Set([...document.querySelectorAll("#tree [data-folder]")]
    .map((e) => e.dataset.folder.replace(/\/+$/, "")).filter(Boolean))]
    .filter((f) => !/^(sources|Inbox)(\/|$)/.test(f)).sort();
}

function dcOpen(prefill = {}) {
  const st = { type: "book", selection: { folders: [], workspaces: [] }, params: { length: "", audience: "",
    register: "narrative", language: "", focus: "" }, wsNames: {}, ...prefill };
  dcForm(rsCard(), st);
}

async function dcCount(st, line) {
  const { folders, workspaces } = st.selection;
  if (!folders.length && !workspaces.length) { line.textContent = "Pick the folders or workspaces it is written from."; return; }
  line.textContent = "counting…";
  try {
    const c = await apiPost("/document/count", { selection: st.selection });
    const pdf = c.pdfs ? `, ${c.pdfs} PDF${c.pdfs === 1 ? "" : "s"} (${c.pages} pages)` : "";
    const skipped = c.skipped ? ` ${c.skipped} file${c.skipped === 1 ? "" : "s"} with no readable text left out.` : "";
    line.textContent = `Every chosen note is read whole: ${c.notes} note${c.notes === 1 ? "" : "s"}${pdf}.${skipped}`;
  } catch (e) {
    line.textContent = e.status ? e.message : "the server could not be reached";
  }
}

function dcForm(card, st) {
  card.replaceChildren();
  rsHead(card, "Long document", "written only from the notes you choose");
  const ids = Object.keys(dcTypes);
  card.append(rsField("Type", rsSeg("type", ids.map((id) => [id, dcTypes[id].label]), st.type, (id) => {
    st.type = id;
    dcForm(card, st);
  })));
  const from = mkEl("div", "dc-from");
  const chips = mkEl("div", "dc-chips");
  const count = mkEl("p", "rs-help dc-count");
  const drawChips = () => {
    chips.replaceChildren();
    for (const f of st.selection.folders) chips.append(dcChip(f + "/", () => {
      st.selection.folders = st.selection.folders.filter((x) => x !== f); drawChips(); dcCount(st, count);
    }));
    for (const w of st.selection.workspaces) chips.append(dcChip("workspace: " + (st.wsNames[w] || noteStem(w)), () => {
      st.selection.workspaces = st.selection.workspaces.filter((x) => x !== w); drawChips(); dcCount(st, count);
    }));
    chips.append(dcPicker(st, () => { drawChips(); dcCount(st, count); }));
  };
  drawChips();
  from.append(chips, count);
  card.append(rsField("From these notes", from));
  dcCount(st, count);
  const grid = mkEl("div", "dc-grid");
  const input = (label, key, hint) => {
    const el = mkEl("input", "rs-input");
    el.value = st.params[key] || "";
    el.placeholder = hint;
    el.addEventListener("input", () => { st.params[key] = el.value; });
    return rsField(label, el);
  };
  const t = dcTypes[st.type] || {};
  // Words, as a number: the server takes 100 to twice the type's default.
  const len = input("Length, in words", "length", t.words ? String(t.words) : "");
  const lenField = len.querySelector("input");
  lenField.type = "number";
  lenField.min = "100";
  lenField.step = "100";
  if (t.words) lenField.max = String(t.words * 2);
  grid.append(len,
    input("Audience", "audience", "who reads it"),
    rsField("Register", rsSeg("register", [["plain", "plain"], ["technical", "technical"], ["narrative", "narrative"]],
      st.params.register, (id) => { st.params.register = id; dcForm(card, st); })),
    input("Language", "language", "the notes' own"));
  card.append(grid);
  const focus = mkEl("textarea", "rs-input");
  focus.rows = 3;
  focus.value = st.params.focus || "";
  focus.placeholder = "What it is about, and the angle it takes";
  focus.addEventListener("input", () => { st.params.focus = focus.value; });
  card.append(rsField("Focus", focus));
  const foot = mkEl("div", "rs-foot");
  foot.append(mkEl("span", "rs-aside", t.short ? "Read, planned and written in one pass, then offered for a check."
    : "Every note is read first, then an outline comes back for you to keep or cut before anything is written."));
  const acts = mkEl("span", "rs-acts");
  const cancel = mkEl("button", "rs-alt", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => card.closest(".rs-msg").remove());
  const go = mkEl("button", "rs-go", "Plan the outline");
  go.type = "button";
  go.addEventListener("click", async () => {
    if (!st.selection.folders.length && !st.selection.workspaces.length) { notify("pick the notes it is written from", "warn"); return; }
    go.disabled = true;
    try {
      const rec = await apiPost("/document/start", { type: st.type, selection: st.selection, params: st.params });
      card.classList.add("rs-sent");
      card.replaceChildren(mkEl("p", "rs-help", "Reading the notes. The outline comes back above the box once every "
        + "note is read; nothing is written before you keep it."));
      dcUpdate(rec);
    } catch (e) {
      notify(e.status ? e.message : "the server could not be reached");
      go.disabled = false;
    }
  });
  acts.append(cancel, go);
  foot.append(acts);
  card.append(foot);
}

function dcChip(text, onRemove) {
  const chip = mkEl("span", "dc-chip", text + " ");
  const x = mkEl("button", "rs-x", "✕");
  x.type = "button";
  x.setAttribute("aria-label", "remove " + text);
  x.addEventListener("click", onRemove);
  chip.append(x);
  return chip;
}

// One select over the vault's folders and workspaces; picking adds a chip.
function dcPicker(st, changed) {
  const sel = mkEl("select", "dc-pick");
  sel.setAttribute("aria-label", "add a folder or a workspace");
  sel.append(new Option("+ a folder or a workspace", ""));
  const fg = document.createElement("optgroup");
  fg.label = "Folders";
  for (const f of dcFolders()) if (!st.selection.folders.includes(f)) fg.append(new Option(f + "/", "f:" + f));
  sel.append(fg);
  const wg = document.createElement("optgroup");
  wg.label = "Workspaces";
  sel.append(wg);
  fetch("/workspaces").then((r) => r.json()).then((d) => {
    for (const w of d.workspaces || []) {
      st.wsNames[w.path] = w.name;
      if (!st.selection.workspaces.includes(w.path)) wg.append(new Option(`${w.name} (${w.count})`, "w:" + w.path));
    }
  }).catch(() => {});
  sel.addEventListener("change", () => {
    const v = sel.value;
    if (v.startsWith("f:")) st.selection.folders.push(v.slice(2));
    if (v.startsWith("w:")) st.selection.workspaces.push(v.slice(2));
    if (v) changed();
  });
  return sel;
}

// --- the outline, kept or cut by the person ------------------------------------

function dcOutline(rec) {
  const card = rsCard();
  card.replaceChildren();
  const o = rec.outline || { sections: [], gaps: [] };
  const word = (dcTypes[rec.type] || {}).short ? "section" : "chapter";
  card.append(mkEl("p", "rs-question", o.title || rec.source.name));
  rsHead(card, `Outline: ${o.sections.length} ${word}s`, "notes assigned to each");
  const kept = o.sections.map((s) => ({ ...s, keep: true }));
  const list = mkEl("ol", "dc-outline");
  const draw = () => {
    list.replaceChildren();
    kept.forEach((s, i) => {
      const li = mkEl("li", "dc-ch" + (s.keep ? "" : " dropped"));
      li.append(mkEl("span", "rs-n", String(i + 1)));
      const title = mkEl("span", "dc-title", s.title);
      title.title = s.brief || "";
      li.append(title);
      const n = (s.notes || []).length;
      const what = mkEl("span", "rs-pwhat" + (s.thin ? " dc-thin" : ""), !n ? "no notes" : s.thin ? `${n} note, thin` : `${n} notes`);
      li.append(what);
      const x = mkEl("button", "rs-x", s.keep ? "✕" : "↺");
      x.type = "button";
      x.setAttribute("aria-label", s.keep ? "drop this " + word : "keep this " + word);
      x.addEventListener("click", () => { s.keep = !s.keep; draw(); });
      li.append(x);
      list.append(li);
    });
  };
  draw();
  card.append(list);
  const uncovered = kept.filter((s) => s.thin);
  if (uncovered.length || (o.gaps || []).length) {
    const gaps = mkEl("section", "dc-gaps");
    gaps.setAttribute("aria-label", "gaps");
    const nums = uncovered.map((s) => kept.indexOf(s) + 1);
    gaps.append(mkEl("p", "dc-gaps-head", nums.length
      ? `Your notes do not cover ${word}${nums.length === 1 ? "" : "s"} ${nums.join(nums.length === 2 ? " and " : ", ")}`
      : "The outline needs what your notes do not hold"));
    for (const g of o.gaps || []) gaps.append(mkEl("p", "rs-help dc-gap", "· " + g));
    gaps.append(mkEl("p", "rs-help", "The writer does not search the web. A deep research on the gaps lands in the "
      + "Inbox; once you nucleate it, plan again and the outline reads the new notes."));
    const acts = mkEl("div", "rs-acts dc-gap-acts");
    if (uncovered.length) {
      const drop = mkEl("button", "rs-alt", `drop the ${uncovered.length === 1 ? word : uncovered.length + " " + word + "s"}`);
      drop.type = "button";
      drop.addEventListener("click", () => { for (const s of uncovered) s.keep = false; draw(); gaps.remove(); });
      const anyway = mkEl("button", "rs-alt", "write them from what there is");
      anyway.type = "button";
      anyway.addEventListener("click", () => gaps.remove());
      acts.append(drop, anyway);
    }
    const research = mkEl("button", "rs-go", "Research the gaps");
    research.type = "button";
    research.addEventListener("click", () => {
      const topics = [...uncovered.map((s) => s.title), ...(o.gaps || [])];
      rsOpen({ shape: "cluster", question: topics.join("; ") }, true);
    });
    acts.append(research);
    gaps.append(acts);
    card.append(gaps);
  }
  const foot = mkEl("div", "rs-foot");
  foot.append(mkEl("span", "rs-aside", "Written in order into a new note, each claim citing the note it comes from."));
  const acts = mkEl("span", "rs-acts");
  const again = mkEl("button", "rs-alt", "Plan again");
  again.type = "button";
  again.title = "read the notes added or changed since, and plan the outline again";
  again.addEventListener("click", async () => {
    again.disabled = true;
    try { dcUpdate(await apiPost(`/document/${rec.id}/replan`)); card.closest(".rs-msg").remove(); }
    catch (e) { notify(e.status ? e.message : "the server could not be reached"); again.disabled = false; }
  });
  const go = mkEl("button", "rs-go", "Write it");
  go.type = "button";
  go.addEventListener("click", async () => {
    const sections = kept.filter((s) => s.keep).map(({ title, brief, words, passages }) => ({ title, brief, words, passages }));
    if (!sections.length) { notify("the outline has no section left", "warn"); return; }
    go.disabled = true;
    try {
      dcUpdate(await apiPost(`/document/${rec.id}/write`, { sections }));
      card.classList.add("rs-sent");
      card.replaceChildren(mkEl("p", "rs-help", `Writing ${sections.length} ${word}${sections.length === 1 ? "" : "s"} in order. `
        + "The note opens from the row above the box when it is done."));
    } catch (e) {
      notify(e.status ? e.message : "the server could not be reached");
      go.disabled = false;
    }
  });
  acts.append(again, go);
  foot.append(acts);
  card.append(foot);
}

// --- the row above the composer ----------------------------------------------

function dcWhat(rec) {
  const p = rec.progress || {};
  if (rec.state === "running") {
    if (rec.stage === "read") return `reading the notes · ${p.done || 0} of ${p.total || "…"}`;
    if (rec.stage === "plan") return "planning the outline";
    if (rec.stage === "write") return `writing · section ${Math.min((p.done || 0) + 1, p.total || 1)} of ${p.total || "…"}`;
    return "working";
  }
  // A stop or a failure after the plan keeps the outline, with the reason.
  if (rec.state === "planned") return rec.error ? `the outline is kept · ${rec.error}` : "the outline is ready";
  if (rec.state === "done") return "written from the vault";
  // recover() writes the conversions' sentence on every kind it interrupts
  return rec.state === "interrupted" ? "the server restarted while this document was being made" : rec.error || "it stopped";
}

function dcRow(rec) {
  const row = mkEl("div", "job rs-row");
  row.dataset.state = rec.state === "planned" ? "done" : rec.state;
  const dot = mkEl("span", "job-dot");
  dot.setAttribute("aria-hidden", "true");
  const label = (dcTypes[rec.type] || {}).label || "Document";
  const name = mkEl("span", "job-name", `${label}: ${(rec.outline && rec.outline.title) || rec.source.name}`);
  const what = mkEl("span", "job-what", dcWhat(rec));
  const acts = mkEl("span", "job-acts");
  if (rec.state === "running") {
    acts.append(jobButton("stop", "job-alt linklike", "stop; nothing is written", async () => {
      try { await apiPost(`/document/${rec.id}/stop`); } catch (e) { notify(e.status ? e.message : "the server could not be reached"); }
    }));
  } else if (rec.state === "planned") {
    acts.append(jobButton("review the outline", "job-go", "keep or cut the outline, then write it", () => dcOutline(rec)));
  } else if (rec.state === "done" && rec.note) {
    what.append(" · ");
    what.append(jobButton(noteStem(rec.note), "job-note linklike", "open " + rec.note, () => openNote(rec.note)));
    acts.append(jobButton("check it", "job-alt linklike", "open it and check each claim against your notes",
      async () => { await openNote(rec.note); setTimeout(() => checkSources(), 300); }));
  }
  if (rec.state !== "running") {
    const x = jobButton("✕", "job-x", "dismiss; a written note stays where it is", async () => {
      try { await apiPost(`/document/${rec.id}/dismiss`); dcById.delete(rec.id); dcRender(); }
      catch (e) { notify(e.status ? e.message : "the server could not be reached"); }
    });
    x.setAttribute("aria-label", "dismiss " + rec.source.name);
    acts.append(x);
  }
  row.append(dot, name, what, acts);
  const p = rec.progress || {};
  if (rec.state === "running" && p.total) row.append(progressBar((p.done || 0) / p.total, rec.stage === "write" ? "sections written" : "notes read"));
  return row;
}

function dcRender() {
  const all = [...dcById.values()].sort((a, b) => (a.created || 0) - (b.created || 0));
  dcTray.replaceChildren(...all.map(dcRow));
  dcTray.hidden = !dcTray.childElementCount;
}

function dcUpdate(rec) {
  if (!rec || !rec.id) return;
  const before = dcById.get(rec.id);
  if (rec.dismissed) dcById.delete(rec.id);
  else dcById.set(rec.id, rec);
  const moved = before && before.state === "running" && rec.state !== "running";
  if (moved && !dcTold.has(rec.id + rec.state)) {
    dcTold.add(rec.id + rec.state);
    const msg = rec.state === "planned" ? "The outline is ready to review"
      : rec.state === "done" ? "The document is written" : "The document stopped";
    if (!osNotify(msg, rec.source.name, `silica-document-${rec.id}`)) announce(msg);
    if (rec.state === "done") { loadVaultInfo(); markVaultChanged(); }
  }
  dcRender();
}

async function dcFetch() {
  try {
    const r = await fetch("/document");
    if (!r.ok) return;
    const { documents, types } = await r.json();
    if (types) dcTypes = types;
    const seen = new Set();
    for (const rec of documents || []) { seen.add(rec.id); dcUpdate(rec); }
    for (const id of [...dcById.keys()]) if (!seen.has(id)) dcById.delete(id);
    dcRender();
  } catch { /* the next reconnect reads the list again; the tray keeps what it had */ }
}

document.addEventListener("silica:document", (e) => dcUpdate(e.detail));
document.addEventListener("silica:stream-open", dcFetch);
dcFetch();
