// --- the first-visit tour (ADR-0105, ADR-0172 point 10) -----------------------
// Eight bubbles at most, the welcome counted, on the student's own file. The
// first three are what a newcomer needs: bring in a file, ask about it, see the
// notes the answer stands on. The four after them each name a part of the app in
// a sentence and light where it lives. A step lights the real control it is
// about and moves on when the thing it asks for has happened, read off the
// events the page already raises. It never sends: the question is prefilled and
// the turn stays the student's, as the chat's + does (ADR-0083). The long run
// through Discover, Explore and Memorize (amendment of 2026-10-29) is gone with
// ADR-0172; a plan step's reading and Create still walk through themselves the
// first time they open.

const TOUR_KEY = "silica.tour";
// The field's visible box: #composer is the whole width of the view.
const TOUR_COMPOSER = "#composer .composer-shell";
const tourLastAnswer = () => [...document.querySelectorAll("#log .msg.silica")].pop();
const tourHosted = () => "hosted" in document.documentElement.dataset;

// The steps by name, in order of `next`. `n` is the number the student sees; the
// link's step (fetch) keeps the number of the one it stands in for. `wait` names
// what moves a step on when it has no button: a note in the tree, a turn that
// ended well.
const TOUR_STEPS = {
  welcome: {
    n: 0, dim: "all",
    body: "Silica answers from your own notes and names the note behind each answer. Three steps get you started: add a file, ask about it, see where the answer came from.",
    primary: ["Start", () => tourGo("file")],
    secondary: ["Later", () => tourEnd()],
  },
  file: {
    n: 1, target: "#vault-add", prepare: () => tourOpenDrawer(), wait: "note",
    body: "Add your notes here: a PDF, a Word file, a .md or a .txt. Each file becomes a note at the top of your vault.",
    link: ["only have a link?", () => tourGo("fetch")],
  },
  fetch: {
    n: 1, target: TOUR_COMPOSER, prepare: () => prefillChat("/fetch "), wait: "note",
    body: "Paste the link after /fetch and send it. Silica reads the page into your Inbox.",
  },
  ask: {
    n: 2, target: TOUR_COMPOSER, wait: "turn",
    prepare: () => prefillChat(/\.md$/i.test(tour.note)
      ? `What are the key points of [[${noExt(tour.note)}]]? `
      : `What are the key points of the notes in "${tour.note}"? `),
    body: "Ask about it. The question is written for you: send it, or write your own.",
    // The turn steps had no button, so a turn that failed, was stopped or was
    // never sent left the tour on step 2 for good (reported 2026-10-29).
    link: ["skip this step", () => tourGo("views")],
  },
  cite: {
    // Where the answer says what it stands on (ADR-0117), not the answer: a
    // long one is taller than the window and lights nothing. A cited note is a
    // numbered link in the text; a note read and not cited is named on the
    // line above the answer.
    n: 3, target: () => tourSource() || tourLastAnswer(),
    body: () => {
      const s = tourSource();
      if (s?.dataset.cite) return "The answer comes from your notes: each numbered link is one of them. Click it to open the note.";
      if (s) return "The answer comes from your notes: the ones it read are named above it. Click one to open it.";
      return "The answer comes from the notes in your vault.";
    },
    primary: ["Show me more", () => tourGo("views")],
    secondary: ["Stop here", () => tourEnd()],
  },
  views: {
    // The rows on the drawer, or the rail's icons below the fold: whichever is laid out.
    n: 4, target: "#views, #railmini", prepare: () => showTab("chat"),
    body: () => [
      viewServed("graph") && "Explore draws your notes as a map, where notes that say similar things sit close.",
      viewServed("learn") && "Memorize asks you questions on them and brings back what you miss; Choose offers four answers to pick from.",
      viewServed("calendar") && "Calendar holds your dates, your study plans and the decisions you log with /decide.",
    ].filter(Boolean).join(" ") || "Your views are listed here.",
    primary: ["Next", () => tourGo("write")],
  },
  write: {
    n: 5, target: "#write-note, #rail-write",
    body: "Write a note here. Its toolbar's Formula and Molecule buttons write a formula or draw a molecule from its name, a table shows as a grid of cells, and pointing at a link shows the note it leads to.",
    primary: ["Next", () => tourGo("more")],
  },
  more: {
    n: 6, target: "#attach",
    body: () => (tourHosted() ? "The + holds more ways to ask: make a page builds a web page from your notes."
      : "The + holds more ways to ask: a council puts one question to several models, and make a page builds a web page from your notes.")
      + " When your notes hold nothing on a question, any answer from general knowledge comes in its own labelled block.",
    primary: ["Next", () => tourGo("review")],
  },
  review: {
    n: 7, target: '.tab[data-tab="metrics"]',
    body: () => (viewServed("metrics") ? "Examine shows what needs your attention, and its Review lists what Silica proposed, to accept or dismiss. " : "")
      + "When two notes disagree, the note shows both sides for you to settle. Shortcuts and help has all of this, and this tour, again.",
    primary: ["Done", () => tourEnd()],
  },
};
// The highest number a step shows, for "Step n of N".
const TOUR_LAST = Math.max(...Object.values(TOUR_STEPS).map((s) => s.n));
// A reload lands on the half of a step that can be drawn again from nothing:
// the answer a `cite` pointed at is gone.
function tourSource() {
  const a = tourLastAnswer();
  return a?.querySelector(".stream-text .note-link[data-cite]") || a?.querySelector(".lg-reads") || null;
}
const TOUR_RESUME = { fetch: "file", cite: "views" };

