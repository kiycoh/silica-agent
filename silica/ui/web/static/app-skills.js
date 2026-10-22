// --- skills --------------------------------------------------------------------
// Settings › Skills (ADR-0097 point 4): every skill `/` can run, grouped by the
// root that holds it (GET /skills), and one SKILL.md installed into this vault
// through POST /skills, first as a preview that writes nothing, then for real.
// Direction: docs/design/skills/skills-in-settings.html. Everything a skill
// file says reaches the page through textContent: it is someone's upload.

const SK_GROUPS = {
  vault: ["This vault", "what you install lands here; removable"],
  home: ["Every vault", "a folder you manage by hand; shown here, never written from here"],
  builtin: ["Built in", "shipped with Silica · read-only"],
};
const SK_SOURCE = { vault: "vault", home: "home", builtin: "built-in" };

async function skCall(url, init) {
  const r = await fetch(url, init);
  let d = {};
  try { d = await r.json(); } catch { /* the status still says what happened */ }
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

function skPost(file, confirm) {
  const body = new FormData();
  body.append("file", file);
  body.append("confirm", confirm ? "true" : "false");
  return skCall("/skills", { method: "POST", body });
}

function skBtn(text, cls) {
  const b = stEl("button", "st-btn" + (cls ? " " + cls : ""), text);
  b.type = "button";
  return b;
}

// `/name` in the grid face and the hint beside it, as the / picker shows them.
function skCommand(name, hint, cls) {
  const line = stEl("span", cls || "st-name");
  line.appendChild(stEl("span", "sk-cmd", "/" + name));
  if (hint) {
    const h = stEl("span", "sk-hint", hint);
    h.title = hint;
    line.append(document.createTextNode(" "), h);
  }
  return line;
}

// `after` carries what the redraw should say: the outcome line under the
// install row, and the name to flash once in its group.
async function renderSkills(host, after = {}) {
  host.querySelectorAll(".sk-instr, .sk-write, .sk-install, .sk-group, .st-note-line").forEach((e) => e.remove());
  let data;
  try { data = await skCall("/skills"); }
  catch { host.appendChild(stEl("div", "st-note-line", "could not read the skills")); return; }
  host.appendChild(await skInstructionsRow());
  if (data.proposed && data.proposed.length) host.appendChild(skProposedGroup(data.proposed, host));
  const write = skWriteRow(host, data, skDraft);
  skDraft = null;
  host.appendChild(write);
  const install = skInstallRow(host, data);
  host.appendChild(install);
  if (after.note) {
    const note = install.querySelector(".st-note");
    note.className = "st-note good";
    note.textContent = after.note;
  }
  for (const g of data.groups) host.appendChild(skGroup(g, data, host, after.fresh));
}

// --- writing one here, and the chat's + (ADR-0127) ----------------------------
// The fields become a SKILL.md in the page and go through the same preview and
// install as a picked file, so a written skill passes the checks a file does.
// `skDraft` is what the chat's + hands over: the words in the message box.
let skDraft = null;

function skWriteSkill(text) {
  skDraft = { body: text || "" };
  stState.section = "Skills";
  openSettings();
}

function skWriteRow(host, data, draft) {
  const row = stEl("div", "st-row sk-write");
  row.dataset.search = "skill write new persona create";
  const label = stEl("div", "st-label");
  label.appendChild(stEl("span", "st-name", "write a skill"));
  label.appendChild(stEl("div", "st-help", data.vault
    ? "Name it, say what it does, write what the model should do. A persona is worn by a chat "
      + "from /name until /persona off, instead of running once."
    : "Open a vault first: a skill is written into one."));
  row.appendChild(label);
  const ctl = stEl("div", "st-ctl");
  const open = skBtn("write one…", "primary");
  open.disabled = !data.vault;
  ctl.appendChild(open);
  row.appendChild(ctl);
  const form = stEl("div", "sk-form");
  form.hidden = true;
  const field = (text, el) => {
    const l = stEl("label", "sk-field");
    l.append(stEl("span", "sk-field-name", text), el);
    form.appendChild(l);
    return el;
  };
  const name = field("name, typed after /", stEl("input", "st-input"));
  name.placeholder = "integra-appunti";
  name.spellcheck = false;
  const desc = field("what it does", stEl("input", "st-input"));
  desc.placeholder = "Integrates my notes with the attached file, one chapter at a time";
  const body = field("instructions for the model · $ARGUMENTS is what you type after the name",
    stEl("textarea", "st-mem-text"));
  body.rows = 7;
  const persona = stEl("input");
  persona.type = "checkbox";
  const pl = stEl("label", "sk-check");
  pl.append(persona, document.createTextNode(" a persona: worn by the chat until /persona off"));
  form.appendChild(pl);
  const go = skBtn("preview", "primary");
  const acts = stEl("div", "sk-pv-actions");
  acts.appendChild(go);
  form.appendChild(acts);
  row.appendChild(form);
  row.appendChild(stEl("div", "st-note"));
  row.appendChild(stEl("div", "sk-preview-host"));
  const show = () => { form.hidden = false; open.hidden = true; name.focus({ preventScroll: true }); };
  open.addEventListener("click", show);
  go.addEventListener("click", () => {
    // JSON's double-quoted string is valid YAML: a description with ": " in
    // it would otherwise empty the whole frontmatter.
    const text = "---\nname: " + name.value.trim() + "\ndescription: " + JSON.stringify(desc.value.trim())
      + (persona.checked ? "\nmetadata:\n  persona: true" : "") + "\n---\n" + body.value.trim() + "\n";
    skPreview(host, row, new File([text], "SKILL.md", { type: "text/markdown" }));
  });
  if (draft && data.vault) {
    body.value = draft.body;
    show();
    setTimeout(() => row.scrollIntoView({ block: "nearest" }), 0);
  }
  return row;
}

// --- proposed by Silica (ADR-0127 point 5) --------------------------------------
// Written from a request the person made in three chats; nothing lists or runs
// one until it is installed here. Its requests are shown, so the person sees
// what it was made from, not only what it says.
function skProposedGroup(rows, host) {
  const el = stEl("div", "sk-group");
  el.appendChild(stEl("div", "st-conn-title", "Proposed by Silica"));
  el.appendChild(stEl("div", "sk-group-help",
    "written from a request you made in several chats · nothing runs until you install it"));
  for (const p of rows) {
    const row = stEl("div", "st-row sk-row sk-proposed");
    row.dataset.search = `proposed skill ${p.name} ${p.description}`.toLowerCase();
    const label = stEl("div", "st-label");
    label.appendChild(skCommand(p.name, ""));
    label.appendChild(stEl("div", "st-help", p.description));
    const from = stEl("details", "sk-from");
    from.appendChild(stEl("summary", "", `made from ${p.asks.length} requests · what it would do`));
    for (const a of p.asks) from.appendChild(stEl("div", "sk-ask", "“" + a + "”"));
    from.appendChild(stEl("pre", "sk-pv-body", p.body));
    label.appendChild(from);
    row.appendChild(label);
    const ctl = stEl("div", "st-ctl");
    const no = skBtn("dismiss");
    const yes = skBtn("install", "primary");
    ctl.append(no, yes);
    row.appendChild(ctl);
    row.appendChild(stEl("div", "st-note"));
    const act = async (verb, done) => {
      yes.disabled = no.disabled = true;
      try {
        await skCall(`/skills/proposed/${encodeURIComponent(p.name)}/${verb}`, { method: "POST" });
      } catch (e) {
        yes.disabled = no.disabled = false;
        stNote(row, "bad", "✕ " + String(e.message || e));
        return;
      }
      if (typeof loadCommands === "function") loadCommands();
      renderSkills(host, done);
    };
    yes.addEventListener("click", () => act("install", { note: `installed · type /${p.name} in the chat`, fresh: p.name }));
    no.addEventListener("click", () => act("dismiss", { note: `dismissed · /${p.name} will not be proposed again` }));
    el.appendChild(row);
  }
  return el;
}

// The vault's instructions (ADR-0127 point 4): one file every chat over this
// vault reads, as its author's words. Saved on its button like a memory file,
// and for the same reason: the server may refuse it over the cap.
async function skInstructionsRow() {
  const row = stEl("div", "st-row sk-instr");
  row.dataset.search = "vault instructions author every chat";
  const label = stEl("div", "st-label");
  const lab = stEl("label", "st-name", "vault instructions");
  label.appendChild(lab);
  row.appendChild(label);
  let d;
  try { d = await skCall("/instructions"); }
  catch { label.appendChild(stEl("div", "st-help", "could not read the instructions")); return row; }
  label.appendChild(stEl("div", "st-help", d.available
    ? `what every chat over this vault reads first, as the vault's author's words · ${d.path}`
    : "Open a vault first: instructions belong to one."));
  if (!d.available) return row;
  const ctl = stEl("div", "st-ctl");
  const count = stEl("span", "st-mem-count");
  const save = skBtn("save");
  ctl.append(count, save);
  row.appendChild(ctl);
  const area = stEl("textarea", "st-mem-text");
  area.id = "sk-instr-text";
  lab.setAttribute("for", area.id);
  area.rows = 4;
  area.spellcheck = false;
  area.placeholder = "e.g. Cite articles as art. 1321 c.c. and quote the article before explaining it.";
  area.value = area.dataset.saved = d.text;
  row.appendChild(area);
  row.appendChild(stEl("div", "st-note"));
  const paint = () => {
    const n = area.value.trim().length;
    count.textContent = `${n} / ${d.cap}`;
    count.classList.toggle("over", n > d.cap);
    save.disabled = area.value === area.dataset.saved;
  };
  area.addEventListener("input", () => { paint(); stNote(row, "", ""); });
  save.addEventListener("click", async () => {
    save.disabled = true;
    stNote(row, "pending", "saving…");
    try {
      await skCall("/instructions", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: area.value }) });
    } catch (e) {
      stNote(row, "bad", "✕ " + String(e.message || e));
      paint();
      return;
    }
    area.value = area.dataset.saved = area.value.trim();
    stNote(row, "good", "✓ saved · read from the next chat");
    paint();
  });
  paint();
  return row;
}

