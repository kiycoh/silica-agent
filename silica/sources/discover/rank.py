# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""For you, ranked by the vault (ADR-0137 point 6).

By the notes near a story (the same search the chat uses), by the topics
switched on in "Shaped by your vault", by More like this and Less like this,
and by what was saved. News is not ranked by the vault: it is read in the
order the stories grew.
"""
from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from silica.kernel.base.layout import NOT_A_TOPIC
from silica.sources.discover import store

logger = logging.getLogger(__name__)

# How many stories are matched against the vault per read, newest and
# largest first; the rest wait for the next read.
_NEAR_PER_READ = 60
# "Near notes in your vault" (ADR-0137 point 6, its search left to the build):
# the story's vector against the vault's own note vectors, one cosine scale
# per embedder. Not the chat's reranked search, whose score is comparable
# only within one call and sat between -0.2 and 0.22 for every query on this
# machine's reranker.
# A floor belongs to one embedder (ADR-0078). Measured 2026-10-31 on
# tests/fixtures/synthetic_vault with the 1,716 stories of one Discover store.
# 139 were labeled blind to the score (20 unrelated, 87 tangent, 32 match):
# every story above the floors and a few per score band below, so these are
# the median top-1 cosines of the labeled stories of each class:
#   jina-embeddings-v5-text-small  0.154 / 0.357 / 0.445  floor 0.40
#   pplx-embed-v1-4b               0.100 / 0.248 / 0.316  floor 0.28
# Each floor is the midpoint of the tangent and match medians (jina's agrees
# with the 0.40 of the first reading, 2026-10-28), and 0.28 passes 3.7% of the
# stories where 0.40 passes 3.4% on jina. Hosted tenants run pplx, and 0.40
# there would pass 4 of the 1,716.
NEAR_AT = 0.40
NEAR_AT_BY_MODEL: dict[str, float] = {"pplx-embed-v1-4b": 0.28}
# shortcut: a flat floor does not know how many notes the vault has. The
# owner's 2,083-note vault on jina (92 labeled stories, 2026-10-31): 47% of
# stories pass 0.40, 95% of those in the right subfield and only 18% real
# matches; the tangent and match medians there are 0.45 and 0.57, a floor near
# 0.51. The 13-note fixture passes 3%. pplx has no such reading: a person's
# notes do not go to the embedding API for a measurement. Reopen at the first
# hosted vault over 500 notes, or by 2026-12-15, with the vault's own p95 of
# story-to-note top-1 as the floor.
_NEAR_K = 3
_TOPICS_SHOWN = 8
_TAG_SCAN = 3000           # notes read for their tags on the first run


def near_at(model: str) -> float:
    """The floor for the configured embedder id, matched the way its config
    row is (config.embedder_defaults: the key inside the id); NEAR_AT for a
    model nobody measured."""
    m = (model or "").lower()
    return next((v for k, v in NEAR_AT_BY_MODEL.items() if k in m), NEAR_AT)


def near_notes(vault: str | None, stories: list[str]) -> None:
    """Fill `near` for the given stories, newest and largest first."""
    from silica.config import CONFIG
    from silica.kernel.recall.embed import get_store
    from silica.sources.discover import read

    with store.LOCK:
        doc = store.load(vault)
    todo = [sid for sid in stories if sid in doc["stories"] and doc["stories"][sid].get("near") is None]
    todo.sort(key=lambda s: (doc["stories"][s]["updated"], len(doc["stories"][s]["articles"])), reverse=True)
    todo = todo[:_NEAR_PER_READ]
    try:
        es = get_store()
        if not len(es):
            return
    except Exception:
        # No index yet (a vault not embedded): nothing is near anything, and
        # the next read asks again.
        logger.debug("discover: vault index unavailable for near notes", exc_info=True)
        return
    vectors = store.load_vectors(vault)
    missing = [sid for sid in todo if sid not in vectors]
    if missing:
        texts = []
        for sid in missing:
            a = doc["articles"].get(doc["stories"][sid]["articles"][0]) or {"title": "", "lead": ""}
            texts.append(f"{a['title']}. {a['lead'][:240]}")
        got = read._embed(texts)
        if got is None:
            todo = [sid for sid in todo if sid in vectors]
        else:
            vectors.update({sid: got[i] for i, sid in enumerate(missing)})
    floor = near_at(CONFIG.embedding_model)
    found: dict[str, list[dict]] = {}
    for sid in todo:
        if sid not in vectors:
            continue
        hits = es.cosine_top_k([float(x) for x in vectors[sid]], k=_NEAR_K)
        found[sid] = [{"path": h["path"] + ".md", "name": h.get("name") or h["path"].rsplit("/", 1)[-1],
                       "score": round(float(h["score"]), 3)} for h in hits if h["score"] >= floor]
    if not found:
        return
    with store.LOCK:
        doc = store.load(vault)
        for sid, near in found.items():
            if sid in doc["stories"]:
                doc["stories"][sid]["near"] = near
        store.save(doc, vault)


def topic_of(path: str) -> str:
    """A note's topic: its top folder, the way the tree names it."""
    parts = path.replace("\\", "/").split("/")
    return parts[0] if len(parts) > 1 else ""


