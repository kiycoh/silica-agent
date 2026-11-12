// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Alessandro Carosia

// The Calendar's Decisions view (ADR-0167): the open decisions, the ones due
// for review first, what was expected beside what came of it, and a way to add
// an outcome. Nothing here compares the two columns: that reading is the
// person's, and a model's verdict on it would be one more claim to check.

const DEC_STATUS = [["", "still open"], ["kept", "kept"], ["changed", "changed"], ["reversed", "reversed"]];
let decSeq = 0;

function decDay(s) {
  return s ? fmtDay(new Date(s + "T00:00"), { year: "numeric" }) : "";
}

function decColumn(title, rows, empty) {
  const col = mkEl("div", "dec-col");
  col.appendChild(mkEl("div", "dec-col-h", title));
  if (!rows.length) col.appendChild(mkEl("div", "dec-empty", empty));
  for (const r of rows) col.appendChild(r);
  return col;
}

function decOutcomeForm(card, d) {
  if (card.querySelector(".dec-form")) { card.querySelector(".dec-form input").focus(); return; }
  const form = mkEl("form", "dec-form");
  const input = mkEl("input");
  input.type = "text";
  input.placeholder = "what happened";
  input.setAttribute("aria-label", "what happened");
  const status = mkEl("select");
  status.setAttribute("aria-label", "the decision now");
  for (const [v, label] of DEC_STATUS) {
    const o = mkEl("option", "", label);
    o.value = v;
    status.appendChild(o);
  }
  const save = mkEl("button", "ctr-act", "save");
  save.type = "submit";
  const cancel = mkEl("button", "ctr-act", "cancel");
  cancel.type = "button";
  const acts = card.querySelector(".dec-acts");
  cancel.addEventListener("click", () => { form.remove(); acts.hidden = false; });
  form.append(input, status, save, cancel);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!input.value.trim()) { input.focus(); return; }
    save.disabled = true;
    let res;
    try {
      res = await apiPost("/calendar/decisions/outcome", { path: d.path, text: input.value, status: status.value });
    } catch (err) {
      notify(err.message || "couldn't add the outcome");
      save.disabled = false;
      return;
    }
    notify("Outcome added to " + noteStem(d.path) + ".", "info", res.run ? {
      label: "undo",
      run: async () => { if (await undoTreeRun(res.run)) { refreshOpenNote(); renderDecisions($("#cal-grid")); } },
    } : null);
    refreshOpenNote();
    renderDecisions($("#cal-grid"));
  });
  acts.before(form);
  acts.hidden = true;  // the form's own buttons stand in for it while it is open
  input.focus();
}

function decCard(d) {
  const card = mkEl("article", "dec-card" + (d.due ? " due" : ""));
  const head = mkEl("div", "dec-head");
  const title = mkEl("button", "dec-title", d.name);
  title.type = "button";
  title.title = "open " + d.path;
  title.addEventListener("click", () => openNote(d.path));
  head.appendChild(title);
  const meta = [`decided ${decDay(d.decided)}`];
  if (d.review_on) meta.push((d.due ? "review was due " : "review ") + decDay(d.review_on));
  if (d.status !== "open") meta.push(d.status);
  head.appendChild(mkEl("span", "dec-meta", meta.join(" · ")));
  card.appendChild(head);
  if (d.decision && d.decision !== d.name) card.appendChild(mkEl("p", "dec-text", d.decision));
  const outcomes = d.outcomes.map((o) => {
    const row = mkEl("div", "dec-out");
    if (o.date) row.appendChild(mkEl("span", "dec-when", decDay(o.date) + (o.status ? ` · ${o.status}` : "")));
    row.appendChild(mkEl("span", "", o.text));
    return row;
  });
  const grid = mkEl("div", "dec-grid");
  grid.append(
    decColumn("Expected", d.expected ? [mkEl("div", "dec-exp", d.expected)] : [], "nothing written"),
    decColumn("Outcomes", outcomes, "none yet"));
  card.appendChild(grid);
  const acts = mkEl("div", "dec-acts");
  const add = mkEl("button", "ctr-act", "add outcome");
  add.type = "button";
  add.addEventListener("click", () => decOutcomeForm(card, d));
  acts.appendChild(add);
  card.appendChild(acts);
  return card;
}

async function renderDecisions(main) {
  const seq = ++decSeq;
  let d;
  try { d = await api("/calendar/decisions?which=open"); } catch (e) { notify(e.message || "couldn't read the decisions"); return; }
  if (seq !== decSeq || calMode !== "decisions") return;
  const panel = mkEl("div", "dec");
  const due = d.decisions.filter((x) => x.due).length;
  panel.appendChild(mkEl("div", "cal-ag-head",
    d.decisions.length ? `${d.decisions.length} open` + (due ? `, ${due} due for review` : "") : "Decisions"));
  if (!d.decisions.length) {
    panel.appendChild(mkEl("p", "dec-empty",
      "No open decisions. A decision you log sits here, what you expected beside what happened: "
      + "type /decide and the decision in the chat to log your first."));
  }
  for (const x of d.decisions) panel.appendChild(decCard(x));
  main.replaceChildren(panel);
}
