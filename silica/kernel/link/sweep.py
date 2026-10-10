# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""End-of-run dangling-link sweep.

Validate keeps unresolved wikilinks during a nucleate run on purpose: a chunk
may reference a note a later chunk of the same run will create (forward-refs,
see validate_operations step 4). What that design never had was the closing
half — once the run is over, a forward-ref that never materialized is not a
forward-ref any more, it is a broken link the run itself manufactured
(measured 2026-08-15: 127 of 469 links dangling after one paper).

`sweep_dangling_links` runs after the FSM and its repair sub-agents finish:
for every note the run wrote, wikilinks whose target still does not resolve
are unlinked back to plain text (`[[X]]` → `X`, `[[X|alias]]` → `alias`) and
unresolved `related:` frontmatter entries are dropped. A target that resolves
only under a folded spelling is repointed instead of stripped (`[[Ahura
Mazda]]` → `[[Ahura-Mazda|Ahura Mazda]]`, see `_make_resolver`). Deterministic,
no LLM.
Notes the run touched are the only ones edited, so a run-level /revert (which
deletes those notes) already covers the sweep's edits.

Scoped to nucleate runs: a human typing `[[Future Note]]` in Obsidian is
expressing intent; the sweep never touches notes it did not just write.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from silica.kernel.link.ast import NON_MD_EXTENSIONS

logger = logging.getLogger(__name__)

# [[Target]], [[Target|alias]], [[Target#anchor]], and the embed form ![[…]].
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]|#\n]+)(#[^\]|\n]*)?(?:\|([^\]\n]*))?\]\]")

# A quoted wikilink entry in a `related:` YAML list — either quote style and
# either indentation (`  - "[[X]]"` and column-0 `- '[[X]]'` both occur).
_RELATED_ENTRY_RE = re.compile(r"^[ \t]*-[ \t]+[\"']?\[\[([^\]|#\n]+)[^\]\n]*\]\][\"']?[ \t]*$")


