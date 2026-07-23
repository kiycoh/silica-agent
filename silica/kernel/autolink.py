# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Deterministic wikilink injector for touched notes (Phase 4).

Rule (from the plan):
  "embeddings PROPOSE, graph DISPOSES"
  - `candidates`  — optional list of titles prioritized by embedding similarity.
  - `title_index` — authoritative list of titles that exist in the vault graph.
  A link is emitted ONLY when the title exists in `title_index`.  If `candidates`
  is given, only titles in candidates∩title_index are considered, which keeps
  the autolink pass focused and fast.

Skip regions (never modified):
  - YAML frontmatter  (--- block at the very top of the note, LF or CRLF)
  - Fenced code       (``` or ~~~ blocks, incl. unclosed-to-EOF) and indented code
  - Inline code       (`...`)
  - LaTeX math        ($...$  and  $$...$$)
  - Bare URLs, markdown links/images, inline #tags, HTML tags/comments
  - Existing wikilinks ([[...]]) and heading lines

Disambiguation rule:
  If `title_index` contains two entries that differ only in path but share the
  same display name, the caller must deduplicate them before passing — this
  function works on display names only and will happily link an ambiguous title.
  Use `build_title_index` (below) to get a pre-disambiguated index from the
  driver.

Idempotency: calling autolink twice on the same body is a no-op — any already-
linked title is in a skip region on the second pass.
"""
from __future__ import annotations

import re
from typing import Sequence

# ---------------------------------------------------------------------------
# Skip-region detection
# ---------------------------------------------------------------------------

# Matches the YAML frontmatter at the very top of a note (OFM convention).
# \r?\n so Windows (CRLF) user notes touched by backlink_pass are protected too
# — otherwise the whole frontmatter fails to match and becomes linkable.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n?", re.DOTALL)

# Inline code (`...`)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Display math ($$...$$) — must come before single-$ match
_DISPLAY_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)

# Inline math ($...$) — single-line only
_INLINE_MATH_RE = re.compile(r"\$[^$\n]+\$")

# Bare URLs — never link a word inside https://example.com/Neural-Networks
_URL_RE = re.compile(r"https?://[^\s<>()\[\]]+")

# Inline Obsidian tags (#tag, #nested/tag) — preceded by start/whitespace so
# C# and heading markers ("# Title") don't match. Linking would kill the tag.
_INLINE_TAG_RE = re.compile(r"(?<!\S)#[A-Za-z_][\w/-]*")

# Existing wikilinks [[...]]
_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")

# Markdown links and images: [text](href) / ![alt](href). Protects both the
# link/alt text and the href. Autolink-only — rename REWRITES these hrefs, so
# this never goes in the BASE set.
_MD_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")

# HTML tags with their attributes (<img alt="Neural Networks" ...>)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")

# HTML comments
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Heading lines (# ... at line start)
_HEADING_RE = re.compile(r"^#{1,6} .+$", re.MULTILINE)


def _block_skip_spans(text: str) -> list[tuple[int, int]]:
    """Char spans for line-based code regions: fenced code (``` / ~~~, including
    an unclosed fence that runs to EOF) and indented (4-space/tab) code blocks.

    Line-scanned, not regex: sequential-pairing regexes can't survive an
    unbalanced fence marker (audit finding 6), and an indented code block is
    defined by a preceding blank line (CommonMark) — neither is a clean regex.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    fence_open_at: int | None = None
    prev_blank = True          # start of doc can begin an indented code block
    in_indented = False
    for line in text.splitlines(keepends=True):
        end = pos + len(line)
        is_fence = line.lstrip(" \t").startswith(("```", "~~~"))
        is_blank = line.strip() == ""

        if fence_open_at is not None:          # inside a fence
            if is_fence:                       # closing delimiter
                spans.append((fence_open_at, end))
                fence_open_at = None
            prev_blank, in_indented, pos = False, False, end
            continue
        if is_fence:                           # opening delimiter
            fence_open_at = pos
            prev_blank, in_indented, pos = False, False, end
            continue

        indented = (line.startswith(("    ", "\t")) and not is_blank)
        if indented and (prev_blank or in_indented):
            spans.append((pos, end))
            in_indented = True
        elif not is_blank:                     # blank lines may sit inside a block
            in_indented = False
        prev_blank, pos = is_blank, end

    if fence_open_at is not None:              # unclosed fence → mask to EOF
        spans.append((fence_open_at, len(text)))
    return spans


