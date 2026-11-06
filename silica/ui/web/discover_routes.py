# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Discover view's routes (ADR-0137), an APIRouter mounted on the app.

The engine is silica/sources/discover; these routes shape its document for
the page. Nothing here writes to the vault except `save`, which writes one
findings note into the Inbox (ADR-0015).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from importlib import resources

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from silica.sources.discover import feeds as F
from silica.sources.discover import rank, read, store
from silica.sources.discover import story as S
from silica.ui.web import drop


router = APIRouter(prefix="/discover")
_COVER_NAME = re.compile(r"^[0-9a-f]{24}\.(?:jpg|png|webp|gif|avif)$")
# A host name and nothing else: it becomes both a fetched address and a file name.
_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_IMAGE_HEADERS = {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": "default-src 'none'; sandbox"}
_TABS = ("foryou", "news", "research", "video")
_FEEDBACK = (-1, 0, 1)
_CATALOG_SHOWN = 40


def _hosted() -> bool:
    from silica.config import hosted

    return hosted()


def _label(doc: dict, bucket: str) -> str:
    return store.BUCKET_LABEL.get(bucket) or (doc.get("bucket_labels") or {}).get(bucket) or bucket


def _tab_of(bucket: str) -> str:
    return bucket if bucket in ("research", "video") else "news"


def _age(ts: float | None) -> str:
    if not ts:
        return ""
    m = max(0, int((time.time() - ts) / 60))
    if m < 60:
        return f"{max(m, 1)} min ago"
    if m < 48 * 60:
        return f"{m // 60} h ago"
    return f"{m // 1440} days ago"


def _card(doc: dict, s: dict) -> dict:
    arts = [doc["articles"][a] for a in s["articles"] if a in doc["articles"]]
    a = arts[0] if arts else {"title": "", "lead": "", "url": ""}
    newest = max((x.get("published") or x.get("seen") or 0 for x in arts), default=0)
    tag = (s.get("tags") or [""])[0]
    domains = list(dict.fromkeys(x.get("domain", "") for x in arts))
    card = {
        "id": s["id"], "bucket": s["bucket"], "title": a["title"], "lead": a.get("lead", ""),
        "kicker": " · ".join(x for x in (_label(doc, s["bucket"]), tag, _age(newest)) if x),
        "articles": len(arts), "domains": domains[:3], "url": a.get("url", ""),
        "cover": f"/discover/cover/{s['cover']['file']}" if s.get("cover") else "",
        "cover_from": (s.get("cover") or {}).get("from", ""),
        "near": s.get("near") or [], "saved": doc["saved"].get(s["id"]) or "",
        "feedback": doc["feedback"].get(s["id"], 0), "written": bool(s.get("written")),
        "topics": [t["key"] for t in doc.get("topic_list") or []
                   if rank.in_topic(s, t["key"], doc.get("topic_paths") or {})],
    }
    if s["bucket"] == "research":
        card["authors"] = ", ".join(a.get("authors") or [])
        m = re.search(r"arxiv\.org/abs/([\w.\-/]+)", a.get("url", ""))
        card["arxiv"] = m.group(1) if m else ""
        card["pdf"] = a["url"].replace("/abs/", "/pdf/") if m else ""
        card["category"] = (doc["articles"][s["articles"][0]].get("feed_name") or "").removeprefix("arXiv ")
    if s["bucket"] == "video":
        card["duration"] = a.get("duration")
        card["channel"] = a.get("feed_name") or ", ".join(a.get("authors") or [])
    return card


def _feed_row(f: dict) -> dict:
    st = f.get("state") or {}
    hosted = _hosted()
    if f.get("kind") == "wikipedia":
        status, bad = "on demand", ""
    elif f.get("kind") == "youtube" and hosted:
        status, bad = "desktop app only", "warn"
    elif st.get("status") == "error":
        status = st.get("error", "failed") + (f" since {_age(st.get('since'))}" if st.get("since") else "")
        bad = "err"
    elif st.get("status") == "refused":
        retry = st.get("retry_at")
        status = st.get("error", "refused") + (time.strftime(", retry at %H:%M", time.localtime(retry)) if retry else "")
        bad = "warn"
    elif st.get("read_at"):
        status = f"read {_age(st['read_at'])}" + (f" · {st['items']} items" if st.get("items") is not None else "")
        bad = ""
    else:
        status, bad = "not read yet", ""
    kind = {"feed": "Feed", "arxiv": "arXiv category", "youtube": "YouTube channel",
            "wikipedia": "Encyclopedia", "site": "Site search"}.get(f.get("kind", "feed"), "Feed")
    return {"id": f["id"], "name": f.get("name") or F.domain(f["url"]), "url": f["url"], "kind": kind,
            "tags": f.get("tags") or [], "status": status, "bad": bad, "bucket": f["bucket"]}


@router.get("")
def discover():
    """The view: its tabs' cards, the read in progress, and the side column.
    Opening it reads the feeds when the last read is an hour old (point 4)."""
    with store.LOCK:
        doc = store.load()
    reading = read.start() if doc.get("setup") else False
    stories = list(doc["stories"].values())
    by_tab: dict[str, list[dict]] = {t: [] for t in _TABS}
    for s in stories:
        if not doc["buckets"].get(s["bucket"], True):
            continue
        by_tab[_tab_of(s["bucket"])].append(s)
    # News reads in the order stories grew, biggest first within the day.
    by_tab["news"].sort(key=lambda s: (int(s["updated"] // 86400), len(s["articles"])), reverse=True)
    for t in ("research", "video"):
        by_tab[t].sort(key=lambda s: doc["articles"].get(s["articles"][0], {}).get("published") or s["created"],
                       reverse=True)
    foryou = rank.for_you(doc, [s for s in stories if doc["buckets"].get(s["bucket"], True)
                                and s["bucket"] != "video"])
    tabs = {"foryou": [_card(doc, s) for s in foryou[:60]],
            **{t: [_card(doc, s) for s in by_tab[t][:80]] for t in ("news", "research", "video")}}
    feeds = read.readable(doc)
    failing = sum(1 for f in feeds if (f.get("state") or {}).get("status") in ("error", "refused"))
    topics = [{**t, "on": doc["topics"].get(t["key"], True)} for t in doc.get("topic_list") or []]
    from silica.kernel.base.manifest import active_inbox_dir
    return {"setup": bool(doc.get("setup")), "hosted": _hosted(), "reading": dict(read.PROGRESS) if reading else None,
            "youtube_on_server": drop.YOUTUBE_ON_SERVER, "inbox": active_inbox_dir() or "Inbox",
            "read_at": doc.get("read_at") or 0, "tabs": tabs, "topics": topics,
            "buckets": {k: v for k, v in doc["buckets"].items()},
            "research_cats": [f.get("name", "").removeprefix("arXiv ") for f in doc["feeds"]
                              if f["bucket"] == "research" and not f.get("off")],
            "feeds": {"read": len(feeds) - failing, "failing": failing},
            "saved": len(doc["saved"])}


@router.get("/status")
def status():
    with store.LOCK:
        doc = store.load()
    return {"reading": dict(read.PROGRESS) if read.PROGRESS["running"] else None, "read_at": doc.get("read_at") or 0}


@router.post("/read")
def read_now():
    """The refresh button: read again now, whatever the hour says."""
    return {"reading": read.start(force=True) and dict(read.PROGRESS)}


@router.get("/setup")
def setup_info():
    """What the first run offers: the starter buckets and the vault's topics."""
    with store.LOCK:
        doc = store.load()
    counts = {b: sum(1 for f in doc["feeds"] if f["bucket"] == b) for b in store.BUCKETS}
    return {"buckets": doc["buckets"], "counts": counts, "hosted": _hosted(),
            "topics": [{k: v for k, v in t.items() if k != "paths"} for t in rank.vault_topics()],
            "catalog": _catalog_meta()}


@router.post("/setup")
def setup(payload: dict):
    """Start reading: the buckets picked and the vault topics switched on."""
    picked = payload.get("buckets") or {}
    keys = set(payload.get("topics") or [])
    suggested = rank.vault_topics()
    with store.LOCK:
        doc = store.load()
        for b in store.BUCKETS:
            if b in picked:
                doc["buckets"][b] = bool(picked[b])
        doc["topic_list"] = [{k: v for k, v in t.items() if k != "paths"} for t in suggested]
        doc["topic_paths"] = {t["key"]: t.get("paths") or [] for t in suggested}
        doc["topics"] = {t["key"]: t["key"] in keys for t in suggested}
        doc["setup"] = True
        store.save(doc)
    read.start(force=True)
    return {"ok": True}


@router.post("/topics")
def topics(payload: dict):
    key = str(payload.get("key") or "")
    with store.LOCK:
        doc = store.load()
        if key not in {t["key"] for t in doc.get("topic_list") or []}:
            raise HTTPException(404, "no such topic")
        doc["topics"][key] = bool(payload.get("on"))
        store.save(doc)
    return {"ok": True}


@router.post("/feedback")
def feedback(payload: dict):
    """More like this (1), Less like this (-1), or neither (0)."""
    sid, value = str(payload.get("id") or ""), payload.get("value")
    if value not in _FEEDBACK:
        raise HTTPException(400, "value is -1, 0 or 1")
    with store.LOCK:
        doc = store.load()
        if sid not in doc["stories"]:
            raise HTTPException(404, "that story is no longer kept")
        if value:
            doc["feedback"][sid] = value
        else:
            doc["feedback"].pop(sid, None)
        store.save(doc)
    return {"ok": True}


@router.get("/cover/{name}")
def cover(name: str):
    """A cover the server fetched; the page never loads a publisher's image."""
    if not _COVER_NAME.match(name):
        raise HTTPException(404)
    path = store.covers_dir() / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers=_IMAGE_HEADERS)


@router.get("/icon/{domain}")
def icon(domain: str):
    """A site's icon the server fetched, for a site an article in the store
    came from only, so the route is no proxy to an address a page names."""
    if not _DOMAIN.match(domain):
        raise HTTPException(404)
    # A kept icon was fetched for a store domain only, so it is served without
    # the store's read (38 ms on a 2.2 MB store, measured 2026-11-02, eight per story).
    got = read.kept_icon(domain)
    if got is None:
        with store.LOCK:
            doc = store.load()
        if not any(a.get("domain") == domain for a in doc["articles"].values()):
            raise HTTPException(404)
        got = read.site_icon(domain)
    if got is None:
        raise HTTPException(404)
    # The type from the bytes: the file is named after the site, and ".com" guesses wrong.
    return FileResponse(got[0], media_type=got[1], headers=_IMAGE_HEADERS)


@router.get("/story/{sid}")
def story(sid: str):
    with store.LOCK:
        doc = store.load()
    s = doc["stories"].get(sid)
    if s is None:
        raise HTTPException(404, "that story is no longer kept")
    arts = [doc["articles"][a] for a in s["articles"] if a in doc["articles"]]
    w = dict(s["written"]) if s.get("written") else None
    if w:
        w.pop("texts", None)
        w.pop("body", None)
    prog = S.WRITING.get(sid)
    related = _related(doc, s)
    return {**_card(doc, s), "written": w, "grew": s.get("grew", 0),
            "writing": dict(prog) if prog and (prog.get("running") or prog.get("error")) else None,
            "list": [{"title": a["title"], "url": a["url"], "domain": a.get("domain", ""), "lead": a.get("lead", "")}
                     for a in arts],
            "related": related, "hosted": _hosted(), "youtube_on_server": _youtube_copy()}


def _youtube_copy() -> str:
    return drop.YOUTUBE_ON_SERVER


def _related(doc: dict, s: dict) -> list[dict]:
    """Up to three other stories near this one: by vector when both have one,
    else by a shared feed tag."""
    vectors = store.load_vectors()
    out: list[tuple[float, dict]] = []
    v = vectors.get(s["id"])
    for o in doc["stories"].values():
        if o["id"] == s["id"] or o["bucket"] != s["bucket"]:
            continue
        if v is not None and (ov := vectors.get(o["id"])) is not None:
            score = float(v @ ov)
        else:
            score = 0.3 * len(set(o.get("tags") or []) & set(s.get("tags") or []))
        if score >= 0.45:
            out.append((score, o))
    out.sort(key=lambda x: -x[0])
    return [{"id": o["id"], "title": doc["articles"].get(o["articles"][0], {}).get("title", "")} for _, o in out[:3]]


@router.post("/story/{sid}/write")
def story_write(sid: str):
    """Write the story now: the first open, or Rewrite after it grew."""
    with store.LOCK:
        doc = store.load()
    if sid not in doc["stories"]:
        raise HTTPException(404, "that story is no longer kept")
    return {"writing": dict(S.start_write(None, sid))}


@router.post("/save")
async def save(payload: dict):
    sid = str(payload.get("id") or "")
    answer = str(payload.get("answer") or "")[:40_000]
    question = str(payload.get("question") or "")[:300]
    try:
        path = await asyncio.to_thread(S.save, None, sid, answer, question)
    except S.StoryError as exc:
        raise HTTPException(404, str(exc)) from None
    return {"path": path}


@router.post("/unsave")
async def unsave(payload: dict):
    sid, path = str(payload.get("id") or ""), str(payload.get("path") or "")
    with store.LOCK:
        doc = store.load()
    if doc["saved"].get(sid) != path:
        raise HTTPException(409, "that note is not this story's save")
    await asyncio.to_thread(S.unsave, None, sid, path)
    return {"ok": True}


# --- the Feeds screen ---------------------------------------------------------------

@router.get("/feeds")
def feeds_list():
    with store.LOCK:
        doc = store.load()
    order = list(store.BUCKETS) + [b for b in doc["buckets"] if b not in store.BUCKETS]
    out = []
    for b in order:
        rows = [_feed_row(f) for f in doc["feeds"] if f["bucket"] == b]
        if not rows and b in store.BUCKETS and b != "news":
            continue
        out.append({"key": b, "label": _label(doc, b), "on": bool(doc["buckets"].get(b)), "rows": rows})
    return {"buckets": out, "hosted": _hosted(), "keep_days": store.KEEP_DAYS, "catalog": _catalog_meta()}


@router.post("/buckets")
def bucket_toggle(payload: dict):
    key = str(payload.get("key") or "")
    with store.LOCK:
        doc = store.load()
        if key not in doc["buckets"]:
            raise HTTPException(404, "no such bucket")
        doc["buckets"][key] = bool(payload.get("on"))
        store.save(doc)
    return {"ok": True}


def _probe(url: str) -> tuple[str, str]:
    """(feed url, its title) for a typed address: a feed as it is, else the
    first feed its page declares or a conventional path answers. Raises
    LookupError when there is none, ValueError when the address fails."""
    resp, final = read._get(url)
    ctype = read.wf.content_type(resp)
    if "html" not in ctype:
        title, _arts = F.parse(resp.content, final)
        return final, title
    for cand in F.discover_links(resp.text, final)[:6]:
        try:
            r2, f2 = read._get(cand)
            if "html" in read.wf.content_type(r2):
                continue
            title, _arts = F.parse(r2.content, f2)
            return f2, title
        except Exception:
            # A guessed path that is not there or not a feed; the next is tried.
            continue
    raise LookupError("no feed found")


@router.post("/feeds")
async def feeds_add(payload: dict):
    """Add a feed by what the person typed, to a bucket, with tags."""
    raw = str(payload.get("url") or "").strip()
    if not raw or len(raw) > 2048 or any(c.isspace() for c in raw):
        raise HTTPException(400, "a feed is one address, at most 2,048 characters")
    with store.LOCK:
        doc = store.load()
    bucket = str(payload.get("bucket") or "news")
    if bucket not in doc["buckets"]:
        raise HTTPException(400, "no such bucket")
    tags = [t.strip() for t in str(payload.get("tags") or "").split(",") if t.strip()][:6]
    kind, url = F.classify(raw)
    name = ""
    if kind == "url":
        try:
            url, name = await asyncio.to_thread(_probe, url)
            kind = "feed"
        except LookupError:
            # A site with no feed is read through search, locally only
            # (ADR-0137 decision 6): the hosted pilot shares one address
            # across every tenant on the keyless lanes.
            if _hosted():
                raise HTTPException(422, "no feed found there, and the hosted pilot reads feeds only") from None
            kind = "site"
        except Exception as exc:
            raise HTTPException(422, f"that address could not be read: {read._reason(exc)}") from None
    elif kind == "arxiv":
        bucket = "research"
        name = "arXiv " + url.rsplit("/", 1)[-1]
    elif kind == "youtube":
        bucket = "video"
    elif kind == "wikipedia":
        bucket = "reference"
        name = "Wikipedia, " + F.domain(url).split(".")[0]
    feed = {"id": store.feed_id(url), "url": url, "name": name, "bucket": bucket, "tags": tags, "kind": kind}
    if kind == "site":
        feed["site"] = F.domain(url)
    with store.LOCK:
        doc = store.load()
        if any(f["id"] == feed["id"] for f in doc["feeds"]):
            raise HTTPException(409, "that feed is already in Discover")
        doc["feeds"].append(feed)
        store.save(doc)
    return {"feed": _feed_row(feed)}


@router.post("/feeds/remove")
def feeds_remove(payload: dict):
    fid = str(payload.get("id") or "")
    with store.LOCK:
        doc = store.load()
        before = len(doc["feeds"])
        doc["feeds"] = [f for f in doc["feeds"] if f["id"] != fid]
        if len(doc["feeds"]) == before:
            raise HTTPException(404, "no such feed")
        store.save(doc)
    return {"ok": True}


@router.post("/opml")
async def opml(file: UploadFile = File(...)):
    """Import OPML: every feed it lists, into News, with its folders as tags."""
    data = await file.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(413, "an OPML file is at most 2 MB")
    try:
        listed = F.parse_opml(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    added = 0
    with store.LOCK:
        doc = store.load()
        have = {f["id"] for f in doc["feeds"]}
        for o in listed:
            url = o["url"] if "://" in o["url"] else "https://" + o["url"]
            fid = store.feed_id(url)
            if fid in have or not url.startswith(("http://", "https://")):
                continue
            kind, _ = F.classify(url)
            bucket = {"arxiv": "research", "youtube": "video"}.get(kind, "news")
            doc["feeds"].append({"id": fid, "url": url, "name": o["name"][:80], "bucket": bucket,
                                 "tags": o["tags"], "kind": kind if kind in ("arxiv", "youtube") else "feed"})
            have.add(fid)
            added += 1
        store.save(doc)
    return {"added": added, "listed": len(listed)}


# --- the catalog ----------------------------------------------------------------------

_CATALOG: dict | None = None


def _catalog() -> dict:
    global _CATALOG
    if _CATALOG is None:
        text = resources.files("silica.sources.discover").joinpath("catalog.json").read_text(encoding="utf-8")
        _CATALOG = json.loads(text)
    return _CATALOG


def _catalog_meta() -> dict:
    cats = _catalog()["categories"]
    return {"topics": sum(1 for c in cats if c["kind"] == "topic"), "feeds": sum(len(c["feeds"]) for c in cats)}


@router.get("/catalog")
def catalog(q: str = ""):
    """Kagi News's list, shipped with the package: browsing it calls no one
    (ADR-0137 decision 5)."""
    with store.LOCK:
        doc = store.load()
    q = q.strip().lower()
    cats = [c for c in _catalog()["categories"] if not q or q in c["name"].lower()]
    if not q:
        cats = [c for c in cats if c["kind"] == "topic"]
        cats.sort(key=lambda c: -len(c["feeds"]))
    return {"topics": [{"name": c["name"], "kind": c["kind"], "feeds": len(c["feeds"]),
                        "added": "c:" + c["name"] in doc["buckets"]} for c in cats[:_CATALOG_SHOWN]],
            "meta": _catalog_meta()}


@router.post("/catalog/add")
def catalog_add(payload: dict):
    """Adding a catalog topic adds its feeds as one bucket."""
    name = str(payload.get("name") or "")
    cat = next((c for c in _catalog()["categories"] if c["name"] == name), None)
    if cat is None:
        raise HTTPException(404, "no such topic in the catalog")
    key = "c:" + name
    with store.LOCK:
        doc = store.load()
        if key in doc["buckets"]:
            raise HTTPException(409, "that topic is already a bucket")
        doc["buckets"][key] = True
        doc.setdefault("bucket_labels", {})[key] = name
        have = {f["id"] for f in doc["feeds"]}
        for url in cat["feeds"]:
            fid = store.feed_id(url)
            if fid not in have:
                doc["feeds"].append({"id": fid, "url": url, "name": "", "bucket": key, "tags": [name], "kind": "feed"})
                have.add(fid)
        store.save(doc)
    return {"ok": True, "feeds": len(cat["feeds"])}


@router.post("/catalog/remove")
def catalog_remove(payload: dict):
    key = "c:" + str(payload.get("name") or "")
    with store.LOCK:
        doc = store.load()
        if key not in doc["buckets"]:
            raise HTTPException(404, "that topic is not a bucket")
        doc["buckets"].pop(key)
        (doc.get("bucket_labels") or {}).pop(key, None)
        doc["feeds"] = [f for f in doc["feeds"] if f["bucket"] != key]
        store.save(doc)
    return {"ok": True}
