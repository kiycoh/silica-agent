// Vanilla client: POST /chat returns text/event-stream, read incrementally via
// the body's ReadableStream (not EventSource — that only does GET).
const $ = (s) => document.querySelector(s);
const log = $("#log");
const input = $("#input");
// The run line (P1): the dock's strip, and in the chat the live ledger line at
// the head of the running turn (ADR-0117 point 4). Stop rides in the dock's
// strip; in the chat it is the send button for the length of the turn.
const runLines = () => document.querySelectorAll(".run-line");
const stopBtns = () => document.querySelectorAll(".rl-stop");
function setRunVisible(v) {
  for (const l of runLines()) l.hidden = !v;
  for (const b of stopBtns()) { b.disabled = false; b.textContent = "■ stop"; }
}

let streaming = false;
let activeTab = "chat";

// --- follow-bottom: a two-state machine, not an unconditional snap ----------
// Every frame used to set scrollTop = scrollHeight, so scrolling up to re-read
// a definition while an answer streamed was undone on the next token. Following
// flips off on any upward movement of a pixel or more and back on when the
// reader reaches the bottom themselves; while it is off during a stream the
// pill under the transcript is the one way back down. Native scroll anchoring
// is left to the browser only while NOT following: while following, this
// machine owns the position and anchoring would fight it.
let following = true;
let newBelow = 0;      // sections appended while not following, for the pill
const FOLLOW_TOL = 1;  // px: the study's own tolerance, "a two-pixel upward scroll stops following"
const jumpBtn = $("#jump-latest");

function atBottom() {
  return log.scrollHeight - log.scrollTop - log.clientHeight <= FOLLOW_TOL;
}
function syncJump() {
  log.classList.toggle("following", following);
  jumpBtn.hidden = !(streaming && !following);
  jumpBtn.textContent = "↓ new text below" + (newBelow ? ` · ${newBelow} new` : "");
}
let lastSetTop = 0; // where the machine last put the transcript itself
function followBottom() {
  if (following) { log.scrollTop = log.scrollHeight; lastSetTop = log.scrollTop; }
  syncJump();
}
// Something new landed in the transcript: count it for the pill when the
// reader is elsewhere, or keep the bottom in view when they are not.
function noteAppended() {
  if (!following) newBelow++;
  followBottom();
}
log.addEventListener("scroll", () => {
  const was = following;
  // Up from where the machine left it is the reader's act; anything else is
  // the content growing under a position the machine set, which must not
  // flip the state, or a fast stream reads as "the reader scrolled up".
  if (log.scrollTop < lastSetTop - FOLLOW_TOL) following = false;
  else if (atBottom()) following = true;
  if (following) newBelow = 0;
  if (was !== following) syncJump();
});

log.addEventListener("wheel", (e) => {
  if (e.deltaY < 0 && following) { following = false; syncJump(); }
}, { passive: true });
jumpBtn.addEventListener("click", () => {
  following = true;
  newBelow = 0;
  followBottom();
});

// --- notifications + screen-reader status ------------------------------------
// A hairline toast strip fills the silent catch(){} gaps: a failed background
// fetch now says so instead of leaving a stale "—". Two levels only (info =
// accent, error = gold/caution) — the palette reserves no third UI signal.
// Every notify() also lands in the polite SR region, so the streaming
// transcript itself needn't be a chatty live region.
const srStatus = $("#sr-status");
const toasts = $("#toasts");
function announce(msg) { if (srStatus) srStatus.textContent = msg; }
// Cap the visible stack. Two diagnostics fire on every load and an error turn
// stacked five, ~300px of grey boxes sitting on top of #send — so whatever you
// had just done, the screen closed on a debug message covering the primary
// control. Older ones roll up behind a "+N" the user can expand.
const TOAST_MAX = 2;

function rollUpToasts() {
  const all = [...toasts.querySelectorAll(".toast")];
  const overflow = Math.max(0, all.length - TOAST_MAX);
  all.forEach((t, i) => { t.hidden = i < overflow; });
  let more = toasts.querySelector(".toast-more");
  if (!overflow) { if (more) more.remove(); return; }
  if (!more) {
    more = document.createElement("button");
    more.type = "button";
    more.className = "toast-more";
    // recompute on click: the stack keeps changing under this handler
    more.addEventListener("click", () => {
      toasts.querySelectorAll(".toast").forEach((t) => { t.hidden = false; });
      more.remove();
    });
  }
  toasts.prepend(more);
  more.textContent = `+${overflow} more`;
}

function notify(msg, level = "error") {
  announce(msg);
  if ([...toasts.querySelectorAll(".toast")].some((t) => t.textContent === msg)) return; // dedupe visible
  const t = document.createElement("div");
  t.className = "toast " + level;
  t.textContent = msg;
  t.title = msg; // CSS clamps the box to 3 lines; the full text stays reachable
  const kill = () => { t.remove(); rollUpToasts(); };
  t.addEventListener("click", kill);
  toasts.appendChild(t);
  setTimeout(kill, level === "error" ? 6000 : 3000);
  rollUpToasts();
}

// Name what a tool acted on. The verb alone ("write note") never told the user
// which file the agent touched in their own vault. One formatter, so a replayed
// transcript reads exactly like the stream that produced it.
const toolLabel = (t) => (t.target ? `${t.name} "${t.target}"` : t.name);

// A write to user memory gets a line of its own with its Undo (ADR-0076 §7):
// the tool row's card is collapsed by default, and a memory the user never saw
// being written is one they cannot take back. Shared by the model's
// silica_remember (tool_done) and by /remember (the done frame).
const MEMORY_VERB = { save: "Saved to memory", replace: "Updated memory", forget: "Forgot" };
function memoryLine(m) {
  const row = document.createElement("div");
  row.className = "turn-outcome memory-line";
  const what = String(m.text || "").replace(/^- /, "");
  const text = document.createElement("span");
  text.className = "ml-text";
  text.textContent = `${MEMORY_VERB[m.action] || "Memory changed"}: ${what}`;
  const scope = document.createElement("span");
  scope.className = "ml-scope";
  scope.textContent = m.scope === "user" ? "about you, every vault" : "this vault";
  const undo = document.createElement("button");
  undo.type = "button";
  undo.className = "to-continue";
  undo.textContent = "undo";
  undo.title = "put memory back as it was before this write";
  undo.addEventListener("click", async () => {
    undo.disabled = true;
    let resp = null, data = null;
    try {
      resp = await fetch("/memory/undo", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope: m.scope, token: m.token }),
      });
      data = await resp.json();
    } catch { /* reported below */ }
    if (resp && resp.ok) {
      text.textContent = `Undone: ${what}`;
      undo.remove();
      return;
    }
    // A refusal means memory changed since this write: a retry cannot succeed,
    // so the button stays off and says why.
    const why = (data && data.detail) || "could not undo";
    undo.title = why;
    notify(why);
  });
  row.append(text, scope, undo);
  return row;
}

// A save the gates refused (ADR-0076 §5) still names what the model wanted to
// keep. The user reads the exact text and one click sends it as /remember,
// which writes it verbatim: the consent the gate could not infer, given by the
// person it protects. The model's own relay of the command is not trusted to
// be exact (qwen3.5-9b once wrote "/reremember").
function memorySuggestLine(cmd) {
  const row = document.createElement("div");
  row.className = "turn-outcome memory-line";
  const what = cmd.replace(/^\/remember\s+/, "");
  const text = document.createElement("span");
  text.className = "ml-text";
  text.textContent = `Not saved: ${what}`;
  const save = document.createElement("button");
  save.type = "button";
  save.className = "to-continue";
  save.textContent = "save it yourself";
  save.title = `send ${cmd}`;
  save.addEventListener("click", () => {
    save.disabled = true;
    send(cmd);
  });
  row.append(text, save);
  return row;
}

// --- injector pipeline block -------------------------------------------------
// Every other tool is one flat line; a nucleate run is minutes of work with a
// 15-phase cycle inside it, and a lone spinner reading "injector" was the whole
// of what the GUI said about it. The TUI has always shown the phases (it holds
// the only subscriber to the FSM's phase stream) — this is the same information,
// laid out for a surface that has vertical space and no 12fps redraw budget.

const PHASE_MARK = { done: "✓", running: "◉", failed: "✗", pending: "·" };
const SUMMARY_MARK = { ok: "✓", partial: "◐", empty: "⊘", failed: "✗" };

