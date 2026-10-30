# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The public page (ADR-0143): which notes a person put on it, and where
Silica keeps the static files it writes for them.

The choice lives in two places, as in Obsidian Publish: a folder's globe in
`vault.yaml` (`public: folders:`), a note's own `publish:` in its frontmatter,
which beats its folder. The whole page has a switch (`public: page:`). The
files themselves are written by `silica.ui.web.public_site`, which renders
with the drawer's renderer; this module only decides and moves folders.
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

PUBLIC_KEY = "public"

# Learn's plan writer takes its folder as a default argument, not a constant
# (learn/plans.py), so the name is repeated here; a plan is a study schedule,
# never something a page should show.
PLANS_DIR = "Plans"


def _vault() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path or ".")


def _block(vault: Path) -> dict:
    """The raw `public:` block of vault.yaml; {} when absent or malformed."""
    import yaml

    from silica.kernel.vault_manifest import MANIFEST_REL

    path = vault / MANIFEST_REL
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    block = raw.get(PUBLIC_KEY) if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else {}


def slugify(text: str) -> str:
    """`Sleep and memory consolidation` -> `sleep-and-memory-consolidation`;
    accents folded, so an Italian title keeps a readable address."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")


def handle(vault: Path | None = None) -> str:
    """The page's @name. On the server the host assigns it and the account
    cannot pick another's (ADR-0143 point 7), so an account without one has
    no page rather than one named after its vault folder."""
    from silica.config import hosted

    env = os.environ.get("SILICA_PUBLIC_HANDLE", "").strip()
    if env or hosted():
        return slugify(env)
    vault = vault or _vault()
    named = _block(vault).get("handle")
    return slugify(named if isinstance(named, str) and named.strip() else vault.resolve().name)


def page_url(vault: Path | None = None) -> str:
    """The page's address, "" when no SILICA_PUBLIC_BASE says where it is served."""
    base = os.environ.get("SILICA_PUBLIC_BASE", "").strip().rstrip("/")
    h = handle(vault)
    return f"{base}/@{h}" if base and h else ""


def settings(vault: Path | None = None) -> dict:
    """{"page": bool, "folders": [...]}: off and empty until the person says
    otherwise, so a vault never goes public by default."""
    block = _block(vault or _vault())
    raw = block.get("folders")
    folders = sorted({f.strip().strip("/") for f in raw if isinstance(f, str) and f.strip().strip("/")}) \
        if isinstance(raw, list) else []
    return {"page": block.get("page") is True, "folders": folders}


def never_dirs() -> tuple[str, ...]:
    """The folders Silica writes for its own work, never published and given
    no globe (ADR-0143 point 3): the text of every nucleated document sits in
    sources/, the inbox and its archive hold converted files."""
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import (
        active_done_dir,
        active_inbox_dir,
        get_active_manifest,
        in_write_dir,
    )
    from silica.kernel.workspaces import DEFAULT_FOLDER

    dirs = {active_inbox_dir(), active_done_dir(), in_write_dir(SOURCES_DIR),
            in_write_dir(DEFAULT_FOLDER), in_write_dir(PLANS_DIR),
            get_active_manifest().conventions.templates_dir}
    return tuple(sorted({d.strip("/") for d in dirs if d and d.strip("/")}))


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def never(path: str, dirs: tuple[str, ...] | None = None) -> bool:
    """True for a path no globe can publish: under one of Silica's folders,
    or under any dot-folder (.silica, .silica-chat, .obsidian, .trash)."""
    if any(part.startswith(".") for part in path.split("/")[:-1]):
        return True
    return any(_within(path, d) for d in (never_dirs() if dirs is None else dirs))


def _flag(value) -> bool | None:
    """A note's `publish:` as a choice: True, False, or None for no choice.
    A quoted "true" is still a choice; anything else is none."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def state(path: str, fm: dict, folders: list[str], dirs: tuple[str, ...]) -> str:
    """"on", "off", "struck" (left out of a public folder by its own globe)
    or "never". A clip is someone else's text, so a folder's globe does not
    take it: only its own does (ADR-0143 point 3)."""
    if never(path, dirs):
        return "never"
    own = _flag(fm.get("publish"))
    if own is True:
        return "on"
    folder_on = any(_within(path, f) for f in folders)
    if own is False:
        return "struck" if folder_on else "off"
    return "on" if folder_on and fm.get("form") != "clip" else "off"


def frontmatters(vault: Path | None = None) -> dict[str, dict]:
    """{path: frontmatter} of every note, the walk Workspaces already memoizes."""
    from silica.kernel.workspaces import _frontmatters

    return _frontmatters(vault or _vault())


def selection(vault: Path | None = None) -> dict:
    """Every note's state and the settings it was computed from."""
    vault = vault or _vault()
    conf = settings(vault)
    dirs = never_dirs()
    fms = frontmatters(vault)
    notes = {p: state(p, fm, conf["folders"], dirs) for p, fm in fms.items()}
    return {**conf, "never": list(dirs), "notes": notes, "frontmatter": fms}


