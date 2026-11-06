# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one writer of an event note. Below the tools, because the calendar's
quick card lands here as the chat's tool does."""
from __future__ import annotations

import datetime as dt
from typing import Any

import yaml

from silica.driver import DRIVER


def _event_stem(title: str, start: str, rrule: str) -> tuple[str, str]:
    """(stem, "") or ("", error) for the note an event is written to."""
    from silica.kernel.write import templates as tpl

    # Recurring events keep a date-free stem: the series has no single day.
    # slugify strips every path separator, so the stem cannot steer the write out
    # of the calendar folder — but a recurring title leads the stem, and every
    # reader of the calendar (scan_events, the timeline walk, ignore_matcher)
    # skips a path part starting with "." as vault plumbing. A leading dot would
    # therefore write the note, report success, and leave an event no agenda,
    # update or reminder can ever see again. A title of nothing but dots and
    # stripped characters has no filename left at all.
    safe_title = tpl.slugify(title).lstrip(".")
    if not safe_title:
        return "", f"title {title!r} has no usable characters for a filename"
    return (safe_title if rrule else f"{start.strip()[:10]} {safe_title}"), ""


def _free_path(folder: str, stem: str) -> str:
    path = f"{folder}/{stem}.md"
    n = 2
    while True:
        try:
            DRIVER.read_note(path)
        except Exception:
            return path  # free slot — the happy path
        path = f"{folder}/{stem} {n}.md"
        n += 1


def event_note_path(title: str, start: str, rrule: str = "") -> dict[str, Any]:
    """The path `create_event` would write right now, for the calendar's quick
    card to name before it saves (ADR-0118 point 1): the note is the event."""
    from silica.kernel.base.manifest import in_write_dir

    stem, err = _event_stem(title.strip(), start, rrule)
    if err:
        return {"error": err}
    return {"path": _free_path(in_write_dir("calendar"), stem)}


def create_event(title: str, start: str, *, end: str = "", rrule: str = "",
                 reminder: str = "", body: str = "", plan: str = "",
                 person: bool = False) -> dict[str, Any]:
    """The one writer of an event note: the chat's tool and the calendar's
    quick card (`POST /calendar/event`) both land here, so the slug rule, the
    free-name loop and the restore point exist once. `plan` is a plan note's
    vault path and makes the event a study session (ADR-0118 point 2).
    `person` is the quick card: the person typed the event, so it does not
    take the `AI: true` floor every model write gets, which Learn reads as
    "written by the AI"."""
    from silica.kernel.calendar.model import validate_event
    from silica.kernel.base.manifest import in_write_dir
    from silica.kernel.workqueue import path_lease
    from silica.kernel.write import templates as tpl
    from silica.kernel.write.checkpoints import checkpoint_landed

    title = title.strip()
    if not title:
        return {"error": "title is required"}
    fm: dict[str, Any] = {"event_start": start}
    if end:
        fm["event_end"] = end
    if rrule:
        fm["event_rrule"] = rrule
    if reminder:
        fm["event_reminder"] = reminder
    errors = validate_event(fm)
    if errors:
        return {"error": "; ".join(errors)}
    if plan.strip():
        fm["event_plan"] = f"[[{plan.strip().removesuffix('.md')}]]"

    stem, err = _event_stem(title, start, rrule)
    if err:
        return {"error": err}
    folder = in_write_dir("calendar")

    ordered: dict[str, Any] = {"title": title, **fm}
    ordered["last modified"] = dt.date.today().isoformat()
    block = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False).strip()
    content = (f"---\n{block}\n---\n\n{body.strip()}\n" if body.strip()
               else f"---\n{block}\n---\n")
    if not person:
        content = tpl.ensure_system_floor(content)

    with path_lease(f"{folder}/{stem}.md"):
        path = _free_path(folder, stem)
        try:
            ref = DRIVER.create(path, content)
        except Exception as e:
            return {"error": f"Failed to create event note '{path}': {e}"}

        checkpoint = checkpoint_landed(path, None)  # None: created, /undo removes it

    return {"op": "write", "success": True, "path": ref.path or path, **checkpoint}
