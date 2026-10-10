# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_session_search: the earlier chats with this vault, searched by their
words (ADR-0063).

The vault is what the user wrote; the narration is what they asked silica and
what it answered, and until now only /resume could reach it, whole and by
title. This ranks the messages themselves with the vault's own lexical leg,
so a question like "what did we settle about the reranker last week" finds the
exchange instead of the notes.

What is searched: user and assistant text in this vault's TUI and GUI
sessions, minus the open one (it is already in context) and minus slash
directives (harness text, not anyone's words). Tool results are left out: they
are vault content, which silica_search already reaches, and 98% of the turn
text (13.2 of 13.4 million characters in the 2026-10-06 store).
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from silica.tools import tool

_WINDOW = 400   # chars of a message shown around its first matching word
_PER_SESSION = 2   # a long session about one topic must not fill every slot


def _window(text: str, query: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _WINDOW:
        return text
    words = re.findall(r"\w{3,}", query.lower())
    low = text.lower()
    at = min((i for i in (low.find(w) for w in words) if i >= 0), default=0)
    start = max(0, min(at - _WINDOW // 2, len(text) - _WINDOW))
    return ("…" if start else "") + text[start:start + _WINDOW] + ("…" if start + _WINDOW < len(text) else "")


def _neighbour(msgs: list[dict], i: int) -> tuple[str, int] | None:
    """The other half of the exchange: the answer a question got, or the
    question an answer was for."""
    want, step = ("assistant", 1) if msgs[i].get("role") == "user" else ("user", -1)
    j = i + step
    while 0 <= j < len(msgs):
        m = msgs[j]
        if m.get("role") == "user" and (want == "assistant" or m.get("origin") == "cli"):
            return None   # a new turn began, or a directive asked: no human other half
        if m.get("role") == want and isinstance(m.get("content"), str) and m["content"].strip():
            return ("answer" if want == "assistant" else "asked"), j
        j += step
    return None


@tool(cls="composed")
def silica_session_search(
    query: Annotated[str, Field(description="Words the earlier exchange used")],
    k: Annotated[int, Field(ge=1, le=30, description="Maximum number of matching messages")] = 8,
) -> dict[str, Any]:
    """Search this vault's earlier chat sessions for what was asked and answered.

    Returns matching messages with their session id, title and last-active
    date, each with the other half of its exchange. For what the notes say,
    use silica_search instead."""
    from silica.agent import narration
    from silica.config import CONFIG
    from silica.kernel.recall.lexical import LexicalStore

    vault = CONFIG.vault_path or ""
    # ponytail: every session file is read per call: 0.17 s for the largest
    # vault's 39 sessions, most of it list_sessions walking all 350 files
    # (59 MB, measured 2026-10-06). An index of turn text pays off at the 5000
    # sessions where list_sessions itself is due to be reopened.
    store = LexicalStore(Path(os.devnull))   # in memory: never synced, never saved
    turns: dict[str, tuple[dict, list[dict], int]] = {}
    searched = 0
    for row in narration.list_sessions(vault):
        if row["id"] == narration.NARRATOR.sid:
            continue
        msgs = narration.load_session_messages(row["id"], vault) or []
        searched += 1
        for i, m in enumerate(msgs):
            text = m.get("content")
            if (m.get("role") in ("user", "assistant") and m.get("origin") != "cli"
                    and isinstance(text, str) and text.strip()):
                key = f"{row['id']}#{i}"
                store.upsert(key, "", text)
                turns[key] = (row, msgs, i)

    hits: list[dict[str, Any]] = []
    per: dict[str, int] = {}
    shown: set[tuple[str, int]] = set()   # a question and its answer are one exchange, shown once
    for key, _score in store.rank(query, k=k * _PER_SESSION * 3):
        row, msgs, i = turns[key]
        if per.get(row["id"], 0) >= _PER_SESSION or (row["id"], i) in shown:
            continue
        per[row["id"]] = per.get(row["id"], 0) + 1
        hit = {"session": row["id"], "title": row["title"],
               "updated": dt.date.fromtimestamp(row["updated"]).isoformat(),
               "role": msgs[i]["role"], "text": _window(msgs[i]["content"], query)}
        shown.add((row["id"], i))
        other = _neighbour(msgs, i)
        if other:
            hit[other[0]] = _window(msgs[other[1]]["content"], query)
            shown.add((row["id"], other[1]))
        hits.append(hit)
        if len(hits) >= k:
            break
    return {"query": query, "sessions_searched": searched, "hits": hits}
