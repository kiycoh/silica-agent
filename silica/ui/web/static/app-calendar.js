// --- calendar ----------------------------------------------------------------
// ADR-0118: Google Calendar's frame in Silica's one hue. A side column holds New
// event, a month to jump with and the layers; the view is the week or the day
// with hours, the month grid, or the agenda list, all over GET /calendar. A kind
// is told by its shape and never by a colour: an event is a block, a study
// session a block led by its plan's diamond, a plan due a diamond, a daily note
// a triangle, a task a checkbox. A click or a drag on an empty time opens the
// quick card, which writes the note the chat's silica_event_create writes
// (POST /calendar/event). POST /reminders every 30 s IS the reminder tick
// (setInterval stays alive in hidden tabs); delivered ones land on the toast
// strip.

let calMode = "week";         // "day" | "week" | "month" | "agenda"
let calAnchor = new Date();   // the day the view is about
let calMini = null;           // first of the month the side column shows
let calDays = {};             // date -> DayRow of the fetched window
let calPlans = [];            // active plans whose `by` is on or after the window
let calBursting = [];         // V6: what the last fortnight of WRITING was about
let calFetched = "";          // "start|days" of calDays: a day step inside the week redraws without a fetch
let calCard = null;           // the open quick or event card: { el, ghost }
let calSeq = 0;               // the latest /calendar request: an older answer landing later is dropped
let calPending = false;       // a reload landed while a card was open; drawn when it closes

const CAL_HOUR = 48;          // px an hour in the time grids (ADR-0118 point 3)
let calScroll = 7 * CAL_HOUR - 10; // the time grids open on 07:00, its label in view, and keep the reader's scroll after that
const CAL_LANE_CAP = 3;       // month mode: visible multi-day lanes per week
const CAL_CHIP_CAP = 2;       // month mode: visible timed chips per day
const CAL_DERIVED_CAP = 3;    // month mode: chips + derived lines together
const CAL_AGENDA_CAP = 6;     // agenda: rows per axis before "show all"

// The seven kinds are layers, as Google's calendars are (ADR-0118 point 5). On
// by default: what is planned. Off: the two axes that log what already happened.
const CAL_LAYERS = [
  ["events", "Events"], ["plans", "Study plans"], ["tasks", "Tasks"], ["review", "To review"],
  ["daily", "Daily notes"], ["runs", "Silica's runs"], ["notes", "Dated notes"],
];
const CAL_LAYERS_OFF = ["runs", "notes"];
const CAL_LAYERS_KEY = "silica.calendar.layers";
let calLayers = {};
try {
  const saved = JSON.parse(localStorage.getItem(CAL_LAYERS_KEY) || "{}");
  if (saved && typeof saved === "object") calLayers = saved;
} catch { /* storage refused or a hand-edited value: the defaults stand */ }
const calOn = (k) => (k in calLayers ? !!calLayers[k] : !CAL_LAYERS_OFF.includes(k));

function calFmt(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function calMonday(d) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}
function calDay(s) { return new Date(s + "T00:00"); }
function calMin(s) { return Number(s.slice(11, 13)) * 60 + Number(s.slice(14, 16)); }
function calHM(m) { return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; }
function calShort(d) { return d.toLocaleString("en-US", { month: "short", day: "numeric" }); }
function calWindow() {
  if (calMode === "month") {
    return { start: calMonday(new Date(calAnchor.getFullYear(), calAnchor.getMonth(), 1)), days: 42 };
  }
  if (calMode === "agenda") {
    return { start: new Date(calAnchor.getFullYear(), calAnchor.getMonth(), calAnchor.getDate()), days: 7 };
  }
  return { start: calMonday(calAnchor), days: 7 };   // the day view's strip is its week
}
function calWeekDates() {
  const m = calMonday(calAnchor);
  return Array.from({ length: 7 }, (_, i) => { const d = new Date(m); d.setDate(d.getDate() + i); return calFmt(d); });
}

async function loadCalendar() {
  const w = calWindow();
  // Only on a cold open. Every step refetches, and flashing a spinner over a
  // grid that is already on screen would be the louder of the two lies.
  const frame = $("#cal-loading");
  if (!Object.keys(calDays).length) frame.hidden = false;
  const seq = ++calSeq;
  try {
    const d = await fetch(`/calendar?start=${calFmt(w.start)}&days=${w.days}`).then((r) => r.json());
    // Two steps in a row, or a save and a step, run two loads at once; the
    // server answers them on parallel threads, so the older can land last.
    if (seq !== calSeq) return;
    if (d.error) { notify(d.error); return; }
    calDays = {};
    for (const row of d.days) calDays[row.date] = row;
    calPlans = d.plans || [];
    calBursting = d.bursting || [];
    calFetched = `${calFmt(w.start)}|${w.days}`;
    // A redraw replaces what a card is about and closes it: the card a person
    // is typing in outlives the reload, which is drawn when it closes.
    if (calCard) calPending = true;
    else renderCalendar();
  } catch { notify("couldn't load the calendar"); }
  finally { frame.hidden = true; }
}
function calGo() {
  const w = calWindow();
  if (`${calFmt(w.start)}|${w.days}` === calFetched) renderCalendar();
  else loadCalendar();
}
function calSetMode(m) { calMode = m; calGo(); }
function calStep(dir) {
  const a = new Date(calAnchor);
  if (calMode === "month") { a.setDate(1); a.setMonth(a.getMonth() + dir); }
  else a.setDate(a.getDate() + dir * (calMode === "day" ? 1 : 7));
  calAnchor = a;
  calGo();
}

// A bar is anything that must draw as a span: all-day, or crossing midnight.
function calIsBar(e) {
  return e.all_day || (e.end && e.end.slice(0, 10) !== e.start.slice(0, 10));
}

// nodus layoutCalendarWeek: first-fit lanes over column intervals.
function calPackLanes(spans) {
  spans.sort((a, b) => a.c0 - b.c0 || (b.c1 - b.c0) - (a.c1 - a.c0));
  const lanes = [];
  for (const s of spans) {
    let lane = lanes.findIndex((l) => l.every((o) => o.c1 < s.c0 || o.c0 > s.c1));
    if (lane === -1) { lane = lanes.length; lanes.push([]); }
    lanes[lane].push(s);
    s.lane = lane;
  }
  return lanes.length;
}

// Timed events of one day, side by side where they overlap: a cluster is a run
// of events each starting before the cluster's latest end, and inside it each
// takes the first column free at its start. Every event in a cluster shares its
// width, the rule Google's week follows.
function calPackDay(evs) {
  evs.sort((a, b) => a.s - b.s || b.end - a.end);
  let group = [];
  let until = -1;
  const flush = () => {
    const cols = [];
    for (const x of group) {
      let c = cols.findIndex((end) => end <= x.s);
      if (c === -1) { c = cols.length; cols.push(0); }
      cols[c] = x.end;
      x.col = c;
    }
    for (const x of group) x.ncol = cols.length;
    group = [];
  };
  for (const x of evs) {
    if (group.length && x.s >= until) flush();
    until = group.length ? Math.max(until, x.end) : x.end;
    group.push(x);
  }
  if (group.length) flush();
}

// "nucleate `07-part.md` → 2 new, 26 patch, 2 deferred · run 5e88feb0"
// The run id is provenance, not reading matter: it rides the row's tooltip and
// leaves the line. Anything that doesn't match comes through whole rather than
// being dropped — the log's shape is the agent's to change.
function calActivity(s) {
  const at = s.lastIndexOf(" · run ");
  const body = at > 0 ? s.slice(0, at) : s;
  const m = body.match(/^(\S+)\s+`(.+?)`\s*(?:→\s*(.*))?$/);
  if (!m) return { verb: "", what: body, detail: "" };
  return { verb: m[1], what: m[2], detail: (m[3] || "").trim() };
}

