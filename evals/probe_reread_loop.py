# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Does a chapter-sized rework turn land, or loop re-reading its source?

Hosted t06, 2026-10-22: asked to rework a 4.7k-word chapter "summarising where
possible", the chat wrote a 3k-word note, the pre-call fit then stubbed the
chapter to make room for the note's body in the call's arguments, the stub read
as a chapter never seen, and the model re-read it in slices 14 times until the
turn's 400k prompt budget. The landing answered in its reasoning, so the reply
was "(silica: maximum iterations reached)".

One case is one fresh vault holding one chapter, one request shaped like that
one (paraphrased: it makes silica_rework refuse as "shorter", so the chat writes
the note itself), the web chat's constraints and the hosted model, endpoint pin
and window. t06's chapter carried U+0251 for every "a" (1.6x the tokens), which
is what put it over 32k; `--alpha` writes the text that way to reproduce it. The silica under test is whichever one the interpreter imports, so
a baseline runs from a worktree at the baseline commit:

  uv run --extra dev --extra gui python -I <this file> run --arm B32 --ctx 32000 \\
      --n 8 --jobs 4 --text chapter.md --out results.jsonl

Scored from the turn's own messages: whether a note was written, how many reads
of the chapter came after the write, whether the answer is a real one that names
the note, prompt tokens summed over the turn's calls, and wall time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REQUEST = ("Rielabora in modo chiaro il capitolo che ti ho caricato nel vault, sintetizzando dove "
           "ritieni possibile, mantenendo tutta la terminologia usata per esprimere i concetti e "
           "tutti i concetti importanti.")
SOURCE = "Inbox/capitolo.md"
# The host's model, endpoint pin and decision model (silica-hosted deploy/host.env, 2026-10-22).
HOSTED_ENV = {
    "SILICA_HOSTED": "1",
    "SILICA_PROVIDER": "openrouter",
    "SILICA_MODEL": "openrouter/deepseek/deepseek-v4-flash-0731",
    # No fallback, unlike the host: DeepInfra's and Parasail's shared pools 429
    # in bursts (2026-10-22, 22:00 UTC), and a case half written by GLM would
    # measure another model. A case that runs out of retries is an error row.
    "SILICA_FALLBACK_MODELS": "",
    "SILICA_DECISION_MODEL": "typesafe/jev-1.13-20260917",
    "OPENROUTER_PROVIDER_BY_MODEL": json.dumps({
        "deepseek/deepseek-v4-flash-0731": {"order": ["deepinfra/fp8", "parasail/fp8"],
                                            "allow_fallbacks": False, "zdr": True},
        "z-ai/glm-5.3-flash": {"zdr": True}}),
}
READS = ("silica_read", "silica_read_note")
WRITES = ("silica_write_note", "silica_rework")


