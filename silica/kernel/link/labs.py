# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A lab is filed whole, and linked to the notes it practises.

A course's notebook is filed intact (`form: draft`), and filing wrote no link:
the exercises sat beside the theory with nothing tying `SVC(kernel="rbf")` to
the SVM note, which is the one thing a lab adds that a deck of slides cannot.
One worker call per lab picks, from the titles of the notes under the folder
it is filed into, the ones it puts to work. A title the reply invents is
dropped: the section only ever links notes that exist.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

HEADING = "## Concepts practised"   # UI copy: English whatever the vault's language
# Measured on the optdigits lab (2026-10-23): of 8 links, 3 were notes it only
# brushed ("Classificatore binario" for a ten-class problem). Six, and the
# prompt asks for fewer.
MAX_LINKS = 6
VOTES = 3   # replies per lab; a note is linked when most of them pick it (practised_section)
# Titles in one prompt (about 10 tokens each). A folder past it (a whole
# textbook nucleated beside the course) is ranked by the words its titles share
# with the lab first. It was 300, and a course folder holds about 450 notes once
# its lectures land: an English lab shares no word with Italian titles, the
# ties kept alphabetical order, and the cut fell in the middle of the alphabet,
# Support Vector Machine and Rete convolutiva behind it (2026-10-23).
MAX_CANDIDATES = 900
# The lab's prose and code, its printed outputs dropped: what it does with a
# model, not the 50 epochs of loss it printed while doing it.
LAB_CHARS = 6000

_OUTPUT_RE = re.compile(r"```text\n.*?```\n?", re.S)
_EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")
_WORD_RE = re.compile(r"[^\W\d_]{4,}")

_PROMPT = """You link a lab exercise (a notebook of code and prose) to the notes it practises.
Reply with at most {n} lines, each exactly `Title | why`: Title copied character for character
from the list below, why a few words in the lab's own language on what the lab does with it.
Pick a note only when the lab's code or text uses, implements, trains or measures it: a model
it fits, a metric it reports, a technique it applies. Fewer is better: leave out a note the lab
only touches in passing or that you have to argue for. Reply NONE when nothing fits.

Notes:
{titles}"""


def _lab_text(body: str) -> str:
    return _EMBED_RE.sub("", _OUTPUT_RE.sub("", body)).strip()[:LAB_CHARS]


def _ranked(titles: list[str], text: str) -> list[str]:
    if len(titles) <= MAX_CANDIDATES:
        return titles
    words = {w.casefold() for w in _WORD_RE.findall(text)}
    score = {t: len({w.casefold() for w in _WORD_RE.findall(t)} & words) for t in titles}
    return sorted(titles, key=lambda t: -score[t])[:MAX_CANDIDATES]


def _ask_worker(system: str, user: str) -> str:
    from silica.agent.providers import get_provider
    from silica.config import CONFIG

    provider = get_provider(CONFIG, role="worker")
    reply = provider.call_llm(messages=[{"role": "system", "content": system},
                                        {"role": "user", "content": user}],
                              tools=None, max_tokens=800, temperature=0.0, reasoning=False)
    return reply.text or ""


def practised_section(body: str, titles: list[str], *,
                      ask: Callable[[str, str], str] | None = None) -> str:
    """The `## Concepts practised` block for a lab, "" when no model answers
    or nothing in `titles` fits. `titles` are note names (file stems).

    VOTES replies, and a note is linked when most of them pick it: one reply
    per lab was a draw at temperature 0. Four calls on one Keras lab named
    four different sets, and a re-run on another replaced five sound links
    with one wrong one (2026-10-23)."""
    pool = _ranked(sorted(set(titles)), body)
    if not pool:
        return ""
    by_key = {t.casefold(): t for t in pool}
    system, user = _PROMPT.format(n=MAX_LINKS, titles="\n".join(pool)), _lab_text(body)
    with ThreadPoolExecutor(max_workers=VOTES) as pool_ex:
        replies = list(pool_ex.map(lambda _i: _reply(ask or _ask_worker, system, user), range(VOTES)))
    answered = [r for r in replies if r is not None]
    if not answered:
        return ""
    picks: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for reply in answered:
        for hit, why in _picks(reply, by_key):
            picks[hit] = picks.get(hit, 0) + 1
            reasons.setdefault(hit, why)
    # first-seen order among equal votes: the order of the first reply
    kept = sorted((h for h, n in picks.items() if n * 2 > len(answered)), key=lambda h: -picks[h])[:MAX_LINKS]
    lines = [f"- [[{h}]]: {reasons[h]}" if reasons[h] else f"- [[{h}]]" for h in kept]
    return f"\n{HEADING}\n" + "\n".join(lines) + "\n" if lines else ""


def _reply(ask: Callable[[str, str], str], system: str, user: str) -> str | None:
    try:
        return ask(system, user)
    except Exception as e:
        # Linking is the bonus on top of a filed lab: no model, no section, and
        # the lab still lands whole.
        logger.info("lab links skipped: %s", e)
        return None


def _picks(reply: str, by_key: dict[str, str]) -> list[tuple[str, str]]:
    """(note, why) in reply order, each note once, only notes that exist."""
    out: list[tuple[str, str]] = []
    for raw in reply.splitlines():
        title, _, why = raw.strip().lstrip("-* ").partition("|")
        hit = by_key.get(title.strip().strip("[]`\"").casefold())
        why = why.strip()
        # "Title | None" is a pick with no reason: one reply gave six of them,
        # "Reti Neurali Ricorrenti per l'Inferenza" for a feed-forward lab
        # among them, while two more calls on the same lab gave reasons (2026-10-23).
        if hit is None or any(h == hit for h, _ in out) or why.casefold() in ("none", "null", "n/a"):
            continue
        out.append((hit, why))
    return out[:MAX_LINKS]


def folder_titles(folder: str, exclude: str = "") -> list[str]:
    """Note names under `folder` a lab may link: not source leaves, not itself,
    and not what the outline stage hides, a lesson spine or a merge loser. A
    lab practises concepts, and those two kinds were 172 of the 961 notes of a
    course folder with its textbook, enough to push it past MAX_CANDIDATES
    (2026-10-23)."""
    from silica.kernel.outline import _hidden_note
    from silica.kernel.recall.paths import is_source_leaf
    from silica.tools.atomic import notes_under

    return [Path(p).stem for p in notes_under(folder)
            if p != exclude and not is_source_leaf(p) and not _hidden_note(p)]
