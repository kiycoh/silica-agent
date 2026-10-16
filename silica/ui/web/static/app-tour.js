// --- the first-visit tour (ADR-0105) -----------------------------------------
// Four numbered steps on the student's own file. Each lights the real control it
// is about and moves on when the thing it asks for has happened, read off the
// events the page already raises. It never sends: the question and the quiz are
// prefilled and the turn stays the student's, as the chat's + does (ADR-0083).

const TOUR_KEY = "silica.tour";
// The field's visible box: #composer is the whole width of the view.
const TOUR_COMPOSER = "#composer .composer-shell";
const tourLastAnswer = () => [...document.querySelectorAll("#log .msg.silica")].pop();

// The steps by name, in order of `next`. `n` is the number the student sees; a
// step's second half (cite, map) keeps its number. `wait` names what moves it
// on when it has no button: a note in the tree, a turn that ended well, a view.
const TOUR_STEPS = {
  welcome: {
    n: 0, dim: "all",
    body: "Silica works on your own notes. Four steps, about three minutes: bring in a file, ask about it, see it on the map, get quizzed on it.",
    primary: ["Start", () => tourGo("file")],
    secondary: ["Later", () => tourEnd()],
  },
  file: {
    n: 1, target: "#vault-add", prepare: () => tourOpenDrawer(), wait: "note",
    body: "Add your notes here: a PDF, a .md or a .txt. Each file becomes a note in your Inbox.",
    link: ["only have a link?", () => tourGo("fetch")],
  },
  fetch: {
    n: 1, target: TOUR_COMPOSER, prepare: () => prefillChat("/fetch "), wait: "note",
    body: "Paste the link after /fetch and send it. Silica reads the page into your Inbox.",
  },
  ask: {
    n: 2, target: TOUR_COMPOSER, wait: "turn",
    prepare: () => prefillChat(/\.md$/i.test(tour.note)
      ? `What are the key points of [[${tour.note.replace(/\.md$/i, "")}]]? `
      : `What are the key points of the notes in "${tour.note}"? `),
    body: "Ask about it. The question is written for you: send it, or write your own.",
  },
  cite: {
    // Where the answer says what it stands on (ADR-0117), not the answer: a
    // long one is taller than the window and lights nothing. A cited note is a
    // numbered link in the text; a note read and not cited is named on the
    // line above the answer.
    n: 2, target: () => tourSource() || tourLastAnswer(),
    body: () => {
      const s = tourSource();
      if (s?.dataset.cite) return "The answer comes from your notes: each numbered link is one of them. Click it to open the note.";
      if (s) return "The answer comes from your notes: the ones it read are named above it. Click one to open it.";
      return "The answer comes from the notes in your vault.";
    },
    primary: ["Next", () => tourGo("explore")],
  },
  explore: {
    n: 3, target: '.tab[data-tab="graph"]', wait: "graph",
    body: "Now open Explore.",
  },
  map: {
    n: 3, dim: "none",
    body: "Every note is a dot on this map, and notes that say similar things sit close together. It grows with every file you add.",
    primary: ["Next", () => tourGo("quiz")],
  },
  quiz: {
    n: 4, target: TOUR_COMPOSER, wait: "turn",
    prepare: () => prefillChat(`/quiz "${tour.note}" --n=3`),
    body: "Last one: Silica asks you three questions on your note. Send it, then answer in the chat.",
  },
  learn: {
    n: 4, target: '.tab[data-tab="learn"]',
    body: "What you get wrong comes back here to review. You can take this tour again from Shortcuts and help.",
    primary: ["Done", () => tourEnd()],
  },
};
// A reload lands on the half of a step that can be drawn again from nothing:
// the answer a `cite` pointed at and the frame a `map` sat on are gone.
function tourSource() {
  const a = tourLastAnswer();
  return a?.querySelector(".stream-text .note-link[data-cite]") || a?.querySelector(".lg-reads") || null;
}
const TOUR_RESUME = { fetch: "file", cite: "explore", map: "quiz" };

// {step, note} while it runs. `note` is what the steps name: the note, or for a
// document split into parts (convert.py, over 40,000 characters) the folder the
// parts are in, because its first part is often only the title page.
let tour = null;
const tourJobs = new Set(); // conversions step 1 saw start and has not seen end

const tourSpot = document.createElement("div");
tourSpot.id = "tour-spot";
tourSpot.hidden = true;
const tourTip = document.createElement("div");
tourTip.id = "tour-tip";
tourTip.hidden = true;
tourTip.setAttribute("role", "dialog");
tourTip.setAttribute("aria-label", "guided tour");
document.body.append(tourSpot, tourTip);

