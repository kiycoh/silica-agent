# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Outline-first nucleation lane: the model names the units, the machine guards.

Why this lane exists (ADR-0036; docs/specs/nucleation-paradigm-ab.md, measured
2026-09-02 on two SVM lectures): the keyphrase pipeline let a miner decide the
units (tokens // 20, cap 40), handed the distiller {name, excerpt} pairs with
no document structure, hid same-run notes from the related candidates and
linked by cosine + BM25. The result was a star around the hub: section-level
provenance 0%, typed edges 0, one real link between the two lectures. The
prototype that reads the lecture whole reached 100% section provenance, 37
typed and explained edges and 8 cross-lecture pairs at 7 calls and 5.5 min
against 53 min for the pipeline. This module is that prototype with the four
defects it showed fixed mechanically: a coverage pass over the source
headings, `same_as` handed to the dedup judge instead of merged blind, a cap
on edges per target, and lecture-level notes hidden from the edge stage.

Call plan per source file: A (outline) -> GAP (only when a heading has no
idea) -> BODIES (batches of 8) -> B (cross edges, only when the vault has
notes). Every stage is one JSON reply; `ask` is injectable so the plan is
testable without a model.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from collections.abc import Set as AbstractSet
from typing import Callable

from silica.kernel.text.media import strip_images

logger = logging.getLogger(__name__)

# Types are rendered, never ranked on: no retrieval leg reads them, and no
# measurement says a reader gains from the label over an untyped link (as of
# 2026-09-17; the 2x2 that would decide it is in docs/audits/2026-09-17-paper-backed-simplifications.md §3).
RELATIONS = ("defines", "derives", "relaxes", "applies", "bounds", "contrasts",
             "instance_of", "motivates", "generalizes", "justifies")
CROSS_RELATIONS = RELATIONS + ("same_as",)

# Readable verb phrase per relation, subject = the note the line sits in.
# English on purpose: vault strings emitted by the harness are UI copy.
RELATION_LABEL = {
    "defines": "defined via", "derives": "derived from", "relaxes": "relaxes",
    "applies": "applies", "bounds": "bounds", "contrasts": "contrasts with",
    "instance_of": "instance of", "motivates": "motivates",
    "generalizes": "generalizes", "justifies": "justifies",
}

# Frontmatter `section:` value that marks a lesson spine note. The spine is
# the one note per source that is about the source's argument, not an idea,
# so the edge stage must never propose it as a target (measured: 6 of 9 edges
# went to the lecture-level note when it was visible).
SPINE_SECTION = "(outline)"

BODIES_BATCH = 4            # ideas per bodies call. Was 8: the wall time of a lecture is its longest
                            # stream, and two 8-idea batches streamed 3.7 and 6.5 min on deepseek
                            # flash (2026-09-05); four ideas halve the stream, the pool below runs them.
BODIES_WORKERS = 6          # concurrent bodies calls per file: the batches are independent, and one
                            # lecture of 4 batches spent 12 of its 13 minutes on them in series (2026-09-05)
BODIES_REPLY_TOKENS = 4000  # what one bodies call adds to the KV beyond its prompt: four 4-idea calls
                            # overflowed a 32k unified KV at ~8.2k tokens each on a 4.6k prompt (2026-10-12)
MAX_EDGES_PER_TARGET = 3    # a target with more is a hub in disguise, not three real dependencies
MIN_WHY_CHARS = 20          # "e' collegato" is not a reason
MAX_OUTLINE_ROWS = 400      # ponytail: folder-scoped outline; reopen at 400 notes in one folder, then seed with cosine top-k

STAGE_A_TAG = "[silica outline: stage A]"
STAGE_GAP_TAG = "[silica outline: coverage]"
STAGE_BODIES_TAG = "[silica outline: bodies]"
STAGE_B_TAG = "[silica outline: stage B]"

STAGE_A = STAGE_A_TAG + """
You are reading one complete source document (markdown, LaTeX kept). Reconstruct its ARGUMENT as a small graph of ideas, the way a careful student would after studying it.

Emit ONLY a JSON object:
{{
 "lesson_title": "<short title of what this document is about, in {lang}>",
 "spine": ["<idea title>", ...],
 "ideas": [
   {{
     "title": "<noun phrase in {lang} of 2-6 words naming ONE idea, capitalised like a note title; never a sentence, a slide line ('Note that', 'Arriving at the end, case l=L') or a formula>",
     "section": "<heading of the source section(s) the idea comes from, verbatim>",
     "claim": "<one sentence in {lang}: what this idea asserts>",
     "depends_on": [{{"title": "<another idea title from THIS list>", "relation": "<one of {rels}>", "why": "<one sentence in {lang}>"}}]
   }}
 ]
}}
Rules:
- "spine" lists every idea title exactly once, in the order the argument develops.
- Between 6 and 16 ideas. Merge repeated slides of the same idea into one idea. Skip noise (author, email, course header, empty slides).
- Every depends_on.title MUST be another idea title of this same JSON. Relation meanings: defines (A is defined in terms of B), derives (A is derived from B), relaxes (A weakens B's assumptions), applies (A applies B), bounds (A bounds B), contrasts (A vs B), instance_of, motivates (A is why B is needed), generalizes, justifies.
- lesson_title, every claim and every why are written in {lang}.
- No markdown fences around the JSON.
"""

# Every field the outline call asked for is asked for again here, in the same
# language: this is a separate call to a model with no memory of the outline
# call, and "same shape as before" gave Lezione 11 eleven English ideas out of
# fifteen, six of them without a section (run 7ad24860, 2026-09-06).
STAGE_GAP = STAGE_GAP_TAG + """
You already extracted the ideas listed below from the source document that follows. These source sections have NO idea yet:
{gaps}
For each of them either add an idea or record an explicit skip with the reason (empty slide, duplicate of an existing idea, noise). An idea has the shape
{{"title": "<noun phrase in {lang} of 2-6 words naming ONE idea, capitalised like a note title>", "section": "<the source heading it comes from, verbatim from the list above>", "claim": "<one sentence in {lang}: what this idea asserts>", "depends_on": [{{"title": "<an existing or new idea title>", "relation": "<one of {rels}>", "why": "<one sentence in {lang}>"}}]}}
Title, claim and every why are written in {lang}; the section is copied as it is.

Emit ONLY a JSON object: {{"ideas": [...], "skips": [{{"section": "<heading>", "reason": "<why>"}}]}}. No markdown fences.
"""

STAGE_BODIES = STAGE_BODIES_TAG + """
You are given one complete source document (markdown, LaTeX kept) and a list of ideas already identified in it (a quoted title, then the source section it comes from). For EACH listed idea write its note body in {lang}: the facts themselves (definitions, formulas in LaTeX verbatim, theorem statements, algorithm steps), copied from the source, never outside knowledge, never a description of the source. Markdown. Keep every formula that belongs to the idea, drop repeated slides.

Reply in PLAIN MARKDOWN, not JSON: for each listed idea, one line `### IDEA: <idea title exactly as quoted>` followed by its body on the following lines. One section per listed idea, in the given order, keyed by the idea title and never by the section (several ideas can share one section). Nothing before the first marker, no code fences around the reply.
"""

# Bodies are LaTeX-heavy prose: as JSON strings they came back with single
# backslashes, literal newlines, unescaped quotes and objects split in two
# (four reply shapes, about one retry per lecture, 2026-09-05). A marker line
# per idea needs no escaping at all.
# Leading blanks allowed: the model indents marker lines by a space now and
# then (" ### IDEA: ...", 2026-09-05) and a line-anchored `#` missed them all.
_IDEA_MARK = re.compile(r"^[ \t]*#{1,6}\s*IDEA:\s*(.+?)\s*$", re.M)


_ANY_HEADING = re.compile(r"^[ \t]*(#{1,6})\s+(.+?)\s*$", re.M)


def _parse_bodies_text(text: str, titles: list[str] | None = None) -> dict[str, str]:
    """`### IDEA: title` sections -> {title: body}; a fenced reply is unwrapped.

    Without a single IDEA marker the reply is split on the headings that name
    a requested title (the model drops the prefix and writes `### Title`,
    seen on the first lecture that used this format, 2026-09-05).
    """
    text = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", (text or "").strip())
    mark = _IDEA_MARK
    if not mark.search(text) and titles:
        from difflib import SequenceMatcher
        keys = {_clean_title(t).casefold(): t for t in titles}

        def _owner(h: str) -> str | None:
            k = _clean_title(h).casefold()
            if k in keys:
                return keys[k]
            best = max(((SequenceMatcher(None, k, c).ratio(), t) for c, t in keys.items()), default=(0, None))
            return best[1] if best[0] >= 0.85 else None

        out: dict[str, str] = {}
        heads = [(m.start(), m.end(), len(m.group(1)), _owner(m.group(2))) for m in _ANY_HEADING.finditer(text)]
        for i, (_s, e, level, t) in enumerate(heads):
            if not t:
                continue
            # A body runs to the next heading of its own level or higher:
            # deeper headings belong to it, sibling sections do not.
            nxt = next((h[0] for h in heads[i + 1:] if h[2] <= level), len(text))
            if text[e:nxt].strip():
                out.setdefault(t, text[e:nxt].strip())
        return out
    parts = mark.split(text)
    return {t.strip().strip('"*').strip(): b.strip() for t, b in zip(parts[1::2], parts[2::2]) if b.strip()}

STAGE_B = STAGE_B_TAG + """
You maintain a knowledge vault. Below is the OUTLINE of the vault so far (one line per existing note: lesson | title | claim), then the ideas of a NEW source (title | claim).

Find the connections a careful student would draw between the NEW ideas and the EXISTING notes: where a new idea applies, relaxes, generalizes, bounds, justifies, contrasts with, is an instance of, or is the same idea as an existing one. Propose only connections you can justify in one sentence from the two claims; at most three connections per existing note.

Emit ONLY a JSON object: {{"edges": [{{"from": "<new idea title>", "to": "<existing note title>", "relation": "<one of {rels}>", "why": "<one sentence in {lang}>"}}]}}
"from" MUST be a new idea title and "to" an existing note title, both verbatim; the new ideas are already linked to each other, so never propose an edge between two NEW ideas. Use "same_as" only for the same idea restated. Every why is written in {lang}. No markdown fences.
"""


# Reply schemas, one per stage: passed as `response_schema` so the provider
# constrains decoding (the distiller's guard since the two-pass split). A
# free-form reply on an upstream that ignores `reasoning: false` was the
# thinking trace (no JSON, finish=stop) or a JSON cut at the budget
# (finish=length): both seen on the 2026-09-02 live run.
from pydantic import BaseModel, Field


class DependencyReply(BaseModel):
    title: str
    relation: str
    why: str = ""


class IdeaReply(BaseModel):
    title: str
    section: str = ""
    claim: str = ""
    depends_on: list[DependencyReply] = Field(default_factory=list)


class OutlineReply(BaseModel):
    lesson_title: str = ""
    spine: list[str] = Field(default_factory=list)
    ideas: list[IdeaReply]


class SkipReply(BaseModel):
    section: str
    reason: str = ""


class GapReply(BaseModel):
    ideas: list[IdeaReply] = Field(default_factory=list)
    skips: list[SkipReply] = Field(default_factory=list)


class BodiesReply(BaseModel):
    bodies: dict[str, str]


class EdgeReply(BaseModel):
    from_: str = Field(alias="from")
    to: str
    relation: str
    why: str = ""


class EdgesReply(BaseModel):
    edges: list[EdgeReply] = Field(default_factory=list)


SCHEMA_BY_TAG: dict[str, type[BaseModel]] = {STAGE_A_TAG: OutlineReply, STAGE_GAP_TAG: GapReply,
                 STAGE_BODIES_TAG: BodiesReply, STAGE_B_TAG: EdgesReply}


@dataclass
class Idea:
    title: str
    section: str
    claim: str
    body: str = ""
    depends_on: list[dict] = field(default_factory=list)


@dataclass
class Outline:
    lesson_title: str
    spine: list[str]
    ideas: list[Idea]
    skips: list[dict] = field(default_factory=list)

    def titles(self) -> list[str]:
        return [i.title for i in self.ideas]


# ------------------------------------------------------------------ routing --
def lane_for(profile: str | None) -> str:
    """'outline' for document-shaped sources, 'keyphrase' for the memory forms.

    clip and promotion carry episodic facts the distiller's ephemeral schema
    routes; the outline lane has no such channel, so they keep the keyphrase
    pipeline. SILICA_NUCLEATE_LANE=keyphrase turns the lane off everywhere
    (the A/B rerun knob).
    """
    from silica.config import CONFIG
    if (getattr(CONFIG, "nucleate_lane", "outline") or "outline") != "outline":
        return "keyphrase"
    return "keyphrase" if (profile or "default") in ("clip", "promotion") else "outline"


# ------------------------------------------------------------------ parsing --
def _clean_title(s: object) -> str:
    # A slide line keeps its full stop ("X variabile discreta."): a note name
    # never ends on one, and the dot would survive into the file name.
    return re.sub(r"\s+", " ", str(s or "")).strip().strip("*").strip().rstrip(".").strip()


# Course apparatus the model turns into ideas when a lecture opens with it
# ("Libri di testo" became a note, 2026-09-05). Closed, bilingual, exact.
_APPARATUS_TITLES = frozenset({
    "libri di testo", "testi consigliati", "bibliografia", "riferimenti", "contatti",
    "orario", "esame", "modalità d'esame", "textbooks", "bibliography", "references",
    "contacts", "schedule", "exam", "syllabus",
})


def _title_ok(title: str) -> bool:
    """A note name, not a slide line. Live 2026-09-05: "Da notare che",
    "Arrivo alla fine, caso l=L", "Fase di back, caso 1<l<L" and a 150-char
    sentence with V={V1,...} all became notes. Rejected ideas leave their
    section to the coverage pass, which asks once more or records a skip."""
    from silica.kernel.text.candidates import is_fragment
    from silica.kernel.text.recon import _dangles
    if len(title) > 90 or len(title.split()) > 12:
        return False
    # The shared name test (verb in the title, headless adjective, lone short
    # token): the lane had only the dangling-tail rule and let 17 declarative
    # slide lines through as titles (2026-09-06).
    if is_fragment(title):
        return False
    if title.casefold().strip() in _APPARATUS_TITLES:
        return False
    if re.search(r"[=<>{}]|,$", title):
        return False
    return not _dangles(title)


def parse_outline(obj: dict) -> Outline:
    """Model JSON -> Outline. Drops duplicate titles, unknown dependency
    targets, self-dependencies, relations outside the closed set and titles
    that are not names (_title_ok); a lowercase first letter is capitalised."""
    ideas: list[Idea] = []
    seen: set[str] = set()
    for raw in obj.get("ideas") or []:
        if not isinstance(raw, dict):
            continue
        title = _clean_title(raw.get("title"))
        if title[:1].islower():
            title = title[0].upper() + title[1:]
        if not title or title.casefold() in seen:
            continue
        if not _title_ok(title):
            logger.info("outline: idea title rejected as a slide line: %r", title)
            continue
        section = _clean_title(raw.get("section"))
        # An idea born in an apparatus section is apparatus: the lecturer's
        # name under "Machine Learning (9 CFU)" became a note the bodies
        # stage then had nothing to say about (2026-09-05).
        if section and _NOISE_HEADING_RE.search(section):
            logger.info("outline: idea %r dropped, its section %r is apparatus", title, section)
            continue
        seen.add(title.casefold())
        ideas.append(Idea(title=title, section=_clean_title(raw.get("section")),
                          claim=str(raw.get("claim") or "").strip(),
                          body=str(raw.get("body") or ""),
                          depends_on=list(raw.get("depends_on") or [])))
    by_key = {i.title.casefold(): i.title for i in ideas}
    for idea in ideas:
        kept = []
        for d in idea.depends_on:
            if not isinstance(d, dict):
                continue
            target = by_key.get(_clean_title(d.get("title")).casefold())
            rel = str(d.get("relation") or "").strip()
            if not target or target == idea.title or rel not in RELATIONS:
                continue
            kept.append({"title": target, "relation": rel, "why": str(d.get("why") or "").strip()})
        idea.depends_on = kept
    spine = [by_key[_clean_title(t).casefold()] for t in (obj.get("spine") or [])
             if _clean_title(t).casefold() in by_key]
    spine += [i.title for i in ideas if i.title not in spine]
    return Outline(lesson_title=str(obj.get("lesson_title") or "").strip(), spine=spine, ideas=ideas)


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
_NOISE_HEADING_RE = re.compile(
    r"@|^\s*(dove|where|note|notes)\s*:?\s*$|\b\d+\s*cfu\b|^\s*lezione\s+\d+\s*$", re.I)


def source_headings(md: str) -> list[str]:
    """Substantive headings of the source, in order, once each.

    Noise is a closed list (author line, email, course header, "dove:"): the
    coverage pass asks the model about every heading left, so a false
    positive here costs one wasted question, a false negative a silent gap.
    """
    out: list[str] = []
    seen: set[str] = set()
    for h in _HEADING_RE.findall(md):
        h = _clean_title(h)
        if not h or _NOISE_HEADING_RE.search(h) or h.casefold() in seen:
            continue
        seen.add(h.casefold())
        out.append(h)
    return out


def _sec_key(s: str) -> str:
    return re.sub(r"[^\w]+", " ", _clean_title(s).casefold()).strip()


def coverage_gaps(outline: Outline, headings: list[str]) -> list[str]:
    """Headings no idea claims as its section (and no skip explains)."""
    claimed = {_sec_key(i.section) for i in outline.ideas} | {_sec_key(s.get("section", "")) for s in outline.skips}
    gaps = []
    for h in headings:
        k = _sec_key(h)
        if k in claimed or any(k and (k in c or c in k) for c in claimed if c):
            continue
        gaps.append(h)
    return gaps


# -------------------------------------------------------------------- edges --
def select_edges(raw: list, *, ideas: set[str], existing: dict[str, str],
                 spine_titles: AbstractSet[str] = frozenset()) -> list[dict]:
    """Stage B edges the vault will act on, in proposal order."""
    kept: list[dict] = []
    per_target: dict[str, int] = {}
    # Casefold lookups: the model re-cases titles ("Support Vector Machines"
    # for a note filed as "Support vector machines"), and an exact match threw
    # every edge of one live run away (2026-09-02, 0 of the proposals kept).
    def _endpoint_key(raw: object) -> str:
        # The model echoes the outline ROW it read ("Lezione 11 | Margine
        # geometrico") instead of the title alone: measured 2026-09-02, all 22
        # proposed edges of one run died on exact matching. Take the last
        # pipe-separated field, which is the title in every row shape we emit.
        return _clean_title(str(raw or "").split("|")[-1]).casefold()

    idea_by_key = {t.casefold(): t for t in ideas}
    existing_by_key = {t.casefold(): t for t in existing}
    spine_keys = {t.casefold() for t in spine_titles}
    dropped = 0
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        src = idea_by_key.get(_endpoint_key(e.get("from")))
        tgt = existing_by_key.get(_endpoint_key(e.get("to")))
        rel = str(e.get("relation") or "").strip()
        why = str(e.get("why") or "").strip()
        # An edge between two NEW ideas is kept as an intra edge on the source
        # idea: the prompt forbids them and the model proposes them anyway (24
        # of 79 in one run, 2026-09-05), and they are real dependencies. Never
        # same_as: two ideas of one outline are distinct by construction.
        intra = tgt is None and rel != "same_as" and (tgt := idea_by_key.get(_endpoint_key(e.get("to")))) is not None
        if not src or not tgt or tgt == src or tgt.casefold() in spine_keys:
            dropped += 1
            logger.debug("outline stage B: dropped edge %r -> %r (unknown endpoint or spine)", e.get("from"), e.get("to"))
            continue
        if rel not in CROSS_RELATIONS or len(why) < MIN_WHY_CHARS:
            dropped += 1
            logger.debug("outline stage B: dropped edge %r -> %r (relation %r, why %d chars)", src, tgt, rel, len(why))
            continue
        if per_target.get(tgt, 0) >= MAX_EDGES_PER_TARGET:
            continue
        per_target[tgt] = per_target.get(tgt, 0) + 1
        kept.append({"from": src, "to": tgt, "relation": rel, "why": why, **({"intra": True} if intra else {})})
    logger.info("outline stage B: %d edge(s) proposed, %d kept, %d dropped", len(raw or []), len(kept), dropped)
    return kept


# ---------------------------------------------------------------------- ops --
def _stem(basename: str) -> str:
    return os.path.splitext(os.path.basename(basename))[0]


def outline_ops(outline: Outline, *, target: str, hub: str | None, source_basename: str,
                edges: list[dict], existing: dict[str, str],
                only_titles: set[str] | None = None) -> list[dict]:
    """Outline + edges -> raw ops in the distiller's own shape.

    One write per idea (section, parent = the spine note, typed relations in
    the body), one write for the spine, one patch per cross edge on the
    existing note. A `same_as` edge is NOT a merge: the new note lands
    flagged with the near-title verdict format, so WRITE's settle path hands
    the pair to the dedup judge with a real loser to mark (the prototype
    merged blind and collapsed a variant into its parent).
    """
    tgt = target.rstrip("/")
    spine_title = _stem(source_basename)
    ops: list[dict] = []
    # An idea the bodies stage left empty is not written: the claim alone was
    # enough body for VALIDATE's hard gate (==0) and the 275-char floor is
    # soft, so five claim-only notes landed and the expand worker gave up on
    # every one (run 7ad24860, 2026-09-06). Its claim stays in the spine,
    # unlinked, and nothing points at the note that does not exist.
    bodiless = {i.title for i in outline.ideas if not i.body.strip()}
    if bodiless:
        logger.info("outline ops: %d idea(s) without a body not written: %s", len(bodiless), sorted(bodiless))
    by_from: dict[str, list[dict]] = {}
    for e in edges:
        if e["from"] not in bodiless and e.get("to") not in bodiless:
            by_from.setdefault(e["from"], []).append(e)
    for idea in outline.ideas:
        if only_titles is not None and idea.title not in only_titles:
            continue
        if idea.title in bodiless:
            continue
        deps = [d for d in idea.depends_on if d["title"] not in bodiless]
        lines = [f"- {RELATION_LABEL[d['relation']]} [[{d['title']}]]: {d['why']}".rstrip(": ")
                 for d in deps]
        related = [d["title"] for d in deps]
        review = None
        for e in by_from.get(idea.title, []):
            if e.get("intra"):
                lines.append(f"- {RELATION_LABEL[e['relation']]} [[{e['to']}]]: {e['why']}")
                related.append(e["to"])
                continue
            if e["relation"] == "same_as":
                review = f"near_title candidate='{e['to']}' path='{existing[e['to']]}' ratio=1.00"
                continue
            lines.append(f"- {RELATION_LABEL[e['relation']]} [[{e['to']}]] ({spine_title} -> earlier): {e['why']}")
            related.append(e["to"])
        body = f"> {idea.claim}\n\n{idea.body.strip()}\n" if idea.claim else idea.body.strip() + "\n"
        if lines:
            body += "\n## Relations\n" + "\n".join(lines) + "\n"
        op = {
            "op": "write", "path": f"{tgt}/{idea.title}.md", "heading": idea.title, "title": idea.title,
            "snippet": body, "source_basename": source_basename, "hub": hub, "parent": spine_title,
            "related": list(dict.fromkeys(related)), "section": idea.section,
        }
        if review:
            op["review"] = review
        ops.append(op)
    if only_titles is None:
        rows = [f"{n}. {t if t in bodiless else f'[[{t}]]'}: {next(i.claim for i in outline.ideas if i.title == t)}"
                for n, t in enumerate(outline.spine, 1)]
        head = f"> {outline.lesson_title}\n\n" if outline.lesson_title else ""
        ops.append({
            "op": "write", "path": f"{tgt}/{spine_title}.md", "heading": spine_title, "title": spine_title,
            "snippet": head + "\n".join(rows) + "\n", "source_basename": source_basename, "hub": hub,
            "section": SPINE_SECTION,
        })
        for e in edges:
            if e["relation"] == "same_as" or e.get("intra") or e["from"] in bodiless:
                continue
            ops.append({
                "op": "patch", "path": existing[e["to"]], "heading": e["to"],
                "snippet": f"- [[{e['from']}]] {RELATION_LABEL[e['relation']]} this ({spine_title}): {e['why']}\n",
                "source_basename": source_basename, "relation": True,
            })
    return ops


MAX_SECTION_EXCERPT = 12000  # a lecture section; the span-grounding gate needs the formulas whole, not a 2k window


def flag_cosine_near_titles(ops: list[dict], *, tau: float, embedder=None, store=None,
                            hidden: Callable[[str], bool] | None = None) -> int:
    """Flag each idea write whose text sits within cosine `tau` of a vault note.

    COLLISION skips outline chunks (they have no concepts before DELEGATE), so
    the only doors to the dedup judge were VALIDATE's title band and stage B's
    `same_as`: over 14 lectures the judge saw 19 title pairs and 2 same_as, and
    a pair at cosine .92 whose titles share nothing ("Separabilità Lineare
    tramite Nonlinearità" / "Trasformazione per risolvere problemi non
    linearmente separabili", ratio .19) never reached it (run 7ad24860,
    2026-09-06). Same query text as COLLISION, same flag format as the title
    band, so WRITE's settle path hands the pair to the same judge with a
    loser to mark. Fail-open: no embedder, no store, no flag. Returns the
    number of ops flagged.
    """
    from silica.kernel.recall.embed import _note_text
    from silica.kernel.recall.paths import is_inbox_path

    todo = [o for o in ops if o.get("op") == "write" and o.get("section") != SPINE_SECTION
            and not o.get("review") and (o.get("snippet") or "").strip()]
    if not todo:
        return 0
    try:
        if embedder is None or store is None:
            from silica.agent.providers import get_embedder_or_none
            from silica.config import CONFIG
            from silica.kernel.recall.embed import get_store
            embedder = embedder or get_embedder_or_none(CONFIG, "outline dedup")
            store = store or get_store()
            if embedder is None or len(store) == 0:
                return 0
        vecs = embedder.embed([_note_text(o["title"], o["snippet"]) for o in todo])
    except Exception as exc:  # noqa: BLE001
        # A dead embedder costs this lane its cosine door, never the lecture:
        # the title band and same_as still run, as they did before the door.
        logger.info("outline dedup: cosine door skipped (%s)", exc)
        return 0
    hidden = hidden or _hidden_note
    flagged = 0
    for o, vec in zip(todo, vecs):
        try:
            hits = store.cosine_top_k(vec, k=5)
        except Exception as exc:  # noqa: BLE001
            logger.debug("outline dedup: lookup failed for %r (%s)", o["title"], exc)
            continue
        for h in hits:
            path, score = str(h["path"]), float(h["score"])
            if score < tau:
                break
            # The store keys a note without `.md` (WRITE refreshes it under
            # `path.removesuffix(".md")`) and the op carries it: compared raw,
            # an idea matched its own landed note at .96 (2026-10-12).
            if path.removesuffix(".md") == o["path"].removesuffix(".md") \
                    or is_inbox_path(path) or hidden(path):
                continue
            o["review"] = f"near_title candidate='{h['name']}' path='{path}' ratio={score:.2f}"
            flagged += 1
            break
    return flagged


def _hidden_note(rel: str) -> bool:
    """A note the outline stage hides: a merge loser or a lesson spine. A
    loser as judge target re-litigates a settled pair; a spine matches every
    idea it lists."""
    try:
        from silica.driver import DRIVER
        from silica.kernel.write import frontmatter
        data, _raw, _body = frontmatter.split(DRIVER.read_note(rel).content or "")
    except Exception:  # noqa: BLE001
        # Unreadable candidate: not a merge target, the judge would fail on it anyway.
        return True
    data = data if isinstance(data, dict) else {}
    return bool(data.get("superseded_by")) or str(data.get("section") or "") == SPINE_SECTION


def section_text(source: str, heading: str) -> str:
    """The source section under `heading` (to the next heading of the same or
    higher level, every repeat of the heading joined), or "" when absent."""
    key = _sec_key(heading)
    if not key:
        return ""
    parts: list[str] = []
    for m in _HEADING_RE.finditer(source):
        if _sec_key(m.group(1)) != key:
            continue
        level = len(m.group(0)) - len(m.group(0).lstrip("#"))
        nxt = re.compile(rf"^#{{1,{level}}}\s", re.M).search(source, m.end())
        parts.append(source[m.start():nxt.start() if nxt else len(source)].strip())
    return "\n\n".join(parts)


def concept_entries(ops: list[dict], source: str = "") -> list[dict]:
    """Payload concept rows that make VALIDATE hold for these ops, in the shape
    `payload.build_concept_entry` emits (name, excerpt, hint, collision).

    The excerpt is the idea's own source section, whole: the span-grounding
    gate and the extractive floor compare bodies against it, and the
    keyphrase pipeline's 450-char windows are what flagged verbatim formulas
    as "not grounded" (7 of 24 notes on the 2026-09-02 before arm). A section
    the source does not have falls back to the whole document."""
    entries: list[dict] = []
    seen: set[str] = set()
    for op in ops:
        name = op.get("heading")
        if not name or name in seen:
            continue
        seen.add(name)
        collision = {"path": op["path"], "match_type": "title", "total_hits": 1, "excerpt": ""} \
            if op.get("op") == "patch" else None
        excerpt = section_text(source, op.get("section") or "") or source
        entries.append({"name": name, "action_hint": "enrich" if collision else "create",
                        "inbox_excerpt": excerpt[:MAX_SECTION_EXCERPT], "vault_collision": collision})
    return entries


# ------------------------------------------------------------ vault outline --
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.+?)\s*$", re.M)


