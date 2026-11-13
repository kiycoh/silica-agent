# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import io
import re

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
    """A command's lines as a terminal would show them, without colour and
    unwrapped: the browser wraps. Wrapped at CONSOLE's width (the width of the
    terminal the server started from, 80 with none), a block's lines broke a
    second time inside the chat's measure, mid-sentence (ADR-0178)."""
    text = io.StringIO()
    console = Console(file=text, width=CONSOLE.width, highlight=False, theme=SILICA_THEME, color_system=None)
    for objects, style in lines:
        console.print(*objects, **{**style, "soft_wrap": True})
    return Text.from_ansi(text.getvalue()).plain.strip()


# What a mark in a command's line means in the chat (ADR-0178), by the names the
# handlers print with. Anything else (dim, cyan, green, underline) is colour the
# chat's prose does without.
_WARN = frozenset({"yellow", "warn"})
_FAIL = frozenset({"red", "tool.err"})
_BOLD = frozenset({"bold", "b"})
_TITLE = {"warn": "Warning", "fail": "Failed"}

# Escapes, each only where Markdown reads the character as a mark, so a file name
# or a flag copied from the answer reads as typed: `my_notes_v2.md` and `--me=x`
# carry no backslash, `_a_`, `$x$` and `<b>` do. Comments are cut from the raw
# text before parsing, where a backslash is not read, so `%%` and `<!--` go in as
# entities. The renderer's own sugar runs on the text after the escapes are
# undone, and no escape reaches it: a `[[name]]`, `#word` or `==x==` in a title
# still draws as a note link, a tag or a highlight. Each keeps its words and none
# fetches anything; stopping them is a renderer change for every note.
_ALWAYS = re.compile(r"([\\*\[\]$|~&])")
_SIDE_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")
_HIDDEN = (("<!--", "&lt;!--"), ("%%", "%&#37;"), ("<", "\\<"))
_LINE_START = re.compile(r"^(\s*)([#>+=-]|\d+[.)])")
_CODE_SPAN = re.compile(r"(`[^`\n]+`)")
_COMMAND = re.compile(r"(?<![\w/.:-])(/[a-z][a-z-]*)(?![\w/-]|\.\w)")


def _escaped(text: str, commands: frozenset[str]) -> str:
    """`text` inert, but for the code spans a message wrote, which are inert
    already, and a command's name, which becomes one."""
    out = []
    for part in _CODE_SPAN.split(text):
        if _CODE_SPAN.fullmatch(part):
            out.append(part)
            continue
        for bit in _COMMAND.split(part):
            if bit in commands:
                out.append(f"`{bit}`")
                continue
            bit = _SIDE_UNDERSCORE.sub(r"\\_", _ALWAYS.sub(r"\\\1", bit))
            for mark, inert in _HIDDEN:
                bit = bit.replace(mark, inert)
            out.append(bit)
    return "".join(out)


def _runs(line: str) -> list[tuple[str, frozenset[str]]]:
    """A marked-up line as (text, style words) runs, a run per change of style."""
    t = Text.from_markup(line)
    words: list[set[str]] = [set() for _ in t.plain]
    for span in t.spans:
        for i in range(span.start, min(span.end, len(words))):
            words[i].update(str(span.style).split())
    runs: list[tuple[str, frozenset[str]]] = []
    for ch, w in zip(t.plain, words):
        key = frozenset(w)
        if runs and runs[-1][1] == key:
            runs[-1] = (runs[-1][0] + ch, key)
        else:
            runs.append((ch, key))
    return runs


def _inline(runs, resolve, commands) -> str:
    out = []
    for text, words in runs:
        ref = text.strip()
        if not (words & _BOLD) or not ref:
            out.append(_escaped(text, commands))
            continue
        lead, tail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
        # A count or an id in bold is no note's name, however a vault names its notes.
        key = (resolve(ref) if resolve and any(c.isalpha() for c in ref)
               and not any(c in ref for c in "|#^[]") else None)
        if key:
            out.append(f"{lead}[[{key[:-3] if key.endswith('.md') else key}]]{tail}")
        else:
            out.append(f"{lead}**{_escaped(ref, commands)}**{tail}")
    return "".join(out)


def markdown(lines: list[tuple[tuple, dict]], resolve=None) -> str:
    """A command's lines as the chat's prose, in place of the code block `plain`
    filled (ADR-0178). A bold run `resolve` finds is a link to its note; a yellow
    line a Warning box, a red one a Failed box, titled by the bold words it opens
    with if it opens with some; a line set in from the one above an item of a
    list; a printed layout (more than one line with markup=False, or any object
    that is not a string) the block it always was. Everything else is escaped:
    titles, file names and errors are text, never Markdown."""
    from silica.command_table import COMMANDS

    commands = frozenset(c.name for c in COMMANDS)
    rows: list[tuple] = []          # ("block", md) or ("line", indent, kind, runs)
    for objects, style in lines:
        said = str(style.get("sep", " ")).join(o for o in objects if isinstance(o, str))
        if any(not isinstance(o, str) for o in objects) or (
                style.get("markup", True) is False and "\n" in said.strip("\n")):
            rows.append(("block", f"```text\n{plain([(objects, style)])}\n```"))
            continue
        for raw in said.split("\n"):
            runs = _runs(raw) if style.get("markup", True) else [(raw, frozenset())]
            text = "".join(r[0] for r in runs)
            if not text.strip():
                rows.append(("line", 0, "gap", []))
                continue
            indent = len(text) - len(text.lstrip(" "))
            runs = _trimmed(runs)
            # The line's own colour, from where it starts: a list row with a
            # yellow note at its end is a row, not a warning.
            first = runs[0][1]
            kind = "warn" if first & _WARN else "fail" if first & _FAIL else ""
            rows.append(("line", indent, kind, runs))

    base = min((r[1] for r in rows if r[0] == "line" and r[2] != "gap"), default=0)
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []
    box: list = []                  # [kind, title, lines]

    def flush() -> None:
        if para:
            blocks.append("\\\n".join(para))
            para.clear()
        if items:
            blocks.append("\n".join(f"- {i}" for i in items))
            items.clear()
        if box:
            body = "\\\n> ".join(box[2])
            blocks.append(f"> [!{'warning' if box[0] == 'warn' else 'failure'}] {box[1]}\n> {body}")
            box.clear()

    def line_of(runs) -> str:
        return _LINE_START.sub(r"\1\\\2", _inline(runs, resolve, commands))

    for row in rows:
        if row[0] == "block":
            flush()
            blocks.append(row[1])
            continue
        _, indent, kind, runs = row
        if kind == "gap":
            flush()
            continue
        if kind:
            title = _TITLE[kind]
            if runs[0][1] & _BOLD and len(runs) > 1:
                title = runs[0][0].strip().rstrip(".:")
                runs = _trimmed(runs[1:])
            if box and box[0] == kind and title == _TITLE[kind] == box[1]:
                box[2].append(line_of(runs))
                continue
            flush()
            box.extend([kind, _escaped(title, commands), [line_of(runs)]])
            continue
        line = line_of(runs)
        if indent > base and (para or items):
            if para:
                blocks.append("\\\n".join(para))
                para.clear()
            items.append(line)
            continue
        if items or box:
            flush()
        para.append(line)
    flush()
    return "\n\n".join(blocks) or "Done."


def _trimmed(runs):
    """`runs` without the spaces that set the line in: the indent is read
    before, and Markdown would read it as a code block."""
    out = list(runs)
    while out and not out[0][0].strip():
        out.pop(0)
    if out:
        out[0] = (out[0][0].lstrip(), out[0][1])
    return out or [("", frozenset())]
