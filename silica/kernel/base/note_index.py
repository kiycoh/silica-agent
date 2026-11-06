# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's note index: what every note states about itself, read once.

Below report, learn and the workspaces, because each of them needs it: it lived
as a private of a Learn report module, 20+ call sites read it from there, and
the report's own imports of it made the two lanes a cycle.

Memoized on the vault's file-state epoch, so repeated reads between vault
changes cost one stat walk instead of a full re-parse. No body is held: the YAML
was the cost, and the bodies were a whole second copy of the vault, 102 MiB of
t20's 1 GB before anyone opened Learn (2026-11-06).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from silica.kernel.base import frontmatter, layout
from silica.kernel.base.noteshape import STAGED, is_spine, note_clock, note_kind
from silica.kernel.base.syntax import WIKILINK_TARGET_RE

_meta_memo: dict[str, tuple[str, dict[str, dict]]] = {}  # vault -> (epoch, meta)
_fm_memo: dict[str, tuple[str, dict[str, dict]]] = {}  # vault -> (epoch, path -> frontmatter)
_parsed_memo: dict[str, tuple[tuple[int, int], Any]] = {}  # file -> ((mtime_ns, size), frontmatter)


def created_and_ai(text: str, mtime: float) -> tuple[float, bool]:
    """A note's creation timestamp and authorship from its own frontmatter."""
    ts, ai, _kind, _extra = note_meta(text, mtime)
    return ts, ai


def note_meta(text: str, mtime: float) -> tuple[float, bool, str, dict]:
    """(created ts, AI-authored, kind, extras) from one parse. The extras hold
    what Learn's title lookups read, only when present: the winner a merge loser
    points at, the note's aliases, whether it is a lesson spine.

    Same date precedence as the timeline: explicit `date:` outranks the claim
    clock, and a note that states neither falls back to mtime — the ceiling
    AttentionCandidate already lives with.
    """
    data, _raw, _body = frontmatter.split(text)
    date = (data or {}).get("date") or note_clock(text)
    ts = mtime
    if date:
        try:
            ts = datetime.strptime(str(date)[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            pass  # unparseable statement: keep the mtime proxy
    data = data or {}
    extra: dict = {}
    winner = WIKILINK_TARGET_RE.search(str(data.get("superseded_by") or ""))
    if winner:
        extra["winner"] = winner.group(1).strip()
    if aliases := frontmatter.aliases_in(data):
        extra["aliases"] = aliases
    if is_spine(data):
        extra["spine"] = True
    return ts, bool(data.get("AI")), note_kind(data), extra


def parsed(vault: Path, path: str, *, body: bool = True) -> tuple[Any, str] | None:
    """(frontmatter, body) of a note, its YAML parsed once per file state; None
    when it cannot be read. A Learn session read every note's YAML three times
    (contrast pairs, conflicts, the plan list), about 4 s each on a 2,166-note
    vault (2026-10-23, profiled). The frontmatter is shared: read it, never
    change it.

    body=False is for a caller that wants the frontmatter alone: a hit then
    opens no file, and the body comes back empty."""
    f = vault / path
    try:
        st = f.stat()
    except OSError:
        return None
    state = (st.st_mtime_ns, st.st_size)
    hit = _parsed_memo.get(str(f))
    if hit is not None and hit[0] != state:
        hit = None
    if hit is not None and not body:
        return hit[1], ""
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if hit is not None:
        return hit[1], frontmatter.body_of(text)
    data, _raw, rest = frontmatter.split(text)
    _parsed_memo[str(f)] = (state, data)
    return data, rest if body else ""


def meta(vault: Path) -> dict[str, dict]:
    """{relative path: {"created", "ai", "mtime", "kind", ...}} for every readable
    note of the reader.

    The pipeline's own staging (the inbox, the archive, the run log) is walked,
    since a workspace may list an inbox handout, but its kind is "staged", which
    is not study material.
    """
    epoch = layout.vault_epoch(str(vault))
    if epoch:
        hit = _meta_memo.get(str(vault))
        if hit is not None and hit[0] == epoch:
            return hit[1]

    out: dict[str, dict] = {}
    for rel, f in layout.iter_notes(vault):
        try:
            text = f.read_text(encoding="utf-8")
            mtime = f.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable note must not blind the view
        created, ai, kind, extra = note_meta(text, mtime)
        # Converted text waiting in the inbox or filed in the archive carries
        # the paper's own `date:` and no `AI: true`, so it read as notes the
        # reader wrote years ago: a fresh vault of six papers opened Learn on
        # "8 questions waiting from earlier sessions", a references segment
        # among them (2026-10-26).
        if layout.is_staging_path(rel):
            kind = STAGED
        # mtime rides along for the picker's freshness rule (learn-suite D13):
        # a note edited after its last rating is asked as new material.
        out[rel] = {"created": created, "ai": ai, "mtime": mtime, "kind": kind, **extra}

    if epoch:
        _meta_memo.clear()
        _meta_memo[str(vault)] = (epoch, out)
    return out


def frontmatters(vault: Path) -> dict[str, dict]:
    """{path: frontmatter} for every readable note, since `where:` and `sources:`
    need every note's properties and a sidebar refresh asks for them often.

    ponytail: one pass per vault change; ADR-0109 point 2 sets the ceiling for
    the same pass (100 ms at p90 on a 2,000-note vault), cache it per index
    generation past that.
    """
    epoch = layout.vault_epoch(str(vault))
    hit = _fm_memo.get(str(vault))
    if epoch and hit is not None and hit[0] == epoch:
        return hit[1]
    out: dict[str, dict] = {}
    for path in meta(vault):
        note = parsed(vault, path, body=False)
        if note is not None:  # one unreadable note must not blind the walk
            out[path] = note[0] or {}
    if epoch:
        _fm_memo.clear()
        _fm_memo[str(vault)] = (epoch, out)
    return out
