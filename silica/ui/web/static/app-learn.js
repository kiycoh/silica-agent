// --- learn tab ---------------------------------------------------------------
// The Learn tab (learn-suite §5, refined 2026-09-07 per docs/design/learn/
// ui-ux-refinement.md, rethought by ADR-0081): it opens on Next, one queue in
// four groups where every row says why it is up; Plans holds the list and the
// composer; Map is what you know, a mark per note. A stable list on the left
// and one detail on the right; one session, its receipt. Everything on screen
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
let lrSeg = "next";           // next | plans | map: the left pane (ADR-0081)
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
const lrOriginRoute = (o) => (o?.kind === "plan" ? { view: "plan", plan: o.path } : o?.kind === "note" ? { view: "note", path: o.path } : { view: "home" });

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
const LR_MARK_SAY = { open: "still to verify", half: "met with help, not counted", full: "met without help", warn: "caution" };
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

// A question's markdown, inline only: bold, italics, code, and a cloze's
// blank drawn as a gap. The text is escaped first, so the only tags in the
// result are the four written here.
function lrMd(el, text) {
  const code = [];
  el.innerHTML = String(text || "")
    .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]))
    .replace(/`([^`]+)`/g, (_m, c) => `\u0000${code.push(c) - 1}\u0000`)
    .replace(/_{3,}/g, '<span class="lr-gap" role="img" aria-label="blank"></span>')
    .replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*\n]+?)\*/g, "<em>$1</em>")
    .replace(/\u0000(\d+)\u0000/g, (_m, i) => `<code>${code[i]}</code>`);
  return el;
}
const lrPlain = (text) => String(text || "").replace(/_{3,}/g, " blank ").replace(/\*\*|`|\*/g, "");

// The note's line with the reference answer marked in it, built from text
// nodes so a line never becomes markup.
function lrMarked(el, text, term) {
  const t = String(text || "");
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
  if (lrRoute.view === "home" && !lrPlansError) {
    // entering: the last selected plan, else the only plan, else the most
    // recently used one, else the first row of Next; never a session
    // started for you (refinement §4)
    const active = (lrData.plans || []).filter((p) => p.status !== "completed");
    let last = null;
    try { last = localStorage.getItem(LR_LAST_PLAN); } catch { /* private window */ }
    const pick = active.find((p) => p.path === last) || (active.length === 1 ? active[0] : [...active].sort((a, b) => String(b.last || "").localeCompare(String(a.last || "")))[0]);
    const n = lrData.next || {};
    const first = (n.missed || [])[0] || (n.due || [])[0] || (n.never || [])[0];
    if (pick) { lrRoute = { view: "plan", plan: pick.path, path: "", id: "" }; lrSeg = "next"; }
    else if (first) lrRoute = { view: "note", plan: "", path: first.path, id: "" };
  }
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
    lrSession = { ...s, cur: cur < 0 ? s.items.length - 1 : cur, answers: {}, verdicts, results, skipped, hintsUsed: {}, shown: {}, conf: null, told: false, state: "input" };
    const it = lrSession.items[lrSession.cur];
    if (it && verdicts[it.i]) { lrSession.state = "reveal"; lrSession.conf = verdicts[it.i].conf ?? null; lrSession.told = !!verdicts[it.i].told; }
  } catch { /* the session is gone: the home view reads as usual */ }
}

