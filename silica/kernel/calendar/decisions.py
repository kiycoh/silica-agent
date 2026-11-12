# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A ledger of decisions and what came of them (ADR-0167).

A decision is a plain note, so it is searched, linked, published and taken back
like any other: `decision: true`, `decided:`, an optional `review_on:` and a
`status:` in its frontmatter, and four sections, Decision, Why, Expected and
Outcomes, the last one a dated line per outcome. No model reads it and none
judges what was expected against what happened: the ledger keeps both side by
side for the person to read.

Any note that gains the frontmatter is a decision where it stands; `create`
writes new ones to `Decisions/` at the top of the vault.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from silica.kernel.base import frontmatter

FOLDER = "Decisions"
STATUSES = ("open", "kept", "changed", "reversed")
OUTCOMES = "## Outcomes"
# What a file name may not carry on any of the three desktop systems, plus what
# Obsidian reads as link syntax.
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
_SECTION = re.compile(r"^## (Decision|Why|Expected|Outcomes)\s*$", re.MULTILINE)
_OUTCOME = re.compile(r"^- (\d{4}-\d{2}-\d{2})(?: \((kept|changed|reversed)\))?: (.*)$")
NAME_CHARS = 80


def day(text: str) -> str:
    """`text` as an ISO day, or ValueError saying the form it takes."""
    try:
        return dt.date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        raise ValueError(f"a date is YYYY-MM-DD, got {text!r}") from None


def name_for(decision: str) -> str:
    """The file name a decision gets: its first words, cut at a word before
    NAME_CHARS, with what a file system or a wikilink would misread taken out."""
    words = " ".join(_UNSAFE.sub(" ", decision).split()).strip(" .")
    if len(words) > NAME_CHARS:
        words = words[:NAME_CHARS].rsplit(" ", 1)[0]
    return words or "Decision"


def note_text(decision: str, *, today: str, why: str = "", expect: str = "", review: str = "") -> str:
    """The note `create` writes: the frontmatter written by hand and not
    dumped, so `decided:` stays a date Obsidian reads as one and nothing is
    quoted that the person did not quote."""
    head = ["---", "decision: true", f"decided: {today}", *([f"review_on: {review}"] if review else []),
            "status: open", "---", ""]
    parts = [("Decision", decision), ("Why", why), ("Expected", expect), ("Outcomes", "")]
    body = [f"## {h}\n\n{t.strip()}\n" if t.strip() else f"## {h}\n" for h, t in parts]
    return "\n".join(head) + "\n".join(body)


def add_outcome(content: str, what: str, today: str, status: str = "") -> str:
    """`content` with one dated outcome under `## Outcomes` (made above
    `## Superseded` when the note has none), and `status:` set when given."""
    from silica.kernel.base.noteshape import SUPERSEDED_MARKER
    from silica.kernel.write.templates import append_under, upsert_props

    line = f"- {today}{f' ({status})' if status else ''}: {' '.join(what.split())}"
    out = append_under(content, OUTCOMES, line, above=(SUPERSEDED_MARKER,))
    return upsert_props(out, {"status": status}) if status else out


def read(path: str, text: str, today: dt.date | None = None) -> dict[str, Any] | None:
    """A decision note as the ledger shows it, None when `text` is not one."""
    data, _raw, body = frontmatter.split(text or "")
    if not data or data.get("decision") is not True:
        return None
    marks = list(_SECTION.finditer(body))
    sections = {m.group(1): body[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(body))].strip()
                for i, m in enumerate(marks)}
    outcomes = []
    for ln in sections.get("Outcomes", "").splitlines():
        if (m := _OUTCOME.match(ln.strip())):
            outcomes.append({"date": m.group(1), "status": m.group(2) or "", "text": m.group(3)})
        elif ln.strip():
            outcomes.append({"date": "", "status": "", "text": ln.strip().removeprefix("- ")})
    review = str(data.get("review_on") or "")[:10]
    status = str(data.get("status") or "open").strip().lower()
    return {"path": path, "name": Path(path).stem, "decided": str(data.get("decided") or "")[:10],
            "review_on": review, "status": status, "decision": sections.get("Decision", ""),
            "why": sections.get("Why", ""), "expected": sections.get("Expected", ""), "outcomes": outcomes,
            "due": status == "open" and bool(review) and review <= (today or dt.date.today()).isoformat()}


