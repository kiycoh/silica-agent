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
function notify(msg, level = "error") {
  announce(msg);
  if ([...toasts.children].some((t) => t.textContent === msg)) return; // dedupe visible
  const t = document.createElement("div");
  t.className = "toast " + level;
  t.textContent = msg;
  const kill = () => t.remove();
  t.addEventListener("click", kill);
  toasts.appendChild(t);
  setTimeout(kill, level === "error" ? 6000 : 3000);
}

function bubble(role) {
  const el = document.createElement("div");
  el.className = "msg " + (role === "user" ? "user" : "silica");
  el.innerHTML = `<div class="role">${role === "user" ? "you" : "silica"}</div><div class="body"></div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el.querySelector(".body");
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
// inline + fenced code, bullet/ordered lists, links. Re-parses the whole segment
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
  const isBlock = (l) => /^```|^#{1,6}\s|^\s*[-*]\s|^\s*\d+\.\s/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { closeList(); i++; continue; }
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
    const para = [];
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
}

async function runTurn(fetchPromise, pendingLabel = "working") {
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
  const claimed = {};  // call id → { refs, effect }, held until tool_done
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
    if (touched.size) {
      // Grouped by what the turn DID, not merged into one "sources" row: a note
      // the agent deleted is not a citation, and a write is the whole point of
      // the product. Mutations first — that is the part worth checking.
      const s = document.createElement("div");
      s.className = "sources";
      for (const effect of ["written", "moved", "deleted", "read"]) {
        const refs = [...touched].filter(([, e]) => e === effect).map(([r]) => r);
        if (!refs.length) continue;
        const g = document.createElement("div");
        g.className = "sgroup " + effect;
        g.innerHTML = `<span class="sources-label">${effect}</span>`;
        for (const ref of refs) {
          const c = document.createElement("span");
          // A deleted note has no page to open: keep the chip as a record, drop
          // the click, or it routes to /note and answers "not found in vault".
          c.className = effect === "deleted" ? "note-gone" : "note-link";
          if (effect !== "deleted") c.dataset.path = ref; // delegated click → note drawer
          c.textContent = ref.split("/").pop().replace(/\.md$/, "");
          g.appendChild(c);
        }
        s.appendChild(g);
      }
      if ([...touched.values()].some((e) => e !== "read")) {
        const u = document.createElement("button");
        u.type = "button";
        u.className = "undo-turn";
        u.textContent = "undo this turn";
        u.title = "run /undo — rolls back this turn's vault writes";
        u.addEventListener("click", () => { u.disabled = true; send("/undo"); });
        s.appendChild(u);
      }
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
      // Name what it acted on. The verb alone ("write note") never told the user
      // which file the agent touched in their own vault.
      t.dataset.label = ev.target ? `${ev.name} "${ev.target}"` : ev.name;
      t.textContent = "» " + t.dataset.label + " …";
      toolsGroup().appendChild(t);
      curTools.appendChild(caret);
      toolEls[ev.id] = t;
      claimed[ev.id] = { refs: ev.notes || [], effect: ev.effect || "read" };
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
      if (t) { t.className = "tool error"; t.textContent = "✗ " + (t.dataset.label || ev.name) + " — " + ev.error; }
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
      setCtxTokens(ev.context_tokens, ev.max_context_tokens);
      peekDone(ev); // card gets the canonical OFM render
      announce("response ready");
    } else if (ev.type === "error") {
      close("");
      peekError(ev.error);
      notify("response failed: " + ev.error);
      const t = document.createElement("div");
      t.className = "tool error";
      t.textContent = "error: " + ev.error;
      flow.appendChild(t);
    }
    log.scrollTop = log.scrollHeight;
  }
}

function send(text) {
  if (!text.trim() || streaming) return;
  bubble("user").textContent = text;
  const find = text.trim().match(/^\/find\s*(.*)$/);
  if (find) { runFind(find[1]); return; }
  runTurn(fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }));
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
    el.innerHTML = `<span class="cmd-name">${c.name}</span> <span class="cmd-summary">${escapeHtml(c.usage ? c.usage + " — " + c.summary : c.summary)}</span>`;
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
$(".tabs").addEventListener("click", (e) => {
  const tab = e.target.dataset.tab;
  if (!tab) return;
  activeTab = tab;
  if (tab === "chat") closePeek(); // stream visible → card redundant
  $("#dock").hidden = tab === "chat"; // ask-from-here strip lives on graph + map
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#view-chat").classList.toggle("active", tab === "chat");
  $("#view-graph").classList.toggle("active", tab === "graph");
  $("#view-metrics").classList.toggle("active", tab === "metrics");
  if (tab === "graph") setGraphMode(graphMode); // load the active mode's content
  if (tab === "metrics") loadMetrics();
});

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
      $("#graph-frame").src = "/graph?t=" + Date.now();
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
  if (lastNotePath) focusGraphNode(lastNotePath);
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

