# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A plan step read as one explanation (ADR-0132).

A reading is one note per step: a section per concept, the step's notes
grouped into them, the sections in the order of their prerequisites with the
lecture order breaking ties. Each section opens on a guess and closes on the
reader's own words; what the reader writes stays out of the vault and out of
the answer ledger, in `readings.jsonl` beside `quiz.jsonl`.

The order and the sections are a model's; the checks are code's: only the
step's notes count, each once; a cycle keeps the lecture order; every sentence
of a definition, a mechanism and a contrast is held against the section's
notes with the flashcards' gate (`artifacts._grounded`, formulas included),
and one the notes do not hold is marked, never dropped.
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
# A section's notes handed over whole up to this much (a lesson's few screens);
# past it each note is cut evenly, so no note of the section goes missing.
SECTION_CHARS = 9000
SECTION_MAX_TOKENS = 2200
# Calls of one step in flight at once: four keeps a 12-section step near a
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
KINDS = ("guess", "own")

ORDER_PROMPT = """These are the notes of one study step, in the order the lectures present them:
{notes}

Group them into sections for one explanation a student reads from start to end. A section explains one concept and may cover several notes that belong together; a long note may stand alone. Put a section before another only when the other needs it to be understood; otherwise keep the lectures' order.

Write every section as three lines, and nothing else:
SECTION: a short title in {language}
NOTES: the titles of the notes it covers, exactly as written above, separated by " ; "
NEEDS: the titles of notes of this step that must be read before this section, separated by " ; ", or none
Every note appears in exactly one section."""

SECTION_PROMPT = """You write one section of a study explanation for a university student, from these notes only:
{notes}

The section is "{title}".{needs}
Write in {language}, with formulas in $...$ as the notes write them. Use only what the notes say, except in EXAMPLE. No history, anecdotes, curiosities or motivation.

Reply with these labelled parts, each label at the start of its own line, and nothing else:
DEFINITION: what the concept is, in two or three sentences in the notes' words.
MECHANISM: how it works and why. For a formula or a derivation, give each step on its own line and say what it does.
EXAMPLE: one concrete case worked through, with small numbers where the topic allows, using the notes' symbols.
CONTRAST: what it must not be confused with among these notes or the step's other concepts, and the difference; or none.
GUESS: one question, asked before the student reads, on the central claim of this section; it must be answerable from DEFINITION or MECHANISM.
ANSWER: the one sentence of DEFINITION or MECHANISM that answers GUESS, copied exactly.
OWN: one prompt asking the student to explain the concept or its principle in their own words."""

_FIELDS = ("DEFINITION", "MECHANISM", "EXAMPLE", "CONTRAST", "GUESS", "ANSWER", "OWN")
_LABEL_RE = re.compile(r"^\s*(?:[-*]\s*)?\**\s*(SECTION|NOTES|NEEDS|" + "|".join(_FIELDS) + r")\s*\**\s*:\s*\**\s*(.*)$", re.I)
_NONE_RE = re.compile(r"^\s*(?:none|nessun[oa]?|-|n/?a)\.?\s*$", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ý$(])")
_FENCE_SAFE_RE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_WIKI_RE = re.compile(r"\[\[([^\]|#]+)(?:[^\]]*)\]\]")


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


# ------------------------------------------------------------------ the order

def parse_sections(reply: str) -> list[dict]:
    """[{title, notes, needs}] from the ordering reply's three-line blocks."""
    out: list[dict] = []
    for line in (reply or "").splitlines():
        m = _LABEL_RE.match(line)
        if not m:
            continue
        key, val = m.group(1).upper(), m.group(2).strip().strip("*").strip()
        if key == "SECTION":
            out.append({"title": val, "notes": [], "needs": []})
        elif out and key in ("NOTES", "NEEDS"):
            items = [] if _NONE_RE.match(val) else [x.strip().strip("[]").strip() for x in re.split(r"\s*;\s*", val)]
            out[-1][key.lower()] = [x for x in items if x]
    return out


