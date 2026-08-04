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
  const inline = (t) =>
    wiki(esc(t))
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+?)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2">$1</a>');
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
  let curText = null;   // open markdown segment { el, raw }
  let curTools = null;  // open group of consecutive tools
  let curThink = null;  // open thinking block { details, body, raw }
  let segments = 0;     // text runs so far; an uninterrupted one upgrades to server html

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
    return (curThink = { details, body: details.querySelector(".thinking-body"), raw: "" });
  }
  function textSeg() {
    if (curText) return curText;
    close("text");
    const el = document.createElement("div");
    el.className = "stream-text";
    flow.appendChild(el);
    curText = { el, raw: "" };
    texts.push(curText);
    segments++;
    return curText;
  }
  function toolsGroup() {
    if (curTools) return curTools;
    close("tools");
    const g = document.createElement("div");
    g.className = "tools";
    flow.appendChild(g);
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
    graphStale = true; // a turn may have written notes — rebuild next graph view
    metricsStale = true; // …and remeasure the next time the metrics tab opens
  }

  function handle(ev) {
    pending.remove(); // something arrived — the placeholder has done its job
    if (ev.type === "delta" && ev.kind === "reasoning") {
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
      const t = document.createElement("div");
      t.className = "tool";
      t.dataset.label = toolLabel(ev);
      t.textContent = "» " + t.dataset.label + " …";
      toolsGroup().appendChild(t);
      curTools.appendChild(caret);
      toolEls[ev.id] = t;
      claimed[ev.id] = { refs: ev.notes || [], effect: ev.effect || "read", verb: ev.name };
    } else if (ev.type === "tool_done") {
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
// Switching tabs is a function, not only a click: a synthetic .click() bubbles
// to the document's outside-click handler, which closes the note drawer. Every
// caller that needs the drawer to survive the switch (the context drawer's
// concept cloud, its suggested rows) calls this instead.
function showTab(tab) {
  activeTab = tab;
  if (tab === "chat") closePeek(); // stream visible → card redundant
  $("#dock").hidden = tab !== "graph"; // ask-from-here lives on the graph + map only
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#view-chat").classList.toggle("active", tab === "chat");
  $("#view-graph").classList.toggle("active", tab === "graph");
  $("#view-metrics").classList.toggle("active", tab === "metrics");
  if (tab === "graph") setGraphMode(graphMode); // load the active mode's content
  if (tab === "metrics") loadMetrics();
}
$(".tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (tab) showTab(tab);
});

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
  document.querySelectorAll(".gmode-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.gmode === m));
  const isMap = m === "map";
  $("#graph-frame").hidden = isMap;
  $("#map-frame").hidden = !isMap || !mapRootedPath;
  $("#map-picker").hidden = !isMap || !!mapRootedPath;
  closeNodeResults();
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
  document.querySelectorAll("#note-mode button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === drawerMode);
    // A ghost has no file to read; the reader half stops being an offer.
    if (b.dataset.mode === "note") b.disabled = !!ghostName;
  });
  // The five actions act on the SELECTED NOTE, so they survive the mode switch
  // — but a ghost has no note to act on, and an enabled button that does
  // nothing is the same silent no-op this drawer exists to fix.
  document.querySelectorAll("#note-actions .na").forEach((b) => { b.disabled = !!ghostName; });
  $("#note-body").hidden = drawerMode !== "note";
  $("#note-context").hidden = drawerMode !== "context";
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
  if (b.dataset.mode === "note") openNote(lastNotePath || lastViewedPath);
  else openContext({ path: lastNotePath || lastViewedPath });
});

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
  peek = { body, caret, raw: "" };
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
          d.className = "tool " + (t.error ? "error" : "done");
          d.textContent = (t.error ? "✗ " : "✓ ") + toolLabel(t);
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
  if (row.key === "SILICA_VAULT") { loadVault(); loadVaultInfo(); loadSessions(); }
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
    b.classList.toggle("active", !q && b.dataset.section === stState.section);
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
        const h = document.createElement("div");
        h.className = "notice-hint";
        h.textContent = r.hint;
        n.appendChild(h);
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
loadConfig(); // header shows the active model without opening the panel
loadHealth(); // a chat/embedder/reranker server that isn't up says so, once, here
// Land on chat — it's the primary surface. The tab handler does the rest.
document.querySelector('.tab[data-tab="chat"]').click();
