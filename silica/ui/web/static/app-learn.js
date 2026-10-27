// --- learn tab ---------------------------------------------------------------
// The Learn tab (learn-suite §5, refined 2026-09-07 per docs/design/learn/
// ui-ux-refinement.md, rethought by ADR-0081, then by ADR-0119): it opens on
// one column, "What would you like to learn?", the next session as the list
// of notes it will ask with a reason each, what you missed and your plans. A
// test or a plan is made in one dialog over that column; a plan, a note and
// the map ("What you know") are views of their own; one session, its receipt. Everything on screen
// is a reading of the ledger through /learn; the answer key never reaches
// this file before the reader has answered (G5), and a ledger line is
// written only on `rate`.
//
// How it is laid out is ADR-0061: each fact once, as a figure or a mark, at
// the moment it is expected. At rest what you act on next; on request, in a
// closed disclosure, what explains; a learning state is a mark (○ ◐ ●), never
// green or red; amber only for caution; one primary action per view.
//
// Sub-states live in the hash (#learn/plan/<name>, #learn/note/<path>,
// #learn/plans, #learn/map, #learn/new, #learn/edit/<plan>, #learn/session/<id>,
// #learn/receipt/<id>) so Back, Forward and a reload restore them. One live session is held by the server and
// mirrored here as `lrSession`; leaving the view keeps it, a reload
// re-attaches to it through GET /learn/session, and a second start is
// refused by the server (409) until the first is closed.
let lrData = null;            // the /learn payload
let lrRoute = { view: "home", plan: "", path: "", id: "" };
let lrPlan = null;            // the selected plan's /learn/plan report
let lrNote = null;            // the selected note's /learn/note detail (a row of Next, a cell of the Map)
let lrSeg = "next";           // "map" while the map shows: the gate counts its openings once
let lrMap = null;             // the /learn/map payload
let lrMapLoading = false;
let lrMapPlan = "";           // the plan filtering the map, "" for every folder
let lrMapCounted = false;     // the map's opening is counted once per visit (the use gate)
let lrSession = null;         // {id, plan, note, origin, n, items[], cur, ...}
let lrReceipt = null;
let lrLoading = false;
let lrStarting = false;       // a session start owns the loading overlay until it returns
let lrPlansError = false;     // the last /learn read failed: an error is not an empty vault
let lrGradeTimer = null;
const lrMore = new Set();     // the groups of Next (and the map) shown in full
let lrEndAsk = false;         // the End session choice is open
let lrPrevOpen = false;       // the previous question's feedback is unfolded
const lrDiscOpen = new Set(); // disclosures the reader opened, kept across re-renders
const LR_LAST_PLAN = "silica.learn.lastPlan";

const LR_CONF = [["certain", 90], ["pretty sure", 70], ["half unsure", 50], ["guessing", 25]];
const LR_RATING = { 1: "again", 2: "hard", 3: "good", 4: "easy" };
const LR_LEVEL = { 1: "Facts", 2: "Parts", 3: "Mechanisms", 4: "New contexts" };
const LR_OBJ = { recall: "Recall information", explain: "Explain how and why", apply: "Apply to a case", compare: "Compare and distinguish", evaluate: "Evaluate and justify" };
const LR_OBJ_SHORT = { recall: "recall", explain: "explain", apply: "apply", compare: "compare", evaluate: "evaluate" };
const LR_OBJ_DESC = {
  recall: "Restate definitions, facts or steps without looking them up.",
  explain: "Make relations, mechanisms and conditions of validity explicit.",
  apply: "Use a principle or a procedure, showing the steps it requires.",
  compare: "Find similarities, differences and the conditions that make them matter.",
  evaluate: "Support a choice or a judgement with criteria and evidence.",
};
const LR_SCOPE = { notes: "some notes", folder: "a folder", topic: "a topic", steps: "a syllabus", workspace: "a workspace" };
const LR_GRADE_WAIT_MS = 20000;   // §5.1: after 20 s without a verdict, self-grade with the evidence
// An explanation said aloud runs longer than a chat message (ADR-0049); the
// server's upload cap (_STT_MAX_BYTES) is sized for this ceiling.
const LR_MIC_MAX_MS = 180000;
const LR_READ_KEY = "silica.learn.readAloud";
let lrReadAloud = (() => { try { return localStorage.getItem(LR_READ_KEY) === "1"; } catch { return false; } })();
let lrSpoken = new Set();     // what was already read aloud, so a re-render never repeats it

const lrEl = (tag, cls, text) => mkEl(tag, cls, text);
const lrStem = (p) => String(p || "").replace(/\.md$/, "").split("/").pop();
const lrDays = (s) => (typeof s === "number" ? `~${Math.round(s)} d` : "new");
const lrDate = (iso) => {
  if (!iso) return "";
  const d = new Date(typeof iso === "number" ? iso * 1000 : iso);
  // English, as every other word on the tab: the reasons read "missed Oct 6"
  // and a plan row "by 30 nov" beside them looked like two products. A bare
  // date is that date (parsed as UTC midnight); a moment is the local day
  const bare = typeof iso === "string" && /^\d{4}-\d{2}-\d{2}$/.test(iso);
  return isNaN(d) ? String(iso).slice(0, 10) : d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(bare ? { timeZone: "UTC" } : {}) });
};
const lrTime = (ts) => { const d = new Date(ts * 1000); return isNaN(d) ? "" : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); };
const lrPlural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
const lrCap = (s) => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);
const lrA = (w) => `${/^[aeiou]/i.test(w || "") ? "an" : "a"} ${w}`;
const lrAnd = (l) => (l.length > 1 ? l.slice(0, -1).join(", ") + " and " + l[l.length - 1] : l[0] || "");
const lrObjList = (objs) => { const l = (objs || []).map((o) => LR_OBJ[o] ? LR_OBJ[o].toLowerCase() : o); return l.length > 1 ? l.slice(0, -1).join(", ") + ", and " + l[l.length - 1] : l[0] || ""; };
// a session returns to its plan, else to the home column, where a note is a
// row of the list (ADR-0119); before, a note's session returned to its pane
const lrOriginRoute = (o) => (o?.kind === "plan" ? { view: "plan", plan: o.path } : { view: "home" });

async function lrPost(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.detail || r.statusText), { status: r.status, data });
  return data;
}

// The server restarted under an open page: the session lived in its memory
// and is gone, and every call on it would fail the same way (2026-10-06:
// Resume, then "couldn't grade: no live session with that id" on each submit).
function lrGone(e) {
  if (!(e?.status === 404 && /no live session/.test(e.message || ""))) return false;
  lrSession = null;
  notify("this session ended when Silica restarted; start it again", "warn");
  lrGo({ view: "home" });
  return true;
}

// --- the grammar every surface is built from (ADR-0061) ------------------------
// A learning state is a mark: open (still to verify, or missed), half (met
// with help, not counted), full (met without help); warn is the one amber
// mark, for caution. `say` labels it for a screen reader; "" when the words
// beside it already say it.
const LR_MARK_SAY = { open: "still to verify", half: "met with help, not counted", full: "met without help", warn: "caution", never: "not tested yet" };
function lrMark(kind, say) {
  const m = lrEl("span", "lr-mk " + kind);
  const label = say === undefined ? LR_MARK_SAY[kind] : say;
  if (label) { m.setAttribute("role", "img"); m.setAttribute("aria-label", label); m.title = label; }
  else m.setAttribute("aria-hidden", "true");
  return m;
}

// A figure is a number that decides, with its label under it.
function lrFig(n, label, sub) {
  const f = lrEl("div", "lr-fig");
  f.append(lrEl("span", "n num", String(n)), lrEl("span", "l", label));
  if (sub) f.appendChild(lrEl("span", "s", sub));
  return f;
}

// A disclosure is one hairline row with its count: what explains is there on
// request, closed, and stays open across a re-render once opened.
function lrDisc(box, title, count, build) {
  const key = `${lrRoute.view}:${title}`;
  const det = lrEl("details", "lr-dr");
  det.open = lrDiscOpen.has(key);
  det.addEventListener("toggle", () => (det.open ? lrDiscOpen.add(key) : lrDiscOpen.delete(key)));
  const sum = lrEl("summary");
  sum.appendChild(lrEl("span", "t", title));
  if (count !== "" && count != null) sum.appendChild(lrEl("span", typeof count === "number" ? "c num" : "c", String(count)));
  det.appendChild(sum);
  const inner = lrEl("div", "in");
  build(inner);
  det.appendChild(inner);
  box.appendChild(det);
  return det;
}

// A key is shown on the button it presses, not in a legend across the bar.
function lrKey(btn, key, shortcut) {
  const k = lrEl("kbd", "lr-k", key);
  k.setAttribute("aria-hidden", "true");
  btn.appendChild(k);
  if (shortcut) btn.setAttribute("aria-keyshortcuts", shortcut);
  return btn;
}

function lrBtn(cls, text, run) {
  const b = lrEl("button", cls, text);
  b.type = "button";
  if (run) b.addEventListener("click", run);
  return b;
}

// A question's markdown, inline only: bold, italics, code, formulas, and a
// cloze's blank drawn as a gap. Code and formulas are parked before the rest
// is escaped, so the only tags in the result are the ones written here and
// the server's MathML.
const lrEsc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function lrMd(el, text) {
  const code = [], math = [];
  el.innerHTML = lrEsc(String(text || "")
    .replace(/`([^`]+)`/g, (_m, c) => `\u0000${code.push(c) - 1}\u0000`)
    .replace(LR_MATH_RE, (_m, d, i) => `\u0001${math.push([d !== undefined, d ?? i]) - 1}\u0001`))
    .replace(/_{3,}/g, '<span class="lr-gap" role="img" aria-label="blank"></span>')
    .replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*\n]+?)\*/g, "<em>$1</em>")
    .replace(/\u0000(\d+)\u0000/g, (_m, i) => `<code>${lrEsc(code[i])}</code>`)
    .replace(/\u0001(\d+)\u0001/g, (_m, i) => lrMathSpan(...math[i]));
  if (math.length) lrTypeset(el);
  return el;
}

// Formulas in Learn's text. A course's notes write them in LaTeX, and a
// question that reads `$y_{i}f\left(\boldsymbol{x}^{(i)}\right)$` is not
// readable (the machine-learning course, 2026-10-23). The server converts them
// the way the chat's are converted (MathML, no renderer in the page), once a
// formula; one that does not convert stays as written.
const LR_MATH = new Map();
const LR_MATH_RE = /\$\$([^$]+?)\$\$|\$(?!\s)([^$\n]+?)(?<!\s)\$/g;
function lrMathSpan(display, tex) {
  const k = (display ? "D" : "I") + tex;
  const done = LR_MATH.get(k);
  if (done) return `<span class="lr-math">${done}</span>`;
  const fence = display ? "$$" : "$";
  return `<span class="lr-math" data-k="${lrEsc(k)}">${lrEsc(fence + tex + fence)}</span>`;
}
async function lrTypeset(root) {
  const want = [...new Set([...root.querySelectorAll(".lr-math[data-k]")].map((e) => e.dataset.k))].filter((k) => !LR_MATH.has(k));
  if (want.length) {
    try {
      const r = await lrPost("/learn/math", { tex: want.map((k) => k.slice(1)), display: want.map((k) => k[0] === "D") });
      want.forEach((k, i) => LR_MATH.set(k, r.mathml[i]));
    } catch { return; }   // the formula stays as written, which is what it was before
  }
  for (const e of root.querySelectorAll(".lr-math[data-k]")) {
    const m = LR_MATH.get(e.dataset.k);
    if (m) { e.innerHTML = m; e.removeAttribute("data-k"); }
  }
}
const lrPlain = (text) => String(text || "").replace(/_{3,}/g, " blank ").replace(/\*\*|`|\*/g, "");

// The note's line with the reference answer marked in it, built from text
// nodes so a line never becomes markup.
function lrMarked(el, text, term) {
  const t = String(text || "");
  if (LR_MATH_RE.test(t)) { LR_MATH_RE.lastIndex = 0; return lrMd(el, t); }   // the formulas over the bolded term
  const k = String(term || "").trim();
  const at = k && k.length <= 80 ? t.toLowerCase().indexOf(k.toLowerCase()) : -1;
  if (at < 0) { el.textContent = t; return el; }
  el.append(t.slice(0, at), lrEl("b", "key", t.slice(at, at + k.length)), t.slice(at + k.length));
  return el;
}

// --- routing ----------------------------------------------------------------
// Three segments (ADR-0081): Next, the queue that says why; Plans, the list
// and the composer; Map, what you know as marks. A plan opens under the
// segment it was picked from, so the left pane does not jump under the reader.
function lrHash(r) {
  if (r.view === "plan") return "#learn/plan/" + encodeURIComponent(r.plan);
  if (r.view === "note") return "#learn/note/" + encodeURIComponent(r.path);
  if (r.view === "plans") return "#learn/plans";
  if (r.view === "map") return "#learn/map" + (r.path ? "/" + encodeURIComponent(r.path) : "");
  if (r.view === "new") return "#learn/new";
  if (r.view === "edit") return "#learn/edit/" + encodeURIComponent(r.plan);
  if (r.view === "session") return "#learn/session/" + r.id;
  if (r.view === "receipt") return "#learn/receipt/" + r.id;
  // the step first: a plan's path has slashes of its own
  if (r.view === "read") return "#learn/read/" + r.step + "/" + encodeURIComponent(r.plan);
  return "#learn";
}
function lrParse(hash) {
  const parts = String(hash || "").replace(/^#/, "").split("/");
  if (parts[0] !== "learn") return null;
  const v = parts[1] || "";
  let arg = parts.slice(2).join("/");
  try { arg = decodeURIComponent(arg); } catch { /* a hand-typed hash: keep it as is */ }
  if (v === "plan" && arg) return { view: "plan", plan: arg };
  // #learn/suggest/ is the hash before ADR-0081; a bookmark of it still lands
  if ((v === "note" || v === "suggest") && arg) return { view: "note", path: arg };
  if (v === "plans") return { view: "plans" };
  if (v === "map") return { view: "map", path: arg };
  if (v === "new") return { view: "new" };
  if (v === "edit" && arg) return { view: "edit", plan: arg };
  if (v === "session" && arg) return { view: "session", id: arg };
  if (v === "receipt" && arg) return { view: "receipt", id: arg };
  if (v === "read" && /^\d+\//.test(arg)) { const k = arg.indexOf("/"); return { view: "read", step: Number(arg.slice(0, k)), plan: arg.slice(k + 1) }; }
  return { view: "home" };
}
const lrSegOf = (r) => { const seg = lrSegRaw(r); if (seg !== "map") lrMapCounted = false; return seg; };
const lrSegRaw = (r) => (["home", "note"].includes(r.view) ? "next" : ["plans", "new", "edit"].includes(r.view) ? "plans" : r.view === "map" ? "map" : lrSeg);
// Navigation is a history entry (refinement §7): Back from Create plan returns
// to the detail it replaced, Forward restores the view.
function lrGo(r, { replace = false } = {}) {
  lrRoute = { view: "home", plan: "", path: "", id: "", ...r };
  lrSeg = lrSegOf(lrRoute);
  const h = lrHash(lrRoute);
  if (location.hash !== h) history[replace ? "replaceState" : "pushState"](null, "", h);
  if (lrRoute.view === "plan") { try { localStorage.setItem(LR_LAST_PLAN, lrRoute.plan); } catch { /* private window */ } }
  lrRender();
}
window.addEventListener("hashchange", () => {
  const r = lrParse(location.hash);
  if (!r) return;
  lrRoute = { view: "home", plan: "", path: "", id: "", ...r };
  lrSeg = lrSegOf(lrRoute);
  if (activeTab === "learn") lrRender();
});

async function loadLearn(force = false) {
  const r = lrParse(location.hash);
  if (r) { lrRoute = { view: "home", plan: "", path: "", id: "", ...r }; lrSeg = lrSegOf(lrRoute); }
  if (lrLoading) return;
  if (lrData && !force) { lrRender(); return; }
  lrLoading = true;
  $("#learn-loading").hidden = false;
  try {
    lrData = await (await fetch("/learn")).json();
    lrPlansError = !!(lrData && lrData.error);
  } catch {
    lrPlansError = true;
    if (!lrData) lrData = { plans: [], next: { plans: [], missed: [], due: [], never: [] }, preview: { items: [] }, total: 0, measured: 0, due: 0 };
  } finally {
    lrLoading = false;
    if (!lrStarting) $("#learn-loading").hidden = true;
  }
  // a live session on the server after a reload: re-attach instead of a 409
  if (lrData.live && (!lrSession || lrSession.id !== lrData.live)) await lrAttach(lrData.live);
  // none on the server (it restarted): a session kept in this page offered
  // Resume and then failed every answer with "no live session" (2026-10-06)
  else if (!lrPlansError && !lrData.live && lrSession) lrSession = null;
  // entering stays on the home: before ADR-0119 it jumped to the last plan
  // or the first row, because the home was a list waiting for a selection
  lrRender();
}

async function lrAttach(id) {
  try {
    const s = await (await fetch("/learn/session?id=" + encodeURIComponent(id))).json();
    if (s.detail || !s.items) return;
    const done = new Set([...(s.done || []), ...(s.skipped || [])]);
    const cur = Math.max(0, s.items.findIndex((it) => !done.has(it.i)));
    const verdicts = {};
    for (const [i, v] of Object.entries(s.verdicts || {})) verdicts[Number(i)] = v;
    const results = {};
    for (const [i, r] of Object.entries(s.results || {})) results[Number(i)] = r;
    const skipped = {};
    for (const i of s.skipped || []) skipped[i] = true;
    const teachT = {};
    for (const [i, t] of Object.entries(s.teach || {})) teachT[Number(i)] = { turn: t.turn, transcript: t.transcript || [] };
    lrSession = { ...s, cur: cur < 0 ? s.items.length - 1 : cur, answers: {}, verdicts, results, skipped, hintsUsed: {}, shown: {}, conf: null, told: false, state: "input", teachT };
    const it = lrSession.items[lrSession.cur];
    if (it && verdicts[it.i]) { lrSession.state = "reveal"; lrSession.conf = verdicts[it.i].conf ?? null; lrSession.told = !!verdicts[it.i].told; }
  } catch { /* the session is gone: the home view reads as usual */ }
}

// --- the bar -----------------------------------------------------------------
// The bar carries the place: on the home the view's purpose, said where a
// first look lands (t4, messages 4 and 6; ADR-0119); elsewhere the crumb, a
// return, End session. ADR-0081's three segments are gone with its panes.
function lrBar({ crumb = [], stamp = "", stampTitle = "", back = null, end = false, say = "" } = {}) {
  const c = $("#learn-crumb");
  c.innerHTML = "";
  if (say) c.appendChild(lrEl("span", "lr-purpose", say));
  crumb.forEach((part, i) => { if (i) c.appendChild(lrEl("span", "sep", "›")); c.appendChild(lrEl("b", null, part)); });
  if (stamp) { const s = lrEl("span", "vault num", stamp); if (stampTitle) s.title = stampTitle; c.appendChild(s); }
  const b = $("#learn-back");
  b.hidden = !back;
  b.onclick = back ? back.run : null;
  if (back) { b.textContent = "← " + back.label; b.title = back.title || ""; }
  const e = $("#learn-end");
  e.hidden = !end;
  e.setAttribute("aria-expanded", lrEndAsk ? "true" : "false");
}
$("#learn-end").addEventListener("click", () => lrEndAsk ? (lrEndAsk = false, lrRender()) : lrEnd());

// The map's stamp names its base (ADR-0081 §4): notes practised of the vault.
function lrMapStamp() {
  const d = lrData || {};
  if (!d.total) return "";
  return d.measured ? `${nfmt(d.measured)} of ${nfmt(d.total)} notes practised` : `${nfmt(d.total)} notes, none practised yet`;
}

function lrRender() {
  const body = $("#learn-body");
  // a list control keeps the focus through a re-render (a detail landing
  // after a fetch re-renders too), so ↑ ↓, Tab and Enter go on working
  const keep = document.activeElement?.dataset?.lrKey;
  // The dialog scrolls in its scrim, which every re-render (each field edit,
  // each preview landing) rebuilds at the top: without this the form jumped
  // back to its first field while it was being filled.
  const dlgScroll = body.querySelector(".lr-scrim")?.scrollTop || 0;
  body.innerHTML = "";
  lrEndAsk = lrEndAsk && lrRoute.view === "session";
  const h = lrHash(lrRoute);
  if (activeTab === "learn" && location.hash !== h) history.replaceState(null, "", h);
  // the chat composer competes with the answer box during a session (refinement §23)
  if (activeTab === "learn") $("#dock").hidden = lrRoute.view === "session";
  if (lrRoute.view === "session") lrRenderSession(body);
  else if (lrRoute.view === "receipt") lrRenderReceipt(body);
  else if (lrRoute.view === "read") lrRenderRead(body);
  else lrRenderHome(body);
  const scrim = body.querySelector(".lr-scrim");
  if (scrim && dlgScroll) scrim.scrollTop = dlgScroll;
  if (keep) body.querySelector(`[data-lr-key="${CSS.escape(keep)}"]`)?.focus({ preventScroll: true });
}

// --- home: one column (ADR-0119) ----------------------------------------------
// What would you like to learn, then the next session as its list, what you
// missed, your plans. A plan, a note and the map are views of their own, one
// pane each with Learn as their way back; the dialog sits over the column.
const LR_PURPOSE = "Test yourself on your notes, then see what you missed.";
const LR_LIST_SHOWN = 5;
const lrAsk = { q: "", opts: [], idx: -1, picks: [] };
// notes taken out of the next session before Start (ADR-0119, Gate)
const lrDropped = new Set();

function lrRenderHome(body) {
  const d = lrData;
  if (!d) return;
  const v = lrRoute.view;
  if (["home", "new", "edit"].includes(v)) {
    lrBar({ say: LR_PURPOSE });
    const col = lrHomeColumn(d);
    body.appendChild(col);
    if (v === "new" || v === "edit") { col.inert = true; body.appendChild(lrDialog()); }
    return;
  }
  const back = { label: "Learn", run: () => lrGo({ view: "home" }) };
  if (v === "map") {
    lrBar({ crumb: ["What you know"], stamp: lrMapStamp(), back });
    const mv = lrEl("section", "mv lr-mv lr-mapv");
    const left = lrEl("div", "mv-l");
    const right = lrEl("div", "mv-r");
    lrRenderMapLeft(left);
    mv.append(left, right);
    body.appendChild(mv);
    if (lrRoute.path) { lrNoteInto(right, lrRoute.path); return; }
    const inv = lrEl("div", "lr-invite");
    inv.append(lrEl("h2", null, "Pick a note to see its history"), lrEl("p", null, "Each mark is a note: dashed not tested yet, open still to verify, half met with help, full met without help."));
    right.appendChild(inv);
    const discs = lrEl("div", "lr-discs end");
    lrReadings(discs, d);
    right.appendChild(discs);
    return;
  }
  const one = lrEl("section", "lr-one");
  body.appendChild(one);
  if (v === "plans") {
    lrBar({ crumb: ["All plans"], back });
    lrRenderPlansLeft(one, null);
    return;
  }
  if (v === "plan") {
    const sel = (d.plans || []).find((p) => p.path === lrRoute.plan);
    lrBar({ crumb: [sel ? sel.name : "Plan"], back });
    if (!sel) { one.appendChild(lrEl("p", "lr-help", "That plan is not in the vault any more.")); return; }
    if (lrPlan && lrPlan.path === sel.path) lrRenderPlanDetail(one, lrPlan);
    else { one.appendChild(lrEl("p", "lr-help", "Reading the plan.")); lrFetchPlan(sel.path); }
    return;
  }
  lrBar({ crumb: [lrStem(lrRoute.path)], back });
  lrNoteInto(one, lrRoute.path);
}

function lrNoteInto(box, path) {
  if (lrNote && lrNote.path === path && lrNote.gone) box.appendChild(lrEl("p", "lr-help", "That note is not in the vault any more."));
  else if (lrNote && lrNote.path === path) lrRenderNoteDetail(box, lrNote);
  else { box.appendChild(lrEl("p", "lr-help", "Reading the note.")); lrFetchNote(path); }
}

function lrHomeColumn(d) {
  const col = lrEl("div", "lr-home");
  if (lrSession) col.appendChild(lrResumeBox());
  col.appendChild(lrEl("h2", "lr-ask-h", "What would you like to learn?"));
  col.appendChild(lrAskBox());
  if (d.recent?.length) {
    // what a student who studies to their own plan wants tested (t4, messages 3, 7)
    const row = lrEl("div", "lr-recent");
    row.appendChild(lrEl("span", "lr-help", "Changed this week"));
    for (const r of d.recent) {
      const c = lrBtn("lr-chip", r.title, () => learnStartSession({ note: r.path, from: "recent" }));
      c.title = `Test me on ${r.title}`;
      c.disabled = !!lrSession;
      row.appendChild(c);
    }
    col.appendChild(row);
  }
  const n = d.next || {};
  if (lrPlansError) col.appendChild(lrPlansErrorBox());
  else if (n.plans?.length) col.appendChild(lrPlansHome(n.plans));
  col.appendChild(lrSessionList(d));
  if (n.missed?.length) col.appendChild(lrMissedHome(n.missed));
  const links = lrEl("div", "lr-links");
  links.append(lrBtn("lr-link", "What you know", () => lrGo({ view: "map" })), lrBtn("lr-link", "All plans", () => lrGo({ view: "plans" })));
  col.appendChild(links);
  return col;
}

// "What would you like to learn?": a note, a folder or a topic from the
// typeahead starts a test at once and writes nothing; notes and folders
// gather, one after another, into one test; a syllabus note opens the dialog
// with it as the Reference; a sentence is read into the dialog.
function lrAskBox() {
  const row = lrEl("div", "lr-ask");
  const wrap = lrEl("div", "lr-ta lr-askin");
  lrAsk.picks.forEach((o, i) => {
    const tok = lrEl("span", "lr-tok");
    tok.append(lrEl("i", null, lrKindWord(o)), o.label);
    const x = lrBtn("lr-x", "×", () => { lrAsk.picks.splice(i, 1); lrRender(); setTimeout(() => $("#lr-ask")?.focus(), 0); });
    x.setAttribute("aria-label", lrAsk.picks.length > 1 ? `remove ${o.label}` : "clear what to learn");
    tok.appendChild(x);
    wrap.appendChild(tok);
  });
  if (lrAsk.picks.every(lrJoins)) {
    const inp = lrEl("input", "lr-field");
    inp.type = "text"; inp.id = "lr-ask"; inp.value = lrAsk.q; inp.autocomplete = "off";
    inp.placeholder = lrAsk.picks.length ? "Add a note or a folder" : "A note, a folder, a topic, or what you are preparing for";
    inp.setAttribute("aria-label", "What would you like to learn?");
    inp.setAttribute("role", "combobox"); inp.setAttribute("aria-autocomplete", "list");
    inp.setAttribute("aria-controls", "lr-ask-opts"); inp.setAttribute("aria-expanded", "false");
    inp.dataset.lrKey = "ask";
    const list = lrEl("ul", "lr-opts");
    list.id = "lr-ask-opts"; list.setAttribute("role", "listbox"); list.hidden = true;
    let t = null;
    inp.addEventListener("input", () => { lrAsk.q = inp.value; lrAskBtn(); clearTimeout(t); t = setTimeout(() => lrAskSearch(inp, list), 120); });
    inp.addEventListener("blur", () => setTimeout(() => { list.hidden = true; inp.setAttribute("aria-expanded", "false"); }, 150));
    inp.addEventListener("keydown", (e) => {
      const n = lrAsk.opts.length;
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && n && !list.hidden) {
        e.preventDefault();
        lrAsk.idx = (lrAsk.idx + (e.key === "ArrowDown" ? 1 : n + (lrAsk.idx < 0 ? 0 : -1))) % n;
        lrAskPaint(inp, list);
      } else if (e.key === "Enter") {
        // a highlighted option is picked; otherwise the words are read as a
        // sentence, so a long request is never hijacked by a near title
        e.preventDefault();
        const o = !list.hidden && lrAsk.idx >= 0 ? lrAsk.opts[lrAsk.idx] : null;
        if (o) lrAskPick(o); else lrAskGo();
      } else if (e.key === "Escape" && !list.hidden) { e.stopPropagation(); list.hidden = true; inp.setAttribute("aria-expanded", "false"); }
    });
    wrap.append(inp, list);
  }
  const form = lrBtn("ghost box", "Create", () => { lrFormReset({ step: "fields" }); lrGo({ view: "new" }); });
  form.title = "set a test or a plan up field by field";
  const go = lrBtn("lr-send", "Test me", lrAskGo);
  go.id = "lr-ask-go";
  row.append(wrap, go, form);
  lrAskBtn(go);
  return row;
}

function lrAskBtn(btn = $("#lr-ask-go")) {
  if (btn) btn.disabled = !!lrSession || !(lrAsk.picks.length || lrAsk.q.trim());
}

async function lrAskSearch(inp, list) {
  const q = inp.value.trim();
  let opts = [];
  // past a title's length the words are a sentence, read on Enter
  if (q && q.length <= 60) {
    try { opts = await (await fetch("/learn/scopes?q=" + encodeURIComponent(q))).json(); } catch { opts = []; }
  }
  if (inp.value.trim() !== q) return;    // a later keystroke owns the list
  // past the first pick, only a note or a folder not picked yet can join it
  const taken = new Set(lrAsk.picks.map((o) => o.kind + ":" + o.target));
  lrAsk.opts = (Array.isArray(opts) ? opts : []).filter((o) => !lrAsk.picks.length || (lrJoins(o) && !taken.has(o.kind + ":" + o.target))).slice(0, 8);
  lrAsk.idx = -1;
  lrAskPaint(inp, list);
}

function lrAskPaint(inp, list) {
  list.innerHTML = "";
  lrAsk.opts.forEach((o, i) => {
    const li = lrEl("li", i === lrAsk.idx ? "on" : null);
    li.id = "lr-ask-opt-" + i;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === lrAsk.idx ? "true" : "false");
    li.append(lrEl("i", null, lrKindWord(o)), lrEl("span", "l", o.label), lrEl("span", "c num", o.kind === "steps" ? `${o.count} lines` : lrPlural(o.count, "note")));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); lrAskPick(o); });
    list.appendChild(li);
  });
  list.hidden = !lrAsk.opts.length;
  inp.setAttribute("aria-expanded", list.hidden ? "false" : "true");
  if (lrAsk.idx >= 0) inp.setAttribute("aria-activedescendant", "lr-ask-opt-" + lrAsk.idx);
  else inp.removeAttribute("aria-activedescendant");
}