// --- the two walkthroughs that start by themselves -----------------------------
// A run is a list of steps shown in order with Next and Back. `ch` names the
// chapter the head counts in; `when` leaves a step out when the vault has no
// such thing, so a new vault sees fewer steps, not empty ones. `side: "right"`
// asks to sit beside a wide control; `high` lifts the tour over Learn's dialog.
const tourQ = (sel) => document.querySelector(sel);
// the one laid out: a reading can hold a second Rewrite, on its stale line
const tourByText = (sel, text) => [...document.querySelectorAll(sel)]
  .find((b) => b.textContent.trim() === text && b.getBoundingClientRect().width) || null;
const tourField = (name) => () => [...document.querySelectorAll(".lr-dialog .lr-fs")]
  .find((s) => s.querySelector("h3")?.firstChild?.textContent.trim() === name) || null;

const TOUR_RUNS = {
  // The first time a plan step's reading opens. It only scrolls: moving between
  // cards marks the one left behind as read (lrReadShow).
  read: [
    { ch: "Reading", target: ".lr-read-side", scroll: true,
      body: "The step's cards, each after the notes it needs. ✓ is done, – is read with its question left. Click a card to go to it." },
    { ch: "Reading", target: ".lr-card-one .lr-read-text", scroll: true,
      body: "A card is Silica's summary of the note. A blue name opens that note on the right. A sentence your notes do not hold is marked (not in the notes); an Example is worked by Silica." },
    { ch: "Reading", when: () => !!tourQ(".lr-read-ask"), target: ".lr-read-ask", scroll: true,
      body: "Each card ends on a question toward the next one. Answer from what you read, or press I don't know: either way you see what the next note says. Nothing is graded." },
    { ch: "Reading", when: () => !!tourQ(".lr-read-ask .lr-read-actions"), target: ".lr-read-ask .lr-read-actions", scroll: true,
      body: "Or go on without answering: the card stays marked – and you can come back to it. The arrow keys move between cards." },
    { ch: "Reading", target: ".lr-read-test", scroll: true,
      body: "After the last card, Start the practice asks these questions with the reading closed. What you miss comes back over the next days." },
    { ch: "Reading", target: () => tourByText(".lr-read button", "Rewrite"), scroll: true,
      body: "Rewrite writes the reading again from the step's notes, for when they change. Your answers on it start over." },
  ],
  readNone: [
    { ch: "Reading", target: ".lr-read-choice button", primary: ["Got it", () => tourEnd()],
      body: "No reading yet for this step. Generate it and Silica writes one card per note, a call per card; you can leave and come back. The walkthrough of the cards starts when they are ready." },
  ],
  create: [
    // `once`: counted, never gone back to; the sentence is gone once the fields open
    { ch: "Create", high: true, side: "right", target: ".lr-dialog .lr-composer", wait: "fields", once: true,
      body: "Say in one sentence what, by when, and what you want to be able to do, then Read it: it fills the fields, and each can be changed. Nothing is written until you keep it. Or fill the fields yourself." },
    { ch: "Create", high: true, side: "right", scroll: true, target: tourField("Study"),
      body: "Study: the folders and notes the questions come from, and where a reference's lines are looked for. Add one, or take one out with its ×." },
    { ch: "Create", high: true, side: "right", scroll: true, target: tourField("Reference"),
      body: "Reference, optional: a syllabus note, or text you paste. Each line is matched to a note in Study; a line with no note is kept as a gap, so you see what is missing." },
    { ch: "Create", high: true, side: "right", scroll: true, target: tourField("Be able to"),
      body: "Be able to: what the questions ask of you. Choose any; with none, Silica picks one per part from the material." },
    { ch: "Create", high: true, side: "right", scroll: true, target: tourField("By"),
      body: "By, optional: a date spreads the work over the days left, and hours a day sets how much goes into each." },
    { ch: "Create", high: true, side: "right", scroll: true, target: ".lr-dialog .lr-cta",
      body: "Start begins now. With a reference, a date or goals it is also kept as a plan in Plans/; without, it is a one-off test and only your answers are written. Save only keeps the plan for later." },
  ],
};
// The view each walkthrough belongs to: leaving it ends the walkthrough.
const TOUR_MINI_VIEW = { read: "read", readNone: "read", create: "new" };
const TOUR_SEEN = "silica.tour.seen";

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