const fmtDur = (s) =>
  s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${String(Math.floor(s % 60)).padStart(2, "0")}s`;

// One line of counts for a finished run. Shared by the live tool_done event and
// by transcript replay, so reopening a chat restates what it said while running.
function injectorSummaryLine(label, s) {
  const bits = [];
  if (s.files) bits.push(s.files === 1 ? "1 file" : `${s.files} files`);
  if (s.notes) bits.push(`${s.notes} notes`);
  if (s.links) bits.push(`${s.links} links`);
  if (s.reason) bits.push(s.reason);
  if (s.kind === "partial" && s.failed_chunks.length) {
    bits.push(`${s.failed_chunks.length} of ${s.committed + s.failed_chunks.length} chunks failed`);
  }
  const mark = SUMMARY_MARK[s.kind] || "✓";
  return `${mark} injector · ${label}${bits.length ? "   " + bits.join(" · ") : ""}`;
}

function makePipelineBlock(label, tracks) {
  const el = document.createElement("div");
  el.className = "tool tool-pipeline running";
  el.innerHTML =
    `<div class="pipe-head"><span class="pipe-title"></span><span class="pipe-pos"></span></div>` +
    `<div class="pipe-track pipe-file"></div><div class="pipe-track pipe-chunk"></div>`;
  const head = el.querySelector(".pipe-title");
  const pos = el.querySelector(".pipe-pos");
  head.textContent = `» injector · ${label}`;

  // Both tracks are drawn once, greyed out, so the pipeline reads as a known
  // sequence with a position in it rather than a list that grows as it goes.
  const rows = {};
  for (const [scope, names] of Object.entries(tracks)) {
    const box = el.querySelector(scope === "file" ? ".pipe-file" : ".pipe-chunk");
    for (const name of names) {
      const r = document.createElement("div");
      r.className = "pipe-phase pending";
      r.innerHTML = `<span class="pipe-mark">·</span><span class="pipe-name"></span><span class="pipe-time"></span>`;
      r.querySelector(".pipe-name").textContent = name;
      box.appendChild(r);
      rows[`${scope}:${name}`] = r;
    }
  }

  let chunkKey = null;      // resets the chunk track when the run moves on
  let running = null;       // { row, at } — the phase whose timer is ticking
  // The TUI gets a live timer free from Rich re-rendering at 12fps; here the
  // running row is ticked locally from when its event arrived, so the server
  // sends elapsed only once, on done.
  const timer = setInterval(() => {
    if (running) running.row.querySelector(".pipe-time").textContent = fmtDur((Date.now() - running.at) / 1000);
  }, 100);

  function setRow(row, state, secs) {
    const undo = row.dataset.rollback === "1";
    // A completed rollback is not a step that went well: it is the undo of one
    // that did not. Ticking it in the same grey as `write` read as success.
    row.className = `pipe-phase ${undo && state !== "pending" ? "failed" : state}`;
    row.querySelector(".pipe-mark").textContent =
      undo && state !== "pending" ? "↳" : (PHASE_MARK[state] || "·");
    if (secs != null) row.querySelector(".pipe-time").textContent = fmtDur(secs);
  }

  return {
    el,
    applyPhase(ev) {
      // Position rides on every event, so a dropped one cannot leave the header
      // naming the wrong file or chunk — the next event restates all of it.
      const bits = [];
      if (ev.file_total > 1) bits.push(`file ${ev.file_idx + 1}/${ev.file_total}`);
      if (ev.chunk_total > 0) bits.push(`chunk ${ev.chunk_idx + 1}/${ev.chunk_total}`);
      pos.textContent = bits.join(" · ");
      if (ev.source_file) head.textContent = `» injector · ${ev.source_file}`;

      const key = `${ev.file_idx}:${ev.chunk_idx}`;
      if (ev.scope === "chunk" && key !== chunkKey) {
        chunkKey = key;
        for (const [k, r] of Object.entries(rows)) {
          if (k.startsWith("chunk:")) { setRow(r, "pending"); r.querySelector(".pipe-time").textContent = ""; }
        }
      }

      // rollback is not in either track: it is an exception branch, and drawing
      // it as a pending step made every healthy run advertise a rollback that
      // was never coming. It gets appended only when it actually fires.
      // ev.phase is the display label (the server maps it), so this is an exact
      // match — an id-to-label rule here would have to cover hub_update/hub-update,
      // and guessing left that phase permanently grey.
      let row = rows[`${ev.scope}:${ev.phase}`];
      if (!row && ev.phase === "rollback") {
        row = rows["exception:rollback"];
        if (!row) {
          row = document.createElement("div");
          row.className = "pipe-phase";
          row.dataset.rollback = "1";
          row.innerHTML = `<span class="pipe-mark">↳</span><span class="pipe-name">rollback</span><span class="pipe-time"></span>`;
          el.querySelector(".pipe-chunk").appendChild(row);
          rows["exception:rollback"] = row;
        }
      }
      if (!row) return;
      if (ev.status === "running") {
        setRow(row, "running");
        running = { row, at: Date.now() };
      } else {
        const secs = ev.elapsed != null ? ev.elapsed
          : (running && running.row === row ? (Date.now() - running.at) / 1000 : null);
        setRow(row, ev.status === "failed" ? "failed" : "done", secs);
        if (running && running.row === row) running = null;
      }
    },
    finish(summary) {
      clearInterval(timer);
      running = null;
      const s = summary || { kind: "failed", reason: "", notes: 0, links: 0, files: 0, committed: 0, failed_chunks: [] };
      const name = (head.textContent || "").replace(/^» injector · /, "");
      el.classList.remove("running");
      el.classList.add(s.kind);
      head.textContent = injectorSummaryLine(name, s);
      // A good run collapses to its one line; a bad one keeps the track open on
      // the phase that broke, which is the only time the detail is worth rows.
      if (s.kind === "ok" || s.kind === "empty") {
        el.classList.add("collapsed");
        pos.textContent = "";
      } else if (s.failed_chunks && s.failed_chunks.length) {
        const d = document.createElement("div");
        d.className = "pipe-failed";
        d.textContent = s.failed_chunks.map((f) => `✗ ${f.chunk}${f.phase ? " " + f.phase : ""}`).join(" · ");
        el.appendChild(d);
      }
    },
  };
}

function bubble(role) {
  const el = document.createElement("div");
  el.className = "msg " + (role === "user" ? "user" : "silica");
  el.innerHTML = `<div class="role">${role === "user" ? "you" : "silica"}</div><div class="body"></div>`;
  log.appendChild(el);
  // A new bubble is a turn boundary, and a turn boundary is where the reader
  // wants to be: whatever they were re-reading, the exchange has moved on.
  following = true;
  newBelow = 0;
  followBottom();
  if (role === "user") lastUserBody = el.querySelector(".body");
  return el.querySelector(".body");
}
// The person's most recent bubble: a failure attaches to the message that
// failed (P3), and only send() knows which one that is.
let lastUserBody = null;

// One vault change as an object that stays in the transcript: what happened, to
// which note, and the way back out of it. `effect` is written | moved | deleted |
// failed. The card owns its own revert, so you undo the write you are looking at
// rather than the whole turn.
const WRITE_COPY = {
  written: { label: "written", act: "revert", hint: "restore this note to its state before the turn" },
  moved: { label: "moved", act: "revert", hint: "move this note back" },
  deleted: { label: "deleted", act: "restore", hint: "bring this note back" },
  failed: { label: "not written", act: null, hint: "" },
  // A delete the tool refused because other notes link the note (ADR-0092):
  // deleting it is the user's act, so the card offers it, behind a second click.
  refused: { label: "not deleted", act: "delete anyway", hint: "other notes keep their links to it, which then point nowhere; /undo brings it back" },
};

// One rule for claiming a tool's refs into a turn's footer, shared by the
// live stream (tool_done / tool_error) and the replay (app-boot.js): a
// mutation always wins over a read of the same note, a read never downgrades
// a write recorded earlier, and a failed write is recorded as a mutation that
// did NOT land rather than dropped.
function claimRefs(t, touched, failed) {
  const refs = t.notes || [];
  const effect = t.effect || "read";
  if (t.error) {
    const why = { verb: t.name || "write", refused: t.refused || null };
    // A refused delete keeps its card even when the turn also wrote the note
    // (a model flagged Autoencoders, then tried to delete it, 2026-10-11):
    // the card is where the user deletes it, not a record of a failure.
    if (effect !== "read") for (const r of refs) if (why.refused || !touched.has(r)) failed.set(r, why);
    return;
  }
  for (const r of refs) if (effect !== "read" || !touched.has(r)) touched.set(r, effect);
}

// The footer of a turn: a card per mutation, a disabled-with-reason card per
// failed write, and the bulk revert when there is more than one. The reads it
// used to end on are the source cards and the ledger line now (ADR-0117 point
// 2). One builder for the live turn and the replayed one, so a reopened chat
// shows the same writes the live one showed (audit P4).
function turnFooter(touched, failed) {
  const frag = document.createDocumentFragment();
  const mutations = [...touched].filter(([, e]) => e !== "read");
  if (mutations.length || failed.size) {
    const w = document.createElement("div");
    w.className = "writes";
    for (const [ref, effect] of mutations) w.appendChild(writeCard(ref, effect, null));
    for (const [ref, why] of failed) w.appendChild(writeCard(ref, "failed", why.verb, why.refused));
    // One button for the whole turn stays, but only when there is more than one
    // card: with a single write, per-card revert already says it better.
    if (mutations.length > 1) {
      const u = document.createElement("button");
      u.type = "button";
      u.className = "undo-turn";
      const label = `revert all ${mutations.length} changes`;
      u.textContent = label;
      u.title = "run /undo for every note this turn touched";
      armThenRun(u, label, `click again to revert ${mutations.length}`, () => send("/undo"));
      w.appendChild(u);
    }
    frag.appendChild(w);
  }
  return frag.childElementCount ? frag : null;
}

function writeCard(ref, effect, verb, refused) {
  const copy = WRITE_COPY[refused ? "refused" : effect] || WRITE_COPY.written;
  const card = document.createElement("div");
  card.className = "wcard " + effect;
  card.dataset.ref = ref;
  const op = document.createElement("span");
  op.className = "wc-op";
  op.textContent = copy.label;
  const path = document.createElement("span");
  // A deleted note has no page to open, and a failed write may have created
  // nothing at all: keep the path as a record, drop the click, or it routes to
  // /note and answers "not found in vault".
  // `wc-open`, not `note-link`: a card's path is the thing that changed, not a
  // citation, and borrowing the chip's class also borrowed its cyan underline —
  // which `:is(.msg, #peek-body, …) .note-link` carries at ID specificity, so no
  // amount of class stacking could take it back off. The delegated open-note
  // handler matches both classes.
  const openable = effect !== "deleted" && effect !== "failed";
  path.className = "wc-path" + (openable ? " wc-open" : "");
  if (openable) path.dataset.path = ref;
  path.textContent = ref;
  path.title = ref;
  card.append(op, path);
  if (refused) {
    // The refusal's own line, not "delete failed": nothing failed, the note
    // is the user's to delete. The button arms first, as the bulk revert
    // does, because a delete that leaves links pointing nowhere is the one
    // act on this card that edits what other notes say.
    const n = document.createElement("span");
    n.className = "wc-note";
    n.textContent = refused.text;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wc-act";
    b.textContent = copy.act;
    b.dataset.tip = copy.hint;
    armThenRun(b, copy.act, "click again to delete", () => send("/delete " + refused.path));
    card.append(n, b);
    return card;
  }
  if (!copy.act) {
    const n = document.createElement("span");
    n.className = "wc-note";
    n.textContent = verb ? `${verb} failed · vault unchanged` : "vault unchanged";
    card.appendChild(n);
    // Disabled with its reason, not absent (audit P13): a card with no button
    // where every other card has one reads as a card that lost it.
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wc-act";
    b.textContent = "revert";
    b.disabled = true;
    b.dataset.tip = "nothing landed, nothing to revert";
    b.setAttribute("aria-disabled", "true");
    card.appendChild(b);
    return card;
  }
  // How big the change was. Filled in by loadChanges() from the same /changes
  // payload the sidebar reads, so the number on the card, the number on the
  // sidebar row and the number in the header of the diff this card opens are
  // one number off one baseline. A card that renders before that payload lands
  // simply has no tally yet, rather than a wrong one.
  const tally = document.createElement("span");
  tally.className = "wc-tally";
  tally.dataset.for = ref;
  card.appendChild(tally);
  const b = document.createElement("button");
  b.type = "button";
  b.className = "wc-act";
  b.textContent = copy.act;
  b.dataset.tip = copy.hint;
  b.addEventListener("click", () => {
    b.disabled = true;
    card.classList.add("reverting");
    send("/undo " + ref);
  });
  card.appendChild(b);
  return card;
}

// A write the gate was unsure of asks for your eye (audit P4). /changes says
// which notes carry `review:` and what the judge said; the card whose note it
// is takes the one amber border in the app, the sentence in plain words, a
// three-line excerpt of the diff, `keep` as the primary act (the write already
// landed and is reversible), `revert`, and the sentence that says what revert
// takes back and what stays. Idempotent: called after every /changes and
// after a replay, and a card is dressed once.
function stampReviewCards() {
  if (typeof reviewOf === "undefined") return;
  for (const card of document.querySelectorAll(".wcard.written, .wcard.moved")) {
    const ref = card.dataset.ref || "";
    const open = card.querySelector(".wc-open");
    const path = (open && open.dataset.path) || resolveRef(ref);
    const why = reviewOf.get(path) || "";
    if (!why) {
      if (card.classList.contains("review")) { card.classList.remove("review"); card.querySelector(".wc-review")?.remove(); }
      continue;
    }
    if (card.classList.contains("review")) continue;
    card.classList.add("review");
    const box = document.createElement("div");
    box.className = "wc-review";
    const s = document.createElement("div");
    s.className = "wc-why";
    s.textContent = "review requested · " + why;
    box.appendChild(s);
    const ex = document.createElement("div");
    ex.className = "wc-excerpt";
    box.appendChild(ex);
    fetch("/changes/diff?path=" + encodeURIComponent(path)).then((r) => r.json()).then((d) => {
      // Three lines of what changed, prose before frontmatter: a patch also
      // restamps `AI:` and `sources:`, and those are never the sentence the
      // judge could not find.
      const changed = (d.lines || []).filter((l) => (l.op === "+" || l.op === "-") && l.text.trim());
      const prose = changed.filter((l) => !/^\s*[\w][\w -]*:(\s|$)/.test(l.text) && !/^\s*-\s*["\[]/.test(l.text));
      const lines = (prose.length ? prose : changed).slice(0, 3);
      for (const l of lines) {
        const row = document.createElement("div");
        row.className = "wc-ex-line " + (l.op === "+" ? "add" : "del");
        row.textContent = l.op + " " + l.text;
        ex.appendChild(row);
      }
    }).catch(() => { /* the sentence stands without the excerpt */ });
    const acts = document.createElement("div");
    acts.className = "wc-review-acts";
    const keep = document.createElement("button");
    keep.type = "button";
    keep.className = "wc-keep";
    keep.textContent = "keep";
    keep.addEventListener("click", async () => {
      keep.disabled = true;
      try {
        const r = await fetch("/changes/keep", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
        if (!r.ok) { notify("couldn't clear the review mark"); keep.disabled = false; return; }
      } catch { notify("couldn't clear the review mark"); keep.disabled = false; return; }
      loadChanges(); // the count drops and this card sheds its border
    });
    const revert = document.createElement("button");
    revert.type = "button";
    revert.className = "wc-act";
    revert.textContent = "revert";
    revert.addEventListener("click", () => {
      revert.disabled = true;
      card.classList.add("reverting");
      send("/undo " + path);
    });
    const siblings = card.parentElement ? card.parentElement.querySelectorAll(".wcard.written, .wcard.moved").length - 1 : 0;
    const note = document.createElement("span");
    note.className = "wc-review-note";
    note.textContent = siblings
      ? `revert takes back this one patch; the other write${siblings === 1 ? "" : "s"} in this turn stay${siblings === 1 ? "s" : ""}`
      : "revert takes back this one patch";
    acts.append(keep, revert, note);
    box.appendChild(acts);
    card.appendChild(box);
  }
}

// A button that asks before it acts, without a modal. Reverting is itself a
// mutation of a corpus the user means to keep, and in bulk it was the least
// guarded action in the app: one click took back every note a turn had written,
// with no preview and nothing to press to get back. The button arms instead, so
// the second click is the decision and the first one costs nothing to abandon —
// doing nothing disarms it. No new markup, no focus trap, and the keyboard path
// is the one the button already had.
const ARM_MS = 4000;
function armThenRun(btn, label, armed, run) {
  let timer = null;
  const disarm = () => { timer = null; btn.classList.remove("armed"); btn.textContent = label; };
  btn.addEventListener("click", () => {
    if (timer) {
      clearTimeout(timer);
      disarm();
      btn.disabled = true;
      run();
      return;
    }
    btn.classList.add("armed");
    btn.textContent = armed;
    timer = setTimeout(disarm, ARM_MS);
  });
  btn.addEventListener("blur", () => { if (timer) { clearTimeout(timer); disarm(); } });
}

// The sentence for a failure is authored server-side, once, in
// silica/ui/web/failures.py: every `error` frame and every `tool_error` carries
// it (`error` on the frame, `plain` on the tool row) with the raw text beside
// it as `detail`. The regex table that used to live here guessed at the raw
// text after the fact, and guessed differently from the TUI.

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// A failure belongs to the thing that failed (P3). It attaches under the
// person's own bubble as one authored sentence, a "try again" that re-runs
// exactly that turn, and the raw text behind a disclosure for whoever is
// debugging. Never as an assistant row: a stack trace is not something the
// agent said, and drawing it in the agent's sheet made it read that way.
function attachFailure(userBody, sentence, detail, retry) {
  const host = userBody ? userBody.parentElement : log;
  const box = document.createElement("div");
  box.className = "turn-error";
  const msg = document.createElement("div");
  msg.className = "te-msg";
  msg.textContent = sentence;
  box.appendChild(msg);
  if (retry) {
    const r = document.createElement("button");
    r.type = "button";
    r.className = "te-retry";
    r.textContent = "try again";
    r.addEventListener("click", () => {
      if (streaming) return;
      box.remove();
      retry();
    });
    box.appendChild(r);
  }
  if (detail && detail !== sentence) {
    const d = document.createElement("details");
    d.className = "te-detail";
    d.innerHTML = "<summary>detail</summary>";
    const pre = document.createElement("pre");
    pre.textContent = detail;
    d.appendChild(pre);
    box.appendChild(d);
  }
  host.appendChild(box);
  followBottom();
}


// Hover-revealed "copy" button in a message body's corner. getText() is called
// at click time so live turns can hand back their accumulated raw markdown.
function addCopyBtn(bodyEl, getText) {
  const b = document.createElement("button");
  b.className = "copy-btn";
  b.type = "button";
  b.textContent = "copy";
  b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(getText()); b.textContent = "copied"; }
    catch { b.textContent = "failed"; }
    setTimeout(() => (b.textContent = "copy"), 1200);
  });
  // At the end of the ledger line when the turn has one (ADR-0117): the line
  // is the answer's first row, so the button needs no lane of its own above it.
  const led = bodyEl.querySelector(":scope > .ledger");
  (led || bodyEl).appendChild(b);
}

