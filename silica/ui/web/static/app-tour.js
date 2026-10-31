// --- the first-visit tour (ADR-0105) -----------------------------------------
// Four numbered steps on the student's own file. Each lights the real control it
// is about and moves on when the thing it asks for has happened, read off the
// events the page already raises. It never sends: the question and the quiz are
// prefilled and the turn stays the student's, as the chat's + does (ADR-0083).
// After the four it can go on through Discover, Explore and Learn, and a plan
// step's reading and Create walk through themselves the first time they open
// (amendment of 2026-10-29: the owner chose one long tour over chapters).

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
    // The turn steps had no button, so a turn that failed, was stopped or was
    // never sent left the tour on step 2 for good (reported 2026-10-29).
    link: ["skip this step", () => tourGo("explore")],
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
    // Step 4 is Learn's: an account without it ends here (ADR-0140).
    primary: viewServed("learn") ? ["Next", () => tourGo("quiz")] : ["Done", () => tourEnd()],
  },
  quiz: {
    // Two turns: the questions, then the student's answers, which are what
    // Learn's queue records. Moving on after the first dimmed the questions
    // the bubble had just said to answer.
    n: 4, target: TOUR_COMPOSER, wait: "turn",
    dim: () => tour.quizAsked ? "none" : "",  // the questions are what they read now
    prepare: () => tour.quizAsked ? showTab("chat") : prefillChat(`/quiz "${tour.note}" --n=3`),
    body: () => tour.quizAsked
      ? "Now answer the three questions in the chat and send your answers. Silica grades each one."
      : "Last one: Silica asks you three questions on your note. Send it, then answer in the chat.",
    link: ["skip this step", () => tourGo("learn")],
  },
  learn: {
    n: 4, target: '.tab[data-tab="learn"]',
    body: "What you get wrong comes back here to review. That is the basics. The tour can go on through Discover, Explore and Memorize, about three minutes more; Shortcuts and help has it again any time.",
    primary: ["Go on", () => tourRun("more", 0)],
    secondary: ["Stop here", () => tourEnd()],
  },
};
// A reload lands on the half of a step that can be drawn again from nothing:
// the answer a `cite` pointed at and the frame a `map` sat on are gone.
function tourSource() {
  const a = tourLastAnswer();
  return a?.querySelector(".stream-text .note-link[data-cite]") || a?.querySelector(".lg-reads") || null;
}
const TOUR_RESUME = { fetch: "file", cite: "explore", map: "quiz" };

// --- after the basics, and the two walkthroughs that start by themselves -------
// A run is a list of steps shown in order with Next and Back. `ch` names the
// chapter the head counts in; `when` leaves a step out when the vault has no
// such thing (no plan, no story yet), so a new vault sees fewer steps, not empty
// ones. A chapter's first step holds Next until its view has the data every
// later `when` reads (`ready`). Each step restores its own view, because Back
// crosses from one chapter into the last. `side: "right"` asks to sit beside a
// wide control; `high` lifts the tour over Learn's dialog.
const tourQ = (sel) => document.querySelector(sel);
// the one laid out: a reading can hold a second Rewrite, on its stale line
const tourByText = (sel, text) => [...document.querySelectorAll(sel)]
  .find((b) => b.textContent.trim() === text && b.getBoundingClientRect().width) || null;
function tourView(tab) { if (activeTab !== tab) showTab(tab); }
function tourSurface(mode) { tourView("graph"); if (graphMode !== mode) setGraphMode(mode); }
// replace: the tour's moves are not the student's history to walk back through
function tourLearn(r) {
  tourView("learn");
  if (lrRoute.view !== r.view || (r.plan && lrRoute.plan !== r.plan)) lrGo(r, { replace: true });
}
// Two controls a step names together, lit as one box (only its rect is read).
function tourUnion(...els) {
  if (!els.every(Boolean)) return null;
  return { getBoundingClientRect() {
    const rs = els.map((e) => e.getBoundingClientRect());
    const left = Math.min(...rs.map((r) => r.left)), top = Math.min(...rs.map((r) => r.top));
    const right = Math.max(...rs.map((r) => r.right)), bottom = Math.max(...rs.map((r) => r.bottom));
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  } };
}
const tourDvFront = () => !!dv.data?.setup;
const tourLrPlan = () => lrData?.plans?.[0]?.path;
const tourField = (name) => () => [...document.querySelectorAll(".lr-dialog .lr-fs")]
  .find((s) => s.querySelector("h3")?.firstChild?.textContent.trim() === name) || null;