function skInstallRow(host, data) {
  const row = stEl("div", "st-row sk-install");
  row.dataset.search = "skill install skill.md";
  const label = stEl("div", "st-label");
  label.appendChild(stEl("span", "st-name", "install a skill"));
  label.appendChild(stEl("div", "st-help", data.vault
    ? "One SKILL.md in the Agent Skills format, picked or dropped here. It goes into this vault, "
      + "under .silica/skills/<name>/, and runs only when you type its name after /."
    : "Open a vault first: a skill is installed into one."));
  row.appendChild(label);
  const ctl = stEl("div", "st-ctl");
  const pick = stEl("input");
  pick.type = "file";
  pick.accept = ".md,text/markdown,text/plain";
  pick.hidden = true;
  const choose = skBtn("choose SKILL.md", "primary");
  choose.disabled = !data.vault;
  choose.addEventListener("click", () => pick.click());
  ctl.append(pick, choose);
  row.appendChild(ctl);
  row.appendChild(stEl("div", "st-note"));
  const preview = stEl("div", "sk-preview-host");
  row.appendChild(preview);

  const take = (file) => { if (file && data.vault) skPreview(host, row, file); };
  pick.addEventListener("change", () => { take(pick.files[0]); pick.value = ""; });
  row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("sk-over"); });
  row.addEventListener("dragleave", () => row.classList.remove("sk-over"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("sk-over");
    take(e.dataTransfer.files[0]);
  });
  return row;
}

