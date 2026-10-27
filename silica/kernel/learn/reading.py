# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A plan step read as a card per note (ADR-0132, as amended by ADR-0134).

A reading is one note per step: a card per note of the step, each after the
notes it needs, the lecture order breaking ties. A card summarises its note
and ends on a question that uses its concept to anticipate the next card's
note, answered by a sentence copied from that note. A lecture's outline (a
spine) is a card too, at its lecture place, with no call and no question.
What the reader answers stays out of the vault and out of the answer ledger,
in `readings.jsonl` beside `quiz.jsonl`.

The order and the cards are a model's; the checks are code's: only the step's
notes count, each once; a cycle keeps the lecture order; a link stays only
when it names a note of the step; every sentence of a card's summary is held
against the notes its call saw with the flashcards' gate
(`artifacts._grounded`, formulas included), and one they do not hold is
marked, never dropped; an answer the next note does not hold is not shown.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# What the ordering call sees of a note: its opening, enough to tell what a
# note is about and what it leans on, and 37 notes stay under 12k characters.
LEDE_CHARS = 280
ORDER_MAX_TOKENS = 2000
# A card's note handed over whole up to this much (a lesson's few screens).
SECTION_CHARS = 9000
# The next card's note, read only to ask the closing question: its first
# screens hold its definition, which is what the question anticipates.
NEXT_CHARS = 3000
SECTION_MAX_TOKENS = 2200
# Calls of one step in flight at once: four keeps a 19-card step near a
# minute on the hosted model without queueing the provider.
PARALLEL = 4
# The practice after a reading asks one question a note up to a session's cap
# (the session route's own ceiling of 24).
SESSION_MAX = 24
SESSION_MIN = 4
# Reading speed for the minutes shown: technical prose with formulas.
WORDS_PER_MINUTE = 150
NOT_HELD = "*(not in the notes)*"
FOLDER_SUFFIX = " - readings"
LOG_NAME = "readings.jsonl"
# `seen`: the card was opened (standby until answered); `answer`: the card's
# closing question answered, or "I don't know".
KINDS = ("seen", "answer")

# The needs asked by number, alone: asked to group and order at once, the
# local 9B answered "NEEDS: none" for every section of the owner's 37-note
# step (2026-10-26), so nothing could move; the vault's own prerequisite graph
# held 1 edge inside all 16 steps of that plan.
NEEDS_PROMPT = """These are the notes of one study step, in the lectures' order, each with its opening:
{notes}

For each note, which other notes must a student have read to understand it, because it uses a definition, a formula or a result they explain? A note can need one that comes later in the list.

Write one line per note, and nothing else:
N: the numbers of the notes it needs, separated by commas, or none"""

CARD_PROMPT = """You write one card of a study reading for a university student. The card summarises this note:
{note}

The notes read before this one: {before}.{needs}
{next_intro}
{next}

Write in {language}, with formulas in $...$ as the note writes them. Use only what the note says, except in EXAMPLE. No history, anecdotes, curiosities or motivation. At the end of a sentence that uses a note, before its full stop, write that note's link as [[title]], with only these titles: {titles}.

Reply with these labelled parts, each label at the start of its own line, and nothing else:
OPENING: one sentence tying this note's concept to a concept an earlier note explains, when the notes give a reason, with that note linked; or none. Speak of the concepts, never of cards.
DEFINITION: what the concept is, in two or three sentences in the note's words.
MECHANISM: how it works and why. Write each formula on its own line, then say in place what it does.
EXAMPLE: one concrete case worked through, with small numbers where the topic allows, using the note's symbols.
QUESTION: {question}
ANSWER: the one sentence of {answer_from} that answers QUESTION, copied exactly."""

_NEXT_INTRO = "The next card is about this note; read it only to ask the question at the end:"
_NEXT_QUESTION = "one question that uses this card's concept to anticipate what the next note says; the next note must answer it."
# The last card has no next note: it asks on its own (ADR-0134 point 2).
_LAST_INTRO = "This is the last card of the reading."
_LAST_QUESTION = "one question on this card's own concept, answered by the note above."

_FIELDS = ("OPENING", "DEFINITION", "MECHANISM", "EXAMPLE", "QUESTION", "ANSWER")
_LABEL_RE = re.compile(r"^\s*(?:[-*]\s*)?\**\s*(" + "|".join(_FIELDS) + r")\s*\**\s*:\s*\**\s*(.*)$", re.I)
_NONE_RE = re.compile(r"^\s*(?:none|nessun[oa]?|-|n/?a)\.?\s*$", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ý$(])")
_FENCE_SAFE_RE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_WIKI_RE = re.compile(r"\[\[([^\]|#]+)(?:[^\]]*)\]\]")
_ANY_WIKI_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


