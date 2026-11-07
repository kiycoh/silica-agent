// --- Discover (ADR-0137) ------------------------------------------------------
// The person's feeds read into stories: a front page (For you, News, Research,
// Video), a story written the first time it is opened, the Feeds screen and
// the first run. The page owns three addresses, #discover, #discover/feeds and
// #discover/story/<id>, the way Learn owns its own (syncUrl leaves them be).
// Everything a publisher wrote is set as text, never as markup (ADR-0009).

const DV_TABS = Object.entries(VOCAB.discover.tabs);
const DV_SCOPES = [["story", "This story"], ["web", "Web"], ["vault", "Vault"]];
const DV_POLL_MS = 900;
const dv = {
  data: null, tab: "foryou", topic: "all", cat: "", story: null, scope: "story",
  feeds: null, catalog: null, catalogOpen: false, toast: null, poll: 0, storyPoll: 0, firstPick: null,
};

const DV_ICON = {
  refresh: '<svg aria-hidden="true" focusable="false" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"></path></svg>',
  list: '<svg aria-hidden="true" focusable="false" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"></path></svg>',
  save: '<svg aria-hidden="true" focusable="false" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12v18l-6-4-6 4z"></path></svg>',
  ask: '<svg aria-hidden="true" focusable="false" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H9l-5 4z"></path></svg>',
  up: '<svg aria-hidden="true" focusable="false" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M7 11v9H4v-9zM7 11l4-8a2 2 0 0 1 2 2v4h6a2 2 0 0 1 2 2.3l-1.3 7A2 2 0 0 1 17.7 20H7"></path></svg>',
  down: '<svg aria-hidden="true" focusable="false" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M17 13V4h3v9zM17 13l-4 8a2 2 0 0 1-2-2v-4H5a2 2 0 0 1-2-2.3l1.3-7A2 2 0 0 1 6.3 4H17"></path></svg>',
  back: '<svg aria-hidden="true" focusable="false" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M11 6l-6 6 6 6"></path></svg>',
  send: '<svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"></path></svg>',
  ext: '<svg aria-hidden="true" focusable="false" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"></path></svg>',
  play: '<svg aria-hidden="true" focusable="false" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 5v14l11-7z"></path></svg>',
  warn: '<svg aria-hidden="true" focusable="false" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"></path></svg>',
  check: '<svg aria-hidden="true" focusable="false" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"></path></svg>',
  tick: '<svg aria-hidden="true" focusable="false" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"></path></svg>',
  chev: '<svg aria-hidden="true" focusable="false" width="10" height="10" viewBox="0 0 10 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l4 4 4-4"></path></svg>',
  compass: '<svg aria-hidden="true" focusable="false" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><path d="M15.5 8.5l-2 5-5 2 2-5z"></path></svg>',
};

