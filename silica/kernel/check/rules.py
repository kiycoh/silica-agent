# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a rule can tell with no model (ADR-0110 point 5): identifiers and
dates looked up word for word, the arithmetic of a period, quotations.

Everything here is exact or arithmetic. A judgment of meaning (is this
statement what the passage says?) is the model's, in step 2, and nothing in
this module guesses at one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from silica.kernel.check.pool import Note, fold
from silica.kernel.text import dates
from silica.kernel.text.text import squash

# A citation-like token: a digit, and an uppercase letter, a slash, or a Latin
# ordinal ("2409-bis"). Lowercase units ("30-day", "6-year") and figures
# ("1.250.000", "3.5") are prose, not identifiers.
_TOKEN = re.compile(r"(?<![\w/.-])[\w/.-]*\d[\w/.-]*(?<![./-])")
_ORDINAL = re.compile(r"\d-(?:bis|ter|quater|quinquies|sexies|septies|octies|novies|decies)\b", re.I)
QUOTE_MIN_WORDS = 6
MISQUOTE_SHARE = 0.8
_QUOTED = re.compile(r"[\"“«]([^\"“”«»\n]{10,}?)[\"”»]")


def identifiers(text: str) -> list[tuple[str, str]]:
    """(spelling, key) per identifier in `text`. The key keeps digits and
    separators only, so an Italian `2010/87/UE` meets the English
    `2010/87/EU`; a key of fewer than three digits is too short to mean one
    instrument ("Q1", "PD57")."""
    taken = [(s, e) for _iso, s, e in dates.find_dates(text)]
    out = []
    for m in _TOKEN.finditer(text or ""):
        tok = m.group(0)
        if any(s < m.end() and m.start() < e for s, e in taken):
            continue
        if not ("/" in tok or re.search(r"[A-Z]", tok) or _ORDINAL.search(tok)):
            continue
        key = re.sub(r"[^\d/.-]+", "", tok).strip("/.-")
        key = re.sub(r"([/.-])[/.-]+", r"\1", key)
        if sum(c.isdigit() for c in key) >= 3:
            out.append((tok, key))
    return out


@dataclass
class Index:
    """Dates and identifiers of every note but the checked one, by line."""
    dates: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)   # iso -> (path, line, text)
    idents: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)  # key -> (path, line, spelling)


def index(notes: dict[str, Note], skip: str) -> Index:
    ix = Index()
    for path, n in notes.items():
        if path == skip:
            continue
        for i, line in enumerate(n.text.split("\n"), 1):
            for iso, _s, _e in dates.find_dates(line):
                ix.dates.setdefault(iso, []).append((path, i, line))
            for spelling, key in identifiers(line):
                ix.idents.setdefault(key, []).append((path, i, spelling))
    return ix


def at(path: str, line: int) -> str:
    return f"{path}:{line}"


# ---------------------------------------------------------------- dates

def _stems(text: str) -> list[str]:
    return [w[:6] for w in fold(text).split() if len(w) >= 4]


def event_dates(event: str, ix: Index, pool: set[str]) -> list[tuple[str, str, int, str]]:
    """(iso, path, line, text) for the pool's dated lines that name `event`:
    every word of the event, by its first six letters, on the line
    ("acceptance" meets "Acceptance certificate issued")."""
    want = _stems(event)
    if not want:
        return []
    out = []
    for iso, rows in ix.dates.items():
        for path, line, text in rows:
            if path not in pool or not all(any(w.startswith(s) for w in fold(text).split()) for s in want):
                continue
            # "The six-year period runs from acceptance. Limitation expires
            # 19 April 2028" names acceptance to state the rule, not to date
            # it: a line that counts a period from the event never dates it.
            if any(_stems(p["event"]) == want for p in dates.find_periods(text)):
                continue
            out.append((iso, path, line, text))
    return out


def _label(text: str) -> str:
    """A dated line's words without its dates and table pipes: the event."""
    for _iso, s, e in reversed(dates.find_dates(text)):
        text = text[:s] + text[e:]
    return " ".join(re.sub(r"[|*_`]", " ", text).split())[:80]


