// Vanilla client: POST /chat returns text/event-stream, read incrementally via
// the body's ReadableStream (not EventSource — that only does GET).
const $ = (s) => document.querySelector(s);
const log = $("#log");
const input = $("#input");
const stopBtn = $("#stop");

let streaming = false;
let activeTab = "chat";

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
  log.scrollTop = log.scrollHeight;
  return el.querySelector(".body");
}

// One vault change as an object that stays in the transcript: what happened, to
// which note, and the way back out of it. `effect` is written | moved | deleted |
// failed. The card owns its own revert, so you undo the write you are looking at
// rather than the whole turn.
const WRITE_COPY = {
  written: { label: "written", act: "revert", hint: "restore this note to its state before the turn" },
  moved: { label: "moved", act: "revert", hint: "move this note back" },
  deleted: { label: "deleted", act: "restore", hint: "bring this note back" },
  failed: { label: "not written", act: null, hint: "" },
};

function writeCard(ref, effect, verb) {
  const copy = WRITE_COPY[effect] || WRITE_COPY.written;
  const card = document.createElement("div");
  card.className = "wcard " + effect;
  const op = document.createElement("span");
  op.className = "wc-op";
  op.textContent = copy.label;
  const path = document.createElement("span");
  // A deleted note has no page to open, and a failed write may have created
  // nothing at all: keep the path as a record, drop the click, or it routes to
  // /note and answers "not found in vault".
  // `wc-open`, not `note-link`: a card's path is the thing that changed, not a
  // citation, and borrowing the chip's class also borrowed its cyan underline —
  // which `:is(.msg, #note-body, …) .note-link` carries at ID specificity, so no
  // amount of class stacking could take it back off. The delegated open-note
  // handler matches both classes.
  const openable = effect !== "deleted" && effect !== "failed";
  path.className = "wc-path" + (openable ? " wc-open" : "");
  if (openable) path.dataset.path = ref;
  path.textContent = ref;
  path.title = ref;
  card.append(op, path);
  if (!copy.act) {
    const n = document.createElement("span");
    n.className = "wc-note";
    n.textContent = verb ? `${verb} failed · vault unchanged` : "vault unchanged";
    card.appendChild(n);
    return card;
  }
  const b = document.createElement("button");
  b.type = "button";
  b.className = "wc-act";
  b.textContent = copy.act;
  b.title = copy.hint;
  b.addEventListener("click", () => {
    b.disabled = true;
    card.classList.add("reverting");
    send("/undo " + ref);
  });
  card.appendChild(b);
  return card;
}

// Raw exception text used to land in the transcript as the agent's own speech:
// "HTTPError 502 … (request id 4f2a-9c11-bd03)". Say what happened and what to do
// about it, in the product's own language. The original text is never discarded —
// it stays on the element's title, because the person debugging this needs it.
const ERROR_PLAIN = [
  [/\b(50[0-9]|timeout|timed out|connection|ECONNREFUSED|unreachable)\b/i,
    "the model endpoint didn't answer. Try again, or check the endpoint under the model button."],
  [/\b(401|403|unauthorized|forbidden|api[_ -]?key)\b/i,
    "the provider rejected the credentials. Check the API key for this endpoint."],
  [/\b(429|rate limit)\b/i, "the provider is rate-limiting. Wait a moment and try again."],
  [/lint failed/i, "the write was rolled back because the note would have broken a vault rule. The vault is unchanged."],
  [/not found in vault|no such note/i, "that note isn't in the vault under that name."],
  [/context length|too many tokens|max_tokens/i,
    "the conversation outgrew the model's context. Start a new chat, or narrow the question."],
];

function plainError(raw) {
  const s = String(raw || "");
  for (const [re, msg] of ERROR_PLAIN) if (re.test(s)) return msg;
  return s;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
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
  bodyEl.appendChild(b);
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
  const wiki = (t) =>
    t.replace(/\[\[([^\]|\n]+?)(?:\|([^\]\n]+?))?\]\]/g, (_m, target, alias) => {
      const path = target.split("#")[0].trim();
      const shown = (alias || path.split("/").pop().replace(/\.md$/, "")).trim();
      return `<a class="note-link" data-path="${path.replace(/"/g, "&quot;")}">${shown}</a>`;
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
  // in `href="…"` and nests the anchors. The lookbehind keeps it off the target of
  // a `[[…]]` that wiki() already turned into an attribute.
  const LINK = /\[([^\]]+)\]\(([^)\s]+)\)|(?<![\w"'=@./-])(https?:\/\/[^\s<>"'`]+)/g;
  const inline = (t) => {
    // Code spans are parked as placeholders for the rest of the pass: emphasis and
    // links used to be applied INSIDE the <code> they had already produced, so
    // `` `https://x` `` would have come out of this change as a live anchor.
    const code = [];
    return wiki(esc(t))
      .replace(/`([^`]+)`/g, (_m, c) => `\u0000${code.push(c) - 1}\u0000`)
      .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+?)\*/g, "<em>$1</em>")
      .replace(LINK, (_m, txt, target, bare) => {
        if (bare === undefined) {
          const h = safeHref(target);
          return h ? `<a href="${h.replace(/"/g, "&quot;")}">${txt}</a>` : txt;
        }
        const u = trimUrl(bare);
        return `<a href="${u.replace(/"/g, "&quot;")}">${u}</a>` + bare.slice(u.length);
      })
      .replace(/\u0000(\d+)\u0000/g, (_m, i) => `<code>${code[i]}</code>`);
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
    // ponytail: no escaped `\|` inside a cell, no per-column alignment. Both are
    // in the server render; add here if a live table ever needs them.
    if (/^\s*\|/.test(line) && DELIM.test(lines[i + 1] || "")) {
      closeList();
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      const tr = (cs, tag) => `<tr>${cs.map((c) => `<${tag}>${inline(c)}</${tag}>`).join("")}</tr>`;
      out.push(
        `<table><thead>${tr(head, "th")}</thead><tbody>${rows.map((r) => tr(r, "td")).join("")}</tbody></table>`
      );
      continue;
    }
    if (/^```/.test(line)) {
      closeList();
      const buf = []; i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // closing fence (or EOF while still streaming)
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
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

function fmtTokens(n) {
  n = Number(n) || 0;
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}
function setCtxTokens(used, max) {
  max = Number(max) || 0;
  $("#ctx-tokens").textContent = max ? `CTX ${fmtTokens(used)}/${fmtTokens(max)}` : "";
}

// Both send buttons go dead for the length of a turn: the server answers one at
// a time (409 otherwise), and an enabled-looking button that discards the click
// is worse than a disabled one.
function setSendDisabled(v) {
  $("#send").disabled = v;
  $("#dock-send").disabled = v;
  // Both edges of a turn in one place: an open settings panel locks itself while
  // a response runs and unlocks when it ends, rather than waiting for a 409.
  stSetBusy(v);
}

// `retry` re-runs the exact same turn. It is a callback rather than the text so
// that /find, the dock and a nucleate-and-ask all retry the thing THEY sent.
async function runTurn(fetchPromise, pendingLabel = "working", retry = null) {
  if (streaming) return;
  streaming = true;
  stopBtn.hidden = false;
  setSendDisabled(true);
  announce("silica is responding");
  const body = bubble("silica");
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
    if (keep !== "text") curText = null;
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
    flowMsg("error: " + e);
    peekError(String(e));
    notify("the turn failed: " + e);
  } finally {
    streaming = false;
    stopBtn.hidden = true;
    setSendDisabled(false);
    pending.remove(); // no-op if the first event already dropped it
    caret.remove(); // no-op if a rerender already detached it
    freezePeek(); // done or aborted — stop mirroring, keep the preview up
    if (curThink) curThink.details.open = false; // aborted mid-thought — still collapse
    // A mutation is an object, a read is a citation, and they no longer share a
    // row of identical chips. The product's whole claim is that a write to your
    // vault is safe to delegate, and a write used to announce itself as a 12px
    // chip with a small outlined button beside it — the least prominent thing in
    // its own footer. Each one now gets a card it can carry a state on, and its
    // own revert, so you undo the write you are looking at rather than the turn.
    const mutations = [...touched].filter(([, e]) => e !== "read");
    if (mutations.length || failed.size) {
      const w = document.createElement("div");
      w.className = "writes";
      for (const [ref, effect] of mutations) w.appendChild(writeCard(ref, effect, null));
      for (const [ref, verb] of failed) w.appendChild(writeCard(ref, "failed", verb));
      // One button for the whole turn stays, but only when there is more than one
      // card: with a single write, per-card revert already says it better.
      if (mutations.length > 1) {
        const u = document.createElement("button");
        u.type = "button";
        u.className = "undo-turn";
        u.textContent = `revert all ${mutations.length} changes`;
        u.title = "run /undo for every note this turn touched";
        u.addEventListener("click", () => { u.disabled = true; send("/undo"); });
        w.appendChild(u);
      }
      flow.appendChild(w);
    }
    const reads = [...touched].filter(([, e]) => e === "read").map(([r]) => r);
    if (reads.length) {
      const s = document.createElement("div");
      s.className = "sources";
      const g = document.createElement("div");
      g.className = "sgroup read";
      g.innerHTML = '<span class="sources-label">read</span>';
      for (const ref of reads) {
        const c = document.createElement("span");
        c.className = "note-link";
        c.dataset.path = ref; // delegated click → note drawer
        c.textContent = ref.split("/").pop().replace(/\.md$/, "");
        g.appendChild(c);
      }
      s.appendChild(g);
      flow.appendChild(s);
    }
    const answer = texts.map((t) => t.raw).join("\n\n").trim();
    if (answer) addCopyBtn(body, () => answer);
    loadSessions(); // turn saved server-side — refresh titles/order
    loadVaultInfo(); // a turn may have written notes — refresh stats + tree
    loadChanges();   // …and the sidebar's record of what it changed
    graphStale = true; // a turn may have written notes — rebuild next graph view
    metricsStale = true; // …and remeasure the next time the metrics tab opens
    // The one place shape is dropped: the other two `graphStale` sites are a
    // theme flip and a render setting, and neither moves a note. folders/areas/
    // read take their colours from tokens, so they survive both untouched.
    shapeData = null;
  }

  function handle(ev) {
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
      const seg = textSeg();
      seg.raw += ev.text;
      seg.el.innerHTML = mdLite(seg.raw);
      (seg.el.lastElementChild || seg.el).appendChild(caret); // inline at the text tail
      peekDelta(ev.text);
    } else if (ev.type === "tool_start") {
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
      const t = document.createElement("div");
      t.className = "tool";
      t.dataset.label = toolLabel(ev);
      t.textContent = "» " + t.dataset.label + " …";
      toolsGroup().appendChild(t);
      curTools.appendChild(caret);
      toolEls[ev.id] = t;
      claimed[ev.id] = { refs: ev.notes || [], effect: ev.effect || "read", verb: ev.name };
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
      if (t) { t.className = "tool done"; t.textContent = "✓ " + (t.dataset.label || ev.name); }
      const c = claimed[ev.id];
      if (c) {
        // A mutation always wins over a read of the same note; a read never
        // downgrades a write recorded earlier in the turn.
        for (const r of c.refs) if (c.effect !== "read" || !touched.has(r)) touched.set(r, c.effect);
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
        t.className = "tool error";
        t.textContent = "✗ " + (t.dataset.label || ev.name) + " · " + plainError(ev.error);
        t.title = ev.error; // the raw text stays reachable for whoever is debugging
      }
      const f = claimed[ev.id];
      // Still not claimed as written — but now recorded as a mutation that did
      // NOT land, so the turn can say so in the footer instead of going quiet.
      if (f && f.effect !== "read") for (const r of f.refs) if (!touched.has(r)) failed.set(r, f.verb || "write");
      delete claimed[ev.id]; // it failed: do not claim its notes
    } else if (ev.type === "batch") {
      const t = document.createElement("div");
      t.className = "tool";
      t.textContent = "» " + ev.kind + " · " + ev.label;
      toolsGroup().appendChild(t);
      curTools.appendChild(caret);
    } else if (ev.type === "done") {
      // Uninterrupted answer (no tool split the text) → upgrade the live md to the
      // canonical server render (wikilinks, callouts, mermaid). Interleaved turns
      // keep their live segments; they render canonically on the next reload.
      if (segments === 0 && (ev.html || ev.answer)) {
        const seg = textSeg();
        seg.raw = ev.answer || ""; // keep the copy button fed on no-delta turns
        seg.el.innerHTML = ev.html || escapeHtml(ev.answer || "");
      } else if (segments === 1 && curText && (ev.html || ev.answer)) {
        curText.el.innerHTML = ev.html || escapeHtml(ev.answer || "");
      }
      close(""); // collapse any open thinking, end all segments
      if (ev.hint) {
        // Informational only: every recall call this turn came back empty. It
        // arms nothing — /web works the same with or without it.
        const h = document.createElement("div");
        h.className = "turn-hint";
        h.textContent = ev.hint;
        flow.appendChild(h);
      }
      setCtxTokens(ev.context_tokens, ev.max_context_tokens);
      peekDone(ev); // card gets the canonical OFM render
      announce("response ready");
    } else if (ev.type === "error") {
      close("");
      peekError(plainError(ev.error));
      // The error belongs where it happened, not in a corner: it used to render
      // as a raw exception line attributed to the agent as speech AND as a toast
      // over the send button, and neither offered a way to try again — so a
      // failed turn ended the conversation and took the typed question with it.
      const box = document.createElement("div");
      box.className = "turn-error";
      const msg = document.createElement("div");
      msg.className = "te-msg";
      msg.textContent = plainError(ev.error);
      msg.title = ev.error; // raw text stays reachable
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
      flow.appendChild(box);
      announce("the turn failed: " + plainError(ev.error));
    }
    log.scrollTop = log.scrollHeight;
  }
}

function send(text, replay = false) {
  if (!text.trim() || streaming) return;
  // A retry re-runs the turn but must not stack a second copy of your question
  // in the transcript: the bubble from the first attempt is still there.
  if (!replay) bubble("user").textContent = text;
  const find = text.trim().match(/^\/find\s*(.*)$/);
  if (find) { runFind(find[1]); return; }
  runTurn(fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }), "working", () => send(text, true));
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

// --- composer ---------------------------------------------------------------
function autoGrow(el) {
  el.style.height = "auto";
  const border = el.offsetHeight - el.clientHeight; // box-sizing: border-box
  el.style.height = (el.scrollHeight + border) + "px"; // clamped visually by CSS max-height
}
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  // Guard BEFORE clearing. send() and nucleateStaged() both bail out on
  // `streaming`, so clearing first silently destroyed a follow-up typed while
  // the answer was still landing — the most natural thing to do on this surface.
  // #dock-composer already had the check in this order.
  if (streaming) return;
  const t = input.value;
  input.value = "";
  autoGrow(input);
  renderCommands(input.value); // clearing by hand fires no `input` event — dismiss the picker
  if (staged.length) nucleateStaged(t); // files attached: upload + act on them together
  else send(t);
});
let allCommands = [];
let filteredCommands = [];
let cmdSelIdx = -1;

fetch("/commands").then(r => r.json()).then(data => allCommands = data || []).catch(() => {});

function renderCommands(q) {
  const box = $("#commands");
  syncQuick(); // every path that changes the box comes through here
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
  input.value = c.name + (c.usage ? " " : "");
  input.focus();
  renderCommands(input.value);
}

input.addEventListener("input", () => {
  autoGrow(input);
  renderCommands(input.value);
});

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
    } else if (e.key === "Enter" && !e.shiftKey) {
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
  }
});

// --- dock composer (graph/map) — same conversation, mirrored into the card ---
// The turn is a real chat turn (user bubble + transcript land in the chat tab);
// the dock card is a lens showing only the latest exchange.
const dockInput = $("#dock-input");
$("#dock-composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = dockInput.value;
  if (!t.trim() || streaming) return;
  dockInput.value = "";
  autoGrow(dockInput);
  openPeek(t.trim());
  send(t);
});
dockInput.addEventListener("input", () => autoGrow(dockInput));
dockInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#dock-composer").requestSubmit();
  }
});
stopBtn.addEventListener("click", () => fetch("/stop", { method: "POST" }));
// Optimistic: clear the transcript at once (the reset itself is a cached-seed
// copy server-side, but never make the click wait on the network).
$("#brand-logo").addEventListener("click", async () => {
  if (streaming) return;
  log.innerHTML = "";
  await fetch("/reset", { method: "POST" });
  document.querySelector('.tab[data-tab="chat"]').click(); // surface the loaded chat
  loadVault();
  loadSessions();
});

// --- unified sidebar (stats · search · files · history) ----------------------
if (localStorage.getItem("sidebar-collapsed") === "1")
  document.body.classList.add("sidebar-collapsed");
$("#sidebar-toggle").addEventListener("click", () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
  sidebarYielded = false; // an explicit choice outranks the drawer's auto-yield
});

// Vault stats + file tree, from /vault_info. Best-effort: on error the placeholders stay.
async function loadVaultInfo() {
  try {
    const r = await fetch("/vault_info");
    const data = await r.json();
    if (data.error) return;
    if (data.path) $("#vault").textContent = data.path; // follows a /vault switch
    $("#stat-notes").textContent = data.notes;
    $("#stat-links").textContent = data.links;
    $("#stat-clusters").textContent = data.clusters;
    $("#stat-unresolved").textContent = data.unresolved;
    $("#tree").innerHTML = data.tree || "";
    renderMapPicker(data.hubs || []); // map landing: best-connected notes
    buildNoteIndex();                 // explore note search reads the fresh tree
    applySidebarFilter();
  } catch { notify("couldn't refresh vault stats"); }
}

// Tree click routing follows the active view: in explore's map mode a click
// roots the radial map on the note; otherwise it opens the note drawer (which
// also mirrors focus into the graph iframe via focusGraphNode).
$("#tree").addEventListener("click", (e) => {
  const leaf = e.target.closest(".tree-note");
  if (!leaf) return;
  const path = leaf.dataset.id;
  if (activeTab === "graph" && graphMode === "map") rootMap(path);
  else openNote(path);
});

// --- changes (what this session did to the vault) ----------------------------
// The list is the server's, not the transcript's: it survives a reload, folds
// five writes to one note into one row, and empties a row when /undo puts the
// bytes back. A row opens the drawer on the diff, which is the only place in the
// app where you can read what actually changed rather than what was claimed.
const changedPaths = new Set();
const KIND_MARK = { created: "+", deleted: "−", moved: "→", modified: "±" };

// A turn-end refresh is enough for a one-note write, and wrong for a run: an
// injector writes for minutes before its tool returns, so the notes were on disk
// — Obsidian showing them — while this list stayed empty and read as broken.
// Every phase bumps it, throttled: a run emits one every few hundred ms, and
// /changes re-reads each tracked note off disk. The trailing edge is covered by
// the turn-end call, so a leading-edge throttle drops nothing.
let lastBump = 0;
function bumpChanges() {
  const now = performance.now();
  if (now - lastBump < 2000) return;
  lastBump = now;
  loadChanges();
}

async function loadChanges() {
  let rows = [];
  try {
    rows = await (await fetch("/changes")).json();
  } catch { return; } // ambient, not an errand: a failed poll says nothing
  changedPaths.clear();
  const box = $("#changes");
  box.innerHTML = "";
  for (const r of rows) {
    changedPaths.add(r.path);
    const row = mkEl("div", "chg-row " + r.kind);
    row.dataset.path = r.path;
    row.title = r.from ? `${r.from} → ${r.path}` : r.path;
    row.appendChild(mkEl("span", "chg-mark", KIND_MARK[r.kind] || "±"));
    row.appendChild(mkEl("span", "chg-name", r.name));
    const tally = mkEl("span", "chg-tally");
    if (r.added) tally.appendChild(mkEl("span", "chg-add", "+" + r.added));
    if (r.removed) tally.appendChild(mkEl("span", "chg-del", "−" + r.removed));
    if (!r.added && !r.removed) tally.appendChild(mkEl("span", "chg-quiet", r.kind));
    row.appendChild(tally);
    box.appendChild(row);
  }
  $("#side-changes").hidden = !rows.length;
  $("#changes-count").textContent = rows.length || "";
  applySidebarFilter();
  syncDrawerMode(); // the diff tab may have just become available for the open note
}

$("#changes").addEventListener("click", (e) => {
  const row = e.target.closest(".chg-row");
  if (row) openDiff(row.dataset.path);
});

// One search box filters both the file tree and the chat history.
function applySidebarFilter() {
  const q = $("#side-search").value.trim().toLowerCase();
  // notes: substring on name or full path
  $("#tree").querySelectorAll(".tree-note").forEach((el) => {
    el.hidden = !!q && !el.textContent.toLowerCase().includes(q) &&
                !(el.dataset.id || "").toLowerCase().includes(q);
  });
  // folders: hide if nothing visible remains inside; reveal matches while searching
  $("#tree").querySelectorAll("details").forEach((d) => {
    const any = Array.from(d.querySelectorAll(".tree-note")).some((n) => !n.hidden);
    d.hidden = !!q && !any;
    if (q && any) d.open = true;
  });
  // changed notes: same substring rule as the tree, on the same names
  $("#changes").querySelectorAll(".chg-row").forEach((el) => {
    el.hidden = !!q && !el.textContent.toLowerCase().includes(q) &&
                !(el.dataset.path || "").toLowerCase().includes(q);
  });
  // sessions: substring on title; while searching, the expand cap is lifted
  $("#sessions").querySelectorAll(".session").forEach((el) => {
    el.hidden = (!!q && !el.textContent.toLowerCase().includes(q)) ||
                (!q && !sessionsExpanded && +el.dataset.idx >= SESSION_CAP);
  });
  $("#sessions-more").hidden = !!q || sessionsExpanded || sessionCount <= SESSION_CAP;

  // Say what the filter did. It used to empty both lists in silence, so a real
  // zero and a typo rendered pixel-identical and the only way to learn which was
  // to clear the field. It also only ever matched names, never note bodies, and
  // the placeholder never said so — hence the offer to ask the vault instead.
  const notes = $("#tree").querySelectorAll(".tree-note:not([hidden])").length;
  const chats = $("#sessions").querySelectorAll(".session:not([hidden])").length;
  const box = $("#side-search-status");
  box.hidden = !q;
  if (!q) return;
  if (notes || chats) {
    box.textContent = `${notes} note${notes === 1 ? "" : "s"}, ${chats} chat${chats === 1 ? "" : "s"} by name`;
    box.classList.remove("empty");
  } else {
    box.textContent = `no name matches "${$("#side-search").value.trim()}".`;
    box.classList.add("empty");
    const ask = document.createElement("button");
    ask.type = "button";
    ask.className = "linklike";
    ask.textContent = "search inside the notes";
    ask.addEventListener("click", () => {
      input.value = "/find " + $("#side-search").value.trim();
      input.focus();
      autoGrow(input);
    });
    box.appendChild(ask);
  }
}
$("#side-search").addEventListener("input", applySidebarFilter);

