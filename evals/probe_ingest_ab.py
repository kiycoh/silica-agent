# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Ingest A/B: the outline lane as shipped against a verbatim split on headings,
by what ingest costs and how well the chat answers afterwards.

Why this exists: the product's own substrate pin (silica-docs evaluation/
v1-substrate/V1-substrate-pin.md) concluded "substrate default = verbatim, no
LLM at ingest", yet /nucleate's default lane rewrites a lecture with 4-8 worker
calls that resend the whole source. The literature agrees with the pin
(Zhou et al. 2026, arXiv 2602.16974: structure-based chunking beats LLM-guided
in-corpus; Laitenberger et al., EMNLP 2025: retrieve-then-read in source order
matches multi-stage pipelines). This measures it on the product.

Arms, each ingesting the same lectures into a fresh copy of the test vault:
  outline   silica_run_injector as the hosted chat calls it: the default lane,
            keep_sources on (a verbatim source leaf beside the concept notes)
  outline-cache     the same, SILICA_OUTLINE_CACHE=1: the source rides as the
            shared system prefix of every call on a file (lever 4, 2026-10-15)
  outline-verbatim  the same, SILICA_OUTLINE_BODIES=verbatim: stage A and B
            only, each idea's body looked up as its slice of the source (lever 2)
  verbatim  no LLM: each lecture split on its headings, consecutive slides of
            one title merged, sections packed to SECTION_CHARS, each written
            as a note titled with the lecture and its heading; the original is
            removed, then the index is swept

`--model hosted` is the pilot's config (HOSTED_ENV, Qwen 3.7 Flash);
`--model local` the personal stack's Qwen3.5-9B on llama-server :1237 as both
chat and worker. Each run also samples the process's threads (lever 1: t03 ran
out of them at TasksMax=256 on 2026-10-14) into <arm>/threads.jsonl.

Phase 1 (`--ingest`) writes <root>/<arm>/vault, its index under the real HOME,
and <root>/<arm>/ingest.json (time, calls, tokens, metered USD). Phase 2 is
evals/probe_context_cap.py with PRISTINE_VAULT=<root>/<arm>/vault, so the chat
is the same seven-turn conversations at the hosted 32k cap.

Usage:
  uv run python -m evals.probe_ingest_ab --ingest --arm verbatim --arm outline
  uv run python -m evals.probe_ingest_ab --ingest --model local --arm outline-verbatim \
      --root ~/.cache/silica-levers/local
  PRISTINE_VAULT=~/.cache/silica-ingestab/outline/vault uv run python -m \\
      evals.probe_context_cap --arm ctx:32000 --runs 3 --out ~/.cache/silica-ingestab/outline/chat
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LECTURES = (7, 12, 13, 18, 23, 24, 30, 32)   # the ones the chat cases ask about
ML = "Inbox/machine_learning_md"
TARGET = "Corsi/Machine Learning"
# `--corpus book`: a converted book's segments, planted into each arm's vault
# copy (BOOK_DIR holds them as convert() wrote them, one .md per segment).
BOOK_DIR = Path(os.environ.get("PROBE_BOOK_DIR", "~/.cache/silica-levers/book/convert/vault/Inbox/udl/UDL")).expanduser()
BOOK_INBOX = "Inbox/udl/UDL"
BOOK_TARGET = "Libri/UDL"


def corpus(name: str) -> tuple[list[str], str]:
    """(vault-relative inbox files, target folder) of a corpus."""
    if name == "book":
        return [f"{BOOK_INBOX}/{p.name}" for p in sorted(BOOK_DIR.glob("*.md"))], BOOK_TARGET
    return [f"{ML}/Lezione {n}.md" for n in LECTURES], TARGET
# ~1000 tokens: the upper end of what Bhat et al. 2025 (arXiv 2505.21700) find
# best for context-heavy questions, and well inside the hosted 32k window.
SECTION_CHARS = 4000
# outline-cache also sets the explicit Qwen breakpoint: without it the
# provider reused the shared source only at fixed 7936-token checkpoints, 32%
# of the prompt on the first run (2026-10-15).
# Looked-up bodies are the default since 2026-10-15, so the arms that measure
# the bodies stage ask for it.
ARM_ENV = {"outline": {"SILICA_OUTLINE_BODIES": "llm"},
           "outline-cache": {"SILICA_OUTLINE_BODIES": "llm", "SILICA_OUTLINE_CACHE": "1", "SILICA_QWEN_CACHE": "1"},
           "outline-verbatim": {"SILICA_OUTLINE_BODIES": "verbatim"}, "verbatim": {}}
