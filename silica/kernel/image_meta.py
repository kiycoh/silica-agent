# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A photo's location, out of what a wiki publishes (ADR-0153 point 9).

Only the GPS block goes. The whole EXIF block would be one line, but it holds
Orientation too, and a phone's portrait photo would then publish on its side.
The block is blanked in place, so no offset in the file moves: the pointer to
it reads 0, which readers take as no GPS. Pure bytes because nothing in the
core install reads images (no Pillow), and a re-encode would change the picture.

Lightroom, Photoshop and darktable write the location into XMP too, or only
there, so each `GPS*` property of an XMP packet is overwritten with spaces of
its length, which leaves the packet well-formed and every offset where it was.
shortcut: a property split across two of a JPEG's extended-XMP segments, and
XMP in a GIF, are not read; split the reassembly in if a published photo ever
keeps one.
"""
from __future__ import annotations

import re
import struct
import zlib

_GPS_POINTER = 0x8825
_PNG = b"\x89PNG\r\n\x1a\n"
_EXIF_HEAD = b"Exif\x00\x00"
_XMP_HEADS = (b"http://ns.adobe.com/xap/1.0/\x00", b"http://ns.adobe.com/xmp/extension/\x00")
_XMP_KEY = b"XML:com.adobe.xmp\x00"
# Any prefix: XML lets a file bind the EXIF namespace to a name of its own, and `exif` is a habit.
_XMP_GPS = re.compile(rb"""[\w.-]+:GPS[A-Za-z]+\s*=\s*(?:"[^"]*"|'[^']*')"""
                      rb"|<([\w.-]+:GPS[A-Za-z]+)\b[^>]*?(?:/>|>.*?</\1\s*>)", re.DOTALL)
# An XMP packet is a few KB; past this a compressed one is taken as holding a location, unread.
_XMP_MAX = 1 << 20
# Bytes per value of each TIFF field type; a field over four bytes lives at an offset.
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _tiff_span(data: bytes) -> tuple[int, int] | None:
    """Where the TIFF block of a JPEG, PNG or WebP sits, None when it has none."""
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] != 0xDA:
            size = int.from_bytes(data[i + 2:i + 4], "big")
            if size < 2:
                return None
            if data[i + 1] == 0xE1 and data[i + 4:i + 10] == _EXIF_HEAD:
                return i + 10, min(i + 2 + size, len(data))
            i += 2 + size
    elif data[:8] == _PNG:
        i = 8
        while i + 12 <= len(data):
            size = int.from_bytes(data[i:i + 4], "big")
            if data[i + 4:i + 8] == b"eXIf":
                return i + 8, min(i + 8 + size, len(data))
            i += 12 + size
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        i = 12
        while i + 8 <= len(data):
            size = int.from_bytes(data[i + 4:i + 8], "little")
            if data[i:i + 4] == b"EXIF":
                start = i + 8 + (len(_EXIF_HEAD) if data[i + 8:i + 14] == _EXIF_HEAD else 0)
                return start, min(i + 8 + size, len(data))
            i += 8 + size + (size & 1)
    return None


def _gps(tiff: bytes) -> tuple[str, int, int] | None:
    """(byte order, offset of the pointer's entry, offset of the GPS block) in a TIFF block."""
    order = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if order is None:
        return None
    try:
        ifd = struct.unpack_from(order + "I", tiff, 4)[0]
        for k in range(struct.unpack_from(order + "H", tiff, ifd)[0]):
            at = ifd + 2 + 12 * k
            if struct.unpack_from(order + "H", tiff, at)[0] == _GPS_POINTER:
                block = struct.unpack_from(order + "I", tiff, at + 8)[0]
                return (order, at, block) if block else None
    except struct.error:
        # A truncated or hand-made block: nothing a reader could take a location from either.
        return None
    return None


