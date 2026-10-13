// --- conversions that left the request (ADR-0099) -------------------------------
// A dropped recording, PDF or document converts after the answer, one at a time:
// waiting, then transcribing or converting, then ready or failed. The row sits
// above the composer, where the eye waits, and survives a reload: the server
// keeps each job in a file, the narration stream carries its transitions as
// `job` events, and every (re)connect of that stream reads the list again, so
// an event missed while it was down is never a row that stays stale.
// A finished job offers the turn and never starts it (point 6): Nucleate it.
const jobsTray = $("#jobs");
const jobsById = new Map();
// Ids this tab has already told the person about: a stream reconnect replays
// nothing, but the list read on reconnect must not toast a job twice.
const jobsToldDone = new Set();
let jobsClock = 0;

// "47 min" for a recording's length: whole minutes past one, seconds below it.
function jobLength(s) {
  if (!s) return "";
  return s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;
}

// Where a job shows. Running and waiting ones everywhere: they are the
// person's files in flight, whatever chat is open. A finished or stopped one
// on its chat, or everywhere when it has none the list shows: an import, a
// drop into a chat that never got a turn, a chat since deleted. An import's
// finished files are the import box's to report, not a row each here.
function jobHere(j) {
  if (j.state === "queued" || j.state === "running") return true;
  if (j.origin === "import" && j.state === "done") return false;
  if (!j.session || j.session === currentSessionId) return true;
  return !document.querySelector(`#sessions .session[data-id="${CSS.escape(j.session)}"]`);
}

function jobWhat(j, ahead) {
  if (j.state === "queued") return ahead ? `waiting, ${ahead} ahead of it` : "waiting";
  if (j.state === "running") {
    const verb = j.media ? "transcribing" : "converting";
    const len = j.media && j.seconds ? ` ${jobLength(j.seconds)} of audio` : "";
    const t = j.started ? Math.max(0, Date.now() / 1000 - j.started) : -1;
    const since = t < 0 ? "" : t < 60 ? " · under a minute so far" : ` · ${Math.round(t / 60)} min so far`;
    return verb + len + since;
  }
  if (j.state === "done") return "ready";
  return j.error || (j.state === "interrupted" ? "the server restarted during this conversion" : "it could not be converted");
}

