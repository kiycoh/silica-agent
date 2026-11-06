# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The notes a user names in a message, resolved for the model (ADR-0097 point 2).

A note is named with a wikilink, typed or written by the composer's @ picker.
The model used to receive the link as text and find the note itself, by a
search call or a guessed path. Here every link is resolved against the vault
before the turn reaches the model, and the paths ride the model's copy of the
turn in one tagged block. The reference goes, not the note: the model reads
what it needs with silica_read_note, and the gate in ADR-0097 says whether the
reference is enough.
"""
from __future__ import annotations

import logging
import re

from silica.kernel.base.formats import ATTACHMENT_EXTS, ext_alternation
from silica.kernel.base.syntax import MENTION_RE

logger = logging.getLogger(__name__)

# `[[Target#heading|alias]]` names Target. `![[...]]` is an embed, a figure
# ADR-0082 already handles, so it is not a note the user named.
# A link to a PDF or an image names an attachment, never a note. Listed, not
# "anything after a dot": titles carry dots (`Industria 4.0`, `Python 3.11`),
# and those are notes.
_NOT_A_NOTE = re.compile(rf"\.{ext_alternation(ATTACHMENT_EXTS)}$", re.I)

# A pasted passage full of wikilinks would otherwise list every one of them.
CAP = 10
_AMBIGUOUS_SHOWN = 5

OPEN, CLOSE = "<named-notes>", "</named-notes>"
# The block is the model's, not the user's words: a reopened chat shows the
# turn without it, as it shows a turn without its `[image attached: …]` lines.
# jobs.settled's `converted-files` block is the same kind and goes with it, so
# every reader of the user's words (the replay, rework's request) drops both,
# and Discover's story block (ADR-0137 point 8) with them.
STRIP = re.compile(r"\n*<(named-notes|converted-files|discover-story)>.*?</\1>", re.S)

# An image a turn carries, as its message holds it: the saved path, which the
# transcript and every later turn keep where the base64 would be re-sent. Here, with
# the other things that are in a message and are not the person's words, because a
# tool reads it back too and the web server is no import for a tool.
IMAGE_MARK = "[image attached: "
IMAGE_MARK_RE = re.compile(r"\n*\[image attached: ([^\]\n]+)\]")


def image_mark(path: object) -> str:
    return f"\n\n{IMAGE_MARK}{path}]"


def refs(text: str) -> list[str]:
    """The note targets `text` links to, in order, each once."""
    out: list[str] = []
    for m in MENTION_RE.finditer(text):
        target = m.group(1).strip()
        if target and not _NOT_A_NOTE.search(target) and target not in out:
            out.append(target)
    return out


def resolve(ref: str, titles: dict[str, str]) -> list[str]:
    """Every vault path `ref` can mean: one is a hit, more is ambiguous.

    The order Obsidian reads a link in: the path itself, then a partial path
    as a suffix, then a bare name against every note's file name and title.
    Case-insensitive, as Obsidian is on every platform.
    """
    low = ref.replace("\\", "/").strip().lower().removesuffix(".md")
    keys = {p: p.lower().removesuffix(".md") for p in titles}
    exact = [p for p, k in keys.items() if k == low]
    if exact:
        return sorted(exact)
    if "/" in low:
        return sorted(p for p, k in keys.items() if k.endswith("/" + low))
    return sorted(p for p, k in keys.items()
                  if k.rsplit("/", 1)[-1] == low or (titles[p] or "").lower() == low)


def _vault_titles() -> dict[str, str]:
    from silica.driver import get_driver

    notes, _unresolved, _graph = get_driver().graph_data()
    return {p.replace("\\", "/"): ref.name for p, ref in notes.items()}


def named_notes(text: str, titles: dict[str, str] | None = None, open_note: str = "") -> str:
    """The block for the notes `text` names, or "" when it names none.

    `titles` maps every vault path to its title; the live vault's when None.
    Reads the driver's graph, so a caller on an event loop runs it off-loop:
    with the ws backend a driver call on the loop thread deadlocks.

    `open_note` is the vault path of the note open beside the chat, which the
    GUI's composer chip sends: "what does this article say?" names it without
    a link, so it rides the same block on a line of its own. A path, never a
    title to resolve: the drawer holds the canonical one. A path the vault no
    longer has (renamed under the open drawer) is left out.
    """
    wanted = refs(text)
    if not wanted and not open_note:
        return ""
    if titles is None:
        try:
            titles = _vault_titles()
        except Exception:
            # The block is an aid. With no readable vault the turn still goes,
            # carrying the links as the user wrote them, which is what it
            # carried before this existed.
            logger.warning("named notes not resolved: the vault could not be read", exc_info=True)
            return ""
    opened = open_note.replace("\\", "/") if open_note.replace("\\", "/") in titles else ""
    found: list[str] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    for ref in wanted[:CAP]:
        paths = resolve(ref, titles)
        if len(paths) == 1:
            if paths[0] not in found and paths[0] != opened:
                found.append(paths[0])
        elif paths:
            shown = ", ".join(paths[:_AMBIGUOUS_SHOWN]) + (", …" if len(paths) > _AMBIGUOUS_SHOWN else "")
            ambiguous.append(f"[[{ref}]] ({shown})")
        else:
            missing.append(f"[[{ref}]]")
    if not (wanted or opened):
        return ""
    lines = [OPEN]
    if opened:
        lines.append("The note open beside the chat, what \"this note\", \"this article\" or \"here\" means "
                     "(read it before answering about its content): " + opened)
    if found:
        lines.append("Notes the user named (read one before answering about its content): "
                     + " · ".join(found))
    if missing:
        lines.append("Not found in the vault: " + ", ".join(missing) + ".")
    if ambiguous:
        lines.append("Ambiguous, more than one note has that name: " + "; ".join(ambiguous) + ".")
    if len(wanted) > CAP:
        lines.append(f"And {len(wanted) - CAP} more links not resolved.")
    lines.append(CLOSE)
    return "\n".join(lines)


def strip(content: str) -> str:
    """`content` without the block, as the user typed it."""
    return STRIP.sub("", content)