function lrAskPick(o) {
  lrAsk.picks = lrJoins(o) && lrAsk.picks.every(lrJoins) ? [...lrAsk.picks, o] : [o];
  Object.assign(lrAsk, { q: "", opts: [], idx: -1 });
  lrRender();
  // the field stays open for the next note or folder; Enter on it empty starts
  setTimeout(() => $(lrJoins(o) ? "#lr-ask" : "#lr-ask-go")?.focus(), 0);
}
// one note picked reads "note"; the plan form's "notes" names a scope of several
const lrKindWord = (o) => (o.kind === "notes" && (o.count || 1) === 1 ? "note" : LR_KIND_WORD[o.kind] || o.kind);
const lrAskClear = () => Object.assign(lrAsk, { q: "", opts: [], idx: -1, picks: [] });

async function lrAskGo() {
  const picks = lrAsk.picks;
  const o = picks.length === 1 ? picks[0] : null;
  const text = lrAsk.q.trim();
  if (!picks.length && !text) { $("#lr-ask")?.focus(); return; }
  if (lrSession) { notify("a session is open; end it or resume it first", "warn"); return; }
  if (o?.kind === "steps") {
    lrAskClear();
    lrFormReset({ step: "fields", ref: { kind: "note", target: o.target, label: o.label } });
    lrGo({ view: "new" });
    lrPreviewScope();
    return;
  }
  if (o?.kind === "notes") { lrAskClear(); learnStartSession({ note: o.path, from: "ask" }); return; }
  if (o?.kind === "folder") { lrAskClear(); learnStartSession({ scope: o.target, from: "ask" }); return; }
  if (picks.length) {
    // a topic, a workspace, or several notes and folders: the notes they
    // resolve to, as a plan's preview reads them
    const s = lrStudyOf(picks);
    let paths = [];
    try { paths = (await lrPost("/learn/plan", { scope: s.scope, target: s.target, preview: true })).paths || []; } catch (e) { notify("couldn't read that: " + e.message); return; }
    if (!paths.length) { notify(`no notes to test on ${s.label}`, "warn"); return; }
    lrAskClear();
    learnStartSession({ scope: paths, from: "ask" });
    return;
  }
  // a sentence opens the dialog, filled by the reading (ADR-0081 §10)
  lrAskClear();
  lrFormReset({ step: "fields", sentence: text });
  lrGo({ view: "new" });
  lrReadSentence();
}

// The next session as its list (ADR-0119, D): the notes it will ask, each
// with its reason. Drop takes one out; Start then sends the rows left as the
// session's scope, and untouched it is the picker's own session.
function lrSessionList(d) {
  const sec = lrEl("section", "lr-hsec lr-next");
  const all = d.session_rows || [];
  if (!all.length) {
    sec.appendChild(lrEl("p", "lr-help", d.total ? "Nothing waiting from earlier sessions: a note needs a footnoted claim or a Relations bullet to be asked about."
      : "Nothing tested yet. Name a note above and test yourself on it; this list grows from there."));
    return sec;
  }
  const rows = all.filter((r) => !lrDropped.has(r.path));
  const head = lrEl("div", "lr-nhead");
  const t = lrEl("div", "t");
  // Before any answer there is no earlier session: a fresh vault of six
  // papers read "8 questions waiting from earlier sessions" (2026-10-26).
  t.appendChild(lrEl("b", null, `${lrPlural(rows.length, "question")} ${d.measured ? "waiting from earlier sessions" : "for your first session"}`));
  t.appendChild(lrEl("span", "lr-help", lrDropped.size ? `about ${_lrMinutes(rows.length)} min · ${lrPlural(lrDropped.size, "note")} dropped`
    : [d.session ? `about ${d.session.minutes} min` : "", d.mix || ""].filter(Boolean).join(" · ")));
  head.appendChild(t);
  if (lrDropped.size) head.appendChild(lrBtn("lr-link", "Put them back", () => { lrDropped.clear(); lrRender(); }));
  const start = lrBtn("ghost box", "Start these", () => lrStartList(rows));
  start.disabled = !rows.length || !!lrSession;
  head.appendChild(start);
  sec.appendChild(head);
  const open = lrMore.has("list");
  for (const r of open ? rows : rows.slice(0, LR_LIST_SHOWN)) sec.appendChild(lrSessionRow(r));
  if (rows.length > LR_LIST_SHOWN) sec.appendChild(lrBtn("lr-more", open ? "Show fewer" : `${rows.length - LR_LIST_SHOWN} more`, () => { open ? lrMore.delete("list") : lrMore.add("list"); lrRender(); }));
  return sec;
}

function lrStartList(rows) {
  if (!lrDropped.size) { learnStartSession({ from: "primary" }); return; }
  const paths = [...new Set(rows.flatMap((r) => r.pair || [r.path]))];
  lrDropped.clear();
  learnStartSession({ scope: paths, n: rows.length, from: "edited" });
}

function lrSessionRow(r, { drop = true } = {}) {
  const row = lrEl("div", "lr-sg lr-srow");
  const pick = lrBtn("pick", undefined, () => lrGo({ view: "note", path: r.path }));
  pick.dataset.lrRow = pick.dataset.lrKey = "note:" + r.path;
  pick.setAttribute("aria-pressed", "false");
  const t = lrEl("span", "t");
  t.append(lrMark(r.mark || "open", ""), r.title);
  pick.append(t, lrEl("span", "r", r.why));
  const act = lrEl("span", "act");
  const ex = lrBtn("lr-link", "Explain it", () => learnExplain(r.path));
  ex.setAttribute("aria-label", `Explain ${r.title} in your own words`);
  ex.disabled = !!lrSession || !!r.pair;
  const dropB = lrBtn("lr-link", "Drop", () => { lrDropped.add(r.path); lrRender(); });
  dropB.setAttribute("aria-label", `Drop ${r.title} from the next session`);
  act.appendChild(ex);
  if (drop) act.appendChild(dropB);
  row.append(pick, act);
  return row;
}

// Missed: a row per note with its reason, as the session's list, and one
// action for all of them in the head (three reasons on one line did not read)
function lrMissedHome(missed) {
  const sec = lrEl("section", "lr-hsec");
  const head = lrEl("div", "lr-nhead");
  const t = lrEl("div", "t");
  const h = lrEl("b", null, "Missed");
  h.appendChild(lrEl("span", "cnt num", ` ${missed.length}`));
  t.append(h, lrEl("span", "lr-help", "lately, and not met since"));
  const q = missed.reduce((a, m) => a + (m.questions || 1), 0);
  const go = lrBtn("ghost box", "Ask these again", () => learnStartSession({ scope: missed.map((m) => m.path), n: Math.min(24, q), from: "missed" }));
  go.disabled = !!lrSession;
  head.append(t, go);
  sec.appendChild(head);
  const open = lrMore.has("missed");
  for (const m of open ? missed : missed.slice(0, 3)) sec.appendChild(lrSessionRow({ path: m.path, title: m.title, why: m.why, mark: "open" }, { drop: false }));
  if (missed.length > 3) sec.appendChild(lrBtn("lr-more", open ? "Show fewer" : `${missed.length - 3} more`, () => { open ? lrMore.delete("missed") : lrMore.add("missed"); lrRender(); }));
  return sec;
}

function lrPlansHome(plans) {
  const sec = lrEl("section", "lr-hsec");
  const h = lrEl("h3", "lr-hh", "Your plans");
  h.appendChild(lrEl("span", "cnt num", String(plans.length)));
  sec.appendChild(h);
  for (const p of plans) {
    const row = lrEl("div", "lr-hline");
    const tx = lrEl("div", "tx");
    const name = lrEl("div", "t");
    const open = lrBtn("lr-link", p.name, () => lrGo({ view: "plan", plan: p.path }));
    open.dataset.lrRow = open.dataset.lrKey = "plan:" + p.path;
    name.appendChild(open);
    if (p.by) name.appendChild(lrEl("span", "lr-help", ` · by ${lrDate(p.by)}`));
    const live = lrSession && lrSession.plan === p.path;
    tx.append(name, lrEl("span", "lr-help", live ? "in the open session" : p.why));
    const go = live ? lrBtn("ghost box", "Resume", () => lrGo({ view: "session", id: lrSession.id }))
      : lrBtn("ghost box", "Continue", () => learnStartSession({ plan: p.path, from: "plan" }));
    if (!live) go.disabled = !!lrSession;
    go.setAttribute("aria-label", `${live ? "Resume" : "Continue"} ${p.name}`);
    row.append(tx, go);
    sec.appendChild(row);
  }
  return sec;
}

function lrPlansErrorBox() {
  // an error is not an empty vault (refinement §4): where the plans would be
  const eb = lrEl("div", "lr-errbox");
  eb.setAttribute("role", "alert");
  const t = lrEl("b");
  t.append(lrMark("warn", ""), "Couldn't read your plans");
  eb.append(t, lrEl("span", null, "The vault answered with an error. Your plans are untouched."), lrBtn("ghost box", "Retry", () => loadLearn(true)));
  return eb;
}

// Plans: today's list and the composer (ADR-0081 §5).
function lrRenderPlansLeft(left, sel) {
  const plans = lrData.plans || [];
  const active = plans.filter((p) => p.status !== "completed");
  const done = plans.filter((p) => p.status === "completed");
  const lh = lrEl("div", "lr-lh", "Your plans");
  lh.appendChild(lrEl("span", "cnt num", lrPlansError ? "–" : String(active.length)));
  left.appendChild(lh);
  if (lrPlansError) {
    const eb = lrEl("div", "lr-errbox");
    eb.setAttribute("role", "alert");
    const t = lrEl("b");
    t.append(lrMark("warn", ""), "Couldn't read your plans");
    eb.append(t, lrEl("span", null, "The vault answered with an error. Your plans are untouched."), lrBtn("ghost box", "Retry", () => loadLearn(true)));
    left.appendChild(eb);
    return;
  }
  const isNew = lrRoute.view === "new";
  const create = lrBtn("lr-create" + (isNew ? " on" : ""), "+ New plan", () => { lrFormReset({ step: "fields" }); lrGo({ view: "new" }); });
  create.setAttribute("aria-pressed", isNew ? "true" : "false");
  left.appendChild(create);
  for (const p of active) left.appendChild(lrPlanRow(p, sel && sel.path === p.path));
  if (done.length) lrDisc(left, "Completed", done.length, (inner) => {
    for (const p of done) inner.appendChild(lrPlanRow(p, sel && sel.path === p.path, true));
  }).classList.add("side");
}

// A plan row keeps the plan's name, its next step and its progress with the
// unit (refinement §17); the rest is in its detail.
function lrPlanRow(p, on, completed = false) {
  const b = lrBtn("lr-pl" + (on ? " on" : ""), undefined, () => lrGo({ view: "plan", plan: p.path }));
  b.dataset.lrRow = b.dataset.lrKey = "plan:" + p.path;
  b.setAttribute("aria-pressed", on ? "true" : "false");
  const t = lrEl("div", "t", p.name);
  if (p.open_session) t.appendChild(lrEl("span", "due", "session open"));
  else if (p.by && !completed) t.appendChild(lrEl("span", "due", `by ${lrDate(p.by)}`));
  b.appendChild(t);
  if (completed) {
    const met = Object.entries(p.met_by_objective || {}).map(([o, n]) => `${LR_OBJ_SHORT[o] || o} met on ${n} of ${p.coverage[1]} notes`);
    b.appendChild(lrEl("div", "n", (p.steps ? `${p.steps_passed || 0} of ${p.steps} steps` : met.join(" · ") || `${p.coverage[1]} notes`) + ` · closed ${lrDate(p.completed)} · ${lrPlural(p.sessions, "session")}`));
    return b;
  }
  if (p.open_session && lrSession && lrSession.plan === p.path) {
    b.appendChild(lrEl("div", "n", `in progress · ${lrSession.items[lrSession.cur]?.title || ""}, question ${lrSession.cur + 1} of ${lrSession.items.length}`));
  } else {
    const why = (lrData.next?.plans || []).find((x) => x.path === p.path)?.why;
    b.appendChild(lrEl("div", "n", why || (p.gaps ? `not started · ${lrPlural(p.gaps, "step has no note in the vault", "steps have no note in the vault")}` : "nothing pending")));
  }
  const meter = lrEl("div", "lr-meter");
  const fill = lrEl("i");
  fill.style.width = (p.progress[1] ? Math.round((p.progress[0] / p.progress[1]) * 100) : 0) + "%";
  meter.appendChild(fill);
  b.appendChild(meter);
  b.appendChild(lrEl("div", "n num", `${p.progress[0]} of ${p.progress[1]} ${p.unit}`));
  return b;
}

// --- the map (ADR-0081 §7) -------------------------------------------------------
// One row per folder, one mark per note; a plan filters it. A cell says its
// reason on hover and selects the note on click: one note, two steps from
// anywhere. No colour carries a state (ADR-0061).
const LR_MAP_SHOWN = 12;
const LR_CELL_SAY = { never: "not tested yet", open: "asked, still open", half: "met with help", full: "met without help" };
function lrRenderMapLeft(left) {
  if (!lrMapCounted) { lrMapCounted = true; lrGateCount("map"); }
  if (!lrMap) { left.appendChild(lrEl("p", "lr-lh-sub", "Reading the map.")); lrFetchMap(); return; }
  const plan = lrMap.plans.find((p) => p.path === lrMapPlan) ? lrMapPlan : "";
  const folders = lrMap.folders.filter((f) => !plan || f.plans.includes(plan))
    .map((f) => (plan ? { ...f, notes: f.notes.filter((n) => n.plans.includes(plan)) } : f));
  const lh = lrEl("div", "lr-lh", "Folders");
  lh.appendChild(lrEl("span", "cnt", plan ? `${folders.length} in the plan` : `${folders.filter((f) => f.practised || f.plans.length).length} with evidence or in a plan`));
  left.appendChild(lh);
  if (lrMap.plans.length) {
    const chips = lrEl("div", "lr-chips lr-mapf");
    chips.setAttribute("role", "group"); chips.setAttribute("aria-label", "filter the map");
    for (const [k, label] of [["", "All folders"], ...lrMap.plans.map((p) => [p.path, p.name])]) {
      const c = lrBtn("lr-chip" + (k === plan ? " on" : ""), label, () => { lrMapPlan = k; lrRender(); });
      c.setAttribute("aria-pressed", k === plan ? "true" : "false");
      chips.appendChild(c);
    }
    left.appendChild(chips);
  }
  const grid = lrEl("div", "lr-map");
  const open = lrMore.has("map") || folders.slice(LR_MAP_SHOWN).some((f) => f.notes.some((n) => n.path === lrRoute.path));
  for (const f of open ? folders : folders.slice(0, LR_MAP_SHOWN)) grid.appendChild(lrMapRow(f, plan));
  left.appendChild(grid);
  if (folders.length > LR_MAP_SHOWN) left.appendChild(lrBtn("lr-more", open ? "Show fewer folders" : `Show ${folders.length - LR_MAP_SHOWN} more folders`, () => { lrMore.has("map") ? lrMore.delete("map") : lrMore.add("map"); lrRender(); }));
  const key = lrEl("div", "lr-mkey lr-mapkey");
  for (const m of ["never", "open", "half", "full"]) { const s = lrEl("span"); s.append(lrEl("i", "lr-cell " + m), LR_CELL_SAY[m]); key.appendChild(s); }
  left.appendChild(key);
}

function lrMapRow(f, plan) {
  const row = lrEl("div", "lr-maprow");
  const head = lrEl("div", "f");
  const practised = f.notes.filter((n) => n.mark !== "never").length;
  head.append(lrEl("span", "fn", f.folder), lrEl("span", "cnt", `${practised} of ${f.notes.length}`));
  // Plan this folder (ADR-0081 §17): on hover and focus, the folder filled in
  const pl = lrBtn("lr-link act", "Plan this folder", () => lrPlanFolder(f.folder + "/"));
  pl.setAttribute("aria-label", `Plan the folder ${f.folder}`);
  if (!plan && f.folder !== "(root)") head.appendChild(pl);
  row.appendChild(head);
  const cells = lrEl("div", "cells");
  f.notes.forEach((n, i) => {
    const on = lrRoute.path === n.path;
    const c = lrBtn("lr-cell " + n.mark + (on ? " on" : ""), undefined, () => lrGo({ view: "map", path: n.path }, { replace: lrRoute.view === "map" && !!lrRoute.path }));
    // one tab stop per folder: ← → walk the cells, so 700 notes are not 700 tabs
    c.tabIndex = on || (i === 0 && !f.notes.some((x) => x.path === lrRoute.path)) ? 0 : -1;
    c.setAttribute("aria-label", `${n.title}, ${n.why}`);
    c.title = `${n.title} · ${n.why}`;
    c.dataset.lrKey = "cell:" + n.path;
    cells.appendChild(c);
  });
  cells.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const all = [...cells.children];
    const i = all.indexOf(document.activeElement);
    const nxt = all[Math.max(0, Math.min(all.length - 1, i + (e.key === "ArrowRight" ? 1 : -1)))];
    if (!nxt) return;
    e.preventDefault();
    all.forEach((x) => (x.tabIndex = -1));
    nxt.tabIndex = 0;
    nxt.focus();
  });
  row.appendChild(cells);
  return row;
}

async function lrFetchMap() {
  if (lrMapLoading) return;
  lrMapLoading = true;
  try { lrMap = await (await fetch("/learn/map")).json(); } catch { notify("couldn't read the map"); lrMap = { folders: [], plans: [], total: 0, practised: 0 }; }
  lrMapLoading = false;
  if (lrSeg === "map") lrRender();
}

// A session left open: one strip on top of the detail it belongs to, with
// Resume as the view's only primary (ADR-0061 §12).
function lrResumeBox() {
  const s = lrSession;
  const box = lrEl("div", "lr-resume");
  box.setAttribute("role", "status");
  const t = lrEl("div", "t");
  t.appendChild(lrEl("b", null, `Session in progress · ${s.items[s.cur]?.title || ""}, question ${s.cur + 1} of ${s.items.length}`));
  const rec = Object.keys(s.results).length;
  const draft = s.answers[s.items[s.cur]?.i];
  t.appendChild(lrEl("span", null, `${lrPlural(rec, "answer")} recorded` + (draft ? ` · your draft for question ${s.cur + 1} is kept` : "") + (s.started ? ` · started ${lrTime(s.started)}` : "")));
  box.append(lrEl("span", "dot"), t,
    lrBtn("lr-q", "End session", () => { lrGo({ view: "session", id: s.id }); lrEnd(); }),
    lrBtn("lr-send", "Resume", () => lrGo({ view: "session", id: s.id })));
  return box;
}

async function lrFetchPlan(path) {
  try {
    const p = await (await fetch("/learn/plan?path=" + encodeURIComponent(path))).json();
    if (p.detail) throw new Error(p.detail);
    lrPlan = p;
  } catch (e) { notify("couldn't read that plan"); lrPlan = null; return; }
  if ((lrRoute.view === "plan" || lrRoute.view === "edit") && lrRoute.plan === path) lrRender();
}

async function lrFetchNote(path) {
  try {
    const n = await (await fetch("/learn/note?path=" + encodeURIComponent(path))).json();
    if (n.detail) throw new Error(n.detail);
    lrNote = n;
  } catch (e) {
    // renamed or deleted since it was listed: the pane says so and stops waiting
    lrNote = { path, gone: true };
  }
  if ((lrRoute.view === "note" || lrRoute.view === "map") && lrRoute.path === path) lrRender();
}

// The head answers "what now?": an eyebrow, the title, the size of the next
// session, Open, the primary action (ADR-0061 §1).
function lrHead(eyebrow, title, meta, buttons) {
  const head = lrEl("div", "lr-phead");
  head.appendChild(lrEl("span", "eyebrow", eyebrow));
  const row = lrEl("div", "phead-row");
  row.appendChild(lrEl("h2", null, title));
  if (meta) row.appendChild(lrEl("span", "meta", meta));
  row.append(...buttons.filter(Boolean));
  head.appendChild(row);
  return head;
}