// What a day's derived axes say in one line of words. Events are the only axis
// that is an appointment, so they are blocks; the others already happened or
// are waiting, and they are counted, never drawn (ADR-0118 point 4).
function calDerived(row) {
  const out = [];
  if (!row) return out;
  // Tasks first (ADR-0062): of the waiting lines they are the ones the writer
  // set for this day. Amber only when one is overdue, as review-due is.
  const open = calOn("tasks") ? (row.tasks || []).filter((t) => t.status === "open") : [];
  const late = open.filter((t) => t.overdue).length;
  if (open.length) {
    out.push({ cls: late ? "due" : "", text: open.length + " to do",
               title: open.length + " open tasks" + (late ? `, ${late} overdue` : "") });
  }
  const done = calOn("tasks") ? (row.tasks || []).length - open.length : 0;
  if (done) out.push({ cls: "", text: done + " done", title: done + " tasks completed this day" });
  if (calOn("review") && row.review?.length) {
    out.push({ cls: "due", text: row.review.length + " to review",
               title: row.review.length + " notes the review queue has due" });
  }
  const verbs = new Map();
  for (const a of calOn("runs") ? row.activity || [] : []) {
    const v = calActivity(a).verb || "wrote";
    verbs.set(v, (verbs.get(v) || 0) + 1);
  }
  for (const [v, n] of [...verbs].sort((a, b) => b[1] - a[1])) {
    out.push({ cls: "", text: `${v} ×${n}`, title: `${n} ${v} runs on this day` });
  }
  if (calOn("notes") && row.notes?.length) {
    out.push({ cls: "", text: row.notes.length + (row.notes.length === 1 ? " note" : " notes"),
               title: row.notes.length + " notes whose claim clock lands here" });
  }
  return out;
}

function calSpan(e) {
  if (e.all_day) return "all day";
  const a = e.start.slice(11, 16);
  if (!e.end) return a;
  return e.end.slice(0, 10) === e.start.slice(0, 10) ? `${a} to ${e.end.slice(11, 16)}`
    : `${a} to ${calShort(calDay(e.end.slice(0, 10)))} ${e.end.slice(11, 16)}`;
}

// An event is a button: its click opens the event card on it (ADR-0118 point
// 6), and it never starts the drag that makes a new one under it.
function calEventEl(e, cls, withTime, block) {
  const el = mkEl("button", cls + (e.status ? " " + e.status : "") + (e.plan ? " sess" : ""));
  el.type = "button";
  const t = mkEl("span", "et");
  if (e.plan) t.appendChild(mkEl("span", "cal-dia"));
  if (withTime && !e.all_day) t.appendChild(mkEl("span", "t", e.start.slice(11, 16)));
  t.appendChild(document.createTextNode((e.status === "done" ? "✓ " : "") + e.title));
  el.appendChild(t);
  if (block) el.appendChild(mkEl("span", "tm", calSpan(e)));
  el.title = e.title;
  el.addEventListener("pointerdown", (ev) => ev.stopPropagation());
  el.addEventListener("click", (ev) => { ev.stopPropagation(); calEventCard(e, el); });
  return el;
}

// A study plan stands on its deadline (`by`), and it opens in Learn rather
// than in the note drawer: the note is where the plan is kept, Learn is where
// it is read and practised. Through the hash and not showTab + lrGo: showTab
// replaces the history entry, so Back from the plan landed on Learn's home
// instead of on the month it was clicked from.
function calOpenPlan(path) {
  location.hash = lrHash({ view: "plan", plan: path });
}
function calPlanEl(p) {
  const done = p.status === "completed";
  // the diamond, or the check once completed, is the chip's ::before
  const el = mkEl("button", "cal-chip plan" + (done ? " done" : ""), p.name);
  el.type = "button";
  el.title = "study plan due: " + p.name;
  el.addEventListener("pointerdown", (ev) => ev.stopPropagation());
  el.addEventListener("click", (ev) => { ev.stopPropagation(); calOpenPlan(p.path); });
  return el;
}
function calDaysLeft(by) {
  const n = Math.round((calDay(by) - calDay(calFmt(new Date()))) / 864e5);
  return n === 0 ? "due today" : n === 1 ? "1 day" : `${n} days`;
}
// A plan still ahead rides the all-day band from today to its `by`, so a week
// before an exam says how far off it is instead of being empty (ADR-0118
// point 3). Outlined, not filled: a fill is an appointment.
function calPlanBar(p, open) {
  const el = mkEl("button", "ct-plan" + (open ? " open" : ""));
  el.type = "button";
  el.append(mkEl("span", "cal-dia"), mkEl("b", "", p.name), mkEl("span", "left", calDaysLeft(p.by)));
  if (open) el.appendChild(mkEl("span", "to", calShort(calDay(p.by)) + " →"));
  el.title = `study plan due ${p.by}: open it in Learn`;
  el.addEventListener("pointerdown", (ev) => ev.stopPropagation());
  el.addEventListener("click", () => calOpenPlan(p.path));
  return el;
}

// The day's daily note (ADR-0062), the triangle the graph draws it as. Its own
// click: the day's opens or makes something else, this one opens the note.
function calDailyBtn(daily) {
  const tri = mkEl("button", "cal-daily");
  tri.type = "button";
  tri.title = "daily note " + daily.stem;
  tri.setAttribute("aria-label", "open the daily note " + daily.stem);
  tri.addEventListener("pointerdown", (ev) => ev.stopPropagation());
  tri.addEventListener("click", (ev) => { ev.stopPropagation(); openNote(daily.path); });
  return tri;
}

// --- the views ----------------------------------------------------------------

function renderCalendar() {
  calCloseCard();
  document.querySelectorAll("#cal-mode button").forEach((b) => setActive(b, b.dataset.calmode === calMode));
  calRenderTitle();
  calMini = new Date(calAnchor.getFullYear(), calAnchor.getMonth(), 1);
  calRenderMini();
  calRenderLayers();
  const main = $("#cal-grid");
  main.replaceChildren();
  main.dataset.mode = calMode;
  if (calMode === "month") calRenderMonth(main);
  else if (calMode === "agenda") renderCalAgenda();
  else if (calMode === "day") calRenderDay(main);
  else main.appendChild(calTimeGrid(calWeekDates(), true));
}

function calRenderTitle() {
  const t = $("#cal-title");
  const sub = $("#cal-sub");
  if (calMode === "day") {
    t.textContent = calAnchor.toLocaleString("en-US", { weekday: "long", month: "long", day: "numeric" });
    sub.textContent = String(calAnchor.getFullYear());
    return;
  }
  if (calMode === "month") {
    t.textContent = calAnchor.toLocaleString("en-US", { month: "long", year: "numeric" });
    sub.textContent = "";
    return;
  }
  const w = calWindow();
  const b = new Date(w.start);
  b.setDate(b.getDate() + w.days - 1);
  t.textContent = calMode === "agenda" ? "Agenda"
    : w.start.toLocaleString("en-US", { month: "long", year: "numeric" });
  sub.textContent = `${calShort(w.start)} to ${calShort(b)}`;
}

// The month the side column jumps with. Its arrows page the small month alone;
// a click on a day moves the view there in whatever mode it is in.
function calRenderMini() {
  const box = $("#cal-mini");
  box.replaceChildren();
  const today = calFmt(new Date());
  const head = mkEl("div", "cal-mini-h");
  head.appendChild(mkEl("span", "", calMini.toLocaleString("en-US", { month: "long", year: "numeric" })));
  const nav = mkEl("span", "nv");
  for (const [dir, label, glyph] of [[-1, "previous month", "‹"], [1, "next month", "›"]]) {
    const b = mkEl("button", "", glyph);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.addEventListener("click", () => {
      calMini = new Date(calMini.getFullYear(), calMini.getMonth() + dir, 1);
      calRenderMiniDays(box.querySelector(".cal-mini-g"), today);
      head.firstChild.textContent = calMini.toLocaleString("en-US", { month: "long", year: "numeric" });
    });
    nav.appendChild(b);
  }
  head.appendChild(nav);
  box.appendChild(head);
  const g = mkEl("div", "cal-mini-g");
  box.appendChild(g);
  calRenderMiniDays(g, today);
}
function calRenderMiniDays(g, today) {
  g.replaceChildren();
  for (const n of ["M", "T", "W", "T", "F", "S", "S"]) g.appendChild(mkEl("span", "dw", n));
  const start = calMonday(calMini);
  const week = calMode === "week" || calMode === "day" ? calWeekDates() : [];
  const due = new Set(calOn("plans") ? calPlans.map((p) => p.by) : []);
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    const s = calFmt(d);
    const b = mkEl("button", "");
    b.type = "button";
    b.appendChild(mkEl("span", "", String(d.getDate())));
    b.setAttribute("aria-label", d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }));
    if (d.getMonth() !== calMini.getMonth()) b.classList.add("o");
    if (week.includes(s)) b.classList.add("wk", ...(s === week[0] ? ["l"] : s === week[6] ? ["r"] : []));
    if (s === today) b.classList.add("td");
    if (calMode === "day" && s === calFmt(calAnchor)) b.classList.add("on");
    if (due.has(s)) b.classList.add("pd");
    b.addEventListener("click", () => { calAnchor = d; calGo(); });
    g.appendChild(b);
  }
}

