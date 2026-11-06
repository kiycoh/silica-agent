# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One check of one note (ADR-0110): the rules of points 4 to 6, then the
judge of point 7 on each block the rules did not settle.

The two search legs come in as arguments. `search(query, k)` is the chat's
passage search (silica_search_context's engine), whose hits carry `coverage`;
`dense(text, pool)` is the best cosine of a block against the pool's notes, or
None when the vault has no vectors. Passed in, they keep this module free of
the agent and its network, and a test hands in a list.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from silica.kernel.check import GRADES, ORDER
from silica.kernel.check import blocks as blk
from silica.kernel.check import pool as pl
from silica.kernel.check import rules
from silica.kernel.text import dates
from silica.kernel.write.contested import lifecycle, trust

# Both floors are set at step 1's gate (point 14) and frozen for step 2. The
# lexical floor is the share of a block's rare-term weight a passage must
# match; the dense floor is a cosine, so it belongs to one embedder (ADR-0078)
# and an embedder without a measured row decides no absence at all.
# Measured 2026-10-24 on the five gate drafts over the legal testbench (ORACLE.md,
# "ADR-0110 step 1 gate"), each draft alone, pool-restricted, EmbeddingGemma 2:
# the lecture-notes blocks, which have no source, scored coverage 0.0 and cosine
# at most 0.630; the blocks with a source in the pool and no link, name or
# identifier scored coverage 0.06 to 0.34 (Italian over English) and cosine at
# least 0.665. The lexical floor sits on the safe side: anything that clears it
# goes to the model. The dense margin is 0.015 a side on 29 blocks; re-measure
# on the first 200-note vault a gate runs on, or by 2027-01-31.
LEXICAL_FLOOR = 0.3
# jina v5 small, measured the same way the same day once its drive was
# mounted, dates counted as sources (point 4.3): no-source blocks at most
# 0.234, sourced blocks with no link, name, identifier or date at least 0.294.
# A thinner absolute margin than Gemma's, the same re-measure condition.
DENSE_FLOORS: dict[str, float] = {"embeddinggemma-2": 0.645, "jina-embeddings-v5-text-small": 0.26}


def dense_floor(model: str) -> float | None:
    """The floor for the configured embedder id, matched the way its config
    row is (config.embedder_defaults: the key inside the id)."""
    m = (model or "").lower()
    return next((v for k, v in DENSE_FLOORS.items() if k in m), None)
PASSAGES = 5
SEARCH_WIDTH = 200
# Point 13: a pass over more model calls than this starts only on the
# person's go, with the count shown. 60 blocks is about $0.005 at the
# server's prices of 2026-10-13, a fifth of a Free window (ADR-0102).
CONFIRM_OVER = 60
PASSAGE_QUOTE = 240

Search = Callable[[str, int], list[dict]]
# The pool's nearest notes to a block, best first, as (path, cosine); [] or
# None when the vault has no vectors.
Dense = Callable[[str, set[str]], "list[tuple[str, float]] | None"]
Judge = Callable[[str, list[dict]], "dict | None"]
CACHE_DIR = ".silica/checks"
# A note this short is read whole, never by a search window. Two of the
# three misses of the second test set were windows: one cut at "Agreements
# known to have been drafted fro" before "none recorded", one the wrong
# section of a 900-character note (2026-10-25). The testbench's shared notes
# run 300 to 1,500 characters of body.
# It is also the most a longer note's passage carries (_excerpt). At 1,800,
# three of Jev's seven misses on ADR-0134's gate (2026-10-27) were a note
# shown as its first 1,800 characters or a 600-character window, the
# deciding sentence 2,121 to 2,581 characters into its body. 4,000 holds a
# whole section for 95% of the course vault's 13,008 sections and a whole
# note for 66% of its 2,166 notes (measured 2026-10-27).
NOTE_CHARS = 4000
# All of one judge call's passages together: four at NOTE_CHARS, about the
# 8 x 1,800 = 14,400 the released check could already send. On ADR-0134's
# gate (2026-10-27) its calls ran at a median of 13,976 characters (4,796
# before) and Jev's 94 fresh requests took 0.37 s median, 0.67 s p95, none
# unanswered (decide.py's budget is 3 s). Cited notes are kept first, then the
# search and the dense leg in turn by rank: dropping the dense neighbours
# first cost two faithful blocks their source to lower lexical hits, one a
# "passo" (stride) block read against gradient descent's step.
JUDGE_CHARS = 16000
# silica_core's own section split (core._HEADING), so a hit's line falls in
# exactly one of the sections _excerpt reads.
_SECTION = re.compile(r"(?m)^(?=#{1,4} )")
# Fewer body words than this under a window's headings and frontmatter, and
# the window is a title, not a passage (_thin).
THIN_WORDS = 8


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _periods(path: str, text: str) -> list[dict]:
    return [{**p, "origin": rules.at(path, _line_of(text, p["start"]))} for p in dates.find_periods(text)]


def _status(path: str, note: pl.Note) -> str | None:
    """The source-status sentence for a linked or named source, or None."""
    lc = lifecycle(note.fm)
    if lc == "superseded":
        return f"{note.stem} is superseded by {note.fm.get('superseded_by')}"
    if lc == "contested":
        return f"{note.stem} is marked contested"
    if trust(note.text) == "distilled":
        return f"{note.stem} is Silica's summary, not the source's own words"
    return None


def _cache_path(vault: str, path: str) -> Path:
    return Path(vault) / CACHE_DIR / (hashlib.sha1(path.encode()).hexdigest()[:16] + ".json")


def _body(note: pl.Note) -> str:
    return note.text[sum(len(x) + 1 for x in note.text.split("\n")[:blk.body_start(note.text)]):]


def _excerpt(note: pl.Note, lines: list[int] | None = None) -> tuple[int, str]:
    """(first line, text): what the judge reads of a note. A note up to
    NOTE_CHARS is read whole. A longer one is read by its sections: first
    those the block's search hits landed in (`lines`, best first), then the
    rest from the top while they fit, joined in the note's own order. A
    hit's own section can rank low or not at all for the block that needs
    it: the SOM note's neighbourhood formula sits in a section outside the
    top 40 hits of the block that misstates it (2026-10-27)."""
    skip = blk.body_start(note.text)
    body = _body(note)
    if len(body) <= NOTE_CHARS:
        return skip + 1, body
    secs, off = [], 0  # (first line, text)
    for part in _SECTION.split(body):
        if part.strip():
            secs.append((skip + 1 + body.count("\n", 0, off), part))
        off += len(part)
    hit: dict[int, int] = {}  # section -> its best hit line
    for ln in lines or []:
        hit.setdefault(max((i for i, s in enumerate(secs) if s[0] <= ln), default=0), ln)
    take, left = [], NOTE_CHARS
    for i in list(hit) + [i for i in range(len(secs)) if i not in hit]:
        first, part = secs[i]
        if len(part) <= left:
            take.append(i)
            left -= len(part)
        elif not take:
            # shortcut: a section over NOTE_CHARS alone (5% of the course
            # vault's, the longest 1% over 9,652 characters) is cut to the
            # cap from a quarter before its hit, so a sentence further on is
            # lost again; reopen when a gate misses on one.
            at = len("\n".join(part.split("\n")[:hit.get(i, first) - first]))
            start = part.rfind("\n", 0, max(0, min(at - NOTE_CHARS // 4, len(part) - NOTE_CHARS))) + 1
            return first + part.count("\n", 0, start), part[start:start + NOTE_CHARS]
    take.sort()
    return secs[take[0]][0], "".join(secs[i][1] for i in take)


def _thin(window: str) -> bool:
    """A search window that is only a note's title and subtitle, or its
    frontmatter: the note's words matched, its point sits further down. All
    four misses of step 2's test set were such windows (2026-10-25): the
    judge was handed "# Note on disclosure ... On models A to E." and never
    the sentence it needed, so it answered "not in these passages"."""
    lines = window.split("\n")[blk.body_start(window):]
    return len([w for ln in lines if not ln.lstrip().startswith("#") for w in ln.split()]) < THIN_WORDS


def _short(note: pl.Note | None) -> bool:
    return note is not None and len(_body(note)) <= NOTE_CHARS


def _model_passages(passages: list[dict], cited: list[str], near: list[tuple[str, float]],
                    notes: dict[str, pl.Note], found: list[dict] | None = None) -> list[dict]:
    """What the judge reads: the search's passages, then the notes the block
    links or names, then the pool's nearest notes by meaning, which is how an
    Italian block reaches its English source when no word matches. A note is
    read by _excerpt, one passage per note, from the sections every hit of
    the block's search (`found`, the whole ranking) landed in; a window is
    kept only for a hit outside the walked notes."""
    lines: dict[str, list[int]] = {}
    for h in found or passages:
        lines.setdefault(h["path"], []).append(h.get("line", 1))
    out, thin = [], []
    for h in passages:
        text = h.get("window") or h.get("snippet") or ""
        note = notes.get(h["path"])
        if _thin(text) or _short(note):
            thin.append(h["path"])
        elif note is None:
            out.append({"path": h["path"], "line": h.get("line", 1), "text": text})
        elif h["path"] not in {o["path"] for o in out}:
            line, text = _excerpt(note, lines.get(h["path"]))
            out.append({"path": h["path"], "line": line, "text": text})
    # A thin hit's section is already in its note's read above, ahead of the
    # rest: Brindle's "Ratio" window once came without the "Holding" above it.
    seen = {o["path"] for o in out}
    for path in list(dict.fromkeys(thin + cited + [p for p, _s in near[:2]])):
        if path not in seen and path in notes:
            seen.add(path)
            line, text = _excerpt(notes[path], lines.get(path))
            out.append({"path": path, "line": line, "text": text})
    # The budget's order: cited notes, then the two legs taken in turn by rank.
    prio: dict[str, int] = {}
    for r, h in enumerate(passages):
        prio.setdefault(h["path"], 2 * r + 1)
    for j, (p, _s) in enumerate(near[:2]):
        prio[p] = min(prio.get(p, 2 * j + 2), 2 * j + 2)
    prio.update(dict.fromkeys(cited, 0))
    kept, left = set(), JUDGE_CHARS
    for i in sorted(range(len(out)), key=lambda i: prio.get(out[i]["path"], len(out) * 2)):
        if len(out[i]["text"]) <= left:
            kept.add(i)
            left -= len(out[i]["text"])
    return [o for i, o in enumerate(out) if i in kept]


def _verdict_rows(v: dict, line: int) -> tuple[list[dict], list[dict]]:
    """A judge's verdict as findings and states: the grade from the table,
    never from the judge (point 8)."""
    who = "Jev" if v.get("by") == "jev" else "the chat model"
    p = f", p {v['p']:.2f}" if "p" in v else ""
    if v.get("state") == "not checked":
        why = v.get("why") or f"{who} was not sure enough{p}"
        return [], [{"state": "not checked", "line": line, "say": why}]
    kind = v["kind"]
    if kind in ("supported", "not a statement of fact"):
        # `at` so a selection's box can name what backs it; a verdict cached
        # before 2026-11-06 has none, and the box then says only "supported".
        at = {"at": v["at"]} if v.get("at") else {}
        return [], [{"state": kind, "line": line, "say": f"{who}{p}", **at}]
    kind = "not found" if kind == "not in these passages" else kind
    # `model`: the drawer turns these hollow on an edit until the next check,
    # while the rules run again (point 11).
    return [{"kind": kind, "words": "", "at": v.get("at", ""), "say": f"{_VERDICT_SAY[kind]} ({who}{p})",
             "model": True}], []


# The one sentence a verdict's box and line carry (point 11); the kind is
# already beside it, so the sentence says what it asks of the writer.
_VERDICT_SAY = {
    "contradicted": "a source gives a different value for this block's point",
    "overstated": "a source says something narrower than this block does",
    "off-point": "the passage it rests on speaks to something else",
    "not found": "the passages on its subject do not say this",
}


def _quote(text: str) -> str:
    """A passage as the box quotes it: a table's rows read as text, not as
    the pipes and separator rows a note excerpt carries."""
    out = []
    for ln in text.split("\n"):
        if re.fullmatch(r"\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*", ln):
            continue
        if ln.lstrip().startswith("|"):
            ln = " · ".join(c.strip() for c in ln.strip().strip("|").split("|")) + ";"
        out.append(re.sub(r"^\s*#+\s*", "", ln))
    flat = " ".join(" ".join(out).split())
    return flat if len(flat) <= PASSAGE_QUOTE else flat[:PASSAGE_QUOTE - 1].rstrip() + "…"


def _summary(path: str, blocks: list, pool: pl.Pool, findings: list[dict], states: list[dict],
             waiting: int, cached: int) -> dict[str, Any]:
    findings = sorted(findings, key=lambda f: (ORDER.index(f["grade"]), f["line"]))
    return {
        "note": path, "blocks": len(blocks), "waiting": waiting, "cached": cached,
        "pool": {"size": len(pool.paths), "of": pool.of, "scope": pool.scope, "values": sorted(pool.values)},
        "findings": findings, "states": states,
        "counts": {g: sum(f["grade"] == g for f in findings) for g in ORDER},
    }


def overlapping(blocks: list, lines: tuple[int, int]) -> list:
    """The blocks a 1-based inclusive range touches. Overlap, not containment:
    a selection inside a paragraph of several lines lies inside no block's
    span, and containment found none (ADR-0110, amendment of 2026-11-06, point
    4). ADR-0134's cards hold whole blocks, so they read the same. `end` is 0
    on a Block split() has not finished."""
    return [b for b in blocks if b.line <= lines[1] and (b.end or b.line) >= lines[0]]


def read_cache(vault: str, path: str) -> dict:
    """{"verdicts": {key: verdict}, "report": the last check's report}; empty
    when the note was never checked or its file is torn."""
    try:
        data = json.loads(_cache_path(vault, path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # a torn cache costs one re-judgment, never a wrong verdict
    return data if isinstance(data, dict) and "verdicts" in data else {}


def check_note(path: str, vault: str, *, write_dir: str = "", search: Search | None = None,
               dense: Dense | None = None, dense_floor: float | None = None,
               judge: Judge | None = None, cache_only: bool = False,
               progress: Callable[[dict], None] | None = None,
               cancelled: Callable[[], bool] | None = None,
               lines: tuple[int, int] | None = None) -> dict[str, Any]:
    """The report for the note at vault-relative `path`: rule findings, and
    with a `judge` the verdict on each block the rules did not settle.

    Two passes, so the drawer shows the rules at once and the verdicts as
    they come (point 11): `progress` gets {"event": "rules", report} after
    the first and {"event": "block", ...} after each judged block. With
    `cache_only`, only the verdicts already paid for are read and nothing is
    asked: the drawer's rerun after an edit. `cancelled` is polled before
    each call; the verdicts paid for until then are kept. `lines`, a
    1-based inclusive range, checks only the blocks it touches: one card of
    a reading the reader writes (ADR-0134 point 4), or a selection, which
    widens to its blocks (ADR-0110, amendment of 2026-11-06, point 4)."""
    notes, files = pl.walk(vault)
    if path not in notes:
        raise FileNotFoundError(path)
    me = notes[path]
    blocks, defs = blk.split(me.text)
    if lines is not None:
        blocks = overlapping(blocks, lines)
    res = pl.Resolver(notes, files)
    body = me.text[sum(len(x) + 1 for x in me.text.split("\n")[:blk.body_start(me.text)]):]
    named_all = {p for _t, p in res.links(body) if p} | res.mentions(body)
    named_all.discard(path)
    pool = pl.build(path, notes, pl.party_keys(vault), write_dir, named_all)
    ix = rules.index(notes, skip=path)
    findings: list[dict] = []
    states: list[dict] = []
    waiting = cached = 0
    reading = judge is not None or cache_only
    old = read_cache(vault, path) if reading else {}
    cache = old.get("verdicts", {})
    fresh: dict[str, dict] = {}
    pending: list[tuple[Any, list[dict], str, list[str]]] = []  # (block, passages, key, cited)
    seen_source_dates: set[tuple[str, str]] = set()
    prev_sourced = False
    prev_cited: list[str] = []

    def keep(b, mine: list[dict]) -> None:
        for x in mine:
            findings.append({"grade": GRADES[x["kind"]], "line": b.line, "end": b.end, "at": x.get("at", ""), **x})

    for b in blocks:
        text = " ".join([b.clean] + [defs[f] for f in b.footnotes if f in defs])
        mine: list[dict] = []
        srcs: list[str] = []
        for target, hit in res.links(" ".join([b.text] + [defs[f] for f in b.footnotes if f in defs])):
            if hit is None:
                mine.append({"kind": "not in the vault", "words": target,
                             "say": f"[[{target}]] links to nothing in the vault"})
            elif hit and hit in pool.paths:
                srcs.append(hit)
        named = sorted((res.mentions(text) & pool.paths) - {path} - set(srcs))
        # Point 4.3: the notes that hold the block's identifiers and absolute
        # dates word for word are sources, as a link is.
        found_in: list[str] = []
        for spelling, key in rules.identifiers(text):
            rows = ix.idents.get(key, [])
            if not rows:
                mine.append({"kind": "not in the vault", "words": spelling,
                             "say": f"no note in the vault holds {spelling}"})
                continue
            inpool = [r for r in rows if r[0] in pool.paths]
            found_in += [r[0] for r in inpool[:1]]
        cited = list(dict.fromkeys(srcs + named))
        for s in cited:
            if (say := _status(s, notes[s])):
                mine.append({"kind": "source status", "words": notes[s].stem, "say": say, "at": s})

        # Dates: the block's own, then those its cited sources give. `mark`
        # is the date as the note spells it, which the drawer underlines.
        scope = [{**p, "origin": f"{path}:{b.line}"} for p in dates.find_periods(text)]
        for s in cited:
            scope += _periods(s, notes[s].text)
        for d, d0, d1 in dates.find_dates(text):
            holders = [r for r in ix.dates.get(d, []) if r[0] in pool.paths]
            found_in += [r[0] for r in holders[:1]]
            local = scope + [p for h in {r[0] for r in holders} - set(cited) for p in _periods(h, notes[h].text)]
            f, st = rules.date_rules(d, local, ix, pool.paths)
            mine += [{**x, "mark": text[d0:d1]} for x in f]
            states += [{**x, "line": b.line, "end": b.end} for x in st]
            if not f and not st and d not in ix.dates:
                mine.append({"kind": "date not found", "words": dates.human(d), "mark": text[d0:d1],
                             "say": f"no note in the vault holds {dates.human(d)}"})
        for s in cited:
            src_periods = _periods(s, notes[s].text)
            for d, _s2, _e2 in dates.find_dates(notes[s].text):
                if (s, d) in seen_source_dates:
                    continue
                seen_source_dates.add((s, d))
                f, _st = rules.date_rules(d, src_periods, ix, pool.paths)
                mine += [{**x, "say": f"{s} gives " + x["say"]} for x in f]

        # Passages, before the quotations: their notes are where a near
        # quotation is looked for.
        found: list[dict] = []
        if search is not None:
            found = [h for h in search(b.clean, SEARCH_WIDTH) if h.get("path") in pool.paths]
        passages = found[:PASSAGES]
        lexical = any(float(h.get("coverage") or 0) >= LEXICAL_FLOOR for h in passages)

        sourced = bool(cited or found_in)
        attributed = b.kind == "quote" and (sourced or prev_sourced)
        quote_found = False
        for q in rules.quotations(b.text, whole=attributed):
            near = list(dict.fromkeys(cited + (prev_cited if attributed else []) + [h["path"] for h in passages]))
            r = rules.find_quote(q, notes, sorted(pool.paths), near)
            short = q if len(q) <= 60 else q[:57] + "..."
            if r["state"] == "verbatim":
                quote_found = True
                if not sourced and not attributed:
                    mine.append({"kind": "quotation without its source", "words": short, "mark": q,
                                 "say": f"{r['path']} holds it word for word; the block names no source",
                                 "at": rules.at(r["path"], r["line"])})
            elif r["state"] == "near":
                quote_found = True
                mine.append({"kind": "misquoted", "words": short, "mark": q, "at": rules.at(r["path"], r["line"]),
                             "say": f"no note holds it word for word; {r['path']} holds {round(r['share'] * 100)}% "
                                    f"of its words in order: “{r['passage']}”"})
            else:
                mine.append({"kind": "not found", "words": short, "mark": q,
                             "say": f"no note of the {len(pool.paths)} searched holds this quotation, even nearly"})

        # Absence by rule (point 6): no link, no named note, no identifier,
        # no quotation found, and no passage over either floor.
        near = (dense(b.clean, pool.paths) or []) if dense is not None else []
        to_judge = True
        if not (sourced or quote_found or lexical):
            best = near[0][1] if near and dense_floor is not None else None
            if best is None:
                to_judge = reading
                if not to_judge:
                    states.append({"state": "not checked", "line": b.line, "end": b.end,
                                   "say": "absence needs the dense floor, and this vault has no vectors for it"})
            elif dense_floor is not None and best < dense_floor:
                to_judge = False
                mine.append({"kind": "not found", "words": "",
                             "say": f"no note of the {len(pool.paths)} searched holds this"})
        keep(b, mine)
        if to_judge:
            waiting += 1
            if reading:
                mp = _model_passages(passages, list(dict.fromkeys(cited + found_in)), near, notes, found)
                key = hashlib.sha1("\0".join([b.clean] + sorted(
                    f"{x['path']}\0{x['text']}" for x in mp)).encode()).hexdigest()
                pending.append((b, mp, key, cited))
        prev_sourced, prev_cited = sourced, cited

    # The model's pass. Cached verdicts first, so a rerun's marks are all on
    # screen before the first call.
    pending.sort(key=lambda x: x[2] not in cache)
    calls = sum(1 for x in pending if x[2] not in cache and x[1])
    if progress:
        progress({"event": "rules", "calls": 0 if cache_only else calls,
                  **_summary(path, blocks, pool, findings, states, waiting, cached)})
    stopped = False
    for n, (b, mp, key, cited) in enumerate(pending, 1):
        mine, st = [], []
        v = cache.get(key)
        if v is not None:
            cached += 1
        elif not mp:
            st.append({"state": "not checked", "line": b.line, "say": "no passage in the pool to judge it against"})
        elif cache_only:
            # Edited, a source changed, or a cancelled run never reached it:
            # the drawer shows which (its marks turn hollow on an edit).
            st.append({"state": "not checked", "line": b.line,
                       "say": "no verdict for this text yet; check sources asks the model"})
        elif stopped or (cancelled and cancelled()):
            stopped = True
            st.append({"state": "not checked", "line": b.line, "say": "the check was cancelled"})
        else:
            assert judge is not None  # pending fills only while reading, and cache_only took the branch above
            v = judge(b.clean, mp)
        if v is not None:
            fresh[key] = v
            f, vst = _verdict_rows(v, b.line)
            # The passage the verdict rests on, for the drawer's box to quote.
            said = {f"{x['path']}:{x['line']}": x["text"] for x in mp}
            for x in f:
                if x.get("at") in said:
                    x["passage"] = _quote(said[x["at"]])
            mine += f
            st += vst
            # Point 5: a searched passage's status counts once a verdict
            # rests on it; a cited source already had its line above.
            rest = (v.get("at") or "").rsplit(":", 1)[0]
            if rest in notes and rest not in cited and (say := _status(rest, notes[rest])):
                mine.append({"kind": "source status", "words": notes[rest].stem, "say": say, "at": rest})
        st = [{**x, "end": b.end} for x in st]
        states += st
        before = len(findings)
        keep(b, mine)
        if progress:
            progress({"event": "block", "line": b.line, "end": b.end, "done": n, "of": len(pending),
                      "findings": findings[before:], "states": st})

    report = _summary(path, blocks, pool, findings, states, waiting, cached)
    if reading and (fresh or old):
        # Only this run's verdicts are kept, so the cache never outgrows the
        # note; a cancelled run keeps the older ones it did not reach too, and
        # so does a ranged run, whose blocks outside the range were never read.
        # The report rides along: the drawer's counts are the file's, and a
        # ranged run's report is one card's, so the whole note's stays.
        # shortcut: a written reading checked card by card only ever adds
        # verdicts, one per edited block; a whole-note check prunes them.
        merge = stopped or lines is not None
        cache_file = _cache_path(vault, path)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"verdicts": {**cache, **fresh} if merge else fresh,
                                          "report": old.get("report", report) if lines is not None else report},
                                         ensure_ascii=False), encoding="utf-8")
    return report


def render(report: dict) -> str:
    """The terminal's form of a report: findings by grade, each with the
    block's line and the `path:line` that decided it."""
    p = report["pool"]
    where = {"whole": "searched the whole vault",
             "party": f"{p['size']} of {p['of']} notes (party: {', '.join(p['values'])})",
             "shared": f"{p['size']} of {p['of']} notes: this note carries no party value, so other parties' notes are out"}[p["scope"]]
    c = report["counts"]
    out = [f"{report['note']} · {report['blocks']} blocks · {where}",
           f"error {c['error']} · warning {c['warning']} · nit {c['nit']}"
           f" · {report['waiting']} block{'s' if report['waiting'] != 1 else ''} wait for the model"]
    for f in report["findings"]:
        tail = f"  [{f['at']}]" if f.get("at") else ""
        words = f" “{f['words']}”" if f.get("words") else ""
        out.append(f"  {f['grade']:<7} {f['kind']:<28} line {f['line']}{words}: {f['say']}{tail}")
    quiet = ("supported", "not a statement of fact")
    for s in report["states"]:
        if s["state"] not in quiet:
            out.append(f"  {'state':<7} {s['state']:<28} line {s['line']}: {s['say']}")
    held = [s["line"] for s in report["states"] if s["state"] == "supported"]
    if held:
        out.append(f"  supported on line{'s' if len(held) > 1 else ''} {', '.join(map(str, held))}")
    return "\n".join(out)