// --- the bar -----------------------------------------------------------------
// The bar carries the place: on the home views the three segments and the
// stamp of the segment's base, on a session or a receipt the crumb; a
// return, End session.
function lrBar({ crumb = [], stamp = "", stampTitle = "", back = null, end = false, segs = false } = {}) {
  const c = $("#learn-crumb");
  c.innerHTML = "";
  if (segs) {
    const nav = lrEl("nav", "lr-seg lr-segs");
    nav.setAttribute("aria-label", "Learn");
    for (const [k, label, to] of [["next", "Next", { view: "home" }], ["plans", "Plans", { view: "plans" }], ["map", "Map", { view: "map" }]]) {
      // a selected plan stays selected between Next and Plans: both list it
      const b = lrBtn(lrSeg === k ? "on" : null, label, () => (lrRoute.view === "plan" && k !== "map" ? (lrSeg = k, lrRender()) : lrGo(to)));
      if (lrSeg === k) b.setAttribute("aria-current", "page");
      nav.appendChild(b);
    }
    c.appendChild(nav);
  }
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

// Each segment's stamp names its own base (ADR-0081 §4): Next is the vault's,
// with the amnesty sentence as its title; Plans counts plans; Map counts notes.
function lrVaultStamp() {
  const d = lrData || {};
  if (!d.total) return ["", ""];
  if (lrSeg === "plans") { const n = (d.plans || []).filter((p) => p.status !== "completed").length; return [n ? lrPlural(n, "plan") : "", ""]; }
  if (lrSeg === "map") return [d.measured ? `${nfmt(d.measured)} of ${nfmt(d.total)} notes practised` : `${nfmt(d.total)} notes, none practised yet`, ""];
  const st = d.stamp;
  if (!st || !st.waiting) return [d.measured ? `${nfmt(d.measured)} of ${nfmt(d.total)} notes practised` : "", ""];
  return [`${nfmt(st.waiting)} waiting` + (st.more ? ` · ${st.more} more by ${lrDate(st.by)}` : ""), st.title || ""];
}

function lrRender() {
  const body = $("#learn-body");
  // a list control keeps the focus through a re-render (a detail landing
  // after a fetch re-renders too), so ↑ ↓, Tab and Enter go on working
  const keep = document.activeElement?.dataset?.lrKey;
  body.innerHTML = "";
  lrEndAsk = lrEndAsk && lrRoute.view === "session";
  const h = lrHash(lrRoute);
  if (activeTab === "learn" && location.hash !== h) history.replaceState(null, "", h);
  // the chat composer competes with the answer box during a session (refinement §23)
  if (activeTab === "learn") $("#dock").hidden = lrRoute.view === "session";
  if (lrRoute.view === "session") lrRenderSession(body);
  else if (lrRoute.view === "receipt") lrRenderReceipt(body);
  else lrRenderHome(body);
  if (keep) body.querySelector(`[data-lr-key="${CSS.escape(keep)}"]`)?.focus({ preventScroll: true });
}

// --- home: a list (or the map), then one detail -----------------------------------
function lrRenderHome(body) {
  const d = lrData;
  if (!d) return;
  const plans = d.plans || [];
  const active = plans.filter((p) => p.status !== "completed");
  const sel = lrRoute.view === "plan" || lrRoute.view === "edit" ? plans.find((p) => p.path === lrRoute.plan) : null;
  let back = null;
  if (lrRoute.view === "new" || lrRoute.view === "edit") {
    let last = null;
    try { last = localStorage.getItem(LR_LAST_PLAN); } catch { /* private window */ }
    const prev = lrRoute.view === "edit" ? sel : active.find((p) => p.path === last);
    back = { label: prev ? `Back to ${prev.name}` : "Back to your plans", run: () => lrGo(prev ? { view: "plan", plan: prev.path } : { view: "plans" }) };
  }
  const [stamp, stampTitle] = lrVaultStamp();
  lrBar({ stamp, stampTitle, back, segs: true });
  const mv = lrEl("section", "mv lr-mv" + (lrSeg === "map" ? " lr-mapv" : ""));
  const left = lrEl("div", "mv-l");
  const right = lrEl("div", "mv-r");
  if (lrSeg === "map") lrRenderMapLeft(left);
  else if (lrSeg === "plans") lrRenderPlansLeft(left, sel);
  else lrRenderNextLeft(left);
  mv.append(left, right);
  body.appendChild(mv);
  // -- the detail
  if (lrRoute.view === "new" || lrRoute.view === "edit") return lrRenderComposer(right);
  if (lrRoute.view === "plan") {
    if (!sel) { right.appendChild(lrEl("p", "lr-help", "That plan is not in the vault any more.")); return; }
    if (lrPlan && lrPlan.path === sel.path) lrRenderPlanDetail(right, lrPlan);
    else { right.appendChild(lrEl("p", "lr-help", "Reading the plan.")); lrFetchPlan(sel.path); }
    return;
  }
  const notePath = lrRoute.view === "note" ? lrRoute.path : lrRoute.view === "map" ? lrRoute.path : "";
  if (notePath) {
    if (lrNote && lrNote.path === notePath && lrNote.gone) right.appendChild(lrEl("p", "lr-help", "That note is not in the vault any more."));
    else if (lrNote && lrNote.path === notePath) lrRenderNoteDetail(right, lrNote);
    else { right.appendChild(lrEl("p", "lr-help", "Reading the note.")); lrFetchNote(notePath); }
    return;
  }
  if (lrSession) right.appendChild(lrResumeBox());
  const inv = lrEl("div", "lr-invite");
  if (lrSeg === "map") {
    inv.append(lrEl("h2", null, "Pick a note to see its history"), lrEl("p", null, "Each mark is a note: dashed never asked, open still to verify, half met with help, full met without help."));
  } else if (lrPlansError) {
    inv.append(lrEl("h2", null, "Pick a row to see why it is up"), lrEl("p", null, "Your plans come back here once Retry reads them."));
  } else if (!plans.length) {
    // the first run (ADR-0061 §10): two sentences and one primary
    inv.append(lrEl("h2", null, "Start with a plan, or with one note"),
      lrEl("p", null, "A plan says what you want to study and what you want to be able to do. It lands as a note in the vault, with one line per session and never a score."));
    const row = lrEl("div", "row");
    row.append(lrBtn(lrSession ? "ghost box" : "lr-send", "Create plan", () => lrGo({ view: "new" })), lrEl("span", "lr-help", "or practise a row on the left"));
    inv.appendChild(row);
  } else {
    inv.append(lrEl("h2", null, lrSeg === "plans" ? "Pick a plan to continue it" : "Pick a row to see why it is up"),
      lrEl("p", null, lrSeg === "plans" ? "Or create one: one sentence is enough to start." : "Every row says why; the right pane shows what you already answered on it."));
  }
  right.appendChild(inv);
  const discs = lrEl("div", "lr-discs end");
  lrReadings(discs, d);
  right.appendChild(discs);
}

// Next (ADR-0081 §1, §2): the picker's session as the one primary, its mix
// named; then four groups in a fixed order, every row with its reason.
const LR_GROUPS = [["missed", "Missed lately", 4], ["due", "Falling due", 3], ["never", "Never practised", 2]];
function lrRenderNextLeft(left) {
  const d = lrData;
  const n = d.next || { plans: [], missed: [], due: [], never: [] };
  if (d.session) {
    const box = lrEl("div", "lr-primary");
    // one primary per view (ADR-0061): the picker's session until a row is
    // selected, then the selected row's action in the detail is the primary
    const quiet = lrSession || ["plan", "note"].includes(lrRoute.view);
    const go = lrBtn(quiet ? "ghost box" : "lr-send", lrSession ? "A session is open" : `Practise · ${lrPlural(d.session.questions, "question")} · ~${d.session.minutes} min`,
      () => (lrSession ? lrGo({ view: "session", id: lrSession.id }) : learnStartSession({ from: "primary" })));
    box.appendChild(go);
    if (d.mix) box.appendChild(lrEl("div", "lr-lh-sub", d.mix));
    left.appendChild(box);
  }
  const lh = lrEl("div", "lr-lh", "Your plans");
  lh.appendChild(lrEl("span", "cnt num", lrPlansError ? "–" : n.plans.length ? String(n.plans.length) : ""));
  left.appendChild(lh);
  if (lrPlansError) {
    // an error is not an empty vault (refinement §4): where the plans would be
    const eb = lrEl("div", "lr-errbox");
    eb.setAttribute("role", "alert");
    const t = lrEl("b");
    t.append(lrMark("warn", ""), "Couldn't read your plans");
    eb.append(t, lrEl("span", null, "The vault answered with an error. Your plans are untouched, and the rows below still work."),
      lrBtn("ghost box", "Retry", () => loadLearn(true)));
    left.appendChild(eb);
  } else if (!n.plans.length) {
    left.appendChild(lrBtn("lr-create", "+ Create plan", () => lrGo({ view: "new" })));
  }
  for (const p of n.plans) left.appendChild(lrNextRow({ ...p, title: p.name }, "plan"));
  for (const [g, title, shownN] of LR_GROUPS) {
    const rows = n[g] || [];
    if (!rows.length) continue;
    const h = lrEl("div", "lr-lh lr-lh2", title);
    h.appendChild(lrEl("span", "cnt num", String(rows.length)));
    left.appendChild(h);
    const open = lrMore.has(g) || rows.some((r, i) => i >= shownN && lrRoute.view === "note" && lrRoute.path === r.path);
    for (const r of open ? rows : rows.slice(0, shownN)) left.appendChild(lrNextRow(r, g));
    if (rows.length > shownN) left.appendChild(lrBtn("lr-more", open ? "Show fewer" : `Show ${rows.length - shownN} more`, () => { lrMore.has(g) ? lrMore.delete(g) : lrMore.add(g); lrRender(); }));
  }
  if (!n.plans.length && !LR_GROUPS.some(([g]) => (n[g] || []).length)) {
    left.appendChild(lrEl("p", "lr-lh-sub", d.measured || d.total
      ? "Nothing to suggest: notes need a footnoted claim or a Relations bullet to be asked about."
      : "Nothing measured yet. Pick a note in the vault and practise it; the list grows from there."));
  }
}

// A row of Next is a title and its reason (ADR-0081 §1); the action shows on
// hover, on focus and on the selected row. A plan row starts the plan.
const LR_ACT = { plan: "Continue", missed: "Ask again", due: "Practice", never: "Practice" };
function lrNextRow(r, g) {
  const isPlan = g === "plan";
  const on = isPlan ? (lrRoute.view === "plan" && lrRoute.plan === r.path) : (lrRoute.view === "note" && lrRoute.path === r.path);
  const row = lrEl("div", "lr-sg" + (on ? " on" : ""));
  const key = (isPlan ? "plan:" : "note:") + r.path;
  const pick = lrBtn("pick", undefined, () => lrGo(isPlan ? { view: "plan", plan: r.path } : { view: "note", path: r.path }));
  pick.dataset.lrRow = pick.dataset.lrKey = key;
  pick.setAttribute("aria-pressed", on ? "true" : "false");
  const inLive = lrSession && (isPlan ? lrSession.plan === r.path : lrSession.note === r.path);
  pick.append(lrEl("span", "t", r.title), lrEl("span", "r", inLive ? "in the open session" : r.why));
  const verb = inLive ? "Resume" : LR_ACT[g];
  const act = lrBtn("act", verb, () => inLive ? lrGo({ view: "session", id: lrSession.id })
    : isPlan ? learnStartSession({ plan: r.path, from: "row" }) : learnStartSession(r.pair ? { scope: r.pair, n: 1, from: "row" } : { note: r.path, from: "row" }));
  act.setAttribute("aria-label", `${verb} ${r.title}`);
  act.dataset.lrKey = "act:" + key;
  row.append(pick, act);
  return row;
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
  const create = lrBtn("lr-create" + (isNew ? " on" : ""), "+ Create plan", () => { lrFormReset(); lrGo({ view: "new" }); });
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
const LR_CELL_SAY = { never: "never asked", open: "asked, still open", half: "met with help", full: "met without help" };
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
    const step = p.steps?.length ? p.steps.find((s) => s.text.startsWith(node.node) || s.text.includes(node.node)) : null;
    const tr = lrEl("tr", "node");
    const th0 = lrEl("th");
    th0.scope = "row";
    const exp = leaves.length ? lrBtn("lr-exp") : lrEl("span", "lr-exp");
    exp.append(lrEl("span", "chev"), label);
    th0.appendChild(exp);
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
// Open note and the action, the reason as a sentence, History, what a session
// covers, how this is measured. It retires the suggestion detail (§6).
const LR_GROUP_EYEBROW = { missed: "Missed lately", due: "Falling due", never: "Never practised" };
const LR_HIST_MARK = { met: "full", assisted: "half", missed: "open" };
function lrRenderNoteDetail(right, n) {
  const inLive = lrSession && lrSession.note === n.path;
  if (inLive) right.appendChild(lrResumeBox());
  const verb = n.group === "missed" ? "Ask again" : "Practice";
  const go = inLive ? null : lrBtn("lr-send", verb, () => learnStartSession({ note: n.path }));
  if (go) go.disabled = !n.grounded || !!lrSession;
  const eyebrow = lrRoute.view === "map" ? n.path : LR_GROUP_EYEBROW[n.group] || "Note";
  const head = lrHead(eyebrow + (n.also_in?.length ? ` · in ${n.also_in.map((p) => p.name).join(", ")}` : ""), n.title,
    inLive ? "" : n.questions ? `${lrPlural(n.questions, "question")} · about ${n.minutes} min` : "no grounded line to ask about",
    [lrBtn("ghost box", "Open note", () => openNote(n.path)), go]);
  // why it is up, in words: the ledger's own fields, never a model's (ADR-0081 §3)
  if (n.sentence) head.appendChild(lrEl("p", "lr-reason", n.sentence));
  if (lrSession && !inLive) head.appendChild(lrEl("p", "lr-help", "A session is open; end it or resume it before starting this one."));
  right.appendChild(head);
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
    name: "", nameTouched: false, scope: "", target: "", label: "", count: null, restrict: "", by: "",
    objectives: [], refused: [], preview: null, previewBusy: false, cards: null, cardsBusy: false, cardsAt: 0,
    cardsModel: false, cardsSaved: false, cardsFailed: false, editing: null, editPath: "", q: "", opts: [], optIdx: -1, seq: 0, cardSeq: 0,
  }, extra);
}
lrFormReset();
const LR_KIND_WORD = { folder: "folder", notes: "notes", topic: "topic", steps: "syllabus", workspace: "workspace" };
const LR_SESSION_Q = 8;

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

