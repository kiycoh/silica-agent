// --- mail ---------------------------------------------------------------------
// ADR-0090 "GUI and TUI": the Mail row of Settings › Connectors (the shell is
// ADR-0089 point 8's, in app-settings.js). Part A only: import a file the user
// exported, a dry run first, then Import in one undo run, which the row's
// undo takes back by its run id. What the dry run
// SAYS is decided by the pure functions below, which tests/test_mail_web_ui.py
// runs under node; the DOM half under them only places it.

// mail-pure:start
function mailCount(n, word) {
  const v = Number(n || 0);
  return `${v.toLocaleString("en-US")} ${word}${v === 1 ? "" : "s"}`;
}

// "214 threads you took part in, 3,180 in the file; the first 200 would be
// written, 3 already in the vault": the ADR's own sentence, so the row and the
// record read alike.
function mailPlanLine(p) {
  const who = p.takeout ? "you sent in (Gmail's Sent label)" : "you took part in";
  const n = (p.threads || []).length;
  const writes = p.over_limit ? `the first ${n.toLocaleString("en-US")} would be written`
    : `${n.toLocaleString("en-US")} would be written`;
  return `${mailCount(p.took_part, "thread")} ${who}, `
    + `${Number(p.in_file || 0).toLocaleString("en-US")} in the file; ${writes}, `
    + `${Number(p.present || 0).toLocaleString("en-US")} already in the vault`;
}

// The --all switch, named by what it adds; "" when it would add nothing.
function mailAllLabel(p) {
  return p.added_by_all ? `also the ${mailCount(p.added_by_all, "thread")} you took no part in` : "";
}

// Why Import is not offered, "" when it is. A guessed address is not the
// user's word (point 4): the backend refuses it, and the button used to fill
// the guess in as if the user had typed it (2026-10-12).
function mailImportBlock(p) {
  if (!(p.threads || []).length) return "nothing to write";
  if (p.guessed && !p.takeout) return "confirm your address above first";
  return "";
}

// What an undo did, from the revert's own counts: a note edited since the
// import is kept, never clobbered, and the line says so.
function mailUndoLine(out) {
  let line = `removed ${mailCount(out.removed, "note")}`;
  if (out.kept) line += ` · kept ${mailCount(out.kept, "note")} you edited since`;
  if (out.gone) line += ` · ${mailCount(out.gone, "note")} already gone`;
  if ((out.errors || []).length) line += ` · ${out.errors.length} failed: ${out.errors[0]}`;
  return line;
}
// mail-pure:end

async function mailAct(action, payload) {
  const r = await fetch(`/connectors/mail/${action}`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}) });
  let d = {};
  try { d = await r.json(); } catch { /* an empty answer is still an answer */ }
  if (!r.ok || d.ok === false) throw new Error(d.error || d.detail || `HTTP ${r.status}`);
  return d;
}

function mailRow(host, label, help) {
  const row = stEl("div", "st-row");
  row.dataset.search = `mail ${label} ${help || ""}`.toLowerCase();
  const lab = stEl("div", "st-label");
  lab.appendChild(stEl("span", "st-name", label));
  if (help) lab.appendChild(stEl("div", "st-help", help));
  row.appendChild(lab);
  const ctl = stEl("div", "st-ctl");
  row.appendChild(ctl);
  const note = stEl("div", "st-note");
  row.appendChild(note);
  host.appendChild(row);
  return { row, ctl, note };
}

function mailSay(note, cls, text) { note.className = "st-note " + cls; note.textContent = text; }

// The file the dry run is about and the fields set on it, kept across a
// redraw of the block. Import and Undo redraw it (the undo row is the
// status's), and with 200 threads a run a big export is imported in rounds:
// a redraw that forgot the file meant uploading gigabytes again for the next
// round (2026-10-12).
let mailForm = null;

