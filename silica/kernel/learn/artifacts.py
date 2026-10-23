# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The study artifacts drawn from Learn's checked key points, and whether
they stand on the notes they cite.

Each card and each slide cites its note, so the check is mechanical: the words
of a card's answer, or of a slide's bullet, must be in the note it cites (the
cite-or-drop gate of the research report notebooklm-baseline-and-study-
artifacts, recommendation 1). Cards are made here as data, so a card that
fails is dropped before the deck is shown. A deck streams from the agent, so a
slide point that fails is named under the answer, the way the numbers check
names a number the turn did not read: the flagging form of the same gate.
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
    """The turn's grounding note for /slides, or none: no line ever says a
    deck is grounded, because the rule cannot know that."""
    cmd = (turn_text or "").strip().split(None, 1)[0].lower() if (turn_text or "").strip() else ""
    if cmd != "/slides":
        return []
    try:
        bad, total = ungrounded_bullets(answer)
    except Exception:
        # A check that cannot read a note costs the line, never the answer.
        return []
    return [{"kind": "grounding", "text": f"slide points not found in their note: {bad} of {total}"}] if bad else []


# --------------------------------------------------------------- the material

# The points a deck or a set of cards is drawn from: Learn's key points where
# the notes carry them, the claims already checked against their sources, so
# the model writes from the points rather than from what it remembers of the
# topic (owner's comparison with NotebookLM's Studio, 2026-10-23).
STUDY_POINTS_MAX = 40   # points handed over: about 40 lines, a deck's or 12 cards' worth twice over
# and at most this much text: a note with no footnote gives its paragraphs as
# points, some of a thousand characters, and 40 of those would slow the local
# 9B for no extra card (about 3k tokens)
STUDY_CHARS = 12000


def study_points(targets: list[str], limit: int = STUDY_POINTS_MAX) -> tuple[list[str], list[str]]:
    """(the target notes as titles, `[[Note]]: point` lines) for /slides and
    /flashcards: a folder is its notes, a note is itself, and a note without
    footnoted claims gives no point and is read whole."""
    from pathlib import Path

    from silica.kernel.learn import items, plans
    from silica.tools.atomic import notes_under

    paths: list[str] = []
    for t in targets:
        hit = plans.resolve_title(t)
        if hit:
            paths.append(hit)
        else:
            paths += [p for p in notes_under(t.rstrip("/")) if p.endswith(".md")]
    titles = list(dict.fromkeys(Path(p).stem for p in paths))
    lines: list[str] = []
    size = 0
    for p in dict.fromkeys(paths):
        try:
            for kp in items.key_points(p):
                lines.append(f"- [[{Path(p).stem}]]: {kp['text']}")
                size += len(lines[-1])
                if len(lines) >= limit or size >= STUDY_CHARS:
                    return titles, lines
        except Exception:
            # A note whose claims do not parse is read whole instead.
            continue
    return titles, lines


# ------------------------------------------------------------------ the cards

# Made as data, not asked of the agent: asked for `question :: answer [[note]]`
# in its chat turn, the local 9B wrote "statement: [[note]]" bullets with no
# answer for 3 cards in 8 and no separator on any (2026-10-23), so neither the
# Spaced Repetition plugin nor this check could read the deck. One call, the
# cards parsed and checked here, the format written by code.
CARD_MAX_TOKENS = 1600   # 12 cards of about 60 tokens with their formulas, with room to spare
NOTE_CHARS = 8000        # note text handed over when no note has a key point, all notes together (a lesson note's first screens)
CARD_PROMPT = """Write {n} flashcards from the notes {titles}.
{ground}

Output only the cards, one per line, with nothing before or after them. Each line is:
question? :: answer [[note title]]
The question ends with a question mark and asks for one point; the answer is short (a term, a formula, a number, a phrase) in the notes' own words; the note title is the note the answer comes from, written exactly as one of: {names}. No yes/no question, and no card the notes do not support. Mix definitions, conditions, formulas and contrasts. Formulas in $...$ as the notes write them. Write in {language}.
"""
CARD_RETRY = ("Your reply had no line of the form `question? :: answer [[note title]]`. "
              "Write the cards again, in that form only.")
PRACTICE_LINE = "Graded practice that counts toward your plan: /quiz or a Learn session."
CARD_TITLES_SHOWN = 3    # notes named in the deck's title; a folder's thirty would fill the screen