function dvBtn(label, cls, onClick, icon) {
  const b = mkEl("button", "dv-btn " + (cls || ""));
  b.type = "button";
  if (icon) b.innerHTML = icon;
  if (label) b.append(document.createTextNode(label));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function dvSwitch(on, label, onToggle) {
  const b = mkEl("button", "dv-switch");
  b.type = "button";
  b.setAttribute("aria-pressed", on ? "true" : "false");
  b.setAttribute("aria-label", label);
  b.addEventListener("click", () => {
    const next = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", next ? "true" : "false");
    onToggle(next);
  });
  return b;
}

function dvCover(src, cls) {
  const img = mkEl("img", "dv-cover " + (cls || ""));
  img.src = src;
  img.alt = ""; // the title beside it says what the story is; the image is the article's own
  img.loading = "lazy";
  img.decoding = "async";
  // A cover dropped by the ceiling between the list and the draw: the card
  // goes typographic rather than showing a broken image.
  img.addEventListener("error", () => img.remove());
  return img;
}

// Every link the page draws comes from a publisher or an encyclopedia: only
// a web address becomes an href, never a javascript: or data: one.
const dvHref = (url) => (/^https?:\/\//i.test(String(url || "")) ? url : "#");

function dvMarks(domains) {
  const w = mkEl("span", "dv-marks");
  w.setAttribute("aria-hidden", "true");
  for (const d of domains) w.append(mkEl("span", "dv-mark", (d || "?")[0]));
  return w;
}

function dvBody() { return $("#dv-body"); }

function dvRoute() {
  const parts = (location.hash || "").replace(/^#/, "").split("?")[0].split("/");
  if (parts[0] !== "discover") return { screen: "feed" };
  if (parts[1] === "story" && parts[2]) return { screen: "story", id: decodeURIComponent(parts[2]) };
  if (parts[1] === "feeds") return { screen: "feeds" };
  return { screen: "feed" };
}

function dvGo(path, push = true) {
  const h = "#discover" + (path ? "/" + path : "");
  if (location.hash !== h) (push ? history.pushState : history.replaceState).call(history, null, "", h);
  loadDiscover();
}

// Back and Forward between a story and the front page. The shell's router
// runs first and loads the view when it switches to it; this only follows a
// move inside the view, so a hash it already loaded is not loaded twice.
window.addEventListener("hashchange", () => {
  if (activeTab === "discover" && location.hash !== dv.loaded) loadDiscover();
});

function loadDiscover() {
  dvPopClose();
  dv.loaded = location.hash;
  const r = dvRoute();
  clearTimeout(dv.storyPoll);
  if (r.screen === "story") return dvOpenStory(r.id);
  if (r.screen === "feeds") return dvLoadFeeds();
  return dvLoadFront();
}

async function dvLoadFront(quiet) {
  if (!quiet && !dv.data) $("#dv-loading").hidden = false;
  try {
    dv.data = await api("/discover");
  } catch (e) {
    $("#dv-loading").hidden = true;
    dvBody().replaceChildren(dvEmpty("Discover could not load", String(e.message || e)));
    return;
  }
  $("#dv-loading").hidden = true;
  if (!dv.data.setup) return dvFirstRun();
  dvRenderFront();
  dvWatchRead();
}

function dvWatchRead() {
  clearTimeout(dv.poll);
  if (!dv.data || !dv.data.reading) return;
  dv.poll = setTimeout(async () => {
    let st;
    try { st = await api("/discover/status"); } catch { return; }
    if (activeTab !== "discover") return;
    if (st.reading) {
      dv.data.reading = st.reading;
      dvPaintProgress();
      dvWatchRead();
    } else {
      dv.data.reading = null;
      if (dvRoute().screen === "feed") dvLoadFront(true);
    }
  }, DV_POLL_MS);
}

function dvAgo(ts) {
  if (!ts) return "not read yet";
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 1 ? "read just now" : m < 60 ? `read ${m} min ago` : m < 2880 ? `read ${Math.round(m / 60)} h ago` : `read ${Math.round(m / 1440)} days ago`;
}

function dvPaintProgress() {
  const bar = $("#dv-progress");
  if (!bar) return;
  const r = dv.data && dv.data.reading;
  bar.hidden = !r;
  if (!r) return;
  const pct = r.of ? Math.round((100 * r.done) / r.of) : 0;
  bar.querySelector(".dv-track > div").style.width = (r.phase === "feeds" ? pct : 100) + "%";
  bar.querySelector(".dv-meta-strong").textContent = r.phase === "feeds"
    ? `Reading feeds · ${r.done} of ${r.of}`
    : r.phase === "covers" ? "Fetching covers" : r.phase === "near" ? "Matching your vault" : "Grouping into stories";
}

function dvRenderFront() {
  const d = dv.data;
  const root = mkEl("div");
  root.style.cssText = "display:contents";
  const head = mkEl("header", "dv-head");
  head.append(mkEl("h1", "", "Discover"));
  const nav = mkEl("nav", "dv-tabs");
  nav.setAttribute("aria-label", "Discover tabs");
  for (const [k, label] of DV_TABS) {
    if (k === "video" && !d.buckets.video && !d.tabs.video.length) continue;
    const t = mkEl("button", "dv-tab", label);
    t.type = "button";
    t.setAttribute("aria-pressed", dv.tab === k ? "true" : "false");
    t.addEventListener("click", () => { dv.tab = k; dvRenderFront(); });
    nav.append(t);
  }
  head.append(nav, mkEl("div", "dv-spacer"), mkEl("span", "dv-meta", d.reading ? "reading…" : dvAgo(d.read_at)));
  const refresh = dvBtn("", "dv-icon", async () => {
    refresh.disabled = true;
    try { const r = await apiPost("/discover/read"); d.reading = r.reading || { done: 0, of: 0, phase: "feeds" }; dvPaintProgress(); dvWatchRead(); }
    catch (e) { notify("couldn't read the feeds: " + e.message); }
    refresh.disabled = false;
  }, DV_ICON.refresh);
  refresh.setAttribute("aria-label", "read the feeds again");
  refresh.title = "read the feeds again";
  head.append(refresh, dvBtn("Feeds", "", () => dvGo("feeds"), DV_ICON.list));
  const prog = mkEl("div", "dv-progress");
  prog.id = "dv-progress";
  prog.setAttribute("role", "status");
  const track = mkEl("div", "dv-track");
  track.append(mkEl("div"));
  prog.append(track, mkEl("span", "dv-meta-strong"));
  const scroll = mkEl("div", "dv-scroll");
  root.append(head, prog, scroll);
  dvBody().replaceChildren(root);
  dvPaintProgress();
  const cards = d.tabs[dv.tab] || [];
  if (dv.tab === "foryou") dvForYou(scroll, cards);
  else if (dv.tab === "news") dvNews(scroll, cards);
  else if (dv.tab === "research") dvResearch(scroll, cards);
  else dvVideo(scroll, cards);
}

function dvEmpty(head, text) {
  const e = mkEl("div", "dv-empty");
  e.append(mkEl("strong", "", head));
  if (text) e.append(mkEl("span", "", text));
  return e;
}

function dvNothingYet(scroll) {
  const d = dv.data;
  scroll.append(d.reading
    ? dvEmpty("Reading your feeds", "Stories appear as soon as the first feeds answer.")
    : dvEmpty("No stories here yet", d.read_at ? "Your feeds published nothing in this bucket in the last 14 days, or it is switched off in Feeds." : "Press refresh to read your feeds."));
}

function dvSaveBtn(card, small) {
  const saved = !!card.saved;
  const b = dvBtn(small ? "" : saved ? "Saved" : "Save", "dv-flat dv-sm" + (saved ? " dv-saved" : ""), () => dvSave(card, b), DV_ICON.save);
  b.setAttribute("aria-label", saved ? "saved to the Inbox" : "save to the Inbox");
  if (small) b.classList.add("dv-icon");
  b.title = saved ? card.saved : "save this story to the Inbox as a cited note";
  return b;
}

function dvAskBtn(card) {
  return dvBtn("Ask", "dv-flat dv-sm", () => dvToChat(card, "", "story"), DV_ICON.ask);
}

function dvFeedbackBtns(card) {
  const mk = (v, icon, label) => {
    const b = dvBtn("", "dv-flat dv-sm dv-icon", async () => {
      const next = card.feedback === v ? 0 : v;
      try { await apiPost("/discover/feedback", { id: card.id, value: next }); }
      catch (e) { notify("couldn't record that: " + e.message); return; }
      card.feedback = next;
      // Less like this leaves For you now, not on the next load.
      if (next === -1 && dv.tab === "foryou") {
        dv.data.tabs.foryou = dv.data.tabs.foryou.filter((c) => c.id !== card.id);
        dvRenderFront();
        notify("fewer stories like this one", "info");
        return;
      }
      for (const x of [up, down]) x.setAttribute("aria-pressed", "false");
      if (next) b.setAttribute("aria-pressed", "true");
    }, icon);
    b.setAttribute("aria-label", label);
    b.title = label;
    b.setAttribute("aria-pressed", card.feedback === v ? "true" : "false");
    return b;
  };
  const up = mk(1, DV_ICON.up, "more like this");
  const down = mk(-1, DV_ICON.down, "less like this");
  return [up, down];
}

function dvTitle(tag, card, cls) {
  const h = mkEl(tag, cls || "");
  const b = mkEl("button", "dv-title-btn", card.title);
  b.type = "button";
  b.addEventListener("click", () => dvGo("story/" + encodeURIComponent(card.id)));
  h.append(b);
  return h;
}

function dvMetaLine(card) {
  const bits = [`${card.articles} article${card.articles === 1 ? "" : "s"}`];
  return bits.join(" · ");
}

function dvForYou(scroll, all) {
  const d = dv.data;
  const topics = (d.topics || []).filter((t) => t.on);
  if (topics.length) {
    const row = mkEl("div", "dv-topics");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "topics");
    for (const t of [{ key: "all", label: "All" }, ...topics]) {
      const c = mkEl("button", "dv-chip", t.label);
      c.type = "button";
      c.setAttribute("aria-pressed", dv.topic === t.key ? "true" : "false");
      c.addEventListener("click", () => { dv.topic = t.key; dvRenderFront(); });
      row.append(c);
    }
    const add = mkEl("button", "dv-chip dv-dashed", "+ Topic");
    add.type = "button";
    add.title = "add a topic from the catalog";
    add.addEventListener("click", () => { dv.catalogOpen = true; dvGo("feeds"); });
    row.append(add);
    scroll.append(row);
  }
  const cards = dv.topic === "all" ? all : all.filter((c) => (c.topics || []).includes(dv.topic));
  const front = mkEl("div", "dv-front");
  const main = mkEl("div", "dv-main");
  front.append(main, dvRail());
  scroll.append(front);
  if (!cards.length) { dvNothingYet(main); return; }
  const [lead, ...rest] = cards;
  main.append(dvLead(lead));
  const grid = mkEl("div", "dv-grid");
  const gridCards = rest.slice(0, 3);
  for (const c of gridCards) grid.append(dvCard(c));
  if (gridCards.length) main.append(grid);
  const after = rest.slice(3);
  const nearIdx = after.findIndex((c) => (c.near || []).length);
  if (nearIdx >= 0) main.append(dvNearCard(after.splice(nearIdx, 1)[0]));
  if (after.length) {
    const sec = mkEl("section", "dv-list");
    sec.setAttribute("aria-label", "more stories");
    sec.append(mkEl("h2", "", "More stories"));
    for (const c of after.slice(0, 30)) sec.append(dvMoreRow(c));
    main.append(sec);
  }
}

function dvLead(c) {
  const a = mkEl("article", "dv-lead");
  const t = mkEl("div", "dv-lead-text");
  t.append(mkEl("span", "dv-meta", c.kicker), dvTitle("h2", c));
  if (c.lead) t.append(mkEl("p", "", c.lead));
  const acts = mkEl("div", "dv-actions");
  const near = (c.near || []).length ? `near ${c.near.length} note${c.near.length === 1 ? "" : "s"} in your vault` : "nothing in your vault yet";
  acts.append(dvMarks(c.domains), mkEl("span", "dv-meta-strong", dvMetaLine(c)), mkEl("span", "dv-meta", "· " + near),
    mkEl("div", "dv-spacer"), dvSaveBtn(c), dvAskBtn(c), ...dvFeedbackBtns(c));
  t.append(acts);
  a.append(t);
  if (c.cover) a.append(dvCover(c.cover));
  return a;
}

function dvCard(c) {
  const a = mkEl("article", "dv-card");
  if (c.cover) a.append(dvCover(c.cover));
  a.append(mkEl("span", "dv-meta", c.kicker), dvTitle("h3", c), mkEl("div", "dv-spacer"), mkEl("span", "dv-meta-strong", dvMetaLine(c)));
  const acts = mkEl("div", "dv-actions dv-tight");
  acts.append(dvSaveBtn(c), dvAskBtn(c), mkEl("div", "dv-spacer"), ...dvFeedbackBtns(c));
  a.append(acts);
  return a;
}

function dvNearList(near, cls) {
  const box = mkEl("div", cls || "dv-near");
  box.append(mkEl("span", "dv-meta-strong", "Near notes in your vault"));
  for (const n of near) {
    const b = mkEl("button", "dv-link", n.name);
    b.type = "button";
    b.title = "open " + n.path + " in the note drawer";
    b.addEventListener("click", () => openNote(n.path));
    box.append(b);
  }
  return box;
}

function dvNearCard(c) {
  const a = mkEl("article", "dv-near-card");
  if (c.cover) a.append(dvCover(c.cover));
  const t = mkEl("div", "dv-near-text");
  t.append(mkEl("span", "dv-meta", c.kicker), dvTitle("h3", c));
  if (c.lead) t.append(mkEl("p", "", c.lead));
  a.append(t, dvNearList(c.near));
  return a;
}

function dvMoreRow(c) {
  const r = mkEl("div", "dv-row");
  if (c.cover) r.append(dvCover(c.cover));
  const t = mkEl("div", "dv-row-text");
  const title = mkEl("button", "dv-title-btn dv-row-title", c.title);
  title.type = "button";
  title.addEventListener("click", () => dvGo("story/" + encodeURIComponent(c.id)));
  t.append(title, mkEl("span", "dv-meta", c.kicker + " · " + dvMetaLine(c)));
  r.append(t, dvSaveBtn(c, true));
  return r;
}

function dvRail() {
  const d = dv.data;
  const rail = mkEl("div", "dv-rail");
  const shaped = mkEl("section", "dv-panel");
  shaped.append(mkEl("h2", "", "Shaped by your vault"),
    mkEl("p", "", "For you ranks stories by the notes you keep, the ones you saved, and More or Less like this."));
  if (!(d.topics || []).length) shaped.append(mkEl("span", "dv-meta", "No topic yet: a folder or tag needs three notes to be one."));
  for (const t of d.topics || []) {
    const row = mkEl("div", "dv-interest");
    const txt = mkEl("div");
    txt.append(mkEl("div", "dv-il", t.label), mkEl("div", "dv-meta", t.why));
    row.append(txt, dvSwitch(t.on, "rank by " + t.label, async (on) => {
      try { await apiPost("/discover/topics", { key: t.key, on }); t.on = on; dvLoadFront(true); }
      catch (e) { notify("couldn't change that topic: " + e.message); }
    }));
    shaped.append(row);
  }
  const feeds = mkEl("section", "dv-panel");
  const line = mkEl("div", "dv-meta-strong", `${d.feeds.read} read`);
  if (d.feeds.failing) {
    line.append(" · ");
    line.append(mkEl("span", "dv-warn", `${d.feeds.failing} need${d.feeds.failing === 1 ? "s" : ""} a look`));
  }
  const review = mkEl("button", "dv-link", "Review feeds");
  review.type = "button";
  review.addEventListener("click", () => dvGo("feeds"));
  feeds.append(mkEl("h2", "", "Feeds"), line, mkEl("span", "dv-meta", "Read when you open Discover, at most once an hour"), review);
  const saved = mkEl("section", "dv-panel");
  saved.append(mkEl("h2", "", "Saved to Inbox"),
    mkEl("div", "dv-meta-strong", d.saved ? `${d.saved} stor${d.saved === 1 ? "y" : "ies"} waiting` : "Nothing saved yet"),
    mkEl("span", "dv-meta", "A saved story waits in the Inbox as a cited note. It joins the vault when you nucleate it."));
  if (d.saved) saved.append(dvBtn("Nucleate the Inbox", "dv-sm", () => dvNucleate(d.inbox || "Inbox")));
  rail.append(shaped, feeds, saved);
  return rail;
}

function dvNews(scroll, cards) {
  scroll.append(mkEl("p", "dv-intro", "The latest stories from your news feeds, grouped across them. Not ranked by your vault."));
  if (!cards.length) return dvNothingYet(scroll);
  const list = mkEl("div", "dv-list dv-news");
  for (const c of cards) {
    const r = mkEl("div", "dv-row");
    if (c.cover) r.append(dvCover(c.cover));
    else {
      const n = mkEl("span", "dv-nocover", "no cover");
      n.setAttribute("aria-hidden", "true");
      r.append(n);
    }
    const t = mkEl("div", "dv-row-text");
    const title = mkEl("button", "dv-title-btn dv-row-title", c.title);
    title.type = "button";
    title.addEventListener("click", () => dvGo("story/" + encodeURIComponent(c.id)));
    t.append(title, mkEl("span", "dv-meta", c.kicker + " · " + dvMetaLine(c)));
    r.append(t, dvSaveBtn(c));
    list.append(r);
  }
  scroll.append(list);
}

function dvResearch(scroll, cards) {
  const d = dv.data;
  const row = mkEl("div", "dv-topics");
  row.append(mkEl("span", "dv-meta", "arXiv, the latest listings"));
  for (const cat of d.research_cats || []) {
    const c = mkEl("button", "dv-chip dv-mono", cat);
    c.type = "button";
    c.setAttribute("aria-pressed", dv.cat === cat ? "true" : "false");
    c.addEventListener("click", () => { dv.cat = dv.cat === cat ? "" : cat; dvRenderFront(); });
    row.append(c);
  }
  const add = mkEl("button", "dv-chip dv-dashed", "+ Category");
  add.type = "button";
  add.addEventListener("click", () => dvGo("feeds"));
  row.append(add);
  scroll.append(row);
  const shown = dv.cat ? cards.filter((c) => c.category === dv.cat) : cards;
  if (!shown.length) return dvNothingYet(scroll);
  const list = mkEl("div", "dv-list dv-papers");
  for (const c of shown) {
    const r = mkEl("div", "dv-row dv-paper");
    const t = mkEl("div", "dv-row-text");
    const title = mkEl("button", "dv-title-btn dv-row-title", c.title);
    title.type = "button";
    title.addEventListener("click", () => dvGo("story/" + encodeURIComponent(c.id)));
    t.append(title);
    if (c.authors) t.append(mkEl("span", "dv-authors", c.authors));
    t.append(mkEl("span", "dv-id", (c.arxiv ? "arXiv:" + c.arxiv : c.url) + (c.category ? " · " + c.category : "")));
    const acts = mkEl("div", "dv-actions dv-tight");
    acts.append(dvSaveBtn(c), dvAskBtn(c));
    if (c.pdf) {
      const a = mkEl("a", "dv-btn dv-flat dv-sm", "PDF");
      a.href = dvHref(c.pdf);
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.insertAdjacentHTML("beforeend", DV_ICON.ext);
      acts.append(a);
    }
    r.append(t, acts);
    list.append(r);
  }
  scroll.append(list);
}

function dvVideo(scroll, cards) {
  const d = dv.data;
  if (d.hosted) {
    const n = mkEl("div", "dv-note");
    n.setAttribute("role", "note");
    n.innerHTML = DV_ICON.warn;
    const t = mkEl("div");
    t.append(mkEl("div", "dv-note-h", d.youtube_on_server || ""),
      mkEl("div", "dv-note-p", "Your video feeds stay listed in Feeds and are read by the desktop app."));
    n.append(t);
    scroll.append(n);
  }
  if (!d.buckets.video) scroll.append(mkEl("p", "dv-intro", "The Video bucket is switched off. Switch it on in Feeds to read your channels."));
  if (!cards.length) return d.hosted ? undefined : dvNothingYet(scroll);
  const grid = mkEl("div", "dv-videos" + (d.hosted ? " dv-off" : ""));
  for (const c of cards) {
    const v = mkEl("article", "dv-video");
    const th = mkEl("div", "dv-thumb");
    th.innerHTML = DV_ICON.play;
    if (c.cover) th.append(dvCover(c.cover));
    if (c.duration) th.append(mkEl("span", "dv-dur", dvDuration(c.duration)));
    const title = mkEl("button", "dv-title-btn dv-row-title", c.title);
    title.type = "button";
    title.disabled = d.hosted;
    title.addEventListener("click", () => dvGo("story/" + encodeURIComponent(c.id)));
    const acts = mkEl("div", "dv-actions dv-tight");
    const read = dvBtn("Read transcript", "dv-flat dv-sm", () => dvGo("story/" + encodeURIComponent(c.id)));
    read.disabled = d.hosted;
    const save = dvSaveBtn(c);
    save.disabled = d.hosted;
    acts.append(read, save);
    v.append(th, mkEl("span", "dv-meta", [c.channel, c.written ? "transcribed" : ""].filter(Boolean).join(" · ")), title, acts);
    grid.append(v);
  }
  scroll.append(grid);
}

function dvDuration(s) {
  s = Math.round(Number(s) || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

async function dvSave(card, btn) {
  if (card.saved) { openNote(card.saved); return; }
  if (btn) btn.disabled = true;
  let r;
  try { r = await apiPost("/discover/save", { id: card.id }); }
  catch (e) { notify("couldn't save the story: " + e.message); if (btn) btn.disabled = false; return; }
  card.saved = r.path;
  if (dv.data) dv.data.saved = (dv.data.saved || 0) + 1;
  dvToast(card, r.path);
  loadVaultInfo(); // the tree's Inbox gains the note
  if (dvRoute().screen === "story") dvRenderStory();
  else if (dv.data) dvRenderFront();
}

function dvToast(card, path) {
  document.querySelector(".dv-toast")?.remove();
  clearTimeout(dv.toast);
  const t = mkEl("div", "dv-toast");
  t.setAttribute("role", "status");
  t.innerHTML = DV_ICON.check;
  const txt = mkEl("div");
  txt.append(mkEl("span", "dv-th", "Saved to Inbox"), mkEl("span", "dv-tp", path + " · cited, articles listed"));
  const undo = dvBtn("Undo", "dv-flat", async () => {
    try { await apiPost("/discover/unsave", { id: card.id, path }); }
    catch (e) { notify("couldn't undo: " + e.message); return; }
    card.saved = "";
    if (dv.data) dv.data.saved = Math.max(0, (dv.data.saved || 1) - 1);
    t.remove();
    loadVaultInfo();
    if (dvRoute().screen === "story") dvRenderStory();
    else if (dv.data) dvRenderFront();
  });
  const nuc = dvBtn("Nucleate now", "", () => { t.remove(); dvNucleate(path); });
  t.append(txt, undo, nuc);
  $("#view-discover").append(t);
  dv.toast = setTimeout(() => t.remove(), 12000);
}

function dvNucleate(path) {
  showTab("chat");
  send(`/nucleate "${path}"`);
}

async function dvOpenStory(id) {
  if (!dv.story || dv.story.id !== id) {
    dv.story = null;
    dv.scope = "story";
    $("#dv-loading").hidden = false;
  }
  let s;
  try { s = await api("/discover/story/" + encodeURIComponent(id)); }
  catch (e) {
    $("#dv-loading").hidden = true;
    const box = mkEl("div", "dv-empty");
    box.append(mkEl("strong", "", "That story is no longer kept"),
      mkEl("span", "", "Unsaved stories are kept for 14 days."), dvBtn("Back to Discover", "", () => dvGo("")));
    dvBody().replaceChildren(box);
    return;
  }
  $("#dv-loading").hidden = true;
  dv.story = s;
  // Written the first time it is opened (ADR-0137 point 5): the page asks
  // once, and the writing state is what it shows until the text lands.
  if (!s.written && !s.writing && !(s.hosted && s.bucket === "video")) {
    try { const w = await apiPost(`/discover/story/${encodeURIComponent(id)}/write`); s.writing = w.writing; }
    catch (e) { s.writing = { error: e.message, read: 0, of: 0 }; }
  }
  dvRenderStory();
  if (s.writing && s.writing.running) {
    dv.storyPoll = setTimeout(() => { if (activeTab === "discover" && dvRoute().id === id) dvOpenStory(id); }, DV_POLL_MS);
  }
}

// A site's name as a chip says it: bbc.co.uk is "bbc", theguardian.com is
// "theguardian", news.ycombinator.com is "ycombinator". The short labels at the
// end are the suffix (com, co, uk), not the site.
function dvSite(domain) {
  const parts = String(domain || "").split(".");
  while (parts.length > 1 && parts[parts.length - 1].length <= 3) parts.pop();
  return parts[parts.length - 1] || domain;
}

function dvCiteText(p, text, sources) {
  // [n] and [n, m] become chips naming the article, linked to it. A full stop
  // or comma right after a marker goes before its chip, where it reads.
  const parts = text.split(/(\[\d+(?:, \d+)*\])/);
  const isMark = (x) => /^\[\d+(?:, \d+)*\]$/.test(x);
  let inRun = false;
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    const m = part.match(/^\[(\d+(?:, \d+)*)\]$/);
    if (!m) {
      if (part) { p.append(document.createTextNode(part)); inRun = false; }
      continue;
    }
    if (!inRun) {
      // The first marker of a run ([1][2].): the stop after the whole run
      // moves in front of it, once.
      let j = i + 1;
      while (j < parts.length && (parts[j] === "" || isMark(parts[j]))) j++;
      const punct = (parts[j] || "").match(/^[.,;:]/);
      if (punct) {
        const last = p.lastChild;
        if (last && last.nodeType === 3) last.textContent = last.textContent.replace(/\s+$/, "");
        p.append(document.createTextNode(punct[0]));
        parts[j] = parts[j].slice(1);
      }
      inRun = true;
    }
    const ns = m[1].split(/,\s*/).map(Number);
    const first = sources.find((s) => s.n === ns[0]);
    if (!first) continue;
    const a = mkEl("a", "dv-cite", dvSite(first.domain) + (ns.length > 1 ? ` +${ns.length - 1}` : ""));
    a.href = dvHref(first.url);
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = ns.map((n) => { const s = sources.find((x) => x.n === n); return s ? `[${n}] ${s.title} (${s.domain})` : ""; }).join("\n");
    const last = p.lastChild;
    if (!last || last.nodeType !== 3 || !/\s$/.test(last.textContent)) p.append(" ");
    p.append(a);
  }
}

function dvRenderStory() {
  const s = dv.story;
  if (!s) return;
  dvPopClose();
  const root = mkEl("div");
  root.style.cssText = "display:contents";
  const head = mkEl("header", "dv-head dv-bar");
  // Written, the count is what its sentences cite: only READ_MAX articles are
  // read, so the story's own count can name articles nothing was checked against.
  const w0 = s.written;
  const cited = w0 ? new Set(w0.sections.flatMap((x) => x.paras.flatMap((p) => p.cites))) : null;
  const count = w0
    ? dvChecked("Every sentence cites a quote found in one of these articles.",
      [...w0.sources.filter((x) => cited.has(x.n)).map((x) => ({ src: x, cited: true })),
        ...w0.sources.filter((x) => !cited.has(x.n)).map((x) => ({ src: x, cited: false }))])
    : mkEl("span", "dv-meta", `${s.articles} article${s.articles === 1 ? "" : "s"}`);
  head.append(dvBtn("Discover", "dv-flat", () => dvGo(""), DV_ICON.back), mkEl("div", "dv-spacer"), count,
    dvBtn("Ask in Chat", "", () => dvToChat(s, "", dv.scope), DV_ICON.ask));
  const save = dvBtn(s.saved ? "Saved to Inbox" : "Save to Inbox", s.saved ? "dv-saved" : "dv-primary", () => dvSave(s, save), DV_ICON.save);
  if (s.saved) save.title = "open " + s.saved;
  head.append(save);
  const scroll = mkEl("div", "dv-scroll");
  scroll.style.padding = "0";
  const wrap = mkEl("div", "dv-story");
  const art = mkEl("article", "dv-article");
  art.append(mkEl("span", "dv-meta", s.kicker), mkEl("h1", "", s.title));
  if (s.cover) {
    const fig = mkEl("figure");
    fig.append(dvCover(s.cover), mkEl("figcaption", "", "Cover: " + (s.cover_from || "the first article")));
    art.append(fig);
  }
  const w = s.written;
  if (w) dvStoryText(art, s, w);
  else dvStoryState(art, s);
  art.append(dvComposer(s));
  wrap.append(art, dvStoryAside(s));
  scroll.append(wrap);
  root.append(head, scroll);
  dvBody().replaceChildren(root);
}

function dvStoryText(art, s, w) {
  if (s.grew) {
    const g = mkEl("div", "dv-sec-foot");
    g.append(mkEl("span", "", `${s.grew} new article${s.grew === 1 ? "" : "s"} since this was written`));
    const re = mkEl("button", "dv-link", "Rewrite");
    re.type = "button";
    re.style.fontSize = "12px";
    re.addEventListener("click", async () => {
      try { await apiPost(`/discover/story/${encodeURIComponent(s.id)}/write`); s.written = null; dvOpenStory(s.id); }
      catch (e) { notify("couldn't rewrite: " + e.message); }
    });
    g.append(re);
    art.append(g);
  }
  w.sections.forEach((sec, i) => {
    if (sec.title) art.append(mkEl("h2", "", sec.title));
    for (const para of sec.paras) {
      const p = mkEl("p", "dv-para");
      dvCiteText(p, para.text, w.sources);
      art.append(p);
    }
    if (sec.title) {
      const foot = mkEl("div", "dv-sec-foot");
      const ids = new Set(sec.paras.flatMap((p) => p.cites));
      foot.append(dvChecked("This section's sentences cite these articles.",
        w.sources.filter((x) => ids.has(x.n)).map((x) => ({ src: x, cited: true }))));
      const copy = mkEl("button", "dv-link", "Copy section");
      copy.type = "button";
      copy.style.cssText = "font-size:12px;color:var(--ash)";
      copy.addEventListener("click", async () => {
        const text = sec.title + "\n\n" + sec.paras.map((x) => x.text).join("\n\n") + "\n\n"
          + w.sources.filter((x) => sec.paras.some((p) => p.cites.includes(x.n))).map((x) => `[${x.n}] ${x.title} ${x.url}`).join("\n");
        try { await navigator.clipboard.writeText(text); copy.textContent = "Copied"; }
        catch { copy.textContent = "Could not copy"; }
        setTimeout(() => (copy.textContent = "Copy section"), 1400);
      });
      foot.append(copy);
      art.append(foot);
    }
  });
  if (w.dropped) art.append(mkEl("span", "dv-meta", `${w.dropped} sentence${w.dropped === 1 ? "" : "s"} left out: no article supported ${w.dropped === 1 ? "it" : "them"}.`));
}

function dvSourceGrid(s, w) {
  const grid = mkEl("div", "dv-sources");
  const list = s.list || [];
  const shown = list.slice(0, 4);
  for (const a of shown) {
    const x = mkEl("a", "dv-source");
    x.href = dvHref(a.url);
    x.target = "_blank";
    x.rel = "noopener noreferrer";
    x.append(mkEl("span", "dv-sd", a.domain), mkEl("span", "dv-st", a.title));
    grid.append(x);
  }
  if (list.length > shown.length) {
    const more = mkEl("button", "dv-source dv-more", `+${list.length - shown.length} more`);
    more.type = "button";
    more.addEventListener("click", () => {
      more.remove();
      for (const a of list.slice(shown.length)) {
        const x = mkEl("a", "dv-source");
        x.href = dvHref(a.url);
        x.target = "_blank";
        x.rel = "noopener noreferrer";
        x.append(mkEl("span", "dv-sd", a.domain), mkEl("span", "dv-st", a.title));
        grid.append(x);
      }
    });
    grid.append(more);
  }
  return grid;
}

// --- the sources a story checked against -------------------------------------------
// One popover at a time, on the body: #view-discover is a size container, which
// makes it the containing block of a fixed descendant, so a popover inside the
// view would be placed against it and cut by its scroll.
let dvPop = null;

function dvPopClose(refocus) {
  if (!dvPop) return;
  const { el, btn } = dvPop;
  dvPop = null;
  el.remove();
  btn.setAttribute("aria-expanded", "false");
  if (refocus) btn.focus();
}

document.addEventListener("pointerdown", (e) => {
  if (dvPop && !dvPop.el.contains(e.target) && !dvPop.btn.contains(e.target)) dvPopClose();
}, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && dvPop) { e.stopPropagation(); dvPopClose(true); } }, true);
// Placed once against the button: a scroll or a resize would leave it pointing at nothing.
document.addEventListener("scroll", (e) => { if (dvPop && !dvPop.el.contains(e.target)) dvPopClose(); }, true);
window.addEventListener("resize", () => dvPopClose());

function dvIcon(domain) {
  const img = mkEl("img", "dv-ico");
  img.src = "/discover/icon/" + encodeURIComponent(domain);
  img.alt = ""; // the domain is written beside it
  img.width = img.height = 20;
  img.loading = "lazy";
  img.decoding = "async";
  const mono = () => img.replaceWith(mkEl("span", "dv-ico dv-ico-mono", dvSite(domain).charAt(0).toUpperCase()));
  img.addEventListener("error", mono);
  // techcrunch.com's /favicon.ico is 16x16 with every pixel transparent (2026-11-02):
  // a blank tile names no site, the initial does. Same origin, so the canvas reads it.
  img.addEventListener("load", () => {
    const g = Object.assign(document.createElement("canvas"), { width: 16, height: 16 }).getContext("2d");
    g.drawImage(img, 0, 0, 16, 16);
    if (!g.getImageData(0, 0, 16, 16).data.some((v, i) => i % 4 === 3 && v)) mono();
  });
  return img;
}

// rows: [{src: {n, url, title, domain}, cited}], the cited first; the label
// counts the cited rows, so it never names more than the list shows.
function dvChecked(head, rows) {
  const n = rows.filter((r) => r.cited).length;
  const b = mkEl("button", "dv-checked");
  b.type = "button";
  b.setAttribute("aria-expanded", "false");
  b.setAttribute("aria-haspopup", "dialog");
  b.innerHTML = DV_ICON.tick;
  b.append(document.createTextNode(`Checked against ${n} article${n === 1 ? "" : "s"}`));
  b.insertAdjacentHTML("beforeend", DV_ICON.chev);
  b.addEventListener("click", () => {
    const mine = dvPop && dvPop.btn === b;
    dvPopClose();
    if (!mine) dvPopOpen(b, head, rows);
  });
  return b;
}

function dvPopOpen(btn, head, rows) {
  const el = mkEl("div", "dv-pop");
  el.id = "dv-pop";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", btn.textContent);
  const ul = mkEl("ul");
  for (const { src, cited } of rows) {
    const a = mkEl("a");
    a.href = dvHref(src.url);
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const t = mkEl("span", "dv-pop-t", src.title || src.domain);
    t.insertAdjacentHTML("beforeend", DV_ICON.ext);
    a.append(dvIcon(src.domain), t, mkEl("span", "dv-pop-d", cited ? src.domain : src.domain + " · read, cited nowhere"));
    const li = mkEl("li", cited ? "" : "dv-uncited");
    li.append(a);
    ul.append(li);
  }
  el.append(mkEl("div", "dv-pop-h", head), ul, mkEl("div", "dv-pop-f", "Each title opens the publisher's page in a new tab."));
  // Focus moving out of the popover closes it, except onto its own button, whose click toggles it.
  el.addEventListener("focusout", (e) => { if (e.relatedTarget && !el.contains(e.relatedTarget) && e.relatedTarget !== btn) dvPopClose(); });
  document.body.append(el);
  btn.setAttribute("aria-controls", el.id);
  btn.setAttribute("aria-expanded", "true");
  dvPop = { el, btn };
  const r = btn.getBoundingClientRect();
  const w = el.offsetWidth, h = el.offsetHeight;
  const below = innerHeight - r.bottom >= r.top || innerHeight - r.bottom - 14 >= h;
  const top = below ? r.bottom + 6 : r.top - 6 - h;
  let left = r.left;
  if (left + w > innerWidth - 8) left = r.right - w;
  el.style.top = Math.max(8, Math.min(top, innerHeight - h - 8)) + "px";
  el.style.left = Math.max(8, Math.min(left, innerWidth - w - 8)) + "px";
  const first = el.querySelector("a");
  if (first) first.focus({ preventScroll: true });
}

function dvStoryState(art, s) {
  const st = s.writing || {};
  if (s.hosted && s.bucket === "video") {
    art.append(dvEmptyNote(s.youtube_on_server));
    art.append(dvSourceGrid(s, null));
    return;
  }
  if (st.error) {
    const box = mkEl("div", "dv-note");
    box.innerHTML = DV_ICON.warn;
    const t = mkEl("div");
    t.append(mkEl("div", "dv-note-h", "The story was not written: " + st.error));
    const again = mkEl("button", "dv-link", "Try again");
    again.type = "button";
    again.addEventListener("click", async () => {
      try { await apiPost(`/discover/story/${encodeURIComponent(s.id)}/write`); dvOpenStory(s.id); }
      catch (e) { notify("couldn't start writing: " + e.message); }
    });
    t.append(mkEl("div", "dv-note-p", "Its articles are listed below; you can still save them or ask about them."), again);
    box.append(t);
    art.append(box, dvSourceGrid(s, null));
    return;
  }
  const prog = mkEl("div", "dv-progress");
  prog.style.padding = "0";
  prog.setAttribute("role", "status");
  const track = mkEl("div", "dv-track");
  const fill = mkEl("div");
  const of = st.of || s.articles;
  const done = st.phase === "articles" || !st.phase ? st.read || 0 : of;
  fill.style.width = Math.max(6, Math.round((100 * done) / Math.max(of, 1)) * (st.phase === "sections" ? 1 : 0.8)) + "%";
  track.append(fill);
  const label = st.phase === "quotes" ? `Quoting the articles · ${of} of ${of} read`
    : st.phase === "sections" ? "Writing the sections" : `Writing the story · ${st.read || 0} of ${of} articles read`;
  prog.append(track, mkEl("span", "dv-meta-strong", label));
  const sk = mkEl("div", "dv-skeleton");
  sk.setAttribute("aria-hidden", "true");
  for (const wpct of [96, 91, 64, 0, 88, 93, 41]) {
    const line = mkEl("div");
    if (!wpct) { line.style.cssText = "height:10px;background:none;animation:none"; }
    else line.style.width = wpct + "%";
    sk.append(line);
  }
  art.append(prog, sk, mkEl("p", "dv-meta", "A story is written the first time you open it, from its articles only. Every sentence keeps the article it came from."));
}

function dvEmptyNote(text) {
  const box = mkEl("div", "dv-note");
  box.innerHTML = DV_ICON.warn;
  box.append(mkEl("div", "dv-note-h", text));
  return box;
}

function dvComposer(s) {
  const c = mkEl("div", "dv-composer");
  const qs = (s.written && s.written.questions) || [];
  if (qs.length) {
    const sug = mkEl("div", "dv-suggest");
    for (const q of qs) {
      const b = mkEl("button", "dv-chip", q);
      b.type = "button";
      b.addEventListener("click", () => dvToChat(s, q, dv.scope));
      sug.append(b);
    }
    c.append(sug);
  }
  const box = mkEl("div", "dv-box");
  const ta = mkEl("textarea");
  ta.rows = 1;
  ta.placeholder = "Ask a follow-up about this story";
  ta.setAttribute("aria-label", "ask a follow-up about this story");
  const row = mkEl("div", "dv-box-row");
  row.append(mkEl("span", "dv-meta", "Answer from"));
  // One choice, not the drafts' three toggles: a turn is either a /web turn or
  // a vault turn, and This story is the articles alone (ADR-0137 point 8).
  const seg = mkEl("div", "dv-scope");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "answer from");
  for (const [k, label] of DV_SCOPES) {
    const b = mkEl("button", "", label);
    b.type = "button";
    b.setAttribute("aria-pressed", dv.scope === k ? "true" : "false");
    b.title = { story: "only this story's articles", web: "this story, then the web", vault: "this story and your notes" }[k];
    b.addEventListener("click", () => {
      dv.scope = k;
      for (const x of seg.children) x.setAttribute("aria-pressed", x === b ? "true" : "false");
    });
    seg.append(b);
  }
  const go = mkEl("button", "dv-send");
  go.type = "button";
  go.innerHTML = DV_ICON.send;
  go.setAttribute("aria-label", "ask in Chat");
  go.disabled = true;
  ta.addEventListener("input", () => {
    go.disabled = !ta.value.trim();
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  });
  const fire = () => { if (ta.value.trim()) dvToChat(s, ta.value.trim(), dv.scope); };
  go.addEventListener("click", fire);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fire(); } });
  row.append(seg, mkEl("div", "dv-spacer"), go);
  box.append(ta, row);
  c.append(box);
  return c;
}

