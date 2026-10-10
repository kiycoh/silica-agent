# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Place the vault's own figures in a chat answer, in code (ADR-0082).

A figure the turn read sits in its note beside the sentence it illustrates.
The answer that retells that sentence gets the figure after the paragraph that
shares the most words with the figure's surroundings, the way a textbook puts
a figure beside its text (the one chat study where visuals raised learning
retrieved such figures: Taneja 2026). A prompt rule asking the 9B to copy the
embed itself was tried first and placed 1 figure in 40 turns that had one;
this placed 35 in 29 of 40 answers written without it, 31 of them on subject.

Words, not vectors: the figure's neighbourhood and the answer are both short,
both in the note's language, and the answer paraphrases the note, so shared
content words are the signal that survives; a cosine would cost an embedder
call per paragraph on the reply path for the same decision.
"""
from __future__ import annotations

import json
import re

EMBED = re.compile(r"!\[\[([^\]|\n]+?\.(?:png|jpe?g|gif|webp|bmp|svg))(?:\|[^\]\n]*)?\]\]", re.I)
_WORD = re.compile(r"[^\W\d_]{4,}")
# Four letters or more already drops most function words; these are the ones
# that are not, in the two languages the vaults are written in.
_STOP = frozenset("""
della delle dello degli nella nelle nello negli alla alle allo agli dalla dalle
questo questa questi queste quello quella quelli quelle sono come anche essere
viene vengono ogni dove quando perché molto più loro così tutto tutti senza
that this with from have which their there these those about into when what
""".split())
# Shared content words a paragraph needs before a figure is put after it. Set
# before the gate; at it, 4 of 35 placements were off subject (ADR-0082).
MIN_SHARED = 3
# At most this many figures per answer: only what the step uses (Butcher 2006).
MAX_FIGURES = 2
_CONTEXT = 600


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _target(embed: str) -> str:
    """The file an embed names. Every embed handled here is a match of EMBED
    (the keys of `_figures`), so the search cannot miss."""
    m = EMBED.search(embed)
    assert m is not None
    return m.group(1)


def _figures(results: list[str]) -> dict[str, set[str]]:
    """embed -> the words around it in what the turn read: the text before it
    back to a blank line, and what follows (the reading ADR-0077 puts under it,
    then the prose that explains it)."""
    out: dict[str, set[str]] = {}
    for text in results:
        for m in EMBED.finditer(text):
            # An embed is usually its own paragraph: the blank line right above it
            # is not where its text ends.
            before = text[max(0, m.start() - _CONTEXT):m.start()].rstrip()
            before = before[before.rfind("\n\n") + 1:] if "\n\n" in before else before
            after = text[m.end():m.end() + _CONTEXT // 2]
            out.setdefault(m.group(0), set()).update(_words(EMBED.sub(" ", before + " " + after)))
    return out


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    return [s for v in value for s in _strings(v)] if isinstance(value, list) else []


def _turn_results(messages: list[dict]) -> list[str]:
    """Tool results since the last user message: what this answer was built from.
    A JSON result (silica_recall's) is read through its strings, or its escaped
    newlines would glue an `n` onto the first word of every line."""
    out: list[str] = []
    for m in reversed(messages):
        if m.get("role") == "user":
            break
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            try:
                out += _strings(json.loads(m["content"]))
            except ValueError:
                out.append(m["content"])
    return out


def place_figures(answer: str, messages: list[dict], exists=None) -> str:
    """`answer` with each figure the turn read placed after the paragraph it
    belongs to; unchanged when nothing qualifies. Updates the stored assistant
    message too, so a reopened chat shows what the live one showed.

    `exists(file)` says whether the embed resolves to a file: a note can embed
    one that is gone (one on the 40 gate turns, 2026-10-08), and a broken image in
    the answer is worse than none."""
    if not answer:
        return answer
    figures = {e: w for e, w in _figures(_turn_results(messages)).items()
               if _target(e) not in answer}
    if not figures:
        return answer
    blocks = answer.split("\n\n")
    fence, scores = False, []
    for i, b in enumerate(blocks):
        ticks = b.count("```")
        if fence or ticks:  # code is never a figure's paragraph, open or closed here
            fence ^= ticks % 2 == 1
            continue
        if b.lstrip().startswith(("#", "|", ">")):
            continue
        words = _words(b)
        for e, ctx in figures.items():
            shared = len(words & ctx)
            if shared >= MIN_SHARED:
                scores.append((shared, i, e))
    placed: dict[int, str] = {}
    used: set[str] = set()
    for _shared, i, e in sorted(scores, key=lambda s: (-s[0], s[1])):
        if e in used or i in placed or len(placed) == MAX_FIGURES:
            continue
        # Asked here, of the few that scored, not of every embed the turn read:
        # a miss walks the vault for the basename, the way /asset resolves it
        # (2 to 5 ms a figure on the 730-note test vault, 2026-10-08).
        if exists is not None and not exists(_target(e).strip()):
            continue
        placed[i], used = e, used | {e}
    if not placed:
        return answer
    out = "\n\n".join(b + (f"\n\n{placed[i]}" if i in placed else "") for i, b in enumerate(blocks))
    if messages and messages[-1].get("role") == "assistant":
        messages[-1]["content"] = out
    return out
