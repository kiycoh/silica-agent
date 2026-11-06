# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One read of the person's feeds into stories (ADR-0137 points 3, 4 and 7).

When Discover opens, at most once an hour, and on refresh: never while it is
closed (decision 3, the morning read is not built). Feeds are read in
parallel with conditional requests; a read stores articles and groups them
into stories with no model call; a story is written only when it is opened
(story.py). Covers are fetched by the server, after the stories are on
screen, so the person's browser never calls a publisher.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

from silica.sources import web_fetch as wf
from silica.sources.discover import feeds as F
from silica.sources.discover import store

logger = logging.getLogger(__name__)

READ_EVERY_S = 3600
_WORKERS = 8
_FEED_CAP = 8 * 1024 * 1024
COVER_CAP = 2 * 1024 * 1024
# A card's cover, per read: the og:image fallback costs one article page each,
# so a first read of 300 stories does not open 300 pages before it ends.
_PAGE_LOOKUPS = 30
_YT_LIST = 12
# A new article joins the nearest story centroid at or above this. The first
# reading (2026-10-28, jina, 343 articles of one day) put same-event pairs down
# to 0.74 and the first wrong merge at 0.688. Re-measured 2026-10-31 on the 477
# News articles of 10-28 to 10-30, title plus lead through _embed, with every
# pair at or above 0.60 on either embedder labeled by hand (350 pairs; a later
# step of the same saga counts as another event) and group() itself run
# against the labeled events:
#   - same-event pairs do not stop at 0.74: on both embedders they run down
#     to 0.60, the lowest cosine labeled.
#   - the highest different-event pair is 0.75 on pplx-embed-v1-4b and 0.79 on
#     jina-embeddings-v5-text-small, one Wired pair (a Kindle buying guide and
#     a Kindle review); the next are 0.73 and 0.72.
#   - group() at 0.74 makes 416 stories on pplx and 412 on jina, one on each
#     mixing two events (that same pair). It is precision first: of the 38
#     events with several articles 14 (pplx) and 15 (jina) end whole in one
#     story, and an article with partners finds 61% and 65% of them. At 0.70
#     that is 19 and 21 events whole and 7 and 4 stories mixing events.
# The embedders land within 0.04 of each other whichever line is drawn (fewest
# mixed stories, pair precision), under the 0.05 that separated the first
# reading's two lines, so one value serves both (the hosted one is pplx).
# shortcut: one judge (the agent, blind to the cosines), English feeds, three
# days. Re-label 300 pairs by 2027-01-31, or when the first feed in another
# language is read by default.
GROUP_AT = 0.74
# Research and Video are lists of their own items: two papers on one topic
# are two papers, not one event. Every other bucket (News, a catalog topic)
# is news and is grouped.
_LISTS = ("research", "video", "reference")


def grouped(bucket: str) -> bool:
    return bucket not in _LISTS

# The read in progress, for the page to poll; one at a time.
_LOCK = threading.Lock()
PROGRESS: dict = {"running": False, "done": 0, "of": 0, "phase": "", "started": 0.0}


def _hosted() -> bool:
    from silica.config import hosted

    return hosted()


def feed_state(feed: dict) -> dict:
    return feed.setdefault("state", {})


# --- one feed --------------------------------------------------------------------

def _get(url: str, state: dict | None = None, *, types=wf.FEED_TYPES, cap=_FEED_CAP):
    headers = dict(wf._READER_HEADERS)
    if state:
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("modified"):
            headers["If-Modified-Since"] = state["modified"]
    return wf._fetch(url, headers, types=types, cap=cap)


def _yt_feed_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.path.startswith("/channel/UC"):
        return "https://www.youtube.com/feeds/videos.xml?channel_id=" + parts.path.split("/")[2]
    if "feeds/videos.xml" in parts.path and parse_qs(parts.query).get("channel_id"):
        return url
    return ""