def date_rules(d: str, periods: list[dict], ix: Index, pool: set[str]) -> tuple[list[dict], list[dict]]:
    """(findings, states) for one date `d` of a block, given the periods in
    its scope (each {n, unit, event, origin}).

    *inconsistent* is narrow on purpose: the date must equal some other pool
    date plus the same period, while the period names an event the pool dates
    differently. That is the Northwind defect (19 April 2028 is execution plus
    six years; the rule counts from acceptance) and nothing else: a memo's own
    date or a hearing date is never "not acceptance plus six years"."""
    findings, states = [], []
    for p in periods:
        span = dates.period_words(p["n"], p["unit"])
        named = event_dates(p["event"], ix, pool)
        isos = {r[0] for r in named}
        if len(isos) != 1:
            continue  # the pool dates the event not at all, or twice: nothing to compare
        e_iso, e_path, e_line, _t = named[0]
        expected = dates.add_period(e_iso, p["n"], p["unit"])
        if expected == d:
            states.append({"state": "derived date", "say": f"{dates.human(d)} = {e_iso} ({_label(_t)}, "
                           f"{at(e_path, e_line)}) + {span} ({p['origin']})"})
            return findings, states
        named_lines = {(r[1], r[2]) for r in named}
        for b_iso, rows in ix.dates.items():
            if dates.add_period(b_iso, p["n"], p["unit"]) != d:
                continue
            base = [r for r in rows if r[0] in pool and (r[0], r[1]) not in named_lines]
            if base:
                b_path, b_line, b_text = base[0]
                findings.append({
                    "kind": "inconsistent", "words": dates.human(d), "at": p["origin"],
                    "say": (f"{dates.human(d)} = {b_iso} ({_label(b_text)}, {at(b_path, b_line)}) + {span}; "
                            f"the period counts from {p['event']}: {e_iso} ({at(e_path, e_line)}) + {span} = "
                            f"{dates.human(expected)}")})
                return findings, states
    for p in periods:
        for b_iso, rows in ix.dates.items():
            if dates.add_period(b_iso, p["n"], p["unit"]) == d:
                base = [r for r in rows if r[0] in pool]
                if base:
                    b_path, b_line, b_text = base[0]
                    states.append({"state": "derived date", "say": (
                        f"{dates.human(d)} = {b_iso} ({_label(b_text)}, {at(b_path, b_line)}) + "
                        f"{dates.period_words(p['n'], p['unit'])} ({p['origin']})")})
                    return findings, states
    return findings, states


# ---------------------------------------------------------------- quotations

def quotations(text: str, whole: bool) -> list[str]:
    """The quotations of a block: text between quotation marks, or the whole
    of an attributed blockquote; six words or more."""
    found = [text] if whole else _QUOTED.findall(text)
    return [q.strip() for q in found if len(q.split()) >= QUOTE_MIN_WORDS]


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", squash(text).casefold())


def find_quote(q: str, notes: dict[str, Note], where: list[str], near: list[str]) -> dict:
    """{state: verbatim|near|absent, path, line, passage}. Verbatim is looked
    for in every note of `where`, a near passage only in `near` (the block's
    sources and the search's candidates): an 80% match against the whole
    vault would find any common phrase."""
    needle = squash(q).casefold()
    for path in where:
        lines = notes[path].text.split("\n")
        flat = squash(notes[path].text).casefold()
        if needle in flat:
            head = needle[:40]
            line = next((i for i, ln in enumerate(lines, 1) if head[:20] in squash(ln).casefold()), 1)
            return {"state": "verbatim", "path": path, "line": line}
    qw = _words(q)
    best = None
    for path in near:
        lines = notes[path].text.split("\n")
        flat: list[tuple[str, int]] = [(w, i) for i, ln in enumerate(lines, 1) for w in _words(ln)]
        ws = [w for w, _ in flat]
        size = len(qw) + max(2, len(qw) // 4)
        for start in range(0, max(1, len(ws) - len(qw) + 1)):
            window = ws[start:start + size]
            if not window or window[0] not in qw:
                continue
            got = sum(b.size for b in SequenceMatcher(None, qw, window, autojunk=False).get_matching_blocks())
            share = got / len(qw)
            if share >= MISQUOTE_SHARE and (best is None or share > best[0]):
                a, b = flat[start][1], flat[min(start + size, len(flat)) - 1][1]
                best = (share, path, a, " ".join(ln.strip() for ln in lines[a - 1:b]))
    if best:
        return {"state": "near", "path": best[1], "line": best[2], "passage": best[3], "share": best[0]}
    return {"state": "absent"}
