# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Whether the cards and slides a turn wrote stand on the notes they cite.

/flashcards and /slides hand the model Learn's checked key points, and the
model still writes the artifact in its own words. Each card and each slide
cites its note, so the check is mechanical: the words of a card's answer, or
of a slide's bullet, must be in the note it cites. What fails is named under
the answer, never removed from it, the way the numbers check names a number
the turn did not read (the cite-or-drop gate of the research report
notebooklm-baseline-and-study-artifacts, recommendation 1, in its flagging
form: the answer has already streamed).
"""
from __future__ import annotations

import re

# The share of an item's content words its note must hold. A card's answer is
# a short phrase or a formula in the notes' words; half of its words leaves
# room for an inflection and none for an invented claim.
GROUNDED_SHARE = 0.5

# `Question :: Answer [[Note]]`, and the spelling the local 9B used as often:
# "Question: ... Answer: ... [[Note]]" (2026-10-23), in either language.
_CARD_RE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*)?(?:question|domanda)?:?(?:\*\*)?\s*(?P<q>.+?)\s*"
    r"(?:::|(?:\*\*)?\b(?:answer|risposta)\b:?(?:\*\*)?)\s*(?P<a>.+?)\s*\[\[(?P<note>[^\]|#]+)",
    re.M | re.I)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(?P<b>.+)$", re.M)
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")
_WORD_RE = re.compile(r"[^\W\d_]{4,}")
_SPACE_RE = re.compile(r"\s+")


_MATH_RE = re.compile(r"\$\$?(.+?)\$\$?", re.S)


def _words(text: str) -> set[str]:
    from silica.kernel.text.language import stopwords_for

    stop = stopwords_for("italian") | stopwords_for("english")
    text = _MATH_RE.sub(" ", _LINK_RE.sub(lambda m: m.group(1), text))
    return {w.casefold()[:6] for w in _WORD_RE.findall(text) if w.casefold() not in stop}


_LATEX_DROP_RE = re.compile(r"\\(?:left|right|big|Big|bigg|Bigg)\b|\\[,;!:]|[{}\s]")
_LATEX_STYLE_RE = re.compile(r"\\(?:boldsymbol|mathbf|mathrm|mathit|pmb|bm|operatorname)\b")
_LATEX_SAME = {"≤": "\\leq", "≥": "\\geq", "\\le ": "\\leq ", "\\ge ": "\\geq ", "−": "-", "·": "\\cdot"}


def _latex_key(text: str) -> str:
    """A formula as the check compares it: the same LaTeX written two ways
    reads the same. The note wrote `\\xi_{i} = \\max \\left(0, ...\\right)` and
    the card `\\xi_i = \\max(0, ...)` (2026-10-23)."""
    for a, b in _LATEX_SAME.items():
        text = text.replace(a, b)
    return _LATEX_DROP_RE.sub("", _LATEX_STYLE_RE.sub("", text))


def _grounded(item: str, note_text: str) -> bool:
    """The item's content words are in the note, at GROUNDED_SHARE, and each
    formula it writes is in the note, spaces aside. A formula answer led by a
    function word ("Quando $y_i f(x) > \\gamma$") is judged by its formula,
    not by "quando"."""
    held_math = _latex_key(note_text)
    formulas = _MATH_RE.findall(item)
    if any(_latex_key(f) not in held_math for f in formulas):
        return False
    words = _words(item)
    # a held formula with a word or two around it ("Quando $...$") is the formula
    if not words or (formulas and len(words) <= 2):
        return True
    held = _words(note_text)
    return sum(1 for w in words if w in held) >= GROUNDED_SHARE * len(words)


def _note_text(title: str) -> str | None:
    from silica.kernel.learn.items import read_note
    from silica.kernel.learn.plans import resolve_title

    path = resolve_title(title.strip())
    return read_note(path) if path else None


def ungrounded_cards(answer: str) -> list[int]:
    """The 1-based numbers of the cards whose answer their cited note does
    not hold; a card citing a note that does not exist counts too."""
    out = []
    for n, m in enumerate(_CARD_RE.finditer(answer), 1):
        text = _note_text(m.group("note"))
        if text is None or not _grounded(m.group("a"), text):
            out.append(n)
    return out


def ungrounded_bullets(answer: str) -> tuple[int, int]:
    """(bullets not held by their slide's cited note, bullets checked). A
    slide is the text between `---` lines and cites its note on its last link;
    a slide citing none is not checked."""
    bad = total = 0
    for slide in re.split(r"^\s*---\s*$", answer, flags=re.M):
        links = _LINK_RE.findall(slide)
        if not links:
            continue
        text = _note_text(links[-1])
        for m in _BULLET_RE.finditer(slide):
            total += 1
            if text is None or not _grounded(m.group("b"), text):
                bad += 1
    return bad, total


def grounding_notes(turn_text: str, answer: str) -> list[dict]:
    """The turn's grounding note for /flashcards and /slides, or none: no line
    ever says an artifact is grounded, because the rule cannot know that."""
    cmd = (turn_text or "").strip().split(None, 1)[0].lower() if (turn_text or "").strip() else ""
    try:
        if cmd == "/flashcards":
            bad = ungrounded_cards(answer)
            if bad:
                return [{"kind": "grounding",
                         "text": "cards whose answer is not in their note: " + " · ".join(map(str, bad))}]
        elif cmd == "/slides":
            bad, total = ungrounded_bullets(answer)
            if bad:
                return [{"kind": "grounding", "text": f"slide points not found in their note: {bad} of {total}"}]
    except Exception:
        # A check that cannot read a note costs the line, never the answer.
        return []
    return []
