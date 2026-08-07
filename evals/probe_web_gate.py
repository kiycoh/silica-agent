# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""L2 gate (spec-web-research-memory-bank §6): one-shot writer vs bank+outline.

The clean experiment replays the SAME frozen retrieval through both writers,
so the difference is attributable to the writer alone:

  record  — run each concept through the live /web-search loop once, with
            composition disabled: the note written is arm A, and the raw
            materials (trace, bank, one-shot body) are frozen to a JSON file.
            Needs live network + LLM, and a scratch --vault: the notes it
            writes are experiment output, not knowledge.
  replay  — per frozen run: arm A = the one-shot body as recorded; arm B =
            _compose_findings over the frozen bank (live writer calls, frozen
            retrieval). Both bind against the frozen trace. Metrics per arm:
            factscore (the gate), effective citations; per run: quote
            validity (guardian sanity — 1.0 or the guardian is broken) and
            arm B's extra token cost.

  uv run python -m evals.probe_web_gate record --concepts c.txt --out runs/ --vault /tmp/gate-vault
  uv run python -m evals.probe_web_gate replay --runs runs/ --out gate.json

L3 gate (spec-web-research-plan-steering §6): record both acquisition arms
live (A = steering off, B = steering on; --tag A2 names the A/A noise run),
then `steer` reports acquisition primaries per recording and composes each
frozen bank through the same live writer for the factscore floor:

  uv run python -m evals.probe_web_gate record --concepts c.txt --out runs3/ --vault /tmp/l3-vault --arm A
  uv run python -m evals.probe_web_gate record --concepts c.txt --out runs3/ --vault /tmp/l3-vault --arm B
  uv run python -m evals.probe_web_gate steer --runs runs3/ --out gate3.json

Kill rule (spec §6): if B does not beat A on factscore with effective
citations flat or up, drop §3.3-3.4 and keep the bank+guardian alone.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from evals.factscore import decompose, judge_facts
from silica.agent.events import ToolCompleteEvent
from silica.config import CONFIG
from silica.kernel.write.templates import slugify
from silica.sources import web_research as wr

# Distinct sources actually cited inline: the FACT-style metric. Bound bodies
# carry [n] / [n, m] markers, so distinct numbers = distinct cited sources.
_NREF_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def effective_citations(body: str) -> int:
    return len({
        int(n)
        for m in _NREF_RE.finditer(body)
        for n in re.split(r"\s*,\s*", m.group(1))
    })


def acquisition(rec: dict) -> dict:
    """L3 primaries, straight off the recording. Composition cannot touch
    them (spec-web-research-plan-steering §6): urls_with_quotes and
    yield_per_fetch decide, cited counts only confirm."""
    bank = rec["bank"]
    fetches = sum(1 for t in rec["trace"].values() if t.startswith("Source: "))
    return {
        "arm": rec.get("arm", "A"),
        "urls_with_quotes": len({v[0] for v in bank.values()}),
        "quotes": len(bank),
        "fetches": fetches,
        "yield_per_fetch": len(bank) / fetches if fetches else None,
        "steps": len(rec["trace"]),
    }


def bank_validity(bank: dict[str, wr._Quote], trace_values: list[str]) -> float | None:
    """Fraction of banked quotes verbatim in their page. None on an empty bank.

    Below 1.0 the remember guardian is broken — that is a test failure to go
    fix, not a gate number to report and move past.
    """
    pages: dict[str, str] = {}
    for text in trace_values:
        head, _, _ = text.partition("\n")
        if head.startswith("Source: "):
            pages[head[len("Source: "):].strip()] = text
    if not bank:
        return None
    ok = sum(
        1
        for q in bank.values()
        if q.url in pages and wr._squash(q.quote) in wr._squash(pages[q.url])
    )
    return ok / len(bank)


def factscore_any_page(model: str, body: str, pages: list[str]) -> dict:
    """FActScore where the source is a set of pages, not one document.

    A 48-step trace does not fit one judge context, so each fact is judged
    per page and counts as supported when ANY page supports it; pages stop
    being consulted for a fact once one supports it. A fact judged definitely
    on no page is excluded from the denominator (factscore.py posture).
    """
    facts = decompose(model, body) or []
    if not facts:
        return {"facts": 0, "judged": 0, "supported": 0, "score": None}
    supported = [False] * len(facts)
    judged = [False] * len(facts)
    for page in pages:
        undecided = [i for i in range(len(facts)) if not supported[i]]
        if not undecided:
            break
        verdicts = judge_facts(model, [facts[i] for i in undecided], page)
        for i, verdict in zip(undecided, verdicts):
            if verdict is not None:
                judged[i] = True
            if verdict:
                supported[i] = True
    n_judged = sum(judged)
    n_supported = sum(supported)
    return {
        "facts": len(facts),
        "judged": n_judged,
        "supported": n_supported,
        "score": n_supported / n_judged if n_judged else None,
    }


