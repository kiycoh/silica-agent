# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Markdown's grammar as this codebase reads it, one regex per shape.

The parser in `markdown.py` is the one link GENERATOR (ADR-0029); these are for
everyone who scans text without it. Stdlib only, because `frontmatter` needs
them and importing markdown-it from there cost 20 ms on a process that opens no
note (2026-11-06).
"""
from __future__ import annotations

import re

# Wikilink target extraction: captures the target of [[Target]], [[Target|alias]]
# and [[Target#anchor]] (everything before the first | or #). The shared regex
# for quick target scans; extract_links is the full AST-aware version.
WIKILINK_TARGET_RE = re.compile(r"\[\[([^\]|#]+)")