// A workspace plan keeps its target as a wikilink by path (plans.target_text);
// the reader is shown the workspace's name.
const lrTargetShown = (p) => p.scope === "workspace"
  ? String(p.target || "").replace(/^\[\[|\]\]$/g, "").split("/").pop() : p.target;

// Plan from a workspace (ADR-0112 point 5): the same fields, scoped to its
// members, which the server resolves again at every read.
function learnPlanWorkspace(name) {
  // "plan", so the plan note does not take its workspace's name
  lrFormReset({ step: "fields", scope: "workspace", target: name, label: name, name: `${name} plan` });
  if (activeTab !== "learn") showTab("learn");
  lrGo({ view: "new" });
  lrPreviewScope();
}

// Edit plan opens the fields filled, without the sentence (§17).
function lrFormFromPlan(p) {
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

function lrRenderComposer(right) {
  if (lrRoute.view === "edit" && lrForm.editPath !== lrRoute.plan) {
    if (lrPlan && lrPlan.path === lrRoute.plan) lrFormFromPlan(lrPlan);
    else { right.appendChild(lrEl("p", "lr-help", "Reading the plan.")); lrFetchPlan(lrRoute.plan); return; }
  }
  if (lrRoute.view === "new" && lrForm.editPath) lrFormReset();
  const box = lrEl("div", "lr-composer");
  if (lrForm.step === "sentence") lrSentenceStep(box);
  else lrFieldsStep(box);
  right.appendChild(box);
}

function lrSentenceStep(box) {
  const head = lrEl("div", "lr-phead");
  head.append(lrEl("span", "eyebrow", "New plan · nothing is written until you create it"), lrEl("h2", null, "What are you preparing for?"));
  box.appendChild(head);
  const ta = lrEl("textarea", "lr-field textarea lr-sentence");
  ta.rows = 3;
  ta.value = lrForm.sentence;
  ta.placeholder = "Psychology exam on November 30: perception, short-term memory, reinforcement learning, theory of mind. I need to explain and apply them.";
  ta.setAttribute("aria-label", "What are you preparing for?");
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
  if (lrRoute.view === "new") lrRender();
  if (lrForm.scope) lrPreviewScope();
}

function lrFieldsStep(box) {
  const edit = !!lrForm.editPath;
  const head = lrEl("div", "lr-phead");
  head.appendChild(lrEl("span", "eyebrow", edit ? "Edit plan · one revertible write, sessions untouched" : "New plan · nothing is written until you create it"));
  const name = lrEl("input", "lr-title");
  name.type = "text"; name.value = lrForm.name; name.placeholder = "Name the plan";
  name.setAttribute("aria-label", "Plan name");
  name.dataset.lrKey = "form:name";
  name.addEventListener("input", () => { lrForm.name = name.value; lrForm.nameTouched = true; const at = $("#lr-lands"); if (at) at.textContent = lrLands(); lrCtaState(); });
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
  box.appendChild(lrGoalsSection());
  box.appendChild(lrBySection());
  box.appendChild(lrObjectivesSection());
  const cta = lrEl("div", "lr-cta");
  const lands = lrEl("span", "lr-help");
  lands.append(edit ? "Saves " : "Lands as ", Object.assign(lrEl("span", "path", lrLands()), { id: "lr-lands" }), " · revertible");
  const create = lrBtn("lr-send", edit ? "Save plan" : "Create plan", lrWritePlan);
  create.id = "lr-create-btn";
  cta.append(lands, lrBtn("ghost box", "Cancel", () => { lrForm.editing = null; history.length > 1 && ["new", "edit"].includes(lrParse(location.hash)?.view) ? history.back() : lrGo({ view: "plans" }); }), create);
  box.appendChild(cta);
  lrCtaState(create);
}

const lrLands = () => lrForm.editPath || `Plans/${lrForm.name.trim() || "<name>"}.md`;
// Create is the one primary, enabled once Study holds a checked value (§12)
function lrCtaState(btn = $("#lr-create-btn")) {
  if (!btn) return;
  const ok = !!(lrForm.preview && lrForm.preview.paths?.length && lrForm.name.trim());
  btn.disabled = !ok;
  btn.title = ok ? "" : !lrForm.name.trim() ? "name the plan first" : "choose what to study first";
}

// -- Study: a token, or a typeahead over folders, notes, topics and syllabus notes
function lrStudySection() {
  const sec = lrEl("section", "lr-fs");
  sec.appendChild(lrEl("h3", null, "Study"));
  const f = lrForm;
  const row = lrEl("div", "lr-studyrow");
  if (f.scope === "steps" && f.target.includes("\n") || f.scope === "steps" && !f.label) {
    const ta = lrEl("textarea", "lr-field textarea");
    ta.rows = Math.min(8, Math.max(3, f.target.split("\n").length));
    ta.value = f.target;
    ta.placeholder = "One subject per line, in the order you will study them";
    ta.setAttribute("aria-label", "the syllabus, one subject per line");
    ta.addEventListener("input", () => (f.target = ta.value));
    ta.addEventListener("change", () => { f.refused = []; lrPreviewScope(); });
    row.appendChild(ta);
    sec.appendChild(row);
    const aside = lrEl("div", "lr-asides");
    aside.appendChild(lrBtn("lr-link", "pick a folder, a note or a topic instead", () => { Object.assign(f, { scope: "", target: "", label: "", preview: null, cards: null }); lrRender(); }));
    sec.appendChild(aside);
  } else if (f.scope && f.target) {
    const tok = lrEl("span", "lr-tok");
    const n = f.preview?.paths?.length;
    tok.append(lrEl("i", null, LR_KIND_WORD[f.scope]), `${f.label || f.target}` + (n != null ? ` · ${lrPlural(n, "note")}` : ""));
    const x = lrBtn("lr-x", "×", () => { Object.assign(f, { scope: "", target: "", label: "", restrict: "", preview: null, cards: null, refused: [] }); lrRender(); setTimeout(() => $("#lr-study")?.focus(), 0); });
    x.setAttribute("aria-label", "clear what to study");
    tok.appendChild(x);
    row.appendChild(tok);
    row.appendChild(lrBtn("lr-link", "Paste a syllabus", () => { Object.assign(f, { scope: "steps", target: "", label: "", preview: null, cards: null, refused: [] }); lrRender(); }));
    sec.appendChild(row);
  } else {
    row.appendChild(lrStudyInput());
    row.appendChild(lrBtn("lr-link", "Paste a syllabus", () => { Object.assign(f, { scope: "steps", target: "", label: "", preview: null, cards: null, refused: [] }); lrRender(); }));
    sec.appendChild(row);
  }
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
  if (f.previewBusy && !f.preview) sec.appendChild(lrEl("p", "lr-help", "Resolving the material…"));
  else if (f.preview?.error) { const e = lrEl("p", "lr-help warn"); e.append(lrMark("warn", ""), "Could not resolve: " + f.preview.error); sec.appendChild(e); }
  else if (f.preview) lrShowPreview(sec, f.preview);
  return sec;
}

function lrStudyInput() {
  const wrap = lrEl("div", "lr-ta");
  const inp = lrEl("input", "lr-field");
  inp.type = "text"; inp.id = "lr-study"; inp.placeholder = "A folder, a note or a topic";
  inp.value = lrForm.q;
  inp.setAttribute("role", "combobox"); inp.setAttribute("aria-autocomplete", "list");
  inp.setAttribute("aria-controls", "lr-opts"); inp.setAttribute("aria-expanded", "false");
  inp.setAttribute("aria-label", "what to study");
  inp.dataset.lrKey = "form:study";
  const list = lrEl("ul", "lr-opts");
  list.id = "lr-opts"; list.setAttribute("role", "listbox"); list.hidden = true;
  let t = null;
  inp.addEventListener("input", () => { lrForm.q = inp.value; clearTimeout(t); t = setTimeout(() => lrScopeSearch(inp, list), 120); });
  inp.addEventListener("focus", () => lrScopeSearch(inp, list));
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
    } else if (e.key === "Escape") { list.hidden = true; inp.setAttribute("aria-expanded", "false"); }
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
  lrForm.opts = Array.isArray(opts) ? opts : [];
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
    li.append(lrEl("i", null, LR_KIND_WORD[o.kind] || o.kind), lrEl("span", "l", o.label), lrEl("span", "c num", o.kind === "steps" ? `${o.count} lines` : lrPlural(o.count, "note")));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); lrPickScope(o); });
    list.appendChild(li);
  });
  list.hidden = !lrForm.opts.length;
  inp.setAttribute("aria-expanded", list.hidden ? "false" : "true");
  if (lrForm.optIdx >= 0) inp.setAttribute("aria-activedescendant", "lr-opt-" + lrForm.optIdx);
}

