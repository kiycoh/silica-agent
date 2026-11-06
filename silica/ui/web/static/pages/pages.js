// Pages, a Page and a vault on the public host (ADR-0151 points 6, 7, 8): the one script the host
// serves from /_s/. Everything a page says is in its HTML already; this adds the covers, the
// paper and graphite switch, the catalog's search, sort and area filter, and the room a vault's
// page and a note's open as, with its selection and the question of which Silica saves it (ADR-0151,
// amendment of 2026-11-06). No account can write it, and it reads only files the same host serves.
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  // one Public vault is one map to open (amendment of 2026-11-06, item 3); replace, so Back leaves
  const one = $("[data-one]");
  if (one) { location.replace(one.dataset.one); return; }
  const fmt = (n) => Number(n).toLocaleString("en-GB");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const press = (cls, text, act) => { const b = el("button", cls, text); b.type = "button"; b.addEventListener("click", act); return b; };
  const root = document.documentElement;
  root.classList.add("js");
  // Read while the script runs: a deferred script's currentScript is gone in any later callback.
  const here = document.currentScript ? document.currentScript.src : "";

  // pages-pure:start
  const SLUG = /^[a-z0-9-]+$/;
  // The address Save opens. Whole areas travel as numbers and the rest by name, and everything chosen
  // is the plain vault. A Silica older than `notes` reads `areas` alone: with no whole area it would
  // offer the whole vault, so -2, an area nothing is in, makes it offer nothing instead.
  function saveQuery(url, rows, picks) {
    const base = `?save=${encodeURIComponent(url)}`;
    if (!picks.size || picks.size === rows.length) return base;
    const areas = new Map();
    for (const r of rows) { if (!areas.has(r.area)) areas.set(r.area, []); areas.get(r.area).push(r.slug); }
    const whole = [], rest = new Set(picks);
    for (const [a, slugs] of areas) if (slugs.every((s) => rest.has(s))) { whole.push(a); slugs.forEach((s) => rest.delete(s)); }
    if (!rest.size) return `${base}&areas=${whole.join(",")}`;
    return `${base}&areas=${(whole.length ? whole : [-2]).join(",")}&notes=${[...rest].join(",")}`;
  }
  // A note's markdown as a reader reads it: no links, marks or table bars.
  const plain = (s) => s.replace(/<[^>]+>/g, "").replace(/!\[[^\]]*\]\([^)]*\)/g, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`>#]+/g, " ").replace(/^\s*(\d+\.|-)\s+/, "").replace(/\s*\|\s*/g, " · ").replace(/^[\s·]+|[\s·]+$/g, "").replace(/\s+/g, " ");
  // The line a note's link to `slug` is written on, centred on the link when it is long; "" with none.
  // `slug` has passed SLUG, so it is safe in a pattern. A published link is the note's address, or
  // <slug>.html in a folder built without a host.
  function writtenLine(text, slug) {
    const link = new RegExp(`\\[([^\\]]*)\\]\\((?:[^)]*/)?${slug}(?:\\.html)?\\)`);
    for (const raw of text.split("\n")) {
      const hit = link.exec(raw);
      if (!hit) continue;
      const line = plain(raw), at = Math.max(0, line.indexOf(hit[1]));
      if (line.length <= 230) return line;
      const a = Math.max(0, at - 120);
      return `${a ? "…" : ""}${line.slice(a, a + 230)}${a + 230 < line.length ? "…" : ""}`;
    }
    return "";
  }
  // pages-pure:end

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
  function print(cv, G) {
    const r = cv.getBoundingClientRect(), w = Math.floor(r.width), h = Math.floor(r.height);
    if (w < 40 || h < 40 || !G) return;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const N = G.n, E = G.e, pad = 14;
    const X = (i) => Math.round(pad + N[i][0] * (w - 2 * pad)), Y = (i) => Math.round(pad + N[i][1] * (h - 2 * pad));
    const x = cv.getContext("2d"), img = x.createImageData(w, h), px = new Uint32Array(img.data.buffer);
    const ground = u32(rgb(css("--void"))), ink = u32(rgb(css("--frost")));
    px.fill(ground);
    const put = (a, b, c) => { if (a >= 0 && b >= 0 && a < w && b < h) px[b * w + a] = c; };
    const seg = (x0, y0, x1, y1, c) => { const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1); for (let k = 0; k <= n; k++) put(Math.round(x0 + (x1 - x0) * k / n), Math.round(y0 + (y1 - y0) * k / n), c); };
    const disc = (cx, cy, rr, c) => { for (let a = -rr; a <= rr; a++) for (let b = -rr; b <= rr; b++) if (a * a + b * b <= rr * rr + rr) put(cx + a, cy + b, c); };
    for (const [a, b] of E) seg(X(a), Y(a), X(b), Y(b), ink);
    for (let i = 0; i < N.length; i++) { const rr = N[i][3] >= 12 ? 2 : 1; disc(X(i), Y(i), rr + 1, ground); disc(X(i), Y(i), rr, ink); }
    x.putImageData(img, 0, 0);
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

  // ---- a vault's room: the app's rail, view and drawer, read only --------------------------
  // (ADR-0151, amendment of 2026-11-06.) The page's HTML stays what a reader without the script
  // has; this reads vault.json, hides it and walks the same notes. A vault.json that does not
  // come leaves the page as it is.
  const stage = $("main[data-room]");
  if (stage) load(stage.dataset.room).then((m) => { if (m && Array.isArray(m.n) && Array.isArray(m.unlinked)) walk(m, stage); });

  function walk(m, main) {
    const { here, ext = "", note: first } = main.dataset;
    // an account writes its manifest: a slug that is not one could open another account's page
    if (![...m.n.map((r) => r[5]), ...m.unlinked.map((r) => r[1])].every((s) => SLUG.test(s))) return;
    const N = m.n.map((r, i) => ({ i, x: r[0], y: r[1], px: r[0], py: r[1], area: r[2], deg: r[3], title: r[4], slug: r[5] }));
    for (const u of m.unlinked) N.push({ i: N.length, area: -1, deg: 0, title: u[0], slug: u[1], loose: true });
    const by = new Map(N.map((n) => [n.slug, n]));
    if (first && !by.has(first)) return;
    const drawn = N.filter((n) => !n.loose);
    const adj = N.map(() => []);
    m.e.forEach(([a, b]) => { adj[a].push(b); adj[b].push(a); });
    const ranked = [...drawn].sort((a, b) => b.deg - a.deg || a.title.localeCompare(b.title));
    // Key is ADR-0152's most linked tenth: links at or past the 90th percentile
    const keyLinks = ranked.length ? ranked[Math.floor(ranked.length * .1)].deg : 0;
    const count = (n) => `${fmt(n)} ${n === 1 ? "note" : "notes"}`;
    const areaName = (i) => (i >= 0 ? m.areas[i][0] : "In no area");
    const up = here ? here.replace(/[^/]+\/$/, "") : "../";
    const pageUrl = up + (ext ? "index.html" : "");
    const noteUrl = (slug) => `${here}${slug}${ext}`;
    const vaultUrl = here || "index.html", vaultTitle = `${m.title} · @${m.handle}`;
    const vaultPath = new URL(here || ".", location.href).pathname;
    const noteAt = (href) => {
      const u = new URL(href, location.href);
      if (u.origin !== location.origin || !u.pathname.startsWith(vaultPath)) return null;
      return by.get(u.pathname.slice(vaultPath.length).replace(/\.html$/, "")) || null;
    };
    // the two Silicas the page's own form names; a page built without a host has no form, and no Save
    const silicas = $$("button[formaction]", main).map((b) => [b.getAttribute("formaction"), b.textContent]);
    const can = silicas.length > 0;
    const S = { note: null, area: null, areaHover: null, hover: null, link: null, arr: "map", names: "auto",
      open: new Set(), pick: false, review: false };

    // Save leaves the page in this tab, so the selection is kept for the way back: closing the tab loses it
    const PICKS = `silica-pick:${m.handle}/${m.slug}`, REMEMBER = "silica-save-remember";
    const chosen = new Set();
    try { for (const s of JSON.parse(sessionStorage.getItem(PICKS) || "[]")) if (by.has(s)) chosen.add(by.get(s).i); } catch { /* nothing kept */ }
    const keepPicks = () => { try { sessionStorage.setItem(PICKS, JSON.stringify([...chosen].map((i) => N[i].slug))); } catch { /* it lasts the visit */ } };
    let remember = "";
    try { const r = localStorage.getItem(REMEMBER) || ""; if (/^https?:\/\//i.test(r)) remember = r; } catch { /* the page asks */ }

    const room = el("div", "room");
    room.innerHTML = `<aside class="rail" aria-label="This wiki">
      <div class="rs"><input type="search" name="q" placeholder="Search this wiki" aria-label="Search this wiki" autocomplete="off"><kbd>/</kbd><div class="res" hidden></div></div>
      <div class="rc"></div><div class="rf"></div></aside>
      <section class="view" aria-label="Map of the wiki"><canvas class="map" role="img"></canvas><canvas class="names" aria-hidden="true"></canvas>
      <div class="mb"><div class="seg" data-arr role="group" aria-label="Arrangement"><button type="button" data-v="map" aria-pressed="true">Map</button><button type="button" data-v="clusters" aria-pressed="false">Clusters</button></div>
      <span class="lab nm-seg">Names</span><div class="seg nm-seg" data-nms role="group" aria-label="Names"><button type="button" data-v="auto" aria-pressed="true">Auto</button><button type="button" data-v="off" aria-pressed="false">Off</button><button type="button" data-v="key" aria-pressed="false">Key</button><button type="button" data-v="all" aria-pressed="false">All</button></div>
      <button type="button" class="btn-o pickb" aria-pressed="false">Choose</button></div>
      <p class="hint" hidden>Click a note to choose it, or draw around a group. Esc ends.</p>
      <div class="ctip" hidden></div><div class="ltag" hidden></div><p class="cap"></p>
      <div class="zoom"><button type="button" aria-label="Zoom in">+</button><button type="button" aria-label="Zoom out">&#8722;</button><button type="button" aria-label="Fit the map">&#9633;</button></div></section>
      <article class="drawer" tabindex="-1" hidden></article><dialog class="svc" aria-label="Save into which Silica?"></dialog>`;
    main.hidden = true;
    main.after(room);
    document.body.classList.add("walking");
    const cv = $(".map", room), nc = $(".names", room), view = $(".view", room), drawer = $(".drawer", room);
    const rc = $(".rc", room), rf = $(".rf", room), tip = $(".ctip", room), ltag = $(".ltag", room), sheet = $("dialog", room);
    const q = $(".rs input", room), res = $(".res", room), pickBtn = $(".pickb", room), hint = $(".hint", room);
    const ctx = cv.getContext("2d"), nx = nc.getContext("2d");
    const phone = matchMedia("(max-width: 900px)"), still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    pickBtn.hidden = !can;
    cv.setAttribute("aria-label", `${m.title} as a graph of its notes. Every note is also listed under Areas.`);
    $(".cap", room).textContent = `${fmt(m.count.drawn)} of ${fmt(m.count.notes)} notes drawn · ${fmt(m.count.links)} links`;

    // ---- the printer: the cover's two inks, with a camera and clipped lines -----------------
    const PAD = 40, TOP = 46;
    let cam = { x: 0, y: 0, k: 1 }, W = 40, H = 40, img = null, px = null, INK = {}, lasso = null;
    const inks = () => {
      const g = (n) => u32(rgb(css(n)));
      INK = { ground: g("--void"), node: g("--frost"), link: g("--spot"), linkDim: g("--spot-dim"), nodeDim: g("--node-dim"), sig: g("--accent") };
    };
    const X = (n) => cam.x + (PAD + n.px * (W - 2 * PAD)) * cam.k;
    const Y = (n) => cam.y + (TOP + n.py * (H - TOP - PAD)) * cam.k;
    const put = (a, b, c) => { if (a >= 0 && b >= 0 && a < W && b < H) px[b * W + a] = c; };
    function seg(x0, y0, x1, y1, c) {
      let t0 = 0, t1 = 1; const dx = x1 - x0, dy = y1 - y0;
      const P = [-dx, dx, -dy, dy], Q = [x0, W - 1 - x0, y0, H - 1 - y0];
      for (let i = 0; i < 4; i++) {
        if (P[i] === 0) { if (Q[i] < 0) return; continue; }
        const r = Q[i] / P[i];
        if (P[i] < 0) { if (r > t1) return; if (r > t0) t0 = r; } else { if (r < t0) return; if (r < t1) t1 = r; }
      }
      const ax = x0 + t0 * dx, ay = y0 + t0 * dy, bx = x0 + t1 * dx, by2 = y0 + t1 * dy;
      const n = Math.ceil(Math.max(Math.abs(bx - ax), Math.abs(by2 - ay))) || 1;
      for (let k = 0; k <= n; k++) put(Math.round(ax + (bx - ax) * k / n), Math.round(ay + (by2 - ay) * k / n), c);
    }
    const disc = (cx, cy, rr, c) => { for (let a = -rr; a <= rr; a++) for (let b = -rr; b <= rr; b++) if (a * a + b * b <= rr * rr + rr) put(cx + a, cy + b, c); };
    // ADR-0152's size in pixels at the fit (2 links 3.4 px, 51 links 10 px), past it the root of the zoom;
    // a phone's map is a third as wide and its notes ran together at those sizes
    const rad = (n) => Math.max(1, Math.round((phone.matches ? .43 : .71) * Math.sqrt(16 + 3.5 * n.deg) * Math.sqrt(Math.max(cam.k, 1))));
    let queued = 0;
    const paint = () => { if (!queued) queued = requestAnimationFrame(() => { queued = 0; draw(); }); };
    function draw() {
      if (!img) return;
      const held = S.hover ?? S.note, foc = held != null && !N[held].loose ? held : null, ar = S.areaHover ?? S.area;
      const near = foc != null ? new Set(adj[foc]) : null, quiet = foc != null || ar != null;
      const lit = (a, b) => (foc != null ? a === foc || b === foc : ar != null ? N[a].area === ar && N[b].area === ar : false);
      px.fill(INK.ground);
      const later = [];
      m.e.forEach(([a, b], k) => {
        if (lit(a, b) || k === S.link || (chosen.has(a) && chosen.has(b))) { later.push(k); return; }
        seg(X(N[a]), Y(N[a]), X(N[b]), Y(N[b]), quiet ? INK.linkDim : INK.link);
      });
      // teal on the map says "chosen": an area in focus keeps the plain ink, or it reads as picked
      for (const k of later) { const [a, b] = m.e[k]; seg(X(N[a]), Y(N[a]), X(N[b]), Y(N[b]), foc == null && k !== S.link && !(chosen.has(a) && chosen.has(b)) ? INK.node : INK.sig); }
      for (const n of drawn) {
        const x = Math.round(X(n)), y = Math.round(Y(n)), r = rad(n) + (chosen.has(n.i) ? 1 : 0);
        if (x < -40 || y < -40 || x > W + 40 || y > H + 40) continue;
        const on = foc != null ? n.i === foc || near.has(n.i) : ar != null ? n.area === ar : true;
        disc(x, y, r + 1, INK.ground);
        disc(x, y, r, chosen.has(n.i) ? INK.sig : on ? INK.node : INK.nodeDim);
      }
      for (const i of [S.note, S.hover]) {
        if (i == null || N[i].loose) continue;
        const n = N[i], x = Math.round(X(n)), y = Math.round(Y(n)), r = rad(n) + 1;
        disc(x, y, r + 3, INK.sig); disc(x, y, r + 2, INK.ground); disc(x, y, r, INK.sig);
      }
      if (S.link != null) for (const i of m.e[S.link]) { const n = N[i]; disc(Math.round(X(n)), Math.round(Y(n)), rad(n) + 1, INK.sig); }
      if (lasso) for (let i = 1; i < lasso.length; i++) seg(lasso[i - 1][0], lasso[i - 1][1], lasso[i][0], lasso[i][1], INK.sig);
      ctx.putImageData(img, 0, 0);
      names();
    }

    // ---- names, ADR-0152's modes: Auto climbs with the zoom, Key the most linked tenth, All what fits
    const widths = new Map();
    const drawerW = () => Math.min(540, Math.round(W * .52));
    const visW = () => ((S.note != null || S.review) && !phone.matches ? W - drawerW() - 12 : W);
    function names() {
      const d = devicePixelRatio || 1;
      nx.setTransform(d, 0, 0, d, 0, 0); nx.clearRect(0, 0, W, H);
      const placed = [], ground = css("--void"), frost = css("--frost"), lim = visW();
      const fits = (r) => { for (const p of placed) if (r[0] < p[2] && r[2] > p[0] && r[1] < p[3] && r[3] > p[1]) return false; return true; };
      const label = (txt, x, y, font, color, center) => {
        nx.font = font;
        const key = font + txt; let w = widths.get(key); if (w == null) { w = nx.measureText(txt).width; widths.set(key, w); }
        const left = center ? x - w / 2 : x + w + 8 > lim ? x - w - 12 : x;
        const r = [left - 3, y - 9, left + w + 3, y + 9];
        if (r[0] < 2 || r[1] < TOP || r[2] > lim - 2 || r[3] > H - 2 || !fits(r)) return;
        placed.push(r);
        nx.fillStyle = ground; nx.fillRect(r[0], r[1] + 1, r[2] - r[0], r[3] - r[1] - 2);
        nx.fillStyle = color; nx.textBaseline = "middle"; nx.fillText(txt, left, y + .5);
      };
      const noteFont = '500 12px "Archivo", sans-serif', areaFont = '650 13.5px "Archivo", sans-serif';
      if (S.names !== "off" && S.note != null && !N[S.note].loose) {
        const n = N[S.note];
        label(n.title, X(n) + rad(n) + 8, Y(n), areaFont, css("--accent"));
        for (const i of [...adj[S.note]].sort((a, b) => N[b].deg - N[a].deg)) { const o = N[i]; label(o.title, X(o) + rad(o) + 5, Y(o), noteFont, frost); }
      }
      const mode = S.names === "auto" ? (cam.k < 1.35 ? "areas" : cam.k < 2.6 ? "key" : "all") : S.names;
      // a Clusters head stays at Off; the map's area heads are Auto's alone (ADR-0152)
      if (mode === "areas" || S.arr === "clusters") {
        const sum = m.areas.map(() => [0, 0, 0]);
        for (const n of drawn) if (n.area >= 0) { const s = sum[n.area]; s[0] += X(n); s[1] += Y(n); s[2]++; }
        m.areas.slice(0, S.arr === "clusters" ? 60 : 12).forEach(([name, size], i) => { const s = sum[i]; if (s[2]) label(`${name} ${fmt(size)}`, s[0] / s[2], s[1] / s[2], areaFont, frost, true); });
      }
      if (mode === "areas" || mode === "off") return;
      for (const n of ranked) {
        if (mode === "key" && n.deg < keyLinks) continue;
        const x = X(n), y = Y(n);
        if (x > -40 && y > 0 && x < lim && y < H) label(n.title, x + rad(n) + 5, y, noteFont, frost);
      }
    }

    // ---- arrangements: positions settle once, notes never move on their own -------------------
    function targets() {
      const T = new Map(drawn.map((n) => [n.i, [n.x, n.y]]));
      if (S.arr === "clusters") {
        // one ring, clockwise from the top, the biggest first (ADR-0152)
        const asp = Math.min(1, H / W), A = m.areas.length, roots = m.areas.reduce((t, a) => t + Math.sqrt(a[1]), 0);
        const s = Math.min(.021, 2.3 / (2 * roots)), seen = m.areas.map(() => 0);
        const c = m.areas.map((_, i) => { const t = -Math.PI / 2 + i / A * 2 * Math.PI; return [.5 + .4 * Math.cos(t) * .9, .5 + .4 * Math.sin(t)]; });
        for (const n of ranked) {
          if (n.area < 0) continue;
          const j = seen[n.area]++, r = s * Math.sqrt(j), t = j * 2.39996;
          T.set(n.i, [c[n.area][0] + r * Math.cos(t) * asp, c[n.area][1] + r * Math.sin(t)]);
        }
      }
      return T;
    }
    let tripping = 0, gliding = 0;
    function arrange(instant) {
      const to = targets();
      cancelAnimationFrame(tripping);
      if (instant || still) { for (const n of drawn) [n.px, n.py] = to.get(n.i); paint(); return; }
      const from = new Map(drawn.map((n) => [n.i, [n.px, n.py]])), t0 = performance.now(), T = 700;
      const step = (t) => {
        const u = Math.min(1, (t - t0) / T), e = 1 - Math.pow(1 - u, 3);
        for (const n of drawn) { const a = from.get(n.i), b = to.get(n.i); n.px = a[0] + (b[0] - a[0]) * e; n.py = a[1] + (b[1] - a[1]) * e; }
        draw();
        if (u < 1) tripping = requestAnimationFrame(step);
      };
      tripping = requestAnimationFrame(step);
    }
    function glide(to) {
      cancelAnimationFrame(gliding);
      if (still) { cam = to; paint(); return; }
      const a = { ...cam }, t0 = performance.now(), T = 320;
      const step = (t) => {
        const u = Math.min(1, (t - t0) / T), e = 1 - Math.pow(1 - u, 3);
        cam = { x: a.x + (to.x - a.x) * e, y: a.y + (to.y - a.y) * e, k: a.k + (to.k - a.k) * e };
        draw();
        if (u < 1) gliding = requestAnimationFrame(step);
      };
      gliding = requestAnimationFrame(step);
    }
    // where the map stood before the drawer pushed it aside, until the reader moves it themselves
    let camBack = null;
    function reveal(n) {
      // the map moves only when the note would end up out of sight: it never re-centres for its own sake
      if (n.loose) return;
      const lim = visW(), x = X(n), y = Y(n), outX = x <= 40 || x >= lim - 40, outY = y <= TOP || y >= H - 30;
      if (!outX && !outY) return;
      if (!camBack) camBack = { ...cam };
      glide({ k: cam.k, x: outX ? cam.x + lim / 2 - x : cam.x, y: outY ? cam.y + H / 2 - y : cam.y });
    }
    function frameArea(i) {
      const a = drawn.filter((n) => n.area === i); if (!a.length) return;
      const lim = visW(), bx = (n) => PAD + n.px * (W - 2 * PAD), by2 = (n) => TOP + n.py * (H - TOP - PAD);
      const x0 = Math.min(...a.map(bx)), x1 = Math.max(...a.map(bx)), y0 = Math.min(...a.map(by2)), y1 = Math.max(...a.map(by2));
      const k = Math.max(.8, Math.min(5, (lim - 120) / Math.max(x1 - x0, 40), (H - 150) / Math.max(y1 - y0, 40)));
      glide({ k, x: lim / 2 - k * (x0 + x1) / 2, y: (H + TOP) / 2 - k * (y0 + y1) / 2 });
    }
    function fit() {
      const k = visW() / W;
      glide({ k, x: 0, y: (H - H * k) / 2 });
    }
    function zoomAt(mx, my, f) {
      camBack = null;
      const k = Math.max(.55, Math.min(14, cam.k * f)), r = k / cam.k;
      cam = { k, x: mx - (mx - cam.x) * r, y: my - (my - cam.y) * r };
      tipAt(S.hover); paint();
    }

    // ---- pointing ---------------------------------------------------------------------------
    function pick(mx, my) {
      let best = null, bd = 144;
      for (const n of drawn) { const dx = X(n) - mx, dy = Y(n) - my, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = n.i; } }
      return best;
    }
    function pickLink(mx, my) {
      let best = null, bd = 25;
      const check = (k, lim) => {
        const [a, b] = m.e[k], ax = X(N[a]), ay = Y(N[a]), bx = X(N[b]), by2 = Y(N[b]);
        const dx = bx - ax, dy = by2 - ay, t = Math.max(0, Math.min(1, ((mx - ax) * dx + (my - ay) * dy) / (dx * dx + dy * dy || 1)));
        const ex = ax + t * dx - mx, ey = ay + t * dy - my, d = ex * ex + ey * ey;
        if (d < lim) { bd = d; best = k; }
      };
      m.e.forEach(([a, b], k) => { if (S.note != null && (a === S.note || b === S.note)) check(k, Math.max(bd, 81)); });
      if (best == null) m.e.forEach((_, k) => check(k, bd));
      return best;
    }
    function tipAt(i) {
      tip.hidden = i == null || N[i].loose;
      if (tip.hidden) return;
      const n = N[i], x = X(n), y = Y(n);
      tip.textContent = n.title; tip.style.left = `${x}px`; tip.style.top = `${y}px`;
      tip.classList.toggle("left", x > visW() * .68);
    }
    let drag = null, pinch = 0;
    const touches = new Map();
    const choosing = (ev) => can && (S.pick || ev.shiftKey);
    const inside = (x, y, P) => { let on = false; for (let i = 0, j = P.length - 1; i < P.length; j = i++) if ((P[i][1] > y) !== (P[j][1] > y) && x < (P[j][0] - P[i][0]) * (y - P[i][1]) / (P[j][1] - P[i][1]) + P[i][0]) on = !on; return on; };
    cv.addEventListener("pointerdown", (ev) => {
      cv.setPointerCapture(ev.pointerId); touches.set(ev.pointerId, [ev.offsetX, ev.offsetY]);
      if (choosing(ev)) { lasso = [[ev.offsetX, ev.offsetY]]; return; }
      drag = { x: ev.clientX, y: ev.clientY, cx: cam.x, cy: cam.y, moved: false };
      cancelAnimationFrame(gliding);
    });
    cv.addEventListener("pointermove", (ev) => {
      if (touches.has(ev.pointerId)) touches.set(ev.pointerId, [ev.offsetX, ev.offsetY]);
      if (lasso) { const l = lasso[lasso.length - 1]; if (Math.hypot(ev.offsetX - l[0], ev.offsetY - l[1]) > 3) { lasso.push([ev.offsetX, ev.offsetY]); paint(); } return; }
      if (touches.size === 2) {
        const [a, b] = [...touches.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (pinch) zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, d / pinch);
        pinch = d; if (drag) drag.moved = true; return;
      }
      if (drag) {
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        if (drag.moved) { camBack = null; cam.x = drag.cx + dx; cam.y = drag.cy + dy; cv.style.cursor = "grabbing"; tipAt(null); paint(); }
        return;
      }
      const h = pick(ev.offsetX, ev.offsetY);
      if (h !== S.hover) { S.hover = h; tipAt(h); paint(); }
      cv.style.cursor = choosing(ev) ? "crosshair" : h == null ? "grab" : "pointer";
    });
    cv.addEventListener("pointerup", (ev) => {
      touches.delete(ev.pointerId); pinch = 0;
      if (lasso) {
        const P = lasso; lasso = null;
        if (P.length > 6) { for (const n of drawn) if (inside(X(n), Y(n), P)) chosen.add(n.i); picksChanged(); return; }
        const h = pick(ev.offsetX, ev.offsetY);
        if (h != null) toggle(h); else paint();
        return;
      }
      const wasDrag = drag && drag.moved; drag = null; cv.style.cursor = "grab";
      if (wasDrag) return;
      const h = pick(ev.offsetX, ev.offsetY);
      if (h != null) { S.hover = null; tipAt(null); openNote(N[h].slug); return; }
      S.link = pickLink(ev.offsetX, ev.offsetY); showLink(S.link, ev.offsetX, ev.offsetY); paint();
    });
    cv.addEventListener("pointerleave", () => { if (S.hover != null && !drag) { S.hover = null; tipAt(null); paint(); } });
    cv.addEventListener("wheel", (ev) => { ev.preventDefault(); zoomAt(ev.offsetX, ev.offsetY, Math.exp(-ev.deltaY * .0016)); }, { passive: false });
    const [zi, zo, zf] = $$(".zoom button", room);
    zi.addEventListener("click", () => zoomAt(W / 2, H / 2, 1.4));
    zo.addEventListener("click", () => zoomAt(W / 2, H / 2, 1 / 1.4));
    zf.addEventListener("click", fit);
    const pressed = (seg2, v) => { for (const b of $$("button", seg2)) b.setAttribute("aria-pressed", String(b.dataset.v === v)); };
    for (const b of $$("[data-arr] button", room)) b.addEventListener("click", () => { S.arr = b.dataset.v; pressed(b.parentElement, S.arr); arrange(); });
    for (const b of $$("[data-nms] button", room)) b.addEventListener("click", () => { S.names = b.dataset.v; pressed(b.parentElement, S.names); paint(); });
    function setPick(on) {
      S.pick = on; pickBtn.setAttribute("aria-pressed", String(on)); hint.hidden = !on; cv.style.cursor = on ? "crosshair" : "grab";
    }
    pickBtn.addEventListener("click", () => setPick(!S.pick));

    // ---- the file every note's words are in, read at the first search or clicked link ---------
    let md = null;
    const plainText = new Map();
    const readNotes = () => load(`${here}notes.json`).then((d) => { md = d && typeof d === "object" ? d : {}; });
    const words = (slug) => {
      if (!plainText.has(slug)) plainText.set(slug, typeof md[slug] === "string" ? plain(md[slug].replace(/^---\n[\s\S]*?\n---\n/, "")) : "");
      return plainText.get(slug);
    };

    // ---- a clicked link says where it is written; the host has no GET /link, notes.json does ----
    async function showLink(k, mx, my) {
      ltag.hidden = k == null;
      if (k == null) return;
      const go = (n) => press("linkb", n.title, () => openNote(n.slug));
      const write = (from, to, line, says) => {
        const lk = el("div", "lk");
        lk.append(go(from), el("span", "", line ? "links to" : "and"), go(to));
        ltag.replaceChildren(lk);
        if (line) ltag.append(el("p", "", line));
        ltag.append(el("p", "kd", says));
      };
      const [a, b] = m.e[k];
      write(N[a], N[b], "", "Reading the line…");
      const lim = visW();
      ltag.style.left = `${Math.max(10, Math.min(mx + 12, lim - 334))}px`;
      ltag.style.top = `${Math.max(50, Math.min(my + 12, H - 150))}px`;
      await readNotes();
      if (S.link !== k) return;
      // the line may be in either end
      for (const [f, t] of [[N[a], N[b]], [N[b], N[a]]]) {
        const line = typeof md[f.slug] === "string" ? writtenLine(md[f.slug], t.slug) : "";
        if (line) { write(f, t, line, `Written link, in ${f.title}`); return; }
      }
      write(N[a], N[b], "", "Written link");
    }

    // ---- the note, beside the map, at its own address -----------------------------------------
    const readPage = (n) => fetch(noteUrl(n.slug)).then((r) => (r.ok ? r.text() : null))
      .then((t) => (t ? new DOMParser().parseFromString(t, "text/html") : null)).catch(() => null);
    let asked = 0, opener = "";
    async function openNote(slug, push = true, page = null) {
      const n = by.get(slug);
      if (!n) return;
      const mine = ++asked;
      opener = document.activeElement?.dataset?.k || opener;
      S.note = n.i; S.review = false; S.link = null; ltag.hidden = true;
      if (S.area != null && n.area !== S.area) S.area = null;
      if (push) history.pushState(null, "", noteUrl(slug));
      view.classList.add("has-note");
      renderRail(); reveal(n); paint();
      const doc = page || await readPage(n);
      if (mine !== asked) return;
      fill(n, doc);
      if (!page) drawer.focus({ preventScroll: true });
    }
    function closeNote(push = true) {
      asked++;
      S.note = null; S.review = false; drawer.hidden = true; view.classList.remove("has-note");
      if (camBack) { glide(camBack); camBack = null; }
      if (push) history.pushState(null, "", vaultUrl);
      document.title = vaultTitle;
      renderRail(); paint();
      if (opener) $(`[data-k="${CSS.escape(opener)}"]`, rc)?.focus({ preventScroll: true });
      opener = "";
    }
    function fill(n, doc) {
      const src = doc && $("article.art", doc);
      const top = el("div", "dtop"), art = el("div", "art"), acts = el("div", "row");
      top.append(el("span", "sp", `${m.title} / ${areaName(n.area)}`), press("btn-o", "Close", () => closeNote()));
      if (src) {
        // the note's own page, as it reads alone; its crumb and its form are the room's to say
        for (const part of [".banner", ".meta", "h1", ".tags", ".body"]) {
          const e = $(`:scope > ${part}`, src);
          if (e) art.append(document.importNode(e, true));
        }
        if (doc !== document) document.title = doc.title;
        const form = $(":scope > form[data-save]", src);
        if (form) {
          const said = $("summary", form).textContent, to = $("input[name=save]", form).value;
          acts.append(press("btn btn--small", said === "Save to Silica" ? "Save this note" : said, () => askSilica(`?save=${encodeURIComponent(to)}`)));
        }
      } else art.append(el("h1", "", n.title), el("p", "", "This note could not be read right now."));
      if (can) { const ch = press("btn-o choose", "", () => toggle(n.i)); ch.dataset.i = n.i; acts.append(ch); }
      const rep = src && $(":scope > .row", src);
      if (rep) acts.append(...document.importNode(rep, true).children);
      art.append(acts);
      const lf = doc && $$("aside.side > div", doc).find((d) => $("h2", d)?.textContent === "Linked from");
      if (lf) { const copy = document.importNode(lf, true); copy.className = "lf"; art.append(copy); }
      drawer.replaceChildren(top, art);
      drawer.hidden = false; drawer.scrollTop = 0;
      syncChoose();
      readDiagrams(drawer);
    }
    const syncChoose = () => {
      for (const b of $$("button.choose", drawer)) {
        const on = chosen.has(Number(b.dataset.i));
        b.textContent = on ? "Chosen" : "Add to selection"; b.setAttribute("aria-pressed", String(on));
      }
    };
    drawer.addEventListener("click", (ev) => {
      const a = ev.target.closest("a[href]");
      const n = a && !ev.metaKey && !ev.ctrlKey && !ev.shiftKey && noteAt(a.href);
      if (n) { ev.preventDefault(); openNote(n.slug); }
    });
    drawer.addEventListener("mouseover", (ev) => {
      const a = ev.target.closest("a[href]"), n = a && noteAt(a.href), i = n ? n.i : null;
      if (i !== S.hover) { S.hover = i; paint(); }
    });
    drawer.addEventListener("mouseleave", () => { if (S.hover != null) { S.hover = null; paint(); } });
    addEventListener("popstate", () => {
      const n = noteAt(location.href);
      if (n) openNote(n.slug, false); else if (S.note != null) closeNote(false);
    });

    // ---- the selection: one set, one button that says what it takes ---------------------------
    const toggle = (i) => { if (chosen.has(i)) chosen.delete(i); else chosen.add(i); picksChanged(); };
    function picksChanged() {
      keepPicks(); renderRail(); syncChoose();
      if (S.review) { if (chosen.size) renderReview(); else closeNote(false); }
      paint();
    }
    function openReview() {
      asked++;
      S.note = null; S.review = true; S.link = null; ltag.hidden = true;
      renderReview(); view.classList.add("has-note"); renderRail(); paint();
    }
    function renderReview() {
      let kept = 0, left = 0;
      for (const [a, b] of m.e) { const x = chosen.has(a), y = chosen.has(b); if (x && y) kept++; else if (x || y) left++; }
      const top = el("div", "dtop"), art = el("div", "art"), acts = el("div", "row");
      top.append(el("span", "sp", `${m.title} / Your selection`), press("btn-o", "Close", () => closeNote(false)));
      art.append(el("p", "meta", `${fmt(chosen.size)} of ${count(m.count.notes)} from ${m.title}`), el("h1", "", "Your selection"));
      art.append(el("p", "seltext", `${fmt(kept)} ${kept === 1 ? "link" : "links"} between them stay links in your copy. ${fmt(left)} ${left === 1 ? "link" : "links"} to notes you leave out open @${m.handle}'s page.`));
      acts.append(press("btn btn--small", `Save ${count(chosen.size)} to Silica`, saveChosen), press("btn-o", "Clear", () => { chosen.clear(); picksChanged(); }));
      art.append(acts);
      const groups = new Map();
      for (const i of chosen) { const n = N[i]; if (!groups.has(n.area)) groups.set(n.area, []); groups.get(n.area).push(n); }
      for (const a of [...groups.keys()].sort((p, r) => (p < 0 ? 1e9 : p) - (r < 0 ? 1e9 : r))) {
        const g = el("div", "selg"), ul = el("ul", "rl");
        g.append(el("h2", "rh", `${areaName(a)} ${fmt(groups.get(a).length)}`));
        for (const n of groups.get(a).sort((p, r) => p.title.localeCompare(r.title))) {
          const li = el("li", "arow"), b = press("", "", () => openNote(n.slug));
          b.append(el("span", "nm", n.title));
          li.append(b, press("linkb", "Remove", () => toggle(n.i)));
          ul.append(li);
        }
        g.append(ul); art.append(g);
      }
      drawer.replaceChildren(top, art);
      drawer.hidden = false;
    }

    // ---- the rail: the vault's front page ------------------------------------------------------
    const crumb = document.importNode($("nav.crumb", main), true);
    const report = $(".report", main)?.cloneNode(true);
    const reportFor = report && $("input[name=page]", report);
    if (reportFor) reportFor.value = m.url;
    const tick = (label, on, some, key, change) => {
      const box = el("input"); box.type = "checkbox"; box.checked = on; box.indeterminate = some; box.dataset.k = key; box.setAttribute("aria-label", label);
      box.addEventListener("change", change); return box;
    };
    function row(n, withBox) {
      const li = el("li", withBox ? "arow" : ""), b = press("", "", () => openNote(n.slug));
      if (withBox && can) li.append(tick(`Choose ${n.title}`, chosen.has(n.i), false, `t:${n.slug}`, () => toggle(n.i)));
      b.dataset.k = `n:${withBox ? "a" : "s"}:${n.slug}`;
      b.append(el("span", "nm", n.title));
      if (S.note === n.i) b.setAttribute("aria-current", "true");
      b.addEventListener("pointerenter", () => { S.hover = n.i; paint(); });
      b.addEventListener("pointerleave", () => { S.hover = null; paint(); });
      li.append(b); return li;
    }
    let others = [], menuOff = null;
    load(main.dataset.vaults).then((list) => {
      if (!Array.isArray(list)) return;
      others = list.filter((v) => v.slug !== m.slug && SLUG.test(v.slug));
      renderRail();
    });
    function areasBlock() {
      const box = el("div"), list = el("ul", "rl"), head = el("div", "rhead");
      head.append(el("h2", "rh", "Areas"));
      if (can) {
        const dots = el("button", "dots"), menu = el("div", "amenu");
        dots.type = "button"; dots.dataset.k = "dots";
        dots.setAttribute("aria-label", "Area options"); dots.setAttribute("aria-haspopup", "menu"); dots.setAttribute("aria-expanded", "false");
        dots.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/></svg>';
        menu.setAttribute("role", "menu"); menu.setAttribute("aria-label", "Area options"); menu.hidden = true;
        const item = (label, off, act) => {
          const b = press("", label, () => { act(); menuOff?.(true); picksChanged(); });
          b.setAttribute("role", "menuitem"); b.disabled = off; menu.append(b);
        };
        item("Select all", chosen.size === N.length, () => { for (const n of N) chosen.add(n.i); });
        item("Deselect all", chosen.size === 0, () => chosen.clear());
        const shut = (back) => { menu.hidden = true; dots.setAttribute("aria-expanded", "false"); menuOff = null; if (back) dots.focus(); };
        dots.addEventListener("click", () => {
          if (!menu.hidden) { shut(); return; }
          menu.hidden = false; dots.setAttribute("aria-expanded", "true"); menuOff = shut;
          $("button:not(:disabled)", menu).focus();
        });
        menu.addEventListener("keydown", (ev) => {
          if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
          ev.preventDefault();
          const on = $$("button:not(:disabled)", menu), at = on.indexOf(document.activeElement);
          on[(at + (ev.key === "ArrowDown" ? 1 : -1) + on.length) % on.length].focus();
        });
        head.append(dots, menu);
      }
      box.append(head);
      const groups = m.areas.map(([name, size], i) => ({ name, size, i }));
      if (m.unlinked.length) groups.push({ name: areaName(-1), size: m.unlinked.length, i: -1 });
      for (const g of groups) {
        const li = el("li"), line = el("div", "arow"), isOpen = S.open.has(g.i);
        const members = N.filter((n) => n.area === g.i), got = members.filter((n) => chosen.has(n.i)).length;
        if (can) {
          line.append(tick(`Choose ${g.name}`, got === members.length, got > 0 && got < members.length, `a:${g.i}`, () => {
            for (const n of members) { if (got === members.length) chosen.delete(n.i); else chosen.add(n.i); }
            picksChanged();
          }));
        }
        const b = press("", "", () => {
          if (S.open.has(g.i)) { S.open.delete(g.i); if (S.area === g.i) S.area = null; } else { S.open.add(g.i); if (g.i >= 0) { S.area = g.i; frameArea(g.i); } }
          renderRail(); paint();
        });
        b.dataset.k = `g:${g.i}`;
        b.append(el("span", "nm", g.name), el("span", "ct", got && got < members.length ? `${fmt(got)} of ${fmt(g.size)}` : fmt(g.size)), el("span", "cv", isOpen ? "▾" : "▸"));
        b.setAttribute("aria-expanded", String(isOpen));
        if (S.area === g.i) b.setAttribute("aria-current", "true");
        if (g.i >= 0) { b.addEventListener("pointerenter", () => { S.areaHover = g.i; paint(); }); b.addEventListener("pointerleave", () => { S.areaHover = null; paint(); }); }
        line.append(b); li.append(line);
        if (isOpen) {
          const ul = el("ul");
          for (const n of members.sort((p, r) => r.deg - p.deg || p.title.localeCompare(r.title))) ul.append(row(n, true));
          li.append(ul);
        }
        list.append(li);
      }
      box.append(list);
      return box;
    }
    function renderRail() {
      const keep = rc.scrollTop, k = document.activeElement?.dataset?.k;
      const by2 = el("p", "vp-by"), author = el("a", "", `@${m.handle}`), dl = el("dl", "facts");
      author.href = pageUrl; by2.append("by ", author);
      const c = m.count;
      for (const [t, v, cls] of [["Notes", fmt(c.notes), "num"], ["Links", fmt(c.links), "num"], ["Areas", fmt(c.areas), "num"], ["License", m.license === "not stated" ? "Not stated by the author" : m.license, ""]]) dl.append(el("dt", "", t), el("dd", cls, v));
      const parts = [crumb, el("h1", "rt", m.title), by2];
      if (m.line) parts.push(el("p", "vp-line", m.line));
      parts.push(dl);
      if (report) { const acts = el("div", "acts"); acts.append(report); parts.push(acts); }
      // Start here: the most linked note of the three largest areas; an author's own picks are a later step
      const start = el("div"), sl = el("ul", "rl");
      start.append(el("h2", "rh", "Start here"));
      for (const n of m.areas.slice(0, 3).map((_, i) => ranked.find((r) => r.area === i)).filter(Boolean)) sl.append(row(n));
      start.append(sl);
      parts.push(start, areasBlock());
      if (others.length) {
        const more = el("div"), ml = el("ul", "rl");
        more.append(el("h2", "rh", `Also by @${m.handle}`));
        for (const o of others) {
          const li = el("li"), a = el("a");
          a.href = `${up}${o.slug}/${ext ? "index.html" : ""}`;
          a.append(el("span", "nm", o.title), el("span", "ct", fmt(o.count.notes)));
          li.append(a); ml.append(li);
        }
        more.append(ml); parts.push(more);
      }
      rc.replaceChildren(...parts);
      rc.scrollTop = keep;
      if (k) $(`[data-k="${CSS.escape(k)}"]`, rc)?.focus({ preventScroll: true });
      renderFoot();
    }

    // ---- the foot of the rail and the question of which Silica ---------------------------------
    const silicaName = (to) => silicas.find(([a]) => a === to)?.[1] || new URL(to).host;
    const saveChosen = () => askSilica(saveQuery(m.url, N, new Set([...chosen].map((i) => N[i].slug))));
    function renderFoot() {
      rf.hidden = !can;
      if (!can) return;
      const some = el("p", "rfl");
      if (chosen.size) {
        some.append(press("linkb", `${fmt(chosen.size)} of ${fmt(m.count.notes)} chosen`, openReview), " · ", press("linkb", "Clear", () => { chosen.clear(); picksChanged(); }));
      } else some.append("Tick an area or a note to save only those.");
      rf.replaceChildren(press("btn", `Save ${count(chosen.size || m.count.notes)} to Silica`, saveChosen), some);
      if (remember) {
        const w = el("p", "rfl");
        w.append(`Saves to ${silicaName(remember)}. `, press("linkb", "Change", () => {
          remember = "";
          try { localStorage.removeItem(REMEMBER); } catch { /* it lasts the visit */ }
          renderFoot();
        }));
        rf.append(w);
      }
    }
    // Asked at every Save and kept only on the reader's word: a reader may save into the hosted Silica
    // one day and the local one the next. A navigation, not a form: the host's policy sends forms only
    // to the two Silicas it names, and an address the reader typed is neither.
    // shortcut: past Cloudflare's 16 KB of address (about 700 scattered notes, read 2026-11-06) the app
    // refuses the request and Back keeps the selection; carry it in a POST if a reader ever meets that.
    function askSilica(query) {
      const go = (to) => { location.href = to + query; };
      if (remember) { go(remember); return; }
      const row2 = el("div", "row"), other = el("div", "row"), line = el("div", "keeprow"), keep = el("label", "keep"), box = el("input");
      box.type = "checkbox"; keep.append(box, "Remember this");
      const pickIt = (to) => {
        if (box.checked) {
          remember = to;
          try { localStorage.setItem(REMEMBER, to); } catch { /* it lasts the visit */ }
          renderFoot();
        }
        sheet.close(); go(to);
      };
      silicas.forEach(([to, label], i) => row2.append(press(i ? "btn-o" : "btn btn--small", label, () => pickIt(to))));
      row2.append(press("linkb", "Cancel", () => sheet.close()));
      const addr = el("input", "sel");
      addr.placeholder = "https://"; addr.setAttribute("aria-label", "Address of your Silica");
      const use = press("btn-o", "Use this address", () => {
        let to = addr.value.trim().replace(/\/+$/, "");
        if (!/^https?:\/\//i.test(to)) to = `https://${to}`;
        try { new URL(to); } catch { addr.focus(); return; }
        pickIt(`${to}/`);
      });
      addr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") use.click(); });
      // most readers have one of the two Silicas above, so the field waits behind a link
      other.hidden = true; other.append(addr, use);
      const more = press("linkb", "Another address", () => { other.hidden = false; more.hidden = true; addr.focus(); });
      line.append(keep, more);
      sheet.replaceChildren(el("p", "pt", "Save into which Silica?"), row2, line, other, el("p", "dim", "Your Silica asks before it writes anything, and every note keeps its author's name."));
      sheet.showModal();
    }

    // ---- search: titles at once, the text once notes.json is read -----------------------------
    let hits = [], act = -1;
    function search() {
      const s = q.value.trim().toLowerCase();
      res.replaceChildren(); hits = []; act = -1;
      if (!s) { res.hidden = true; S.hover = null; paint(); return; }
      if (!md) readNotes().then(() => { if (q.value.trim()) search(); });
      const t = [], x = [];
      for (const n of N) {
        if (n.title.toLowerCase().includes(s)) t.push(n);
        else if (md && words(n.slug).toLowerCase().includes(s)) x.push(n);
      }
      const add = (n, snip) => {
        const b = press("", n.title, () => { res.hidden = true; q.value = ""; S.hover = null; openNote(n.slug); });
        if (snip) b.append(el("small", "", snip));
        b.addEventListener("pointerenter", () => { S.hover = n.i; paint(); });
        hits.push({ b, n }); res.append(b);
      };
      if (t.length) { res.append(el("h3", "", "Titles")); t.slice(0, 6).forEach((n) => add(n)); }
      if (x.length) {
        res.append(el("h3", "", `In the text, ${count(x.length)}`));
        x.slice(0, 6).forEach((n) => {
          const txt = words(n.slug), at = txt.toLowerCase().indexOf(s), a = Math.max(0, at - 40);
          add(n, `${a ? "…" : ""}${txt.slice(a, at + s.length + 60)}…`);
        });
      }
      if (can && t.length + x.length > 1) {
        const all = [...t, ...x];
        res.append(press("all", `Add all ${fmt(all.length)} matches to the selection`, () => {
          for (const n of all) chosen.add(n.i);
          res.hidden = true; q.value = ""; S.hover = null; picksChanged();
        }));
      }
      if (!md) res.append(el("p", "", "Reading the notes…"));
      else if (!t.length && !x.length) res.append(el("p", "", `No note in ${m.title} says "${q.value.trim()}".`));
      res.hidden = false;
    }
    q.addEventListener("input", search);
    q.addEventListener("focus", () => { if (q.value) res.hidden = false; });
    q.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { q.value = ""; search(); q.blur(); return; }
      if (ev.key === "Enter" && hits.length) { hits[Math.max(act, 0)].b.click(); return; }
      if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
      ev.preventDefault();
      if (!hits.length) return;
      act = (act + (ev.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length;
      hits.forEach((h, i) => h.b.classList.toggle("on", i === act));
      hits[act].b.scrollIntoView({ block: "nearest" }); S.hover = hits[act].n.i; paint();
    });
    document.addEventListener("keydown", (ev) => {
      if (sheet.open) return;
      if (ev.key === "/" && !document.activeElement?.matches("input:not([type=checkbox]), textarea")) { ev.preventDefault(); q.focus(); return; }
      if (ev.key !== "Escape" || document.activeElement === q) return;
      if (menuOff) menuOff(true);
      else if (S.pick) setPick(false);
      else if (S.link != null) { S.link = null; ltag.hidden = true; paint(); }
      else if (S.note != null || S.review) closeNote(!S.review);
    });
    document.addEventListener("pointerdown", (ev) => {
      if (!ev.target.closest(".rs")) res.hidden = true;
      if (menuOff && !ev.target.closest(".rhead")) menuOff();
    });

    // ---- size and start ------------------------------------------------------------------------
    function resize() {
      const r = view.getBoundingClientRect(), w = Math.max(40, Math.floor(r.width)), h = Math.max(40, Math.floor(r.height));
      if (w === W && h === H && img) { paint(); return; }
      W = w; H = h; cv.width = W; cv.height = H; img = ctx.createImageData(W, H); px = new Uint32Array(img.data.buffer);
      const d = devicePixelRatio || 1; nc.width = W * d; nc.height = H * d;
      room.style.setProperty("--dw", `${drawerW()}px`);
      arrange(true);
    }
    inks();
    drawers.push(() => { inks(); paint(); });
    new ResizeObserver(resize).observe(view);
    renderRail();
    resize();
    if (first) openNote(first, false, document);
    document.fonts?.ready.then(() => { widths.clear(); paint(); });
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
      count.textContent = `${list.length} ${list.length === 1 ? "wiki" : "wikis"} · ${fmt(notes)} notes`;
      empty.hidden = list.length > 0;
      empty.textContent = qq ? `No wiki matches "${S.q.trim()}"${S.area ? ` in ${S.area}` : ""}. Search reads titles, lines, areas and handles.`
        : `No wiki has an area called ${S.area}.`;
      areas();
    }
    $(".cat-tools", cat).hidden = false;
    q.addEventListener("input", () => { S.q = q.value; render(); });
    for (const b of $$("[data-sort] button", cat)) {
      b.addEventListener("click", () => { S.sort = b.dataset.v; $$("[data-sort] button", cat).forEach((x) => x.setAttribute("aria-pressed", String(x === b))); render(); });
    }
    render();
  }

  // ---- a note's diagrams: mermaid from this same folder, only on a page that holds one ------
  // The note renderer writes a ```mermaid fence as <pre class="mermaid"> with its source escaped,
  // so a reader without the script still reads the source. The host's policy refuses inline
  // styles (style-src 'self'), and mermaid's SVG carries its colours in a <style> and in style
  // attributes: drawn as it comes, every box is black. So the SVG is rendered off the page and
  // its styles are handed over through the CSSOM, which the policy leaves to this script, the one
  // the host serves from /_s/. The source is kept: a theme switch draws it again in the other inks.
  let diagrams = [], drawing = 0;
  // The keys a note's own %%{init}%% may not set: its CSS would reach the page through the CSSOM.
  const LOCKED = ["secure", "securityLevel", "startOnLoad", "maxTextSize", "maxEdges", "suppressErrorRendering",
    "theme", "themeVariables", "themeCSS", "fontFamily", "altFontFamily", "darkMode", "htmlLabels"];
  function diagramConfig() {
    const dark = getComputedStyle(root).colorScheme === "dark";
    return {
      startOnLoad: false, suppressErrorRendering: true, securityLevel: "strict", secure: LOCKED,
      theme: "base", htmlLabels: false, fontFamily: css("--sans"), fontSize: 14,
      flowchart: { curve: "basis", padding: 14, htmlLabels: false },
      themeVariables: {
        darkMode: dark, fontSize: "14px", background: css("--slate"),
        primaryColor: css("--sheet"), primaryTextColor: css("--frost"), primaryBorderColor: css("--ash-dim"),
        secondaryColor: css("--slate-2"), tertiaryColor: css("--slate"), lineColor: css("--ash"),
        textColor: css("--frost"), mainBkg: css("--sheet"), nodeBorder: css("--ash-dim"),
        clusterBkg: css("--slate-2"), clusterBorder: css("--line-2"), edgeLabelBackground: css("--slate"),
        titleColor: css("--frost"),
      },
    };
  }
  const sheets = new Map(); // a diagram's index -> the sheet its <style> became
  async function drawOne(d, i) {
    const { svg } = await window.mermaid.render(`pub-diagram-${drawing}-${i}`, d.dataset.src);
    const el = new DOMParser().parseFromString(svg, "image/svg+xml").documentElement;
    if (el.nodeName !== "svg") throw new Error("not an svg");
    const sheet = new CSSStyleSheet();
    sheet.replaceSync([...el.querySelectorAll("style")].map((st) => { st.remove(); return st.textContent; }).join("\n"));
    const inline = [];
    for (const n of [el, ...el.querySelectorAll("[style]")]) {
      if (!n.hasAttribute("style")) continue;
      n.setAttribute("data-st", String(inline.push(n.getAttribute("style")) - 1));
      n.removeAttribute("style");
    }
    const live = document.importNode(el, true);
    d.replaceChildren(live);
    for (const n of [live, ...live.querySelectorAll("[data-st]")]) {
      const at = n.getAttribute("data-st");
      if (at === null) continue;
      n.style.cssText = inline[Number(at)];
      n.removeAttribute("data-st");
    }
    sheets.set(i, sheet);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  }
  async function drawDiagrams() {
    if (!window.mermaid) return;
    const mine = ++drawing;
    window.mermaid.initialize(diagramConfig());
    document.adoptedStyleSheets = document.adoptedStyleSheets.filter((x) => ![...sheets.values()].includes(x));
    sheets.clear();
    for (const [i, d] of diagrams.entries()) {
      // the drawer moved on to another note, or the theme changed: the later run draws its own
      if (mine !== drawing) return;
      // a diagram mermaid cannot parse keeps its source, which still reads
      try { await drawOne(d, i); } catch { d.textContent = d.dataset.src; }
    }
  }
  let fetching = false;
  // The page's diagrams when it loads, then the drawer's at each note: the host's policy allows no
  // other way in, and most notes hold none, so mermaid's file is read at the first that does.
  function readDiagrams(scope) {
    diagrams = $$("pre.mermaid", scope);
    for (const d of diagrams) d.dataset.src ||= d.textContent;
    if (!diagrams.length || !here) return;
    if (window.mermaid) { drawDiagrams(); return; }
    if (fetching) return;
    fetching = true;
    const s = document.createElement("script");
    s.src = new URL("mermaid.min.js", here).href;
    s.onload = drawDiagrams;
    document.head.append(s);
  }
  for (const b of $$("[data-theme-btn]")) b.addEventListener("click", drawDiagrams);
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", drawDiagrams);
  readDiagrams(document);
})();
