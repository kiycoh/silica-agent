// --- session bootstrap (re-render server-side history; never resets on load) -
async function loadVault() {
  try {
    const r = await fetch("/messages");
    setVaultPath(r.headers.get("X-Silica-Vault") || "");
    setCtxTokens(r.headers.get("X-Silica-Context-Tokens"), r.headers.get("X-Silica-Max-Context-Tokens"),
                 jsonHeader(r, "X-Silica-Context-Parts"), r.headers.get("X-Silica-Compact-At"));
    const msgs = await r.json();
    log.innerHTML = "";
    // One reply is ONE bubble. The model's text arrives split around its own
    // tool calls, so the history holds several assistant messages per turn —
    // rendering each as its own bubble made a single answer read as three
    // separate replies, with the steps between them missing entirely.
    // A reopened chat draws the footer the live one drew (audit P4): the same
    // touched/failed maps runTurn keeps, filled from the stored tool calls,
    // closed at the next user message or at the end of the transcript.
    let turn = null;
    const closeTurn = () => {
      if (!turn) return;
      const f = turnFooter(turn.touched, turn.failed);
      if (f) turn.body.appendChild(f);
      turn = null;
    };
    for (const m of msgs) {
      if (m.role === "user") {
        closeTurn();
        const body = bubble("user");
        body.textContent = m.content;
        addTurnImages(body, m.images || []);
        continue;
      }
      if (!turn) {
        turn = { body: bubble("silica"), raw: [], touched: new Map(), failed: new Map() };
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
      const notes = turnNotes(m.notes);
      if (notes) turn.body.appendChild(notes);
      if (m.tools && m.tools.length) {
        const g = document.createElement("div");
        g.className = "tools";
        for (const t of m.tools) {
          if (!t.summary) {
            // Same row object as a live turn, so a reloaded chat keeps the shape
            // it had while streaming. It opens onto nothing: a stored assistant
            // message holds the call it made, not what came back, and a card
            // that says "no output" for a read that returned a whole note would
            // be inventing an absence.
            const row = makeToolRow(toolLabel(t) + (t.error && t.reason ? " · " + t.reason : ""));
            row.finish(t.error ? "error" : "done");
            g.appendChild(row.el);
            claimRefs(t, turn.touched, turn.failed);
            continue;
          }
          claimRefs(t, turn.touched, turn.failed);
          // The run's outcome, restated from the stored tool result. Without it
          // a reloaded chat could only say the injector had run — not what it
          // wrote, or which chunks died.
          const d = document.createElement("div");
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
          g.appendChild(d);
        }
        turn.body.appendChild(g);
      }
    }
    closeTurn();
    // /changes may have landed first: the cards drawn just now take their
    // tally, their resolved path and their review state from that payload.
    stampWriteTallies(changeRows);
    stampReviewCards();
    following = true; // a fresh transcript opens on its newest row
    followBottom();
    // A drawn diagram is taller than its source, so the bottom moves once more.
    drawDiagrams(log).then(() => followBottom());
  } catch { notify("couldn't load the conversation"); }

}

// --- the composer hint, at the width it actually gets ------------------------
// The placeholder is a name plus a parenthetical key hint, and CSS cannot
// shorten it: Chromium drops text-overflow on a <textarea> placeholder, so the
// ellipsis declared for exactly this in app-chat.css never renders and a squeezed
// field hard-cuts mid-word. At the 900px floor with the work drawer folded open
// the field is 223px wide and the string wants 263px, which showed "⇧⏎ nev".
// So drop the clause, not the characters. Canvas measures the same string the
// field will draw; the 6px margin covers what the 2d context cannot carry from
// the element, which is the variable-width axis Archivo is here for.
const phMeter = document.createElement("canvas").getContext("2d");
function fitPlaceholder(el) {
  const full = el.dataset.phFull || (el.dataset.phFull = el.placeholder);
  const cs = getComputedStyle(el);
  phMeter.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  const room = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const fits = phMeter.measureText(full).width + 6 <= room;
  // strip the trailing parenthetical, keeping the name the field is for
  el.placeholder = fits ? full : full.replace(/\s*\([^()]*\)\s*$/, "");
}
// Changing a placeholder resizes nothing, so observing the field it sits in
// cannot feed itself.
for (const el of [$("#input"), $("#dock-input")]) {
  fitPlaceholder(el);
  new ResizeObserver(() => fitPlaceholder(el)).observe(el);
}

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
// The shared tooltip (audit P9): one element, shown on hover AND on focus for
// any control carrying data-tip, hidden on click. `title` reached only the
// mouse, and a disabled control's reason lived nowhere else.
const tip = document.createElement("div");
tip.id = "tip";
tip.setAttribute("role", "tooltip");
tip.hidden = true;
document.body.appendChild(tip);
let tipFor = null;
function showTip(el) {
  const text = el.dataset.tip;
  if (!text) return;
  tipFor = el;
  tip.textContent = text;
  tip.hidden = false;
  const r = el.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = r.left + r.width / 2 - w / 2, y = r.top - h - 6;
  if (y < 4) y = r.bottom + 6;
  x = Math.max(4, Math.min(x, window.innerWidth - w - 4));
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function hideTip() { tip.hidden = true; tipFor = null; }
// mouseover/focusin bubble; a disabled button fires no mouse events of its
// own, so the hit test walks up from whatever did fire to the nearest tip.
document.addEventListener("mouseover", (e) => { const el = e.target.closest("[data-tip]"); if (el && el !== tipFor) showTip(el); });
document.addEventListener("mouseout", (e) => { if (tipFor && !tipFor.contains(e.relatedTarget)) hideTip(); });
document.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el) showTip(el); });
document.addEventListener("focusout", hideTip);
document.addEventListener("click", hideTip, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });

