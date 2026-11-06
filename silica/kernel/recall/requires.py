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

_memo: dict[str, tuple[str, dict[str, list[str]]]] = {}   # vault -> (epoch, map)


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
    """{note path: [prerequisite paths]} for `vault`, the walk the learner's
    notes view makes (dot dirs, .silicaignore and the verbatim sources are not
    notes). Only a frontmatter that names `requires` is parsed as YAML."""
    from silica.kernel.base.layout import SOURCES_DIR, ignore_matcher, vault_epoch
    from silica.kernel.base import frontmatter

    root = Path(vault)
    key = str(root)
    epoch = vault_epoch(key)
    hit = _memo.get(key)
    if epoch and hit is not None and hit[0] == epoch:
        return hit[1]
    ignored = ignore_matcher(root)
    paths: list[str] = []
    aliases: dict[str, list[str]] = {}
    requires: dict[str, list[str]] = {}
    for f in sorted(root.rglob("*.md")):
        parts = f.relative_to(root).parts
        if (any(p.startswith(".") for p in parts) or any(ignored(p) for p in parts[:-1])
                or parts[0] == SOURCES_DIR):
            continue
        rel = "/".join(parts)
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
    out = resolve(requires, aliases, paths) if requires else {}
    if epoch:
        _memo.clear()
        _memo[key] = (epoch, out)
    return out
