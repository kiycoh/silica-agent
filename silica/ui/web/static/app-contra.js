// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Alessandro Carosia

// Contradictions set side by side (ADR-0166). The record lives on the note, so
// its band lives in the note's drawer, which every account has: both sides
// with their notes, a click on either opening it at its line, and the four
// ways to settle it. The vault menu counts the open ones and lights SILICA's
// dot, which already means "something here needs your answer".

const CONTRA_SAY = {
  this: "this note holds",
  other: "the other holds",
  both: "both hold when…",
  open: "keep it open",
};
let contraOpen = 0;       // read by syncVaultDot (app-shell.js)
let contraSeq = 0;

// A note named as a link reads as its name: the brackets are the file's syntax.
const contraName = (other) => String(other || "").replace(/^\[\[|\]\]$/g, "");

function contraAge(r) {
  if (r.age == null) return "age unknown";
  return r.age === 0 ? "found today" : `found ${r.age} day${r.age === 1 ? "" : "s"} ago`;
}

// One side of a pair: whose words, the words, and where they stand. A side
// with no line opens its note at the top; a source file outside the vault's
// notes, or a flag, opens nothing and says so by being plain text.
function contraSide(label, words, open, where) {
  const side = mkEl(open ? "button" : "div", "ctr-side");
  // The words are clamped to three lines in a narrow drawer: the whole stays on the tooltip.
  side.title = (words || "") + (open ? (where ? "\n\nopen it at this line" : "\n\nopen it") : "");
  if (open) {
    side.type = "button";
    side.addEventListener("click", open);
  }
  side.append(mkEl("span", "ctr-who", label), mkEl("span", "ctr-words", words || "(no words on record)"));
  if (where) side.appendChild(mkEl("span", "ctr-at", where));
  return side;
}

function contraCard(item, proposed) {
  const card = mkEl("div", "ctr-card" + (proposed ? " ctr-proposed" : ""));
  const flag = !proposed && !item.other;
  const head = mkEl("div", "ctr-head");
  head.appendChild(mkEl("span", "ctr-kind", proposed ? "Proposed by a source check" : flag ? "Flagged" : "Contradiction"));
  if (!proposed) head.appendChild(mkEl("span", "ctr-age", contraAge(item) + (item.how ? " · " + item.how : "")));
  if (!proposed && item.kept?.length) head.appendChild(mkEl("span", "ctr-age", "kept open " + item.kept[item.kept.length - 1]));
  card.appendChild(head);

  const pair = mkEl("div", "ctr-pair");
  const mineLine = proposed ? item.line : item.mine_line;
  pair.appendChild(contraSide("This note", item.mine || "the note as a whole",
    () => { if (drawerNote.editor) drawerNote.editor.revealLine(mineLine || item.line || 1); },
    mineLine ? `line ${mineLine}` : item.line ? `record on line ${item.line}` : ""));
  if (flag) {
    pair.appendChild(contraSide("The flag", item.their, null, item.how));
  } else {
    const otherPath = item.other_path;
    pair.appendChild(contraSide(contraName(item.other), item.their,
      otherPath ? () => openNote(otherPath, item.their_line || undefined) : null,
      otherPath ? (item.their_line ? `line ${item.their_line}` : noteStem(otherPath)) : "a source file"));
  }
  card.appendChild(pair);

  const acts = mkEl("div", "ctr-acts");
  const holds = flag ? ["this", "other", "open"] : ["this", "other", "both", "open"];
  for (const h of holds) {
    const b = mkEl("button", "ctr-act", flag && h === "other" ? "the flag holds" : CONTRA_SAY[h]);
    b.type = "button";
    if (h === "both") b.addEventListener("click", () => contraWhen(card, item, proposed));
    else b.addEventListener("click", () => contraSettle(card, item, proposed, h, ""));
    acts.appendChild(b);
  }
  card.appendChild(acts);
  return card;
}