function dvStoryAside(s) {
  const aside = mkEl("aside", "dv-aside");
  aside.setAttribute("aria-label", "beside the story");
  const vault = mkEl("section", "dv-panel");
  vault.append(mkEl("h2", "", "In your vault"));
  if ((s.near || []).length) {
    for (const n of s.near) {
      const b = mkEl("button", "dv-link", n.name);
      b.type = "button";
      b.title = "open " + n.path;
      b.addEventListener("click", () => openNote(n.path));
      vault.append(b);
    }
    vault.append(mkEl("span", "dv-meta", "Opens in the note drawer, beside the story."));
  } else {
    vault.append(mkEl("p", "", "No note is near this story yet. Saving it starts one in your Inbox."));
  }
  aside.append(vault);
  const bg = (s.written && s.written.background) || [];
  if (bg.length) {
    const sec = mkEl("section", "dv-panel");
    sec.append(mkEl("h2", "", "Background"));
    for (const b of bg) {
      const a = mkEl("a", "dv-meta", `${b.host} · Reference bucket`);
      a.href = dvHref(b.url);
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      sec.append(mkEl("span", "dv-bg-t", b.title), mkEl("p", "", b.extract), a);
    }
    aside.append(sec);
  }
  if ((s.related || []).length) {
    const sec = mkEl("section", "dv-panel");
    sec.style.gap = "10px";
    sec.append(mkEl("h2", "", "Related"));
    for (const r of s.related) {
      const b = mkEl("button", "dv-rel", r.title);
      b.type = "button";
      b.addEventListener("click", () => dvGo("story/" + encodeURIComponent(r.id)));
      sec.append(b);
    }
    aside.append(sec);
  }
  return aside;
}

