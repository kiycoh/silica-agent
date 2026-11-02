// Pages, a Page and a vault on the public host (ADR-0151 points 6, 7, 8): the one script the host
// serves from /_s/. Everything a page says is in its HTML already; this adds the covers, the
// paper and graphite switch, the catalog's search, sort and area filter, the save chooser that
// remembers a reader's Silica, and the areas' sum. No account can write it, and it reads only
// files the same host serves.
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const fmt = (n) => Number(n).toLocaleString("en-GB");
  const root = document.documentElement;
  root.classList.add("js");

  // ---- paper or graphite: the reader's system decides until they choose -------------------
  const THEME = "silica-pages-theme";
  try { const t = localStorage.getItem(THEME); if (t === "light" || t === "dark") root.dataset.theme = t; } catch { /* storage off: the system decides */ }
  for (const b of $$("[data-theme-btn]")) {
    b.addEventListener("click", () => {
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem(THEME, root.dataset.theme); } catch { /* the choice lasts the visit */ }
      redraw();
    });
  }

  // ---- the printer: the app's drawPubCover, two inks on the well (ADR-0146 point 4) --------
  const css = (name) => getComputedStyle(root).getPropertyValue(name).trim();
  const rgb = (hex) => { const h = hex.replace("#", ""); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); };
  const u32 = (c) => ((255 << 24) | (c[2] << 16) | (c[1] << 8) | c[0]) >>> 0;
  function print(cv, G, opt = {}) {
    const r = cv.getBoundingClientRect(), w = Math.floor(r.width), h = Math.floor(r.height);
    if (w < 40 || h < 40 || !G) return;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const N = G.n, E = G.e, pad = 14;
    const X = (i) => Math.round(pad + N[i][0] * (w - 2 * pad)), Y = (i) => Math.round(pad + N[i][1] * (h - 2 * pad));
    const x = cv.getContext("2d"), img = x.createImageData(w, h), px = new Uint32Array(img.data.buffer);
    const ground = u32(rgb(css("--void"))), ink = u32(rgb(css("--frost"))), sig = u32(rgb(css("--accent")));
    px.fill(ground);
    const put = (a, b, c) => { if (a >= 0 && b >= 0 && a < w && b < h) px[b * w + a] = c; };
    const seg = (x0, y0, x1, y1, c) => { const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1); for (let k = 0; k <= n; k++) put(Math.round(x0 + (x1 - x0) * k / n), Math.round(y0 + (y1 - y0) * k / n), c); };
    const disc = (cx, cy, rr, c) => { for (let a = -rr; a <= rr; a++) for (let b = -rr; b <= rr; b++) if (a * a + b * b <= rr * rr + rr) put(cx + a, cy + b, c); };
    for (const [a, b] of E) seg(X(a), Y(a), X(b), Y(b), ink);
    for (let i = 0; i < N.length; i++) { const rr = N[i][3] >= 12 ? 2 : 1; disc(X(i), Y(i), rr + 1, ground); disc(X(i), Y(i), rr, ink); }
    if (opt.note != null) {
      const i = opt.note;
      for (const [a, b] of E) if (a === i || b === i) seg(X(a), Y(a), X(b), Y(b), sig);
      disc(X(i), Y(i), 4, ground); disc(X(i), Y(i), 3, sig);
    }
    if (opt.area != null) {
      for (const [a, b] of E) if (N[a][2] === opt.area && N[b][2] === opt.area) seg(X(a), Y(a), X(b), Y(b), sig);
      for (let i = 0; i < N.length; i++) if (N[i][2] === opt.area) { disc(X(i), Y(i), 3, ground); disc(X(i), Y(i), 2, sig); }
    }
    x.putImageData(img, 0, 0);
    cv.parentElement.classList.add("printed");
    const svg = $("svg", cv.parentElement);
    if (svg) svg.style.visibility = "hidden";
  }

  const graphs = new Map();
  const load = (url) => {
    if (!graphs.has(url)) graphs.set(url, fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null));
    return graphs.get(url);
  };
  const drawers = [];
  let redrawing = 0;
  function redraw() { cancelAnimationFrame(redrawing); redrawing = requestAnimationFrame(() => drawers.forEach((d) => d())); }
  addEventListener("resize", redraw);
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", redraw);

  // ---- a card's cover, drawn when it comes into view --------------------------------------
  const seen = new IntersectionObserver((items) => {
    for (const it of items) {
      if (!it.isIntersecting) continue;
      seen.unobserve(it.target);
      const cv = $("canvas", it.target);
      load(it.target.dataset.graph).then((G) => { if (!G) return; const d = () => print(cv, G); drawers.push(d); d(); });
    }
  }, { rootMargin: "200px" });
  const watch = (card) => { if (card.dataset.graph) seen.observe(card); };
  $$("a.vcard[data-graph]").forEach(watch);

  // ---- a vault's page ---------------------------------------------------------------------
  const vp = $("section.vp");
  if (vp) {
    const well = $("[data-cover]", vp), cv = $("canvas", well), tip = $(".ctip", well);
    const ext = location.pathname.endsWith(".html") ? ".html" : "";
    let G = null, hover = null, area = null;
    const draw = () => print(cv, G, { note: hover, area });
    load(well.dataset.cover).then((g) => { if (!g) return; G = g; drawers.push(draw); draw(); });
    const pad = 14;
    well.addEventListener("pointermove", (ev) => {
      if (!G) return;
      const r = cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
      let best = -1, bd = 144;
      G.n.forEach((n, i) => { const dx = pad + n[0] * (r.width - 2 * pad) - mx, dy = pad + n[1] * (r.height - 2 * pad) - my, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = i; } });
      hover = best >= 0 ? best : null;
      well.style.cursor = hover == null ? "" : "pointer";
      tip.hidden = hover == null;
      if (hover != null) {
        const n = G.n[hover];
        tip.textContent = n[4];
        tip.style.left = `${pad + n[0] * (r.width - 2 * pad)}px`;
        tip.style.top = `${pad + n[1] * (r.height - 2 * pad)}px`;
        tip.classList.toggle("left", n[0] > 0.62);
      }
      draw();
    });
    well.addEventListener("pointerleave", () => { hover = null; tip.hidden = true; draw(); });
    well.addEventListener("click", () => { if (G && hover != null) location.href = G.n[hover][5] + ext; });

    // the areas: what Save takes, and what they light on the cover
    const boxes = $$(".areas input", vp);
    const sum = $("[data-sum]", vp);
    const sizes = boxes.map((b) => Number(b.closest("li").querySelector(".ct").textContent.replace(/[^0-9]/g, "")));
    const sync = () => { sum.textContent = fmt(boxes.reduce((t, b, i) => t + (b.checked ? sizes[i] : 0), 0)); };
    boxes.forEach((b) => b.addEventListener("change", sync));
    const all = $("[data-all]", vp);
    if (all && !boxes[0]?.disabled) {
      all.hidden = false;
      for (const btn of $$("[data-pick]", all)) btn.addEventListener("click", () => { boxes.forEach((b) => { b.checked = btn.dataset.pick === "all"; }); sync(); });
    }
    for (const li of $$(".areas li", vp)) {
      li.addEventListener("pointerenter", () => { const i = Number(li.dataset.area); area = i >= 0 ? i : null; draw(); });
      li.addEventListener("pointerleave", () => { area = null; draw(); });
    }

    // the author's other vaults, from the Page's own list, so a vault published later shows here
    const more = $("[data-more]", vp);
    load(vp.dataset.vaults).then((list) => {
      if (!Array.isArray(list)) return;
      const n = list.length;
      const of = $("[data-of]", vp);
      if (of && n > 1) of.textContent = `, one of ${n} vaults on their Page`;
      const others = list.filter((v) => v.slug !== vp.dataset.self);
      if (!others.length || !more) return;
      const grid = $(".cat-grid", more);
      const base = vp.dataset.root.endsWith("index.html") ? vp.dataset.root.replace(/index\.html$/, "") : vp.dataset.root;
      for (const v of others) {
        const a = document.createElement("a");
        a.className = "vcard";
        a.href = base + v.slug + (ext ? "/index.html" : "/");
        a.dataset.graph = base + v.slug + "/vault.json";
        a.innerHTML = '<div class="well"><canvas role="img"></canvas></div><div><h2></h2><p class="vline"></p><p class="vmeta"><span><span class="num"></span> notes</span><span><span class="num"></span> areas</span></p></div>';
        $("h2", a).textContent = v.title;
        $(".vline", a).textContent = v.line || "";
        const nums = $$(".vmeta .num", a);
        nums[0].textContent = fmt(v.count.notes); nums[1].textContent = fmt(v.count.areas);
        $("canvas", a).setAttribute("aria-label", `${v.title} as a graph of its notes`);
        grid.append(a);
        watch(a);
      }
      more.hidden = false;
    });
  }

  // ---- save into which Silica: asked once, remembered in this browser (ADR-0151 point 8) ---
  const WHERE = "silica-save-to";
  for (const form of $$("form[data-save]")) {
    const other = $(".other", form), input = $("[data-other]", other);
    other.hidden = false;
    let last = "";
    try { last = localStorage.getItem(WHERE) || ""; } catch { /* nothing remembered */ }
    const known = [...$$("button[formaction]", form)].map((b) => b.getAttribute("formaction"));
    if (last && !known.includes(last)) input.value = last;
    if (last) {
      for (const b of $$("button[formaction]", form)) if (b.getAttribute("formaction") === last) b.classList.replace("btn-o", "btn");
    }
    const prune = () => {
      // every area chosen is the whole vault: the address stays short and says so
      const boxes = $$(`input[name="areas"][form="${form.id}"]`);
      if (boxes.length && boxes.every((b) => b.checked)) boxes.forEach((b) => { b.disabled = true; });
      setTimeout(() => boxes.forEach((b) => { b.disabled = false; }), 0);
    };
    form.addEventListener("submit", (ev) => {
      const to = ev.submitter?.getAttribute("formaction");
      if (to) { try { localStorage.setItem(WHERE, to); } catch { /* not remembered */ } }
      prune();
    });
    $("[data-go]", other).addEventListener("click", () => {
      let to = input.value.trim().replace(/\/+$/, "");
      if (!/^https?:\/\//i.test(to)) to = "https://" + to;
      try { new URL(to); } catch { input.focus(); return; }
      try { localStorage.setItem(WHERE, to + "/"); } catch { /* not remembered */ }
      form.action = to + "/";
      prune();
      form.submit();
    });
  }

  // ---- Pages: search, sort, and the area filter over the cards the host wrote --------------
  const cat = $("[data-catalog]");
  if (cat) {
    const grid = $(".cat-grid", cat), cards = $$("a.vcard", grid), q = $("input[type=search]", cat);
    const count = $("[data-count]", cat), empty = $("[data-empty]", cat), row = $("[data-areas-row]", cat);
    const S = { q: "", sort: "new", area: "", open: false };
    const areasOf = (c) => { try { return JSON.parse(c.dataset.areas || "[]"); } catch { return []; } };
    const meta = cards.map((c) => ({ c, areas: areasOf(c), notes: Number(c.dataset.notes) || 0,
      text: [c.dataset.title, c.dataset.line, c.dataset.who, ...areasOf(c).map((a) => a[0])].join(" ").toLowerCase() }));
    const by = { new: (a, b) => b.c.dataset.date.localeCompare(a.c.dataset.date), size: (a, b) => b.notes - a.notes,
      az: (a, b) => a.c.dataset.title.localeCompare(b.c.dataset.title) };
    function areas() {
      const tally = new Map();
      for (const m of meta) for (const [a, n] of m.areas) tally.set(a, (tally.get(a) || 0) + n);
      const names = [...tally].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map((x) => x[0]);
      const shown = S.open ? names : names.slice(0, 12);
      if (S.area && !shown.includes(S.area)) shown.push(S.area);
      row.replaceChildren();
      const lab = document.createElement("span"); lab.className = "ar-l"; lab.textContent = "Areas"; row.append(lab);
      const chip = (label, value, cls = "ar") => {
        const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label;
        if (value != null) { b.setAttribute("aria-pressed", String(value === S.area)); b.addEventListener("click", () => { S.area = value; render(); }); }
        row.append(b); return b;
      };
      chip("All", "");
      shown.forEach((a) => chip(a, a));
      if (names.length > 12) chip(S.open ? "Fewer" : `${names.length - 12} more`, null, "ar more").addEventListener("click", () => { S.open = !S.open; areas(); });
      row.hidden = names.length === 0;
    }
    function render() {
      const qq = S.q.trim().toLowerCase();
      const list = meta.filter((m) => (!S.area || m.areas.some(([a]) => a === S.area)) && (!qq || m.text.includes(qq)));
      list.sort(by[S.sort]);
      for (const m of meta) m.c.hidden = true;
      for (const m of list) {
        m.c.hidden = false; grid.append(m.c);
        const inArea = $("[data-in]", m.c), hit = m.areas.find(([a]) => a === S.area);
        inArea.innerHTML = S.area && hit ? `<span class="num">${fmt(hit[1])}</span> in ` : `<span class="num">${fmt(m.areas.length)}</span> areas`;
        if (S.area && hit) inArea.append(S.area);
      }
      const notes = list.reduce((t, m) => t + m.notes, 0);
      count.textContent = `${list.length} ${list.length === 1 ? "vault" : "vaults"} · ${fmt(notes)} notes`;
      empty.hidden = list.length > 0;
      empty.textContent = qq ? `No vault matches "${S.q.trim()}"${S.area ? ` in ${S.area}` : ""}. Search reads titles, lines, areas and handles.`
        : `No vault has an area called ${S.area}.`;
      areas();
    }
    $(".cat-tools", cat).hidden = false;
    q.addEventListener("input", () => { S.q = q.value; render(); });
    for (const b of $$("[data-sort] button", cat)) {
      b.addEventListener("click", () => { S.sort = b.dataset.v; $$("[data-sort] button", cat).forEach((x) => x.setAttribute("aria-pressed", String(x === b))); render(); });
    }
    render();
  }
})();
