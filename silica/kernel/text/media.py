# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Media handling for note text — image stripping.

Image embeds are removed from text before it reaches the embedding model,
the distiller payload, or any LLM context.  Obsidian-flavored embeds
(![[file.jpg]]) and standard Markdown images (![alt](src)) are both removed.

Call strip_images(text) anywhere you want images silently removed.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Obsidian-flavored image embed: ![[path/to/file.ext]] or ![[file.ext|300]]
# Matches any file extension that looks like a raster/vector image or media.
_IMAGE_EXTENSIONS = r"(?:jpe?g|png|gif|webp|svg|bmp|tiff?|avif|mp4|mov|avi|mkv|pdf)"

# Plain-tuple form of the same extensions, for endswith() checks when re-attaching.
_IMG_EXT_TUPLE = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp",
    ".tif", ".tiff", ".avif", ".mp4", ".mov", ".avi", ".mkv", ".pdf",
)

_OFM_IMAGE_RE = re.compile(
    rf"!\[\[([^\]]*\.{_IMAGE_EXTENSIONS})(\|[^\]]*)?\]\]",
    re.IGNORECASE,
)

# Standard Markdown image: ![alt text](src) — local or remote
# Also matches empty alt: ![](...) and wikilink-style src paths
_MD_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\([^)]*\)",
    re.IGNORECASE,
)

# Same as _MD_IMAGE_RE but capturing the src, for re-attaching (not stripping).
_MD_IMAGE_SRC_RE = re.compile(
    r"!\[[^\]]*\]\(([^)]+)\)",
    re.IGNORECASE,
)

# A figure caption: the <details> block a PDF converter emits DIRECTLY under the
# image it describes (MinerU + a vision model: "<summary>contour</summary>" and a
# transcribed axis table). It is machine-written description of a picture, not
# something the document says — and it read as source content: one real run made
# a note titled "Y-axis Range" out of a caption table header, body and all.
# Positional, not a list of summary labels: a hand-written <details> that follows
# no image is the author's and stays.
_IMAGE_CAPTION_RE = re.compile(
    rf"(?:!\[\[[^\]]*\.{_IMAGE_EXTENSIONS}(?:\|[^\]]*)?\]\]|!\[[^\]]*\]\([^)]*\))"
    r"[ \t]*\n\s*<details>.*?</details>",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def strip_images(text: str) -> str:
    """Remove all image embeds from *text*, returning clean prose.

    Handles:
      - ``![[images/abc123.jpg]]``           Obsidian embed (no alt)
      - ``![[file.png|300]]``                Obsidian embed with size hint
      - ``![](path/to/image.jpeg)``          Standard Markdown (empty alt)
      - ``![alt text](https://…/img.png)``   Standard Markdown (remote)

    A ``<details>`` block sitting directly under an image goes with it: that is
    a PDF converter's machine-written figure caption, not something the document
    says. A ``<details>`` that follows no image is the author's and survives.

    Wikilinks without ``!`` prefix (``[[Note]]``) are left untouched.
    Plain text, headings, and code blocks are left untouched.

    Empty lines left by removed embeds are collapsed to at most one blank line
    so the surrounding text reads cleanly.
    """
    # 0. Image + its converter-written caption, together — after step 1 the
    #    caption no longer follows anything and can't be told from an author's.
    text = _IMAGE_CAPTION_RE.sub("", text)
    # 1. Remove Obsidian-flavor embeds
    text = _OFM_IMAGE_RE.sub("", text)
    # 2. Remove standard Markdown images
    text = _MD_IMAGE_RE.sub("", text)
    # 3. Collapse runs of blank/whitespace-only lines (≥2) into a single blank line
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    return text


# A caption keeps at most this many of its characters as the figure's words. A
# converter caption can be a whole transcribed table (4,961 characters on one
# test-vault figure, 2026-10-07), and the embedding text is cut at 1,200, so an
# uncapped figure near the top would push the note's own prose out of it.
_FIGURE_CHARS = 300

_CAPTION_PARTS_RE = re.compile(
    r"<details>\s*(?:<summary>(.*?)</summary>)?(.*?)</details>", re.IGNORECASE | re.DOTALL
)
_TAG_OR_FENCE_RE = re.compile(r"<[^>]+>|^\s*```\w*\s*$", re.MULTILINE)


def figures_as_text(text: str, readings: "Mapping[str, str] | None" = None) -> str:
    """``strip_images`` for an index: a figure keeps the words that describe it.

    ``readings`` maps an image's basename to its image reading (ADR-0077 point
    2). An image with a reading becomes ``[figure: words]``; one with a
    converter caption under it and no reading becomes ``[figure: kind: words]``
    (the 9B's reading beat mineru's caption on 334 vault figures, 0.224 against
    0.136 MRR@10); both are capped at ``_FIGURE_CHARS``. An image with neither
    is removed exactly as ``strip_images`` removes it, so a note that only
    embeds unread screenshots keeps a byte-identical text and its embedding
    signature does not move. Never for a distill payload: a caption read as
    source made a note titled "Y-axis Range".
    """
    readings = readings or {}

    def read(raw: str) -> str | None:
        words = readings.get(os.path.basename(raw.split("|", 1)[0].strip()))
        return f"[figure: {words[:_FIGURE_CHARS]}]" if words else None

    def captioned(m: "re.Match[str]") -> str:
        head = re.match(r"!\[\[([^\]|]*)|!\[[^\]]*\]\(([^)\s]*)", m.group(0))
        if head and (r := read(head.group(1) or head.group(2) or "")):
            return r
        parts = _CAPTION_PARTS_RE.search(m.group(0))
        if parts is None:
            return ""
        kind = (parts.group(1) or "").strip()
        words = " ".join(_TAG_OR_FENCE_RE.sub(" ", parts.group(2)).split())[:_FIGURE_CHARS]
        return f"[figure: {kind}: {words}]" if kind else f"[figure: {words}]"

    text = _IMAGE_CAPTION_RE.sub(captioned, text)
    if readings:
        text = _OFM_IMAGE_RE.sub(lambda m: read(m.group(1)) or m.group(0), text)
        text = _MD_IMAGE_SRC_RE.sub(lambda m: read(m.group(1)) or m.group(0), text)
    return strip_images(text)


def _add_embed(out: list[str], raw: str) -> None:
    """Normalize one image src to an Obsidian basename embed and append (deduped).

    Remote URLs are skipped (they'd resolve to a nonexistent local file).
    Normalizing to ``![[basename.ext]]`` matches convert.py and makes the embed
    resolve from any note location in the vault, not just the inbox folder.
    """
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "mailto:")):
        return
    base = os.path.basename(raw.split("|", 1)[0].strip())  # drop OFM |size hint
    if not base.lower().endswith(_IMG_EXT_TUPLE):
        return
    embed = f"![[{base}]]"
    if embed not in out:
        out.append(embed)