// --- the story chip in Chat (ADR-0137 point 8, ADR-0097's note chip) ------------------
// The story rides every message of the chat it was sent to until its x is
// pressed or another chat opens: the follow-ups are about it too.
let dvChip = null;

function chatStory() { return dvChip; }

function dvToChat(s, text, scope) {
  dvChip = { id: s.id, title: s.title, articles: s.articles, scope: scope || "story", sid: undefined };
  showTab("chat");
  renderStoryChip();
  if (text) send(text);
  else $("#input").focus();
}

function renderStoryChip() {
  let row = $("#story-chip");
  if (!row) {
    row = mkEl("div", "dv-chip-row");
    row.id = "story-chip";
    $("#note-chip").before(row);
  }
  row.replaceChildren();
  row.hidden = !dvChip;
  if (!dvChip) return;
  const chip = mkEl("span", "chip note-chip story-chip");
  chip.innerHTML = DV_ICON.compass;
  const scope = DV_SCOPES.find(([k]) => k === dvChip.scope)?.[1] || "This story";
  chip.append(mkEl("span", "chip-name", `Story · ${dvChip.title} · ${dvChip.articles} articles`));
  chip.title = `each message reads this story's articles while it stays here; answers from: ${scope}`;
  const x = mkEl("button", "chip-x");
  x.type = "button";
  x.setAttribute("aria-label", "remove the story from this chat");
  x.innerHTML = '<svg aria-hidden="true" focusable="false" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"></line><line x1="18" y1="6" x2="6" y2="18"></line></svg>';
  x.addEventListener("click", () => { dvChip = null; renderStoryChip(); $("#input").focus(); });
  chip.append(x);
  row.append(chip);
}

