// --- a deep research (ADR-0158 point 8) ----------------------------------------
// The + menu's "deep research…" opens a form in the conversation; "Plan it"
// asks for the sub-questions, which the person edits; "Approve and start" is
// the spend, and the job runs behind the chat, a loop per sub-question, one
// after another. Its row sits above the composer with the conversions', and
// when it ends it becomes the choice of what to nucleate: the parts are in the
// Inbox, and the person's yes to "Nucleate N parts" is the step into the vault.
// Nothing is recorded before the approval: an abandoned form is just gone.
const rsTray = $("#research");
const rsById = new Map();
const rsToldDone = new Set();
// The depths' caps as the server runs them (GET /research), so the line under
// the form cannot promise what start() refuses.
let rsDepths = { quick: { parts: 4, searches: 8 }, standard: { parts: 8, searches: 12 }, deep: { parts: 12, searches: 20 } };

const RS_SHAPES = [
  { id: "literature", label: "Literature", ask: "The question",
    help: "Studies and reviews on a topic. A protocol is fixed before the search: what counts as evidence, what is left out.",
    hint: "How do mRNA vaccines differ from vector vaccines in their formulation?" },
  { id: "investigation", label: "Investigation", ask: "The matter",
    help: "A contested matter with named people. Primary documents first; every claim keeps who asserts it and its status.",
    hint: "What do the court filings establish, and what is only reported?" },
  { id: "anchored", label: "A note of yours", ask: "The note to look into",
    help: "Look deeper into a note you have, such as a video's transcript: each claim it makes is researched on its own." },
  { id: "cluster", label: "Cluster", ask: "The theme",
    help: "A theme your vault lacks, as a map of notes: each sub-question is one note to write.",
    hint: "The immune response, from antigen to memory cell" },
];
const rsShape = (id) => RS_SHAPES.find((s) => s.id === id) || RS_SHAPES[0];

function rsSeg(label, options, value, onPick) {
  const seg = mkEl("div", "rs-seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", label);
  for (const [id, text] of options) {
    const b = mkEl("button", id === value ? "on" : "", text);
    b.type = "button";
    b.setAttribute("aria-pressed", String(id === value));
    b.addEventListener("click", () => onPick(id));
    seg.append(b);
  }
  return seg;
}

function rsField(labelText, control) {
  const wrap = mkEl("label", "rs-field");
  wrap.append(mkEl("span", "rs-lab", labelText), control);
  return wrap;
}

// The note open beside the chat, which "A note of yours" looks into.
function rsOpenNote() {
  return notePanel.classList.contains("open") && drawerMode !== "node" && lastNotePath ? lastNotePath : "";
}

// One card at a time, in the conversation where the person asked for it, in
// the column a message's text takes (`.rs-msg` pads and centres as `.msg` does).
function rsCard() {
  let card = log.querySelector(".rs-card:not(.rs-sent)");
  if (!card) {
    const wrap = mkEl("div", "rs-msg");
    card = mkEl("section", "rs-card");
    card.setAttribute("aria-label", "deep research");
    wrap.append(card);
    if (log.childElementCount) log.append(wrap);
    else swapLanding(() => log.append(wrap));
    card.scrollIntoView({ block: "nearest" });
  }
  return card;
}

function rsOpen(prefill = {}) {
  const st = { shape: "literature", question: "", depth: "standard", scholarly: true, ...prefill };
  if (st.shape === "anchored" && !st.note) st.note = rsOpenNote();
  rsForm(rsCard(), st);
}

function rsHead(card, title, aside) {
  const head = mkEl("div", "rs-head");
  head.append(mkEl("h2", "", title), mkEl("span", "rs-aside", aside));
  card.append(head);
}