async function skPreview(host, row, file) {
  const slot = row.querySelector(".sk-preview-host");
  const note = row.querySelector(".st-note");
  note.className = "st-note pending";
  note.textContent = "reading " + file.name + "…";
  slot.innerHTML = "";
  const box = stEl("div", "sk-preview");
  const actions = stEl("div", "sk-pv-actions");
  const dismiss = skBtn("cancel");
  dismiss.addEventListener("click", () => { slot.innerHTML = ""; });
  let s;
  try {
    s = (await skPost(file, false)).skill;
  } catch (e) {
    note.className = "st-note";
    note.textContent = "";
    box.classList.add("sk-bad");
    box.appendChild(stEl("div", "sk-pv-label", file.name));
    box.appendChild(stEl("div", "sk-pv-err", String(e.message || e)));
    // The written skill's row has no file to pick again: its fields are the fix.
    const picker = row.querySelector("input[type=file]");
    if (picker) {
      const again = skBtn("choose another file");
      again.addEventListener("click", () => picker.click());
      actions.append(dismiss, again);
    } else actions.append(dismiss);
    box.appendChild(actions);
    slot.appendChild(box);
    return;
  }
  note.className = "st-note";
  note.textContent = "";
  box.appendChild(skCommand(s.name, s.hint, "sk-pv-head"));
  box.appendChild(stEl("div", "sk-pv-desc", s.description || "no description"));
  const facts = stEl("div", "sk-pv-facts");
  facts.append(
    stEl("span", "", `${(s.bytes / 1024).toFixed(1)} KiB of ${s.cap / 1024}`),
    stEl("span", "", "goes to " + s.path),
    stEl("span", "", "nothing in it runs"),
  );
  box.appendChild(facts);
  if (s.replaces) box.appendChild(stEl("div", "st-warn", `You already have a vault skill named ${s.name}. Installing this one replaces it.`));
  else if (s.shadows) box.appendChild(stEl("div", "sk-also", `While it is here, it answers /${s.name} in place of the ${SK_SOURCE[s.shadows]} skill of that name.`));
  box.appendChild(stEl("div", "sk-pv-label", s.persona
    ? `a persona: what every answer of a chat follows from /${s.name} until /persona off`
    : `what the model receives when you type /${s.name}`));
  box.appendChild(stEl("pre", "sk-pv-body", s.body));
  if (s.stripped) box.appendChild(stEl("div", "st-help", "Maintainer comments (<!-- -->) are left out above, as the loader leaves them out of the turn."));
  const ok = skBtn(s.replaces ? `replace your vault skill ${s.name}` : "install to this vault", "primary");
  ok.addEventListener("click", async () => {
    ok.disabled = true;
    try {
      await skPost(file, true);
    } catch (e) {
      ok.disabled = false;
      note.className = "st-note bad";
      note.textContent = String(e.message || e);
      return;
    }
    if (typeof loadCommands === "function") loadCommands();
    renderSkills(host, { note: `installed · type /${s.name} in the chat`, fresh: s.name });
  });
  actions.append(dismiss, ok);
  box.appendChild(actions);
  slot.appendChild(box);
  ok.focus({ preventScroll: true });
}

