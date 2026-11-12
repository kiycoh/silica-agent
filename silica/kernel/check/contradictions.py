# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A contradiction on record, the ones a check proposes, and the four ways a
person settles one (ADR-0166).

The record is what the vault already held before this module: `contested: true`,
one `contradictions:` entry per contest, and for an entry naming a source or a
note a warning callout in the body quoting the other side. Nothing is added
beside the note: a new record is that callout with two more lines (this note's
words and the day it was found), and a settled one stays in the file as text,
so the history is the file's and every reader of `contradictions:` keeps working.

A proposal is a `contradicted` finding a source check left in `.silica/checks/`
(ADR-0110 point 12). It is not written anywhere until a person settles it.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from silica.kernel.base import frontmatter
from silica.kernel.base.noteshape import SUPERSEDED_MARKER, SUPERSEDED_RE, stamp
from silica.kernel.check import blocks as blk
from silica.kernel.write.contested import (
    _CONTRADICTION_START_RE,
    _UNRESOLVED_TAIL,
    CONTESTED_KEY,
    CONTRADICTIONS_KEY,
    append_before_superseded,
    mark_contested,
    ref_source,
    resolve_contested,
)
from silica.kernel.write.moc import merge_moc_section

HOLDS = ("this", "other", "both", "open")
MINE = "This note: "
FOUND = "Found "
KEPT = "Kept open "
FOUND_BY = "by a source check"

_FLAG_RE = re.compile(r"^flagged:\s*(?P<why>.*?)\s*\(by (?P<who>[^,()]+), (?P<date>\d{4}-\d{2}-\d{2})\)\s*$")
_FOUND_RE = re.compile(r"^Found (?P<date>\d{4}-\d{2}-\d{2})\s*(?P<how>.*?)\.?$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LINK_RE = re.compile(r"^\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]$")

_OPEN_MEMO: dict = {}


def link_target(other: str) -> str:
    """The note a record's other side names, "" for a source file: a new record
    names a note as `[[Note]]`, an import's names the source's file name."""
    m = _LINK_RE.match(other.strip())
    return m.group(1).strip() if m else ""


def _callouts(lines: list[str], stop: int) -> list[tuple[int, int, str]]:
    """(first line, line past the last, the `from` it names) of each open-style
    warning callout above `stop`, the line where `## Superseded` starts."""
    out, i = [], 0
    while i < stop:
        m = _CONTRADICTION_START_RE.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < stop and lines[j].startswith(">"):
            j += 1
        out.append((i, j, (m.group("src") or "").strip()))
        i = j
    return out


def _parse(callout: list[str]) -> dict[str, Any]:
    """The other side's words and the meta lines of one callout. The meta is
    what follows the last empty `>` line; an import's callout has only the
    "Conflicts with this note" line there."""
    inner = [re.sub(r"^>\s?", "", ln) for ln in callout[1:]]
    blanks = [k for k, x in enumerate(inner) if not x.strip()]
    cut = blanks[-1] if blanks else len(inner)
    meta = inner[cut + 1:]
    found = next((m for x in meta if (m := _FOUND_RE.match(x))), None)
    return {"their": "\n".join(inner[:cut]).strip(),
            "mine": next((x[len(MINE):].strip() for x in meta if x.startswith(MINE)), ""),
            "found": found.group("date") if found else "",
            "how": found.group("how") if found else "",
            "kept": [d.group(0) for x in meta if x.startswith(KEPT) and (d := _DATE_RE.search(x))]}


def _open_callout(lines: list[str], stop: int, src: str) -> tuple[int, int] | None:
    """The callout of `src` still open, else the first one naming it."""
    named = [(a, b) for a, b, s in _callouts(lines, stop) if s == src]
    still = [c for c in named if _UNRESOLVED_TAIL in lines[c[1] - 1]]
    best = still or named
    return best[0] if best else None


