# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import io

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

# The terminal's two hues. The app went to one hue on 2026-08-23 and these stayed: a change here
# recolours every terminal, which is the owner's call (test_design_copies states the drift).
BRAND_CYAN = (0x22, 0xD3, 0xEE)
BRAND_INDIGO = (0x63, 0x66, 0xF1)
CYAN = "#22d3ee"
INDIGO = "#6366f1"
AMBER = "#f59e0b"

SILICA_THEME = Theme(
    {
        "brand.cyan": CYAN,
        "brand.indigo": INDIGO,
        "reasoning": CYAN,
        "reasoning.gutter": INDIGO,
        "role.assistant": f"bold {CYAN}",
        "tool.ok": "green",
        "tool.err": "red",
        "warn": AMBER,
        "muted": "dim",
        # Flat headings (see style.FlatMarkdown): color carries the hierarchy, no box/underline.
        "markdown.h1": f"bold {CYAN}",
        "markdown.h2": f"bold {CYAN}",
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
