# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One check of one note, step 1: rules only (ADR-0110 points 4 to 6).

The two search legs come in as arguments. `search(query, k)` is the chat's
passage search (silica_search_context's engine), whose hits carry `coverage`;
`dense(text, pool)` is the best cosine of a block against the pool's notes, or
None when the vault has no vectors. Passed in, they keep this module free of
the agent and its network, and a test hands in a list.
"""
from __future__ import annotations

import hashlib
import json
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

Search = Callable[[str, int], list[dict]]
# The pool's nearest notes to a block, best first, as (path, cosine); [] or
# None when the vault has no vectors.
Dense = Callable[[str, set[str]], "list[tuple[str, float]] | None"]
Judge = Callable[[str, list[dict]], "dict | None"]
CACHE_DIR = ".silica/checks"
NOTE_CHARS = 900


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


def _excerpt(note: pl.Note) -> str:
    return note.text[sum(len(x) + 1 for x in note.text.split("\n")[:blk.body_start(note.text)]):][:NOTE_CHARS]


def _model_passages(passages: list[dict], cited: list[str], near: list[tuple[str, float]],
                    notes: dict[str, pl.Note]) -> list[dict]:
    """What the judge reads: the search's passages, then the notes the block
    links or names, then the pool's nearest notes by meaning, which is how an
    Italian block reaches its English source when no word matches."""
    out = [{"path": h["path"], "line": h.get("line", 1), "text": h.get("window") or h.get("snippet") or ""}
           for h in passages]
    seen = {o["path"] for o in out}
    for path in cited + [p for p, _s in near[:2]]:
        if path not in seen and path in notes:
            seen.add(path)
            out.append({"path": path, "line": blk.body_start(notes[path].text) + 1, "text": _excerpt(notes[path])})
    return out


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
        return [], [{"state": kind, "line": line, "say": f"{who}{p}"}]
    kind = "not found" if kind == "not in these passages" else kind
    return [{"kind": kind, "words": "", "at": v.get("at", ""), "say": f"{v['kind']}, by {who}{p}"}], []


def check_note(path: str, vault: str, *, write_dir: str = "", search: Search | None = None,
               dense: Dense | None = None, dense_floor: float | None = None,
               judge: Judge | None = None) -> dict[str, Any]:
    """The report for the note at vault-relative `path`: rule findings, and
    with a `judge` the verdict on each block the rules did not settle."""
    notes, files = pl.walk(vault)
    if path not in notes:
        raise FileNotFoundError(path)
    me = notes[path]
    blocks, defs = blk.split(me.text)
    res = pl.Resolver(notes, files)
    body = me.text[sum(len(x) + 1 for x in me.text.split("\n")[:blk.body_start(me.text)]):]
    named_all = {p for _t, p in res.links(body) if p} | res.mentions(body)
    named_all.discard(path)
    pool = pl.build(path, notes, pl.party_keys(vault), write_dir, named_all)
    ix = rules.index(notes, skip=path)
    findings: list[dict] = []
    states: list[dict] = []
    waiting = cached = 0
    cache_file = _cache_path(vault, path)
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if judge and cache_file.is_file() else {}
    except (OSError, ValueError):
        cache = {}  # a torn cache costs one re-judgment, never a wrong verdict
    fresh: dict[str, dict] = {}
    seen_source_dates: set[tuple[str, str]] = set()
    prev_sourced = False
    prev_cited: list[str] = []

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

        # Dates: the block's own, then those its cited sources give.
        scope = [{**p, "origin": f"{path}:{b.line}"} for p in dates.find_periods(text)]
        for s in cited:
            scope += _periods(s, notes[s].text)
        for d, _s, _e in dates.find_dates(text):
            holders = [r for r in ix.dates.get(d, []) if r[0] in pool.paths]
            found_in += [r[0] for r in holders[:1]]
            local = scope + [p for h in {r[0] for r in holders} - set(cited) for p in _periods(h, notes[h].text)]
            f, st = rules.date_rules(d, local, ix, pool.paths)
            mine += f
            states += [{**x, "line": b.line} for x in st]
            if not f and not st and d not in ix.dates:
                mine.append({"kind": "date not found", "words": dates.human(d),
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
        passages: list[dict] = []
        if search is not None:
            passages = [h for h in search(b.clean, SEARCH_WIDTH) if h.get("path") in pool.paths][:PASSAGES]
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
                    mine.append({"kind": "quotation without its source", "words": short,
                                 "say": f"{r['path']} holds it word for word; the block names no source",
                                 "at": rules.at(r["path"], r["line"])})
            elif r["state"] == "near":
                quote_found = True
                mine.append({"kind": "misquoted", "words": short, "at": rules.at(r["path"], r["line"]),
                             "say": f"no note holds it word for word; {r['path']} holds {round(r['share'] * 100)}% "
                                    f"of its words in order: “{r['passage']}”"})
            else:
                mine.append({"kind": "not found", "words": short,
                             "say": f"no note of the {len(pool.paths)} searched holds this quotation, even nearly"})

        # Absence by rule (point 6): no link, no named note, no identifier,
        # no quotation found, and no passage over either floor.
        near = (dense(b.clean, pool.paths) or []) if dense is not None else []
        to_judge = True
        if not (sourced or quote_found or lexical):
            best = near[0][1] if near and dense_floor is not None else None
            if best is None:
                to_judge = judge is not None
                if not to_judge:
                    states.append({"state": "not checked", "line": b.line,
                                   "say": "absence needs the dense floor, and this vault has no vectors for it"})
            elif best < dense_floor:
                to_judge = False
                mine.append({"kind": "not found", "words": "",
                             "say": f"no note of the {len(pool.paths)} searched holds this"})
        if to_judge:
            waiting += 1
            if judge is not None:
                mp = _model_passages(passages, list(dict.fromkeys(cited + found_in)), near, notes)
                key = hashlib.sha1("\0".join([b.clean] + sorted(
                    f"{x['path']}\0{x['text']}" for x in mp)).encode()).hexdigest()
                v = cache.get(key)
                if v is not None:
                    cached += 1
                elif mp:
                    v = judge(b.clean, mp)
                else:
                    states.append({"state": "not checked", "line": b.line,
                                   "say": "no passage in the pool to judge it against"})
                if v is not None:
                    fresh[key] = v
                    f, st = _verdict_rows(v, b.line)
                    mine += f
                    states += st
                    # Point 5: a searched passage's status counts once a verdict
                    # rests on it; a cited source already had its line above.
                    rest = (v.get("at") or "").rsplit(":", 1)[0]
                    if rest in notes and rest not in cited and (say := _status(rest, notes[rest])):
                        mine.append({"kind": "source status", "words": notes[rest].stem, "say": say, "at": rest})
        for x in mine:
            findings.append({"grade": GRADES[x["kind"]], "line": b.line, "at": x.get("at", ""), **x})
        prev_sourced, prev_cited = sourced, cited

    if fresh:
        # Only this run's verdicts are kept: a block that changed takes its
        # old key with it, so the cache never outgrows the note.
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    findings.sort(key=lambda f: (ORDER.index(f["grade"]), f["line"]))
    return {
        "note": path, "blocks": len(blocks), "waiting": waiting, "cached": cached,
        "pool": {"size": len(pool.paths), "of": pool.of, "scope": pool.scope, "values": sorted(pool.values)},
        "findings": findings, "states": states,
        "counts": {g: sum(f["grade"] == g for f in findings) for g in ORDER},
    }


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