def _split(content: str) -> tuple[dict | None, list[str], int, int]:
    """(frontmatter, the note's lines, the first body line, the `## Superseded` line or the end)."""
    data, _raw, body = frontmatter.split(content or "")
    head = (content or "")[: len(content or "") - len(body)].count("\n")
    lines = (content or "").split("\n")
    m = SUPERSEDED_RE.search(body)
    stop = head + body.count("\n", 0, m.start()) if m else len(lines)
    return data, lines, head, stop


def records(content: str) -> list[dict[str, Any]]:
    """One row per open entry of the note's `contradictions:`: `other` the
    source or `[[note]]` it names ("" for a flag), `their` the other side's
    words (a flag's reason), `mine` this note's words when the record holds
    them, `found` and `how` when it says, `line` the callout's 1-based line
    (0 for a flag, which has no callout), `kept` the days it was kept open."""
    data, lines, head, stop = _split(content)
    if not data or not data.get(CONTESTED_KEY):
        return []
    out = []
    for ref in [str(r) for r in data.get(CONTRADICTIONS_KEY) or []]:
        src = ref_source(ref)
        row: dict[str, Any] = {"ref": ref, "other": src, "note": link_target(src), "their": "", "mine": "",
                               "found": "", "how": "", "line": 0, "kept": []}
        if not src:
            f = _FLAG_RE.match(ref)
            row.update(their=f.group("why") if f else ref, found=f.group("date") if f else "",
                       how=f"flagged by {f.group('who')}" if f else "flagged")
        elif (c := _open_callout(lines[head:], stop - head, src)) is not None:
            row.update(_parse(lines[head + c[0]:head + c[1]]), line=head + c[0] + 1)
        out.append(row)
    return out


def callout(other: str, their: str, mine: str, today: str, how: str = FOUND_BY) -> str:
    """A new record's callout: the import's shape, with this note's words and
    the day it was found after the empty line. No em dash in the header, which
    the import's still carries; the start pattern reads both."""
    quoted = [f"> {ln}".rstrip() for ln in their.strip().splitlines()] or [">"]
    return "\n".join([f"> [!warning] Contradiction from {other}", *quoted, ">", f"> {MINE}{mine.strip()}",
                      f"> {FOUND}{today} {how}.", f"> Conflicts with this note. {_UNRESOLVED_TAIL}"])


def add_record(content: str, other: str, their: str, mine: str, today: str, how: str = FOUND_BY) -> str:
    """`content` with a new open record against `other`: the callout at the end
    of the live body and the entry in `contradictions:`."""
    return mark_contested(append_before_superseded(content, "\n" + callout(other, their, mine, today, how) + "\n"),
                          f"source: {other}")


def said(holds: str, other: str, condition: str = "") -> str:
    """The resolution in words, as the file keeps it."""
    return {"this": "this note holds", "other": f"{other or 'the flag'} holds",
            "both": f"both hold when {condition.strip()}"}[holds]


def side_line(name: str, holds: str, today: str, condition: str = "") -> str:
    """The dated line the other note gets when this note holds or both do
    (ADR-0166 point 3): a note callout, never a warning, so no reader takes it
    for an open contest."""
    words = f"[[{name}]] holds" if holds == "this" else said(holds, "", condition)
    return f"> [!note] Contradiction with [[{name}]] resolved {today}: {words}."


def _drop_ref(content: str, ref: str) -> str:
    data, _raw, body = frontmatter.split(content)
    if data is None:
        return content
    rest = [r for r in data.get(CONTRADICTIONS_KEY) or [] if str(r) != ref]
    if rest:
        data[CONTRADICTIONS_KEY] = rest
    else:
        data.pop(CONTESTED_KEY, None)
        data.pop(CONTRADICTIONS_KEY, None)
    return frontmatter.dump(data, body) if data else body.lstrip("\n")  # no `{}` block (resolve_contested)


def _file(content: str, block: list[str]) -> str:
    """`block` under `## Superseded`, created at the end when absent."""
    data, _raw, body = frontmatter.split(content)
    filed = merge_moc_section(body, SUPERSEDED_MARKER, block)
    return frontmatter.dump(data, filed) if data else filed


