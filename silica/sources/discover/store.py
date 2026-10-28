# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What Discover keeps, beside the vault and never in it (ADR-0137, ADR-0015).

One JSON document per vault under its index directory (`~/.silica/index/<vault
digest>/discover/`, the tenant's own `.silica/` on the hosted pilot, so
outside the vault's caps of ADR-0086), the story vectors in a numpy file
beside it, and the covers in `covers/`. Written whole by rename after each
change, as Learn's live session is.

shortcut: the whole document is read and rewritten on every change; at the
measured ~340 articles a day over a 14-day window that is ~5,000 articles,
about 4 MB. Move articles to SQLite past 20,000 articles.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

KEEP_DAYS = 14
LOCK = threading.RLock()

# Four starter buckets (ADR-0137 point 3). Reference is looked up, never read.
# Video starts off: a transcript is the slowest article to read, and the
# hosted pilot cannot read one.
BUCKETS = ("news", "research", "reference", "video")
BUCKET_LABEL = {"news": "News", "research": "Research", "reference": "Reference", "video": "Video"}

# The News starters are the ten feeds browser-harness lists as tested
# (news-aggregation/multi-source.md, 2026-04-18) plus The Verge from the same
# table and Quanta for the science desk; all twelve answered 200 to the
# descriptive agent on 2026-10-28. Twelve News, two arXiv, three channels:
# the 17 feeds of ADR-0137 gate G3.
STARTERS = [
    ("news", "TechCrunch", "https://techcrunch.com/feed/", ["Tech"]),
    ("news", "The Verge", "https://www.theverge.com/rss/index.xml", ["Tech"]),
    ("news", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", ["Tech"]),
    ("news", "Wired", "https://www.wired.com/feed/rss", ["Tech"]),
    ("news", "Hacker News", "https://news.ycombinator.com/rss", ["Tech"]),
    ("news", "The Guardian, Technology", "https://www.theguardian.com/technology/rss", ["Tech"]),
    ("news", "Quanta Magazine", "https://www.quantamagazine.org/feed/", ["Science"]),
    ("news", "BBC News", "http://feeds.bbci.co.uk/news/rss.xml", ["World"]),
    ("news", "The Guardian, World", "https://www.theguardian.com/world/rss", ["World"]),
    ("news", "NPR", "https://feeds.npr.org/1001/rss.xml", ["World"]),
    ("news", "CNN", "http://rss.cnn.com/rss/cnn_topstories.rss", ["World"]),
    ("news", "The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", ["World"]),
    ("research", "arXiv cs.CC", "https://rss.arxiv.org/rss/cs.CC", ["Complexity"]),
    ("research", "arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG", ["Machine learning"]),
    ("reference", "Wikipedia, English", "https://en.wikipedia.org", ["Background"]),
    ("reference", "Wikipedia, Italiano", "https://it.wikipedia.org", ["Background"]),
    ("video", "3Blue1Brown", "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw", ["Math"]),
    ("video", "Computerphile", "https://www.youtube.com/channel/UC9-y-6csu5WGm29I7JiwpnA", ["CS"]),
    ("video", "Two Minute Papers", "https://www.youtube.com/channel/UCbfYPyITQ-7l4upoX8nvctg", ["AI"]),
]

KIND_OF_BUCKET = {"research": "arxiv", "reference": "wikipedia", "video": "youtube"}


def feed_id(url: str) -> str:
    from silica.sources.discover.feeds import article_id

    return article_id(url.strip().lower().rstrip("/"))


def _starter_feeds() -> list[dict]:
    return [{"id": feed_id(url), "url": url, "name": name, "bucket": bucket, "tags": tags,
             "kind": KIND_OF_BUCKET.get(bucket, "feed"), "starter": True}
            for bucket, name, url, tags in STARTERS]


def _blank() -> dict:
    return {"v": 1, "setup": False,
            "buckets": {"news": True, "research": True, "reference": True, "video": False},
            "feeds": _starter_feeds(), "articles": {}, "stories": {}, "feedback": {},
            "saved": {}, "topics": {}, "topic_list": [], "topic_paths": {}, "bucket_labels": {},
            "read_at": 0.0}


def home(vault: str | None = None) -> Path:
    from silica.config import CONFIG
    from silica.kernel.recall import paths

    return paths.index_dir_for(vault or CONFIG.vault_path) / "discover"


def covers_dir(vault: str | None = None) -> Path:
    return home(vault) / "covers"


def load(vault: str | None = None) -> dict:
    """The vault's Discover document; the blank one before the first write."""
    path = home(vault) / "discover.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blank()
    except (OSError, ValueError):
        # Written whole by rename, so a file that does not parse was damaged
        # on disk or edited by hand. Discover starts over rather than failing
        # every route on it: what it held is a cache of the feeds, and the
        # person's own feeds list is the one loss, named in the log.
        logger.warning("discover: the store does not read, starting blank", exc_info=True)
        return _blank()
    base = _blank()
    for k, v in base.items():
        doc.setdefault(k, copy.deepcopy(v))
    return doc


def save(doc: dict, vault: str | None = None) -> None:
    from silica.kernel.recall.paths import atomic_write_bytes

    path = home(vault) / "discover.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(doc, ensure_ascii=False).encode("utf-8"), mode=0o600)


# --- story vectors -------------------------------------------------------------
# One centroid per story, float16, beside the document rather than in it: a
# 1024-dim vector is 2 KB here and ~9 KB as JSON floats.

def load_vectors(vault: str | None = None) -> dict:
    import numpy as np

    path = home(vault) / "vectors.npz"
    try:
        with np.load(path, allow_pickle=False) as z:
            return {str(k): v.astype(np.float32) for k, v in zip(z["ids"], z["vecs"])}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError):
        # The vectors only decide which story a new article joins; without
        # them every new article starts a story of its own until the next read.
        logger.warning("discover: story vectors do not read", exc_info=True)
        return {}


def save_vectors(vectors: dict, vault: str | None = None) -> None:
    import io

    import numpy as np

    from silica.kernel.recall.paths import atomic_write_bytes

    path = home(vault) / "vectors.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = list(vectors)
    buf = io.BytesIO()
    dim = len(next(iter(vectors.values()))) if vectors else 0
    np.savez(buf, ids=np.array(ids, dtype=str),
             vecs=np.array([vectors[i] for i in ids], dtype=np.float16).reshape(len(ids), dim))
    atomic_write_bytes(path, buf.getvalue(), mode=0o600)


def prune(doc: dict, vectors: dict, vault: str | None = None, now: float | None = None) -> int:
    """Drop what is past the keep window (ADR-0137 point 10), saved stories
    excepted, with their covers. Returns the stories dropped."""
    now = now or time.time()
    cutoff = now - KEEP_DAYS * 86400
    saved = set(doc["saved"])
    gone = [sid for sid, s in doc["stories"].items() if s.get("updated", 0) < cutoff and sid not in saved]
    for sid in gone:
        doc["stories"].pop(sid, None)
        vectors.pop(sid, None)
        doc["feedback"].pop(sid, None)
    live_articles = {a for s in doc["stories"].values() for a in s["articles"]}
    for aid in [a for a in doc["articles"] if a not in live_articles]:
        doc["articles"].pop(aid, None)
    keep_covers = {s["cover"]["file"] for s in doc["stories"].values() if s.get("cover")}
    cdir = covers_dir(vault)
    if cdir.is_dir():
        for f in cdir.iterdir():
            if f.name not in keep_covers:
                f.unlink(missing_ok=True)
    return len(gone)


# The covers' own ceiling (ADR-0137 decision 8), applied on every install
# rather than on the hosted pilot only: one code path, and a laptop gains a
# bound it never had.
COVERS_CAP = 200 * 1024 * 1024


def cap_covers(doc: dict, vault: str | None = None) -> int:
    """Delete the oldest covers until the folder is under COVERS_CAP; the
    stories that wore them fall back to the typographic card."""
    cdir = covers_dir(vault)
    if not cdir.is_dir():
        return 0
    files = sorted(cdir.iterdir(), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    dropped = set()
    for f in files:
        if total <= COVERS_CAP:
            break
        total -= f.stat().st_size
        dropped.add(f.name)
        f.unlink(missing_ok=True)
    for s in doc["stories"].values():
        if s.get("cover") and s["cover"]["file"] in dropped:
            s["cover"] = None
    return len(dropped)