def order_sections(titles: list[str], raw: list[dict]) -> dict:
    """{sections: [{title, notes, needs, needed_by}], order: "prerequisites"|"lecture"}.

    Only the step's notes count, each in the first section that names it; a
    note no section names becomes a section of its own. A section depends on
    the sections holding the notes it needs; a stable topological sort keyed
    on each section's earliest lecture position moves a section earlier only
    when a prerequisite forces it. A cycle keeps the lecture order and says so."""
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
    for rank, j in enumerate(placed):
        s = secs[j]
        # moved up: placed before a section that came earlier in the lectures and needs it
        needed_by = [secs[i]["notes"][0] for i in after[j] if secs[i]["key"] < s["key"]] if order == "prerequisites" else []
        out.append({"title": s["title"], "notes": s["notes"],
                    "needs": [n for n in s["needs_raw"] if where[n.casefold()] != j], "needed_by": needed_by})
    return {"sections": out, "order": order}


# ------------------------------------------------------------------ a section

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


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        out += [s for s in _SENTENCE_RE.split(line) if s.strip()]
    return out


def mark_unheld(text: str, notes_text: str) -> tuple[str, int]:
    """The text with each sentence the notes do not hold marked, line breaks
    kept; and how many were marked. A line of formula alone is judged by its
    formula; a sentence of fewer than three content words is not judged."""
    from silica.kernel.learn import artifacts

    marked = 0
    lines = []
    for line in text.splitlines():
        if not line.strip():
            lines.append(line)
            continue
        parts = []
        for s in _SENTENCE_RE.split(line):
            judged = artifacts._MATH_RE.search(s) or len(artifacts._words(s)) >= 3
            if judged and not artifacts._grounded(s, notes_text):
                parts.append(f"{s.rstrip()} {NOT_HELD}")
                marked += 1
            else:
                parts.append(s)
        lines.append(" ".join(parts))
    return "\n".join(lines), marked


def highlight_answer(text: str, answer: str) -> tuple[str, bool]:
    """The text with the sentence that answers the guess marked `==...==`:
    the answer copied exactly, else the sentence sharing most of its words
    (at least half). Unchanged when none does."""
    from silica.kernel.learn import artifacts

    a = (answer or "").strip().strip("=").strip()
    if not a or "==" in text:
        return text, "==" in text
    if a in text:
        return text.replace(a, f"=={a}==", 1), True
    want = artifacts._words(a)
    if not want:
        return text, False
    best, score = None, 0.0
    for s in _sentences(text):
        got = artifacts._words(s)
        sc = len(want & got) / len(want)
        if sc > score:
            best, score = s.strip(), sc
    if best and score >= 0.5:
        clean = best.replace(f" {NOT_HELD}", "")
        return text.replace(clean, f"=={clean}==", 1), True
    return text, False


def _notes_block(paths: list[str], limit: int = SECTION_CHARS) -> tuple[str, str]:
    """(the notes as `### title` blocks, their joined text for the check)."""
    from silica.kernel.learn import items
    from silica.kernel.write import frontmatter

    bodies = []
    for p in paths:
        _d, _r, body = frontmatter.split(items.read_note(p))
        bodies.append((Path(p).stem, body.strip()))
    each = max(800, limit // max(1, len(bodies)))
    total = sum(len(b) for _t, b in bodies)
    shown = [(t, b if total <= limit else b[:each]) for t, b in bodies]
    return "\n\n".join(f"### {t}\n{b}" for t, b in shown), "\n".join(b for _t, b in bodies)


def _lede(path: str) -> str:
    from silica.kernel.learn import items
    from silica.kernel.write import frontmatter

    _d, _r, body = frontmatter.split(items.read_note(path))
    text = re.sub(r"<!--.*?-->|!\[\[[^\]]*\]\]|^#+\s.*$|\[\^[^\]]+\]", " ", body, flags=re.M | re.S)
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


def write_section(sec: dict, paths_by_title: dict[str, str], language: str, ask=None) -> dict:
    """A section's parts from one call, checked: {title, notes, needed_by,
    guess, definition, mechanism, example, contrast, own, marked, highlighted}."""
    ask = ask or _ask
    paths = [paths_by_title[n] for n in sec["notes"]]
    block, held = _notes_block(paths)
    needs = f' It comes after: {", ".join(sec["needs"])}.' if sec.get("needs") else ""
    f = parse_fields(ask(SECTION_PROMPT.format(notes=block, title=sec["title"], needs=needs, language=language),
                         SECTION_MAX_TOKENS))
    definition, m1 = mark_unheld(f.get("DEFINITION", ""), held)
    mechanism, m2 = mark_unheld(f.get("MECHANISM", ""), held)
    contrast = "" if _NONE_RE.match(f.get("CONTRAST", "") or "none") else f.get("CONTRAST", "")
    contrast, m3 = mark_unheld(contrast, held)
    answer = f.get("ANSWER", "")
    definition, hit = highlight_answer(definition, answer)
    if not hit:
        mechanism, hit = highlight_answer(mechanism, answer)
    return {"title": sec["title"], "notes": sec["notes"], "needed_by": sec.get("needed_by") or [],
            "guess": f.get("GUESS", "").strip(), "definition": definition, "mechanism": mechanism,
            "example": f.get("EXAMPLE", "").strip(), "contrast": contrast, "own": f.get("OWN", "").strip(),
            "marked": m1 + m2 + m3, "highlighted": hit}


# ------------------------------------------------------------------ the note

def _quote(text: str) -> str:
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.strip().splitlines())