def set_folder(folder: str, on: bool, vault: Path | None = None) -> list[str]:
    """Put a folder's globe on or off; returns the folders now public."""
    from silica.kernel.vault_manifest import set_block_key

    vault = vault or _vault()
    folder = folder.strip().strip("/")
    if not folder or never(folder + "/x", never_dirs()):
        raise ValueError(f"{folder or 'that folder'} cannot be published")
    folders = set(settings(vault)["folders"])
    if on:
        folders.add(folder)
    else:
        folders.discard(folder)
    set_block_key(vault, PUBLIC_KEY, "folders", sorted(folders))
    return sorted(folders)


def set_note(path: str, on: bool, vault: Path | None = None) -> str:
    """Put a note's globe on or off, writing the least that says it: no
    `publish:` when its folder already decides, `true` or `false` when the
    note must differ from its folder. Returns the note's new state."""
    vault = vault or _vault()
    dirs = never_dirs()
    if never(path, dirs):
        raise ValueError(f"{path} cannot be published")
    fm = frontmatters(vault).get(path)
    if fm is None:
        raise ValueError(f"{path} is not a note of this vault")
    folders = settings(vault)["folders"]
    by_folder = any(_within(path, f) for f in folders) and fm.get("form") != "clip"
    value = (None if by_folder else True) if on else (False if by_folder else None)
    _write_publish(path, value)
    return state(path, {**fm, "publish": value} if value is not None
                 else {k: v for k, v in fm.items() if k != "publish"}, folders, dirs)


def _write_publish(path: str, value: bool | None) -> None:
    """Set or remove `publish:` through the driver, so the change is a session
    change like any edit: the ledger keeps the old bytes and /undo restores
    them. Not `overwrite`, which stamps a `type:` the person never asked for.
    Lowercase YAML booleans, from the dumper."""
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter as fm

    data, raw, _body = fm.split(DRIVER.read_note(path).content)
    if data is None and raw is not None:
        raise ValueError(f"the properties of {path} do not parse; fix them before publishing it")
    if value is not None:
        DRIVER.set_prop(path, "publish", value)
        return
    unset = getattr(DRIVER, "unset_prop", None)
    if unset is None:
        # The Obsidian bridge has no removal; Obsidian itself does.
        raise ValueError(f"remove `publish:` from {path} in Obsidian")
    unset(path, "publish")


# --- where the files go -------------------------------------------------------

def site_root(h: str | None = None) -> Path:
    """`~/.silica/public/<handle>`: one folder per page, so a laptop serving
    two vaults keeps two pages. `live/` is what the host serves, `paused/`
    the last publication while the page is off."""
    base = os.environ.get("SILICA_PUBLIC_DIR", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".silica" / "public"
    return root / (h if h is not None else handle())


def set_page(on: bool, vault: Path | None = None) -> bool:
    """Turn the whole page on or off (ADR-0143 point 5). Off moves `live/`
    aside, so its address answers that there is no page; on moves the last
    publication back. Nothing about the choice of notes changes."""
    from silica.kernel.vault_manifest import set_block_key

    vault = vault or _vault()
    set_block_key(vault, PUBLIC_KEY, "page", bool(on))
    h = handle(vault)
    if not h:
        return bool(on)
    root = site_root(h)
    live, paused = root / "live", root / "paused"
    if on and not live.exists() and paused.exists():
        paused.rename(live)
    elif not on and live.exists():
        if paused.exists():
            shutil.rmtree(paused)
        live.rename(paused)
    return bool(on)


def swap_in(build: Path, h: str) -> Path:
    """Make a finished build the live page. Two renames, so a visitor can miss
    the page for the instant between them, never read half of one.
    shortcut: two renames leave a sub-millisecond 404; a symlink swapped with
    os.replace closes it, if a host ever reports a reader hitting that gap."""
    root = site_root(h)
    live, old = root / "live", root / ".old"
    if old.exists():
        shutil.rmtree(old)
    if live.exists():
        live.rename(old)
    build.rename(live)
    if old.exists():
        shutil.rmtree(old)
    paused = root / "paused"
    if paused.exists():
        shutil.rmtree(paused)  # a new publication replaces the one put aside
    return live
