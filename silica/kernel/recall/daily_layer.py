# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A daily note in the graph (ADR-0062 Decisions 5 to 7).

A daily note is related to the vault by its blocks, one at a time, and never
by its whole text. Measured on the founder's vault (2026-10-06): 73 to 88% of
the 1200 characters a note is embedded by were the daily template's Tasks
query blocks, identical every day, so each day's four nearest notes were the
four other days. A block alone (one task, one paragraph) landed on a note
that is about it.

The block vectors live in their own store, keyed by the block's text. Not in
the note store: `cosine_top_k_batch` would hand blocks back as the neighbours
of notes.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from silica.kernel.calendar.daily import daily_date, daily_notes, daily_rule
from silica.kernel.calendar.tasks import FIELD_START
from silica.kernel.recall.graph_export import _NODE_DEFAULT_COLOR, similar_edge

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[.\]\s*)?")
# One request's worth of blocks. A block is a line or a paragraph, so 64 of
# them is about the size of the 32-note batches build_index sends.
_BATCH = 64


def blocks(text: str) -> list[str]:
    """The blocks of a note: list items and paragraphs, in order.

    Fenced code is dropped (a ```tasks query is a view, not content), and so
    are headings: a template's "## Da fare oggi" is the same every day and
    would relate every day to the same note. Tasks' signifiers are cut off a
    list item, since a due date says nothing about what the task is about.
    """
    from silica.kernel.write import frontmatter

    _, _, body = frontmatter.split(text)
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(" ".join(para))
            para.clear()

    fenced = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            flush()
            fenced = not fenced
            continue
        if fenced or _HEADING_RE.match(line):
            flush()
            continue
        item = _ITEM_RE.match(line)
        if item:
            flush()
            rest = line[item.end():]
            cut = FIELD_START.search(rest)
            rest = (rest[:cut.start()] if cut else rest).replace("**", "").strip()
            if rest:
                out.append(rest)
            continue
        if line.strip():
            para.append(line.strip())
        else:
            flush()
    flush()
    return out


def block_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


_STORES: dict[str, Any] = {}


def block_store():
    """The current vault's block store, one instance per file like get_store()."""
    from silica.kernel.recall import paths
    from silica.kernel.recall.embed import EmbedStore

    path = paths.index_file("daily_blocks")
    store = _STORES.get(str(path))
    if store is None:
        store = _STORES[str(path)] = EmbedStore(path)
    return store


def refresh_blocks(embedder: Any, vault: Path, *, store=None) -> int:
    """Embed every daily-note block the store lacks; drop the ones gone. Returns
    how many were embedded. Saves only when something changed, since an index
    rewritten unchanged still moves the mtime the GUI's "vault changed" reads."""
    from silica.kernel.recall.embed import embedder_identity

    store = store if store is not None else block_store()
    model = embedder_identity(embedder)
    wanted: dict[str, str] = {}
    for rel in daily_notes(vault):
        try:
            text = (Path(vault) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue   # its blocks stay as they were and are retried next export
        for b in blocks(text):
            wanted[block_key(b)] = b
    # A block embedded by another model is stale: its vector cannot be
    # compared with the notes the current model embedded.
    stale = [k for k in wanted
             if store.get_vec(k) is None or store.get_content_hash(k) != model]
    for i in range(0, len(stale), _BATCH):
        batch = stale[i:i + _BATCH]
        vecs = embedder.embed([wanted[k] for k in batch])
        if len(vecs) != len(batch):
            raise RuntimeError(f"embedder returned {len(vecs)} vectors for {len(batch)} blocks")
        for k, v in zip(batch, vecs):
            store.upsert(k, wanted[k][:80], v, content_hash=model)
    for k in [k for k in store.paths() if k not in wanted]:
        store.delete(k)
    if store.is_dirty():
        store.save()
    return len(stale)


def stamp_daily(nodes: list[dict], vault: Path) -> None:
    """Type the vault's daily notes `daily` and give each its day, in place."""
    rule = daily_rule(Path(vault))
    if rule is None:
        return
    for n in nodes:
        if n.get("type") == "ghost":
            continue
        day = daily_date(n["id"], rule)
        if day is not None:
            n["type"] = "daily"
            n["daily"] = day.isoformat()


def daily_edges(nodes: list[dict]) -> list[dict]:
    """One SIMILAR edge from each daily note to each note one of its blocks is
    nearest to, at the best block's score (ADR-0062 Decision 5).

    No threshold: on the 13 tasks measured, a miss at 0.423 sat above a hit at
    0.399, so no cut separates them on the data there is.
    """
    from silica.config import CONFIG
    from silica.kernel.recall.cooccurrence import cooccur_key
    from silica.kernel.recall.embed import get_store

    days = [n for n in nodes if n.get("type") == "daily"]
    if not days:
        return []
    store, bstore = get_store(), block_store()
    if len(store) == 0 or len(bstore) == 0:
        return []
    id_of = {cooccur_key(n["id"]): n["id"] for n in nodes
             if n.get("type") not in ("ghost", "daily")}
    exclude = {cooccur_key(n["id"]) for n in days}
    vault = Path(CONFIG.vault_path or ".")
    edges: list[dict] = []
    for d in days:
        try:
            text = (vault / d["id"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue   # the day keeps its node; it just draws no relation
        best: dict[str, float] = {}
        for b in blocks(text):
            vec = bstore.get_vec(block_key(b))
            if vec is None:
                continue
            # k=5 and the first hit in the graph: the nearest note can sit
            # outside a folder-scoped build, and the next one in it still says
            # what the block is about.
            for hit in store.cosine_top_k(vec, k=5, exclude=exclude):
                tid = id_of.get(cooccur_key(hit["path"]))
                if tid is None:
                    continue
                # A 0.0 is a vector of another dimension (a model swap), not a
                # relation: the store scores what it cannot compare as zero.
                if hit["score"] > 0.0:
                    best[tid] = max(best.get(tid, 0.0), float(hit["score"]))
                break
        for tid, score in sorted(best.items()):
            edges.append(similar_edge(f"sd{len(edges)}", d["id"], tid, score))
    return edges


def neutral_lone_dailies(nodes: list[dict]) -> None:
    """A daily note alone in its community takes the neutral node colour.

    Louvain runs on the wikilinks, so a day with none is a community of one,
    and a year of them would be 365 hues for nothing a reader can use.
    """
    size: dict[int, int] = {}
    for n in nodes:
        if n.get("type") != "ghost":
            size[n.get("group", -1)] = size.get(n.get("group", -1), 0) + 1
    for n in nodes:
        if n.get("type") == "daily" and (n.get("group", -1) < 0 or size[n["group"]] == 1):
            n["color"] = dict(_NODE_DEFAULT_COLOR)
