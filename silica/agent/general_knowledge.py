# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""An answer's general-knowledge block (ADR-0169), checked off the turn and not
taken on the model's word: the prompt asks for a search first, and this says
under the answer when the block came without one, or in a command's turn, which
answers from the notes alone."""
from __future__ import annotations

import re

from silica.prompts import GENERAL_MARK
from silica.tools import TOOLS

# The block is a callout: its head line holds the marker, its body is every
# `>` line after it. Matched loosely on case and spacing, as the renderer's
# CALLOUT_HEAD_RE reads a callout, so a block the app draws is one this sees.
_HEAD = re.compile(r"^[ \t]*>[ \t]*" + re.escape(GENERAL_MARK) + r".*$", re.IGNORECASE | re.MULTILINE)
_QUOTED = re.compile(r"^[ \t]*>")

LABEL = "partly from general knowledge"
UNSEARCHED = "the general-knowledge block came before any search of your notes"
IN_COMMAND = "this command answers from your notes alone; the general-knowledge block is not from them"


def has_general(answer: str) -> bool:
    return bool(_HEAD.search(answer or ""))


def without_general(answer: str) -> str:
    """`answer` with its general-knowledge blocks taken out: the numbers check
    (ADR-0095) reads the rest, because the block already says it is not from
    what the turn read, and a second line naming its years says it twice."""
    out: list[str] = []
    inside = False
    for line in (answer or "").splitlines():
        if _HEAD.match(line):
            inside = True
            continue
        if inside and _QUOTED.match(line):
            continue
        inside = False
        out.append(line)
    return "\n".join(out)


def looks_in_vault(name: str) -> bool:
    """A call that searches the vault or reads a note's words: what the rule asks
    to come first. Read off the tool's own facts (Tool.found_nothing, Tool.reads),
    so a search tool added later counts without a list of names here."""
    tool = TOOLS.get(name)
    return tool is not None and bool(tool.found_nothing or tool.reads)


def general_notes(answer: str, *, looked: bool, command: bool) -> list[dict]:
    """The turn notes (turn_notes, kind `general`) for an answer: none without
    a block; the label with one; and a warning when the block came in a
    command's turn or before any search of the vault."""
    if not has_general(answer):
        return []
    notes = [{"kind": "general", "text": LABEL}]
    if command:
        notes.append({"kind": "general", "text": IN_COMMAND})
    elif not looked:
        notes.append({"kind": "general", "text": UNSEARCHED})
    return notes
