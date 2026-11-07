# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note's name and frontmatter as the readers above want them, with no markup rendered:
what the app shows and what the reports quote start from the same two calls."""
from __future__ import annotations



def clean_name(ref: str) -> str:
    """Display name: basename without folders or `.md` (`a/b.md` -> `b`)."""
    return ref.rsplit("/", 1)[-1].removesuffix(".md")


def title_of(path: str, fm: dict) -> str:
    """A note's title as a published page names it: its `title:`, else its file."""
    t = fm.get("title")
    return t.strip() if isinstance(t, str) and t.strip() else path.rsplit("/", 1)[-1].removesuffix(".md")


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a leading YAML frontmatter block. Returns (props, body); props is
    None unless the block parses to a mapping."""
    import yaml

    from silica.kernel.base.frontmatter import FM_RE_DRAWER

    m = FM_RE_DRAWER.match(text or "")
    if not m:
        return None, text
    try:
        props = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text
    if not isinstance(props, dict):
        return None, text
    return props, text[m.end():]