// collapse every open folder in the tree. The button lives inside the Files
// <summary>, so a click on it would also toggle the whole section. Cancelling
// on the summary itself is what reliably suppresses that: preventDefault from
// the button's own listener is order-dependent and stopPropagation does not
// consistently reach summary's activation behaviour.
$("#side-files").querySelector("summary").addEventListener("click", (e) => {
  if (e.target.closest("#tree-collapse")) e.preventDefault();
});

$("#tree-collapse").addEventListener("click", () => {
  for (const d of $("#tree").querySelectorAll("details[open]")) d.open = false;
});

// --- history (last sidebar section; capped, "expand" reveals the rest) -------
const SESSION_CAP = 8;
let sessionsExpanded = false;
let sessionCount = 0;

$("#sessions-more").addEventListener("click", () => {
  sessionsExpanded = true;
  applySidebarFilter();
});

async function loadSessions() {
  try {
    const r = await fetch("/sessions");
    const current = r.headers.get("X-Silica-Session") || "";
    const box = $("#sessions");
    box.innerHTML = "";
    const sessions = await r.json();
    sessionCount = sessions.length;
    sessions.forEach((s, i) => {
      const el = document.createElement("div");
      el.className = "session" + (s.id === current ? " active" : "");
      el.dataset.idx = i;
      el.textContent = s.title || "untitled";
      el.title = s.title || "";
      el.addEventListener("click", () => openSession(s.id));
      box.appendChild(el);
    });
    $("#sessions-more").textContent = "+ " + Math.max(0, sessionCount - SESSION_CAP) + " more";
    applySidebarFilter();
  } catch { notify("couldn't load chat history"); }
}

async function openSession(id) {
  if (streaming) return;
  try {
    const r = await fetch("/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    if (!r.ok) { notify("couldn't load that chat"); return; }
  } catch { notify("couldn't load that chat"); return; }
  document.querySelector('.tab[data-tab="chat"]').click(); // surface the loaded chat
  await loadVault();
  loadSessions();
}

// --- tabs -------------------------------------------------------------------
// Rebuilding the graph (Louvain + cooccurrence labels) is not free — only do it
// when the vault might actually have changed (graphStale), not on every switch
// back into the tab. A turn that writes notes sets graphStale = true.
let graphStale = true;
// One vocabulary for "which of these is showing". `.active` paints it and this
// says it out loud, so a segmented control is not a state only a sighted user
// can read. `aria-pressed` and not `aria-selected`, because these are groups of
// buttons rather than an ARIA tablist with its roving tabindex — and because it
// is what the quick actions already carry, so the app keeps one convention
// instead of gaining a second.
function setActive(btn, on) {
  btn.classList.toggle("active", on);
  btn.setAttribute("aria-pressed", on ? "true" : "false");
}

// Switching tabs is a function, not only a click: a synthetic .click() bubbles
// to the document's outside-click handler, which closes the note drawer. Every
// caller that needs the drawer to survive the switch (the context drawer's
// concept cloud, its suggested rows) calls this instead.
function showTab(tab) {
  activeTab = tab;
  if (tab === "chat") closePeek(); // stream visible → card redundant
  $("#dock").hidden = tab !== "graph"; // ask-from-here lives on the graph + map only
  document.querySelectorAll(".tab").forEach((b) => setActive(b, b.dataset.tab === tab));
  $("#view-chat").classList.toggle("active", tab === "chat");
  $("#view-graph").classList.toggle("active", tab === "graph");
  $("#view-calendar").classList.toggle("active", tab === "calendar");
  $("#view-metrics").classList.toggle("active", tab === "metrics");
  if (tab === "graph") setGraphMode(graphMode); // load the active mode's content
  if (tab === "calendar") loadCalendar();
  if (tab === "metrics") loadMetrics();
  // Deep-linkable views: the hash names the tab ("explore" is the label users
  // see for the graph view), so a pasted URL opens on the right screen.
  const slug = tab === "graph" ? "explore" : tab;
  if (location.hash !== "#" + slug) history.replaceState(null, "", "#" + slug);
}
$(".tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (tab) showTab(tab);
});
// #explore / #metrics / #chat on the URL select the tab, at load and on manual
// hash edits alike. Unknown hashes are left alone (note anchors, etc.).
function tabFromHash() {
  const slug = (location.hash || "").replace(/^#/, "");
  const tab = slug === "explore" ? "graph" : slug;
  if (["chat", "graph", "calendar", "metrics"].includes(tab) && tab !== activeTab) showTab(tab);
}
window.addEventListener("hashchange", tabFromHash);

// --- theme ------------------------------------------------------------------
// The palette itself is CSS, and the <head> script owns resolving "auto" — this
// is only the two things CSS cannot reach. The iframes get the resolved value
// on their URL rather than inheriting it, because they are separate documents
// with their own <head> script and no way to read this one's :root. Mermaid
// gets a repaint because a rendered diagram is baked SVG.
const liveTheme = () => document.documentElement.dataset.theme || "dark";

function applyThemePref(pref) {
  document.documentElement.dataset.themePref = pref;
  document.documentElement.__silicaPaintTheme?.();
}

// One watcher for both ways the theme moves: the settings row writes the
// preference, and the OS moving under an "auto" preference fires the <head>
// script's own listener. Both land on data-theme, so that is the only thing
// worth watching — and watching the result rather than the causes is what keeps
// this from needing a second call site every time a new one appears.
new MutationObserver(() => {
  repaintMermaid();
  graphStale = true;
  if (activeTab === "graph") {
    setGraphMode(graphMode);
    if (graphMode === "map" && mapRootedPath) rootMap(mapRootedPath);
  }
}).observe(document.documentElement, { attributeFilter: ["data-theme"] });

// --- explore tab: network graph | radial map ---------------------------------
// Two modes in one view, one toolbar. "graph" is one build (wikilink structure
// + semantic k-NN overlay, layers toggled in the frame's HUD); "map" is a radial
// map rooted on one note (/map), which needs a root, so it opens on a hub-picker
// landing. Each mode owns its iframe so switching back doesn't rebuild the graph.
let graphMode = "graph";
let mapRootedPath = null; // note the radial map is rooted on, or null → picker

// Show one mode: toggle which frame/picker is visible, and rebuild /graph only
// when the vault changed under us. Also the entry point when switching INTO the
// explore tab, so it must be idempotent.
function setGraphMode(m) {
  graphMode = m;
  document.querySelectorAll(".gmode-tabs button").forEach((b) => setActive(b, b.dataset.gmode === m));
  const isMap = m === "map";
  // folders / areas / read render in-page. They take the whole pane, so both
  // iframes hide and the note search goes with them: it flies the graph camera
  // and roots the map, and neither means anything on a treemap or a matrix.
  const isShape = m in SHAPE_VIEWS;
  $("#shape-pane").hidden = !isShape;
  $("#node-search-wrap").hidden = isShape;
  $("#graph-frame").hidden = isMap || isShape;
  $("#map-frame").hidden = !isMap || !mapRootedPath;
  $("#map-picker").hidden = !isMap || !!mapRootedPath;
  closeNodeResults();
  if (isShape) {
    $("#graph-loading").hidden = true;
    $("#map-loading").hidden = true;
    drawShape();
    return;
  }
  $("#shape-loading").hidden = true;
  if (isMap) {
    $("#graph-loading").hidden = true;
    if (mapRootedPath) $("#map-loading").hidden = true;
    $("#node-search").focus();
  } else {
    $("#map-loading").hidden = true;
    if (graphStale) {
      $("#graph-loading").hidden = false;
      $("#graph-frame").src = "/graph?theme=" + liveTheme() + "&t=" + Date.now();
      graphStale = false;
    }
  }
}

$("#graph-bar").addEventListener("click", (e) => {
  const m = e.target.dataset.gmode; // only the mode buttons carry it; inputs don't
  if (!m || m === graphMode) return;
  setGraphMode(m);
});

// #graph-frame finishes loading only once the server is done building — drop the
// loader then and re-sync the focus dim state after a (re)load.
$("#graph-frame").addEventListener("load", () => {
  $("#graph-loading").hidden = true;
  replayGraphFocus(); // re-sync whatever is focused after a (re)load
  syncDrawerToViews(); // ditto for the drawer, which hides this frame's HUD
});
$("#map-frame").addEventListener("load", () => { $("#map-loading").hidden = true; });

// --- explore: three surfaces that are not link-space -------------------------
// graph and map both lay notes out by how they CONNECT. Three questions that
// shape cannot answer: where does a note sit, how do two areas couple as a
// whole, and in what order could this be read. One /shape load feeds all three.
let shapeData = null;
let shapeLoading = false;
let folderPrefix = []; // drill state for the containment view

async function loadShape() {
  if (shapeData || shapeLoading) return shapeData;
  shapeLoading = true;
  $("#shape-loading").hidden = false;
  try {
    const d = await (await fetch("/shape")).json();
    if (d.error) { notify("couldn't read the vault shape: " + d.error); return null; }
    shapeData = d;
    return d;
  } catch { notify("couldn't read the vault shape"); return null; }
  finally { shapeLoading = false; $("#shape-loading").hidden = true; }
}

// Squarified treemap. ~30 lines instead of d3-hierarchy: the vendored bundles
// are the graph renderers, and pulling a layout library in for one rect split
// would be the largest dependency on the page by a wide margin.
// Returns [{...item, x, y, w, h}] in the given rect.
function squarify(items, x, y, w, h) {
  const out = [];
  let rest = items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value);
  while (rest.length) {
    const total = rest.reduce((s, i) => s + i.value, 0);
    const vertical = w < h;          // lay the next row along the shorter side
    const side = vertical ? w : h;
    // Grow the row while the worst aspect ratio in it keeps improving.
    let row = [], best = Infinity, sum = 0;
    for (const it of rest) {
      const trial = sum + it.value;
      const thickness = (trial / total) * (vertical ? h : w);
      const worst = Math.max(
        ...[...row, it].map((r) => {
          const len = (r.value / trial) * side;
          return Math.max(thickness / len, len / thickness);
        }));
      if (row.length && worst > best) break;
      row.push(it); sum = trial; best = worst;
    }
    const thickness = (sum / total) * (vertical ? h : w);
    let off = 0;
    for (const it of row) {
      const len = (it.value / sum) * side;
      out.push(vertical
        ? { ...it, x: x + off, y, w: len, h: thickness }
        : { ...it, x, y: y + off, w: thickness, h: len });
      off += len;
    }
    if (vertical) { y += thickness; h -= thickness; } else { x += thickness; w -= thickness; }
    rest = rest.slice(row.length);
  }
  return out;
}

// One level of the containment tree at `prefix`: immediate children, each with
// its note count and how much of it belongs to a single area.
function folderLevel(notes, prefix, real) {
  const pre = prefix.length ? prefix.join("/") + "/" : "";
  const kids = new Map();
  for (const n of notes) {
    if (!n.path.startsWith(pre)) continue;
    const tail = n.path.slice(pre.length);
    const cut = tail.indexOf("/");
    const name = cut === -1 ? tail : tail.slice(0, cut);
    let k = kids.get(name);
    if (!k) kids.set(name, k = { name, folder: cut !== -1, count: 0, areas: new Map() });
    k.count++;
    // Only multi-note areas count toward purity. A singleton carries a group id
    // like any other community, so counting it would let a folder of six
    // unrelated notes report six areas and a purity of 1/6 — a number about the
    // clustering's tail, not about the filing. `real` is the same set the
    // matrix draws, so the two surfaces agree on what an area is.
    if (real.has(n.area)) k.areas.set(n.area, (k.areas.get(n.area) || 0) + 1);
  }
  return [...kids.values()].map((k) => {
    const placed = [...k.areas.values()].reduce((s, v) => s + v, 0);
    const top = Math.max(0, ...k.areas.values());
    return { ...k, value: k.count, placed,
             purity: placed ? top / placed : null, spread: k.areas.size };
  });
}

// The containment view. Area is note count; the fill is IMPURITY, so a folder
// whose notes all belong to one area is nearly blank and one that mixes nine
// areas is solid. That direction is deliberate: the question this surface
// answers is where filing and meaning disagree, so the disagreements are the
// ones that should be loud. Colouring by area instead would need a 26-hue
// categorical palette, which is exactly what the viz tokens exist to avoid.
function renderFolders(s) {
  const pane = mkEl("div", "shape-body");
  const head = mkEl("div", "shape-head");
  const crumbs = mkEl("div", "fcrumb");
  const mk = (label, depth) => {
    const b = mkEl("button", "fcrumb-b", label);
    b.type = "button";
    b.addEventListener("click", () => { folderPrefix = folderPrefix.slice(0, depth); drawShape(); });
    return b;
  };
  crumbs.appendChild(mk("vault", 0));
  folderPrefix.forEach((p, i) => {
    crumbs.appendChild(mkEl("span", "fcrumb-sep", "/"));
    crumbs.appendChild(mk(p, i + 1));
  });
  head.appendChild(crumbs);
  head.appendChild(mkEl("span", "shape-sub",
    "area = notes · fill = how much the folder mixes areas · click a folder to descend"));
  pane.appendChild(head);

  const rows = folderLevel(s.notes, folderPrefix, new Set(s.areas.map((a) => a.id)));
  if (!rows.length) { pane.appendChild(mkEl("p", "mempty", "Nothing here.")); return pane; }

  const box = mkEl("div", "tmap");
  // A fixed viewBox with percentage-positioned tiles: the layout is computed in
  // an abstract 100x100 box and the container decides the pixels, so a resize
  // needs no relayout and no observer.
  for (const t of squarify(rows, 0, 0, 100, 100)) {
    const tile = mkEl("div", "tmap-tile" + (t.folder ? " folder" : ""));
    tile.style.cssText = `left:${t.x}%;top:${t.y}%;width:${t.w}%;height:${t.h}%`;
    const impurity = t.purity === null ? 0 : 1 - t.purity;
    tile.style.setProperty("--i", impurity.toFixed(3));
    const pur = t.purity === null ? "no area" : `${Math.round(t.purity * 100)}% one area`;
    tile.title = `${t.name} — ${t.count} notes · ${pur}`
      + (t.spread > 1 ? ` · spans ${t.spread} areas` : "");
    const lbl = mkEl("div", "tmap-lbl");
    lbl.appendChild(mkEl("span", "tmap-name", t.name));
    lbl.appendChild(mkEl("span", "tmap-n", nfmt(t.count)));
    tile.appendChild(lbl);
    if (t.folder) {
      tile.addEventListener("click", () => { folderPrefix = [...folderPrefix, t.name]; drawShape(); });
    } else {
      tile.dataset.path = (folderPrefix.length ? folderPrefix.join("/") + "/" : "") + t.name;
      tile.classList.add("clickable");
    }
    box.appendChild(tile);
  }
  pane.appendChild(box);

  const worst = rows.filter((r) => r.purity !== null && r.placed >= 3)
    .sort((a, b) => a.purity - b.purity)[0];
  pane.appendChild(mkEl("p", "mnote", worst
    ? `Most mixed here: ${worst.name}, ${Math.round(worst.purity * 100)}% in its biggest area across ${worst.spread}.`
    : "Too few placed notes here to read purity."));
  return pane;
}

