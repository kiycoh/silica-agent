# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A raw and the sources it became (ADR-0129 point 1).

A raw is a non-markdown file brought in to become notes (a PDF, a document, a
deck); a source is a note it became, which names it in `source_file`. The
link goes from the note to the file and is not a wikilink, so nothing in the
graph knows it: the tree, the drawer and `silica_files` ask here.

Its own walk and not Learn's `_notes_meta`, which parses every note's YAML
again whenever anything in the vault changes: the tree asks after every turn,
and a turn that writes one note would cost a parse of all of them. Here a
note is read again only when its own mtime or size moved, and parsed only
when its head names a `source_file`.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

# What the drawer and a re-import read of a note's raw; `document` marks a
# split document's parent note (ADR-0129 point 5).
_KEYS = ("source_file", "source_sha256", "source_bytes", "source_pages",
         "source_page", "source_page_end", "document")
# A markdown file is a source, never a raw: /nucleate's draft filing names the
# inbox note it filed this way (`source_file: notes.md`).
_NOT_RAW = (".md", ".txt")
# convert() writes `source_file` among the first lines of the frontmatter; a
# note that names it past this point is not read as naming a raw.
_HEAD = 4096

_memo: dict[str, tuple[int, int, dict]] = {}  # file -> (mtime_ns, size, fields)


def fields(path: Path) -> dict:
    """The raw keys of one note's frontmatter, {} when it names no raw."""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    hit = _memo.get(key)
    if hit is not None and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    out: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(_HEAD)
        if head.startswith("---") and "source_file:" in head:
            from silica.kernel.write import frontmatter

            data, _, _ = frontmatter.split(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = {k: data[k] for k in _KEYS if data.get(k) not in (None, "")}
    except (OSError, UnicodeDecodeError):
        out = {}  # a note that cannot be read names nothing; the next stat retries it
    _memo[key] = (st.st_mtime_ns, st.st_size, out)
    return out


def resolve(value: object, note: str, vault: Path) -> Path | None:
    """The file a note's `source_file` names inside the vault, None when it
    names none there: a markdown file, a path outside the vault or under a
    dot-folder, or a file that is gone.

    An absolute path is the laptop's; a path with a folder is the server's
    since ADR-0129. A bare name is an older server note's, written while the
    server deleted the file: it is looked for where an import put it, beside
    the note or beside the folder of the parts, so a re-import finds it."""
    v = str(value or "").strip().replace("\\", "/")
    if not v or PurePosixPath(v).suffix.lower() in _NOT_RAW:
        return None
    root = vault.resolve()
    if os.path.isabs(v):
        tries = [Path(v)]
    elif "/" in v:
        tries = [root / v]
    else:
        here = PurePosixPath(note).parent
        tries = [root / here / v, root / here.parent / v, root / v]
    for t in tries:
        try:
            r = t.resolve()
        except OSError:
            continue
        if (r.is_relative_to(root) and r.is_file()
                and not any(p.startswith(".") for p in r.relative_to(root).parts)):
            return r
    return None


def kept(found: Path | None, f: dict) -> bool:
    """Whether `found` is still the raw: a file the note's own size names
    otherwise is another one at that path. The size and not the sha256,
    because this runs on every open of the note and a hash reads the file."""
    if found is None:
        return False
    size = f.get("source_bytes")
    return not isinstance(size, int) or found.stat().st_size == size


def named(vault: Path) -> Iterator[tuple[str, dict]]:
    """(note, its raw keys) for every note outside the dot-folders that names
    a `source_file`."""
    root = vault.resolve()
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for name in files:
            if name.endswith(".md"):
                p = Path(d) / name
                f = fields(p)
                if f.get("source_file"):
                    yield p.relative_to(root).as_posix(), f


def index(vault: Path) -> dict[str, list[str]]:
    """{a raw's vault path: the notes naming it} for every raw in the vault,
    the document's parent note first, then the parts in their order, and
    the archive's copies last: the rail opens the first, and a part filed in
    done/ by /nucleate is the older copy of one still in the inbox."""
    from silica.kernel.vault_manifest import active_done_dir

    root = vault.resolve()
    done = (active_done_dir() or "").strip("/") + "/"
    out: dict[str, list[tuple[bool, bool, str]]] = {}
    for note, f in named(root):
        found = resolve(f["source_file"], note, root)
        if found is not None and kept(found, f):  # kept() says the same; mypy needs it said here
            out.setdefault(found.relative_to(root).as_posix(), []).append(
                (not f.get("document"), done != "/" and note.startswith(done), note))
    return {raw: [n for *_, n in sorted(rows)] for raw, rows in out.items()}
