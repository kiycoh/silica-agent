# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""W1 audit — of the claims the extractive span gate rejects, how many deserved
it? (survey-provenance spec §10)

Throwaway instrument, NOT product code. Zero-LLM. Calls the real
`nonextractive_lines` — the exact predicate `_extractive_reject` rejects ops
with — so the audit cannot drift from the gate it audits.

The risk being audited is over-strictness: "an over-strict gate silently drops
correct claims". So the unit is a FLAGGED LINE, and the question per line is
whether the span really is absent from its source (correct rejection) or is a
verbatim selection the normalizer failed to match (false rejection, a dropped
correct claim).

Corpus: a real extractive-profile run (`bench/ab_extractive/conv-26`), whose
notes are per-source blocks under provenance headers with the sources still on
disk. Each block is attributed to the source its header names (the leading
block to `sources[0]`, the write order stamp_sources guarantees), then passed
to the gate.

Conservative by construction: the live gate judges a body against that
concept's EXCERPT, while this judges it against the WHOLE source file. A
larger source can only make matching easier, so every line flagged here would
also be flagged in production — the sample is a subset of real rejections,
never an inflation of them.

Triage is mechanical (case-only, punctuation-only, fuzzy-partial, absent); the
verdict is by hand, which is what the gate asks for.

  uv run python -m evals.probe_w1_extractive_audit --vault bench/ab_extractive/conv-26
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path


def _blocks(body: str, sources: list[str]) -> list[tuple[str, str]]:
    """(source_basename, block_text) for each provenance block in *body*.

    The leading segment (before any header) attributes to sources[0]; each
    header segment to the source its own header names.
    """
    from silica.kernel.write.templates import PROVENANCE_HEADER_PREFIXES

    pat = re.compile(
        r"^(?:%s)[^\n]*?\(d[ai] ([^)]+)\)\s*$"
        % "|".join(re.escape(p) for p in PROVENANCE_HEADER_PREFIXES),
        re.MULTILINE,
    )
    out: list[tuple[str, str]] = []
    marks = list(pat.finditer(body))
    lead = body[: marks[0].start()] if marks else body
    # The rendered H1 is the note's TITLE, which the gate never judges: it
    # reads `op.snippet` (the body) while the template renders `# {op.title}`
    # separately. Leaving it in would score the instrument's own artifact as a
    # rejection. Sub-headings (##+) stay — those DO live in the snippet.
    lead = re.sub(r"^#[ \t]+[^\n]*\n?", "", lead.lstrip("\n"), count=1)
    if lead.strip() and sources:
        out.append((sources[0], lead))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((m.group(1).strip(), body[m.end():end]))
    return out


def _triage(line: str, source: str) -> str:
    """Why the gate flagged this line — mechanical, for hand-audit triage."""
    from silica.kernel.write.provenance import _norm_extract

    src = _norm_extract(source)
    if line.lower() in src.lower():
        return "case-only"
    stripped = re.sub(r"[^\w\s]", "", line).lower()
    if stripped and stripped in re.sub(r"[^\w\s]", "", src).lower():
        return "punctuation-only"
    # Longest verbatim run of this line inside the source.
    sm = difflib.SequenceMatcher(None, line, src, autojunk=False)
    _, _, n = sm.find_longest_match(0, len(line), 0, len(src))
    frac = n / len(line) if line else 0.0
    if frac >= 0.8:
        return "near-verbatim"
    if frac >= 0.4:
        return "partial"
    return "absent"


def run(vault: Path, *, verbose: bool = False) -> dict:
    from silica.kernel.write import frontmatter
    from silica.kernel.write.provenance import nonextractive_lines

    mem = vault / "memory"
    notes = sorted(p for p in mem.rglob("*.md")) if mem.is_dir() else []

    # Attribution fallback for runs written before `sources:` was stamped: the
    # CLEANUP provenance record already names, per source, the notes it wrote.
    prov: dict[str, list[str]] = {}
    pj = vault / "provenance.json"
    if pj.is_file():
        try:
            for rec in json.loads(pj.read_text(encoding="utf-8")):
                for n in rec.get("notes", []):
                    prov.setdefault(
                        n.split("/", 1)[-1] + ".md", []
                    ).append(rec["source"])
        except Exception:
            prov = {}
    src_text: dict[str, str] = {}
    for d in ("done", "inbox"):
        for p in (vault / d).glob("*.md"):
            src_text.setdefault(p.name, p.read_text(encoding="utf-8"))

    flagged: list[dict] = []
    notes_scanned = blocks_scanned = blocks_flagged = 0
    missing_source = 0

    for np_ in notes:
        raw = np_.read_text(encoding="utf-8")
        data, _, body = frontmatter.split(raw)
        srcs = (data or {}).get("sources") if isinstance(data, dict) else None
        if isinstance(srcs, str):
            srcs = [srcs]
        srcs = [str(s) for s in (srcs or []) if s]
        if not srcs:
            srcs = prov.get(np_.relative_to(mem).as_posix(), [])
        if not srcs:
            continue
        notes_scanned += 1
        for basename, block in _blocks(body, srcs):
            text = src_text.get(basename)
            if text is None:
                missing_source += 1
                continue
            if len(srcs) > 1 and basename == srcs[0]:
                # Multi-source note, leading block: which source wrote the lede
                # is not recorded, so judge it against all of them (a larger
                # source only makes matching easier — still a subset of real
                # rejections, never an inflation).
                text = "\n".join(src_text.get(s2, "") for s2 in srcs)
            blocks_scanned += 1
            bad = nonextractive_lines(block, text)
            if not bad:
                continue
            blocks_flagged += 1
            for line in bad:
                flagged.append({
                    "note": np_.relative_to(mem).as_posix(),
                    "source": basename,
                    "line": line,
                    "triage": _triage(line, text),
                })

    by_triage: dict[str, int] = {}
    for f in flagged:
        by_triage[f["triage"]] = by_triage.get(f["triage"], 0) + 1

    out = {
        "notes_scanned": notes_scanned,
        "blocks_scanned": blocks_scanned,
        "blocks_flagged": blocks_flagged,
        "block_flag_rate": (
            round(blocks_flagged / blocks_scanned, 4) if blocks_scanned else 0.0
        ),
        "lines_flagged": len(flagged),
        "blocks_missing_source": missing_source,
        "by_triage": dict(sorted(by_triage.items(), key=lambda kv: -kv[1])),
        "flagged": flagged,
    }
    if verbose:
        print(f"\nnotes={notes_scanned} blocks={blocks_scanned} "
              f"flagged_blocks={blocks_flagged} ({out['block_flag_rate']:.1%}) "
              f"flagged_lines={len(flagged)}")
        print(f"triage: {out['by_triage']}")
        for f in flagged:
            print(f"\n  [{f['triage']}] {f['note']}  <- {f['source']}")
            print(f"    {f['line'][:300]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--out")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    rep = run(vault, verbose=not args.quiet)
    if args.out:
        Path(args.out).write_text(
            json.dumps({"probe": "w1_extractive_audit",
                        "corpus": str(vault), "report": rep}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