function calRenderLayers() {
  const box = $("#cal-layers");
  box.replaceChildren();
  const today = calFmt(new Date());
  const counts = {
    plans: calPlans.filter((p) => p.by >= today).length,
    tasks: Object.values(calDays).reduce((n, r) => n + (r.tasks || []).filter((t) => t.status === "open").length, 0),
    review: (calDays[today]?.review || []).length,
  };
  for (const [k, label] of CAL_LAYERS) {
    const on = calOn(k);
    const b = mkEl("button", "cal-layer" + (on ? " on" : ""));
    b.type = "button";
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.append(mkEl("span", "cb"), mkEl("span", "cal-g cal-g-" + k), mkEl("span", "", label));
    if (counts[k]) b.appendChild(mkEl("span", "n", String(counts[k])));
    b.addEventListener("click", () => {
      calLayers[k] = !on;
      try { localStorage.setItem(CAL_LAYERS_KEY, JSON.stringify(calLayers)); }
      catch { /* storage refused (a private window): the toggle holds for this page */ }
      renderCalendar();
    });
    box.appendChild(b);
  }
}

// --- week and day: the hours ----------------------------------------------------

function calTimeGrid(dates, withHead) {
  const today = calFmt(new Date());
  const g = mkEl("div", "ct" + (dates.length === 1 ? " one" : ""));
  g.style.setProperty("--n", dates.length);
  if (withHead) {
    const head = mkEl("div", "ct-head");
    head.appendChild(mkEl("div", "ct-gut"));
    for (const date of dates) head.appendChild(calDayHead(date, today));
    g.appendChild(head);
  }
  const band = calBand(dates, today, withHead);
  if (band) g.appendChild(band);
  const scroll = mkEl("div", "ct-scroll");
  const body = mkEl("div", "ct-body");
  const gut = mkEl("div", "ct-gut");
  for (let h = 1; h < 24; h++) {
    const l = mkEl("span", "ct-hl", calHM(h * 60));
    l.style.top = h * CAL_HOUR + "px";
    gut.appendChild(l);
  }
  body.appendChild(gut);
  for (const date of dates) body.appendChild(calDayCol(date, today));
  scroll.appendChild(body);
  scroll.addEventListener("scroll", () => { calScroll = scroll.scrollTop; });
  g.appendChild(scroll);
  requestAnimationFrame(() => { scroll.scrollTop = calScroll; });
  return g;
}

function calDayHead(date, today) {
  const row = calDays[date] || {};
  const d = calDay(date);
  const h = mkEl("div", "ct-dh" + (date === today ? " today" : date < today ? " past" : ""));
  h.appendChild(mkEl("span", "dn", d.toLocaleString("en-US", { weekday: "short" })));
  const nr = mkEl("span", "nr");
  const num = mkEl("button", "num", String(d.getDate()));
  num.type = "button";
  num.setAttribute("aria-label", "open " + d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }));
  num.addEventListener("click", () => { calAnchor = d; calSetMode("day"); });
  nr.appendChild(num);
  if (row.daily && calOn("daily")) nr.appendChild(calDailyBtn(row.daily));
  h.appendChild(nr);
  const sig = calDerived(row);
  const ct = mkEl("span", "ct-ct");
  sig.slice(0, 3).forEach((s, i) => {
    if (i) ct.append(" · ");
    const x = mkEl("span", s.cls, s.text);
    x.title = s.title;
    ct.appendChild(x);
  });
  h.appendChild(ct);
  return h;
}

// The band above the hours: all-day and multi-day events, and the plans. In
// the week a plan still ahead is a countdown bar; in the day it is the chip on
// its `by`, since the day view's strip already counts down.
function calBand(dates, today, planBars) {
  const spans = [];
  const at = new Map(dates.map((d, i) => [d, i]));
  const last = dates[dates.length - 1];
  if (calOn("events")) {
    // Same (stem, start) on consecutive days is one occurrence.
    const seen = new Map();
    dates.forEach((date, i) => {
      for (const e of (calDays[date] || {}).events || []) {
        if (!calIsBar(e)) continue;
        const k = e.stem + "|" + e.start;
        const s = seen.get(k) || { c0: i, c1: i, ev: e };
        s.c1 = i;
        seen.set(k, s);
      }
    });
    spans.push(...seen.values());
  }
  if (calOn("plans")) {
    const barred = new Set();
    if (planBars) {
      for (const p of calPlans) {
        const from = today > dates[0] ? today : dates[0];
        const to = p.by < last ? p.by : last;
        if (from > to || !at.has(from) || !at.has(to)) continue;
        spans.push({ c0: at.get(from), c1: at.get(to), plan: p, open: p.by > last });
        barred.add(p.path);
      }
    }
    dates.forEach((date, i) => {
      for (const p of (calDays[date] || {}).plans || []) {
        if (!barred.has(p.path)) spans.push({ c0: i, c1: i, due: p });
      }
    });
  }
  if (!spans.length) return null;
  const lanes = calPackLanes(spans);
  const band = mkEl("div", "ct-band");
  band.style.gridTemplateRows = `repeat(${lanes}, 24px)`;
  band.appendChild(mkEl("div", "ct-gut", "all day"));
  for (const s of spans) {
    const el = s.ev ? calEventEl(s.ev, "ct-bar", false, false)
      : s.plan ? calPlanBar(s.plan, s.open) : calPlanEl(s.due);
    el.style.gridColumn = `${s.c0 + 2} / ${s.c1 + 3}`;
    el.style.gridRow = String(s.lane + 1);
    band.appendChild(el);
  }
  return band;
}

function calDayCol(date, today) {
  const col = mkEl("div", "ct-col" + (date === today ? " today" : ""));
  col.dataset.date = date;
  const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
  if (calOn("events")) {
    const evs = ((calDays[date] || {}).events || []).filter((e) => !calIsBar(e)).map((e) => {
      const s = calMin(e.start);
      return { e, s, end: Math.max(e.end ? calMin(e.end) : s + 30, s + 20) };
    });
    calPackDay(evs);
    for (const x of evs) {
      const h = (x.end - x.s) * CAL_HOUR / 60 - 2;
      const past = date < today || (date === today && x.end <= nowMin);
      // under 36px the time rides the title's line; under 60px the title
      // keeps one line so the time is never the part cut off
      const el = calEventEl(x.e, "ct-ev" + (past ? " past" : "") + (h < 36 ? " short" : h < 60 ? " s1" : ""), false, true);
      el.style.top = x.s * CAL_HOUR / 60 + "px";
      el.style.height = Math.max(16, h) + "px";
      el.style.left = `calc(${(x.col / x.ncol) * 100}% + 2px)`;
      el.style.width = `calc(${100 / x.ncol}% - 6px)`;
      col.appendChild(el);
    }
  }
  if (date === today) {
    const now = mkEl("div", "ct-now");
    now.style.top = nowMin * CAL_HOUR / 60 + "px";
    col.appendChild(now);
  }
  calDragCreate(col, date);
  return col;
}

// Now is drawn and moves each minute (ADR-0118 point 7); past midnight the
// view is redrawn, since "today" moved with it.
setInterval(() => {
  if (activeTab !== "calendar" || calCard) return;
  const now = $("#cal-grid .ct-now");
  if (!now) return;
  if (now.parentElement.dataset.date !== calFmt(new Date())) { renderCalendar(); return; }
  now.style.top = (new Date().getHours() * 60 + new Date().getMinutes()) * CAL_HOUR / 60 + "px";
}, 60000);