# ------------------------------------------------------------------ the step

def step_notes(plan: dict, step: int) -> list[tuple[str, str]]:
    """[(title, path)] of a step's notes in the step's order, unresolved ones
    left out (a gap has no text to explain)."""
    from silica.kernel.learn import plans

    steps = plan.get("steps") or []
    if not 0 <= step < len(steps):
        raise ValueError(f"no step {step + 1} in {plan.get('path')!r}")
    out, seen = [], set()
    for title in steps[step].get("notes") or []:
        path = plans.resolve_title(title)
        if path and path not in seen:
            seen.add(path)
            out.append((Path(path).stem, path))
    return out


def notes_hash(paths: list[str]) -> str:
    """The step's notes as they are now; a reading written from other bytes is stale."""
    from silica.kernel.learn import items

    h = hashlib.sha256()
    for p in paths:
        h.update(p.encode("utf-8") + b"\0" + items.read_note(p).encode("utf-8") + b"\0")
    return h.hexdigest()[:16]


def reading_path(plan: dict, step: int) -> str:
    from silica.kernel.learn import plans

    label = plans._step_label((plan.get("steps") or [])[step])
    safe = _FENCE_SAFE_RE.sub(" ", label).strip(" .") or f"Step {step + 1}"
    plan_path = Path(plan["path"])
    # beside the plan, so outside every study folder a plan's restrict names
    return (plan_path.parent / f"{plan_path.stem}{FOLDER_SUFFIX}" / f"{step + 1:02d} {safe[:80].strip()}.md").as_posix()


def session_size(n_notes: int) -> int:
    return max(SESSION_MIN, min(SESSION_MAX, n_notes))


def is_spine(path: str) -> bool:
    """A lecture's outline (`section: (outline)`): a card at its lecture
    place, never a prerequisite, written with no call."""
    from silica.kernel.learn import items
    from silica.kernel.outline import SPINE_SECTION
    from silica.kernel.write import frontmatter

    data, _raw, _body = frontmatter.split(items.read_note(path))
    return isinstance(data, dict) and str(data.get("section") or "") == SPINE_SECTION


# ------------------------------------------------------------------ the order

def order_sections(titles: list[str], raw: list[dict]) -> dict:
    """{sections: [{title, notes, needs, needed_by}], order: "prerequisites"|"lecture"}.

    Only the step's notes count, each in the first entry that names it; a
    note no entry names stands alone. An entry depends on the entries holding
    the notes it needs; a stable topological sort keyed on each entry's
    earliest lecture position moves one earlier only when a prerequisite
    forces it. A cycle keeps the lecture order and says so."""
    pos = {t.casefold(): i for i, t in enumerate(titles)}
    canon = {t.casefold(): t for t in titles}
    used: set[str] = set()
    secs: list[dict] = []
    for r in raw:
        notes = []
        for n in r.get("notes") or []:
            k = n.casefold()
            if k in pos and k not in used:
                used.add(k)
                notes.append(canon[k])
        if notes:
            secs.append({"title": (r.get("title") or notes[0]).strip() or notes[0], "notes": notes,
                         "needs_raw": [canon[x.casefold()] for x in r.get("needs") or [] if x.casefold() in pos]})
    for t in titles:
        if t.casefold() not in used:
            secs.append({"title": t, "notes": [t], "needs_raw": []})
    for s in secs:
        s["key"] = min(pos[n.casefold()] for n in s["notes"])
    secs.sort(key=lambda s: s["key"])
    where = {n.casefold(): i for i, s in enumerate(secs) for n in s["notes"]}
    after: dict[int, set[int]] = {i: set() for i in range(len(secs))}
    indeg = [0] * len(secs)
    for i, s in enumerate(secs):
        for need in s["needs_raw"]:
            j = where[need.casefold()]
            if j != i and i not in after[j]:
                after[j].add(i)
                indeg[i] += 1
    heap = [(secs[i]["key"], i) for i in range(len(secs)) if indeg[i] == 0]
    heapq.heapify(heap)
    placed: list[int] = []
    while heap:
        _k, j = heapq.heappop(heap)
        placed.append(j)
        for i in after[j]:
            indeg[i] -= 1
            if indeg[i] == 0:
                heapq.heappush(heap, (secs[i]["key"], i))
    order = "prerequisites"
    if len(placed) < len(secs):
        placed, order = list(range(len(secs))), "lecture"
    out = []
    for j in placed:
        s = secs[j]
        # moved up: placed before an entry that came earlier in the lectures and needs it
        needed_by = [secs[i]["notes"][0] for i in after[j] if secs[i]["key"] < s["key"]] if order == "prerequisites" else []
        out.append({"title": s["title"], "notes": s["notes"],
                    "needs": [n for n in s["needs_raw"] if where[n.casefold()] != j], "needed_by": needed_by})
    return {"sections": out, "order": order}