LOCAL_ENV = {
    "SILICA_PROVIDER": "custom", "SILICA_MODEL": "custom/qwen3.5-9b",
    "SILICA_PROVIDER_BASE_URL": "http://127.0.0.1:1237/v1",
    "SILICA_WORKER_MODEL": "", "SILICA_WORKER_PROVIDER": "", "SILICA_DECISION_MODEL": "",
    "SILICA_RERANK_BASE_URL": "", "SILICA_RERANK_MODEL": "",
    "SILICA_CAPTURE_SESSIONS": "false", "SILICA_VAULT_BRIEF": "false",
    # Off the pilot, call_llm meters only on request; the probe counts calls
    # through that meter's emit.
    "SILICA_TOKEN_METER": "1",
}
_HEADING = re.compile(r"^#{1,3} +(.+?)\s*$", re.M)
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def split_sections(text: str) -> list[tuple[str, str]]:
    """(title, body) per section: a cut at every heading, a run of slides
    under one repeated title kept together, then packed up to SECTION_CHARS."""
    marks = [(m.start(), m.group(1).strip()) for m in _HEADING.finditer(text)]
    if not marks or marks[0][0] > 0:
        marks.insert(0, (0, ""))
    raw = [(t, text[s:e]) for (s, t), (e, _) in zip(marks, marks[1:] + [(len(text), "")])]
    merged: list[tuple[str, str]] = []
    for title, body in raw:
        if merged and (title == merged[-1][0] or not title):
            merged[-1] = (merged[-1][0], merged[-1][1] + body)
        else:
            merged.append((title, body))
    # A packed section is named by every heading it holds: by its first alone,
    # each lecture's opening section was "Machine Learning (9 CFU)", the
    # course banner, which says nothing a search can use.
    packed: list[tuple[list[str], str]] = []
    for title, body in merged:
        if packed and len(packed[-1][1]) + len(body) <= SECTION_CHARS:
            packed[-1][0].append(title)
            packed[-1] = (packed[-1][0], packed[-1][1] + body)
        else:
            packed.append(([title], body))
    return [(" / ".join(t for t in titles if t) or "Introduzione", b.strip())
            for titles, b in packed if b.strip()]


def write_verbatim(vault: Path, files: list[str], target: str) -> int:
    n_notes = 0
    for rel in files:
        src = vault / rel
        stem = src.stem
        sections = split_sections(src.read_text(encoding="utf-8"))
        folder = vault / target / stem
        folder.mkdir(parents=True, exist_ok=True)
        for i, (title, body) in enumerate(sections, 1):
            name = f"{stem} · {i:02d} · {_UNSAFE.sub(' ', title).strip()[:70]}"
            head = f'---\nsource_file: "{rel}"\npart: "{i}/{len(sections)}"\n---\n'
            (folder / f"{name}.md").write_text(f"{head}# {stem} › {title}\n\n{body}\n", encoding="utf-8")
            n_notes += 1
        src.unlink()
    return n_notes


