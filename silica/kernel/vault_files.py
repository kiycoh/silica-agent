# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Files a vault keeps beside its notes: a chat's folder and the Inbox it copies into, and
where a pasted image goes."""
from __future__ import annotations

import glob
import json
from collections.abc import Sequence
from pathlib import Path

from silica.config import CONFIG
from silica.kernel.base.formats import SHOWN_IMAGE_EXTS
from silica.kernel.base.syntax import CHAT_IMAGE_RE


class NotAChatFile(ValueError):
    """A path this chat's routes may not touch; the app answers it with a 400."""


class InboxClash(ValueError):
    """The Inbox holds another file of that name; the app answers it with a 409."""


def find_image(vault: str | None, path: str) -> Path | None:
    """The image a note's `path` names in `vault`, by its path or else by its bare name,
    None when there is none. One at a time, for a browser asking for one file: it matches
    the name as written and looks in dot-folders. `images_by_name` is the walk for a whole
    wiki, and differs on both, because what it answers decides what may leave the vault."""
    if not path or not vault:
        return None
    if Path(path).suffix.lower() not in SHOWN_IMAGE_EXTS:
        return None
    root = Path(vault).resolve()
    target = (root / path).resolve()
    found: Path | None = target
    if not (target.is_relative_to(root) and target.is_file()):
        # escaped: rglob reads the name as a pattern, so `fig[1].png` matched
        # fig1.png and a name holding `**` raised ValueError mid-answer
        found = next((p for p in root.rglob(glob.escape(Path(path).name)) if p.is_file()), None)
    if found is None or not found.is_relative_to(root) or found.suffix.lower() not in SHOWN_IMAGE_EXTS:
        return None
    return found


def images_by_name(vault: Path) -> dict[str, Path]:
    """{name: file}, one walk, the way `find_image` takes a bare name, for the check before
    Publish: casefolded, as `![[x.png]]` matches `X.PNG` in Obsidian, and out of dot-folders,
    which never publish.
    shortcut: an image is told apart by its name, as `![[x.png]]` names it, so two files of one
    name share one answer and one thumbnail. No name repeats among the 885 image files measured on
    2026-11-06, where pasted names carry a date; key on the path if a vault of `image.png` exports
    shows up."""
    out: dict[str, Path] = {}
    for p in sorted(vault.rglob("*")):
        if p.suffix.lower() in SHOWN_IMAGE_EXTS and p.is_file() \
                and not any(s.startswith(".") for s in p.relative_to(vault).parts):
            out.setdefault(p.name.casefold(), p)
    return out


def chat_rel(rel: str, sid: str | None = None) -> tuple[str, str]:
    """(the chat folder, the rest of the path) for a file of a chat's folder;
    with `sid`, only of that chat. Anything else is refused: these routes copy
    what the browser names, and it names only its own chat's files."""
    from silica.kernel.base.layout import CHAT_DIR

    parts = Path(rel).parts
    at = parts.index(CHAT_DIR) if CHAT_DIR in parts else -1
    if (at < 0 or len(parts) < at + 3 or ".." in parts or Path(rel).is_absolute()
            or (sid is not None and parts[at + 1] != sid)):
        raise NotAChatFile("that is not a file of this chat")
    return "/".join(parts[:at + 2]), "/".join(parts[at + 2:])


def inbox_rel() -> str:
    from silica.kernel.base.manifest import active_write_dir, resolve_inbox_dir

    return resolve_inbox_dir(CONFIG.vault_path or ".", active_write_dir(), CONFIG.inbox_dir or "Inbox")


def add_to_vault(notes: Sequence[str], sources: Sequence[str] = ()) -> list[str]:
    """Copy a chat's files into the Inbox (ADR-0121 point 5), where they are
    what a drop wrote before files belonged to their chat: the file at
    `<inbox>/<name>`, its notes beside it, the pictures they embed under
    `<inbox>/Images`. Returns the notes' Inbox paths, in order.

    A copy, not a move: the chat's history names the chat's paths, and a turn
    asked again reads them there. A file the Inbox already has with other
    bytes is refused rather than overwritten, since it may be another file
    of the same name the student put there."""
    import filecmp
    import shutil

    from silica.driver import DRIVER
    from silica.kernel.write.preserve import Loss

    vault = Path(CONFIG.vault_path or ".")
    inbox = inbox_rel()
    moved: dict[str, str] = {}
    for rel in dict.fromkeys(sources):
        if rel in notes:
            continue
        _, rest = chat_rel(rel)
        src, dst = vault / rel, vault / inbox / rest
        if dst.exists() and not filecmp.cmp(src, dst, shallow=False):
            raise InboxClash(f"the Inbox already has a different file named {dst.name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)
        moved[str(src)] = str(dst)
    out: list[str] = []
    for rel in notes:
        chat, rest = chat_rel(rel)
        dst_rel = f"{inbox}/{rest}"
        if not rel.endswith(".md"):
            (vault / dst_rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vault / rel, vault / dst_rel)
            out.append(dst_rel)
            continue
        text = (vault / rel).read_text(encoding="utf-8", errors="replace")
        for old, new in moved.items():   # source_file names the file it came from
            text = text.replace(old, new)
        DRIVER.upsert(dst_rel, text, loss=Loss.STAGED)
        out.append(dst_rel)
        for m in CHAT_IMAGE_RE.finditer(text):
            pic = vault / chat / "Images" / Path(m[1] or m[2]).name
            if pic.is_file():
                (vault / inbox / "Images").mkdir(parents=True, exist_ok=True)
                shutil.copy2(pic, vault / inbox / "Images" / pic.name)
    return out


def attachment_folder(canon: str) -> str:
    """Where a pasted image of this note goes, vault-relative: what Obsidian's
    `attachmentFolderPath` says, read as Obsidian does (`./x` under the note's
    folder, `/` or empty the vault's root, else a folder of the vault), and with
    no such setting `attachments/` beside the note. A vault here is often the
    person's Obsidian vault, and an image Silica files elsewhere is lost to them."""
    beside = canon.rpartition("/")[0]
    try:
        app_json = (Path(CONFIG.vault_path) / ".obsidian" / "app.json").read_text(encoding="utf-8")
        setting = json.loads(app_json).get("attachmentFolderPath")
    except (OSError, ValueError, AttributeError):
        setting = None  # no Obsidian here, or a file it would not read either
    if not isinstance(setting, str):
        return f"{beside}/attachments".lstrip("/")
    if setting.startswith("./"):
        return f"{beside}/{setting[2:]}".strip("/")
    return setting.strip("/")