_NEED_LINE_RE = re.compile(r"^\s*(?:(?:section|note|nota)\s*)?(\d+)\s*[:.)-]\s*(.*)$", re.I)


def parse_needs(reply: str, n: int) -> dict[int, list[int]]:
    """{entry: [entries it needs]}, 0-based, from `N: a, b` lines; a self
    reference or a number out of range is dropped."""
    out: dict[int, list[int]] = {}
    for line in (reply or "").splitlines():
        m = _NEED_LINE_RE.match(line.replace("*", ""))
        if not m:
            continue
        i = int(m.group(1)) - 1
        if not 0 <= i < n or _NONE_RE.match(m.group(2)):
            continue
        got = [int(x) - 1 for x in re.findall(r"\d+", m.group(2))]
        out[i] = [j for j in dict.fromkeys(got) if 0 <= j < n and j != i]
    return out


def order_cards(notes: list[tuple[str, str]], ask=None) -> dict:
    """{cards: [{title, path, spine, needs, needed_by}], order}: the step's
    notes after the ones each needs, from one call that names the needs by
    number (none without `ask`: the lecture order). A spine keeps its lecture
    place and is neither asked about nor needed."""
    spines = {t for t, p in notes if is_spine(p)}
    body = [(t, p) for t, p in notes if t not in spines]
    titles = [t for t, _p in body]
    needs: dict[int, list[int]] = {}
    asked = False
    if ask is not None and len(body) > 1:
        listing = "\n".join(f"{i}. {t}: {_lede(p)}" for i, (t, p) in enumerate(body, 1))
        try:
            needs = parse_needs(ask(NEEDS_PROMPT.format(notes=listing), ORDER_MAX_TOKENS), len(body))
            asked = True
        except Exception:
            # The call only adds moves; without it the lecture order stands.
            needs = {}
    raw = [{"title": t, "notes": [t], "needs": [titles[j] for j in needs.get(i, [])]} for i, t in enumerate(titles)]
    ordered = order_sections(titles, raw)
    path_of = dict(notes)
    cards = [{"title": s["title"], "path": path_of[s["title"]], "spine": False, "needs": s["needs"],
              "needed_by": s["needed_by"]} for s in ordered["sections"]]
    for k, (t, p) in enumerate(notes):
        if t in spines:
            cards.insert(min(k, len(cards)), {"title": t, "path": p, "spine": True, "needs": [], "needed_by": []})
    return {"cards": cards, "order": ordered["order"] if asked else "lecture"}


# ------------------------------------------------------------------ a card

def parse_fields(reply: str) -> dict[str, str]:
    """{DEFINITION: ..., ...}: each label's text up to the next label."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in (reply or "").splitlines():
        m = _LABEL_RE.match(line)
        if m and m.group(1).upper() in _FIELDS:
            cur = m.group(1).upper()
            out[cur] = [m.group(2).rstrip()]
        elif cur:
            out[cur].append(line.rstrip())
    return {k: "\n".join(v).strip() for k, v in out.items()}


_DISPLAY_RE = re.compile(r"\$\$\s*\n(.*?)\n\s*\$\$", re.S)


def collapse_display(text: str) -> str:
    """A `$$` block written over three lines onto one: the check reads a line
    at a time, and a formula line without its fences was judged by its LaTeX
    words and marked though the note holds it verbatim (the gradient update of
    "Gradiente discendente", 2026-10-26); Learn draws `$$...$$` only on one line."""
    return _DISPLAY_RE.sub(lambda m: "$$" + " ".join(x.strip() for x in m.group(1).splitlines()) + "$$", text or "")


def _formula_held(formula: str, note_keys: list[str], held_all: str) -> bool:
    """A formula the notes hold: verbatim once LaTeX spelling is normalized,
    or a chain's ends (`A = B` where a note's formula reads `A = ... = B`):
    the model wrote the partial derivative of a weighted sum as `= alpha_j`,
    the note's last link, and the verbatim check marked it (2026-10-26)."""
    from silica.kernel.learn import artifacts

    key = artifacts._latex_key(formula)
    if key in held_all:
        return True
    parts = [p for p in key.split("=") if p]
    if len(parts) < 2:
        return False
    for nk in note_keys:
        at = 0
        for p in parts:
            at = nk.find(p, at)
            if at < 0:
                break
            at += len(p)
        else:
            return True
    return False