function tourSave() {
  try { localStorage.setItem(TOUR_KEY, JSON.stringify(tour || "off")); } catch { /* private window: the tour still runs, it just cannot resume */ }
}
function tourSaved() {
  try { return JSON.parse(localStorage.getItem(TOUR_KEY)); } catch { return null; }
}

// The first note the tree lists. Only markdown notes are tree rows, so a PDF
// still converting is never mistaken for the note it will become.
const tourFirstNote = () => document.querySelector("#tree .tree-note")?.dataset.id || null;

// The vault's + lives in the drawer: below the fold (isNarrow, the one media
// query) the drawer is the rail and is summoned over the page (ADR-0084);
// above it, it may be folded.
function tourOpenDrawer() {
  if (isNarrow()) { if (railSection !== "side-vault") toggleRail("side-vault"); }
  else if (document.body.classList.contains("sidebar-collapsed")) toggleSidebar();
}

// The control a step lights is the first match that has a box: the views are
// rows in the drawer and icons on the rail, and one of the two is laid out.
function tourTarget(s) {
  if (!s.target) return null;
  if (typeof s.target === "function") return s.target() || null;
  for (const el of document.querySelectorAll(s.target)) {
    const r = el.getBoundingClientRect();
    if (r.width && r.height) return el;
  }
  return null;
}

function tourButton(cls, [label, act]) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = label;
  // Stopped here: the document's outside-click handlers would otherwise close
  // the rail that "Start" has just summoned for step 1 (found at 1024 px).
  b.addEventListener("click", (e) => { e.stopPropagation(); act(); });
  return b;
}

function tourRender() {
  const s = TOUR_STEPS[tour.step];
  tourTip.replaceChildren();
  const head = document.createElement("div");
  head.className = "tour-head";
  const count = document.createElement("span");
  count.textContent = s.n ? `Step ${s.n} of 4` : "Welcome";
  const close = tourButton("tour-x", ["✕", () => tourEnd()]);
  close.setAttribute("aria-label", "close the tour");
  head.append(count, close);
  const body = document.createElement("p");
  body.className = "tour-body";
  body.id = "tour-body";
  body.textContent = typeof s.body === "function" ? s.body() : s.body;
  // The status line is where step 1 says a conversion is running or why it
  // failed: polite, so a screen reader hears it without losing its place.
  const status = document.createElement("p");
  status.className = "tour-status";
  status.setAttribute("aria-live", "polite");
  status.hidden = true;
  const acts = document.createElement("div");
  acts.className = "tour-acts";
  if (s.link) acts.append(tourButton("tour-link", s.link));
  if (s.secondary) acts.append(tourButton("tour-btn", s.secondary));
  const primary = s.primary && tourButton("tour-btn primary", s.primary);
  if (primary) acts.append(primary);
  tourTip.append(head, body, status);
  if (acts.childElementCount) tourTip.append(acts);
  tourTip.setAttribute("aria-describedby", "tour-body");
  tourTip.hidden = false;
  tourPlaced = "";
  tourPlace();
  if (!tourRaf) tourRaf = requestAnimationFrame(tourLoop);
  if (primary) primary.focus();
}