def _yt_list(url: str) -> list[dict]:
    """The channel's latest videos through yt-dlp, which `web_fetch` already
    needs for a transcript.

    The channel feed is what ADR-0137 point 3 names, and it answered 404 for
    all three starter channels on 2026-10-28 (three known channel ids, the
    descriptive agent), so this is the list when the feed is gone. A flat
    listing carries no upload date: a video is dated when it was first seen.
    """
    exe = shutil.which("yt-dlp")
    if not exe:
        raise ValueError("YouTube's channel feed answered 404; listing a channel needs yt-dlp on PATH")
    base = url.rstrip("/")
    proc = subprocess.run(
        [exe, "--flat-playlist", "--playlist-items", f"1-{_YT_LIST}", "-J", "--", base + "/videos"],
        capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise ValueError("yt-dlp could not list the channel: " + (proc.stderr or "").strip()[-200:])
    doc = json.loads(proc.stdout or "{}")
    out = []
    for e in doc.get("entries") or []:
        vid = str(e.get("id") or "")
        if not vid:
            continue
        thumbs = [t for t in e.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
        out.append({"url": f"https://www.youtube.com/watch?v={vid}", "title": str(e.get("title") or ""),
                    "lead": "", "published": None, "video": vid, "duration": e.get("duration"),
                    "image": thumbs[-1]["url"] if thumbs else "", "authors": [str(doc.get("channel") or "")]})
    return out


def _site_search(host: str) -> list[dict]:
    """A site with no feed, read through the search lanes: one query per site
    (ADR-0137 point 3), never Perplexica's site-times-phrase fan-out.

    Tavily, when a key is set, restricts to the domain rather than boosting it.
    The keyless lanes get `site:`; Wikipedia, the last lane of /web-search, is
    left out because it cannot search someone else's site.
    shortcut: `include_domains_mode` is the name deer-flow passes to Tavily's
    SDK (backend/tests, 2026-10); not checked against the REST API, which had
    no key here. Check it with the first pilot key.
    """
    from silica.config import CONFIG
    from silica.sources import web_research as wr

    key = (CONFIG.tavily_api_key or "").strip()
    if key:
        import httpx

        resp = httpx.post(wr._TAVILY_URL, timeout=wr._HTTP_TIMEOUT, json={
            "api_key": key, "query": f"latest from {host}", "max_results": 10, "topic": "news",
            "include_domains": [host], "include_domains_mode": "filter", "include_answer": False})
        resp.raise_for_status()
        hits = resp.json().get("results", [])
    else:
        hits = []
        for lane in (wr._ddg_search, wr._mojeek_search):
            try:
                hits = lane(f"site:{host}")
                break
            except Exception:
                # A lane that challenged us; the next one is tried, and the
                # feed's state says search was refused when none answered.
                logger.debug("discover: site search lane failed for %s", host, exc_info=True)
        else:
            raise ValueError("search refused")
    return [{"url": h["url"], "title": h.get("title") or h["url"], "lead": F._lead(h.get("content") or ""),
             "published": None, "image": "", "authors": []}
            for h in hits if F.domain(h.get("url", "")).endswith(host.removeprefix("www."))]


def read_feed(feed: dict) -> list[dict] | None:
    """Read one feed; its articles, or None when it did not change (304).
    Updates `feed["state"]` in place and raises nothing: a feed that fails is
    a row with its reason, never a failed read."""
    st = feed_state(feed)
    now = time.time()
    kind = feed.get("kind", "feed")
    try:
        if kind == "youtube":
            if _hosted():
                st.update(status="desktop", error="desktop app only")
                return []
            arts = None
            if (furl := _yt_feed_url(feed["url"])):
                try:
                    resp, _ = _get(furl, st)
                    if resp.status_code == 304:
                        arts = None
                        st.update(status="ok", read_at=now, error="")
                        return None
                    _title, arts = F.parse(resp.content, furl)
                except Exception as exc:
                    # Measured: YouTube answers 404 to every channel feed this
                    # date. The listing below is the way, and its own failure
                    # is the one the row shows.
                    logger.debug("discover: youtube feed %s failed: %s", furl, exc)
                    arts = None
            if arts is None:
                arts = _yt_list(feed["url"])
        elif kind == "site":
            if _hosted():
                st.update(status="refused", error="no feed, and the hosted pilot reads feeds only")
                return []
            if st.get("retry_at", 0) > now:
                return []
            try:
                arts = _site_search(feed.get("site") or F.domain(feed["url"]))
            except ValueError:
                st.update(status="refused", error="search refused", retry_at=now + 3600)
                return []
        else:
            resp, final = _get(feed["url"], st)
            if resp.status_code == 304:
                st.update(status="ok", read_at=now, error="")
                return None
            title, arts = F.parse(resp.content, final)
            if not feed.get("name") and title:
                feed["name"] = title[:80]
            st.update(etag=resp.headers.get("etag", ""), modified=resp.headers.get("last-modified", ""))
        F.drop_logos(arts)
        st.update(status="ok", read_at=now, items=len(arts), error="", since=0, retry_at=0)
        return arts
    except Exception as exc:
        reason = _reason(exc)
        if st.get("status") != "error" or st.get("error") != reason:
            st["since"] = now
        st.update(status="error", error=reason, read_at=now)
        logger.info("discover: feed %s failed: %s", feed.get("url"), reason)
        return []


def _reason(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    msg = str(exc)
    for code in ("401", "403", "404", "429"):
        if msg.startswith(code):
            return f"HTTP {code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect"
    return msg[:120] or type(exc).__name__


# --- grouping ----------------------------------------------------------------------

def _embed(texts: list[str]):
    """Unit vectors for `texts`, or None when the vault's embedder is down:
    then every new article is a story of its own, which is a feed that reads
    flat, not a feed that breaks."""
    import numpy as np

    from silica.agent.providers import get_embedder_or_none
    from silica.config import CONFIG

    emb = get_embedder_or_none(CONFIG, "discover grouping", level="info")
    if emb is None:
        return None
    try:
        vecs = []
        for i in range(0, len(texts), 64):
            vecs += emb.embed(texts[i:i + 64])
    except Exception:
        # The embedder logged its own failure (warn_down_once); the read goes on.
        return None
    v = np.array(vecs, dtype=np.float32)
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    return v


def story_id(first_article: str) -> str:
    return "s" + first_article[:12]


def group(doc: dict, vectors: dict, new: list[str]) -> None:
    """Put each new article into the nearest story at GROUP_AT, else a new one.

    Greedy over the read order, against story centroids kept between reads,
    so an article published tomorrow joins today's story.
    """
    import numpy as np

    arts = doc["articles"]
    news = [a for a in new if grouped(arts[a]["bucket"])]
    vecs = _embed([f"{arts[a]['title']}. {arts[a]['lead'][:240]}" for a in news]) if news else None
    vec_of = {a: vecs[i] for i, a in enumerate(news)} if vecs is not None else {}
    if vecs is not None:
        # Centroids of another embedder (SILICA_EMBEDDING_MODEL changed; the
        # file carries vectors and no model tag) cannot be compared with these
        # articles, and `mat @ v` would raise on every read from then on. Their
        # stories keep their articles and stop taking new ones: a feed that
        # reads flat, not one that breaks.
        for stale in [s for s, c in vectors.items() if len(c) != vecs.shape[1]]:
            del vectors[stale]
    ids = [s for s in vectors if grouped(doc["stories"].get(s, {}).get("bucket", "research"))]
    mat = np.array([vectors[s] for s in ids], dtype=np.float32) if ids else None
    for aid in new:
        a = arts[aid]
        v = vec_of.get(aid)
        if v is not None and mat is not None and len(ids):
            sims = mat @ v
            k = int(sims.argmax())
            if sims[k] >= GROUP_AT:
                sid = ids[k]
                s = doc["stories"][sid]
                n = s.get("n", 1)
                c = (vectors[sid] * n + v) / (n + 1)
                c /= max(float(np.linalg.norm(c)), 1e-9)
                vectors[sid] = c
                mat[k] = c
                s["articles"].append(aid)
                s["n"] = n + 1
                s["updated"] = time.time()
                s["tags"] = sorted(set(s["tags"]) | set(a.get("tags") or []))
                # A written story keeps its text, which was paid for; the page
                # says how many articles arrived since and offers to rewrite.
                if s.get("written"):
                    s["grew"] = s.get("grew", 0) + 1
                continue
        sid = story_id(aid)
        doc["stories"][sid] = {"id": sid, "articles": [aid], "bucket": a["bucket"], "tags": list(a.get("tags") or []),
                               "created": time.time(), "updated": time.time(), "n": 1, "cover": None,
                               "written": None, "near": None}
        if v is not None:
            vectors[sid] = v
            ids.append(sid)
            mat = v[None, :].copy() if mat is None else np.vstack([mat, v])


# --- covers ------------------------------------------------------------------------

def cover_file(url: str, ctype: str) -> str:
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}.get(ctype, ".jpg")
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ext


def fetch_cover(url: str, vault: str | None = None) -> dict | None:
    """Fetch one image through the guard into the covers folder: image/*
    only, at most 2 MB (ADR-0137 point 7). None when it cannot be had."""
    try:
        resp, final = _get(url, None, types=("image/",), cap=COVER_CAP)
    except Exception as exc:
        logger.debug("discover: cover %s refused: %s", url, exc)
        return None
    ctype = wf.content_type(resp)
    if resp.status_code != 200 or not resp.content:
        return None
    name = cover_file(url, ctype)
    cdir = store.covers_dir(vault)
    cdir.mkdir(parents=True, exist_ok=True)
    from silica.kernel.recall.paths import atomic_write_bytes

    atomic_write_bytes(cdir / name, resp.content, mode=0o600)
    return {"file": name, "type": ctype, "from": F.domain(final)}


# What a wiki may copy (ADR-0153 point 20), each a type its pages can name by extension. Not avif:
# the published files go by `_ASSET_EXTS`, and a copy nothing can show is worse than a link.
_FIGURE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
                 "image/bmp": ".bmp"}


def fetch_figure(url: str) -> dict:
    """One remote image for a wiki, through the guard the covers use: `data`, its `type`, and `why`
    it cannot be copied, in the words the check before Publish shows. An SVG is refused for the
    reason `icon_type` gives; `fetch_cover` is not reused because it writes to the covers folder
    and names an SVG `.jpg`."""
    try:
        resp, _final = _get(url, None, types=("image/",), cap=COVER_CAP)
    except wf.WrongType:
        return {"data": b"", "type": "", "why": "Not an image"}
    except wf.TooLarge:
        return {"data": b"", "type": "", "why": "Over 2 MB"}
    except Exception as exc:
        # Unreachable, refused by the guard or a bad status: all one image that stays a link.
        logger.debug("wiki: image %s refused: %s", url, exc)
        return {"data": b"", "type": "", "why": "Could not be read"}
    ctype = wf.content_type(resp)
    if ctype == "image/svg+xml":
        return {"data": b"", "type": "", "why": "An SVG"}
    if ctype not in _FIGURE_TYPES:
        return {"data": b"", "type": "", "why": "Not an image"}
    if resp.status_code != 200 or not resp.content:
        return {"data": b"", "type": "", "why": "Could not be read"}
    return {"data": resp.content, "type": ctype, "why": ""}


# --- site icons ----------------------------------------------------------------------
# Measured 2026-11-02 on ten news and research sites: eight serve /favicon.ico,
# the largest 15,086 bytes, so the cap leaves four times that.
ICON_CAP = 64 * 1024
# A site that gave no icon is asked again after a week, not on every story view.
ICON_RETRY_DAYS = 7
# Decided on the bytes, never the header: nytimes.com serves a PNG as image/x-icon,
# and an SVG is a document that can carry script, so it is never taken.
_ICON_MAGIC = ((b"\x00\x00\x01\x00", "image/x-icon"), (b"\x89PNG\r\n\x1a\n", "image/png"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"\xff\xd8\xff", "image/jpeg"))


def icon_type(body: bytes) -> str:
    """The raster type the bytes are, "" for anything else."""
    for magic, ctype in _ICON_MAGIC:
        if body.startswith(magic):
            return ctype
    return "image/webp" if body[:4] == b"RIFF" and body[8:12] == b"WEBP" else ""


def kept_icon(domain: str, vault: str | None = None) -> tuple[str, str] | None:
    """The icon already fetched for `domain`, with no store read and no network."""
    path = store.home(vault) / "icons" / domain
    return (str(path), icon_type(path.read_bytes()[:12])) if path.is_file() else None


def site_icon(domain: str, vault: str | None = None) -> tuple[str, str] | None:
    """A site's /favicon.ico, fetched by the server through the guard the covers
    use and kept beside them, so the page never calls a publisher (ADR-0137).
    Returns (path, type), None when the site has none.

    shortcut: icons are kept for every site ever read, ~64 KB at most each, so
    6 MB per hundred sites; prune them with the covers past 1,000 sites."""
    if got := kept_icon(domain, vault):
        return got
    idir = store.home(vault) / "icons"
    path, miss = idir / domain, idir / (domain + ".none")
    if miss.is_file() and time.time() - miss.stat().st_mtime < ICON_RETRY_DAYS * 86400:
        return None
    idir.mkdir(parents=True, exist_ok=True)
    try:
        resp, _ = _get(f"https://{domain}/favicon.ico", None, types=("image/",), cap=ICON_CAP)
        body = resp.content if resp.status_code == 200 else b""
    except Exception as exc:
        # Unreachable, refused by the guard, over the cap or not an image: all
        # one missing icon, which the page draws as the site's initial.
        logger.debug("discover: icon for %s refused: %s", domain, exc)
        body = b""
    ctype = icon_type(body)
    if not ctype:
        miss.touch()
        return None
    from silica.kernel.recall.paths import atomic_write_bytes

    atomic_write_bytes(path, body, mode=0o600)
    miss.unlink(missing_ok=True)
    return str(path), ctype


def _page_image(url: str) -> str:
    try:
        resp, final = _get(url, None, types=wf._TEXT_TYPES, cap=wf._MAX_BODY_BYTES)
    except Exception as exc:
        logger.debug("discover: og:image lookup %s failed: %s", url, exc)
        return ""
    if "html" not in wf.content_type(resp):
        return ""
    return wf.page_image(resp.text, final)


def covers(doc: dict, vault: str | None = None, stories: list[str] | None = None) -> int:
    """Give each story the cover of its first article that has one: the feed
    item's own image, else the article page's og:image; arXiv never.

    Newest stories first, eight fetches at a time: a first read has ~300
    stories, and one at a time took 97 s (measured 2026-10-28). A story whose
    page was looked up for og:image is not looked up again (`cover_tried`).
    """
    todo = [sid for sid in (stories or list(doc["stories"]))
            if not doc["stories"][sid].get("cover") and doc["stories"][sid]["bucket"] != "research"]
    todo.sort(key=lambda sid: doc["stories"][sid]["updated"], reverse=True)
    looked = [0]
    lock = threading.Lock()

    def one(sid: str) -> bool:
        s = doc["stories"][sid]
        arts = [doc["articles"][a] for a in s["articles"] if a in doc["articles"]]
        owner = next((a for a in arts if a.get("image")), None)
        url = owner["image"] if owner else ""
        if not url and not s.get("cover_tried") and grouped(s["bucket"]) and arts:
            with lock:
                if looked[0] >= _PAGE_LOOKUPS:
                    return False
                looked[0] += 1
            s["cover_tried"] = True
            url = _page_image(arts[0]["url"])
            if url:
                arts[0]["image"] = url
                owner = arts[0]
        if url and (c := fetch_cover(url, vault)):
            # Credited to the article that declared it, not the image host
            # (i.guim.co.uk is the Guardian's CDN, not a publisher of its own).
            c["from"] = (owner or {}).get("domain") or c["from"]
            s["cover"] = c
            return True
        return False

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return sum(pool.map(one, todo))


# --- the read ------------------------------------------------------------------------

def due(doc: dict) -> bool:
    return bool(doc.get("setup")) and time.time() - doc.get("read_at", 0) >= READ_EVERY_S


def readable(doc: dict) -> list[dict]:
    return [f for f in doc["feeds"] if doc["buckets"].get(f["bucket"]) and f.get("kind") != "wikipedia"
            and not f.get("off")]


def run(vault: str | None = None) -> bool:
    """One read, start to end; False when another read holds the lock."""
    if not _LOCK.acquire(blocking=False):
        return False
    try:
        with store.LOCK:
            doc = store.load(vault)
        todo = readable(doc)
        PROGRESS.update(running=True, done=0, of=len(todo), phase="feeds", started=time.time())
        results: dict[str, list[dict] | None] = {}

        def one(feed: dict):
            out = read_feed(feed)
            PROGRESS["done"] += 1
            return feed["id"], out

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for fid, out in pool.map(one, todo):
                results[fid] = out
        PROGRESS["phase"] = "stories"
        with store.LOCK:
            fresh = store.load(vault)
            states = {f["id"]: f for f in todo}
            for f in fresh["feeds"]:
                if f["id"] in states:
                    f["state"] = states[f["id"]].get("state", {})
                    f["name"] = f.get("name") or states[f["id"]].get("name", "")
            vectors = store.load_vectors(vault)
            new = _merge(fresh, results, states)
            group(fresh, vectors, new)
            fresh["read_at"] = time.time()
            store.prune(fresh, vectors, vault)
            store.save(fresh, vault)
            store.save_vectors(vectors, vault)
        PROGRESS["phase"] = "covers"
        touched = list({s["id"] for s in fresh["stories"].values() if set(s["articles"]) & set(new)})
        got = covers(fresh, vault, touched)
        with store.LOCK:
            doc = store.load(vault)
            for sid in touched:
                if sid in doc["stories"] and sid in fresh["stories"]:
                    doc["stories"][sid]["cover"] = fresh["stories"][sid].get("cover")
                    doc["stories"][sid]["cover_tried"] = fresh["stories"][sid].get("cover_tried", False)
            for aid, a in fresh["articles"].items():
                if aid in doc["articles"]:
                    doc["articles"][aid]["image"] = a.get("image", "")
            store.cap_covers(doc, vault)
            store.save(doc, vault)
        PROGRESS["phase"] = "near"
        from silica.sources.discover import rank

        rank.near_notes(vault, touched)
        logger.info("discover: read %d feeds, %d new articles, %d covers", len(todo), len(new), got)
        return True
    finally:
        PROGRESS.update(running=False, phase="")
        _LOCK.release()


def _merge(doc: dict, results: dict, feeds: dict) -> list[str]:
    """New articles into the document, in read order; returns their ids."""
    new: list[str] = []
    now = time.time()
    for fid, arts in results.items():
        if not arts:
            continue
        feed = feeds[fid]
        for a in arts:
            aid = F.article_id(a["url"])
            if aid in doc["articles"]:
                # the same feed's current summary replaces the stored one: a lead the parser
                # once got wrong ("Comments", 2026-11-01) is mended at the next read, not in
                # the 14 days the story is kept
                if doc["articles"][aid].get("feed") == fid:
                    doc["articles"][aid]["lead"] = a.get("lead", "")
                continue
            published = a.get("published")
            if published and published < now - store.KEEP_DAYS * 86400:
                # A feed can keep years-old items (CNN's carried 2023 stories
                # on 2026-10-28): past the keep window, it is not news here.
                continue
            doc["articles"][aid] = {**a, "id": aid, "feed": fid, "feed_name": feed.get("name", ""),
                                    "bucket": feed["bucket"], "tags": list(feed.get("tags") or []),
                                    "domain": F.domain(a["url"]), "seen": now,
                                    "published": published if published and published <= now + 3600 else None}
            new.append(aid)
    # Oldest first, so the earliest report of an event names its story.
    new.sort(key=lambda a: doc["articles"][a].get("published") or doc["articles"][a]["seen"])
    return new


def start(vault: str | None = None, force: bool = False) -> bool:
    """Read in the background if it is time (or `force`); True when a read
    is running after this call."""
    if PROGRESS["running"]:
        return True
    with store.LOCK:
        doc = store.load(vault)
    if not force and not due(doc):
        return False
    if not doc.get("setup"):
        return False
    t = threading.Thread(target=run, args=(vault,), name="discover-read", daemon=True)
    t.start()
    PROGRESS.update(running=True, done=0, of=len(readable(doc)), phase="feeds", started=time.time())
    return True