// A press on an empty time is a new event: a click takes the hour from the
// half hour it landed in, a drag takes what it covered, in quarter hours.
function calDragCreate(col, date) {
  col.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    calCloseCard();
    const top = col.getBoundingClientRect().top;
    const quarter = (y) => Math.max(0, Math.min(24 * 60, Math.round((y - top) / CAL_HOUR * 4) * 15));
    const a = Math.min(23 * 60 + 30, Math.floor((ev.clientY - top) / CAL_HOUR * 2) * 30);
    let b = a + 60;
    let moved = false;
    const ghost = mkEl("div", "ct-ghost");
    const paint = () => {
      const s = Math.min(a, b);
      const e = Math.max(a, b);
      ghost.style.top = s * CAL_HOUR / 60 + "px";
      ghost.style.height = Math.max(14, (e - s) * CAL_HOUR / 60 - 2) + "px";
      ghost.textContent = `${calHM(s)} to ${calHM(Math.min(e, 24 * 60 - 1))}`;
    };
    paint();
    col.appendChild(ghost);
    col.setPointerCapture(ev.pointerId);
    const move = (m) => {
      if (!moved && Math.abs(m.clientY - ev.clientY) < 6) return;
      moved = true;
      const y = quarter(m.clientY);
      b = y === a ? a + 15 : y;
      paint();
    };
    const done = (open) => {
      col.removeEventListener("pointermove", move);
      col.removeEventListener("pointerup", up);
      col.removeEventListener("pointercancel", cancel);
      if (!open) { ghost.remove(); return; }
      const s = Math.min(a, b);
      calQuickCard({ date, start: s, end: Math.min(Math.max(a, b), 24 * 60 - 1), ghost }, ghost);
    };
    const up = () => done(true);
    // On a touch screen the column pans (touch-action: pan-y), and a swipe
    // that became a scroll is cancelled: it was never a new event.
    const cancel = () => done(false);
    col.addEventListener("pointermove", move);
    col.addEventListener("pointerup", up);
    col.addEventListener("pointercancel", cancel);
  });
}

// The day: Google's day view laid out as the strip of its week, its hours, and
// beside them what the day asks for (ADR-0118, option C's layout as A's Day).
function calRenderDay(main) {
  const date = calFmt(calAnchor);
  const today = calFmt(new Date());
  const wrap = mkEl("div", "cd");
  wrap.appendChild(calStrip(date, today));
  const body = mkEl("div", "cd-body");
  body.appendChild(calTimeGrid([date], false));
  body.appendChild(calDayStack(calDays[date] || {}, date, today));
  wrap.appendChild(body);
  main.appendChild(wrap);
}

function calStrip(date, today) {
  const strip = mkEl("div", "cd-strip");
  for (const s of calWeekDates()) {
    const row = calDays[s] || {};
    const d = calDay(s);
    const t = mkEl("button", "cd-tile" + (s === date ? " on" : "") + (s === today ? " today" : s < today ? " past" : ""));
    t.type = "button";
    t.setAttribute("aria-label", d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }));
    const th = mkEl("span", "th");
    th.append(mkEl("span", "dn", d.toLocaleString("en-US", { weekday: "short" })), mkEl("span", "n", String(d.getDate())));
    if (row.daily && calOn("daily")) th.appendChild(mkEl("span", "tri"));
    // how full the day is between 08:00 and 20:00, a session in the accent
    const mb = mkEl("span", "mb");
    const timed = calOn("events") ? (row.events || []).filter((e) => !calIsBar(e)) : [];
    for (const e of timed) {
      const a = Math.max(calMin(e.start), 480);
      const b = Math.min(e.end ? calMin(e.end) : calMin(e.start) + 30, 1200);
      if (b <= a) continue;
      const seg = mkEl("i", e.plan ? "s" : "");
      seg.style.left = (a - 480) / 7.2 + "%";
      seg.style.width = (b - a) / 7.2 + "%";
      mb.appendChild(seg);
    }
    const sig = calDerived(row)[0];
    const ct = mkEl("span", "cd-ct" + (sig?.cls ? " " + sig.cls : ""), sig ? sig.text : timed.length ? "" : "free");
    t.append(th, mb, ct);
    t.addEventListener("click", () => { calAnchor = d; renderCalendar(); });
    strip.appendChild(t);
  }
  const plans = mkEl("div", "cd-plans");
  if (calOn("plans")) {
    const ahead = calPlans.filter((p) => p.by >= today).sort((a, b) => (a.by < b.by ? -1 : 1));
    for (const p of ahead.slice(0, 3)) {
      const r = mkEl("button", "cd-plan");
      r.type = "button";
      r.append(mkEl("span", "cal-dia"), mkEl("span", "nm", p.name), mkEl("b", "", calDaysLeft(p.by)));
      r.title = `${p.name}, due ${calShort(calDay(p.by))}: open the plan in Learn`;
      r.addEventListener("click", () => calOpenPlan(p.path));
      plans.appendChild(r);
    }
    if (!ahead.length) {
      plans.appendChild(mkEl("span", "none", "No study plan has an exam day."));
      const a = mkEl("button", "cd-new", "New study plan");
      a.type = "button";
      a.addEventListener("click", () => { location.hash = lrHash({ view: "new" }); });
      plans.appendChild(a);
    }
  }
  strip.appendChild(plans);
  return strip;
}

function calDayStack(row, date, today) {
  const st = mkEl("div", "cd-stack");
  const panel = (title, sub) => {
    const p = mkEl("section", "cd-pnl");
    p.setAttribute("aria-label", title);
    const h = mkEl("div", "ph");
    h.appendChild(mkEl("b", "", title));
    if (sub) h.appendChild(mkEl("span", "", sub));
    p.appendChild(h);
    st.appendChild(p);
    return p;
  };
  const rowBtn = (glyph, text, meta, metaCls, open) => {
    const it = mkEl("button", "cd-row");
    it.type = "button";
    it.append(mkEl("span", "cal-g cal-g-" + glyph), mkEl("span", "t", text));
    if (meta) it.appendChild(mkEl(metaCls || "i", "", meta));
    it.addEventListener("click", open);
    return it;
  };
  // Review lands on today's row only (the agenda's asymmetry), so this panel
  // is today's. Start is Learn's own start, from here.
  const rv = calOn("review") ? row.review || [] : [];
  if (rv.length) {
    const p = panel(`${rv.length} ${rv.length === 1 ? "note" : "notes"} to review`);
    for (const r of rv.slice(0, 4)) {
      const path = r.path || "";
      const cut = path.lastIndexOf("/");
      // the note leads; of its folder only the last step, which is the one
      // that tells two notes apart (the whole path rides the tooltip)
      const folder = cut > 0 ? path.slice(0, cut).split("/").pop() : "";
      const it = rowBtn("review", path.slice(cut + 1).replace(/\.md$/, ""), folder, "i", () => openNote(path));
      it.title = path;
      p.appendChild(it);
    }
    const f = mkEl("div", "pf");
    if (rv.length > 4) f.appendChild(mkEl("span", "", `and ${rv.length - 4} more`));
    const go = mkEl("button", "go", "Start review");
    go.type = "button";
    go.addEventListener("click", () => { showTab("learn"); learnStartSession({ from: "calendar" }); });
    f.appendChild(go);
    p.appendChild(f);
  }
  // A task opens its note at its line. Read-only by decision (ADR-0062
  // Decision 4): ticking one is the Tasks plugin's job, since done also writes
  // the date, the next recurrence and the on-completion action.
  const tk = calOn("tasks") ? row.tasks || [] : [];
  if (tk.length) {
    const p = panel("Tasks", "tick them in their note");
    for (const t of tk) {
      const done = t.status === "done";
      const due = t.due ? (t.due === today ? "today" : calShort(calDay(t.due))) : "";
      const it = rowBtn("tasks", t.text, t.overdue ? "late since " + due : done ? "done" : due ? "due " + due : "",
                        t.overdue ? "em" : "i", () => openNote(t.path, t.line));
      if (done) it.classList.add("done");
      it.title = t.path + ":" + t.line;
      p.appendChild(it);
    }
  }
  if (calOn("daily") && row.daily) {
    const p = panel(row.daily.stem, "daily note");
    p.classList.add("dn");
    const open = mkEl("button", "open", "Open");
    open.type = "button";
    open.addEventListener("click", () => openNote(row.daily.path));
    p.querySelector(".ph").appendChild(open);
    const ex = mkEl("p", "ex");
    p.appendChild(ex);
    fetch(`/note?path=${encodeURIComponent(row.daily.path)}`).then((r) => r.json()).then((d) => {
      // Text only, parsed inert: the excerpt is a reminder of the day, and the
      // note's properties block is the drawer's to show.
      const doc = new DOMParser().parseFromString(d.html || "", "text/html");
      // Its paragraphs: the tasks are listed above, and a list read as prose
      // runs the due dates into the sentences.
      doc.querySelectorAll("details.fm").forEach((n) => n.remove());
      const paras = [...doc.querySelectorAll("p")].map((n) => n.textContent).join(" ");
      const txt = paras.replace(/\s+/g, " ").trim();
      ex.textContent = !txt ? "Nothing written yet." : txt.length > 280 ? txt.slice(0, 280) + "…" : txt;
    }).catch(() => ex.remove());
  }
  const runs = calOn("runs") ? row.activity || [] : [];
  if (runs.length) {
    const p = panel("Silica's runs", `${runs.length} on this day`);
    for (const a of runs.slice(0, CAL_AGENDA_CAP)) {
      const r = calActivity(a);
      const it = mkEl("div", "cd-row log");
      it.append(mkEl("span", "cal-g cal-g-runs"), mkEl("span", "t", (r.verb ? r.verb + " " : "") + r.what), mkEl("i", "", r.detail));
      it.title = a;
      p.appendChild(it);
    }
  }
  const notes = calOn("notes") ? row.notes || [] : [];
  if (notes.length) {
    const p = panel("Dated notes");
    for (const n of notes) {
      const it = mkEl("div", "cd-row log");
      it.append(mkEl("span", "cal-g cal-g-notes"), mkEl("span", "t", n.label));
      p.appendChild(it);
    }
  }
  if (!st.children.length) {
    st.appendChild(mkEl("div", "cd-empty", date === today ? "Nothing else asks for today." : "Nothing else on this day."));
  }
  return st;
}