// Area x area coupling. Every pair at once, where the metrics tab's gap list is
// a top-N: an absence is only readable against the pairs that are present, and
// a ranked list of the emptiest pairs cannot show that.
// Two scales in one grid, so they get two treatments: off-diagonal cells ramp on
// the accent by inter-area link count, the diagonal is neutral and carries the
// area's own cohesion. A shared ramp would put a 0.11 cohesion and 11 links in
// the same ink and invite reading one as the other.
function renderAreas(s) {
  const pane = mkEl("div", "shape-body");
  const head = mkEl("div", "shape-head");
  head.appendChild(mkEl("strong", null, "Area coupling"));
  const pairs = s.areas.length * (s.areas.length - 1) / 2;
  let linked = 0;
  for (let i = 0; i < s.areas.length; i++) {
    for (let j = i + 1; j < s.areas.length; j++) if (s.matrix[i][j]) linked++;
  }
  head.appendChild(mkEl("span", "shape-sub",
    `${s.areas.length} areas · ${linked} of ${pairs} pairs share a link · diagonal is cohesion`));
  pane.appendChild(head);

  let max = 1;
  for (let i = 0; i < s.areas.length; i++) {
    for (let j = 0; j < s.areas.length; j++) if (i !== j) max = Math.max(max, s.matrix[i][j]);
  }
  const wrap = mkEl("div", "smx-scroll");
  const g = mkEl("div", "smx dense");
  g.style.setProperty("--cols", s.areas.length);
  g.appendChild(mkEl("div", "smx-corner"));
  for (const a of s.areas) {
    const h = mkEl("div", "smx-col", a.label);
    h.title = `${a.label} — ${a.size} notes · cohesion ${a.cohesion}`;
    g.appendChild(h);
  }
  s.areas.forEach((a, i) => {
    const lbl = mkEl("div", "smx-row", a.label);
    lbl.title = `${a.label} — ${a.size} notes`;
    if (a.path) { lbl.dataset.path = a.path; lbl.classList.add("clickable"); }
    g.appendChild(lbl);
    s.areas.forEach((b, j) => {
      const v = s.matrix[i][j];
      if (i === j) {
        // ".55" saves two characters in a 22px cell, but only where there IS a
        // leading zero: slicing it off 1.00 printed ".00", so a perfectly
        // cohesive area read as the least cohesive one on the grid.
        const coh = a.cohesion >= 1 ? "1" : a.cohesion ? a.cohesion.toFixed(2).slice(1) : "";
        const c = mkEl("div", "smx-cell diag", coh);
        c.title = `${a.label}: cohesion ${a.cohesion} — ${a.intra} linked pairs inside ${a.size} notes`;
        g.appendChild(c);
        return;
      }
      const c = mkEl("div", "smx-cell" + (v ? "" : " empty"), v ? String(v) : "");
      if (v) {
        c.style.setProperty("--i", Math.sqrt(v / max).toFixed(3));
        c.title = `${a.label} ↔ ${b.label}: ${v} linked note pairs`;
      } else {
        c.title = `${a.label} ↮ ${b.label}: nothing links them`;
      }
      g.appendChild(c);
    });
  });
  wrap.appendChild(g);
  pane.appendChild(wrap);
  pane.appendChild(mkEl("p", "mnote",
    `${nfmt(s.totals.singletons)} single-note areas are left out: each would be a row and a column `
    + "of zeroes, and 65 of them would bury the 26 that carry the vault."));
  return pane;
}

// A reading order, derived and not authored. The one surface here that is not a
// layout: it answers "where do I start and what next", which no arrangement of
// nodes in space can, because space has no order.
function renderReading(s) {
  const pane = mkEl("div", "shape-body");
  const head = mkEl("div", "shape-head");
  const r = s.reading;
  head.appendChild(mkEl("strong", null, "A way through"));
  head.appendChild(mkEl("span", "shape-sub",
    `${r.stops.length} stops · areas biggest first, each hub then what it opens onto`));
  pane.appendChild(head);
  // The vault holds notes that share a name across folders, so a path can list
  // the same label twice for two different files. Where that happens the parent
  // folder rides along: two identical rows pointing at different notes is worse
  // than a longer label, and silently dropping one would be worse still.
  const seenLabel = new Map();
  for (const st of r.stops) seenLabel.set(st.label, (seenLabel.get(st.label) || 0) + 1);
  const ol = mkEl("ol", "rpath");
  let area = null;
  for (const stop of r.stops) {
    if (stop.area !== area) {
      area = stop.area;
      ol.appendChild(mkEl("li", "rpath-area", area));
    }
    const li = mkEl("li", "rpath-stop clickable");
    li.dataset.path = stop.path;
    // The full path, not the parent folder: the notes that collide here are
    // forks of each other under `silica/`, so they share every segment except
    // the first, and one parent segment disambiguated nothing.
    const name = seenLabel.get(stop.label) > 1
      ? stop.path.replace(/\.md$/, "") : stop.label;
    const n = mkEl("span", "rpath-name", name);
    n.title = stop.path;
    li.appendChild(n);
    li.appendChild(mkEl("span", "rpath-why", stop.why));
    ol.appendChild(li);
  }
  pane.appendChild(ol);
  // The cut is stated rather than left to be inferred from a heading count: the
  // path stops at a readable length, and 18 unmentioned areas would make this
  // read as a tour of the whole vault.
  const cut = r.areas_total - r.areas_covered;
  pane.appendChild(mkEl("p", "mnote",
    "Derived from link structure alone, so it promises adjacency and not importance: "
    + "each stop is linked to something already read."
    + (cut > 0 ? ` ${nfmt(cut)} smaller areas are past the end of the path.` : "")));
  return pane;
}

const SHAPE_VIEWS = { folders: renderFolders, areas: renderAreas, read: renderReading };

async function drawShape() {
  const pane = $("#shape-pane");
  const render = SHAPE_VIEWS[graphMode];
  if (!render) return;
  const s = shapeData || await loadShape();
  if (!s || !SHAPE_VIEWS[graphMode]) return; // mode may have changed while loading
  pane.innerHTML = "";
  pane.appendChild(SHAPE_VIEWS[graphMode](s));
}

// Same convention the metrics rows use: a row that names a note opens it.
$("#shape-pane").addEventListener("click", (e) => {
  const el = e.target.closest("[data-path]");
  if (el && el.dataset.path) openContext({ path: el.dataset.path });
});

// --- metrics tab -------------------------------------------------------------
// Everything the L1 graph report measures, as cards. Charts are HTML tables:
// the bar IS the row, so the chart and its table view are one DOM — every value
// stays readable without a hover, and there is no chart/table toggle to keep in
// sync. Deliberately library-free; a bar is a div with a width.
//
// Palette (validated with the dataviz skill's checker against the --page
// surface, dark mode): magnitude uses the accent hue snapped into the dark
// lightness band, the energy chart is diverging accent↔amber over a neutral
// zero rule, and reliability tiers take a 3-step ordinal ramp of the accent.
// The chrome tokens themselves (--accent, --warn) sit above the band and stay
// where they are — they light chrome, not fills.
// Two depths, because the report's co-occurrence leg costs ~100x the rest
// (one expanded ranking per note). The tab opens at structural depth in a
// couple of seconds; the four PROPOSED signals are a second, explicit pass the
// reader asks for. E(vault) is labelled with the depth it was measured at —
// its `deficits` term is absent from the cheap pass, and on a real vault that
// term dominates, so an unlabelled number would compare two different things.
let metricsStale = true;
let metricsLoading = false;
let metricsDepth = "structural";

let metricsAbort = null;

async function loadMetrics(force = false, proposals = false) {
  if (metricsLoading) return;
  if (!metricsStale && !force && !(proposals && metricsDepth !== "full")) return;
  metricsLoading = true;
  const body = $("#metrics-body");
  const loading = $("#metrics-loading");
  // `.fl-msg`, not `div:last-child`: the overlay now ends with a cancel button,
  // so the positional selector matched nothing and this line threw before the
  // overlay was ever shown — the whole metrics load failed silently.
  loading.querySelector(".fl-msg").textContent = proposals
    ? "Running the co-occurrence delta over every note."
    : "Measuring the vault.";
  loading.hidden = false;
  body.style.opacity = body.childElementCount ? "0.45" : ""; // hold the last render, no skeleton flash
  // A full report takes ~20s behind an indeterminate spinner, with no way out
  // short of switching tab and hoping. The previous render stays underneath at
  // reduced opacity, so cancelling leaves you exactly where you were.
  metricsAbort = new AbortController();
  try {
    const data = await (await fetch("/metrics" + (proposals ? "?proposals=1" : ""),
                                    { signal: metricsAbort.signal })).json();
    if (data.error) { notify("metrics unavailable: " + data.error); return; }
    metricsDepth = data.depth || "structural";
    renderMetrics(data);
    metricsStale = false;
  } catch (e) {
    if (e.name !== "AbortError") notify("couldn't measure the vault");
  } finally {
    metricsAbort = null;
    metricsLoading = false;
    loading.hidden = true;
    body.style.opacity = "";
  }
}

$("#metrics-refresh").addEventListener("click", () => loadMetrics(true, metricsDepth === "full"));

// Clicking any row that names a note opens its context — the metrics are only
// useful if the note they point at is one click away, and a metrics row is a
// measurement about a note's PLACE in the vault, which is what context answers.
$("#metrics-body").addEventListener("click", (e) => {
  if (e.target.id === "metrics-proposals") { loadMetrics(true, true); return; }
  const row = e.target.closest("[data-path]");
  if (row && row.dataset.path) openContext({ path: row.dataset.path });
});

const mkEl = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text; // labels are vault data — never innerHTML
  return n;
};

// A card: hairline compartment, micro-label title, optional one-line note.
// A div, not a <header>: the global `header>* {display:flex}` rule would flatten
// the title and its subtitle onto one line.
function mCard(title, sub) {
  const c = mkEl("section", "mcard");
  const h = mkEl("div", "mcard-head");
  h.appendChild(mkEl("h3", null, title));
  if (sub) h.appendChild(mkEl("span", "mcard-sub", sub));
  c.appendChild(h);
  return c;
}

function mEmpty(card, msg) { card.appendChild(mkEl("p", "mempty", msg)); return card; }

// Magnitude chart: one hue, bars grow from a single baseline, value at the tip.
// rows: [{label, value, path?, title?, note?}]
function barChart(rows, { fmt = (v) => nfmt(v), tone = "accent" } = {}) {
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 1);
  const t = mkEl("table", "chart bars");
  const tb = mkEl("tbody");
  for (const r of rows) {
    const tr = mkEl("tr");
    if (r.path) { tr.dataset.path = r.path; tr.classList.add("clickable"); }
    if (r.title) tr.title = r.title;
    const th = mkEl("th", null, r.label);
    th.scope = "row";
    const td = mkEl("td", "cell");
    const bar = mkEl("div", "bar " + tone);
    bar.style.width = (Math.abs(r.value) / max) * 100 + "%";
    td.appendChild(bar);
    const val = mkEl("td", "num", fmt(r.value));
    tr.append(th, td, val);
    if (r.note !== undefined) tr.appendChild(mkEl("td", "num sub", r.note));
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  return t;
}

// Waterfall: the right form for an additive decomposition. Each bar starts
// where the previous one ended, and the last bar IS the total — so the chart
// says what the hero number is made of, which a common-baseline chart cannot.
// It also survives the scale: E's terms span three orders of magnitude on a
// real vault, and off a shared baseline the small ones paint as 2px slivers
// that read "measured, came out flat". Stacked end to end they are steps.
// Cool arm lowers the total, warm arm raises it, neutral rule marks zero.
function waterfall(rows, total, { negLabel, posLabel }) {
  let cum = 0, lo = 0, hi = 0;
  const steps = rows.map((r) => {
    const start = cum;
    cum += r.value;
    lo = Math.min(lo, cum);
    hi = Math.max(hi, cum);
    return { label: r.label, value: r.value, start, end: cum };
  });
  hi = Math.max(hi, total);
  lo = Math.min(lo, total);
  const span = hi - lo || 1;
  const at = (v) => ((v - lo) / span) * 100;

  const wrap = mkEl("div", "diverge");
  wrap.appendChild(mLegend([
    { tone: "accent", label: negLabel },
    { tone: "amber", label: posLabel },
  ]));
  const t = mkEl("table", "chart bars waterfall");
  t.style.setProperty("--zero", at(0) + "%");
  const tb = mkEl("tbody");
  const addRow = (label, from, to, value, tone, cls) => {
    const tr = mkEl("tr", cls);
    const num = (value > 0 ? "+" : "") + value.toFixed(2);
    tr.title = `${label}: ${num}`;
    const th = mkEl("th", null, label);
    th.scope = "row";
    const td = mkEl("td", "cell");
    const bar = mkEl("div", "bar " + tone);
    bar.style.left = at(Math.min(from, to)) + "%";
    bar.style.width = (Math.abs(to - from) / span) * 100 + "%";
    td.appendChild(bar);
    tr.append(th, td, mkEl("td", "num", num));
    tb.appendChild(tr);
  };
  for (const s of steps) {
    addRow(s.label, s.start, s.end, s.value, s.value < 0 ? "accent" : "amber");
  }
  addRow("E(vault)", 0, total, total, "total", "total");
  t.appendChild(tb);
  wrap.appendChild(t);
  return wrap;
}

// Histogram: columns, because the x-axis is an ordered numeric scale and
// position has to read left-to-right. One hue — the bins are a single series
// ("notes"), and their order is already carried by position, so spending the
// identity channel on a ramp would re-encode what the axis says.
// Every column is capped at 24px and labeled on the cap, so the values are
// readable without hovering; the row beneath is the axis.
function histogram(bins) {
  const max = Math.max(...bins.map((b) => b.count), 1);
  const wrap = mkEl("div", "hist");
  const plot = mkEl("div", "hist-plot");
  for (const b of bins) {
    const col = mkEl("div", "hist-col");
    col.title = `degree ${b.label}: ${nfmt(b.count)} notes`;
    // A zero bin gets a labeled slot but no mark: painting a stub would say
    // "small", and the reading here is "none".
    col.appendChild(mkEl("div", "hist-cap", b.count ? nfmt(b.count) : ""));
    // The track is the only fixed-height box, so the bar's percentage resolves
    // against the plot area alone and the cap/tick bands sit outside it — a
    // column chart whose fixed height swallowed its own axis labels would make
    // the card grow a nested scrollbar.
    const track = mkEl("div", "hist-track");
    const bar = mkEl("div", "hist-bar" + (b.count ? "" : " empty"));
    bar.style.height = (b.count / max) * 100 + "%";
    track.appendChild(bar);
    col.appendChild(track);
    col.appendChild(mkEl("div", "hist-tick", b.label));
    plot.appendChild(col);
  }
  wrap.appendChild(plot);
  return wrap;
}

// A legend is always present for two or more series — identity never rests on
// color alone. Single-series charts get none; their title already names them.
function mLegend(items) {
  const l = mkEl("div", "mlegend");
  for (const it of items) {
    const row = mkEl("span", "mlegend-item");
    row.appendChild(mkEl("i", "swatch " + it.tone));
    row.appendChild(mkEl("span", null, it.label));
    l.appendChild(row);
  }
  return l;
}

// Meter: one ratio against its limit. Fill and track are steps of one ramp.
function meter(done, total, label) {
  const w = mkEl("div", "meter-wrap");
  const track = mkEl("div", "meter");
  const fill = mkEl("div", "meter-fill");
  fill.style.width = (total ? (done / total) * 100 : 0) + "%";
  track.appendChild(fill);
  w.append(track, mkEl("div", "meter-lbl", label));
  return w;
}

// Ordinal part-to-whole: one stacked bar, steps of a single hue in rank order,
// 2px surface gaps doing the separating (never a stroke around a segment).
function stackedBar(segs) {
  const total = segs.reduce((s, x) => s + x.value, 0) || 1;
  const bar = mkEl("div", "stack");
  for (const s of segs) {
    if (!s.value) continue;
    const seg = mkEl("div", "stack-seg " + s.tone);
    seg.style.width = (s.value / total) * 100 + "%";
    seg.title = s.label + ": " + nfmt(s.value);
    bar.appendChild(seg);
  }
  return bar;
}

// Session × area matrix. A time axis is the wrong form for this data and the
// vault says so: the claim clocks land on a handful of days inside a couple of
// months, with the odd straggler years back, so a linear date axis spends its
// width on the gap and smears everything that matters into one column. Dropping
// the duration and keeping the ordering leaves what actually varies — which
// areas recur session after session, and which never come up at all.
// Cells carry the count as text, not just as intensity: the tab's rule is that
// every value reads without a hover, and colour here is the second encoding.
function sessionMatrix(s) {
  const wrap = mkEl("div", "smx-scroll");
  const g = mkEl("div", "smx");
  g.style.setProperty("--cols", s.areas.length);
  const max = Math.max(...s.days.map((d) => Math.max(...Object.values(d.cells), 0)), 1);

  g.appendChild(mkEl("div", "smx-corner"));
  for (const a of s.areas) {
    const h = mkEl("div", "smx-col", a.label);
    h.title = `${a.label} — ${a.total} claims across ${s.days.length} sessions`;
    g.appendChild(h);
  }
  g.appendChild(mkEl("div", "smx-col smx-tot", "total"));

  for (const d of s.days) {
    const lbl = mkEl("div", "smx-row", d.date);
    lbl.title = `${d.date}: ${d.notes} claims`;
    g.appendChild(lbl);
    for (const a of s.areas) {
      const n = d.cells[a.id] || 0;
      // An empty cell gets its slot and no mark: painting a stub would say
      // "a little", and the reading is "that area saw nothing that day".
      const c = mkEl("div", "smx-cell" + (n ? "" : " empty"), n ? String(n) : "");
      if (n) {
        // sqrt, so a session of 1 stays visible next to one of 12 instead of
        // resolving to a tint indistinguishable from empty.
        c.style.setProperty("--i", Math.sqrt(n / max).toFixed(3));
        c.title = `${d.date} · ${a.label}: ${n}`;
        if (a.path) { c.dataset.path = a.path; c.classList.add("clickable"); }
      }
      g.appendChild(c);
    }
    g.appendChild(mkEl("div", "smx-cell smx-tot", String(d.notes)));
  }
  wrap.appendChild(g);
  return wrap;
}

// cols: [{key, label, num?}] — `num` right-aligns and tabularises the column.
function mTable(cols, rows) {
  const t = mkEl("table", "chart data");
  const thead = mkEl("thead");
  const hr = mkEl("tr");
  for (const c of cols) {
    const th = mkEl("th", c.num ? "num" : null, c.label);
    th.scope = "col";
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  const tb = mkEl("tbody");
  for (const r of rows) {
    const tr = mkEl("tr");
    if (r._path) { tr.dataset.path = r._path; tr.classList.add("clickable"); }
    if (r._title) tr.title = r._title;
    for (const c of cols) {
      // An action column builds its own cell: a measurement can carry the move
      // it suggests, and the row click keeps meaning "open that note".
      if (c.el) {
        const td = mkEl("td", "act");
        td.appendChild(c.el(r));
        tr.appendChild(td);
        continue;
      }
      const td = mkEl("td", c.num ? "num" : null, String(r[c.key] ?? ""));
      // Text columns are clamped to keep the card from growing a scrollbar; the
      // full value has to stay reachable, so it rides the cell's own tooltip.
      if (!c.num) td.title = td.textContent;
      tr.appendChild(td);
    }
    tb.appendChild(tr);
  }
  t.append(thead, tb);
  const wrap = mkEl("div", "tscroll");
  wrap.appendChild(t);
  return wrap;
}

const nfmt = (n) => (typeof n === "number" ? n.toLocaleString() : String(n));

// The one action a metrics row carries: a gap names two areas, and the move it
// suggests is a write, so it drafts the turn and the agent's gate still owns the
// write. The turn has to leave room for "there is no bridge here" — the gap is a
// shape in the link graph, not evidence that the two areas belong connected.
// stopPropagation: the row itself is clickable and means "open that note".
function mBridgeBtn(a, b) {
  const btn = mkEl("button", "cx-do", "bridge");
  btn.type = "button";
  btn.title = "draft a note that connects these two areas";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    prefillChat(
      'Nothing links "' + a + '" and "' + b + '", the hubs of two areas ' +
      "of the vault that stand apart. Read both, and if a real connection exists, " +
      "write the note that states it and link it to each side. If there isn't one, " +
      "say so instead of inventing it.");
  });
  return btn;
}

// A cut list must never read as the whole list.
function mMore(shown, total, noun) {
  return shown < total ? mkEl("p", "mnote", `showing ${shown} of ${nfmt(total)} ${noun}`) : null;
}