// `refocus` is false only for the outside click, which is itself a focus
// move the person made; every other close hands focus back to the vault the
// panel came out of (its entry is a row of the vault's panel, ADR-0084).
function closeHelpPanel(refocus = true) {
  if (helpPanel.hidden) return;
  helpPanel.hidden = true;
  helpBtn.setAttribute("aria-expanded", "false");
  if (refocus) vaultOpener().focus();
}
// It takes the vault panel's place, over the same foot: one panel at a time.
function toggleHelpPanel() {
  if (!helpPanel.hidden) { closeHelpPanel(); return; }
  closeVaultPanel(false);
  placeOverVault(helpPanel);
  helpPanel.hidden = false;
  helpBtn.setAttribute("aria-expanded", "true");
}
helpBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleHelpPanel();
});

document.addEventListener("click", (e) => {
  if (!helpPanel.hidden && !e.target.closest("#help-panel") && !e.target.closest("#help-btn"))
    closeHelpPanel(false);
  if (!e.target.closest("#ctx-panel") && !e.target.closest("#ctx-ring")) closeCtxPanel();
  if (!vaultPanel.hidden && !e.target.closest("#vault-panel, #vault-chip, #rail-vault"))
    closeVaultPanel(false);

});

$("#ctx-ring").addEventListener("click", (e) => {
  e.stopPropagation();
  const panel = $("#ctx-panel");
  const opening = panel.hidden;
  if (opening) renderCtxPanel();
  panel.hidden = !opening;
  $("#ctx-ring").setAttribute("aria-expanded", opening ? "true" : "false");
});
// --- dictation: microphone → 16 kHz mono WAV → /stt --------------------------
// Whisper transcribes a clip in one pass, so there is no live partial text to
// stream in and the button has to carry the state on its own: idle, recording,
// transcribing. The WAV conversion happens here rather than on the server
// because MediaRecorder can only produce webm/opus, whisper.cpp's server reads
// WAV unless it was built with ffmpeg, and converting in the browser costs a
// dependency on neither side.
// Three lanes, in order (ADR-0049): the /stt route, which itself prefers a
// served endpoint and falls back to the server's in-process transcriber, and
// last the browser's own recognizer, only when the settings allow it.
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

// One microphone, one take, held here and not in a button's closure: Learn
// rebuilds its answer box and its button on every render, and a take that
// lived in the old ones would finish into a detached textarea.
let micTake = null;

