# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Which note a reference means: a path or a title, against one read of the driver's graph.

Every surface that turns a name into a note asks here, so a link in a chat answer, a
note's drawer and the agent's own context resolve the same name to the same note. It sat
inside the mind map, whose layout changes for reasons of its own, and recall's
perception reached up into the graph lane for it.
"""
from __future__ import annotations

from silica.kernel.base import notekey


def _matches_in(note: str, note_paths: list[str], titles: dict[str, str]) -> list[str]:
    """Every graph key `note` (a path OR a title) can mean, sorted. Pure.

    Graph keys are full vault-relative paths WITH .md; a user (or the GUI input)
    may give a bare title or a path with/without .md. An exact path is the one
    answer; otherwise every note whose basename or title matches,
    case-insensitively.
    """
    keys = sorted(p.replace("\\", "/") for p in note_paths)
    key_set = set(keys)
    cand = note.replace("\\", "/").strip()
    for c in (cand, cand + ".md", cand.removesuffix(".md")):
        if c in key_set:
            return [c]
    target = notekey.stem(cand).lower()
    return [path for path in keys
            if notekey.stem(path).lower() == target
            or (titles.get(path, "") or "").lower() == target]


def _resolve_in(note: str, note_paths: list[str], titles: dict[str, str]) -> str | None:
    """Resolve `note` (a path OR a title) to a graph key; the first sorted
    match when several notes share the name, so it is stable."""
    hits = _matches_in(note, note_paths, titles)
    return hits[0] if hits else None


def driver_graph():
    """One driver read → (raw notes map, {path: title}, undirected wikilink graph)."""
    from silica.driver import get_driver

    notes, _unresolved, g = get_driver().graph_data()
    return (
        notes,
        {p.replace("\\", "/"): ref.name for p, ref in notes.items()},
        g.to_undirected(as_view=True) if hasattr(g, "to_undirected") else g,
    )


def note_resolver():
    """One driver read → a pure closure: ref (path or title) -> graph key | None.

    Reuse when resolving many refs per render (e.g. linkifying a message): the
    driver graph is read once, the returned callable does no further IO.
    """
    notes, titles, _g = driver_graph()
    paths = list(notes)
    return lambda ref: _resolve_in(ref, paths, titles)


def answer_resolver(read_refs=(), graph=None) -> tuple:
    """(resolve, read) for a chat answer, from one driver read (ADR-0127);
    `graph`, a `driver_graph()` already read, spares it for many answers.

    `read` is the notes the turn read, as graph keys: each of `read_refs` that
    names exactly one note. `resolve` differs from note_resolver's on a name
    several notes share (ADR-0114: never resolved silently): it is the one of
    them the turn read, or "" when that is not exactly one. An answer that
    named "Lezione 16" from one course used to open the other course's
    lecture, whichever sorted first. "" and not None, because None draws a
    broken link, and the note is there, twice.
    """
    notes, titles, _g = graph or driver_graph()
    paths = list(notes)
    read = sorted({hits[0] for r in read_refs
                   if len(hits := _matches_in(str(r), paths, titles)) == 1})
    mine = set(read)

    def resolve(ref: str) -> str | None:
        hits = _matches_in(ref, paths, titles)
        if len(hits) < 2:
            return hits[0] if hits else None
        ours = [h for h in hits if h in mine]
        return ours[0] if len(ours) == 1 else ""

    return resolve, read