def _user_key() -> str:
    """The OpenRouter key from the operator's own ~/.silica/.env: each case
    runs under a fresh HOME, where silica would find none."""
    for line in (Path.home() / ".silica" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("no OPENROUTER_API_KEY in ~/.silica/.env")


def _names_source(args: dict) -> bool:
    ref = str(args.get("path") or args.get("name") or args.get("note") or "")
    return Path(SOURCE).stem in ref


def score(messages: list[dict], answer: str, vault: Path) -> dict:
    calls, results = [], {}
    for m in messages:
        for tc in m.get("tool_calls") or ():
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((tc.get("id"), fn.get("name"), args))
        if m.get("role") == "tool":
            results[m.get("tool_call_id")] = str(m.get("content") or "")
    written, write_at = None, None
    for i, (cid, name, args) in enumerate(calls):
        res = results.get(cid, "")
        if name in WRITES and "error" not in res[:200].lower():
            hit = re.search(r"path=([^;\"]+)|\"path\": \"([^\"]+)\"|\"note\": \"([^\"]+)\"", res)
            written = next((g for g in (hit.groups() if hit else ()) if g), args.get("path"))
            write_at = i
            break
    reads = [i for i, (_, name, args) in enumerate(calls) if name in READS and _names_source(args)]
    after = [i for i in reads if write_at is not None and i > write_at]
    note = (vault / written) if written else None
    words = len(note.read_text(encoding="utf-8").split()) if note and note.is_file() else 0
    stem = Path(written).stem if written else ""
    return {
        "written": written, "written_words": words,
        "source_reads": len(reads), "source_reads_after_write": len(after),
        "answer_is_sentinel": answer.strip().startswith("(silica:"),
        "answer_names_note": bool(stem) and stem.lower() in answer.lower(),
        "answer_chars": len(answer), "tools": [name for _, name, _ in calls],
        # What compaction left of the turn's reads: the stub is the trigger.
        "stubs": sum(1 for m in messages if m.get("role") == "tool"
                     and str(m.get("content") or "").startswith("⟪silica: result elided")),
    }


def one(opts: argparse.Namespace) -> None:
    """A single case, in this process: HOME and SILICA_VAULT are the parent's
    fresh directories."""
    from silica.agent import loop
    from silica.agent import narration as narr
    from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools
    from silica.cli import seed_messages
    from silica.config import CONFIG

    if opts.fit_narrows:
        loop.FIT_NARROWS = True
    meter = {"calls": 0, "prompt": 0, "completion": 0, "models": []}
    real = loop.call_llm

    def counted(*a, **kw):
        resp = real(*a, **kw)
        meter["calls"] += 1
        meter["prompt"] += int((resp.usage or {}).get("prompt_tokens") or 0)
        meter["completion"] += int((resp.usage or {}).get("completion_tokens") or 0)
        model = (resp.request or {}).get("model")
        if model and model not in meter["models"]:
            meter["models"].append(model)
        return resp

    loop.call_llm = counted
    messages = [m for m in seed_messages() if m.get("content")]
    messages.append({"role": "user", "content": REQUEST})
    # An open session is what makes OpenRouter return the reasoning, as the
    # GUI's does on the host (llm.py, include_reasoning).
    narr.NARRATOR.ensure_session(driver="probe", seed={"probe": "reread-loop"})
    narr.NARRATOR.turn(messages[-1])
    t0 = time.time()
    error = None
    try:
        answer = loop.run_agent(messages, CONFIG.model, cancel_token=threading.Event(),
                                constraints=AgentConstraints(tools=chat_tools(messages),
                                                             deferred=chat_deferred(messages),
                                                             interactive=True))
    except Exception as e:  # a provider failure is a result of the case, not a crash of the run
        answer, error = "", f"{type(e).__name__}: {e}"[:300]
    finally:
        narr.NARRATOR.close()
    out = {"seconds": round(time.time() - t0, 1), "error": error, **meter,
           **score(messages, answer or "", Path(os.environ["SILICA_VAULT"])),
           "answer_head": (answer or "")[:300]}
    print(json.dumps(out, ensure_ascii=False))


def _case(opts: argparse.Namespace, i: int, key: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"reread-{opts.arm}-{i}-") as tmp:
        vault = Path(tmp) / "vault"
        (vault / Path(SOURCE).parent).mkdir(parents=True)
        text = Path(opts.text).read_text(encoding="utf-8")
        (vault / SOURCE).write_text(text.replace("a", "\u0251") if opts.alpha else text, encoding="utf-8")
        (Path(tmp) / "home").mkdir()
        env = {**os.environ, **HOSTED_ENV, "HOME": str(Path(tmp) / "home"),
               "SILICA_VAULT": str(vault), "SILICA_MAX_CONTEXT": str(opts.ctx),
               "OPENROUTER_API_KEY": key}
        cmd = [sys.executable, "-I", __file__, "one"] + (["--fit-narrows"] if opts.fit_narrows else [])
        try:
            p = subprocess.run(cmd, env=env, cwd=tmp, capture_output=True, text=True, timeout=opts.timeout)
            lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
            row = json.loads(lines[-1]) if lines else {"error": (p.stderr or "no output")[-500:]}
        except subprocess.TimeoutExpired:
            row = {"error": f"timeout after {opts.timeout}s"}
    return {"arm": opts.arm, "ctx": opts.ctx, "fit_narrows": opts.fit_narrows, "alpha": opts.alpha,
            "case": i, **row}


def run(opts: argparse.Namespace) -> None:
    key = _user_key()
    out = Path(opts.out)
    lock = threading.Lock()

    def go(i: int) -> None:
        row = _case(opts, i, key)
        with lock, out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{opts.arm} #{i}: {row.get('seconds')}s written={row.get('written')} "
              f"reads_after={row.get('source_reads_after_write')} stubs={row.get('stubs')} "
              f"sentinel={row.get('answer_is_sentinel')} "
              f"prompt={row.get('prompt')} error={row.get('error')}", flush=True)

    with ThreadPoolExecutor(opts.jobs) as pool:
        list(pool.map(go, range(opts.n)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--arm", required=True)
    r.add_argument("--ctx", type=int, default=32000)
    r.add_argument("--n", type=int, default=8)
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--text", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--fit-narrows", action="store_true")
    r.add_argument("--alpha", action="store_true")
    o = sub.add_parser("one")
    o.add_argument("--fit-narrows", action="store_true")
    opts = ap.parse_args()
    run(opts) if opts.cmd == "run" else one(opts)


if __name__ == "__main__":
    main()