// --- the plan detail -----------------------------------------------------------
function lrRenderPlanDetail(right, p) {
  const r = p.readings;
  const live = lrSession && lrSession.plan === p.path;
  const closed = p.status === "completed";
  if (live) right.appendChild(lrResumeBox());
  const n = p.pace?.session || 8;
  const open = lrBtn("ghost box", "Open plan note", () => openNote(p.path));
  const edit = lrBtn("ghost box", "Edit plan", () => lrGo({ view: "edit", plan: p.path }));
  const go = !live && !closed ? lrBtn(p.complete ? "ghost box" : "lr-send", "Continue plan", () => learnStartSession({ plan: p.path, n })) : null;
  const done = p.complete && !closed ? lrBtn(live ? "ghost box" : "lr-send", "Mark completed", async () => { await lrPost("/learn/plan/close", { path: p.path }); lrData = null; lrPlan = null; loadLearn(true); }) : null;
  const reopen = closed ? lrBtn("ghost box", "Reopen", async () => { await lrPost("/learn/plan/close", { path: p.path, reopen: true }); lrData = null; lrPlan = null; loadLearn(true); }) : null;
  const head = lrHead(closed ? `Plan · completed ${lrDate(p.completed)}` : "Plan", p.name,
    live ? "a new session waits for this one to end" : !closed ? `${lrPlural(n, "question")} · about ${Math.max(1, Math.round(n * 1.3))} min` : "",
    [open, edit, reopen, go, done]);
  const goal = lrEl("p", "goal");
  goal.append(`Across ${p.scope === "workspace" ? "the workspace " : ""}${lrTargetShown(p)}, ${lrPlural(r.coverage[1], "note")}: `);
  p.objectives.forEach((o, i) => { goal.appendChild(lrEl("b", null, LR_OBJ[o]?.toLowerCase() || o)); goal.append(i < p.objectives.length - 2 ? ", " : i === p.objectives.length - 2 ? ", and " : "."); });
  head.appendChild(goal);
  const next = lrEl("p", "next");
  next.appendChild(lrEl("span", "k", "Next"));
  if (p.next) {
    next.appendChild(lrEl("span", "v", `${p.next.step ? `step ${p.next.step}, ` : ""}${p.next.title}`));
    const bits = [];
    if (p.objectives.length > 1) bits.push(LR_OBJ_SHORT[p.next.objective] || p.next.objective);
    if (live) bits.push("in the open session");
    if (bits.length) next.appendChild(lrEl("span", "m", " · " + bits.join(" · ")));
  } else next.appendChild(lrEl("span", "v", p.complete ? "every note met every objective" : p.gaps?.length ? "not started" : "nothing pending"));
  head.appendChild(next);
  if (p.revisit?.length) {
    const q = lrEl("p", "next");
    q.append(lrEl("span", "k", "Queued"), lrEl("span", "v", p.revisit.map(lrStem).join(", ")), lrEl("span", "m", " · queued by you, they come first"));
    head.appendChild(q);
  }
  // exceptions are written; a default is not (ADR-0061 §3)
  if (p.pace?.say) head.appendChild(lrEl("p", "lr-help", p.pace.say));
  if (p.today?.say) {
    // today's row of the calendar, and how far the first step not done lies
    // behind the calendar drawn when the plan was made (ADR-0131 amendment)
    const t = lrEl("p", "lr-help" + ((p.today.behind || 0) >= 2 ? " warn" : ""));
    if ((p.today.behind || 0) >= 2) t.appendChild(lrMark("warn", ""));
    t.append(p.today.say);
    head.appendChild(t);
  }
  if (p.budget?.say) head.appendChild(lrEl("p", "lr-help", p.budget.say));
  if (p.cycles?.length) head.appendChild(lrEl("p", "lr-help", `Prerequisite cycle among: ${p.cycles[0].map(lrStem).join(" → ")}. Reported, not linearized.`));
  if (p.gaps?.length) head.appendChild(lrEl("p", "lr-help", `${lrPlural(p.gaps.length, "step")} not in the vault: ${p.gaps.join("; ")}.`));
  right.appendChild(head);
  // Lately: the plan's misses as rows, a misconception joined to its row
  // (ADR-0081 §5); Observations folded in here
  if (p.lately?.length) {
    const sec = lrEl("section", "lr-sec");
    const h = lrEl("h3", null, "Lately");
    h.appendChild(lrEl("span", "c", String(p.lately.length)));
    sec.appendChild(h);
    for (const x of p.lately) sec.appendChild(lrPlanNoteRow(x, p, x.misconception ? `${x.why} · noted: ${x.misconception}` : x.why, "Ask again"));
    const q = p.lately.reduce((a, x) => a + (x.questions || 1), 0);
    const again = lrBtn("ghost box", `Ask these again · ${lrPlural(q, "question")}`, () => learnStartSession({ scope: p.lately.map((x) => x.path), n: q }));
    again.disabled = !!lrSession;
    const row = lrEl("div", "lr-again");
    row.appendChild(again);
    sec.appendChild(row);
    right.appendChild(sec);
  }
  // the facts a decision needs, as figures, once one is not zero; before that
  // one sentence, because 0 / 0 read as failure early in a plan (ADR-0081 §5)
  if (!r.due && !r.all_met) {
    const met = p.objectives.map((o) => LR_OBJ_SHORT[o] || o);
    right.appendChild(lrEl("p", "lr-lead", `${r.coverage[0]} of ${lrPlural(r.coverage[1], "note")} practised; none has met ${lrAnd(met)} without help yet.`));
  } else {
    const figs = lrEl("div", "lr-figs");
    figs.append(lrFig(r.not_measured, "notes never practised"), lrFig(r.due, "to review in this plan"), lrFig(r.all_met, `of ${r.coverage[1]} met every objective`));
    right.appendChild(figs);
  }
  right.appendChild(lrPlanTree(p));
  // After this: three in-scope rows with reasons, the plan's own "what next"
  if (p.after?.length) {
    const sec = lrEl("section", "lr-sec");
    sec.appendChild(lrEl("h3", null, "After this"));
    for (const x of p.after) sec.appendChild(lrPlanNoteRow(x, p, x.why, "Practice"));
    right.appendChild(sec);
  }
  // the readings and every explanation, closed (refinement §20, ADR-0061 §6)
  const discs = lrEl("div", "lr-discs");
  const cards = (p.cards || []).filter((c) => c.source !== "gap");
  // rewording the cards on the notes is the plan's action too (ADR-0081 §17)
  if (p.cards?.length) lrDisc(discs, "Objectives", cards.length, (box) => {
    for (const c of p.cards) box.appendChild(lrObjRow(c));
    const rw = lrBtn("ghost box", "Reword on the notes", async () => {
      rw.disabled = true; rw.textContent = "Rewording…";
      try {
        // no `refused`: the plan keeps the matches the reader refused
        const target = p.scope === "notes" ? p.notes : p.target;
        await lrPost("/learn/plan", { path: p.path, name: p.name, scope: p.scope, target, restrict: p.restrict || "", by: p.by || null, objectives: p.objectives_chosen ? p.objectives : [] });
        lrPlan = null; lrFetchPlan(p.path);
      } catch (e) { notify("not reworded: " + e.message); rw.disabled = false; rw.textContent = "Reword on the notes"; }
    });
    box.appendChild(rw);
  });
  lrDisc(discs, "Sessions", p.sessions.length, (box) => {
    if (!p.sessions.length) { box.appendChild(lrEl("p", "lr-help", "No session yet.")); return; }
    const tbl = lrEl("table", "chart tight lr-tbl");
    const thd = lrEl("thead"); const h2 = lrEl("tr");
    for (const [l, c] of [["date", null], ["questions", "num"], ["objectives met", null], ["", null]]) h2.appendChild(lrEl("th", c, l));
    thd.appendChild(h2); tbl.appendChild(thd);
    const tb2 = lrEl("tbody");
    for (const s of [...p.sessions].reverse()) {
      const tr = lrEl("tr");
      tr.appendChild(lrEl("th", null, lrDate(s.date)));
      tr.appendChild(lrEl("td", "num", String(s.items)));
      tr.appendChild(lrEl("td", null, s.met ? s.met.replace(/,/g, " ·") : s.line.replace(/ · session: [0-9a-f]+$/, "").split(" · ").slice(2).join(" · ")));
      const td = lrEl("td");
      if (s.session) td.appendChild(lrBtn("lr-link", "receipt", () => lrOpenReceipt(s.session)));
      tr.appendChild(td);
      tb2.appendChild(tr);
    }
    tbl.appendChild(tb2);
    box.appendChild(tbl);
  });
  const shaped = Object.values(r.depth_profile || {}).reduce((a, b) => a + b, 0);
  lrDisc(discs, "Answer structure", shaped ? lrPlural(shaped, "note") : "no answers yet", (box) => {
    const total = r.coverage[1] || 1;
    const stack = lrEl("div", "lr-stack");
    for (const lv of [1, 2, 3, 4]) {
      const k = r.depth_profile[String(lv)] || 0;
      if (!k) continue;
      const i = lrEl("i", "r" + lv);
      i.style.width = Math.round((k / total) * 100) + "%";
      i.title = `${LR_LEVEL[lv]}: ${k}`;
      stack.appendChild(i);
    }
    box.appendChild(stack);
    const legend = lrEl("div", "lr-legend");
    for (const [lv, name] of Object.entries(LR_LEVEL)) { const s = lrEl("span"); s.appendChild(lrEl("i", "r" + lv)); s.append(`${name} · ${r.depth_profile[String(lv)] || 0}`); legend.appendChild(s); }
    box.appendChild(legend);
    box.appendChild(lrEl("p", "lr-help", `The shape of the answers observed, per note. Relations answered: ${p.integration.label}.`));
  });
  lrReadings(discs, lrData || {}, { plan: p });
  lrDisc(discs, "About this plan", "", (box) => {
    const dl = lrEl("dl", "lr-kv");
    const kv = (k, v) => { if (v) dl.append(lrEl("dt", null, k), lrEl("dd", null, v)); };
    kv("studies", `${LR_SCOPE[p.scope] || p.scope}, ${lrTargetShown(p)}, ${lrPlural(r.coverage[1], "note")}`);
    kv(p.objectives.length > 1 ? "objectives" : "objective", `${lrObjList(p.objectives)}, ${p.objectives_chosen ? "chosen by you" : "proposed by Silica"}`);
    kv("created", lrDate(p.created));
    kv("target date", p.by ? lrDate(p.by) : "");
    kv("completed", closed ? lrDate(p.completed) : "");
    kv("note", p.path);
    box.appendChild(dl);
  });
  lrDisc(discs, "How this is measured", "", (box) => {
    box.appendChild(lrMarkKey());
    const say = (k, t) => { const para = lrEl("p"); para.append(lrEl("b", null, k), " " + t); box.appendChild(para); };
    say("To review", "counts notes whose estimated retention is below the retention target.");
    say("Met every objective", "counts notes where every objective was met without help, since the plan was written. An objective is met on a note when its criteria were satisfied without help on a dated question; meeting one objective says nothing about another, and an assisted answer never counts as \"without help\".");
    say(p.steps?.length ? "Steps" : "Material", "has one row per child, expandable to its notes; a node counts distinct notes, so children need not sum to the parent.");
    if (p.cards?.length) say("Objectives", "say what the plan asks you to show, per step; the notes' own key points say what to cover.");
    say("Sessions", `are each a line under ## Sessions in ${p.path}.`);
    say("Answer structure", "reads the shape of the answers observed (SOLO: unistructural, multistructural, relational, extended abstract); it orients the feedback and is not an objective you chose.");
  });
  right.appendChild(discs);
}

// A note inside the plan's detail: its title selects it in Next, its reason
// is the ledger's, its action shows on hover and focus.
function lrPlanNoteRow(x, p, why, verb) {
  const row = lrEl("div", "lr-sg lr-inrow");
  const pick = lrBtn("pick", undefined, () => lrGo({ view: "note", path: x.path }));
  pick.append(lrEl("span", "t", x.title), lrEl("span", "r", why));
  const act = lrBtn("act", verb, () => learnStartSession({ note: x.path, plan: p.path }));
  act.setAttribute("aria-label", `${verb} ${x.title}`);
  row.append(pick, act);
  return row;
}

// The key of the marks, with the measurements it explains.
function lrMarkKey() {
  const k = lrEl("div", "lr-mkey");
  for (const [m, say] of [["open", "still to verify, or missed"], ["half", "met with help, not counted"], ["full", "met without help"]]) {
    const s = lrEl("span");
    s.append(lrMark(m, ""), say);
    k.appendChild(s);
  }
  return k;
}

// The tree: topic; practised, a bar and n/m; one column per objective with a
// mark and the count of notes met (ADR-0061 §4). A leaf's Practice shows on
// hover and focus.
function lrPlanTree(p) {
  const r = p.readings;
  const sec = lrEl("section", "lr-sec");
  sec.appendChild(lrEl("h3", null, p.steps?.length ? "Steps" : "Material"));
  const t = lrEl("table", "lr-tree");
  const th = lrEl("thead");
  const hr = lrEl("tr");
  hr.append(lrEl("th", null, "topic"), lrEl("th", "pr", "practised"));
  for (const o of p.objectives) hr.appendChild(lrEl("th", "ob", `${LR_OBJ_SHORT[o] || o} met`));
  th.appendChild(hr);
  t.appendChild(th);
  const tb = lrEl("tbody");
  const root = p.target.replace(/\/$/, "");
  for (const node of p.tree) {
    const leaves = node.leaves || [];
    const rd = node.readings || { coverage: [0, leaves.length], met_by_objective: {} };
    const label = p.scope === "folder" && node.node.startsWith(root) ? (node.node.slice(root.length).replace(/^\//, "") || p.target) : node.node;
    // the step whose label is the node's, before any whose text merely starts
    // with it: "Reti neurali superficiali" took "Reti neurali superficiali
    // feedforward", the step before it in the owner's plan (2026-10-26)
    const step = p.steps?.length ? (p.steps.find((s) => s.text === node.node || s.text.startsWith(node.node + ":"))
      || p.steps.find((s) => s.text.startsWith(node.node) || s.text.includes(node.node))) : null;
    const tr = lrEl("tr", "node");
    const th0 = lrEl("th");
    th0.scope = "row";
    const exp = leaves.length ? lrBtn("lr-exp") : lrEl("span", "lr-exp");
    exp.append(lrEl("span", "chev"), label);
    th0.appendChild(exp);
    // the step read as one explanation before its practice (ADR-0132)
    const stepIdx = step && leaves.length ? p.steps.indexOf(step) : -1;
    if (stepIdx >= 0) {
      const rd0 = lrBtn("lr-link act lr-readbtn", "Read", () => lrGo({ view: "read", plan: p.path, step: stepIdx }));
      rd0.setAttribute("aria-label", `Read ${label}`);
      th0.appendChild(rd0);
    }
    const ex = [];
    if (node.gap) ex.push("not in the vault");
    if (step?.passed) ex.push("passed");
    if (rd.due) ex.push(`${rd.due} to review`);
    if (ex.length) th0.appendChild(lrEl("span", "m", ex.join(" · ")));
    tr.appendChild(th0);
    const pr = lrEl("td", "pr");
    if (!node.gap) {
      const meter = lrEl("span", "lr-meter");
      const fill = lrEl("i");
      fill.style.width = (rd.coverage[1] ? Math.round((rd.coverage[0] / rd.coverage[1]) * 100) : 0) + "%";
      meter.appendChild(fill);
      pr.append(meter, lrEl("span", "num" + (rd.coverage[0] ? "" : " zero"), `${rd.coverage[0]}/${rd.coverage[1]}`));
    }
    tr.appendChild(pr);
    for (const o of p.objectives) {
      const td = lrEl("td", "ob");
      if (!node.gap) {
        const k = rd.met_by_objective?.[o] || 0;
        const all = k && k === rd.coverage[1];
        td.append(lrMark(all ? "full" : "open", all ? "every note met" : `${k} of ${rd.coverage[1]} notes met`), lrEl("span", "num" + (k ? "" : " zero"), String(k)));
      }
      tr.appendChild(td);
    }
    tb.appendChild(tr);
    const leafRows = [];
    for (const leaf of leaves) {
      const lv = r.leaves[leaf] || {};
      const ltr = lrEl("tr", "leaf");
      ltr.hidden = true;
      const lth = lrEl("th");
      lth.scope = "row";
      lth.append(lrBtn("lr-link", lrStem(leaf), () => openNote(leaf)),
        lrBtn("lr-link act", "Practice", () => learnStartSession({ note: leaf, plan: p.path })));
      lth.lastChild.setAttribute("aria-label", `Practice ${lrStem(leaf)}`);
      ltr.appendChild(lth);
      // "to review" is the node's count, on the plan's own reading (measured and
      // under the target); a leaf's raw `due` is true for a note never practised
      ltr.appendChild(lrEl("td", "pr m", lv.attempts ? "practised" : ""));
      for (const o of p.objectives) {
        const x = lv.objectives?.[o] || {};
        const td = lrEl("td", "ob");
        if (x.met) td.appendChild(lrMark("full", `met without help ${lrDate(x.met)}`));
        else if (x.assisted) td.append(lrMark("half", ""), lrEl("span", "m", `with help ${lrDate(x.assisted)}`));
        else if (x.missed) td.append(lrMark("open", ""), lrEl("span", "m", `missed ${lrDate(x.missed)}`));
        else td.appendChild(lrMark("open"));
        ltr.appendChild(td);
      }
      leafRows.push(ltr);
      tb.appendChild(ltr);
    }
    if (leaves.length) {
      // the step as a deck or as cards, the Studio row on a selection of notes.
      // shortcut: the first LR_STEP_NOTES notes; /slides hands over 40 checked
      // points (learn.artifacts.STUDY_POINTS_MAX), about that many notes' worth, so a longer
      // step's tail would add nothing. Raise the two together.
      const str = lrEl("tr", "leaf lr-step-tools");
      str.hidden = true;
      const td = lrEl("td");
      td.colSpan = 2 + p.objectives.length;
      const q = leaves.slice(0, LR_STEP_NOTES).map(shellQuote).join(" ");
      td.append(lrEl("span", "m", "This step as"),
        lrBtn("lr-link act", "slides", () => prefillChat(`/slides ${q}`)),
        lrBtn("lr-link act", "flashcards", () => prefillChat(`/flashcards ${q} --n=10`)));
      str.appendChild(td);
      leafRows.push(str);
      tb.appendChild(str);
      exp.setAttribute("aria-expanded", "false");
      exp.addEventListener("click", () => {
        const open = exp.getAttribute("aria-expanded") !== "true";
        exp.setAttribute("aria-expanded", String(open));
        leafRows.forEach((x) => (x.hidden = !open));
      });
    }
  }
  t.appendChild(tb);
  sec.appendChild(t);
  return sec;
}

// An objective is a row, its node and its sentence; how it is checked is
// closed under it; edit and remove show on hover and focus (ADR-0061 §16).
function lrObjRow(c, { edit = null, remove = null } = {}) {
  const row = lrEl("div", "lr-ob" + (c.source === "gap" ? " gap" : ""));
  if (c.source === "gap") {
    row.append(lrEl("span", "chev none"), lrEl("span", "n", c.node), lrEl("span", "i"), lrEl("span", "t", "no material in the vault, no objective proposed"));
    return row;
  }
  const det = lrEl("details");
  const sum = lrEl("summary");
  const t = lrEl("span", "t", c.title);
  // the 9B opens 6 in 21 titles with another intention's verb (ADR-0081 §16)
  if (c.reads_as) { const a = lrEl("em", "amb"); a.append(lrMark("warn", ""), `reads as ${c.reads_as}`); t.appendChild(a); }
  sum.append(lrEl("span", "chev"), lrEl("span", "n", c.node), lrEl("span", "i", LR_OBJ_SHORT[c.intention] || ""), t);
  det.appendChild(sum);
  const dl = lrEl("dl", "lr-kv");
  const kv = (k, v) => { if (v) dl.append(lrEl("dt", null, k), lrEl("dd", null, v)); };
  kv("intention", `${LR_OBJ[c.intention]?.toLowerCase() || c.intention}, ${c.source === "chosen" ? "chosen by you" : "proposed by Silica"}`);
  kv("conditions", c.conditions);
  kv("criterion", c.criterion);
  det.appendChild(dl);
  row.appendChild(det);
  if (edit || remove) {
    const ed = lrEl("span", "ed");
    if (edit) ed.appendChild(lrBtn("lr-link", c.reads_as ? "reword" : "edit", edit));
    if (remove) ed.appendChild(lrBtn("lr-link", "remove", remove));
    row.appendChild(ed);
  }
  return row;
}

// --- a note: why it is up, and what you already answered (ADR-0081 §3) ------------
// One grammar for every row of Next and every cell of the Map: the head with
// The note in other forms, NotebookLM's Studio row (owner's comparison,
// 2026-10-23): each button writes its command into the chat, which runs it on
// send, so nothing is generated unasked. Below the practice, because practice
// is what counts toward a plan (research report difficulty-detection, §8: the
// aids raise felt learning faster than learning). A path where the command
// resolves one, so a title two folders share makes this note's deck.
const LR_TOOLS = [
  ["Flashcards", (p) => `/flashcards ${shellQuote(p)} --n=8`],
  ["Slides", (p) => `/slides ${shellQuote(p)}`],
  ["Mind map", (p) => `/map ${shellQuote(p)}`],
  ["Table", (_p, t) => `/schematize ${shellQuote(t)}`],
  ["Diagram", (_p, t) => `/diagram ${shellQuote(t)}`],
  ["Explanation", (_p, t) => `/explain ${shellQuote(t)}`],
];
const LR_STEP_NOTES = 10;
function lrStudyTools(path, title, reading = null) {
  const sec = lrEl("section", "lr-sec lr-tools");
  sec.appendChild(lrEl("h3", null, "Study tools"));
  const row = lrEl("div", "lr-tools-row");
  for (const [name, cmd] of LR_TOOLS) {
    // a note of a plan step is explained by that step's reading, in Learn (ADR-0132)
    const inStep = name === "Explanation" && reading;
    const b = lrBtn("ghost box", name, inStep
      ? () => lrGo({ view: "read", plan: reading.plan, step: reading.step, path })
      : () => prefillChat(cmd(path, title)));
    b.title = inStep ? "opens the reading of this note's step" : "writes the command in the chat; it runs when you send it";
    row.appendChild(b);
  }
  sec.appendChild(row);
  return sec;
}

// Open note and the action, the reason as a sentence, History, what a session
// covers, how this is measured. It retires the suggestion detail (§6).
const LR_GROUP_EYEBROW = { missed: "Missed", due: "Fading", never: "Not tested yet" };
const LR_HIST_MARK = { met: "full", assisted: "half", missed: "open" };
function lrRenderNoteDetail(right, n) {
  const inLive = lrSession && lrSession.note === n.path;
  if (inLive) right.appendChild(lrResumeBox());
  const verb = n.group === "missed" ? "Ask again" : "Test me";
  const go = inLive ? null : lrBtn("lr-send", verb, () => learnStartSession({ note: n.path }));
  if (go) go.disabled = !n.grounded || !!lrSession;
  const explain = inLive ? null : lrBtn("ghost box", "Explain it", () => learnExplain(n.path));
  if (explain) { explain.disabled = !n.grounded || !!lrSession; explain.title = "explain it in your own words; a student who has not read it asks four questions"; }
  const eyebrow = lrRoute.view === "map" ? n.path : LR_GROUP_EYEBROW[n.group] || "Note";
  const head = lrHead(eyebrow + (n.also_in?.length ? ` · in ${n.also_in.map((p) => p.name).join(", ")}` : ""), n.title,
    inLive ? "" : n.questions ? `${lrPlural(n.questions, "question")} · about ${n.minutes} min` : "no grounded line to ask about",
    [lrBtn("ghost box", "Open note", () => openNote(n.path)), explain, go]);
  // why it is up, in words: the ledger's own fields, never a model's (ADR-0081 §3)
  if (n.sentence) head.appendChild(lrEl("p", "lr-reason", n.sentence));
  if (lrSession && !inLive) head.appendChild(lrEl("p", "lr-help", "A session is open; end it or resume it before starting this one."));
  right.appendChild(head);
  if (!inLive) right.appendChild(lrStudyTools(n.path, n.title, n.reading));
  if (n.history?.length) {
    const sec = lrEl("section", "lr-sec");
    const h = lrEl("h3", null, "History");
    const days = [...new Set(n.history.map((x) => x.day))];
    h.appendChild(lrEl("span", "c", `${lrPlural(n.history.length, "answer")}` + (days.length === 1 ? ` · ${days[0]}` : "")));
    sec.appendChild(h);
    const t = lrEl("table", "lr-hist");
    const tb = lrEl("tbody");
    for (const x of n.history) {
      const tr = lrEl("tr");
      tr.appendChild(lrEl("td", "d", x.day));
      const q = lrEl("td", "q");
      q.append(lrMark(LR_HIST_MARK[x.outcome] || "open", ""));
      lrMd(q.appendChild(lrEl("span")), x.q || "(question not kept)");
      tr.appendChild(q);
      const bits = [x.word, x.conf != null ? `said ${x.conf}` : "", x.rating, x.line ? `line ${x.line}` : ""].filter(Boolean);
      tr.appendChild(lrEl("td", "rt", bits.join(" · ")));
      tb.appendChild(tr);
    }
    t.appendChild(tb);
    sec.appendChild(t);
    right.appendChild(sec);
  }
  const sec = lrEl("section", "lr-sec");
  const asked = n.points.filter((pt) => pt.objective);
  const sh = lrEl("h3", null, "What a session covers");
  if (n.points.length) sh.appendChild(lrEl("span", "c", `${lrPlural(n.questions, "question")} on ${lrPlural(n.points.filter((p) => p.i).length, "key point")}`));
  sec.appendChild(sh);
  sec.appendChild(lrEl("p", "lr-help", n.points.length
    ? `Found in the note's own text${n.leaves?.length ? `; it cites ${n.leaves.join(", ")}` : n.self_grounded ? "; no source cited, the questions rest on the note's lines" : ""}. Questions and answers stay hidden until you answer.`
    : "No key point found: the note has no footnoted claim, no Relations bullet and no prose line long enough to check."));
  // key points grouped by heading, named once (ADR-0061 §9)
  let heading = null;
  let grid = null;
  for (const pt of asked) {
    if (pt.heading !== heading || !grid) {
      heading = pt.heading;
      if (heading) sec.appendChild(lrEl("span", "lr-grp", heading));
      grid = lrEl("div", "lr-points");
      sec.appendChild(grid);
    }
    grid.append(lrEl("span", "i num", pt.i ? String(pt.i) : "+"), lrEl("span", "p", lrCap(pt.kind)), lrEl("span", "h", pt.line ? `line ${pt.line}` : ""),
      lrEl("span", "k", (LR_OBJ_SHORT[pt.objective] || pt.objective) + (pt.questions > 1 ? ` · ${pt.questions} questions` : "")));
  }
  const rest = n.points.filter((pt) => !pt.objective);
  if (rest.length) {
    const kinds = [...new Set(rest.map((pt) => pt.kind))];
    const what = kinds.length === 1 ? lrPlural(rest.length, kinds[0]) : lrPlural(rest.length, "point");
    lrDisc(sec, `${what.replace(/^\d+/, (k) => `${k} more`)}, not asked this time`, "", (box) => {
      for (const k of kinds) {
        const lines = rest.filter((pt) => pt.kind === k && pt.line).map((pt) => pt.line);
        box.appendChild(lrEl("p", "lr-help", `${lrCap(k)}${lines.length ? `, ${lines.length > 1 ? "lines" : "line"} ${lrAnd(lines.map(String))}` : ""}.`));
      }
    });
  }
  right.appendChild(sec);
  const discs = lrEl("div", "lr-discs");
  lrDisc(discs, "How this is measured", "", (box) => {
    box.appendChild(lrMarkKey());
    const objs = lrObjList(n.objectives);
    if (typeof n.R === "number") box.appendChild(lrEl("p", null, `Estimated retention ${n.R.toFixed(2)}${typeof n.S === "number" ? `, holding about ${lrDays(n.S)}` : ""}; the schedule asks for a note below 0.90.`));
    box.appendChild(lrEl("p", null, n.atomic ? "This note is atomic: one point, one question." : "A key point is a claim, mechanism, condition, step or relation the note's source supports; repeated wording was grouped."));
    if (objs) box.appendChild(lrEl("p", null, `${n.also_in?.length ? `The objectives of ${n.also_in[0].name}, ${objs},` : `The objectives ${objs}`} say what counts as evidence on each.`));
    box.appendChild(lrEl("p", null, "The reason above is read from the recorded answers only: the last date, the misses, whether you were sure, whether the note changed since."));
  });
  right.appendChild(discs);
}

// The deep readings, one closed row each with its count (refinement §20).
function lrReadings(box, d, { plan = null } = {}) {
  const fresh = !plan && !d.measured;
  {
    const areas = (d.areas || []).filter((a) => a.measured);
    lrDisc(box, "Areas", areas.length ? lrPlural(areas.length, "area") : "none practised yet", (inner) => {
      inner.appendChild(lrEl("p", "lr-help", "Mean estimated retention over practised notes."));
      if (!areas.length) return;
      inner.appendChild(lrTable(
        [{ key: "folder", label: "folder" }, { key: "r", label: "retention est.", num: true, bar: true }, { key: "due", label: "to review", num: true }, { key: "never", label: "not practised", num: true }],
        areas.map((a) => ({ folder: a.folder, r: a.R_mean, due: a.due, never: a.never, _bar: a.R_mean }))));
    });
    const rows = d.horizon || [];
    const falling = rows.reduce((a, h) => a + (h.notes || 0), 0);
    lrDisc(box, "Forgetting horizon", fresh ? "from the first practice on" : `${falling} in the next seven days`, (inner) => {
      inner.appendChild(lrEl("p", "lr-help", "Notes estimated to fall due, day by day."));
      if (!rows.length) return;
      const max = Math.max(1, ...rows.map((h) => h.notes));
      inner.appendChild(lrTable(
        [{ key: "day", label: "day" }, { key: "notes", label: "notes", num: true, bar: true }],
        rows.map((h, i) => ({ day: i === 0 ? "today" : lrDate(h.day), notes: h.notes, _bar: h.notes / max, _o2: i > 0 }))));
      if (d.median_S) inner.appendChild(lrEl("p", "lr-help", `Estimates from the schedule. Median estimated hold over practised notes: ${Math.round(d.median_S)} days.`));
    });
  }
  if (fresh) return;
  const cal = plan ? plan.calibration : d.calibration;
  lrDisc(box, "Calibration", cal && cal.enough ? `n = ${cal.n}` : `${cal ? cal.n : 0} of ${cal ? cal.min : 10} answers needed`, (inner) => {
    inner.appendChild(lrEl("p", "lr-help", "Confidence before the reveal, against criteria met."));
    if (!cal || !cal.enough) { inner.appendChild(lrEl("p", "lr-help", "Not enough evidence. Unassisted, model-graded answers with a confidence count; a null confidence does not.")); return; }
    const rows = LR_CONF.map(([lbl, b]) => {
      const band = cal.bands[String(b)] || {};
      return { said: `${lbl} · ${b}`, met: band.n ? Math.round(band.hit * 100) + "%" : "Not enough evidence", n: band.n || 0, _bar: band.n ? band.hit : 0, _amber: band.n && band.hit < b / 100 };
    });
    inner.appendChild(lrTable([{ key: "said", label: "you said" }, { key: "met", label: "criteria met", num: true, bar: true }, { key: "n", label: "n", num: true }], rows));
    inner.appendChild(lrEl("p", "lr-help", `${cal.bias_say}: ${cal.bias > 0 ? "+" : ""}${cal.bias.toFixed(2)} (Brier ${cal.brier.toFixed(2)}). Descriptive, not a trait.`));
  });
  const g = plan ? plan.grader : d.grader;
  lrDisc(box, "Grader", g && g.n ? `${g.n} graded` : "none graded yet", (inner) => {
    inner.appendChild(lrEl("p", "lr-help", "The model proposes a rating, you decide."));
    if (!g || !g.n) { inner.appendChild(lrEl("p", "lr-help", "Agreement is measured once the model has graded an item.")); return; }
    inner.appendChild(lrTable([{ key: "k", label: "you kept" }, { key: "s", label: "share", num: true, bar: true }],
      [{ k: "the proposed rating", s: Math.round(g.share * 100) + "%", _bar: g.share }, { k: "another rating", s: Math.round((1 - g.share) * 100) + "%", _bar: 1 - g.share, _o2: true }]));
    inner.appendChild(lrEl("p", "lr-help", "Agreement about a scheduling rating, not accuracy. Below 75% the grader prompt is wrong, not you."));
    if (g.voice && g.voice.n) {
      const pct = (x) => (x.n ? Math.round(x.share * 100) + "%" : "none yet");
      inner.appendChild(lrEl("p", "lr-help", `Spoken answers: kept ${g.voice.kept} of ${g.voice.n} (${pct(g.voice)}); typed: ${g.typed.kept} of ${g.typed.n} (${pct(g.typed)}). A lower share on spoken answers means the transcription, not the grader, is being corrected.`));
    }
  });
}

// A chart table where one cell carries the bar (the app's chart grammar).
function lrTable(cols, rows) {
  const t = lrEl("table", "chart lr-chart");
  const thead = lrEl("thead");
  const hr = lrEl("tr");
  for (const c of cols) {
    hr.appendChild(lrEl("th", c.num ? "num" : null, c.label));
    if (c.bar) hr.appendChild(lrEl("th", "bar"));
  }
  thead.appendChild(hr);
  const tb = lrEl("tbody");
  for (const r of rows) {
    const tr = lrEl("tr");
    cols.forEach((c, i) => {
      const v = r[c.key];
      const cell = lrEl(i === 0 ? "th" : "td", c.num ? "num" : null, typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v == null ? "—" : String(v));
      if (i === 0) cell.scope = "row";
      tr.appendChild(cell);
      if (c.bar) {
        const td = lrEl("td", "bar");
        const cellDiv = lrEl("div", "cell");
        const fill = lrEl("i", r._amber ? "amber" : r._o2 ? "o2" : null);
        fill.style.width = Math.max(0, Math.min(100, Math.round((r._bar || 0) * 100))) + "%";
        cellDiv.appendChild(fill);
        td.appendChild(cellDiv);
        tr.appendChild(td);
      }
    });
    tb.appendChild(tr);
  }
  t.append(thead, tb);
  return t;
}

// --- create plan: one sentence, then the fields it fills (ADR-0081 §10 to §17) -----
// The sentence is the door: code reads the date and the verbs, one model call
// the material, and every field opens filled and editable. With no model,
// an error or nothing usable, the fields open empty with the sentence kept:
// the form never depends on the call. The objective cards are written in the
// background and wait in a closed row; they never gate Create.
const lrForm = {};
function lrFormReset(extra = {}) {
  Object.assign(lrForm, {
    step: "sentence", sentence: "", read: "", reading: false, said: [], took: null, model: false,
    name: "", nameTouched: false, scope: "", target: "", label: "", count: null, restrict: "", by: "", ref: null, refMode: "note", refQ: "", refOpts: [], refIdx: -1,
    objectives: [], refused: [], preview: null, previewBusy: false, cards: null, cardsBusy: false, cardsAt: 0,
    // ADR-0131: roles the reader set by collection label, study hours a day,
    // the assessment's parts (null: as Silica reads them), a guide note
    roles: {}, rolesTouched: false, rolesKept: false, hours: "", assessment: null, guide: "", budget: null, budgetSeq: 0,
    matched: {}, matchedTouched: false,
    cardsModel: false, cardsSaved: false, cardsFailed: false, editing: null, editPath: "", q: "", opts: [], optIdx: -1, seq: 0, cardSeq: 0,
  }, extra);
}
lrFormReset();
const LR_KIND_WORD = { folder: "folder", notes: "notes", topic: "topic", steps: "syllabus", workspace: "workspace" };
const LR_SESSION_Q = 8;

// The Study as the items it holds (ADR-0119, amendment 2026-10-17): notes and folders gather, a
// topic, a workspace or a syllabus stands alone. Notes alone stay a notes
// scope, by title; any folder among them makes one folder scope whose parts
// are joined by " | ", as `restrict` joins its bounds (plans.folder_parts).
const LR_SEP = " | ";
const lrJoins = (o) => o.kind === "folder" || o.kind === "notes";
function lrStudyItems(f) {
  if (!f.scope || !String(f.target).trim()) return [];
  if (f.scope === "notes") return (Array.isArray(f.target) ? f.target : [f.target]).map((t) => ({ kind: "notes", target: t, label: t }));
  if (f.scope === "folder") {
    return String(f.target).split(LR_SEP).map((b) => b.trim()).filter(Boolean)
      .map((b) => (/\.md$/i.test(b) ? { kind: "notes", target: lrStem(b), label: lrStem(b), path: b } : { kind: "folder", target: b, label: b }));
  }
  return [{ kind: f.scope, target: f.target, label: f.label || f.target }];
}
function lrStudyOf(items) {
  if (!items.length) return { scope: "", target: "", label: "" };
  const label = items.map((i) => i.label).join(", ");
  if (items.every((i) => i.kind === "notes")) return { scope: "notes", target: items.map((i) => i.target), label };
  if (items.length === 1) return { scope: items[0].kind, target: items[0].target, label: items[0].label };
  // a note by its path when the typeahead gave one, else by its title: the server resolves either
  return { scope: "folder", target: items.map((i) => (i.kind === "folder" ? i.target : i.path || i.target + ".md")).join(LR_SEP), label };
}

// Plan this folder (ADR-0081 §17): from a Map row or the sidebar tree, the
// fields filled with the folder and the sentence skipped.
function lrPlanFolder(folder) {
  const name = folder.replace(/\/$/, "").split("/").pop();
  lrFormReset({ step: "fields", scope: "folder", target: folder, label: folder, name });
  if (activeTab !== "learn") showTab("learn");
  lrGo({ view: "new" });
  lrPreviewScope();
}
window.learnPlanFolder = lrPlanFolder;

// Test me on this… from the note menu (ADR-0119): the dialog with the note
// as its Study; Start tests it at once, unless a date or a reference makes
// it a plan.
function learnTestNote(path) {
  const stem = lrStem(path);
  lrFormReset({ step: "fields", scope: "notes", target: [stem], label: stem });
  if (activeTab !== "learn") showTab("learn");
  lrGo({ view: "new" });
  lrPreviewScope();
}
window.learnTestNote = learnTestNote;

// Explain it (ADR-0119, suggestion 2): a teach-back on one note. The server
// has run it since 0.7.0 (POST /learn/teach); the view never called it.
function learnExplain(path) {
  learnStartSession({ note: path, teach: true, from: "explain" });
}
window.learnExplain = learnExplain;

// A workspace plan keeps its target as a wikilink by path (plans.target_text);
// the reader is shown the workspace's name.
const lrTargetShown = (p) => p.scope === "workspace"
  ? String(p.target || "").replace(/^\[\[|\]\]$/g, "").split("/").pop()
  : p.scope === "folder" ? lrStudyItems(p).map((i) => i.label).join(", ") || p.target : p.target;

// Plan from a workspace (ADR-0112 point 5): the same fields, scoped to its
// members, which the server resolves again at every read.
function learnPlanWorkspace(name) {
  // "plan", so the plan note does not take its workspace's name
  lrFormReset({ step: "fields", scope: "workspace", target: name, label: name, name: `${name} plan` });
  if (activeTab !== "learn") showTab("learn");
  lrGo({ view: "new" });
  lrPreviewScope();
}

// Edit plan opens the fields filled, without the sentence (§17). A syllabus
// plan reads as a Reference and its `restrict` bounds as the Study (ADR-0119).
function lrStudyFromBounds(restrict) {
  return lrStudyOf(lrStudyItems({ scope: "folder", target: String(restrict || "") }));
}
function lrRefOf(target) {
  const t = String(target || "");
  return t.includes("\n") ? { kind: "text", target: t, label: "" } : { kind: "note", target: t, label: lrStem(t) };
}
function lrFormFromPlan(p) {
  if (p.scope === "steps") {
    lrFormReset({
      step: "fields", editPath: p.path, name: p.name, nameTouched: true, ...lrStudyFromBounds(p.restrict), restrict: "",
      ref: lrRefOf(p.target), refMode: String(p.target || "").includes("\n") ? "text" : "note", by: p.by || "",
      objectives: p.objectives_chosen ? [...p.objectives] : [], refused: [...(p.refused || [])],
      cards: p.cards?.length ? p.cards.map((c) => ({ ...c })) : null, cardsSaved: !!p.cards?.length,
      ...lrSourcesFromPlan(p),
    });
    lrPreviewScope({ keepCards: !!p.cards?.length });
    return;
  }
  lrFormReset({
    step: "fields", editPath: p.path, name: p.name, nameTouched: true, scope: p.scope,
    target: p.scope === "notes" ? [...(p.notes || [])] : p.target,
    label: p.scope === "notes" ? (p.notes || []).join(", ") : lrTargetShown(p), restrict: p.restrict || "", by: p.by || "",
    objectives: p.objectives_chosen ? [...p.objectives] : [], refused: [...(p.refused || [])],
    // a plan from before the cards (or with none) gets them written now
    cards: p.cards?.length ? p.cards.map((c) => ({ ...c })) : null, cardsSaved: !!p.cards?.length,
  });
  lrPreviewScope({ keepCards: !!p.cards?.length });
}

// A plan's ADR-0131 keys as the form holds them: roles by label, the hours,
// the assessment it states (or null when Silica reads it), its guide.
function lrSourcesFromPlan(p) {
  const roles = {};
  for (const [key, role] of [["materials", "material"], ["references", "reference"], ["unused", "unused"]]) {
    for (const label of String(p[key] || "").split(" | ").map((x) => x.trim()).filter(Boolean)) roles[label] = role;
  }
  return {
    // kept, not touched: they go back as the plan's, said "as the plan says"
    roles, rolesTouched: false, rolesKept: Object.keys(roles).length > 0, hours: p.hours ? String(p.hours) : "",
    assessment: p.assessment_set ? (p.assessment || []).map((a) => ({ ...a })) : null,
    matched: { ...(p.matched || {}) }, matchedTouched: false,
    guide: String(p.guide || "").replace(/^\[\[|\]\]$/g, ""),
  };
}

// The dialog (ADR-0119, suggestion 5): one form for a test or a plan, over
// the home column, opened by Form, by a sentence, by the note menu and by
// the chat's + menu. Escape, × and a click outside close it.
function lrDialog() {
  const scrim = lrEl("div", "lr-scrim");
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) lrCloseDialog(); });
  const dlg = lrEl("div", "lr-dialog");
  dlg.setAttribute("role", "dialog");
  dlg.setAttribute("aria-modal", "true");
  dlg.setAttribute("aria-label", lrRoute.view === "edit" ? "Edit plan" : "New test or plan");
  const x = lrBtn("lr-x lr-dlg-x", "×", lrCloseDialog);
  x.setAttribute("aria-label", "close");
  dlg.appendChild(x);
  lrRenderComposer(dlg);
  scrim.appendChild(dlg);
  if (!dlg.contains(document.activeElement)) setTimeout(() => { if (!dlg.contains(document.activeElement)) dlg.querySelector("input, textarea")?.focus({ preventScroll: true }); }, 0);
  return scrim;
}

