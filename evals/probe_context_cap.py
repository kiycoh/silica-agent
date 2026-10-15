# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Context cap for the hosted chat: what a student's long conversation costs,
and how well it still answers, at each `max_context_tokens`.

Why this exists: on OpenRouter `cli._resolve_context_budget` sizes the window
to the provider's `context_length`, 1M for Qwen 3.7 Flash, so neither the
post-call compaction (60% of the window) nor the pre-call fit (90%) ever fires
on the pilot, and every call resends the whole chat. The endpoint the pilot
pins (alibaba/us-east-1) bills a call of 32k prompt tokens or more at 3.3x,
and 256k or more at 6.7x. A cap moves both thresholds; what it costs in
answers is what a cheaper chat could lose: a read collapsed to a stub is
either read again (one more call, resending the context) or answered from the
stub.

Each conversation is seven student turns on the ML course of the test vault:
four first reads of lectures, two returns to a lecture read turns earlier (the
case compaction is made to hurt), one synthesis over all four. A turn scores
the share of facts from the note it names (regexes over the answer). The
config mirrors the host's /etc/silica/host.env: the model, its fallback and
per-model routing, no reranker, no decision model. Only the arm's cap differs.

Arms: `ctx:<N>` sets CONFIG.max_context_tokens to N in the child; ctx:1000000
is the host today. `ctx:<N>:jev` also sets SILICA_DECISION_MODEL (JEV_MODEL,
default typesafe/jev-1.13), which the host leaves unset: without it
silica_read_note serves every note whole whatever its `query`
(agent/read_scope.py) and relevance compaction is off (ADR-0052, ADR-0053).
Jev's spend is not metered here (decide() is not call_llm): read it as the
key's usage delta over a run of that arm alone. `:fitnarrow` turns on
loop.FIT_NARROWS: a note read this turn that will not fit is served as its
passages densest in the query's (or the request's) words instead of a stub.
`:runon` sets atomic.READ_QUERY_RUN_ON to 800: a narrowed window that holds
a heading runs on toward the next one. Flags combine in any order:
`ctx:32000:jev:fitnarrow:runon`.

`<arm>@<model>` runs that arm on another chat model (litellm id, e.g.
`ctx:32000@openrouter/mistralai/mistral-nemo`) with SILICA_FALLBACK_MODELS
emptied, so a call the model fails shows as a failure and not as GLM's answer.
Give the host's model the same suffix when comparing, so both arms lose the
fallback alike. `:t<T>` sends temperature T: on OpenRouter the product sends
none, so a model runs at its provider's default (Mistral advises 0.3 for Nemo).

Usage:
  uv run python -m evals.probe_context_cap --arm ctx:1000000 --arm ctx:32000 --runs 3 \\
      --scratch-root ~/.cache/silica-ctxcap/a --out ~/.cache/silica-ctxcap/out
  uv run python -m evals.probe_context_cap --table --out ~/.cache/silica-ctxcap/out
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

MODEL = "openrouter/qwen/qwen3.7-flash-20260727"

# /etc/silica/host.env as of 2026-10-13 (silica-hosted deploy/host.env.example),
# minus what has no bearing on a chat turn. Exported, so the copied user .env in
# the run's HOME cannot override any of it (override=False).
HOSTED_ENV = {
    "SILICA_HOSTED": "1",
    "SILICA_PROVIDER": "openrouter",
    "SILICA_MODEL": MODEL,
    "SILICA_FALLBACK_MODELS": "openrouter/z-ai/glm-5.3-flash",
    "OPENROUTER_PROVIDER_BY_MODEL": json.dumps({
        "qwen/qwen3.7-flash-20260727": {"order": ["alibaba/us-east-1"], "allow_fallbacks": False},
        "z-ai/glm-5.3-flash": {"zdr": True},
        # The two cheapest endpoints that take `tools` (read 2026-10-14);
        # DekaLLM and Parasail are cheaper per token but drop tool calls.
        "mistralai/mistral-nemo": {"order": ["io-net/fp16", "deepinfra/fp8"], "allow_fallbacks": False},
        # The cheapest endpoint ($0.0093/M in, $0.60 out, read 2026-10-14).
        # First-party deepseek is refused by the account's no-training guardrail.
        "deepseek/deepseek-v4.1-flash": {"order": ["relace"], "allow_fallbacks": False}}),
    "SILICA_RERANK_BASE_URL": "",
    "SILICA_RERANK_MODEL": "",
    "SILICA_DECISION_MODEL": "",
    "SILICA_WORKER_MODEL": "",
    "SILICA_CAPTURE_SESSIONS": "false",
    "SILICA_VAULT_BRIEF": "false",
}