// --- flagging an answer (ADR-0100 point 6) -----------------------------------
// The server names a turn ({ session, seq }) in its done frame and on a reopened
// chat's user rows only when it runs hosted, so the turn id is the whole switch:
// a local install never draws the control. A flag is the student's consent for
// the owner to read this one turn, so the sheet says what that covers before
// anything is sent, and nothing is sent until Send.
let flagOpener = null;

function flagControl(turn) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "flag-btn";
  b.textContent = "this answer was wrong";
  b.addEventListener("click", () => openFlagSheet(turn, b));
  return b;
}

function openFlagSheet(turn, opener) {
  const note = $("#flag-note");
  const send = $("#flag-send");
  flagOpener = opener;
  note.value = "";
  send.disabled = false;
  send.onclick = async () => {
    send.disabled = true;
    try {
      const r = await fetch("/flag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session: turn.session, seq: turn.seq, note: note.value.trim() }),
      });
      if (!r.ok) throw new Error(String(r.status));
      const done = mkFlagDone();
      opener.replaceWith(done);
      flagOpener = null;
      closeFlagSheet();
    } catch {
      send.disabled = false;
      notify("couldn't send the flag. Try again.");
    }
  };
  $("#flag-sheet").hidden = false;
  note.focus();
}

function mkFlagDone() {
  const s = document.createElement("span");
  s.className = "flag-done";
  s.textContent = "flagged as wrong, thank you";
  return s;
}

// True when it closed something, for the app's one Escape handler.
function closeFlagSheet() {
  const sheet = $("#flag-sheet");
  if (!sheet || sheet.hidden) return false;
  sheet.hidden = true;
  if (flagOpener && flagOpener.isConnected) flagOpener.focus();
  flagOpener = null;
  return true;
}
$("#flag-cancel").addEventListener("click", closeFlagSheet);
$("#flag-sheet").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeFlagSheet(); });

// A chat answer's diagrams run the way a note's do (ADR-0082). Until then the
// server's <pre class="mermaid"> reached the transcript as its source, in every
// answer, because renderMermaid only ever ran on the note body (3f5d43c9
// onward). A fence mermaid's own parser rejects closes into one line with the
// source behind it: mid-session a broken diagram would read as code, and the
// owner still has the text to see what the model wrote. Parsed before run(),
// not judged after it: run() marks a block processed before it draws, so a
// second call landing mid-draw would have taken a pending block for a failed one.
async function drawDiagrams(root) {
  const pending = [...root.querySelectorAll("pre.mermaid:not([data-processed])")];
  if (!pending.length) return;
  await loadMermaid();
  for (const b of pending) {
    // A model's own `style A fill:#f9f` paints over the theme (ADR-0069) and,
    // on graphite, puts the light label ink on a pastel fill: 2 of 10 audited
    // /diagram answers did it. A note keeps its author's styles; a reply does not.
    b.textContent = b.textContent.replace(/^\s*(?:style|linkStyle)\s.*$\n?/gm, "");
    if (await mermaid.parse(b.textContent, { suppressErrors: true })) continue;
    const quoted = quoteLabels(b.textContent);
    if (quoted !== b.textContent && await mermaid.parse(quoted, { suppressErrors: true })) {
      b.textContent = quoted;
      continue;
    }
    const d = document.createElement("details");
    d.className = "diagram-failed";
    d.innerHTML = "<summary>diagram not drawn</summary><pre><code></code></pre>";
    d.querySelector("code").textContent = b.textContent;
    b.replaceWith(d);
  }
  return renderMermaid(root);
}