function renderMetrics(d) {
  const body = $("#metrics-body");
  body.innerHTML = "";
  const T = d.totals || {};
  $("#metrics-stamp").textContent = d.generated_at ? d.generated_at.slice(0, 16).replace("T", " ") : "";

  // --- hero: E(vault) + the KPI row it summarises ----------------------------
  const head = mkEl("section", "mcard mhero");
  const e = d.energy || { total: 0, terms: [] };
  const full = d.depth === "full";
  const hv = mkEl("div", "hero-val", (e.total > 0 ? "+" : "") + e.total.toFixed(2));
  head.appendChild(mkEl("div", "hero-lbl", "E(vault) · lattice energy"));
  head.appendChild(hv);
  head.appendChild(mkEl("p", "hero-sub",
    "Lower is more coherent. A thermometer, not a target: read it to compare runs, "
    + "never descend it. "
    + (full
      ? "Measured at full depth: comparable only to other full-depth readings."
      : "Structural depth: integration deficits are not measured, so this is not "
        + "comparable to a full-depth E.")));
  if (d.discourse_state) {
    const chip = mkEl("div", "chip", "discourse: " + d.discourse_state);
    head.appendChild(chip);
  }
  body.appendChild(head);

  // Rates, not counts: notes / links / areas / unresolved already sit in the
  // sidebar's vault box two panes to the left, and printing them twice on one
  // screen spends the loudest row in the view on nothing. These are the
  // numbers that box cannot carry — including the correction to its own
  // "areas" count, most of which are single notes.
  const kpi = mkEl("section", "mkpi");
  const links = T.links || 0, notes = T.notes || 0;
  const orphans = T.orphans || 0;
  const zeroBin = d.degree_histogram?.[0];
  const isolated = zeroBin && zeroBin.lo === 0 ? zeroBin.count : 0;
  const pct = (n, of) => (of ? Math.round((n / of) * 100) + "%" : "—");
  const tiles = [
    ["Links / note", notes ? (links / notes).toFixed(1) : "0", false],
    ["Orphaned", pct(orphans, notes), orphans > 0],
    ["No link at all", nfmt(isolated), isolated > 0],
    ["Areas > 1 note", nfmt((d.clusters || []).filter((c) => c.size > 1).length), false],
  ];
  // Four fixed plus at most two conditional: six is what fits one row at the
  // 900px floor the .mkpi grid is sized for, and a seventh tile wraps to a row
  // of its own with five dead cells beside it.
  if (d.code_coverage) {
    tiles.push(["Code documented",
      pct(d.code_coverage.documented, d.code_coverage.total), false]);
  }
  if (d.temporal) {
    tiles.push(["Human tier",
      pct(d.temporal.by_tier?.["3"] || 0, d.temporal.notes_scanned), false]);
  }
  for (const [lbl, val, warn] of tiles) {
    const s = mkEl("div", "stat");
    s.appendChild(mkEl("div", "val" + (warn ? " warn" : ""), nfmt(val)));
    s.appendChild(mkEl("div", "lbl", lbl));
    kpi.appendChild(s);
  }
  body.appendChild(kpi);

  const grid = mkEl("div", "mgrid");
  body.appendChild(grid);

  // --- energy decomposition --------------------------------------------------
  const ec = mCard("Energy decomposition", `the ${e.terms.length} terms that sum to E`);
  ec.appendChild(waterfall(
    e.terms.map((t) => ({ label: t.name, value: t.value })), e.total,
    { negLabel: "bonds formed (lowers E)", posLabel: "entropic cost (raises E)" },
  ));
  grid.appendChild(ec);

  // --- areas -----------------------------------------------------------------
  const CL_ROWS = 14;
  const cl = mCard("Areas by size", "Louvain communities · cohesion = intra-links / possible");
  if (d.clusters?.length) {
    const shown = d.clusters.slice(0, CL_ROWS);
    const rest = d.clusters.slice(CL_ROWS);
    const rows = shown.map((c) => ({
      label: c.hub || "#" + c.id, value: c.size, path: c.path,
      note: c.cohesion ? c.cohesion.toFixed(2) : "—",
      title: `${c.size} notes · cohesion ${c.cohesion}`,
    }));
    cl.appendChild(barChart(rows));
    cl.appendChild(mkEl("p", "mnote", "right column: cohesion"));
    // The tail is a count, not a fourteenth area: as a bar row its total
    // outgrew every real area and crushed them all into stubs. An aggregate
    // never shares a magnitude scale with the things it aggregates.
    if (rest.length) {
      cl.appendChild(mkEl("p", "mnote",
        `${nfmt(rest.length)} smaller areas hold the other `
        + `${nfmt(rest.reduce((s, c) => s + c.size, 0))} notes`));
    }
  } else mEmpty(cl, "No communities yet. Link some notes.");
  grid.appendChild(cl);

  // --- degree distribution ---------------------------------------------------
  // An empty vault gets no card at all: the endpoint still returns one zeroed
  // bin, and "every note carries at least one link" is a silly thing to say
  // about no notes.
  if (d.degree_histogram?.some((b) => b.count)) {
    const dh = mCard("Link distribution", "notes by how many resolved links they carry");
    dh.appendChild(histogram(d.degree_histogram));
    const isolated = d.degree_histogram[0];
    dh.appendChild(mkEl("p", "mnote",
      isolated && isolated.lo === 0 && isolated.count
        ? `${nfmt(isolated.count)} notes carry no resolved link at all`
        : "every note carries at least one resolved link"));
    grid.appendChild(dh);
  }

  // --- hubs ------------------------------------------------------------------
  // In/out degree are dropped: degree is their sum, and six columns in a card
  // this narrow is a scrollbar, not a table. Both still ride the row tooltip.
  const hb = mCard("Hubs", "degree · betweenness = how much traffic routes through");
  if (d.hubs?.length) {
    hb.appendChild(mTable(
      [{ key: "label", label: "Note" }, { key: "area", label: "Area" },
       { key: "degree", label: "Links", num: true },
       { key: "betweenness", label: "Btw", num: true }],
      d.hubs.map((h) => ({ ...h, _path: h.path, _title: `${h.in} in · ${h.out} out` })),
    ));
  } else mEmpty(hb, "No connected notes yet.");
  grid.appendChild(hb);

  // --- maintenance -----------------------------------------------------------
  // Heterogeneous counts in different units — a bar chart would imply they are
  // comparable. A table is the honest form.
  const mt = mCard("Maintenance", "what the report says needs attention");
  mt.appendChild(mTable(
    [{ key: "what", label: "Signal" }, { key: "n", label: "Count", num: true },
     { key: "means", label: "Means" }],
    [
      ["Orphans", T.orphans, "nothing links to them"],
      ["Unresolved links", T.dangling_links, "wikilinks with no target"],
      ["Contested", T.contested, "frontmatter flags a conflict"],
      ["Source drift", T.source_drift, "source moved on without the note"],
      // "—" not "0" when the leg that measures it never ran: a printed zero
      // reads as "measured, came out flat".
      ["Integration deficits", full ? T.integration_deficits : null, "concept-rich, weakly linked"],
      ["Attention", T.attention_candidates, "idle + missed in recall"],
      ["Lean notes", T.lean_notes, "too thin to carry their topic"],
      ["Structural gaps", T.structural_gaps, "areas that should connect, don't"],
    ].map(([what, n, means]) => ({ what, n: n === null ? "—" : nfmt(n || 0), means })),
  ));
  grid.appendChild(mt);

  // --- reliability tiers -----------------------------------------------------
  if (d.temporal) {
    const tp = d.temporal, bt = tp.by_tier || {};
    const tiers = [
      { tone: "ord-3", label: "human", value: bt["3"] || 0 },
      { tone: "ord-2", label: "grounded", value: bt["2"] || 0 },
      { tone: "ord-1", label: "distilled", value: bt["1"] || 0 },
    ];
    const tc = mCard("Reliability", `${nfmt(tp.notes_scanned)} notes scanned`);
    tc.appendChild(mLegend(tiers.map((t) => ({ tone: t.tone, label: t.label }))));
    tc.appendChild(stackedBar(tiers));
    tc.appendChild(mTable(
      [{ key: "k", label: "Signal" }, { key: "v", label: "Notes", num: true }],
      [
        ...tiers.map((t) => ({ k: "Tier · " + t.label, v: nfmt(t.value) })),
        { k: "Carrying a claim stamp", v: `${nfmt(tp.stamped)} / ${nfmt(tp.notes_scanned)}` },
        { k: "With a Superseded section", v: nfmt(tp.superseded_sections) },
        { k: "Merged away", v: nfmt(tp.superseded_notes) },
        ...(tp.oldest_valid_from ? [{ k: "Earliest valid_from", v: tp.oldest_valid_from }] : []),
      ],
    ));
    grid.appendChild(tc);
  }

  // --- write sessions --------------------------------------------------------
  // What wrote the vault, not when its subjects happened: only a nucleated note
  // carries a claim clock. Reads as coverage — the areas the writing keeps
  // landing in, and the ones it has never reached.
  if (d.sessions?.days?.length) {
    const s = d.sessions;
    const sc = mCard("Write sessions",
      `${s.days.length} days · ${s.areas.length} of ${s.areas_total} areas written into`);
    sc.appendChild(sessionMatrix(s));
    // The unmeasured majority, named. A matrix that omitted it would read as
    // "the whole vault, over 9 days", which is the opposite of true.
    const caveats = [`${nfmt(s.undated)} notes carry no claim clock and have no place here`];
    if (s.untouched) {
      caveats.push(`${nfmt(s.untouched)} areas have never been written into`);
    }
    // Not a rounding loss: these are notes whose name resolves to two areas at
    // once, so any column would be a guess. Printed because the row totals
    // otherwise silently fall short of the dated count.
    if (s.ambiguous) {
      caveats.push(`${nfmt(s.ambiguous)} claims sit on a name that two areas both hold`);
    }
    sc.appendChild(mkEl("p", "mnote", caveats.join(" · ")));
    grid.appendChild(sc);
  }

  // --- code coverage ---------------------------------------------------------
  if (d.code_coverage) {
    const cc = d.code_coverage;
    const card = mCard("Code coverage", "source files with at least one note");
    card.appendChild(meter(cc.documented, cc.total,
      `${nfmt(cc.documented)} / ${nfmt(cc.total)} files documented`));
    if (cc.undocumented?.length) {
      card.appendChild(mTable(
        [{ key: "path", label: "Undocumented" }, { key: "fan_in", label: "Fan-in", num: true }],
        cc.undocumented.slice(0, 10),
      ));
    }
    grid.appendChild(card);
  }

  // --- structural gaps + bridges ---------------------------------------------
  // The gap list used to sit in the graph's HUD, next to the colour keys, where
  // a worklist reads as a legend entry. A gap is a fact about the vault, so it
  // belongs on the surface that measures the vault — and here it can carry the
  // ranking (size × size ÷ links) the HUD had no room for.
  const gp = mCard("Structural gaps", "well-formed areas with few links between them");
  if (d.gaps?.length) {
    // Sizes, not the absent-link fraction: that fraction reads 99.7-100% on
    // every row of a real vault, so it cannot explain why row 1 outranks row
    // 20. Size × size ÷ (1 + links) is the actual ranking, and with both
    // sizes on the row the order is readable instead of asserted.
    gp.appendChild(mTable(
      [{ key: "pair", label: "Area hubs" }, { key: "sizes", label: "Notes", num: true },
       { key: "inter_edges", label: "Links", num: true },
       { label: "", el: (r) => mBridgeBtn(r._a, r._b) }],
      d.gaps.map((g) => ({
        pair: g.a + " ↮ " + g.b, sizes: `${g.size_a} × ${g.size_b}`,
        inter_edges: g.inter_edges, _path: g.a_path, _a: g.a, _b: g.b,
      })),
    ));
  } else mEmpty(gp, "No gaps measured.");
  grid.appendChild(gp);

  const br = mCard("Surprising bridges", "cross-area links between otherwise distant notes");
  if (d.bridges?.length) {
    br.appendChild(mTable(
      [{ key: "pair", label: "Pair" }, { key: "weight", label: "Surprise", num: true }],
      d.bridges.map((b) => ({ pair: b.source + " ↔ " + b.target, weight: b.weight, _path: b.source_path })),
    ));
  } else mEmpty(br, "No cross-area links yet.");
  grid.appendChild(br);

  // --- lists that point at a note -------------------------------------------
  const orph = mCard("Orphans", "nothing links to these");
  if (d.orphans?.length) {
    orph.appendChild(mTable([{ key: "label", label: "Note" }],
      d.orphans.map((o) => ({ ...o, _path: o.path }))));
    const more = mMore(d.orphans.length, T.orphans || 0, "orphans");
    if (more) orph.appendChild(more);
  } else mEmpty(orph, "None. Every note is reachable.");
  grid.appendChild(orph);

  const dg = mCard("Unresolved links", "wikilink targets that don't exist yet");
  if (d.dangling?.length) {
    dg.appendChild(mTable(
      [{ key: "target", label: "Target" }, { key: "refs", label: "Refs", num: true }],
      d.dangling,
    ));
    const more = mMore(d.dangling.length, T.dangling_links || 0, "targets");
    if (more) dg.appendChild(more);
  } else mEmpty(dg, "None. Every wikilink resolves.");
  grid.appendChild(dg);

  const at = mCard("Attention", "idle × missed in recall ÷ how well linked");
  if (d.attention?.length) {
    at.appendChild(mTable(
      [{ key: "label", label: "Note" }, { key: "days_idle", label: "Idle (d)", num: true },
       { key: "misses", label: "Missed", num: true }, { key: "score", label: "Score", num: true }],
      d.attention.map((a) => ({ ...a, _path: a.path })),
    ));
  } else mEmpty(at, "Nothing overdue.");
  grid.appendChild(at);

  if (full) {
    const df = mCard("Integration deficits", "concept-rich text, few wikilinks");
    if (d.deficits?.length) {
      df.appendChild(mTable(
        [{ key: "label", label: "Note" }, { key: "concepts", label: "Concepts", num: true },
         { key: "degree", label: "Links", num: true }, { key: "score", label: "Score", num: true }],
        d.deficits.map((x) => ({ ...x, _path: x.path })),
      ));
    } else mEmpty(df, "None measured.");
    grid.appendChild(df);
  }

  if (d.contested?.length) {
    const ct = mCard("Contested", "frontmatter marks these as in conflict");
    ct.appendChild(mTable(
      [{ key: "label", label: "Note" }, { key: "refs", label: "Conflicts with" }],
      d.contested.map((c) => ({ label: c.label, refs: (c.refs || []).join(", "), _path: c.path })),
    ));
    grid.appendChild(ct);
  }

  // --- proposals (not authoritative) -----------------------------------------
  // The co-occurrence leg runs one expanded ranking per note, so it is minutes
  // on a real vault. Asked for, never assumed.
  if (!full) {
    const ask = mCard("Proposals", "co-occurrence delta, not yet measured");
    ask.classList.add("proposed");
    ask.appendChild(mkEl("p", "mempty",
      "Autolink candidates, stale links, missing hubs and integration deficits "
      + "come from comparing the co-occurrence graph against the wikilinks. "
      + "That pass ranks every note against every other, so it grows with the "
      + "square of the vault: seconds here, longer on a big one."));
    const btn = mkEl("button", "mbtn", "measure proposals");
    btn.type = "button";
    btn.id = "metrics-proposals";
    ask.appendChild(btn);
    grid.appendChild(ask);
  }
  const props = [
    ["Duplicate pairs", "embeddings propose · graph disposes", d.duplicates,
     [{ key: "pair", label: "Pair" }, { key: "score", label: "Cosine", num: true },
      { key: "band", label: "Band" }],
     (x) => ({ pair: x.a + " ↔ " + x.b, score: x.score, band: x.confirmed ? "merge?" : "link", _path: x.a_path }),
     (T.confirmed_duplicates || 0) + (T.duplicate_pairs || 0), "pairs"],
    ["Autolink candidates", "co-mentioned in text, never linked", d.autolinks,
     [{ key: "pair", label: "Pair" }, { key: "shared", label: "Shared concepts" },
      { key: "weight", label: "Weight", num: true }],
     (x) => ({ pair: x.a + " ↔ " + x.b, shared: (x.shared || []).join(", "), weight: x.weight, _path: x.a_path }),
     T.autolink_candidates || 0, "pairs"],
    ["Stale links", "linked, but share no concepts in text", d.stale_links,
     [{ key: "pair", label: "Pair" }],
     (x) => ({ pair: x.a + " ↔ " + x.b, _path: x.a_path }),
     T.stale_links || 0, "links"],
    ["Missing hubs", "central concepts with no note of their own", d.missing_hubs,
     [{ key: "concept", label: "Concept" }, { key: "centrality", label: "Centrality", num: true }],
     (x) => x, T.missing_hubs || 0, "concepts"],
  ];
  for (const [title, sub, rows, cols, map, total, noun] of props) {
    if (!rows?.length) continue;
    const c = mCard(title, sub);
    c.classList.add("proposed");
    c.appendChild(mTable(cols, rows.map(map)));
    const more = total ? mMore(rows.length, total, noun) : null;
    if (more) c.appendChild(more);
    grid.appendChild(c);
  }
}

// --- map landing: root the radial map on a note; hub-picker until one is set --
function rootMap(path) {
  mapRootedPath = path;
  if (graphMode !== "map") setGraphMode("map");
  $("#map-picker").hidden = true;
  $("#map-frame").hidden = false;
  $("#map-loading").hidden = false;
  $("#map-frame").src = "/map?note=" + encodeURIComponent(path)
    + "&theme=" + liveTheme() + "&t=" + Date.now();
  closeNodeResults();
}

function renderMapPicker(hubs) {
  const box = $("#map-picker-list");
  box.innerHTML = "";
  for (const h of hubs) {
    const row = document.createElement("div");
    row.className = "hub-row";
    row.dataset.path = h.path;
    row.innerHTML = '<span class="hub-name"></span><span class="hub-deg"></span>';
    row.querySelector(".hub-name").textContent = h.name;
    row.querySelector(".hub-deg").textContent = h.degree;
    box.appendChild(row);
  }
}
$("#map-picker-list").addEventListener("click", (e) => {
  const row = e.target.closest(".hub-row");
  if (row) rootMap(row.dataset.path);
});

// --- explore note search (network: fly the camera · map: root the map) --------
// A fuzzy ranked picker over the vault's notes, indexed from the sidebar tree —
// same title→prefix→substring→path ranking the graph viewer's own search uses.
let noteIdx = [];      // [{name, path, ln, lp}]
let nodeResults = [];  // current ranked matches
let nodeSel = -1;

function buildNoteIndex() {
  noteIdx = Array.from($("#tree").querySelectorAll(".tree-note")).map((el) => {
    const name = el.textContent, path = el.dataset.id || "";
    return { name, path, ln: name.toLowerCase(), lp: path.toLowerCase() };
  });
}

function scoreNote(n, q) {
  if (n.ln === q) return 5;
  if (n.ln.startsWith(q)) return 4;
  if (n.ln.includes(q)) return 3;
  if (n.lp.includes(q)) return 2;
  return 0;
}

function renderNodeResults(raw) {
  const q = raw.trim().toLowerCase();
  const box = $("#node-results");
  if (!q) { closeNodeResults(); return; }
  nodeResults = noteIdx
    .map((n) => [scoreNote(n, q), n])
    .filter((p) => p[0] > 0)
    .sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name))
    .slice(0, 12)
    .map((p) => p[1]);
  nodeSel = nodeResults.length ? 0 : -1;
  box.innerHTML = "";
  nodeResults.forEach((n, i) => {
    const el = document.createElement("div");
    el.className = "node-result" + (i === nodeSel ? " sel" : "");
    el.innerHTML = '<span class="nr-name"></span><span class="nr-path"></span>';
    el.querySelector(".nr-name").textContent = n.name;
    el.querySelector(".nr-path").textContent = n.path;
    el.addEventListener("click", () => pickNote(n.path));
    box.appendChild(el);
  });
  box.hidden = nodeResults.length === 0;
}

function closeNodeResults() {
  $("#node-results").hidden = true;
  nodeResults = [];
  nodeSel = -1;
}

function moveNodeSel(d) {
  nodeSel = (nodeSel + d + nodeResults.length) % nodeResults.length;
  document.querySelectorAll("#node-results .node-result").forEach((el, i) => el.classList.toggle("sel", i === nodeSel));
}