function lrPickScope(o) {
  Object.assign(lrForm, { scope: o.kind, target: o.kind === "notes" ? [o.target] : o.target, label: o.label, q: "", opts: [], refused: [], preview: null, cards: null });
  if (o.kind === "folder" || o.kind === "notes") lrForm.restrict = "";
  lrRender();
  lrPreviewScope();
}

// The name follows the material until the reader types one (the category's
// pattern; the old form asked for it last, empty, with a fixed placeholder)
function lrDefaultName() {
  const f = lrForm;
  if (f.nameTouched || !f.scope) return;
  if (f.scope === "folder") f.name = f.target.replace(/\/$/, "").split("/").pop();
  else if (f.scope === "steps") f.name = f.label && !f.target.includes("\n") ? f.label : `Syllabus, ${lrDate(new Date().toISOString())}`;
  else f.name = f.label || f.target;
}

async function lrPreviewScope({ keepCards = false } = {}) {
  const f = lrForm;
  if (!f.scope || !String(f.target).trim()) return;
  if (!keepCards) f.cardsSaved = false;
  lrDefaultName();
  const seq = ++f.seq;
  f.previewBusy = true;
  if (!keepCards) f.cards = null;
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
  if (!out.error && out.paths?.length && !(keepCards && f.cards)) lrWriteCards();
}