def _held(sentence: str, notes_text: str, note_keys: list[str], held_all: str, held_words: set[str]) -> bool:
    """The flashcards' gate (artifacts._grounded) with a chain's ends allowed."""
    from silica.kernel.learn import artifacts

    formulas = artifacts._MATH_RE.findall(sentence)
    if any(not _formula_held(f, note_keys, held_all) for f in formulas):
        return False
    words = artifacts._words(sentence)
    if not words or (formulas and len(words) <= 2):
        return True
    return sum(1 for w in words if w in held_words) >= artifacts.GROUNDED_SHARE * len(words)


def mark_unheld(text: str, notes_text: str) -> tuple[str, int]:
    """The text with each sentence the notes do not hold marked, line breaks
    kept; and how many were marked. A line of formula alone is judged by its
    formula; a sentence of fewer than three content words is not judged. A
    link is judged by its title's words (artifacts._words reads `[[x]]` as
    x), so the caller adds the titles it allows to `notes_text`."""
    from silica.kernel.learn import artifacts

    note_keys = [artifacts._latex_key(f) for f in artifacts._MATH_RE.findall(notes_text)]
    held_all = artifacts._latex_key(notes_text)
    held_words = artifacts._words(notes_text)
    marked = 0
    lines = []
    for line in text.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        parts = []
        for s in _SENTENCE_RE.split(line):
            judged = artifacts._MATH_RE.search(s) or len(artifacts._words(s)) >= 3
            if judged and not _held(s, notes_text, note_keys, held_all, held_words):
                parts.append(f"{s.rstrip()} {NOT_HELD}")
                marked += 1
            else:
                parts.append(s)
        lines.append(" ".join(parts))
    return "\n".join(lines), marked


def keep_step_links(text: str, titles: list[str]) -> str:
    """A link stays only when it names a note of the step, written by its
    title; any other becomes its text, so a card never opens a note the
    reading did not draw from."""
    canon = {t.casefold(): t for t in titles}

    def one(m: re.Match) -> str:
        target, shown = m.group(1).strip(), (m.group(2) or "").strip()
        hit = canon.get(target.casefold())
        return f"[[{hit}]]" if hit else (shown or target)

    return _ANY_WIKI_RE.sub(one, text or "")


def _answer_key(text: str) -> str:
    """A sentence as the answer check compares it: links read as their text,
    emphasis, quotes, spaces and LaTeX spelling aside."""
    from silica.kernel.learn import artifacts

    text = _ANY_WIKI_RE.sub(lambda m: (m.group(2) or m.group(1)), text or "")
    text = re.sub(r"\*\*|\*|==|[«»“”\"']", "", text)
    return artifacts._latex_key(text).casefold().rstrip(".;:")


def answer_held(answer: str, note_text: str) -> bool:
    """The answer is a sentence the note holds, spaces and LaTeX spelling
    aside; a reply that only paraphrases it is not shown as the note's words."""
    key = _answer_key(answer)
    return len(key) >= 8 and key in _answer_key(note_text)


def _body(path: str, limit: int) -> str:
    from silica.kernel.learn import items
    from silica.kernel.write import frontmatter

    _d, _r, body = frontmatter.split(items.read_note(path))
    return body.strip()[:limit]


def _lede(path: str) -> str:
    # `[^\n]*` and not `.*` for a heading: under re.S, `.*$` ran from the
    # note's title to its end, so ADR-0132's ordering call saw every lede empty
    text = re.sub(r"<!--.*?-->|!\[\[[^\]]*\]\]|^#+\s[^\n]*$|\[\^[^\]]+\]", " ", _body(path, 20000), flags=re.M | re.S)
    text = _WIKI_RE.sub(lambda m: m.group(1), text)
    return re.sub(r"\s+", " ", text).strip()[:LEDE_CHARS]


def _ask(prompt: str, max_tokens: int) -> str:
    from silica.agent.llm import call_llm
    from silica.config import CONFIG

    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}], max_tokens=max_tokens,
                    temperature=0.2, reasoning=False)
    return resp.text or ""