def images_for_section(source: str, concept: str) -> list[str]:
    """Image embeds living in *concept*'s section of *source*, normalized.

    The section is the one whose heading contains *concept* — the SAME match
    payload.py uses to build the concept's excerpt, so a note gets exactly the
    images from the section its content was distilled from. No matching heading
    → no images (concepts pulled from windows have no well-defined section).
    Embeds are returned as deduped, in-order ``![[basename.ext]]``.
    """
    from silica.kernel.text.payload import find_heading, extract_section  # lazy: avoid cycle

    h = find_heading(source, concept)
    if not h:
        return []
    section = extract_section(source, h)
    out: list[str] = []
    for m in _OFM_IMAGE_RE.finditer(section):
        _add_embed(out, m.group(1))
    for m in _MD_IMAGE_SRC_RE.finditer(section):
        _add_embed(out, m.group(1))
    return out


def append_section_images(snippet: str, source: str, concept: str) -> str:
    """Append *concept*'s section images to *snippet*. No-op when there are none.

    Embeds the snippet already carries are skipped (matched without the closing
    brackets, so a kept `![[fig.jpg|300]]` counts): an overwrite body is the
    whole rewritten note, and the model usually keeps the figure it was given.
    """
    embeds = [e for e in images_for_section(source, concept) if e[:-2] not in snippet]
    if not embeds:
        return snippet
    block = "\n".join(embeds)
    # The figure belongs to the body: appended to the end it landed after the
    # `## Relations` block on 5% of the outline-lane notes (2026-09-06).
    m = re.search(r"^## (?:Relations|Sources)\b", snippet, re.M)
    if m:
        head, tail = snippet[:m.start()].rstrip(), snippet[m.start():]
        return f"{head}\n\n{block}\n\n{tail}" if head else f"{block}\n\n{tail}"
    base = snippet.rstrip()
    return f"{base}\n\n{block}" if base else block