function micBusy(boxId) { return !!micTake && micTake.boxId === boxId; }

function micState(state) {
  if (!micTake) return;
  micTake.state = state;
  micTake.btn.classList.toggle("recording", state === "recording");
  micTake.btn.classList.toggle("busy", state === "busy");
}

function endTake() {
  micState(null);
  micTake = null;
}

// opts.maxMs caps the take. opts.insert(text) replaces the default, which types
// into whichever element carries the box's id when the text arrives.
function attachMic(box, btn, opts = {}) {
  if (micTake && micTake.boxId === box.id) { micTake.btn = btn; micState(micTake.state); }
  btn.addEventListener("click", () => {
    if (micTake) {
      if (micTake.boxId === box.id) micTake.stop(); // the second click ends the take
      else notify("the microphone is already taking dictation in another field", "info");
      return;
    }
    startTake(box, btn, opts);
  });
}

async function startTake(box, btn, opts) {
  // Claimed before the first await, so a double click cannot open two takes.
  const take = micTake = { boxId: box.id, btn, state: null, cancelled: false };
  take.stop = () => { take.cancelled = true; };
  const put = (text) => {
    if (!text) { notify("nothing was transcribed", "info"); return; }
    if (opts.insert) opts.insert(text);
    else insertAtCursor(document.getElementById(box.id) || box, text);
  };
  const avail = await sttAvailable();
  if (take.cancelled) { endTake(); return; }
  hush(); // a question being read aloud would otherwise be dictated back
  if (avail.ok) return recordTake(take, avail, opts, put);
  const lane = browserLane(avail);
  if (lane) return listenTake(take, lane, opts, put);
  endTake();
  showSttPanel(avail.detail || "no transcription endpoint is answering");
}

async function recordTake(take, avail, opts, put) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    // Denied, or no device at all: the browser's to fix, not silica's.
    endTake();
    notify("no microphone: " + String((err && err.message) || err));
    return;
  }
  if (take.cancelled) { stream.getTracks().forEach((t) => t.stop()); endTake(); return; }
  if (avail.via === "local" && avail.cold) {
    notify("loading the speech model; the first dictation on this machine also downloads it, about 480 MB", "info");
  }
  const chunks = [];
  const rec = new MediaRecorder(stream);
  let cap = null;
  take.stop = () => { if (rec.state !== "inactive") rec.stop(); };
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = async () => {
    clearTimeout(cap);
    // Release the device, which is also what drops the browser's own
    // recording indicator — leaving it lit would say silica is still listening.
    stream.getTracks().forEach((t) => t.stop());
    if (!chunks.length) { endTake(); return; }
    micState("busy");
    announce("transcribing");
    try {
      const wav = await toWav16k(new Blob(chunks, { type: chunks[0].type }));
      const form = new FormData();
      form.append("audio", wav, "clip.wav");
      const resp = await fetch("/stt", { method: "POST", body: form });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(body.detail || resp.statusText);
      if (body.via === "local" && sttProbe) sttProbe.cold = false;
      put((body.text || "").trim());
    } catch (err) {
      notify("dictation failed: " + String((err && err.message) || err));
    } finally {
      endTake();
    }
  };
  rec.start();
  micState("recording");
  announce("recording, click again to stop");
  cap = setTimeout(take.stop, opts.maxMs || MIC_MAX_MS);
}

// --- the browser's recognizer (Web Speech API) --------------------------------
// Chrome's recognizer is a Google service unless asked for on-device
// recognition, so this lane runs only when settings say which one is allowed.
// Firefox has neither and the lane simply does not exist there.
function speechLang(lang) { return lang || navigator.language || "en"; }

function browserLane(avail) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR || (avail.browser !== "on-device" && avail.browser !== "cloud")) return null;
  return { SR, local: avail.browser === "on-device", lang: speechLang(avail.lang) };
}

