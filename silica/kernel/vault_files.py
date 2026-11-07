# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Files a vault keeps beside its notes: a chat's folder and the Inbox it copies into, and
where a pasted image goes."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from silica.config import CONFIG
from silica.kernel.base.syntax import CHAT_IMAGE_RE


class NotAChatFile(ValueError):
    """A path this chat's routes may not touch; the app answers it with a 400."""


class InboxClash(ValueError):
    """The Inbox holds another file of that name; the app answers it with a 409."""


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