// --- month ------------------------------------------------------------------------
// Lane-packing for multi-day bars: per week row, spans pack into the first free
// lane, Mon–Sun clipped; lanes cap at 3 and the overflow folds into the day's
// "+N". A click on a day's empty space is a new all-day event there, its number
// opens the day.

function calRenderMonth(main) {
  const m = mkEl("div", "cm");
  const dow = mkEl("div", "cal-dow");
  for (const n of ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) dow.appendChild(mkEl("div", "", n));
  m.appendChild(dow);
  const w = calWindow();
  const dates = [];
  for (let i = 0; i < w.days; i++) {
    const d = new Date(w.start);
    d.setDate(d.getDate() + i);
    dates.push(calFmt(d));
  }
  for (let r = 0; r < dates.length / 7; r++) m.appendChild(calBuildWeek(dates.slice(r * 7, r * 7 + 7)));
  main.appendChild(m);
}

function calBuildWeek(dates) {
  const week = document.createElement("div");
  week.className = "cal-week";

  // Reconstruct spans from the per-day buckets: same (stem, start) on
  // consecutive days is one occurrence.
  const spans = new Map();
  if (calOn("events")) {
    dates.forEach((date, i) => {
      for (const e of (calDays[date] || {}).events || []) {
        if (!calIsBar(e)) continue;
        const k = e.stem + "|" + e.start;
        const s = spans.get(k) || { c0: i, c1: i, ev: e };
        s.c1 = i;
        spans.set(k, s);
      }
    });
  }
  const bars = [...spans.values()];
  const laneTotal = calPackLanes(bars);
  const laneCap = Math.min(laneTotal, CAL_LANE_CAP);
  // repeat(0, …) is invalid CSS and would drop the whole declaration
  week.style.gridTemplateRows =
    laneCap > 0 ? `22px repeat(${laneCap}, 20px) 1fr` : "22px 1fr";

  const todayStr = calFmt(new Date());
  const overflow = new Array(7).fill(0);
  const visMonth = calAnchor.getMonth();

  dates.forEach((date, i) => {
    const cell = document.createElement("div");
    cell.className = "cal-day";
    // Explicit column: a cell with a definite row span but an auto column
    // would be pushed past any bar-occupied column by the sparse placement
    // cursor, spilling days 5..7 into implicit tracks (measured: 10 columns).
    cell.style.gridColumn = String(i + 1);
    if (i === 6) cell.classList.add("c7");
    if (date === todayStr) cell.classList.add("today");
    const d = calDay(date);
    if (d.getMonth() !== visMonth) cell.classList.add("other");
    const num = document.createElement("div");
    num.className = "cal-num";
    const daily = (calDays[date] || {}).daily;
    if (daily && calOn("daily")) num.appendChild(calDailyBtn(daily));
    const nn = mkEl("button", "cal-n", String(d.getDate()));
    nn.type = "button";
    nn.setAttribute("aria-label", "open " + d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }));
    nn.addEventListener("click", (ev) => { ev.stopPropagation(); calAnchor = d; calSetMode("day"); });
    num.appendChild(nn);
    cell.appendChild(num);

    if (laneCap > 0) {
      // Hold the lane band open: chips flow in normal cell layout while bars
      // paint on the overlapping grid rows — without this the first chip
      // renders underneath a bar.
      const spacer = document.createElement("div");
      spacer.style.flex = `0 0 ${laneCap * 20}px`;
      cell.appendChild(spacer);
    }
    const chips = document.createElement("div");
    chips.className = "cal-chips";
    // A deadline leads the day's chips: it is the one an exam week is read for.
    const timed = [
      ...(calOn("plans") ? (calDays[date] || {}).plans || [] : []).map(calPlanEl),
      ...(calOn("events") ? (calDays[date] || {}).events || [] : []).filter((e) => !calIsBar(e))
        .map((e) => calEventEl(e, "cal-chip", true, false)),
    ];
    timed.slice(0, CAL_CHIP_CAP).forEach((el) => chips.appendChild(el));
    overflow[i] += Math.max(0, timed.length - CAL_CHIP_CAP);
    // The derived axes, under whatever appointments the day holds. They are
    // counts of what is already true, so they never take the space an event
    // wanted: the cap is what is left of the cell after the chips.
    const derived = calDerived(calDays[date]);
    const dcap = Math.max(0, CAL_DERIVED_CAP - Math.min(timed.length, CAL_CHIP_CAP));
    derived.slice(0, dcap).forEach((x) => {
      const el = mkEl("div", "cal-sig" + (x.cls ? " " + x.cls : ""), x.text);
      el.title = x.title;
      chips.appendChild(el);
    });
    overflow[i] += Math.max(0, derived.length - dcap);
    cell.appendChild(chips);

    const more = document.createElement("div");
    more.className = "cal-more";
    cell.appendChild(more);

    cell.addEventListener("click", () => calQuickCard({ date, allDay: true }, cell));
    week.appendChild(cell);
  });

  for (const s of bars) {
    if (s.lane >= laneCap) {
      for (let c = s.c0; c <= s.c1; c++) overflow[c] += 1;
      continue;
    }
    const bar = calEventEl(s.ev, "cal-bar", false, false);
    bar.style.gridColumn = `${s.c0 + 1} / ${s.c1 + 2}`;
    bar.style.gridRow = String(s.lane + 2);
    week.appendChild(bar);
  }
  // The day view holds what the cell could not; a click on the rest of the
  // cell is a new event, so this one stops there.
  overflow.forEach((n, i) => {
    if (n <= 0) return;
    const more = mkEl("button", "cal-more", `+${n} more`);
    more.type = "button";
    more.addEventListener("click", (ev) => { ev.stopPropagation(); calAnchor = calDay(dates[i]); calSetMode("day"); });
    week.children[i].querySelector(".cal-more").replaceWith(more);
  });
  return week;
}

// --- agenda -------------------------------------------------------------------------