function lrCloseDialog() {
  lrForm.editing = null;
  lrGo(lrRoute.view === "edit" ? { view: "plan", plan: lrRoute.plan } : { view: "home" }, { replace: true });
}

function lrRenderComposer(right) {
  if (lrRoute.view === "edit" && lrForm.editPath !== lrRoute.plan) {
    if (lrPlan && lrPlan.path === lrRoute.plan) lrFormFromPlan(lrPlan);
    else { right.appendChild(lrEl("p", "lr-help", "Reading the plan.")); lrFetchPlan(lrRoute.plan); return; }
  }
  if (lrRoute.view === "new" && lrForm.editPath) lrFormReset();
  const box = lrEl("div", "lr-composer");
  if (lrForm.reading) {
    box.append(lrEl("span", "eyebrow", "New test"), lrEl("p", "lr-readback s", `“${lrForm.sentence}”`), lrEl("p", "lr-help", "Reading your sentence…"));
  } else if (lrForm.step === "sentence") lrSentenceStep(box);
  else lrFieldsStep(box);
  right.appendChild(box);
}

function lrSentenceStep(box) {
  const head = lrEl("div", "lr-phead");
  head.append(lrEl("span", "eyebrow", "New test · nothing is written until you keep it"), lrEl("h2", null, "What would you like to learn?"));
  box.appendChild(head);
  const ta = lrEl("textarea", "lr-field textarea lr-sentence");
  ta.rows = 3;
  ta.value = lrForm.sentence;
  ta.placeholder = "Psychology exam on November 30: perception, short-term memory, reinforcement learning, theory of mind. I need to explain and apply them.";
  ta.setAttribute("aria-label", "What would you like to learn?");
  ta.disabled = lrForm.reading;
  ta.addEventListener("input", () => (lrForm.sentence = ta.value));
  // Enter, or leaving the field, reads it (§10); Shift+Enter is a new line
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); lrReadSentence(); } });
  ta.addEventListener("blur", (e) => { if (!e.relatedTarget?.closest?.(".lr-cta")) lrReadSentence(); });
  box.appendChild(ta);
  const cta = lrEl("div", "lr-cta");
  cta.append(lrEl("span", "lr-help", lrForm.reading ? "Reading the sentence…" : "Say what, by when, and what you want to be able to do. Every field it fills can be changed."),
    lrBtn("lr-q", "Fill the fields yourself", () => { lrForm.step = "fields"; lrRender(); }),
    lrBtn("lr-send" + (lrForm.reading ? " off" : ""), "Read it", lrReadSentence));
  box.appendChild(cta);
  if (!lrForm.reading) setTimeout(() => ta.focus({ preventScroll: true }), 0);
}

async function lrReadSentence() {
  const text = lrForm.sentence.trim();
  if (!text || lrForm.reading || text === lrForm.read) return;
  lrForm.reading = true;
  lrRender();
  let r;
  try { r = await lrPost("/learn/plan/read", { sentence: text }); } catch (e) { r = { said: ["The reading failed (" + e.message + "); fill the fields, the sentence is kept."] }; }
  lrForm.reading = false;
  lrForm.read = text;
  Object.assign(lrForm, {
    step: "fields", said: r.said || [], took: r.took ?? null, model: !!r.model,
    scope: r.scope || "", target: r.target || "", label: Array.isArray(r.target) ? r.target.join(", ") : r.target || "", restrict: r.restrict || "",
    objectives: r.objectives || [], by: r.by || "", name: r.name || "", nameTouched: !!r.name, refused: [], preview: null, cards: null,
  });
  // a syllabus read from the sentence is the Reference, its folder the Study (ADR-0119)
  if (lrForm.scope === "steps") {
    Object.assign(lrForm, { ref: lrRefOf(lrForm.target), refMode: String(lrForm.target).includes("\n") ? "text" : "note",
      ...(lrForm.restrict ? { scope: "folder", target: lrForm.restrict, label: lrForm.restrict } : { scope: "", target: "", label: "" }), restrict: "" });
  }
  if (lrRoute.view === "new") lrRender();
  if (lrForm.scope || lrForm.ref) lrPreviewScope();
}

function lrFieldsStep(box) {
  const edit = !!lrForm.editPath;
  const head = lrEl("div", "lr-phead");
  head.appendChild(lrEl("span", "eyebrow", edit ? "Edit plan · one revertible write, sessions untouched" : lrForm.read ? "Filled from your sentence. Change anything." : "New test"));
  const name = lrEl("input", "lr-title");
  name.type = "text"; name.value = lrForm.name; name.placeholder = edit ? "Name the plan" : "Name it, to keep it as a plan";
  name.setAttribute("aria-label", "Plan name");
  name.dataset.lrKey = "form:name";
  name.addEventListener("input", () => { lrForm.name = name.value; lrForm.nameTouched = true; lrCtaState(); });
  head.appendChild(name);
  box.appendChild(head);
  // the sentence, kept above the fields, and what the reading did with it
  if (lrForm.read && !edit) {
    const said = lrEl("div", "lr-readback");
    said.appendChild(lrEl("p", "s", `“${lrForm.read}”`));
    const line = lrEl("p", "lr-help");
    line.append(lrForm.model ? `Read in ${lrForm.took} s · every field can be changed` : "Not read · the fields are yours to fill", " · ");
    line.appendChild(lrBtn("lr-link", "rewrite the sentence", () => { lrForm.step = "sentence"; lrForm.read = ""; lrRender(); }));
    said.appendChild(line);
    for (const s of lrForm.said) said.appendChild(lrEl("p", "lr-help", s));
    box.appendChild(said);
  }
  box.appendChild(lrStudySection());
  box.appendChild(lrReferenceSection());
  box.appendChild(lrSourcesSection());
  box.appendChild(lrGoalsSection());
  box.appendChild(lrBySection());
  box.appendChild(lrObjectivesSection());
  const cta = lrEl("div", "lr-cta");
  const lands = lrEl("span", "lr-help");
  lands.id = "lr-lands";
  if (edit) {
    lands.append("Saves ", lrEl("span", "path", lrLands()), " · revertible");
    const save = lrBtn("lr-send", "Save plan", () => lrWritePlan());
    save.id = "lr-create-btn";
    const del = lrBtn("lr-link", "Delete plan", lrDeletePlan);
    del.title = "move the plan note to the trash; undo stays on the toast";
    cta.append(del, lands, lrBtn("ghost box", "Cancel", lrCloseDialog), save);
  } else {
    lands.textContent = lrKeptLine();
    const keep = lrBtn("ghost box", "Save only", () => lrWritePlan());
    keep.id = "lr-create-btn";
    keep.title = "keep it as a plan without starting";
    const start = lrBtn("lr-send", "Start", lrStartTest);
    start.id = "lr-start-btn";
    cta.append(lands, lrBtn("ghost box", "Cancel", lrCloseDialog), keep, start);
  }
  box.appendChild(cta);
  // the box is not in the page yet: its buttons are found inside it
  lrCtaState(box);
}

// The plan is a note, so it goes the way a note goes from the rail (trash,
// undo on the toast); its sessions stay in the log, as Edit plan leaves them.
async function lrDeletePlan() {
  const path = lrForm.editPath;
  if (!path || !(await deleteFromTree(path))) return;
  lrForm.editing = null;
  lrData = null; lrPlan = null;
  lrGo({ view: "home" }, { replace: true });
  loadLearn(true);
}

const lrLands = () => lrForm.editPath || `Plans/${lrForm.name.trim() || "<name>"}.md`;

// A test is written as a plan only when it holds something to keep: a
// reference, a date or goals chosen (ADR-0119). Otherwise each quick test
// would add a note to Plans/ and, once active, narrow what Learn offers
// next to its notes (ADR-0111 point 4).
function lrKeeps(f = lrForm) {
  return !!(f.ref?.target?.trim?.() || f.by || f.objectives.length);
}
function lrKeptLine() {
  const f = lrForm;
  const why = [f.ref?.target?.trim?.() ? "a reference" : "", f.by ? "a date" : "", f.objectives.length ? "goals" : ""].filter(Boolean);
  return why.length ? `Kept as a plan in ${lrLands()}: it has ${lrAnd(why)}.` : "A one-off test: nothing is written but the answers. Save only keeps it as a plan.";
}

// Start and Save only need material; Save only, and Start when it keeps, a name
function lrCtaState(root = document) {
  const ok = !!lrForm.preview?.paths?.length;
  const named = !!lrForm.name.trim();
  const save = root.querySelector("#lr-create-btn");
  if (save) { save.disabled = !(ok && named); save.title = !ok ? "choose what to study first" : !named ? "name it first" : save.title; }
  const start = root.querySelector("#lr-start-btn");
  if (start) {
    start.disabled = !ok || (lrKeeps() && !named) || !!lrSession;
    start.title = !ok ? "choose what to study first" : lrKeeps() && !named ? "name it first: it is kept as a plan" : lrSession ? "a session is open" : "";
  }
  const lands = root.querySelector("#lr-lands");
  if (lands && !lrForm.editPath) lands.textContent = lrKeptLine();
}

async function lrStartTest() {
  const f = lrForm;
  if (lrKeeps(f)) { await lrWritePlan({ start: true }); return; }
  const paths = f.preview?.paths || [];
  if (!paths.length) { notify("choose what to study first", "warn"); return; }
  lrFormReset();
  learnStartSession({ scope: paths, from: "dialog" });
}

// -- Study: a token, or a typeahead over folders, notes, topics and syllabus notes
function lrStudySection() {
  const sec = lrEl("section", "lr-fs");
  sec.appendChild(lrEl("h3", null, "Study"));
  const f = lrForm;
  const row = lrEl("div", "lr-studyrow");
  const items = lrStudyItems(f);
  // with a Reference the preview counts its matches, not the Study's notes;
  // with several items the preview's line counts them all
  const n = f.ref || items.length !== 1 ? null : f.preview?.paths?.length;
  items.forEach((it, i) => {
    const tok = lrEl("span", "lr-tok");
    tok.append(lrEl("i", null, lrKindWord(it)), it.label + (n != null ? ` · ${lrPlural(n, "note")}` : ""));
    const x = lrBtn("lr-x", "×", () => lrDropStudy(i));
    x.setAttribute("aria-label", items.length > 1 ? `remove ${it.label}` : "clear what to study");
    tok.appendChild(x);
    row.appendChild(tok);
  });
  if (items.every(lrJoins)) row.appendChild(lrStudyInput(items.length));
  sec.appendChild(row);
  // a restriction read from the sentence is shown on the field and can be cleared (§13)
  if (f.restrict && ["steps", "topic"].includes(f.scope)) {
    const r = lrEl("div", "lr-asides");
    const tok = lrEl("span", "lr-tok sm");
    tok.append(lrEl("i", null, "only in"), f.restrict);
    const x = lrBtn("lr-x", "×", () => { f.restrict = ""; f.refused = []; lrPreviewScope(); });
    x.setAttribute("aria-label", "clear the restriction");
    tok.appendChild(x);
    r.appendChild(tok);
    sec.appendChild(r);
  }
  // with a Reference the preview is its lines, shown under it; here, what bounds them
  if (f.ref) sec.appendChild(lrEl("p", "lr-help", f.scope ? "The reference's lines are looked for here." : "Empty: the reference's lines are looked for in the whole vault."));
  else if (f.previewBusy && !f.preview) sec.appendChild(lrEl("p", "lr-help", "Resolving the material…"));
  else if (f.preview?.error) { const e = lrEl("p", "lr-help warn"); e.append(lrMark("warn", ""), "Could not resolve: " + f.preview.error); sec.appendChild(e); }
  else if (f.preview) lrShowPreview(sec, f.preview);
  return sec;
}

// -- Reference: what must be studied, a syllabus note or pasted text (ADR-0119).
// Stored as today's syllabus scope, bounded by the Study; optional.
function lrReferenceSection() {
  const f = lrForm;
  const sec = lrEl("section", "lr-fs");
  const h = lrEl("h3", null, "Reference");
  h.appendChild(lrEl("span", "lr-help", " optional"));
  sec.appendChild(h);
  const seg = lrEl("div", "lr-seg lr-refseg");
  seg.setAttribute("role", "radiogroup"); seg.setAttribute("aria-label", "the reference is");
  for (const [k, label] of [["note", "A note"], ["text", "Paste text"]]) {
    const on = f.refMode === k;
    const b = lrBtn(on ? "on" : null, label, () => {
      if (f.refMode === k) return;
      Object.assign(f, { refMode: k, ref: null, refused: [], preview: null, cards: null });
      lrRender();
      lrPreviewScope();
    });
    b.setAttribute("role", "radio"); b.setAttribute("aria-checked", on ? "true" : "false");
    seg.appendChild(b);
  }
  sec.appendChild(seg);
  if (f.refMode === "text") {
    const ta = lrEl("textarea", "lr-field textarea");
    const text = f.ref?.kind === "text" ? f.ref.target : "";
    ta.rows = Math.min(8, Math.max(3, text.split("\n").length));
    ta.value = text;
    ta.placeholder = "The concepts or the files to study, one per line";
    ta.setAttribute("aria-label", "the reference, one line per concept or file");
    ta.dataset.lrKey = "form:ref";
    ta.addEventListener("input", () => { f.ref = ta.value.trim() ? { kind: "text", target: ta.value, label: "" } : null; lrCtaState(); });
    ta.addEventListener("change", () => { f.refused = []; lrPreviewScope(); });
    sec.appendChild(ta);
  } else if (f.ref?.kind === "note") {
    const tok = lrEl("span", "lr-tok");
    tok.append(lrEl("i", null, "note"), f.ref.label || lrStem(f.ref.target));
    const x = lrBtn("lr-x", "×", () => { Object.assign(f, { ref: null, refused: [], preview: null, cards: null }); lrRender(); lrPreviewScope(); });
    x.setAttribute("aria-label", "clear the reference");
    tok.appendChild(x);
    sec.appendChild(tok);
  } else sec.appendChild(lrRefInput());
  if (f.ref) {
    if (f.previewBusy && !f.preview) sec.appendChild(lrEl("p", "lr-help", "Matching the lines against your notes…"));
    else if (f.preview?.error) { const e = lrEl("p", "lr-help warn"); e.append(lrMark("warn", ""), "Could not resolve: " + f.preview.error); sec.appendChild(e); }
    else if (f.preview) lrShowPreview(sec, f.preview);
  } else sec.appendChild(lrEl("p", "lr-help", "A syllabus or a list of what the exam covers: each line is matched to your notes, and a line with no note is shown."));
  return sec;
}

function lrRefInput() {
  const f = lrForm;
  const wrap = lrEl("div", "lr-ta");
  const inp = lrEl("input", "lr-field");
  inp.type = "text"; inp.id = "lr-ref"; inp.placeholder = "A note that lists what to study"; inp.value = f.refQ; inp.autocomplete = "off";
  inp.setAttribute("role", "combobox"); inp.setAttribute("aria-autocomplete", "list");
  inp.setAttribute("aria-controls", "lr-ref-opts"); inp.setAttribute("aria-expanded", "false");
  inp.setAttribute("aria-label", "the reference note");
  inp.dataset.lrKey = "form:refnote";
  const list = lrEl("ul", "lr-opts");
  list.id = "lr-ref-opts"; list.setAttribute("role", "listbox"); list.hidden = true;
  const paint = () => {
    list.innerHTML = "";
    f.refOpts.forEach((o, i) => {
      const li = lrEl("li", i === f.refIdx ? "on" : null);
      li.setAttribute("role", "option"); li.setAttribute("aria-selected", i === f.refIdx ? "true" : "false");
      li.append(lrEl("i", null, o.kind === "steps" ? "syllabus" : "note"), lrEl("span", "l", o.label));
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(o); });
      list.appendChild(li);
    });
    list.hidden = !f.refOpts.length;
    inp.setAttribute("aria-expanded", list.hidden ? "false" : "true");
  };
  const pick = (o) => { Object.assign(f, { ref: { kind: "note", target: o.target, label: o.label }, refQ: "", refOpts: [], refIdx: -1, refused: [], preview: null, cards: null }); lrRender(); lrPreviewScope(); };
  let t = null;
  inp.addEventListener("input", () => {
    f.refQ = inp.value;
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = inp.value.trim();
      let opts = [];
      if (q) { try { opts = await (await fetch("/learn/scopes?q=" + encodeURIComponent(q))).json(); } catch { opts = []; } }
      if (inp.value.trim() !== q) return;
      // a syllabus note first, then any note: a note's lines are what is read
      const seen = new Set();
      f.refOpts = (Array.isArray(opts) ? opts : []).filter((o) => ["steps", "notes"].includes(o.kind))
        .sort((a, b) => (a.kind === "steps" ? 0 : 1) - (b.kind === "steps" ? 0 : 1))
        .filter((o) => !seen.has(o.target) && seen.add(o.target)).slice(0, 6);
      f.refIdx = f.refOpts.length ? 0 : -1;
      paint();
    }, 120);
  });
  inp.addEventListener("blur", () => setTimeout(() => { list.hidden = true; inp.setAttribute("aria-expanded", "false"); }, 150));
  inp.addEventListener("keydown", (e) => {
    const n = f.refOpts.length;
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && n) { e.preventDefault(); f.refIdx = (f.refIdx + (e.key === "ArrowDown" ? 1 : n - 1)) % n; paint(); }
    else if (e.key === "Enter") { e.preventDefault(); const o = f.refOpts[Math.max(0, f.refIdx)]; if (o) pick(o); }
    else if (e.key === "Escape" && !list.hidden) { e.stopPropagation(); list.hidden = true; inp.setAttribute("aria-expanded", "false"); }
  });
  wrap.append(inp, list);
  return wrap;
}