# USD per million tokens on alibaba/us-east-1, read 2026-10-13 from
# https://openrouter.ai/api/v1/models/qwen/qwen3.7-flash-20260727/endpoints:
# (min prompt tokens, prompt, completion, cache read). Used only when a
# response carries no `usage.cost`.
TIERS = ((256_000, 0.23, 0.92, 0.046), (32_000, 0.115, 0.46, 0.023), (0, 0.0345, 0.1495, 0.0069))
CLIFF = 32_000

READ_TOOLS = {"silica_read_note", "silica_read"}
# `:narrow` arms: the share of the window, in chars at 4 a token, over which a
# note read with a query is narrowed. A third leaves room for the 7.5k prefix
# and the chat when the cap is 32k.
NARROW_SHARE = 0.33
_NOTE_ARGS = ("name", "path", "note", "ref")

CASES = {
    "reti": [
        {"kind": "first", "text": "Sto ripassando Machine Learning. Cosa presenta la Lezione 18 sugli "
                                  "algoritmi di apprendimento delle reti neurali? Riassumimi i metodi principali.",
         "facts": [r"momentum",
                   r"(learning rate|tasso di apprendimento|passo)[^.\n]{0,40}adattiv|adattiv[^.\n]{0,40}(learning rate|tasso|passo)",
                   r"early stopping|arresto anticipato",
                   r"validazion"]},
        {"kind": "first", "text": "Ora passiamo alla Lezione 23: come mostra che una rete feed forward "
                                  "riesce a risolvere lo XOR?",
         "facts": [r"\bxor\b", r"nascost|hidden", r"separabil|non linear|non-linear"]},
        {"kind": "first", "text": "Lezione 24: che differenza c'è tra convoluzione e cross-correlation "
                                  "secondo le note?",
         "facts": [r"commutativ", r"cross[- ]?correla",
                   r"ribalt|flip|capovol|invert|rovesc|specchi|i-m|i\+m"]},
        {"kind": "first", "text": "E la Lezione 30: quali sono le tre tipologie principali di reti ricorrenti?",
         "facts": [r"(ogni|ciascun)[^.\n]{0,30}(step|passo|istante)",
                   r"(tra|fra) (le )?unità nascoste|hidden[- ]to[- ]hidden",
                   r"(output|uscita)[^.\n]{0,60}(unità nascoste|nascost|hidden)",
                   r"intera sequenza|un (solo|singolo|unico) output|una (sola|singola|unica) uscita",
                   r"paralleliz"]},
        {"kind": "return", "text": "Tornando alla Lezione 18: scrivimi la regola di aggiornamento del "
                                   "momentum esattamente come nelle note, con il significato di ogni simbolo.",
         "facts": [r"\\alpha|α|alpha", r"\\eta|η|\beta\b",
                   r"v(\\leftarrow|←|:?=)(\\alpha|α|alpha)v-(\\eta|η|eta)g",
                   r"w(\\leftarrow|←|:?=)w\+v", r"gradient"]},
        {"kind": "return", "text": "E nell'esempio dello XOR della Lezione 23, quali sono i quattro punti "
                                   "e come sono distinti (colore e forma)?",
         "facts": [r"\(0,1\)", r"\(1,0\)",
                   r"(ross|red)[^.\n]{0,40}(quadrat|square)|(quadrat|square)[^.\n]{0,40}(ross|red)",
                   r"(blu|blue)[^.\n]{0,40}(cerch|circle)|(cerch|circle)[^.\n]{0,40}(blu|blue)"]},
        {"kind": "synth", "text": "Fammi uno schema di ripasso che colleghi le Lezioni 18, 23, 24 e 30, "
                                  "con un punto chiave per ciascuna preso dalle note.",
         "facts": [r"\b18\b", r"\b23\b", r"\b24\b", r"\b30\b", r"momentum|early stopping", r"xor",
                   r"commutativ|cross[- ]?correla", r"paralleliz|intera sequenza|tre tipolog"]},
    ],
    "classici": [
        {"kind": "first", "text": "Sto ripassando Machine Learning. Nella Lezione 7, che differenza c'è tra "
                                  "lo stimatore della varianza campionaria con 1/m e quello con 1/(m-1)?",
         "facts": [r"bias|distort", r"m-1", r"non ha bias|senza bias|non distort|corrett|unbiased",
                   r"sottostim|ha (un )?bias|è distort|distorto"]},
        {"kind": "first", "text": "Ora la Lezione 12: come si ricava la kernel ridge regression e qual è la "
                                  "soluzione per i coefficienti alpha?",
         "facts": [r"\\lambda|λ|lambda", r"kernel",
                   r"kk\+(\\lambda|λ|lambda)k|k\+(\\lambda|λ|lambda)i", r"\^\(?-1\)?|inversa"]},
        {"kind": "first", "text": "Lezione 13: cos'è il margine funzionale e quando si chiama margine geometrico?",
         "facts": [r"margine funzionale", r"w\^?\(?t\)?x[^+]{0,14}\+b", r"versor|unitari|norma (1|uno|unitaria)",
                   r"minim|\\min|min_"]},
        {"kind": "first", "text": "Lezione 32: come funzionano gli alberi di decisione e quali criteri di "
                                  "impurità usano?",
         "facts": [r"gini", r"entropi", r"1-\\?sum|1-σ|1-∑",
                   r"(mse|varianza)[^.\n]{0,60}regression|regression[^.\n]{0,60}(mse|varianza)"]},
        {"kind": "return", "text": "Tornando alla Lezione 7: come scrivono le note la scomposizione "
                                   "dell'errore quadratico medio di uno stimatore? E cosa succede se lo "
                                   "stimatore è senza bias?",
         "facts": [r"mse", r"var", r"bias[^\n]{0,24}(\^2|²)",
                   r"(senza bias|non ha bias|unbiased|non distort)[^\n]{0,160}varianza"]},
        {"kind": "return", "text": "E nella Lezione 12, riscrivimi la formula esatta della soluzione per alpha "
                                   "nella kernel ridge regression.",
         "facts": [r"kk\+(\\lambda|λ|lambda)k|k\+(\\lambda|λ|lambda)i", r"\^\(?-1\)?|inversa"]},
        {"kind": "synth", "text": "Fammi uno schema di ripasso che colleghi le Lezioni 7, 12, 13 e 32, con un "
                                  "punto chiave per ciascuna preso dalle note.",
         "facts": [r"\b7\b", r"\b12\b", r"\b13\b", r"\b32\b", r"bias|stimator", r"kernel",
                   r"margine|svm|pca", r"gini|alber"]},
    ],
}