// Mermaid reads ( [ { | inside a bare label as syntax, which failed most of the
// /diagram blocks the 9B wrote that did not parse (ADR-0082): "f(a)", "[a,b]",
// "toString()". A quoted label is mermaid's own escape, so a flowchart label or
// edge label holding one of those is quoted, keeping its shape bracket. In a
// mindmap the 9B writes every node but the root as plain text ("equals(obj)"
// is a method, not a node `equals` labelled "obj"), so each such line becomes
// one quoted node. Run only on a block that already failed to parse: a diagram
// that parses is never rewritten.
function quoteLabels(src) {
  const pairs = { "[": "]", "(": ")", "{": "}" };
  const risky = /[()[\]{}|"]/;
  const q = (t) => `"${t.replace(/"/g, "#quot;")}"`;
  const lines = src.split("\n");
  const mind = /^\s*mindmap\b/.test(lines[0] || "");
  return lines.map((line, n) => {
    const t = line.trim();
    if (n === 0 || !t) return line;
    if (mind) {
      // the root, `::icon(...)` / `:::class` syntax, and a node already quoted stay
      if (n === 1 || t.startsWith(":") || /^[\w-]*\["/.test(t) || !risky.test(t)) return line;
      return `${line.match(/^\s*/)[0]}n${n}[${q(t)}]`;
    }
    let out = "", i = 0;
    for (const m of line.matchAll(/[A-Za-z_][\w-]*([[({])/g)) {
      const open = m[1], at = m.index + m[0].length;
      if (m.index < i || line[at] === open || line[at] === '"') continue; // a shape like [[ ]] or (( )), or quoted
      let depth = 1, j = at;
      for (; j < line.length && depth; j++) {
        if (line[j] === open) depth++;
        else if (line[j] === pairs[open]) depth--;
      }
      if (depth || !risky.test(line.slice(at, j - 1))) continue;
      out += line.slice(i, at) + q(line.slice(at, j - 1)) + pairs[open];
      i = j;
    }
    return (out + line.slice(i)).replace(/\|([^|"\n]*[()[\]{}][^|"\n]*)\|/g, (_m, l) => `|${q(l)}|`);
  }).join("\n");
}

// ponytail: lazy live markdown for the streaming turn — headings, bold, italic,
// inline + fenced code, bullet/ordered lists, links, rules, GFM tables. Re-parses the whole segment
// on every delta (O(n²) over the turn, fine at KB scale; parse from the last
// block boundary if very long turns ever stutter). The server re-renders
// the canonical answer (wikilinks, callouts, mermaid) on `done` for uninterrupted
// turns; swap in a vendored parser if full CommonMark is ever needed here.
function mdLite(src) {
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  // `[[note|alias]]` -> the same .note-link the server render emits. The target
  // is passed through raw: /note resolves titles and paths itself, so the live
  // segment needs no vault index (and an unresolvable one just says so on click).
  // `![[figure.png|500]]` -> the <img> the server's _embed_img emits: a figure
  // copied from a note beside the sentence it supports is what ADR-0082 asks the
  // model for, and a tool-split turn keeps this render until reload.
  const IMG = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;
  const unesc = (s) => s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  const wiki = (t, hold) =>
    t.replace(/(!?)\[\[([^\]|\n]+?)(?:\|([^\]\n]+?))?\]\]/g, (_m, bang, target, alias) => {
      if (bang && IMG.test(target.trim())) {
        const file = unesc(target.trim());
        const width = alias && /^\d+$/.test(alias) ? ` width="${alias}"` : "";
        const alt = (alias && !width ? alias : file.split("/").pop().replace(IMG, "")).replace(/"/g, "&quot;");
        return hold(`<img src="/asset?path=${encodeURIComponent(file)}" alt="${alt}"${width}>`);
      }
      const path = target.split("#")[0].trim();
      const shown = (alias || path.split("/").pop().replace(/\.md$/, "")).trim();
      return bang + hold(`<a class="note-link" data-path="${path.replace(/"/g, "&quot;")}">${shown}</a>`);
    });
  // Only these schemes become a live href. mdLite builds its anchors by hand and
  // gets none of the validateLink pass markdown-it runs server-side, so a
  // model-authored `[x](javascript:…)` landed as a live anchor in the app's own
  // origin. A whitelist, not a blocklist: `java\tscript:` walks straight through
  // a blocklist and the browser still runs it. Unsafe → the text, no anchor.
  const safeHref = (u) => {
    const s = u.trim().replace(/[\x00-\x1f]/g, "");
    const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(s);
    return !scheme || ["http", "https", "mailto"].includes(scheme[1].toLowerCase()) ? s : null;
  };
  // Sentence punctuation and an unbalanced closing paren belong to the prose, not
  // to the URL — the same call linkify-it makes server-side, so a citation ending
  // in a full stop links the same in both renders (a Wikipedia URL's own balanced
  // parens survive).
  const trimUrl = (u) => {
    let end = u.length;
    const count = (s, c) => (s.split(c).length - 1);
    for (;;) {
      if (end > 0 && ".,;:!?".includes(u[end - 1])) { end--; continue; }
      const head = u.slice(0, end);
      if (end > 0 && u[end - 1] === ")" && count(head, "(") < count(head, ")")) { end--; continue; }
      return u.slice(0, end);
    }
  };
  // Both link forms in ONE pass. A bare URL matched before the markdown form eats
  // the target inside `](…)`; matched after, it re-matches the URL already sitting
  // in `href="…"` and nests the anchors. The lookbehind keeps it off a URL that
  // sits in an attribute or right after a word.
  const LINK = /\[([^\]]+)\]\(([^)\s]+)\)|(?<![\w"'=@./-])(https?:\/\/[^\s<>"'`]+)/g;
  const inline = (t) => {
    // wiki()'s tags and code spans are parked as placeholders for the rest of the
    // pass. Emphasis and links used to run INSIDE the <code> already produced, so
    // `` `https://x` `` came out a live anchor; and the link pattern matched from a
    // `[` in an <img>'s alt to a later `](…)`, closing the attribute early and
    // writing the model's target as new attributes of the <img>: an onerror that
    // ran as the segment painted (review of ADR-0082, 2026-10-08).
    const held = [];
    const hold = (html) => `\u0000${held.push(html) - 1}\u0000`;
    // a code span can hold a parked wiki tag, so a restored one is restored again
    const restore = (s) => s.replace(/\u0000(\d+)\u0000/g, (_m, i) => restore(held[i]));
    return restore(wiki(esc(t), hold)
      .replace(/`([^`]+)`/g, (_m, c) => hold(`<code>${c}</code>`))
      .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+?)\*/g, "<em>$1</em>")
      .replace(LINK, (_m, txt, target, bare) => {
        if (bare === undefined) {
          const h = safeHref(target);
          return h ? `<a href="${h.replace(/"/g, "&quot;")}">${txt}</a>` : txt;
        }
        const u = trimUrl(bare);
        return `<a href="${u.replace(/"/g, "&quot;")}">${u}</a>` + bare.slice(u.length);
      }));
  };
  const lines = src.split("\n");
  const out = [];
  let i = 0, list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const HR = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;
  // A GFM delimiter row: pipes, colons, spaces, and at least one dash.
  const DELIM = /^\s*\|?[\s:|-]*-[\s:|-]*$/;
  const isBlock = (l) => /^```|^#{1,6}\s|^\s*[-*]\s|^\s*\d+\.\s|^\s*\|/.test(l) || HR.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { closeList(); i++; continue; }
    if (HR.test(line)) { closeList(); out.push("<hr>"); i++; continue; }
    // GFM table: a piped header row whose next line is the delimiter. Checked
    // before the paragraph branch, or the whole grid collapses into one <p> of
    // pipes — which is what every tool-interrupted turn was showing, since only
    // uninterrupted turns get upgraded to the server render.
    // Escaped `\|` cells and per-column alignment match the server render:
    // the escape parks as U+0001 so the split never sees it, and the delimiter
    // row's colons become the same inline text-align markdown-it emits.
    if (/^\s*\|/.test(line) && DELIM.test(lines[i + 1] || "")) {
      closeList();
      const cells = (l) =>
        l.trim().replace(/\\\|/g, "\u0001").replace(/^\||\|$/g, "").split("|")
          .map((c) => c.trim().replace(/\u0001/g, "|"));
      const head = cells(line);
      const align = cells(lines[i + 1]).map((c) =>
        /^:-+:$/.test(c) ? "center" : /^-+:$/.test(c) ? "right" : /^:-+$/.test(c) ? "left" : "");
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      const td = (c, j, tag) =>
        `<${tag}${align[j] ? ` style="text-align:${align[j]}"` : ""}>${inline(c)}</${tag}>`;
      const tr = (cs, tag) => `<tr>${cs.map((c, j) => td(c, j, tag)).join("")}</tr>`;
      out.push(
        `<table><thead>${tr(head, "th")}</thead><tbody>${rows.map((r) => tr(r, "td")).join("")}</tbody></table>`
      );
      continue;
    }
    if (/^```/.test(line)) {
      closeList();
      const buf = []; i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      // Only a closed mermaid fence becomes the server's <pre class="mermaid">:
      // drawDiagrams() runs it once the turn ends, and half a diagram is not one.
      const diagram = i < lines.length && line.slice(3).trim() === "mermaid";
      i++; // closing fence (or EOF while still streaming)
      out.push(diagram ? `<pre class="mermaid">${esc(buf.join("\n"))}</pre>` : `<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) {
      const want = /^\s*\d/.test(line) ? "ol" : "ul";
      if (list !== want) { closeList(); out.push(`<${want}>`); list = want; }
      out.push(`<li>${inline(item[1])}</li>`); i++; continue;
    }
    closeList();
    // The current line always goes in, even when isBlock() calls it a block. A
    // table header whose delimiter row has not streamed in yet lands here: it is
    // a block by isBlock(), so an empty paragraph would leave `i` untouched and
    // the outer loop would spin forever, growing `out` until the tab threw
    // "RangeError: Invalid array length" — which killed the SSE reader and ate
    // the rest of the answer. Consuming one line per pass is what guarantees the
    // parser terminates, whatever the half-arrived block looks like.
    const para = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !isBlock(lines[i])) para.push(lines[i++]);
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  closeList();
  return out.join("");
}

// A JSON payload that rides a header. Fail-open by design: a meter is a reading,
// and a malformed one must leave the app answering questions, not stop it.
function jsonHeader(r, name) {
  try { return JSON.parse(r.headers.get(name) || "null"); } catch { return null; }
}

function fmtTokens(n) {
  n = Number(n) || 0;
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}
// --- the context ring -------------------------------------------------------
// One number, three readings: the ring is how full the window is, the panel is
// what filled it, and the caution level is the one fill that means something.
// The parts are counted server-side by _context_breakdown, which charges
// litellm's per-call chat envelope exactly once so they sum to the total printed
// beside them — a meter that invites you to add up its own segments has to
// survive that.
const CTX_R = 8;                    // tracks the <circle r> in index.html
const CTX_C = 2 * Math.PI * CTX_R;
// In the order the model meets them, which is also the reverse of the order
// they stop being yours: the instructions and the tool block never move, the
// read results are what compaction collapses first. The tool definitions are
// their own part rather than part of `instructions` because they are the
// largest resident of an idle window (7.2k against 2.1k), and a window that
// counted only the messages reported a sixth of what the provider billed.
// Ordinal ramp, not the interface palette: this is a bar series and --accent is
// reserved for what you can click.
const CTX_PARTS = [
  ["system", "instructions", "ord-1"],
  ["tool_specs", "tool definitions", "ord-2"],
  ["tool_io", "recall + tool results", "ord-3"],
  ["messages", "conversation", "ord-4"],
];
let ctxMeter = { used: 0, max: 0, parts: null, compactAt: 0.6 };

function setCtxTokens(used, max, parts, compactAt) {
  ctxMeter = {
    used: Number(used) || 0,
    max: Number(max) || 0,
    parts: parts || null,
    compactAt: Number(compactAt) || ctxMeter.compactAt,
  };
  const ring = $("#ctx-ring");
  if (!ring) return;
  // An empty window has nothing to read: on a fresh chat the ring would be a
  // blank circle beside send, which is a control that answers nothing. It
  // appears with the first turn, the way #side-changes appears with the first
  // write.
  ring.hidden = !(ctxMeter.max && ctxMeter.used);
  if (ring.hidden) { closeCtxPanel(); return; }
  const frac = Math.min(1, ctxMeter.used / ctxMeter.max);
  const pct = Math.round(frac * 100);
  ring.querySelector(".ctx-fill").style.strokeDasharray =
    `${(CTX_C * frac).toFixed(2)} ${CTX_C.toFixed(2)}`;
  // Amber is caution in this palette and nothing else, so it arms where the
  // condition it names actually begins — the fill at which the loop starts
  // collapsing old read results — not at an eyeballed "looks full".
  ring.dataset.level = frac >= ctxMeter.compactAt ? "high" : "";
  ring.setAttribute("aria-label",
    `context window ${pct}% full, ${fmtTokens(ctxMeter.used)} of ${fmtTokens(ctxMeter.max)}`);
  ring.title = `context ${pct}% · ${fmtTokens(ctxMeter.used)}/${fmtTokens(ctxMeter.max)}`;
  if (!$("#ctx-panel").hidden) renderCtxPanel();
}

// Who answers in this session, filled by loadConfig (app-boot.js).
let chatModels = null;
function renderCtxPanel() {
  const panel = $("#ctx-panel");
  const { used, max, parts, compactAt } = ctxMeter;
  const pct = max ? Math.round((used / max) * 100) : 0;
  const free = Math.max(0, max - used);
  const rows = [];
  // The bar is the composition of what is IN the window, at full width, and the
  // ring beside it is how full. Splitting the two questions is what keeps the
  // bar readable: a bar that also carried the free space spent 98% of itself on
  // the empty end at every fill anyone actually works at, and compressed the
  // three parts it exists to compare into a six-pixel sliver.
  const segs = [];
  for (const [key, label, tier] of CTX_PARTS) {
    const n = (parts && Number(parts[key])) || 0;
    if (n > 0) segs.push(`<div class="stack-seg ${tier}" style="flex:${n}"></div>`);
    rows.push(`<div class="ctxp-row"><span class="swatch ${tier}"></span>`
      + `<span class="ctxp-k">${label}</span>`
      + `<span class="ctxp-v">${fmtTokens(n)}</span></div>`);
  }

  // Stated rather than implied by the empty part of the bar: what a reader wants
  // at 70% is how much room is left, and reading it off a track is a subtraction
  // they should not have to do.
  rows.push(`<div class="ctxp-row free">`
    + `<span class="ctxp-k">free</span>`
    + `<span class="ctxp-v">${fmtTokens(free)}</span></div>`);
  // The one thing this panel exists to warn about, and only while it is true.
  const note = used / max >= compactAt
    ? `<p class="ctxp-note">Past ${Math.round(compactAt * 100)}%: old read results`
      + ` are being collapsed to stubs that name the call to re-issue.</p>`
    : "";
  panel.innerHTML = `<div class="ctxp-head"><span class="ctxp-title">Context</span>`
    + `<span class="ctxp-fig">${fmtTokens(used)} / ${fmtTokens(max)}</span>`
    + `<span class="ctxp-pct">${pct}%</span></div>`
    + `<div class="stack">${segs.join("")}</div>`
    + rows.join("") + note
    + (chatModels
      ? `<div class="ctxp-models"><span class="cm-lbl">model</span><span class="cm-val">${escapeHtml(chatModels.model)}</span>`
        + (chatModels.worker ? `<span class="cm-lbl">worker</span><span class="cm-val">${escapeHtml(chatModels.worker)}</span>` : "")
        + `<button type="button" class="linklike" id="ctxp-settings">settings</button></div>`
      : "");
  $("#ctxp-settings")?.addEventListener("click", () => { closeCtxPanel(); openSettings(); });
}

function closeCtxPanel(refocus = false) {
  const panel = $("#ctx-panel");
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  $("#ctx-ring").setAttribute("aria-expanded", "false");
  if (refocus) $("#ctx-ring").focus(); // Escape: back to the opener (P9)
}

// Both send buttons change their word for the length of a turn: the server
// answers one at a time (409 otherwise), so a second message cannot be SENT,
// but it can be queued (P2), and a button that says so is better than a dead
// one. The placeholder says the same thing where the eye already is.
// The idle placeholder names what the field is for and stops teaching keys
// (ADR-0117 point 5); the busy one keeps its clause, because a queue is not
// something anyone guesses an Enter does.
const PH_IDLE = "Ask about your notes";
// The parenthetical is the clause the fitter drops when the field is narrow.
const PH_BUSY = "type the next message  (⏎ queues it for after this turn)";
// The chat's send button has three acts and shows one (ADR-0117 point 4): send,
// or for the length of a turn stop, and queue while the field holds text, so
// a message typed mid-turn is never one click away from interrupting it. Enter
// only ever sends or queues; stopping is this button and the dock's strip.
const SEND_ACT = {
  send: "send",
  queue: "queue this message for after the current turn",
  stop: "stop the current response",
};
function syncSend() {
  const b = $("#send");
  const mode = !streaming ? "send" : (input.value.trim() ? "queue" : "stop");
  b.dataset.mode = mode;
  b.setAttribute("aria-label", SEND_ACT[mode]);
  b.title = SEND_ACT[mode];
  if (mode !== "stop") b.disabled = false; // a stop that landed re-arms here
}
input.addEventListener("input", () => { if (streaming) syncSend(); });
function setSendDisabled(v) {
  syncSend();
  $("#dock-send").textContent = v ? "queue" : "send";
  // Through the fitter (app-boot.js), not the attribute: it re-fits the FULL
  // string on every resize, so a placeholder written here alone lasted until
  // the next layout pass.
  input.dataset.phFull = v ? PH_BUSY : PH_IDLE;
  if (typeof fitPlaceholder === "function") fitPlaceholder(input);

  // Both edges of a turn in one place: an open settings panel locks itself while
  // a response runs and unlocks when it ends, rather than waiting for a 409.
  stSetBusy(v);
}

// --- the queue: one message, held until this turn settles ------------------
// ponytail: client-side and one deep. The server already guarantees one turn
// at a time (409), so a second slot is a client fact; move it to the server
// when a second driver (Obsidian, MCP) needs to see what the GUI has queued.
let queued = null;
const queueBox = $("#queue");

function renderQueue() {
  queueBox.hidden = queued === null;
  queueBox.querySelector(".q-text").textContent = queued || "";
  queueBox.querySelector(".q-text").title = queued || "";
}
function queueMessage(text) {
  if (!text.trim()) return;
  // A second Enter while one is already held appends to it: the study's
  // "queued messages are one turn, in order", without a list to manage.
  queued = queued ? queued + "\n" + text : text;
  renderQueue();
  announce("queued: sends when this turn ends");
}
function takeQueued() {
  const t = queued;
  queued = null;
  renderQueue();
  return t;
}
$("#q-drop").addEventListener("click", () => { takeQueued(); input.focus(); });
// Interrupting is the one high-regret act, and it is this button and nothing
// else: it stops the running turn, and the settle path below sends what was
// held. Stop first, then send, by construction rather than by timing.
$("#q-now").addEventListener("click", () => {
  if (!streaming) { const t = takeQueued(); if (t) send(t); return; }
  fetch("/stop", { method: "POST" });
});
function sendQueued() {
  // A drop's question whose file was read during this turn goes first: it was
  // asked before anything queued (ADR-0121 point 1). The queued text stays
  // queued, and the end of that turn sends it.
  const drop = typeof takeDropTurn === "function" ? takeDropTurn() : null;
  if (drop) { setTimeout(() => jobOffer(drop, "ask"), 0); return; }
  const t = takeQueued();
  if (t) setTimeout(() => send(t), 0); // after the finally that called this returns
}

// --- the draft: what you typed survives a reload -----------------------------
// Per chat, in localStorage, on a debounce; flushed on pagehide and on hidden
// visibility because those are the two edges a tab leaves through. A restore
// concatenates UNDER whatever was typed since, so it cannot overwrite a
// sentence the person started before the restore ran.
const DRAFT_MS = 400;
let draftSid = "";
let draftTimer = null;
const draftKey = () => "draft:" + (draftSid || "new");
function saveDraft() {
  try {
    if (input.value.trim()) localStorage.setItem(draftKey(), input.value);
    else localStorage.removeItem(draftKey());
  } catch { /* quota or private mode: the draft is a courtesy, not a record */ }
}
function clearDraft() {
  try { localStorage.removeItem(draftKey()); } catch { /* same */ }
}
function restoreDraft(sid) {
  if (sid === draftSid && sid) return; // same chat, already restored
  draftSid = sid || "";
  let d = "";
  try { d = localStorage.getItem(draftKey()) || ""; } catch { /* same */ }
  if (!d) return;
  const typed = input.value;
  input.value = typed.trim() ? typed.replace(/\s+$/, "") + "\n" + d : d;
  autoGrow(input);
  renderHighlight();
}
window.addEventListener("pagehide", saveDraft);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") saveDraft();
});

// `retry` re-runs the exact same turn. It is a callback rather than the text so
// that /find, the dock and a nucleate-and-ask all retry the thing THEY sent.
async function runTurn(fetchPromise, pendingLabel = "working", retry = null) {
  if (streaming) return;
  streaming = true;
  const turnT0 = performance.now();
  let toolsDone = 0; // for the stopped row: how much of the turn had landed
  // What the stone in the composer shows when the turn ends (ADR-0048): a
  // stopped turn is neither an arrival nor a failure, so it goes back to idle.
  let outcome = "idle";
  setRunVisible(true);
  setSendDisabled(true);
  announce("silica is responding");
  const body = bubble("silica");
  // The ledger line heads the turn (ADR-0117 point 3): live while it runs, the
  // counts once it ends, and the one way to open the rows the flow keeps.
  const ledger = makeLedger(body.closest(".msg"), true);
  body.appendChild(ledger);
  let steps = 0;
  // flow = thinking blocks, tool groups and text segments interleaved in arrival
  // order, so the transcript reads chronologically: think, tools, think, tools,
  // text… (Claude-style). In this agent the connective tissue between tool calls
  // is *thinking*, so it must interleave too or tools pile into one group.
  const flow = document.createElement("div");
  body.appendChild(flow);

  // The live iridescent caret is ONE physical element, re-parented onto
  // whatever is streaming right now (thinking body / tool group / text tail).
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.textContent = "▍";

  const toolEls = {};
  const texts = [];    // every text segment { el, raw }, for the copy button
  let flagTurn = null; // { session, seq } from the done frame, hosted only (ADR-0100)
  // ref → effect ("read" | "written" | "moved" | "deleted"), for the footer.
  // A ref is only recorded once its tool SUCCEEDS: a failed write must not be
  // reported as written, in the one place the user looks to trust the agent.
  const touched = new Map();
  // ref → the verb that failed. A write that fails lints and self-reverts used to
  // leave NOTHING behind: its refs were dropped, so the footer showed no chip and
  // the only trace was a tool line with a raw exception in it. "the vault did not
  // change" is a result the user needs stated, not inferred from an absence.
  const failed = new Map();
  const claimed = {};  // call id → { refs, effect, verb }, held until tool_done
  const found = [];    // notes the searches returned, shown when none was read (ADR-0121)
  const pipes = {};    // call id → injector pipeline block, held until tool_done
  let curPipe = null;  // the block phase events currently belong to
  let curText = null;   // open markdown segment { el, raw }
  let curTools = null;  // open group of consecutive tools
  let curThink = null;  // open thinking block { details, body, raw }
  let segments = 0;     // text runs so far; an uninterrupted one upgrades to server html
  // Segments painted since the last tool block — all a `reset` can still take
  // back. A tool result is committed, so anything above one stands, and the
  // *open* segment is not the unit: a retry that streamed think→text has already
  // let go of its thinking block by the time the retraction arrives.
  let live = [];

  // Opening one segment kind closes the other two; a thinking block collapses
  // as it closes (it stays open only while it is the live tail).
  function close(keep) {
    if (keep !== "text") { flushPaint(); curText = null; }
    if (keep !== "tools") curTools = null;
    if (keep !== "think" && curThink) { curThink.details.open = false; curThink = null; }
  }
  function thinkSeg() {
    if (curThink) return curThink;
    close("think");
    const details = document.createElement("details");
    details.className = "thinking";
    details.open = true;
    details.innerHTML = `<summary>thinking</summary><div class="thinking-body"></div>`;
    flow.appendChild(details);
    curThink = { details, body: details.querySelector(".thinking-body"), raw: "" };
    live.push(curThink);
    return curThink;
  }
  function textSeg() {
    if (curText) return curText;
    close("text");
    const el = document.createElement("div");
    el.className = "stream-text";
    flow.appendChild(el);
    curText = { el, raw: "" };
    texts.push(curText);
    live.push(curText);
    segments++;
    return curText;
  }
  // One paint per frame, not per delta: mdLite re-parses the whole segment and
  // rewrites its innerHTML, so painting on every token was O(n²) over the turn
  // (a 32k-token answer arrives as ~30k deltas). A delta only grows `raw`; the
  // frame paints whatever accumulated. Anything that ends or supersedes the
  // segment (close, done) flushes first, so a segment cut by a tool call never
  // loses its last tokens to a frame that fires after it.
  let paintPending = 0;
  function paintLive() {
    paintPending = 0;
    if (!curText) return;
    curText.el.innerHTML = mdLite(curText.raw);
    (curText.el.lastElementChild || curText.el).appendChild(caret); // inline at the text tail
  }
  function paintSoon() {
    if (!paintPending) paintPending = requestAnimationFrame(paintLive);
  }
  function flushPaint() {
    if (!paintPending) return;
    cancelAnimationFrame(paintPending);
    paintLive();
  }
  // Retract the model's output for a server-sent `reset` delta. `textOnly` keeps
  // the thinking: a turn that resolved into a tool call retracts the preamble it
  // streamed, not the reasoning that produced the call. A full reset (a retry
  // replays the attempt from the top) takes the reasoning too, or the thinking
  // block ends up holding both passes.
  function dropLiveSegments(textOnly) {
    for (const seg of live) {
      if (seg.details) {
        if (!textOnly) seg.details.remove();
        continue;
      }
      seg.el.remove();
      const i = texts.indexOf(seg);
      if (i >= 0) texts.splice(i, 1);
      segments--; // keeps `done`'s "uninterrupted turn" upgrade test honest
    }
    live = textOnly ? live.filter((s) => s.details) : [];
    curText = null;
    if (!textOnly) curThink = null;
    peekRollback();
    // The drop detached the caret with the segment it lived in; a retry can
    // back off for seconds, and a bubble with no activity marker reads as done.
    flow.appendChild(caret);
  }
  function toolsGroup() {
    if (curTools) return curTools;
    close("tools");
    const g = document.createElement("div");
    g.className = "tools";
    flow.appendChild(g);
    live = [];  // a tool result commits everything above it
    peekMark(); // …including the dock's copy of it
    return (curTools = g);
  }
  const flowMsg = (s) => { const d = document.createElement("div"); d.className = "stream-text"; d.textContent = s; flow.appendChild(d); };

  // The first SSE event can be minutes away — /nucleate converts a PDF before
  // the turn even starts — and until it lands `flow` is empty, so the answer
  // block read as a hang next to a live Stop button. Park a pulsing line and the
  // caret there and drop them on the first event, as openPeek() does for the dock.
  const pending = document.createElement("div");
  pending.className = "tools";
  pending.innerHTML = `<div class="tool">» ${escapeHtml(pendingLabel)} …</div>`;
  flow.appendChild(pending);
  pending.appendChild(caret);

  try {
    const resp = await fetchPromise;
    if (resp.status === 409) { flowMsg("(a turn is already in progress)"); return; }
    // Any other refusal used to be read as an event stream, yield no event and
    // vanish without a word: Cloudflare's 524 after its 125 s read timeout, the
    // proxy's 502 while a tenant restarts, a 413 or 400 that carries a sentence
    // (found 2026-10-13, ADR-0099).
    if (!resp.ok) {
      let said = "";
      try {
        const d = await resp.json();
        said = d.error || (typeof d.detail === "string" ? d.detail : "");
      } catch {
        // A proxy's HTML page carries no sentence; the status in the detail does.
      }
      // A proxy's 413 carries no sentence: Caddy's own cap can sit under the
      // edge's 100 MB the page checks before sending.
      if (!said && resp.status === 413) said = "the file is larger than the server accepts";
      throw Object.assign(new Error(`HTTP ${resp.status}`), { said });
    }
    // A drop or a command that only queued conversions answers 202 with its
    // jobs (ADR-0099), and anything else that is not an event stream used to
    // yield no event and end without a word (point 5).
    const type = resp.headers.get("content-type") || "";
    if (!type.startsWith("text/event-stream")) {
      const d = type.includes("json") ? await resp.json().catch(() => null) : null;
      if (!d || !Array.isArray(d.jobs)) {
        throw Object.assign(new Error(`HTTP ${resp.status} ${type || "without a type"}`),
          { said: "the server's answer was not a reply. Try again." });
      }
      pending.remove();
      drawJobAnswer(flow, d);
      outcome = "success";
      announce("files queued for conversion");
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        handle(JSON.parse(line.slice(6)));
      }
    }
  } catch (e) {
    // The transport failed, not the agent: the failure rides the message that
    // was sent, and no assistant row is drawn for words nobody said.
    peekError(String(e));
    attachFailure(lastUserBody, e.said || "the server didn't answer. Try again.", String(e), retry);
    announce("the turn failed");
    outcome = "error";
  } finally {
    streaming = false;
    setRunVisible(false);
    composerPresence?.state(outcome);
    setSendDisabled(false);
    pending.remove(); // no-op if the first event already dropped it
    caret.remove(); // no-op if a rerender already detached it
    freezePeek(); // done or aborted — stop mirroring, keep the preview up
    if (curThink) curThink.details.open = false; // aborted mid-thought — still collapse
    settleApprovalCards(); // the turn is over: a card still open was never answered
    // A mutation is an object, a read is a citation, and they no longer share a
    // row of identical chips. The product's whole claim is that a write to your
    // vault is safe to delegate, and a write used to announce itself as a 12px
    // chip with a small outlined button beside it — the least prominent thing in
    // its own footer. Each one now gets a card it can carry a state on, and its
    // own revert, so you undo the write you are looking at rather than the turn.
    const footer = turnFooter(touched, failed);
    if (footer) flow.appendChild(footer);
    finishTurn(body, ledger, touched, { steps, secs: (performance.now() - turnT0) / 1000, found });
    const answer = texts.map((t) => t.raw).join("\n\n").trim();
    if (answer) addCopyBtn(body, () => answer);
    if (answer && flagTurn) flow.appendChild(flagControl(flagTurn));
    // A bubble that never got a word, a step or a footer is an empty sheet
    // attributed to the agent; the failure that emptied it already sits on the
    // person's message.
    if (!flow.childElementCount) body.closest(".msg").remove();
    markLastTurn();
    syncJump();
    sendQueued(); // what was held during this turn goes next, in order
    loadSessions(); // turn saved server-side — refresh titles/order
    loadVaultInfo(); // a turn may have written notes — refresh stats + tree
    loadChanges();   // …and the sidebar's record of what it changed
    if (typeof loadLimits === "function") loadLimits(); // what the turn spent (ADR-0102)
    markVaultChanged(); // a turn may have written notes
    document.dispatchEvent(new CustomEvent("silica:turn-end", { detail: { outcome } })); // ADR-0105
  }

  function handle(ev) {
    // The files the question carried, named by the server (ADR-0121 point 4):
    // they belong to the bubble above, and the answer has not begun.
    // They are what the answer stands on, so they count as read, ahead of
    // anything a search returns.
    if (ev.type === "files") {
      userFiles(lastUserBody, ev.files);
      for (const f of ev.files) claimRefs({ notes: f.notes || [], effect: "read" }, touched, failed);
      return;
    }
    pending.remove(); // something arrived — the placeholder has done its job
    if (ev.type === "delta" && ev.kind === "reset") {
      // The server retracts what it just streamed: a transient retry replays the
      // whole attempt (agent/llm.py), and a turn that resolved into tool calls
      // streamed a preamble, never an answer (agent/loop.py). Without this branch
      // the event fell through and the replay was spliced under the truncated
      // first take, so the GUI showed a duplicated answer the TUI did not.
      // A reset's `text` is the retraction scope, not delta text: "" takes the
      // whole attempt (reasoning included), "text" the answer alone.
      dropLiveSegments(ev.text === "text");
    } else if (ev.type === "delta" && ev.kind === "reasoning") {
      const th = thinkSeg();
      th.raw += ev.text;
      th.body.textContent = th.raw;
      th.body.appendChild(caret);
      th.body.scrollTop = th.body.scrollHeight; // follow the caret in the capped box
    } else if (ev.type === "delta" && ev.kind === "text") {
      if (!curText) noteAppended(); // a new section, for the pill's count
      textSeg().raw += ev.text;
      paintSoon();
      peekDelta(ev.text);
      composerPresence?.spoke(); // the stone in the composer lights while text lands
    } else if (ev.type === "tool_start") {
      steps++;
      if (ev.pipeline) {
        // A nucleate run gets the block instead of a line; tool calls are
        // dispatched one at a time (agent/loop.py), so the phase events that
        // follow belong to this one until its tool_done arrives.
        const p = makePipelineBlock(ev.target || "?", ev.pipeline);
        toolsGroup().appendChild(p.el);
        curTools.appendChild(caret);
        pipes[ev.id] = p;
        curPipe = p;
        claimed[ev.id] = { refs: ev.notes || [], effect: ev.effect || "read", verb: ev.name };
        return;
      }
      const t = makeToolRow(toolLabel(ev));
      t.section("in", ev.input);
      toolsGroup().appendChild(t.el);
      curTools.appendChild(caret);
      toolEls[ev.id] = t;
      claimed[ev.id] = { refs: ev.notes || [], effect: ev.effect || "read", verb: ev.name };
      noteAppended();
    } else if (ev.type === "phase") {
      if (curPipe) curPipe.applyPhase(ev);
      bumpChanges(); // notes land mid-run, not at tool_done
    } else if (ev.type === "tool_done") {
      bumpChanges(); // a write tool that emits no phases still changed the vault
      if (pipes[ev.id]) {
        pipes[ev.id].finish(ev.summary);
        if (curPipe === pipes[ev.id]) curPipe = null;
        delete pipes[ev.id];
      }
      const t = toolEls[ev.id];
      if (t) { t.section("out", ev.output); t.finish(ev.failed ? "error" : "done", ev.ms); }
      if (ev.memory || ev.memory_suggest) {
        const line = ev.memory ? memoryLine(ev.memory) : memorySuggestLine(ev.memory_suggest);
        if (t) t.el.after(line); else toolsGroup().appendChild(line);
      }
      toolsDone++;
      if (ev.found) found.push(...ev.found);
      const c = claimed[ev.id];
      if (c) {
        // A result that is an error landed nothing, as the replay of the same
        // turn already says (ev.failed is the server's _is_tool_failure).
        claimRefs({ notes: c.refs, effect: c.effect, error: !!ev.failed, name: c.verb,
                    refused: ev.refused }, touched, failed);
        delete claimed[ev.id];
      }
    } else if (ev.type === "tool_error") {
      if (pipes[ev.id]) {
        // No summary to read: the tool raised instead of returning a verdict, so
        // the block keeps the track open on whatever phase was in flight.
        pipes[ev.id].finish(null);
        if (curPipe === pipes[ev.id]) curPipe = null;
        delete pipes[ev.id];
      }
      const t = toolEls[ev.id];
      if (t) {
        t.setLabel(toolLabel(ev) + " · " + (ev.plain || ev.error));
        // The raw text moves out of a `title` and into the card: a tooltip on a
        // row that is already a disclosure is a second, worse copy of the same
        // affordance, and it cannot be selected or copied.
        t.section("error", { text: ev.error, cut: 0 });
        t.finish("error", ev.ms);
      }
      const f = claimed[ev.id];
      // Still not claimed as written — but now recorded as a mutation that did
      // NOT land, so the turn can say so in the footer instead of going quiet.
      if (f) claimRefs({ notes: f.refs, effect: f.effect, error: true, name: f.verb }, touched, failed);
      delete claimed[ev.id]; // it failed: do not claim its notes
    } else if (ev.type === "batch") {
      steps++;
      toolsGroup().appendChild(makeToolRow(ev.kind + " · " + ev.label).el);
      curTools.appendChild(caret);
    } else if (ev.type === "approval") {
      // A connector call waiting for a yes (ADR-0091 point 6); the card and
      // its answer live in app-connectors.js.
      toolsGroup().appendChild(approvalCard(ev));
      curTools.appendChild(caret);
    } else if (ev.type === "done") {
      flushPaint(); // the live segment is complete before anything supersedes it
      flagTurn = ev.turn || null;
      if (!ev.stopped) outcome = "success";
      if (ev.stopped) {
        // Stop is an outcome (P3): what streamed stays, and a dashed row under
        // it says who stopped it, after how long, and what had landed. It is
        // never the reply: "(silica: cancelled)" used to replace the answer.
        close("");
        // Drawn here only: a stopped turn has no answer to store them on.
        const notes = turnNotes(ev.notes);
        if (notes) ledger.appendChild(notes);
        const o = document.createElement("div");
        o.className = "turn-outcome";
        const secs = (performance.now() - turnT0) / 1000;
        // Not "the text above" over nothing: a preamble the stop cut short is
        // retracted, so often no word of this turn is left (ADR-0121 point 7).
        const arrived = texts.some((t) => t.raw.trim()) ? "the text above is what had arrived" : "nothing had been written yet";
        o.textContent = `stopped by you after ${fmtDur(secs)} · ${toolsDone} tool call${toolsDone === 1 ? "" : "s"} finished, ${arrived}`;
        if (retry) {
          const c = document.createElement("button");
          c.type = "button";
          c.className = "to-continue";
          c.textContent = "continue from here";
          c.title = "ask the same question again; what arrived so far stays in the conversation";
          c.addEventListener("click", () => { if (!streaming) retry(); });
          o.appendChild(c);
        }
        flow.appendChild(o);
        setCtxTokens(ev.context_tokens, ev.max_context_tokens, ev.context_parts, ev.compact_at);
        freezePeek();
        announce("stopped");
        followBottom();
        return;
      }
      if (ev.memory) {
        // /remember: the write is the whole answer, drawn as its line with
        // Undo rather than as a sentence saying the same thing.
        close("");
        flow.appendChild(memoryLine(ev.memory));
        setCtxTokens(ev.context_tokens, ev.max_context_tokens, ev.context_parts, ev.compact_at);
        announce("saved to memory");
        followBottom();
        return;
      }
      // Uninterrupted answer (no tool split the text) → upgrade the live md to the
      // canonical server render (wikilinks, callouts, mermaid). An interleaved
      // turn upgrades its last segment only when that segment is the answer the
      // server rendered (its opening matches), since the figures the server
      // places (ADR-0082) exist only in that render; 3 of 33 study turns were
      // split on 2026-10-08. Earlier segments keep their live render until reload.
      const tail = curText && curText.raw.trim().slice(0, 40);
      if (segments === 0 && (ev.html || ev.answer)) {
        const seg = textSeg();
        seg.raw = ev.answer || ""; // keep the copy button fed on no-delta turns
        seg.el.innerHTML = ev.html || escapeHtml(ev.answer || "");
      } else if (segments === 1 && curText && (ev.html || ev.answer)) {
        curText.el.innerHTML = ev.html || escapeHtml(ev.answer || "");
      } else if (tail && ev.html && (ev.answer || "").startsWith(tail)) {
        curText.el.innerHTML = ev.html;
      }
      close(""); // collapse any open thinking, end all segments
      drawDiagrams(body);
      const notes = turnNotes(ev.notes);
      if (notes) ledger.appendChild(notes);
      setCtxTokens(ev.context_tokens, ev.max_context_tokens, ev.context_parts, ev.compact_at);
      peekDone(ev); // card gets the canonical OFM render
      announce("response ready");
    } else if (ev.type === "error") {
      close("");
      outcome = "error";
      // A usage limit's reset arrives in UTC; app-limits.js writes it in the
      // reader's clock (ADR-0102 point 6).
      const said = typeof limitSentence === "function" ? limitSentence(ev) : ev.error;
      peekError(said);
      // The error belongs to the message that failed, not to a corner and not
      // to the agent: it used to render as a raw exception line attributed to
      // the agent as speech AND as a toast over the send button, and neither
      // offered a way to try again, so a failed turn ended the conversation
      // and took the typed question with it.
      attachFailure(lastUserBody, said, ev.detail || "", retry);
      announce("the turn failed: " + said);
    }
    followBottom();
  }
}

// The lines under an answer (ADR-0094 point 8): one muted block, one line per
// turn note, in the order the server already put them in (retrieval, numbers,
// graph). Informational only: a note arms nothing. Shared with the replay in
// app-boot.js, which reads the same notes off the stored answer.
function turnNotes(notes) {
  if (!notes || !notes.length) return null;
  const block = document.createElement("div");
  block.className = "turn-notes";
  for (const n of notes) {
    const line = document.createElement("div");
    line.className = "turn-note " + n.kind;
    line.textContent = n.text;
    block.appendChild(line);
  }
  return block;
}

// Read chips, unique by RESOLVED path: the live stream names a note by the
// tool's own argument, which is a title here and a path there, so the same
// note used to chip twice (`Teorema di Bayes` and `Matematica/…/Teorema di
// Bayes.md`). Resolve first, compare after, the house rule from the recall
// keyspace mismatch. Shared with the replay in app-boot.js so a reopened chat
// draws the footer the live one drew.
function resolveRef(ref) {
  const r = String(ref || "").trim();
  const idx = typeof noteIdx !== "undefined" ? noteIdx : [];
  if (!r || !idx.length) return r;
  const lr = r.toLowerCase();
  const stem = lr.replace(/\.md$/, "");
  const hit = idx.find((n) => n.lp === lr || n.lp.replace(/\.md$/, "") === stem);
  if (hit) return hit.path;
  const base = stem.split("/").pop();
  const same = idx.filter((n) => n.lp.replace(/\.md$/, "").split("/").pop() === base);
  return same.length === 1 ? same[0].path : r; // ambiguous: keep the argument as written
}

// The notes a turn read, unique by resolved path (see resolveRef above).
function uniqueReads(refs) {
  const byPath = new Set();
  for (const ref of refs) {
    const p = resolveRef(ref);
    if (p) byPath.add(p);
  }
  return [...byPath];
}

const noteLabel = (path) =>
  window.chipLabel ? window.chipLabel(path) : path.split("/").pop().replace(/\.md$/, "");

// --- the ledger line (ADR-0117 point 3) --------------------------------------
// Everything that describes how a turn ran, folded into one line at its head.
// The thinking and tool rows stay in the flow in arrival order, hidden by CSS
// until the line opens them in place, so the live stream and the replay keep
// building exactly what they built before. While the turn runs the line holds
// the run line's spans, which work.js fills from the narration beats.
function makeLedger(msg, live) {
  const led = document.createElement("div");
  led.className = "ledger";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ledger-toggle";
  btn.setAttribute("aria-expanded", "false");
  btn.innerHTML = '<span class="lg-caret" aria-hidden="true">▸</span><span class="lg-sum"></span>';
  if (live) {
    btn.querySelector(".lg-sum").innerHTML = '<span class="run-line" data-state="live">'
      + '<span class="rl-dot" aria-hidden="true"></span><span class="rl-verb">working</span>'
      + '<span class="rl-target"></span><span class="rl-elapsed"></span>'
      + '<span class="rl-pos"></span><span class="rl-stall"></span></span>';
  }
  btn.addEventListener("click", () => {
    const open = msg.classList.toggle("show-work");
    btn.setAttribute("aria-expanded", String(open));
  });
  led.appendChild(btn);
  return led;
}

// The line once the turn is over: what it read, how many steps, how long, and
// by name the notes it read without citing them, which are still where the
// answer may have come from. A turn with nothing folded and nothing to say
// loses the line.
function finishLedger(msg, led, { steps, secs, reads, cited, found = [] }) {
  const hasWork = !!msg.querySelector(".body .thinking, .body .tools");
  // An answer that read no note still stood on what its searches returned
  // (ADR-0121 point 7): those notes, at most six, are its sources then.
  const fromSearch = reads.length ? [] : [...new Set(found)].filter((p) => !cited.has(p)).slice(0, 6);
  const uncited = fromSearch.length ? fromSearch : reads.filter((p) => !cited.has(p));
  const notes = led.querySelector(".turn-notes");
  const parts = [];
  if (reads.length) parts.push(`read ${reads.length} note${reads.length === 1 ? "" : "s"}`);
  if (steps) parts.push(`${steps} step${steps === 1 ? "" : "s"}`);
  if (!parts.length && msg.querySelector(".body .thinking")) parts.push("thought");
  if (parts.length && secs >= 0.05) parts.push(fmtDur(secs));
  if (!parts.length && !uncited.length && !notes) {
    const copy = led.querySelector(".copy-btn"); // a replayed turn put it here first
    if (copy) led.parentElement.appendChild(copy);
    led.remove();
    return;
  }
  const toggle = led.querySelector(".ledger-toggle");
  toggle.querySelector(".lg-sum").textContent = parts.join(" · ");
  toggle.hidden = !parts.length;
  // Nothing folded, nothing to open: the line stays a statement.
  toggle.disabled = !hasWork;
  if (!uncited.length) return;
  const r = document.createElement("span");
  r.className = "lg-reads";
  // Beside numbered citations, a name here needs saying why it is not one.
  if (fromSearch.length) r.appendChild(mkEl("span", "lg-label", "found by search"));
  else if (cited.size) r.appendChild(mkEl("span", "lg-label", "not cited"));
  for (const p of uncited) {
    const c = document.createElement("button");
    c.type = "button";
    c.className = "note-link";
    c.dataset.path = p; // delegated click (app-notes.js) opens the drawer
    c.textContent = noteLabel(p);
    r.appendChild(c);
  }
  toggle.after(r);
}

// One end for the live turn and the replayed one: number the citations, then
// state the line with the reads the answer did not cite.
function finishTurn(body, ledger, touched, meta) {
  const cited = citeTurn(body);
  const reads = uniqueReads([...touched].filter(([, e]) => e === "read").map(([r]) => r));
  finishLedger(body.closest(".msg"), ledger, { ...meta, reads, cited });
}

// --- citations (ADR-0117 point 2, amended 2026-10-16) -------------------------
// Each note the answer links gets a number after the link, in order of first
// use. The margin of cards beside them is gone at the owner's call: a card per
// linked note repeated the link it stood next to. A citation opens its note in
// the drawer at the line that says what the answer took from it (openCite).
function citeTurn(body) {
  const marks = body.querySelectorAll(".cite-mark");
  if (marks.length) return new Set([...marks].map((m) => m.dataset.path)); // already numbered
  const order = [];
  for (const a of body.querySelectorAll(".stream-text .note-link[data-path]")) {
    const p = resolveRef(a.dataset.path);
    let n = order.indexOf(p) + 1;
    if (!n) { order.push(p); n = order.length; }
    a.dataset.cite = n;
    const m = document.createElement("sup");
    m.className = "cite-mark";
    m.dataset.cite = n;
    m.dataset.path = p; // the delegated click (app-notes.js) hands it to openCite
    m.title = "open " + noteLabel(p);
    m.textContent = n;
    a.after(m);
  }
  return new Set(order);
}

// The words of every paragraph that cites note n, minus the link's own.
function citeWordsFor(msg, n) {
  const words = new Set();
  for (const a of msg.querySelectorAll(`.stream-text .note-link[data-cite="${n}"]`)) {
    const block = a.closest("li, td, th, .stream-text > *");
    let text = block ? block.textContent : "";
    // "Label (Note):" heads the list that holds what it says about the note
    if (/:\s*$/.test(text) && block.nextElementSibling) text += " " + block.nextElementSibling.textContent;
    const own = citeWords(a.textContent);
    for (const w of citeWords(text)) if (!own.has(w)) words.add(w);
  }
  return words;
}

// Opens the cited note and marks its line closest to the citing paragraphs.
// Read off the editor once the note has loaded, not off a fetch of our own,
// so the line number is one of the document on screen. No line shares enough
// with the answer: the note opens at its top and nothing is marked.
async function openCite(el) {
  const msg = el.closest(".msg");
  const words = msg ? citeWordsFor(msg, el.dataset.cite) : new Set();
  if (!(await openNote(el.dataset.path)) || !editor) return;
  const hit = bestLine(proseLines(editor.getText()), words);
  if (hit) editor.markLine(hit.line);
}

// cite-pure:start (run under node by tests/test_cite_cards.py)
// ponytail: words of four letters or more, minus the commonest English and
// Italian ones, and a line needs two in common with the citing paragraph. An
// answer in one language over notes in another shares mostly names, symbols
// and numbers, so most of those citations open the note with no line marked.
// The upgrade is the passage the turn's own read returned: the server keeps
// every tool result with the session (server.py, the replay's `results`), but
// the page gets 1200 characters of a live one and nothing of a stored one, so
// the join belongs on the server. Review at the pilot report on 2027-03-01.
const CITE_STOP = new Set(("that this with from have your what when which their there these those they them then than "
  + "also into only some more most such each been were will would could should about between because "
  + "della delle dello degli nella nelle sono come anche questo questa quella quello perché dove quando "
  + "essere viene molto tutti tutte ogni loro").split(" "));
function citeWords(text) {
  const out = new Set();
  for (const w of (text || "").toLowerCase().match(/[\p{L}\p{N}]{4,}/gu) || []) if (!CITE_STOP.has(w)) out.add(w);
  return out;
}
function bestLine(lines, words) {
  let best = null, top = 1;
  for (const l of lines) {
    let k = 0;
    for (const w of citeWords(l.text)) if (words.has(w)) k++;
    if (k > top) { top = k; best = l; }
  }
  return best;
}
// A note's prose as sentences, each with the 1-based line of the raw file it
// sits on (the editor's document): no frontmatter, fences, headings, tables or
// comments, and the link and emphasis markup read as the words it shows.
function proseLines(md) {
  const rows = md.split("\n");
  const out = [];
  let i = 0;
  if (rows[0].trimEnd() === "---") {
    const end = rows.findIndex((r, k) => k > 0 && r.trimEnd() === "---");
    if (end > 0) i = end + 1;
  }
  let fence = false, comment = false;
  for (; i < rows.length; i++) {
    let l = rows[i].trim();
    if (l.startsWith("```")) { fence = !fence; continue; }
    if (fence) continue;
    if (comment) { comment = !l.includes("-->"); continue; }
    l = l.replace(/<!--.*?-->/g, "");
    if (l.includes("<!--")) { comment = true; continue; }
    // A quote's or a callout's body is the note's own prose; a callout's head
    // is only its type.
    l = l.replace(/^(>\s*)+/, "");
    if (!l || /^(#|\||!\[|\$\$|\[!)/.test(l)) continue;
    const plain = l.replace(/^([-*+]|\d+\.)\s+/, "")
      .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2").replace(/\[\[([^\]]+)\]\]/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[*_`=]+/g, "");
    for (const sent of plain.split(/(?<=[.!?;])\s+/)) {
      if (sent.length >= 40 && sent.length <= 240) out.push({ text: sent, line: i + 1 });
    }
  }
  return out;
}
// cite-pure:end

// Pointing at a citation tints every paragraph of the answer that cites the
// same note; focus does the same for the keyboard.
let citeHotKey = "";
function setCiteHot(target) {
  const hit = target && target.closest && target.closest(".cite-mark, .stream-text .note-link[data-cite]");
  const msg = hit && hit.closest(".msg");
  const key = msg ? `${[...log.children].indexOf(msg)}:${hit.dataset.cite}` : "";
  if (key === citeHotKey) return;
  citeHotKey = key;
  for (const el of log.querySelectorAll(".cite-hot")) el.classList.remove("cite-hot");
  if (!msg) return;
  for (const a of msg.querySelectorAll(`.stream-text .note-link[data-cite="${hit.dataset.cite}"]`)) {
    a.closest("li, td, th, .stream-text > *")?.classList.add("cite-hot");
  }
}
log.addEventListener("pointerover", (e) => setCiteHot(e.target));
log.addEventListener("pointerleave", () => setCiteHot(null));
log.addEventListener("focusin", (e) => setCiteHot(e.target));

// The images a user turn carried, under its words: live from the data URLs the
// composer read, on reload from /attachment (ADR-0077).
function addTurnImages(body, images) {
  for (const im of images) {
    const el = document.createElement("img");
    el.className = "turn-image";
    el.src = im.src;
    el.alt = im.name;
    body.appendChild(el);
  }
}

function send(text, replay = false, images = []) {
  if ((!text.trim() && !images.length) || streaming) return;
  // A retry re-runs the turn but must not stack a second copy of your question
  // in the transcript: the bubble from the first attempt is still there.
  if (!replay) {
    const body = bubble("user");
    body.textContent = text;
    body.dataset.words = text;
    addTurnImages(body, images.map((im) => ({ src: im.data, name: im.name })));
  }
  const find = text.trim().match(/^\/find\s*(.*)$/);
  if (find && !images.length) { runFind(find[1]); return; }
  runTurn(fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, ...(images.length ? { images } : {}),
                           ...(chatWorkspace ? { workspace: chatWorkspace } : {}) }),
  }), "working", () => send(text, true, images));
}

