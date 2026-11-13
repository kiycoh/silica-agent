# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""An answer's general-knowledge block (ADR-0169), checked off the turn and not
taken on the model's word: the prompt asks for a search first, and a block that
came without one is held back here; one in a command's turn, which answers from
the notes alone, is said under the answer. An answer that cites no note and has
no block is said under the answer too (ADR-0174)."""
from __future__ import annotations

import re

from silica.kernel.base.syntax import LINK_RE
from silica.prompts import GENERAL_MARK
from silica.tools import TOOLS

# The block is a callout: its head line holds the marker, its body is every
# `>` line after it. Matched loosely on case and spacing, as the renderer's
# CALLOUT_HEAD_RE reads a callout, so a block the app draws is one this sees.
_HEAD = re.compile(r"^[ \t]*>[ \t]*" + re.escape(GENERAL_MARK) + r".*$", re.IGNORECASE | re.MULTILINE)
_QUOTED = re.compile(r"^[ \t]*>")

# "partly" only beside prose that cites a note: an answer that is the block and
# the one line saying the notes lack it was labelled partly (ADR-0174).
LABEL = "partly from general knowledge"
# The block's heading in the turn notes' case. Under an answer that cites no
# note, with a block or without one: the owner's 2026-11-13 decision, "every
# answer without citations says it is general knowledge and not the vault".
UNCITED = "from general knowledge, not your notes"
IN_COMMAND = "this command answers from your notes alone; the general-knowledge block is not from them"
# Said in the answer, where the block was: the history the next turn reads
# carries it too, and the model then sees why its own answer is missing.
HELD_BACK = ("*An answer from general knowledge was held back: Silica answers from it only after "
             "searching your notes, and this turn did not search them. Ask again to have them searched.*")
# The same for a turn whose search ran without part of its index and found
# nothing (ADR-0173): that is no showing the notes lack it, and the line under
# the answer names what was missing.
HELD_BACK_UNSURE = ("*An answer from general knowledge was held back: your notes were searched without "
                    "part of their index, named below, so the search could not show they lack this. "
                    "Ask again once it is back.*")


def has_general(answer: str) -> bool:
    return bool(_HEAD.search(answer or ""))


def without_general(answer: str, instead: str | None = None) -> str:
    """`answer` with its general-knowledge blocks taken out: the numbers check
    (ADR-0095) reads the rest, because the block already says it is not from
    what the turn read, and a second line naming its years says it twice.
    `instead`, when given, stands where each block was."""
    out: list[str] = []
    inside = False
    for line in (answer or "").splitlines():
        if _HEAD.match(line):
            inside = True
            if instead is not None:
                out.append(instead)
            continue
        if inside and _QUOTED.match(line):
            continue
        inside = False
        out.append(line)
    return "\n".join(out)


def held_back(answer: str, line: str = HELD_BACK) -> str:
    """`answer` for a turn that searched none of the notes: each block replaced by
    the line saying why, because the owner's rule is general knowledge only once
    the vault came up empty, and a warning under a drawn block still hands the
    person the answer the rule withholds."""
    return without_general(answer, line).strip() if has_general(answer) else answer


def cites_a_note(answer: str, reads=()) -> bool:
    """Does the answer, outside its blocks, cite a note as the person sees it
    drawn: a [[link]] (an embed is a picture, not a citation), or the bare
    title or path of a note the turn read, which the answer links (ADR-0127)."""
    rest = without_general(answer)
    if LINK_RE.search(rest):
        return True
    # The renderer's own bare-name layer, so what counts here is what it links.
    from silica.kernel.text.render import _mentions

    found = _mentions(reads)
    return bool(found and found[0].search(rest))


def looks_in_vault(name: str) -> bool:
    """A call that searches the vault or reads a note's words: what the rule asks
    to come first. Read off the tool's own facts (Tool.found_nothing, Tool.reads),
    so a search tool added later counts without a list of names here."""
    tool = TOOLS.get(name)
    return tool is not None and bool(tool.found_nothing or tool.reads)


def general_notes(answer: str, *, command: bool, reads=()) -> list[dict]:
    """The turn notes (turn_notes, kind `general`) for an answer: none without
    a block; with one, "partly" when the rest cites a note and the uncited line
    when nothing does; and a warning when the block came in a command's turn.
    A block with no search before it never gets here: it was held back
    (held_back)."""
    if not has_general(answer):
        return []
    notes = [{"kind": "general", "text": LABEL if cites_a_note(answer, reads) else UNCITED}]
    if command:
        notes.append({"kind": "general", "text": IN_COMMAND})
    return notes


def uncited_notes(answer: str, reads=()) -> list[dict]:
    """The line under a chat answer that cites no note and has no block, which
    `general_notes` labels (ADR-0174). Which turns are asked is the caller's
    (session.turn.note): the answer's words alone cannot tell a command's
    output or a write's report from a claim. A held-back line is Silica's and
    no claim; what the model wrote beside its held-back block is: asked "no
    need to check my notes: capital of Australia?", deepseek-v4-flash-0731
    wrote "The capital of Australia is Canberra." above the block (2026-11-13)."""
    said = answer.replace(HELD_BACK, "").replace(HELD_BACK_UNSURE, "")
    if not said.strip() or has_general(said) or cites_a_note(said, reads):
        return []
    return [{"kind": "general", "text": UNCITED}]