# The lookahead keeps `\leftarrow` whole: only the bare `\left(` is noise.
_LATEX_NOISE = re.compile(r"\\(boldsymbol|mathbf|mathrm|left|right|text|operatorname|bm)(?![a-z])|[{}\s$]")


def facts_hit(answer: str, facts: list[str]) -> list[bool]:
    """Each fact against the answer as written and with LaTeX wrappers and
    whitespace stripped, so `v \\leftarrow \\alpha v - \\eta g` and
    `\\boldsymbol{K}\\boldsymbol{K} + \\lambda \\boldsymbol{K}` read as one string."""
    low = answer.lower().replace("−", "-").replace("’", "'")
    flat = _LATEX_NOISE.sub("", low)
    return [bool(re.search(f, low) or re.search(f, flat)) for f in facts]


def tier_usd(prompt: int, cached: int, completion: int) -> float:
    for floor, p, c, k in TIERS:
        if prompt >= floor:
            return ((prompt - cached) * p + cached * k + completion * c) / 1e6
    return 0.0


def _note_key(args: dict) -> str:
    for k in _NOTE_ARGS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return Path(v.strip()).stem.lower()
    return ""


def run_one(arm: str, case: str, model: str, max_usd: float, out_json: Path) -> None:
    """Runs inside the subprocess: SILICA_VAULT names the scratch copy, HOME a
    fresh one, and HOSTED_ENV is exported."""
    from evals.probe_prompt_ab import _calls_from_narration, _stub_egress
    from evals.probe_study_plan_replay import Ctx, score_v2
    from silica.agent import loop as _loop
    from silica.agent import narration as _narr
    from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools
    from silica.agent.llm import _cached_tokens
    from silica.agent.loop import run_agent
    from silica.cli import seed_messages
    from silica.config import CONFIG

    base = arm.split("@")[0]
    cap = int(base.split(":")[1])
    temp = next((float(p[1:]) for p in base.split(":")[2:] if p.startswith("t")), None)
    CONFIG.max_context_tokens = cap
    flags = set(base.split(":")[2:])
    if "runon" in flags:
        from silica.tools import atomic
        atomic.READ_QUERY_RUN_ON = 800
    if "fitnarrow" in flags:
        from silica.agent import loop
        loop.FIT_NARROWS = True
    if "narrow" in flags:
        # A note over NARROW_SHARE of the window, read with a `query`, is served
        # as its query windows: whole, the pre-call fit would stub it before
        # the model saw it, and the model reads it again (seen at ctx:32000 on
        # 2026-10-13: Lezione 18 read 10 times in one turn).
        from silica.agent import read_scope
        from silica.tools import atomic
        atomic.READ_QUERY_WHOLE_UNDER = int(NARROW_SHARE * cap * 4)
        read_scope.wants_part = lambda name, query: bool(query.strip())
    _stub_egress()
    messages = [m for m in seed_messages(math=True) if m.get("content")]
    sid = _narr.NARRATOR.ensure_session(driver="probe", seed={"arm": arm, "case": case})
    cancel = threading.Event()
    calls: list[dict] = []
    turn_no = [0]
    spent = [0.0]
    real_call = _loop.call_llm

    def metered(model_, messages_, **kw):
        resp = real_call(model_, messages_, **kw)
        u = resp.usage or {}
        prompt, completion = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        cached = _cached_tokens(u)
        billed = u.get("cost")
        usd = float(billed) if billed is not None else tier_usd(prompt, cached, completion)
        calls.append({"turn": turn_no[0], "model": resp.request.get("model"), "prompt": prompt,
                      "cached": cached, "completion": completion, "usd": usd,
                      "billed": billed is not None})
        spent[0] += usd
        if spent[0] > max_usd:
            cancel.set()
        return resp
    _loop.call_llm = metered
    ctx = Ctx()
    turns: list[dict] = []
    try:
        for i, turn in enumerate(CASES[case]):
            turn_no[0] = i
            messages.append({"role": "user", "content": turn["text"]})
            _narr.NARRATOR.turn(messages[-1])
            t0 = time.time()
            answer = run_agent(messages, model=model, cancel_token=cancel, temperature=temp,
                               constraints=AgentConstraints(tools=chat_tools(messages), interactive=True,
                                                            deferred=chat_deferred(messages)))
            aborted = cancel.is_set()
            hits = facts_hit("" if aborted else answer, turn["facts"])
            v2 = score_v2({"answer": "" if aborted else answer, "aborted": aborted, "tool_calls": []}, ctx)
            turns.append({"kind": turn["kind"], "user": turn["text"], "answer": "" if aborted else answer,
                          "aborted": aborted, "seconds": round(time.time() - t0, 1),
                          "facts": hits, "score": sum(hits) / len(hits),
                          "dead_links": len((v2.get("links") or {}).get("dead") or [])})
            if aborted:
                break
    finally:
        _loop.call_llm = real_call
        _narr.NARRATOR.close()
    narr_file = _narr.narration_dir() / f"{sid}.jsonl"
    tool_turns = _calls_from_narration(narr_file) if narr_file.exists() else []
    compactions = _compactions(narr_file) if narr_file.exists() else []
    seen: set[str] = set()
    for i, rec in enumerate(turns):
        tcalls = tool_turns[i] if i < len(tool_turns) else []
        reads = [_note_key(c["args"]) for c in tcalls if c["name"] in READ_TOOLS]
        rec["tool_plan"] = [c["name"] for c in tcalls]
        rec["rereads"] = sum(1 for k in reads if k and k in seen)
        seen.update(k for k in reads if k)
        rec["collapsed"] = compactions[i] if i < len(compactions) else 0
        rec["calls"] = [c for c in calls if c["turn"] == i]
    out = {"arm": arm, "cap": cap, "case": case, "model": model, "sid": sid, "turns": turns,
           "aborted": any(t["aborted"] for t in turns), "usd": round(spent[0], 6)}
    out_json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def _compactions(narr_file: Path) -> list[int]:
    """Messages collapsed per user turn, by the post-call budget or the pre-call fit."""
    per: list[int] = []
    for line in narr_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["kind"] == "turn" and r["payload"]["message"].get("role") == "user":
            per.append(0)
        elif r["kind"] == "compaction" and per and (r.get("payload") or {}).get("by") != "relevance":
            per[-1] += len((r.get("payload") or {}).get("indices") or [])
    return per


