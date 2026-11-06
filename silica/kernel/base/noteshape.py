# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a note's own text says it is: the tail headings, the `type:` words,
the keys that make a note an event or a spine, the claim stamp and the clock
read off it.

Each of these was matched by its readers one by one, so a new word had to be
found by hand. Words only and pure functions of a note's text: which FOLDERS
are not notes is `layout`'s answer, which keys join notes is `notekey`'s. Stdlib
and frontmatter alone, because the session hook's budget (test_migrate) reaches
here through `templates`.
"""
from __future__ import annotations

import re

from silica.kernel.base import frontmatter

# --- tail headings ----------------------------------------------------------

RELATIONS_MARKER = "## Relations"
# The H2 under which a note links back to its verbatim leaf. Its presence is
# also the note-side signal that the source is still retrievable, which is what
# separates a grounded claim from a purely distilled one (reliability_tier).
SOURCES_MARKER = "## Sources"
SUPERSEDED_MARKER = "## Superseded"
TAIL_MARKERS = (RELATIONS_MARKER, SOURCES_MARKER, SUPERSEDED_MARKER)

MOC_PREFIX = "## From: "
# Emitted until 2026-09. Recognised forever by merge_moc_section: a hub that
# already carries this spelling must keep merging into it, or every later chunk
# of the same source opens a second section. Never emitted again.
LEGACY_MOC_PREFIX = "## Da: "
# A hub's index blocks are a tail too: a facet appended after them filed itself
# under the index (2026-09-05).
MOC_PREFIXES = (MOC_PREFIX, LEGACY_MOC_PREFIX)

# Emitted until 2026-09-04 (English) and 2026-08 (Italian). Recognized
# forever, never emitted: the header was the idempotency key for a patch block
# (block_present), so a vault carrying blocks in either spelling must keep
# matching or every re-ingest appends a second copy of what it already wrote.
# Today a patch lands headerless (patch_snippet); its keys are snippet_present
# and missing_relations.
PROVENANCE_HEADER_PREFIX = "## Additional notes"
LEGACY_PROVENANCE_HEADER_PREFIX = "## Note aggiuntive"
PROVENANCE_HEADER_PREFIXES = (PROVENANCE_HEADER_PREFIX, LEGACY_PROVENANCE_HEADER_PREFIX)

SUPERSEDED_RE = re.compile(r"^## Superseded\s*$", re.MULTILINE)
# Three readers of "the tail", three cuts, on purpose: a figure is kept out of
# Relations too, a footnote pass stops at any depth and any case, because a
# hand-typed `### sources` still closes the claims, and the rights check and the
# web note drop everything from the first Sources line, however it goes on.
BODY_END_RE = re.compile(r"^## (?:Relations|Sources)\b", re.MULTILINE)
CLAIMS_END_RE = re.compile(r"^#{1,6}\s+(Sources|Superseded)\b", re.IGNORECASE)
SOURCES_TAIL_RE = re.compile(r"^## Sources.*", re.DOTALL | re.MULTILINE)


def is_tail(line: str) -> bool:
    return line.rstrip() in TAIL_MARKERS or line.startswith(MOC_PREFIXES)


# --- type words -------------------------------------------------------------

PLAN, SYLLABUS = "plan", "syllabus"   # Learn's study plan; syllabus is its legacy spelling
READING = "reading"                   # a step's reading (ADR-0132)
WORKSPACE = "workspace"
# Kinds no `type:` states: `note_kind` reads them off other keys, the note
# index off the folder.
EVENT, MERGED, STAGED = "event", "merged", "staged"

# OKF's four words (notetype.derive_type). Free-form per §4.1: this is Silica's
# vocabulary, not the spec's.
OKF_SOURCE, OKF_CODE, OKF_PLAN, OKF_NOTE = "Source", "Code", "Plan", "Note"

PLAN_WORDS = (PLAN, SYLLABUS)
# Notes about learning, never asked about; the calendar's own notes, which
# Learn's "never practised" listed as study material until 2026-10-15; and
# merge losers, whose winner is the note to study; a step's reading (ADR-0132),
# an explanation written from notes that are themselves the material; and
# the pipeline's staging, converted text that is the source, not a note.
NOT_MATERIAL = (PLAN, SYLLABUS, EVENT, MERGED, READING, STAGED)
# A workspace is the card, never the material, as a plan is: neither kind is
# ever a member of anything.
NOT_MEMBERS = (WORKSPACE, PLAN, SYLLABUS)
# A note a Silica feature runs on (ADR-0070): drawn, as a diamond, but not
# counted. Unlike log.md it is the user's to open and edit, so it stays in the
# graph; unlike a note it is not knowledge, and "Prova Learn" showed what
# counting it costs: its tree of 52 links named an area on the test vault and
# was the only in-link of 10 notes, which then never rang as orphans.
NATIVE_TYPES = (PLAN, EVENT, READING)

# A lesson spine's `section:`. Named here and not by the outline lane, because
# that lane reaches the agent layer and the graph report reads this word
# (import contract P2/DKB).
SPINE_SECTION = "(outline)"


def type_word(data: dict | None) -> str:
    """The `type:` of a parsed frontmatter as readers compare it, lowercased.

    OKF's `Plan` is the exception and reads as a note: `stamp_type` writes it
    on any note with a task `status:`, on create and on overwrite, and folded
    to `plan` it made a to-do a study plan, off the study material and drawn as
    a plan in the graph. Case tells them apart because only `derive_type` ever
    wrote the capital, and Learn only ever wrote `plan`; the stamped notes stay
    on disk, so the reader is where this is decided.
    """
    word = str((data or {}).get("type") or "").strip()
    return "note" if word == OKF_PLAN else word.lower()


def is_plan(data: dict | None) -> bool:
    return type_word(data) in PLAN_WORDS


def is_event(data: dict | None) -> bool:
    """A note states no `type` to be an event: `event_start` is what makes it one."""
    return (data or {}).get("event_start") is not None


def is_spine(data: dict | None) -> bool:
    return str((data or {}).get("section") or "") == SPINE_SECTION


def note_kind(data: dict | None) -> str:
    """What a note's frontmatter makes it, for Learn and Workspaces: an event,
    a merge loser, else its `type`. A merge loser is its winner's material
    twice: a course plan listed "Reti neurali ricorrenti" (superseded_by Rete
    ricorrente) beside its winner (2026-10-23)."""
    if is_event(data):
        return EVENT
    return MERGED if (data or {}).get("superseded_by") else type_word(data)


def is_material(meta: dict) -> bool:
    return meta.get("kind") not in NOT_MATERIAL


# --- the claim stamp and the note's clock (spec-contested-bitemporal §3) -----

STAMP_RE = re.compile(r"<!--\s*silica:\s*(.*?)\s*-->")
# Values are dates and hex run ids. Anything outside this class is dropped so a
# value can never close the comment early or break the key=value split.
_STAMP_VALUE_RE = re.compile(r"[^\w.:+-]")


def stamp(**fields: str) -> str:
    """A claim stamp: `<!-- silica: valid_from=2023-05-08 run=b07f1268 -->`.

    Key order is the caller's, so the rendered line is deterministic. Fields
    with an empty value are dropped; all-empty yields "" so a caller can splice
    unconditionally.
    """
    parts = []
    for k, v in fields.items():
        if v is None:  # str(None) is "None", which is truthy — never emit it
            continue
        cleaned = _STAMP_VALUE_RE.sub("", str(v).strip())
        if cleaned:
            parts.append(f"{k}={cleaned}")
    return f"<!-- silica: {' '.join(parts)} -->" if parts else ""


def _stamp_fields(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in raw.split():
        k, _, v = tok.partition("=")
        if k and v:
            out[k] = v
    return out


def parse_stamps(text: str) -> list[dict[str, str]]:
    """Fields of EVERY claim stamp in `text`, in document order.

    A note accumulates claims from several sources on different dates, so the
    first stamp is not the note's clock.
    """
    return [_stamp_fields(m.group(1)) for m in STAMP_RE.finditer(text or "")]


def verified_entries(data: dict | None) -> list[dict]:
    """The `verified` entries of a note's frontmatter, always as a list.

    §5.2 is a MUST on readers: a single `{by, at}` mapping means a one-element
    list. Hand-edited frontmatter is where this field comes from, and a person
    writing one verification writes the mapping, not a list of one.
    """
    raw = (data or {}).get("verified")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def note_clock(content: str) -> str | None:
    """The freshest date a note can show for its own claims; None when it shows none.

    Two sources, both already written by this codebase: the newest `valid_from`
    on a claim stamp (Fase A), and OKF §5.2 `verified.at`, a person recording
    the day they read the note. A note that carries neither is not thereby
    fresh — it is silent, which `suppress_contest` treats as the risk it is.
    """
    dates = [s["valid_from"] for s in parse_stamps(content) if s.get("valid_from")]
    data, _raw, _body = frontmatter.split(content or "")
    dates += [str(e.get("at")) for e in verified_entries(data) if e.get("at")]
    return max(dates) if dates else None
