# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's two downloads (ADR-0142): the vault whole, and what Silica
added, written for the root of the person's own Obsidian vault.

Both stream. zipfile writes into a sink drained after every write, so neither
archive is ever whole on disk, where the tenant's quota would count it against
the vault it copies, nor in memory (point 6).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import IO, Iterable, Iterator, NamedTuple

STATE = "exports.json"
# One additions download at a time (point 3): two at once would both carry and
# record the same files. Released by the stream, however it ends.
BUSY = threading.Lock()
# The import's mark and a download's sent list write the same file.
_STATE_LOCK = threading.Lock()
_CHUNK = 1 << 16
# A zip entry cannot be dated before 1980; a day of margin for the timezone.
_ZIP_EPOCH = 315_619_200


def _state_path(vault: Path) -> Path:
    from silica.kernel.recall.paths import index_dir_for

    return index_dir_for(str(vault)) / STATE


def load_state(vault: Path) -> dict:
    """The Obsidian roots, the sent list and the last download's time."""
    try:
        data = json.loads(_state_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Tolerated: a lost list costs duplicates in one new folder and never
        # an overwrite (point 3), so an unreadable file reads as no download yet.
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"roots": list(data.get("roots") or []), "sent": dict(data.get("sent") or {}),
            "last": data.get("last")}


def save_state(vault: Path, state: dict) -> None:
    from silica.kernel.recall.paths import atomic_write_bytes

    path = _state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(state, ensure_ascii=False).encode("utf-8"))


def mark_root(vault: Path, folder: str) -> None:
    """`Inbox/<folder>` was an Obsidian vault's own folder (point 5)."""
    with _STATE_LOCK:
        state = load_state(vault)
        if folder not in state["roots"]:
            state["roots"].append(folder)
            save_state(vault, state)


def vault_files(vault: Path) -> Iterator[tuple[str, Path]]:
    """Every regular file under the vault, vault-relative, in a stable order.
    A symlink is never followed nor sent: it could name any file the tenant's
    process can read."""
    for root, dirs, files in os.walk(vault):
        dirs.sort()
        for name in sorted(files):
            p = Path(root) / name
            if not p.is_symlink() and p.is_file():
                yield p.relative_to(vault).as_posix(), p


