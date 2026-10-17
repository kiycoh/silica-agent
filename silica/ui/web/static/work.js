// Work: the left rail's last compartment, and the only surface in this app that
// reads the narration. It fills TWO panes now, at opposite edges, and the split
// is the point: the run goes left (#work-body, always there, readable while a
// note is open), what you POINTED at goes right (#node-pane, the drawer mode
// named after it). One name used to cover both, and it drew the run on chat and
// the node on explore - so the segment labelled `work` was wrong half the time.
//
// `silica/agent/narration.py` has written an append-only account of every run
// since it shipped, and `/narration/sse` has served it live for just as long.
// Nothing rendered it. So this file is a PROJECTION of a stream that already
// exists, not new plumbing: every duration, token delta, phase and write below
// is a record the agent already wrote.
//
// It loads after every cut of app.js and uses two of their globals on purpose —
// openNote() from app-notes.js to read a source, send() from app-chat.js to
// revert a write — rather than restating either.

// --- projection:begin --------------------------------------------------------
// Everything between this marker and projection:end is pure: no DOM, no fetch,
// no globals. tests/test_work_panel.py lifts the block out and runs it under
// node, which is how the fold below is checked at all.
// The narration payload carries the RAW tool name and the raw args, and the
// browser has no renderer, so the verb table has to live here too. A copy that
// nothing checks is a copy that goes stale on the next tool, so
// tests/test_work_panel.py pins it entry-for-entry to _TOOL_DESC in
// silica/ui/renderer.py and fails when the two disagree.
const TOOL_VERBS = {
  "silica_search": ["search titles", "query"],
  "silica_search_context": ["search inside", "query"],
  "silica_read_note": ["read", "name"],
  "silica_write_note": ["write note", "path"],
  "silica_patch_note": ["patch note", "name"],
  "silica_flag_note": ["flag note", "name"],
  "silica_record_quiz": ["record quiz", null],
  "silica_review_queue": ["review queue", null],
  "silica_props": ["props", "name"],
  "silica_outline": ["outline", "name"],
  "silica_links": ["links", "name"],
  "silica_backlinks": ["backlinks", "name"],
  "silica_orphans": ["orphans", null],
  "silica_unresolved": ["unresolved links", null],
  "silica_files": ["list files", "folder"],
  "silica_exists": ["exists", "path"],
  "silica_deferred_list": ["deferred list", null],
  "silica_deferred_flush": ["deferred flush", null],
  "silica_deferred_retry": ["deferred retry", null],
  "silica_anneal": ["anneal", null],
  "silica_inbox_ls": ["inbox", null],
  "silica_recon": ["recon", "inbox_file"],
  "silica_payload": ["payload", "recon_report_path"],
  "silica_sanitize": ["sanitize", "distiller_output_path"],
  "silica_validate_ops": ["validate", "ops_json_path"],
  "silica_bulk_write": ["bulk write", "ops_json_path"],
  "silica_lint": ["lint", "note_name"],
  "silica_run_injector": ["injector", "inbox_file"],
  "silica_delete": ["delete", "ref"],
  "silica_snapshot": ["snapshot", "ops_json_path"],
  "silica_restore": ["restore", "txn_id"],
  "silica_cleanup": ["cleanup", "inbox_file"],
  "web_search": ["web search", "query"],
  "web_fetch": ["web fetch", "url"],
  "silica_web_answer": ["web answer", "question"],
};

// How a tool changes the vault. Same keys as _TOOL_EFFECT in web/callback.py,
// and the reason a patch target is a WRITE here and never also a source: the
// panel's two lists are derived from one table, so a note cannot appear in both.
const WRITE_OPS = {
  "silica_write_note": "write",
  "silica_patch_note": "patch",
  "silica_flag_note": "flag",
  "silica_bulk_write": "bulk write",
  "silica_restore": "restore",
  "silica_delete": "delete",
  "silica_move": "move",
};

// Arg keys that name a note, the allowlist from web/callback.py. Missing one
// only drops a row from Sources; it never names the wrong note.
const NOTE_KEYS = ["name", "path", "note", "note_path", "ref"];