def settle(content: str, ref: str, holds: str, today: str, *, condition: str = "", who: str = "user") -> str:
    """`content` with the record `ref` settled as `holds` says. Unchanged when
    the note carries no such open record.

    - this: the other side's callout moves under `## Superseded`, as
      silica_flag_note's clear always filed it, its tail reading who won.
    - other: the callout stays where it is, turned from a warning into a note,
      since it now holds the claim that stands; this note's words, when still
      found as a block exactly as the record quotes them, move under
      `## Superseded` with the day and the winner. Found nowhere, the body is
      left alone: a sentence edited since is the person's to correct.
    - both: the callout stays, a note now, saying the condition.
    - open: a dated "Kept open" line, and the record stays open.
    A flag names no other side, so it takes this, other (the flag holds) and
    open; both raises ValueError.
    """
    if holds not in HOLDS:
        raise ValueError(f"holds is one of {', '.join(HOLDS)}")
    if holds == "both" and not condition.strip():
        raise ValueError("both hold needs the condition under which each holds")
    rec = next((r for r in records(content) if r["ref"] == ref), None)
    if rec is None:
        return content
    if not rec["other"]:
        if holds == "both":
            raise ValueError("a flag names no other side: this note holds, or the flag does")
        if holds == "open":
            return content  # a flag carries no line to date; it stays open as it was
        return _file(_drop_ref(content, ref), [stamp(valid_to=today, resolved_by=who),
                                               f"> [!warning] Contradiction, {ref}",
                                               f"> Resolved {today}: {said(holds, '')}.", ""])
    if holds == "this":
        return resolve_contested(content, resolved_by=who, valid_to=today, source_ref=ref, said=said("this", ""))
    _data, lines, head, stop = _split(content)
    c = _open_callout(lines[head:], stop - head, rec["other"])
    if c is None:
        return content
    first, last = head + c[0], head + c[1] - 1
    if holds == "open":
        return "\n".join(lines[:last] + [f"> {KEPT}{today}."] + lines[last:])
    lines[first] = lines[first].replace("[!warning]", "[!note]", 1)
    lines[last] = lines[last].replace(_UNRESOLVED_TAIL, f"Resolved {today}: {said(holds, rec['other'], condition)}.")
    text = "\n".join(lines)
    lost: list[str] = []
    if holds == "other" and rec["mine"]:
        found = next((b for b in blk.split(text)[0] if b.text == rec["mine"] and not first < b.line <= last + 1), None)
        if found is not None:
            kept = text.split("\n")
            del kept[found.line - 1:found.end]
            at = found.line - 1
            if 0 < at < len(kept) and not kept[at].strip() and not kept[at - 1].strip():
                del kept[at]  # the paragraph's blank line goes with it, not two in a row
            text = "\n".join(kept)
            lost = [stamp(valid_to=today, resolved_by=who), f"> [!quote] Superseded {today} by {rec['other']}",
                    f"> {rec['mine']}", ""]
    text = _drop_ref(text, ref)
    return _file(text, lost) if lost else text


def has_record(text: str, name: str) -> bool:
    """Whether `text` already records a contest with the note `name`, open or
    settled: then a check's proposal of the same pair is history, not news."""
    n = re.escape(name)
    return bool(re.search(rf"^> \[!(?:warning|note)\] Contradiction\b.*\[\[{n}(?:[|#\]])", text, re.MULTILINE)
                or re.search(rf"Contradiction with \[\[{n}\]\]", text))


def find_line(text: str, quote: str) -> int:
    """The 1-based line of `text` where `quote`'s first words stand, 0 when
    none: a quote the judge flattened from a table or across lines is found by
    its opening, not whole."""
    words = quote.replace("…", " ").split()
    for n in (6, 3):
        needle = " ".join(words[:n])
        if not needle:
            return 0
        for i, ln in enumerate(text.split("\n")):
            if needle in " ".join(ln.split()):
                return i + 1
    return 0