class _Sink:
    """What zipfile writes into. It has no tell(), so zipfile writes data
    descriptors and never seeks back, and it is drained after every write."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, b) -> int:
        self.buf += b
        return len(b)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def stream_zip(entries: Iterable[tuple[str, Path | bytes, float | None]]) -> Iterator[bytes]:
    """A zip of (name in the archive, file or bytes, mtime or None), in chunks."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, src, mtime in entries:
            fh: IO[bytes]
            if isinstance(src, bytes):
                fh, size = io.BytesIO(src), len(src)
            else:
                try:
                    fh = open(src, "rb")
                except FileNotFoundError:
                    # Deleted by a turn since the walk: the archive is the vault
                    # as it stands, and one file gone must not cut it short.
                    continue
                st = os.fstat(fh.fileno())
                size, mtime = st.st_size, st.st_mtime
            info = zipfile.ZipInfo(name, time.localtime(max(mtime or time.time(), _ZIP_EPOCH))[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.file_size = size
            with fh, zf.open(info, "w") as out:
                while chunk := fh.read(_CHUNK):
                    out.write(chunk)
                    yield sink.take()
    yield sink.take()


def whole(vault: Path, top: str) -> Iterator[bytes]:
    """Point 2: every file, `.silica/` included, under one folder `top`."""
    return stream_zip((f"{top}/{rel}", p, None) for rel, p in vault_files(vault))


class Plan(NamedTuple):
    top: str
    files: list[tuple[str, Path | bytes, float | None]]  # stream_zip's entries
    sent: dict[str, dict]  # vault path -> its sent-list entry, for each file carried
    notes: int             # how many of the files are notes
    left: int              # path links left as written: no file the person has


def _read(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None  # deleted by a turn since the walk: not part of the vault any more


def _sha(p: Path) -> str | None:
    """A file's sha256 read in chunks: an image or a leaf is sent from disk,
    never held whole, as point 6 holds for the archive itself."""
    h = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
    except FileNotFoundError:
        return None  # deleted by a turn since the walk
    return h.hexdigest()


def _links(text: str):
    """The wikilinks of `text` outside code and math, its frontmatter's
    included: Silica writes links into properties too (rename.py, A19)."""
    from silica.kernel.link.autolink import _FRONTMATTER_RE, SKIP_PATTERNS_BASE, build_skip_mask
    from silica.kernel.link.rename import _WIKILINK_FULL_RE, _span_is_clear

    mask = build_skip_mask(text, SKIP_PATTERNS_BASE)
    fm = _FRONTMATTER_RE.match(text)
    if fm:
        mask[:fm.end()] = [False] * fm.end()
    return [m for m in _WIKILINK_FULL_RE.finditer(text) if _span_is_clear(mask, m.start(), m.end())]


def plan_additions(vault: Path, state: dict, top: str) -> Plan:
    """Points 3 and 4: the notes outside the person's material (the Inbox,
    `Done/`, `sources/`, hidden folders) not sent before, the files they embed,
    the leaves they cite that the person has no copy of, and every path link
    in them rewritten for the root of the person's Obsidian vault."""
    from silica.config import CONFIG
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.base.manifest import (
        active_done_dir,
        active_write_dir,
        in_write_dir,
        resolve_inbox_dir,
    )
    from silica.kernel.base import frontmatter

    inbox = resolve_inbox_dir(str(vault), active_write_dir(), CONFIG.inbox_dir or "Inbox")
    bases = [b for b in (inbox, active_done_dir(), in_write_dir(SOURCES_DIR)) if b]
    leaves = in_write_dir(SOURCES_DIR)
    files = dict(vault_files(vault))
    by_path = {rel.casefold(): rel for rel in files}
    by_name: dict[str, str] = {}
    for rel in files:
        by_name.setdefault(PurePosixPath(rel).name.casefold(), rel)
    roots, sent = set(state["roots"]), state["sent"]
    gone = {e.get("sha256") for p, e in sent.items() if p not in files}

    def theirs(rel: str) -> bool:
        return any(rel.startswith(b + "/") for b in bases) or any(s.startswith(".") for s in rel.split("/"))

    def below_root(rel: str) -> tuple[str, str] | None:
        """(path below the root, path under the inbox) of a file the person
        keeps in Obsidian: in the Inbox, in `Done/` or a leaf under `sources/`,
        below a folder that was an Obsidian vault."""
        for b in bases:
            if rel.startswith(b + "/"):
                key = rel[len(b) + 1:]
                root, _, rest = key.partition("/")
                return (rest, key) if root in roots and rest else None
        return None

    def raw_of(below: str, key: str) -> str:
        """`below`, or the raw a converted source names: the person keeps the
        PDF in Obsidian, not the markdown Silica made of it."""
        if not below.lower().endswith(".md"):
            return below
        for b in bases:
            p = files.get(f"{b}/{key}")
            data = _read(p) if p else None
            fm = frontmatter.split(data.decode("utf-8", "replace"))[0] if data else None
            raw = str((fm or {}).get("source_file") or "")
            if raw and not raw.lower().endswith(".md"):
                return str(PurePosixPath(below).with_name(PurePosixPath(raw).name))
        return below

    carried: dict[str, str] = {}  # vault path -> sha256 of what goes
    raw: dict[str, bytes] = {}     # the notes whose links are rewritten; the rest stays on disk

    def carry(rel: str, data: bytes | None = None) -> None:
        # Sent before at this path, or moved since with the same bytes: the
        # sha256 alone would drop a distinct note that happens to match one.
        if rel in sent or rel in carried:
            return
        sha = hashlib.sha256(data).hexdigest() if data is not None else _sha(files[rel])
        if sha and sha not in gone:
            carried[rel] = sha
            if data is not None:
                raw[rel] = data

    for rel, p in files.items():
        if rel.lower().endswith(".md") and not theirs(rel):
            data = _read(p)
            if data is not None:
                carry(rel, data)
    notes = list(carried)

    def resolve(target: str, bang: str) -> str | None:
        t = target.strip()
        if "/" in t:
            return by_path.get(t.casefold()) or by_path.get(f"{t}.md".casefold())
        # A bare embed of a file resolves by its name anywhere, as Obsidian does.
        return by_name.get(t.casefold()) if bang and PurePosixPath(t).suffix not in ("", ".md") else None

    texts: dict[str, str] = {}
    for rel in notes:
        texts[rel] = text = raw[rel].decode("utf-8", "replace")
        for m in _links(text):
            got = resolve(m.group(2), m.group(1))
            if not got or got in carried or below_root(got):
                continue
            if m.group(1) and not got.lower().endswith(".md"):
                carry(got)        # an image the note shows
            elif got.startswith(leaves + "/"):
                carry(got)        # the leaf of a source the person has no copy of

    def where(rel: str) -> str | None:
        if rel in carried:
            return f"{top}/{rel}"
        if rel in sent:
            return f"{sent[rel].get('folder')}/{rel}"
        found = below_root(rel)
        return raw_of(*found) if found else None

    left = 0
    out: dict[str, bytes] = {}
    for rel, text in texts.items():
        parts, at = [], 0
        for m in _links(text):
            bang, target, suffix, alias = m.group(1), m.group(2).strip(), m.group(3) or "", m.group(4) or ""
            if "/" not in target:
                continue  # a bare name resolves in any folder: nothing to rewrite
            got = resolve(target, bang)
            new = where(got) if got else None
            if new is None:
                left += 1
                continue
            if new.lower().endswith(".md") and not target.lower().endswith(".md"):
                new = new[:-3]
            parts += [text[at:m.start()], f"{bang}[[{new}{suffix}{alias}]]"]
            at = m.end()
        # shortcut: wikilinks only; a markdown link to a vault path is left as
        # written and not counted, since Silica writes wikilinks. Reopen if
        # G1's vault holds one.
        out[rel] = ("".join(parts) + text[at:]).encode("utf-8") if parts else raw[rel]

    entries: list[tuple[str, Path | bytes, float | None]] = []
    for rel in sorted(carried):
        try:
            mtime = files[rel].stat().st_mtime
        except OSError:
            mtime = None
        entries.append((f"{top}/{rel}", out[rel] if rel in out else files[rel], mtime))
    return Plan(top, entries, {rel: {"sha256": sha, "folder": top} for rel, sha in carried.items()},
                sum(rel.lower().endswith(".md") for rel in carried), left)


def added(vault: Path, plan: Plan) -> Iterator[bytes]:
    """The archive of `plan`, then the sent list advanced once its last byte is
    handed over (point 3). The route starts it before returning, so that it
    releases BUSY however it ends, a request dropped before its first byte
    included."""
    try:
        yield b""
        yield from stream_zip(plan.files)
        with _STATE_LOCK:
            state = load_state(vault)
            state["sent"].update(plan.sent)
            state["last"] = time.time()
            save_state(vault, state)
    finally:
        BUSY.release()


def summary(vault: Path) -> dict:
    """What each of the two downloads would give, for the vault menu.
    shortcut: plans the additions in full each time the menu opens; cache it
    by /vault_version if G2 measures it past 200 ms on a 5,000-note vault."""
    count = size = 0
    for _, p in vault_files(vault):
        try:
            size += p.stat().st_size
            count += 1
        except OSError:
            continue  # deleted since the walk
    state = load_state(vault)
    plan = plan_additions(vault, state, "")
    return {"vault": {"files": count, "bytes": size},
            "added": {"files": len(plan.files), "notes": plan.notes, "last": state["last"]}}
