# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Does a folder read as a source tree? Asked once, at adoption, to pick the default write root."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from silica.kernel.base.layout import ignore_matcher


logger = logging.getLogger(__name__)


# Root-level files that mark a source tree regardless of file counts (a repo
# whose sources all sit under src/ still has one at the top).
_CODE_MARKERS: frozenset[str] = frozenset({
    "pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod",
    "CMakeLists.txt", "Makefile", "pom.xml", "build.gradle", "Gemfile",
    "composer.json", "mix.exs", "build.sbt", "Package.swift",
})


def looks_like_code(root, sample_max: int = 400) -> bool:
    """True when `root` reads as a source tree rather than a folder of prose.

    Two signals, cheapest first: a root-level build/manifest marker, else a
    bounded walk comparing code files to prose files. Never git: an Obsidian
    vault under git is ordinary, and `.git/` says nothing about content.

    Only picks the *default* write root at adoption (`onboarding.adopt`), which
    the vault then declares in `vault.yaml` — a wrong guess costs one edit, not
    a wrong vault. Ratio rather than mere presence so a stray snippet.py in a
    notes folder does not read as a codebase.
    """
    from silica.kernel.code.codeast.base import BARE_LANGUAGES, EXTENSION_MAP

    root = Path(root)
    if not root.is_dir():
        return False
    if any((root / marker).is_file() for marker in _CODE_MARKERS):
        return True

    code_exts = {e for e, lang in EXTENSION_MAP.items() if lang not in BARE_LANGUAGES}
    code = prose = seen = 0
    skip = ignore_matcher(root)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not skip(d)]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in code_exts:
                code += 1
            elif ext in (".md", ".txt", ".docx", ".pdf"):
                prose += 1
            seen += 1
            if seen >= sample_max:
                return code > prose
    return code > prose