def render(label: str, plan_stem: str, step: int, sections: list[dict], order: str) -> str:
    """The reading's body. Labels are English, as every string Silica writes
    into a vault; the parts are in the notes' language."""
    n_notes = sum(len(s["notes"]) for s in sections)
    how = ("grouped by concept, each section after the ones it needs" if order == "prerequisites"
           else "grouped by concept, in the lectures' order")
    lines = [f"{len(sections)} sections from {n_notes} notes of step {step + 1} of [[{plan_stem}]], {how}.", ""]
    for i, s in enumerate(sections, 1):
        lines += [f"## {i}. {s['title']}", "", "Notes: " + ", ".join(f"[[{n}]]" for n in s["notes"])]
        if s.get("needed_by"):
            lines.append("Moved up: needed by " + ", ".join(f"[[{n}]]" for n in s["needed_by"]) + ".")
        lines.append("")
        if s.get("guess"):
            lines += ["> [!question] Before you read", _quote(s["guess"]), ""]
        for name, key in (("Definition", "definition"), ("Mechanism", "mechanism")):
            if s.get(key):
                lines += [f"**{name}.** {s[key]}", ""]
        if s.get("example"):
            lines += [f"**Example** (worked by Silica, not from the notes). {s['example']}", ""]
        if s.get("contrast"):
            lines += [f"**Do not confuse.** {s['contrast']}", ""]
        if s.get("own"):
            lines += ["> [!example] In your own words", _quote(s["own"]), ""]
    return "\n".join(lines).rstrip() + "\n"


_SEC_RE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")
_PART_RE = re.compile(r"^\*\*(Definition|Mechanism|Example|Do not confuse)\.?\*\*(?:\s*\(worked by Silica, not from the notes\))?\.?\s*(.*)$")
_CALLOUT_RE = re.compile(r"^>\s*\[!(question|example)\][^\n]*$", re.I)


def parse(text: str) -> list[dict]:
    """The sections of a reading note, read back from its Markdown; a hand
    edit keeps working as long as the `## N. title` headings stay."""
    from silica.kernel.write import frontmatter

    _d, _r, body = frontmatter.split(text or "")
    secs: list[dict] = []
    cur = None
    part = None
    for line in body.splitlines():
        m = _SEC_RE.match(line)
        if m:
            cur = {"n": int(m.group(1)), "title": m.group(2).strip(), "notes": [], "needed_by": [], "guess": "",
                   "definition": "", "mechanism": "", "example": "", "contrast": "", "own": ""}
            secs.append(cur)
            part = None
            continue
        if cur is None:
            continue
        if line.startswith("Notes:"):
            cur["notes"] = _WIKI_RE.findall(line)
            part = None
            continue
        if line.startswith("Moved up:"):
            cur["needed_by"] = _WIKI_RE.findall(line)
            continue
        c = _CALLOUT_RE.match(line)
        if c:
            part = "guess" if c.group(1).lower() == "question" else "own"
            continue
        if part in ("guess", "own") and line.startswith(">"):
            cur[part] = (cur[part] + "\n" + line[1:].strip()).strip()
            continue
        p = _PART_RE.match(line)
        if p:
            part = {"Definition": "definition", "Mechanism": "mechanism", "Example": "example",
                    "Do not confuse": "contrast"}[p.group(1)]
            cur[part] = p.group(2).strip()
            continue
        # a blank line inside a part keeps it open until the next label: a
        # derivation's steps are written one a line
        if part in ("definition", "mechanism", "example", "contrast") and line.strip():
            cur[part] = (cur[part] + "\n" + line.rstrip()).strip()
    return secs