function lrDropStudy(i) {
  const items = lrStudyItems(lrForm);
  items.splice(i, 1);
  Object.assign(lrForm, lrStudyOf(items), { preview: null, cards: null, refused: [] });
  if (!items.length) lrForm.restrict = "";
  lrRender();
  lrPreviewScope();
  setTimeout(() => $("#lr-study")?.focus(), 0);
}

function lrStudyInput(chosen = 0) {
  const wrap = lrEl("div", "lr-ta");
  const inp = lrEl("input", "lr-field");
  inp.type = "text"; inp.id = "lr-study"; inp.placeholder = chosen ? "Add a folder or a note" : "A folder, a note or a topic";
  inp.value = lrForm.q;
  inp.setAttribute("role", "combobox"); inp.setAttribute("aria-autocomplete", "list");
  inp.setAttribute("aria-controls", "lr-opts"); inp.setAttribute("aria-expanded", "false");
  inp.setAttribute("aria-label", "what to study");
  inp.dataset.lrKey = "form:study";
  const list = lrEl("ul", "lr-opts");
  list.id = "lr-opts"; list.setAttribute("role", "listbox"); list.hidden = true;
  let t = null;
  inp.addEventListener("input", () => { lrForm.q = inp.value; clearTimeout(t); t = setTimeout(() => lrScopeSearch(inp, list), 120); });
  // empty, focus offers the folders; once one is chosen the focus a re-render
  // hands back would reopen the list over the preview at every pick
  inp.addEventListener("focus", () => { if (!chosen || inp.value) lrScopeSearch(inp, list); });
  inp.addEventListener("blur", () => setTimeout(() => { list.hidden = true; inp.setAttribute("aria-expanded", "false"); }, 150));
  inp.addEventListener("keydown", (e) => {
    const n = lrForm.opts.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!n) return;
      e.preventDefault();
      lrForm.optIdx = (lrForm.optIdx + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      lrPaintOpts(inp, list);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const o = lrForm.opts[Math.max(0, lrForm.optIdx)];
      if (o) lrPickScope(o);
    } else if (e.key === "Escape" && !list.hidden) { e.stopPropagation(); list.hidden = true; inp.setAttribute("aria-expanded", "false"); }
  });
  wrap.append(inp, list);
  return wrap;
}

async function lrScopeSearch(inp, list) {
  const q = inp.value;
  let opts = [];
  // a failed lookup is an empty list: the reader can still paste a syllabus
  try { opts = await (await fetch("/learn/scopes?q=" + encodeURIComponent(q))).json(); } catch { opts = []; }
  if (inp.value !== q) return;      // a later keystroke owns the list
  const items = lrStudyItems(lrForm);
  const taken = new Set(items.map((i) => i.kind + ":" + i.target));
  lrForm.opts = (Array.isArray(opts) ? opts : []).filter((o) => o.kind !== "steps"
    && (!items.length || (lrJoins(o) && !taken.has(o.kind + ":" + o.target))));
  lrForm.optIdx = lrForm.opts.length ? 0 : -1;
  lrPaintOpts(inp, list);
}

function lrPaintOpts(inp, list) {
  list.innerHTML = "";
  lrForm.opts.forEach((o, i) => {
    const li = lrEl("li", i === lrForm.optIdx ? "on" : null);
    li.id = "lr-opt-" + i;
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", i === lrForm.optIdx ? "true" : "false");
    li.append(lrEl("i", null, lrKindWord(o)), lrEl("span", "l", o.label), lrEl("span", "c num", o.kind === "steps" ? `${o.count} lines` : lrPlural(o.count, "note")));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); lrPickScope(o); });
    list.appendChild(li);
  });
  list.hidden = !lrForm.opts.length;
  inp.setAttribute("aria-expanded", list.hidden ? "false" : "true");
  if (lrForm.optIdx >= 0) inp.setAttribute("aria-activedescendant", "lr-opt-" + lrForm.optIdx);
}

function lrPickScope(o) {
  const had = lrStudyItems(lrForm);
  const items = lrJoins(o) && had.every(lrJoins) ? [...had, o] : [o];
  Object.assign(lrForm, lrStudyOf(items), { q: "", opts: [], refused: [], preview: null, cards: null });
  if (lrJoins(o)) lrForm.restrict = "";
  lrRender();
  lrPreviewScope();
}

// The name follows the material until the reader types one (the category's
// pattern; the old form asked for it last, empty, with a fixed placeholder)
function lrDefaultName() {
  const f = lrForm;
  if (f.nameTouched) return;
  if (f.ref?.kind === "note") { f.name = f.ref.label || lrStem(f.ref.target); return; }
  if (f.ref && !f.scope) { f.name = `Syllabus, ${lrDate(new Date().toISOString())}`; return; }
  if (!f.scope) return;
  if (f.scope === "folder") f.name = lrStudyItems(f).map((i) => (i.kind === "folder" ? i.target.replace(/\/$/, "").split("/").pop() : i.label)).join(", ");
  else if (f.scope === "steps") f.name = f.label && !f.target.includes("\n") ? f.label : `Syllabus, ${lrDate(new Date().toISOString())}`;
  // "plan", so the plan note does not take its workspace's name (ADR-0112)
  else if (f.scope === "workspace") f.name = `${f.label} plan`;
  else f.name = f.label || f.target;
}

async function lrPreviewScope({ keepCards = false } = {}) {
  const f = lrForm;
  if (!f.ref?.target?.trim?.() && !(f.scope && String(f.target).trim())) { f.preview = null; lrCtaState(); return; }
  if (!keepCards) f.cardsSaved = false;
  lrDefaultName();
  const seq = ++f.seq;
  f.previewBusy = true;
  // a cards call still running was written for the material before this change
  if (!keepCards) { f.cards = null; f.cardSeq++; f.cardsBusy = false; }
  lrRender();
  let out;
  try {
    out = await lrPost("/learn/plan", { ...lrStudyBody(), preview: true });
  } catch (e) { out = { error: e.message }; }
  if (seq !== f.seq) return;   // a later change owns the preview
  f.preview = out;
  f.previewBusy = false;
  if (!["new", "edit"].includes(lrRoute.view)) return;
  lrRender();
  if (!out.error && f.by) lrBudget();
  if (!out.error && out.paths?.length && !(keepCards && f.cards)) lrWriteCards();
}

function lrStudyBody() {
  const f = lrForm;
  // notes travel as a list: "Sensazione, percezione, psicofisica" is one title
  const target = f.scope === "notes" ? (Array.isArray(f.target) ? f.target : [f.target]) : f.target;
  const body = { scope: f.scope, target, restrict: f.restrict, refused: f.refused, objectives: f.objectives };
  // the server turns Study and Reference into a syllabus bounded by the Study (ADR-0119)
  // pasted text always carries a line break: one line equal to a title would
  // otherwise be read as that note (plans._steps_scope)
  if (f.ref?.target?.trim?.()) body.reference = f.ref.kind === "text" && !f.ref.target.includes("\n") ? f.ref.target + "\n" : f.ref.target;
  if (f.ref || f.scope === "steps") Object.assign(body, lrSourcesBody());
  return body;
}

// What the form says about the sources (ADR-0131): the roles only once the
// reader changed one, so Silica's reading stands until then; every key left
// out keeps what the plan says.
function lrSourcesBody() {
  const f = lrForm;
  const out = {};
  if (f.rolesTouched || f.rolesKept) {
    const roles = { ...Object.fromEntries((f.preview?.sources || []).map((s) => [s.label, s.role])), ...f.roles };
    for (const [key, role] of [["materials", "material"], ["references", "reference"], ["unused", "unused"]]) {
      out[key] = Object.keys(roles).filter((l) => roles[l] === role);
    }
    if (!f.rolesTouched) out.roles_kept = true;
  }
  if (f.hours) out.hours = Number(f.hours);
  if (f.assessment !== null) out.assessment = f.assessment.length ? f.assessment : "none";
  if (f.guide) out.guide = f.guide;
  // the reader's answers to "is it one of these?", sent once there are any
  if (f.matchedTouched || Object.keys(f.matched || {}).length) out.matched = f.matched || {};
  return out;
}

// The cards, in the background (§16): one model call, never the gate
async function lrWriteCards() {
  const f = lrForm;
  const seq = ++f.cardSeq;
  f.cardsBusy = true;
  f.cardsAt = Date.now();
  lrPaintCardsStatus();
  let out;
  try { out = await lrPost("/learn/plan", { ...lrStudyBody(), cards_only: true }); } catch (e) {
    // the cards are never the gate (§16): a failed call leaves Create writing
    // the templates, and the row says so instead of a stale "written on the notes"
    out = null;
  }
  if (seq !== f.cardSeq) return;
  f.cardsBusy = false;
  f.cardsFailed = !out;
  f.cardsSaved = false;
  if (out) { f.cards = out.cards || []; f.cardsModel = !!out.model; } else f.cardsModel = false;
  if (["new", "edit"].includes(lrRoute.view)) lrRender();
}
setInterval(() => { if (lrForm.cardsBusy) lrPaintCardsStatus(); }, 1000);
function lrPaintCardsStatus() {
  const el = $("#lr-cards-status");
  if (el) el.textContent = lrCardsLine();
}
function lrCardsLine() {
  const f = lrForm;
  if (!f.preview?.paths?.length) return "Written once the material is chosen.";
  const parts = f.preview.parts || 0;
  const n = f.cards ? f.cards.filter((c) => c.source !== "gap").length : parts * Math.max(1, f.objectives.length);
  const head = `${lrPlural(n, "objective")}${f.objectives.length <= 1 && parts > 1 ? ", one per part" : ""}`;
  if (f.cardsBusy) return `${head} · writing, ${Math.round((Date.now() - f.cardsAt) / 1000)} s`;
  if (f.cardsFailed && !f.cards) return `${head} · not written, the call failed; Create writes the templates`;
  return `${head} · ${f.cardsSaved ? "as saved in the plan" : f.cardsModel ? "written on the notes" : "template wording"}`;
}