def _language(paths: list[str]) -> str:
    from silica.kernel.learn import items
    from silica.kernel.text import language

    sample = " ".join(items.read_note(p)[:1500] for p in paths[:6])
    return language.display_name(language.detect(sample[:4000]))


def write_card(i: int, cards: list[dict], language: str, ask=None) -> dict:
    """Card i's parts from one call, checked: {title, note, needs, needed_by,
    text, example, question, answer, answer_from, marked}. The call sees the
    card's note and the next card's (the next that is not a spine), and asks
    toward it; the last asks on its own note."""
    ask = ask or _ask
    card = cards[i]
    nxt = next((c for c in cards[i + 1:] if not c["spine"]), None)
    titles = [c["title"] for c in cards]
    note = _body(card["path"], SECTION_CHARS)
    nxt_text = _body(nxt["path"], NEXT_CHARS) if nxt else ""
    before = [c["title"] for c in cards[:i] if not c["spine"]]
    needs = f' It needs: {", ".join(card["needs"])}.' if card.get("needs") else ""
    prompt = CARD_PROMPT.format(
        note=f"### {card['title']}\n{note}", before="; ".join(before) or "none", needs=needs,
        next_intro=_NEXT_INTRO if nxt else _LAST_INTRO, next=f"### {nxt['title']}\n{nxt_text}" if nxt else "",
        language=language, titles="; ".join(titles), question=_NEXT_QUESTION if nxt else _LAST_QUESTION,
        answer_from="the next note" if nxt else "the note above")
    f = {k: collapse_display(keep_step_links(v, titles)) for k, v in parse_fields(ask(prompt, SECTION_MAX_TOKENS)).items()}
    held = "\n".join([note, nxt_text] + titles)
    opening = "" if _NONE_RE.match(f.get("OPENING", "") or "none") else f.get("OPENING", "")
    parts = [p for p in (opening, f.get("DEFINITION", ""), f.get("MECHANISM", "")) if p.strip()]
    text, marked = mark_unheld("\n\n".join(parts), held)
    answer_from = nxt["title"] if nxt else card["title"]
    answer = f.get("ANSWER", "").strip()
    if not answer_held(answer, nxt_text if nxt else note):
        answer = ""
    return {"title": card["title"], "note": card["title"], "spine": False, "needs": card.get("needs") or [],
            "needed_by": card.get("needed_by") or [], "text": text, "example": f.get("EXAMPLE", "").strip(),
            "question": f.get("QUESTION", "").strip(), "answer": answer, "answer_from": answer_from,
            "marked": marked}


_POINT_RE = re.compile(r"^\s*\d+[.)]\s+\[\[([^\]|#]+)")


def spine_card(card: dict) -> dict:
    """A lecture's outline as its card: its lede and its points as links,
    from the note's own lines, with no call."""
    body = _body(card["path"], 40000)
    lede = next((ln.lstrip("> ").strip() for ln in body.splitlines() if ln.startswith(">")), "")
    points = list(dict.fromkeys(m.group(1).strip() for ln in body.splitlines() if (m := _POINT_RE.match(ln))))
    return {"title": card["title"], "note": card["title"], "spine": True, "needs": [], "needed_by": [],
            "text": lede, "example": "", "question": "", "answer": "", "answer_from": "", "points": points,
            "marked": 0}


# ------------------------------------------------------------------ a card the reader writes

# More than this share of a written card's sentences found word for word in
# its note is a copy, not a rewrite (ADR-0134 point 4).
COPY_SHARE = 0.5
# A sentence this short ("Sia x reale.") is not counted either way.
COPY_MIN_WORDS = 4


def _plain(text: str) -> str:
    text = _ANY_WIKI_RE.sub(lambda m: (m.group(2) or m.group(1)), text or "")
    text = re.sub(r"\*\*|\*|==|`|^\s*>\s?|^\s*[-*+]\s+|^\s*\d+[.)]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip().casefold()


def copy_share(text: str, note_text: str) -> tuple[int, int]:
    """(sentences of the card found word for word in the note, sentences
    counted): links read as their text, emphasis and spacing aside."""
    held = _plain(note_text)
    said = [x for line in (text or "").splitlines() for x in _SENTENCE_RE.split(line)]
    said = [_plain(x).rstrip(".;:!? ") for x in said]
    said = [x for x in said if len(x.split()) >= COPY_MIN_WORDS]
    return sum(1 for x in said if x in held), len(said)


def is_copy(text: str, note_text: str) -> bool:
    found, of = copy_share(text, note_text)
    return of > 0 and found > COPY_SHARE * of


def note_lede(path_or_text: str, *, is_text: bool = False) -> str:
    """A note's central claim: its first blockquote line, the one-line lede
    a nucleated note opens on; else its first sentence."""
    from silica.kernel.write import frontmatter

    body = frontmatter.split(path_or_text)[2] if is_text else _body(path_or_text, 20000)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    for ln in body.splitlines():
        if ln.startswith(">") and ln.lstrip("> ").strip() and not ln.lstrip("> ").startswith("[!"):
            return ln.lstrip("> ").strip()
    prose = " ".join(ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "$$", "|")))
    return (_SENTENCE_RE.split(prose.strip()) or [""])[0]