# Shared skip-region idiom (kernel/rename.py reuses it via build_skip_mask).
# BASE = regions both callers protect; FULL adds regions only autolink skips.
# rename REWRITES wikilinks, headings and markdown-link hrefs, so those live in
# FULL, never BASE. Fenced/indented code is always masked (see build_skip_mask).
SKIP_PATTERNS_BASE = (
    _FRONTMATTER_RE,
    _INLINE_CODE_RE,
    _DISPLAY_MATH_RE,
    _INLINE_MATH_RE,
    _URL_RE,
    _INLINE_TAG_RE,
)
SKIP_PATTERNS_FULL = SKIP_PATTERNS_BASE + (
    _WIKILINK_RE,
    _MD_LINK_RE,
    _HTML_TAG_RE,
    _HTML_COMMENT_RE,
    _HEADING_RE,
)


def build_skip_mask(text: str, patterns=SKIP_PATTERNS_FULL) -> list[bool]:
    """Return a per-character boolean mask: True = inside a skip region.

    Applies `patterns` plus the always-on line-based code regions
    (`_block_skip_spans`): fenced and indented code protect both callers.
    """
    mask = [False] * len(text)
    for pattern in patterns:
        for m in pattern.finditer(text):
            for i in range(m.start(), m.end()):
                mask[i] = True
    for start, end in _block_skip_spans(text):
        for i in range(start, end):
            mask[i] = True
    return mask


def _build_skip_mask(text: str) -> list[bool]:
    return build_skip_mask(text, SKIP_PATTERNS_FULL)


# ---------------------------------------------------------------------------
# Main autolink function
# ---------------------------------------------------------------------------

def autolink(
    body: str,
    title_index: Sequence[str],
    candidates: Sequence[str] | None = None,
    self_title: str | None = None,
) -> tuple[str, list[str]]:
    """Wrap the first occurrence of each vault title in `body` with a wikilink.

    Args:
        body:        The full note text (including frontmatter if present).
        title_index: All vault titles that may be linked (pre-disambiguated).
        candidates:  Optional prioritized subset from embeddings.  If given,
                     only titles in candidates∩title_index are processed.
        self_title:  The title/basename of the note being processed.  When
                     provided, this title is excluded from linking — a note must
                     never contain a wikilink to itself.

    Returns:
        (new_body, added_links) — modified body and list of linked titles.

    Guarantees:
        - Never modifies text inside skip regions.
        - Only links titles that already exist in `title_index` (graph-safe).
        - At most one wikilink per title per call (first occurrence only).
        - Never creates a self-referential wikilink (self_title excluded).
        - Idempotent: running twice produces the same result.
    """
    if not body or not title_index:
        return body, []

    # Determine which titles to consider
    if candidates is not None:
        title_set = {t.lower(): t for t in title_index}
        work_titles = [t for t in candidates if t.lower() in title_set]
        # Canonicalise to the title_index spelling
        work_titles = [title_set[t.lower()] for t in work_titles]
    else:
        work_titles = list(title_index)

    # Exclude the note's own title to prevent self-referential wikilinks
    if self_title:
        _self_lower = self_title.lower()
        work_titles = [t for t in work_titles if t.lower() != _self_lower]

    if not work_titles:
        return body, []

    # Sort longest-first: prevents short titles from shadowing longer ones
    # ("Deep Learning" before "Learning")
    work_titles = sorted(work_titles, key=len, reverse=True)

    # Pre-scan: collect titles that are already wikilinked in the body.
    # A note should have at most one [[title]] link — if it's already there,
    # skip that title entirely regardless of where in the body it appears.
    # Path-qualified targets ([[topics/Python]]) also register their basename
    # ("python") so we don't add a second, redundant [[Python]] (audit §3).
    from silica.kernel.ast import WIKILINK_TARGET_RE
    existing_links: set[str] = set()
    for _m in WIKILINK_TARGET_RE.findall(body):
        t = _m.strip().lower()
        if t:
            existing_links.add(t)
            existing_links.add(t.rsplit("/", 1)[-1])

    added: list[str] = []
    current = body

    # Skip mask built once; rebuilt only after an actual substitution shifts
    # positions. When most titles don't match (full-index fallback), this is the
    # difference between one mask build and one-per-title (audit §4).
    mask = _build_skip_mask(current)

    for title in work_titles:
        if len(title) < 2:
            continue  # single-character titles are too noisy

        if title.lower() in existing_links:
            continue  # already linked elsewhere in the note — skip

        # Build case-insensitive whole-word pattern
        escaped = re.escape(title)
        pattern = re.compile(
            r"(?<!\[)(?<!\w)" + escaped + r"(?!\w)(?!\])",
            re.IGNORECASE,
        )

        # Find the first match that is NOT inside a skip region
        match = None
        for m in pattern.finditer(current):
            if not any(mask[i] for i in range(m.start(), m.end())):
                match = m
                break

        if match is None:
            continue

        # Preserve the body's casing as an alias when it differs from the
        # canonical title ([[Neural Networks|neural networks]]) — otherwise the
        # canonical form rewrites mid-sentence prose (audit §3).
        matched_text = current[match.start() : match.end()]
        link = f"[[{title}]]" if matched_text == title else f"[[{title}|{matched_text}]]"
        current = current[: match.start()] + link + current[match.end() :]
        added.append(title)
        existing_links.add(title.lower())  # prevent duplicates within this call
        mask = _build_skip_mask(current)   # positions shifted — rebuild

    return current, added


