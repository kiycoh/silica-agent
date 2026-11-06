# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A sentence a model writes about what the folder holds, and the hubs it is written from."""
from __future__ import annotations

import logging
from pathlib import Path

from silica.config import CONFIG

logger = logging.getLogger(__name__)


def top_hubs(nodes: list[dict], edges: list[dict], top_n: int = 24) -> list[dict]:
    """Best-connected notes by resolved-link degree — the map view's landing
    picker (a radial map must be rooted on one note, so 'most central' is the
    sensible entry point). Ghost/unlinked nodes are skipped."""
    from collections import Counter

    deg: Counter = Counter()
    for e in edges:
        if e.get("type") == "EXTRACTED":
            deg[e.get("from")] += 1
            deg[e.get("to")] += 1
    hubs = [
        {"name": n.get("label") or (n.get("path") or "").rsplit("/", 1)[-1],
         "path": n["path"], "degree": deg[n["id"]]}
        for n in nodes
        if n.get("type") != "ghost" and n.get("path") and deg[n["id"]] > 0
    ]
    hubs.sort(key=lambda h: (-h["degree"], h["name"].lower()))
    return hubs[:top_n]


# --- the vault brief ---------------------------------------------------------
# A sentence a model writes about what the folder holds, which silica_vaults
# hands the agent when it picks among vaults (vault_registry._brief). It was
# the chat landing's second line until the greeting took the landing (ADR-0048,
# amendment 2026-10-22); the page still asks for it at load, because nothing
# else writes it. A convenience and not a fact, so a toggle switches it off.
#
# It caches into the vault's index dir rather than the vault: Silica's own
# bookkeeping never lands in a folder the user reads.

def brief_path() -> Path:
    from silica.kernel.base import home

    return home.index_dir() / "vault_brief.json"


def write_brief(notes: int, topics: list[str], hubs: list[str]) -> str:
    """Ask the worker model for one sentence over evidence it cannot embellish.

    It is handed labels and note names, never note bodies: the sentence has to
    say what the collection is about, and a corpus of 1,200 notes has no
    summary that fits in one line anyway. Trimmed hard on the way out: a
    model asked for one sentence returns three often enough that no reader of
    it can be built on trusting it.
    """
    if not topics and not hubs:
        return ""
    from silica.model.providers import get_provider

    prompt = (
        "Below are the topic labels of the largest link clusters in a personal "
        "markdown vault, and the names of its best-connected notes.\n\n"
        f"Clusters: {', '.join(topics) or 'none'}\n"
        f"Best-connected notes: {', '.join(hubs) or 'none'}\n"
        f"Total notes: {notes}\n\n"
        "Write ONE sentence, at most 24 words, naming what this collection is "
        "about. Describe only what the labels above support. No preamble, no "
        "quotes, no note counts, no adjectives of praise. Reply with the "
        "sentence and nothing else."
    )
    try:
        provider = get_provider(CONFIG, role="worker")
        reply = provider.call_llm(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            response_schema=None,
            max_tokens=200,
        )
        out = (reply.text or "").strip().strip('"').split("\n")[0].strip()
    except Exception as exc:
        logger.info("vault brief unavailable: %s", exc)
        return ""
    return out[:240]