// A walkthrough that starts by itself is not the tour: it never resumes and
// never turns the tour off.
function tourSave() {
  if (tour?.mini) return;
  try { localStorage.setItem(TOUR_KEY, JSON.stringify(tour || "off")); } catch { /* private window: the tour still runs, it just cannot resume */ }
}
const tourStep = () => tour.run ? TOUR_RUNS[tour.run][tour.i] : TOUR_STEPS[tour.step];
// A chapter whose view the account is not served is skipped whole, as a step
// this vault lacks is (ADR-0140); Create and the rest live in the chat.
const tourShows = (st) => (!st.ch || viewServed("learn")) && (!st.when || st.when());
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
  showSidePane("side-vault"); // the + leaves with the notes when Chats is the list in view (ADR-0122)
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
  const s = tourStep();
  tourTip.replaceChildren();
  const head = document.createElement("div");
  head.className = "tour-head";
  const count = document.createElement("span");
  if (s.ch) {
    // counted among the chapter's steps this vault has
    const shown = TOUR_RUNS[tour.run].filter((st) => st.ch === s.ch && tourShows(st));
    count.textContent = `${s.ch} · ${shown.indexOf(s) + 1} of ${shown.length}`;
  } else count.textContent = s.n ? `Step ${s.n} of ${TOUR_LAST}` : "Welcome";
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
  let secondary = s.secondary, primaryAct = s.primary;
  if (tour.run && !s.wait) {
    const steps = TOUR_RUNS[tour.run];
    const back = steps.slice(0, tour.i).some((st) => tourShows(st) && !st.once);
    const next = steps.slice(tour.i + 1).some(tourShows);
    if (back) secondary = ["Back", () => tourRun(tour.run, tour.i - 1, -1)];
    primaryAct ||= next ? ["Next", () => tourRun(tour.run, tour.i + 1, 1)] : ["Done", () => tourEnd()];
  }
  if (s.link) acts.append(tourButton("tour-link", s.link));
  if (secondary) acts.append(tourButton("tour-btn", secondary));
  const primary = primaryAct && tourButton("tour-btn primary", primaryAct);
  if (primary) acts.append(primary);
  tourTip.append(head, body, status);
  if (acts.childElementCount) tourTip.append(acts);
  tourTip.setAttribute("aria-describedby", "tour-body");
  tourTip.hidden = false;
  tourTip.classList.toggle("tour-high", !!s.high);
  tourSpot.classList.toggle("tour-high", !!s.high);
  // Next waits for the view's data, which every later step of the chapter reads
  tour.ready = !s.ready || s.ready();
  if (primary) primary.disabled = !tour.ready;
  if (!tour.ready) { status.textContent = s.loading; status.hidden = false; }
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
  const s = tourStep();
  const el = tourTarget(s);
  if (el && s.scroll && !tour.scrolled) { tour.scrolled = true; el.scrollIntoView({ block: "nearest" }); }
  const w = tourTip.offsetWidth, h = tourTip.offsetHeight, gap = 12, m = 8, pad = 6;
  const vw = innerWidth, vh = innerHeight;
  let spot = null, x, y;
  if (el) {
    const r = el.getBoundingClientRect();
    spot = [r.left - pad, r.top - pad, r.width + 2 * pad, r.height + 2 * pad];
    // beside a control taller than the window, level with the part in view
    const mid = (Math.max(r.top, 0) + Math.min(r.bottom, vh)) / 2 - h / 2;
    if ((s.side === "right" || r.width < vw / 3) && r.right + gap + w <= vw - m) { x = r.right + gap; y = mid; }
    else if (r.bottom + gap + h <= vh - m) { x = r.left + r.width / 2 - w / 2; y = r.bottom + gap; }
    else if (r.top - gap - h >= m) { x = r.left + r.width / 2 - w / 2; y = r.top - gap - h; }
    // Discover's panel at the right edge is too tall for over or under
    else if (r.left - gap - w >= m) { x = r.left - gap - w; y = mid; }
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
  const dim = (typeof s.dim === "function" ? s.dim() : s.dim) === "none" ? "none" : spot ? "spot" : "all";
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
  if (tour && !tour.ready && tourStep().ready?.()) tourRender();
  tourPlace();
  tourRaf = tour ? requestAnimationFrame(tourLoop) : 0;
}