# ---------------------------------------------------------------------------
# Reverse-link pass — inject links to newly created notes into pre-existing ones
# ---------------------------------------------------------------------------

def backlink_pass(
    new_titles: list[str],
    *,
    title_index: list[str],
    neighbourhood: list[str],
) -> dict[str, list[str]]:
    """For each note in `neighbourhood`, autolink only the `new_titles`.

    Runs `autolink(body, title_index, candidates=new_titles)` on every neighbour,
    wrapping mentions of newly-created notes with wikilinks in pre-existing content.
    Returns {path: titles_added}. Inherits all autolink() guarantees (graph-safe,
    skip-region aware, idempotent). Best-effort: per-note failures are logged and
    skipped.
    """
    import os as _os
    from silica.driver import DRIVER

    result: dict[str, list[str]] = {}
    for path in neighbourhood:
        try:
            nc = DRIVER.read_note(path)
            body = nc.content or ""
            if not body.strip():
                continue
            stem = _os.path.splitext(_os.path.basename(path))[0]
            new_body, added = autolink(body, title_index, candidates=new_titles, self_title=stem)
            if added:
                DRIVER.overwrite(path, new_body)
                result[path] = added
                import logging as _l
                _l.getLogger(__name__).info("BACKLINK: %s ← %s", path, added)
        except Exception as _e:
            import logging as _l
            _l.getLogger(__name__).debug("BACKLINK: skipped '%s' (non-fatal): %s", path, _e)
    return result


# ---------------------------------------------------------------------------
# Title index helpers
# ---------------------------------------------------------------------------

def build_title_index(refs: list) -> list[str]:
    """Build a disambiguated title list from driver NoteRef objects.

    Drops any title that appears more than once (basename conflict) — such
    titles cannot be safely linked without an explicit path qualifier. The count
    is case-insensitive to match autolink's IGNORECASE matching: `Foo` and `foo`
    are ambiguous together and both dropped (audit §3).

    Args:
        refs: list of NoteRef objects with `.name` attribute.

    Returns:
        Sorted list of unique, unambiguous display names.
    """
    from collections import Counter

    lower_counts: Counter[str] = Counter()
    first_casing: dict[str, str] = {}
    for ref in refs:
        name = ref if isinstance(ref, str) else (getattr(ref, "name", None) or "")
        if name:
            lower_counts[name.lower()] += 1
            first_casing.setdefault(name.lower(), name)

    return sorted(
        first_casing[lc] for lc, count in lower_counts.items() if count == 1
    )