function pickNote(path) {
  if (graphMode === "map") {
    rootMap(path);
  } else { // network: locate the note and fly the graph camera to it
    const f = $("#graph-frame");
    if (f.contentWindow) f.contentWindow.postMessage({ type: "silica-goto-path", path }, "*");
  }
  $("#node-search").value = "";
  closeNodeResults();
}

$("#node-search").addEventListener("input", (e) => renderNodeResults(e.target.value));
$("#node-search").addEventListener("keydown", (e) => {
  if (!nodeResults.length) return;
  if (e.key === "ArrowDown") { e.preventDefault(); moveNodeSel(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); moveNodeSel(-1); }
  else if (e.key === "Enter") { e.preventDefault(); if (nodeSel >= 0) pickNote(nodeResults[nodeSel].path); }
  else if (e.key === "Escape") { $("#node-search").value = ""; closeNodeResults(); }
});


// --- attachments: drop / "+" accumulate files as chips above the input; they
// are NOT nucleated on drop. The next composer submit uploads them together with
// the typed message, so the agent acts on the files per the user's instruction.
let staged = []; // File objects awaiting the next submit
const attachEls = $("#attachments");

function renderAttachments() {
  attachEls.innerHTML = "";
  attachEls.hidden = staged.length === 0;
  syncQuick(); // staged files are what the next message does
  staged.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span class="chip-name"></span><button type="button" class="chip-x" title="remove">✕</button>`;
    chip.querySelector(".chip-name").textContent = f.name;
    chip.querySelector(".chip-x").addEventListener("click", () => { staged.splice(i, 1); renderAttachments(); });
    attachEls.appendChild(chip);
  });
}
function addFiles(fileList) {
  for (const f of fileList) staged.push(f);
  renderAttachments();
}

// Upload every staged file + the typed text as one turn (server stages them —
// converts PDFs, stubs code — then the agent works on them per `text`).
function nucleateStaged(text) {
  if (streaming || !staged.length) return;
  const names = staged.map((f) => f.name);
  bubble("user").textContent = (text.trim() ? text.trim() + "\n" : "") + "⇪ " + names.join(", ");
  const fd = new FormData();
  for (const f of staged) fd.append("files", f);
  fd.append("text", text);
  staged = [];
  renderAttachments();
  runTurn(fetch("/nucleate", { method: "POST", body: fd }), "staging " + names.length + (names.length === 1 ? " file" : " files"));
}

let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; document.body.classList.add("dragging"); });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", (e) => { e.preventDefault(); if (--dragDepth <= 0) document.body.classList.remove("dragging"); });
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("dragging");
  if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});

// "+" opens the native picker, constrained to what the nucleate lanes accept.
const nucleateInput = $("#nucleate-file");
fetch("/supported_types")
  .then((r) => r.json())
  .then((d) => { nucleateInput.accept = (d.extensions || []).join(","); })
  .catch(() => {}); // accept="" just means the picker shows all files
$("#attach").addEventListener("click", () => nucleateInput.click());
nucleateInput.addEventListener("change", () => {
  addFiles(nucleateInput.files);
  nucleateInput.value = ""; // reset so re-picking the same file fires change again
});

// --- note panel (right overlay drawer; opens from .note-link, the graph, and the map) -
const notePanel = $("#note-panel");
let lastNotePath = null;   // note currently open in the drawer
let lastViewedPath = null; // survives close — feeds the header reopen button

// The dock inset and the drawer width must agree; CSS reads it as --note-w.
function setNoteW(w) {
  document.documentElement.style.setProperty("--note-w", w + "px");
  syncDrawerToViews(); // the frame parks its focus bar against the drawer's edge
}

// Mirror the open note onto the graph + map iframes: the matching node + its
// 1-hop neighbours go full-opacity, everything else dims. No-op harmlessly if
// a tab was never opened (contentWindow still exists, message just has no
// listener yet).
function postToViews(msg) {
  for (const id of ["#graph-frame", "#map-frame"]) {
    const frame = $(id);
    if (frame.contentWindow) frame.contentWindow.postMessage(msg, "*");
  }
}
// The last focus INTENT, replayed whenever a view (re)loads. A frame that is
// still loading drops the message, and /graph is rebuilt on every entry into
// the explore tab — so "light these notes" issued from the chat tab used to be
// posted into a loading iframe and then overwritten by the load handler.
let graphFocus = [];
function focusGraphNode(path) {
  graphFocus = path ? [path] : [];
  replayGraphFocus();
}
// Same, for a SET: a concept lights every note that carries it.
function focusGraphNodes(paths) {
  graphFocus = paths || [];
  replayGraphFocus();
}
// Both shapes, in order: the radial map only speaks the single-path message,
// the graph speaks both and the set arrives second, so it wins there.
function replayGraphFocus() {
  postToViews({ type: "silica-focus-path", path: graphFocus[0] || null });
  if (graphFocus.length > 1) postToViews({ type: "silica-focus-paths", paths: graphFocus });
}

// Explore does not inset for the drawer the way chat does — the graph keeps its
// full width and the drawer overlays it, so the frame's own HUD sits under a
// translucent panel and reads through the note. The frame cannot see the
// drawer, so tell it. Replayed on frame load like the focus state, because
// /graph is rebuilt on every entry into the tab.
function syncDrawerToViews() {
  const open = document.body.classList.contains("note-open");
  postToViews({ type: "silica-host-drawer", open, width: open ? notePanel.offsetWidth : 0 });
}

// Mermaid is a 3.5MB vendored bundle, so it loads on demand — only the first
// time an opened note actually contains a ```mermaid fence. Render failures
// leave the fence as plain text (suppressErrorRendering).
let mermaidLoad = null;

// Read the palette instead of restating it. The old block carried five hex
// literals copied out of app.css, and by the time anyone looked they were two
// revisions stale — which is the failure mode a second theme turns from a
// blemish into a wrong-coloured diagram on a white page. getComputedStyle on
// :root returns whichever ramp is live, so this cannot drift from either.
// `base` in both themes, never "dark"/"default". Mermaid derives a theme object
// on the first initialize() and a later one does not rebuild it: switching
// theme:"dark" -> theme:"default" re-rendered every diagram in mermaid's own
// lavender defaults with our themeVariables sitting in the live config, ignored.
// `base` is the theme that exists to be driven entirely by those variables, so
// nothing has to be re-derived and there is one code path instead of two.
function mermaidConfig() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  const light = document.documentElement.dataset.theme === "light";
  return {
    startOnLoad: false, suppressErrorRendering: true, theme: "base",
    fontFamily: "Martian Mono, ui-monospace, monospace",
    themeVariables: {
      darkMode: !light,
      background: v("--void"),
      primaryColor: v("--slate-2"),
      primaryTextColor: v("--frost"),
      primaryBorderColor: v("--line-2"),
      lineColor: v("--ash"),
      // base derives the rest from these four, and derives them wrong when the
      // ramp is not a neutral gray: naming them is cheaper than correcting it.
      secondaryColor: v("--slate"),
      tertiaryColor: v("--sheet"),
      mainBkg: v("--slate-2"),
      textColor: v("--text"),
    },
  };
}

function renderMermaid(root) {
  const blocks = root.querySelectorAll("pre.mermaid");
  if (!blocks.length) return;
  // Keep the source: mermaid.run() replaces the fence's text with an <svg>, so
  // without this a repaint has nothing left to re-render from.
  blocks.forEach((b) => { if (!b.dataset.mmd) b.dataset.mmd = b.textContent; });
  mermaidLoad ||= new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = "/static/mermaid.min.js";
    s.onload = () => { mermaid.initialize(mermaidConfig()); resolve(); };
    document.head.appendChild(s);
  });
  mermaidLoad.then(() => mermaid.run({ nodes: blocks }).catch(() => {}));
}

// A diagram is baked SVG, so it does not follow a token swap the way the rest
// of the page does — every rendered fence has to be rewound to its source and
// drawn again. Nothing to do if the bundle was never loaded.
function repaintMermaid() {
  if (!mermaidLoad) return;
  mermaidLoad.then(() => {
    mermaid.initialize(mermaidConfig());
    const done = document.querySelectorAll("pre.mermaid[data-processed]");
    if (!done.length) return;
    done.forEach((b) => {
      b.removeAttribute("data-processed");
      b.textContent = b.dataset.mmd || "";
    });
    mermaid.run({ nodes: done }).catch(() => {});
  });
}

// The transcript is what the drawer exists to serve, so it gets a floor and the
// two panes beside it negotiate around it.
//
// The old rule was a fixed 1100px breakpoint, and it made the layout NON-MONOTONIC:
// below 1100 the sidebar yielded, but at 1200 it stayed (264px) while the drawer
// kept its full 630, leaving 306px of log — measured at 21 characters of prose per
// line, worse than at the 900px floor. The window got bigger and the transcript got
// smaller. 1280 and 1366 are the two commonest laptop widths after 1440, so the
// broken band was the common case.
const MIN_PROSE = 560;   // px of transcript that must survive, ~47ch of prose once
                         // the sheet's and the row's padding come out of it
const MIN_DRAWER = 320;  // below this the drawer stops being worth its own pane
const SIDE_W = 264;      // must match --side-w
let sidebarYielded = false;

// What the drawer may take before the transcript drops below its floor.
function drawerBudget(sidebarOn) {
  return window.innerWidth - (sidebarOn ? SIDE_W : 0) - MIN_PROSE;
}

// Single owner of the open-drawer layout: decides whether the sidebar can stay,
// then sizes the drawer to whatever is left over the floor. Runs on open, on
// resize and after a drag, so the same window can no longer sit in two different
// layouts depending on the order those happened in.
function fitPanes() {
  if (!document.body.classList.contains("note-open")) return;
  const userCollapsed = document.body.classList.contains("sidebar-collapsed") && !sidebarYielded;
  if (!userCollapsed) {
    if (drawerBudget(true) >= MIN_DRAWER) restoreYieldedSidebar();
    else yieldSidebarToDrawer();
  }
  const sidebarOn = !document.body.classList.contains("sidebar-collapsed");
  const want = parseInt(localStorage.getItem("note-width"), 10) || 630;
  const w = Math.max(MIN_DRAWER, Math.min(want, drawerBudget(sidebarOn)));
  notePanel.style.width = w + "px";
  setNoteW(w);
}

function yieldSidebarToDrawer() {
  if (document.body.classList.contains("sidebar-collapsed")) return;
  document.body.classList.add("sidebar-collapsed");
  sidebarYielded = true;
}

function restoreYieldedSidebar() {
  if (!sidebarYielded) return;
  document.body.classList.remove("sidebar-collapsed");
  sidebarYielded = false;
}

// The drawer has two readings of the same note. `note` is the reader, exactly as
// before. `context` is what the vault knows about it — concepts, how it IS
// connected, how it SHOULD be — all deterministic, one blocking /context call.
//
// The click contract: naming a note means "I want to read it", so a wikilink and
// the file tree always land in the reader, even when the drawer is already in
// context. Pointing at a node means "what is this", so the graph, the map and
// the metrics rows land in context.
let drawerMode = "note";
let ghostName = null; // set while the drawer holds an unresolved link, which has no reader

function syncDrawerMode() {
  const path = lastNotePath || lastViewedPath;
  document.querySelectorAll("#note-mode button").forEach((b) => {
    setActive(b, b.dataset.mode === drawerMode);
    // A ghost has no file to read; the reader half stops being an offer.
    if (b.dataset.mode === "note") b.disabled = !!ghostName;
    // Same rule for diff: a note this session never touched has no diff, and an
    // enabled tab onto an empty pane is a promise the drawer cannot keep.
    if (b.dataset.mode === "diff") {
      b.disabled = !!ghostName || !changedPaths.has(path);
      b.title = b.disabled ? "this session has not changed this note"
                           : "what this session changed in this note";
    }
  });
  // The five actions act on the SELECTED NOTE, so they survive the mode switch
  // — but a ghost has no note to act on, and an enabled button that does
  // nothing is the same silent no-op this drawer exists to fix.
  document.querySelectorAll("#note-actions .na").forEach((b) => { b.disabled = !!ghostName; });
  $("#note-body").hidden = drawerMode !== "note";
  $("#note-context").hidden = drawerMode !== "context";
  $("#note-diff").hidden = drawerMode !== "diff";
}

// Shared tail of both openers: raise the panel and let fitPanes negotiate the
// widths. Kept in one place so the two modes cannot drift apart on layout.
function showDrawer(title) {
  $("#note-title").textContent = title || "";
  notePanel.classList.add("open");
  notePanel.setAttribute("aria-hidden", "false");
  document.body.classList.add("note-open"); // dock + chat inset to the drawer's edge
  syncDrawerToViews();
  fitPanes(); // owns the sidebar decision AND the drawer width, in that order
  $("#note-last").querySelector("span").textContent = title || "";
}

async function openNote(path) {
  if (!path) return;
  lastNotePath = path;
  lastViewedPath = path;
  ghostName = null;
  drawerMode = "note";
  syncDrawerMode();
  focusGraphNode(path);
  try {
    const r = await fetch("/note?path=" + encodeURIComponent(path));
    const data = await r.json();
    $("#note-body").innerHTML = data.html || "";
    renderMermaid($("#note-body"));
    $("#note-body").scrollTop = 0;
    showDrawer(data.title || path);
  } catch { notify("couldn't open that note"); }
}

// `target` is {path} for a real note, or {name, ghost:true} for an unresolved
// wikilink — which has no body, no reader, and one action: write it.
async function openContext(target) {
  const path = target.path || "";
  const ghost = !!target.ghost || !path;
  if (!path && !target.name) return;
  ghostName = ghost ? (target.name || path) : null;
  if (!ghost) { lastNotePath = path; lastViewedPath = path; }
  drawerMode = "context";
  syncDrawerMode();
  focusGraphNode(ghost ? null : path);
  const box = $("#note-context");
  box.textContent = "reading the vault…";
  box.className = "cx-wait";
  showDrawer(ghost ? (target.name || "") : (path.split("/").pop().replace(/\.md$/, "")));
  try {
    const q = ghost
      ? "ghost=1&name=" + encodeURIComponent(target.name || "")
      : "path=" + encodeURIComponent(path);
    const data = await (await fetch("/context?" + q)).json();
    box.className = "";
    renderContext(data);
    box.scrollTop = 0;
    showDrawer(data.title || target.name || path);
  } catch {
    box.className = "cx-wait";
    box.textContent = "couldn't read that note's context";
  }
}

$("#note-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]");
  if (!b || b.disabled || b.dataset.mode === drawerMode) return;
  const path = lastNotePath || lastViewedPath;
  if (b.dataset.mode === "note") openNote(path);
  else if (b.dataset.mode === "diff") openDiff(path);
  else openContext({ path });
});

// --- diff mode ---------------------------------------------------------------
// The note against how it stood before this session touched it. Red is what left
// the file, green is what arrived; the gaps are the unchanged stretches the
// server left out. Rows are DOM nodes, never innerHTML: every line here is a line
// of the user's own vault.
async function openDiff(path) {
  if (!path) return;
  lastNotePath = path;
  lastViewedPath = path;
  ghostName = null;
  drawerMode = "diff";
  syncDrawerMode();
  focusGraphNode(path);
  const box = $("#note-diff");
  box.className = "dl-wait";
  box.textContent = "reading the diff…";
  showDrawer(path.split("/").pop().replace(/\.md$/, ""));
  let d;
  try {
    d = await (await fetch("/changes/diff?path=" + encodeURIComponent(path))).json();
  } catch {
    box.className = "dl-wait";
    box.textContent = "couldn't read that diff";
    return;
  }
  box.className = "";
  box.innerHTML = "";
  const head = mkEl("div", "dl-head");
  head.appendChild(mkEl("span", "dl-kind " + d.kind, d.kind));
  if (d.from) head.appendChild(mkEl("span", "dl-from", d.from + " →"));
  if (d.added) head.appendChild(mkEl("span", "chg-add", "+" + d.added));
  if (d.removed) head.appendChild(mkEl("span", "chg-del", "−" + d.removed));
  box.appendChild(head);
  if (!d.lines.length) {
    box.appendChild(mkEl("div", "dl-empty", d.kind === "moved"
      ? "moved — the bytes are unchanged"
      : "no difference left: this note is back to how it started"));
  }
  const track = mkEl("div", "dl-track");
  const CLS = { "+": "dl-add", "-": "dl-del", "@": "dl-gap" };
  for (const l of d.lines) {
    const cls = CLS[l.op] || "dl-ctx";
    // A gap is the elision itself, not a line of the note.
    track.appendChild(mkEl("div", "dl-line " + cls, l.op === "@" ? "⋯" : l.op + l.text));
  }
  box.appendChild(track);
  if (d.clipped)
    box.appendChild(mkEl("div", "dl-empty",
      `${d.clipped} more lines — open the note for the rest`));
  box.scrollTop = 0;
  showDrawer(d.name || path);
}

function closeNote() {
  notePanel.classList.remove("open");
  notePanel.setAttribute("aria-hidden", "true");
  document.body.classList.remove("note-open");
  syncDrawerToViews();
  restoreYieldedSidebar();
  lastNotePath = null; // lastViewedPath survives — the header button can reopen
  ghostName = null;
  focusGraphNode(null);
}
$("#note-last").addEventListener("click", () => {
  if (lastViewedPath) openNote(lastViewedPath);
});

// --- context mode rendering --------------------------------------------------
// Built from DOM nodes, never innerHTML: every string here is vault data.
function cxSection(title, sub) {
  const sec = mkEl("div", "cx-sec");
  sec.appendChild(mkEl("div", "cx-label", title));
  if (sub) sec.appendChild(mkEl("div", "cx-sub", sub));
  return sec;
}

// A note row. The name opens that note's CONTEXT — which is what makes the
// drawer navigable, and the graph mirrors each hop. The small icon opens the
// READER, for when you meant "let me actually read this one".
function cxRow(r, why) {
  const row = mkEl("div", "cx-row");
  const name = mkEl("button", "cx-name", r.name || r.path);
  name.type = "button";
  if (r.path) name.addEventListener("click", () => openContext({ path: r.path }));
  else { name.disabled = true; name.title = "not a note in this vault"; }
  row.appendChild(name);
  if (why) row.appendChild(mkEl("span", "cx-why", why));
  if (r.path) {
    const open = mkEl("button", "cx-open", "↗");
    open.type = "button";
    open.title = "read this note";
    open.setAttribute("aria-label", "read " + (r.name || r.path));
    open.addEventListener("click", () => openNote(r.path));
    row.appendChild(open);
  }
  return row;
}

// Row lists get a floor of five and a native <details> for the tail. Five rows
// are enough to see what kind of neighbourhood a note sits in; the rest is one
// click away when the question is "all of them". <details> because the browser
// already owns this toggle — no open/closed state to keep in JS, and it
// survives a re-render by simply not existing across one.
const CX_VISIBLE = 5;
function cxList(sec, items, make) {
  items.slice(0, CX_VISIBLE).forEach((it) => sec.appendChild(make(it)));
  const rest = items.slice(CX_VISIBLE);
  if (!rest.length) return;
  const more = mkEl("details", "cx-more");
  more.appendChild(mkEl("summary", null, rest.length + " more"));
  rest.forEach((it) => more.appendChild(make(it)));
  sec.appendChild(more);
}

// Weight drives font size — but the RAMP is scaled by how far the weights
// actually spread. A plain min-max map puts weight 1 at the floor and weight 2
// at the ceiling, so a trivial 2:1 difference reads as loud as a 20:1 one. The
// full 12→20px ramp is spent only at a 4x spread or more; a flat cloud stays
// flat rather than pretending to a hierarchy the data does not have.
function cxCloud(concepts) {
  const box = mkEl("div", "cx-cloud");
  const ws = concepts.map((c) => c.weight || 1);
  const max = Math.max(...ws), min = Math.min(...ws);
  const span = max > min ? Math.min(1, Math.log2(max / min) / 2) : 0;
  for (const c of concepts) {
    const b = mkEl("button", "cx-concept", c.concept);
    b.type = "button";
    const t = max > min ? ((c.weight || 1) - min) / (max - min) : 0;
    b.style.fontSize = (12 + Math.round(t * span * 8)) + "px";
    b.title = "weight " + (c.weight || 1) + " — light its notes in the graph";
    b.addEventListener("click", () => lightConcept(c.concept, b));
    box.appendChild(b);
  }
  return box;
}