function tourStatus(text, failed = false) {
  const el = tourTip.querySelector(".tour-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("failed", failed);
  el.hidden = !text;
}

// Placed the way the shared tooltip is (app-boot.js showTip): next to a narrow
// control, under or over a wide one, clamped to the window. Read every frame
// while the tour runs, because what moves the control (the drawer sliding, the
// rail summoned, the composer growing) raises no event of its own; the styles
// are written only when the numbers change.
let tourPlaced = "";
let tourRaf = 0;
function tourPlace() {
  if (!tour) return;
  const s = TOUR_STEPS[tour.step];
  const el = tourTarget(s);
  const w = tourTip.offsetWidth, h = tourTip.offsetHeight, gap = 12, m = 8, pad = 6;
  const vw = innerWidth, vh = innerHeight;
  let spot = null, x, y;
  if (el) {
    const r = el.getBoundingClientRect();
    spot = [r.left - pad, r.top - pad, r.width + 2 * pad, r.height + 2 * pad];
    if (r.width < vw / 3 && r.right + gap + w <= vw - m) { x = r.right + gap; y = r.top + r.height / 2 - h / 2; }
    else if (r.bottom + gap + h <= vh - m) { x = r.left + r.width / 2 - w / 2; y = r.bottom + gap; }
    else if (r.top - gap - h >= m) { x = r.left + r.width / 2 - w / 2; y = r.top - gap - h; }
    else { x = (vw - w) / 2; y = vh - h - 24; } // a long answer fills the window: dock at the foot
  } else if (s.dim === "none" || s.target) {
    // Docked at the foot: the map step shows the view itself, and a step whose
    // control is not laid out yet still says what to do.
    x = (vw - w) / 2; y = vh - h - 24;
  } else {
    x = (vw - w) / 2; y = (vh - h) / 2;
  }
  x = Math.max(m, Math.min(x, vw - w - m));
  y = Math.max(m, Math.min(y, vh - h - m));
  const dim = s.dim === "none" ? "none" : spot ? "spot" : "all";
  const key = [dim, ...(spot || []), x, y].map((v) => typeof v === "number" ? Math.round(v) : v).join();
  if (key !== tourPlaced) {
    tourPlaced = key;
    tourTip.style.left = Math.round(x) + "px";
    tourTip.style.top = Math.round(y) + "px";
    tourSpot.hidden = dim === "none";
    tourSpot.classList.toggle("tour-all", dim === "all");
    // The whole-page dim is a spot of no size in the middle: one element, one
    // shadow, the same rule as the lit one.
    const [l, t, sw, sh] = spot || [vw / 2, vh / 2, 0, 0];
    Object.assign(tourSpot.style, { left: l + "px", top: t + "px", width: sw + "px", height: sh + "px" });
  }
}
function tourLoop() {
  tourPlace();
  tourRaf = tour ? requestAnimationFrame(tourLoop) : 0;
}

function tourGo(step) {
  tour.step = step;
  tourSave();
  tourStatus("");
  TOUR_STEPS[step].prepare?.();
  tourRender();
  // A replay on a vault that already has notes: step 1 has nothing to wait
  // for, so it says which note the tour will use and offers to go on.
  if (step === "file" && !tourJobs.size && tourFirstNote()) tourHaveNote();
}

function tourHaveNote() {
  tour.note = tour.note || tourFirstNote();
  tourGo("ask");
}

function tourStart() {
  tourJobs.clear();
  tour = { step: "welcome", note: null };
  tourGo("welcome");
}

function tourEnd() {
  tour = null;
  tourSave();
  cancelAnimationFrame(tourRaf);
  tourRaf = 0;
  tourTip.hidden = true;
  tourSpot.hidden = true;
}

// --- what moves a waiting step on --------------------------------------------
const tourWaiting = (what) => tour && TOUR_STEPS[tour.step].wait === what;

document.addEventListener("silica:job", (e) => {
  if (!tourWaiting("note")) return;
  const j = e.detail || {};
  const name = (j.source && j.source.name) || "your file";
  if (j.state === "queued" || j.state === "running") {
    tourJobs.add(j.id);
    tourStatus(`Reading ${name}…`);
  } else if (tourJobs.delete(j.id)) {
    if (j.state === "done" && j.notes && j.notes.length)
      tour.note = j.notes.length > 1 ? j.notes[0].slice(0, j.notes[0].lastIndexOf("/")) : j.notes[0];
    else if (j.state !== "done") tourStatus(`${name}: ${j.error || "it could not be read"}. Try another file.`, true);
  }
});

document.addEventListener("silica:vault-info", () => {
  if (tourWaiting("note") && !tourJobs.size && tourFirstNote()) tourHaveNote();
});

document.addEventListener("silica:turn-end", (e) => {
  if (!tourWaiting("turn") || e.detail.outcome !== "success") return;
  tourGo(tour.step === "ask" ? "cite" : "learn");
});

document.addEventListener("silica:view", (e) => {
  if (tourWaiting("graph") && e.detail === "graph") tourGo("map");
});

// Esc on the bubble only: Esc elsewhere already closes the menu or panel the
// step opened, and closing that must not also close the tour.
tourTip.addEventListener("keydown", (e) => { if (e.key === "Escape") tourEnd(); });

$("#tour-start").addEventListener("click", (e) => {
  e.stopPropagation();
  closeHelpPanel(false);
  tourStart();
});

// --- the first visit ---------------------------------------------------------
// Both lists the condition reads arrive once at start (app-settings.js), in
// either order; the tour decides when it has seen the two.
{
  let seen = 0;
  const boot = () => {
    if (++seen !== 2) return;
    const saved = tourSaved();
    if (saved && saved.step && TOUR_STEPS[saved.step]) {
      tour = saved;
      tourGo(TOUR_RESUME[saved.step] || saved.step);
    } else if (saved !== "off" && !tourFirstNote() && sessionCount === 0) {
      tourStart();
    }
  };
  document.addEventListener("silica:vault-info", boot, { once: true });
  document.addEventListener("silica:sessions", boot, { once: true });
}
