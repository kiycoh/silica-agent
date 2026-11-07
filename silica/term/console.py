# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import io

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

BRAND_CYAN = (0x22, 0xD3, 0xEE)
BRAND_INDIGO = (0x63, 0x66, 0xF1)

SILICA_THEME = Theme(
    {
        "brand.cyan": "#22d3ee",
        "brand.indigo": "#6366f1",
        "reasoning": "#22d3ee",
        "reasoning.gutter": "#6366f1",
        "role.assistant": "bold #22d3ee",
        "tool.ok": "green",
        "tool.err": "red",
        "warn": "#f59e0b",
        "muted": "dim",
        # Flat headings (see style.FlatMarkdown): color carries the hierarchy, no box/underline.
        "markdown.h1": "bold #22d3ee",
        "markdown.h2": "bold #22d3ee",
        "markdown.h3": "bold",
    }
)

CONSOLE = Console(highlight=False, theme=SILICA_THEME)


def plain(lines: list[tuple[tuple, dict]]) -> str:
    """A command's lines as the text a terminal of CONSOLE's width would show,
    without its colour: what a surface with no terminal draws in a code block.
    Wrapped by Rich, because a long path reads the same in both that way."""
    text = io.StringIO()
    console = Console(file=text, width=CONSOLE.width, highlight=False, theme=SILICA_THEME, color_system=None)
    for objects, style in lines:
        console.print(*objects, **style)
    return Text.from_ansi(text.getvalue()).plain.strip()