function calAgendaDay(row) {
  const sec = document.createElement("div");
  sec.className = "cal-ag-day";
  const d = calDay(row.date);
  const h = document.createElement("div");
  h.className = "cal-ag-date";
  h.textContent = calShort(d);
  const wd = document.createElement("span");
  wd.className = "wd";
  wd.textContent = d.toLocaleString("en-US", { weekday: "long" });
  h.appendChild(wd);
  sec.appendChild(h);

  let any = false;
  // An axis renders six rows and then says how many it is holding back. A
  // nucleation run puts sixty lines on one day, and sixty rows of a path with
  // a run id after it is not an agenda, it is a log file in a 300px column.
  const axis = (kind, items, build) => {
    if (!items?.length) return;
    any = true;
    const rows = items.map((x) => {
      const it = mkEl("div", "cal-ag-item");
      const k = mkEl("span", "when cal-ag-kind", kind);
      it.appendChild(k);
      build(it, x);
      return it;
    });
    rows.slice(0, CAL_AGENDA_CAP).forEach((r) => sec.appendChild(r));
    if (rows.length <= CAL_AGENDA_CAP) return;
    const more = mkEl("button", "cal-ag-more",
      `show ${rows.length - CAL_AGENDA_CAP} more`);
    more.type = "button";
    more.addEventListener("click", () => {
      rows.slice(CAL_AGENDA_CAP).forEach((r) => sec.insertBefore(r, more));
      more.remove();
    });
    sec.appendChild(more);
  };
  // The daily note heads the day, above its appointments: it is where the day
  // was written down (ADR-0062).
  axis("daily", calOn("daily") && row.daily ? [row.daily] : [], (it, dn) => {
    const t = mkEl("span", "cal-ag-txt", dn.stem);
    it.title = dn.path;
    it.classList.add("clickable");
    it.addEventListener("click", () => openNote(dn.path));
    it.appendChild(t);
  });
  for (const e of calOn("events") ? row.events || [] : []) {
    any = true;
    const it = mkEl("div", "cal-ag-item ev" + (e.status ? " " + e.status : ""));
    it.appendChild(mkEl("span", "when", e.all_day ? "all day" : e.start.slice(11, 16)));
    const ti = mkEl("span", "", e.title);
    if (e.plan) ti.prepend(mkEl("span", "cal-dia"));
    it.appendChild(ti);
    it.addEventListener("click", () => calEventCard(e, it));
    sec.appendChild(it);
  }

  axis("plan", calOn("plans") ? row.plans : [], (it, p) => {
    it.appendChild(mkEl("span", "cal-ag-txt", (p.status === "completed" ? "✓ " : "") + p.name));
    it.title = "study plan due · " + p.path;
    it.classList.add("clickable");
    if (p.status === "completed") it.classList.add("done");
    it.addEventListener("click", () => calOpenPlan(p.path));
  });

  // A task opens its note at its line. The box is read-only by decision
  // (ADR-0062 Decision 4): ticking one is the Tasks plugin's job, since done
  // also writes the date, the next recurrence and the on-completion action.
  axis("task", calOn("tasks") ? row.tasks : [], (it, t) => {
    const txt = mkEl("span", "cal-ag-txt", (t.status === "done" ? "☑ " : "☐ ") + t.text);
    if (t.overdue) txt.appendChild(mkEl("i", "late", " due " + t.due));
    it.title = t.path + ":" + t.line;
    it.classList.add("clickable");
    if (t.status === "done") it.classList.add("done");
    it.addEventListener("click", () => openNote(t.path, t.line));
    it.appendChild(txt);
  });
  axis("note", calOn("notes") ? row.notes : [], (it, n) => it.appendChild(document.createTextNode(n.label)));
  // Verb, then the note it touched, then what it did to it — three fields
  // instead of one sentence, because the file name is the part you scan for.
  axis("agent", calOn("runs") ? row.activity : [], (it, a) => {
    const p = calActivity(a);
    it.title = a;
    const t = mkEl("span", "cal-ag-txt");
    if (p.verb) t.appendChild(mkEl("b", "", p.verb + " "));
    t.appendChild(document.createTextNode(p.what));
    if (p.detail) t.appendChild(mkEl("i", "", " " + p.detail));
    it.appendChild(t);
  });
  // The queue names a path; a path in a narrow column ellipsises to its
  // folder, which is the half you already know. The note leads, the folder
  // trails at meta weight, and the whole path stays on the tooltip.
  axis("review", calOn("review") ? row.review : [], (it, r) => {
    const path = r.path || "";
    const cut = path.lastIndexOf("/");
    const t = mkEl("span", "cal-ag-txt");
    t.appendChild(document.createTextNode(path.slice(cut + 1).replace(/\.md$/, "")));
    if (cut > 0) t.appendChild(mkEl("i", "", " " + path.slice(0, cut)));
    it.title = path;
    it.classList.add("clickable");
    it.addEventListener("click", () => openNote(path));
    it.appendChild(t);
  });
  if (!any) {
    const e = document.createElement("div");
    e.className = "cal-ag-empty";
    e.textContent = "nothing scheduled";
    sec.appendChild(e);
  }
  return sec;
}

// The one reading in this app whose axis is time and not space, so it rides the
// only tab whose axis is time. Above the days rather than beside them: it frames
// what follows ("this fortnight has been about X"), and a frame printed after
// the thing it frames is a footnote.
//
// Concepts, not notes, so the pills do not open anything: clicking one lights
// its notes in the graph, the same verb the note drawer's concept cloud has.
function calBurstStrip() {
  if (!calBursting.length) return null;
  const box = mkEl("div", "cal-burst");
  box.appendChild(mkEl("div", "cal-ag-head", "this fortnight of writing"));
  const pills = mkEl("div", "cal-burst-p");
  for (const b of calBursting) {
    const pill = mkEl("button", "wk-pill", b.concept);
    pill.type = "button";
    pill.title = `${b.recent} of the ${b.total} notes carrying it were written in the `
      + `window (z ${b.z}): light its notes in the graph`;
    pill.addEventListener("click", () => lightConcept(b.concept, pill));
    pills.appendChild(pill);
  }
  box.appendChild(pills);
  return box;
}

// The agenda view is the list the side panel was before ADR-0118: seven days
// from the day it is anchored on, every axis a row.
function renderCalAgenda() {
  const panel = mkEl("div", "cal-ag");
  $("#cal-grid").appendChild(panel);
  const burst = calBurstStrip();
  if (burst) panel.appendChild(burst);
  const head = document.createElement("div");
  head.className = "cal-ag-head";
  const w = calWindow();
  head.textContent = calFmt(w.start) === calFmt(new Date()) ? "next 7 days" : "7 days from " + calShort(w.start);
  panel.appendChild(head);
  for (const row of Object.values(calDays).sort((a, b) => (a.date < b.date ? -1 : 1))) {
    panel.appendChild(calAgendaDay(row));
  }
}

// --- the cards ----------------------------------------------------------------------

function calCloseCard() {
  if (!calCard) return;
  calCard.el.remove();
  calCard.ghost?.remove();
  calCard = null;
  if (calPending) { calPending = false; renderCalendar(); }
}
// Beside what it is about, on the side with room, inside the view.
function calPlaceCard(el, anchor) {
  const host = $("#view-calendar");
  host.appendChild(el);
  const hr = host.getBoundingClientRect();
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  let x = r.right + 10 - hr.left;
  if (x + w > hr.width - 8) x = r.left - 10 - w - hr.left;
  if (x < 8) x = Math.max(8, Math.min(hr.width - w - 8, r.left - hr.left));
  const y = Math.max(8, Math.min(r.top - hr.top, hr.height - h - 8));
  el.style.left = x + "px";
  el.style.top = y + "px";
}
document.addEventListener("pointerdown", (ev) => {
  if (calCard && !calCard.el.contains(ev.target)) calCloseCard();
}, true);

