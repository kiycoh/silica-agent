# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A long document written from the vault (ADR-0158 points 2, 3 and 11): the
person picks a type, its parameters and a set of folders or workspaces; code
runs the stages, the model only fills them.

- **Read** (point 2). One tool-less call per part of every chosen note, a part
  at most 16k tokens, `subagent_max_concurrent` at a time. Each reader sees
  the type and parameters verbatim and returns the passages that bear on them,
  verbatim, with one line of why. Code keeps only the passages it finds in the
  text, names each `P<n>` and computes its anchor (the heading above it, or
  the PDF page) from where it was found. The digests go to a sidecar under
  `silica_home()`, keyed by path and content hash, so a replan reads only
  what is new or changed.
- **Plan**. One call over the digest index writes the outline, a word budget
  and the passages of each section, and the gaps no passage covers. The person
  edits it in the page; nothing is written before they send it back.
- **Write**. Sections in order, one call each (a short type in one call), each
  seeing the outline, its passages verbatim, the type and parameters verbatim,
  the register of names, dates and numbers stated so far and the last
  paragraph. A `[P17]` becomes a footnote that quotes the passage; an id the
  call was not shown is dropped.
- **Deliver**. A new note under `Documents/`, stamped `written_from: vault` so
  it is never read back as a source (point 11).

