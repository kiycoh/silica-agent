# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Written prerequisites (ADR-0145): each note's `requires:` resolved to notes.

One walk of the vault per file-state epoch, in recall and not in the learner,
because the graph export reads it too: through the learner, graph_export
reached codegraph (learner -> graph_report.compute -> code_signals), which the
"structural is not semantic" import contract forbids for the fusion legs.
"""
from __future__ import annotations

from pathlib import Path

_memo: dict[tuple[str, str], dict[str, list[str]]] = {}   # (epoch, vault) -> map


def resolve(requires: dict[str, list[str]], aliases: dict[str, list[str]],
            paths: list[str]) -> dict[str, list[str]]:
    """{note path: [prerequisite paths]}. A name is a path, a file name or an
    alias; one that names several notes, or none, is skipped rather than
    guessed (ADR-0114), and a note never requires itself."""
    by_name: dict[str, set[str]] = {}
    for p in paths:
        for name in (p.removesuffix(".md"), Path(p).stem, *aliases.get(p, ())):
            by_name.setdefault(str(name).casefold(), set()).add(p)

    def one(name: str) -> str | None:
        hits = by_name.get(name.removesuffix(".md").casefold(), set())
        return next(iter(hits)) if len(hits) == 1 else None

    out: dict[str, list[str]] = {}
    for p, names in requires.items():
        got = [q for q in map(one, names) if q and q != p]
        if got:
            out[p] = list(dict.fromkeys(got))
    return out


def written_requires(vault: str | Path) -> dict[str, list[str]]:
    """{note path: [prerequisite paths]} for `vault`, over the reader's notes.
    Only a frontmatter that names `requires` is parsed as YAML."""
    from silica.kernel.base import layout

    key = str(Path(vault))
    return layout.on_epoch(_memo, key, lambda: _written(Path(vault)), key)


def _written(root: Path) -> dict[str, list[str]]:
    from silica.kernel.base.layout import iter_notes
    from silica.kernel.base import frontmatter

    paths: list[str] = []
    aliases: dict[str, list[str]] = {}
    requires: dict[str, list[str]] = {}
    for rel, f in iter_notes(root):
        paths.append(rel)
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable note must not blind the map
        if al := frontmatter.aliases_of(text):
            aliases[rel] = list(al)
        m = frontmatter.FM_RE.match(text)
        if m and "requires" in m.group(1):
            data, _raw, _body = frontmatter.split(text)
            if names := frontmatter.requires_in(data):
                requires[rel] = names
    return resolve(requires, aliases, paths) if requires else {}
