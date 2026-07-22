# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""FActScore-style digest fidelity: atomic-fact precision of a distilled note
against its source transcript (Min et al. 2023, adapted to notes).

The source document IS the gold reference, so no external annotation is
needed: decompose the note body into atomic facts (one LLM call), then judge
each fact as supported-by-source or not (batched LLM calls). Score =
supported / judged. Precision only — recall/coverage of the source stays with
the masked-signal golden lane, which already owns that direction.

Two modes:
  generic  --note note.md --source transcript.txt      one pair
  locomo   --data locomo.json --conv conv-26 --vault d  every distilled note in
           a run vault, note -> session source via frontmatter session_id
           (per-session --distill vaults) or provenance records (FSM vaults).

Verbatim vaults are the built-in control: scoring one should sit near 1.0;
a distilled vault's gap below that is the distiller's hallucination rate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from evals import _shared
from evals.locomo.runner import (
    _SOURCES_MARKER,
    _provenance_session_map,
    conversation_sessions,
    render_session,
)

_CHUNK = 25  # facts per judge call: one source copy amortized over the batch

_DECOMPOSE_PROMPT = (
    "Break the following text into atomic facts.\n"
    "Rules:\n"
    "- One fact per line, each line starting with \"- \".\n"
    "- Each fact is a single self-contained statement; resolve pronouns to names.\n"
    "- Cover every claim in the text; do not add facts that are not in the text.\n"
    "- Output only the fact lines, nothing else.\n\n"
    "Text:\n{text}"
)

_JUDGE_PROMPT = (
    "Source document:\n{source}\n\n"
    "Facts:\n{facts}\n\n"
    "For each fact, decide whether it is supported by the source document. "
    "A fact is supported when the source states it or directly implies it. "
    "Answer with one line per fact, exactly \"N: yes\" or \"N: no\". "
    "No other text."
)

_FACT_RE = re.compile(r"^-\s+(.+)$", re.M)
_VERDICT_RE = re.compile(r"^\s*(\d+)\s*[:.)]\s*(yes|no)\b", re.M | re.I)


def _llm(model: str, prompt: str, max_tokens: int) -> str:
    import time

    from silica.agent.llm import call_llm

    # An HTTP-200 completion with empty text is not an exception, so
    # call_llm's transient-retry never sees it; under concurrency this
    # provider drops enough empties to silently exclude ~half the notes.
    # Retry the empty as a transient — it clears on the next attempt.
    # temperature=0: same single-run A/B comparability rule as the QA judges.
    for attempt in range(3):
        resp = call_llm(model, [{"role": "user", "content": prompt}],
                        max_tokens=max_tokens, temperature=0.0)
        text = (resp.text or "").strip()
        if text:
            return text
        time.sleep(1.0 * (attempt + 1))
    return ""


def decompose(model: str, text: str) -> list[str] | None:
    """Note body -> atomic facts. None = decompose failure (empty/format-broken
    reply): the note is excluded from scoring and surfaced, never scored 0."""
    out = _llm(model, _DECOMPOSE_PROMPT.format(text=text), 2048)
    facts = [f.strip() for f in _FACT_RE.findall(out) if f.strip()]
    return facts or None


def _parse_verdicts(out: str, n: int) -> list[bool | None]:
    """Judge reply -> per-fact verdicts; a fact the judge skipped or garbled is
    None (judge failure, excluded from the denominator — LME judge convention)."""
    verdicts: list[bool | None] = [None] * n
    for num, ans in _VERDICT_RE.findall(out):
        i = int(num) - 1
        if 0 <= i < n:
            verdicts[i] = ans.lower() == "yes"
    return verdicts


def judge_facts(model: str, facts: list[str], source: str) -> list[bool | None]:
    verdicts: list[bool | None] = []
    for i in range(0, len(facts), _CHUNK):
        chunk = facts[i:i + _CHUNK]
        numbered = "\n".join(f"{j + 1}. {f}" for j, f in enumerate(chunk))
        out = _llm(model, _JUDGE_PROMPT.format(source=source, facts=numbered), 1024)
        verdicts += _parse_verdicts(out, len(chunk))
    return verdicts


def score_note(model: str, body: str, source: str) -> dict:
    facts = decompose(model, body)
    if facts is None:
        return {"facts": 0, "judged": 0, "supported": 0, "score": None,
                "unsupported": [], "judge_failures": 0, "error": "decompose_failed"}
    verdicts = judge_facts(model, facts, source)
    judged = [v for v in verdicts if v is not None]
    supported = sum(judged)
    return {"facts": len(facts), "judged": len(judged), "supported": supported,
            "score": supported / len(judged) if judged else None,
            "unsupported": [f for f, v in zip(facts, verdicts) if v is False],
            "judge_failures": verdicts.count(None), "error": None}


def aggregate(rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("score") is not None]
    fj = sum(r["judged"] for r in scored)
    fs = sum(r["supported"] for r in scored)
    return {"notes": len(rows), "notes_scored": len(scored),
            "notes_error": sum(1 for r in rows if r.get("error")),
            "facts_total": sum(r.get("facts", 0) for r in rows),
            "facts_judged": fj, "facts_supported": fs,
            "judge_failures": sum(r.get("judge_failures", 0) for r in rows),
            "micro_factscore": fs / fj if fj else None,
            "macro_factscore": (sum(r["score"] for r in scored) / len(scored)
                                if scored else None)}


def _note_body(text: str) -> str:
    """Strip frontmatter and the harness-added '## Sources' overlay block —
    neither is distiller output, so neither may contribute facts."""
    from silica.kernel import frontmatter

    _data, _raw, body = frontmatter.split(text)
    return body.split(_SOURCES_MARKER)[0].strip()