async function loadMetrics(force = false, proposals = false) {
  if (metricsLoading) return;
  if (!metricsStale && !force && !(proposals && metricsDepth !== "full")) return;
  metricsLoading = true;
  const body = $("#metrics-body");
  const loading = $("#metrics-loading");
  loading.querySelector("div:last-child").textContent = proposals
    ? "Running the co-occurrence delta over every note."
    : "Measuring the vault.";
  loading.hidden = false;
  body.style.opacity = body.childElementCount ? "0.45" : ""; // hold the last render, no skeleton flash
  try {
    const data = await (await fetch("/metrics" + (proposals ? "?proposals=1" : ""))).json();
    if (data.error) { notify("metrics unavailable: " + data.error); return; }
    metricsDepth = data.depth || "structural";
    renderMetrics(data);
    metricsStale = false;
  } catch {
    notify("couldn't measure the vault");
  } finally {
    metricsLoading = false;
    loading.hidden = true;
    body.style.opacity = "";
  }
}

$("#metrics-refresh").addEventListener("click", () => loadMetrics(true, metricsDepth === "full"));

// Clicking any row that names a note opens it in the drawer — the metrics are
// only useful if the note they point at is one click away.
$("#metrics-body").addEventListener("click", (e) => {
  if (e.target.id === "metrics-proposals") { loadMetrics(true, true); return; }
  const row = e.target.closest("[data-path]");
  if (row && row.dataset.path) openNote(row.dataset.path);
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
  head.appendChild(mkEl("div", "hero-lbl", "E(vault) — lattice energy"));
  head.appendChild(hv);
  head.appendChild(mkEl("p", "hero-sub",
    "Lower is more coherent. A thermometer, not a target: read it to compare runs, "
    + "never descend it. "
    + (full
      ? "Measured at full depth — comparable only to other full-depth readings."
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
  } else mEmpty(cl, "No communities yet — link some notes.");
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
        ...tiers.map((t) => ({ k: "Tier — " + t.label, v: nfmt(t.value) })),
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
  const gp = mCard("Structural gaps", "well-formed areas with few links between them");
  if (d.gaps?.length) {
    // Sizes, not the absent-link fraction: that fraction reads 99.7-100% on
    // every row of a real vault, so it cannot explain why row 1 outranks row
    // 20. Size × size ÷ (1 + links) is the actual ranking, and with both
    // sizes on the row the order is readable instead of asserted.
    gp.appendChild(mTable(
      [{ key: "pair", label: "Area hubs" }, { key: "sizes", label: "Notes", num: true },
       { key: "inter_edges", label: "Links", num: true }],
      d.gaps.map((g) => ({
        pair: g.a + " ↮ " + g.b, sizes: `${g.size_a} × ${g.size_b}`,
        inter_edges: g.inter_edges,
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
  } else mEmpty(orph, "None — every note is reachable.");
  grid.appendChild(orph);

  const dg = mCard("Unresolved links", "wikilink targets that don't exist yet");
  if (d.dangling?.length) {
    dg.appendChild(mTable(
      [{ key: "target", label: "Target" }, { key: "refs", label: "Refs", num: true }],
      d.dangling,
    ));
    const more = mMore(d.dangling.length, T.dangling_links || 0, "targets");
    if (more) dg.appendChild(more);
  } else mEmpty(dg, "None — every wikilink resolves.");
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
    const ask = mCard("Proposals", "co-occurrence delta — not yet measured");
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
  $("#map-frame").src = "/map?note=" + encodeURIComponent(path) + "&t=" + Date.now();
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
}

// Mirror the open note onto the graph + map iframes: the matching node + its
// 1-hop neighbours go full-opacity, everything else dims. No-op harmlessly if
// a tab was never opened (contentWindow still exists, message just has no
// listener yet).
function focusGraphNode(path) {
  for (const id of ["#graph-frame", "#map-frame"]) {
    const frame = $(id);
    if (frame.contentWindow) frame.contentWindow.postMessage({ type: "silica-focus-path", path }, "*");
  }
}

// Mermaid is a 3.5MB vendored bundle, so it loads on demand — only the first
// time an opened note actually contains a ```mermaid fence. Render failures
// leave the fence as plain text (suppressErrorRendering).
let mermaidLoad = null;
function renderMermaid(root) {
  const blocks = root.querySelectorAll("pre.mermaid");
  if (!blocks.length) return;
  mermaidLoad ||= new Promise((resolve) => {
    const s = document.createElement("script");
    s.src = "/static/mermaid.min.js";
    s.onload = () => {
      mermaid.initialize({
        startOnLoad: false, theme: "dark", suppressErrorRendering: true,
        fontFamily: "Martian Mono, ui-monospace, monospace",
        themeVariables: {
          darkMode: true, background: "#0A0D14",
          primaryColor: "#161B27", primaryTextColor: "#E8ECF5",
          primaryBorderColor: "#38425A", lineColor: "#8B95AC",
        },
      });
      resolve();
    };
    document.head.appendChild(s);
  });
  mermaidLoad.then(() => mermaid.run({ nodes: blocks }).catch(() => {}));
}

// Below this width the shell cannot hold the sidebar, a readable transcript and
// the drawer at once: at 900px an open 55vw drawer left 141px of prose. Two panes
// is the answer, so the sidebar yields — via the real `sidebar-collapsed` class,
// so #sidebar-toggle keeps telling the truth — and comes back with the note,
// unless the user had collapsed it themselves.
const NARROW_W = 1100;
let sidebarYielded = false;

function yieldSidebarToDrawer() {
  if (window.innerWidth > NARROW_W || document.body.classList.contains("sidebar-collapsed")) return;
  document.body.classList.add("sidebar-collapsed");
  sidebarYielded = true;
}

function restoreYieldedSidebar() {
  if (!sidebarYielded) return;
  document.body.classList.remove("sidebar-collapsed");
  sidebarYielded = false;
}

async function openNote(path) {
  if (!path) return;
  lastNotePath = path;
  lastViewedPath = path;
  focusGraphNode(path);
  $("#note-mini-map").open = false; // reset: reload lazily if reopened for the new note
  $("#note-mini-map-frame").src = "";
  try {
    const r = await fetch("/note?path=" + encodeURIComponent(path));
    const data = await r.json();
    $("#note-title").textContent = data.title || "";
    $("#note-body").innerHTML = data.html || "";
    renderMermaid($("#note-body"));
    $("#note-body").scrollTop = 0;
    notePanel.classList.add("open");
    notePanel.setAttribute("aria-hidden", "false");
    document.body.classList.add("note-open"); // dock + chat inset to the drawer's edge
    yieldSidebarToDrawer();
    const btn = $("#note-last");
    btn.querySelector("span").textContent = data.title || path;
  } catch { notify("couldn't open that note"); }
}
function closeNote() {
  notePanel.classList.remove("open");
  notePanel.setAttribute("aria-hidden", "true");
  document.body.classList.remove("note-open");
  restoreYieldedSidebar();
  lastNotePath = null; // lastViewedPath survives — the header button can reopen
  focusGraphNode(null);
}
$("#note-last").addEventListener("click", () => {
  if (lastViewedPath) openNote(lastViewedPath);
});

// Mini-map: load only when expanded (native <details>), so a plain note read
// never pays for a /map render.
$("#note-mini-map").addEventListener("toggle", function () {
  if (this.open && lastNotePath) {
    $("#note-mini-map-frame").src = "/map?note=" + encodeURIComponent(lastNotePath);
  }
});

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
// The drawer's max-width is viewport-relative, so its rendered width changes with
// the window. --note-w drives the header and dock insets, so it has to follow or
// they reserve the wrong gap and the drawer covers #stop / #dock-send again.
window.addEventListener("resize", syncNoteW);
let resizingNote = false; // guards the outside-click-closes handler below: a drag
                           // that ends outside #note-panel fires a "click" there too
$("#note-resize").addEventListener("mousedown", (e) => {
  e.preventDefault();
  resizingNote = true;
  const startX = e.clientX, startWidth = notePanel.getBoundingClientRect().width;
  const onMove = (e2) => {
    const w = Math.min(NOTE_MAX_W, Math.max(NOTE_MIN_W, startWidth + (startX - e2.clientX)));
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
  const link = e.target.closest(".note-link");
  if (link) { e.preventDefault(); openNote(link.dataset.path); return; }
  if (notePanel.classList.contains("open") &&
      !e.target.closest("#note-panel") && !e.target.closest("#sidebar") &&
      !e.target.closest("#dock") && !e.target.closest("#note-last")) closeNote();
});
$("#note-close").addEventListener("click", closeNote);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNote(); });
// Graph node clicks (in the iframe) post a message up when embedded.
window.addEventListener("message", (e) => {
  if (e.data && e.data.type === "silica-open-note") openNote(e.data.path);
});

// --- session bootstrap (re-render server-side history; never resets on load) -
async function loadVault() {
  try {
    const r = await fetch("/messages");
    $("#vault").textContent = r.headers.get("X-Silica-Vault") || "";
    setCtxTokens(r.headers.get("X-Silica-Context-Tokens"), r.headers.get("X-Silica-Max-Context-Tokens"));
    const msgs = await r.json();
    log.innerHTML = "";
    for (const m of msgs) {
      const b = bubble(m.role === "user" ? "user" : "silica");
      if (m.role === "user") b.textContent = m.content;
      else { b.innerHTML = m.html || escapeHtml(m.content); addCopyBtn(b, () => m.content); }
    }
  } catch { notify("couldn't load the conversation"); }
}
// --- quick-action launch pad (empty chat only; CSS collapses it on first turn).
// Command chips prefill the composer (the user reviews + hits enter); action
// chips fire directly.
$("#quick-actions").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const a = btn.dataset.action;
  if (a === "attach") $("#attach").click();
  else if (a === "graph") document.querySelector('.tab[data-tab="graph"]').click();
  else { input.value = a + " "; input.focus(); autoGrow(input); renderCommands(input.value); }
});

// --- session config panel (header) — model read-only (Silica has no runtime
// model-switch op, mirroring the TUI's display-only /model) + the live thinking
// toggle (/thinking). Progressive disclosure: nothing until the model chip is
// clicked.
const sessionPanel = $("#session-panel");
const modelBtn = $("#model-btn");
let configLoaded = false;
async function loadConfig() {
  try {
    const c = await (await fetch("/config")).json();
    $("#model-name").textContent = c.model ? c.model.split("/").pop() : "no model";
    $("#sp-model").textContent = c.model || "(not configured)";
    $("#sp-provider").textContent = c.provider || "—";
    $("#sp-ctx").textContent = c.context_window ? (c.context_window / 1000).toFixed(0) + "k tokens" : "—";
    $("#sp-thinking").checked = !!c.show_thinking;
    configLoaded = true;
  } catch { notify("couldn't load session config"); }
}
function closeSessionPanel() {
  sessionPanel.hidden = true;
  modelBtn.setAttribute("aria-expanded", "false");
}
modelBtn.addEventListener("click", (e) => {
  e.stopPropagation(); // don't let the outside-click handler below re-close it
  const opening = sessionPanel.hidden;
  sessionPanel.hidden = !opening;
  modelBtn.setAttribute("aria-expanded", opening ? "true" : "false");
  if (opening && !configLoaded) loadConfig();
});
$("#sp-thinking").addEventListener("change", async (e) => {
  const want = e.target.checked;
  try {
    await fetch("/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ show_thinking: want }),
    });
  } catch { notify("couldn't update thinking"); e.target.checked = !want; }
});
document.addEventListener("click", (e) => {
  if (!sessionPanel.hidden && !e.target.closest("#session-panel") && !e.target.closest("#model-btn"))
    closeSessionPanel();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSessionPanel(); });

// --- boot health: the doctor's non-ok rows as toasts -------------------------
// The TUI logs a degraded embedder/reranker to stderr; the browser sees none of
// that, so without this a server left down just makes recall quietly worse.
async function loadHealth() {
  try {
    for (const r of await (await fetch("/health")).json())
      notify(r.name + ": " + r.detail + (r.hint ? " — " + r.hint : ""));
  } catch { /* the page works without the report; don't toast about the toast */ }
}

loadVault();
loadSessions();
loadVaultInfo();
loadConfig(); // header shows the active model without opening the panel
loadHealth(); // a chat/embedder/reranker server that isn't up says so, once, here
// Land on chat — it's the primary surface. The tab handler does the rest.
document.querySelector('.tab[data-tab="chat"]').click();
