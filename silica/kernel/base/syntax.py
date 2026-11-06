# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Markdown's grammar as this codebase reads it, one regex per shape.

The parser in `markdown.py` is the one link GENERATOR (ADR-0029); these are for
everyone who scans text without it. Stdlib only, because `frontmatter` needs
them and importing markdown-it from there cost 20 ms on a process that opens no
note (2026-11-06).

Frontmatter's grammars are in `frontmatter.py`, next to the one who decides
which notes parse.
"""
from __future__ import annotations

import re

# --- wikilinks --------------------------------------------------------------
#
# A wikilink is typed in five shapes on purpose, by what its reader needs back.
# `[[#Heading]]` and `[[^block]]` point inside the note carrying them, so a
# reader of targets refuses them: none has a target before its `#`.

_TARGET = r"[^\]|#\n]+"
_ANCHOR = r"#[^\]|\n]*"
_ALIAS = r"[^\]\n]*"
_PARTS = rf"\[\[({_TARGET})({_ANCHOR})?(?:\|({_ALIAS}))?\]\]"

# One line, as a renderer sees a link: groups are embed `!`, target, `#anchor`,
# alias. The grammar of a reader that rewrites or resolves a link.
WIKILINK_RE = re.compile(rf"(!?){_PARTS}")
# The same without embeds: target, `#anchor`, alias. An embed is a picture, and
# marking or mentioning one as a note would be wrong in the sentence it sits in.
LINK_RE = re.compile(rf"(?<!!){_PARTS}")
# A link typed in a message, where a bracket inside is code and not a name:
# `[[word_map['k']]]` is a Python subscript in 5 of 6,801 real notes. Target only.
MENTION_RE = re.compile(r"(?<!!)\[\[([^\]\[|#\n]+)(?:#[^\]\[|\n]*)?(?:\|[^\]\[\n]*)?\]\]")
# The target alone, as the one group, for `findall` and for building a line
# shape around a link (`WIKILINK_NAME` is its source, to compose with).
WIKILINK_NAME = rf"\[\[({_TARGET})(?:{_ANCHOR})?(?:\|{_ALIAS})?\]\]"
WIKILINK_NAME_RE = re.compile(WIKILINK_NAME)

# Wikilink target extraction: captures the target of [[Target]], [[Target|alias]]
# and [[Target#anchor]] (everything before the first | or #). The shared regex
# for quick target scans, closed or not; extract_links is the full AST-aware
# version.
WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#]+)")
LINK_TARGET_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)")

# Where a link stands in a line, whatever it holds: what a mask covers.
WIKILINK_TOKEN = r"\[\[[^\]]+\]\]"
WIKILINK_TOKEN_RE = re.compile(WIKILINK_TOKEN)

# Every span a rename has to rewrite, empty parts included, across a line break
# because a rename must not leave a link behind. Groups: embed `!`, target,
# `#anchor` or `^block`, `|alias`, each kept as written so a rewrite changes the
# target and nothing else.
WIKILINK_SPAN_RE = re.compile(
    r"(!?)"                         # optional embed
    r"\[\["
    r"([^\]#^|]*)"                  # target (no suffix, no alias delimiters)
    r"([#^][^\]|]*)?"               # optional heading/block suffix
    r"(\|[^\]]*)?"                  # optional alias
    r"\]\]",
    re.DOTALL,
)

# A link as the reader sees it, for the preservation rule (ADR-0093): an empty
# target stays a link, and `\|` is how a link hides its pipe from a table cell.
# Groups as WIKILINK_RE.
WIKILINK_CELL_RE = re.compile(r"(!?)\[\[([^\]|#\n]*?)(#[^\]|\n]*)?(?:\\?\|([^\]\n]*))?\]\]")

# Text a link flattens to. FLAT keeps the target, `#anchor` included: a note's
# line compared with its source, which never wrote the alias. SHOWN keeps what a
# reader sees, the alias when there is one. INNER keeps every word between the
# brackets, alias and all, for an index that wants them all.
WIKILINK_FLAT_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
WIKILINK_SHOWN_RE = re.compile(r"\[\[([^\]|]+\|)?([^\]]+)\]\]")
WIKILINK_INNER_RE = re.compile(r"!?\[\[([^\[\]]*?)\]\]")

# A decision record cited by number in prose. The 36 ADRs under docs/adr cite
# each other this way and had `out_links: []`: the graph was blind to the one
# reference they all write. Emitted as the literal `ADR-NNNN` token; the
# backend resolver maps it to the note filed as `NNNN-*` (fs_backend).
ADR_REF_RE = re.compile(r"\bADR-(\d{4})\b")

# --- embeds -----------------------------------------------------------------

EMBED_TOKEN = r"!\[\[[^\]]+\]\]"
# Everything between the brackets, size hint included.
EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
# An Obsidian embed or a Markdown image: group 1 the embed's target, group 2 the
# image's address. A title after the address is left out of it.
IMAGE_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]|!\[[^\]]*\]\(([^)\s]+)[^)]*\)")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# --- code -------------------------------------------------------------------

INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# A line that opens or closes a fence, for a reader going line by line.
FENCE_LINE_RE = re.compile(r"^\s*(```|~~~)")

# --- headings ---------------------------------------------------------------

# Groups: the `#` run, the title. Multiline, so it scans a body and matches a line.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def section_end(text: str, level: int, start: int) -> int:
    """Where a section of heading `level` that runs on from `start` stops: the
    next heading of the same or a higher level, or the end of `text`."""
    m = re.compile(rf"^#{{1,{level}}}\s", re.MULTILINE).search(text, start)
    return m.start() if m else len(text)


def section_chain(body: str, offset: int, depth: int = 3) -> str:
    """Markdown heading chain still open at `offset` ("Training > Gradients").
    Deepest `depth` levels only — the nearest heading carries the most anchor
    per token, and a full chain on a deeply nested note is header bloat.
    '' above the first heading, so headingless notes cost zero tokens."""
    chain: list[tuple[int, str]] = []
    for m in HEADING_RE.finditer(body[:offset]):
        lvl = len(m.group(1))
        while chain and chain[-1][0] >= lvl:
            chain.pop()
        chain.append((lvl, m.group(2)))
    return " > ".join(t for _lvl, t in chain[-depth:])


# --- math -------------------------------------------------------------------

DISPLAY_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
# Single line. Loose on purpose: a mask would rather skip two prices in a line
# than link inside a formula. INLINE_MATH_STRICT_RE is for a reader that keeps
# what it does not recognise, so `$5 and $6` stays text.
INLINE_MATH_RE = re.compile(r"\$[^$\n]+\$")
INLINE_MATH_STRICT_RE = re.compile(r"(?<![\\$])\$(?=\S)[^$\n]*?\S\$(?!\d)")

# --- names ------------------------------------------------------------------

# What a filesystem refuses, and what a filename can hold and a wikilink cannot.
# Named apart, because a note name needs both and a plain file only the first.
FILE_UNSAFE = r'\\/:*?"<>|'
LINK_UNSAFE = r"#^\[\]"
FILE_UNSAFE_RE = re.compile(rf"[{FILE_UNSAFE}]")
NAME_UNSAFE_RE = re.compile(rf"[{FILE_UNSAFE}{LINK_UNSAFE}]+")