// --- the last turn asked again (ADR-0121 point 7) -----------------------------
// "again" on the last answer, "edit" on the last question: both send the
// question back through POST /turn/redo, which takes the old exchange out of
// the conversation first. The last turn only, so only it carries the buttons.
let editing = false;

function markLastTurn() {
  for (const b of log.querySelectorAll(".redo-btn, .edit-btn")) b.remove();
  const users = log.querySelectorAll(".msg.user");
  const user = users[users.length - 1];
  const words = user?.querySelector(".body")?.dataset.words ?? "";
  // A direct command is answered without the model, /find never reaches the
  // server's history, and a drop still being read has no turn there yet.
  if (!user || words.trim().startsWith("/") || user.querySelector(".body[data-drop]")) return;
  const answer = user.nextElementSibling;
  if (answer?.classList.contains("silica")) {
    const again = document.createElement("button");
    again.type = "button";
    again.className = "copy-btn redo-btn";
    again.textContent = "again";
    again.title = "ask the same question again; this answer leaves the conversation";
    again.addEventListener("click", () => redoLast(null));
    const body = answer.querySelector(".body");
    (body.querySelector(":scope > .ledger") || body).appendChild(again);
  }
  if (words.trim()) {
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "copy-btn edit-btn";
    edit.textContent = "edit";
    edit.title = "change this question; sending it replaces the question and its answer";
    edit.addEventListener("click", () => {
      if (streaming) return;
      editing = true;
      input.value = words;
      autoGrow(input);
      input.focus();
      input.setSelectionRange(words.length, words.length);
      user.classList.add("editing");
      notify("editing your last question: send replaces it, Esc keeps it", "info");
    });
    user.querySelector(".body").after(edit);
  }
}