def _claim_of(body: str) -> str:
    m = _BLOCKQUOTE_RE.search(body)
    if m:
        return m.group(1)
    prose = re.sub(r"\$\$.*?\$\$", " ", body, flags=re.S)          # display math first: it spans lines
    prose = re.sub(r"^#.*$|!\[\[.*?\]\]", " ", prose, flags=re.M)   # headings and embeds, line-bound
    prose = re.sub(r"\s+", " ", prose).strip()
    return (re.split(r"(?<=[.!?])\s", prose, 1)[0] or "")[:200]


def vault_outline(target_dir: str, *, exclude_titles: AbstractSet[str] = frozenset()) -> list[dict]:
    """Rows {title, claim, lesson, path} for every idea note under `target_dir`.

    Folder-scoped by design: stage B reads titles + one-line claims, and a
    folder is the unit the user nucleates into. Spine notes and the hub are
    excluded (see SPINE_SECTION). Best-effort per note: an unreadable note is
    one missing row, never a failed run.
    """
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter
    from silica.tools.atomic import notes_under

    rows: list[dict] = []
    for rel in notes_under(target_dir):
        title = _stem(rel)
        if title in exclude_titles:
            continue
        try:
            content = DRIVER.read_note(rel).content or ""
            data, _raw, body = frontmatter.split(content)
        except Exception as e:  # one bad note must not blind the whole stage
            logger.debug("vault_outline: skipping %s (%s)", rel, e)
            continue
        data = data if isinstance(data, dict) else {}
        if str(data.get("section") or "") == SPINE_SECTION:
            continue
        if data.get("superseded_by"):
            # A merge loser shown as a live note draws the model's same_as
            # edges onto the stub instead of the winner (Lezione 8, f30ace50).
            continue
        srcs = data.get("sources") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        lesson = _stem(str(srcs[0])) if srcs else ""
        rows.append({"title": title, "claim": _claim_of(body or ""), "lesson": lesson, "path": rel})
        if len(rows) >= MAX_OUTLINE_ROWS:
            break
    return rows