// A concept is a set of notes, so it focuses a set. Clicked from chat it also
// switches to explore first — there is nothing to see otherwise.
async function lightConcept(term, btn) {
  document.querySelectorAll(".cx-concept.lit").forEach((e) => e.classList.remove("lit"));
  btn.classList.add("lit");
  if (activeTab !== "graph") showTab("graph");
  try {
    const d = await (await fetch("/concept?term=" + encodeURIComponent(term))).json();
    if (!d.notes || !d.notes.length) { notify("no notes carry “" + term + "”"); return; }
    focusGraphNodes(d.notes);
  } catch { notify("couldn't resolve that concept"); }
}

// Suggested rows never write. They prefill a chat turn and hand it back to you,
// so the write still goes through the agent's gate — validate, checkpoint, undo
// journal — instead of a drawer button reaching the disk on its own.
function prefillChat(text) {
  showTab("chat");
  const box = $("#input");
  box.value = text;
  box.focus();
  box.dispatchEvent(new Event("input")); // let the autosize/palette hooks see it
}

// Two notes can share a name (A/Cell and B/Cell). Two rows both reading "Cell"
// are not a list, they are a coin flip — so any name that repeats in this
// drawer carries its path instead.
function disambiguator(groups) {
  // Distinct PATHS per name, not occurrences: a mutual link puts the same note
  // under both "links to" and "linked from", and that is one note, not two.
  const paths = {};
  for (const g of groups) for (const r of g || []) (paths[r.name] ||= new Set()).add(r.path || r.name);
  return (r) => (paths[r.name] && paths[r.name].size > 1 && r.path
    ? { ...r, name: r.path.replace(/\.md$/, "") }
    : r);
}

function renderContext(data) {
  const box = $("#note-context");
  box.textContent = "";
  if (data.error) {
    box.className = "cx-wait";
    box.textContent = data.error;
    return;
  }
  const rel = data.related || {};
  const has = (a) => a && a.length;
  const label = disambiguator([rel.frontmatter, rel.outgoing, rel.backlinks, data.suggested]);

  if (data.ghost) {
    const s = cxSection("unresolved link",
      "no file carries this name yet — these notes already point at it");
    box.appendChild(s);
  }
  if (data.hint) box.appendChild(mkEl("div", "cx-hint", data.hint));

  if (has(data.snippets)) {
    const s = cxSection("key snippets");
    for (const sn of data.snippets) {
      const row = mkEl("div", "cx-snip");
      if (sn.heading) row.appendChild(mkEl("span", "cx-snip-h", sn.heading));
      row.appendChild(mkEl("span", "cx-snip-t", sn.text));
      s.appendChild(row);
    }
    box.appendChild(s);
  }

  if (has(data.concepts)) {
    const s = cxSection("concepts", "click one to light its notes in the graph");
    s.appendChild(cxCloud(data.concepts));
    box.appendChild(s);
  }

  const relRows = [
    ["related:", rel.frontmatter], ["links to", rel.outgoing], ["linked from", rel.backlinks],
  ].filter(([, v]) => has(v));
  if (relRows.length) {
    const s = cxSection("connected", "how this note IS connected today");
    for (const [groupLabel, rows] of relRows) {
      s.appendChild(mkEl("div", "cx-group", groupLabel));
      cxList(s, rows, (r) => cxRow(label(r)));
    }
    box.appendChild(s);
  }

  if (data.ghost) {
    const s = cxSection("missing");
    const b = mkEl("button", "cx-write", "write this note");
    b.type = "button";
    b.addEventListener("click", () => prefillChat(
      'Write the note "' + data.title + '". It is already linked from ' +
      (rel.backlinks || []).map((r) => '"' + r.name + '"').join(", ") +
      ", so ground it in what those notes already say."));
    s.appendChild(b);
    box.appendChild(s);
  } else if (has(data.suggested)) {
    // The subtitle says the METHOD, not the effect. Two machines feed this list
    // and neither is a model: ghost rows come from wikilinks you already wrote,
    // note rows from embedding + co-occurrence distance (each row's `why` says
    // which). Naming the machine is what lets you weigh a suggestion instead of
    // reading it as a recommendation.
    const s = cxSection("suggested next",
      "no LLM — links you wrote + embedding/co-occurrence; a click drafts the turn");
    cxList(s, data.suggested, (sg) => {
      const row = cxRow(label(sg), sg.why);
      const act = mkEl("button", "cx-do", sg.kind === "ghost" ? "write" : "link");
      act.type = "button";
      act.addEventListener("click", () => prefillChat(sg.kind === "ghost"
        ? 'Write the note "' + sg.name + '", which "' + data.title + '" already links to.'
        : 'Check whether "' + data.title + '" and "' + sg.name + '" belong linked, and if ' +
          "they do, add the wikilink in whichever direction reads right."));
      row.appendChild(act);
      return row;
    });
    box.appendChild(s);
  }

  if (!box.childElementCount) {
    box.className = "cx-wait";
    box.textContent = "nothing indexed for this note yet — run /report or /embed to build the graph";
  }
}

// "map" button in the drawer header — jump to explore's map mode, rooted here.
// Capture the path FIRST: the programmatic tab .click() bubbles to the document
// outside-click handler, which closes the drawer and nulls lastNotePath
// synchronously before rootMap runs (else note=null). Pre-set graphMode so the
// tab-enter goes straight to map instead of loading the graph first.
$("#note-map").addEventListener("click", () => {
  const note = lastNotePath;
  if (!note) return;
  graphMode = "map";
  document.querySelector('.tab[data-tab="graph"]').click();
  rootMap(note);
});

// summarize / explain / quiz — dispatch the reader slash-command for the open
// note as a chat turn. The drawer stays open (the peek dock tucks under it and
// mirrors the turn), so the note you launched from is never lost.
const shellQuote = (s) => '"' + String(s).replace(/"/g, '\\"') + '"';
function drawerReader(makeCmd) {
  if (!lastNotePath || streaming) return; // streaming: send() would no-op — no peek either
  const cmd = makeCmd(lastNotePath, $("#note-title").textContent.trim());
  if (activeTab !== "chat") openPeek(cmd); // on chat the stream is already visible
  send(cmd);
}
$("#note-summarize").addEventListener("click", () => drawerReader((p) => "/summarize " + shellQuote(p)));
$("#note-explain").addEventListener("click", () => drawerReader((p, t) => "/explain " + shellQuote(t || p)));
$("#note-quiz").addEventListener("click", () => drawerReader((p) => "/quiz " + shellQuote(p)));
$("#note-relate").addEventListener("click", () => drawerReader((p) => "/relate " + shellQuote(p)));

// --- dock card (rendered answer for a dock- or drawer-launched turn) ---------
// Not a re-implementation of the chat flow: no tools, no thinking text. Title =
// the dispatched prompt; body = pulsing "thinking", then the answer as live
// markdown (mdLite), upgraded to the canonical OFM render on `done` — so
// wikilinks in the card open the note drawer and focus the graph. One exchange
// only; the next one replaces it. "open in chat" → the full transcript.
const peekEl = $("#peek");
let peek = null; // { body, caret, raw } while a turn is being mirrored
function openPeek(title) {
  const body = $("#peek-body");
  body.className = "";
  body.textContent = "thinking";
  const caret = document.createElement("span"); // own instance: the chat caret is a
  caret.className = "caret";                    // single element, re-parented live
  caret.textContent = "▍";
  body.appendChild(caret);
  $("#peek-title").textContent = title;
  peekEl.hidden = false;
  peek = { body, caret, raw: "", mark: 0 };
}
function closePeek() {
  peekEl.hidden = true;
  peek = null;
}
// Freeze: stop mirroring, drop the caret, leave the card up until dismissed.
function freezePeek() {
  if (!peek) return;
  peek.caret.remove();
  peek = null;
}
function peekDelta(text) {
  if (!peek) return;
  peek.raw += text;
  peek.body.innerHTML = mdLite(peek.raw);
  (peek.body.lastElementChild || peek.body).appendChild(peek.caret);
  peek.body.scrollTop = peek.body.scrollHeight;
}
// The dock mirrors the same text deltas as one flat string, so a retraction has
// to cut it back too — to the last tool block, not to zero: text the chat pane
// already committed above one still stands.
function peekMark() { if (peek) peek.mark = peek.raw.length; }
function peekRollback() {
  if (!peek) return;
  peek.raw = peek.raw.slice(0, peek.mark);
  peek.body.innerHTML = mdLite(peek.raw);
  (peek.body.lastElementChild || peek.body).appendChild(peek.caret);
}
// `done` upgrade: the server's canonical OFM render (wikilinks, callouts, math),
// same swap the chat pane does. Also covers no-delta turns (raw still empty).
function peekDone(ev) {
  if (!peek) return;
  if (ev.html || ev.answer) peek.body.innerHTML = ev.html || escapeHtml(ev.answer);
  freezePeek();
}
function peekError(msg) {
  if (!peek) return;
  peek.body.classList.add("error");
  peek.body.textContent = "error: " + msg;
  peek = null; // frozen; card stays until dismissed
}
$("#peek-open-chat").addEventListener("click", () => {
  document.querySelector('.tab[data-tab="chat"]').click(); // tab handler closes the peek
});
$("#peek-close").addEventListener("click", closePeek);

// --- note panel resize (drag left edge, clamped) ----------------------------
const NOTE_MIN_W = 280, NOTE_MAX_W = 800;
const savedNoteWidth = parseInt(localStorage.getItem("note-width"), 10);
if (savedNoteWidth) notePanel.style.width = Math.min(NOTE_MAX_W, Math.max(NOTE_MIN_W, savedNoteWidth)) + "px";
// Read the rendered width, not the inline style: with no saved width the inline
// style is "" and the old `|| 420` fallback set --note-w to 420 while the panel
// rendered at its stylesheet width, so the header and dock reserved 210px too
// little and the drawer covered #stop and #dock-send on every fresh profile.
const syncNoteW = () => setNoteW(Math.round(notePanel.getBoundingClientRect().width));
syncNoteW();
// Toasts now hang under the header instead of over the composer, so they need its
// REAL height: the strip wraps to two rows when the drawer is open on a narrow
// window, and a hardcoded offset would put them on top of it there.
const headerEl = document.querySelector("header");
// Unrounded: this also sets the note drawer's title strip, so that the two bands
// across the top of the window meet flush. Math.round turned a 36.5px header
// into a 37px strip beside it, and half a pixel of step is still a step. The
// toast offset that first needed this value does not care about the fraction.
const syncHeaderH = () => document.documentElement.style.setProperty(
  "--header-h", headerEl.getBoundingClientRect().height + "px");
syncHeaderH();
new ResizeObserver(syncHeaderH).observe(headerEl);
// The drawer's width is viewport-relative, so it changes with the window.
// --note-w drives the header and dock insets, so it has to follow or they reserve
// the wrong gap and the drawer covers #stop / #dock-send again. fitPanes() is the
// one that re-negotiates against the prose floor; syncNoteW is the fallback for a
// resize with the drawer closed.
window.addEventListener("resize", () => { fitPanes(); syncNoteW(); });
let resizingNote = false; // guards the outside-click-closes handler below: a drag
                           // that ends outside #note-panel fires a "click" there too
$("#note-resize").addEventListener("mousedown", (e) => {
  e.preventDefault();
  resizingNote = true;
  const startX = e.clientX, startWidth = notePanel.getBoundingClientRect().width;
  const onMove = (e2) => {
    // The drag is clamped by the same prose floor the automatic fit obeys, so a
    // user cannot hand-drag the transcript down to four words a line either.
    const cap = Math.max(MIN_DRAWER, drawerBudget(!document.body.classList.contains("sidebar-collapsed")));
    const w = Math.min(NOTE_MAX_W, cap, Math.max(NOTE_MIN_W, startWidth + (startX - e2.clientX)));
    notePanel.style.width = w + "px";
    // Read the rendered width, not the requested one: max-width can clamp the
    // drawer on a narrow window, and --note-w must never disagree with it.
    setNoteW(Math.round(notePanel.getBoundingClientRect().width));
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    localStorage.setItem("note-width", Math.round(notePanel.getBoundingClientRect().width));
    setTimeout(() => { resizingNote = false; }, 0); // clear after this click event finishes
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
});
// One delegated handler: .note-link (chat OR in-panel → in-place nav) opens the
// drawer; a click outside an open drawer closes it. The sidebar and the dock
// are persistent instruments — picking a note, toggling a folder, or typing a
// question about the open note must not close the drawer or reset the graph
// focus, so they never count as "outside". Neither does the reopen button
// (its own listener would immediately fight the close).
document.addEventListener("click", (e) => {
  if (resizingNote) return;
  // dismiss the explore note-search dropdown on any click outside it (a result
  // click runs its own handler first, so pickNote still fires)
  if (!e.target.closest("#node-search-wrap")) closeNodeResults();
  const link = e.target.closest(".note-link, .wc-open");
  if (link) { e.preventDefault(); openNote(link.dataset.path); return; }
  // An external link opens in its own tab. The app has no internal <a href> of
  // its own — every in-app move is JS — so any href in the flow came out of a
  // rendered answer or note, and following it in place tore down the SPA: the
  // turn, the open drawer and the graph focus all went with it. Scoped to
  // http(s) on purpose: a `[text](nota.md)` still resolves against the origin
  // the way it does today, rather than opening a new tab on a 404.
  const ext = e.target.closest('a[href^="http:"], a[href^="https:"]');
  if (ext) { e.preventDefault(); window.open(ext.href, "_blank", "noopener"); return; }
  if (notePanel.classList.contains("open") &&
      !e.target.closest("#note-panel") && !e.target.closest("#sidebar") &&
      !e.target.closest("#dock") && !e.target.closest("#note-last")) closeNote();
});
$("#note-close").addEventListener("click", closeNote);
// (Escape is handled once, at the bottom of this file, in priority order.)
// Graph node clicks (in the iframe) post a message up when embedded.
window.addEventListener("message", (e) => {
  if (!e.data) return;
  if (e.data.type === "silica-open-note") openNote(e.data.path);
  // Graph nodes and map cards point rather than name, so they open context.
  // A ghost node arrives with no path at all — context is the only mode that
  // can say anything about an unresolved link.
  if (e.data.type === "silica-open-context") openContext(e.data);
});

// --- session bootstrap (re-render server-side history; never resets on load) -
async function loadVault() {
  try {
    const r = await fetch("/messages");
    $("#vault").textContent = r.headers.get("X-Silica-Vault") || "";
    setCtxTokens(r.headers.get("X-Silica-Context-Tokens"), r.headers.get("X-Silica-Max-Context-Tokens"));
    const msgs = await r.json();
    log.innerHTML = "";
    // One reply is ONE bubble. The model's text arrives split around its own
    // tool calls, so the history holds several assistant messages per turn —
    // rendering each as its own bubble made a single answer read as three
    // separate replies, with the steps between them missing entirely.
    let turn = null;
    for (const m of msgs) {
      if (m.role === "user") { turn = null; bubble("user").textContent = m.content; continue; }
      if (!turn) {
        turn = { body: bubble("silica"), raw: [] };
        const t = turn;
        addCopyBtn(t.body, () => t.raw.join("\n\n"));
      }
      // Thinking first, then what it produced — the order the stream had. It
      // replays collapsed: the live block is open only while it is the tail.
      if (m.thinking) {
        const d = document.createElement("details");
        d.className = "thinking";
        d.innerHTML = '<summary>thinking</summary><div class="thinking-body"></div>';
        d.querySelector(".thinking-body").textContent = m.thinking;
        turn.body.appendChild(d);
      }
      if (m.content) {
        const seg = document.createElement("div");
        seg.className = "stream-text";
        seg.innerHTML = m.html || escapeHtml(m.content);
        turn.body.appendChild(seg);
        turn.raw.push(m.content);
      }
      if (m.tools && m.tools.length) {
        const g = document.createElement("div");
        g.className = "tools";
        for (const t of m.tools) {
          const d = document.createElement("div");
          if (t.summary) {
            // The run's outcome, restated from the stored tool result. Without
            // it a reloaded chat could only say the injector had run — not what
            // it wrote, or which chunks died.
            d.className = "tool tool-pipeline collapsed " + t.summary.kind;
            d.innerHTML = `<div class="pipe-head"><span class="pipe-title"></span></div>`;
            d.querySelector(".pipe-title").textContent = injectorSummaryLine(t.target || "?", t.summary);
            if (t.summary.failed_chunks.length) {
              const f = document.createElement("div");
              f.className = "pipe-failed";
              f.textContent = t.summary.failed_chunks.map((x) => `✗ ${x.chunk}${x.phase ? " " + x.phase : ""}`).join(" · ");
              d.appendChild(f);
              d.classList.remove("collapsed");
            }
          } else {
            d.className = "tool " + (t.error ? "error" : "done");
            d.textContent = (t.error ? "✗ " : "✓ ") + toolLabel(t);
          }
          g.appendChild(d);
        }
        turn.body.appendChild(g);
      }
    }
    log.scrollTop = log.scrollHeight;
  } catch { notify("couldn't load the conversation"); }
}
// --- quick-action launch pad (empty chat only; CSS collapses it on first turn).
// A segmented control, not four buttons: the pill says what the next message
// will do. It is DERIVED, never stored — from the command already in the box,
// or from files waiting to be nucleated — so a segment cannot claim a mode the
// composer isn't in. Picking a segment only prefills; the user still hits enter.
const qaTrack = $(".qa-track");
const qaPill = $("#qa-pill");

function syncQuick() {
  const cmd = (input.value.match(/^\/[a-z-]+/) || [""])[0];
  const want = staged.length ? "/nucleate" : cmd; // staged files outrank a typed command
  const segs = Array.from(qaTrack.querySelectorAll(".qa"));
  const on = segs.find((b) => b.dataset.action === want) || segs[0]; // unknown command → ask
  segs.forEach((b) => b.setAttribute("aria-pressed", String(b === on)));
  // The pill sits at the track's padding-box origin, so the offset is just the
  // segment's rect minus that origin (clientLeft/Top are the track's border).
  // Both axes: the track wraps on a narrow pane and centres each row, so the
  // segments move vertically AND horizontally under it.
  const a = on.getBoundingClientRect(), t = qaTrack.getBoundingClientRect();
  const x = a.left - t.left - qaTrack.clientLeft, y = a.top - t.top - qaTrack.clientTop;
  // Vertically the pill is the full height of its ROW of the track, not of the
  // segment: it has to read as a lens sliding along the container. The inset is
  // measured off the first segment (always on row one), never hardcoded.
  const pad = segs[0].getBoundingClientRect().top - t.top - qaTrack.clientTop;
  qaPill.style.width = on.offsetWidth + "px";
  qaPill.style.height = on.offsetHeight + 2 * pad + "px";
  qaPill.style.transform = `translate(${x}px, ${y - pad}px)`;
}

$("#quick-actions").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const a = btn.dataset.action;
  input.value = input.value.replace(/^\/[a-z-]*\s*/, ""); // swap the command, keep the text
  if (a) input.value = a + " " + input.value;
  input.focus();
  autoGrow(input);
  renderCommands(input.value); // syncQuick runs from here
});

syncQuick();
// Lexend loads async and the segments get wider when it lands; the track also
// re-wraps whenever the sidebar or the drawer renegotiates the pane. Both are
// resizes of the track, so one observer covers them.
new ResizeObserver(syncQuick).observe(qaTrack);

