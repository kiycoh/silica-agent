// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Alessandro Carosia

// A linked note's first screen, on pointing at its link (ADR-0161). Every surface
// draws a resolved wikilink the server's way, `.note-link[data-path]` (the reader's
// blocks, the chat, Memorize), and the editor's live preview also as
// `a.cm-link-live[data-target]`, so one delegated listener serves them all. The
// note comes from GET /note, the route the drawer reads, once per path for the
// session. A click is the app's own handler's, as before: this only shows.
(() => {
  const LINK = ".note-link[data-path], a.cm-link-live[data-target]";
  const WAIT = 350, GRACE = 160; // the pointer's dwell, and its time to cross into the box
  const cache = new Map(); // key -> Promise<{title, html} | null>
  let pop = null, at = null, openTimer = 0, closeTimer = 0;

  const keyOf = (a) => a.dataset.path || a.dataset.target || "";
  // `path` is in the answer only when the name resolved to one note: a miss and a
  // name two notes share answer 200 with a sentence, and show nothing here.
  const load = (key) => {
    if (!cache.has(key)) {
      cache.set(key, fetch("/note?path=" + encodeURIComponent(key))
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => (d && d.path ? d : null))
        .catch(() => { cache.delete(key); return null; })); // offline: the next point asks again
    }
    return cache.get(key);
  };

  function close() {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    if (at) at.removeAttribute("aria-describedby");
    at = null;
    if (pop) { pop.remove(); pop = null; }
  }

  function place(el, a) {
    const r = a.getBoundingClientRect(), m = 8, gap = 6;
    const w = el.offsetWidth, h = el.offsetHeight;
    const x = Math.min(Math.max(m, r.left), innerWidth - w - m);
    // below the link, or above it when the window ends first and there is room there
    const y = r.bottom + gap + h > innerHeight - m && r.top - gap - h > m ? r.top - gap - h : r.bottom + gap;
    el.style.left = `${Math.round(x)}px`;
    el.style.top = `${Math.round(Math.max(m, y))}px`;
  }

  async function show(a) {
    const d = await load(keyOf(a));
    if (at !== a || !d) return;
    if (!a.isConnected) {
      // The editor redrew its line under a still pointer (a note just opened sets its
      // broken links, a focus change): the same link is the one under the pointer now.
      const b = pt && document.elementFromPoint(pt.x, pt.y)?.closest(LINK);
      if (!b || keyOf(b) !== keyOf(a)) return;
      at = a = b;
    }
    if (pop) pop.remove();
    pop = document.createElement("div");
    pop.className = "hv-pop";
    pop.id = "hv-pop";
    pop.setAttribute("role", "tooltip");
    const head = document.createElement("div");
    head.className = "hv-title";
    head.textContent = d.title || keyOf(a);
    // the chat's prose rules (tables, MathML, code) are scoped to .msg
    const body = document.createElement("div");
    body.className = "msg hv-body";
    body.innerHTML = d.html;
    pop.append(head, body);
    pop.addEventListener("pointerenter", () => clearTimeout(closeTimer));
    pop.addEventListener("pointerleave", (e) => { if (!(e.relatedTarget && at && at.contains(e.relatedTarget))) later(); });
    document.body.append(pop);
    place(pop, a);
    a.setAttribute("aria-describedby", "hv-pop");
    renderMermaid(body); // diagrams and molecules, as the reader's
  }

  function point(a) {
    clearTimeout(closeTimer);
    if (at === a) return;
    close();
    at = a;
    openTimer = setTimeout(() => show(a), WAIT);
  }
  function later() {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(close, GRACE);
  }

  // A pen or a finger has no hover: a press is the click it always was.
  let pt = null; // the pointer's last place, for a link redrawn under it
  document.addEventListener("pointermove", (e) => { pt = { x: e.clientX, y: e.clientY }; }, { passive: true });
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType === "touch") return;
    pt = { x: e.clientX, y: e.clientY };
    const a = e.target.closest && e.target.closest(LINK);
    if (a) point(a);
  });
  document.addEventListener("pointerout", (e) => {
    if (!at || e.pointerType === "touch") return;
    const to = e.relatedTarget;
    if (to && (at.contains(to) || (pop && pop.contains(to)))) return;
    if (e.target.closest && e.target.closest(LINK) === at) later();
  });
  document.addEventListener("focusin", (e) => {
    const a = e.target.closest && e.target.closest(LINK);
    if (a) point(a);
    else if (!(pop && pop.contains(e.target))) close();
  });
  document.addEventListener("focusout", (e) => {
    if (at && e.target === at && !(pop && e.relatedTarget && pop.contains(e.relatedTarget))) later();
  });
  // A press anywhere but the box puts it away: a click on the link opens the note.
  document.addEventListener("pointerdown", (e) => { if (!(pop && pop.contains(e.target))) close(); }, true);
  // Escape takes the box alone, before the app's own Escape closes the drawer under it.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !at) return;
    if (pop) e.stopPropagation(); // a box only waiting to show leaves the Escape to the app
    close();
  }, true);
  document.addEventListener("scroll", (e) => { if (pop && !pop.contains(e.target)) close(); }, true);
})();