function jobButton(label, cls, title, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = label;
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

async function jobPost(j, action, body) {
  try {
    const r = await fetch(`/jobs/${j.id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      notify(typeof d.detail === "string" ? d.detail : `the server answered ${r.status}`);
      return null;
    }
    return r;
  } catch {
    notify("the server could not be reached");
    return null;
  }
}

// The offered turn: the person's click starts it, in the chat that is open.
function jobOffer(j, ask) {
  if (streaming) { notify("a turn is running: offer it again when it ends", "warn"); return; }
  const said = ask ? j.text.trim() : "Nucleate " + j.source.name;
  bubble("user").textContent = said;
  jobsById.delete(j.id);
  renderJobs();
  const go = () => runTurn(fetch(`/jobs/${j.id}/turn`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ask }),
  }), "working", go);
  go();
}

function jobRow(j, ahead) {
  const row = document.createElement("div");
  row.className = "job";
  row.dataset.state = j.state;
  row.dataset.job = j.id;
  const dot = document.createElement("span");
  dot.className = "job-dot";
  dot.setAttribute("aria-hidden", "true");
  const name = document.createElement("span");
  name.className = "job-name";
  name.textContent = j.source.name;
  name.title = j.source.path || j.source.name;
  const what = document.createElement("span");
  what.className = "job-what";
  what.textContent = jobWhat(j, ahead);
  row.append(dot, name, what);
  const acts = document.createElement("span");
  acts.className = "job-acts";
  if (j.state === "done") {
    for (const n of j.notes || []) {
      const a = jobButton(n.split("/").pop().replace(/\.md$/, ""), "job-note linklike", "open " + n, () => openNote(n));
      what.append(" ", a);
    }
    acts.append(jobButton("Nucleate it", "job-go", "nucleate it into the vault, in this chat", () => jobOffer(j, false)));
    if ((j.text || "").trim()) {
      acts.append(jobButton("ask again", "job-alt linklike",
        `send "${j.text.trim()}" again, with the note attached`, () => jobOffer(j, true)));
    }
  } else if ((j.state === "failed" || j.state === "interrupted") && j.retry) {
    acts.append(jobButton("Retry", "job-go", "convert the same file again", async () => {
      if (await jobPost(j, "retry")) jobsFetch();
    }));
  } else if (j.state === "failed" || j.state === "interrupted") {
    what.textContent += ". The file is no longer here: add it again";
  }
  if (j.state !== "queued" && j.state !== "running") {
    const x = jobButton("✕", "job-x", "dismiss", async () => {
      if (await jobPost(j, "dismiss")) { jobsById.delete(j.id); renderJobs(); }
    });
    x.setAttribute("aria-label", "dismiss " + j.source.name);
    acts.append(x);
  }
  if (acts.childElementCount) row.append(acts);
  return row;
}

// Rebuilt whole on each change: a handful of rows, and a rebuild cannot leave
// a row in a state the list no longer has.
function renderJobs() {
  const all = [...jobsById.values()].sort((a, b) => (a.created || 0) - (b.created || 0));
  const shown = all.filter(jobHere);
  jobsTray.replaceChildren();
  // The worker takes them in order, so what waits behind the running one is
  // its place in this tenant's line; the line is never anyone else's.
  const live = all.filter((j) => j.state === "running" || j.state === "queued");
  const waiting = shown.filter((j) => j.state === "queued");
  for (const j of shown) {
    if (j.state === "queued" && waiting.indexOf(j) >= 3) continue;
    jobsTray.append(jobRow(j, live.indexOf(j)));
  }
  if (waiting.length > 3) {
    const more = document.createElement("div");
    more.className = "job job-more";
    more.textContent = `and ${waiting.length - 3} more waiting`;
    jobsTray.append(more);
  }
  jobsTray.hidden = !jobsTray.childElementCount;
  // Elapsed time on a running row is the one thing that moves on its own.
  const running = all.some((j) => j.state === "running");
  if (running && !jobsClock) jobsClock = setInterval(renderJobs, 15000);
  if (!running && jobsClock) { clearInterval(jobsClock); jobsClock = 0; }
}

function jobsUpdate(j) {
  if (!j || !j.id) return;
  const before = jobsById.get(j.id);
  if (j.dismissed) jobsById.delete(j.id);
  else jobsById.set(j.id, j);
  const ended = (j.state === "done" || j.state === "failed") && before && before.state !== j.state;
  if (ended && !jobsToldDone.has(j.id)) {
    jobsToldDone.add(j.id);
    const msg = j.state === "done" ? `${j.source.name} is ready` : `${j.source.name} could not be converted`;
    // Said aloud always; shown as a toast only when its row is not in view.
    if (jobHere(j)) announce(msg); else notify(msg + ", in the chat it was added to", j.state === "done" ? "info" : "error");
  }
  // Its notes landed in the vault: the tree and the vault's figures redraw, as
  // they do when a turn ends.
  if (j.state === "done" && before && before.state !== "done") {
    loadVaultInfo();
    markVaultChanged();
  }
  if (typeof vaultImportJob === "function") vaultImportJob(j);
  renderJobs();
}

async function jobsFetch() {
  try {
    const r = await fetch("/jobs");
    if (!r.ok) return;
    const { jobs } = await r.json();
    const seen = new Set();
    for (const j of jobs) {
      seen.add(j.id);
      jobsUpdate(j);
    }
    for (const id of [...jobsById.keys()]) if (!seen.has(id)) jobsById.delete(id);
    renderJobs();
  } catch { /* the next reconnect reads the list again; the tray keeps what it had */ }
}

// The answer to a drop or a command that only queued conversions: no turn ran,
// so the reply says what happens next instead of streaming anything.
function drawJobAnswer(flow, d) {
  const lines = [];
  for (const j of d.jobs || []) {
    const verb = j.media ? "is transcribed" : "converts";
    lines.push(`${j.source.name} ${verb} after this answer. It shows above the message box, where you can nucleate it once it is ready.`);
    jobsUpdate(j);
  }
  if (d.command === "/nucleate" && (d.jobs || []).length) {
    lines.push("The command runs once its files are converted: use Nucleate it, or type it again then.");
  }
  for (const s of d.skipped || []) lines.push(`${s.name}: not taken, ${s.reason}.`);
  for (const m of d.mail || []) {
    lines.push(`${m}: mail is imported in Settings › Connectors › Mail, or with /mail import <file>, which show a dry run first.`);
  }
  for (const t of lines) {
    const p = document.createElement("div");
    p.className = "stream-text job-answer";
    p.textContent = t;
    flow.appendChild(p);
  }
}

document.addEventListener("silica:job", (e) => jobsUpdate(e.detail));
document.addEventListener("silica:stream-open", jobsFetch);
document.addEventListener("silica:session", renderJobs);
// loadSessions() sets currentSessionId and redraws the list without an event;
// a chat's first turn is when a drop's job learns it has a listed chat.
new MutationObserver(renderJobs).observe($("#sessions"), { childList: true });
jobsFetch();