function skGroup(g, data, host, fresh) {
  const [title, help] = SK_GROUPS[g.source];
  const el = stEl("div", "sk-group");
  el.appendChild(stEl("div", "st-conn-title", title));
  const line = stEl("div", "sk-group-help");
  if (g.dir) line.appendChild(stEl("code", "", g.dir));
  line.appendChild(document.createTextNode((g.dir ? " · " : "") + help));
  el.appendChild(line);
  if (!g.skills.length) el.appendChild(stEl("div", "st-note-line", "none yet"));
  for (const s of g.skills) el.appendChild(skRow(s, data, host, fresh));
  return el;
}

function skRow(s, data, host, fresh) {
  const row = stEl("div", "st-row sk-row");
  row.dataset.search = `skill ${s.name} ${s.hint} ${s.description} ${s.source}`.toLowerCase();
  const label = stEl("div", "st-label");
  label.appendChild(skCommand(s.name, s.hint));
  label.appendChild(stEl("div", "st-help", s.description));
  if (s.persona) label.appendChild(stEl("div", "sk-also", `persona: a chat wears it from /${s.name} until /persona off`));
  if (s.state === "inactive") {
    label.appendChild(stEl("div", "st-warn", `⚠ never runs: /${s.name} is a built-in command, which answers first`));
  } else if (s.shadows) {
    label.appendChild(stEl("div", "sk-also", `answers /${s.name} in place of the ${s.shadows.map((x) => SK_SOURCE[x]).join(" and ")} skill while it is here`));
  }
  row.appendChild(label);
  const ctl = stEl("div", "st-ctl");
  if (s.source === "vault") {
    const rm = skBtn("remove");
    rm.addEventListener("click", () => skRemove(s, data, host, rm));
    ctl.appendChild(rm);
  } else {
    const text = s.state === "replaced" ? `replaced by your ${SK_SOURCE[s.by]} skill`
      : s.state === "inactive" ? "never runs" : s.source === "home" ? "managed by hand" : "built in";
    ctl.appendChild(stEl("div", "st-ctl-text" + (s.state === "active" ? "" : " down"), text));
  }
  row.appendChild(ctl);
  row.appendChild(stEl("div", "st-note"));
  if (fresh === s.name && s.source === "vault") row.classList.add("sk-fresh");
  return row;
}

async function skRemove(s, data, host, btn) {
  const others = data.groups.filter((g) => g.source !== "vault")
    .flatMap((g) => g.skills).filter((o) => o.name === s.name && o.state !== "inactive");
  const back = s.state === "inactive" ? `/${s.name} stays the built-in command it already was.`
    : others.length ? `Your ${SK_SOURCE[others[0].source]} /${s.name} answers again.`
    : `/${s.name} leaves the / picker.`;
  const ok = await stAsk(`remove /${s.name}?`, `Its SKILL.md leaves .silica/skills/${s.name}/ in this vault.\n${back}`, "remove");
  if (!ok) return;
  btn.disabled = true;
  let out;
  try {
    out = await skCall(`/skills/${encodeURIComponent(s.name)}`, { method: "DELETE" });
  } catch (e) {
    btn.disabled = false;
    const note = btn.closest(".st-row").querySelector(".st-note");
    note.className = "st-note bad";
    note.textContent = String(e.message || e);
    return;
  }
  if (typeof loadCommands === "function") loadCommands();
  const kept = out.kept.length ? ` · its folder keeps ${out.kept.length} other file${out.kept.length > 1 ? "s" : ""}` : "";
  renderSkills(host, { note: `removed /${s.name}${kept}` });
}