const CAL_REPEAT = (d) => [
  ["", "Does not repeat"],
  ["FREQ=DAILY", "Every day"],
  [`FREQ=WEEKLY;BYDAY=${["SU", "MO", "TU", "WE", "TH", "FR", "SA"][d.getDay()]}`,
   "Every week on " + d.toLocaleString("en-US", { weekday: "long" })],
  ["FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "Every weekday"],
  ["FREQ=MONTHLY", "Every month on day " + d.getDate()],
  ["FREQ=YEARLY", "Every year"],
];
const CAL_REMIND = [["", "No reminder"], ["0m", "At the start"], ["10m", "10 minutes before"],
                    ["30m", "30 minutes before"], ["1h", "1 hour before"], ["1d", "1 day before"]];

function calSelect(options, value, label) {
  const s = document.createElement("select");
  s.setAttribute("aria-label", label);
  for (const [v, t] of options) s.appendChild(Object.assign(document.createElement("option"), { value: v, textContent: t }));
  s.value = value;
  return s;
}

// The quick card (ADR-0118 point 1): a title, the time it was opened on, the
// kind, and Save, which writes the event note and says where before it does.
function calQuickCard(at, anchor) {
  calCloseCard();
  const today = calFmt(new Date());
  const plans = calPlans.filter((p) => p.by >= today);
  const el = mkEl("form", "cal-card cq");
  el.setAttribute("aria-label", "new event");
  const x = mkEl("button", "x", "×");
  x.type = "button";
  x.setAttribute("aria-label", "close");
  x.addEventListener("click", calCloseCard);
  const title = mkEl("input", "cq-title");
  title.placeholder = "Add a title";
  title.setAttribute("aria-label", "title");
  title.maxLength = 200;
  el.append(x, title);

  // Event or study session; a session is offered only while a plan is ahead.
  let session = false;
  const plan = calSelect(plans.map((p) => [p.path, p.name]), plans[0]?.path || "", "the plan this session is for");
  const planRow = mkEl("label", "cq-row cq-plan");
  planRow.append(mkEl("span", "k", "for"), plan);
  planRow.hidden = true;
  if (plans.length) {
    const kind = mkEl("div", "cq-kind");
    kind.setAttribute("role", "group");
    kind.setAttribute("aria-label", "kind");
    for (const [v, label] of [[false, "Event"], [true, "Study session"]]) {
      const b = mkEl("button", v === session ? "active" : "", label);
      b.type = "button";
      if (v) b.prepend(mkEl("span", "cal-dia"));
      b.addEventListener("click", () => {
        session = v;
        kind.querySelectorAll("button").forEach((o) => o.classList.toggle("active", o === b));
        planRow.hidden = !session;
        if (session && !title.value.trim()) title.placeholder = "What will you study?";
      });
      kind.appendChild(b);
    }
    el.append(kind, planRow);
  }

  const date = Object.assign(document.createElement("input"), { type: "date", value: at.date });
  date.setAttribute("aria-label", "day");
  // 23:59 at most: a time input refuses "24:00" and empties itself
  const t1 = Object.assign(document.createElement("input"), { type: "time", step: 900, value: calHM(Math.min(at.start ?? 9 * 60, 24 * 60 - 1)) });
  const t2 = Object.assign(document.createElement("input"), { type: "time", step: 900, value: calHM(Math.min(at.end ?? (at.start ?? 9 * 60) + 60, 24 * 60 - 1)) });
  t1.setAttribute("aria-label", "from");
  t2.setAttribute("aria-label", "to");
  const allDay = Object.assign(document.createElement("input"), { type: "checkbox", checked: !!at.allDay });
  const adl = mkEl("label", "cq-ad");
  adl.append(allDay, " All day");
  const when = mkEl("div", "cq-row");
  const times = mkEl("span", "cq-times");
  times.append(t1, mkEl("span", "k", "to"), t2);
  when.append(date, times, adl);
  const syncAllDay = () => { times.hidden = allDay.checked; };
  allDay.addEventListener("change", syncAllDay);
  syncAllDay();
  const repeat = calSelect(CAL_REPEAT(calDay(at.date)), "", "repeat");
  const remind = calSelect(CAL_REMIND, "", "reminder");
  const opts = mkEl("div", "cq-row");
  opts.append(repeat, remind);
  const path = mkEl("div", "cq-path");
  const err = mkEl("div", "cq-err");
  err.setAttribute("role", "alert");
  err.hidden = true;
  const foot = mkEl("div", "cq-foot");
  const more = mkEl("button", "cq-more", "More options");
  more.type = "button";
  more.title = "save, then open the note";
  const save = mkEl("button", "cq-save", "Save");
  save.type = "submit";
  foot.append(more, save);
  el.append(when, opts, path, err, foot);

  const values = () => {
    const d = date.value;
    return {
      title: title.value.trim(),
      start: allDay.checked ? d : `${d} ${t1.value}`,
      end: allDay.checked || t2.value <= t1.value ? "" : `${d} ${t2.value}`,
      rrule: repeat.value, reminder: remind.value, plan: session ? plan.value : "",
    };
  };
  // The path is the server's, from the same function that will write it: a
  // client-side guess would miss the write dir and the " 2" a clash adds.
  let previewing = 0;
  const preview = () => {
    const v = values();
    const n = ++previewing;
    if (!v.title || !date.value) { path.textContent = "Saved as a note in calendar/"; return; }
    const q = new URLSearchParams({ title: v.title, start: v.start, rrule: v.rrule });
    fetch("/calendar/event/path?" + q).then((r) => r.json()).then((d) => {
      if (n === previewing) path.textContent = d.path ? "Saved as " + d.path : d.error;
    }).catch(() => { /* the preview is a courtesy; Save reports the real path */ });
  };
  let pt = 0;
  for (const i of [title, date, repeat, allDay]) {
    i.addEventListener("input", () => { clearTimeout(pt); pt = setTimeout(preview, 180); });
  }
  date.addEventListener("change", () => {
    const keep = repeat.selectedIndex;
    repeat.replaceChildren(...calSelect(CAL_REPEAT(calDay(date.value)), "", "repeat").children);
    repeat.selectedIndex = keep;
  });
  preview();

  const submit = async (openAfter) => {
    const v = values();
    if (!v.title) { title.focus(); return; }
    // "YYYY-MM-DD " would be read as an all-day event, saved without a word
    if (!date.value || (!allDay.checked && !t1.value)) {
      err.textContent = !date.value ? "pick a day" : "pick a start time, or make it all day";
      err.hidden = false;
      return;
    }
    save.disabled = more.disabled = true;
    err.hidden = true;
    try {
      const r = await fetch("/calendar/event", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(v),
      }).then((x) => x.json());
      if (r.error) { err.textContent = r.error; err.hidden = false; return; }
      calCloseCard();
      notify("saved " + r.path, "info");
      if (openAfter) openNote(r.path);
      // Drawn now, on its day, and confirmed by the refetch: GET /calendar
      // takes 1.6 s on a 750-note vault (measured 2026-10-16), and an event
      // that appears that long after Save reads as a save that failed.
      const row = calDays[date.value];
      if (row) {
        row.events = [...(row.events || []), {
          stem: r.path.split("/").pop().replace(/\.md$/, ""), title: v.title, path: r.path,
          start: v.start, end: v.end, all_day: allDay.checked, status: "",
          rrule: v.rrule, reminder: v.reminder, plan: v.plan.replace(/\.md$/, ""),
        }];
        renderCalendar();
      }
      loadCalendar();
    } catch {
      err.textContent = "couldn't save: the server did not answer";
      err.hidden = false;
    } finally { save.disabled = more.disabled = false; }
  };
  el.addEventListener("submit", (ev) => { ev.preventDefault(); submit(false); });
  more.addEventListener("click", () => submit(true));
  el.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); calCloseCard(); } });
  calCard = { el, ghost: at.ghost };
  calPlaceCard(el, anchor);
  title.focus();
}

function calRepeatText(rr) {
  const f = (rr.match(/FREQ=(\w+)/) || [])[1];
  const by = (rr.match(/BYDAY=([\w,]+)/) || [])[1];
  if (f === "WEEKLY" && by === "MO,TU,WE,TH,FR") return "every weekday";
  if (f === "WEEKLY" && by) return "every week on " + by.split(",").map((x) => x[0] + x[1].toLowerCase()).join(", ");
  return { DAILY: "every day", WEEKLY: "every week", MONTHLY: "every month", YEARLY: "every year" }[f] || rr;
}

// The event card (ADR-0118 point 6): what the note says, then Open note, Done
// and Cancel. Done and Cancel close a recurring series, as the tool does.
function calEventCard(e, anchor) {
  calCloseCard();
  const el = mkEl("div", "cal-card ce");
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", e.title);
  const x = mkEl("button", "x", "×");
  x.type = "button";
  x.setAttribute("aria-label", "close");
  x.addEventListener("click", calCloseCard);
  const h = mkEl("div", "ce-title", e.title);
  if (e.plan) h.prepend(mkEl("span", "cal-dia"));
  el.append(x, h);
  const d = calDay(e.start.slice(0, 10));
  el.appendChild(mkEl("div", "ce-row", d.toLocaleString("en-US", { weekday: "long", month: "long", day: "numeric" }) + " · " + calSpan(e)));
  if (e.rrule) el.appendChild(mkEl("div", "ce-row", "Repeats " + calRepeatText(e.rrule)));
  if (e.reminder) {
    const lead = e.reminder === "0m" ? "at the start" : e.reminder.replace(/(\d+)([mhd])/, (_, n, u) => `${n} ${{ m: "min", h: "h", d: "d" }[u]} before`);
    el.appendChild(mkEl("div", "ce-row", "Reminds " + lead));
  }
  if (e.plan) {
    const p = calPlans.find((q) => q.path.replace(/\.md$/, "") === e.plan);
    const b = mkEl("button", "ce-link", "Session for " + (p ? p.name : e.plan.split("/").pop()));
    b.type = "button";
    b.addEventListener("click", () => { calCloseCard(); calOpenPlan(p ? p.path : e.plan + ".md"); });
    el.appendChild(b);
  }
  if (e.status) el.appendChild(mkEl("div", "ce-row ce-st", e.status === "done" ? "Done" : "Cancelled"));
  el.appendChild(mkEl("div", "cq-path", e.path));
  const foot = mkEl("div", "cq-foot");
  const open = mkEl("button", "cq-more", "Open note");
  open.type = "button";
  open.addEventListener("click", () => { calCloseCard(); openNote(e.path); });
  foot.appendChild(open);
  // Done once it has started: the kernel hides a closed event's occurrences
  // from now on, so Done on a future one would make it vanish like a cancel.
  const now = new Date();
  const started = e.start <= (e.all_day ? calFmt(now) : `${calFmt(now)} ${calHM(now.getHours() * 60 + now.getMinutes())}`);
  if (!e.status) {
    for (const [status, label] of [["cancelled", "Cancel event"], ...(started ? [["done", "Done"]] : [])]) {
      const b = mkEl("button", status === "done" ? "cq-save" : "ce-cancel", label);
      b.type = "button";
      if (e.rrule) b.title = "closes the whole series";
      b.addEventListener("click", () => calSetStatus(e, status));
      foot.appendChild(b);
    }
  }
  el.appendChild(foot);
  el.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); calCloseCard(); } });
  calCard = { el };
  calPlaceCard(el, anchor);
  open.focus();
}