def minutes(sections: list[dict]) -> int:
    words = sum(len(re.findall(r"\w+", " ".join(str(s.get(k, "")) for k in
                                                ("definition", "mechanism", "example", "contrast")))) for s in sections)
    return max(1, round(words / WORDS_PER_MINUTE))


def write_reading(plan_path: str, step: int, ask=None, progress=None) -> dict:
    """Write (or rewrite) the reading of a step: {path, sections, order, marked}.
    `ask(prompt, max_tokens)` is the model call (tests pass their own);
    `progress(done, total)` hears each finished section."""
    from silica.kernel.learn import plans

    plan = plans.find_plan(plan_path)
    if plan is None:
        raise ValueError(f"no plan at {plan_path!r}")
    notes = step_notes(plan, step)
    if not notes:
        raise ValueError(f"step {step + 1} has no note in the vault to explain")
    ask = ask or _ask
    titles = [t for t, _p in notes]
    by_title = {t: p for t, p in notes}
    language = _language([p for _t, p in notes])
    listing = "\n".join(f"- {t}: {_lede(p)}" for t, p in notes)
    try:
        raw = parse_sections(ask(ORDER_PROMPT.format(notes=listing, language=language), ORDER_MAX_TOKENS))
    except Exception:
        # A failed ordering call is not a failed reading: one section a note,
        # in the lectures' order, is the reading without the grouping.
        raw = []
    ordered = order_sections(titles, raw)
    if not raw:
        ordered["order"] = "lecture"
    total = len(ordered["sections"])
    done = [0]
    lock = threading.Lock()

    def one(sec):
        out = write_section(sec, by_title, language, ask)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], total)
        return out

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        sections = list(pool.map(one, ordered["sections"]))
    label = plans._step_label(plan["steps"][step])
    body = render(label, Path(plan["path"]).stem, step, sections, ordered["order"])
    path = reading_path(plan, step)
    # `written` scopes the reader's words to this version: a rewrite regroups
    # the sections, and a guess given to the old section 3 is not the new one's
    props = {"type": "reading", "plan": f"[[{Path(plan['path']).stem}]]", "step": str(step + 1),
             "notes_hash": notes_hash([p for _t, p in notes]), "order": ordered["order"],
             "written": str(int(time.time()))}
    _save(path, label, body, props)
    return {"path": path, "sections": len(sections), "order": ordered["order"],
            "marked": sum(s["marked"] for s in sections)}


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


# ------------------------------------------------------------------ the reader's words

def _log() -> Path:
    from silica.config import CONFIG
    from silica.kernel.recall import paths

    return paths.index_dir_for(CONFIG.vault_path) / LOG_NAME


def record(reading: str, section: int, kind: str, text: str, unknown: bool = False) -> dict:
    """One guess or one own-words explanation, appended; never graded."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    row = {"t": time.time(), "reading": reading, "section": int(section), "kind": kind,
           "text": str(text or "")[:4000], "unknown": bool(unknown)}
    p = _log()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def answers(reading: str, since: float = 0.0) -> dict[int, dict]:
    """{section: {"guess": row, "own": row}}, the latest of each, given after `since`."""
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
    sections, whether the notes changed since, the reader's words, the
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
           "notes": [p for _t, p in notes], "path": path, "exists": False, "sections": [],
           "stale": False, "answers": {}, "minutes": 0, "questions": session_size(len(notes)),
           "job": job_status(plan["path"], step)}
    text = items.read_note(path) if (plans._vault() / path).is_file() else ""
    if text:
        data, _r, _b = frontmatter.split(text)
        secs = parse(text)
        since = float(str((data or {}).get("written") or "0")) if str((data or {}).get("written") or "0").isdigit() else 0.0
        out.update(exists=True, sections=secs, minutes=minutes(secs), order=str((data or {}).get("order") or ""),
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