def scan(vault: Path, which: str = "all", today: dt.date | None = None) -> list[dict[str, Any]]:
    """The vault's decisions, those due for review first, then by the day
    decided, newest first. `which` is open, due or all."""
    from silica.kernel.calendar.timeline import decision_paths

    rows = []
    for rel in decision_paths(vault):
        try:
            row = read(rel, (vault / rel).read_text(encoding="utf-8"), today)
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable note costs its row, as in the timeline walk
        if row and (which == "all" or (which == "open" and row["status"] == "open") or (which == "due" and row["due"])):
            rows.append(row)
    rows.sort(key=lambda r: r["decided"], reverse=True)
    return sorted(rows, key=lambda r: not r["due"])


def create(decision: str, *, why: str = "", expect: str = "", review: str = "", target: str = "",
           path: str = "", person: bool = True) -> dict[str, Any]:
    """Write a new decision note and say where. `person`: the words are the
    person's, typed with /decide, so the note is created as the editor's new
    note is (ADR-0155 point 8), with no `AI:` stamp; otherwise the chat's
    tool wrote it and the stamp says so. `path` is the tool's, which names its
    note so the write card can; /decide names a folder and the words name the
    file. ValueError names what is refused."""
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel import publish
    from silica.kernel.base.layout import contain_in_vault

    if not decision.strip():
        raise ValueError("a decision needs its words")
    review = day(review) if review.strip() else ""
    if path:
        path = contain_in_vault(path if path.endswith(".md") else path + ".md", Path(CONFIG.vault_path))
        if DRIVER.exists(path):
            raise ValueError(f"{path} already exists: name the decision another way")
        folder = path.rpartition("/")[0]
    else:
        folder = target.strip().strip("/") or FOLDER
        contain_in_vault(f"{folder}/x.md", Path(CONFIG.vault_path))
    if publish.never(f"{folder}/x.md" if folder else "x.md"):
        raise ValueError(f"{folder} is Silica's own folder: write the decision in another")
    if not path:
        stem = name_for(decision)
        path = f"{folder}/{stem}.md"
        n = 2
        while DRIVER.exists(path):
            path, n = f"{folder}/{stem} {n}.md", n + 1
    text = note_text(decision, today=dt.date.today().isoformat(), why=why, expect=expect, review=review)
    if not person:
        from silica.kernel.write.notes import write_note

        return write_note(path, text, template="none")
    from silica.kernel.write.checkpoints import checkpoint_landed
    from silica.kernel.write.gate import create_note_with_restore_point
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE

    run = create_note_with_restore_point(path, text, USER_WRITE_SOURCE)
    return {"path": path, "run": run, **checkpoint_landed(path, None)}


def outcome(path: str, what: str, status: str = "") -> dict[str, Any]:
    """Append one dated outcome to the decision at `path`, as the person's
    write: a run for the page's Undo and a checkpoint for /undo."""
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import checkpoint_landed
    from silica.kernel.write.gate import journaled_write
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE

    if not what.strip():
        raise ValueError("an outcome needs its words")
    if status and status not in STATUSES[1:]:
        raise ValueError(f"status is one of {', '.join(STATUSES[1:])}")
    nc = DRIVER.read_note(path)
    path = nc.ref.path or path
    if read(path, nc.content or "") is None:
        raise ValueError(f"{path} is not a decision: it has no `decision: true`")
    prior = nc.content or ""
    today = dt.date.today().isoformat()
    # `status:` is the one property an outcome changes, declared as such.
    run = journaled_write(path, lambda t: add_outcome(t, what, today, status), source=USER_WRITE_SOURCE,
                          free=("status",))
    return {"path": path, "run": run, **(checkpoint_landed(path, prior) if run else {})}
