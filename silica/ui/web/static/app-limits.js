// --- usage limits (ADR-0102 points 6 and 7) ------------------------------------
// How much of each limit is used and when it resets, from GET /limits: a bar
// per layer at the drawer's foot, and from 80% of any of them a line above the
// composer. A share and never a currency: the bar moves with what the student
// did, where a price would move with the model. The server sends instants in
// UTC and this file writes them in the reader's own clock, including in the
// refusal sentence a turn's error frame carries (limitSentence below). Where
// the key has no limit, /limits is empty and nothing here draws.

const LIM_NAMES = { window: "5-hour window", week: "This week", month: "This month" };
const LIM_PHRASE = { window: "this 5-hour window", week: "this week's allowance", month: "this month's allowance" };
const LIM_WARN = 0.8;
// setTimeout fires at once past 2^31 ms (24.8 days), and a month's reset can
// be 31 days out: the refresh waits for the soonest reset, never past 6 hours.
const LIM_REFRESH_CAP_MS = 6 * 3600 * 1000;
let limTimer = 0;

// The reset in words, in the reader's zone: "at 14:20" for the window, which
// is never more than 5 hours away, "on Monday at 02:00" for the week, "on 1
// November at 01:00" for the month. en-GB for the names and the 24-hour clock,
// as the rest of the copy is English and the ADR's sentences are written so.
function limWhen(layer, iso) {
  const at = new Date(iso);
  if (isNaN(at)) return "";
  const clock = at.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  if (layer === "window") {
    return at.toDateString() === new Date().toDateString() ? `at ${clock}` : `tomorrow at ${clock}`;
  }
  if (layer === "week") return `on ${at.toLocaleDateString("en-GB", { weekday: "long" })} at ${clock}`;
  return `on ${at.toLocaleDateString("en-GB", { day: "numeric", month: "long" })} at ${clock}`;
}

// A failure frame's sentence with its reset in the reader's clock. The server
// wrote it in UTC and says which words did (`when`); any other failure, or a
// frame from a server that sends no instant, keeps its sentence as written.
function limitSentence(ev) {
  if (!ev || !ev.limit || !ev.when || !ev.resets_at) return ev ? ev.error : "";
  const local = limWhen(ev.limit, ev.resets_at);
  return local ? ev.error.replace(ev.when, local) : ev.error;
}

function limBox() {
  let box = document.getElementById("usage");
  if (box) return box;
  const foot = document.getElementById("side-foot");
  if (!foot) return null;
  box = document.createElement("section");
  box.id = "usage";
  box.setAttribute("aria-label", "usage");
  box.hidden = true;
  foot.prepend(box);
  return box;
}

function limLine() {
  let line = document.getElementById("limit-line");
  if (line) return line;
  const composer = document.getElementById("composer");
  if (!composer) return null;
  line = document.createElement("div");
  line.id = "limit-line";
  line.setAttribute("role", "status");
  line.hidden = true;
  composer.before(line);
  return line;
}

function limLevel(used) {
  return used >= 1 ? "full" : used >= LIM_WARN ? "warn" : "ok";
}

function limRow(l) {
  const pct = Math.round(Math.min(1, Math.max(0, l.used)) * 100);
  const row = document.createElement("div");
  row.className = "us-row";
  row.dataset.layer = l.layer;
  row.dataset.level = limLevel(l.used);
  const head = document.createElement("div");
  head.className = "us-head";
  const name = document.createElement("span");
  name.className = "us-name";
  name.textContent = LIM_NAMES[l.layer] || l.layer;
  const val = document.createElement("span");
  val.className = "us-pct";
  val.textContent = `${pct}%`;
  head.append(name, val);
  const bar = document.createElement("div");
  bar.className = "us-bar";
  bar.setAttribute("role", "progressbar");
  bar.setAttribute("aria-label", `${LIM_NAMES[l.layer] || l.layer} used`);
  bar.setAttribute("aria-valuemin", "0");
  bar.setAttribute("aria-valuemax", "100");
  bar.setAttribute("aria-valuenow", String(pct));
  const fill = document.createElement("i");
  fill.style.width = `${pct}%`;
  bar.appendChild(fill);
  const reset = document.createElement("div");
  reset.className = "us-reset";
  // A window opens at the first message after the last one closed (point 4),
  // so between sittings there is no reset to name yet.
  reset.textContent = l.resets_at ? `resets ${limWhen(l.layer, l.resets_at)}` : "starts with your next message";
  row.append(head, bar, reset);
  return row;
}

// The line above the composer names one layer: a spent one first, and of
// several the one that resets last, since that is how long answers stop;
// otherwise the fullest at 80% or more.
function limNotice(limits) {
  const line = limLine();
  if (!line) return;
  const latest = (a, b) => Date.parse(b.resets_at) - Date.parse(a.resets_at);
  const full = limits.filter((l) => l.used >= 1 && l.resets_at).sort(latest);
  const pick = full[0] || limits.filter((l) => l.used >= LIM_WARN && l.resets_at).sort((a, b) => b.used - a.used)[0];
  if (!pick) {
    line.hidden = true;
    return;
  }
  const what = LIM_PHRASE[pick.layer] || pick.layer;
  const used = pick.used >= 1 ? what : `${Math.round(pick.used * 100)}% of ${what}`;
  line.textContent = `You have used ${used}. It resets ${limWhen(pick.layer, pick.resets_at)}.`;
  line.dataset.level = limLevel(pick.used);
  line.hidden = false;
}

async function loadLimits() {
  let limits = [];
  try {
    limits = (await (await fetch("/limits")).json()).limits || [];
  } catch {
    // A failed read draws nothing rather than a stale figure: the server's
    // own check before each call still holds the limits (ADR-0102 point 5).
    limits = [];
  }
  const box = limBox();
  if (box) {
    box.replaceChildren(...limits.map(limRow));
    box.hidden = !limits.length;
  }
  limNotice(limits);
  clearTimeout(limTimer);
  const now = Date.now();
  const soonest = Math.min(...limits.map((l) => Date.parse(l.resets_at)).filter((t) => t > now));
  if (Number.isFinite(soonest)) {
    limTimer = setTimeout(loadLimits, Math.min(soonest - now + 2000, LIM_REFRESH_CAP_MS));
  }
}

loadLimits();