def holds_lede(text: str, lede: str) -> bool:
    """The card says the note's central claim, by the flashcards' gate
    (`artifacts._grounded`): the lede's content words and formulas are in
    the card. A lede the gate cannot read (no content words) is held."""
    from silica.kernel.learn import artifacts

    return not artifacts._words(lede) or artifacts._grounded(lede, text or "")


# ------------------------------------------------------------------ the note

def _quote(text: str) -> str:
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.strip().splitlines())


def render(plan_stem: str, step: int, cards: list[dict], order: str) -> str:
    """The reading's body. Labels are English, as every string Silica writes
    into a vault; the parts are in the notes' language."""
    how = "each after the notes it needs" if order == "prerequisites" else "in the lectures' order"
    lines = [f"{len(cards)} cards, one a note of step {step + 1} of [[{plan_stem}]], {how}.", ""]
    for i, c in enumerate(cards, 1):
        lines.append(f"## {i}. {c['title']}")
        lines.append("")
        if c.get("spine"):
            lines += [f"Outline: [[{c['note']}]]", ""]
            if c.get("text"):
                lines += [c["text"], ""]
            lines += [f"{k}. [[{p}]]" for k, p in enumerate(c.get("points") or [], 1)]
            lines.append("")
            continue
        lines.append(f"Note: [[{c['note']}]]")
        if c.get("needed_by"):
            lines.append("Moved up: needed by " + ", ".join(f"[[{n}]]" for n in c["needed_by"]) + ".")
        lines.append("")
        if c.get("text"):
            lines += [c["text"], ""]
        if c.get("example"):
            lines += [f"**Example** (worked by Silica, not from the notes). {c['example']}", ""]
        if c.get("question"):
            lines += ["> [!question] To finish the card", _quote(c["question"]), ""]
            if c.get("answer"):
                lines += [f"> [!quote] The note says, in [[{c['answer_from']}]]", _quote(c["answer"]), ""]
    return "\n".join(lines).rstrip() + "\n"


_SEC_RE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")
_EXAMPLE_RE = re.compile(r"^\*\*Example\.?\*\*(?:\s*\(worked by Silica, not from the notes\))?\.?\s*(.*)$")
# ADR-0132's parts, read back from a reading written before the cards
_OLD_PART_RE = re.compile(r"^\*\*(Definition|Mechanism|Do not confuse)\.?\*\*\s*(.*)$")
_CALLOUT_RE = re.compile(r"^>\s*\[!(question|quote|example)\]([^\n]*)$", re.I)


def _blank_card(n: int, title: str) -> dict:
    return {"n": n, "title": title, "note": "", "notes": [], "spine": False, "needed_by": [], "text": "",
            "example": "", "question": "", "answer": "", "answer_from": "", "points": []}