const TOUR_RUNS = {
  more: [
    { ch: "Discover", prepare: () => tourView("discover"),
      ready: () => !!dv.data && !!(tourQ(".dv-tabs") || tourQ(".dv-first-in")), loading: "Discover is loading.",
      // the first visit's whole setup, Start reading included: the step asks for it
      target: () => tourDvFront() ? tourQ(".dv-tabs") : tourQ(".dv-first-in"),
      body: () => tourDvFront()
        ? "Discover reads the feeds you picked and groups what they publish into stories. For you puts first the ones nearest your notes; News and Research show each feed as it comes."
        : "Pick what Discover reads, then Start reading. Its stories come in while the tour goes on." },
    { ch: "Discover", prepare: () => tourView("discover"), when: () => tourDvFront() && !!tourQ(".dv-lead"), target: ".dv-lead",
      body: "A story is written from its articles the first time you open it, and names the notes in your vault it sits near." },
    { ch: "Discover", prepare: () => tourView("discover"), when: () => tourDvFront() && !!tourQ(".dv-lead"), target: ".dv-lead .dv-actions",
      body: "Ask starts a chat on the story. Save puts it in your Inbox as a note, the only way anything from Discover reaches your vault. The thumbs teach For you." },
    { ch: "Discover", prepare: () => tourView("discover"), when: tourDvFront, target: ".dv-rail .dv-panel",
      body: "These folders and tags steer For you. Switch one off and its notes stop ranking the stories." },
    { ch: "Discover", prepare: () => tourView("discover"), when: tourDvFront, target: () => tourByText(".dv-head .dv-btn", "Feeds"),
      body: "Feeds lists what Discover reads: add a feed, import a list, or browse the catalog. It reads them when you open Discover, at most once an hour." },
    { ch: "Explore", prepare: () => tourSurface("graph"), dim: "none",
      body: "A dot is a note. A grey line is a link you wrote; a blue one joins two notes that say similar things. The key at the right says what each mark means and how many there are." },
    { ch: "Explore", prepare: () => tourSurface("graph"), target: "#node-search",
      body: "Find a note and the map flies to it, with its neighbours arranged around it. Click any dot to open its note beside the map." },
    { ch: "Explore", prepare: () => tourSurface("graph"), target: "#surface-tabs",
      body: "The same notes, read five ways. Graph draws the links; Path, Folders, Areas and Read read the shape. Library is here when the vault is linked to Zotero." },
    { ch: "Explore", prepare: () => tourSurface("path"), target: '#surface-tabs [data-gmode="path"]',
      body: "Path puts one note in its reading order: what comes before it, and what it unlocks. Pick a note to start from." },
    { ch: "Explore", prepare: () => tourSurface("areas"), target: '#surface-tabs [data-gmode="areas"]',
      body: "Areas takes one area and draws its links to the others, over how much every pair is linked. Folders, beside it, shades each folder by how much it mixes areas." },
    { ch: "Explore", prepare: () => tourSurface("read"), target: '#surface-tabs [data-gmode="read"]',
      body: "Read is a way through the whole vault: each area's hub first, then the notes it opens onto." },
    { ch: "Explore", prepare: () => tourSurface("graph"), target: "#dock .composer-shell",
      body: "Ask from here. The answer opens in a card over the map, and goes on in the chat when you want the whole of it." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "home" }),
      ready: () => !!lrData && !!tourQ(".lr-ask"), loading: "Memorize is reading your notes.", target: ".lr-ask",
      body: "Name a note, a folder or a topic and press Test me: the questions start at once and nothing is written. Create makes it a plan with a date." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "home" }), when: () => !!lrData?.recent?.length, target: ".lr-recent",
      body: "Changed this week: one click tests you on a note you have just edited." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "home" }), when: () => !!lrData?.next?.plans?.length,
      target: () => [...document.querySelectorAll(".lr-home .lr-hsec")].find((s) => s.querySelector(".lr-hh")?.textContent.startsWith("Your plans")) || null,
      body: "Your plans. A plan spreads a folder or a syllabus over the days up to your date. Its name opens it; Continue asks its next questions." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "home" }), when: () => !!lrData?.session_rows?.length, target: ".lr-next",
      body: "Your next session, chosen for you: each note says why it is up. Take one out before you start, then Start these." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "plan", plan: tourLrPlan() }), when: () => !!tourLrPlan(),
      target: () => tourUnion(tourByText(".lr-one button", "Continue plan"), tourByText(".lr-one button", "Check what I already know")),
      body: "Continue plan asks the next step's questions. Check what I already know asks up to two a step first; a step you know moves to the end, and nothing is passed for you." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "plan", plan: tourLrPlan() }), when: () => !!tourLrPlan(), target: ".lr-readbtn",
      body: "Read opens a step as cards: Silica's summary of each note, ending on a question about the next one. Rewrite a card in your own words and Silica checks it." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "map" }), target: ".lr-mapv .mv-l", side: "right",
      body: "What you know: every mark is a note. Dashed is not tested yet, open is still to verify, half is met with help, full is met without help. Pick one to see its history." },
    { ch: "Memorize", prepare: () => tourLearn({ view: "home" }), target: () => tourByText(".lr-next button", "Start these"),
      body: "In a session you answer, say how sure you are, then compare with your note. What you miss comes back in a later session. You can take this tour again from Shortcuts and help." },
  ],
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
const tourShows = (st) => (!st.ch || viewServed({ Discover: "discover", Explore: "graph", Memorize: "learn", Reading: "learn" }[st.ch] ?? "chat"))
  && (!st.when || st.when());
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
  } else count.textContent = s.n ? `Step ${s.n} of ${viewServed("learn") ? 4 : 3}` : "Welcome";
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
    const back = steps.slice(0, tour.i).some((st) => tourShows(st) && !st.once) || tour.run === "more";
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