def record_run(concept: str, out_dir: Path, max_searches: int,
               arm: str = "B", tag: str | None = None) -> Path:
    """One live research run, frozen. Composition is off either way; `arm`
    picks the acquisition: "A" runs the exact pre-steering loop (no plan
    tool, no prompt step), "B" runs with steering on."""
    trace: dict[str, str] = {}

    def freeze(event) -> None:
        if isinstance(event, ToolCompleteEvent) and isinstance(event.result, str):
            trace[event.call_id] = event.result

    captured: dict[str, str] = {}
    real_run_agent = wr.run_agent

    def observing_run_agent(messages, model, **kw):
        body = real_run_agent(messages, model=model, **kw)
        captured["oneshot"] = body
        return body

    # The loop's LLM calls are counted so the recording carries the live cost
    # of its own arm, dead plan revisions included (spec §0.3 and §6.4).
    # loop.py binds call_llm as a module attribute, so patching it intercepts
    # every call run_agent makes.
    from silica.agent import loop as _loop

    tokens = {"n": 0}
    real_loop_llm = _loop.call_llm

    def counting_llm(*a, **kw):
        resp = real_loop_llm(*a, **kw)
        usage = resp.usage or {}
        tokens["n"] += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        return resp

    # Manual patching, not pytest: composition off so the recorded note is the
    # one-shot arm, run_agent observed so the pre-bind body is kept even
    # though web_research never returns it.
    wr.run_agent = observing_run_agent
    real_compose = wr._compose_findings
    real_steering = wr._STEERING
    wr._compose_findings = lambda concept, bank: None
    # Arm A is the pre-steering loop, live. The seam switches tool and prompt
    # step together (spec §6: the only variable between arms is acquisition).
    wr._STEERING = arm == "B"
    _loop.call_llm = counting_llm
    try:
        note_rel = wr.web_research(
            concept, max_searches=max_searches, tool_progress_callback=freeze
        )
        bank = {qid: list(q) for qid, q in wr._BANK.items()}
        final_plan = wr._PLAN
    finally:
        wr.run_agent = real_run_agent
        wr._compose_findings = real_compose
        wr._STEERING = real_steering
        _loop.call_llm = real_loop_llm

    rec = {
        "concept": concept,
        "model": CONFIG.model,
        "max_searches": max_searches,
        "arm": arm,
        "plan": final_plan,
        "tokens": tokens["n"],
        "note_rel": note_rel,
        "oneshot_body": captured.get("oneshot", ""),
        "bank": bank,
        "trace": trace,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slugify(concept) or 'concept'}-{tag or arm}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _arm(body: str, trace_values: list[str], bank: dict[str, wr._Quote],
         judge_model: str) -> dict:
    bound, _, audit = wr._bind_citations(
        body, wr._collect_sources(trace_values), bank
    )
    pages = [t for t in trace_values if t.startswith("Source: ")]
    fs = factscore_any_page(judge_model, bound, pages)
    # ponytail: a transient judge failure yields score None with no retry;
    # the summary counts unjudged arms, rerunning replay is the retry.
    return {
        "body": bound,
        "effective_citations": effective_citations(bound),
        "phantom_audit": audit,
        "factscore": fs["score"],
        "factscore_detail": fs,
    }


def replay_run(rec: dict, judge_model: str) -> dict:
    """Both arms from one frozen run. Arm B's writer calls are live; its
    retrieval is the recording's."""
    bank = {qid: wr._Quote(*v) for qid, v in rec["bank"].items()}
    trace_values = list(rec["trace"].values())

    tokens = {"n": 0}
    real_call_llm = wr.call_llm

    def counting_call_llm(*a, **kw):
        resp = real_call_llm(*a, **kw)
        usage = resp.usage or {}
        tokens["n"] += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        return resp

    wr.call_llm = counting_call_llm
    try:
        composed = wr._compose_findings(rec["concept"], bank) if bank else None
    finally:
        wr.call_llm = real_call_llm

    arm_b = None
    if composed is not None:
        arm_b = _arm(composed, trace_values, bank, judge_model)
        arm_b["extra_tokens"] = tokens["n"]
    return {
        "concept": rec["concept"],
        "quote_validity": bank_validity(bank, trace_values),
        "arms": {
            "A": _arm(rec["oneshot_body"], trace_values, bank, judge_model),
            "B": arm_b,
        },
    }