def ingest_one(arm: str, out_json: Path) -> None:
    """Runs in the subprocess: SILICA_VAULT is the arm's vault, HOME a fresh one."""
    import logging

    files, target = corpus(os.environ.get("PROBE_CORPUS", "lectures"))

    import silica.agent.llm as llm

    # The run's own log beside its numbers: a rejection or a steer otherwise
    # leaves one stderr line and no reason (lever 2's first run, 2026-10-15).
    logging.basicConfig(filename=out_json.with_name("silica.log"), level=logging.INFO,
                        format="%(asctime)s %(threadName)s %(name)s %(levelname)s %(message)s")
    calls: list[dict] = []
    real_emit = llm.emit

    def capture(event, **f):
        if event == "llm_call":
            calls.append({**f, "t": round(time.time() - t0, 1)})
        return real_emit(event, **f)
    llm.emit = capture
    vault = Path(os.environ["SILICA_VAULT"])
    stop = _sample_threads(out_json.with_name("threads.jsonl"))
    t0 = time.time()
    if arm.startswith("outline"):
        from silica.tools.runners import silica_run_injector
        result = silica_run_injector(inbox_files=files, target_dir=target)
        summary = {k: result.get(k) for k in ("success", "final_status", "notes_created", "error",
                                              "failed_chunks", "files_summary") if k in result}
    else:
        summary = {"notes_created": write_verbatim(vault, files, target)}
        from silica.kernel.recall.sync import sweep
        sweep(force=True)
    seconds = time.time() - t0
    peak = stop()
    from silica.kernel.recall.paths import index_dir_for
    notes = sorted(str(p.relative_to(vault)) for p in vault.rglob("*.md")
                   if target in str(p.relative_to(vault)) or "sources" in p.parts)
    out = {"arm": arm, "seconds": round(seconds, 1), "summary": summary,
           "calls": len(calls), "prompt": sum(c.get("prompt") or 0 for c in calls),
           "cached": sum(c.get("cached") or 0 for c in calls),
           "completion": sum(c.get("completion") or 0 for c in calls),
           "usd_metered": round(sum(c.get("usd") or 0 for c in calls), 6),
           "usd_missing": sum(c.get("usd") is None for c in calls),
           "by_site": _by_site(calls), "notes": notes, "threads": peak,
           "per_call": [{k: c.get(k) for k in ("t", "site", "prompt", "cached", "completion", "usd")} for c in calls],
           "index_dir": str(index_dir_for(str(vault)))}
    out_json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def _sample_threads(path: Path, every: float = 0.5):
    """Sample the Python threads by name and the OS thread count every
    `every` s; the returned stop() gives the peak and its name census."""
    import collections
    import threading

    done = threading.Event()
    peak = {"os": 0, "py": 0, "names": {}}

    def os_threads() -> int:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
        return 0

    def run():
        with path.open("w", encoding="utf-8") as fh:
            while not done.wait(every):
                names = collections.Counter(re.sub(r"[-_ ]?\d+(_\d+)?$", "", t.name) for t in threading.enumerate())
                row = {"t": round(time.time(), 1), "os": os_threads(), "py": sum(names.values()), "names": dict(names)}
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                if row["os"] > peak["os"]:
                    peak.update(row)
    t = threading.Thread(target=run, name="probe-sampler", daemon=True)
    t.start()

    def stop() -> dict:
        done.set()
        t.join(5)
        return {k: peak[k] for k in ("os", "py", "names")}
    return stop


def _by_site(calls: list[dict]) -> dict:
    out: dict[str, list[int]] = {}
    for c in calls:
        s = out.setdefault(str(c.get("site")), [0, 0, 0])
        s[0] += 1
        s[1] += c.get("prompt") or 0
        s[2] += c.get("completion") or 0
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", choices=tuple(ARM_ENV))
    ap.add_argument("--model", choices=("hosted", "local"), default="hosted")
    ap.add_argument("--corpus", choices=("lectures", "book"), default="lectures")
    ap.add_argument("--root", default=str(Path.home() / ".cache" / "silica-ingestab"))
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--one", nargs=2, metavar=("ARM", "OUT_JSON"), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.one:
        ingest_one(args.one[0], Path(args.one[1]))
        return 0
    from evals.probe_context_cap import HOSTED_ENV
    from evals.probe_prompt_ab import fresh_home, reset_vault
    root = Path(args.root).expanduser()
    for arm in args.arm or ["verbatim", "outline"]:
        base = root / arm
        home = fresh_home(base / "home")
        plant = None
        if args.corpus == "book":
            def plant(v: Path) -> None:
                shutil.copytree(BOOK_DIR, v / BOOK_INBOX)
        reset_vault(base / "vault", plant, home)
        env = {**os.environ, **(HOSTED_ENV if args.model == "hosted" else LOCAL_ENV), **ARM_ENV[arm],
               # Resolved here: the child's HOME is the fresh one, so its `~` is not ours.
               "PROBE_CORPUS": args.corpus, "PROBE_BOOK_DIR": str(BOOK_DIR),
               "SILICA_VAULT": str(base / "vault"), "HOME": str(home),
               "HF_HOME": str(Path.home() / ".cache" / "huggingface")}
        out_json = base / "ingest.json"
        proc = subprocess.run([sys.executable, "-m", "evals.probe_ingest_ab", "--one", arm, str(out_json)],
                              env=env, capture_output=True, text=True)
        (base / "ingest.stderr").write_text(proc.stderr, encoding="utf-8")
        if not out_json.exists():
            print(f"{arm}: FAILED rc={proc.returncode} ({base / 'ingest.stderr'})", flush=True)
            continue
        rec = json.loads(out_json.read_text(encoding="utf-8"))
        # Phase 2 copies the index from under the real HOME (probe_prompt_ab.reset_vault).
        idx = Path(rec["index_dir"])
        real = Path.home() / ".silica" / idx.relative_to(home / ".silica")
        shutil.rmtree(real, ignore_errors=True)
        shutil.copytree(idx, real)
        print(f"{arm}: {rec['seconds']} s, {rec['calls']} calls, prompt {rec['prompt']:,}, "
              f"completion {rec['completion']:,}, usd {rec['usd_metered']}, notes {len(rec['notes'])}, "
              f"threads {rec['threads']['os']}, summary {rec['summary']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
