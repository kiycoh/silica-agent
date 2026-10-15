# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's + (ADR-0098): what a path the page sends becomes on disk, and
what a file must look like before a lane reads it.

Everything here takes the page's word as hostile (ADR-0098 H7, H8): the page
filters a folder by type, and the server filters again, because a page is not
a trust boundary.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

from silica.sources.convert import AUDIO_EXTS

# ADR-0087 point 5, the owner's answer of 2026-10-12: the hosted pilot reads
# PDF, markdown and plain text, and nothing else is installed to read more.
# Audio since the amendment of 2026-10-14: ffmpeg is on the host for the
# transcriber, and its demuxers are held to convert._FFMPEG_FORMATS. Not
# video, which the notice does not name and which is mostly bytes the
# transcriber never hears under the edge's 100 MB.
HOSTED_EXTENSIONS = (".md", ".pdf", ".txt", *AUDIO_EXTS)
# 255 bytes is the name limit on ext4 and XFS; 200 leaves room for " (99)".
MAX_NAME_BYTES = 200
MAX_DEPTH = 16
# Near ADR-0087's cap of 8 million characters out of one PDF: a larger note
# is not one a reader or the index can use.
TEXT_MAX_BYTES = 8 * 1024 * 1024
# The vault's + caps a folder at the free tier's note cap (ADR-0086 point 4):
# the page stops there, so a folder of bombs costs one tenant one CPU (H2).
MAX_FILES_PER_IMPORT = 500

# C0 and C1 controls, DEL, and the bidirectional embeddings, overrides and
# isolates: U+202E shows `invoice<U+202E>fdp.exe` as `invoiceexe.pdf` (H7).
_STRIP = dict.fromkeys(
    [*range(0x00, 0x20), *range(0x7F, 0xA0), *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)
_DRIVE = re.compile(r"^[A-Za-z]:$")


class Refused(ValueError):
    """One file the import will not take, with the sentence the student sees."""


def importable_extensions(hosted: bool) -> list[str]:
    """What the vault's + accepts: the hosted list, or every nucleate lane
    minus images, which go with a chat message and are not filed (ADR-0077
    point 3)."""
    if hosted:
        return list(HOSTED_EXTENSIONS)
    from silica.sources.convert import IMG_EXTS
    from silica.sources.registry import supported_nucleate_extensions

    return [e for e in supported_nucleate_extensions() if e not in IMG_EXTS]


def clean_rel(raw: str) -> PurePosixPath:
    """The page's relative path as a path that cannot leave the folder it is
    joined to. Raises Refused when nothing safe is left of it."""
    if "\x00" in raw:
        raise Refused("its name holds a NUL byte")
    if "\\" in raw:
        raise Refused("its path holds a backslash")
    text = unicodedata.normalize("NFC", raw).translate(_STRIP)
    parts = [p.strip() for p in text.split("/")]
    parts = [p for p in parts if p not in ("", ".", "..")]
    if not parts:
        raise Refused("it has no name")
    if _DRIVE.match(parts[0]):
        raise Refused("its path names a drive")
    if len(parts) - 1 > MAX_DEPTH:
        raise Refused(f"it sits deeper than {MAX_DEPTH} folders")
    return PurePosixPath(*(_fit(p) for p in parts))


def _fit(name: str) -> str:
    """`name` cut to MAX_NAME_BYTES of UTF-8, keeping its extension."""
    if len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    suffix = PurePosixPath(name).suffix
    stem = name[: len(name) - len(suffix)] if suffix else name
    budget = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    cut = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip()
    return cut + suffix


def skipped(rel: PurePosixPath, ignore) -> bool:
    """A file under a hidden folder (.git, .obsidian, .silica), a hidden file,
    or a folder `.silicaignore` names: the folders the index walk skips too."""
    if any(part.startswith(".") for part in rel.parts):
        return True
    return any(ignore(part) for part in rel.parts[:-1])


def numbered(dest: Path) -> Path:
    """`dest`, or the first `name (n).ext` that does not exist yet: a second
    upload with a name already there never overwrites it (ADR-0086 point 4)."""
    if not dest.exists() and not dest.is_symlink():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        candidate = dest.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


def check_text(data: bytes) -> str:
    """A markdown or text upload's content, or Refused: UTF-8 with no NUL
    byte is text; anything else is a binary file with a text extension."""
    if b"\x00" in data:
        raise Refused("it is not text (it holds a NUL byte)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("it is not UTF-8 text") from None
