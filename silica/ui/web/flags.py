# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Which turn of which chat a flag points at (ADR-0100 point 6), read off the narration."""
from __future__ import annotations

import json

from silica.ui.web.session import SESSION


def session_beats(sid: str) -> list[dict]:
    from silica.obs import narration as _narr

    return list(_narr.read_beats(_narr.narration_dir() / f"{sid}.jsonl"))


def user_turn_seqs(beats: list[dict]) -> list[int]:
    """The seqs of the user `turn` beats, in file order."""
    from silica.obs.narration import REWIND

    out = []
    for b in beats:
        p = b.get("payload")
        m = p.get("message") if isinstance(p, dict) else None
        if b.get("kind") == "turn" and isinstance(m, dict) and m.get("role") == "user" and b.get("seq"):
            out.append(b["seq"])
        elif b.get("kind") == REWIND and out:
            out.pop()   # the question asked again left the conversation (ADR-0121 point 7)
    return out


def flagged(sid: str) -> set[int]:
    """The answers of `sid` already flagged, so a reopened chat says so and a
    second click is not a second flag."""
    from silica.config import silica_home

    try:
        lines = (silica_home() / "flags.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    out = set()
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue  # a torn last line from a full disk: the other flags still count
        if isinstance(rec, dict) and rec.get("session") == sid and isinstance(rec.get("answer"), int):
            out.add(rec["answer"])
    return out


def plain_sid(sid: object) -> str:
    """A session id as a file stem, or "" when it could name anything else."""
    s = str(sid or "")
    return s if s.isascii() and s.isalnum() else ""


def session_head(sid: str | None) -> int:
    """The last seq written to `sid`, 0 for a session not yet opened."""
    sid = plain_sid(sid)
    return max((b.get("seq") or 0 for b in session_beats(sid)), default=0) if sid else 0


def turn_since(sid0: str | None, seq0: int) -> dict | None:
    """The turn a flag would point at: the first user beat narrated past where
    the session stood before the turn. None when this turn narrated none (the
    session was busy in another process), so no flag can point at a neighbour."""
    sid = plain_sid(SESSION.chat.sid)
    if not sid:
        return None
    after = seq0 if sid == sid0 else 0
    seqs = [s for s in user_turn_seqs(session_beats(sid)) if s > after]
    return {"session": sid, "seq": seqs[0]} if seqs else None