// The newest import the journal can still take back, as status.last names it:
// undone by its run id, never by /revert's "latest run", which after a chat
// write since the import is that write.
function mailUndoRow(blockEl, last, rerender) {
  const row = mailRow(blockEl, "last import", `${last.file}, ${last.at}`);
  const undo = stEl("button", "st-btn", `undo ${mailCount(last.written, "note")}`);
  undo.type = "button";
  undo.addEventListener("click", async () => {
    const ok = await stAsk(`undo the import of ${last.file}?`,
      "Its notes leave sources/mail in one step; a note you edited since stays. "
      + "The file stays in the inbox, so it can be imported again.", "undo");
    if (!ok) return;
    undo.disabled = true;
    try {
      const out = await mailAct("undo", { run_id: last.run_id });
      mailSay(row.note, (out.errors || []).length ? "bad" : "ok", mailUndoLine(out));
      setTimeout(rerender, 1500);
    } catch (err) {
      mailSay(row.note, "bad", err.message);
      undo.disabled = false;
    }
  });
  row.ctl.appendChild(undo);
}

window.ST_CONNECTOR_RENDERERS = window.ST_CONNECTOR_RENDERERS || {};
window.ST_CONNECTOR_RENDERERS.mail = function renderMail(blockEl, status, rerender) {
  blockEl.appendChild(stEl("div", "st-note-line", status.detail || "Keep chosen mail threads as notes"));
  const file = mailRow(blockEl, "Import a file",
    "an .mbox or .eml you exported: your mail client's export, or Google Takeout");
  const input = stEl("input");
  input.type = "file";
  input.accept = ".mbox,.eml";
  // The server's list, which the composer's picker leaves out (a chat drop
  // stages no mail); the literal above stands in until it answers.
  fetch("/supported_types").then((r) => r.json())
    .then((d) => { if (d.mail && d.mail.length) input.accept = d.mail.join(","); })
    .catch(() => { /* the literal stays: the same two types */ });
  input.hidden = true;
  const pick = stEl("button", "st-btn", "Import a file");
  pick.type = "button";
  pick.addEventListener("click", () => input.click());
  file.ctl.append(input, pick);
  if (status.account) file.note.textContent = `your addresses: ${status.account}`;
  if (status.imported) file.note.textContent += `${status.account ? " · " : ""}${mailCount(status.imported, "thread")} imported`;
  if (status.last) mailUndoRow(blockEl, status.last, rerender);

  const dry = stEl("div", "st-mail-dry");
  blockEl.appendChild(dry);
  // The fields are built once per file and only the result under them is
  // redrawn: a dry run that rebuilt the whole block on the address field's
  // blur took away the checkbox the same click was landing on (2026-10-12).
  const fields = stEl("div");
  const result = stEl("div");
  dry.append(fields, result);
  let form = mailForm || {};
  let controls = null;
  let seq = 0; // two dry runs in flight (a blur, then a tick): the last one draws

  function buildFields(p) {
    fields.replaceChildren();
    let me = null;
    if (!p.takeout) {
      const row = mailRow(fields, "your address", "the threads kept are the ones a message of yours is in");
      // type=text, not email: app-settings.css styles text fields, and a
      // comma-separated list is not one address for the browser to check.
      const box = stEl("input");
      box.type = "text";
      box.value = (p.me || []).join(", ");
      box.setAttribute("aria-label", "your mail addresses, comma-separated");
      // Confirm sends the field as it stands: a guess the user agrees with is
      // never edited, so the field's change event alone could not say yes.
      const confirm = stEl("button", "st-btn", "confirm");
      confirm.type = "button";
      const take = () => { form.me = box.value.trim(); plan(); };
      box.addEventListener("change", take);
      box.addEventListener("input", () => { confirm.disabled = !box.value.trim(); });
      confirm.addEventListener("click", take);
      row.ctl.append(box, confirm);
      me = { row, box, confirm };
    } else {
      const lab = mailRow(fields, "label", "Takeout: keep the threads under one Gmail label");
      const box = stEl("input");
      box.type = "text";
      box.placeholder = "any label";
      box.value = form.label || "";
      box.setAttribute("aria-label", "Gmail label");
      box.addEventListener("change", () => { form.label = box.value.trim(); plan(); });
      lab.ctl.appendChild(box);
    }
    const all = mailRow(fields, "include", "every thread in the file");
    const allHelp = all.row.querySelector(".st-help");
    const tick = stEl("input");
    tick.type = "checkbox";
    tick.checked = Boolean(form.all);
    tick.addEventListener("change", () => { form.all = tick.checked; plan(); });
    all.ctl.appendChild(tick);
    return {
      update(q) {
        if (me) {
          me.confirm.hidden = !q.guessed;
          me.confirm.disabled = !me.box.value.trim();
          if (q.guessed) mailSay(me.row.note, "pending", "guessed from the file: confirm it or correct it");
          else mailSay(me.row.note, "", "");
        }
        const text = mailAllLabel(q);
        all.row.hidden = !(text || form.all);
        allHelp.textContent = text || "every thread in the file";
        tick.setAttribute("aria-label", allHelp.textContent);
      },
    };
  }

  function drawResult(p) {
    result.replaceChildren();
    result.appendChild(stEl("div", "st-note-line", mailPlanLine(p)));
    // st-note-line for the type and colour the panel's own lines use; layout
    // only inline: a 200-thread list scrolls inside the row instead of pushing
    // Import a screen away, and a subject wraps anywhere, so one with no space
    // in it (a stranger's) cannot scroll the list sideways (2026-10-12).
    const list = stEl("ul", "st-note-line st-mail-threads");
    list.style.cssText = "max-height:14rem;overflow:hidden auto;margin:0;padding:0 0 .6rem 1.2rem";
    for (const t of p.threads) {
      const li = stEl("li", "", `${t.date || "undated"}  ${t.subject || "no subject"} · ${mailCount(t.messages, "message")}`);
      li.style.overflowWrap = "anywhere";
      li.title = t.path;
      list.appendChild(li);
    }
    result.appendChild(list);
    const go = mailRow(result, "Import", "writes them under sources/mail in one run, which undo takes back");
    const btn = stEl("button", "st-btn", `Import ${mailCount(p.threads.length, "thread")}`);
    btn.type = "button";
    const blocked = mailImportBlock(p);
    btn.disabled = Boolean(blocked);
    if (blocked) mailSay(go.note, "pending", blocked);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const out = await mailAct("import", form);
        const failed = out.failed || [];
        mailSay(go.note, failed.length ? "bad" : "ok", `wrote ${mailCount(out.written, "note")}`
          + (failed.length ? ` · ${mailCount(failed.length, "thread")} not written: ${failed[0].error}` : ""));
        setTimeout(rerender, 1500);
      } catch (err) {
        mailSay(go.note, "bad", err.message);
        btn.disabled = false;
      }
    });
    go.ctl.appendChild(btn);
  }

  async function plan() {
    const mine = ++seq;
    // The list on screen no longer answers what the fields say: Import waits
    // for the dry run that does (point 5, the dry run is the confirmation).
    for (const b of result.querySelectorAll("button")) b.disabled = true;
    let p;
    try { p = await mailAct("plan", form); }
    catch (err) {
      if (mine === seq) {
        result.replaceChildren();
        mailSay(file.note, "bad", err.message);
        mailForm = null; // a file the server cannot read is not one to come back to
      }
      return;
    }
    if (mine !== seq) return;
    if (!controls) {
      controls = buildFields(p);
      mailSay(file.note, "ok", `${p.file} is in ${form.path}; nothing is written until Import`);
    }
    controls.update(p);
    drawResult(p);
  }

  input.addEventListener("change", async () => {
    const f = input.files && input.files[0];
    if (!f) return;
    mailSay(file.note, "pending", `uploading ${f.name} into the inbox…`);
    try {
      // The file as the body, not a form: the server streams it to the inbox
      // with no copy in its temp dir, and a Takeout export runs to gigabytes.
      const r = await fetch(`/mail/upload?name=${encodeURIComponent(f.name)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: f });
      let d = {};
      try { d = await r.json(); } catch { /* an empty answer: the status says it */ }
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      form = mailForm = { path: d.path, me: "", label: "", all: false };
      controls = null;
      fields.replaceChildren();
      result.replaceChildren();
      // The first dry run of a big export parses all of it (about 20 s a GB).
      mailSay(file.note, "pending", `${f.name} is in ${d.path}; reading it…`);
      plan();
    } catch (err) {
      mailSay(file.note, "bad", err.message);
    }
    input.value = "";
  });

  if (form.path) plan();
};
