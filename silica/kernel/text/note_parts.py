# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note's name as the readers above want it, with no markup rendered: what the
app shows and what the reports quote start from the same call."""
from __future__ import annotations


def clean_name(ref: str) -> str:
    """Display name: basename without folders or `.md` (`a/b.md` -> `b`)."""
    return ref.rsplit("/", 1)[-1].removesuffix(".md")


def title_of(path: str, fm: dict) -> str:
    """A note's title as a published page names it: its `title:`, else its file."""
    t = fm.get("title")
    return t.strip() if isinstance(t, str) and t.strip() else path.rsplit("/", 1)[-1].removesuffix(".md")
