# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A file's cover for the Library (ADR-0163): a PDF's first page drawn small,
an EPUB's declared image, and nothing for any other file, whose place the page
fills with a tile of its title. No network and no model: only what the file
holds.

Drawn on the first request and kept in the vault's index namespace, never in
the vault, as Discover keeps its covers (ADR-0137). Named by the file's path
and stamped with its size and mtime, so a file that changed is drawn again and
its old cover goes in the same look.
"""
from __future__ import annotations

import hashlib
import posixpath
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

COVER_EXTS = (".pdf", ".epub")
MEDIA = {"png": "image/png", "jpg": "image/jpeg"}
# An EPUB's image is served as the book carries it: nothing installed here
# decodes a JPEG or a PNG to make it smaller (no Pillow, ADR-0087 amendment
# point 1). shortcut: a cover over 2 MiB is the tile and not the image; decode
# and shrink it in the PDF child when a library's EPUBs are measured past it
# (none was, 2026-11-12).
EPUB_COVER_BYTES = 2 * 1024 * 1024
# By its bytes and never by the manifest's word: an SVG cover is a document
# that runs (render.ASSET_HEADERS), so only these two are pictures here.
_MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"))


def covers_dir() -> Path:
    from silica.kernel.base import home

    return home.index_dir() / "covers"


def cover(full: Path) -> tuple[Path, str] | None:
    """(the kept image, its media type) for the vault file `full`, drawn now if
    it never was; None for a file with no cover.

    shortcut: covers of files deleted from the vault stay (a PDF's measured 25
    KB, an EPUB's up to EPUB_COVER_BYTES); prune with Discover's ceiling
    (200 MB) when the folder passes it."""
    from silica.kernel.base.durable import atomic_write_bytes

    if full.suffix.lower() not in COVER_EXTS:
        return None
    st = full.stat()
    who = hashlib.sha1(str(full.resolve()).encode("utf-8")).hexdigest()[:16]
    stamp = f"{who}-{st.st_size:x}-{st.st_mtime_ns:x}"
    cdir = covers_dir()
    if (found := _kept(cdir, who, stamp)) is not _MISSING:
        return found
    # One draw at a time, asked again under the lock: on the server each draw
    # is a child of up to 1 GiB, and a rail of raws asks for a dozen at once
    # inside the tenant's own 1 GB.
    with _DRAWING:
        if (found := _kept(cdir, who, stamp)) is not _MISSING:
            return found
        drawn = _draw(full)
        cdir.mkdir(parents=True, exist_ok=True)
        # A file with nothing to draw is kept as such, so a library's rail does
        # not open every coverless book again on each look.
        out = cdir / f"{stamp}.{drawn[1] if drawn else 'none'}"
        atomic_write_bytes(out, drawn[0] if drawn else b"")
    return (out, MEDIA[drawn[1]]) if drawn else None


_DRAWING = threading.Lock()
_MISSING: Any = object()


def _kept(cdir: Path, who: str, stamp: str) -> Any:
    """The kept cover of `stamp` (None when it was kept as having none), or
    _MISSING; a cover of the file as it was before is removed on the way."""
    if cdir.is_dir():
        for kept in cdir.glob(f"{who}-*"):
            name, _, ext = kept.name.partition(".")
            if name != stamp:
                kept.unlink(missing_ok=True)
            else:
                return (kept, MEDIA[ext]) if ext in MEDIA else None
    return _MISSING


def _draw(full: Path) -> tuple[bytes, str] | None:
    if full.suffix.lower() == ".pdf":
        from silica.sources import pdf_extract

        drawn = pdf_extract.cover_png(full)
        return (drawn, "png") if drawn else None
    return epub_cover(full)


def epub_cover(full: Path) -> tuple[bytes, str] | None:
    """The image an EPUB's package declares its cover: EPUB 3's item with
    `properties="cover-image"`, else EPUB 2's `<meta name="cover">`, which names
    an item by its id (by its href in some books). Read through the converter's
    own guards: the package with no DTD, a member under its size ceiling."""
    from silica.sources.convert import _epub_opf, _local_name, _zip_member

    try:
        with zipfile.ZipFile(full) as z:
            opf, base = _epub_opf(z)
            items = [el for el in opf.iter() if _local_name(el.tag) == "item" and el.get("href")]
            named = next((el.get("content") for el in opf.iter()
                          if _local_name(el.tag) == "meta" and el.get("name") == "cover"), None)
            # Not `or` between the two: an Element with no children is falsy.
            item = next((i for i in items if "cover-image" in (i.get("properties") or "").split()), None)
            if item is None:
                item = next((i for i in items if named and named in (i.get("id"), i.get("href"))), None)
            if item is None:
                return None
            member = posixpath.normpath(posixpath.join(base, unquote(item.get("href") or "")))
            if z.getinfo(member).file_size > EPUB_COVER_BYTES:
                return None
            data = _zip_member(z, member)
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError, OSError):
        # A package that does not read, a DTD refused, a member it names and
        # lacks: the book has no cover to show, and its tile stands in.
        return None
    return next(((data, ext) for magic, ext in _MAGIC if data.startswith(magic)), None)