// --- the chat's model line ---------------------------------------------------
// Its own cheap read, kept separate from /settings: that one probes four
// endpoints for their model lists, and the line must not wait seconds to say
// which model answers you.
//
// The worker half only appears when there IS a second model: it defaults to
// empty and every call site falls back to the chat model, so printing the same
// name twice would claim two models where one is configured.
async function loadConfig() {
  try {
    const c = await (await fetch("/config")).json();
    const short = (m) => (m || "").split("/").pop();
    const box = $("#chat-models");
    box.textContent = "";
    const pair = (lbl, val) => {
      box.appendChild(mkEl("span", "cm-lbl", lbl));
      box.appendChild(mkEl("span", "cm-val", val));
    };
    pair("model", c.model ? short(c.model) : "no model");
    if (c.worker_model && c.worker_model !== c.model) pair("worker", short(c.worker_model));
  } catch { notify("couldn't load session config"); }
}
$("#chat-models").addEventListener("click", () => openSettings());
$("#metrics-cancel").addEventListener("click", () => {
  if (metricsAbort) metricsAbort.abort();
});

// --- help panel -------------------------------------------------------------
// There was no help surface anywhere in the app: no shortcut list, no tour, no
// docs link, and twelve buttons whose only label was a `title`. This is the
// smallest thing that answers "what can I do here" without leaving the window.
const helpPanel = $("#help-panel");
const helpBtn = $("#help-btn");
function closeHelpPanel() {
  helpPanel.hidden = true;
  helpBtn.setAttribute("aria-expanded", "false");
}
helpBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  const opening = helpPanel.hidden;
  helpPanel.hidden = !opening;
  helpBtn.setAttribute("aria-expanded", opening ? "true" : "false");
});

document.addEventListener("click", (e) => {
  if (!helpPanel.hidden && !e.target.closest("#help-panel") && !e.target.closest("#help-btn"))
    closeHelpPanel();
});
// --- dictation: microphone → 16 kHz mono WAV → /stt --------------------------
// Whisper transcribes a clip in one pass, so there is no live partial text to
// stream in and the button has to carry the state on its own: idle, recording,
// transcribing. The WAV conversion happens here rather than on the server
// because MediaRecorder can only produce webm/opus, whisper.cpp's server reads
// WAV unless it was built with ffmpeg, and converting in the browser costs a
// dependency on neither side.
const sttPanel = $("#stt-panel");
// A recording nobody stopped is otherwise a twenty-minute upload and a wait to
// match, so the take ends itself.
const MIC_MAX_MS = 60000;

function showSttPanel(why) {
  $("#stt-why").textContent = why;
  sttPanel.hidden = false;
}
$("#stt-close").addEventListener("click", () => { sttPanel.hidden = true; });

// A success is cached for the page's life; a failure is not. Someone who reads
// the panel, starts whisper-server and clicks again should get a microphone,
// not the same panel until they reload.
let sttProbe = null;
async function sttAvailable() {
  if (sttProbe) return sttProbe;
  const answer = await fetch("/stt")
    .then((r) => r.json())
    .catch(() => ({ ok: false, detail: "the silica server did not answer" }));
  if (answer.ok) sttProbe = answer;
  return answer;
}

// Canonical 44-byte header + PCM16, written out by hand: the alternative is a
// dependency for twenty lines of byte-poking.
function wavFromPcm(samples, rate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const ascii = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  ascii(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); ascii(8, "WAVE");
  ascii(12, "fmt "); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);   // PCM
  view.setUint16(22, 1, true);   // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);   // block align
  view.setUint16(34, 16, true);  // bits
  ascii(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view.buffer], { type: "audio/wav" });
}

// Resample and downmix in one render pass. Hand-rolling either would be a worse
// filter than the one the browser already ships.
async function toWav16k(blob) {
  const ctx = new AudioContext();
  let decoded;
  try { decoded = await ctx.decodeAudioData(await blob.arrayBuffer()); }
  finally { ctx.close(); }
  const off = new OfflineAudioContext(1, Math.ceil(decoded.duration * 16000), 16000);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  return wavFromPcm((await off.startRendering()).getChannelData(0), 16000);
}

// Whisper mishears proper nouns and invents punctuation, and what lands here can
// become a write to the vault, so the text goes in for review and never sends
// itself. At the cursor, so dictating into a half-typed message works.
function insertAtCursor(box, text) {
  if (!text) { notify("nothing was transcribed", "info"); return; }
  const at = box.selectionStart ?? box.value.length;
  const before = box.value.slice(0, at);
  const after = box.value.slice(box.selectionEnd ?? at);
  const sep = before && !/\s$/.test(before) ? " " : "";
  box.value = before + sep + text + after;
  const caret = (before + sep + text).length;
  box.setSelectionRange(caret, caret);
  box.focus();
  box.dispatchEvent(new Event("input", { bubbles: true })); // autogrow + send state
}

function attachMic(box, btn) {
  let rec = null;
  let chunks = [];
  let cap = null;
  const stop = () => { if (rec && rec.state !== "inactive") rec.stop(); };

  btn.addEventListener("click", async () => {
    if (rec) { stop(); return; } // the second click ends the take
    const avail = await sttAvailable();
    if (!avail.ok) {
      showSttPanel(avail.detail || "no transcription endpoint is answering");
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      // Denied, or no device at all: the browser's to fix, not silica's.
      notify("no microphone: " + plainError((err && err.message) || err));
      return;
    }
    chunks = [];
    rec = new MediaRecorder(stream);
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      clearTimeout(cap);
      // Release the device, which is also what drops the browser's own
      // recording indicator — leaving it lit would say silica is still listening.
      stream.getTracks().forEach((t) => t.stop());
      rec = null;
      btn.classList.remove("recording");
      if (!chunks.length) return;
      btn.classList.add("busy");
      announce("transcribing");
      try {
        const wav = await toWav16k(new Blob(chunks, { type: chunks[0].type }));
        const form = new FormData();
        form.append("audio", wav, "clip.wav");
        const resp = await fetch("/stt", { method: "POST", body: form });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || resp.statusText);
        insertAtCursor(box, (body.text || "").trim());
      } catch (err) {
        notify("dictation failed: " + plainError((err && err.message) || err));
      } finally {
        btn.classList.remove("busy");
      }
    };
    rec.start();
    btn.classList.add("recording");
    announce("recording, click again to stop");
    cap = setTimeout(stop, MIC_MAX_MS);
  });
}

document.querySelectorAll(".mic").forEach((b) => {
  const box = document.getElementById(b.dataset.for);
  if (box) attachMic(box, b);
});

// --- settings ----------------------------------------------------------------
// Rows are built from /settings, never hardcoded here: the table lives in
// silica/ui/web/settings.py, so what the panel offers and what the server will
// accept are the same list. Saving is per row — no save button, no dirty state,
// no exit dialog. Toggles and pick-lists apply at once, text fields on blur or
// Enter (the browser's own `change`), never on every keystroke.
const stModal = $("#st-modal");
const stBackdrop = $("#st-backdrop");
const stSheet = $("#st-sheet");
const stPanel = $("#st-panel");
const stTabs = $("#st-tabs");
const stSearch = $("#st-search");
const settingsBtn = $("#settings-btn");
const ST_EXTRA = ["Endpoints", "Diagnostics", "About"];
const stState = { data: null, section: "Session", controls: [], uid: 0 };

// A key is shown as its own head and tail: enough to tell an OpenRouter key from
// a stale one without putting the secret on screen.
function maskKey(v) {
  if (!v) return "";
  return v.length <= 12 ? "•".repeat(8) : v.slice(0, 5) + "••••" + v.slice(-4);
}

function stEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}

function stSectionEl(name) {
  const s = stEl("section", "st-section");
  s.dataset.section = name;
  s.appendChild(stEl("div", "st-section-title", name));
  return s;
}

function stNote(rowEl, cls, text) {
  const note = rowEl.querySelector(".st-note");
  note.className = "st-note " + cls;
  note.textContent = text;
}

function stValueOf(row, input) {
  return row.kind === "toggle" ? String(input.checked) : input.value;
}

function stRevert(row, input) {
  const prev = input.dataset.prev || "";
  if (row.kind === "toggle") input.checked = prev === "true";
  else input.value = prev;
}

// One control per kind. Every list row is an <input list> with a <datalist>
// rather than a dropdown: _endpoint_model_ids returns [] on any error, so the
// list is empty exactly when the endpoint is down — which is the moment you
// opened this panel to fix it. A datalist degrades to a text field on its own.
function stBuildControl(row, rowEl, labelEl) {
  if (row.kind === "readonly") {
    // Registered like any control so a write that derives it (safe mode sets
    // write_dir) refreshes the readout instead of leaving it stale until reopen.
    const ro = stEl("span", "st-ro", row.value || "—");
    stState.controls.push({ row, input: ro });
    return ro;
  }
  const id = `st-c${++stState.uid}`;
  labelEl.setAttribute("for", id);
  let input;
  if (row.kind === "toggle") {
    input = stEl("input");
    input.type = "checkbox";
    input.checked = row.value === "true";
  } else if (row.kind === "enum") {
    input = stEl("select");
    const opts = row.options.includes(row.value) || !row.value
      ? row.options : [row.value, ...row.options];
    for (const o of opts) input.appendChild(new Option(o, o));
    input.value = row.value;
  } else {
    input = stEl("input");
    input.type = "text";
    input.spellcheck = false;
    input.autocomplete = "off";
    if (row.kind === "int") input.inputMode = "numeric";
    input.value = row.kind === "secret" ? maskKey(row.value) : row.value;
    if (row.options.length) {
      const dl = stEl("datalist");
      dl.id = id + "-list";
      for (const o of row.options) dl.appendChild(new Option(o, o));
      input.setAttribute("list", dl.id);
      rowEl.appendChild(dl);
    }
  }
  input.id = id;
  input.dataset.prev = stValueOf(row, input);
  if (row.locked) {
    input.disabled = true;
    input.dataset.locked = "1";
  }
  input.addEventListener("change", () => stCommit(row, input, rowEl));
  stState.controls.push({ row, input });
  return input;
}

function stRowEl(row) {
  const el = stEl("div", "st-row");
  el.dataset.key = row.key;
  el.dataset.search = `${row.label} ${row.help} ${row.key}`.toLowerCase();
  const label = stEl("div", "st-label");
  const name = stEl("label", "st-name", row.label);
  label.appendChild(name);
  if (row.help) label.appendChild(stEl("div", "st-help", row.help));
  if (row.warn) label.appendChild(stEl("div", "st-warn", "⚠ " + row.warn));
  el.appendChild(label);

  const ctl = stEl("div", "st-ctl");
  const input = stBuildControl(row, el, name);
  ctl.appendChild(input);
  // A key already set is shown masked and read-only: the eye is how you say
  // "I mean to replace this", so a mask can never be saved as a value.
  if (row.kind === "secret" && row.value && !row.locked) {
    const eye = stEl("button", "st-eye", "👁");
    eye.type = "button";
    eye.title = "reveal and replace";
    eye.setAttribute("aria-label", `reveal ${row.label}`);
    input.readOnly = true;
    eye.addEventListener("click", () => {
      input.readOnly = false;
      input.value = row.value;
      input.dataset.prev = row.value;
      eye.remove();
      input.focus();
    });
    ctl.appendChild(eye);
  }
  el.appendChild(ctl);

  const note = stEl("div", "st-note");
  if (row.locked) note.textContent = `🔒 defined in the environment (${row.key})`;
  else if (row.kind === "secret" && row.value) note.textContent = "set · reveal to replace";
  el.appendChild(note);
  return el;
}

// Every control bound to a key this write touched, resynced: `thinking` is both
// the session's live toggle and a display preference, and a provider change
// drags the model and the base url with it.
function stSyncKey(key, value) {
  for (const { row, input } of stState.controls) {
    if (row.key !== key) continue;
    row.value = value;
    if (row.kind === "readonly") { input.textContent = value || "—"; continue; }
    if (row.kind === "toggle") input.checked = value === "true";
    else if (row.kind === "secret") input.value = input.readOnly ? maskKey(value) : value;
    else input.value = value;
    input.dataset.prev = stValueOf(row, input);
  }
}

async function stCommit(row, input, rowEl) {
  const value = stValueOf(row, input);
  if (value === input.dataset.prev) return;
  if (row.warn && !(await stConfirmRow(row, value))) {
    stRevert(row, input);
    stNote(rowEl, "", "");
    return;
  }
  stNote(rowEl, "pending", "saving…");
  let resp = null, data = null;
  try {
    resp = await fetch(row.confirm ? "/settings/confirm" : "/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: row.key, value }),
    });
    data = await resp.json();
  } catch { /* falls through to the failure branch */ }
  if (!resp || !resp.ok || (data && data.ok === false)) {
    const why = (data && (data.detail || data.error)) || "could not save";
    stNote(rowEl, "bad", "✕ " + why);
    stRevert(row, input);
    if (resp && resp.status === 409) stSetBusy(true);
    return;
  }
  const keys = Object.keys(data.values || {});
  stNote(rowEl, "good", keys.length > 1
    ? `✓ ${keys.length} keys saved to ${data.path}`
    : `✓ saved to ${data.path}`);
  for (const [k, v] of Object.entries(data.values || {})) stSyncKey(k, v);
  for (const n of data.notes || []) notify(n, "info");
  if (data.reindex) {
    const n = data.reindex.indexed ?? data.reindex.embedded ?? "";
    stNote(rowEl, "good", `✓ saved to ${data.path} · re-indexed${n === "" ? "" : " " + n} notes`);
  }
  if (row.key === "SILICA_MODEL" || row.key === "SILICA_PROVIDER") loadConfig();
  // The graph document bakes these in at render time, so the change only lands
  // on a rebuild. Stale it, and rebuild now if that view is the one on screen.
  if (row.key === "SILICA_THEME") applyThemePref((data.values || {})[row.key] || value);
  if (row.key === "SILICA_GRAPH_PARTICLES" || row.key === "SILICA_GRAPH_SHADING") {
    graphStale = true;
    if (activeTab === "graph" && graphMode === "graph") setGraphMode("graph");
  }
  if (row.key === "SILICA_VAULT") { loadVault(); loadVaultInfo(); loadSessions(); loadChanges(); }
}

// --- sheets: confirmations and the bug report, inside the modal so the focus
// trap keeps holding.
let stSheetResolve = null;
function openSheet(title, body, actions) {
  $("#st-sheet-title").textContent = title;
  const bodyEl = stSheet.querySelector(".st-sheet-body");
  const actEl = stSheet.querySelector(".st-sheet-actions");
  bodyEl.innerHTML = "";
  actEl.innerHTML = "";
  bodyEl.appendChild(body);
  for (const [label, fn, kind] of actions) {
    const b = stEl("button", "st-btn " + (kind || ""), label);
    b.type = "button";
    b.addEventListener("click", () => fn());
    actEl.appendChild(b);
  }
  stSheet.hidden = false;
  actEl.querySelector("button").focus();
}

function closeSheet(answer) {
  stSheet.hidden = true;
  const resolve = stSheetResolve;
  stSheetResolve = null;
  if (resolve) resolve(!!answer);
}

// The consequence, named, before the change happens — and the button says what
// it will do, not "ok".
const ST_CONFIRM = {
  SILICA_VAULT: (row, value) => [
    "switch vault?",
    `silica will read and write ${value} instead.\nevery index is rebuilt for the new folder.`,
    "switch",
  ],
  SILICA_EMBEDDING_MODEL: (row) => [
    "change the embedding model?",
    `the vectors already stored were produced by ${row.value || "another model"}.\n` +
    "new queries cannot be compared against them.\n" +
    "repairing this means a full re-index, which takes a while on a large vault.",
    "change and re-index",
  ],
  SILICA_EMBEDDING_BASE_URL: (row) => [
    "change where embeddings come from?",
    "a different server means different vectors, even under the same model name.\n" +
    "repairing this means a full re-index.",
    "change and re-index",
  ],
  SILICA_COOCCURRENCE_LANG: () => [
    "change the vault language?",
    "the language is frozen per vault. changing it after notes exist\n" +
    "makes old keywords disagree with new ones.",
    "change",
  ],
};

function stConfirmRow(row, value) {
  const build = ST_CONFIRM[row.key] || (() => [`change ${row.label}?`, row.warn, "change"]);
  const [title, body, ok] = build(row, value);
  const el = stEl("p", "st-sheet-text", body);
  return new Promise((resolve) => {
    stSheetResolve = resolve;
    openSheet(title, el, [
      ["cancel", () => closeSheet(false)],
      [ok, () => closeSheet(true), "primary"],
    ]);
  });
}

// --- the three sections that are not config rows ------------------------------
const ST_DOT = { ok: "●", warn: "◐", fail: "○", unknown: "○" };

function stInfoRow(host, label, value, cls) {
  const el = stEl("div", "st-row" + (cls ? " " + cls : ""));
  el.dataset.search = `${label} ${value}`.toLowerCase();
  el.appendChild(stEl("div", "st-label", label));
  el.appendChild(stEl("div", "st-ctl-text", value));
  host.appendChild(el);
  return el;
}

async function renderEndpoints(host) {
  host.querySelectorAll(".st-row, .st-note-line").forEach((e) => e.remove());
  host.appendChild(stEl("div", "st-note-line",
    "reachability is checked with a real request, not an open port"));
  let rows = [];
  try { rows = await (await fetch("/endpoints")).json(); }
  catch { host.appendChild(stEl("div", "st-note-line", "could not probe the endpoints")); return; }
  for (const e of rows) {
    const row = stEl("div", "st-row");
    row.dataset.search = `${e.label} ${e.url} endpoint`.toLowerCase();
    const label = stEl("div", "st-label");
    label.appendChild(stEl("span", "st-name", e.label));
    label.appendChild(stEl("div", "st-help", e.url || "not configured"));
    row.appendChild(label);
    const state = stEl("div", "st-ctl-text " + (e.up ? "up" : "down"));
    state.textContent = e.up
      ? `● up${e.models ? ` · ${e.models} model${e.models === 1 ? "" : "s"}` : ""}`
      : `○ down${e.command || !e.local ? "" : " · no start command set"}`;
    row.appendChild(state);
    const note = stEl("div", "st-note");
    if (e.command) note.textContent = `start command read-only · edit ${e.command_key} in the .env`;
    row.appendChild(note);
    if (!e.up && e.local && e.command) {
      const start = stEl("button", "st-btn", "start");
      start.type = "button";
      start.addEventListener("click", async () => {
        start.disabled = true;
        note.className = "st-note pending";
        note.textContent = `starting ${e.label}… loading a model takes a while`;
        let out = null;
        try {
          out = await (await fetch("/endpoints/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ label: e.label }),
          })).json();
        } catch { /* reported below */ }
        if (out && out.ok) renderEndpoints(host);
        else {
          note.className = "st-note bad";
          note.textContent = (out && out.error)
            || `${e.label} did not come up · see ${out ? out.log : "~/.silica/logs"}`;
          start.disabled = false;
        }
      });
      state.appendChild(start);
    }
    host.appendChild(row);
  }
}

async function renderDiagnostics(host) {
  host.querySelectorAll(".st-row, .st-note-line").forEach((e) => e.remove());
  let rows = [];
  try { rows = await (await fetch("/health?all=1")).json(); }
  catch { host.appendChild(stEl("div", "st-note-line", "could not run the checks")); return; }
  if (!rows.length) { host.appendChild(stEl("div", "st-note-line", "everything checks out")); return; }
  for (const r of rows) {
    const el = stInfoRow(host, r.name, `${ST_DOT[r.status] || "○"} ${r.detail}`, "st-check-" + r.status);
    if (r.hint) el.appendChild(stEl("div", "st-note", r.hint));
  }
}

