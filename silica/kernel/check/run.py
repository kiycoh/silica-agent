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

from collections.abc import Callable
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
DENSE_FLOORS: dict[str, float] = {"embeddinggemma-2": 0.645}
PASSAGES = 5
SEARCH_WIDTH = 200

Search = Callable[[str, int], list[dict]]
Dense = Callable[[str, set[str]], "float | None"]


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


def check_note(path: str, vault: str, *, write_dir: str = "", search: Search | None = None,
               dense: Dense | None = None, dense_floor: float | None = None) -> dict[str, Any]:
    """The step 1 report for the note at vault-relative `path`."""
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
    waiting = 0
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
        id_hits: list[str] = []
        for spelling, key in rules.identifiers(text):
            rows = ix.idents.get(key, [])
            if not rows:
                mine.append({"kind": "not in the vault", "words": spelling,
                             "say": f"no note in the vault holds {spelling}"})
                continue
            inpool = [r for r in rows if r[0] in pool.paths]
            id_hits += [rules.at(r[0], r[1]) + (f" ({r[2]})" if r[2] != spelling else "") for r in inpool[:1]]
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

        sourced = bool(cited or id_hits)
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
        if not (sourced or quote_found or lexical):
            best = dense(b.clean, pool.paths) if dense is not None and dense_floor is not None else None
            if best is None:
                states.append({"state": "not checked", "line": b.line,
                               "say": "absence needs the dense floor, and this vault has no vectors for it"})
            elif best < dense_floor:
                mine.append({"kind": "not found", "words": "",
                             "say": f"no note of the {len(pool.paths)} searched holds this"})
            else:
                waiting += 1
        else:
            waiting += 1
        for x in mine:
            findings.append({"grade": GRADES[x["kind"]], "line": b.line, "at": x.get("at", ""), **x})
        prev_sourced, prev_cited = sourced, cited

    findings.sort(key=lambda f: (ORDER.index(f["grade"]), f["line"]))
    return {
        "note": path, "blocks": len(blocks), "waiting": waiting,
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
    for s in report["states"]:
        out.append(f"  {'state':<7} {s['state']:<28} line {s['line']}: {s['say']}")
    return "\n".join(out)
