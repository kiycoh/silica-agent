# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note's checkable blocks (ADR-0110 point 2): a paragraph, a list item, a
table row, a footnote's definition, a blockquote.

The block and not the sentence, because legal and academic prose breaks a
sentence splitter on "art.", "n.", "Cass.", "c.c.", "et al.". Headings,
frontmatter, code and math are never checked, and neither is a block of fewer
than six words: it states nothing a source could hold.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_WORDS = 6

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?")
_FOOTDEF = re.compile(r"^\[\^([^\]\s]+)\]:\s?")
_FOOTREF = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_INLINE_MATH = re.compile(r"(?<![\\$])\$(?=\S)[^$\n]*?\S\$(?!\d)")
_WORD = re.compile(r"[^\W_]+")


@dataclass
class Block:
    line: int                 # 1-based line of the file where the block starts
    kind: str                 # paragraph | item | row | footnote | quote
    text: str                 # as written, list and quote markers stripped
    footnotes: list[str] = field(default_factory=list)  # labels it references

    @property
    def clean(self) -> str:
        """The text with inline code and inline math taken out."""
        return _INLINE_MATH.sub(" ", _INLINE_CODE.sub(" ", self.text))

    @property
    def words(self) -> int:
        return len(_WORD.findall(re.sub(r"\[\[|\]\]|[*_~=|>]", " ", self.clean)))


def body_start(content: str) -> int:
    """Index of the first body line: past a frontmatter block when there is one."""
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                return i + 1
    return 0


def split(content: str) -> tuple[list[Block], dict[str, str]]:
    """(the checkable blocks, footnote label -> its definition's text)."""
    lines = (content or "").split("\n")
    blocks: list[Block] = []
    cur: Block | None = None
    fence = math = False

    def close():
        nonlocal cur
        if cur is not None:
            cur.text = cur.text.strip()
            blocks.append(cur)
        cur = None

    for i in range(body_start(content or ""), len(lines)):
        raw = lines[i]
        s = raw.strip()
        if fence:
            fence = not _FENCE.match(raw)
            continue
        if _FENCE.match(raw):
            close()
            fence = True
            continue
        if math:
            math = s != "$$" and not s.endswith("$$")
            continue
        if s.startswith("$$"):
            close()
            math = not (len(s) > 2 and s.endswith("$$"))
            continue
        if not s or _HEADING.match(raw):
            close()
            continue
        if s.startswith("|"):
            close()
            if not _TABLE_SEP.match(s):
                cells = [c.strip() for c in s.strip("|").split("|")]
                blocks.append(Block(i + 1, "row", " | ".join(c for c in cells if c)))
            continue
        if (m := _FOOTDEF.match(s)):
            close()
            cur = Block(i + 1, "footnote", s[m.end():])
            cur.footnotes = [m.group(1)]  # the definition's own label, popped below
            continue
        if s.startswith(">"):
            body = re.sub(r"^(>\s?)+", "", s)
            if cur is None or cur.kind != "quote":
                close()
                cur = Block(i + 1, "quote", "")
            cur.text += " " + body
            continue
        if (m := _ITEM.match(raw)):
            close()
            cur = Block(i + 1, "item", raw[m.end():])
            continue
        if cur is not None and cur.kind in ("paragraph", "item", "footnote"):
            cur.text += " " + s
            continue
        close()
        cur = Block(i + 1, "paragraph", s)
    close()

    defs: dict[str, str] = {}
    out: list[Block] = []
    for b in blocks:
        if b.kind == "footnote":
            label = b.footnotes.pop()
            defs[label] = b.text
        b.footnotes = list(dict.fromkeys(_FOOTREF.findall(b.text)))
        if b.words >= MIN_WORDS:
            out.append(b)
    return out, defs