function renderAbout(host, data) {
  host.querySelectorAll(".st-row, .st-note-line").forEach((e) => e.remove());
  stInfoRow(host, "version", `silica ${data.version}`);
  stInfoRow(host, "updates", data.behind
    ? `${data.behind} commit${data.behind === 1 ? "" : "s"} behind · update with \`silica update\``
    : "up to date");
  const row = stEl("div", "st-row");
  row.dataset.search = "report a bug issue github";
  row.appendChild(stEl("div", "st-label", "report a bug"));
  const btn = stEl("button", "st-btn st-safe", "report a bug");
  btn.type = "button";
  btn.addEventListener("click", () => openBugReport(data.issues_url));
  const ctl = stEl("div", "st-ctl");
  ctl.appendChild(btn);
  row.appendChild(ctl);
  host.appendChild(row);
}

// The attached payload is built by the server, not read off this panel: the API
// key fields are one querySelector away from here, and an issue is public.
async function openBugReport(fallbackUrl) {
  let data = { payload: "", issues_url: fallbackUrl };
  try { data = await (await fetch("/bug_report")).json(); } catch { /* file it bare */ }
  const body = stEl("div", "st-bug");
  body.appendChild(stEl("label", "st-bug-label", "what happened?"));
  const what = stEl("textarea", "st-bug-what");
  what.rows = 4;
  what.placeholder = "what you did, what you expected, what happened instead";
  body.appendChild(what);
  body.appendChild(stEl("label", "st-bug-label", "this will be attached · edit it if you like"));
  const payload = stEl("textarea", "st-bug-payload");
  payload.rows = 8;
  payload.value = data.payload;
  body.appendChild(payload);
  body.appendChild(stEl("div", "st-note-line",
    "your vault path is shortened to ~ · api keys are never included"));
  openSheet("report a bug", body, [
    ["cancel", () => closeSheet(false)],
    ["open on github ↗", () => {
      const title = (what.value.trim().split("\n")[0] || "bug report").slice(0, 80);
      const text = `${what.value.trim()}\n\n\`\`\`\n${payload.value}\n\`\`\`\n`;
      window.open(
        `${data.issues_url}?title=${encodeURIComponent(title)}&body=${encodeURIComponent(text)}`,
        "_blank", "noopener");
      closeSheet(true);
    }, "primary"],
  ]);
  what.focus();
}

// --- render, filter, open, close ---------------------------------------------
function stRender(data) {
  stState.data = data;
  stState.controls = [];
  stPanel.innerHTML = "";
  stTabs.innerHTML = "";
  $("#st-env").textContent = data.env_path;
  for (const section of data.sections) {
    const el = stSectionEl(section.name);
    for (const row of section.rows) el.appendChild(stRowEl(row));
    stPanel.appendChild(el);
  }
  for (const name of ST_EXTRA) stPanel.appendChild(stSectionEl(name));
  stPanel.appendChild(stEl("div", "st-empty"));
  for (const name of [...data.sections.map((s) => s.name), ...ST_EXTRA]) {
    const b = stEl("button", "st-tab", name);
    b.type = "button";
    b.dataset.section = name;
    b.addEventListener("click", () => stShow(name));
    stTabs.appendChild(b);
  }
  renderAbout(stPanel.querySelector('[data-section="About"]'), data);
  renderEndpoints(stPanel.querySelector('[data-section="Endpoints"]'));
  const diagnostics = stPanel.querySelector('[data-section="Diagnostics"]');
  // The checks are a snapshot of a machine that keeps changing — starting the
  // server this panel just told you was down is the whole point.
  const recheck = stEl("button", "st-btn st-safe", "recheck");
  recheck.type = "button";
  recheck.addEventListener("click", () => renderDiagnostics(diagnostics));
  diagnostics.querySelector(".st-section-title").appendChild(recheck);
  renderDiagnostics(diagnostics);
  stShow(stState.section);
  stSetBusy(data.busy || streaming);
}

function stShow(name) {
  stState.section = name;
  stSearch.value = "";
  stFilter();
  stPanel.scrollTop = 0;
}

// The search reaches every section at once — the rows are already in the DOM,
// so it needs no index and no second surface.
function stFilter() {
  const q = stSearch.value.trim().toLowerCase();
  let hits = 0;
  for (const section of stPanel.querySelectorAll(".st-section")) {
    let any = 0;
    for (const row of section.querySelectorAll(".st-row")) {
      const hit = !q || (row.dataset.search || "").includes(q);
      row.hidden = !hit;
      if (hit) any++;
    }
    section.hidden = q ? !any : section.dataset.section !== stState.section;
    if (!section.hidden) hits += any;
  }
  const empty = stPanel.querySelector(".st-empty");
  empty.hidden = !(q && !hits);
  empty.textContent = `no setting matches "${q}"`;
  for (const b of stTabs.querySelectorAll(".st-tab"))
    setActive(b, !q && b.dataset.section === stState.section);
}

// One rule, not a list of which rows a turn happens to read: that list would rot
// at the first new tool and no test would catch it.
function stSetBusy(busy) {
  $("#st-busy").hidden = !busy;
  // `.st-safe` reads and writes nothing — rechecking the diagnostics or filing a
  // bug is exactly what you want to do while a turn is misbehaving.
  for (const el of stPanel.querySelectorAll("input, select, .st-btn:not(.st-safe), .st-eye")) {
    if (el.dataset.locked === "1") continue;
    el.disabled = !!busy;
  }
}

function stTrapFocus(e) {
  if (e.key !== "Tab" || stModal.hidden) return;
  const scope = stSheet.hidden ? stModal : stSheet;
  const focusable = [...scope.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])'
  )].filter((el) => el.offsetParent !== null && !el.closest("[hidden]"));
  if (!focusable.length) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

async function openSettings() {
  stBackdrop.hidden = false;
  stModal.hidden = false;
  settingsBtn.setAttribute("aria-expanded", "true");
  stPanel.innerHTML = "";
  stPanel.appendChild(stEl("div", "st-note-line", "reading your configuration…"));
  stSearch.focus();
  try {
    stRender(await (await fetch("/settings")).json());
  } catch {
    stPanel.innerHTML = "";
    stPanel.appendChild(stEl("div", "st-note-line", "could not read the settings"));
  }
}

function closeSettings() {
  if (!stSheet.hidden) closeSheet(false);
  stModal.hidden = true;
  stBackdrop.hidden = true;
  settingsBtn.setAttribute("aria-expanded", "false");
  // Always the gear, not wherever focus happened to be: it is the control the
  // modal came out of, and it is where a keyboard user expects to land back.
  settingsBtn.focus();
}

settingsBtn.addEventListener("click", () => {
  if (stModal.hidden) openSettings(); else closeSettings();
});
$("#st-close").addEventListener("click", closeSettings);
stBackdrop.addEventListener("click", closeSettings);
stSearch.addEventListener("input", stFilter);
document.addEventListener("keydown", stTrapFocus);

// One Escape handler for the whole app: there used to be two independent ones,
// so a single press with a panel open over a note closed both at once.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // The settings modal is the only surface with a backdrop: whatever is under
  // it is unreachable, so it answers first, and its own sheet before it.
  if (!stSheet.hidden) { closeSheet(); return; }
  if (!stModal.hidden) { closeSettings(); return; }
  if (!sttPanel.hidden) { sttPanel.hidden = true; return; }
  if (!helpPanel.hidden) { closeHelpPanel(); return; }
  closeNote();
});

// --- boot health: the doctor's non-ok rows as toasts -------------------------
// The TUI logs a degraded embedder/reranker to stderr; the browser sees none of
// that, so without this a server left down just makes recall quietly worse.
// Boot health is CONFIGURATION, not an event: it is equally true a second after
// load and ten minutes in. As toasts these fired on every single load, stacked
// over whatever the user was reading, and one of them is a five-line JSON hooks
// blob — so the app's resting state was a debug message. They live in the
// sidebar now, where they stay legible and dismissible for as long as they are
// true, and the toast strip goes back to meaning "something just happened".
async function loadHealth() {
  const box = $("#boot-notices");
  try {
    const rows = await (await fetch("/health")).json();
    box.innerHTML = "";
    for (const r of rows) {
      const n = document.createElement("div");
      n.className = "notice";
      const t = document.createElement("div");
      t.className = "notice-text";
      t.textContent = r.name + ": " + r.detail;
      n.appendChild(t);
      if (r.hint) {
        // Hints collapsed behind a disclosure: three stacked notices with a
        // five-line JSON hooks blob were eating half the sidebar on first
        // load. One line each until the user asks for the remedy.
        const d = document.createElement("details");
        const s = document.createElement("summary");
        s.textContent = "how to fix";
        d.appendChild(s);
        const h = document.createElement("div");
        h.className = "notice-hint";
        h.textContent = r.hint;
        d.appendChild(h);
        n.appendChild(d);
      }
      const x = document.createElement("button");
      x.type = "button";
      x.className = "notice-x";
      x.setAttribute("aria-label", "dismiss this notice");
      x.textContent = "✕";
      x.addEventListener("click", () => { n.remove(); box.hidden = !box.childElementCount; });
      n.appendChild(x);
      box.appendChild(n);
      announce(r.name + ": " + r.detail);
    }
    box.hidden = !rows.length;
  } catch { /* the page works without the report; don't toast about the toast */ }
}

loadVault();
loadSessions();
loadVaultInfo();
loadChanges(); // the server's ledger outlives the tab — a reload keeps the list
loadConfig(); // header shows the active model without opening the panel
loadHealth(); // a chat/embedder/reranker server that isn't up says so, once, here
// --- calendar ----------------------------------------------------------------
// Month grid + week view over GET /calendar (the 4-axis agenda payload), with
// nodus-style lane-packing for multi-day bars: per week row, spans pack into
// the first free lane, Mon–Sun clipped; in month mode lanes cap at 3 and the
// overflow folds into the day's "+N". The agenda panel shows the upcoming 7
// days, or the one day clicked in the grid. POST /reminders every 30 s IS the
// reminder tick (setInterval stays alive in hidden tabs); delivered ones land
// on the toast strip.

let calMode = "month";        // "month" | "week"
let calAnchor = new Date();   // any date inside the visible month/week
let calSelected = null;       // "YYYY-MM-DD" the agenda panel focuses on, or null
let calDays = {};             // date -> DayRow of the visible window
let calUpcoming = null;       // DayRows of today+7, for the default agenda panel

const CAL_LANE_CAP = 3;       // month mode: visible multi-day lanes per week
const CAL_CHIP_CAP = 2;       // month mode: visible timed chips per day

function calFmt(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function calMonday(d) {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}
function calWindow() {
  if (calMode === "week") return { start: calMonday(calAnchor), days: 7 };
  const first = new Date(calAnchor.getFullYear(), calAnchor.getMonth(), 1);
  return { start: calMonday(first), days: 42 };
}

async function loadCalendar() {
  const w = calWindow();
  try {
    const [grid, up] = await Promise.all([
      fetch(`/calendar?start=${calFmt(w.start)}&days=${w.days}`).then((r) => r.json()),
      fetch("/calendar?start=today&days=7").then((r) => r.json()),
    ]);
    if (grid.error) { notify(grid.error); return; }
    calDays = {};
    for (const row of grid.days) calDays[row.date] = row;
    calUpcoming = up.error ? null : up.days;
    renderCalendar();
    renderCalAgenda();
  } catch { notify("couldn't load the calendar"); }
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

function calEventEl(e, cls, withTime) {
  const el = document.createElement("div");
  el.className = cls + (e.status ? " " + e.status : "");
  if (withTime && !e.all_day) {
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = e.start.slice(11, 16);
    el.appendChild(t);
  }
  el.appendChild(document.createTextNode((e.status === "done" ? "✓ " : "") + e.title));
  el.title = e.title;
  el.addEventListener("click", (ev) => { ev.stopPropagation(); openNote(e.path); });
  return el;
}

function calBuildWeek(dates, tall) {
  const week = document.createElement("div");
  week.className = "cal-week" + (tall ? " wk" : "");

  // Reconstruct spans from the per-day buckets: same (stem, start) on
  // consecutive days is one occurrence.
  const spans = new Map();
  dates.forEach((date, i) => {
    for (const e of (calDays[date] || {}).events || []) {
      if (!calIsBar(e)) continue;
      const k = e.stem + "|" + e.start;
      const s = spans.get(k) || { c0: i, c1: i, ev: e };
      s.c1 = i;
      spans.set(k, s);
    }
  });
  const bars = [...spans.values()];
  const laneTotal = calPackLanes(bars);
  const laneCap = tall ? laneTotal : Math.min(laneTotal, CAL_LANE_CAP);
  // repeat(0, …) is invalid CSS and would drop the whole declaration
  week.style.gridTemplateRows =
    laneCap > 0 ? `20px repeat(${laneCap}, 20px) 1fr` : "20px 1fr";

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
    if (date === calSelected) cell.classList.add("selected");
    const d = new Date(date + "T00:00");
    if (calMode === "month" && d.getMonth() !== visMonth) cell.classList.add("other");
    const num = document.createElement("div");
    num.className = "cal-num";
    const nn = document.createElement("span");
    nn.textContent = d.getDate();
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
    const timed = ((calDays[date] || {}).events || []).filter((e) => !calIsBar(e));
    const cap = tall ? timed.length : CAL_CHIP_CAP;
    timed.slice(0, cap).forEach((e) => chips.appendChild(calEventEl(e, "cal-chip", true)));
    overflow[i] += Math.max(0, timed.length - cap);
    cell.appendChild(chips);

    const more = document.createElement("div");
    more.className = "cal-more";
    cell.appendChild(more);

    cell.addEventListener("click", () => {
      calSelected = calSelected === date ? null : date;
      renderCalendar();
      renderCalAgenda();
    });
    week.appendChild(cell);
  });

  for (const s of bars) {
    if (s.lane >= laneCap) {
      for (let c = s.c0; c <= s.c1; c++) overflow[c] += 1;
      continue;
    }
    const bar = calEventEl(s.ev, "cal-bar", false);
    bar.style.gridColumn = `${s.c0 + 1} / ${s.c1 + 2}`;
    bar.style.gridRow = String(s.lane + 2);
    week.appendChild(bar);
  }
  overflow.forEach((n, i) => {
    if (n > 0) week.children[i].querySelector(".cal-more").textContent = `+${n} more`;
  });
  return week;
}

function renderCalendar() {
  const grid = $("#cal-grid");
  grid.replaceChildren();
  const dow = document.createElement("div");
  dow.className = "cal-dow";
  for (const n of ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) {
    const c = document.createElement("div");
    c.textContent = n;
    dow.appendChild(c);
  }
  grid.appendChild(dow);

  const w = calWindow();
  const dates = [];
  for (let i = 0; i < w.days; i++) {
    const d = new Date(w.start);
    d.setDate(d.getDate() + i);
    dates.push(calFmt(d));
  }
  for (let r = 0; r < dates.length / 7; r++) {
    grid.appendChild(calBuildWeek(dates.slice(r * 7, r * 7 + 7), calMode === "week"));
  }

  const t = $("#cal-title");
  if (calMode === "month") {
    t.textContent = calAnchor.toLocaleString("en-US", { month: "long", year: "numeric" });
  } else {
    const a = calMonday(calAnchor);
    const b = new Date(a); b.setDate(b.getDate() + 6);
    const f = (d) => d.toLocaleString("en-US", { month: "short", day: "numeric" });
    t.textContent = `${f(a)} – ${f(b)}, ${b.getFullYear()}`;
  }
}

function calAgendaDay(row) {
  const sec = document.createElement("div");
  sec.className = "cal-ag-day";
  const d = new Date(row.date + "T00:00");
  const h = document.createElement("div");
  h.className = "cal-ag-date";
  h.textContent = d.toLocaleString("en-US", { month: "short", day: "numeric" });
  const wd = document.createElement("span");
  wd.className = "wd";
  wd.textContent = d.toLocaleString("en-US", { weekday: "long" });
  h.appendChild(wd);
  sec.appendChild(h);

  let any = false;
  for (const e of row.events) {
    any = true;
    const it = document.createElement("div");
    it.className = "cal-ag-item ev" + (e.status ? " " + e.status : "");
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = e.all_day ? "all-day" : e.start.slice(11, 16);
    it.appendChild(when);
    const ti = document.createElement("span");
    ti.textContent = e.title;
    it.appendChild(ti);
    it.addEventListener("click", () => openNote(e.path));
    sec.appendChild(it);
  }
  for (const n of row.notes) {
    any = true;
    const it = document.createElement("div");
    it.className = "cal-ag-item";
    const k = document.createElement("span");
    k.className = "when cal-ag-kind";
    k.textContent = "note";
    it.appendChild(k);
    it.appendChild(document.createTextNode(n.label));
    sec.appendChild(it);
  }
  for (const a of row.activity) {
    any = true;
    const it = document.createElement("div");
    it.className = "cal-ag-item";
    const k = document.createElement("span");
    k.className = "when cal-ag-kind";
    k.textContent = "agent";
    it.appendChild(k);
    it.appendChild(document.createTextNode(a));
    sec.appendChild(it);
  }
  for (const r of row.review || []) {
    any = true;
    const it = document.createElement("div");
    it.className = "cal-ag-item";
    const k = document.createElement("span");
    k.className = "when cal-ag-kind";
    k.textContent = "review";
    it.appendChild(k);
    it.appendChild(document.createTextNode(r.path || ""));
    sec.appendChild(it);
  }
  if (!any) {
    const e = document.createElement("div");
    e.className = "cal-ag-empty";
    e.textContent = "nothing scheduled";
    sec.appendChild(e);
  }
  return sec;
}

function renderCalAgenda() {
  const panel = $("#cal-agenda");
  panel.replaceChildren();
  const head = document.createElement("div");
  head.className = "cal-ag-head";
  if (calSelected && calDays[calSelected]) {
    head.textContent = "selected day";
    panel.appendChild(head);
    panel.appendChild(calAgendaDay(calDays[calSelected]));
  } else if (calUpcoming) {
    head.textContent = "next 7 days";
    panel.appendChild(head);
    for (const row of calUpcoming) panel.appendChild(calAgendaDay(row));
  }
}

$("#cal-prev").addEventListener("click", () => {
  if (calMode === "month") calAnchor.setMonth(calAnchor.getMonth() - 1);
  else calAnchor.setDate(calAnchor.getDate() - 7);
  loadCalendar();
});
$("#cal-next").addEventListener("click", () => {
  if (calMode === "month") calAnchor.setMonth(calAnchor.getMonth() + 1);
  else calAnchor.setDate(calAnchor.getDate() + 7);
  loadCalendar();
});
$("#cal-today").addEventListener("click", () => {
  calAnchor = new Date();
  calSelected = null;
  loadCalendar();
});
$("#cal-mode").addEventListener("click", (e) => {
  const m = e.target.dataset.calmode;
  if (!m || m === calMode) return;
  calMode = m;
  document.querySelectorAll("#cal-mode button").forEach((b) => setActive(b, b.dataset.calmode === m));
  loadCalendar();
});

// The poll is the tick: the endpoint computes due, advances the sidecar marks,
// and returns what to show. At-most-once shared with the REPL daemon.
setInterval(async () => {
  try {
    const d = await (await fetch("/reminders", { method: "POST" })).json();
    for (const r of d.due || []) {
      notify((r.late ? "late reminder: " : "reminder: ") + r.title + " · " + r.start.slice(0, 16), "info");
    }
  } catch { /* a reminder is a courtesy; the next tick retries */ }
}, 30000);

// Land on chat — it's the primary surface — unless the URL names another view
// (#explore, #calendar, #metrics): a pasted deep link must win over the default.
// Read the hash BEFORE the default click: showTab rewrites it via replaceState.
const bootSlug = (location.hash || "").replace(/^#/, "");
const bootTab = bootSlug === "explore" ? "graph" : bootSlug;
document.querySelector(
  `.tab[data-tab="${["chat", "graph", "calendar", "metrics"].includes(bootTab) ? bootTab : "chat"}"]`
).click();
