# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault as the check reads it, and the pool of notes that may be a
checked note's sources (ADR-0110 point 3).

The pool exists for one reason: a check of one client's draft must never send
another client's contract to the model, nor cite it. The review's run of
2026-10-15 put a different client's chronology into the top five for a
Northwind draft, on shared Italian words.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from silica.kernel.write import frontmatter

logger = logging.getLogger(__name__)

# English and Italian (the owner's C, 2026-10-24). A default applies only
# where a note carries it, so a vault with none of them is untouched.
DEFAULT_PARTY_KEYS = ("client", "matter", "patient", "cliente", "pratica", "paziente")
_WIKILINK = re.compile(r"!?\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")


@dataclass
class Note:
    path: str                       # vault-relative, with its .md
    text: str
    fm: dict = field(default_factory=dict)   # keys case-folded

    @property
    def stem(self) -> str:
        return Path(self.path).stem


def fold(s: str) -> str:
    """Case, brackets and punctuation folded away: "Brindle v Castor Holdings
    [2025] EWHC 2214 (Comm)" meets the note "Brindle v Castor Holdings 2025
    EWHC 2214 Comm"."""
    return " ".join(re.sub(r"[\W_]+", " ", (s or "").casefold()).split())


def walk(vault: str) -> tuple[dict[str, Note], set[str]]:
    """(every Markdown note by path, every file's case-folded basename).
    Hidden folders are never the person's material (.silica, .obsidian, .trash)."""
    root = Path(vault)
    notes: dict[str, Note] = {}
    files: set[str] = set()
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or not p.is_file():
            continue
        files.add(p.name.casefold())
        if p.suffix.lower() != ".md":
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # an unreadable file is no one's source; the check goes on without it
        data, _raw, _body = frontmatter.split(text)
        fm = {str(k).casefold(): v for k, v in (data or {}).items()} if isinstance(data, dict) else {}
        notes[rel.as_posix()] = Note(rel.as_posix(), text, fm)
    return notes, files


def party_keys(vault: str) -> tuple[str, ...]:
    """`check_party_keys:` in vault.yaml when it is a list of strings, else the
    defaults. Read here and not through vault_manifest, which parses only the
    keys Silica writes with; a malformed list falls back to the defaults."""
    path = Path(vault) / "vault.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except Exception:
        logger.warning("vault.yaml unreadable; the check uses the default party keys")
        return DEFAULT_PARTY_KEYS
    keys = raw.get("check_party_keys") if isinstance(raw, dict) else None
    if isinstance(keys, list) and keys and all(isinstance(k, str) and k.strip() for k in keys):
        return tuple(k.strip().casefold() for k in keys)
    return DEFAULT_PARTY_KEYS


def values_of(note: Note, keys: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for k in keys:
        v = note.fm.get(k)
        for item in (v if isinstance(v, list) else [v]):
            if item is not None and str(item).strip():
                out.add(" ".join(str(item).casefold().split()))
    return out


class Resolver:
    """Wikilink targets and title mentions to note paths, over one walk."""

    def __init__(self, notes: dict[str, Note], files: set[str]):
        self.notes, self.files = notes, files
        self.by_name: dict[str, list[str]] = {}
        self.titles: list[tuple[str, str]] = []
        for path, n in notes.items():
            aliases = n.fm.get("aliases")
            names = [n.stem] + ([str(a) for a in aliases] if isinstance(aliases, list) else [str(aliases)] if aliases else [])
            for name in names:
                self.by_name.setdefault(name.casefold(), []).append(path)
                t = fold(name)
                # A title of one or two plain words ("Chronology", "Case
                # summary") is a word a writer uses, not a citation of the note.
                if len(t.split()) >= 3 or (len(t.split()) == 2 and re.search(r"\d", t)):
                    self.titles.append((t, path))

    def resolve(self, target: str) -> str | None:
        """The note a link names; "" for an existing non-note file or an
        ambiguous name (several notes share it: ADR-0114, never resolved
        silently), None when nothing in the vault answers to it."""
        t = target.strip().replace("\\", "/")
        if not t:
            return None
        suffix = Path(t).suffix.lower()
        if suffix and suffix != ".md":
            return "" if Path(t).name.casefold() in self.files else None
        t = t.removesuffix(".md")
        if "/" in t:
            want = (t + ".md").casefold()
            hits = [p for p in self.notes if p.casefold() == want or p.casefold().endswith("/" + want)]
        else:
            hits = self.by_name.get(t.casefold(), [])
        if len(hits) == 1:
            return hits[0]
        return "" if hits else None

    def links(self, text: str) -> list[tuple[str, str | None]]:
        """(target as written, resolved path or ""/None) for each wikilink."""
        return [(m.group(1).strip(), self.resolve(m.group(1))) for m in _WIKILINK.finditer(text or "")]

    def mentions(self, text: str) -> set[str]:
        folded = f" {fold(text)} "
        return {path for title, path in self.titles if f" {title} " in folded}


@dataclass
class Pool:
    paths: set[str]
    scope: str            # whole | party | shared
    values: set[str]      # the checked note's party values, one hop included
    of: int               # notes the pool was cut from


def build(checked: str, notes: dict[str, Note], keys: tuple[str, ...], write_dir: str,
          named: set[str]) -> Pool:
    """The notes that may be `checked`'s sources.

    Never: the note itself, anything under the write boundary (its copies read
    `distilled`), Silica's own root files. Never another party's material,
    unless the person links or names it (`named`)."""
    from silica.kernel.recall.graph_export import is_vault_artifact

    wd = (write_dir or "").strip("/")
    candidates = {p for p in notes if p != checked and not is_vault_artifact(p)
                  and not (wd and (p == wd or p.startswith(wd + "/")))}
    parties = {p: values_of(notes[p], keys) for p in candidates}
    if not any(parties.values()):
        return Pool(candidates, "whole", set(), len(candidates))
    own = values_of(notes[checked], keys) if checked in notes else set()
    values = set(own)
    for p, vs in parties.items():
        if vs & own:
            values |= vs
    keep = {p for p, vs in parties.items() if vs <= values or p in named}
    return Pool(keep, "party" if own else "shared", values, len(candidates))