# -------------------------------------------------------------- the runner --
_LATEX_N = re.compile(r"n(?=abla\b|eq\b|e\b|ot\b|u\b|ewline\b)")


def _repair_json_escapes(text: str) -> str:
    """Double the backslashes a model left single inside JSON strings.

    LaTeX-heavy bodies come back with `\\Rightarrow` (invalid escape: the
    whole reply is lost to a retry) and `\\boldsymbol` (`\\b` is a valid
    escape: it parses to a backspace and "$oldsymbol{...}" lands in the note,
    seen 2026-09-05). Every escape that is not valid JSON, `\\b \\f \\r \\t`
    followed by a letter, `\\u` not followed by four hex digits and the few
    LaTeX commands that start with `n` are doubled; `\\\\` is left alone so
    a reply that did escape is unchanged.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c != "\\" or i + 1 >= n:
            out.append(c); i += 1; continue
        nxt = text[i + 1]
        if nxt == "\\":
            out.append("\\\\"); i += 2; continue
        latex = (
            nxt not in '"/bfnrtu'
            or (nxt in "bfrt" and text[i + 2:i + 3].isalpha())
            or (nxt == "u" and not re.match(r"[0-9a-fA-F]{4}", text[i + 2:i + 6]))
            or (nxt == "n" and _LATEX_N.match(text, i + 1))
        )
        out.append("\\\\" if latex else "\\")
        i += 1
    return "".join(out)


def _decode_objects(text: str, start: int) -> dict:
    """The JSON object at `start`, merged with any objects that follow it.

    Two reply shapes of 2026-09-05 live here: a literal newline inside a body
    string (strict=False), and two objects back to back (`{...}\n{...}`: the
    slice to the last brace failed at the boundary, one retry per batch).
    Later objects merge dict-into-dict and list-onto-list at the top level, so
    a split "bodies" comes back whole.
    """
    import json as _json
    dec = _json.JSONDecoder(strict=False)
    obj, idx = dec.raw_decode(text, start)
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    while True:
        nxt = text.find("{", idx)
        if nxt < 0 or text[idx:nxt].strip():
            break
        try:
            extra, idx = dec.raw_decode(text, nxt)
        except ValueError:
            break
        for k, v in (extra.items() if isinstance(extra, dict) else []):
            if isinstance(v, dict) and isinstance(obj.get(k), dict):
                obj[k].update(v)
            elif isinstance(v, list) and isinstance(obj.get(k), list):
                obj[k].extend(v)
            else:
                obj.setdefault(k, v)
    return obj


def _unwrap_string_encoded(text: str) -> str:
    """A reply that is the JSON *string encoding* of its object (`{\\"bodies\\":
    ...`, every quote escaped, backslashes doubled twice) decodes to the object
    text in one string-literal pass; seen on a bodies call, 2026-09-05."""
    if not text.startswith('{\\"'):
        return text
    try:
        import json as _json
        return _json.loads('"' + text + '"')
    except ValueError:
        return text


def bodies_workers_for(source_text: str) -> int:
    """How many bodies calls the worker's context holds at once.

    A hosted provider gives each request its own context, so BODIES_WORKERS
    stands. llama-server shares one KV across its slots: a 4B at 32k took four
    concurrent calls of ~8.2k tokens and 500d "Context size has been exceeded"
    39 times (2026-10-12). Unknown window: BODIES_WORKERS, as before.
    """
    from silica.agent.compaction import TEXT_CHARS_PER_TOKEN
    from silica.agent.providers import model_limits
    from silica.config import CONFIG

    provider, model = CONFIG.worker_provider, CONFIG.worker_model
    if not provider or not model:
        provider, model = CONFIG.provider, CONFIG.model
    if provider != "custom":
        return BODIES_WORKERS
    window, _ = model_limits(provider, model or "")
    if not window:
        return BODIES_WORKERS
    per_call = (len(STAGE_BODIES) + len(source_text)) // TEXT_CHARS_PER_TOKEN + BODIES_REPLY_TOKENS
    return max(1, min(BODIES_WORKERS, window // per_call))


def _default_ask(system: str, user: str, *, max_tokens: int, raw: bool = False) -> dict:
    """One JSON reply from the worker model, no tools, no thinking; `raw`
    returns {"text": reply} for the marker-delimited bodies stage (one retry
    on an empty reply or one without a marker).

    reasoning=False: measured 2026-09-02 on deepseek-v4-flash, the thinking
    trace ate the whole completion budget and the reply carried no JSON.
    """
    import threading
    from silica.agent.providers import get_provider
    from silica.config import CONFIG
    from silica.kernel.prep_delegation import _call_with_deadline
    provider = get_provider(CONFIG, role="worker")
    schema_for_retry = next((m for tag, m in SCHEMA_BY_TAG.items() if system.startswith(tag)), None)
    last = ""
    for attempt in range(2):
        # Free JSON first, constrained decode only on the retry: measured
        # 2026-09-02, the free reply closed lecture 12's outline in 1.3k
        # tokens (7/7 calls in the prototype, 9/9 in the first product runs)
        # while the schema-constrained one repeated itself past 12k tokens
        # twice. The two fail differently, which is what makes the retry a
        # second chance rather than the same ask again.
        schema = schema_for_retry if attempt and not raw else None
        # Wall-clock bound, same helper and knob as run_distiller: a dead
        # upstream keeps the socket alive with keep-alive bytes, and the first
        # live run of this lane (2026-09-02) sat ten minutes on one such call.
        def _once(temperature: float, schema=schema):
            abandoned = threading.Event()
            return _call_with_deadline(lambda: provider.call_llm(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=None, response_schema=schema, max_tokens=max_tokens, temperature=temperature,
                reasoning=False, openrouter_provider=CONFIG.openrouter_provider_distiller, cancel=abandoned,
                # Temperature 0: a 500 the client retried came back as the same
                # reply, three times on Lezione 8 (llama-server, 2026-10-12). This
                # loop and the stage's re-ask are the retries.
                sdk_retries=0,
            ), float(os.getenv("DISTILLER_TIMEOUT", "300")), abandoned)

        from litellm import InternalServerError
        try:
            resp = _once(0.0)
        except InternalServerError as exc:
            # llama-server b10053 500s a finished Qwen3.5 reply its chat parser
            # cannot read ("does not match the expected peg-native format").
            # At temperature 0 every retry is that reply again: Lezione 8 lost
            # the same 4 ideas twice (2026-10-12). One ask off zero is a
            # different reply; any other 500 is the caller's, as before.
            if "does not match the expected" not in str(exc):
                raise
            logger.warning("outline stage: server could not parse the reply, asking once at 0.3")
            resp = _once(0.3)
        if resp is None:
            raise TimeoutError("outline stage call abandoned")
        if raw:
            # Any text goes back to the parser (headings without the IDEA
            # prefix are matched there); only an empty reply is retried.
            if (resp.text or "").strip():
                return {"text": resp.text or ""}
            last = f"finish={resp.finish_reason} empty reply"
            logger.warning("outline stage reply unusable (attempt %d): %s", attempt + 1, last)
            continue
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.text or "").strip())
        text = _unwrap_string_encoded(text)
        text = _repair_json_escapes(text)
        start = text.find("{")
        why = "no JSON object"
        if start >= 0:
            try:
                return _decode_objects(text, start)
            except ValueError as exc:
                why = str(exc)
        # The head alone never said what broke: 3 of the first 5 lecture runs
        # of 2026-09-05 retried on replies whose first 200 chars parsed fine.
        last = f"finish={resp.finish_reason} {why} head={text[:200]!r}"
        logger.warning("outline stage reply unusable (attempt %d): %s", attempt + 1, last)
        if resp.finish_reason == "length":
            max_tokens *= 2  # a cut JSON: the same ask with room to finish
    raise ValueError(f"outline stage returned no {'bodies' if raw else 'JSON'} after retry ({last})")


def _match_bodies(reply: dict, titles: list[str],
                  sections: dict[str, str] | None = None) -> dict[str, str]:
    """Reply bodies keyed back to the requested titles.

    Tolerates a reply without the "bodies" wrapper and keys the model re-cased,
    padded or bolded: both shapes came back on 2026-09-05 and, matched by exact
    key, left 15 of 82 notes with the claim alone. Empty bodies count as missing.
    A body keyed by the SECTION is taken only when one requested idea comes
    from that section: several ideas under one section share one text then,
    and no split of it is right.
    """
    raw = reply.get("bodies") if isinstance(reply.get("bodies"), dict) else reply
    # First pipe field: the model echoes the "title | section" row it was
    # shown as the key (8 of 8 bodies of one batch lost that way, 2026-09-05).
    by_key = {_clean_title(str(k).split("|")[0]).casefold(): v for k, v in (raw or {}).items()
              if isinstance(v, str) and v.strip()}
    got = {t: by_key[_clean_title(t).casefold()] for t in titles if _clean_title(t).casefold() in by_key}
    for t in titles:
        if t in got or not sections or not sections.get(t):
            continue
        sec = _clean_title(sections[t]).casefold()
        if sec in by_key and sum(1 for u in titles if _clean_title(sections.get(u, "")).casefold() == sec) == 1:
            got[t] = by_key[sec]
    # Last resort, a paraphrased key ("multi-classe" -> "multi-cluster",
    # 2026-09-05): the closest unused key, and only when it is close.
    from difflib import SequenceMatcher
    used = {k for k, v in by_key.items() if any(v is w for w in got.values())}
    for t in titles:
        if t in got:
            continue
        key = _clean_title(t).casefold()
        best = max(((SequenceMatcher(None, key, k).ratio(), k) for k in by_key if k not in used), default=(0, ""))
        if best[0] >= 0.85:
            got[t] = by_key[best[1]]
            used.add(best[1])
    return got


def skeleton(src: str, *, language: str, ask: Callable[..., dict]) -> Outline:
    """Stage A, then the coverage pass when a heading has no idea: the ideas
    and their typed edges, every source heading claimed or skipped. Shared
    with the study-mode inventory (docs/specs/study-mode.md D31), whose key
    is this same reconstruction of the argument."""
    rels = ", ".join(RELATIONS)
    outline = parse_outline(ask(STAGE_A.format(lang=language, rels=rels), src, max_tokens=8000))
    gaps = coverage_gaps(outline, source_headings(src))
    if not gaps:
        return _unshout(outline, src)
    extra = ask(STAGE_GAP.format(gaps="\n".join(f"- {g}" for g in gaps), lang=language, rels=rels),
                "IDEAS SO FAR:\n" + "\n".join(f"{i.title} | {i.section}" for i in outline.ideas)
                + "\n\nSOURCE:\n" + src, max_tokens=4000)
    # A coverage idea exists to close a named section; one without a
    # section closes nothing and reaches the bodies stage as
    # "(section: )", which is how the claim-only notes were born.
    filled = [i for i in (extra.get("ideas") or [])
              if isinstance(i, dict) and _clean_title(i.get("section"))]
    for i in (extra.get("ideas") or []):
        if isinstance(i, dict) and not _clean_title(i.get("section")):
            logger.info("outline coverage: idea %r dropped, no section", i.get("title"))
    merged = parse_outline({"lesson_title": outline.lesson_title, "spine": outline.spine,
                            "ideas": [i.__dict__ for i in outline.ideas] + filled})
    merged.skips = [s for s in (extra.get("skips") or []) if isinstance(s, dict)]
    return _unshout(merged, src)


_WORD_RE = re.compile(r"[^\W\d_]+")
_HEADING_LINE_RE = re.compile(r"(?m)^[ \t]*#{1,6}\s.*$")


def _unshout(outline: Outline, src: str) -> Outline:
    """An idea titled in capitals gets each word as the lecture's body spells it.

    Qwen3.5-9B copied 33 slide headings as titles in the coverage pass ("STORIA
    DELL'AI: TEST DI TURING", 2026-10-12). The body decides, not a rule on word
    length, which would keep the Italian "DI" and "DELL" as acronyms: a word the
    body writes in lower or title case takes its commonest such spelling, one
    the body only writes in capitals is an acronym and stays, one only the
    headings use goes lower case. Spine and depends_on follow the rename.
    """
    from collections import Counter

    shouted = [i for i in outline.ideas
               if (letters := "".join(_WORD_RE.findall(i.title))) and letters.isupper() and len(letters) > 1]
    if not shouted:
        return outline
    forms: dict[str, Counter] = {}
    for w in _WORD_RE.findall(_HEADING_LINE_RE.sub("", src)):
        forms.setdefault(w.casefold(), Counter())[w] += 1

    def spell(m: re.Match) -> str:
        seen = forms.get(m.group(0).casefold())
        if not seen:
            return m.group(0).lower()
        quiet = [w for w, _ in seen.most_common() if not w.isupper()]
        return quiet[0] if quiet else m.group(0)

    rename: dict[str, str] = {}
    for idea in shouted:
        new = _WORD_RE.sub(spell, idea.title)
        new = new[:1].upper() + new[1:]
        if new not in outline.titles():
            rename[idea.title] = new
    for idea in outline.ideas:
        idea.title = rename.get(idea.title, idea.title)
        for d in idea.depends_on:
            if d.get("title") in rename:
                d["title"] = rename[d["title"]]
    outline.spine = [rename.get(t, t) for t in outline.spine]
    return outline


def run_outliner(*, source_text: str, source_basename: str, target: str, hub: str | None,
                 language: str, vault_outline: list[dict],
                 ask: Callable[..., dict] | None = None,
                 outline: Outline | None = None, only_titles: set[str] | None = None,
                 steer_context: str | None = None) -> dict:
    """The lane's DELEGATE: source text -> {"updates", "ephemerals", "concepts", "outline", "gaps"}.

    `outline` + `only_titles` is the steer retry: VALIDATE rejected some
    bodies, so only those are regenerated (with the feedback appended) and the
    outline, spine and edges are not paid for again.
    """
    src = strip_images(source_text)
    # Sized only for the real worker: an injected `ask` is a test or a probe.
    workers = BODIES_WORKERS if ask else bodies_workers_for(src)
    ask = ask or _default_ask
    if outline is None:
        outline = skeleton(src, language=language, ask=ask)
    titles = [i.title for i in outline.ideas if only_titles is None or i.title in only_titles]

    def _bodies(batch: list[str]) -> dict[str, str]:
        section_of = {t: next(i.section for i in outline.ideas if i.title == t) for t in batch}
        # Quoted titles with the section set apart: shown as "title | section"
        # rows, the model keyed a whole lecture's bodies by the section (three
        # batches of Lezione 1, 2026-09-05) or echoed the row as the key.
        user = "SOURCE:\n" + src + "\n\nIDEAS:\n" + "\n".join(
            f'- "{t}" (section: {section_of[t]})' for t in batch)
        if steer_context:
            user += "\n\nPREVIOUS ATTEMPT WAS REJECTED:\n" + steer_context
        from litellm import InternalServerError
        try:
            reply = ask(STAGE_BODIES.format(lang=language), user, max_tokens=12000, raw=True)
        except (ValueError, TimeoutError, InternalServerError) as exc:
            # One batch must not take the lecture down: Lezione 8 landed 0
            # notes when a batch failed twice (2026-09-05). Its titles go to
            # the re-ask and, at worst, land as claim-only notes flagged for
            # review, which the source kept in the inbox does not.
            # InternalServerError since 2026-10-12: llama-server b10053 500s a
            # finished Qwen3.5 reply its chat parser cannot read, the same reply
            # on every retry at temperature 0, and it rolled back Lezione 8 whole.
            logger.warning("outline bodies: batch of %d failed (%s); asking again for its titles", len(batch), exc)
            return {}
        bodies = _parse_bodies_text(reply["text"], batch) if "text" in reply else reply
        got = _match_bodies(bodies, batch, sections=section_of)
        if len(got) < len(batch):
            inner = nested if isinstance(nested := bodies.get("bodies"), dict) else bodies
            logger.warning("outline bodies: no body for %s (reply keys: %s)",
                           [t for t in batch if t not in got], list(inner)[:12])
        return got

    from concurrent.futures import ThreadPoolExecutor
    batches = [titles[k:k + BODIES_BATCH] for k in range(0, len(titles), BODIES_BATCH)]
    got: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
        for part in pool.map(_bodies, batches):
            got.update(part)
    missing = [t for t in titles if t not in got]
    if missing:
        # One more ask, only for what came back empty: cheaper than landing a
        # claim-only note the gate can only flag.
        for k in range(0, len(missing), BODIES_BATCH):
            got.update(_bodies(missing[k:k + BODIES_BATCH]))
        missing = [t for t in titles if t not in got]
    for idea in outline.ideas:
        if idea.title in got:
            idea.body = got[idea.title]
    edges: list[dict] = []
    existing = {r["title"]: r["path"] for r in vault_outline}
    if vault_outline and only_titles is None:
        from litellm import InternalServerError
        try:
            reply = ask(STAGE_B.format(lang=language, rels=", ".join(CROSS_RELATIONS)),
                        "EXISTING NOTES:\n" + "\n".join(f"{r.get('lesson', '')} | {r['title']} | {r.get('claim', '')}" for r in vault_outline)
                        + "\n\nNEW SOURCE IDEAS:\n" + "\n".join(f"{i.title} | {i.claim}" for i in outline.ideas),
                        max_tokens=4000)
        except (ValueError, TimeoutError, InternalServerError) as exc:
            # The cross-lecture edges are the one optional stage: the ideas and
            # bodies above are the lecture. Qwen3.5-9B ran stage B past 30k chars
            # twice (finish=length at 4000 and 8000 tokens, 2026-10-12) and the
            # raise rolled back Lezioni 9 and 11 whole. Without the edges the
            # notes still land; links to earlier lectures are what is lost.
            logger.warning("outline stage B failed (%s); the lecture lands without cross edges", exc)
            reply = {}
        edges = select_edges(reply.get("edges") or [], ideas=set(outline.titles()), existing=existing,
                             spine_titles={r["title"] for r in vault_outline if r.get("spine")})
    ops = outline_ops(outline, target=target, hub=hub, source_basename=source_basename,
                      edges=edges, existing=existing, only_titles=only_titles)
    return {
        "updates": ops, "ephemerals": [], "concepts": concept_entries(ops, src), "missing_bodies": missing,
        "outline": {"lesson_title": outline.lesson_title, "spine": outline.spine,
                    "ideas": [i.__dict__ for i in outline.ideas], "skips": outline.skips},
        "gaps": coverage_gaps(outline, source_headings(src)),
    }