_FULL_CONV = "<full-conversation>"


def locomo_note_pairs(vault: Path, inst: dict) -> tuple[list[dict], list[str]]:
    """Every scoreable note in the vault -> {rel, sessions, body, source}.

    Reference document:
      flat vault (note has a `session_id` frontmatter key) -> that one session,
        strict 1:1, the same render_session() text the distiller saw.
      entity/merged note (no session_id — FSM vaults) -> the FULL conversation.
        These aggregate across sessions, so the reference is the whole
        transcript, not one session (Min et al. judge a bio against the entire
        Wikipedia article, not a paragraph). Per-session provenance was over-
        narrow: it recorded only the sessions with a complete run, so a note
        drawing on an unrecorded session scored its verbatim facts as
        unsupported against the wrong source. Judging against the whole
        conversation removes that artifact and stops dropping every entity
        note whose provenance record is missing.

    Returns (pairs, unmapped) — only bodyless notes end up unmapped now."""
    from silica.kernel import frontmatter

    sessions = {f"session_{n}": render_session(turns)
                for n, _dt, turns in conversation_sessions(inst["conversation"])}
    full_source = "\n\n".join(sessions[s] for s in sorted(
        sessions, key=lambda s: int(s.split("_")[1])))
    pairs, unmapped = [], []
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        # Not distiller output, so none may contribute facts: sources/ leaves
        # (hybrid overlay), done/ archived inbox transcripts, inbox/ leftovers
        # from partial sessions — all verbatim source copies that would score
        # 1.0 against themselves and inflate the aggregate. log.md is the run
        # log, not a note.
        if parts[0] in ("sources", "done", "inbox", "log.md") or any(
                p.startswith(".") for p in parts):
            continue
        rel = f.relative_to(vault).as_posix().removesuffix(".md")
        data, _raw, _body = frontmatter.split(f.read_text(encoding="utf-8"))
        sid = (data or {}).get("session_id")
        body = _note_body(f.read_text(encoding="utf-8"))
        if not body:
            unmapped.append(rel)
            continue
        if sid in sessions:
            sids, source = [sid], sessions[sid]
        else:
            sids, source = [_FULL_CONV], full_source
        pairs.append({"rel": rel, "sessions": sids, "body": body,
                      "source": source})
    return pairs, unmapped


def _score_pairs(model: str, pairs: list[dict], verbose: bool) -> list[dict]:
    """Notes are independent (isolated remote calls) — thread-pool like the QA
    runners; FACTSCORE_WORKERS=1 forces serial."""
    def one(p: dict) -> dict:
        row = {"rel": p["rel"], "sessions": p["sessions"]} | score_note(
            model, p["body"], p["source"])
        if verbose:
            print(f"  {row['rel']:<32} score={row['score']} "
                  f"({row['supported']}/{row['judged']})"
                  f"{' ERROR' if row['error'] else ''}", flush=True)
        return row

    workers = int(os.getenv("FACTSCORE_WORKERS", "8"))
    if workers <= 1:
        rows = [one(p) for p in pairs]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(one, pairs))
    return sorted(rows, key=lambda r: r["rel"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=None, help="decompose+judge model "
                    "(litellm string, default CONFIG.model)")
    ap.add_argument("--note", help="generic mode: note file to score")
    ap.add_argument("--source", help="generic mode: source document")
    ap.add_argument("--data", help="locomo mode: dataset json")
    ap.add_argument("--conv", help="locomo mode: sample_id (default: sole conversation)")
    ap.add_argument("--vault", help="locomo mode: run vault to score")
    ap.add_argument("--out", help="write full metrics doc to this json file")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    from silica.config import CONFIG

    model = args.model or CONFIG.model
    _shared.warn_unpinned_provider(model, os.getenv("SILICA_OPENROUTER_PROVIDER"))

    if args.note:
        if not args.source:
            ap.error("--note requires --source")
        body = _note_body(Path(args.note).read_text(encoding="utf-8"))
        source = Path(args.source).read_text(encoding="utf-8")
        rows = [{"rel": args.note} | score_note(model, body, source)]
        doc = {"config": {"mode": "generic", "model": model,
                          "note": args.note, "source": args.source},
               "provenance": _shared.provenance(args.source),
               "notes": rows, "metrics": aggregate(rows)}
    elif args.data and args.vault:
        data = json.loads(Path(args.data).read_text(encoding="utf-8"))
        by_id = {inst.get("sample_id"): inst for inst in data}
        if args.conv is None and len(data) == 1:
            inst = data[0]
        elif args.conv in by_id:
            inst = by_id[args.conv]
        else:
            ap.error(f"--conv must be one of {sorted(k for k in by_id if k)}")
        pairs, unmapped = locomo_note_pairs(Path(args.vault), inst)
        if not pairs:
            raise SystemExit(f"no scoreable notes in {args.vault} "
                             f"({len(unmapped)} unmapped)")
        rows = _score_pairs(model, pairs, args.verbose)
        doc = {"config": {"mode": "locomo", "model": model, "vault": args.vault,
                          "conv": inst.get("sample_id"), "unmapped": unmapped},
               "provenance": _shared.provenance(args.data),
               "notes": rows, "metrics": aggregate(rows)}
    else:
        ap.error("either --note/--source or --data/--vault")
        return 2

    m = doc["metrics"]
    print(f"\nfactscore — model={model} notes={m['notes_scored']}/{m['notes']} "
          f"micro={m['micro_factscore']} macro={m['macro_factscore']} "
          f"facts={m['facts_supported']}/{m['facts_judged']} supported")
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
