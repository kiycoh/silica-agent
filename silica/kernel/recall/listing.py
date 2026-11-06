# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Which notes a folder holds, in the order a reader and the injector take them.

Below the tools, because the outline lane and the lab pass ask it too, and they
were importing a 2,000-line tool module to list a folder.
"""
from __future__ import annotations

from pathlib import Path

from silica.driver import DRIVER


def natural_key(path: str) -> list:
    """Sort key that reads embedded runs of digits as numbers.

    Plain lexicographic order interleaves "Lezione 10" between 1 and 2, and the
    injector ingests a folder in listing order: lesson 10's concepts would land
    before lesson 2 defines them. re.split alternates non-digit/digit, so the
    element at a given index always has the same type across paths.
    """
    import re

    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path)]


def notes_under(folder: str) -> list[str]:
    """Vault-relative `.md` paths under `folder`, natural-sorted. "" ⇒ the vault.

    The one answer to "which notes does this folder hold?", because neither
    half of the codebase could give it for an inbox folder: the note index used
    to skip the inbox entirely, and `sources.registry.expand_folder` answers
    only for the code lane off a git-backed census — a plain Obsidian vault is
    no repo, so it returns [] for every folder in it. Between the two a folder
    argument had no listing at all, and the only caller left to produce one was
    an LLM guessing filenames.

    The index now walks the inbox, so `list_files` alone answers. The
    `list_inbox_files` fallback below stays as belt-and-braces: its failure mode
    was a model inventing filenames, which is not a failure worth re-earning to
    save four lines.
    """
    from silica.kernel.base.layout import in_folder
    from silica.kernel.base.manifest import active_inbox_dir

    scope = vault_rel(folder)
    # list_files(folder) pre-filters loosely (startswith); in_folder tightens it
    # so a Foo/ argument never leaks into a sibling FooBar/.
    paths = [r.path for r in DRIVER.list_files(scope) if in_folder(r.path, scope)]
    if not paths and scope:
        inbox = active_inbox_dir()
        if inbox and in_folder(scope, inbox):
            # .md only: the unconverted files (PDFs) are silica_inbox_ls's job,
            # and a converted folder's Images/ leaves outnumber its notes 70:1.
            paths = [
                r.path for r in DRIVER.list_inbox_files()
                if r.path.endswith(".md") and in_folder(r.path, scope)
            ]
    return sorted(paths, key=natural_key)


def vault_rel(path: str) -> str:
    """`path` as a vault-relative posix path; already-relative input passes through.

    Users do paste absolute vault paths at /nucleate, and every listing below is
    keyed vault-relative. A path outside the vault is returned as given: it
    matches nothing, which is the honest answer for it.
    """
    from silica.config import CONFIG

    p = Path(path.strip())
    if p.is_absolute():
        try:
            return p.resolve().relative_to(Path(CONFIG.vault_path).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.replace("\\", "/").strip("/")