const fmtSecs = (s) => (s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${String(Math.floor(s % 60)).padStart(2, "0")}s`);
const fmtTok = (n) => Number(n).toLocaleString("en-US");

function toolVerb(name) {
  const e = TOOL_VERBS[name];
  return e ? e[0] : String(name || "").replace(/^silica_/, "").replace(/_/g, " ");
}

function toolTarget(name, args) {
  const a = args || {};
  if (name === "silica_move") return `${a.ref || ""} → ${a.to || ""}`.trim();
  const e = TOOL_VERBS[name];
  const key = e && e[1];
  const raw = key ? String(a[key] == null ? "" : a[key]).trim() : "";
  // web_fetch's target is a URL, and a URL in a 372px column is a wall. The
  // host is what you judge a source by at a glance; the full link arrives in
  // the turn's own footer anyway.
  if (name === "web_fetch" && raw) { try { return new URL(raw).host; } catch { return raw; } }
  return raw;
}

// --- the projection ----------------------------------------------------------
// One pass over the beats of a session, returning the CURRENT run: a user turn
// starts a new one, so the panel shows what is happening rather than the whole
// file. Pinned by tests/test_work_panel.py, which runs this function under node.
function projectRun(beats) {
  let title = "", model = "", t0 = null, tLast = null;
  let rows = [], writes = [], sources = [];
  const seen = new Set();
  let spans = {};          // span id -> row (or {ts} for a call, which has no row)
  let openThoughts = [];   // innermost last: a call folds into the one it ran inside
  for (const b of beats || []) {
    const kind = b.kind, st = b.status, id = b.id, p = b.payload || {};
    const terminal = st !== "running";
    if (b.ts != null) tLast = b.ts;
    if (kind === "session") continue;
    if (kind === "turn") {
      const m = p.message || {};
      // Only the human's turn starts a run. The assistant and tool turns are
      // the same exchange, and resetting on them would blank the panel between
      // the question and the answer, which is exactly when it is being read.
      if (m.role === "user" && typeof m.content === "string") {
        title = m.content.split("\n")[0];
        t0 = b.ts; rows = []; writes = []; sources = []; seen.clear();
        spans = {}; openThoughts = [];
      }
      continue;
    }
    if (kind === "thought") {
      if (!terminal) {
        const row = { kind: "think", text: "", sub: "", dur: "", running: true, seq: b.seq };
        rows.push(row); spans[id] = { row, ts: b.ts }; openThoughts.push(row);
      } else if (spans[id]) {
        const row = spans[id].row;
        row.text = String(p.text || b.summary || "").trim();
        row.dur = fmtSecs(p.duration_s != null ? p.duration_s : b.ts - spans[id].ts);
        row.running = false;
        openThoughts = openThoughts.filter((r) => r !== row);
      }
      continue;
    }
    if (kind === "call") {
      // A call opens INSIDE the thought it produces and closes on the same tick,
      // and neither carries the other as a parent, so the fold keys on nesting
      // and not on `parent`. Printing both rows prints the same seconds twice.
      // A retry re-narrates the span as running; the start stays the first one.
      if (!terminal) { if (!spans[id]) spans[id] = { ts: b.ts }; if (p.model) model = p.model; continue; }
      const bits = [];
      if (p.prompt_tokens != null) bits.push(`${fmtTok(p.prompt_tokens)} → ${fmtTok(p.completion_tokens || 0)} tok`);
      if (p.cached_tokens) bits.push(`${fmtTok(p.cached_tokens)} cached`);
      const sub = bits.join(" · ");
      const host = openThoughts[openThoughts.length - 1];
      if (host) { host.sub = sub; }
      else {
        rows.push({ kind: "call", verb: "call", target: p.model || model, sub: sub,
                    dur: spans[id] ? fmtSecs(b.ts - spans[id].ts) : "", running: false, seq: b.seq });
      }
      continue;
    }
    if (kind === "tool") {
      const name = p.name || "";
      if (!terminal) {
        const row = { kind: "tool", name: name, verb: toolVerb(name), target: toolTarget(name, p.args),
                      sub: "", dur: "", running: true, failed: false, seq: b.seq };
        rows.push(row); spans[id] = { row, ts: b.ts, args: p.args || {} };
        continue;
      }
      const sp = spans[id];
      if (!sp) continue;
      const row = sp.row;
      row.running = false;
      row.failed = st === "failed";
      row.dur = fmtSecs(b.ts - sp.ts);
      const op = WRITE_OPS[row.name];
      if (op) {
        writes.push({ op: op, target: row.target, failed: row.failed, seq: b.seq });
      } else {
        const key = NOTE_KEYS.find((k) => sp.args[k]);
        const nm = key ? String(sp.args[key]).trim() : "";
        if (nm && !seen.has(nm)) { seen.add(nm); sources.push({ name: nm, verb: row.verb }); }
      }
      continue;
    }
    if (kind === "write") {
      writes.push({ op: p.op || "write", target: p.path || b.summary || "",
                    failed: st === "rolled_back" || st === "failed", seq: b.seq });
      continue;
    }
    if (kind === "phase") {
      const label = String(p.phase || b.summary || "").trim();
      const pos = [];
      if (p.file_total) pos.push(`file ${p.file_idx || 0} of ${p.file_total}`);
      if (p.chunk_total) pos.push(`chunk ${p.chunk_idx || 0} of ${p.chunk_total}`);
      const last = rows[rows.length - 1];
      if (last && last.kind === "phase" && last.target === label && last.running) {
        last.running = !terminal; last.sub = pos.join(" · ");
        if (terminal && spans[id]) last.dur = fmtSecs(b.ts - spans[id].ts);
        continue;
      }
      rows.push({ kind: "phase", verb: "phase", target: label, sub: pos.join(" · "),
                  dur: "", running: !terminal, seq: b.seq });
      spans[id || label] = { ts: b.ts };
      continue;
    }
    if (kind === "cancel") {
      for (const r of rows) r.running = false;
      continue;
    }
  }
  const running = rows.some((r) => r.running);
  const tools = rows.filter((r) => r.kind === "tool").length;
  const thinks = rows.filter((r) => r.kind === "think").length;
  const shape = [thinks ? `${thinks} calls` : "", tools ? `${tools} tools` : ""].filter(Boolean).join(" · ");
  // t0 and tLast ride out raw so the run line can tick between beats and name
  // a stall: a formatted elapsed freezes at the last beat, which is exactly
  // the moment a stalled run stops being distinguishable from a finished one.
  return { title, model, running, rows, writes, sources, shape, t0, tLast,
           elapsed: t0 != null && tLast != null ? fmtSecs(tLast - t0) : "" };
}

// --- the node projection -----------------------------------------------------
// This column states the node you pointed at, on every view and not only on
// explore. It is the ONLY reader of /context now. The note drawer used to draw
// the same payload as its `context` mode, and the two had already drifted: the
// drawer showed snippets this projection threw away, this projection showed
// degree the drawer threw away, and neither header said which of the two you
// were looking at.
//
// Two sources meet here and neither is asked twice: the graph's own message
// carries the facts it had to compute in order to DRAW the node (degree, state,
// area), and /context carries what the vault knows about it.
//
// `loading` is a state and not an absence: the head facts arrive with the click
// and the sections one round trip later, so the panel says the node's name
// immediately rather than staying on the previous node until the fetch lands.
function projectNode(node, ctx) {
  const n = node || {};
  const c = ctx || {};
  const rel = c.related || {};
  const sug = c.suggested || [];
  // The count is kept apart from the words because it is painted apart: it is
  // the one figure on this line, and three grey strings in a row read as one
  // sentence — the more so when the area's own label already contains a dot.
  const meta = [];
  // "note" is the default state, so printing it says nothing; hub and orphan
  // are the readings worth a word, and they come from nodeState() in the frame
  // rather than from a second threshold invented here.
  if (n.state && n.state !== "note") meta.push(n.state);
  if (n.area) meta.push(n.area);
  const name = (p) => String(p || "").split("/").pop().replace(/\.md$/, "");
  const linked = new Set(
    [...(rel.outgoing || []), ...(rel.backlinks || [])].map((r) => r.path).filter(Boolean));
  const d = {
    title: c.title || n.name || name(n.path),
    path: n.path || c.path || "",
    ghost: !!(n.ghost || c.ghost),
    count: n.links == null ? "" : n.links + (n.links === 1 ? " link" : " links"),
    meta: meta,
    loading: !ctx,
    error: c.error || "",
    concepts: (c.concepts || []).slice(0, 12),
    // What the note SAYS, which details' Content tab draws. It was here so
    // the pane could settle whether to spend a SECOND click on the text; that
    // click is gone (ADR-0075), the text is the default view, and whether this
    // still earns its tab is the ADR's one open question. The server drops
    // snippets under 700 characters, so a short note never repeats itself.
    says: c.snippets || [],
    // A frontmatter `related:` is not a body wikilink. One is a claim the
    // author made about the note, the other is a sentence that happened to name
    // another note. But a note that is BOTH is one relation, not two, and
    // listing every entry stated those rows twice on one panel - which is the
    // duplicate this whole surface exists to remove, rebuilt one heading lower.
    // Measured on the dev vault the day this shipped, 7 of 7 entries were
    // duplicates, so the full list would have been pure noise and the filtered
    // one is empty until it has something to say.
    //
    // What is left is the half nothing else states: declared and never linked.
    // An entry that resolves to nothing has no path to match on, so a dead
    // `related:` still lists, which is the one case worth acting on.
    related: (rel.frontmatter || []).filter((r) => !r.path || !linked.has(r.path)),
    out: rel.outgoing || [],
    // A paper's own notes already list under "Notes from this paper"; what
    // is left here is notes citing it from elsewhere.
    from: (rel.backlinks || []).filter((r) =>
      !(c.paper && c.paper.notes.some((p) => p.path === r.path))),
    // Two flavours of one list, split because they are two different offers:
    // a ghost is a link you already wrote and never filled, a note is a
    // relative the vault computed and you never linked.
    missing: sug.filter((x) => x.kind === "ghost"),
    similar: sug.filter((x) => x.kind === "note"),
    // Where the note SITS, as opposed to what it says or who it links.
    // Passed through whole and read by placeReadings(), which is where each
    // number is turned into the sentence it means: the projection stays a
    // projection, and the thresholds live in one place a test can reach.
    structure: c.structure || null,
    // The reading order around it: rungs by depth, the note itself at 0. Passed
    // through whole for the same reason `structure` is - the server decided
    // which rungs are near enough to name and which are only a count, and a
    // second opinion about that in JS is a second thing to keep in step.
    ladder: c.ladder || null,
    // Why a section came back thin, from the machine that came back thin.
    // Without it "Similar (0)" reads as "this note has no relatives" when what
    // it means is "nobody has run /embed".
    hint: c.hint || "",
    // A Zotero paper (ADR-0055): its identity and citations, passed through
    // whole, the way structure is; the renderer turns it into rows.
    paper: c.paper || null,
    // A missing paper (ADR-0055): cited by the library, absent from it. The
    // frame knows its reference; there is no note to ask /context about.
    cited: n.href ? { quote: n.quote || "", href: n.href } : null,
    // A paper on the rim (ADR-0066 decision 4): what the frame knew, its
    // covering notes with k/K, and Zotero's record once it answers.
    outside: n.outside ? { ...n.outside, item: c.item || null } : null,
  };
  // The details' two derived readings (ADR-0075), off the fields above so the
  // lists they merge are the filtered ones: a paper's own notes stay out of
  // `from`, and a declared relation that is also a link is not a second row.
  d.neighbours = neighbourRows(d);
  d.place = placeReadings(d.structure);
  return d;
}

// Where a note SITS, in the words ADR-0075 settled: each reading states a
// consequence and carries its scale, and the graph term comes last, for the
// reader who knows ADR-0027. The gates decided what each may CLAIM:
//
//   cut vertex  PASSED, so it is allowed an imperative reading: the one
//     warning, amber, and the reason the details open on Place.
//   coreness    PASSED; stated as a depth against the vault's deepest.
//   surprise    PASSED as part of the same variable; stated as the two ranks
//     it is the difference of, never as the difference or as a verdict.
//
// null when the graph has never seen the note. Inside, a reading is null when
// it says nothing, which is why an ordinary note draws no tile at all: "core 1"
// and "+4" are two lines saying "normal".
const countOf = (n, one, many) => n + " " + (n === 1 ? one : many);

function placeReadings(st) {
  if (!st || st.coreness == null) return null;
  let bridge = null;
  if (st.articulation) {
    const n = st.strands || 0;
    // Without the component's size (the names failed, the count did not)
    // there is no M to state, and "the other 0 notes" would be a lie.
    const rest = st.component ? Math.max(0, st.component - 1 - n) : null;
    bridge = {
      title: n ? "Only bridge for " + countOf(n, "note", "notes") : "A bridge",
      why: !n ? "Removing it splits its part of the vault in two."
        : rest == null ? (n === 1 ? "It is" : "They are") + " cut off from the rest without this one."
        : (n === 1 ? "It reaches" : "They reach") + " the other " + countOf(rest, "note", "notes")
          + " only through this one.",
      strands: n, rest: rest, pieces: st.pieces || [], behind: st.behind || [],
      cut: st.cut || null,
      term: "cut vertex",
    };
  }
  const k = st.coreness;
  const depth = k >= 2 ? {
    title: "Mesh depth " + k + " of " + (st.core_max || k),
    why: "Each note in its group links to at least " + k + " others in it.",
    scale: st.notes ? st.core_reach + " of " + st.notes + " notes reach depth " + k + "." : "",
    k: k, max: st.core_max || k, hist: st.core_hist || [], term: "k-core",
  } : null;
  // Both tails are worth a reading and they read as opposites. ±20 is the
  // threshold the old "more crossings than links" row used; the ranks are new.
  const s = st.surprise || 0;
  const routing = Math.abs(s) >= 20 ? {
    title: s > 0 ? "Busier than its links" : "Linked more than crossed",
    why: s > 0
      ? "More paths cross it than " + st.betweenness_pct + "% of notes; it has more links than "
        + st.degree_pct + "%."
      : "Well linked, but few paths go through it: a member, not a junction.",
    links: st.degree_pct || 0, paths: st.betweenness_pct || 0,
    focus: s > 0 ? "paths" : "links",
    term: "surprise",
  } : null;
  return { warn: !!st.articulation, bridge: bridge, depth: depth, routing: routing };
}

// Every neighbour once, its relations as tags (ADR-0075). Seven sections used
// to list the same note up to three times: on Backward chaining, Forward
// chaining sat under Links out AND Linked from, Backtracking under Reading
// order AND Links out. One row per note, and the relation is what varies.
//
// Groups, in this order: linked; only in the reading order; declared in the
// frontmatter; similar, not linked; linked and never written. Inside a group
// a note behind the bridge comes first, because that is the warning's list.
const TAG_ORDER = ["both", "in", "out", "-1", "+1", "declared", "similar", "unwritten", "behind"];
const noteName = (p) => String(p || "").split("/").pop().replace(/\.md$/, "");

function neighbourRows(d) {
  const rows = new Map();
  const put = (r, tag, group) => {
    const key = r.path || "ghost:" + (r.name || "");
    let row = rows.get(key);
    if (!row) {
      row = { name: r.name || noteName(r.path), path: r.path || "", tags: [], group: group,
              unreachable: false, why: "" };
      rows.set(key, row);
    }
    row.group = Math.min(row.group, group);
    if (!row.tags.includes(tag)) row.tags.push(tag);
    return row;
  };
  for (const r of d.out) put(r, "out", 0);
  for (const r of d.from) put(r, "in", 0);
  // ±1 only: the near rungs past that are Place's, where the whole spine is.
  const lad = d.ladder;
  for (const rung of (lad && lad.rungs) || []) {
    if (Math.abs(rung.depth) !== 1) continue;
    for (const r of rung.notes) put(r, rung.depth < 0 ? "-1" : "+1", 1);
  }
  for (const r of d.related) put(r, "declared", 2);
  for (const r of d.similar) {
    const row = put(r, "similar", 3);
    row.unreachable = String(r.why || "").startsWith("unreachable");
  }
  for (const r of d.missing) { put(r, "unwritten", 4).why = r.why || ""; }
  const behind = new Set((d.structure && d.structure.behind) || []);
  const out = [...rows.values()].filter((r) => !r.path || r.path !== d.path);
  for (const row of out) {
    if (row.tags.includes("in") && row.tags.includes("out"))
      row.tags = ["both", ...row.tags.filter((t) => t !== "in" && t !== "out")];
    if (row.path && behind.has(row.path)) row.tags.push("behind");
    row.behind = row.tags.includes("behind");
    row.tags.sort((a, b) => TAG_ORDER.indexOf(a) - TAG_ORDER.indexOf(b));
  }
  return out.sort((a, b) => a.group - b.group || Number(b.behind) - Number(a.behind));
}

// --- projection:end ----------------------------------------------------------

// --- the surface -------------------------------------------------------------
(function () {
  // Three elements of the right sidebar's `work` mode: its pane, and the two
  // marks that ride in the sidebar's one header. Looked up from the document and
  // not from a wrapper of their own — this file used to own a whole #work panel
  // with a header, a name and a close button, and all three belonged to being a
  // second surface at an edge that already had one. app.js owns whether the
  // sidebar is open and which mode it shows; this file only fills the pane.
  // The node pane, in the right drawer. On metrics it also stated the report
  // until ADR-0084 took its only entry; Trend and Attention say the same.
  const body = document.getElementById("node-pane");
  if (!body) return;
  const head = document.getElementById("node-state");
  // The scope chip. Empty on a node: the segment beside it is already named
  // after that.
  const scope = document.getElementById("node-scope");
  // The run, in the left rail. Its own head is the live dot in the summary, so
  // the compartment says whether anything is happening while it is folded shut.
  const runBody = document.getElementById("work-body");
  const runState = document.getElementById("work-state");

  let beats = [], sid = null, curSid = null, run = null, tick = null;
  // Which tab is up: it decides what the NODE pane draws (the node on either
  // view that can select one). The rail's Work compartment
  // does not ask - the run is the same run on all four.
  // Read from the DOM and not left at "chat" until the first silica:view: app.js
  // selects the boot tab while it is parsing, which is BEFORE this file loads,
  // so a deep link to #metrics used to land on a pane drawing something else,
  // and stayed that way until the reader clicked a tab.
  const bootTab = document.querySelector(".tab.active");
  let view = bootTab ? bootTab.dataset.tab : "chat";
  let node = null, nodeCtx = null;  // on explore: the node pointed at, and its context
  let bridge = null;                // on explore: the bridge clicked, a /shape row
  let concepts = null;              // on explore: the concepts lit together, a /concepts payload
  let compare = null;               // on explore: two parts side by side, a /compare payload

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  function sectionHead(label, count) {
    const h = el("div", "wk-sec");
    h.appendChild(el("span", "wk-sec-t", label));
    if (count != null) h.appendChild(el("span", "wk-sec-n", String(count)));
    return h;
  }

  function beatRow(r) {
    const row = el("div", "wk-beat" + (r.running ? " running" : "") + (r.failed ? " failed" : ""));
    row.appendChild(el("span", "wk-mark wk-" + r.kind));
    const lab = el("div", "wk-lab");
    if (r.kind === "think") {
      lab.appendChild(el("div", "wk-thought", r.text || "thinking"));
    } else {
      const v = el("div", "wk-v");
      v.appendChild(el("span", "wk-verb", r.verb));
      if (r.target) v.appendChild(el("span", "wk-target", r.target));
      lab.appendChild(v);
    }
    if (r.sub) lab.appendChild(el("div", "wk-sub", r.sub));
    row.appendChild(lab);
    row.appendChild(el("div", "wk-dur", r.dur));
    return row;
  }

  function writeCardEl(w) {
    const card = el("div", "wk-write" + (w.failed ? " failed" : ""));
    const h = el("div", "wk-write-h");
    h.appendChild(el("span", "wk-op", w.failed ? "rolled back" : w.op));
    if (w.failed) h.appendChild(el("span", "wk-note", "vault unchanged"));
    card.appendChild(h);
    card.appendChild(el("div", "wk-path", w.target));
    if (!w.failed && w.target) {
      const acts = el("div", "wk-acts");
      const read = el("button", "wk-mini go", "read");
      read.type = "button";
      read.addEventListener("click", () => { if (window.openNote) window.openNote(w.target); });
      const undo = el("button", "wk-mini", "revert");
      undo.type = "button";
      // `/undo <path>` is the same command the transcript's write card sends,
      // and it takes the rest of the line, so a note name with spaces survives.
      undo.addEventListener("click", () => { if (window.send) window.send("/undo " + w.target); });
      acts.appendChild(read); acts.appendChild(undo);
      card.appendChild(acts);
    }
    return card;
  }

  function sourceRow(s) {
    const row = el("button", "wk-src");
    row.type = "button";
    row.appendChild(el("span", "wk-src-sw"));
    const t = el("div", "wk-src-t");
    t.appendChild(el("div", "wk-src-n", s.name));
    t.appendChild(el("div", "wk-src-v", s.verb));
    row.appendChild(t);
    row.addEventListener("click", () => { if (window.openNote) window.openNote(s.name); });
    return row;
  }

  // The run line above the composer (audit P1) is a second projection of the
  // same beats the rail reads: verb and target of the running step, a clock
  // that ticks between beats, the position in the turn, and a stall sentence
  // once nothing has arrived for STALL_S. app-chat.js owns whether the line
  // shows (it knows when THIS tab's turn starts and ends); this only fills it.
  //
  // ponytail: 90s is a placeholder taken from the 130s tail in the nucleate
  // speed bench; experiment E4 (the gap distribution between beats on real
  // turns) sets the real number. Reopen when E4 lands.
  const STALL_S = 90;
  // Queried at each paint, not once at load: the chat's run line is the ledger
  // of the running turn (ADR-0117 point 4), built when the turn starts.
  const runLinesNow = () => document.querySelectorAll(".run-line");
  const nth = (n) => n + (n % 10 === 1 && n !== 11 ? "st" : n % 10 === 2 && n !== 12 ? "nd" : n % 10 === 3 && n !== 13 ? "rd" : "th");

  function paintRunLine(run) {
    const runLines = runLinesNow();
    if (!runLines.length) return;
    const live = run.running;
    const now = Date.now() / 1000;
    const last = live ? [...run.rows].reverse().find((r) => r.running) : null;
    let verb = live ? "working" : (run.rows.length ? "finishing" : "working");
    let target = "";
    if (last) {
      if (last.kind === "think") verb = "thinking";
      else if (last.kind === "phase") { verb = "phase"; target = last.target; }
      else { verb = last.verb || "running"; target = last.target || ""; }
    }
    const thinks = run.rows.filter((r) => r.kind === "think").length;
    const pos = [];
    if (thinks) pos.push(`${nth(thinks)} call`);
    if (run.sources.length) pos.push(`${run.sources.length} note${run.sources.length === 1 ? "" : "s"} read so far`);
    const stalledFor = live && run.tLast != null ? now - run.tLast : 0;
    const stalled = stalledFor >= STALL_S;
    const elapsed = run.t0 != null ? fmtSecs(Math.max(0, (live ? now : (run.tLast || now)) - run.t0)) : "";
    for (const line of runLines) {
      const set = (cls, txt) => { const e = line.querySelector(cls); if (e) e.textContent = txt; };
      set(".rl-verb", verb);
      set(".rl-target", target);
      set(".rl-elapsed", elapsed);
      set(".rl-pos", pos.join(" · "));
      set(".rl-stall", stalled
        ? `still working · no output for ${Math.floor(stalledFor)}s. The endpoint is slow, not gone`
        : "");
      line.dataset.state = stalled ? "stalled" : "live";
    }
    // The stone in the composer reads the same beats: a model call in flight is
    // thinking, a running tool is acting, a silence past STALL_S is drained.
    // Text arriving is app-chat.js's to report, since only it sees the deltas,
    // and so is the outcome: between a tool's done beat and the next call `live`
    // is false for a few milliseconds, and a stone sent to idle there and back
    // restarted its crossfade on every beat and never arrived anywhere.
    if (live) composerPresence?.state(stalled ? "stalled" : verb === "thinking" ? "thinking" : "acting");
  }

  // The spine is the run's own progress bar, so it has to end at the beat that
  // is RUNNING rather than at the fraction the mock happened to draw. Measured
  // and not counted: a thought row is two lines and a tool row is one, so "the
  // fourth of six beats" is nowhere near two thirds of the way down the column.
  // The insets come from the rule's own computed style because the gradient is
  // laid out in the pseudo-element's box, not the list's — reading them keeps
  // the cyan on the beat when those paddings change in CSS alone.
  function paintSpine(tl) {
    const live = tl.querySelectorAll(".wk-beat.running");
    const last = live[live.length - 1];
    if (!last) { tl.style.removeProperty("--spine"); return; }
    const box = tl.getBoundingClientRect();
    const cs = getComputedStyle(tl, "::before");
    const top = box.top + (parseFloat(cs.top) || 0);
    const span = box.height - (parseFloat(cs.top) || 0) - (parseFloat(cs.bottom) || 0);
    // A hidden panel measures zero, and a percentage of zero is NaN in a
    // gradient, which paints the whole rule accent.
    if (!(span > 0)) { tl.style.removeProperty("--spine"); return; }
    const b = last.getBoundingClientRect();
    const pct = ((b.top + b.height / 2 - top) / span) * 100;
    tl.style.setProperty("--spine", Math.max(0, Math.min(100, pct)).toFixed(1) + "%");
  }

  // --- the Node panel --------------------------------------------------------
  // Same column, the third question. The rows are the source rows the Sources
  // section already uses: a name, what it is, and one thing you can do about it
  // is exactly their shape, and a second row layout for the same shape is a
  // second thing to keep in step.
  // Where a note sits, which is the one thing the name does not say and the
  // reason two notes with the same title are not the same row.
  const folderOf = (p) => {
    const i = String(p || "").lastIndexOf("/");
    return i > 0 ? p.slice(0, i) : "";
  };

  // The two lists whose rows have a direction get an arrow instead of the
  // swatch: leaving for what this note points at, arriving for what points at
  // it. Same 3px accent bar on every other section, because "declared" and
  // "similar" are symmetric relations and an arrow on them would claim an
  // order the vault never recorded.
  const ARROW = {
    out: "M4 12 L12 4 M6.5 4 L12 4 L12 9.5",   // leaving, up and to the right
    in: "M12 4 L4 12 M9.5 12 L4 12 L4 6.5",    // arriving, down and to the left
  };

  function dirArrow(dir) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "wk-src-ar");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ARROW[dir]);
    svg.appendChild(path);
    return svg;
  }

  function nodeRow(r, opt) {
    const o = opt || {};
    const row = el("div", "wk-src" + (o.action ? " has-act" : "") + (o.dir ? " dir" : ""));
    row.appendChild(o.dir ? dirArrow(o.dir) : el("span", "wk-src-sw" + (o.warn ? " warn" : "")));
    const t = el("button", "wk-srct");
    t.type = "button";
    t.appendChild(el("div", "wk-src-n", r.name || r.path || ""));
    const v = el("div", "wk-src-v");
    if (o.why) v.appendChild(el("span", null, o.why));
    if (v.childElementCount) t.appendChild(v);
    // A row here NAMES a note, so it opens it, on whichever view the drawer is
    // in (ADR-0075): walking the neighbours with details on stays in details.
    // openNote also loads the note's context, so the pane follows the note.
    // A row with no note behind it (a ghost) has nowhere to go, and its offer
    // is the button on the right instead.
    if (r.path && window.openNote) {
      t.addEventListener("click", () => window.openNote(r.path));
    } else {
      t.disabled = true;
      t.style.cursor = "default";
    }
    row.appendChild(t);
    if (o.action) {
      const b = el("button", "wk-mini" + (o.go ? " go" : ""), o.action);
      b.type = "button";
      b.addEventListener("click", () => { if (window.prefillChat) window.prefillChat(o.prompt); });
      row.appendChild(b);
    }
    return row;
  }

  // Five rows say what kind of neighbourhood a note sits in. A hub with sixty
  // backlinks says it in sixty and buries every section under it, which is what
  // this column would have inherited the moment it took the drawer's lists
  // over: the floor lived in cxList, and cxList is what left. <details> owns
  // the tail because the browser already owns that toggle - no open/closed
  // state to keep in JS, and it survives a re-render by not existing across one.
  const NODE_VISIBLE = 5;

  function nodeSection(label, rows, opt, into = body) {
    if (!rows.length) return;
    into.appendChild(sectionHead(label, rows.length));
    const mk = (r) => nodeRow(r, typeof opt === "function" ? opt(r) : opt);
    for (const r of rows.slice(0, NODE_VISIBLE)) into.appendChild(mk(r));
    const rest = rows.slice(NODE_VISIBLE);
    if (!rest.length) return;
    const more = el("details", "wk-more");
    more.appendChild(el("summary", null, rest.length + " more"));
    for (const r of rest) more.appendChild(mk(r));
    into.appendChild(more);
  }

  // The reading order, as a position rather than a label. Two flat lists said
  // "read first" and "unlocks" and could not say the one thing the DAG knows -
  // that a prerequisite has a prerequisite of its own - so a chain three deep
  // read as two unrelated piles. A rung says the same thing with less: -2 is
  // before -1 is before you, and the note you are on is IN the list rather than
  // implied between two headings.
  //
  // Rungs only, never the edges: this column is 310px and the wires between
  // rungs are what the Path surface exists to draw. The footer hands off to it
  // rather than growing a second, smaller drawing of the same DAG here.
  const rungLabel = (n) => (n === 0 ? "●" : (n < 0 ? "−" : "+") + Math.abs(n));

  function ladderSection(d, into = body) {
    const lad = d.ladder;
    if (!lad || !(lad.rungs || []).length) return;
    const count = lad.rungs.reduce((a, r) => a + r.notes.length + r.hidden, 0) + lad.further;
    into.appendChild(sectionHead("Reading order", count));
    for (const rung of lad.rungs) {
      for (const n of rung.notes) {
        const here = n.path === lad.root;
        const row = el("div", "wk-src rung" + (here ? " here" : ""));
        row.appendChild(el("span", "wk-lad-d", rungLabel(rung.depth)));
        const t = el("button", "wk-srct");
        t.type = "button";
        t.appendChild(el("div", "wk-src-n", n.name || n.path));
        const v = el("div", "wk-src-v");
        // "here" earns the only word on this row that is not a folder: without
        // it a spine of five titles gives no clue which one you are standing on.
        // The folder only where it is news: 9 of 13 rows on one note printed
        // the note's own folder, 73 characters of mono that said nothing.
        const fold = folderOf(n.path);
        v.appendChild(el("span", null, here ? "here" : fold !== folderOf(d.path) ? fold : ""));
        t.appendChild(v);
        // The note you are already reading is not a place to go.
        if (here) { t.disabled = true; t.style.cursor = "default"; }
        else t.addEventListener("click", () => { if (window.openNote) window.openNote(n.path); });
        row.appendChild(t);
        into.appendChild(row);
      }
      if (rung.hidden) into.appendChild(el("div", "wk-lad-x", rung.hidden + " more on this rung"));
    }
    // One line, and it is a hand-off, not a summary: everything the drawer
    // decided not to name is named in full one click away.
    const foot = el("button", "wk-lad-f");
    foot.type = "button";
    foot.textContent = (lad.further ? lad.further + " further out · " : "") + "open full ladder";
    foot.addEventListener("click", () => {
      if (window.openLadder) window.openLadder(d.path);
    });
    into.appendChild(foot);
  }

  // The rim paper's panel (ADR-0066 decision 4). Each covering note with the
  // k/K that made it pass: k of the collection's papers, of the K library
  // papers that have the note among their nearest. That is the number a reader
  // tells a systematic association from a topical one by, so it is on the row.
  function outsideSection(o) {
    const textRow = (text) => {
      const row = el("div", "wk-snip");
      row.appendChild(el("div", "wk-snip-t", text));
      return row;
    };
    if (o.covered == null) {
      body.appendChild(sectionHead("Not in this vault", null));
      body.appendChild(textRow("Which notes cover it is not computed yet: the chip above the graph counts the pass."));
    } else if (o.covers.length) {
      nodeSection("Covered by", o.covers, (r) => ({ why: r.k + "/" + r.K + " · " + r.collection }));
    } else {
      body.appendChild(sectionHead("Covered by", 0));
      body.appendChild(textRow("No note here covers it: none of its nearest notes passes its "
        + "collection's test. No evidence of a relation, which is not proof there is none."));
    }
    if (o.collections.length) {
      body.appendChild(sectionHead("Collections", o.collections.length));
      body.appendChild(textRow(o.collections.join(" · ")));
    }
    const abs = o.item && o.item.abstract;
    if (abs) {
      body.appendChild(sectionHead("Abstract", null));
      body.appendChild(textRow(abs));
    }
  }

  // The paper's byline and its three doors out: Zotero, its PDF in Zotero's
  // reader, and the DOI (ADR-0055). Silica embeds no PDF viewer.
  function stripEl(fm) {
    const s = paperStrip(fm);
    const strip = el("div", "wk-paper");
    if (s.byline) strip.appendChild(el("span", "wk-num", s.byline));
    for (const [label, href] of s.links) {
      const a = el("a", "wk-paper-a", label);
      a.href = href;
      if (href.startsWith("http")) { a.target = "_blank"; a.rel = "noopener"; }
      strip.appendChild(a);
    }
    return strip;
  }

  const emptyLine = (into, text) => {
    const e = el("div", "wk-empty");
    e.appendChild(el("div", "wk-empty-d", text));
    into.appendChild(e);
  };

  function saysRows(d, into) {
    if (!d.says.length) return;
    into.appendChild(sectionHead("Says", d.says.length));
    for (const sn of d.says) {
      const row = el("div", "wk-snip");
      if (sn.heading) row.appendChild(el("div", "wk-snip-h", sn.heading));
      row.appendChild(el("div", "wk-snip-t", sn.text || ""));
      into.appendChild(row);
    }
  }

  function conceptPills(d, into) {
    if (!d.concepts.length) return;
    into.appendChild(sectionHead("Concepts", d.concepts.length));
    const pills = el("div", "wk-pills");
    for (const c of d.concepts) {
      const b = el("button", "wk-pill", c.concept);
      b.type = "button";
      b.title = "weight " + (c.weight || 1) + ": light its notes in the graph";
      if (window.isConceptLit && window.isConceptLit(c.concept)) b.classList.add("lit");
      b.addEventListener("click", () => {
        if (window.lightConcept) window.lightConcept(c.concept, b);
      });
      pills.appendChild(b);
    }
    into.appendChild(pills);
  }

  // A note is drawn as its details (ADR-0075): the drawer's header carries its
  // title and the way back to the text. What has no text to read (an
  // unresolved link, a paper on the rim, a missing paper) keeps the head it
  // always had, with its one offer, since the header has nothing to show.
  const isNoteSubject = (d) => !!d.path && !d.ghost && !d.cited && !d.outside;

  function renderNode() {
    const d = projectNode(node, nodeCtx);
    // Empty, not "node": the chip earns its place on the readings that are not
    // a note, and a ghost says what it is in the chip beside it.
    scope.textContent = "";
    head.textContent = d.ghost ? "unresolved" : "";
    head.className = "wk-num";
    body.textContent = "";
    if (isNoteSubject(d)) { renderDetails(d); return; }

    const rh = el("div", "wk-run");
    rh.appendChild(el("div", "wk-run-t", d.title || "node"));
    // A rim paper's record comes from Zotero, not from a leaf: the same strip,
    // built from the item (its one door out is Zotero itself).
    if (d.outside && d.outside.item && window.paperStrip) {
      rh.appendChild(stripEl({ zotero_key: d.outside.key, authors: d.outside.item.authors,
        year: d.outside.item.year, venue: d.outside.item.venue }));
    }
    if (d.count || d.meta.length) {
      const m = el("div", "wk-run-m");
      if (d.count) m.appendChild(el("span", "wk-num strong", d.count));
      for (const bit of d.meta) m.appendChild(el("span", "wk-num", bit));
      rh.appendChild(m);
    }
    const acts = el("div", "wk-acts");
    const ask = el("button", "wk-mini", "open in chat");
    ask.type = "button";
    ask.addEventListener("click", () => {
      if (!window.prefillChat) return;
      window.prefillChat(d.ghost && window.ghostWritePrompt
        ? window.ghostWritePrompt(d.title, d.from)
        : 'Tell me about "' + d.title + '", grounded in what the vault already says.');
    });
    acts.appendChild(ask);
    rh.appendChild(acts);
    body.appendChild(rh);

    if (d.error) { emptyLine(body, d.error); return; }
    if (d.loading) { emptyLine(body, "reading the vault…"); return; }
    if (d.outside) { outsideSection(d.outside); return; }
    if (d.cited) {
      body.appendChild(sectionHead("Cited by your papers, not in your library", null));
      if (d.cited.quote) body.appendChild(el("div", "wk-snip-t", d.cited.quote));
      const row = el("div", "wk-ext");
      const a = el("a", "wk-paper-a", d.cited.href.replace(/^https:\/\//, ""));
      a.href = d.cited.href;
      a.target = "_blank";
      a.rel = "noopener";
      row.appendChild(a);
      row.appendChild(el("span", "wk-num", "add it in Zotero: Add Item by Identifier"));
      body.appendChild(row);
      return;
    }
    // An unresolved link: the notes that already point at it and what they
    // are about, which is all the material there is to write it from.
    conceptPills(d, body);
    nodeSection("Linked from", d.from, (r) => ({ why: folderOf(r.path), dir: "in" }));
    if (!d.concepts.length && !d.from.length) emptyLine(body, "nothing links to it any more");
  }

  // --- details: Content, Links, Place (ADR-0075) -----------------------------
  // The tab chosen last holds for the session; until the reader picks one, a
  // note with a warning opens on Place and every other note on Content.
  let detailsTab = null;
  const TABS = [["content", "Content"], ["links", "Links"], ["place", "Place"]];

  function pickTab(key) {
    detailsTab = key;
    renderNode();
    body.scrollTop = 0;
    const t = body.querySelector('[role="tab"][aria-selected="true"]');
    if (t) t.focus();
  }

  function renderDetails(d) {
    if (d.error) { emptyLine(body, d.error); return; }
    if (d.loading) { emptyLine(body, "reading the vault…"); return; }
    const warn = !!(d.place && d.place.warn);
    const tab = detailsTab || (warn ? "place" : "content");
    const bar = el("div", "dt-tabs");
    bar.setAttribute("role", "tablist");
    bar.setAttribute("aria-label", "details");
    for (const [key, label] of TABS) {
      const on = key === tab;
      const b = el("button", "dt-tab");
      b.type = "button";
      b.id = "dt-tab-" + key;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(on));
      b.setAttribute("aria-controls", "dt-panel");
      b.tabIndex = on ? 0 : -1;
      if (key === "place" && warn) {
        b.appendChild(el("span", "dt-dot"));
        b.appendChild(el("span", "sr-only", "needs attention: "));
      }
      b.appendChild(document.createTextNode(label));
      if (key === "links" && d.neighbours.length) b.appendChild(el("span", "dt-n", String(d.neighbours.length)));
      b.addEventListener("click", () => pickTab(key));
      bar.appendChild(b);
    }
    // The tabs pattern: the arrows walk the tabs and select as they go.
    bar.addEventListener("keydown", (e) => {
      const i = TABS.findIndex(([k]) => k === tab);
      const go = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[e.key];
      if (go === undefined) return;
      e.preventDefault();
      pickTab(TABS[(go + TABS.length) % TABS.length][0]);
    });
    body.appendChild(bar);
    const panel = el("div", "dt-panel");
    panel.id = "dt-panel";
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", "dt-tab-" + tab);
    body.appendChild(panel);
    if (tab === "content") detailsContent(d, panel);
    else if (tab === "links") detailsLinks(d, panel);
    else detailsPlace(d, panel);
  }

  // Content: what the note says and what it is about, as the node pane drew it.
  // Open question in ADR-0075: with the text now the default view, the opening
  // repeats what the reader has just left.
  function detailsContent(d, into) {
    if (d.paper && window.paperStrip) into.appendChild(stripEl(d.paper.fm));
    saysRows(d, into);
    conceptPills(d, into);
    if (d.paper && d.paper.collections.length) {
      into.appendChild(sectionHead("Collections", d.paper.collections.length));
      const pills = el("div", "wk-pills");
      for (const c of d.paper.collections) pills.appendChild(el("span", "wk-pill", c));
      into.appendChild(pills);
    }
    if (!d.says.length && !d.concepts.length) emptyLine(into, "nothing indexed for this note yet. Run /report or /embed to build the index");
  }

  // --- Links: one table, every neighbour once -------------------------------
  const NB_VISIBLE = 12;
  // What a tag says on the row, to a screen reader, and in the legend.
  const TAG_TEXT = { both: "⇄", in: "in", out: "out", "-1": "−1", "+1": "+1", declared: "declared",
                     similar: "~", unwritten: "unwritten", behind: "behind" };
  const TAG_SAY = { both: "linked both ways", in: "links to this note", out: "linked from this note",
                    "-1": "read before it", "+1": "read after it", declared: "declared in the frontmatter, not linked",
                    similar: "similar, not linked", unwritten: "linked from here, never written",
                    behind: "cut off without this note" };
  // One legend entry per family of tags, listed only when a row carries one.
  const LEGEND = [
    [["both"], "⇄ both ways"], [["in", "out"], "in / out one way"], [["-1", "+1"], "±1 reading order"],
    [["declared"], "declared: in its frontmatter only"], [["similar"], "~ similar, not linked"],
    [["unwritten"], "unwritten: linked, never written"], [["behind"], "behind: cut off without this note"],
  ];

  function neighbourRow(r, d) {
    const row = el("div", "nb-row" + (r.behind ? " behind" : ""));
    const go = r.path ? () => window.openNote && window.openNote(r.path)
      : r.tags.includes("unwritten") ? () => window.showNode && window.showNode({ ghost: true, name: r.name })
      : null;
    const n = el(go ? "button" : "div", "nb-n");
    if (go) { n.type = "button"; n.addEventListener("click", go); }
    n.appendChild(el("span", "nb-t", r.name));
    // The folder only where it is news, and "not connected" for a relative the
    // graph cannot reach at all: two notes called Cell stay two notes.
    const fold = folderOf(r.path);
    const meta = [fold && fold !== folderOf(d.path) ? fold : "", r.unreachable ? "not connected" : ""]
      .filter(Boolean).join(" · ");
    if (meta) n.appendChild(el("span", "nb-m", meta));
    row.appendChild(n);
    const tags = el("div", "nb-tags");
    tags.appendChild(el("span", "sr-only", r.tags.map((t) => TAG_SAY[t]).join(", ")));
    for (const t of r.tags) {
      const tg = el("span", "nb-tag" + (t === "behind" ? " w" : ""), TAG_TEXT[t]);
      tg.setAttribute("aria-hidden", "true");
      tags.appendChild(tg);
    }
    // The two offers the old sections carried stay on their rows: they draft a
    // turn and send nothing, so the write still goes through the agent's gate.
    const offer = r.tags.includes("unwritten") ? ["write it", window.writeGhostPrompt && window.writeGhostPrompt(r.name, d.title)]
      : r.tags.includes("similar") ? ["link", window.linkNotesPrompt && window.linkNotesPrompt(d.title, r.name)]
      : null;
    if (offer && offer[1]) {
      const b = el("button", "wk-mini", offer[0]);
      b.type = "button";
      b.addEventListener("click", () => { if (window.prefillChat) window.prefillChat(offer[1]); });
      tags.appendChild(b);
    }
    row.appendChild(tags);
    return row;
  }

  function detailsLinks(d, into) {
    // A paper's own relations first: they are what a paper adds to a note.
    if (d.paper) {
      const p = d.paper;
      nodeSection("Cites, in library", p.cites, () => ({ why: "", dir: "out" }), into);
      nodeSection("Cited by, in library", p.cited_by, () => ({ why: "", dir: "in" }), into);
      if (p.missing.count) {
        into.appendChild(sectionHead("Cited, not in library", p.missing.count));
        for (const m of p.missing.top) {
          const row = el("div", "wk-ext");
          const a = el("a", "wk-paper-a", m.label);
          a.href = m.href;
          a.target = "_blank";
          a.rel = "noopener";
          row.appendChild(a);
          row.appendChild(el("span", "wk-num",
            m.cited_by + (m.cited_by === 1 ? " paper here cites it" : " papers here cite it")));
          into.appendChild(row);
        }
      }
      nodeSection("Notes from this paper", p.notes, (r) => ({ why: folderOf(r.path) }), into);
      if (d.neighbours.length) into.appendChild(sectionHead("Neighbours", d.neighbours.length));
    }
    const rows = d.neighbours;
    if (!rows.length && !d.paper) emptyLine(into, "No links in or out, and nothing similar enough to propose.");
    if (rows.length) {
      const list = el("div", "nb");
      for (const r of rows.slice(0, NB_VISIBLE)) list.appendChild(neighbourRow(r, d));
      const rest = rows.slice(NB_VISIBLE);
      if (rest.length) {
        const more = el("details", "wk-more");
        more.appendChild(el("summary", null, rest.length + " more"));
        for (const r of rest) more.appendChild(neighbourRow(r, d));
        list.appendChild(more);
      }
      into.appendChild(list);
      const used = new Set(rows.flatMap((r) => r.tags));
      const leg = LEGEND.filter(([tags]) => tags.some((t) => used.has(t))).map(([, text]) => text);
      into.appendChild(el("div", "nb-leg", leg.join(" · ")));
    }
    // Under the rows it explains: it is the reason the similar ones are few.
    if (d.hint) into.appendChild(el("div", "wk-hint", d.hint));
  }

  // --- Place: the cut drawn, two measures, the reading order ----------------
  // The figures speak the app's diagram theme (ADR-0069): a hue is an area and
  // never a note, the signal sits on the note being read, names stay at 12-13px
  // and one that would cover another is held back to its dot's tooltip.
  const SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, text) {
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text != null) n.textContent = text;
    return n;
  }

  // Drawn at the width they are shown at, not scaled from a fixed viewBox: a
  // 360-unit drawing stretched to a 568px drawer read its 12px names at 19px.
  // One observer redraws a figure when its column changes width, and forgets
  // it once a render has thrown it away.
  const sized = new Map();
  const sizeObs = typeof ResizeObserver === "function" ? new ResizeObserver((es) => {
    for (const e of es) {
      if (!e.target.isConnected) { sizeObs.unobserve(e.target); sized.delete(e.target); continue; }
      const paint = sized.get(e.target);
      if (paint) paint();
    }
  }) : null;
  function drawSized(host, draw) {
    const paint = () => {
      const W = Math.floor(host.clientWidth);
      if (!W || W === host._w) return;
      host._w = W;
      host.textContent = "";
      draw(W);
    };
    sized.set(host, paint);
    if (sizeObs) sizeObs.observe(host); else requestAnimationFrame(paint);
    return host;
  }

  let measureCtx = null, sansFamily = "";
  function textWidth(t, size, weight) {
    if (!measureCtx) {
      measureCtx = document.createElement("canvas").getContext("2d");
      sansFamily = getComputedStyle(document.body).fontFamily;
    }
    measureCtx.font = weight + " " + size + "px " + sansFamily;
    return measureCtx.measureText(t).width;
  }

  // Names go down in priority order; the first spot that covers no name, no
  // dot and no edge of the figure wins, and a name with none is held back.
  function labeler(svg, W, H) {
    const boxes = [], dots = [];
    const over = (b, list) => list.some((o) => b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1]);
    return {
      block(x, y, r) { dots.push([x - r, y - r, x + r, y + r]); },
      place(text, tries, opt = {}) {
        const size = opt.size || 12, weight = opt.weight || 450, w = textWidth(text, size, weight);
        // A name that must show may cover a dot (never a name) as a last resort.
        const all = opt.loose ? [...tries, ...tries.map((t) => [...t, true])] : tries;
        for (const [x, y, anchor, loose] of all) {
          let x0 = anchor === "end" ? x - w : anchor === "middle" ? x - w / 2 : x;
          // Slid along its row to fit, inside its side of the figure.
          if (opt.clamp) x0 = Math.max((opt.min || 2) + 2, Math.min((opt.max || W - 2) - 2 - w, x0));
          const b = [x0 - 2, y - size * 0.82, x0 + w + 2, y + size * 0.28];
          if (b[0] < (opt.min || 2) || b[2] > (opt.max || W - 2) || b[1] < 0 || b[3] > H
              || over(b, boxes) || (!loose && over(b, dots))) continue;
          boxes.push(b);
          svg.appendChild(svgEl("text", { x: x0.toFixed(1), y: y.toFixed(1),
            class: "pl-t" + (opt.cls ? " " + opt.cls : ""), "font-size": size, "font-weight": weight }, text));
          return true;
        }
        return false;
      },
    };
  }

  const polyPath = (p) => {
    let d = "M" + p[0].toFixed(1) + " " + p[1].toFixed(1);
    for (let i = 2; i < p.length; i += 2) d += "L" + p[i].toFixed(1) + " " + p[i + 1].toFixed(1);
    return d + "Z";
  };

  // The cut as the 2D graph draws it: the notes where their links put them
  // (laid out by the server, structure.cut_layout), each area a level-set blob
  // (ADR-0074), the vault's side dimmed as the graph dims context, and the cut
  // a dashed line through the note, named at its foot. It draws the cut and not
  // the reading order, which ADR-0075 notes against the "rungs only, never the
  // edges" rule above: Path still owns the DAG.
  function cutSvg(b, title, W) {
    const c = b.cut, nodes = c.nodes, me = nodes.findIndex((n) => n.side === "note");
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const padX = 26, padT = 30, padB = 36;
    // px per hop: fit the width, at most 236px tall, and no bigger than 64 so a
    // note with one stranded neighbour is not drawn as two dots a drawer apart.
    const s = Math.min((W - 2 * padX) / Math.max(x1 - x0, 1), 236 / Math.max(y1 - y0, 1), 64);
    const H = Math.max(124, Math.round((y1 - y0) * s + padT + padB));
    const ox = (W - (x1 - x0) * s) / 2, oy = padT + (H - padT - padB - (y1 - y0) * s) / 2;
    const P = nodes.map((n) => [ox + (n.x - x0) * s, oy + (n.y - y0) * s]);
    const [bx, by] = P[me];
    const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
    const gF = svgEl("g"), gL = svgEl("g"), gD = svgEl("g");
    svg.append(gF, gL, gD);
    const hue = (n, e) => {
      const col = c.colors[String(n.area)];
      if (col) e.setAttribute("style", `--ad:${col[0]};--ap:${col[1]}`);
      return e;
    };

    // Blobs: one field per area AND side, the note itself left out so it sits
    // on the line and not inside a ground. Per side, because an area the cut
    // runs through is two grounds: on Backward chaining, one field per area
    // drew Backtracking and Forward chaining in one blob across the line.
    // R is the frame's 0.85 rest lengths with a hop as the rest length, 14-34px.
    const keyOf = (n) => n.area + "|" + n.side;
    const keys = [...new Set(nodes.filter((n, i) => i !== me && n.area >= 0).map(keyOf))];
    const R = Math.max(14, Math.min(34, 0.85 * s));
    const pts = [];
    nodes.forEach((n, i) => { if (i !== me && n.area >= 0) pts.push({ x: P[i][0], y: P[i][1], k: keys.indexOf(keyOf(n)) }); });
    if (typeof blobGeometry === "function") {
      blobGeometry(pts, R, 0.35, R / 8).forEach((g, k) => {
        if (!g.n) return;
        const sample = nodes.find((n) => keyOf(n) === keys[k]);
        const cls = "pl-a" + (sample.side === "rest" ? " ctx" : "");
        gF.appendChild(hue(sample, svgEl("path", { class: cls + " pl-blob",
          d: g.fill.map(polyPath).join("") })));
        gF.appendChild(hue(sample, svgEl("path", { class: cls + " pl-rim",
          d: g.line.map((l) => `M${l[0].toFixed(1)} ${l[1].toFixed(1)}L${l[2].toFixed(1)} ${l[3].toFixed(1)}`).join("") })));
      });
    }
    gF.appendChild(svgEl("line", { x1: bx, y1: 22, x2: bx, y2: H - 26, class: "pl-cutln" }));

    for (const [i, j] of c.edges)
      gL.appendChild(svgEl("line", { x1: P[i][0].toFixed(1), y1: P[i][1].toFixed(1), x2: P[j][0].toFixed(1),
        y2: P[j][1].toFixed(1), class: "pl-ln" + (i === me || j === me ? " on" : "") }));
    // The links a note has past the drawing, as short stubs pointing away from
    // the cut: the vault goes on beyond the frame.
    nodes.forEach((n, i) => {
      if (i === me || !n.stubs) return;
      const a = Math.atan2(P[i][1] - by, P[i][0] - bx);
      for (let t = 0; t < Math.min(3, n.stubs); t++) {
        const u = a + (t - 1) * 0.4;
        gL.appendChild(svgEl("line", { x1: P[i][0].toFixed(1), y1: P[i][1].toFixed(1),
          x2: (P[i][0] + 14 * Math.cos(u)).toFixed(1), y2: (P[i][1] + 14 * Math.sin(u)).toFixed(1), class: "pl-ln stub" }));
      }
    });

    const L = labeler(svg, W, H);
    nodes.forEach((n, i) => {
      if (i === me) return;
      const dot = hue(n, svgEl("circle", { cx: P[i][0].toFixed(1), cy: P[i][1].toFixed(1), r: 3.4,
        class: "pl-a pl-dot" + (n.side === "rest" ? " ctx" : "") }));
      dot.appendChild(svgEl("title", {}, n.name));
      dot.addEventListener("click", () => { if (window.openNote) window.openNote(n.path); });
      gD.appendChild(dot);
      L.block(P[i][0], P[i][1], 4.5);
    });
    // The signal where reading starts, and the graph's load-bearing ring round it.
    gD.appendChild(svgEl("circle", { cx: bx, cy: by, r: 10.5, class: "pl-ring" }));
    gD.appendChild(svgEl("circle", { cx: bx, cy: by, r: 6, class: "pl-me" }));
    L.block(bx, by, 12);

    L.place(title, [[bx, H - 9, "middle"]], { size: 13, weight: 600, cls: "n", clamp: true });
    if (b.rest != null) L.place("← " + countOf(b.rest, "note", "notes"), [[bx - 10, 14, "end"]], { cls: "d" });
    L.place(b.strands + " behind it →", [[bx + 10, 14, "start"]], { cls: "w" });
    // The pieces' heads first, the names a reader knows them by, then nearest first.
    const heads = new Set((b.pieces || []).map((p) => p.head && p.head.path));
    const order = nodes.map((n, i) => i).filter((i) => i !== me)
      .sort((p, q) => heads.has(nodes[q].path) - heads.has(nodes[p].path) || nodes[p].hop - nodes[q].hop);
    // Beside the dot first, then above or below it starting at the dot: a name
    // centred over a dot read as its neighbour's. A name stays on its side of
    // the cut, so the line divides the names as it divides the notes.
    // A piece's head must show: it may slide along its row, step a line
    // further off (a lone stranded note sits level with the ring, which
    // blocks the near rows in a 320px drawer), and at last cover a dot.
    for (const i of order) {
      const [x, y] = P[i], rest = nodes[i].side === "rest", head = heads.has(nodes[i].path);
      const tries = [[x + 7, y + 4, "start"], [x - 7, y + 4, "end"], [x - 3, y - 9, "start"],
        [x + 3, y - 9, "end"], [x - 3, y + 17, "start"], [x + 3, y + 17, "end"]];
      if (head) tries.push([x - 3, y - 22, "start"], [x - 3, y + 30, "start"]);
      L.place(nodes[i].name, tries, rest ? { cls: "d", max: bx - 6 } : { min: bx + 6, loose: head, clamp: head });
    }
    return svg;
  }

  function cutFigure(b, title) {
    const host = el("div", "pl-cutfig");
    host.setAttribute("role", "img");
    host.setAttribute("aria-label", title + " is the only way between "
      + (b.rest != null ? "the other " + countOf(b.rest, "note", "notes") : "its part of the vault")
      + " and " + countOf(b.strands, "note", "notes") + " behind it, in "
      + countOf(b.pieces.length, "piece", "pieces") + ".");
    return drawSized(host, (W) => host.appendChild(cutSvg(b, title, W)));
  }

  function noteButton(p) {
    const b = el("button", "pl-nm", noteName(p));
    b.type = "button";
    b.addEventListener("click", () => { if (window.openNote) window.openNote(p); });
    return b;
  }

  function bridgeBlock(b, d) {
    const fig = el("figure", "pl-fig");
    // No layout (an older index, or the layout failed): the sentence and the
    // names still say it, and a picture of nothing would not.
    if (b.cut && b.cut.nodes && b.cut.nodes.length > 1) fig.appendChild(cutFigure(b, d.title));
    const cap = el("figcaption", "pl-cap");
    cap.appendChild(el("b", null, b.title + "."));
    cap.appendChild(document.createTextNode(" " + b.why));
    fig.appendChild(cap);
    // Named, because "13" is not something a reader can act on and the names
    // are: the notes that lose every path to the rest without this one.
    if (b.behind.length) {
      const names = el("div", "pl-names");
      names.appendChild(el("span", "pl-k", "Behind it"));
      for (const p of b.behind.slice(0, 3)) names.appendChild(noteButton(p));
      if (b.behind.length > 3) {
        const more = el("details", "pl-more");
        more.appendChild(el("summary", null, (b.behind.length - 3) + " more"));
        for (const p of b.behind.slice(3)) more.appendChild(noteButton(p));
        names.appendChild(more);
      }
      fig.appendChild(names);
    }
    fig.appendChild(el("div", "pl-term", "graph term: " + b.term));
    return fig;
  }

  // The two measures under the drawing follow rule 12 of ADR-0069: marks in
  // neutral ink, the signal on the one mark the reading is about.

  // How many notes sit at each depth, this note's column lit. It replaced
  // nested rings, which drew the scale and none of the vault on it.
  function depthChart(dp) {
    const host = el("div", "pl-chart");
    host.setAttribute("role", "img");
    host.setAttribute("aria-label", "Notes by mesh depth: " + dp.hist.map((v, k) => v + " at " + k).join(", ")
      + ". This note is at " + dp.k + ".");
    const top = Math.max(1, ...dp.hist), gap = 6, H = 86;
    return drawSized(host, (W) => {
      const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
      const bw = (W - gap * (dp.hist.length - 1)) / dp.hist.length;
      dp.hist.forEach((v, k) => {
        const h = Math.max(2, (v / top) * 60), x = k * (bw + gap), on = k === dp.k ? " on" : "";
        svg.appendChild(svgEl("rect", { x: x.toFixed(1), y: (66 - h).toFixed(1), width: bw.toFixed(1),
          height: h.toFixed(1), rx: 2, class: "pl-bar" + on }));
        svg.appendChild(svgEl("text", { x: (x + bw / 2).toFixed(1), y: 82, "text-anchor": "middle",
          class: "pl-ax" + on }, String(k)));
      });
      host.appendChild(svg);
    });
  }

  // Two ranks on one 0-100 axis: what it is linked like, and what it is
  // crossed like. The gap between them is the reading; the rank the reading
  // is about carries the signal, the other is a hollow mark.
  function routeChart(rt) {
    const host = el("div", "pl-chart");
    host.setAttribute("role", "img");
    host.setAttribute("aria-label", "Links: more than " + rt.links + "% of notes. Paths through it: more than "
      + rt.paths + "%.");
    const H = 80, y = 40;
    return drawSized(host, (W) => {
      const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
      const a0 = 6, a1 = W - 6, X = (p) => a0 + ((a1 - a0) * p) / 100;
      svg.appendChild(svgEl("line", { x1: a0, y1: y, x2: a1, y2: y, class: "pl-axis" }));
      for (const p of [0, 50, 100]) svg.appendChild(svgEl("line", { x1: X(p), y1: y - 4, x2: X(p), y2: y + 4, class: "pl-axis" }));
      svg.appendChild(svgEl("line", { x1: X(rt.links), y1: y, x2: X(rt.paths), y2: y, class: "pl-span" }));
      // Anchored by where it sits, so 3% and 97% both stay inside the column.
      const anchor = (p) => (p > 80 ? "end" : p < 20 ? "start" : "middle");
      for (const [word, v] of [["links", rt.links], ["paths", rt.paths]]) {
        const focal = word === rt.focus;
        svg.appendChild(svgEl("circle", { cx: X(v), cy: y, r: focal ? 5.5 : 5, class: focal ? "pl-me" : "pl-hollow" }));
        svg.appendChild(svgEl("text", { x: X(v), y: focal ? y - 13 : y + 22, "text-anchor": anchor(v),
          class: "pl-t" + (focal ? " on" : "") }, word + " " + v));
      }
      svg.appendChild(svgEl("text", { x: a0, y: H - 2, class: "pl-ax" }, "0"));
      svg.appendChild(svgEl("text", { x: a1, y: H - 2, "text-anchor": "end", class: "pl-ax" }, "100"));
      host.appendChild(svg);
    });
  }

  function measure(title, chart, why, term) {
    const t = el("div", "pl-m");
    t.appendChild(el("div", "pl-k", title));
    t.appendChild(chart);
    t.appendChild(el("div", "pl-v", why));
    t.appendChild(el("div", "pl-term", "graph term: " + term));
    return t;
  }

  function detailsPlace(d, into) {
    const pl = d.place;
    if (!pl) {
      emptyLine(into, "Not in the graph yet: /report builds it.");
    } else {
      if (pl.bridge) into.appendChild(bridgeBlock(pl.bridge, d));
      const dp = pl.depth, rt = pl.routing;
      const ms = [
        dp && measure(dp.title, dp.hist.length ? depthChart(dp) : el("span"), dp.why + (dp.scale ? " " + dp.scale : ""), dp.term),
        rt && measure(rt.title, routeChart(rt), rt.why, rt.term),
      ].filter(Boolean);
      if (ms.length) {
        // Side by side under a rule, no tiles: a figure sits on the sheet.
        const g = el("div", "pl-two" + (ms.length === 1 ? " one" : ""));
        for (const m of ms) g.appendChild(m);
        into.appendChild(g);
      }
      if (!pl.bridge && !ms.length) {
        into.appendChild(el("p", "pl-plain", "Nothing stands out about its place: it bridges nothing, "
          + "its group is loose, and paths cross it about as often as its links predict."));
      }
    }
    // The one direction the vault knows, under where the note sits.
    ladderSection(d, into);
  }

  // The run, in the rail. No view gate and no competition: this compartment has
  // one subject and it is the same on all four views.
  // The "this chat" compartment (audit P17): the run's outcome and receipt,
  // the writes that did NOT land (the ones that did are the ledger rows above
  // this, painted by app-shell.js from /changes), the reads behind "show",
  // and the run's own steps behind "steps". The live step list is not drawn
  // while the transcript is on screen: the run line at the composer names the
  // current step, and the transcript already carries every row.
  let stepsOpen = false, readsOpen = false; // survive the 500ms re-render

  function renderRun(state) {
    runState.textContent = state;
    runState.className = "wk-state" + (run.running ? " live" : "");
    runBody.textContent = "";
    if (!run.rows.length) return; // the head line above says "nothing changed yet"

    // The outcome, then the receipt: elapsed freezes here at done (audit P1).
    const rh = el("div", "wk-receipt");
    rh.appendChild(el("span", "wk-num", (run.running ? "running" : "done") + (run.elapsed ? " · " + run.elapsed : "")));
    if (run.shape) rh.appendChild(el("span", "wk-num", run.shape));
    if (run.model) rh.appendChild(el("span", "wk-num", run.model.split("/").pop()));
    runBody.appendChild(rh);

    for (const w of run.writes.filter((x) => x.failed)) runBody.appendChild(writeCardEl(w));

    if (run.sources.length) {
      const n = run.sources.length;
      const row = el("div", "wk-fold");
      const btn = el("button", "linklike", (readsOpen ? "hide" : "show"));
      btn.type = "button";
      btn.setAttribute("aria-expanded", String(readsOpen));
      row.appendChild(el("span", "wk-fold-t", `${n} note${n === 1 ? "" : "s"} read`));
      row.appendChild(btn);
      btn.addEventListener("click", () => { readsOpen = !readsOpen; render(); });
      runBody.appendChild(row);
      if (readsOpen) for (const s of run.sources) runBody.appendChild(sourceRow(s));
    }

    const row = el("div", "wk-fold");
    const btn = el("button", "linklike", stepsOpen ? "hide steps" : "steps");
    btn.type = "button";
    btn.setAttribute("aria-expanded", String(stepsOpen));
    row.appendChild(el("span", "wk-fold-t", `${run.rows.length} step${run.rows.length === 1 ? "" : "s"}`));
    row.appendChild(btn);
    btn.addEventListener("click", () => { stepsOpen = !stepsOpen; render(); });
    runBody.appendChild(row);
    if (stepsOpen) {
      const tl = el("div", "wk-tl" + (run.running ? " live" : ""));
      for (const r of run.rows) tl.appendChild(beatRow(r));
      runBody.appendChild(tl);
      paintSpine(tl);
    }
  }

  // The drawer's details, and node mode. A note is drawn on every view now
  // (ADR-0075): it is the note open in the drawer, read from Chat as much as
  // from Explore, and its details are one button away wherever it was opened.
  // The three graph selections keep their gate: a bridge or a set of concepts
  // picked on Explore is not a subject on a view that cannot show it.
  function renderCtx() {
    if (view === "graph" && compare) { renderCompare(); return; }
    if (view === "graph" && concepts) { renderConcepts(); return; }
    if (view === "graph" && bridge) { renderBridge(); return; }
    if (node) { renderNode(); return; }
    scope.textContent = "";
    head.textContent = "";
    body.textContent = "";
    const e = el("div", "wk-empty");
    e.appendChild(el("div", "wk-empty-t", "Nothing selected"));
    e.appendChild(el("div", "wk-empty-d",
      "Open a note and its info button shows its links and its place in the vault. "
      + "A bridge, a set of concepts or a comparison picked on Explore lands here."));
    body.appendChild(e);
  }

  // A bridge picked on the Areas surface: the pair, why it counts, the
  // sentence that joins it. The two notes are one click from the node view,
  // the pair's spoke one click back on Areas (ADR-0060). A row is over the
  // areas or over the zones; `other` is the other partition's verdict.
  function renderBridge() {
    const r = bridge;
    const zones = r.partition === "zones";
    const unit = zones ? "zones" : "areas";
    // The chip, not the head: a bridge is the reading that is NOT a node,
    // which is the one case the chip exists for.
    scope.textContent = "bridge";
    head.textContent = "";
    head.className = "wk-num";
    body.textContent = "";
    const rh = el("div", "wk-run");
    rh.appendChild(el("div", "wk-run-t", r.a.name + " ↔ " + r.b.name));
    const areas = el("div", "wk-run-m");
    areas.appendChild(el("span", "wk-num", r.a.part + " ↔ " + r.b.part));
    rh.appendChild(areas);
    const why = el("div", "wk-run-m");
    why.appendChild(el("span", "wk-num strong", r.links_between === 1
      ? "the only link between these two " + unit
      : "one of " + r.links_between + " links between these two " + unit));
    if (r.other) why.appendChild(el("span", "wk-num", zones
      ? (r.other === "same" ? "same area" : "different areas")
      : (r.other === "same" ? "same semantic zone" : "different semantic zones")));
    if (r.scaffold) why.appendChild(el("span", "wk-num", "frontmatter"));
    rh.appendChild(why);
    body.appendChild(rh);
    body.appendChild(el("p", "brg-quote" + (r.passage ? "" : " none"), r.passage
      || (r.scaffold ? "Listed in the frontmatter of " + r.a.name + ": no sentence carries this link."
                     : "No sentence in " + r.a.name + " quotes this link.")));
    const acts = el("div", "wk-acts");
    for (const end of [r.a, r.b]) {
      const b = el("button", "wk-mini", end.name);
      b.title = "open this note";
      b.addEventListener("click", () => { if (window.showNode) window.showNode({ path: end.path }); });
      acts.appendChild(b);
    }
    if (window.openBridges) {
      const l = el("button", "wk-mini", "this pair on Areas");
      l.addEventListener("click", () => window.openBridges(r.a.part_id, r.b.part_id, r.partition));
      acts.appendChild(l);
    }
    body.appendChild(acts);
  }

  // The concepts lit together: the reading InfraNodus calls "read the passages".
  // A chip per concept (click drops it), then every note carrying all of them
  // with the lines that carry each, and the note itself one click away.
  function renderConcepts() {
    const d = concepts;
    scope.textContent = "concepts";
    head.textContent = "";
    head.className = "wk-num";
    body.textContent = "";
    const chips = el("div", "wk-pills");
    for (const t of d.terms) {
      const b = el("button", "wk-pill lit", t.concept + " · " + t.notes);
      b.type = "button";
      b.title = t.notes + " notes carry it: click to drop it from the selection";
      b.addEventListener("click", () => { if (window.lightConcept) window.lightConcept(t.term, null); });
      chips.appendChild(b);
    }
    body.appendChild(chips);
    const sum = el("div", "wk-run-m");
    sum.appendChild(el("span", "wk-num strong", d.notes.length
      ? (d.terms.length > 1 ? d.notes.length + " notes carry all " + d.terms.length : d.notes.length + " notes carry it")
      : "no note carries all " + d.terms.length + " together"));
    body.appendChild(sum);
    for (const n of d.notes) {
      const rh = el("div", "wk-run");
      const t = el("button", "wk-mini", n.name);
      t.title = "open this note";
      t.addEventListener("click", () => { if (window.showNode) window.showNode({ path: n.path }); });
      rh.appendChild(t);
      for (const q of n.passages) {
        const line = el("p", "brg-quote");
        line.appendChild(el("b", "", q.terms.join(" + ") + " · "));
        line.appendChild(document.createTextNode(q.text));
        rh.appendChild(line);
      }
      if (!n.passages.length) rh.appendChild(el("p", "brg-quote none", "carried in the index, no line quotes it"));
      body.appendChild(rh);
    }
  }

  // Two parts side by side: what each says (terms rated against the other),
  // then what puts them in touch, every link with its sentence.
  function renderCompare() {
    const d = compare;
    scope.textContent = "compare";
    head.textContent = "";
    head.className = "wk-num";
    body.textContent = "";
    const rh = el("div", "wk-run");
    rh.appendChild(el("div", "wk-run-t", d.a.label + " ↔ " + d.b.label));
    const m = el("div", "wk-run-m");
    m.appendChild(el("span", "wk-num", d.a.size + " notes ↔ " + d.b.size + " notes"));
    rh.appendChild(m);
    body.appendChild(rh);
    for (const side of [d.a, d.b]) {
      body.appendChild(sectionHead(side.label + " says", side.terms.length));
      const pills = el("div", "wk-pills");
      for (const t of side.terms) {
        const b = el("button", "wk-pill", t);
        b.type = "button";
        b.title = "light the notes that carry it";
        b.addEventListener("click", () => { if (window.lightConcept) window.lightConcept(t, b); });
        pills.appendChild(b);
      }
      if (!side.terms.length) pills.appendChild(el("span", "wk-num", "no term stands out"));
      body.appendChild(pills);
    }
    body.appendChild(sectionHead("In touch", d.links.length + d.knn.length));
    const why = el("div", "wk-run-m");
    why.appendChild(el("span", "wk-num strong", d.links.length + " wikilinks cross · " + d.knn.length + " nearest-neighbour pairs"));
    body.appendChild(why);
    for (const l of d.links) {
      const r = el("div", "wk-run");
      const acts = el("div", "wk-acts");
      for (const end of [l.a, l.b]) {
        const b = el("button", "wk-mini", end.name);
        b.addEventListener("click", () => { if (window.showNode) window.showNode({ path: end.path }); });
        acts.appendChild(b);
      }
      r.appendChild(acts);
      r.appendChild(el("p", "brg-quote" + (l.passage ? "" : " none"), l.passage || "No sentence quotes this link."));
      body.appendChild(r);
    }
    for (const k of d.knn.slice(0, 12)) {
      const r = el("div", "wk-run-m");
      r.appendChild(el("span", "wk-num", k.a.name + " ~ " + k.b.name));
      body.appendChild(r);
    }
  }

  // The run alone: the beats and the 500ms tick touch nothing else. The pane
  // beside it used to be rebuilt on every beat, which took the keyboard's
  // focus off a details tab twice a second while a turn was running.
  function renderRunSide() {
    run = projectRun(beats);
    const state = run.running ? "live" : (run.rows.length ? "done" : "idle");
    paintRunLine(run);
    renderRun(state);
  }

  function render() {
    renderRunSide();
    renderCtx();
  }

  // A running turn has to tick, or the elapsed reads as a clock that stopped
  // between beats — and a distill chunk is nine seconds of no beats at all.
  function retick() {
    if (tick) { clearInterval(tick); tick = null; }
    if (run && run.running) tick = setInterval(renderRunSide, 500);
  }

  function ingest(payload) {
    const incoming = payload.beats || [];
    if (!incoming.length) return;
    const bsid = payload.sid || incoming[0].sid;
    // /reset opens a new narration session on the same stream. Adopt it and drop
    // the old run rather than interleaving two conversations in one panel.
    if (bsid && curSid && bsid !== curSid) beats = [];
    if (bsid) curSid = bsid;
    for (const b of incoming) {
      if (!beats.length || b.seq > beats[beats.length - 1].seq) beats.push(b);
    }
    renderRunSide();
    retick();
  }

  // A session switch (History, or the wordmark's new chat) replays nothing onto
  // the bus, so the live stream cannot see it. /narration is the snapshot of
  // whichever session is current now; the stream keeps appending to it.
  async function refresh() {
    beats = []; curSid = null; render(); retick();
    try {
      const d = await (await fetch("/narration")).json();
      ingest(d);
    } catch { /* the next beat fills it; a snapshot that fails is not an error to report */ }
  }
  document.addEventListener("silica:session", refresh);
  document.addEventListener("silica:view", (e) => { view = e.detail; render(); });
  // The graph hands over what it drew (the node) and app.js the round trip that
  // followed (its context). Both arrive here as one event, twice: once bare so
  // the head paints on the click, once filled.
  document.addEventListener("silica:node", (e) => {
    const d = e.detail;
    node = d ? d.node : null;
    nodeCtx = d ? d.context : null;
    bridge = null;   // a node, or a background click, takes the pair's place
    concepts = null; compare = null;
    // Which view of the drawer shows it is app-notes.js's to decide (ADR-0075):
    // a note opens on its text or its details, a subject with no text on this
    // pane, and only the opener knows which of the two it was handed.
    renderCtx();
  });
  // One fetch, two surfaces: app.js loads /metrics for the view and hands the
  // same payload here rather than this file measuring the vault a second time.
  document.addEventListener("silica:bridge", (e) => {
    bridge = e.detail; node = null; nodeCtx = null; concepts = null; compare = null;
    render();
  });
  document.addEventListener("silica:concepts", (e) => {
    concepts = e.detail; if (concepts) { node = null; nodeCtx = null; bridge = null; compare = null; }
    render();
  });
  document.addEventListener("silica:compare", (e) => {
    compare = e.detail; if (compare) { node = null; nodeCtx = null; bridge = null; concepts = null; }
    render();
  });

  let es = null;
  function connect() {
    if (es) es.close();
    es = new EventSource("/narration/sse");
    // The SSE id: field is the seq, so a dropped connection resumes from
    // Last-Event-ID with no cursor of our own.
    es.addEventListener("beats", (e) => {
      try { ingest(JSON.parse(e.data)); } catch { /* one malformed frame is not the stream */ }
    });
    // A conversion job's transitions ride this stream (ADR-0099 point 5), and
    // each (re)connect is when app-jobs.js reads back what it may have missed.
    es.addEventListener("job", (e) => {
      try { document.dispatchEvent(new CustomEvent("silica:job", { detail: JSON.parse(e.data) })); } catch { /* one malformed frame is not the stream */ }
    });
    es.addEventListener("open", () => document.dispatchEvent(new CustomEvent("silica:stream-open")));
    es.onerror = () => { /* EventSource retries by itself; a closed panel is not an error to report */ };
  }

  render();
  connect();
})();
