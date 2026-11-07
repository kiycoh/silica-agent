// What every script on this page shares, loaded first: 22 of them read `$`, 14
// `notify`, and each of these used to live in whichever view wrote it first, or
// in a copy. The list in index.html only has to hold what really is ordered.
// Nothing here reads a name from another script (tests/test_gui_globals.py), so
// this file changes least and anything may depend on it. Nothing here touches
// the DOM while it loads either, which is what lets the tests that run a block
// of a script under node put this file in front of it.
//
// Two surfaces keep their own small copies on purpose, because neither can load
// this file: graph-frame.js stays self-contained for file:// (ADR-0026), and
// pages/pages.js ships alone to the public host's /_s/.

const $ = (s) => document.querySelector(s);

// Setting textContent to null and leaving it unset read the same on a new
// element, so the copies that wrote null and the copies that skipped it never
// differed on screen (checked in Chrome 2026-11-06).
const mkEl = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text; // labels are vault data — never innerHTML
  return n;
};

// Text between tags, where a quote needs nothing. null and undefined are
// nothing, as they were when this round-tripped through a div's textContent.
const escapeHtml = (s) => (s == null ? "" : String(s)).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
// Text inside a double-quoted attribute. mdLite does not use this: what it
// quotes is already escaped, and a second pass would print `&amp;amp;`.
const escapeAttr = (s) => escapeHtml(s).replace(/"/g, "&quot;");

// `.md` as the vault writes it, case and all: the walker takes `Note.md` and not
// `Note.MD`, and Python's removesuffix(".md") reads the same.
const noExt = (p) => String(p || "").replace(/\.md$/, "");
const noteStem = (p) => noExt(p).split("/").pop();

// What opens a link while it is being typed, in the composer and in the editor.
const LINK_OPENING = /(\[\[)([^\][\n|#]{0,64})$/;

// Seconds, the way a run reports them. silica/ui/renderer.py prints the same rule.
const fmtDur = (s) =>
  s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${String(Math.floor(s % 60)).padStart(2, "0")}s`;

// en-US and en-GB write a number alike. The Metrics tab's nfmt and Settings'
// vxDate follow the browser's locale instead, which is a difference a reader can
// see, so they stay apart.
const fmtNum = (n) => Number(n).toLocaleString("en-US");
// "Nov 6". Public and limits rows write "6 Nov" (en-GB) and stay so.
const fmtDay = (d, opts) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...opts });
// Calendar days back from today on the reader's clock; 0 is today.
function daysAgo(ts) {
  const midnight = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  return Math.round((midnight(new Date()) - midnight(new Date(ts * 1000))) / 86400000);
}

// --- talking to the server ---------------------------------------------------
// failureSaid is the one reading of a failure's body, whatever the status it came
// with: `error` is what the person reads (failures.py), and `detail` alone is a
// body from outside that shape, a guard's. FastAPI's 422 holds a list there, which
// is no sentence. postJson is the request and nothing else, for a caller that reads
// the status itself; answerOf is the one rule for what a refusal is: it throws the
// server's words, with the status and the body for whoever must branch on them.
const failureSaid = (data, fallback = "") => {
  const said = data && (data.error || data.detail);
  return typeof said === "string" && said ? said : fallback;
};
const postJson = (url, body) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
});
async function answerOf(r) {
  const data = await r.json().catch(() => ({})); // an empty or HTML body: the status still says what happened
  if (!r.ok) throw Object.assign(new Error(failureSaid(data, `HTTP ${r.status}`)), { status: r.status, data });
  return data;
}
const api = async (url, init) => answerOf(await fetch(url, init));
const apiPost = async (url, body) => answerOf(await postJson(url, body));

// --- notifications + screen-reader status ------------------------------------
// A hairline toast strip fills the silent catch(){} gaps: a failed background
// fetch now says so instead of leaving a stale "—". Two levels only (info =
// accent, error = gold/caution) — the palette reserves no third UI signal.
// Every notify() also lands in the polite SR region, so the streaming
// transcript itself needn't be a chatty live region.
function announce(msg) { const sr = $("#sr-status"); if (sr) sr.textContent = msg; }
// Cap the visible stack. Two diagnostics fire on every load and an error turn
// stacked five, ~300px of grey boxes sitting on top of #send — so whatever you
// had just done, the screen closed on a debug message covering the primary
// control. Older ones roll up behind a "+N" the user can expand.
const TOAST_MAX = 2;

function rollUpToasts() {
  const toasts = $("#toasts");
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

// `action` ({label, run}) puts one button on the toast, the way back from what
// it reports (a tree delete's undo, ADR-0123). Such a toast is never deduped,
// since its button belongs to one event, and it stays longer: the reader has
// to find the button, not only read the line.
function notify(msg, level = "error", action = null) {
  announce(msg);
  const toasts = $("#toasts");
  if (!action && [...toasts.querySelectorAll(".toast")].some((t) => t.textContent === msg)) return; // dedupe visible
  const t = document.createElement("div");
  t.className = "toast " + level;
  t.textContent = msg;
  t.title = msg; // CSS clamps the box to 3 lines; the full text stays reachable
  const kill = () => { t.remove(); rollUpToasts(); };
  if (action) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "toast-act";
    b.textContent = action.label;
    b.addEventListener("click", (e) => { e.stopPropagation(); kill(); action.run(); });
    t.appendChild(b);
  }
  t.addEventListener("click", kill);
  toasts.appendChild(t);
  setTimeout(kill, action ? 10000 : level === "error" ? 6000 : 3000);
  rollUpToasts();
}