// "" when on-device recognition can run for this language, else why it cannot.
async function onDeviceReady(SR, lang) {
  if (typeof SR.available !== "function") {
    return "this browser has no on-device speech recognition; set browser speech to cloud in settings, or install the [stt] extra";
  }
  const opt = { langs: [lang], processLocally: true };
  try {
    let st = await SR.available(opt);
    if (st === "downloadable" || st === "downloading") {
      notify(`downloading the browser's speech model for ${lang}`, "info");
      st = (await SR.install(opt)) ? "available" : "unavailable";
    }
    return st === "available" ? "" : `on-device speech recognition is ${st} for ${lang} in this browser`;
  } catch (err) {
    return "on-device speech recognition failed: " + String((err && err.message) || err);
  }
}

async function listenTake(take, lane, opts, put) {
  const rec = new lane.SR();
  if (lane.local) {
    const why = await onDeviceReady(lane.SR, lane.lang);
    if (why) { endTake(); showSttPanel(why); return; }
    rec.processLocally = true;
  }
  if (take.cancelled) { endTake(); return; }
  rec.lang = lane.lang;
  // Continuous, because a spoken answer pauses and one phrase is not a take;
  // finals only, so each phrase lands once, at the caret, and never rewrites
  // text the reader may already be correcting.
  rec.continuous = true;
  rec.interimResults = false;
  let heard = false;
  let cap = null;
  take.stop = () => rec.stop();
  rec.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const phrase = e.results[i].isFinal ? e.results[i][0].transcript.trim() : "";
      if (phrase) { heard = true; put(phrase); }
    }
  };
  rec.onerror = (e) => {
    if (e.error === "no-speech" || e.error === "aborted") return; // onend says it
    notify("dictation failed: " + e.error + (e.message ? " · " + e.message : ""));
  };
  rec.onend = () => {
    clearTimeout(cap);
    endTake();
    if (!heard) notify("nothing was transcribed", "info");
  };
  try {
    rec.start();
  } catch (err) {
    endTake();
    notify("dictation failed: " + String((err && err.message) || err));
    return;
  }
  micState("recording");
  announce("listening, click again to stop");
  cap = setTimeout(take.stop, opts.maxMs || MIC_MAX_MS);
}

// --- reading aloud (speechSynthesis) -----------------------------------------
// The browser's own voices, no server. A voice that is not localService is a
// network service (Chrome's "Google" voices send the text to Google), so those
// are chosen only when browser speech is set to cloud: the recognizer's consent.
function hush() { if (window.speechSynthesis) speechSynthesis.cancel(); }

function voicesReady() {
  const now = speechSynthesis.getVoices();
  if (now.length) return Promise.resolve(now);
  // Chrome fills the list asynchronously on first use, and never fires the
  // event on a machine with no voices at all, hence the timeout.
  return new Promise((done) => {
    const t = setTimeout(() => done(speechSynthesis.getVoices()), 1500);
    speechSynthesis.addEventListener("voiceschanged", () => { clearTimeout(t); done(speechSynthesis.getVoices()); }, { once: true });
  });
}

// warm=0: asking which language and which voices never loads the transcriber.
async function speechConf() {
  return fetch("/stt?warm=0").then((r) => r.json()).catch(() => ({}));
}

async function speak(text) {
  if (!text || !window.speechSynthesis) return false;
  const conf = await speechConf();
  const lang = speechLang(conf.lang);
  const base = (tag) => tag.toLowerCase().split(/[-_]/)[0];
  const fits = (await voicesReady()).filter((v) => base(v.lang) === base(lang) && (v.localService || conf.browser === "cloud"));
  if (!fits.length) {
    notify(`no ${conf.browser === "cloud" ? "" : "on-device "}voice for "${lang}" in this browser` +
      (conf.browser === "cloud" ? "" : "; network voices need browser speech set to cloud"), "info");
    return false;
  }
  hush();
  const u = new SpeechSynthesisUtterance(text);
  u.voice = fits.find((v) => v.default) || fits[0];
  u.lang = u.voice.lang;
  speechSynthesis.speak(u);
  return true;
}

document.querySelectorAll(".mic").forEach((b) => {
  const box = document.getElementById(b.dataset.for);
  if (box) attachMic(box, b);
});