def age(found: str, today: dt.date | None = None) -> int | None:
    """Days since `found`, None when the record never said."""
    try:
        return ((today or dt.date.today()) - dt.date.fromisoformat(found)).days
    except ValueError:
        return None


# --- what the vault holds: read through the driver ------------------------------------


def _name(path: str) -> str:
    """How a record links a note: its stem, or its path when another note
    shares the stem (ADR-0114)."""
    from silica.driver import DRIVER

    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    return stem if len(DRIVER.notes_named(stem)) <= 1 else path.removesuffix(".md")


def open_records() -> list[dict[str, Any]]:
    """Every open record of the vault, with its note's path, by path. Scanned
    once per file-state epoch, as /contested always did: it reads every note's
    properties and the text of the contested ones."""
    from silica.driver import DRIVER
    from silica.kernel.base import layout

    def scan() -> list[dict[str, Any]]:
        rows = []
        for ref in DRIVER.list_files(""):
            try:
                if not (DRIVER.props_of(ref.path) or {}).get(CONTESTED_KEY):
                    continue
                text = DRIVER.read_note(ref.path).content or ""
            except Exception:
                continue  # an attachment or a note that will not read is not contested
            rows += [{"path": ref.path, **r} for r in records(text)]
        return sorted(rows, key=lambda r: (r["path"], r["line"]))

    return [{**r, "age": age(r["found"])} for r in layout.on_epoch(_OPEN_MEMO, "open", scan)]


def proposals_of(vault: str, path: str, text: str) -> list[dict[str, Any]]:
    """The `contradicted` findings the last whole-note check of `path` left in
    its cache, as proposals, skipping a block edited since (no block starts on
    the line any more) and a pair either note already records."""
    from silica.driver import DRIVER
    from silica.kernel.check.run import read_cache

    findings = (read_cache(vault, path).get("report") or {}).get("findings") or []
    at_line = {b.line: b for b in blk.split(text)[0]}
    out = []
    for f in findings:
        other_path, _, other_line = str(f.get("at") or "").rpartition(":")
        b = at_line.get(int(f.get("line") or 0))
        if f.get("kind") != "contradicted" or not other_path or b is None:
            continue
        name = _name(other_path)
        try:
            theirs = DRIVER.read_note(other_path).content or ""
        except Exception:
            continue  # the other note is gone or unreadable: nothing to set beside this one
        if has_record(text, name) or has_record(theirs, _name(path)):
            continue
        their_line, their = _focus(theirs, str(f.get("passage") or ""), b.text)
        out.append({"path": path, "line": b.line, "end": b.end, "mine": b.text, "at": f["at"],
                    "other_path": other_path, "other": f"[[{name}]]",
                    "their_line": their_line or (int(other_line) if other_line.isdigit() else 0),
                    "their": their})
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.casefold()) if len(w) > 3}


def _focus(theirs: str, passage: str, mine: str) -> tuple[int, str]:
    """(line, words) of the block of the other note the verdict rests on: of
    the blocks the judged passage holds, the one sharing most words with this
    note's. The judge reads a short note whole (ADR-0110), so its passage is
    the whole note at line 1, title included, and a record quoting all of it
    would set a page beside a sentence. (0, passage) when no block matches."""
    flat = " ".join(passage.replace("…", " ").split())
    held = [b for b in blk.split(theirs)[0] if " ".join(b.text.split()[:6]) in flat]
    if not held:
        return 0, passage
    best = max(held, key=lambda b: len(_words(b.text) & _words(mine)))
    return best.line, best.text


def proposals(vault: str, folder: str = "") -> list[dict[str, Any]]:
    """Every proposal under `folder`, each pair once: B against A is A against B."""
    from silica.driver import DRIVER
    from silica.kernel.base.layout import iter_notes
    from silica.kernel.check.run import read_cache

    seen: set[frozenset[str]] = set()
    out = []
    for rel, _f in iter_notes(vault, under=folder):
        if not read_cache(vault, rel):
            continue
        try:
            text = DRIVER.read_note(rel).content or ""
        except Exception:
            continue  # unreadable now: its cached verdicts describe a text nobody can open
        for p in proposals_of(vault, rel, text):
            pair = frozenset((rel, p["other_path"]))
            if pair not in seen:
                seen.add(pair)
                out.append(p)
    return out