function endEditing() {
  editing = false;
  for (const el of log.querySelectorAll(".msg.user.editing")) el.classList.remove("editing");
}

function redoLast(words) {
  if (streaming) return;
  const users = log.querySelectorAll(".msg.user");
  const user = users[users.length - 1];
  if (!user) return;
  endEditing();
  // The old answer leaves the page as it leaves the conversation; an edited
  // question gets a new bubble, the files it carried going with it.
  while (user.nextElementSibling) user.nextElementSibling.remove();
  let body = user.querySelector(".body");
  if (words !== null) {
    const files = user.querySelector(":scope > .msg-files");
    user.remove();
    body = bubble("user");
    body.textContent = words;
    body.dataset.words = words;
    if (files) body.after(files);
  } else {
    lastUserBody = body;
  }
  const go = () => runTurn(fetch("/turn/redo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(words === null ? {} : { text: words }),
  }), "working", go);
  go();
}

// /find bypasses the agent entirely — same "direct tool, no LLM" pattern as
// the /graph and /map tabs, just rendered inline as a result bubble.
async function runFind(rest) {
  const body = bubble("silica");
  // dock-launched /find: mirror the result bubble into the card (no SSE stream
  // here, so the peek would otherwise sit at "thinking" forever)
  const mirror = () => { if (peek) { peek.body.innerHTML = body.innerHTML; freezePeek(); } };
  let k = 5;
  const tokens = [];
  for (const part of rest.trim().split(/\s+/)) {
    const m = part.match(/^--k=(\d+)$/);
    if (m) k = parseInt(m[1], 10);
    else if (part) tokens.push(part);
  }
  const query = tokens.join(" ");
  if (!query) { body.textContent = "usage: /find <query> [--k=N]"; mirror(); return; }
  body.textContent = "searching…";
  try {
    const r = await fetch("/find?q=" + encodeURIComponent(query) + "&k=" + k);
    body.innerHTML = await r.text();
  } catch (e) {
    body.textContent = "error: " + e;
  }
  mirror();
}