// The next step of a run in direction `dir` that this vault has.
function tourRun(run, i, dir = 1) {
  const steps = TOUR_RUNS[run];
  while (i >= 0 && i < steps.length && (!tourShows(steps[i]) || (dir < 0 && steps[i].once))) i += dir;
  if (i < 0) return;
  if (i >= steps.length) { tourEnd(); return; }
  Object.assign(tour, { step: run, run, i, scrolled: false });
  tourSave();
  steps[i].prepare?.();
  tourRender();
}

// A walkthrough that starts by itself, once per browser, when nothing else runs.
function tourOnce(run) {
  if (tour || !run) return;
  let seen = {};
  try { seen = JSON.parse(localStorage.getItem(TOUR_SEEN)) || {}; } catch { /* private window: shown each time */ }
  if (seen[run]) return;
  seen[run] = 1;
  try { localStorage.setItem(TOUR_SEEN, JSON.stringify(seen)); } catch { /* same */ }
  tour = { mini: true };
  tourRun(run, 0);
}

function tourGo(step) {
  delete tour.run;
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

// A replay from help shows the two walkthroughs again on their next opening.
function tourStart() {
  tourJobs.clear();
  try { localStorage.removeItem(TOUR_SEEN); } catch { /* private window: nothing was kept */ }
  tour = { step: "welcome", note: null };
  tourGo("welcome");
}

function tourEnd() {
  const mini = tour?.mini;
  tour = null;
  if (!mini) tourSave();
  cancelAnimationFrame(tourRaf);
  tourRaf = 0;
  tourTip.hidden = true;
  tourSpot.hidden = true;
}

// --- what moves a waiting step on --------------------------------------------
const tourWaiting = (what) => tour && tourStep()?.wait === what;

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
  if (!tourWaiting("turn")) return;
  if (e.detail.outcome !== "success") {
    // The failed message carries its own "try again"; the composer is empty.
    tourStatus("The answer did not arrive. Try again on your message, or skip this step.", true);
    return;
  }
  if (tour.step === "ask") tourGo("cite");
});

document.addEventListener("silica:view", (e) => {
  if (tour?.mini && e.detail !== "learn") tourEnd();
});

// Learn announces each render with the view it drew (app-learn.js lrRender).
document.addEventListener("silica:learn-view", (e) => {
  const v = e.detail;
  if (tour?.mini && TOUR_MINI_VIEW[tour.run] !== v) tourEnd();
  if (tourWaiting("fields") && lrForm.step === "fields" && !lrForm.reading) { tourRun("create", 1); return; }
  // the cards a step had no reading for are written: their walkthrough is next
  if (tour?.run === "readNone" && tourQ(".lr-read-grid")) tourEnd();
  if (activeTab !== "learn") return; // a render behind another view is not an opening
  if (v === "read") tourOnce(tourQ(".lr-read-grid") ? "read" : tourQ(".lr-read-choice") ? "readNone" : "");
  else if (v === "new" && lrForm.step === "sentence" && !lrForm.reading && tourQ(".lr-dialog .lr-composer")) tourOnce("create");
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
// A vault with no notes and no chats, on a browser the tour has not been turned
// off in. Held while the setup card stands over the box (ADR-0172 point 1): the
// tour's first steps ask a model, and its dim covered the card. The card's
// Start asking calls this again.
function tourFirstVisit() {
  if (!tour && tourSaved() !== "off" && !tourFirstNote() && sessionCount === 0 && !setupNeeded) tourStart();
}

// The three answers the condition reads arrive once at start (app-settings.js),
// in any order; the tour decides when it has seen them all.
{
  let seen = 0;
  const boot = () => {
    if (++seen !== 3) return;
    const saved = tourSaved();
    if (saved && saved.step && TOUR_STEPS[saved.step]) {
      tour = saved;
      tourGo(TOUR_RESUME[saved.step] || saved.step);
    } else tourFirstVisit();
  };
  document.addEventListener("silica:vault-info", boot, { once: true });
  document.addEventListener("silica:sessions", boot, { once: true });
  document.addEventListener("silica:config", boot, { once: true });
}