def _card_reply(prompt: str, retry: bool = False) -> str:
    from silica.agent.llm import call_llm
    from silica.config import CONFIG

    msgs = [{"role": "user", "content": prompt}]
    if retry:
        msgs += [{"role": "user", "content": CARD_RETRY}]
    resp = call_llm(CONFIG.model, msgs, max_tokens=CARD_MAX_TOKENS, temperature=0.2, reasoning=False)
    return resp.text or ""


def make_cards(targets: list[str], n: int = 12) -> dict:
    """{"titles", "cards": [(question, answer, note)], "dropped", "written"}:
    the cards the model wrote whose answer the note they cite holds, at most n."""
    from silica.kernel.learn import items, plans
    from silica.kernel.text import language

    titles, points = study_points(targets)
    if not titles:
        return {"titles": [], "cards": [], "dropped": 0, "written": 0}
    if points:
        ground = "Draw every card from one of these points, each already checked against its source:\n" + "\n".join(points)
        sample = " ".join(points)
        # the notes the points come from: a folder's other titles would only lengthen the prompt
        used = list(dict.fromkeys(re.findall(r"^- \[\[([^\]]+)\]\]", "\n".join(points), re.M)))
    else:
        texts, left = [], NOTE_CHARS
        for t in titles:
            p = plans.resolve_title(t)
            if p and left > 0:
                body = items.read_note(p)[:left]
                texts.append((t, body))
                left -= len(body)
        ground = "The notes:\n" + "\n\n".join(f"### {t}\n{body}" for t, body in texts)
        sample = " ".join(body for _t, body in texts)
        used = [t for t, _b in texts]
    prompt = CARD_PROMPT.format(n=n, titles=", ".join(f"[[{t}]]" for t in used), ground=ground,
                                names="; ".join(used), language=language.display_name(language.detect(sample[:4000])))
    found = list(_CARD_RE.finditer(_card_reply(prompt)))
    if not found:
        found = list(_CARD_RE.finditer(_card_reply(prompt, retry=True)))
    cards, seen, dropped = [], set(), 0
    for m in found:
        q, a, note = m.group("q").strip(), m.group("a").strip(), m.group("note").strip()
        if q.casefold() in seen:
            continue
        seen.add(q.casefold())
        text = _note_text(note)
        if text is None or not _grounded(a, text):
            dropped += 1
            continue
        cards.append((q, a, note))
    return {"titles": titles, "cards": cards[:n], "dropped": dropped, "written": len(seen)}


def flashcards(targets: list[str], n: int = 12, save: str = "") -> str:
    """The /flashcards answer as Markdown: the cards in the Spaced Repetition
    plugin's `question :: answer` form, the cards dropped counted, and with
    `save` the note written through the same tool the agent writes with
    (checkpointed, undone by /undo)."""
    from pathlib import Path

    made = make_cards(targets, n)
    if not made["titles"]:
        return f"No note or folder found for {', '.join(targets)}."
    shown = made["titles"][:CARD_TITLES_SHOWN]
    rest = len(made["titles"]) - len(shown)
    head = "## Flashcards: " + ", ".join(f"[[{t}]]" for t in shown) + (f" and {rest} more" if rest else "")
    if not made["cards"]:
        return (f"{head}\n\nNo card held: the model wrote {made['written']}, and none had an answer its note "
                f"holds. {PRACTICE_LINE}")
    out = [head, "", *(f"- {q} :: {a} [[{note}]]" for q, a, note in made["cards"])]
    if made["dropped"]:
        out += ["", f"_{made['dropped']} of {made['written']} cards dropped: the note each cites does not hold its answer._"]
    if save:
        from silica.tools.notes import silica_write_note

        path = save if save.endswith(".md") else save + ".md"
        body = "#flashcards\n\n" + "\n\n".join(f"{q} :: {a} [[{note}]]" for q, a, note in made["cards"]) + "\n"
        res = silica_write_note(path=path, body=body, title=Path(path).stem)
        err = res.get("error") if isinstance(res, dict) else None
        out += ["", f"Not saved: {err}" if err else f"Saved as [[{Path(path).stem}]], the format the Spaced Repetition plugin reads."]
    return "\n".join([*out, "", PRACTICE_LINE])