function rsForm(card, st) {
  card.replaceChildren();
  rsHead(card, "Deep research", "nothing is fetched until you approve the plan");
  const shape = rsShape(st.shape);
  card.append(rsField("Shape", rsSeg("shape", RS_SHAPES.map((s) => [s.id, s.label]), st.shape, (id) => {
    st.shape = id;
    st.scholarly = id === "literature";
    if (id === "anchored" && !st.note) st.note = rsOpenNote();
    rsForm(card, st);
  })));
  card.append(mkEl("p", "rs-help", shape.help));
  if (st.shape === "anchored") {
    const which = mkEl("div", "rs-note");
    if (st.note) {
      const a = mkEl("button", "linklike", noteStem(st.note));
      a.type = "button";
      a.addEventListener("click", () => openNote(st.note));
      which.append(a, mkEl("span", "rs-aside", " the note open beside the chat"));
    } else {
      which.textContent = "Open the note beside the chat first, then choose this shape again.";
    }
    card.append(rsField(shape.ask, which));
  } else {
    const q = mkEl("textarea", "rs-input");
    q.rows = 2;
    q.value = st.question || "";
    q.placeholder = shape.hint || "";
    q.addEventListener("input", () => { st.question = q.value; });
    card.append(rsField(shape.ask, q));
    setTimeout(() => q.focus(), 0);
  }
  const sources = mkEl("div", "rs-checks");
  const web = mkEl("label", "rs-check");
  const w = mkEl("input");
  w.type = "checkbox"; w.checked = true; w.disabled = true;
  web.append(w, " The web");
  const sch = mkEl("label", "rs-check");
  const s = mkEl("input");
  s.type = "checkbox"; s.checked = !!st.scholarly;
  s.addEventListener("change", () => { st.scholarly = s.checked; });
  sch.append(s, " Scholarly sources (papers, with their abstracts)");
  sources.append(web, sch, mkEl("span", "rs-aside", "Your vault is read first, to skip what you have."));
  card.append(rsField("Sources", sources));
  const caps = rsDepths[st.depth] || rsDepths.standard;
  card.append(rsField("Depth", rsSeg("depth", [["quick", "Quick"], ["standard", "Standard"], ["deep", "Deep"]], st.depth,
    (id) => { st.depth = id; rsForm(card, st); })));
  card.append(mkEl("p", "rs-help", `Up to ${caps.parts} sub-questions, one search loop each, at most ${caps.searches} steps a loop. `
    + "You see and edit the plan before anything runs."));
  const foot = mkEl("div", "rs-foot");
  foot.append(mkEl("span", "rs-aside", "Lands in the Inbox, as one note with a part per sub-question."));
  const acts = mkEl("span", "rs-acts");
  const cancel = mkEl("button", "rs-alt", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", () => card.closest(".rs-msg").remove());
  const go = mkEl("button", "rs-go", "Plan it");
  go.type = "button";
  go.addEventListener("click", () => rsPlan(card, st, go));
  acts.append(cancel, go);
  foot.append(acts);
  card.append(foot);
}

async function rsPlan(card, st, btn) {
  btn.disabled = true;
  btn.textContent = "Planning…";
  try {
    const plan = await apiPost("/research/plan", { shape: st.shape, question: st.question, note: st.note || "",
      depth: st.depth, scholarly: !!st.scholarly });
    rsPlanCard(card, st, plan);
  } catch (e) {
    notify(e.status ? e.message : "the server could not be reached");
    btn.disabled = false;
    btn.textContent = "Plan it";
  }
}

function rsPlanCard(card, st, plan) {
  card.replaceChildren();
  const q = mkEl("p", "rs-question", plan.question);
  card.append(q);
  rsHead(card, `Plan: ${rsShape(plan.shape).label.toLowerCase()}, ${plan.parts.length} sub-question${plan.parts.length === 1 ? "" : "s"}`,
    "edit, remove or add; it holds once approved");
  let protocol = null;
  if (plan.protocol) {
    protocol = mkEl("textarea", "rs-input");
    protocol.rows = 3;
    protocol.value = plan.protocol;
    protocol.addEventListener("input", () => autoGrow(protocol));
    requestAnimationFrame(() => autoGrow(protocol));
    card.append(rsField("Protocol", protocol));
  }
  const list = mkEl("ol", "rs-parts");
  const rows = [];
  const addRow = (part) => {
    const li = mkEl("li", "rs-part");
    // A textarea that grows: a sub-question runs past one line, and a cut one
    // cannot be read before it is approved.
    const text = mkEl("textarea", "rs-input");
    text.rows = 1;
    text.value = part.q;
    text.maxLength = 300;
    text.setAttribute("aria-label", "sub-question");
    text.addEventListener("input", () => autoGrow(text));
    li.append(mkEl("span", "rs-n", String(list.childElementCount + 1)), text);
    requestAnimationFrame(() => autoGrow(text));
    const row = { part, text, li };
    if (part.held) {
      const held = mkEl("div", "rs-held");
      const a = mkEl("button", "linklike", noteStem(part.held));
      a.type = "button";
      a.addEventListener("click", () => openNote(part.held));
      const again = mkEl("button", "linklike", "research it anyway");
      again.type = "button";
      again.addEventListener("click", () => { part.held = ""; held.remove(); });
      held.append("Your vault has ", a, ": it is linked, not researched again · ", again);
      li.append(held);
    }
    const x = mkEl("button", "rs-x", "✕");
    x.type = "button";
    x.setAttribute("aria-label", "remove this sub-question");
    x.addEventListener("click", () => {
      li.remove();
      rows.splice(rows.indexOf(row), 1);
      rows.forEach((r, i) => { r.li.querySelector(".rs-n").textContent = String(i + 1); });
      sync();
    });
    li.append(x);
    rows.push(row);
    list.append(li);
  };
  plan.parts.forEach(addRow);
  card.append(list);
  const add = mkEl("button", "linklike rs-add", "+ add a sub-question");
  add.type = "button";
  add.addEventListener("click", () => {
    addRow({ q: "", held: "" });
    rows.at(-1).text.focus();
    sync();
  });
  card.append(add);
  const est = mkEl("p", "rs-help");
  card.append(est);
  const sync = () => {
    const loops = rows.filter((r) => !r.part.held).length;
    add.hidden = rows.length >= plan.max_parts;
    est.textContent = `${loops} search loop${loops === 1 ? "" : "s"}, one after another, at most ${plan.searches} steps each. `
      + "Each step is a model call, counted in your usage window. Topics met on the way come back as a list at "
      + "the end and are not researched in this run.";
  };
  sync();
  const foot = mkEl("div", "rs-foot");
  foot.append(mkEl("span", "rs-aside", "Approving spends; it adds nothing to your vault."));
  const acts = mkEl("span", "rs-acts");
  const back = mkEl("button", "rs-alt", "Back to the form");
  back.type = "button";
  back.addEventListener("click", () => rsForm(card, st));
  const go = mkEl("button", "rs-go", "Approve and start");
  go.type = "button";
  go.addEventListener("click", async () => {
    const parts = rows.map((r) => ({ q: r.text.value.trim(), held: r.part.held })).filter((p) => p.q);
    if (!parts.length) { notify("the plan has no sub-question left", "warn"); return; }
    go.disabled = true;
    try {
      const rec = await apiPost("/research/start", { plan: { ...plan, protocol: protocol ? protocol.value : plan.protocol, parts } });
      card.classList.add("rs-sent");
      const asked = /[.?!]$/.test(plan.question) ? plan.question : plan.question + ".";
      card.replaceChildren(mkEl("p", "rs-help", `Deep research started: ${asked} It runs behind the chat; `
        + "its parts show above the box as they are written."));
      rsUpdate(rec);
    } catch (e) {
      notify(e.status ? e.message : "the server could not be reached");
      go.disabled = false;
    }
  });
  acts.append(back, go);
  foot.append(acts);
  card.append(foot);
}

// --- the row above the composer ----------------------------------------------

function rsPartWhat(p, rec) {
  if (p.state === "done") return `${p.quotes} quote${p.quotes === 1 ? "" : "s"} · ${p.pages} page${p.pages === 1 ? "" : "s"}`;
  // Tool calls, not the loop's steps: one step can search and fetch at once,
  // so a count "of" the cap would run past it.
  if (p.state === "running") return `reading · ${p.steps} call${p.steps === 1 ? "" : "s"} · ${p.pages} page${p.pages === 1 ? "" : "s"}`;
  if (p.state === "held") return "in your vault, linked";
  if (p.state === "empty") return "nothing found";
  if (p.state === "skipped" || p.state === "stopped") return "not researched";
  return "waiting";
}

function rsRow(rec) {
  const row = mkEl("div", "job rs-row");
  row.dataset.state = rec.state;
  const dot = mkEl("span", "job-dot");
  dot.setAttribute("aria-hidden", "true");
  const done = rec.parts.filter((p) => ["done", "empty", "held", "skipped", "stopped"].includes(p.state)).length;
  const label = rec.state === "running" ? "Deep research: " : rec.state === "done" ? "Research done: " : "Research stopped: ";
  const name = mkEl("span", "job-name", label + rec.question);
  name.title = rec.question;
  const what = mkEl("span", "job-what");
  const acts = mkEl("span", "job-acts");
  row.append(dot, name, what);
  if (rec.state === "running") {
    what.textContent = `${done} of ${rec.parts.length}`;
    acts.append(jobButton("stop", "job-alt linklike", "stop after the sub-question being read", async () => {
      try { await apiPost(`/research/${rec.id}/stop`); announce("the research stops after this sub-question"); }
      catch (e) { notify(e.status ? e.message : "the server could not be reached"); }
    }));
  } else if (rec.state === "done") {
    const written = rec.parts.filter((p) => p.note).length;
    what.append(`${written} part${written === 1 ? "" : "s"} in the Inbox · `);
    what.append(jobButton("open", "job-note linklike", "open the note that lists the parts", () => openNote(rec.parent)));
    if (rec.nucleated) what.append(" · nucleated");
  } else {
    what.textContent = rec.error || (rec.state === "interrupted" ? "the server restarted while this research was running" : "it stopped");
  }
  if (rec.state !== "running") {
    const x = jobButton("✕", "job-x", "dismiss; the notes stay where they are", async () => {
      try { await apiPost(`/research/${rec.id}/dismiss`); rsById.delete(rec.id); rsRender(); }
      catch (e) { notify(e.status ? e.message : "the server could not be reached"); }
    });
    x.setAttribute("aria-label", "dismiss " + rec.question);
    acts.append(x);
  }
  row.append(acts);
  if (!(rec.state === "done" && rec.nucleated)) row.append(rsParts(rec));
  if (rec.state === "done" && !rec.nucleated) row.append(rsChooser(rec, row));
  if (rec.state === "done" && (rec.topics || []).length) row.append(rsTopics(rec));
  return row;
}

// The parts as a list. Once the job is done, a written part is a checkbox:
// the thin ones (fewer than three quotes) start unticked, so a part resting on
// too little is not nucleated unread.
function rsParts(rec) {
  const ol = mkEl("ol", "rs-plist");
  const done = rec.state === "done";
  rec.parts.forEach((p, i) => {
    const li = mkEl("li", "rs-pitem");
    li.dataset.state = p.state;
    const n = mkEl("span", "rs-n", String(i + 1));
    const q = mkEl("span", "rs-q");
    if (done && p.note) {
      const box = mkEl("input");
      box.type = "checkbox";
      box.checked = p.quotes >= 3;
      box.dataset.note = p.note;
      box.setAttribute("aria-label", "nucleate this part");
      const open = jobButton(p.q, "linklike rs-qlink", "open this part", () => openNote(p.note));
      q.append(box, " ", open);
    } else if (p.state === "held") {
      q.append("In your vault already: ");
      q.append(jobButton(noteStem(p.held), "linklike", "open " + p.held, () => openNote(p.held)));
    } else {
      q.textContent = p.q;
    }
    const what = mkEl("span", "rs-pwhat", rsPartWhat(p, rec) + (done && p.note && p.quotes < 3 ? ", thin" : ""));
    li.append(n, q, what);
    ol.append(li);
  });
  return ol;
}

function rsChooser(rec, row) {
  const foot = mkEl("div", "rs-choose");
  foot.append(mkEl("span", "rs-aside", "Writes notes into your vault. One undo takes the whole run back."));
  const acts = mkEl("span", "rs-acts");
  const later = jobButton("not now", "job-alt linklike", "close this; the parts stay in the Inbox", async () => {
    try { await apiPost(`/research/${rec.id}/dismiss`); rsById.delete(rec.id); rsRender(); }
    catch (e) { notify(e.status ? e.message : "the server could not be reached"); }
  });
  const go = jobButton("", "job-go", "nucleate the ticked parts into the vault, in this chat", () => {
    const parts = [...row.querySelectorAll(".rs-plist input[type=checkbox]:checked")].map((b) => b.dataset.note);
    if (parts.length) rsNucleate(rec, parts);
  });
  const count = () => {
    const n = row.querySelectorAll(".rs-plist input[type=checkbox]:checked").length;
    go.textContent = `Nucleate ${n} part${n === 1 ? "" : "s"}`;
    go.disabled = !n;
  };
  row.addEventListener("change", count);
  queueMicrotask(count);
  acts.append(later, go);
  foot.append(acts);
  return foot;
}

function rsTopics(rec) {
  const box = mkEl("div", "rs-topics");
  box.append(mkEl("span", "rs-aside", "Met on the way, not researched in this run:"));
  for (const t of rec.topics) {
    const item = mkEl("span", "rs-topic", t + " ");
    item.append(jobButton("research this", "linklike", "a new research on this topic, with its own plan",
      () => rsOpen({ shape: rec.shape === "anchored" ? "literature" : rec.shape, question: t, depth: rec.depth })));
    box.append(item);
  }
  return box;
}

function rsNucleate(rec, parts) {
  if (streaming) { notify("a turn is running: nucleate when it ends", "warn"); return; }
  const said = `Nucleate ${parts.length} part${parts.length === 1 ? "" : "s"} of “${rec.question}”`;
  const body = bubble("user");
  body.textContent = said;
  body.dataset.words = said;
  const go = () => runTurn(postJson(`/research/${rec.id}/nucleate`, { parts }), "working", go);
  go();
}

function rsRender() {
  const all = [...rsById.values()].sort((a, b) => (a.created || 0) - (b.created || 0));
  rsTray.replaceChildren(...all.map(rsRow));
  rsTray.hidden = !rsTray.childElementCount;
}

function rsUpdate(rec) {
  if (!rec || !rec.id) return;
  const before = rsById.get(rec.id);
  if (rec.dismissed) rsById.delete(rec.id);
  else rsById.set(rec.id, rec);
  const ended = rec.state !== "running" && before && before.state === "running";
  if (ended && !rsToldDone.has(rec.id)) {
    rsToldDone.add(rec.id);
    const msg = rec.state === "done" ? `The research is done: ${rec.question}` : `The research stopped: ${rec.question}`;
    if (!osNotify(rec.state === "done" ? "Research done" : "Research stopped", msg, `silica-research-${rec.id}`)) announce(msg);
    loadVaultInfo();
    markVaultChanged();
  }
  // A checkbox the person ticked survives a progress event of another job.
  if (before && before.state === rec.state && rec.state === "done" && before.nucleated === rec.nucleated) return;
  rsRender();
}

async function rsFetch() {
  try {
    const r = await fetch("/research");
    if (!r.ok) return;
    const { research, depths } = await r.json();
    if (depths) rsDepths = depths;
    const seen = new Set();
    for (const rec of research) { seen.add(rec.id); rsUpdate(rec); }
    for (const id of [...rsById.keys()]) if (!seen.has(id)) rsById.delete(id);
    rsRender();
  } catch { /* the next reconnect reads the list again; the tray keeps what it had */ }
}

document.addEventListener("silica:research", (e) => rsUpdate(e.detail));
document.addEventListener("silica:stream-open", rsFetch);
rsFetch();
