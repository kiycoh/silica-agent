# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a source check reads with: the search, the dense leg, the web legs and the judge.

The wiring between `kernel/check`, which takes them as arguments, and the model and
index behind them. Both `/check` and the drawer's `check sources` ask here.
"""
from __future__ import annotations


def judge(cite: bool = True):
    """Jev decides a block's verdict; the chat model answers only when Jev
    answers nothing (ADR-0110 point 7, the owner's decision of 2026-10-24)."""
    from silica.model.decide import decide
    from silica.model.providers import get_provider
    from silica.config import CONFIG
    from silica.kernel.check import judge as jd

    def chat(prompt: str) -> str:
        # reasoning off: a thinking pass spends the budget before the JSON
        # (llm.py, the 9B's empty answers of 2026-10-15).
        resp = get_provider(CONFIG).call_llm(messages=[{"role": "user", "content": prompt}],
                                             max_tokens=300, temperature=0.0, reasoning=False)
        return resp.text or ""

    return lambda text, passages: jd.judge(text, passages, decide=decide, chat=chat, cite=cite)


def web_legs():
    """What a selection's web check reads with (ADR-0110, amendment of
    2026-11-06): the lanes and fetch_page, neither of which touches a chat
    turn's web state."""
    from silica.sources.web_fetch import fetch_page
    from silica.sources.web_research import search_lanes

    return search_lanes, fetch_page


def legs():
    """The passage search and the dense leg a /check reads with. The dense leg
    is the vault's own embed index; a vault without one, an embedder that is
    down, or an embedder with no measured floor gives None, and absence then
    waits for the model rather than be decided on words alone (ADR-0110
    point 6: an Italian block over English sources)."""
    from silica.config import CONFIG
    from silica.kernel.check import run as check
    from silica.kernel.recall.engine import engine

    def search(query: str, k: int) -> list[dict]:
        r = engine().search(query, folder="", k=k)
        return r.get("hits") or []

    floor = check.dense_floor(str(CONFIG.embedding_model or ""))
    if floor is None:
        return search, None, None
    try:
        from silica.model.providers import get_embedder
        from silica.kernel.check.dense import leg

        dense = leg(get_embedder(CONFIG))
    except Exception:
        return search, None, None  # no embedder configured: the dense leg is simply absent
    if dense is None:
        return search, None, None
    return search, dense, floor
