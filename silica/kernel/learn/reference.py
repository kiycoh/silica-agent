# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A reference note read as text (ADR-0131): the lines a plan's steps come
from, what a line says beside its topic, the assessment it states, and the step
line a plan note writes back.

A plan's own step is a reference line with its marks, so both grammars are
here. No vault, no model: every reader takes the note's body.
"""
from __future__ import annotations

import re

# Several bounds in one `restrict:` line, the separator `refused:` already uses
RESTRICT_SEP = " | "


_STEP_RE = re.compile(r"^- \[( |x|X)\]\s*(.*)$")


# A link as a plan's lines read it. `syntax.WIKILINK_NAME_RE` also takes an empty
# alias: `[[x|]]`, on 5 lines of 2,647 notes (2026-11-06), is no link here, so
# merging the two would add a note to a step.
STEP_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]+)?\]\]")


# One line as a heading. An empty `## ` closes a section here, where
# `syntax.HEADING_RE` reads no heading; no note of 2,647 has one (2026-11-06).
HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def note_title(body: str) -> str:
    for line in body.splitlines():
        m = HEADING_LINE_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2)
    return ""


def section_lines(body: str, name: str) -> list[str]:
    out, inside = [], False
    for line in body.splitlines():
        m = HEADING_LINE_RE.match(line)
        if m:
            inside = m.group(2).casefold().startswith(name.casefold())
            continue
        if inside:
            out.append(line)
    return out


def section_links(body: str, name: str) -> list[str]:
    return [w.strip() for line in section_lines(body, name) for w in STEP_LINK_RE.findall(line)]


def parse_steps(body: str) -> list[dict]:
    """`- [ ] [[A]] before [[B]]: because ...` lines: {checked, text, notes, because}.
    Steps live under `## Steps`; a legacy syllabus keeps them anywhere."""
    lines = section_lines(body, "Steps") or body.splitlines()
    out = []
    for line in lines:
        m = _STEP_RE.match(line.strip())
        if not m:
            continue
        text = m.group(2).strip()
        because = text.split(":", 1)[1].strip() if ":" in text else ""
        step = {"checked": m.group(1).lower() == "x", "text": text,
                "notes": [w.strip() for w in STEP_LINK_RE.findall(text)], "because": because}
        # what ADR-0131 writes after a step's notes: its weight, its depth
        h = _HOURS_MARK_RE.search(text)
        if h:
            step["hours"] = float((h.group(2) or h.group(3)).replace(",", "."))
            step["unit"] = h.group(4) or ""
        d = _DEPTH_MARK_RE.search(text)
        if d:
            step["depth"] = d.group(1)
        out.append(step)
    return out


_HOURS_MARK_RE = re.compile(r"\s·\s(?:(weight)\s(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s(h|CFU|ECTS|wk|min))(?=\s·|$)")


_DEPTH_MARK_RE = re.compile(r"\s·\s(deep|well|read)(?=\s·|$)")


# The whole tail step_marks writes. A reference's checkbox line kept it in its
# text, so "lasso · weight 4" was the topic named missing and the weight
# never reached the plan (2026-10-29); reference_rows already parts the two.
_MARKS_TAIL_RE = re.compile(r"(?:\s·\s(?:by meaning, [^·]*|weight\s\d+(?:[.,]\d+)?"
                            r"|\d+(?:[.,]\d+)?\s(?:h|CFU|ECTS|wk|min)|deep|well|read))+$")


def step_rows(body: str) -> list[dict]:
    """A reference's checkbox steps in reference_rows' shape: {text, hours,
    unit, depth}, the text without its marks."""
    return [{"text": _MARKS_TAIL_RE.sub("", s["text"]), **{k: s[k] for k in ("hours", "unit", "depth") if k in s}}
            for s in parse_steps(body)]


def step_marks(node: dict) -> str:
    """The step line's tail: the topics met by meaning only (plain titles: a
    link there would be one more note of the step), its weight in the
    reference's unit, its depth."""
    out = ""
    pairs = node.get("by_meaning") or []
    if pairs:
        # no colon: it is what tells a step's label from its notes
        out += " · by meaning, " + "; ".join(f"{t} ≈ {n}" for t, n in pairs[:3])
    h = node.get("hours")
    if h:
        n = f"{h:g}"
        out += f" · {n} {node['unit']}" if node.get("unit") else f" · weight {n}"
    if node.get("depth") in ("deep", "well", "read"):
        out += f" · {node['depth']}"
    return out


_UNIT_RE = re.compile(r"^(ore|ora|hours?|hrs?|h|cfu|ects|crediti|credits?|settimane|weeks?|wk|minuti|minutes?|min)\b",
                      re.I)


_DEPTH_COL_RE = re.compile(r"profondit|depth", re.I)


_DIFF_COL_RE = re.compile(r"^diff|difficolt|difficulty", re.I)