// The next step of a run in direction `dir` that this vault has. Back past the
// first chapter lands on the basics' last bubble.
function tourRun(run, i, dir = 1) {
  const steps = TOUR_RUNS[run];
  while (i >= 0 && i < steps.length && (!tourShows(steps[i]) || (dir < 0 && steps[i].once))) i += dir;
  if (i < 0) { if (run === "more") tourGo("learn"); return; }
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
  else if (tour.quizAsked) tourGo("learn");
  else { tour.quizAsked = true; tourSave(); tourRender(); }
});

document.addEventListener("silica:view", (e) => {
  if (tourWaiting("graph") && e.detail === "graph") tourGo("map");
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
// Both lists the condition reads arrive once at start (app-settings.js), in
// either order; the tour decides when it has seen the two.
{
  let seen = 0;
  const boot = () => {
    if (++seen !== 2) return;
    const saved = tourSaved();
    if (saved?.run === "more" && TOUR_RUNS.more[saved.i]) {
      // the chapter's start, where Next waits for the data its later steps read
      tour = saved;
      tourRun("more", TOUR_RUNS.more.findIndex((st) => st.ch === TOUR_RUNS.more[saved.i].ch));
    } else if (saved && saved.step && TOUR_STEPS[saved.step]) {
      tour = saved;
      tourGo(TOUR_RESUME[saved.step] || saved.step);
    } else if (saved !== "off" && !tourFirstNote() && sessionCount === 0) {
      tourStart();
    }
  };
  document.addEventListener("silica:vault-info", boot, { once: true });
  document.addEventListener("silica:sessions", boot, { once: true });
}
