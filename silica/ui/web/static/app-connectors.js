// --- connectors ----------------------------------------------------------------
// Settings › Connectors (ADR-0089 point 8): one section, one block per
// connector, drawn from GET /connectors and acted on through
// POST /connectors/<name>/<action>. A block draws itself through
// ST_CONNECTOR_RENDERERS[name] when one is registered (app-mail.js registers
// "mail"), else through connGeneric. The MCP client's block and its approval
// card (ADR-0091) live here too.

window.ST_CONNECTOR_RENDERERS = window.ST_CONNECTOR_RENDERERS || {};

async function connAct(name, action, payload) {
  const r = await fetch(`/connectors/${name}/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  let d = {};
  try { d = await r.json(); } catch { /* an empty answer is still an answer */ }
  if (!r.ok || d.ok === false) {
    const err = new Error(d.error || d.detail || `HTTP ${r.status}`);
    err.data = d; // a refusal can carry what the row offers next (no_store)
    throw err;
  }
  return d;
}

async function renderConnectors(host) {
  host.querySelectorAll(".st-connector, .st-note-line").forEach((e) => e.remove());
  let blocks = [];
  try { blocks = await (await fetch("/connectors")).json(); }
  catch { host.appendChild(stEl("div", "st-note-line", "could not read the connectors")); return; }
  for (const status of blocks) {
    const el = stEl("div", "st-connector");
    el.dataset.connector = status.name;
    host.appendChild(el);
    connDraw(el, status);
  }
}

// One block, redrawn on its own: a Log in polling one row must not wipe the
// half-typed paste in another block's field.
function connDraw(el, status) {
  el.innerHTML = "";
  el.appendChild(stEl("div", "st-conn-title", status.title || status.name));
  const rerender = async () => {
    let blocks = [];
    try { blocks = await (await fetch("/connectors")).json(); } catch { return; }
    const fresh = blocks.find((b) => b.name === status.name);
    if (fresh) connDraw(el, fresh);
  };
  const fn = window.ST_CONNECTOR_RENDERERS[status.name] || connGeneric;
  fn(el, status, rerender);
}

function connRow(host, name, help, search) {
  const row = stEl("div", "st-row");
  row.dataset.search = `connector ${search || ""} ${name} ${help || ""}`.toLowerCase();
  const label = stEl("div", "st-label");
  label.appendChild(stEl("span", "st-name", name));
  if (help) label.appendChild(stEl("div", "st-help", help));
  row.appendChild(label);
  const ctl = stEl("div", "st-ctl");
  row.appendChild(ctl);
  const note = stEl("div", "st-note");
  row.appendChild(note);
  host.appendChild(row);
  return { row, ctl, note };
}

function connBtn(text, cls) {
  const b = stEl("button", "st-btn" + (cls ? " " + cls : ""), text);
  b.type = "button";
  return b;
}

// Run one action from a button: disabled while it runs, its outcome in `note`.
async function connRun(btn, note, name, action, payload, done) {
  btn.disabled = true;
  note.className = "st-note pending";
  note.textContent = "…";
  try {
    const out = await connAct(name, action, payload);
    note.className = "st-note good";
    note.textContent = "done";
    if (done) await done(out);
  } catch (e) {
    note.className = "st-note bad";
    note.textContent = String(e.message || e);
    btn.disabled = false;
  }
}

function connGeneric(el, status, rerender) {
  const { ctl, note } = connRow(el, status.state || "", status.detail || "", status.name);
  for (const action of status.actions || []) {
    const b = connBtn(action);
    b.addEventListener("click", () => connRun(b, note, status.name, action, {}, rerender));
    ctl.appendChild(b);
  }
}

// --- the MCP client's block (ADR-0091, "GUI and TUI") -------------------------
function mcpStateClass(state) {
  if (state.startsWith("connected")) return "up";
  if (state.startsWith("failed") || state.startsWith("inline")) return "down";
  return "";
}

window.ST_CONNECTOR_RENDERERS.mcp = function renderMcp(el, status, rerender) {
  if (status.state === "local-only") {
    connRow(el, "local only", status.detail, "mcp");
    return;
  }
  const head = connRow(el, status.detail || "", `servers are read from ${status.file}`, "mcp reload");
  const reload = connBtn("reload");
  reload.title = "read mcp.json again; the chat's tool list changes once";
  reload.addEventListener("click", () => connRun(reload, head.note, "mcp", "reload", {}, rerender));
  head.ctl.appendChild(reload);
  if (status.detail_extra) head.note.textContent = status.detail_extra;
  for (const r of status.rows || []) mcpServerRow(el, r, rerender);
  mcpAddRow(el, rerender);
};

function mcpServerRow(el, r, rerender) {
  const { row, ctl, note } = connRow(el, r.key, r.kind, "mcp server " + r.state);
  row.classList.add("st-mcp-server");
  const state = stEl("div", "st-ctl-text " + mcpStateClass(r.state), r.state);
  ctl.appendChild(state);

  for (const name of r.needs || []) {
    // One password field per missing secret: it goes to the keyring (or the
    // opted-in file) and never into the .env.
    const field = stEl("input");
    field.type = "password";
    field.autocomplete = "off";
    field.placeholder = name;
    field.setAttribute("aria-label", `${r.key} ${name}`);
    const save = connBtn("save");
    save.addEventListener("click", () => {
      if (!field.value) return;
      connRun(save, note, "mcp", "secret", { key: r.key, name, value: field.value }, rerender);
    });
    const line = stEl("div", "st-mcp-secret");
    line.append(field, save);
    row.insertBefore(line, note);
  }
  if (((r.needs || []).length || (r.inline || []).length || r.oauth) && !r.store) {
    note.textContent = "no keyring on this machine: a secret or a login can only be kept in a 0600 file under ~/.silica/credentials";
    const file = connBtn("store in a file");
    file.addEventListener("click", () => connRun(file, note, "mcp", "store_in_file", { key: r.key }, rerender));
    ctl.appendChild(file);
  } else if (r.store === "file") {
    row.querySelector(".st-help").textContent = `${r.kind} · stored in a file`;
  }
  if ((r.inline || []).length) {
    const move = connBtn(r.store === "file" ? "move to the file" : "move to keyring", "primary");
    move.addEventListener("click", () => connRun(move, note, "mcp", "secure", { key: r.key }, rerender));
    ctl.appendChild(move);
  }
  if (r.client_id_field) {
    // Neither CIMD nor dynamic registration worked: the spec's last step is a
    // client ID the user registered with the service by hand.
    const field = stEl("input");
    field.type = "text";
    field.autocomplete = "off";
    field.spellcheck = false;
    field.placeholder = "client ID registered by hand";
    field.setAttribute("aria-label", `${r.key} client ID`);
    const save = connBtn("save");
    save.addEventListener("click", () => {
      if (!field.value.trim()) return;
      connRun(save, note, "mcp", "client_id", { key: r.key, client_id: field.value.trim() }, rerender);
    });
    const line = stEl("div", "st-mcp-secret");
    line.append(field, save);
    row.insertBefore(line, note);
  }
  if (r.oauth) {
    if (r.logged_in) {
      const out = connBtn("log out");
      out.addEventListener("click", () => connRun(out, note, "mcp", "logout", { key: r.key }, rerender));
      ctl.appendChild(out);
    } else {
      const login = connBtn("log in", "primary");
      login.addEventListener("click", () => mcpLogin(login, note, r.key, rerender));
      ctl.appendChild(login);
    }
  }
  const remove = connBtn("remove");
  remove.addEventListener("click", async () => {
    const ok = await stAsk(`remove ${r.key}?`,
      "Its entry leaves mcp.json and its saved secrets and login leave the keyring.", "remove");
    if (ok) connRun(remove, note, "mcp", "remove", { key: r.key }, rerender);
  });
  ctl.appendChild(remove);

  if ((r.tools || []).length) {
    const list = stEl("details", "st-mcp-tools");
    list.appendChild(stEl("summary", "", `${r.tools.length} tools`));
    for (const t of r.tools) {
      const item = stEl("div", "st-mcp-tool" + (t.note ? " off" : ""), t.name);
      if (t.note) item.appendChild(stEl("span", "st-mcp-tool-note", " · " + t.note));
      list.appendChild(item);
    }
    row.insertBefore(list, note);
  }
}

// Log in opens the browser on this machine and shows the link as well, for
// the case where no browser can open; the row turns connected when the
// callback lands, so it is polled until then.
async function mcpLogin(btn, note, key, rerender) {
  btn.disabled = true;
  note.className = "st-note pending";
  note.textContent = "opening the login page…";
  let out;
  try { out = await connAct("mcp", "login", { key }); }
  catch (e) { note.className = "st-note bad"; note.textContent = String(e.message || e); btn.disabled = false; return; }
  if (out.connected) { rerender(); return; }
  note.textContent = "";
  if (out.url) {
    note.append("waiting for the browser · ");
    const a = stEl("a", "", "open the login page");
    a.href = out.url;
    a.target = "_blank";
    a.rel = "noopener";
    note.appendChild(a);
  }
  const t0 = Date.now();
  const poll = async () => {
    let blocks = [];
    try { blocks = await (await fetch("/connectors")).json(); } catch { /* try again */ }
    const row = ((blocks.find((b) => b.name === "mcp") || {}).rows || []).find((x) => x.key === key);
    // Until the login ends, either way: a refused one redraws with its reason.
    if (row && (row.state.startsWith("connected") || !row.logging_in)) { rerender(); return; }
    if (Date.now() - t0 > 300000) { note.className = "st-note bad"; note.textContent = "no login arrived"; btn.disabled = false; return; }
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 2000);
}

function mcpAddRow(el, rerender) {
  const { row, ctl, note } = connRow(el, "add a server",
    "paste {\"mcpServers\": {...}} from Claude Code or Claude Desktop, or one {\"name\": {...}}; " +
    "a secret in it moves to the keyring", "mcp add paste json");
  row.classList.add("st-mcp-add");
  const text = stEl("textarea", "st-mcp-paste");
  text.rows = 4;
  text.spellcheck = false;
  text.placeholder = '{"mcpServers": {"github": {"type": "http", "url": "https://…"}}}';
  text.setAttribute("aria-label", "MCP server JSON");
  row.insertBefore(text, note);
  const add = connBtn("add", "primary");
  const send = async (inFile) => {
    add.disabled = true;
    note.className = "st-note pending";
    note.textContent = "adding, and listing its tools…";
    try {
      const out = await connAct("mcp", "add", { json: text.value, store_in_file: inFile });
      await rerender();
      // An entry Add could not take (an sse one) is named under the fresh
      // add row, so it is not lost with the redraw that shows the added ones.
      const fresh = el.querySelector(".st-mcp-add .st-note");
      if (fresh && (out.refused || []).length) {
        fresh.className = "st-note bad";
        for (const why of out.refused) fresh.appendChild(stEl("div", "", "not added: " + why));
      }
    } catch (e) {
      note.className = "st-note bad";
      note.textContent = String(e.message || e);
      add.disabled = false;
      if (e.data && e.data.no_store && !inFile) {
        // No keyring here: the opt-in is offered where the secret arrived.
        const file = connBtn("keep its secrets in a file");
        file.title = "one 0600 file per server under ~/.silica/credentials";
        file.addEventListener("click", () => { file.remove(); send(true); });
        ctl.insertBefore(file, add);
      }
    }
  };
  add.addEventListener("click", () => { if (text.value.trim()) send(false); });
  ctl.appendChild(add);
}

// --- the approval card (ADR-0091 point 6) -------------------------------------
// In the transcript, under the connector's row: the server, the tool and the
// full arguments, Run and Deny. No button takes the focus, so a stray Enter
// does not approve. The card settles at its own wait, or when the turn ends.
const OPEN_APPROVALS = new Set();

function approvalSettle(card, text) {
  if (!OPEN_APPROVALS.has(card)) return;
  OPEN_APPROVALS.delete(card);
  clearTimeout(card._timer);
  card.dataset.settled = "";
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  card.querySelector(".approval-state").textContent = text;
}

function approvalCard(ev) {
  const card = document.createElement("div");
  card.className = "approval";
  card.setAttribute("role", "group");
  card.setAttribute("aria-label", `${ev.server} ${ev.tool} wants to run`);
  const head = document.createElement("div");
  head.className = "approval-head";
  head.textContent = `${ev.server} › ${ev.tool} wants to run`;
  const args = document.createElement("div");
  args.className = "approval-args";
  args.textContent = JSON.stringify(ev.args, null, 2);
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  const run = connBtn("run", "primary");
  const deny = connBtn("deny");
  const state = document.createElement("span");
  state.className = "approval-state";
  state.setAttribute("aria-live", "polite");
  actions.append(run, deny, state);
  card.append(head, args, actions);
  const answer = async (yes) => {
    run.disabled = deny.disabled = true;
    try {
      await connAct("mcp", "approve", { id: ev.id, run: yes });
      approvalSettle(card, yes ? "approved" : "denied");
    } catch {
      approvalSettle(card, "not run: no answer");
    }
  };
  run.addEventListener("click", () => answer(true));
  deny.addEventListener("click", () => answer(false));
  OPEN_APPROVALS.add(card);
  card._timer = setTimeout(() => approvalSettle(card, "not run: no answer"), (ev.timeout_s || 120) * 1000);
  return card;
}

function settleApprovalCards() {
  for (const card of [...OPEN_APPROVALS]) approvalSettle(card, "not run: no answer");
}