_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


_DEPTH_WORDS = (("deep", re.compile(r"\b(a fondo|in depth|deep|thorough)", re.I)),
                ("read", re.compile(r"\b(una lettura|lettura|skim|read once|cenni|overview)", re.I)),
                ("well", re.compile(r"\b(bene|well|good)\b", re.I)))


def _unit_of(header: str) -> str:
    """The step-line unit of an hours-like column: the reference's own."""
    m = _UNIT_RE.match(header.strip())
    if not m:
        return ""
    w = m.group(1).casefold()
    return ("h" if w[0] in "oh" else "CFU" if w in ("cfu", "crediti", "credits", "credit") else
            "ECTS" if w == "ects" else "wk" if w[0] in "sw" else "min")


def reference_rows(body: str) -> list[dict]:
    """The lines of a reference note to plan from, each with what it says
    beside its topic: {text, hours, unit, depth}. List items and table rows,
    after a "Programma" marker when the note has one.

    A syllabus converted from a PDF is tables: the official one this was
    written against held its programme as `| 8 | Modelli lineari: ... |` rows
    under a bare PROGRAMMA line, after tables of department, credits and
    office hours, and only `-`/`*`/digit lines were read, so it gave nothing.
    A row stands for its longest cell; a table's header row is skipped.

    A numeric cell under an hours, credits, weeks or minutes
    header is the row's weight (ADR-0131 point 8); a depth cell, a guide's
    (point 11). A note with a table whose header names a depth, difficulty
    or hours column and no programme marker is read from that table only:
    a study guide's bullets and other tables are its prose, not its plan."""
    rows = body.splitlines()
    start = next((i + 1 for i, ln in enumerate(rows) if _PROGRAMME_RE.match(ln.strip())), 0)

    def cells_of(ln: str) -> list[str]:
        return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT_RE.split(ln.strip().strip("|"))]

    headers: dict[int, list[str] | None] = {}   # line of a table's first row -> its header cells
    current = None
    for i, ln in enumerate(rows):
        s = ln.strip()
        if not s.startswith("|"):
            current = None
            continue
        if i + 1 < len(rows) and _TABLE_RULE_RE.match(rows[i + 1].strip()):
            current = cells_of(s)
        headers[i] = current
    def attr(h): return bool(h) and any(_unit_of(c) or _DEPTH_COL_RE.search(c) or _DIFF_COL_RE.search(c) for c in h)
    plan_table = not start and any(attr(h) for h in headers.values())
    out: list[dict] = []
    for i in range(start, len(rows)):
        ln = rows[i].strip()
        if ln.startswith("|"):
            if _TABLE_RULE_RE.match(ln):
                continue
            if i + 1 < len(rows) and _TABLE_RULE_RE.match(rows[i + 1].strip()):
                continue  # the header above a |---| rule
            head = headers.get(i)
            if plan_table and not attr(head):
                continue
            cells = cells_of(ln)
            row: dict = {"text": "", "hours": None}
            skip = set()
            for k, c in enumerate(cells):
                h = head[k] if head and k < len(head) else ""
                if h and _unit_of(h):
                    skip.add(k)
                    if _NUMBER_RE.match(c):
                        row.update(hours=float(c.replace(",", ".")), unit=_unit_of(h))
                elif h and _DEPTH_COL_RE.search(h):
                    skip.add(k)
                    # the first depth the cell says: "bene; una lettura RKHS" is "well"
                    found = [(m.start(), d) for d, rx in _DEPTH_WORDS for m in [rx.search(c)] if m]
                    row["depth"] = min(found)[1] if found else None
                elif h and _DIFF_COL_RE.search(h):
                    skip.add(k)
                elif not head and _NUMBER_RE.match(c) and len(cells) > 1:
                    skip.add(k)
                    row.update(hours=float(c.replace(",", ".")), unit="")
            # A link's alias in a table is `[[A\|a]]`: split on the unescaped
            # bars only, then read the escaped ones back as the alias bar. The
            # autolinker writes exactly that into a syllabus's cells.
            cell = max((c for k, c in enumerate(cells) if k not in skip), key=len, default="")
            if len(cell.split()) >= 2:
                row["text"] = cell
                out.append(row)
        elif not plan_table and ln.startswith(("-", "*", "1", "2", "3", "4", "5", "6", "7", "8", "9")) and len(ln) > 3:
            out.append({"text": ln.strip("-* ").strip(), "hours": None})
    return out


# An assessment's parts as the plan's form names them.
PART_LABEL = {"project": "Project", "oral": "Oral exam", "written": "Written exam", "presentation": "Presentation",
              "exercises": "Exercises"}

_ASSESS_RE = re.compile(r"\b(valutazion\w*|esam[ei]|modalit\w* d.esame|verifica|assessment|exam\w*|grading|evaluation)\b",
                        re.I)