def parse(text: str) -> list[dict]:
    """The cards of a reading note, read back from its Markdown; a hand edit
    keeps working as long as the `## N. title` headings stay. A reading
    written before the cards (ADR-0132's sections) reads as cards of its
    sections, without a question, until it is rewritten."""
    from silica.kernel.write import frontmatter

    _d, _r, body = frontmatter.split(text or "")
    cards: list[dict] = []
    cur: dict | None = None
    part: str | None = None
    for line in body.splitlines():
        m = _SEC_RE.match(line)
        if m:
            cur = _blank_card(int(m.group(1)), m.group(2).strip())
            cards.append(cur)
            part = None
            continue
        if cur is None:
            continue
        head = re.match(r"^(Note|Notes|Outline):", line)
        if head:
            cur["notes"] = _WIKI_RE.findall(line)
            cur["note"] = cur["notes"][0] if cur["notes"] else cur["title"]
            cur["spine"] = head.group(1) == "Outline"
            part = "text"
            continue
        if line.startswith("Moved up:"):
            cur["needed_by"] = _WIKI_RE.findall(line)
            continue
        c = _CALLOUT_RE.match(line)
        if c:
            kind = c.group(1).lower()
            if kind == "quote":
                part = "answer"
                found = _WIKI_RE.findall(c.group(2))
                cur["answer_from"] = found[0] if found else ""
            # an old reading's guess before the section and its own-words
            # prompt are not a card's closing question
            else:
                part = "question" if kind == "question" and "Before you read" not in line else None
            continue
        if line.startswith(">"):
            if part in ("question", "answer"):
                cur[part] = (cur[part] + "\n" + line[1:].strip()).strip()
            continue
        if part in ("question", "answer", None):
            part = "text"
        if cur["spine"]:
            pt = _POINT_RE.match(line)
            if pt:
                cur["points"].append(pt.group(1).strip())
                continue
        ex = _EXAMPLE_RE.match(line)
        if ex:
            cur["example"] = ex.group(1).strip()
            part = "example"
            continue
        old = _OLD_PART_RE.match(line)
        if old:
            said = old.group(2).strip()
            cur["text"] = (cur["text"] + "\n\n" + (f"Do not confuse. {said}" if old.group(1) == "Do not confuse" else said)).strip()
            part = "text"
            continue
        # a blank line inside a part keeps it open until the next label: a
        # derivation's steps are written one a line
        if part == "example" and line.strip():
            cur["example"] = (cur["example"] + "\n" + line.rstrip()).strip()
        elif part == "text":
            cur["text"] += "\n" + line.rstrip()
    for card in cards:
        card["text"] = re.sub(r"\n{3,}", "\n\n", card["text"]).strip()
        card["note"] = card["note"] or card["title"]
    return cards


def minutes(cards: list[dict]) -> int:
    words = sum(len(re.findall(r"\w+", " ".join(str(c.get(k, "")) for k in ("text", "example")))) for c in cards)
    return max(1, round(words / WORDS_PER_MINUTE))


def write_reading(plan_path: str, step: int, ask=None, progress=None) -> dict:
    """Write (or rewrite) the reading of a step: {path, cards, order, marked,
    answered}. `ask(prompt, max_tokens)` is the model call (tests pass their
    own); `progress(done, total)` hears each finished card."""
    from silica.kernel.learn import plans

    plan = plans.find_plan(plan_path)
    if plan is None:
        raise ValueError(f"no plan at {plan_path!r}")
    notes = step_notes(plan, step)
    if not notes:
        raise ValueError(f"step {step + 1} has no note in the vault to explain")
    ask = ask or _ask
    language = _language([p for _t, p in notes])
    ordered = order_cards(notes, ask)
    cards = ordered["cards"]
    total = sum(1 for c in cards if not c["spine"])
    done = [0]
    lock = threading.Lock()

    def one(i):
        if cards[i]["spine"]:
            return spine_card(cards[i])
        out = write_card(i, cards, language, ask)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], total)
        return out

    if progress:
        progress(0, total)
    # A card reads notes, never another card, so the calls stay parallel.
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        written = list(pool.map(one, range(len(cards))))
    label = plans._step_label(plan["steps"][step])
    body = render(Path(plan["path"]).stem, step, written, ordered["order"])
    path = reading_path(plan, step)
    # `written` scopes the reader's answers to this version: a rewrite asks
    # other questions, and an answer to the old card 3's is not the new one's
    props = {"type": "reading", "plan": f"[[{Path(plan['path']).stem}]]", "step": str(step + 1),
             "notes_hash": notes_hash([p for _t, p in notes]), "order": ordered["order"],
             "written": str(int(time.time()))}
    _save(path, label, body, props)
    asked = [c for c in written if c["question"]]
    return {"path": path, "cards": len(written), "order": ordered["order"],
            "marked": sum(c["marked"] for c in written), "answered": sum(1 for c in asked if c["answer"]),
            "questions": len(asked)}


def _save(path: str, title: str, body: str, props: dict) -> None:
    """A new reading through the write gate; an existing one as one revertible
    overwrite (the plan note's own path, ADR-0039), frontmatter rewritten."""
    from silica.kernel.learn import plans
    from silica.kernel.write import frontmatter
    from silica.tools import TOOLS

    full = f"# {title}\n\n{body}"
    if not (plans._vault() / path).is_file():
        out = TOOLS["silica_write_note"].fn(path=path, body=full, title=title, props=props, template="none")
        if isinstance(out, dict) and out.get("error"):
            raise RuntimeError(out["error"])
        return

    def mutate(text: str) -> str:
        data, _raw, _old = frontmatter.split(text)
        keys = dict(data or {})
        keys.update(props)
        return frontmatter.dump(keys, full)

    plans._journaled(path, mutate, source="learn")