// --- tool rows --------------------------------------------------------------
// A tool used to be one line that said `read Etica.md` and stopped. That is the
// verb and the object; what it cost and what came back were nowhere, so every
// step of a turn had to be taken on trust. The row now carries its own duration
// and opens onto the call it made and the answer it got — the two facts that
// separate "the agent read the note" from "the agent says it read the note".
//
// Server-measured duration, not a client timer started when the event painted:
// an SSE frame lands a frame late and a backgrounded tab stops scheduling
// altogether, so a clock on this side reports the transport as the tool's cost.
function fmtMs(ms) {
  if (!(ms >= 0)) return "";
  // A sub-millisecond call printed as "0ms" reads as "not measured", which is
  // the opposite of what it means: the tool was a cache hit.
  if (ms < 1) return "<1ms";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + "s";
}

function makeToolRow(label) {
  const el = document.createElement("div");
  el.className = "tool";
  el.dataset.state = "running";
  const row = document.createElement("button");
  row.type = "button";
  row.className = "tool-row";
  // Inert until it has a card. A transcript replay carries no results, so a
  // reloaded chat would otherwise put six focusable buttons in the tab order
  // that do nothing when activated — and a keyboard user has no hover to tell
  // them apart from the rows that open.
  row.disabled = true;
  row.innerHTML = '<span class="tool-mark" aria-hidden="true">\u00bb</span>'
    + '<span class="tool-label"></span><span class="tool-ms"></span>'
    + '<span class="tool-chev" aria-hidden="true">\u25b8</span>';
  const card = document.createElement("div");
  card.className = "tool-card";
  card.hidden = true;
  el.append(row, card);
  const labelEl = row.querySelector(".tool-label");
  labelEl.textContent = label;

  // Only a row with something to open is a control. The rest keeps the same
  // geometry and stays inert, so a group of tools does not read as a row of
  // buttons half of which do nothing.
  let sections = 0;
  function section(kind, payload) {
    if (!payload || !payload.text) return;
    if (sections) card.appendChild(mkEl("div", "tc-div"));
    const sec = mkEl("div", "tc-sec");
    sec.appendChild(mkEl("span", "tc-k", kind));
    // A div, not a <pre>: `:is(.msg, …) pre` gives every pre in a turn the code
    // well's own surface, and this text already sits inside one. `white-space`
    // below is what <pre> was ever here for, and .tc-text sets it.
    const pre = mkEl("div", "tc-text", payload.text);
    if (payload.cut) {
      // Named, not elided: a result that genuinely ends in an ellipsis and one
      // that was cut look identical once you print "…" and say nothing.
      pre.appendChild(mkEl("span", "tc-cut", `\u2026 ${payload.cut} more characters`));
    }
    sec.appendChild(pre);
    card.appendChild(sec);
    sections++;
    el.dataset.open = "shut";
    row.disabled = false;
    row.setAttribute("aria-expanded", "false");
  }
  row.addEventListener("click", () => {
    if (!sections) return;
    const opening = card.hidden;
    card.hidden = !opening;
    el.dataset.open = opening ? "open" : "shut";
    row.setAttribute("aria-expanded", opening ? "true" : "false");
  });

  return {
    el,
    section,
    setLabel(text) { labelEl.textContent = text; },
    finish(state, ms) {
      el.dataset.state = state;                          // done | error
      row.querySelector(".tool-mark").textContent = state === "error" ? "\u2717" : "\u2713";
      const t = fmtMs(ms);
      if (t) row.querySelector(".tool-ms").textContent = t;
    },
  };
}

// --- composer ---------------------------------------------------------------
function autoGrow(el) {
  el.style.height = "auto";
  const border = el.offsetHeight - el.clientHeight; // box-sizing: border-box
  el.style.height = (el.scrollHeight + border) + "px"; // clamped visually by CSS max-height
}
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = input.value;
  // Files ride the turn they were attached to and cannot wait in a text
  // queue, so an attach during a turn keeps the box as it is.
  if (streaming && staged.length) return;
  input.value = "";
  autoGrow(input);
  renderCommands(input.value); // clearing by hand fires no `input` event — dismiss the picker
  renderHighlight();           // …and clear the bands with it
  clearDraft();                // sent or queued, it is no longer a draft
  if (streaming) { queueMessage(t); syncSend(); return; } // the field is empty again: the button is stop
  if (editing && !staged.length && t.trim() && !t.trim().startsWith("/")) { redoLast(t.trim()); return; }
  endEditing();
  // Images alone ride the turn as pixels the model sees (ADR-0077); anything
  // else, or an explicit /nucleate, is uploaded and staged into the vault.
  if (staged.length && stagedAreImages() && !t.trim().startsWith("/")) lookStaged(t);
  else if (staged.length) nucleateStaged(t); // files attached: upload + act on them together
  else send(t);
});
let allCommands = [];
let filteredCommands = [];
let cmdSelIdx = -1;