def _fold(name: str) -> str:
    """Case, punctuation and accents dropped: `Ahura-Mazda` → `ahura mazda`."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _make_resolver(extra_names: set[str] | None = None):
    """target → the note name it resolves to, "" when nothing does; cached.

    Exact first, mirroring validate's link check and Obsidian's own rule. Only
    on a miss does it consult a folded index of the vault's names: one segment
    of a run writes `Ahura-Mazda.md` while another writes `[[Ahura Mazda]]`, and
    treating that as dangling deleted an edge between two notes of the same
    batch (7 of the 151 links stripped on one book, measured 2026-08-16). The
    fold is exact after folding — no prefix, no ratio — so it can only ever
    match another spelling of the same name. Callers repoint rather than strip.
    """
    from silica.driver import DRIVER

    cache: dict[str, str] = {}
    extra = {n.lower(): n for n in (extra_names or set())}
    folded: dict[str, str] | None = None

    def _folded_index() -> dict[str, str]:
        # Built once per sweep and only when an exact match has already failed.
        nonlocal folded
        if folded is None:
            folded = {}
            try:
                for r in DRIVER.search_names(""):
                    folded.setdefault(_fold(r.name), r.name)
            except Exception:
                folded = {}
            for low, name in extra.items():
                folded.setdefault(_fold(low), name)
        return folded

    def resolves(target: str) -> str:
        stem = target.strip().removesuffix(".md")
        key = stem.lower()
        if key in cache:
            return cache[key]
        if key in extra:
            cache[key] = stem
            return stem
        try:
            if "/" in stem:

                def _path_exists(p: str) -> bool:
                    try:
                        DRIVER.read_note(p)
                        return True
                    except RuntimeError:
                        return False

                result = stem if (_path_exists(stem + ".md") or _path_exists(stem)) else ""
            elif any(r.name.lower() == key for r in DRIVER.search_names(stem)):
                result = stem
            else:
                result = _folded_index().get(_fold(stem), "")
        except Exception:
            result = stem  # resolution failure must never strip a link
        cache[key] = result
        return result

    return resolves


def _unlink_body(body: str, resolves) -> tuple[str, list[str], list[str]]:
    """Unlink what does not resolve, repoint what resolves under another
    spelling; return (new, stripped, relinked)."""
    stripped: list[str] = []
    relinked: list[str] = []

    def _sub(m: re.Match) -> str:
        embed, target, anchor, alias = m.group(1), m.group(2), m.group(3), m.group(4)
        t = target.strip()
        # Embeds and non-note targets (images, PDFs) are not note links.
        if embed or t.lower().endswith(NON_MD_EXTENSIONS):
            return m.group(0)
        hit = resolves(t)
        if hit == t:
            return m.group(0)
        if hit:
            # The display text is what the reader already sees; only the target
            # moves, so the note reads exactly as before and the edge is real.
            relinked.append(hit)
            return f"[[{hit}{anchor or ''}|{(alias or t).strip()}]]"
        stripped.append(t)
        return (alias or t).strip()

    return _WIKILINK_RE.sub(_sub, body), stripped, relinked


def _prune_related(fm_text: str, resolves) -> tuple[str, list[str]]:
    """Drop unresolved `related:` entries from the raw frontmatter text."""
    out: list[str] = []
    dropped: list[str] = []
    in_related = False
    for line in fm_text.splitlines(keepends=True):
        if re.match(r"^related:\s*$", line):
            in_related = True
            out.append(line)
            continue
        if in_related:
            m = _RELATED_ENTRY_RE.match(line)
            if m:
                entry = m.group(1).strip()
                hit = resolves(entry)
                if hit == entry:
                    out.append(line)
                elif hit:
                    out.append(line.replace(m.group(1), hit, 1))
                else:
                    dropped.append(entry)
                continue
            # The list continues on indented lines or further dash items
            # (column-0 lists occur); any other top-level key ends it.
            in_related = bool(re.match(r"^[ \t]+", line) or re.match(r"^[ \t]*- ", line))
        out.append(line)
    return "".join(out), dropped


def sweep_note(rel_path: str, resolves) -> dict | None:
    """Sweep one note; returns {"path", "stripped", "relinked"} when edited."""
    from silica.driver import DRIVER
    from silica.kernel.write.frontmatter import FM_RE

    try:
        nc = DRIVER.read_note(rel_path)
    except Exception:
        return None
    content = nc.content
    if m := FM_RE.match(content):
        fm_new, dropped = _prune_related(m.group(0), resolves)
        body_new, stripped, relinked = _unlink_body(content[m.end():], resolves)
        new = fm_new + body_new
    else:
        new, stripped, relinked = _unlink_body(content, resolves)
        dropped = []
    if new == content:
        return None
    try:
        DRIVER.overwrite(rel_path, new)
    except Exception as e:
        logger.warning("link sweep: overwrite failed for %s (left as-is): %s", rel_path, e)
        return None
    return {
        "path": rel_path,
        "stripped": sorted(set(stripped + dropped)),
        "relinked": sorted(set(relinked)),
    }


def sweep_dangling_links(note_paths: list[str]) -> dict:
    """Unlink still-dangling wikilinks in the given run-written notes.

    `note_paths` are vault-relative, `.md` optional. Returns
    {"notes_edited": N, "links_stripped": M, "links_relinked": K, "details": [...]}.
    """
    resolves = _make_resolver()
    details: list[dict] = []
    for p in note_paths:
        rel = p if p.endswith(".md") else p + ".md"
        r = sweep_note(rel, resolves)
        if r:
            details.append(r)
    total = sum(len(d["stripped"]) for d in details)
    moved = sum(len(d["relinked"]) for d in details)
    if total or moved:
        logger.info(
            "link sweep: unlinked %d dangling wikilink(s) and repointed %d "
            "spelling variant(s) across %d note(s)",
            total, moved, len(details),
        )
    return {
        "notes_edited": len(details),
        "links_stripped": total,
        "links_relinked": moved,
        "details": details,
    }