# ------------------------------------------------------------- report ---

def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else float("nan")


def _sd(xs: list[float]) -> float:
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def table(out_dir: Path) -> str:
    recs = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(out_dir.glob("*.json"))]
    rows = ["| arm | case | n | aborted | USD/chat (sd) | calls | prompt tok | max call | calls >=32k "
            "| cached | first | return | synth | rereads | collapsed | dead | s/turn |",
            "|" + "---|" * 17]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in recs:
        groups.setdefault((r["arm"], r["case"]), []).append(r)
        groups.setdefault((r["arm"], "*"), []).append(r)
    for (cap, case), rs in sorted(groups.items(),
                                  key=lambda kv: (-int(kv[0][0].split("@")[0].split(":")[1]), kv[0][0], kv[0][1])):
        allc = [c for r in rs for t in r["turns"] for c in t["calls"]]
        def kind(k):
            return _mean([t["score"] for r in rs for t in r["turns"] if t["kind"] == k and not t["aborted"]])
        usd = [r["usd"] for r in rs]
        prompt = sum(c["prompt"] for c in allc)
        rows.append(
            f"| {cap} | {case} | {len(rs)} | {sum(r['aborted'] for r in rs)} "
            f"| {_mean(usd):.4f} ({_sd(usd):.4f}) | {len(allc) / len(rs):.1f} "
            f"| {prompt / len(rs):,.0f} | {max((c['prompt'] for c in allc), default=0):,} "
            f"| {sum(c['prompt'] >= CLIFF for c in allc) / max(len(allc), 1):.0%} "
            f"| {sum(c['cached'] for c in allc) / max(prompt, 1):.0%} "
            f"| {kind('first'):.2f} | {kind('return'):.2f} | {kind('synth'):.2f} "
            f"| {_mean([sum(t['rereads'] for t in r['turns']) for r in rs]):.1f} "
            f"| {_mean([sum(t['collapsed'] for t in r['turns']) for r in rs]):.1f} "
            f"| {_mean([sum(t['dead_links'] for t in r['turns']) for r in rs]):.1f} "
            f"| {_mean([t['seconds'] for r in rs for t in r['turns']]):.1f} |")
    models = {}
    for r in recs:
        for t in r["turns"]:
            for c in t["calls"]:
                models[c["model"]] = models.get(c["model"], 0) + 1
    billed = sum(c["billed"] for r in recs for t in r["turns"] for c in t["calls"])
    total = sum(len(t["calls"]) for r in recs for t in r["turns"])
    return "\n".join(rows) + f"\n\nmodels served: {models} · usage.cost present on {billed}/{total} calls"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", help="ctx:<N>[:narrow|:jev|:fitnarrow|:runon...][@<model>]")
    ap.add_argument("--case", action="append", choices=list(CASES))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--max-usd", type=float, default=0.5, help="per conversation; cancels past it")
    ap.add_argument("--scratch-root", default=None, help="on disk: /tmp is RAM on this machine")
    ap.add_argument("--out", default=str(Path.home() / ".cache" / "silica-ctxcap" / "out"))
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--one", nargs=4, metavar=("ARM", "CASE", "MODEL", "OUT_JSON"), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.one:
        arm, case, model, out_json = args.one
        run_one(arm, case, model, args.max_usd, Path(out_json))
        return 0
    out_dir = Path(args.out).expanduser()
    if args.table:
        print(table(out_dir))
        return 0

    from evals._shared import run_id
    from evals.probe_prompt_ab import fresh_home, reset_vault
    root = Path(args.scratch_root or (Path.home() / ".cache" / "silica-ctxcap" / "scratch")).expanduser()
    scratch = root / "vault"
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id()
    for i in range(args.runs):
        for case in args.case or list(CASES):
            for arm in args.arm or ["ctx:1000000", "ctx:32000"]:
                home = fresh_home(root / "home")
                reset_vault(scratch, None, home)
                env = {**os.environ, **HOSTED_ENV, "SILICA_VAULT": str(scratch), "HOME": str(home),
                       "HF_HOME": str(Path.home() / ".cache" / "huggingface")}
                base, _, model = arm.partition("@")
                if model:
                    env.update(SILICA_MODEL=model, SILICA_FALLBACK_MODELS="")
                if "jev" in base.split(":")[2:]:
                    env["SILICA_DECISION_MODEL"] = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
                # `.` too: with_suffix would read "qwen3.7-flash-…-r1" as an extension and
                # write every run of that arm to one file.
                stem = out_dir / f"{case}-{re.sub(r'[:/@.]', '_', arm)}-{rid}-r{i}"
                cmd = [sys.executable, "-m", "evals.probe_context_cap", "--one", arm, case, model or MODEL,
                       str(stem.with_suffix(".json")), "--max-usd", str(args.max_usd)]
                proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
                if not stem.with_suffix(".json").exists():
                    stem.with_suffix(".err").write_text(proc.stderr, encoding="utf-8")
                    print(f"{case} {arm} r{i}: FAILED rc={proc.returncode} ({stem.with_suffix('.err')})", flush=True)
                    continue
                if "; trying " in proc.stderr:
                    # A fallback holds the next model for 300 s, so one failed
                    # call reprices the rest of the chat; keep why it failed
                    # (one unexplained GLM chat on 2026-10-15).
                    stem.with_suffix(".fallback.txt").write_text(
                        "\n".join(l for l in proc.stderr.splitlines() if "failed" in l), encoding="utf-8")
                rec = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))
                t = rec["turns"]
                print(f"{case} {arm} r{i}: usd={rec['usd']:.4f} calls={sum(len(x['calls']) for x in t)} "
                      f"scores={[round(x['score'], 2) for x in t]} rereads={sum(x['rereads'] for x in t)} "
                      f"collapsed={sum(x['collapsed'] for x in t)} aborted={rec['aborted']}", flush=True)
    print()
    print(table(out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
