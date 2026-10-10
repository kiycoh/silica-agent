# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Which notes are daily notes, as Obsidian's core Daily notes plugin says (ADR-0062).

A daily note is the note Obsidian would open as that day's note: its path
below the plugin's folder, without `.md`, matches the plugin's moment.js date
format. That file is the only declaration read. A template leaves no trace in
the note, and a filename that happens to hold a date is a guess Obsidian does
not make, so neither is a second source here.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import NamedTuple

# What the plugin uses when its settings leave a field empty, or were never
# written: Obsidian's help names the vault root and YYYY-MM-DD.
DEFAULT_FORMAT = "YYYY-MM-DD"

# Longest first, so "MMMM" is never read as "MM" twice. Each token is (regex,
# field): the field is what the match contributes to the date, None when the
# token is decoration (a weekday says nothing a full date does not).
_LETTERS = r"[^\W\d_]+"
_TOKENS: list[tuple[str, str, str | None]] = [
    ("YYYY", r"\d{4}", "year"),
    ("YY", r"\d{2}", "yy"),
    ("MMMM", _LETTERS, "month_name"),
    ("MMM", _LETTERS, "month_name"),
    ("MM", r"\d{2}", "month"),
    ("Mo", r"\d{1,2}(?:st|nd|rd|th)", "month"),
    ("M", r"\d{1,2}", "month"),
    ("Do", r"\d{1,2}(?:st|nd|rd|th)", "day"),
    ("DD", r"\d{2}", "day"),
    ("D", r"\d{1,2}", "day"),
    ("dddd", _LETTERS, None),
    ("ddd", _LETTERS, None),
    ("dd", _LETTERS, None),
    ("d", r"\d", None),
]

# ponytail: month NAMES are read in English, moment's default locale. A vault
# whose Obsidian runs in another language and names its months (no MM in the
# format) has no daily notes. Measured 2026-10-06: 0 of the founder's 7 vaults
# name a month in the format (6 use the default, 1 YYYY-MM-DD). Upgrade at the
# first such vault: a month table per locale, keyed by Obsidian's language. A
# format that also carries a numeric month is unaffected; the name is decoration.
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})


class DailyRule(NamedTuple):
    folder: str            # vault-relative, no slashes at either end; "" = root
    pattern: re.Pattern    # full-matches the path below `folder`, without .md
    fields: tuple[str | None, ...]   # what group i contributes


def compile_format(fmt: str) -> tuple[re.Pattern, tuple[str | None, ...]] | None:
    """The moment.js format as a regex, or None for a token not read here."""
    out: list[str] = []
    fields: list[str | None] = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "[":
            end = fmt.find("]", i)
            if end < 0:
                return None
            out.append(re.escape(fmt[i + 1:end]))
            i = end + 1
            continue
        for tok, rx, field in _TOKENS:
            if fmt.startswith(tok, i):
                out.append(f"({rx})")
                fields.append(field)
                i += len(tok)
                break
        else:
            if fmt[i].isascii() and fmt[i].isalpha():
                # A moment token this reader does not know (a week, an hour,
                # a quarter): fail closed, since guessing would mark the
                # wrong notes as days.
                return None
            out.append(re.escape(fmt[i]))
            i += 1
    return re.compile("".join(out)), tuple(fields)


def _enabled(obs: Path) -> bool:
    try:
        core = json.loads((obs / "core-plugins.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Without the file Obsidian has not said; the plugin's own settings
        # existing is the only sign it was ever used.
        return (obs / "daily-notes.json").is_file()
    except (OSError, ValueError):
        return False
    if isinstance(core, dict):
        return bool(core.get("daily-notes"))
    return "daily-notes" in core   # the older list of enabled plugins


def daily_rule(vault: Path) -> DailyRule | None:
    """The vault's daily-note rule, or None when it has no daily notes."""
    obs = Path(vault) / ".obsidian"
    if not obs.is_dir() or not _enabled(obs):
        return None
    try:
        cfg = json.loads((obs / "daily-notes.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    compiled = compile_format(str(cfg.get("format") or "").strip() or DEFAULT_FORMAT)
    if compiled is None:
        return None
    return DailyRule(str(cfg.get("folder") or "").strip().strip("/"), *compiled)


def daily_date(rel: str, rule: DailyRule | None) -> dt.date | None:
    """The day `rel` (vault-relative, `.md` or not) is the daily note of."""
    if rule is None:
        return None
    rel = rel.replace("\\", "/").removesuffix(".md")
    if rule.folder:
        if not rel.startswith(rule.folder + "/"):
            return None
        rel = rel[len(rule.folder) + 1:]
    m = rule.pattern.fullmatch(rel)
    if m is None:
        return None
    got: dict[str, int] = {}
    names: list[str] = []
    for field, raw in zip(rule.fields, m.groups()):
        if field is None:
            continue
        if field == "month_name":
            names.append(raw.casefold())
            continue
        num = re.match(r"\d+", raw)
        assert num is not None  # every numeric token in _TOKENS opens with \d
        val = int(num.group())
        if field == "yy":
            # moment's two-digit pivot: 69-99 are the 1900s.
            field, val = "year", val + (1900 if val > 68 else 2000)
        if got.setdefault(field, val) != val:
            return None   # YYYY/YYYY-MM-DD naming two different years
    if "month" not in got:
        months = {_MONTHS.get(n) for n in names}
        if len(months) != 1 or None in months:
            return None
        month = months.pop()
        assert month is not None  # refused just above
        got["month"] = month
    try:
        return dt.date(got["year"], got["month"], got["day"])
    except (KeyError, ValueError):
        return None   # a format with no day, or February 30th


def daily_notes(vault: Path) -> dict[str, dt.date]:
    """Every daily note of `vault`, vault-relative path (with .md) -> its day."""
    vault = Path(vault)
    rule = daily_rule(vault)
    if rule is None:
        return {}
    base = vault / rule.folder if rule.folder else vault
    out: dict[str, dt.date] = {}
    for f in sorted(base.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts):
            continue
        rel = "/".join(parts)
        day = daily_date(rel, rule)
        if day is not None:
            out[rel] = day
    return out