_TAGS = re.compile(r"(?m)^tags:[ \t]*(.*)$((?:\n[ \t]+-[ \t]*.+)*)")


def note_tags(front: str) -> list[str]:
    """The `tags:` of a frontmatter, in its three YAML spellings: a flow list
    (`[a, b]`), a bare line (`a, b`), or a block list of `- a` lines."""
    m = _TAGS.search(front)
    if not m:
        return []
    raw = m.group(1).strip().strip("[]") + "," + ",".join(
        ln.strip()[1:] for ln in m.group(2).splitlines() if ln.strip().startswith("-"))
    return [t for t in (x.strip().strip("'\"").lstrip("#") for x in re.split(r"[,\s]+", raw)) if t and t != "-"]


def vault_topics(vault: str | None = None) -> list[dict]:
    """Topics suggested from the vault's folders and tags, biggest first:
    [{key, label, why, notes}], and for a tag the notes carrying it. A folder
    or tag of fewer than three notes is not a topic yet."""
    from silica.config import CONFIG
    from silica.driver import get_driver

    try:
        notes, _u, _g = get_driver().graph_data()
    except Exception:
        # No readable vault, no suggestions: the first run still offers the
        # four starter buckets.
        logger.debug("discover: vault topics unavailable", exc_info=True)
        return []
    folders: dict[str, int] = {}
    tags: dict[str, int] = {}
    tagged: dict[str, list[str]] = {}
    root = Path(vault or CONFIG.vault_path)
    for i, path in enumerate(notes):
        top = topic_of(path)
        if top and top.lower() not in NOT_A_TOPIC and not top.startswith("."):
            folders[top] = folders.get(top, 0) + 1
        if i < _TAG_SCAN:
            try:
                head = (root / path).read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            if head.startswith("---"):
                for t in note_tags(head.split("\n---", 1)[0]):
                    if t.lower() not in ("inbox", "web-research", "web-fetch", "discover"):
                        tags[t] = tags.get(t, 0) + 1
                        tagged.setdefault(t, []).append(path)
    out: list[dict[str, Any]] = [{"key": "f:" + k, "label": k, "why": f"folder, {n} notes", "notes": n}
           for k, n in folders.items() if n >= 3]
    out += [{"key": "t:" + k, "label": "#" + k, "why": f"tag, {n} notes", "notes": n, "paths": tagged[k][:500]}
            for k, n in tags.items() if n >= 3]
    out.sort(key=lambda t: -t["notes"])
    return out[:_TOPICS_SHOWN]


def in_topic(story: dict, key: str, members: dict[str, list[str]]) -> bool:
    """A story is in a topic when a note near it is: under the folder, or one
    of the notes that carried the tag when it was suggested."""
    near = [n["path"] for n in story.get("near") or []]
    if key.startswith("f:"):
        return any(topic_of(p) == key[2:] for p in near)
    return bool(set(near) & set(members.get(key) or ()))


def for_you(doc: dict, stories: list[dict]) -> list[dict]:
    """Stories in For you order: the vault first, then size and freshness."""
    now = time.time()
    topics = doc.get("topics") or {}
    members = doc.get("topic_paths") or {}
    on = [k for k, v in topics.items() if v]
    off = [k for k, v in topics.items() if not v]
    liked_tags: dict[str, float] = {}
    for sid, fb in doc.get("feedback", {}).items():
        s = doc["stories"].get(sid)
        for t in (s or {}).get("tags") or []:
            liked_tags[t] = liked_tags.get(t, 0) + (0.4 if fb > 0 else -0.4)
    for sid in doc.get("saved", {}):
        for t in (doc["stories"].get(sid) or {}).get("tags") or []:
            liked_tags[t] = liked_tags.get(t, 0) + 0.3

    def score(s: dict) -> float:
        fb = doc.get("feedback", {}).get(s["id"], 0)
        near = s.get("near") or []
        age_h = max(0.0, (now - s["updated"]) / 3600)
        v = math.log1p(len(s["articles"])) + 2.0 * (near[0]["score"] if near else 0.0)
        v += sum(0.8 for k in on if in_topic(s, k, members)) - sum(1.5 for k in off if in_topic(s, k, members))
        v += sum(liked_tags.get(t, 0) for t in s.get("tags") or [])
        v += 2.0 * fb
        return v - age_h / 24

    return sorted((s for s in stories if doc.get("feedback", {}).get(s["id"], 0) >= 0), key=score, reverse=True)
