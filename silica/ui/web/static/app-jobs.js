// --- conversions that left the request (ADR-0099) -------------------------------
// A dropped recording, PDF or document converts after the answer, one at a time:
// waiting, then transcribing or converting, then ready or failed. The row sits
// above the composer, where the eye waits, and survives a reload: the server
// keeps each job in a file, the narration stream carries its transitions as
// `job` events, and every (re)connect of that stream reads the list again, so
// an event missed while it was down is never a row that stays stale.
// A finished job never starts a turn by itself (point 6). The page does, for
// a drop whose words wait on it (ADR-0121 point 1): the chat holding the drop
// asks its question once the drop's last file is read; otherwise the row
// offers what fits, "answer now" or summarize, quiz, add to vault, nucleate.
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
  // A question waiting on this file says so first: it is why the chat is quiet.
  const waits = j.origin === "drop" && (j.text || "").trim() && !j.answered ? "your question waits · " : "";
  if (j.state === "queued") return waits + (ahead ? `waiting, ${ahead} ahead of it` : "waiting");
  if (j.state === "running") {
    const p = j.progress;
    // The chat model reading a PDF's pages is the slow part, and the one that
    // knows how far it is (ADR-0121 point 1): 105 s for 13 pages in the run.
    const verb = p && p.total ? `reading page ${Math.min(p.done + 1, p.total)} of ${p.total}`
      : j.media ? "transcribing" : "converting";
    const len = j.media && j.seconds ? ` ${jobLength(j.seconds)} of audio` : "";
    const t = j.started ? Math.max(0, Date.now() / 1000 - j.started) : -1;
    const since = t < 0 ? "" : t < 60 ? " · under a minute so far" : ` · ${Math.round(t / 60)} min so far`;
    return waits + verb + len + since;
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

// The offered turn, in the chat that is open: "ask" sends the words typed with
// the drop, "summarize" and "quiz" their request, "nucleate" copies the files
// into the Inbox and nucleates them. A click, or the drop's file being read.
const JOB_SAID = { summarize: "Summarize ", quiz: "Quiz me on ", nucleate: "Nucleate " };
function jobOffer(j, kind) {
  if (streaming) { notify("a turn is running: offer it again when it ends", "warn"); return; }
  const said = kind === "ask" && (j.text || "").trim() ? j.text.trim() : (JOB_SAID[kind] || "Summarize ") + j.source.name;
  // The drop's own bubble when this page drew it: the question is asked once,
  // so it is drawn once, and the line that said it waits goes.
  const prior = kind === "ask" && j.drop ? log.querySelector(`.msg.user .body[data-drop="${CSS.escape(j.drop)}"]`) : null;
  if (prior) {
    delete prior.dataset.drop;
    prior.closest(".msg").nextElementSibling?.querySelector(".job-answer")?.closest(".msg")?.remove();
    log.appendChild(prior.closest(".msg"));
    lastUserBody = prior;
  } else {
    const body = bubble("user");
    body.textContent = said;
    body.dataset.words = said;
    userFiles(body, [{ name: j.source.name }]);
  }
  jobsById.delete(j.id);
  renderJobs();
  const go = () => runTurn(fetch(`/jobs/${j.id}/turn`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ as: kind }),
  }), "working", go);
  go();
}

// --- a drop's question asks itself (ADR-0121 point 1) -------------------------
// Only in the tab that shows the drop's chat, and only on the transition to
// done this tab saw: a file read while no tab held the chat waits for "answer
// now", so no turn is spent after the student left. The server answers a drop
// once, so a second tab on the same chat gets a 409 and draws nothing.
let dropTurn = null;   // a drop whose question waits for the turn running now
// The record as it is now: the drop's own turn may have answered it meanwhile
// (a PDF read from its text layer, ADR-0099 amendment 2026-10-20).
function takeDropTurn() { const j = dropTurn && jobsById.get(dropTurn.id); dropTurn = null; return j && dropReady(j) ? j : null; }

function dropReady(j) {
  if (j.origin !== "drop" || !(j.text || "").trim() || j.answered || j.session !== currentSessionId) return false;
  return [...jobsById.values()].filter((o) => o.drop && o.drop === j.drop).every((o) => o.state === "done");
}

function askDrop(j) {
  if (!dropReady(j)) return;
  if (streaming) { dropTurn = j; return; }   // sendQueued() runs it when this turn ends
  jobOffer(j, "ask");
}

async function jobAddToVault(j) {
  try {
    const r = await fetch("/chat/files/add", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: j.source.path }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { notify(typeof d.detail === "string" ? d.detail : `the server answered ${r.status}`); return; }
    announce(`${j.source.name} added to the Inbox`);
    loadVaultInfo();
    markVaultChanged();
  } catch { notify("the server could not be reached"); }
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
    // ADR-0087 point 3: the note names the pages no model read, and so does
    // the row that offers it, after the note it names them in.
    if (j.pages_not_read) what.append(" · " + j.pages_not_read);
    // What fits the drop (ADR-0121 point 6): its question when it had words,
    // otherwise a summary first, then the rest. Asking anything else is typing:
    // the file joins the next message of this chat (point 4).
    if ((j.text || "").trim() && !j.answered) {
      acts.append(jobButton("answer now", "job-go", `send "${j.text.trim()}" with this file`, () => jobOffer(j, "ask")));
    } else {
      acts.append(jobButton("summarize", "job-go", "a summary of this file, in this chat", () => jobOffer(j, "summarize")));
      acts.append(jobButton("quiz me", "job-alt linklike", "questions on this file, one at a time", () => jobOffer(j, "quiz")));
    }
    if (j.origin === "drop") {
      acts.append(jobButton("add to vault", "job-alt linklike", "copy it and its notes into the Inbox; the chat keeps its own",
        () => jobAddToVault(j)));
    }
    acts.append(jobButton("Nucleate it", "job-alt linklike", "copy it into the Inbox and nucleate it, in this chat",
      () => jobOffer(j, "nucleate")));
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
  // Its notes landed: the tree and the vault's figures redraw, as they do when
  // a turn ends (a chat's file lands in its chat's folder, and nothing moves).
  if (j.state === "done" && before && before.state !== "done") {
    loadVaultInfo();
    markVaultChanged();
  }
  if (ended && j.state === "done" && !j.dismissed) askDrop(j);
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
  const names = (d.jobs || []).map((j) => j.source.name).join(", ");
  // The drop opened this chat's session (ADR-0121 point 1): its rows and its
  // question belong here before the chat list is read again. Its bubble is no
  // turn the server holds yet, so it carries the drop until its answer runs.
  if (d.session) currentSessionId = d.session;
  const drop = (d.jobs || []).find((j) => j.drop)?.drop;
  if (lastUserBody && (d.jobs || []).length) lastUserBody.dataset.drop = drop || "none";
  for (const j of d.jobs || []) jobsUpdate(j);
  if (d.waits && names) {
    lines.push(`Your question waits for ${names}: it is answered here once ${(d.jobs || []).length === 1 ? "the file is" : "the files are"} read.`);
  } else if (d.command === "/nucleate" && (d.jobs || []).length) {
    lines.push(`${names}: the command runs once its files are converted: use Nucleate it on the row above, or type it again then.`);
  } else if (names) {
    lines.push(`${names} ${(d.jobs || []).length === 1 ? "is" : "are"} being read. Once ready, summarize it, `
      + "quiz yourself on it, or just ask about it here.");
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