def _xmp(data: bytes) -> list[tuple[int, int, int | None, bool]]:
    """Each XMP packet of a JPEG, PNG or WebP: (start, end, the PNG chunk's offset or None,
    whether it is compressed, which only a PNG's can be)."""
    out: list[tuple[int, int, int | None, bool]] = []
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] != 0xDA:
            size = int.from_bytes(data[i + 2:i + 4], "big")
            if size < 2:
                break
            head = next((h for h in _XMP_HEADS if data.startswith(h, i + 4)), None) if data[i + 1] == 0xE1 else None
            if head:
                out.append((i + 4 + len(head), min(i + 2 + size, len(data)), None, False))
            i += 2 + size
    elif data[:8] == _PNG:
        i = 8
        while i + 12 <= len(data):
            size = int.from_bytes(data[i:i + 4], "big")
            end = min(i + 8 + size, len(data))
            if data[i + 4:i + 8] == b"iTXt" and data.startswith(_XMP_KEY, i + 8):
                at = i + 8 + len(_XMP_KEY)
                # past the compression flag and method, then the language and the translated
                # keyword, each ended by a NUL
                lang = data.find(b"\x00", at + 2, end)
                text = data.find(b"\x00", lang + 1, end) + 1 if lang >= 0 else 0
                if text > 0:
                    out.append((text, end, i, data[at:at + 1] == b"\x01"))
            i += 12 + size
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        i = 12
        while i + 8 <= len(data):
            size = int.from_bytes(data[i + 4:i + 8], "little")
            if data[i:i + 4] == b"XMP ":
                out.append((i + 8, min(i + 8 + size, len(data)), None, False))
            i += 8 + size + (size & 1)
    return out


def _xmp_located(data: bytes, start: int, end: int, compressed: bool) -> bool:
    if not compressed:
        return _XMP_GPS.search(data, start, end) is not None
    d = zlib.decompressobj()
    try:
        text = d.decompress(data[start:end], _XMP_MAX)
    except zlib.error:
        # Nothing a reader could inflate, so no location a reader could take from it either.
        return False
    return bool(d.unconsumed_tail) or _XMP_GPS.search(text) is not None


def has_location(data: bytes) -> bool:
    span = _tiff_span(data)
    if span is not None and _gps(data[span[0]:span[1]]) is not None:
        return True
    return any(_xmp_located(data, start, end, packed) for start, end, _, packed in _xmp(data))


def strip_location(data: bytes) -> bytes:
    """`data` with its GPS block and its XMP's GPS properties blanked; the same bytes when it
    holds none."""
    return _strip_xmp(_strip_exif(data))


def _strip_xmp(data: bytes) -> bytes:
    out: bytearray | None = None
    drop: list[int] = []
    for start, end, chunk, packed in _xmp(data):
        if packed:
            # A PNG's compressed packet cannot be blanked at its length; PNG has no offsets
            # between chunks, so the chunk goes whole.
            if chunk is not None and _xmp_located(data, start, end, True):
                drop.append(chunk)
            continue
        hits = list(_XMP_GPS.finditer(data, start, end))
        if not hits:
            continue
        out = out if out is not None else bytearray(data)
        for m in hits:
            out[m.start():m.end()] = b" " * (m.end() - m.start())
        if chunk is not None and end + 4 <= len(out):
            out[end:end + 4] = zlib.crc32(bytes(out[chunk + 4:end])).to_bytes(4, "big")
    result = bytes(out) if out is not None else data
    for chunk in reversed(drop):
        size = int.from_bytes(result[chunk:chunk + 4], "big")
        result = result[:chunk] + result[chunk + 12 + size:]
    return result


def _strip_exif(data: bytes) -> bytes:
    span = _tiff_span(data)
    found = _gps(data[span[0]:span[1]]) if span else None
    if span is None or found is None:
        return data
    start, end = span
    order, pointer, block = found
    out = bytearray(data)
    tiff = data[start:end]
    try:
        count = struct.unpack_from(order + "H", tiff, block)[0]
        for k in range(count):
            at = block + 2 + 12 * k
            kind, n, value = struct.unpack_from(order + "HII", tiff, at + 2)
            size = _TYPE_SIZE.get(kind, 1) * n
            if size > 4 and value + size <= len(tiff):
                out[start + value:start + value + size] = bytes(size)
        stop = min(block + 2 + 12 * count + 4, len(tiff))
        out[start + block:start + stop] = bytes(stop - block)
    except struct.error:
        # The pointer goes below whatever was unreadable, so the block is out of every reader's way.
        pass
    out[start + pointer + 8:start + pointer + 12] = bytes(4)
    if data[:8] == _PNG:
        out[end:end + 4] = zlib.crc32(bytes(out[start - 4:end])).to_bytes(4, "big")
    return bytes(out)