// Under the question, as its note is: the story the turn read.
function userStory(body, story) {
  if (!body || !story || !story.id) return;
  body.dataset.story = story.id;
  const row = mkEl("div", "msg-note msg-story");
  const chip = mkEl("span", "mf");
  chip.append(mkEl("span", "mf-in", "with story"));
  const name = mkEl("button", "mf-name", story.title);
  name.type = "button";
  name.title = "open the story in Discover";
  name.addEventListener("click", () => { location.hash = "#discover/story/" + encodeURIComponent(story.id); });
  chip.append(name);
  row.append(chip);
  body.after(row);
}

// "Save answer to Inbox" on an answer that followed a story turn (point 9).
function dvMarkAnswers() {
  for (const q of document.querySelectorAll("#log .msg.user .body[data-story]")) {
    const user = q.closest(".msg");
    let answer = user.nextElementSibling;
    while (answer && !answer.classList.contains("msg")) answer = answer.nextElementSibling;
    if (!answer || !answer.classList.contains("silica")) continue;
    const body = answer.querySelector(".body");
    if (!body || body.querySelector(".dv-save-answer")) continue;
    const b = mkEl("button", "copy-btn dv-save-answer", "save to Inbox");
    b.type = "button";
    b.title = "save this answer and the story's articles to the Inbox";
    b.addEventListener("click", async () => {
      const clone = body.cloneNode(true);
      for (const x of clone.querySelectorAll(".ledger, .copy-btn, details, .turn-outcome")) x.remove();
      b.disabled = true;
      try {
        const r = await apiPost("/discover/save", { id: q.dataset.story, answer: clone.innerText.trim(), question: q.dataset.words || q.textContent });
        b.textContent = "saved";
        notify("saved to " + r.path, "info", { label: "open", run: () => openNote(r.path) });
        loadVaultInfo();
      } catch (e) { b.disabled = false; notify("couldn't save the answer: " + e.message); }
    });
    (body.querySelector(":scope > .ledger") || body).appendChild(b);
  }
}