// The preview is the Map's grid of the material, its counts the plan's own
// bases (§12): what you already know shows while you choose.
function lrShowPreview(sec, p) {
  const f = lrForm;
  const steps = !!f.ref || f.scope === "steps";
  const line = lrEl("p", "lr-pline");
  if (steps) {
    const covered = p.tree.filter((n) => !n.gap);
    const doubt = covered.filter((n) => n.doubt).length;
    line.appendChild(lrEl("b", null, `${lrPlural(p.tree.length, "line")} · ${lrPlural(p.paths.length, "note")}`));
    line.append(` · ${covered.length - doubt} matched` + (doubt ? ` · ${doubt} to check` : "") + (p.gaps.length ? ` · ${lrPlural(p.gaps.length, "gap")}` : ""));
  } else {
    line.appendChild(lrEl("b", null, `${lrPlural(p.paths.length, "note")}` + (p.parts > 1 ? ` in ${p.parts} parts` : "")));
    line.append(` · ${p.measured} practised · ${p.never} not tested yet` + (p.due ? ` · ${p.due} to review` : ""));
  }
  sec.appendChild(line);
  if (steps) {
    const st = lrEl("div", "lr-steps2");
    p.tree.forEach((n, i) => {
      st.appendChild(lrEl("span", "n num", String(i + 1)));
      st.appendChild(lrEl("span", "s", n.node));
      const to = lrEl("span", "to" + (n.gap ? " gap" : ""));
      if (n.gap) to.append(lrEl("i", "lr-cell never"), "not in the vault · kept as a gap");
      else {
        // A syllabus block meets eight notes; listed whole, sixteen blocks made
        // the dialog 4,000 px tall. Four names, the rest counted, all on hover.
        const names = n.leaves.map(lrStem);
        to.textContent = names.slice(0, 4).join(", ") + (names.length > 4 ? ` and ${names.length - 4} more` : "");
        to.title = names.join(", ");
      }
      // a syllabus block's topics no note covers: the line's most useful part
      if (n.missing?.length) to.appendChild(lrEl("span", "lr-miss", ` · not in the vault: ${n.missing.join(", ")}`));
      // the step's notes nearest a topic no source met, to answer by a click,
      // the reader's answers, and the topics met by meaning only: on a line
      // of their own under the step, the names column was 200 px wide
      // (ADR-0131 amendment, point 5)
      const extra = lrEl("span", "x");
      for (const [topic, cands] of Object.entries(n.candidates || {})) {
        if (!cands.length) continue;
        const ask = lrEl("span", "lr-gapask");
        ask.append(`Is “${topic}” one of these? `);
        cands.forEach((c, k) => {
          const b = lrBtn("lr-link", c, () => { f.matched = { ...f.matched, [topic]: c }; f.matchedTouched = true; lrPreviewScope(); });
          b.setAttribute("aria-label", `“${topic}” is ${c}`);
          ask.appendChild(b);
          if (k < cands.length - 1) ask.append(", ");
        });
        extra.appendChild(ask);
      }
      for (const [topic, c] of Object.entries(n.matched || {})) {
        const m = lrEl("span");
        m.append(`${topic} → ${c}, as you said `);
        const u = lrBtn("lr-link", "undo", () => { const x = { ...f.matched }; delete x[topic]; f.matched = x; f.matchedTouched = true; lrPreviewScope(); });
        u.setAttribute("aria-label", `undo “${topic}” is ${c}`);
        m.appendChild(u);
        extra.appendChild(m);
      }
      // a topic only a title vector met: the coverage to check, by topic
      if (n.by_meaning?.length) extra.appendChild(lrEl("span", null, `By meaning, ${n.by_meaning.map(([tp, c]) => `${tp} ≈ ${c}`).join("; ")}`));
      st.appendChild(to);
      const how = lrEl("span", "how" + (n.doubt ? " amb" : ""));
      if (n.doubt) how.append(lrMark("warn", ""), `${n.how} · check`);
      else how.textContent = n.how === "refused" ? `you refused ${lrPlural(n.refused || 0, "note")}` : n.how || "";
      st.appendChild(how);
      // refused here by index, or on the plan note by its text (Edit plan)
      const refusedNow = f.refused.includes(i) || f.refused.includes(n.text);
      const nt = n.gap && !refusedNow ? lrEl("span") : lrBtn("lr-link", refusedNow ? "undo" : "not this", () => {
        f.refused = refusedNow ? f.refused.filter((x) => x !== i && x !== n.text) : [...f.refused, i];
        lrPreviewScope();
      });
      if (!n.gap || refusedNow) nt.setAttribute("aria-label", `${refusedNow ? "undo refusing" : "refuse the match of"} line ${i + 1}`);
      st.appendChild(nt);
      if (extra.childNodes.length) st.appendChild(extra);
    });
    sec.appendChild(st);
    return;
  }
  const grid = lrEl("div", "lr-map lr-pmap");
  const root = f.scope === "folder" ? f.target.replace(/\/$/, "") : "";
  const nodes = f.scope === "notes" ? [{ node: "Notes", leaves: p.paths }] : p.tree.filter((n) => n.leaves?.length);
  for (const n of nodes) {
    const row = lrEl("div", "lr-maprow");
    const head = lrEl("div", "f");
    const label = root && n.node.startsWith(root) ? (n.node.slice(root.length).replace(/^\//, "") || lrStem(root)) : n.node;
    head.append(lrEl("span", "fn", label), lrEl("span", "cnt", String(n.leaves.length)));
    row.appendChild(head);
    const cells = lrEl("div", "cells");
    for (const path of n.leaves) {
      const m = p.marks?.[path] || "never";
      const c = lrEl("i", "lr-cell " + m);
      c.title = `${lrStem(path)} · ${LR_CELL_SAY[m]}`;
      cells.appendChild(c);
    }
    row.appendChild(cells);
    grid.appendChild(row);
  }
  sec.appendChild(grid);
  if (p.neighbours?.length) {
    const nb = lrEl("div", "lr-asides");
    nb.appendChild(lrEl("span", "lr-help", "Nearby concepts"));
    for (const c of p.neighbours.slice(0, 6)) nb.appendChild(lrBtn("lr-chip", c, () => { Object.assign(f, { target: c, label: c, refused: [] }); if (!f.nameTouched) f.name = c; lrPreviewScope(); }));
    sec.appendChild(nb);
  }
}

// -- Sources: each collection of nucleated sources the reference meets, its
// role in this plan and the rule that gave it (ADR-0131 points 2 and 13);
// the assessment the reference states, as parts that can be removed; a
// guide for the steps' depth when the bound holds one.
const LR_ROLE = { material: "Material", reference: "Reference", unused: "Not used" };
const LR_PART = { project: "Project", oral: "Oral exam", written: "Written exam", presentation: "Presentation", exercises: "Exercises" };
function lrSourcesSection() {
  const f = lrForm;
  const p = f.preview;
  const sources = p?.sources || [];
  const read = p?.assessment || [];
  if (!(f.ref || f.scope === "steps") || (!sources.length && !read.length && !p?.guides?.length)) return document.createDocumentFragment();
  const sec = lrEl("section", "lr-fs");
  sec.appendChild(lrEl("h3", null, "Sources"));
  if (sources.length) {
    const list = lrEl("div", "lr-srcs");
    for (const src of sources) {
      const role = f.roles[src.label] || src.role || "unused";
      const row = lrEl("div", "lr-src");
      const name = lrEl("span", "n", src.label);
      name.title = `${src.label} · ${lrPlural(src.spines || 0, "source")}`;
      const sel = lrEl("select", "lr-field");
      sel.setAttribute("aria-label", `role of ${src.label} in this plan`);
      for (const [v, label] of Object.entries(LR_ROLE)) {
        const o = lrEl("option", null, label);
        o.value = v; o.selected = v === role;
        sel.appendChild(o);
      }
      sel.addEventListener("change", () => { f.roles[src.label] = sel.value; f.rolesTouched = true; lrPreviewScope(); });
      const why = lrEl("span", "lr-help", src.why || "");
      row.append(name, sel, why);
      list.appendChild(row);
    }
    sec.appendChild(list);
    sec.appendChild(lrEl("p", "lr-help", "Steps are made of the material's outline rows; a reference's notes come in only where the material is silent, marked (ref)."));
    if (p.order_note) {
      // the steps were matched by their words alone: said where they are chosen
      const w = lrEl("p", "lr-help warn");
      w.append(lrMark("warn", ""), p.order_note);
      sec.appendChild(w);
    }
  }
  const parts = f.assessment !== null ? f.assessment : read;
  const as = lrEl("div", "lr-chips lr-assess");
  as.setAttribute("role", "group"); as.setAttribute("aria-label", "the assessment");
  as.appendChild(lrEl("span", "lr-help", "Assessment"));
  parts.forEach((a, i) => {
    const c = lrBtn("lr-chip on", `${LR_PART[a.part] || a.part}${a.days ? ` · ${a.days} days before` : ""} ×`, () => {
      f.assessment = parts.filter((_x, j) => j !== i);
      lrBudget();
      lrRender();
    });
    c.setAttribute("aria-label", `remove ${LR_PART[a.part] || a.part} from the assessment`);
    as.appendChild(c);
  });
  if (!parts.length) as.appendChild(lrEl("span", "lr-help", read.length ? "none kept" : "none read in the reference"));
  if (f.assessment !== null && read.length) as.appendChild(lrBtn("lr-link", "read it again", () => { f.assessment = null; lrBudget(); lrRender(); }));
  sec.appendChild(as);
  if (p?.guides?.length) {
    const row = lrEl("div", "lr-studyrow");
    const sel = lrEl("select", "lr-field w");
    sel.setAttribute("aria-label", "a study guide for each step's depth, optional");
    const none = lrEl("option", null, "No guide");
    none.value = "";
    sel.appendChild(none);
    for (const g of p.guides) {
      const o = lrEl("option", null, lrStem(g));
      o.value = lrStem(g); o.selected = lrStem(g) === f.guide;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => { f.guide = sel.value; });
    row.append(lrEl("span", "lr-help", "Guide"), sel, lrEl("span", "lr-help", "each step's depth from its table"));
    sec.appendChild(row);
  }
  return sec;
}

// The hours under the calendar (plans.budget), asked of the server on a
// change of date, hours a day or assessment: the reference's own hours and
// what the days before the date hold, with the project's days kept.
async function lrBudget() {
  const f = lrForm;
  const tree = f.preview?.tree || [];
  if (!f.by || !tree.length || !(f.ref || f.scope === "steps")) { f.budget = null; return; }
  // its own counter: the preview's (f.seq) drops a preview still running
  const seq = f.budgetSeq = (f.budgetSeq || 0) + 1;
  try {
    const out = await lrPost("/learn/plan/budget", {
      by: f.by, hours: f.hours ? Number(f.hours) : null,
      assessment: f.assessment !== null ? f.assessment : f.preview?.assessment || [],
      steps: tree.map((n) => ({ hours: n.hours ?? null, unit: n.unit || "", notes: (n.leaves || []).length })),
    });
    if (seq === f.budgetSeq) { f.budget = out?.say || null; lrRender(); }
  } catch (e) { f.budget = null; }
}

// -- Be able to: the five intentions as chips, none required (§14)
function lrGoalsSection() {
  const sec = lrEl("section", "lr-fs");
  sec.appendChild(lrEl("h3", null, "Be able to"));
  const chips = lrEl("div", "lr-chips");
  chips.setAttribute("role", "group"); chips.setAttribute("aria-label", "what you want to be able to do");
  for (const o of Object.keys(LR_OBJ)) {
    const on = lrForm.objectives.includes(o);
    const c = lrBtn("lr-chip" + (on ? " on" : ""), lrCap(LR_OBJ_SHORT[o]), () => {
      lrForm.objectives = on ? lrForm.objectives.filter((x) => x !== o) : Object.keys(LR_OBJ).filter((x) => x === o || lrForm.objectives.includes(x));
      lrPreviewScope();
    });
    c.setAttribute("aria-pressed", on ? "true" : "false");
    c.title = LR_OBJ_DESC[o];
    c.dataset.lrKey = "goal:" + o;
    chips.appendChild(c);
  }
  sec.appendChild(chips);
  const p = lrForm.preview;
  if (!p || !p.paths?.length) {
    sec.appendChild(lrEl("p", "lr-help", "They combine, they are not rungs. Nothing chosen: Silica picks one per part from the material."));
  } else if (!lrForm.objectives.length) {
    const by = new Map();
    for (const x of p.infer || []) { if (!by.has(x.intention)) by.set(x.intention, []); by.get(x.intention).push(x.part); }
    const short = (part) => (lrForm.scope === "folder" ? lrStem(part) : part);
    const bits = [...by].map(([o, parts]) => [o, parts.length <= 2 ? lrAnd(parts.map(short)) : `${parts.length} parts`]);
    const line = lrEl("p", "lr-help");
    line.append("Nothing chosen: Silica asks ");
    bits.forEach(([o, where], i) => { line.append(lrEl("b", null, LR_OBJ_SHORT[o] || o), ` on ${where}`); line.append(i < bits.length - 2 ? ", " : i === bits.length - 2 ? " and " : "."); });
    line.append(" Pick any to ask the same on every part.");
    sec.appendChild(line);
  } else {
    const parts = p.parts || 0;
    const names = lrForm.objectives.map((o) => LR_OBJ_SHORT[o]);
    sec.appendChild(lrEl("p", "lr-help", `${lrCap(lrAnd(names))}, on each of the ${lrPlural(parts, "part")}${p.gaps?.length ? `. ${p.gaps.length > 1 ? "The gaps get" : "The gap gets"} an objective to define, never met` : ""}.`));
  }
  return sec;
}

// -- By: optional; the workload shows before the commit (§15)
function lrBySection() {
  const sec = lrEl("section", "lr-fs");
  sec.appendChild(lrEl("h3", null, "By"));
  const row = lrEl("div", "lr-studyrow");
  const by = lrEl("input", "lr-field w");
  by.type = "date"; by.value = lrForm.by;
  by.setAttribute("aria-label", "target date, optional");
  by.min = new Date().toISOString().slice(0, 10);
  by.addEventListener("change", () => { lrForm.by = by.value; lrBudget(); lrRender(); });
  row.append(by, lrEl("span", "lr-help", "optional"));
  if (lrForm.by) row.appendChild(lrBtn("lr-link", "clear", () => { lrForm.by = ""; lrForm.budget = null; lrRender(); }));
  if (lrForm.ref || lrForm.scope === "steps") {
    // study hours a day: the calendar's hours come from here, the reference's
    // own numbers only weigh the steps (ADR-0131 point 8)
    const h = lrEl("input", "lr-field lr-hours");
    h.type = "number"; h.min = "0.5"; h.max = "24"; h.step = "0.5"; h.value = lrForm.hours; h.placeholder = "h a day";
    h.setAttribute("aria-label", "study hours a day, optional");
    h.addEventListener("change", () => { lrForm.hours = h.value; lrBudget(); lrRender(); });
    row.append(h, lrEl("span", "lr-help", "hours a day"));
  }
  sec.appendChild(row);
  if (lrForm.budget) sec.appendChild(lrEl("p", "lr-help", lrForm.budget));
  const say = lrProjection();
  if (say) {
    const p = lrEl("p", "lr-help" + (say.amber ? " warn" : ""));
    if (say.amber) p.appendChild(lrMark("warn", ""));
    p.append(say.text);
    sec.appendChild(p);
  }
  return sec;
}

// Workload arithmetic from the preview's counts and the session size: one
// pass asks every note once per intention. Never a refusal, and no interval
// is shortened for a deadline (D24).
function lrProjection() {
  const p = lrForm.preview;
  if (!p || !p.paths?.length) return null;
  const notes = p.paths.length;
  const q = notes * Math.max(1, lrForm.objectives.length);
  const sessions = Math.max(1, Math.ceil(q / LR_SESSION_Q));
  const mins = _lrMinutes(LR_SESSION_Q);
  const pass = sessions === 1 ? `One session of ${LR_SESSION_Q} questions covers a pass` : `One pass is ${sessions} sessions of ${LR_SESSION_Q} questions, about ${mins} min each`;
  const today = new Date(new Date().toISOString().slice(0, 10));
  if (!lrForm.by) {
    const end = new Date(today.getTime() + Math.ceil(sessions / 2) * 7 * 86400000);
    return { text: sessions === 1 ? `${pass}.` : `${pass}. At two sessions a week it ends around ${lrDate(end.toISOString())}.` };
  }
  const days = Math.max(0, Math.round((new Date(lrForm.by) - today) / 86400000));
  if (!days) return { text: `The date is today: ${pass.charAt(0).toLowerCase() + pass.slice(1)}.`, amber: sessions > 1 };
  const perDay = q / days;
  if (perDay > LR_SESSION_Q) {
    return { amber: true, text: `${lrPlural(days, "day")} for ${lrPlural(notes, "note")} is ${Math.ceil(perDay)} questions a day, more than a session of ${LR_SESSION_Q} every day. Narrow the material or move the date; nothing is refused.` };
  }
  const perWeek = sessions / (days / 7);
  const when = `By ${lrDate(lrForm.by)}`;
  if (sessions === 1) return { text: `${pass}; ${lrPlural(days, "day")} leave room for a second pass on what you miss.` };
  return { text: `${pass}. ${when} that is ` + (perWeek <= 1 ? "one a week; two leave room to ask again what you miss." : `${Math.ceil(perWeek)} a week.`) };
}
const _lrMinutes = (q) => Math.max(1, Math.round(q * 1.3));

// -- Objectives: a closed row, written in the background, never the gate (§16)
function lrObjectivesSection() {
  const f = lrForm;
  const sec = lrEl("section", "lr-fs");
  sec.appendChild(lrEl("h3", null, "Objectives"));
  const det = lrEl("details", "lr-objrow");
  det.open = lrDiscOpen.has("form:objectives");
  det.addEventListener("toggle", () => (det.open ? lrDiscOpen.add("form:objectives") : lrDiscOpen.delete("form:objectives")));
  const sum = lrEl("summary");
  const st = lrEl("span", "t", lrCardsLine());
  st.id = "lr-cards-status";
  sum.append(lrEl("span", "chev"), st);
  const check = (f.cards || []).filter((c) => c.reads_as).length;
  if (check) { const m = lrEl("span", "m"); m.append(lrMark("warn", ""), `${check} to check`); sum.appendChild(m); }
  else sum.appendChild(lrEl("span", "m mute", "Saving writes what is shown"));
  det.appendChild(sum);
  const inner = lrEl("div", "in");
  if (!f.cards) inner.appendChild(lrEl("p", "lr-help", f.cardsBusy ? "The cards show here once written; Create meanwhile writes them in template wording." : "Written once the material is chosen."));
  else {
    f.cards.forEach((c, i) => {
      if (f.editing === i) { inner.appendChild(lrObjEditor(c, i)); return; }
      inner.appendChild(lrObjRow(c, c.source === "gap" ? {} : { edit: () => { f.editing = i; lrRender(); }, remove: () => { f.cards.splice(i, 1); lrRender(); } }));
    });
    if (!f.cards.some((c) => c.source !== "gap")) inner.appendChild(lrEl("p", "lr-help", "No objective proposed: the plan can still be created with objectives to define, never counted as met."));
    inner.appendChild(lrEl("p", "lr-help", "Objectives say what counts as evidence; each note's key points say what to cover."));
  }
  det.appendChild(inner);
  sec.appendChild(det);
  return sec;
}

function lrObjEditor(c, i) {
  const box = lrEl("div", "lr-ob editing");
  const f = (label, key) => { const fr = lrEl("label", "lr-flab"); fr.appendChild(lrEl("span", "lr-help", label)); const ta = lrEl("textarea", "lr-field textarea"); ta.value = c[key] || ""; ta.rows = 2; ta.addEventListener("input", () => { c[key] = ta.value; if (key === "title") c.reads_as = null; }); fr.appendChild(ta); return fr; };
  box.append(f("Objective", "title"), f("Conditions", "conditions"), f("Criterion", "criterion"));
  box.appendChild(lrBtn("ghost box", "Done", () => { lrForm.editing = null; lrRender(); }));
  void i;
  return box;
}

async function lrWritePlan({ start = false } = {}) {
  const f = lrForm;
  if (!f.name.trim()) { notify("a plan needs a name", "warn"); return; }
  if (!f.preview?.paths?.length) { notify("choose what to study first", "warn"); return; }
  const body = { ...lrStudyBody(), name: f.name.trim(), by: f.by || null };
  // saving is confirming: the roles the reader saw are written, even the ones
  // Silica marked to confirm and the reader left as they were (ADR-0131)
  if ((f.ref || f.scope === "steps") && f.preview?.sources?.length && !f.rolesTouched) {
    const roles = Object.fromEntries(f.preview.sources.map((s) => [s.label, f.roles[s.label] || s.role]));
    for (const [key, role] of [["materials", "material"], ["references", "reference"], ["unused", "unused"]]) {
      body[key] = Object.keys(roles).filter((l) => roles[l] === role);
    }
    delete body.roles_kept;
    body.roles_confirmed = true;
  }
  // what the row says at this moment is what is written (ADR-0041, §16)
  if (f.cards && !f.cardsBusy) body.cards = f.cards;
  else body.templates = true;
  if (f.editPath) body.path = f.editPath;
  const btn = $("#lr-create-btn");
  if (btn) btn.disabled = true;
  try {
    const out = await lrPost("/learn/plan", body);
    notify(`${f.editPath ? "plan saved" : "plan created"} at ${out.path}` + (out.gaps?.length ? ` · ${lrPlural(out.gaps.length, "gap")}` : ""), "info");
    lrFormReset();
    lrData = null; lrPlan = null; lrMap = null;
    await loadLearn(true);
    lrGo({ view: "plan", plan: out.path }, { replace: true });
    if (start) learnStartSession({ plan: out.path, from: "dialog" });
  } catch (e) { notify(`plan not ${f.editPath ? "saved" : "created"}: ` + e.message); if (btn) btn.disabled = false; }
}

// --- session -----------------------------------------------------------------
// The use gate's client half (ADR-0081, Gates): sessions started from a row
// of Next against the primary, and sessions in which the Map was opened.
// Read with localStorage["silica.learn.gate"]; nothing reaches the vault.
const LR_GATE = "silica.learn.gate";
function lrGateCount(key) {
  try {
    const g = JSON.parse(localStorage.getItem(LR_GATE) || "{}");
    if (key === "map") { if (g.mapArmed) { g.map = (g.map || 0) + 1; g.mapArmed = false; } }
    else { g[key] = (g[key] || 0) + 1; g.sessions = (g.sessions || 0) + 1; g.mapArmed = true; }
    g.since = g.since || new Date().toISOString().slice(0, 10);
    localStorage.setItem(LR_GATE, JSON.stringify(g));
  } catch { /* private window: the gate reads the ledger alone */ }
}

async function learnStartSession(opts = {}) {
  if (lrSession) { notify("a session is open; end it or resume it first", "warn"); showTab("learn"); lrGo({ view: "session", id: lrSession.id }); return; }
  const body = {};
  if (opts.n) body.n = opts.n;
  if (opts.plan) body.plan = opts.plan;
  if (opts.note) body.note = opts.note;
  if (opts.points) body.points = opts.points;
  if (opts.scope) body.scope = opts.scope;
  if (opts.step !== undefined) body.step = opts.step;
  if (opts.teach) body.teach = true;
  lrStarting = true;
  $("#learn-loading").hidden = false;
  $("#learn-loading").querySelector(".fl-msg").textContent = "Picking and writing the questions.";
  let s;
  try { s = await lrPost("/learn/session", body); } catch (e) {
    lrStarting = false;
    $("#learn-loading").hidden = true;
    if (e.status === 409) { notify("a session is already open; end it or resume it first", "warn"); lrData = null; loadLearn(true); return; }
    notify("couldn't start a session: " + e.message);
    return;
  }
  lrStarting = false;
  $("#learn-loading").hidden = true;
  if (!s.items.length) {
    notify(s.short ? `no grounded material: ${s.short}` : "no grounded material in scope", "warn");
    await lrPost("/learn/close", { id: s.id }).catch(() => {});
    return;
  }
  lrGateCount(opts.from || "other");
  lrSession = { ...s, cur: 0, state: "input", answers: {}, verdicts: {}, results: {}, skipped: {}, hintsUsed: {}, shown: {}, conf: null, told: false, teachT: {} };
  if (activeTab !== "learn") showTab("learn");
  lrGo({ view: "session", id: s.id });
}

function lrItem() { return lrSession.items[lrSession.cur]; }

// The eyebrow names the objective and the point; the path is the crumb and
// the hold estimate is the receipt's (ADR-0061 §18).
function lrEyebrow(it) {
  const pt = it.point;
  const parts = [LR_OBJ[it.objective] || `${it.kind}${it.level ? " · " + (LR_LEVEL[it.level] || it.level) : ""}`];
  if (pt?.i) parts.push(lrA(pt.kind || "point") + (pt.heading ? `, under ${pt.heading}` : ""));
  else if (pt) parts.push("a relation");
  if (it.stale) parts.push("edited since last practised");
  return parts.join(" · ");
}

// What a verdict says about the point, as a mark and one word (ADR-0061 §21).
function lrVerdictSay(it, v, s) {
  if (v.skip) return { mark: "warn", word: "Source mismatch", sub: "not scored" };
  if (v.needs_self_grade || v.grader === "self") return { mark: "open", word: "Rate it yourself", sub: "" };
  if (v.told) return { mark: "open", word: "Told", sub: "the point stays open" };
  const used = (s.shown[it.i] || []).length;
  if (v.grade === "recalled") return used ? { mark: "half", word: "Met with help", sub: "not counted as without help" } : { mark: "full", word: "Met", sub: "without help" };
  if (it.kind === "predict") return { mark: "open", word: "First attempt", sub: "graded as such" };
  if (v.grade === "partial") return { mark: "open", word: "Partly met", sub: "the point stays open" };
  return { mark: "open", word: "Missed", sub: "the point stays open" };
}

function lrRenderSession(body) {
  const s = lrSession;
  if (!s || s.id !== lrRoute.id) {
    // the hash names a session this tab does not hold: the receipt if it closed, else home
    if (!s && lrRoute.id) { lrOpenReceipt(lrRoute.id, { replace: true }); return; }
    lrGo({ view: "home" }, { replace: true });
    return;
  }
  const it = lrItem();
  const o = s.origin || {};
  const crumb = [];
  if (o.kind === "plan") crumb.push(o.label);
  crumb.push(it.pair ? it.pair.map(lrStem).join(" · ") : it.title);
  // the bar carries the place, the strip the progress (ADR-0061 §17)
  lrBar({ crumb, end: true, back: { label: "Leave", title: "the session stays open; Resume brings you back", run: lrLeave } });
  const wrap = lrEl("div", "lr-session");
  const strip = lrEl("div", "lr-strip");
  const segs = lrEl("div", "segs");
  segs.setAttribute("aria-hidden", "true");
  s.items.forEach((x, i) => segs.appendChild(lrEl("i", s.skipped[x.i] ? "skip" : i < s.cur || s.results[x.i] ? "done" : i === s.cur ? "cur" : "")));
  strip.append(segs, lrEl("span", "num", `${s.cur + 1} of ${s.items.length}`));
  strip.lastChild.setAttribute("aria-label", `question ${s.cur + 1} of ${s.items.length}`);
  wrap.appendChild(strip);
  if (s.amnesty && s.cur === 0 && s.state === "input") wrap.appendChild(lrEl("p", "lr-help", s.amnesty));
  if (lrEndAsk) {
    wrap.appendChild(lrEndConfirm());
    const dim = lrEl("div", "lr-sheet dim");
    dim.setAttribute("aria-hidden", "true");
    dim.inert = true;
    dim.append(lrEl("span", "eyebrow", lrEyebrow(it)), lrMd(lrEl("p", "q"), it.q));
    if (s.answers[it.i]) dim.appendChild(lrEl("p", "lr-draft", s.answers[it.i]));
    wrap.appendChild(dim);
    body.appendChild(wrap);
    setTimeout(() => $("#lr-endkeep")?.focus(), 0);
    return;
  }
  // the previous question stays readable (refinement §22), never re-graded
  const prev = s.cur > 0 ? s.items[s.cur - 1] : null;
  if (prev) wrap.appendChild(lrPrevRow(s, prev, s.cur));
  const sheet = lrEl("div", "lr-sheet");
  if (s.mode === "teach" && s.state !== "reveal") {
    lrTeachParts(sheet, s, it);
    wrap.appendChild(sheet);
    body.appendChild(wrap);
    setTimeout(() => $("#lr-answer")?.focus(), 0);
    return;
  }
  const eb = lrEl("div", "lr-eyebrow");
  // a teach-back's item keeps the kind it was built with; the eyebrow says what was asked
  eb.appendChild(lrEl("span", "eyebrow", s.mode === "teach" ? `Explain it · ${lrStem(it.path)}` : lrEyebrow(it)));
  if (window.speechSynthesis) eb.appendChild(lrReadToggle());
  sheet.appendChild(eb);
  // the question keeps its size after the answer (ADR-0061 §18)
  sheet.appendChild(lrMd(lrEl("p", "q"), s.mode === "teach" ? lrTeachAsk(it) : it.q));
  if (s.state === "input") lrInputParts(sheet, s, it);
  else if (s.state === "grading") lrGradingParts(sheet, s, it);
  else if (s.state === "reveal") lrReveal(sheet, it, s.verdicts[it.i]);
  wrap.appendChild(sheet);
  body.appendChild(wrap);
  if (s.state === "input") setTimeout(() => $("#lr-answer")?.focus(), 0);
  if (s.state === "reveal") setTimeout(() => $("#lr-verdict")?.focus(), 0);
  lrSpeakCurrent(s, it);
}

function lrPrevRow(s, prev, n) {
  const det = lrEl("details", "lr-prev");
  det.open = lrPrevOpen;
  det.addEventListener("toggle", () => (lrPrevOpen = det.open));
  const sum = lrEl("summary");
  const v = s.verdicts[prev.i];
  const res = s.results[prev.i];
  // a reload brings back the results and not the verdicts of recorded answers
  let mark = res?.correct ? "full" : "open";
  let say = s.skipped[prev.i] ? "skipped" : res ? (res.correct ? "met" : "missed") : "not recorded";
  if (!s.skipped[prev.i] && v) { const w = lrVerdictSay(prev, v, s); mark = w.mark; say = w.word.toLowerCase(); }
  sum.append(lrEl("span", "chev"), lrEl("span", "k", `${n} · ${prev.point?.kind || prev.kind}`), lrMark(mark, ""),
    lrEl("span", "v", res ? `${say} · ${res.rating_name}, recorded` : say));
  det.appendChild(sum);
  const d = lrEl("div", "in");
  d.appendChild(lrMd(lrEl("p", "q"), prev.q));
  if (s.answers[prev.i]) d.appendChild(lrEl("p", "you", s.answers[prev.i]));
  if (v?.feedback_line) d.appendChild(lrEl("p", "fb", v.feedback_line));
  if (res?.escalate) d.appendChild(lrAidBox(prev, res.escalate));
  det.appendChild(d);
  return det;
}

function lrInputParts(sheet, s, it) {
  const shown = s.shown[it.i] || [];
  shown.forEach((h, i) => { const d = lrEl("div", "lr-hint"); d.appendChild(lrEl("b", null, `hint ${i + 1} of 2`)); d.append(h); sheet.appendChild(d); });
  const lab = lrEl("label", "lr-answer");
  lab.appendChild(lrEl("span", "lr-help", "Your answer"));
  const ta = lrEl("textarea", "lr-input" + (it.kind === "cloze" ? " short" : ""));
  ta.id = "lr-answer";
  ta.placeholder = it.kind === "cloze" ? "the missing term" : "your answer, in your own words";
  ta.value = s.answers[it.i] || "";
  ta.addEventListener("input", () => (s.answers[it.i] = ta.value));
  const inwrap = lrEl("div", "lr-inwrap");
  inwrap.appendChild(ta);
  if (typeof attachMic === "function") inwrap.appendChild(lrMic(s, it, ta));
  lab.appendChild(inwrap);
  sheet.appendChild(lab);
  // confidence is one segment, guessing to certain, the number only on the chosen one (ADR-0061 §19)
  const conf = lrEl("div", "lr-conf");
  conf.appendChild(lrEl("span", "lr-help", "How likely is this answer to meet the criteria?"));
  const seg = lrEl("div", "lr-seg");
  seg.setAttribute("role", "radiogroup"); seg.setAttribute("aria-label", "confidence");
  for (const [lbl, v] of [...LR_CONF].reverse()) {
    const on = s.conf === v;
    const b = lrBtn(on ? "on" : null, lbl, () => { s.conf = v; s.confSkipped = false; lrRender(); $("#lr-answer")?.focus(); });
    b.setAttribute("role", "radio"); b.setAttribute("aria-checked", on ? "true" : "false");
    b.title = String(v);
    if (on) b.appendChild(lrEl("span", "num", " " + v));
    seg.appendChild(b);
  }
  conf.appendChild(seg);
  const skipc = lrBtn("lr-q" + (s.confSkipped ? " on" : ""), "skip", () => { s.conf = null; s.confSkipped = true; lrRender(); $("#lr-answer")?.focus(); });
  skipc.setAttribute("aria-label", "skip confidence");
  skipc.setAttribute("aria-pressed", s.confSkipped ? "true" : "false");
  conf.appendChild(skipc);
  sheet.appendChild(conf);
  const actions = lrEl("div", "lr-actions");
  const left = 2 - shown.length;
  const hint = lrKey(lrBtn("lr-q", "Hint", lrHint), "H", "h");
  hint.title = `${left} left`;
  hint.disabled = left <= 0 || !(it.hints || []).length;
  const skip = lrBtn("lr-q", "Skip question", lrSkip);
  skip.title = "not a wrong answer: the point stays open and nothing is recorded";
  const ready = s.conf !== null || s.confSkipped;
  actions.append(hint, lrBtn("lr-q", "Just tell me", () => lrSubmit(true)), skip, lrEl("span", "sp"));
  if (!ready) actions.appendChild(lrEl("span", "lr-help", "pick a confidence, or skip it, to submit"));
  const submit = lrKey(lrBtn("lr-send" + (ready ? "" : " off"), "Submit", () => lrSubmit(false)), "⏎", "Enter");
  submit.id = "lr-submit";
  submit.setAttribute("aria-disabled", ready ? "false" : "true");
  actions.appendChild(submit);
  sheet.appendChild(actions);
}

// --- Explain it: the teach-back (ADR-0119, suggestion 2) ------------------------
// You explain the note with the book closed; a student who sees only its
// title asks four times (grade.TEACH_TURNS); the summary then reads your
// words against the note, and the reveal and its rating follow as for any
// question. Answering by voice is the session's own mic.
const LR_TEACH_TURNS = 4;
// the note's name, not items.title_of: that reads the first heading, which in
// this vault is often a section ("Modellamento cognitivo" for L'apprendimento)
const lrTeachAsk = (it) => `Explain ${lrStem(it.path)} in your own words.`;
function lrTeachParts(sheet, s, it) {
  const T = s.teachT[it.i] || (s.teachT[it.i] = { turn: 0, transcript: [] });
  const eb = lrEl("div", "lr-eyebrow");
  eb.appendChild(lrEl("span", "eyebrow", T.turn ? `A student who has not read your note asks · ${T.turn} of ${LR_TEACH_TURNS}` : "Explain it · with the book closed"));
  sheet.appendChild(eb);
  sheet.appendChild(lrEl("p", "q", lrTeachAsk(it)));
  if (T.transcript.length) {
    const log = lrEl("div", "lr-teachlog");
    for (const x of T.transcript) log.appendChild(lrEl("p", x.role === "reader" ? "you" : "ask", x.text));
    sheet.appendChild(log);
  }
  const lab = lrEl("label", "lr-answer");
  lab.appendChild(lrEl("span", "lr-help", T.turn ? "Your answer to the student" : "What it is, how it works, why it matters"));
  const ta = lrEl("textarea", "lr-input");
  ta.id = "lr-answer";
  ta.rows = T.turn ? 3 : 6;
  ta.placeholder = T.turn ? "answer in your own words" : "your explanation, as you would give it to someone who has not read the note";
  ta.value = s.answers[it.i] || "";
  ta.disabled = !!s.teachBusy;
  ta.addEventListener("input", () => (s.answers[it.i] = ta.value));
  const inwrap = lrEl("div", "lr-inwrap");
  inwrap.appendChild(ta);
  if (typeof attachMic === "function") inwrap.appendChild(lrMic(s, it, ta));
  lab.appendChild(inwrap);
  sheet.appendChild(lab);
  const actions = lrEl("div", "lr-actions");
  actions.appendChild(lrEl("span", "sp"));
  if (s.teachBusy) actions.appendChild(lrEl("span", "lr-help", T.turn >= LR_TEACH_TURNS ? "Checking your explanation against the note…" : "The student is reading what you said…"));
  const send = lrKey(lrBtn("lr-send" + (s.teachBusy ? " off" : ""), T.turn >= LR_TEACH_TURNS ? "Finish" : "Send", lrTeachSend), "⏎", "Enter");
  send.id = "lr-submit";
  actions.appendChild(send);
  sheet.appendChild(actions);
}

async function lrTeachSend() {
  const s = lrSession, it = lrItem();
  if (!s || s.mode !== "teach" || s.teachBusy || s.state !== "input") return;
  if (typeof micBusy === "function" && micBusy("lr-answer")) { notify("the dictation is still running; stop it, then send", "warn"); return; }
  const reply = (s.answers[it.i] || "").trim();
  if (!reply) { notify("say it in a sentence or two first", "warn"); return; }
  const T = s.teachT[it.i] || (s.teachT[it.i] = { turn: 0, transcript: [] });
  s.teachBusy = true;
  lrRender();
  let r;
  try { r = await lrPost("/learn/teach", { id: s.id, i: it.i, reply }); } catch (e) {
    s.teachBusy = false;
    if (!lrGone(e)) { notify("couldn't send: " + e.message); lrRender(); }
    return;
  }
  s.teachBusy = false;
  T.transcript.push({ role: "reader", text: reply });
  s.answers[it.i] = "";
  if (r.done) {
    // the reveal compares all you said, not the last answer
    s.answers[it.i] = T.transcript.filter((x) => x.role === "reader").map((x) => x.text).join("\n");
    s.verdicts[it.i] = r;
    s.state = "reveal";
  } else {
    T.transcript.push({ role: "student", text: r.question });
    T.turn = r.turn;
  }
  lrRender();
}

// What the answer was given with, said once on the line that shows it.
function lrSaidWith(s, it) {
  const bits = [];
  if (s.conf !== null && s.conf !== undefined) bits.push(LR_CONF.find((c) => c[1] === s.conf)?.[0] || String(s.conf));
  const used = (s.shown[it.i] || []).length;
  if (used) bits.push(`${lrPlural(used, "hint")} used`);
  return bits;
}

// Checking says what it waits for (ADR-0061 §20).
function lrGradingParts(sheet, s, it) {
  const kv = lrEl("dl", "lr-kv");
  kv.append(lrEl("dt", null, "you wrote"), lrEl("dd", "lr-said", s.told ? "(answer requested)" : s.answers[it.i] || "(no answer)"));
  kv.append(lrEl("dt", null, "confidence"), lrEl("dd", null, s.conf !== null ? `${LR_CONF.find((c) => c[1] === s.conf)?.[0]} · ${s.conf}` : "skipped"));
  const used = (s.shown[it.i] || []).length;
  if (used) kv.append(lrEl("dt", null, "hints"), lrEl("dd", null, `${used} used`));
  sheet.appendChild(kv);
  const g = lrEl("div", "lr-check");
  g.setAttribute("aria-live", "polite"); g.setAttribute("aria-busy", "true");
  const src = it.source || {};
  const where = src.leaf && src.leaf !== it.path ? lrStem(src.leaf) : "the note";
  g.appendChild(lrEl("span", "t", `Checking against ${where}${src.line ? `, line ${src.line}` : ""}`));
  // the bar runs to the self-rating fallback; a re-render resumes it where it was
  const bar = lrEl("div", "lr-wait");
  const fill = lrEl("i");
  fill.style.animationDuration = LR_GRADE_WAIT_MS + "ms";
  fill.style.animationDelay = -Math.min(LR_GRADE_WAIT_MS, Date.now() - (s.gradeStart || Date.now())) + "ms";
  bar.appendChild(fill);
  g.append(bar, lrEl("p", "lr-help", "After 20 seconds without a verdict, the claim, the source and the criterion appear and you rate it yourself."));
  sheet.appendChild(g);
}

// The microphone on the answer box. The take outlives this button (every
// render rebuilds it), so the text lands through `insert`, which finds the
// box the reader is looking at now, or keeps the words on the question they
// were said for. A dictated answer is marked voice, however much is typed
// around it: the grader reads it as a transcript (ADR-0049).
function lrMic(s, it, ta) {
  const b = lrEl("button", "mic lr-mic");
  b.type = "button";
  b.title = "say your answer · click again to stop";
  b.setAttribute("aria-label", "say your answer");
  b.innerHTML = '<svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>';
  const key = it.i;
  attachMic(ta, b, {
    maxMs: LR_MIC_MAX_MS,
    insert: (text) => {
      (s.voiced ||= {})[key] = true;
      const box = document.getElementById("lr-answer");
      if (box && lrSession === s && lrItem()?.i === key && s.state === "input") insertAtCursor(box, text);
      else s.answers[key] = (s.answers[key] ? s.answers[key].replace(/\s*$/, " ") : "") + text;
    },
  });
  return b;
}

// Read aloud is an icon, its label in aria-label and title (ADR-0061 §18).
function lrReadToggle() {
  const b = lrEl("button", "lr-q lr-read" + (lrReadAloud ? " on" : ""));
  b.type = "button";
  b.setAttribute("aria-pressed", String(lrReadAloud));
  b.setAttribute("aria-label", "read aloud");
  b.title = "read each question and each verdict aloud, with the browser's voices";
  b.innerHTML = '<svg aria-hidden="true" focusable="false" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z"></path><path d="M15.5 8.5a5 5 0 0 1 0 7"></path><path d="M19 5a10 10 0 0 1 0 14"></path></svg>';
  b.addEventListener("click", () => {
    lrReadAloud = !lrReadAloud;
    try { localStorage.setItem(LR_READ_KEY, lrReadAloud ? "1" : "0"); } catch { /* a preference for this browser, not a record */ }
    if (!lrReadAloud) hush();
    lrSpoken = new Set(); // switching it on reads what is on screen now
    lrRender();
  });
  return b;
}

// The question when it is asked, the feedback line when it lands, each once.
function lrSpeakCurrent(s, it) {
  if (!lrReadAloud || lrEndAsk || typeof speak !== "function") return;
  const v = s.verdicts[it.i];
  const [key, text] = s.state === "input" ? [`${s.id}:q:${it.i}`, lrPlain(it.q)]
    : s.state === "reveal" && v ? [`${s.id}:v:${it.i}`, v.feedback_line] : ["", ""];
  if (!key || !text || lrSpoken.has(key)) return;
  lrSpoken.add(key);
  speak(text);
}

// End session asks with two marked facts, what is recorded and what would be
// lost; Keep going holds the focus and answers Esc (ADR-0061 §24).
function lrEndConfirm() {
  const s = lrSession, it = lrItem();
  const box = lrEl("div", "lr-confirm");
  box.id = "lr-endq";
  box.setAttribute("role", "alertdialog"); box.setAttribute("aria-labelledby", "lr-endq-t");
  const t = lrEl("b", null, "End this session?");
  t.id = "lr-endq-t";
  box.appendChild(t);
  const rec = Object.keys(s.results).length;
  const facts = lrEl("ul", "facts");
  const fact = (mark, text) => { const li = lrEl("li"); li.append(lrMark(mark, ""), text); facts.appendChild(li); };
  if (rec) fact("full", `${lrPlural(rec, "answer")} ${rec === 1 ? "is" : "are"} recorded and stay${rec === 1 ? "s" : ""} in the ledger.`);
  else fact("open", "Nothing is recorded yet.");
  fact("open", s.state === "input" ? `The answer you are typing for question ${s.cur + 1} is lost.` : `The feedback on question ${s.cur + 1} is not recorded yet and would be lost.`);
  box.appendChild(facts);
  box.appendChild(lrEl("p", "lr-help", `To keep it and come back later, leave instead: the session stays open${s.origin?.kind !== "vault" ? ` on ${s.origin.label}` : ""}.`));
  const row = lrEl("div", "row");
  const keep = lrKey(lrBtn("ghost box", "Keep going", () => { lrEndAsk = false; lrRender(); }), "Esc", "Escape");
  keep.id = "lr-endkeep";
  row.append(keep,
    lrBtn("ghost box", "Leave, keep the session", () => { lrEndAsk = false; lrLeave(); }),
    lrEl("span", "sp"),
    lrBtn("lr-send", "End session", () => { lrEndAsk = false; lrClose(); }));
  box.appendChild(row);
  return box;
}

function lrHint() {
  const s = lrSession, it = lrItem();
  const shown = s.shown[it.i] || (s.shown[it.i] = []);
  const next = (it.hints || [])[shown.length];
  if (!next || shown.length >= 2) return;
  shown.push(next);
  s.hintsUsed[it.i] = shown.length;
  lrRender();
}

async function lrSkip() {
  const s = lrSession, it = lrItem();
  if (!s || s.state !== "input") return;
  try { await lrPost("/learn/skip", { id: s.id, i: it.i }); } catch (e) { if (!lrGone(e)) notify("couldn't skip: " + e.message); return; }
  s.skipped[it.i] = true;
  lrNext();
}

async function lrSubmit(told) {
  const s = lrSession, it = lrItem();
  if (!s || s.state !== "input") return;
  if (!told && s.conf === null && !s.confSkipped) { notify("pick a confidence, or skip it", "warn"); return; }
  // Submitting mid-take would grade the answer without the words still being
  // transcribed, and they would then land on a question already graded.
  if (!told && typeof micBusy === "function" && micBusy("lr-answer")) { notify("the dictation is still running; stop it, then submit", "warn"); return; }
  const production = told ? "" : (s.answers[it.i] || "");
  const modality = s.voiced?.[it.i] ? "voice" : "typed";
  s.told = told;
  s.state = "grading";
  s.gradeStart = Date.now();
  lrRender();
  clearTimeout(lrGradeTimer);
  let fallback = false;
  lrGradeTimer = setTimeout(() => { fallback = true; lrSelfGradeFallback(it); }, LR_GRADE_WAIT_MS);
  let v;
  try {
    v = await lrPost("/learn/answer", { id: s.id, i: it.i, production, conf: s.conf, hints: s.hintsUsed[it.i] || 0, told, modality });
  } catch (e) {
    clearTimeout(lrGradeTimer);
    if (fallback) return;
    if (lrGone(e)) return;
    notify("couldn't grade: " + e.message);
    s.state = "input";
    lrRender();
    return;
  }
  clearTimeout(lrGradeTimer);
  if (fallback && s.verdicts[it.i]?.selfRated) return;   // a late verdict never replaces a self-rating already recorded
  s.verdicts[it.i] = v;
  s.state = "reveal";
  lrRender();
}

async function lrSelfGradeFallback(it) {
  const s = lrSession;
  if (!s || s.state !== "grading") return;
  // the evidence, from the server without a model verdict: the answer route
  // falls through to `needs_self_grade` once its own call returns; here we
  // ask it again with force_model off so the claim, span and criterion show now
  try {
    const v = await lrPost("/learn/answer", { id: s.id, i: it.i, production: s.answers[it.i] || "", conf: s.conf, hints: s.hintsUsed[it.i] || 0, told: false, self: true, modality: s.voiced?.[it.i] ? "voice" : "typed" });
    v.needs_self_grade = true;
    s.verdicts[it.i] = v;
    s.state = "reveal";
    lrRender();
  } catch { /* the pending sheet stays; the verdict may still land */ }
}

// Your words beside the note's line, the reference answer marked in it.
function lrCompare(it, v, s, { criterion = false } = {}) {
  const box = lrEl("div", "lr-cmp");
  const you = lrEl("div", "you");
  you.append(lrEl("span", "lab", ["you wrote", ...lrSaidWith(s, it)].join(" · ")),
    lrEl("p", "txt", s.told ? "(answer requested)" : s.answers[it.i] || v.production || "(no answer)"));
  const note = lrEl("div", "note");
  const lab = lrEl("span", "lab");
  const line = it.point?.line || (v.span?.self ? v.span.line : 0);
  lab.append(lrEl("span", null, `the note${line ? `, line ${line}` : ""}${v.span?.self ? " · no source cited" : ""}`),
    lrBtn("lr-link", "open note", () => openNote(it.path)));
  const text = v.span?.self ? v.span.text || v.claim : v.claim || v.answer;
  note.append(lab, lrMarked(lrEl("p", "txt"), text || "(this item rests on a relation, not a line)", v.answer));
  if (v.answer && v.answer !== text && !String(text || "").toLowerCase().includes(String(v.answer).toLowerCase())) {
    const ref = lrEl("p", "ref");
    ref.append(lrEl("span", "lab", "reference answer"), " ", lrMd(lrEl("span"), v.answer));
    note.appendChild(ref);
  }
  box.append(you, note);
  if (criterion && v.criteria) {
    const c = lrEl("div", "crit");
    c.append(lrEl("span", "lab", "criterion"), lrEl("span", null, v.criteria));
    box.appendChild(c);
  }
  return box;
}

function lrReveal(sheet, it, v) {
  const s = lrSession;
  const mismatch = !!v.skip;
  const self = !!v.needs_self_grade || v.grader === "self";
  const last = s.cur + 1 >= s.items.length;
  const say = lrVerdictSay(it, v, s);
  // the verdict, stated once: a mark, a word, the grader's sentence (ADR-0061 §21)
  const vd = lrEl("div", "lr-verdict" + (mismatch ? " warn" : ""));
  vd.id = "lr-verdict"; vd.tabIndex = -1; vd.setAttribute("aria-live", "polite");
  const vh = lrEl("div", "h");
  vh.append(lrMark(say.mark, ""), lrEl("b", null, say.word));
  if (say.sub) vh.appendChild(lrEl("span", "m", "· " + say.sub));
  vd.appendChild(vh);
  const fb = mismatch ? `The note's claim is not supported by the line it cites, so this answer is not scored.${v.note_defect ? " " + v.note_defect : ""}`
    : v.feedback_line || (self ? "No model verdict. Compare your answer with the claim and the source, then rate it." : "");
  if (fb) vd.appendChild(lrEl("p", "fb", fb));
  if (s.mode === "teach" && v.summary && v.summary !== fb) vd.appendChild(lrEl("p", "fb", v.summary));
  sheet.appendChild(vd);
  if (mismatch) {
    // the one amber verdict: the note's claim beside the source's line (ADR-0061 §23)
    const box = lrEl("div", "lr-cmp col");
    const note = lrEl("div");
    const l1 = lrEl("span", "lab");
    l1.append(lrEl("span", null, "the note says"), lrBtn("lr-link", "open note", () => openNote(it.path)));
    note.append(l1, lrEl("p", "txt", v.claim || ""));
    const src = lrEl("div");
    const l2 = lrEl("span", "lab");
    l2.append(lrEl("span", null, v.span?.leaf ? `the source says · ${v.span.leaf}, line ${v.span.line}` : "the source says"));
    if (v.span?.leaf) l2.appendChild(lrBtn("lr-link", "open source", () => openNote(v.span.leaf)));
    src.append(l2, lrEl("p", "txt", v.span?.text || ""));
    const you = lrEl("div", "you");
    you.append(lrEl("span", "lab", "you wrote"), lrEl("p", "txt", s.answers[it.i] || "(no answer)"));
    box.append(note, src, you);
    sheet.appendChild(box);
    sheet.appendChild(lrEl("p", "lr-help", "No rating is written: the schedule, the depth and the calibration of this note do not change."));
    const row = lrEl("div", "lr-actions");
    const flag = lrBtn("ghost box", "Flag in chat", () => prefillChat(`/flag ${shellQuote(it.path)} claim vs ${v.span?.leaf || "source"}:${v.span?.line || ""} · ${v.note_defect || "not supported by the span"}`));
    flag.title = "prefills a chat turn; nothing is sent until you send it";
    const next = lrKey(lrBtn("lr-send", last ? "Finish session" : "Next question", () => lrNext()), "⏎", "Enter");
    next.id = "lr-next";
    row.append(flag, lrEl("span", "sp"), next);
    sheet.appendChild(row);
    return;
  }
  sheet.appendChild(lrCompare(it, v, s, { criterion: self }));
  // the criterion and the grader's notes, on request (ADR-0061 §21)
  if (!self && (v.criteria || v.rubric_notes || v.misconception || (v.span?.leaf && !v.span.self))) {
    lrDisc(sheet, "Why this verdict", "", (box) => {
      const dl = lrEl("dl", "lr-kv");
      const kv = (k, val) => { if (val) dl.append(lrEl("dt", null, k), lrEl("dd", null, val)); };
      kv("criterion", v.criteria);
      kv("notes", v.rubric_notes);
      kv("observation", v.misconception);
      if (v.paraphrase !== null && v.paraphrase !== undefined) kv("wording", v.paraphrase ? "restated in your own words" : "reused the note's wording");
      if (v.span?.leaf && !v.span.self) {
        const dd = lrEl("dd");
        dd.append(v.span.text || "", " ", lrBtn("lr-link", "open source", () => openNote(v.span.leaf)));
        dl.append(lrEl("dt", null, `source, line ${v.span.line}`), dd);
      }
      box.appendChild(dl);
    });
  }
  const rate = lrEl("div", "lr-actions lr-rate");
  const proposed = v.rating;
  const chosen = s.results[it.i]?.rating ?? s.chosen?.[it.i] ?? (self ? null : proposed) ?? null;
  const seg = () => {
    const g = lrEl("div", "lr-seg");
    g.id = "lr-rate-opts"; g.setAttribute("role", "radiogroup"); g.setAttribute("aria-label", "rating");
    for (const [n, name] of Object.entries(LR_RATING)) {
      const on = Number(n) === chosen;
      const b = lrKey(lrBtn(on ? "on" : null, lrCap(name), () => lrChoose(Number(n))), n, n);
      b.setAttribute("role", "radio"); b.setAttribute("aria-checked", on ? "true" : "false");
      g.appendChild(b);
    }
    return g;
  };
  if (proposed && !self) {
    // a proposal with change (review §10), the four only on request
    rate.appendChild(lrEl("span", "lr-help", "rating"));
    if (s.changing && !s.results[it.i]) {
      rate.append(seg(), lrEl("span", "lr-help", "changes the schedule only; the rubric result stays recorded"));
    } else {
      rate.append(lrEl("b", null, lrCap(LR_RATING[chosen])), lrEl("span", "lr-help", chosen === proposed ? "proposed from the verdict" : "chosen by you"));
    }
    if (!s.results[it.i]) {
      const ch = lrBtn("lr-q", "change", () => { s.changing = !s.changing; lrRender(); });
      ch.setAttribute("aria-expanded", s.changing ? "true" : "false");
      rate.appendChild(ch);
    }
  } else {
    // rate it yourself: the four, with their keys (ADR-0061 §22)
    rate.append(lrEl("span", "lr-help", v.told ? "Told: Hard at best." : "your rating"), seg());
  }
  rate.appendChild(lrEl("span", "sp"));
  if (!chosen) rate.appendChild(lrEl("span", "lr-help", "pick a rating"));
  const confirm = lrKey(lrBtn("lr-send" + (chosen ? "" : " off"), last ? "Finish session" : "Next question", () => lrConfirm()), "⏎", "Enter");
  confirm.id = "lr-confirm";
  confirm.setAttribute("aria-disabled", chosen ? "false" : "true");
  confirm.title = s.results[it.i] ? "" : "records the rating shown, then moves on";
  rate.appendChild(confirm);
  if (s.results[it.i]?.momentum) rate.appendChild(lrEl("p", "lr-momentum", s.results[it.i].momentum));
  sheet.appendChild(rate);
  if (s.results[it.i]?.escalate) sheet.appendChild(lrAidBox(it, s.results[it.i].escalate));
}

// The escalate rung (learn.offer): after the rating that makes three misses in
// a row on a note, a worked example written from the note's checked points,
// one click away and written on the click. On request because in ASSISTments'
// randomized experiments the aids students asked for helped (an example
// d +0.24) and the one that came unasked after an error did not (d -0.05,
// 7,046 students): the owner's call of 2026-10-23. Reading it records nothing.
const LR_AID_WHAT = { worked: "A worked example" };
function lrAidBox(it, esc) {
  const s = lrSession;
  const box = lrEl("section", "lr-aid");
  box.id = `lr-aid-${it.i}`;
  box.setAttribute("aria-live", "polite");
  const h = lrEl("div", "h");
  h.append(lrEl("b", null, LR_AID_WHAT[esc.form] || LR_AID_WHAT.worked), lrEl("span", "m", "· " + esc.why));
  box.appendChild(h);
  const got = s.aids?.[it.i];
  if (got?.html) {
    // the chat's prose styles (tables, MathML, code) are scoped to .msg
    const body = lrEl("div", "msg lr-aid-body");
    body.innerHTML = got.html;
    box.appendChild(body);
    renderMermaid(body);
  } else if (got?.error) {
    const p = lrEl("p", "lr-help", `Not written: ${got.error} `);
    p.appendChild(lrBtn("lr-q", "try again", () => { delete s.aids[it.i]; box.replaceWith(lrAidBox(it, esc)); }));
    box.appendChild(p);
  } else if (got) {
    box.appendChild(lrEl("p", "lr-help", `Writing it from ${lrStem(it.path)}…`));
  } else {
    const go = lrBtn("ghost box", "Show a worked example", () => { lrFetchAid(it); box.replaceWith(lrAidBox(it, esc)); });
    go.title = "written from this note's checked points when you ask";
    box.appendChild(go);
  }
  if (esc.prereq) {
    const p = lrEl("p", "lr-help", "The prerequisite you hold least: ");
    p.appendChild(lrBtn("lr-link", esc.prereq_title || lrStem(esc.prereq), () => openNote(esc.prereq)));
    box.appendChild(p);
  }
  box.appendChild(lrEl("p", "lr-help", "Reading it changes nothing in the schedule: the next answer on this note is what counts."));
  return box;
}

async function lrFetchAid(it) {
  const s = lrSession;
  (s.aids || (s.aids = {}))[it.i] = { pending: true };
  try { s.aids[it.i] = await lrPost("/learn/aid", { id: s.id, i: it.i }); }
  catch (e) { if (lrGone(e)) return; s.aids[it.i] = { error: e.message }; }
  // in place, not a render: the reader may be typing the next answer by now
  const box = lrSession === s && document.getElementById(`lr-aid-${it.i}`);
  if (box) box.replaceWith(lrAidBox(it, s.results[it.i].escalate));
}

function lrChoose(n) {
  const s = lrSession, it = lrItem();
  if (s.results[it.i]) return;    // recorded: the ledger line is written once
  (s.chosen || (s.chosen = {}))[it.i] = n;
  lrRender();
}

async function lrConfirm() {
  const s = lrSession, it = lrItem();
  if (s.results[it.i]) return lrNext();
  // Enter again while the rating is in flight would post it twice (the server
  // holds a lock for that too, learn_routes._RATE_LOCK)
  if (s.confirming) return;
  const v = s.verdicts[it.i];
  const self = !!v.needs_self_grade || v.grader === "self";
  const rating = s.chosen?.[it.i] ?? (self ? null : v.rating);
  if (!rating) { notify("pick a rating", "warn"); return; }
  const btn = $("#lr-confirm");
  if (btn) btn.disabled = true;
  s.confirming = true;
  try {
    const r = await lrPost("/learn/rate", { id: s.id, i: it.i, rating });
    s.results[it.i] = r;
    // a placement's walk (ADR-0135 point 3): a step placed on its deepest note
    // drops its second question, always one after the current, so `cur` holds
    if (r.drop?.length) s.items = s.items.filter((x) => !r.drop.includes(x.i));
    // a miss asked again before the end (ADR-0135 point 4), when it was ready at once
    if (r.added?.length) s.items.push(...r.added);
    if (v.needs_self_grade) v.selfRated = true;
  } catch (e) { if (lrGone(e)) return; notify("not recorded: " + e.message); if (btn) btn.disabled = false; return; }
  finally { s.confirming = false; }
  // a milestone line, or the aid a third miss calls for, reads before moving on
  if (s.results[it.i].momentum || s.results[it.i].escalate) { lrRender(); return; }
  lrNext();
}

function lrNext() {
  const s = lrSession;
  s.chosen = {}; s.changing = false; s.conf = null; s.confSkipped = false; s.told = false;
  lrPrevOpen = false;
  if (s.cur + 1 >= s.items.length) return lrMoreOrClose();
  s.cur += 1;
  s.state = "input";
  lrRender();
}

// At the end, a retest generated in the background may have landed since the
// last rating (ADR-0135 point 4): the server's list is read once more, and
// only an item this tab does not have yet keeps the session open.
async function lrMoreOrClose() {
  const s = lrSession;
  try {
    const d = await (await fetch("/learn/session?id=" + encodeURIComponent(s.id))).json();
    const have = new Set(s.items.map((x) => x.i));
    const more = (d.items || []).filter((x) => !have.has(x.i));
    if (more.length && lrSession === s) {
      s.items.push(...more);
      s.cur += 1;
      s.state = "input";
      return lrRender();
    }
  } catch { /* the list could not be read: the session closes as it always did */ }
  return lrClose();
}

// Leaving keeps the session (refinement §9): the plan shows Resume, the
// suggestion row says "In the open session".
function lrLeave() {
  const s = lrSession;
  lrEndAsk = false;
  lrGo(lrOriginRoute(s?.origin));
}

// End session: direct when nothing would be lost, else a persistent choice.
function lrEnd() {
  const s = lrSession;
  if (!s) return;
  if (lrRoute.view !== "session") lrGo({ view: "session", id: s.id }, { replace: true });
  const it = lrItem();
  const draft = s.state === "input" && (s.answers[it?.i] || "").trim();
  const unrecorded = s.state === "reveal" && !s.results[it?.i] && !s.verdicts[it?.i]?.skip;
  if (draft || unrecorded || s.state === "grading") { lrEndAsk = true; lrRender(); return; }
  lrClose();
}

async function lrClose() {
  const s = lrSession;
  if (!s) return;
  clearTimeout(lrGradeTimer);
  let rc;
  try { rc = await lrPost("/learn/close", { id: s.id }); } catch (e) { if (!lrGone(e)) notify("couldn't end the session: " + e.message); return; }
  lrSession = null;
  lrEndAsk = false;
  lrData = null; lrPlan = null; lrNote = null; lrMap = null;
  if (rc.empty) {
    // nothing answered: back where you started, no receipt to read (refinement §9)
    notify("Session ended. Nothing was recorded.", "info");
    await loadLearn(true);
    lrGo(lrOriginRoute(rc.origin), { replace: true });
    return;
  }
  lrReceipt = rc;
  lrGo({ view: "receipt", id: rc.id }, { replace: true });
}

async function lrOpenReceipt(id, { replace = false } = {}) {
  // Back from a receipt reloads under the receipt's hash, which asks for the
  // receipt again; if that answer lands after Back moved on, it must not pull
  // the reader back (review of ADR-0081: Back needed two clicks)
  const from = lrHash(lrRoute);
  let rc;
  try { rc = await (await fetch("/learn/receipt?id=" + encodeURIComponent(id))).json(); } catch { notify("no receipt for that session"); return; }
  if (lrHash(lrRoute) !== from) return;
  lrReceipt = rc;
  if (lrReceipt.detail) { notify(lrReceipt.detail); lrGo({ view: "home" }, { replace: true }); return; }
  lrGo({ view: "receipt", id }, { replace });
}

// --- receipt -----------------------------------------------------------------
const LR_STATUS_WORD = { assisted: "met with help, not counted", missed: "missed", skipped: "skipped", open: "not answered", not_scored: "not scored" };
const LR_COMES_BACK = { missed: "back in Missed", assisted: "back when it fades", skipped: "nothing recorded" };
const lrStatusMark = (st) => (st === "met" ? "full" : st === "assisted" ? "half" : st === "not_scored" ? "warn" : "open");

// The receipt opens on what was missed (ADR-0119, suggestion 3): each open
// point or question with the line it rests on, a way to read it there, and
// how it comes back. Returns the notes to ask again.
function lrMissedList(wrap, r) {
  const byPoint = !!r.points?.length;
  const pts = byPoint ? r.points.filter((p) => p.status !== "met") : [];
  const items = byPoint ? [] : r.items.filter((x) => ["missed", "assisted", "skipped"].includes(x.status));
  const n = pts.length + items.length;
  // a skip is not a miss (ADR-0061): it is listed, and the head does not count it
  const missedN = [...pts, ...items].filter((x) => x.status !== "skipped").length;
  const skippedN = n - missedN;
  const sec = lrEl("section", "lr-missed");
  sec.appendChild(lrEl("h2", null, missedN ? `You missed ${lrPlural(missedN, byPoint ? "point" : "question")}` : "Nothing missed"));
  const t3 = r.tally3;
  const sub = [t3 ? `${t3.met} of ${t3.points} met without help` : "", t3?.assisted ? `${t3.assisted} more with help, not counted` : "",
    skippedN ? `${skippedN} skipped, not counted as wrong` : ""].filter(Boolean).join(" · ");
  if (sub) sec.appendChild(lrEl("p", "lr-help", sub));
  for (const p of pts) {
    const it = r.items.find((x) => x.point?.i === p.i && x.span?.line) || r.items.find((x) => x.point?.i === p.i);
    sec.appendChild(lrMissedRow(p.status, p.heading ? `${p.kind}, under ${p.heading}` : p.kind, r.note, it?.span, lrStem(r.note)));
  }
  // the note's name, not items.title_of's first heading (a section in this vault)
  for (const x of items) sec.appendChild(lrMissedRow(x.status, x.q ? lrPlain(x.q) : lrStem(x.path), x.path, x.span, lrStem(x.path)));
  wrap.appendChild(sec);
  const paths = [...new Set(items.filter((x) => x.status !== "skipped").map((x) => x.path))];
  return { n, paths };
}

function lrMissedRow(status, what, path, span, title) {
  const row = lrEl("div", "lr-mrow");
  const t = lrEl("div", "t");
  t.append(lrMark(lrStatusMark(status), ""), lrEl("span", null, what));
  row.appendChild(t);
  // a claim's line is in its source when the note cites one (item.source.leaf)
  const inSource = !!(span?.leaf && span.leaf !== path);
  const file = inSource ? span.leaf : path;
  const where = [inSource ? lrStem(file) : title, span?.line ? `line ${span.line}` : ""].filter(Boolean).join(", ");
  row.appendChild(lrEl("p", "src", span?.text ? `${where}: «${span.text}»` : where));
  const acts = lrEl("div", "acts");
  const read = lrBtn("lr-link", inSource ? "Read it in the source" : "Read it in the note", () => openNote(file, span?.line));
  read.setAttribute("aria-label", `Read ${inSource ? lrStem(file) : title}${span?.line ? ` at line ${span.line}` : ""}`);
  acts.append(read, lrEl("span", "lr-help", [LR_STATUS_WORD[status], LR_COMES_BACK[status]].filter(Boolean).join(" · ")));
  row.appendChild(acts);
  return row;
}

// The open points named by heading: "limit and step under Tipi di agente".
function lrNamePoints(points) {
  const by = new Map();
  for (const p of points) { const k = p.heading || ""; if (!by.has(k)) by.set(k, []); if (!by.get(k).includes(p.kind)) by.get(k).push(p.kind); }
  return [...by].map(([h, kinds]) => lrAnd(kinds) + (h ? ` under ${h}` : "")).join(", ");
}

// What moved is one row per note and measure, from where it started to where
// it ended; the per-question table keeps every step between (ADR-0061 §27).
function lrNetMoves(moved, all = false) {
  const out = [];
  const by = new Map();
  for (const x of moved || []) {
    const k = `${x.path}|${x.what}|${x.plan || ""}`;
    if (by.has(k)) by.get(k).to = x.to;
    else { const y = { ...x }; by.set(k, y); out.push(y); }
  }
  return all ? out : out.filter((x) => x.from !== x.to);
}

function lrRenderReceipt(body) {
  const r = lrReceipt;
  if (!r || r.id !== lrRoute.id) { lrOpenReceipt(lrRoute.id, { replace: true }); return; }
  const o = r.origin || {};
  // Back lands on the plan it came from, else on Learn, whose Missed row now
  // carries what this receipt established (ADR-0119)
  const backLabel = o.kind === "plan" ? `Back to ${o.label}` : "Back to Learn";
  const goBack = async () => { lrReceipt = null; if (!lrData) await loadLearn(true); lrGo(o.kind === "plan" ? lrOriginRoute(o) : { view: "home" }); };
  const when = r.ts ? lrDate(r.ts) : r.started ? `${lrDate(r.started)}, ${lrTime(r.started)}` : "";
  const dur = r.duration ? ` · ${Math.floor(r.duration / 60)} min ${Math.round(r.duration % 60)} s` : "";
  const crumb = [];
  if (o.kind === "plan") crumb.push(o.label);
  if (r.note) crumb.push(r.items[0]?.title || lrStem(r.note));
  lrBar({ crumb, stamp: `session of ${when}${dur}`, back: { label: backLabel, run: goBack } });
  const wrap = lrEl("div", "lr-receipt");
  const t3 = r.tally3;
  const npts = r.points?.length ? r.points.filter((p) => p.i).length : 0;
  const eyebrow = ["Receipt", `${lrPlural(r.items.length, "question")}${npts ? ` on ${lrPlural(npts, "key point")}` : ""}`];
  if (r.objectives?.length) eyebrow.push(r.objectives.map((x) => LR_OBJ_SHORT[x] || x).join(", "));
  if (r.from_ledger) eyebrow.push("rebuilt from the ledger: recorded events only");
  const again = r.from_ledger ? null : lrBtn("ghost box", `Another session on ${o.kind === "vault" ? "the vault" : o.kind === "note" ? "this note" : o.label}`,
    () => learnStartSession(o.kind === "plan" ? { plan: o.path } : o.kind === "note" ? { note: o.path } : {}));
  wrap.appendChild(lrHead(eyebrow.join(" · "), r.note ? r.items[0]?.title || lrStem(r.note) : o.kind === "plan" ? o.label : "Suggested practice", "", [again]));
  // what was missed first (ADR-0119, suggestion 3), then the way to ask it again
  const missed = lrMissedList(wrap, r);
  if (lrReadAloud && r.established && typeof speak === "function" && !lrSpoken.has(`rc:${r.id}`)) { lrSpoken.add(`rc:${r.id}`); speak(r.established); }
  if (r.next && !r.note) wrap.appendChild(lrEl("p", "lr-help", `${r.next} ${typeof r.due_left === "number" ? r.due_left + " notes stay due; they can wait." : ""}`));
  // the next step is the primary: the targeted questions, naming them (ADR-0061 §26)
  if (r.followup && r.followup.questions) {
    const fu = lrEl("div", "lr-followup");
    const t = lrEl("div", "t");
    t.appendChild(lrEl("b", null, `${lrPlural(r.followup.questions, "targeted question")} on the points left open`));
    const named = lrNamePoints((r.points || []).filter((p) => (r.followup.points || []).includes(p.i)));
    t.appendChild(lrEl("span", "lr-help", [named, "they come back in the next session either way"].filter(Boolean).join(" · ")));
    fu.append(t, lrBtn("lr-q", "Not now", () => fu.remove()),
      lrBtn("lr-send", `Ask them now · about ${r.followup.minutes} min`, () => learnStartSession({ note: r.note, points: r.followup.points, plan: r.plan || undefined })));
    wrap.appendChild(fu);
  } else if (missed.paths.length && !r.from_ledger) {
    // a session over several notes: the notes missed, asked again now; a
    // miss schedules nothing by itself (_reason_sentence), so "tomorrow" as
    // drawn would have been a promise the schedule does not keep
    const fu = lrEl("div", "lr-followup");
    const t = lrEl("div", "t");
    t.appendChild(lrEl("b", null, `${lrPlural(missed.paths.length, "note")} to ask again`));
    t.appendChild(lrEl("span", "lr-help", "or they come back in Missed"));
    fu.append(t, lrBtn("lr-send", "Ask these again", () => learnStartSession({ scope: missed.paths, n: Math.min(24, missed.n), from: "missed" })));
    wrap.appendChild(fu);
  }
  // the figures and the points' marks, on request (ADR-0061 §25, §27)
  if (t3 || r.points?.length) lrDisc(wrap, "The figures", "", (box) => {
    if (t3) {
      const unit = r.points?.length ? "points" : "questions";
      const figs = lrEl("div", "lr-figs");
      const left = Math.max(0, t3.points - t3.met);
      const sub2 = [t3.missed ? `${t3.missed} missed` : "", t3.skipped ? `${t3.skipped} skipped, not counted as wrong` : ""].filter(Boolean).join(" · ");
      figs.append(lrFig(`${t3.met} of ${t3.points}`, `${unit} met without help`, t3.assisted ? `${t3.assisted} more with help, not counted` : ""),
        lrFig(left, `${unit} still open`, sub2));
      const hold = r.note ? lrNetMoves(r.moved, true).find((x) => x.what === "hold") : null;
      if (hold) figs.appendChild(lrFig(hold.to, "hold, estimated", hold.from !== hold.to ? `was ${hold.from}` : ""));
      else figs.appendChild(lrFig(`${t3.with_evidence} of ${t3.objectives}`, "objectives with new evidence"));
      box.appendChild(figs);
    }
    if (r.points?.length) {
      // points are marks grouped by heading (ADR-0061 §27)
      let heading = null;
      let grid = null;
      for (const p of r.points) {
        if (p.heading !== heading || !grid) {
          heading = p.heading;
          if (heading) box.appendChild(lrEl("span", "lr-grp", heading));
          grid = lrEl("div", "lr-pts");
          box.appendChild(grid);
        }
        const name = lrEl("span", "p");
        name.append(lrMark(lrStatusMark(p.status), p.status === "met" ? "met without help" : ""), p.kind);
        grid.append(name, lrEl("span", "k", LR_OBJ_SHORT[p.objective] || p.objective));
      }
    }
  });
  if (r.revisit?.length) {
    const sec = lrEl("section", "lr-sec");
    sec.appendChild(lrEl("h3", null, "What to revisit"));
    for (const x of r.revisit) {
      const d = lrEl("div", "lr-defect");
      const t = lrEl("div", "t");
      t.appendChild(lrEl("b", null, lrStem(x.path)));
      t.append(` · ${x.say}.` + (x.related?.length ? " Related, not the proven cause: " : ""));
      x.related?.forEach((rel, i) => { const a = lrBtn("lr-link", lrStem(rel.path), () => openNote(rel.path)); a.title = rel.why; t.appendChild(a); if (i < x.related.length - 1) t.append(", "); });
      t.appendChild(lrEl("span", "fn", `${x.kind} · said ${x.conf}`));
      d.appendChild(t);
      if (r.plan) {
        const q = lrBtn("ghost box", "queue for next session");
        q.addEventListener("click", async () => {
          const paths = [x.path, ...(x.related || []).map((rel) => rel.path)];
          try { await lrPost("/learn/revisit", { plan: r.plan, paths }); q.textContent = "queued"; q.disabled = true; } catch (e) { notify("not queued: " + e.message); }
        });
        d.appendChild(q);
      }
      sec.appendChild(d);
    }
    wrap.appendChild(sec);
  }
  if (r.defects?.length) {
    const sec = lrEl("section", "lr-sec");
    const h = lrEl("h3");
    h.append(lrMark("warn", ""), "Source mismatch ", lrEl("span", "num", String(r.defects.length)));
    sec.appendChild(h);
    for (const x of r.defects) {
      const d = lrEl("div", "lr-defect");
      const t = lrEl("div", "t");
      t.appendChild(lrEl("b", null, x.title || lrStem(x.path)));
      t.append(` · ${x.note_defect} Not scored.`);
      t.appendChild(lrEl("span", "fn", x.span?.leaf ? `${x.span.leaf} · line ${x.span.line}` : ""));
      d.appendChild(t);
      const f = lrBtn("ghost box", "Flag in chat", () => prefillChat(`/flag ${shellQuote(x.path)} claim vs ${x.span?.leaf || "source"}:${x.span?.line || ""} · ${x.note_defect}`));
      f.title = "prefills a chat turn; nothing is written until you send it";
      d.appendChild(f);
      sec.appendChild(d);
    }
    wrap.appendChild(sec);
  }
  const moved = lrNetMoves(r.moved);
  if (moved.length) {
    const sec = lrEl("section", "lr-sec");
    sec.appendChild(lrEl("h3", null, "What moved"));
    const m = lrEl("div", "lr-moved");
    let lastPath = null;
    for (const x of moved) {
      m.appendChild(lrEl("span", "lbl", x.path === lastPath ? "" : lrStem(x.path)));
      lastPath = x.path;
      m.appendChild(lrEl("span", "lr-help", x.what === "hold" ? "hold, est." : x.what_label ? (LR_OBJ_SHORT[x.what] || x.what) + (x.plan ? ` (${x.plan})` : "") : x.what));
      const ft = lrEl("span", "n num", `${x.from} → `);
      ft.appendChild(lrEl("b", null, x.to));
      m.appendChild(ft);
    }
    sec.appendChild(m);
    wrap.appendChild(sec);
  }
  const discs = lrEl("div", "lr-discs");
  lrDisc(discs, "Questions", r.items.length, (inner) => {
    const t = lrEl("table", "chart tight lr-tbl lr-items");
    const th = lrEl("thead"); const hr = lrEl("tr");
    for (const [l, c] of [[r.note ? "point" : "note", null], ["objective", null], ["result", null], ["said", "num"], ["rating", null], ["hold, est.", "num"], ["next due", null]]) hr.appendChild(lrEl("th", c, l));
    th.appendChild(hr); t.appendChild(th);
    const tb = lrEl("tbody");
    for (const x of r.items) {
      const tr = lrEl("tr");
      tr.appendChild(lrEl("th", null, r.note ? (x.point?.i ? `${x.point.i} · ${x.point.kind || ""}` : "relation") : x.title || lrStem(x.path)));
      tr.appendChild(lrEl("td", null, `${LR_OBJ_SHORT[x.objective] || x.objective_label || ""} · ${x.kind}`));
      const st = x.status || (x.skip ? "not_scored" : x.correct ? "met" : x.correct === false ? "missed" : "open");
      const word = st === "not_scored" ? "not scored" : st === "met" || st === "assisted" ? "met" : st === "missed" ? "not met" : st === "skipped" ? "skipped" : x.answered ? "not rated" : "not answered";
      const marker = x.skip ? "source mismatch" : st === "skipped" ? "not recorded" : x.marker || "";
      tr.appendChild(lrEl("td", null, marker ? `${word} · ${marker}` : word));
      tr.appendChild(lrEl("td", "num", x.conf == null ? "—" : String(x.conf)));
      tr.appendChild(lrEl("td", null, x.rating_name || ""));
      tr.appendChild(lrEl("td", "num", x.S_after ? `${x.S_before ? Math.round(x.S_before) : "new"} → ${Math.round(x.S_after)} d` : x.S_before ? `${Math.round(x.S_before)} d` : ""));
      tr.appendChild(lrEl("td", null, x.next_due ? lrDate(x.next_due) : ""));
      tb.appendChild(tr);
    }
    t.appendChild(tb);
    const sc = lrEl("div", "tscroll");
    sc.appendChild(t);
    inner.appendChild(sc);
  });
  lrDisc(discs, "How this is measured", "", (box) => {
    box.appendChild(lrMarkKey());
    if (r.points?.length) box.appendChild(lrEl("p", null, "A point counts for an objective when its criteria were met without help; an assisted answer stays recorded but does not count. It does not complete the note, and an answer right after feedback does not prove recall at a distance."));
    if (moved.length) box.appendChild(lrEl("p", null, "What moved comes from the recorded answers only; no model call, no claim about you beyond them."));
    box.appendChild(lrEl("p", null, "Result: whether the rubric was met, before help or confidence enter. Rating: what the schedule received; a hint or \"just tell me\" caps it at Hard, an answer at 25 caps it at Good. A skipped question writes nothing."));
    if (r.revisit?.length) box.appendChild(lrEl("p", null, r.plan ? "Queueing writes a revisit line on the plan through the gate; the plan's next session takes those first. Nothing is queued on its own." : "A free session belongs to no plan, so nothing can be queued; a plan's session can."));
  });
  wrap.appendChild(discs);
  wrap.appendChild(lrEl("p", "lr-help", r.recorded
    ? `${lrPlural(r.recorded, "answer")} recorded in the ledger.` + (r.plan_line ? ` The plan's session line was written under ## Sessions in ${r.plan}; it can be reverted.` : r.plan_error ? ` ${r.plan_error}` : "")
    : "Nothing was recorded."));
  body.appendChild(wrap);
}

// --- keys --------------------------------------------------------------------
// On the home views ↑ and ↓ walk the list and the detail follows, replacing
// the history entry; Enter on a row does what a click does (ADR-0061 §8).
// Neither acts while a text field, or anything outside the Learn view, has
// the focus.
function learnKeys(e) {
  if (activeTab !== "learn") return;
  const tgt = e.target;
  const typing = tgt && (tgt.tagName === "TEXTAREA" || tgt.tagName === "INPUT" || tgt.tagName === "SELECT" || tgt.isContentEditable);
  if (["new", "edit"].includes(lrRoute.view)) {
    // a typeahead's Escape closes its list and stops there (lrAskBox, lrRefInput)
    if (e.key === "Escape") { e.preventDefault(); lrCloseDialog(); }
    return;
  }
  if (["home", "plans"].includes(lrRoute.view)) {
    // ↑ ↓ walk the rows; Enter opens the focused one (ADR-0119: the home has
    // no detail pane, so an arrow no longer navigates)
    if (typing || e.altKey || e.ctrlKey || e.metaKey || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    if (tgt?.closest && tgt !== document.body && !tgt.closest("#view-learn")) return;
    const rows = [...document.querySelectorAll("#learn-body [data-lr-row]")].filter((x) => x.offsetParent !== null);
    if (!rows.length) return;
    e.preventDefault();
    const cur = rows.indexOf(document.activeElement);
    rows[cur < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, cur + (e.key === "ArrowDown" ? 1 : -1)))].focus();
    return;
  }
  if (!lrSession || lrRoute.view !== "session") return;
  const s = lrSession;
  if (e.key === "Escape") { e.preventDefault(); if (lrEndAsk) { lrEndAsk = false; lrRender(); } else lrLeave(); return; }
  if (lrEndAsk) return;
  if (s.state === "input") {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); s.mode === "teach" ? lrTeachSend() : lrSubmit(false); return; }
    if (e.key === "h" && !typing && s.mode !== "teach") { e.preventDefault(); lrHint(); }
    return;
  }
  if (s.state === "reveal") {
    if (typing) return;
    if (e.key === "Enter") { e.preventDefault(); $("#lr-confirm") ? lrConfirm() : lrNext(); return; }
    if (e.key === "n") { e.preventDefault(); lrItem() && (s.results[lrItem().i] || s.verdicts[lrItem().i]?.skip) ? lrNext() : notify("confirm the rating first", "warn"); return; }
    if (["1", "2", "3", "4"].includes(e.key)) { e.preventDefault(); s.changing = true; lrChoose(Number(e.key)); }
  }
}
document.addEventListener("keydown", learnKeys);

