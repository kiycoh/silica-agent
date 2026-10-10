# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import re

from rich.markdown import Heading, Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

# Single glyph vocabulary for the whole TUI — everything single-width, no emoji
# (double-width glyphs tear column alignment).
GLYPHS: dict[str, str] = {
    "ok": "✓",
    "err": "✗",
    "run": "⏺",
    "bullet": "·",
    "arrow": "→",
    "model": "◆",
    "worker": "◇",
    "vault": "⬡",
    "active": "◉",
    "pending": "·",
    "warn": "⚠",
    "info": "ℹ",
    "gear": "⚙",
    "think": "✦",
}


class _FlatHeading(Heading):
    """Left-aligned heading: no h1 box, no centering — flat gutter language."""

    def __rich_console__(self, console, options):
        text = self.text
        text.justify = "left"
        yield Text("")
        yield text


# Code first so a regex like `\\[a-z\\]` in a fence or a span is left alone.
# ponytail: `~~~` fences and indented code are not guarded; the upgrade is to
# rewrite over markdown-it's token stream (text tokens only) instead of a regex.
_MATH_OR_CODE = re.compile(
    r"(```.*?```|`[^`\n]*`)"
    r"|\$\$(.*?)\$\$|\\\[(.*?)\\\]"
    r"|\$([^$\n]+?)\$|\\\((.*?)\\\)",
    re.DOTALL,
)
# The ASCII punctuation CommonMark lets a backslash escape: escaping all of it
# is what makes a span come out of markdown-it byte for byte.
_PUNCT = re.compile(r"([!-/:-@\[-`{-~])")


def _literal_math(markup: str) -> str:
    """Hand every math span to markdown-it as literal text, dollar-delimited.

    rich renders no math and markdown-it has no rule for `$`, so a formula is
    parsed as prose: `\\[` and `\\(` are backslash escapes and lose the slash,
    leaving a bare `[ n = ... ]` where a formula opened; inside `$$` the same
    rule eats `\\{`, `\\,`, `\\\\`, and `*b*` becomes emphasis (seen live
    2026-09-21). Escaping the innards' punctuation is what survives the parser;
    the delimiters become dollars because that is the form the prompt asks for
    (prompts.py) and the form the vault keeps.
    """

    def sub(m: re.Match) -> str:
        if m.group(1):
            return m.group(1)
        assert m.lastindex is not None  # every alternative of _MATH_OR_CODE captures
        body = _PUNCT.sub(r"\\\1", m.group(m.lastindex))
        return f"$${body}$$" if m.lastindex in (2, 3) else f"${body}$"

    return _MATH_OR_CODE.sub(sub, markup)


class FlatMarkdown(Markdown):
    """Markdown whose headings render flat (styles come from theme markdown.h*),
    with bare URLs autolinked so rich wraps them in OSC 8.

    rich builds its own parser in ``Markdown.__init__`` and parses immediately,
    so the only seam is to re-parse. The displayed text stays the URL itself,
    which is what keeps this honest on a terminal with no OSC 8 support (macOS
    Terminal, screen): the sequence is ignored and the URL is still readable and
    selectable, exactly as before.
    """

    elements = {**Markdown.elements, "heading_open": _FlatHeading}

    def __init__(self, markup: str, **kwargs) -> None:
        from markdown_it import MarkdownIt

        markup = _literal_math(markup)
        super().__init__(markup, **kwargs)
        parser = (
            MarkdownIt("gfm-like")
            .enable("strikethrough")
            .enable("table")
            .enable("linkify")
        )
        parser.options["linkify"] = True
        # fuzzy_link off or `nota.md` in prose resolves to the Moldovan ccTLD and
        # renders as a link to http://nota.md; fuzzy_email off keeps the scope at
        # "a URL is clickable", not "prose opens a mail client".
        if parser.linkify is not None:  # only present with the [linkify] extra
            parser.linkify.set({"fuzzy_link": False, "fuzzy_email": False})
        self.parsed = parser.parse(markup)

GROUP_STYLE: dict[str, str] = {
    "workflow": "#22d3ee",  # BRAND_CYAN — works in both markup and Table column style
    "direct": "cyan",
    "system": "dim",
}


def command_table(
    commands: list,
    *,
    name_style: str = "bold #22d3ee",
    usage_style: str = "dim",
    show_summary: bool = True,
    compact: bool = False,
) -> Table:
    """Rich Table (no borders): name | usage | [summary].

    With ``compact=True`` the name column is pinned to its content width and the
    usage column flexes (ellipsised when truncated), so a narrow panel can never
    crush the command names — used by the side-by-side home overview.
    """
    table = Table(
        show_header=False, box=None, padding=(0, 3, 0, 0), pad_edge=False, expand=compact
    )
    name_width = max((len(c.name) for c in commands), default=0) if compact else None
    table.add_column(style=name_style, no_wrap=True, width=name_width)
    if compact:
        table.add_column(style=usage_style, no_wrap=True, overflow="ellipsis", ratio=1)
    else:
        # Wrappable so an outlier usage (e.g. /organize) reflows instead of
        # forcing Rich to crush the no_wrap name column down to "/o…".
        table.add_column(style=usage_style, no_wrap=False)
    if show_summary:
        table.add_column(no_wrap=False)
    for cmd in commands:
        row = (cmd.name, escape(cmd.usage), escape(cmd.summary)) if show_summary else (cmd.name, escape(cmd.usage))
        table.add_row(*row)
    return table