def steer_run(rec: dict, judge_model: str) -> dict:
    """One L3 arm: acquisition primaries plus the composed-note floors.

    The composer is the same live code for every recording, so the writer is
    literally identical across arms and any difference is acquisition. The
    factscore floor guards against the plan steering toward pages that do
    not hold up; it does not decide (spec §6).

    `note` scores the body the agent actually wrote. Reporting only `floor`
    answered a question nobody asked: the floor is recomposed live from the
    bank at replay time, so it exists even when the run wrote no note at all
    (eBPF-A leaked its tool call as text and still floored 0.943) and it
    drifts between replays (the PQC A/A floors differ by 0.068 where their
    notes differ by 0.006)."""
    bank = {qid: wr._Quote(*v) for qid, v in rec["bank"].items()}
    trace_values = list(rec["trace"].values())
    composed = wr._compose_findings(rec["concept"], bank) if bank else None
    return {
        "concept": rec["concept"],
        "acquisition": acquisition(rec),
        "tokens": rec.get("tokens"),
        "quote_validity": bank_validity(bank, trace_values),
        "note": _arm(rec["oneshot_body"], trace_values, bank, judge_model),
        "floor": (
            _arm(composed, trace_values, bank, judge_model)
            if composed is not None
            else None
        ),
    }


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec_p = sub.add_parser("record")
    rec_p.add_argument("--concepts", required=True, help="one concept per line")
    rec_p.add_argument("--out", required=True)
    rec_p.add_argument("--vault", required=True,
                       help="scratch vault dir for the arm-A notes")
    rec_p.add_argument("--max-searches", type=int, default=48)
    rec_p.add_argument("--arm", choices=["A", "B"], default="B",
                       help="A = steering off (pre-L3 loop), B = steering on")
    rec_p.add_argument("--tag", default=None,
                       help="filename suffix, e.g. A2 for the A/A noise run")
    rep_p = sub.add_parser("replay")
    rep_p.add_argument("--runs", required=True)
    rep_p.add_argument("--judge-model", default=None)
    rep_p.add_argument("--out", default=None)
    steer_p = sub.add_parser(
        "steer", help="L3 gate report: acquisition per arm + floors"
    )
    steer_p.add_argument("--runs", required=True)
    steer_p.add_argument("--judge-model", default=None)
    steer_p.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "record":
        vault = Path(args.vault)
        (vault / "Inbox").mkdir(parents=True, exist_ok=True)
        CONFIG.vault_path = str(vault)  # before the DRIVER singleton binds
        concepts = [
            line.strip()
            for line in Path(args.concepts).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for concept in concepts:
            # No single concept may kill the batch — measured twice live: a
            # no-findings ValueError at cap 12, then a convergence-guard
            # RuntimeError at cap 48 that threw away three finished runs.
            try:
                path = record_run(concept, Path(args.out), args.max_searches,
                                  args.arm, args.tag)
            except Exception as err:
                print(f"FAILED {concept!r}: {err}")
                continue
            rec = json.loads(path.read_text(encoding="utf-8"))
            print(f"recorded {path.name}: {len(rec['trace'])} tool results, "
                  f"{len(rec['bank'])} quotes banked")
        return 0

    if args.cmd == "steer":
        judge = args.judge_model or CONFIG.model

        def score(path: Path) -> dict:
            rec = json.loads(path.read_text(encoding="utf-8"))
            row = steer_run(rec, judge)
            row["file"] = path.name
            acq, floor = row["acquisition"], row["floor"] or {}
            print(f"{path.name}: arm={acq['arm']} urls={acq['urls_with_quotes']} "
                  f"quotes={acq['quotes']} yield={acq['yield_per_fetch']} "
                  f"steps={acq['steps']} tokens={row['tokens']} "
                  f"validity={row['quote_validity']} "
                  f"note={row['note']['factscore']} "
                  f"floor={floor.get('factscore')}", flush=True)
            return row

        # Recordings are independent (isolated judge calls), and scoring two
        # bodies per run doubled a loop that already took ~80min for five.
        # STEER_WORKERS=1 forces serial.
        paths = sorted(Path(args.runs).glob("*.json"))
        workers = int(os.getenv("STEER_WORKERS", "4"))
        if workers <= 1:
            rows = [score(p) for p in paths]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as ex:
                rows = list(ex.map(score, paths))
        paired: dict[str, list[str]] = {}
        for row in rows:
            paired.setdefault(row["concept"], []).append(row["file"])
        summary = {"judge_model": judge, "runs": rows, "paired": paired}
        out = json.dumps(summary, ensure_ascii=False, indent=1)
        if args.out:
            Path(args.out).write_text(out, encoding="utf-8")
        else:
            print(out)
        return 0

    judge = args.judge_model or CONFIG.model
    rows = []
    for path in sorted(Path(args.runs).glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        row = replay_run(rec, judge)
        rows.append(row)
        b = row["arms"]["B"]
        print(f"{row['concept']}: validity={row['quote_validity']} "
              f"A={row['arms']['A']['factscore']} "
              f"B={b['factscore'] if b else 'FAILED'}")
    summary = {
        "judge_model": judge,
        "runs": rows,
        "mean_factscore_A": _mean(
            [r["arms"]["A"]["factscore"] for r in rows
             if r["arms"]["A"]["factscore"] is not None]
        ),
        "mean_factscore_B": _mean(
            [r["arms"]["B"]["factscore"] for r in rows
             if r["arms"]["B"] and r["arms"]["B"]["factscore"] is not None]
        ),
        "composition_failures": sum(1 for r in rows if r["arms"]["B"] is None),
        # Judge transients leave factscore None; a mean that silently skips
        # them reads as clean. Say how many runs each mean stands on.
        "unjudged_A": sum(
            1 for r in rows if r["arms"]["A"]["factscore"] is None
        ),
        "unjudged_B": sum(
            1 for r in rows
            if r["arms"]["B"] and r["arms"]["B"]["factscore"] is None
        ),
    }
    out = json.dumps(summary, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