// Read again after Settings installs or removes a skill (ADR-0097), so the
// picker changes without a reload.
function loadCommands() {
  return fetch("/commands").then(r => r.json()).then(data => allCommands = data || []).catch(() => {});
}
loadCommands();

// --- @ names a note (ADR-0097 point 1) ----------------------------------------
// The link the caret is writing: `@query` at the start of a word, or `[[query`
// anywhere. `name@host` is not a trigger, so an address stays text, and a
// query that opens with a space is not one either (`@ 3pm`). The pick is
// written as a wikilink: an `@name` has no closing mark, so nothing could say
// where a title with spaces ends, and the mirror already bands `[[...]]`.
let mention = null;

function mentionAtCaret() {
  if (input.selectionStart !== input.selectionEnd) return null;
  const upto = input.value.slice(0, input.selectionStart);
  const m = /(?:^|[\s(\[])(@)([^@\[\]\n]{0,64})$/.exec(upto) || /(\[\[)([^\]\[\n|#]{0,64})$/.exec(upto);
  if (!m || /^\s/.test(m[2])) return null;
  return { start: upto.length - m[1].length - m[2].length, end: upto.length, query: m[2], brackets: m[1] === "[[" };
}

// The explore search's own index and ranking (app-notes.js), top 8. True when
// the list is showing notes; false hands the box back to the command picker.
function renderMentions() {
  mention = mentionAtCaret();
  if (!mention || typeof noteIdx === "undefined" || !noteIdx.length) return false;
  const q = mention.query.trim().toLowerCase();
  const hits = (q
    ? noteIdx.map((n) => [scoreNote(n, q), n]).filter((p) => p[0] > 0)
        .sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name)).map((p) => p[1])
    : [...noteIdx].sort((a, b) => a.name.localeCompare(b.name))).slice(0, 8);
  if (!hits.length) { mention = null; return false; }
  const box = $("#commands");
  filteredCommands = hits;
  cmdSelIdx = 0;
  box.innerHTML = "";
  hits.forEach((n, i) => {
    const el = document.createElement("button");
    el.className = "cmd-item" + (i === cmdSelIdx ? " sel" : "");
    el.type = "button";
    const folder = n.path.includes("/") ? n.path.slice(0, n.path.lastIndexOf("/")) : "";
    el.innerHTML = `<span class="cmd-note">${escapeHtml(n.name)}</span><span class="cmd-summary">${escapeHtml(folder)}</span>`;
    el.title = n.path;
    el.addEventListener("mousedown", (e) => e.preventDefault()); // the caret stays in the field
    el.addEventListener("click", () => pickMention(n));
    box.appendChild(el);
  });
  box.hidden = false;
  return true;
}

// The basename when no other note has it, else the path: Obsidian's "shortest
// path when possible", and what the server resolves the same way.
function pickMention(n) {
  const m = mention;
  if (!m) return;
  const stem = (p) => p.replace(/\.md$/i, "").split("/").pop().toLowerCase();
  const unique = noteIdx.filter((o) => stem(o.path) === stem(n.path)).length === 1;
  const link = unique ? n.path.replace(/\.md$/i, "").split("/").pop() : n.path.replace(/\.md$/i, "");
  let after = input.value.slice(m.end);
  if (m.brackets && after.startsWith("]]")) after = after.slice(2);
  const ins = `[[${link}]] `;
  input.value = input.value.slice(0, m.start) + ins + after.replace(/^ /, "");
  const caret = m.start + ins.length;
  input.setSelectionRange(caret, caret);
  input.focus();
  mention = null;
  autoGrow(input);
  renderCommands(input.value);
  renderHighlight();
}

function renderCommands(q) {
  const box = $("#commands");
  if (renderMentions()) return;
  if (!q.startsWith("/")) {
    box.hidden = true;
    return;
  }
  const search = q.substring(1).toLowerCase();
  
  filteredCommands = allCommands.map(cmd => {
    let score = 0;
    const name = cmd.name.substring(1).toLowerCase();
    if (name === search) score = 10;
    else if (name.startsWith(search)) score = 5;
    else if (name.includes(search)) score = 3;
    else {
      let i = 0;
      let matched = true;
      for (const c of search) {
        i = name.indexOf(c, i);
        if (i === -1) { matched = false; break; }
        i++;
      }
      if (matched && search.length > 0) score = 1;
    }
    return { cmd, score };
  }).filter(x => x.score > 0).sort((a, b) => b.score - a.score || a.cmd.name.localeCompare(b.cmd.name)).map(x => x.cmd);

  if (!filteredCommands.length) {
    box.hidden = true;
    return;
  }
  
  cmdSelIdx = 0;
  box.innerHTML = "";
  filteredCommands.forEach((c, i) => {
    const el = document.createElement("button");
    el.className = "cmd-item" + (i === cmdSelIdx ? " sel" : "");
    el.type = "button";
    el.innerHTML = `<span class="cmd-name">${c.name}</span><span class="cmd-summary">${escapeHtml(c.usage ? c.usage + " · " + c.summary : c.summary)}</span>`;
    el.title = c.usage ? c.usage + " · " + c.summary : c.summary;
    el.addEventListener("click", () => pickCommand(c));
    box.appendChild(el);
  });
  box.hidden = false;
}

function updateCmdSel() {
  const box = $("#commands");
  Array.from(box.children).forEach((el, i) => {
    el.classList.toggle("sel", i === cmdSelIdx);
    if (i === cmdSelIdx) el.scrollIntoView({ block: "nearest" });
  });
}

function pickCommand(c) {
  if (c.path !== undefined) return pickMention(c); // the list is holding notes
  input.value = c.name + (c.usage ? " " : "");
  input.focus();
  renderCommands(input.value);
  renderHighlight();
}

// --- the composer mirror ----------------------------------------------------
// A layer exactly the size of the field, behind it, drawing what the box has
// understood: a band under the leading command, under every [[wikilink]] and
// under every path, plus the greyed rest of a command you have started typing.
//
// It draws BACKGROUNDS and never glyphs. The textarea keeps its own opaque
// text, caret and selection on top, so the two layers can only ever disagree by
// a band sitting a pixel off — where the usual mirror trick (transparent
// textarea, glyphs in the backdrop) disagrees by showing the wrong characters,
// which is what makes it flicker on IME input and lag a frame behind a wheel
// scroll. The cost of the safe version is that the tokens are tinted rather
// than coloured; the benefit is that nothing here can ever ghost.
const inputHl = $("#input-hl");
// [[wikilink]] first, so a link is never re-matched as a path by its own text.
const HL_TOKENS = [
  [/\[\[[^\][]+\]\]/g, "link"],
  [/(?:^|\s)((?:[\w.\-]+\/)+[\w.\-]+\.md)/g, "path"],
];

function renderHighlight() {
  const raw = input.value;
  if (!raw) { inputHl.textContent = ""; inputHl.scrollTop = 0; return; }
  // Marks by offset, then one pass: overlapping patterns would otherwise nest
  // spans inside each other and the second band would paint over the first.
  const marks = [];
  const cmd = /^\/[a-z-]+/.exec(raw);
  if (cmd) marks.push([0, cmd[0].length, "cmd"]);
  for (const [re, cls] of HL_TOKENS) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(raw))) {
      const hit = m[1] !== undefined ? m[1] : m[0];
      const at = m.index + m[0].indexOf(hit);
      if (!marks.some(([a, b]) => at < b && at + hit.length > a))
        marks.push([at, at + hit.length, cls]);
    }
  }
  marks.sort((a, b) => a[0] - b[0]);
  let out = "";
  let at = 0;
  for (const [start, end, cls] of marks) {
    out += escapeHtml(raw.slice(at, start));
    out += `<span class="hl-${cls}">${escapeHtml(raw.slice(start, end))}</span>`;
    at = end;
  }
  out += escapeHtml(raw.slice(at));
  // The ghost completes the command you are typing, and only while exactly one
  // command can still be meant: offering the first of five is a guess wearing
  // the clothes of an answer, and the picker below already lists all five.
  if (cmd && cmd[0].length === raw.length) {
    const hits = allCommands.filter((c) => c.name.startsWith(raw) && c.name !== raw);
    if (hits.length === 1) out += `<span class="hl-ghost">${escapeHtml(hits[0].name.slice(raw.length))}</span>`;
  }
  inputHl.innerHTML = out;
  inputHl.scrollTop = input.scrollTop;
}

input.addEventListener("input", () => {
  autoGrow(input);
  renderCommands(input.value);
  renderHighlight();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(saveDraft, DRAFT_MS);
});
// Past the 40vh cap the field scrolls, and a band that stays put while its word
// leaves the viewport is worse than no band at all.
input.addEventListener("scroll", () => { inputHl.scrollTop = input.scrollTop; });

input.addEventListener("keydown", (e) => {
  const box = $("#commands");
  if (!box.hidden && filteredCommands.length > 0) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      cmdSelIdx = (cmdSelIdx + 1) % filteredCommands.length;
      updateCmdSel();
      return;
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      cmdSelIdx = (cmdSelIdx - 1 + filteredCommands.length) % filteredCommands.length;
      updateCmdSel();
      return;
    } else if ((e.key === "Enter" || e.key === "Tab") && !e.shiftKey) {
      e.preventDefault();
      if (cmdSelIdx >= 0 && cmdSelIdx < filteredCommands.length) {
        pickCommand(filteredCommands[cmdSelIdx]);
      }
      return;
    } else if (e.key === "Escape") {
      e.preventDefault();
      box.hidden = true;
      return;
    }
  }

  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#composer").requestSubmit();
    box.hidden = true;
  } else if (e.key === "Escape" && editing) {
    e.preventDefault();
    input.value = "";
    autoGrow(input);
    endEditing();
  }
});

// --- dock composer (graph/map) — same conversation, mirrored into the card ---
// The turn is a real chat turn (user bubble + transcript land in the chat tab);
// the dock card is a lens showing only the latest exchange.
const dockInput = $("#dock-input");
$("#dock-composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = dockInput.value;
  if (!t.trim()) return;
  dockInput.value = "";
  autoGrow(dockInput);
  if (streaming) { queueMessage(t); return; } // the same queue as the chat composer (P20)
  openPeek(t.trim());
  send(t);
});

dockInput.addEventListener("input", () => autoGrow(dockInput));
// At rest the dock is the composer folded into its stone (app-panels.css). A tap
// where there is no hover to unfold it lands on the glass, not the field, so the
// glass hands the focus on and :focus-within opens it; the stone itself takes
// no pointer events, so its clicks land here too.
$("#dock-composer .composer-shell").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) dockInput.focus();
});
dockInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#dock-composer").requestSubmit();
  }
});
// Stop disables itself on the click and re-enables only if the cancel has not
// landed within 2s, reading "still stopping": a button that stays pressable
// invites a second click that cannot do more than the first, and one that
// stays dead through a slow cancel reads as a control that did nothing.
const STOP_GRACE_MS = 2000;
function requestStop() {
  const send = $("#send");
  for (const b of stopBtns()) { b.disabled = true; b.textContent = "stopping…"; }
  send.disabled = true;
  send.title = "stopping…";
  fetch("/stop", { method: "POST" }).catch(() => { /* the finally below re-arms */ });
  setTimeout(() => {
    if (!streaming) return;
    for (const b of stopBtns()) { b.disabled = false; b.textContent = "still stopping"; }
    send.disabled = false;
    send.title = "still stopping";
  }, STOP_GRACE_MS);
}
document.addEventListener("click", (e) => {
  if (e.target.closest(".rl-stop")) requestStop();
});
// A click, not the form's submit: Enter on an empty field mid-turn submits too,
// and must queue nothing rather than stop the turn.
$("#send").addEventListener("click", (e) => {
  if ($("#send").dataset.mode !== "stop") return;
  e.preventDefault();
  requestStop();
});

// The wordmark goes home (the chat view); "+ new" in the Chats compartment
// starts a chat (audit P16). Optimistic: clear the transcript at once (the
// reset itself is a cached-seed copy server-side, but never make the click
// wait on the network).
$("#brand-logo").addEventListener("click", () => showTab("chat"));
async function newChat() {
  if (streaming) return;
  saveDraft();
  input.value = "";
  autoGrow(input);
  renderHighlight();
  log.innerHTML = "";
  await fetch("/reset", { method: "POST" });
  currentSessionId = "";
  chatWorkspace = "";       // a new chat is the whole vault until a workspace opens it
  renderWsChip();
  activeTab = "chat";
  syncUrl(true);   // a new chat is a place Back can return from (P7)
  showTab("chat"); // surface the fresh chat
  announceSession();
  loadVault();
  loadSessions();
  loadChanges();
  input.focus();
}
$("#new-chat").addEventListener("click", newChat);
$("#rail-new").addEventListener("click", newChat); // the same row, folded into the rail