document.addEventListener("silica:turn-end", () => {
  if (dvChip && dvChip.sid === undefined) dvChip.sid = currentSessionId;
});
// Another chat, or a new one, leaves the story behind.
document.addEventListener("silica:session", () => {
  if (dvChip && dvChip.sid !== undefined && dvChip.sid !== currentSessionId) { dvChip = null; renderStoryChip(); }
});
for (const id of ["#new-chat", "#rail-new"]) $(id).addEventListener("click", () => { dvChip = null; renderStoryChip(); });

async function dvLoadFeeds() {
  $("#dv-loading").hidden = !dv.feeds;
  try { dv.feeds = await api("/discover/feeds"); }
  catch (e) { $("#dv-loading").hidden = true; dvBody().replaceChildren(dvEmpty("Feeds could not load", e.message)); return; }
  $("#dv-loading").hidden = true;
  if (dv.catalogOpen && !dv.catalog) await dvLoadCatalog("");
  dvRenderFeeds();
}

async function dvLoadCatalog(q) {
  try { dv.catalog = await api("/discover/catalog?q=" + encodeURIComponent(q)); }
  catch (e) { notify("couldn't open the catalog: " + e.message); }
}

function dvRenderFeeds() {
  const f = dv.feeds;
  const root = mkEl("div");
  root.style.cssText = "display:contents";
  const head = mkEl("header", "dv-head dv-bar");
  const h = mkEl("h1", "", "Feeds");
  h.style.marginLeft = "4px";
  const opmlIn = mkEl("input");
  opmlIn.type = "file";
  opmlIn.accept = ".opml,.xml,text/x-opml,text/xml,application/xml";
  opmlIn.hidden = true;
  opmlIn.addEventListener("change", async () => {
    const file = opmlIn.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await api("/discover/opml", { method: "POST", body: fd });
      notify(`imported ${r.added} of ${r.listed} feeds`, "info");
      dvLoadFeeds();
    } catch (e) { notify("couldn't import that file: " + e.message); }
    opmlIn.value = "";
  });
  const cat = dvBtn("Browse catalog", "", async () => {
    dv.catalogOpen = !dv.catalogOpen;
    if (dv.catalogOpen && !dv.catalog) await dvLoadCatalog("");
    dvRenderFeeds();
  });
  cat.setAttribute("aria-pressed", dv.catalogOpen ? "true" : "false");
  if (dv.catalogOpen) cat.style.background = "var(--sel)";
  head.append(dvBtn("Discover", "dv-flat", () => dvGo(""), DV_ICON.back), h, mkEl("div", "dv-spacer"),
    dvBtn("Import OPML", "", () => opmlIn.click()), opmlIn, cat);
  const scroll = mkEl("div", "dv-scroll");
  scroll.style.padding = "0";
  const wrap = mkEl("div", "dv-feeds");
  const main = mkEl("div", "dv-feeds-main");
  main.append(dvAddForm(f));
  for (const b of f.buckets) main.append(dvBucket(b));
  const side = mkEl("div", "dv-feeds-side");
  if (dv.catalogOpen) side.append(dvCatalogPanel());
  side.append(dvWhenPanel(f));
  wrap.append(main, side);
  scroll.append(wrap);
  root.append(head, scroll);
  dvBody().replaceChildren(root);
}

