# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Absolute dates and counted periods in English and Italian prose, shared by
the study plan's deadline (ADR-0081) and the source check (ADR-0110 point 5,
the module ADR-0107 shares).

The month names are shared; the readers are not. `plans.read_date` reads a
plan's deadline sentence and resolves a year-less day forward to the next such
day. A check must never do that, because a date given without a year is not
one a writer can be held to. So `find_dates` requires a year, and its patterns
stay apart from `plans._DATE_RES` on purpose.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

MONTHS = {
    **{m: i for i, m in enumerate(("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
                                   "settembre", "ottobre", "novembre", "dicembre"), 1)},
    **{m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                   "september", "october", "november", "december"), 1)},
    **{m: i for i, m in enumerate(("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"), 1)},
    **{m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)},
    "sept": 9,
}
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

_EN_FIRST = "|".join(sorted(("january", "february", "march", "april", "june", "july", "august", "september", "october",
                             "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
                             "oct", "nov", "dec"), key=len, reverse=True))
# Every pattern carries its year. Numeric dates are day first, as both
# languages write them; a dot needs the full year, since "3.5" is a figure.
_ABSOLUTE = (
    ("ymd", re.compile(r"(?<![\d/.-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d/-])")),
    ("dmy", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|°|º)?\s+(?:di\s+|of\s+)?({MONTH_RE})\.?,?\s+(\d{{4}})\b", re.I)),
    # "May" capitalised only, as plans.py rules: "you may 3 2026" is a verb.
    ("mdy", re.compile(rf"\b((?i:{_EN_FIRST})|May)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b")),
    ("num", re.compile(r"(?<![\d/.])(\d{1,2})([/.])(\d{1,2})\2(\d{4})(?![\d/.])")),
)


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def find_dates(text: str) -> list[tuple[str, int, int]]:
    """(ISO date, start, end) for every absolute date in `text`, in order,
    overlaps resolved to the earliest and then the longest match."""
    found: list[tuple[str, int, int]] = []
    for kind, rx in _ABSOLUTE:
        for m in rx.finditer(text or ""):
            g = m.groups()
            if kind == "ymd":
                iso = _iso(int(g[0]), int(g[1]), int(g[2]))
            elif kind == "dmy":
                iso = _iso(int(g[2]), MONTHS[g[1].lower()], int(g[0]))
            elif kind == "mdy":
                iso = _iso(int(g[2]), MONTHS[g[0].lower()], int(g[1]))
            else:
                iso = _iso(int(g[3]), int(g[2]), int(g[0]))
            if iso:
                found.append((iso, m.start(), m.end()))
    found.sort(key=lambda t: (t[1], -(t[2] - t[1])))
    out: list[tuple[str, int, int]] = []
    for hit in found:
        if out and hit[1] < out[-1][2]:
            continue
        out.append(hit)
    return out


def human(iso: str) -> str:
    """`2028-04-19` as `19 April 2028`, the form a finding's box shows."""
    d = date.fromisoformat(iso)
    return f"{d.day} {calendar.month_name[d.month]} {d.year}"


# ---------------------------------------------------------------- periods

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "sixty": 60, "ninety": 90,
    "un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8,
    "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "quindici": 15, "venti": 20, "trenta": 30,
    "sessanta": 60, "novanta": 90,
}
_UNITS = {
    "year": "years", "years": "years", "anno": "years", "anni": "years",
    "month": "months", "months": "months", "mese": "months", "mesi": "months",
    "week": "weeks", "weeks": "weeks", "settimana": "weeks", "settimane": "weeks",
    "day": "days", "days": "days", "giorno": "days", "giorni": "days",
}
_NUM_RE = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_UNIT_RE = "|".join(sorted(_UNITS, key=len, reverse=True))
# "six years from acceptance", "the six-year period runs from acceptance",
# "sei anni dall'accettazione", "trenta giorni decorrenti dalla consegna".
# At most four words between the unit and the preposition, so a period and an
# event two clauses apart are never paired.
_PERIOD = re.compile(
    rf"\b(?P<n>\d{{1,3}}|{_NUM_RE})[\s-]+(?P<u>{_UNIT_RE})\b(?P<gap>(?:[\s,]+[\w’']+){{0,4}}?)"
    rf"[\s,]+(?P<prep>from|after|since|following|dall['’]|dalla|dallo|dalle|dagli|dai|dal|"
    rf"decorrent[ei]\s+da\w*['’]?|a\s+decorrere\s+da\w*['’]?)\s*(?P<event>[^.;:\n()\[\]|]{{2,80}})",
    re.I,
)
_EVENT_LEAD = re.compile(
    r"^(?:(?:the|a|an|its|il|lo|la|i|gli|le)\s+|l['’]\s*|date\s+of(?:\s+the)?\s+|data\s+(?:di|del|della)\s+"
    r"|data\s+dell['’]\s*)", re.I)
# An event named only by these says when, not what: "six years from the date
# on which the cause of action accrued" names no event a chronology dates.
_GENERIC_EVENT = frozenset({"date", "data", "day", "giorno", "time", "momento", "moment"})
# A word that ends the event's name: "acceptance under that procedure" is acceptance.
_EVENT_STOP = frozenset("""
under which that and or but where when by per as of in on to for with if unless
ai sensi secondo e o ma quando se salvo con per nel nella del della che il la
""".split())


def find_periods(text: str) -> list[dict]:
    """Periods counted from a named event: {n, unit, event, start, end}.
    `event` is the event's words, articles and "date of" dropped, cut at the
    first word that cannot be part of its name."""
    out = []
    for m in _PERIOD.finditer(text or ""):
        raw = m.group("n").lower()
        n = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
        if not n:
            continue
        event = m.group("event").strip()
        while True:
            cut = _EVENT_LEAD.sub("", event, count=1)
            if cut == event:
                break
            event = cut
        words = []
        for w in re.findall(r"[\w’']+", event):
            if w.lower() in _EVENT_STOP or len(words) == 4:
                break
            words.append(w)
        if not words or all(w.lower() in _GENERIC_EVENT for w in words):
            continue
        out.append({"n": n, "unit": _UNITS[m.group("u").lower()], "event": " ".join(words),
                    "start": m.start(), "end": m.end()})
    return out


def add_period(iso: str, n: int, unit: str) -> str:
    """`iso` plus n years, months, weeks or days; a day past the month's end
    lands on its last day (29 February plus one year is 28 February)."""
    d = date.fromisoformat(iso)
    if unit in ("years", "months"):
        months = d.month - 1 + (n * 12 if unit == "years" else n)
        y, m = d.year + months // 12, months % 12 + 1
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1])).isoformat()
    return date.fromordinal(d.toordinal() + n * (7 if unit == "weeks" else 1)).isoformat()


def period_words(n: int, unit: str) -> str:
    return f"{n} {unit[:-1] if n == 1 else unit}"