// an unsubmitted answer or an unrecorded reveal is the only thing a reload
// loses (§5.1); recorded ratings are already in the ledger and the session
// itself is re-attached on the next load
window.addEventListener("beforeunload", (e) => {
  if (lrSession && lrRoute.view === "session" && (lrSession.answers[lrItem()?.i] || lrSession.state === "reveal") && !lrSession.results[lrItem()?.i]) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// --- a step's reading (ADR-0132) ---------------------------------------------
// One explanation per plan step, its notes grouped by concept and each section
// after the ones it needs. A section stays closed until the reader guesses
// what it will say (ungraded, "I don't know" counts), opens with the answering
// sentence marked beside the guess, and ends on the reader's own words; the
// practice that follows asks with the reading closed. What the reader writes
// is kept outside the vault and outside the answer ledger.
let lrRead = null;   // {key, data, view: the section shown (null: done), drafts, polling}
const LR_UNHELD = "(not in the notes)";
const LR_READ_PARTS = [["definition", "Definition"], ["mechanism", "Mechanism"],
  ["example", "Example · worked by Silica, not from the notes"], ["contrast", "Do not confuse"]];
const lrReadKey = (r) => r.plan + "#" + r.step;

async function lrFetchRead(r) {
  const key = lrReadKey(r);
  try {
    const d = await (await fetch(`/learn/reading?plan=${encodeURIComponent(r.plan)}&step=${r.step}`)).json();
    if (d.detail) throw new Error(d.detail);
    const prev = lrRead && lrRead.key === key ? lrRead : null;
    lrRead = { key, data: d, view: prev && prev.data?.exists ? prev.view : undefined, drafts: prev?.drafts || {}, polling: !!prev?.polling };
  } catch (e) { lrRead = { key, error: String(e.message || e) }; }
  if (lrRoute.view === "read" && lrReadKey(lrRoute) === key) lrRender();
}

function lrReadPoll(r) {
  if (!lrRead || lrRead.polling) return;
  lrRead.polling = true;
  const tick = async () => {
    if (lrRoute.view !== "read" || !lrRead || lrReadKey(lrRoute) !== lrRead.key) { if (lrRead) lrRead.polling = false; return; }
    await lrFetchRead(r);
    const j = lrRead?.data?.job;
    if (j && j.state === "running") { setTimeout(tick, 1500); return; }
    if (lrRead) lrRead.polling = false;
    if (j?.state === "error") notify("couldn't write the reading: " + j.error);
    lrRender();
  };
  setTimeout(tick, 1200);
}

// the first section not finished: its guess not given (when it has one), or no own words after it
function lrReadNext(d) {
  const a = d.answers || {};
  for (const s of d.sections) {
    const x = a[String(s.n)] || {};
    if ((s.guess && !x.guess) || !x.own) return s.n;
  }
  return null;
}

async function lrReadWrite(r) {
  try { lrRead.data.job = await lrPost("/learn/reading/write", { plan: r.plan, step: r.step }); }
  catch (e) { notify("couldn't start writing the reading: " + e.message); return; }
  lrRead.view = undefined;
  lrRender();
}

async function lrReadAnswer(r, s, kind, text, unknown) {
  let row;
  try { row = await lrPost("/learn/reading/answer", { plan: r.plan, step: r.step, section: s.n, kind, text, unknown }); }
  catch (e) { notify("couldn't keep that: " + e.message); return; }
  const a = lrRead.data.answers || (lrRead.data.answers = {});
  (a[String(s.n)] ||= {})[kind] = row;
  if (kind === "own") lrRead.view = lrReadNext(lrRead.data);
  lrRender();
  document.querySelector(".lr-read")?.scrollIntoView({ block: "start" });
}

function lrRenderRead(body) {
  const r = lrRoute;
  const key = lrReadKey(r);
  const back = { label: "Plan", run: () => lrGo({ view: "plan", plan: r.plan }) };
  if (!lrRead || lrRead.key !== key) {
    lrBar({ crumb: ["Reading"], back });
    body.appendChild(lrEl("p", "lr-help", "Reading the step."));
    lrFetchRead(r);
    return;
  }
  if (lrRead.error) {
    lrBar({ crumb: ["Reading"], back });
    body.appendChild(lrEl("p", "lr-help", "That step is not in the plan any more."));
    return;
  }
  const d = lrRead.data;
  lrBar({ crumb: [d.plan_name, d.label], back });
  const one = lrEl("section", "lr-one lr-read");
  body.appendChild(one);
  const meta = d.exists
    ? `${lrPlural(d.sections.length, "section")} · ${lrPlural(d.notes.length, "note")} · about ${d.minutes} min of reading`
    : lrPlural(d.notes.length, "note");
  // Rewrite is one revertible overwrite; a running job hides it
  const rewrite = d.exists && !(d.job && d.job.state === "running") ? lrBtn("ghost box", "Rewrite", () => lrReadWrite(r)) : null;
  if (rewrite) rewrite.title = "write this reading again from the step's notes; the reader's words start over";
  one.appendChild(lrHead(`Read · step ${d.step + 1} of ${d.steps}${d.hours ? ` · ${d.hours} h in the syllabus` : ""}`, d.label, meta, [rewrite]));
  const job = d.job;
  if (job && job.state === "running") {
    const p = lrEl("div", "lr-read-card");
    p.setAttribute("role", "status");
    p.append(lrEl("p", null, job.total ? `Writing section ${Math.min(job.done + 1, job.total)} of ${job.total}.` : "Grouping the notes into sections, prerequisites first."),
      lrEl("p", "lr-help", "One call orders the step, then one call a section. You can leave and come back."));
    one.appendChild(p);
    lrReadPoll(r);
    return;
  }
  if (!d.exists) {
    const p = lrEl("div", "lr-read-card");
    p.append(lrEl("p", null, `No reading yet for this step. Silica groups its ${lrPlural(d.notes.length, "note")} by concept, puts each section after the ones it needs, and writes each section from the notes, marking any sentence they do not hold.`));
    const row = lrEl("div", "lr-read-actions");
    row.appendChild(lrBtn("lr-send", "Write the reading", () => lrReadWrite(r)));
    p.appendChild(row);
    if (job?.state === "error") p.appendChild(lrEl("p", "lr-help", "The last attempt failed: " + job.error));
    one.appendChild(p);
    return;
  }
  if (d.stale) {
    const s = lrEl("div", "lr-read-stale");
    s.append(lrEl("span", null, "The step's notes changed since this reading was written."), lrBtn("lr-link act", "Rewrite", () => lrReadWrite(r)));
    one.appendChild(s);
  }
  one.appendChild(lrEl("p", "lr-read-test", `After you read, ${lrPlural(d.questions, "question")} on these notes, with the reading closed.`));
  const next = lrReadNext(d);
  // a note's Study tools open the reading again on that note, whatever was shown before
  if (r.path && lrRead.forPath !== r.path) { lrRead.forPath = r.path; lrRead.view = undefined; }
  if (lrRead.view === undefined) {
    // opened from a note's Study tools: that note's section, once the reader has reached it
    const want = r.path ? d.sections.find((s) => s.notes.includes(lrStem(r.path))) : null;
    lrRead.view = want && (next === null || want.n <= next) ? want.n : next;
  }
  const grid = lrEl("div", "lr-read-grid");
  grid.append(lrReadToc(d, next), lrReadMain(d, r));
  one.appendChild(grid);
}

function lrReadToc(d, next) {
  const ol = lrEl("ol", "lr-read-toc");
  ol.setAttribute("aria-label", d.order === "prerequisites" ? "sections, each after the ones it needs" : "sections, in the lectures' order");
  ol.appendChild(lrEl("li", "lr-read-toc-h", d.order === "prerequisites" ? "Sections · by prerequisite" : "Sections · lecture order"));
  for (const s of d.sections) {
    const done = next === null || s.n < next;
    const li = lrEl("li", "lr-read-toc-i" + (s.n === lrRead.view ? " cur" : "") + (done ? " done" : ""));
    const b = lrBtn("lr-read-toc-b");
    b.disabled = !(done || s.n === next);
    if (s.n === lrRead.view) b.setAttribute("aria-current", "step");
    const txt = lrEl("span", "t");
    txt.append(lrEl("span", "tt", s.title), lrEl("span", "m", s.notes.join(", ")));
    if (s.needed_by?.length) txt.appendChild(lrEl("span", "lr-read-moved", `moved up: needed by ${s.needed_by.join(", ")}`));
    b.append(lrEl("span", "dot", done ? "✓" : String(s.n)), txt);
    b.addEventListener("click", () => { lrRead.view = s.n; lrRender(); });
    li.appendChild(b);
    ol.appendChild(li);
  }
  return ol;
}

function lrReadMain(d, r) {
  const col = lrEl("div", "lr-read-main");
  if (lrRead.view === null) { lrReadDone(col, d, r); return col; }
  const s = d.sections.find((x) => x.n === lrRead.view) || d.sections[0];
  const a = (d.answers || {})[String(s.n)] || {};
  if (s.guess && !a.guess) lrReadGuess(col, d, s, r);
  else lrReadSection(col, d, s, a, r);
  return col;
}

function lrReadGuess(col, d, s, r) {
  const card = lrEl("section", "lr-read-card");
  card.setAttribute("aria-label", "before you read");
  card.append(lrEl("span", "eyebrow", `Section ${s.n} of ${d.sections.length} · before you read, guess`), lrEl("h3", null, s.title));
  card.appendChild(lrMd(lrEl("p", "lr-read-q"), s.guess));
  const id = "lr-guess-" + s.n;
  const lab = lrEl("label", "lr-help", "Your guess, one line is enough");
  lab.htmlFor = id;
  const ta = lrEl("textarea", "lr-input lr-read-ta");
  ta.id = id;
  ta.rows = 3;
  ta.placeholder = "what you think, before reading";
  ta.value = lrRead.drafts["g" + s.n] || "";
  const open = lrBtn("lr-send", "Open the section", () => lrReadAnswer(r, s, "guess", ta.value.trim(), false));
  open.disabled = !ta.value.trim();
  ta.addEventListener("input", () => { lrRead.drafts["g" + s.n] = ta.value; open.disabled = !ta.value.trim(); });
  const row = lrEl("div", "lr-read-actions");
  row.append(open, lrBtn("ghost box", "I don't know", () => lrReadAnswer(r, s, "guess", "", true)),
    lrEl("span", "lr-help", "Not graded. Your guess stays next to the answer."));
  card.append(lab, ta, row);
  col.appendChild(card);
  setTimeout(() => ta.focus({ preventScroll: true }), 0);
}

// a part's text: a paragraph a line, the answering sentence marked, a sentence
// the notes do not hold set apart by its mark
function lrReadText(text) {
  const box = lrEl("div", "lr-read-text");
  // a `$$` block over three lines becomes one line, the only form lrMd draws
  const flat = String(text || "").replace(/\$\$\s*\n([\s\S]*?)\n\s*\$\$/g, (_m, tex) => "$$" + tex.split("\n").map((x) => x.trim()).join(" ") + "$$");
  for (const line of flat.split("\n")) {
    if (!line.trim()) continue;
    const p = lrEl("p");
    line.split(/==(.+?)==/).forEach((t, i) => {
      if (t) p.appendChild(lrMd(lrEl(i % 2 ? "mark" : "span", i % 2 ? "lr-ans" : null), t));
    });
    for (const em of p.querySelectorAll("em")) if (em.textContent.trim() === LR_UNHELD) em.className = "lr-unheld";
    box.appendChild(p);
  }
  return box;
}

function lrReadSection(col, d, s, a, r) {
  const head = lrEl("div", "lr-read-shead");
  head.append(lrEl("span", "eyebrow", `Section ${s.n} of ${d.sections.length}`), lrEl("h3", null, s.title));
  const from = lrEl("p", "lr-help");
  from.append("From ");
  s.notes.forEach((t, i) => {
    if (i) from.append(", ");
    const path = d.notes.find((x) => lrStem(x) === t) || t;
    from.appendChild(lrBtn("lr-link", t, () => openNote(path)));
  });
  head.appendChild(from);
  if (s.needed_by?.length) head.appendChild(lrEl("p", "lr-help lr-read-mv", `Moved up: needed by ${s.needed_by.join(", ")}.`));
  col.appendChild(head);
  if (a.guess) {
    const g = lrEl("div", "lr-read-yours");
    g.append(lrEl("span", "k", "Your guess"), lrEl("span", "v", a.guess.unknown ? "I don't know" : a.guess.text));
    col.appendChild(g);
  }
  for (const [k, name] of LR_READ_PARTS) {
    if (!s[k]) continue;
    const part = lrEl("div", "lr-read-part");
    part.append(lrEl("div", "lr-read-label", name), lrReadText(s[k]));
    col.appendChild(part);
  }
  const own = lrEl("section", "lr-read-card lr-read-own");
  own.setAttribute("aria-label", "in your own words");
  own.appendChild(lrEl("span", "eyebrow", "In your own words, before the next section"));
  if (s.own) own.appendChild(lrMd(lrEl("p", "lr-read-q"), s.own));
  const id = "lr-own-" + s.n;
  const lab = lrEl("label", "lr-help", "Your explanation");
  lab.htmlFor = id;
  const ta = lrEl("textarea", "lr-input lr-read-ta");
  ta.id = id;
  ta.rows = 3;
  ta.placeholder = "as you would say it to someone who has not read it";
  ta.value = lrRead.drafts["o" + s.n] ?? (a.own?.text || "");
  ta.addEventListener("input", () => { lrRead.drafts["o" + s.n] = ta.value; });
  const last = s.n === d.sections[d.sections.length - 1].n;
  const row = lrEl("div", "lr-read-actions");
  row.append(lrBtn("lr-send", last ? "Finish the reading" : "Next section", () => lrReadAnswer(r, s, "own", ta.value.trim(), false)),
    lrEl("span", "lr-help", "Not graded. Kept with the section."));
  own.append(lab, ta, row);
  col.appendChild(own);
}

function lrReadDone(col, d, r) {
  const card = lrEl("section", "lr-read-card");
  card.append(lrEl("span", "eyebrow", "Reading done"), lrEl("h3", null, `Now ${lrPlural(d.questions, "question")}, with the reading closed`),
    lrEl("p", "lr-help", "The step's notes take turns, and each answer is checked against its note. Over the next days they come back as questions, without reading again."));
  const row = lrEl("div", "lr-read-actions");
  const go = lrBtn("lr-send", "Start the practice", () => learnStartSession({ plan: r.plan, step: r.step, from: "reading" }));
  go.disabled = !!lrSession;
  row.append(go, lrBtn("ghost box", "Read again from section 1", () => { lrRead.view = d.sections[0].n; lrRender(); }));
  if (lrSession) row.appendChild(lrEl("span", "lr-help", "A session is open; end it or resume it first."));
  card.appendChild(row);
  col.appendChild(card);
}