def note_view(vault: str, path: str) -> dict[str, Any]:
    """What the drawer sets side by side for one note: its open records, each
    with the line its own words stand on and where the other side's do, and
    the proposals its last check left."""
    from silica.driver import DRIVER

    text = DRIVER.read_note(path).content or ""
    at = {b.text: b.line for b in blk.split(text)[0]}
    rows = []
    for r in records(text):
        other_path, their_line = "", 0
        if r["note"]:
            try:
                nc = DRIVER.read_note(r["note"])
                other_path, their_line = nc.ref.path or "", find_line(nc.content or "", r["their"])
            except Exception:
                pass  # a link to no note: the side shows its words and opens nothing
        rows.append({**r, "path": path, "age": age(r["found"]), "mine_line": at.get(r["mine"], 0),
                     "other_path": other_path, "their_line": their_line})
    return {"path": path, "records": rows, "proposals": proposals_of(vault, path, text)}


def resolve(vault: str, path: str, holds: str, *, ref: str = "", at: str = "", line: int = 0,
            condition: str = "") -> dict[str, Any]:
    """Settle one record of `path`, or one proposal (`at` and `line` name it),
    as one run of the person's: this note, and the other note when it gets its
    dated line. Every write has its restore point and its run, so /undo takes
    back one note and the run takes back both (ADR-0166 point 3)."""
    from silica.driver import DRIVER
    from silica.kernel import contested_register

    today = dt.date.today().isoformat()
    text = DRIVER.read_note(path).content or ""
    proposal = None
    if at:
        proposal = next((p for p in proposals_of(vault, path, text) if p["at"] == at and p["line"] == line), None)
        if proposal is None:
            raise LookupError("that proposal is gone: the note or its check changed")
        other, ref = proposal["other"], f"source: {proposal['other']}"
    else:
        rec = next((r for r in records(text) if r["ref"] == ref), None)
        if rec is None:
            raise LookupError("the note no longer holds that contradiction open")
        other = rec["other"]
    settle("", ref, holds, today, condition=condition)  # the refusals (a word, a missing condition) before any write

    def mine(before: str) -> str:
        if proposal is not None:
            before = add_record(before, other, proposal["their"], proposal["mine"], today)
        return settle(before, ref, holds, today, condition=condition)

    run = _land(path, mine, "")
    touched = [path] if run else []
    target = link_target(other)
    if run and target and holds in ("this", "both"):
        try:
            other_path = DRIVER.read_note(target).ref.path or target
        except Exception:
            other_path = ""  # the other note is gone: this note's record says it all
        if other_path:
            line_b = side_line(_name(path), holds, today, condition)
            if _land(other_path, lambda t: append_before_superseded(t, "\n" + line_b + "\n"), run):
                touched.append(other_path)
    still = bool(records(DRIVER.read_note(path).content or "")) if run else bool(records(text))
    if run:
        contested_register.note(path, still)
    return {"run": run, "paths": touched, "contested": still, "changed": bool(run)}


def _land(path: str, fn: Callable[[str], str], run: str) -> str:
    """One revertible write of the person's, joined to `run`: the undo journal
    for the run, a checkpoint for /undo and History. "" when nothing changed.
    Loss.PERSON because the person chose the outcome, and "the other holds"
    takes this note's sentence out, which the resolve rule would refuse."""
    from silica.kernel.write.checkpoints import checkpoint_landed
    from silica.kernel.write.gate import journaled_write
    from silica.kernel.write.preserve import Loss
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE

    prior: list[str] = []

    def mutate(before: str) -> str:
        prior.append(before)
        return fn(before)

    got = journaled_write(path, mutate, source=USER_WRITE_SOURCE, loss=Loss.PERSON, run=run)
    if got:
        checkpoint_landed(path, prior[0])
    return got