_PARTS = (("project", re.compile(r"\b(progett\w*|project\w*|elaborat\w*|tesina)\b", re.I)),
          ("oral", re.compile(r"\b(oral[ei]?|colloquio|viva)\b", re.I)),
          ("written", re.compile(r"\b(scritt[oa]|written)\b", re.I)),
          ("presentation", re.compile(r"\b(presentazion\w*|presentation)\b", re.I)),
          ("exercises", re.compile(r"\b(eserciz\w*|homework|assignments?|quiz\w*)\b", re.I)))


_LEAD_RE = re.compile(r"\b(una|un|uno|one|due|two|tre|three|quattro|four|\d+)\s+(settiman[ae]|weeks?|giorni|days?)\b",
                      re.I)


_COUNT = {"una": 1, "un": 1, "uno": 1, "one": 1, "due": 2, "two": 2, "tre": 3, "three": 3, "quattro": 4, "four": 4}


def assessment_of(body: str) -> list[dict]:
    """The parts of the assessment a reference states, [{part, days}] in the
    order it names them (ADR-0131 point 9): a row, line or section headed by
    an assessment word, read for a project, an oral, a written exam, a
    presentation, exercises; a project's lead time in days when the text
    gives one ("almeno una settimana di anticipo"). No model call: the
    vocabulary is bounded, and the reader edits what the words miss."""
    texts: list[str] = []
    under = False
    for line in body.splitlines():
        s = line.strip()
        h = HEADING_LINE_RE.match(s)
        if h:
            under = bool(_ASSESS_RE.search(h.group(2)))
            continue
        if under and s:
            texts.append(s)
        elif s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2 and len(cells[0].split()) <= 5 and _ASSESS_RE.search(cells[0]):
                texts.append(" ".join(cells[1:]))
        elif ":" in s and len(s.split(":", 1)[0].split()) <= 5 and _ASSESS_RE.search(s.split(":", 1)[0]):
            texts.append(s.split(":", 1)[1])
    text = " ".join(texts)
    found = sorted(((m.start(), part) for part, rx in _PARTS for m in [rx.search(text)] if m))
    out = []
    for _at, part in found:
        days = None
        if part == "project":
            m = _LEAD_RE.search(text)
            if m:
                n = int(m.group(1)) if m.group(1).isdigit() else _COUNT[m.group(1).casefold()]
                days = n * 7 if m.group(2).casefold().startswith(("settiman", "week")) else n
        out.append({"part": part, "days": days})
    return out


def assessment_text(parts: list[dict]) -> str:
    """The `assessment:` key: "project 7 | oral"; "none" when cleared."""
    return " | ".join(p["part"] + (f" {p['days']}" if p.get("days") else "") for p in parts) or "none"


def parse_assessment(value) -> list[dict]:
    out = []
    for bit in str(value or "").split(" | "):
        m = re.match(r"^\s*([a-z]+)(?:\s+(\d+))?\s*$", bit)
        if m and m.group(1) in {p for p, _rx in _PARTS}:
            out.append({"part": m.group(1), "days": int(m.group(2)) if m.group(2) else None})
    return out


# A syllabus names its topics in one line per block ("Modelli lineari:
# regressione lineare multipla, analisi discriminante ..., lasso."), and the
# whole line used to match at most two notes by its longest words.
_TOPIC_SPLIT_RE = re.compile(r"\s*[,;]\s*")


_PROGRAMME_RE = re.compile(r"^\W*(programma|programme|program|syllabus|contenuti|argomenti"
                           r"|course content|topics)\W*$", re.IGNORECASE)


_TABLE_RULE_RE = re.compile(r"^\|[\s:|-]+\|?$")


_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


def line_topics(text: str) -> list[str]:
    """The topics a step line lists, when it lists two or more: after its
    label ("Modelli lineari: a, b, c"), split on commas and semicolons."""
    plain = STEP_LINK_RE.sub(r"\1", text)
    body = plain.split(":", 1)[1] if ":" in plain else plain
    parts = [p.strip(" .") for p in _TOPIC_SPLIT_RE.split(body) if p.strip(" .")]
    return parts if len(parts) >= 2 else []


_STEP_MARKS_RE = re.compile(r"\s*\((?:≈|ref)\)|\s·\s(?:weight\s\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s(?:h|CFU|ECTS|wk|min)|deep|well|read"
                            r"|by meaning, [^·]*?)(?=\s·|$)")


def step_label(step: dict) -> str:
    """A step's name: its label before the colon, or its notes when it has
    none, without the markers a plan note writes on it."""
    text = STEP_LINK_RE.sub(r"\1", step.get("text") or "")
    head = text.split(":", 1)[0].strip() if ":" in text else text.strip()
    return _STEP_MARKS_RE.sub("", head)[:90] or ", ".join(step.get("notes") or [])[:90]