# ------------------------------------------------------------------ the reader's answers

def _log() -> Path:
    from silica.config import CONFIG
    from silica.kernel.recall import paths

    return paths.index_dir_for(CONFIG.vault_path) / LOG_NAME


def record(reading: str, card: int, kind: str, text: str, unknown: bool = False) -> dict:
    """A card opened, or its question answered; appended, never graded."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    row = {"t": time.time(), "reading": reading, "section": int(card), "kind": kind,
           "text": str(text or "")[:4000], "unknown": bool(unknown)}
    p = _log()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def answers(reading: str, since: float = 0.0) -> dict[int, dict]:
    """{card: {"seen": row, "answer": row}}, the latest of each, given after `since`."""
    p = _log()
    out: dict[int, dict] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue   # a torn last line from a crash mid-append: the rows before it stand
        if r.get("reading") == reading and r.get("kind") in KINDS and float(r.get("t") or 0) >= since:
            out.setdefault(int(r.get("section") or 0), {})[r["kind"]] = r
    return out


# ------------------------------------------------------------------ the state Learn shows

def state(plan_path: str, step: int) -> dict:
    """What the reading view needs: the step, whether a reading exists, its
    cards, whether the notes changed since, the reader's answers, the
    minutes and the practice's size."""
    from silica.kernel.learn import items, plans
    from silica.kernel.write import frontmatter

    plan = plans.find_plan(plan_path)
    if plan is None:
        raise ValueError(f"no plan at {plan_path!r}")
    notes = step_notes(plan, step)
    path = reading_path(plan, step)
    steps = plan.get("steps") or []
    out = {"plan": plan["path"], "plan_name": plan["name"], "step": step, "steps": len(steps),
           "label": plans._step_label(steps[step]), "hours": steps[step].get("hours"),
           "notes": [p for _t, p in notes], "path": path, "exists": False, "cards": [],
           "stale": False, "answers": {}, "minutes": 0, "questions": session_size(len(notes)),
           "job": job_status(plan["path"], step)}
    text = items.read_note(path) if (plans._vault() / path).is_file() else ""
    if text:
        data, _r, _b = frontmatter.split(text)
        cards = parse(text)
        since = float(str((data or {}).get("written") or "0")) if str((data or {}).get("written") or "0").isdigit() else 0.0
        out.update(exists=True, cards=cards, minutes=minutes(cards), order=str((data or {}).get("order") or ""),
                   stale=str((data or {}).get("notes_hash") or "") != notes_hash([p for _t, p in notes]),
                   answers={str(k): v for k, v in answers(path, since).items()})
    return out


def step_of_note(plan: dict, note_path: str) -> int | None:
    """The first step of a plan whose notes include this note, by title: a
    note page asks this of every plan, and resolving every title of every
    step costs a lookup each (241 on the owner's plan)."""
    stem = Path(note_path).stem.casefold()
    for i, s in enumerate(plan.get("steps") or []):
        if any(str(t).split("|")[0].strip().casefold() == stem for t in s.get("notes") or []):
            return i
    return None


# ------------------------------------------------------------------ writing in the background

_JOBS: dict[tuple[str, int], dict] = {}
_JOBS_LOCK = threading.Lock()


def job_status(plan_path: str, step: int) -> dict | None:
    with _JOBS_LOCK:
        j = _JOBS.get((plan_path, step))
        return dict(j) if j else None


def start(plan_path: str, step: int, ask=None) -> dict:
    """Write the reading in a thread; one job a step at a time."""
    key = (plan_path, step)
    with _JOBS_LOCK:
        j = _JOBS.get(key)
        if j and j["state"] == "running":
            return dict(j)
        _JOBS[key] = {"state": "running", "done": 0, "total": 0, "error": "", "started": time.time()}

    def progress(done, total):
        with _JOBS_LOCK:
            _JOBS[key].update(done=done, total=total)

    def run():
        try:
            res = write_reading(plan_path, step, ask=ask, progress=progress)
            with _JOBS_LOCK:
                _JOBS[key].update(state="done", path=res["path"], marked=res["marked"])
        except Exception as exc:
            # The job reports the failure to the page that polls it; the
            # thread has no caller to raise to.
            with _JOBS_LOCK:
                _JOBS[key].update(state="error", error=str(exc)[:300])

    threading.Thread(target=run, daemon=True, name=f"reading-{step}").start()
    return job_status(plan_path, step) or {}