function dvAddForm(f) {
  const form = mkEl("form", "dv-add");
  const row = mkEl("div", "dv-add-row");
  const field = (cls, label, input) => { const l = mkEl("label", "dv-field " + cls, label); l.append(input); return l; };
  const url = mkEl("input");
  url.type = "text";
  url.placeholder = "A site, a feed, a YouTube channel or an arXiv category";
  url.autocomplete = "off";
  const bucket = mkEl("select");
  for (const b of f.buckets) {
    const o = mkEl("option", "", b.label);
    o.value = b.key;
    bucket.append(o);
  }
  const tags = mkEl("input");
  tags.type = "text";
  tags.placeholder = "AI, law";
  const add = dvBtn("Add", "dv-primary");
  add.type = "submit";
  row.append(field("dv-grow", "Add a feed", url), field("dv-bucket", "Bucket", bucket), field("dv-tags", "Tags", tags), add);
  const msg = mkEl("span", "dv-add-msg", f.hosted
    ? "A site's feed is found for you. Here, a site with no feed cannot be added: the hosted pilot reads feeds only."
    : "A site's feed is found for you. A site with none is read through search, which is slower and can be refused.");
  msg.setAttribute("role", "status");
  form.append(row, msg);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!url.value.trim()) { url.focus(); return; }
    add.disabled = true;
    msg.className = "dv-add-msg";
    msg.textContent = "Looking for the feed…";
    try {
      const r = await apiPost("/discover/feeds", { url: url.value.trim(), bucket: bucket.value, tags: tags.value });
      msg.textContent = `Added ${r.feed.name} (${r.feed.kind}). It is read with the next refresh.`;
      url.value = "";
      tags.value = "";
      dv.feeds = await api("/discover/feeds");
      dvRenderFeeds();
    } catch (err) {
      msg.className = "dv-add-msg dv-err";
      msg.textContent = err.message;
      add.disabled = false;
    }
  });
  return form;
}