async function calSetStatus(e, status) {
  let r;
  try {
    r = await fetch("/calendar/event/status", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: e.path, status }),
    }).then((x) => x.json());
  } catch { r = { error: "couldn't reach the server" }; }
  if (r.error) { notify(r.error); return; }
  calCloseCard();
  loadCalendar();
}

function calNewEvent(anchor) {
  const now = new Date();
  const base = calMode === "month" || calFmt(calAnchor) !== calFmt(now) ? calAnchor : now;
  const start = base === now ? Math.min(23 * 60, (now.getHours() + 1) * 60) : 9 * 60;
  calQuickCard({ date: calFmt(base), start, end: start + 60 }, anchor);
}

// --- controls -----------------------------------------------------------------------

$("#cal-prev").addEventListener("click", () => calStep(-1));
$("#cal-next").addEventListener("click", () => calStep(1));
$("#cal-today").addEventListener("click", () => { calAnchor = new Date(); calGo(); });
$("#cal-mode").addEventListener("click", (e) => {
  const m = e.target.closest("button")?.dataset.calmode;
  if (m && m !== calMode) calSetMode(m);
});
for (const id of ["#cal-new", "#cal-new-bar"]) {
  $(id).addEventListener("click", (e) => { e.stopPropagation(); calNewEvent(e.currentTarget); });
}

// Google's keys (ADR-0118 point 8), only with the calendar on screen and the
// focus on it or on nothing: a key typed in the note drawer or a field is text.
document.addEventListener("keydown", (e) => {
  if (activeTab !== "calendar" || e.altKey || e.ctrlKey || e.metaKey) return;
  const tgt = e.target;
  if (tgt && tgt !== document.body && !tgt.closest?.("#view-calendar")) return;
  if (tgt?.closest?.("input, textarea, select, [contenteditable], .cal-card")) return;
  const modes = { d: "day", w: "week", m: "month", a: "agenda" };
  const k = e.key;
  if (modes[k]) calSetMode(modes[k]);
  else if (k === "t") { calAnchor = new Date(); calGo(); }
  else if (k === "j" || k === "n" || k === "ArrowRight") calStep(1);
  else if (k === "k" || k === "p" || k === "ArrowLeft") calStep(-1);
  else if (k === "c") calNewEvent($("#cal-grid"));
  else return;
  e.preventDefault();
});

// --- vault freshness ---------------------------------------------------------
// The explore surfaces are built once and cached, and until now the only thing
// that invalidated them was a chat turn in THIS tab — so an Obsidian edit, or a
// `silica nucleate` in a terminal, left them drawing a vault that no longer
// existed. /vault_version is a digest of the note roster plus the derived
// indexes; when it moves, so did the vault.
//
// A poll and not the BUS the turn stream already runs on: that bus is
// in-process, so it carries this browser's own agent and nothing else, and the
// writes this exists for are precisely the ones from outside it.
let vaultVersion = null;

// Cheap surfaces redraw; the expensive one offers. Rebuilding the graph
// document costs the camera, the zoom and the focused node — the reader's place
// in it — so out-of-band changes never take that away, they put a button up.
function markVaultChanged() {
  graphStale = true;
  metricsStale = true;
  // The one place shape is dropped: the other two `graphStale` sites are a
  // theme flip and a render setting, and neither moves a note. folders/areas/
  // read take their colours from tokens, so they survive both untouched.
  shapeData = null;
  if (activeTab !== "graph") return; // rebuilt on the way back into the tab
  if (graphMode in SHAPE_VIEWS) drawShape();
  syncRefreshCue();
}

// Derived state, never a flag someone has to remember to clear: the offer shows
// exactly when the graph is the surface on screen AND it is out of date. On map
// or folders/areas/read it must not, because those redrew themselves — an offer
// there would point at a staleness the reader cannot see, on a view that has
// none.
//
// A covers pass (ADR-0066 decision 6) borrows the chip: it counts the papers
// read while the pass runs, then offers the rebuild that draws it.
function syncRefreshCue() {
  const btn = $("#graph-refresh");
  const cue = window.currentCoversCue ? currentCoversCue() : null;
  btn.hidden = !(graphMode === "graph" && (graphStale || cue));
  btn.disabled = !!(cue && cue.busy);
  btn.textContent = cue ? cue.text : "⟳ vault changed";
  btn.title = cue ? cue.title : "the vault changed since this graph was built";
}

async function pollVaultVersion() {
  if (document.visibilityState !== "visible") return; // no view to keep fresh
  try {
    const { version } = await (await fetch("/vault_version")).json();
    if (!version) return;            // unreadable vault answers "": nothing to say
    if (vaultVersion === null) vaultVersion = version; // first read is the baseline
    else if (version !== vaultVersion) { vaultVersion = version; markVaultChanged(); }
  } catch { /* a stale view is not worth a toast; the next tick retries */ }
}
setInterval(pollVaultVersion, 15000);
// Coming back from Obsidian is the whole scenario, and it is a tab switch —
// waiting out the interval would show the stale graph for the worst 15 seconds.
document.addEventListener("visibilitychange", pollVaultVersion);
pollVaultVersion();

// The poll is the tick: the endpoint computes due, advances the sidecar marks,
// and returns what to show. At-most-once shared with the REPL daemon. Gated on
// visibility like the vault poll (audit P15): a hidden tab has nobody to tell,
// and a reminder that fires while hidden is held for the next visible tick
// rather than lost to a toast nobody saw.
const heldReminders = [];
function showReminders(due) {
  for (const r of due) {
    notify((r.late ? "late reminder: " : "reminder: ") + r.title + " · " + r.start.slice(0, 16), "info");
  }
}
async function pollReminders() {
  try {
    const d = await (await fetch("/reminders", { method: "POST" })).json();
    const due = d.due || [];
    if (document.visibilityState !== "visible") { heldReminders.push(...due); return; }
    showReminders(heldReminders.splice(0));
    showReminders(due);
  } catch { /* a reminder is a courtesy; the next tick retries */ }
}
setInterval(pollReminders, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && heldReminders.length) showReminders(heldReminders.splice(0));
});

buildSurfaceTabs();

// Land on chat — it's the primary surface — unless the URL names another view
// (#explore, #calendar, #examine): a pasted deep link must win over the default.
// Read the hash BEFORE the default click: showTab rewrites it via replaceState.
// parseHash and not a split on "/": #chat?note=… has no "/", and its whole
// text was taken for the slug, an unknown view.
const bootSlug = parseHash().slug;
const bootTab = viewOfSlug(bootSlug);
// Chat has no row since ADR-0084, so it is the one view entered by call.
if (["graph", "learn", "calendar", "metrics"].includes(bootTab))
  document.querySelector(`#views .tab[data-tab="${bootTab}"]`).click();
else showTab("chat");

// AFTER the boot tab, not with the rest of the drawer's setup: switching tabs
// closes the sidebar, and a restore that ran before this line was undone by it
// on every load - the panel opened and shut inside one frame, which reads as the
// preference never having been saved. Closed until asked for, then remembered:
// the sidebar on `work` narrates a run, and the state it would open on by
// default is the state it spends most of its time in - empty, beside a
// transcript that is 630px narrower for it.