// "Both hold" asks for the condition first: the person writes it, and the
// same words are appended to both notes.
function contraWhen(card, item, proposed) {
  if (card.querySelector(".ctr-when")) { card.querySelector(".ctr-when input").focus(); return; }
  const row = mkEl("form", "ctr-when");
  row.appendChild(mkEl("span", "ctr-who", "both hold when"));
  const input = mkEl("input");
  input.type = "text";
  input.placeholder = "the condition, in your words";
  input.setAttribute("aria-label", "both hold when");
  const ok = mkEl("button", "ctr-act", "save");
  ok.type = "submit";
  row.append(input, ok);
  row.addEventListener("submit", (e) => {
    e.preventDefault();
    if (input.value.trim()) contraSettle(card, item, proposed, "both", input.value.trim());
    else input.focus();
  });
  card.appendChild(row);
  input.focus();
}

async function contraSettle(card, item, proposed, holds, condition) {
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  // What was typed into the note reaches the disk first, or the settle would
  // be written under it and the next save would meet a 409.
  if (drawerNote.path === item.path && drawerNote.editor) await saveEdit(drawerNote);
  const body = { path: item.path, holds, condition };
  if (proposed) Object.assign(body, { at: item.at, line: item.line });
  else body.ref = item.ref;
  let res;
  try {
    res = await apiPost("/contradictions/resolve", body);
  } catch (e) {
    notify(e.message || "couldn't settle it");
    card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    return;
  }
  const said = holds === "open" ? "Kept open, dated in the note." : `Settled: ${CONTRA_SAY[holds].replace("…", "")}.`;
  notify(said + (res.paths.length > 1 ? ` ${res.paths.length} notes written.` : ""), "info", res.run ? {
    label: "undo",
    run: async () => { if (await undoTreeRun(res.run)) contraAfter(); },
  } : null);
  contraAfter();
}

function contraAfter() {
  refreshOpenNote();
  contraBand(drawerNote.path);
  contraCount();
}

async function contraBand(path) {
  const box = $("#note-contra");
  // Another note's pairs never stand over this one while its own load.
  if (!path || box.dataset.for !== path) box.hidden = true;
  if (!path) return;
  box.dataset.for = path;
  const seq = ++contraSeq;
  let d;
  try {
    d = await api("/contradictions/note?path=" + encodeURIComponent(path));
  } catch { d = null; } // a band that cannot load is no band: the note itself still reads
  if (seq !== contraSeq) return;
  const records = d?.records || [], proposals = d?.proposals || [];
  box.replaceChildren();
  box.hidden = !records.length && !proposals.length;
  if (box.hidden) return;
  for (const r of records) box.appendChild(contraCard(r, false));
  for (const p of proposals) box.appendChild(contraCard(p, true));
}

// The vault menu's count, and the dot on SILICA it lights.
async function contraCount() {
  let d;
  try { d = await api("/contradictions"); } catch { return; } // the count waits for the next refresh
  contraOpen = d.open.length;
  const box = $("#vault-contra");
  box.replaceChildren();
  box.hidden = !d.open.length && !d.proposed.length;
  if (!box.hidden) {
    const n = d.open.length, p = d.proposed.length;
    box.appendChild(mkEl("div", "vc-head",
      (n ? `${n} open contradiction${n === 1 ? "" : "s"}` : "No open contradiction")
      + (p ? ` · ${p} proposed` : "")));
    const rows = [...d.open.map((r) => [r, false]), ...d.proposed.map((r) => [r, true])];
    for (const [r, proposed] of rows.slice(0, 8)) {
      const b = mkEl("button", "vc-row");
      b.type = "button";
      b.append(mkEl("span", "vc-note", noteStem(r.path)),
        mkEl("span", "vc-meta", (proposed ? "proposed · " : "") + "against " + (contraName(r.other) || "a flag")
          + (proposed ? "" : " · " + contraAge(r))));
      b.title = r.path;
      b.addEventListener("click", () => { closeVaultPanel(false); openNote(r.path, proposed ? r.line : r.line || undefined); });
      box.appendChild(b);
    }
    if (rows.length > 8) box.appendChild(mkEl("div", "vc-more", `and ${rows.length - 8} more: /contested and /contradictions list them all`));
  }
  syncVaultDot();
}

document.addEventListener("silica:node", (e) => {
  const path = e.detail?.node?.path;
  // Only the note the drawer holds: a graph node pointed at has no text to set beside.
  if (path && path === drawerNote.path && drawerMode === "note") contraBand(path);
});
document.addEventListener("silica:vault-info", () => contraCount());
contraCount();
