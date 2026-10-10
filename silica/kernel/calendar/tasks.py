# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Task lines, read the way the Tasks plugin reads them (ADR-0062 Decision 3).

Read-only by decision (ADR-0062 Decision 4). Completing a task means writing
its done date, the next instance of its recurrence and its on-completion
action, which is Tasks' own job; a second implementation of it here would
drift from the plugin the writer actually ticks with.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from silica.kernel.calendar.daily import DailyRule, compile_format, daily_date, daily_rule

# The same shapes Tasks accepts: a list item of any marker, under any depth of
# blockquote or callout, then a one-character status in brackets.
_TASK_RE = re.compile(r"^[\s>]*(?:[-*+]|\d+[.)])\s+\[(.)\]\s*(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Tasks' signifiers and their alternative spellings. A date is strictly
# YYYY-MM-DD, as Tasks requires: "📅 2026-10-5" is text to the plugin, and the
# calendar shows what the plugin would show.
_DATE_SIGNS = {"created": "➕", "scheduled": "⏳⌛", "start": "🛫",
               "due": "📅📆🗓", "done": "✅", "cancelled": "❌"}
_DATE_RE = {k: re.compile(f"[{signs}]️?\\s*(\\d{{4}}-\\d{{2}}-\\d{{2}})")
            for k, signs in _DATE_SIGNS.items()}
# Every signifier that ends the description: the dates above, the five
# priorities, recurrence, id, depends-on and on-completion.
FIELD_START = re.compile("[" + "".join(_DATE_SIGNS.values()) + "🔺⏫🔼🔽⏬🔁🆔⛔🏁]")

TASKS_PLUGIN = "obsidian-tasks-plugin"


@dataclass(frozen=True)
class Task:
    path: str                  # vault-relative, with .md
    line: int                  # 1-based
    text: str                  # the description, signifiers stripped
    status: str                # "open" | "done" | "cancelled"
    due: dt.date | None = None
    scheduled: dt.date | None = None
    start: dt.date | None = None
    done: dt.date | None = None
    daily: dt.date | None = None   # the day of the daily note it sits in


def _status(ch: str) -> str:
    # Tasks' core statuses: x done, - cancelled, space and / still to do.
    # ponytail: any other status character reads as open. The founder's vault
    # (2026-10-06) defines exactly Tasks' defaults, "/" and "-", both handled
    # here. Upgrade at the first vault with a custom status: read its type
    # (TODO, DONE, CANCELLED, NON_TASK) from statusSettings.customStatuses.
    if ch in "xX":
        return "done"
    return "cancelled" if ch == "-" else "open"


def _date(raw: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(raw) if raw else None
    except ValueError:
        return None   # 2026-02-30: Tasks would not read it as a date either


def _field(rx: re.Pattern, body: str) -> dt.date | None:
    m = rx.search(body)
    return _date(m[1]) if m else None


def parse_tasks(text: str, *, global_filter: str = "", path: str = "") -> list[Task]:
    """Every task line of one note, in order."""
    out: list[Task] = []
    fenced = False
    for i, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        m = None if fenced else _TASK_RE.match(line)
        if m is None:
            continue
        body = m.group(2)
        if global_filter and global_filter not in body:
            continue
        cut = FIELD_START.search(body)
        desc = (body[:cut.start()] if cut else body).replace("**", "").replace("__", "").strip()
        if not desc:
            continue   # the template's empty "- [ ] " waiting to be written
        dates = {k: _field(rx, body) for k, rx in _DATE_RE.items()}
        out.append(Task(path=path, line=i, text=desc, status=_status(m.group(1)),
                        due=dates["due"], scheduled=dates["scheduled"],
                        start=dates["start"], done=dates["done"]))
    return out


def tasks_settings(vault: Path) -> dict:
    """The Tasks plugin's settings that change what a line means, defaulted."""
    out = {"taskFormat": "tasksPluginEmoji", "globalFilter": "",
           "useFilenameAsScheduledDate": False, "filenameAsScheduledDateFormat": "",
           "filenameAsDateFolders": []}
    try:
        data = json.loads((Path(vault) / ".obsidian" / "plugins" / TASKS_PLUGIN /
                           "data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No plugin, or settings it cannot have written: the emoji format and
        # Tasks' defaults, which is what a line in that format means anyway.
        return out
    if isinstance(data, dict):
        out.update({k: data[k] for k in out if k in data})
    return out


_FILENAME_DEFAULT = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})")


def _filename_date(rel: str, settings: dict) -> dt.date | None:
    """Tasks' "use filename as Scheduled date": the date the file's name holds."""
    folders = [str(f).strip("/") for f in settings.get("filenameAsDateFolders") or []]
    if folders and not any(rel.startswith(f + "/") for f in folders if f):
        return None
    stem = rel.rsplit("/", 1)[-1].removesuffix(".md")
    fmt = str(settings.get("filenameAsScheduledDateFormat") or "").strip()
    if fmt:
        compiled = compile_format(fmt)
        return daily_date(stem, DailyRule("", *compiled)) if compiled else None
    m = _FILENAME_DEFAULT.search(stem)
    return _date(f"{m[1]}-{m[2]}-{m[3]}") if m else None


_scan_memo: dict[str, tuple[str, list[Task]]] = {}


def scan_tasks(vault: Path) -> list[Task]:
    """Every task of the vault, memoized on its file-state epoch like the timeline."""
    from silica.kernel.recall.paths import SOURCES_DIR, ignore_matcher, vault_epoch

    vault = Path(vault)
    epoch = vault_epoch(str(vault))
    hit = _scan_memo.get(str(vault))
    if epoch and hit is not None and hit[0] == epoch:
        return hit[1]
    settings = tasks_settings(vault)
    if settings["taskFormat"] != "tasksPluginEmoji":
        # ponytail: the Dataview format ([due:: ...]) is not read, so the axis
        # is empty rather than wrong. 0 of the 1 vault with Tasks installed
        # uses it (2026-10-06); the upgrade is one more pattern per date field.
        return []
    rule = daily_rule(vault)
    ignored = ignore_matcher(vault)
    by_name = settings["useFilenameAsScheduledDate"]
    out: list[Task] = []
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts) or any(ignored(p) for p in parts[:-1]):
            continue
        if parts[0] == SOURCES_DIR:
            continue   # verbatim leaves: a source's checklist is not the writer's
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue   # one unreadable note costs its tasks, not the agenda
        rel = "/".join(parts)
        day = daily_date(rel, rule)
        named = _filename_date(rel, settings) if by_name else None
        for t in parse_tasks(text, global_filter=settings["globalFilter"], path=rel):
            # Tasks dates only the UNDATED from the name: a task with any of
            # its three dates keeps them as written.
            undated = not (t.due or t.scheduled or t.start)
            out.append(Task(**{**t.__dict__, "daily": day,
                               "scheduled": named if undated and named else t.scheduled}))
    if epoch:
        _scan_memo.clear()
        _scan_memo[str(vault)] = (epoch, out)
    return out