function lrStudyBody() {
  const f = lrForm;
  // notes travel as a list: "Sensazione, percezione, psicofisica" is one title
  const target = f.scope === "notes" ? (Array.isArray(f.target) ? f.target : [f.target]) : f.target;
  return { scope: f.scope, target, restrict: f.restrict, refused: f.refused, objectives: f.objectives };
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
  const steps = f.scope === "steps";
  const line = lrEl("p", "lr-pline");
  if (steps) {
    const covered = p.tree.filter((n) => !n.gap);
    const doubt = covered.filter((n) => n.doubt).length;
    line.appendChild(lrEl("b", null, `${lrPlural(p.tree.length, "line")} · ${lrPlural(p.paths.length, "note")}`));
    line.append(` · ${covered.length - doubt} matched` + (doubt ? ` · ${doubt} to check` : "") + (p.gaps.length ? ` · ${lrPlural(p.gaps.length, "gap")}` : ""));
  } else {
    line.appendChild(lrEl("b", null, `${lrPlural(p.paths.length, "note")}` + (p.parts > 1 ? ` in ${p.parts} parts` : "")));
    line.append(` · ${p.measured} practised · ${p.never} never asked` + (p.due ? ` · ${p.due} to review` : ""));
  }
  sec.appendChild(line);
  if (steps) {
    const st = lrEl("div", "lr-steps2");
    p.tree.forEach((n, i) => {
      st.appendChild(lrEl("span", "n num", String(i + 1)));
      st.appendChild(lrEl("span", "s", n.node));
      const to = lrEl("span", "to" + (n.gap ? " gap" : ""));
      if (n.gap) to.append(lrEl("i", "lr-cell never"), "not in the vault · kept as a gap");
      else to.textContent = n.leaves.map(lrStem).join(", ");
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
  by.addEventListener("change", () => { lrForm.by = by.value; lrRender(); });
  row.append(by, lrEl("span", "lr-help", "optional"));
  if (lrForm.by) row.appendChild(lrBtn("lr-link", "clear", () => { lrForm.by = ""; lrRender(); }));
  sec.appendChild(row);
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
  else sum.appendChild(lrEl("span", "m mute", `${f.editPath ? "Save" : "Create"} writes what is shown`));
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

async function lrWritePlan() {
  const f = lrForm;
  if (!f.name.trim()) { notify("a plan needs a name", "warn"); return; }
  if (!f.preview?.paths?.length) { notify("choose what to study first", "warn"); return; }
  const body = { ...lrStudyBody(), name: f.name.trim(), by: f.by || null };
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
    lrSeg = "plans";
    lrRender();
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
  lrSession = { ...s, cur: 0, state: "input", answers: {}, verdicts: {}, results: {}, skipped: {}, hintsUsed: {}, shown: {}, conf: null, told: false };
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
  const eb = lrEl("div", "lr-eyebrow");
  eb.appendChild(lrEl("span", "eyebrow", lrEyebrow(it)));
  if (window.speechSynthesis) eb.appendChild(lrReadToggle());
  sheet.appendChild(eb);
  // the question keeps its size after the answer (ADR-0061 §18)
  sheet.appendChild(lrMd(lrEl("p", "q"), it.q));
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
  let mark = "open";
  let say = s.skipped[prev.i] ? "skipped" : "not recorded";
  if (!s.skipped[prev.i] && v) { const w = lrVerdictSay(prev, v, s); mark = w.mark; say = w.word.toLowerCase(); }
  sum.append(lrEl("span", "chev"), lrEl("span", "k", `${n} · ${prev.point?.kind || prev.kind}`), lrMark(mark, ""),
    lrEl("span", "v", res ? `${say} · ${res.rating_name}, recorded` : say));
  det.appendChild(sum);
  const d = lrEl("div", "in");
  d.appendChild(lrMd(lrEl("p", "q"), prev.q));
  if (s.answers[prev.i]) d.appendChild(lrEl("p", "you", s.answers[prev.i]));
  if (v?.feedback_line) d.appendChild(lrEl("p", "fb", v.feedback_line));
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
    ref.append(lrEl("span", "lab", "reference answer"), " " + v.answer);
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
  const v = s.verdicts[it.i];
  const self = !!v.needs_self_grade || v.grader === "self";
  const rating = s.chosen?.[it.i] ?? (self ? null : v.rating);
  if (!rating) { notify("pick a rating", "warn"); return; }
  const btn = $("#lr-confirm");
  if (btn) btn.disabled = true;
  try {
    const r = await lrPost("/learn/rate", { id: s.id, i: it.i, rating });
    s.results[it.i] = r;
    if (v.needs_self_grade) v.selfRated = true;
  } catch (e) { if (lrGone(e)) return; notify("not recorded: " + e.message); if (btn) btn.disabled = false; return; }
  if (s.results[it.i].momentum) { lrRender(); return; }   // a milestone line reads before moving on
  lrNext();
}

function lrNext() {
  const s = lrSession;
  s.chosen = {}; s.changing = false; s.conf = null; s.confSkipped = false; s.told = false;
  lrPrevOpen = false;
  if (s.cur + 1 >= s.items.length) return lrClose();
  s.cur += 1;
  s.state = "input";
  lrRender();
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
const LR_COMES_BACK = { missed: "in Missed lately", assisted: "back when it falls due", skipped: "nothing recorded" };
const lrStatusMark = (st) => (st === "met" ? "full" : st === "assisted" ? "half" : st === "not_scored" ? "warn" : "open");

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
  // Back lands on Next with the first missed note selected, so what the
  // receipt established is the next thing read (ADR-0081 §9)
  const firstMiss = r.items.find((x) => x.status === "missed");
  const backLabel = firstMiss ? "Back to Next" : `Back to ${o.kind === "vault" ? "Next" : o.label}`;
  const goBack = async () => { lrReceipt = null; if (!lrData) await loadLearn(true); lrGo(firstMiss ? { view: "note", path: firstMiss.path } : lrOriginRoute(o)); };
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
  // three figures (ADR-0061 §25); the summary sentence is what is read aloud
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
    wrap.appendChild(figs);
  }
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
  }
  if (r.points?.length) {
    // points are marks grouped by heading (ADR-0061 §27)
    const sec = lrEl("section", "lr-sec");
    sec.appendChild(lrEl("h3", null, "Points"));
    let heading = null;
    let grid = null;
    for (const p of r.points) {
      if (p.heading !== heading || !grid) {
        heading = p.heading;
        if (heading) sec.appendChild(lrEl("span", "lr-grp", heading));
        grid = lrEl("div", "lr-pts");
        sec.appendChild(grid);
      }
      const name = lrEl("span", "p");
      name.append(lrMark(lrStatusMark(p.status), p.status === "met" ? "met without help" : ""), p.kind);
      if (p.status !== "met") {
        // where it rests and how it comes back (ADR-0081 §9)
        const line = r.items.find((x) => x.point?.i === p.i && x.span?.line)?.span?.line;
        name.appendChild(lrEl("span", "m", [LR_STATUS_WORD[p.status] || p.status, line ? `line ${line}` : "", LR_COMES_BACK[p.status]].filter(Boolean).join(" · ")));
      }
      grid.append(name, lrEl("span", "k", LR_OBJ_SHORT[p.objective] || p.objective));
    }
    wrap.appendChild(sec);
  }
  // a session over several notes: its open questions as rows, each with
  // where it rests and how it comes back (ADR-0081 §9)
  const open = r.points?.length ? [] : r.items.filter((x) => ["missed", "assisted", "skipped"].includes(x.status));
  if (open.length) {
    const sec = lrEl("section", "lr-sec");
    const h = lrEl("h3", null, "Still open");
    h.appendChild(lrEl("span", "num", String(open.length)));
    sec.appendChild(h);
    // one note asked several times names its questions, not the title thrice
    const oneNote = new Set(open.map((x) => x.path)).size === 1;
    for (const x of open) {
      const row = lrEl("div", "lr-openrow");
      const t = lrEl("span", "t");
      t.append(lrMark(lrStatusMark(x.status), ""), oneNote && x.q ? lrEl("span", "q", lrPlain(x.q))
        : lrBtn("lr-link", x.title || lrStem(x.path), () => lrGo({ view: "note", path: x.path })));
      row.append(t, lrEl("span", "m", [LR_STATUS_WORD[x.status], x.span?.line ? `line ${x.span.line}` : "", LR_COMES_BACK[x.status]].filter(Boolean).join(" · ")));
      sec.appendChild(row);
    }
    wrap.appendChild(sec);
  }
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
  if (["home", "plan", "note", "plans"].includes(lrRoute.view)) {
    if (typing || e.altKey || e.ctrlKey || e.metaKey || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    if (tgt?.closest && tgt !== document.body && !tgt.closest("#view-learn")) return;
    const rows = [...document.querySelectorAll("#learn-body [data-lr-row]")].filter((x) => x.offsetParent !== null);
    if (!rows.length) return;
    e.preventDefault();
    const cur = rows.findIndex((x) => x.getAttribute("aria-pressed") === "true");
    const next = rows[cur < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, cur + (e.key === "ArrowDown" ? 1 : -1)))];
    const key = next.dataset.lrRow;
    next.focus();
    const seg = lrSeg;
    lrGo(key.startsWith("plan:") ? { view: "plan", plan: key.slice(5) } : { view: "note", path: key.slice(5) }, { replace: true });
    if (key.startsWith("plan:") && seg !== lrSeg) { lrSeg = seg; lrRender(); }
    return;
  }
  if (!lrSession || lrRoute.view !== "session") return;
  const s = lrSession;
  if (e.key === "Escape") { e.preventDefault(); if (lrEndAsk) { lrEndAsk = false; lrRender(); } else lrLeave(); return; }
  if (lrEndAsk) return;
  if (s.state === "input") {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); lrSubmit(false); return; }
    if (e.key === "h" && !typing) { e.preventDefault(); lrHint(); }
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