The writer never reads the web: when the plan finds the vault lacking it lists
the gaps, the person runs a deep research and nucleates it, and a replan reads
what arrived (point 11). A prototype: point 7's gate has not run, so nothing
here claims a measured quality.
"""
from __future__ import annotations

import datetime
import hashlib
import itertools
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from silica.config import CONFIG, silica_home
from silica.kernel import job_records
from silica.kernel.text.text import squash
from silica.model.limits import ROUGH_CHARS_PER_TOKEN, TEXT_CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

JOB_KIND = "document"
# The narration stream forwards it to the browser as `event: document`.
TOPIC = "document/update"
DOCUMENTS_DIR = "Documents"

# Open 1's seven types. Each fixes what is stable (point 3): how its sections
# are built and a default length. Under SHORT_WORDS a type reads, plans and
# writes once, an inference of point 3 and not a result.
SHORT_WORDS = 3000
TYPES: dict[str, dict[str, Any]] = {
    "book": {
        "label": "Book", "words": 20000,
        "schema": "Chapters in reading order, each one step of a single argument or story: the first sets "
                  "out the question or the world, the last closes it.",
    },
    "screenplay": {
        "label": "Screenplay", "words": 15000,
        "schema": "Sequences in story order, each a run of scenes with one dramatic goal, through setup, "
                  "confrontation and resolution. Every event and every line of dialogue rests on the notes.",
        # Story first, format second (point 3, 2510.23163): the writer tells
        # each sequence as prose, then one more call lays it out as a script.
        "format": True,
    },
    "documentary_script": {
        "label": "Documentary script", "words": 6000,
        "schema": "Segments in viewing order, each a narration block with the footage or interviews it "
                  "calls for as [bracketed directions]: an opening hook, the story in its order, a close "
                  "that returns to the hook.",
    },
    "investigation": {
        "label": "Investigation", "words": 8000,
        "schema": "The findings first, then the background, the chronology, one section per line of "
                  "inquiry, what remains unproven, and the limits of the sources. Every claim keeps who "
                  "asserts it and its status.",
    },
    "decision_record": {
        "label": "Decision record", "words": 1500,
        "schema": "Context, the decision, the options considered with what each costs, the consequences, "
                  "and what would reopen it.",
    },
    "action_plan": {
        "label": "Action plan", "words": 2000,
        "schema": "The goal, where things stand, the actions in order with who, when and what each needs, "
                  "the risks, and how progress is checked.",
    },
    "speech": {
        "label": "Speech", "words": 1500,
        "schema": "An opening that names the occasion and the point, three or four movements that build "
                  "it, and a close the audience can repeat. Written to be heard: short sentences.",
    },
}

# shortcut: 16k tokens a read and 1,500 a digest are ADR-0158 Open 5's proposed
# values (LOCA's DeepSeek-V3.2 holds 80.0 at 16K and drops to 61.3 at 32K), not
# a probe on the hosted model; replace both when Open 5 is answered. A part is
# cut at TEXT_CHARS_PER_TOKEN, the count that never comes in under a request,
# and a digest at ROUGH_CHARS_PER_TOKEN, since nothing clamps on it.
PART_CHARS = 16000 * TEXT_CHARS_PER_TOKEN
DIGEST_CHARS = 1500 * ROUGH_CHARS_PER_TOKEN
# A passage shorter than this is a name or a phrase, verbatim but nothing a
# sentence can stand on.
MIN_QUOTE_WORDS = 3
WHY_CHARS = 200
# The person's caps, checked where the spend happens.
MAX_SECTIONS = 30
MAX_SECTION_WORDS = 8000
MIN_LENGTH = 100
# shortcut: at most 200 files a document and a plan index of at most 20k
# tokens (40,000 characters), past which each note keeps its first passages in
# turn; both keep the plan call inside the 64k cap of the hosted chat
# (2026-10-22). Raise them with Open 5's probe.
MAX_FILES = 200
PLAN_INDEX_CHARS = 20000 * TEXT_CHARS_PER_TOKEN
# shortcut: the register stops growing at 8,000 characters (4k tokens) and
# keeps the earliest entries, the ones the rest of the text builds on; a book
# of 30 chapters may state more. Revisit with point 7's run on the 20,000-word
# book type.
REGISTER_CHARS = 8000
PARAM_CHARS = {"audience": 300, "register": 300, "language": 100, "focus": 2000}
STOPPED = "stopped: what was read is kept, and the outline if there was one"

_STOPS: dict[str, threading.Event] = {}


class Refused(ValueError):
    """A document action turned away. The text is the sentence the person reads."""


def types() -> dict[str, dict]:
    """What the form offers: each type's label, whether it is short, and its
    default length in words."""
    return {k: {"label": t["label"], "short": t["words"] < SHORT_WORDS, "words": t["words"]}
            for k, t in TYPES.items()}


# --- the selection ---------------------------------------------------------------


def _clean_selection(selection: Any) -> dict:
    """The page sent it: a folder that climbs out of the vault or into a
    dot-folder is refused before anything walks it."""
    sel = selection if isinstance(selection, dict) else {}
    folders = list(dict.fromkeys(f.replace("\\", "/").strip("/") for f in sel.get("folders") or []
                                 if isinstance(f, str)))[:100]
    spaces = list(dict.fromkeys(w.strip() for w in sel.get("workspaces") or []
                                if isinstance(w, str) and w.strip()))[:100]
    for f in folders:
        if Path(f).is_absolute() or any(part == ".." or part.startswith(".") for part in f.split("/")):
            raise Refused("a folder in the selection is not one of this vault's")
    if not folders and not spaces:
        raise Refused("pick at least one folder or workspace")
    return {"folders": folders, "workspaces": spaces}


def resolve(sel: dict) -> list[str]:
    """The vault paths Read reads for a cleaned selection: the notes of its
    folders and workspaces, and the PDFs of its folders that no chosen note
    already holds the text of. Silica's own folders (the Inbox, sources/,
    Plans, Workspaces, the archive, templates), dot-folders, a mirror's
    staged copies, the notes that are not material (a workspace, a plan) and
    the documents this module wrote are never read."""
    from silica.kernel import workspaces
    from silica.kernel.base import note_index
    from silica.kernel.base.layout import is_inbox_path, is_source_leaf
    from silica.kernel.base.noteshape import NOT_MEMBERS, type_word
    from silica.kernel.publish import never, never_dirs
    from silica.kernel.raws import index as raws_index
    from silica.kernel.recall.listing import notes_under, unconverted_under

    notes: list[str] = []
    pdfs: list[str] = []
    for folder in sel["folders"]:
        notes += notes_under(folder)
        pdfs += [p for p in unconverted_under(folder) if p.lower().endswith(".pdf")]
    for ref in sel["workspaces"]:
        ws = workspaces.find_workspace(ref)
        if ws is None:
            raise Refused("a workspace in the selection is no longer in this vault")
        notes += list(workspaces.members(ws))
    vault = Path(CONFIG.vault_path)
    fms = note_index.frontmatters(vault)
    dirs = never_dirs()

    def material(p: str) -> bool:
        return not (is_inbox_path(p) or is_source_leaf(p) or never(p, dirs))

    notes = [p for p in dict.fromkeys(notes) if p.endswith(".md") and material(p)]
    staged = workspaces._mirror_copies(notes)
    notes = [p for p in notes if p not in staged and type_word(fms.get(p)) not in NOT_MEMBERS
             and (fms.get(p) or {}).get("written_from") != "vault"]
    chosen = set(notes)
    named = raws_index(vault) if pdfs else {}
    pdfs = [p for p in dict.fromkeys(pdfs) if material(p)
            and str(Path(p).with_suffix(".md")) not in chosen and not chosen & set(named.get(p, []))]
    return notes + pdfs


def _file_text(path: str) -> tuple[str, int, str]:
    """(text, pages, why not): a note's body, or a PDF's text with a
    `<!-- p. N -->` line ahead of each page (ADR-0108's anchor), and the
    sentence saying why a file gives no text."""
    if path.lower().endswith(".pdf"):
        return _pdf_text(path)
    from silica.driver import DRIVER
    from silica.kernel.base import frontmatter

    try:
        _data, _raw, body = frontmatter.split(DRIVER.read_note(path).content or "")
    except Exception:
        # Tolerated: one unreadable note is listed as skipped on the record and
        # the rest of the selection is still read.
        logger.debug("document: %s not read", path, exc_info=True)
        return "", 0, "it could not be read"
    body = body or ""
    return (body, 0, "") if body.strip() else ("", 0, "it is empty")


def _pdf_text(path: str) -> tuple[str, int, str]:
    """The PDF's text as the passage index holds it, extracted once and kept
    on disk, in the limited child on the server (`recall_engine.pdf_text`)."""
    from silica.kernel.recall import engine as recall_engine
    from silica.sources import pdf_extract  # noqa: F401  hands the engine its PDF reader

    try:
        recall_engine.engine()  # the engine's root is this vault before its cache is asked
        text, pages, reason = recall_engine.pdf_text(path, Path(CONFIG.vault_path) / path)
    except Exception:
        # Tolerated as for a note: the PDF is listed as skipped, the rest is read.
        logger.debug("document: %s not read", path, exc_info=True)
        return "", 0, "its text could not be read"
    if not text or not pages:
        return "", 0, f"no text could be read from it ({reason or 'no text layer'})"
    bounds = [p["offset"] for p in pages] + [len(text)]
    body = "".join(f"<!-- p. {p['page']} -->\n{text[p['offset']:bounds[i + 1]]}" for i, p in enumerate(pages))
    return body, len(pages), ""


def count(selection: Any) -> dict:
    """What Read would read of a selection, for the form before it starts."""
    files = resolve(_clean_selection(selection))
    out = {"notes": 0, "pdfs": 0, "pages": 0, "words": 0, "skipped": 0}
    for path in files:
        text, pages, why = _file_text(path)
        if why:
            out["skipped"] += 1
            continue
        out["pdfs" if pages else "notes"] += 1
        out["pages"] += pages
        out["words"] += len(text.split()) - 4 * pages  # each page anchor is four words
    return out


# --- read (point 2) ---------------------------------------------------------------

_READ_SYSTEM = """You read one part of a note from a person's own vault for a \
document they are writing from their notes. You get the document's type, the \
person's parameters exactly as they set them, and the text of the part.

Reply with JSON only, in this form:
{"passages": [{"quote": "...", "why": "..."}]}

- `quote` is a passage that bears on the document, copied exactly as the text \
shows it: the same words in the same order, no ellipsis, no paraphrase, no \
translation. A sentence or a short paragraph, never a whole page.
- `why` is one line on what the passage gives the document.
- At most about 1,500 tokens in all. Prefer the facts, names, dates, numbers, \
definitions, arguments and quotations a document could cite.
- {"passages": []} when nothing in the part bears on the document."""


def _parts(text: str) -> list[str]:
    """`text` cut into parts of at most PART_CHARS, at a blank line where one
    falls in the second half of the part, so a page or paragraph stays whole."""
    out: list[str] = []
    while len(text) > PART_CHARS:
        cut = text.rfind("\n\n", 0, PART_CHARS)
        cut = cut if cut > PART_CHARS // 2 else PART_CHARS
        out.append(text[:cut])
        text = text[cut:]
    return [*out, text] if text.strip() else out


def _brief(rec: dict) -> str:
    """The type and the person's parameters exactly as they set them: every
    stage gets them whole, never summarised (point 3: staging cost GPT-5.2 27%
    of its constraint score, compaction kept 17%)."""
    t = TYPES[rec["type"]]
    return (f"Document type: {t['label']}\nHow it is built: {t['schema']}\n"
            f"Parameters: {json.dumps(rec['params'], ensure_ascii=False)}")


def _read_part(rec: dict, path: str, i: int, n: int, part: str) -> list[tuple[str, str]]:
    """One reader: (quote, why) for each passage found verbatim in `part`,
    cut at DIGEST_CHARS. The containment is `squash`'s, the web bank's rule:
    whitespace and quote style may differ, the words may not."""
    from silica.model.reply import decode_json

    where = f"{Path(path).name}" + (f", part {i + 1} of {n}" if n > 1 else "")
    raw = _ask([{"role": "system", "content": _READ_SYSTEM},
                {"role": "user", "content": f"{_brief(rec)}\n\nThe note: {where}\n\n{part}"}])
    try:
        data, _clean = decode_json(raw or "")
    except ValueError:
        return []  # a reply that is not JSON found nothing this code can check
    items = data.get("passages") if isinstance(data, dict) else None
    hay = squash(part)
    out: list[tuple[str, str]] = []
    size = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        why = " ".join(str(item.get("why") or "").split())[:WHY_CHARS]
        flat = squash(quote)
        if len(flat.split()) < MIN_QUOTE_WORDS or flat not in hay:
            continue
        size += len(quote) + len(why)
        if size > DIGEST_CHARS:
            break
        out.append((quote, why))
    return out


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_PAGE_RE = re.compile(r"^<!-- p\. (\d+) -->$")


def _anchor(text: str, quote: str) -> str:
    """Where `quote` sits in `text`, from where code found it and never from
    the reader's word: `p. N` in a PDF, `§ Heading` under a note's heading,
    "" above the first. The text is squashed line by line, which joins to the
    same string as squashing it whole, so a line's offset there is exact."""
    flat, marks = "", [(0, "")]
    for line in text.splitlines():
        s = squash(line)
        if not s:
            continue
        flat += " " if flat else ""
        stripped = line.strip()
        if m := _PAGE_RE.match(stripped):
            marks.append((len(flat), f"p. {m.group(1)}"))
        elif m := _HEADING_RE.match(stripped):
            marks.append((len(flat), f"§ {m.group(1)}"))
        flat += s
    at = flat.find(squash(quote))
    return next((label for off, label in reversed(marks) if off <= at), "")


def _side_path(job_id: str) -> Path:
    return silica_home() / "documents" / f"{job_id}.json"


def _side(job_id: str) -> dict:
    """The job's digests: `passages` by id, `read` {path: {hash, ids}}, and
    `next`, the id counter, which never reuses a number so an outline's ids
    keep naming the passage they named."""
    try:
        return json.loads(_side_path(job_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"next": 1, "read": {}, "passages": {}}


def _save_side(job_id: str, side: dict) -> None:
    from silica.kernel.base.durable import atomic_write_bytes

    path = _side_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(side, ensure_ascii=False).encode("utf-8"), mode=0o600)


def _read(rec: dict, stop: threading.Event, update: Callable[..., None]) -> None:
    """Point 2's fan-out over every file of the selection not yet read or
    changed since: one tool-less call a part, never a loop, the parts in a
    pool `subagent_max_concurrent` wide. The workers never see each other;
    this thread alone checks and numbers what they return."""
    from silica.model import llm
    from silica.model.usage_limits import UsageLimited

    side = _side(rec["id"])
    files = resolve(rec["selection"])
    skipped: list[dict] = []
    todo: dict[str, tuple[str, str]] = {}
    readable: set[str] = set()
    for path in files:
        text, _pages, why = _file_text(path)
        if why:
            skipped.append({"path": path, "why": why})
            continue
        readable.add(path)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if side["read"].get(path, {}).get("hash") != digest:
            todo[path] = (text, digest)
    work = [(path, i, len(chunks), part) for path, (text, _d) in todo.items()
            for chunks in [_parts(text)] for i, part in enumerate(chunks)]
    update(stage="read", skipped=skipped, progress={"done": 0, "total": len(work)})
    limited: list[UsageLimited] = []

    def one(item: tuple[str, int, int, str]) -> list[tuple[str, str]] | None:
        path, i, n, part = item
        if stop.is_set():
            return None
        try:
            # entered here, in the pool's thread: a scope does not cross threads (ADR-0135 point 8)
            with llm.meter_scope(rec["meter"]):
                got = _read_part(rec, path, i, n, part)
        except UsageLimited as exc:
            # Held until the passages already paid for are saved, then raised.
            limited.append(exc)
            return None
        except Exception:
            # Tolerated: one part whose call failed leaves its file unread, named
            # on the record, and a replan reads it again; the others stand.
            logger.warning("document %s: a read of %s failed", rec["id"], path, exc_info=True)
            return None
        with job_records.LOCK:
            rec["progress"]["done"] += 1
            _write(rec)
        return got

    with ThreadPoolExecutor(max_workers=max(1, CONFIG.subagent_max_concurrent)) as pool:
        results = list(pool.map(one, work))
    found: dict[str, list[tuple[str, str]]] = {}
    failed: set[str] = set()
    for (path, *_rest), got in zip(work, results):
        if got is None:
            failed.add(path)
        else:
            found.setdefault(path, []).extend(got)
    for path, (text, digest) in todo.items():
        if path in failed:
            skipped.append({"path": path, "why": "its reading failed or was stopped; a replan reads it again"})
            continue
        for pid in side["read"].get(path, {}).get("ids", []):
            side["passages"].pop(pid, None)
        ids: list[str] = []
        seen: set[str] = set()
        for quote, why in found.get(path, []):
            if squash(quote) in seen:
                continue
            seen.add(squash(quote))
            pid = f"P{side['next']}"
            side["next"] += 1
            side["passages"][pid] = {"note": path, "anchor": _anchor(text, quote), "quote": quote, "why": why}
            ids.append(pid)
        side["read"][path] = {"hash": digest, "ids": ids}
    # A file that left the selection, or has no text now, takes its passages
    # out of the plan.
    for path in [p for p in side["read"] if p not in readable]:
        for pid in side["read"].pop(path)["ids"]:
            side["passages"].pop(pid, None)
    _save_side(rec["id"], side)
    update(skipped=skipped)
    if limited:
        raise limited[0]


# --- plan -------------------------------------------------------------------------

_PLAN_SYSTEM = """You plan a document a person is writing from their own \
notes. You get its type, how the type is built, the person's parameters \
exactly as they set them, the total length in words, and an index of the \
passages read from the notes they chose (id | note | why).

Reply with JSON only, in this form:
{"title": "...", "sections": [{"title": "...", "brief": "...", "words": 1200, \
"passages": ["P1", "P4"]}], "gaps": ["..."]}

- The sections in the order the document reads, at most 30. `brief` is one or \
two sentences on what the section does.
- `words` is the section's budget; the budgets add up to the total length.
- `passages` names the passages the section stands on, by id, from the index \
only. A passage may serve more than one section.
- `gaps` lists what the document needs that no passage covers, each a short \
phrase; [] when the passages cover it.
- Write the title, briefs and gaps in the language the parameters ask for, or \
in the notes' language when they name none."""


def _section(title: str, brief: str, words: int, ids: list[str], passages: dict) -> dict:
    """A section as the record carries it: `notes` and `thin` are computed
    here from the digests, never taken from the model or the page."""
    return {"title": title, "brief": brief, "words": words, "passages": ids,
            "notes": list(dict.fromkeys(passages[i]["note"] for i in ids)), "thin": len(ids) < 2}


def _index(passages: dict) -> tuple[str, int]:
    """(the plan's index, how many passages it leaves out): every note's
    passages taken in turn, so a cut at PLAN_INDEX_CHARS costs each note its
    last passages rather than the last notes all of theirs."""
    by_note: dict[str, list[str]] = {}
    for pid, p in passages.items():
        by_note.setdefault(p["note"], []).append(f"{pid} | {p['note']} | {p['why'][:120]}")
    lines: list[str] = []
    size = 0
    for row in itertools.zip_longest(*by_note.values()):
        for line in row:
            if line and size + len(line) < PLAN_INDEX_CHARS:
                lines.append(line)
                size += len(line) + 1
    return "\n".join(lines), len(passages) - len(lines)


def _plan(rec: dict) -> dict:
    """The outline: one call over the index; code keeps only the ids that
    exist, trims the plan to the caps, and computes notes and thin."""
    from silica.model.reply import decode_json

    passages = _side(rec["id"])["passages"]
    if not passages:
        raise Refused("no passage of the chosen notes bears on this document: change the focus or the notes")
    length = rec["params"]["length"]
    index, left_out = _index(passages)
    raw = _ask([{"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": f"{_brief(rec)}\nTotal length: {length} words\n\n"
                                            f"Passages:\n{index}"}])
    try:
        data, _clean = decode_json(raw or "")
    except ValueError:
        data = None  # a reply that is not JSON is a plan that could not be written
    rows = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise Refused("the plan could not be written: plan again")
    sections: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or len(sections) == MAX_SECTIONS:
            continue
        title = " ".join(str(row.get("title") or "").split())[:200]
        if not title:
            continue
        ids = [i for i in dict.fromkeys(str(x).strip() for x in row.get("passages") or []) if i in passages] \
            if isinstance(row.get("passages"), list) else []
        try:
            words = max(0, int(row.get("words") or 0))
        except (TypeError, ValueError):
            words = 0
        sections.append(_section(title, " ".join(str(row.get("brief") or "").split())[:1000],
                                 words, ids, passages))
    if not sections:
        raise Refused("the plan came back without sections: plan again")
    # A budget the model left out is an even share; past the length the
    # budgets are cut back in proportion, since the page's write would refuse
    # them at the caps.
    for s in sections:
        s["words"] = s["words"] or length // len(sections)
    total = sum(s["words"] for s in sections)
    for s in sections:
        s["words"] = min(MAX_SECTION_WORDS, max(1, s["words"] * length // total if total > length else s["words"]))
    gaps = data.get("gaps") if isinstance(data, dict) else None
    title = " ".join(str(data.get("title") or "").split())[:200] or TYPES[rec["type"]]["label"]
    return {"title": title, "sections": sections, "left_out": left_out,
            "gaps": [" ".join(str(g).split())[:300] for g in gaps if str(g).strip()][:20]
            if isinstance(gaps, list) else []}


# --- write ------------------------------------------------------------------------

_WRITE_SYSTEM = """You write part of a document a person is writing from \
their own notes. You get its type, how the type is built, the person's \
parameters exactly as they set them, the outline, the section or sections to \
write now with their word budgets, the passages from the notes they stand on, \
the register of names, dates and numbers the document has already stated, and \
the last paragraph written before this part.

- Write each section under its heading exactly as given, `## Title`, in order.
- State only what the passages support. Cite the passage a sentence stands on \
right after it, as [P3] or [P3, P7], with only the ids given here. A sentence \
that only joins or introduces states no fact and needs no citation.
- Never contradict the register: use its names, dates and numbers as written.
- Carry on from the last paragraph without repeating it.
- Keep near the word budget. Write no sources list and no footnotes: they are \
added for you.
- After the text, write a line `REGISTER:` and under it one line `- ` for each \
name, date or number this part states, as it states it."""

_FORMAT_SYSTEM = """You lay out one sequence of a screenplay. You get it told \
as story prose, with citations like [P3]. Rewrite it in screenplay format: \
scene headings (INT. or EXT., the place, the time), action lines, and each \
character's name in capitals above their dialogue. Keep the `## ` heading as \
it is, keep every event in the order told and add none, and keep each \
citation on the line that tells what it supports. Reply with the sequence \
only."""

_REGISTER_RE = re.compile(r"(?im)^\s*\**\s*REGISTER\s*:?\s*\**\s*$")
# One [P3] or [P3, P7] marker with the whitespace that leads it, so a dropped
# one takes its space along (web_research's _QREF_RE); a `^` the writer added
# itself is read the same way, or a phantom [^P99] would slip through as is.
_PREF_RE = re.compile(r"(\s*)\[\^?(P\d+(?:\s*,\s*\^?P\d+)*)\]")


def _split_register(raw: str) -> tuple[str, list[str]]:
    """(the prose, the register entries of its trailer)."""
    marks = list(_REGISTER_RE.finditer(raw))
    if not marks:
        return raw.strip(), []
    tail = raw[marks[-1].end():]
    items = [" ".join(line.strip()[1:].split())[:200] for line in tail.splitlines()
             if line.strip().startswith(("-", "*")) and line.strip()[1:].strip()]
    return raw[:marks[-1].start()].strip(), items


def _merge(register: list[str], items: list[str]) -> list[str]:
    have = {r.casefold() for r in register}
    size = sum(len(r) + 1 for r in register)
    out = list(register)
    for item in items:
        if item.casefold() in have or size + len(item) > REGISTER_CHARS:
            continue
        have.add(item.casefold())
        size += len(item) + 1
        out.append(item)
    return out


def _cite(text: str, shown: set[str], used: list[str]) -> str:
    """Each `[P17]` the call was shown becomes `[^P17]`; any other id is
    dropped, whether it exists or not: a section cites only what it read."""
    def one(m: re.Match) -> str:
        ids = [i for i in dict.fromkeys(x.strip().lstrip("^") for x in m.group(2).split(",")) if i in shown]
        used.extend(i for i in ids if i not in used)
        return m.group(1) + "".join(f"[^{i}]" for i in ids) if ids else ""

    return _PREF_RE.sub(one, text)


def _last_paragraph(text: str) -> str:
    blocks = [b.strip() for b in text.split("\n\n") if b.strip() and not b.strip().startswith("#")]
    return blocks[-1] if blocks else ""


def _write_messages(rec: dict, batch: list[dict], passages: dict, register: list[str], last: str) -> list[dict]:
    outline = "\n".join(f"{n}. {s['title']} ({s['words']} words): {s['brief']}"
                        for n, s in enumerate(rec["outline"]["sections"], 1))
    now = []
    for s in batch:
        quotes = "\n\n".join(f"[{i}] {_link(passages[i]['note'])}"
                             f"{', ' + passages[i]['anchor'] if passages[i]['anchor'] else ''}\n> "
                             + "\n> ".join(passages[i]["quote"].splitlines())
                             for i in s["passages"]) or "(no passage: write no fact here)"
        now.append(f"## {s['title']}\nBudget: {s['words']} words\nBrief: {s['brief']}\n\nPassages:\n{quotes}")
    user = (f"{_brief(rec)}\n\nTitle: {rec['outline']['title']}\nOutline:\n{outline}\n\n"
            "Register so far:\n" + ("\n".join(f"- {r}" for r in register) or "(empty)")
            + f"\n\nLast paragraph before this part:\n{last or '(this part opens the document)'}\n\n"
            "Write now:\n\n" + "\n\n".join(now))
    return [{"role": "system", "content": _WRITE_SYSTEM}, {"role": "user", "content": user}]


def _write_sections(rec: dict, stop: threading.Event, update: Callable[..., None]) -> None:
    """Sections in order, each with the text before it in view (point 3: 6%
    coherence lost in parallel), then the note."""
    passages = _side(rec["id"])["passages"]
    t = TYPES[rec["type"]]
    sections = rec["outline"]["sections"]
    batches = [sections] if t["words"] < SHORT_WORDS else [[s] for s in sections]
    register: list[str] = []
    written: list[str] = []
    used: list[str] = []
    last = ""
    update(stage="write", progress={"done": 0, "total": len(batches)})
    for k, batch in enumerate(batches):
        if stop.is_set():
            raise Refused(STOPPED)
        prose, items = _split_register(_ask(_write_messages(rec, batch, passages, register, last)) or "")
        if t.get("format") and prose:
            prose = (_ask([{"role": "system", "content": _FORMAT_SYSTEM},
                           {"role": "user", "content": prose}]) or "").strip()
        if not prose:
            raise Refused(f"the section {batch[0]['title']!r} came back empty: write again")
        if len(batch) == 1:
            # The heading is code's: an echo of it is dropped, then it is set.
            first, _, rest = prose.partition("\n")
            if re.fullmatch(r"#+\s+" + re.escape(batch[0]["title"]) + r"\s*", first.strip()):
                prose = rest.strip()
            prose = f"## {batch[0]['title']}\n\n{prose}"
        last = _last_paragraph(prose)
        written.append(_cite(prose, {i for s in batch for i in s["passages"]}, used))
        register = _merge(register, items)
        update(progress={"done": k + 1, "total": len(batches)})
    # shortcut: no check runs on the text (ADR-0158 point 3, Check: each claim
    # against the passage it cites, the register against the text); the page
    # offers check_note (kernel/check/run.py) on the delivered note. Build it
    # after point 7's gate, proposed to the owner before 2026-11-25, and audit
    # the checker on 30 hand-checked claims before quoting its rate.
    note = _deliver(rec, written, used, passages)
    update(state="done", stage="", note=note, source={"path": note, "name": rec["outline"]["title"]},
           error="", finished=time.time())


def _link(path: str) -> str:
    return f"[[{Path(path).stem if path.endswith('.md') else Path(path).name}]]"


def _deliver(rec: dict, written: list[str], used: list[str], passages: dict) -> str:
    """The document as a new note (ADR-0116, never a chat answer) at a free
    path under Documents/ inside the write boundary. `written_from: vault` is
    point 11's stamp: a document made of the vault's notes is not a new
    source, and the selection above never reads it back."""
    from silica.driver import DRIVER
    from silica.kernel.base.manifest import in_write_dir
    from silica.kernel.write.templates import slugify

    title = rec["outline"]["title"]
    sources = list(dict.fromkeys(passages[i]["note"] for i in used))
    defs = []
    for i in used:
        p = passages[i]
        # One line each: a footnote definition ends at its first newline.
        quote = " ".join(p["quote"].split())
        defs.append(f"[^{i}]: {_link(p['note'])}{', ' + p['anchor'] if p['anchor'] else ''}: \"{quote}\"")
    head = ["---", f"title: {json.dumps(title, ensure_ascii=False)}", f"document_type: {rec['type']}",
            "written_from: vault"]
    head += ["sources:", *(f"  - {json.dumps(_link(s), ensure_ascii=False)}" for s in sources)] if sources \
        else ["sources: []"]
    head += [f"created: {datetime.date.today().isoformat()}", "---", ""]
    body = "\n\n".join(written) + ("\n\n" + "\n".join(defs) if defs else "")
    folder = in_write_dir(DOCUMENTS_DIR)
    slug = slugify(title)[:100].strip() or "Document"
    vault = Path(CONFIG.vault_path)
    name, n = slug, 2
    while (vault / folder / f"{name}.md").exists():
        name, n = f"{slug} {n}", n + 1
    rel = f"{folder}/{name}.md" if folder else f"{name}.md"
    DRIVER.create(rel, "\n".join(head) + body + "\n")
    return rel


def _ask(messages: list[dict]) -> str:
    from silica.model.llm import call_llm

    return call_llm(CONFIG.model, messages).text or ""


# --- the job ----------------------------------------------------------------------


def _records() -> list[dict]:
    vault = str(CONFIG.vault_path or "")
    return [r for r in job_records.all_records() if r.get("kind") == JOB_KIND and r.get("vault") == vault]


def public(rec: dict) -> dict:
    """The record as the browser gets it: without the vault's path on disk."""
    return {k: v for k, v in rec.items() if k != "vault"}


def _write(rec: dict) -> None:
    from silica.obs.bus import BUS

    rec["updated"] = time.time()
    job_records.write(rec)
    BUS.publish(TOPIC, public(rec))


def listing() -> list[dict]:
    """This vault's documents the person has not dismissed, oldest first."""
    return [public(r) for r in _records() if not r.get("dismissed")]


def read(job_id: str) -> dict | None:
    if not job_records.is_id(job_id):
        return None
    rec = job_records.read(job_records.path_of(job_id))
    return rec if rec and rec.get("kind") == JOB_KIND else None


def _idle() -> None:
    """One document at a time, reading, planning or writing: the readers'
    pool is the process's width already. Called under job_records.LOCK."""
    if any(r.get("state") == "running" for r in _records()):
        raise Refused("a document is already being read or written: it ends before another starts")


def _clean_params(params: Any, type_id: str) -> dict:
    p = params if isinstance(params, dict) else {}
    default, cap = TYPES[type_id]["words"], TYPES[type_id]["words"] * 2
    try:
        length = int(p.get("length") or default)
    except (TypeError, ValueError):
        raise Refused("the length is a number of words") from None
    if not MIN_LENGTH <= length <= cap:
        raise Refused(f"a {TYPES[type_id]['label'].lower()} runs from {MIN_LENGTH} to {cap:,} words")
    return {"length": length, **{k: str(p.get(k) or "").strip()[:n] for k, n in PARAM_CHARS.items()}}


def start(type_id: str, selection: Any, params: Any, *, run: Callable | None = None) -> dict:
    """Record the request and read, then plan, on a thread of its own. The job
    waits as `planned` for the person to keep, edit or drop sections."""
    from silica.model import llm

    if type_id not in TYPES:
        raise Refused("pick one of the types")
    p = _clean_params(params, type_id)
    sel = _clean_selection(selection)
    files = resolve(sel)
    if not files:
        raise Refused("the selection holds no note to read")
    if len(files) > MAX_FILES:
        raise Refused(f"a document reads at most {MAX_FILES} notes and files; this selection holds {len(files)}")
    with job_records.LOCK:
        _idle()
        now = time.time()
        rec = {"id": job_records.new_id(), "kind": JOB_KIND, "origin": "document",
               "source": {"path": "", "name": p["focus"][:80] or TYPES[type_id]["label"]},
               "vault": str(CONFIG.vault_path or ""), "state": "running", "stage": "read",
               "created": now, "started": now, "finished": None, "error": "", "dismissed": False,
               "type": type_id, "params": p, "selection": sel, "progress": {"done": 0, "total": 0},
               "outline": {"title": "", "sections": [], "gaps": []}, "note": "", "skipped": [],
               "meter": llm.new_meter()}
        _write(rec)
    _launch(rec, run or _run_plan)
    return public(rec)


def replan(job_id: str, *, run: Callable | None = None) -> dict:
    """Read what is new or changed in the selection, then plan again: what
    runs after a gap was researched and nucleated (point 11)."""
    with job_records.LOCK:
        rec = _stopped_record(job_id)
        _idle()
        rec.update(state="running", stage="read", error="", finished=None, started=time.time())
        _write(rec)
    _launch(rec, run or _run_plan)
    return public(rec)


def write(job_id: str, body: Any, *, run: Callable | None = None) -> dict:
    """Write the outline the person kept. A section left out is dropped; every
    cap is checked again here, where the spend happens, and each section's
    notes and thinness come from the digests, not from the page."""
    body = body if isinstance(body, dict) else {}
    with job_records.LOCK:
        rec = _stopped_record(job_id)
        _idle()
        passages = _side(job_id)["passages"]
        sections = _clean_sections(body.get("sections"), passages, rec["type"])
        title = " ".join(str(body.get("title") or "").split())[:200] or rec["outline"].get("title") \
            or TYPES[rec["type"]]["label"]
        rec.update(state="running", stage="write", error="", finished=None, started=time.time(),
                   outline={**rec["outline"], "title": title, "sections": sections})
        _write(rec)
    _launch(rec, run or _run_write)
    return public(rec)


def _stopped_record(job_id: str) -> dict:
    rec = read(job_id)
    if rec is None or rec.get("vault") != str(CONFIG.vault_path or ""):
        raise Refused("there is no such document")
    if rec.get("state") == "running":
        raise Refused("this document is still being read or written")
    return rec


def _clean_sections(rows: Any, passages: dict, type_id: str) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise Refused("keep at least one section")
    if len(rows) > MAX_SECTIONS:
        raise Refused(f"a document holds at most {MAX_SECTIONS} sections")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise Refused("a section came in a shape the server does not read")
        title = " ".join(str(row.get("title") or "").split())[:200]
        if not title:
            raise Refused("every section needs a title")
        try:
            words = int(row.get("words") or 0)
        except (TypeError, ValueError):
            raise Refused(f"the section {title!r} needs a length in words") from None
        if not 1 <= words <= MAX_SECTION_WORDS:
            raise Refused(f"a section runs from 1 to {MAX_SECTION_WORDS:,} words")
        ids = row.get("passages")
        out.append(_section(title, " ".join(str(row.get("brief") or "").split())[:1000], words,
                            [i for i in dict.fromkeys(str(x) for x in ids) if i in passages]
                            if isinstance(ids, list) else [], passages))
    cap = TYPES[type_id]["words"] * 2
    if sum(s["words"] for s in out) > cap:
        raise Refused(f"a {TYPES[type_id]['label'].lower()} holds at most {cap:,} words in all")
    # Point 11: every sentence the writer states has an anchor in the vault, so
    # an outline with no passage left has nothing to write from.
    if not any(s["passages"] for s in out):
        raise Refused("no section keeps a passage of the notes: keep at least one")
    return out


def stop(job_id: str) -> dict:
    """Stop at the next call: a read keeps what it read, a write keeps the outline."""
    rec = read(job_id)
    if rec is None:
        raise Refused("there is no such document")
    if rec.get("state") != "running":
        raise Refused("this document is not being read or written")
    ev = _STOPS.get(job_id)
    if ev is None:
        raise Refused("this document belongs to another process: it ends there")
    ev.set()
    return public(rec)


def dismiss(job_id: str) -> None:
    """The person is done with the row. The note stays; the digests go."""
    with job_records.LOCK:
        rec = read(job_id)
        if rec is None:
            return
        if rec.get("state") == "running":
            raise Refused("this document is still being read or written: stop it first")
        rec["dismissed"] = True
        _write(rec)
    _side_path(job_id).unlink(missing_ok=True)


def _launch(rec: dict, target: Callable) -> None:
    # An event per run and not per job: a `planned` record outlives a restart,
    # the event does not.
    stop_ev = _STOPS[rec["id"]] = threading.Event()
    threading.Thread(target=target, args=(rec, stop_ev), daemon=True, name=f"document-{rec['id']}").start()


def _guarded(rec: dict, stop_ev: threading.Event, body: Callable[[Callable[..., None]], None]) -> None:
    """Run a stage on the job's thread. A failure lands back on `planned` when
    an outline exists, so the person can write or plan again from it, and on
    `failed` before one does."""
    from silica.model import llm
    from silica.model.usage_limits import UsageLimited

    def update(**fields: Any) -> None:
        with job_records.LOCK:
            rec.update(fields)
            _write(rec)

    def end(error: str) -> None:
        update(state="planned" if rec["outline"]["sections"] else "failed", stage="", error=error,
               finished=time.time())

    try:
        with llm.meter_scope(rec["meter"]):
            body(update)
    except Refused as exc:
        end(exc.args[0])
    except UsageLimited as exc:
        layer = {"window": "5-hour", "week": "weekly", "month": "monthly"}.get(exc.layer, exc.layer)
        end(f"the {layer} usage limit was reached")
    except Exception:
        # The thread has no caller to raise to: the record carries the failure
        # to the page.
        logger.exception("document %s failed", rec["id"])
        end("the document stopped on an error")
    finally:
        _STOPS.pop(rec["id"], None)


def _run_plan(rec: dict, stop_ev: threading.Event) -> None:
    def body(update: Callable[..., None]) -> None:
        _read(rec, stop_ev, update)
        if stop_ev.is_set():
            raise Refused(STOPPED)
        update(stage="plan", progress={"done": 0, "total": 1})
        outline = _plan(rec)
        update(state="planned", stage="", outline=outline, progress={"done": 1, "total": 1},
               source={**rec["source"], "name": outline["title"]}, finished=time.time())

    _guarded(rec, stop_ev, body)


def _run_write(rec: dict, stop_ev: threading.Event) -> None:
    _guarded(rec, stop_ev, lambda update: _write_sections(rec, stop_ev, update))