function dvBucket(b) {
  const sec = mkEl("section");
  const h = mkEl("div", "dv-bucket-h");
  const desc = { news: "grouped into stories", research: "arXiv categories", reference: "looked up for Background, never a feed",
    video: "YouTube channels, transcribed" }[b.key] || "a catalog topic, grouped into stories";
  h.append(mkEl("h2", "", b.label), mkEl("span", "dv-meta", `${b.rows.length} feed${b.rows.length === 1 ? "" : "s"} · ${desc}`), mkEl("div", "dv-spacer"));
  if (b.key.startsWith("c:")) {
    const rm = dvBtn("Remove", "dv-flat dv-sm", async () => {
      try { await apiPost("/discover/catalog/remove", { name: b.label }); dv.catalog = null; dvLoadFeeds(); }
      catch (e) { notify("couldn't remove that topic: " + e.message); }
    });
    h.append(rm);
  }
  const table = mkEl("div", "dv-table" + (b.on ? "" : " dv-off"));
  h.append(dvSwitch(b.on, "read the " + b.label + " bucket", async (on) => {
    try { await apiPost("/discover/buckets", { key: b.key, on }); b.on = on; table.classList.toggle("dv-off", !on); dv.data = null; }
    catch (e) { notify("couldn't change that bucket: " + e.message); }
  }));
  for (const r of b.rows) {
    const row = mkEl("div", "dv-feed");
    const name = mkEl("div", "dv-feed-name");
    name.append(mkEl("span", "", r.name), mkEl("span", "dv-feed-url", r.url.replace(/^https?:\/\//, "")));
    const tags = mkEl("span", "dv-feed-tags");
    for (const t of r.tags) tags.append(mkEl("span", "dv-tag", t));
    const st = mkEl("span", "dv-feed-state" + (r.bad === "err" ? " dv-err" : r.bad === "warn" ? " dv-warn" : ""), r.status);
    const rm = dvBtn("", "dv-flat dv-sm dv-icon", async () => {
      try { await apiPost("/discover/feeds/remove", { id: r.id }); dvLoadFeeds(); }
      catch (e) { notify("couldn't remove the feed: " + e.message); }
    });
    rm.innerHTML = '<svg aria-hidden="true" focusable="false" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"></line><line x1="18" y1="6" x2="6" y2="18"></line></svg>';
    rm.setAttribute("aria-label", "remove " + r.name);
    rm.title = "remove this feed";
    row.append(name, mkEl("span", "dv-feed-kind", r.kind), tags, st, rm);
    table.append(row);
  }
  if (!b.rows.length) table.append(mkEl("div", "dv-feed dv-meta", "No feed in this bucket yet."));
  sec.append(h, table);
  return sec;
}

function dvCatalogPanel() {
  const sec = mkEl("section", "dv-panel");
  sec.style.gap = "10px";
  sec.style.borderColor = "var(--line-2)";
  const meta = (dv.catalog && dv.catalog.meta) || dv.feeds.catalog;
  sec.append(mkEl("h2", "", "Catalog"),
    mkEl("p", "", `Kagi News's community list, MIT licence: ${meta.topics} topics, ${fmtNum(meta.feeds)} feeds. Adding a topic adds its feeds as one bucket.`));
  const search = mkEl("input", "dv-search");
  search.type = "text";
  search.placeholder = "search topics, regions and cities";
  search.setAttribute("aria-label", "search the catalog");
  let timer = 0;
  const list = mkEl("div");
  list.style.cssText = "display:flex;flex-direction:column;gap:8px";
  const paint = () => {
    list.replaceChildren();
    for (const t of (dv.catalog && dv.catalog.topics) || []) {
      const row = mkEl("div", "dv-cat");
      const b = dvBtn(t.added ? "Added" : "Add", "dv-sm" + (t.added ? " dv-saved" : ""), async () => {
        b.disabled = true;
        try {
          if (t.added) await apiPost("/discover/catalog/remove", { name: t.name });
          else await apiPost("/discover/catalog/add", { name: t.name });
          t.added = !t.added;
          dv.feeds = await api("/discover/feeds");
          dv.data = null;
          dvRenderFeeds();
        } catch (e) { notify("couldn't change that topic: " + e.message); b.disabled = false; }
      });
      row.append(mkEl("span", "", t.name), mkEl("span", "dv-meta", `${t.feeds} feeds`), b);
      list.append(row);
    }
    if (!list.children.length) list.append(mkEl("span", "dv-meta", "No topic by that name."));
  };
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => { await dvLoadCatalog(search.value.trim()); paint(); }, 200);
  });
  paint();
  sec.append(search, list);
  return sec;
}

function dvWhenPanel(f) {
  const sec = mkEl("section", "dv-panel");
  sec.style.gap = "12px";
  const s1 = mkEl("div", "dv-setting");
  const t1 = mkEl("div");
  t1.append(mkEl("div", "dv-sl", "When I open Discover"), mkEl("div", "dv-meta", "At most once an hour. The refresh button reads again."));
  const on = dvSwitch(true, "read when Discover opens", () => {});
  on.disabled = true; // the one way it reads (ADR-0137 decision 3): shown, not a choice
  s1.append(t1, on);
  const s2 = mkEl("div", "dv-setting");
  s2.style.alignItems = "center";
  s2.append(mkEl("div", "dv-sl", "Keep unsaved stories"), mkEl("span", "dv-meta-strong", `${f.keep_days} days`));
  sec.append(mkEl("h2", "", "When Discover reads"), s1, s2);
  return sec;
}

async function dvFirstRun() {
  let s;
  try { s = await api("/discover/setup"); }
  catch (e) { dvBody().replaceChildren(dvEmpty("Discover could not load", e.message)); return; }
  if (!dv.firstPick) dv.firstPick = { buckets: { ...s.buckets }, topics: new Set(s.topics.map((t) => t.key)) };
  const pick = dv.firstPick;
  const wrap = mkEl("div", "dv-first");
  const inner = mkEl("div", "dv-first-in");
  const intro = mkEl("div");
  intro.style.cssText = "display:flex;flex-direction:column;gap:8px";
  intro.append(mkEl("h1", "", "Pick what Discover reads"),
    mkEl("p", "dv-first-p", "It reads these feeds when you open it and groups what they publish into stories. Nothing reaches your vault until you save a story."));
  const starters = mkEl("div", "dv-starters");
  const desc = {
    news: ["News", "Tech, science and world desks: TechCrunch, The Verge, Ars Technica, BBC, The Guardian, Hacker News and more.", `${s.counts.news} feeds`],
    research: ["Research", "The latest arXiv listing in the categories you pick.", "cs.CC, cs.LG to start"],
    reference: ["Reference", "Wikipedia, looked up for the people and ideas a story names.", "never a feed of its own"],
    video: ["Video", "YouTube channels you follow, read through their transcripts.", s.hosted ? VOCAB.discover.desktop_only : `${s.counts.video} channels to start`],
  };
  for (const k of ["news", "research", "reference", "video"]) {
    const [label, d, note] = desc[k];
    const b = mkEl("button", "dv-starter");
    b.type = "button";
    b.setAttribute("aria-pressed", pick.buckets[k] ? "true" : "false");
    const mark = mkEl("span", "dv-box-mark");
    mark.innerHTML = DV_ICON.tick;
    const txt = mkEl("span");
    const n = mkEl("span", "dv-meta", note);
    if (k === "video" && s.hosted) n.classList.add("dv-warn");
    txt.append(mkEl("span", "dv-sl", label), mkEl("span", "dv-sd", d), n);
    b.append(mark, txt);
    b.addEventListener("click", () => {
      pick.buckets[k] = !pick.buckets[k];
      b.setAttribute("aria-pressed", pick.buckets[k] ? "true" : "false");
    });
    starters.append(b);
  }
  const sugg = mkEl("div");
  sugg.style.cssText = "display:flex;flex-direction:column;gap:8px";
  sugg.append(mkEl("h2", "", "Suggested from your vault"));
  const chips = mkEl("div", "dv-topics");
  chips.style.marginBottom = "0";
  for (const t of s.topics) {
    const c = mkEl("button", "dv-chip", `${t.label} · ${t.why}`);
    c.type = "button";
    c.setAttribute("aria-pressed", pick.topics.has(t.key) ? "true" : "false");
    c.addEventListener("click", () => {
      if (pick.topics.has(t.key)) pick.topics.delete(t.key); else pick.topics.add(t.key);
      c.setAttribute("aria-pressed", pick.topics.has(t.key) ? "true" : "false");
    });
    chips.append(c);
  }
  if (!s.topics.length) chips.append(mkEl("span", "dv-meta", "None yet: a folder or tag needs three notes to be a topic."));
  sugg.append(chips, mkEl("span", "dv-meta", "Topics come from your folders and tags. For you ranks stories by them."));
  const acts = mkEl("div", "dv-actions");
  acts.style.paddingTop = "6px";
  const start = dvBtn("Start reading", "dv-primary", async () => {
    start.disabled = true;
    try { await apiPost("/discover/setup", { buckets: pick.buckets, topics: [...pick.topics] }); }
    catch (e) { notify("couldn't start Discover: " + e.message); start.disabled = false; return; }
    dv.firstPick = null;
    dv.data = null;
    dvGo("", false);
  });
  const browse = dvBtn(`Browse ${s.catalog.topics} topics instead`, "", async () => {
    try { await apiPost("/discover/setup", { buckets: { news: false, research: false, reference: true, video: false }, topics: [...pick.topics] }); }
    catch (e) { notify("couldn't start Discover: " + e.message); return; }
    dv.firstPick = null;
    dv.catalogOpen = true;
    dvGo("feeds");
  });
  acts.append(start, browse);
  inner.append(intro, starters, sugg, acts);
  wrap.append(inner);
  dvBody().replaceChildren(wrap);
}
