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

logger = logging.getLogger(__name__)

# `[[Target#heading|alias]]` names Target. `![[...]]` is an embed, a figure
# ADR-0082 already handles, so it is not a note the user named.
_LINK = re.compile(r"(?<!!)\[\[([^\]\[|#\n]+)(?:#[^\]\[|\n]*)?(?:\|[^\]\[\n]*)?\]\]")
# A link to a PDF or an image names an attachment, never a note. Listed, not
# "anything after a dot": titles carry dots (`Industria 4.0`, `Python 3.11`),
# and those are notes.
_NOT_A_NOTE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|bmp|avif|pdf|mp3|mp4|m4a|wav|ogg|webm|mov|"
    r"canvas|base|csv|xlsx?|docx?|pptx?|zip|epub)$", re.I)

# A pasted passage full of wikilinks would otherwise list every one of them.
CAP = 10
_AMBIGUOUS_SHOWN = 5

OPEN, CLOSE = "<named-notes>", "</named-notes>"
# The block is the model's, not the user's words: a reopened chat shows the
# turn without it, as it shows a turn without its `[image attached: …]` lines.
# jobs.settled's `converted-files` block is the same kind and goes with it, so
# every reader of the user's words (the replay, rework's request) drops both.
STRIP = re.compile(r"\n*<(named-notes|converted-files)>.*?</\1>", re.S)


def refs(text: str) -> list[str]:
    """The note targets `text` links to, in order, each once."""
    out: list[str] = []
    for m in _LINK.finditer(text):
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


def named_notes(text: str, titles: dict[str, str] | None = None) -> str:
    """The block for the notes `text` names, or "" when it names none.

    `titles` maps every vault path to its title; the live vault's when None.
    Reads the driver's graph, so a caller on an event loop runs it off-loop:
    with the ws backend a driver call on the loop thread deadlocks.
    """
    wanted = refs(text)
    if not wanted:
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
    found: list[str] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    for ref in wanted[:CAP]:
        paths = resolve(ref, titles)
        if len(paths) == 1:
            if paths[0] not in found:
                found.append(paths[0])
        elif paths:
            shown = ", ".join(paths[:_AMBIGUOUS_SHOWN]) + (", …" if len(paths) > _AMBIGUOUS_SHOWN else "")
            ambiguous.append(f"[[{ref}]] ({shown})")
        else:
            missing.append(f"[[{ref}]]")
    lines = [OPEN]
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
