# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Offline: do a narrowed read's windows hold the facts the context-cap probe
scores? For each first and return turn of evals/probe_context_cap.py, the
turn's request is the query over the lecture it names; a fact counts only if
the note itself holds it.

Why this exists: on 2026-10-15 a narrowed window of Lezione 18 ended 15 chars
before the momentum rule it was centred on (docs/evaluation/
compaction-keep-2026-10-15.md, finding 4). A live chat costs money and mixes
the window with the model's tool choices; whether the served text holds the
fact is a string test.

Run it on a vault with a lexical index, so the windows get the idf weights the
tool uses (rerank.window_weights), e.g. a probe run's scratch copy:

  SILICA_VAULT=<scratch>/vault HOME=<scratch>/home \\
      uv run python -m evals.probe_window_recall <scratch>/vault/Inbox/machine_learning_md
"""
from __future__ import annotations

import math
import re
import sys

from evals.probe_context_cap import CASES
from silica.kernel.recall import rerank
from silica.tools import atomic

_HEAD = re.compile(r"^#{1,6} ", re.M)


def _windows(body: str, query: str, width: int, run_on: int) -> list[tuple[int, str]]:
    spans = rerank.best_window_spans(body, query, width, atomic.READ_QUERY_WINDOWS,
                                     rerank.window_weights(query), snap=True)
    saved, atomic.READ_QUERY_RUN_ON = atomic.READ_QUERY_RUN_ON, run_on
    try:
        return atomic._run_on(body, spans)
    finally:
        atomic.READ_QUERY_RUN_ON = saved


def _sections(body: str, query: str, budget: int = 6000) -> list[tuple[int, str]]:
    """Heading-bounded sections by idf-weighted hits over sqrt(length), in
    document order up to the budget; an oversized one cut to its best window."""
    wts = rerank.window_weights(query)
    cuts = [m.start() for m in _HEAD.finditer(body)]
    scored = []
    for a, b in zip([0] + cuts, cuts + [len(body)]):
        low = body[a:b].lower()
        sc = sum(w * low.count(t) for t, w in wts.items())
        if sc > 0:
            scored.append((sc / math.sqrt(max(b - a, 200)), a, b))
    picked, used = [], 0
    for _, a, b in sorted(scored, reverse=True):
        if used >= budget:
            break
        if b - a > budget - used:
            if budget - used < 800:
                continue
            o, t = rerank.best_window_spans(body[a:b], query, budget - used, 1, wts, snap=True)[0]
            a, b = a + o, a + o + len(t)
        picked.append((a, body[a:b]))
        used += b - a
    return sorted(picked)


ARMS = {
    "3x2000": lambda b, q: _windows(b, q, 2000, 0),
    "3x2700": lambda b, q: _windows(b, q, 2700, 0),
    "3x2000 run-on 800": lambda b, q: _windows(b, q, 2000, 800),
    "sections": _sections,
}


def main(notes_dir: str) -> None:
    recall = {a: 0.0 for a in ARMS}
    chars = {a: 0 for a in ARMS}
    n = 0
    for turns in CASES.values():
        for t in turns:
            lec = re.search(r"Lezione (\d+)", t["text"])
            if t["kind"] == "synth" or not lec:
                continue
            body = open(f"{notes_dir}/Lezione {lec[1]}.md", encoding="utf-8").read()
            # The lecture's own name locates the note, not the passage (atomic._narrow_to_query).
            query = " ".join(w for w in re.findall(r"\w+", t["text"])
                             if w.lower() != "lezione" and not w.isdigit())
            held = [f for f in t["facts"] if re.search(f, body, re.I)]
            for arm, pick in ARMS.items():
                served = "\n".join(x for _, x in pick(body, query))
                recall[arm] += sum(1 for f in held if re.search(f, served, re.I)) / len(held) if held else 1
                chars[arm] += len(served)
            n += 1
    print("| windows | mean fact recall | chars served |\n|---|---|---|")
    for arm in ARMS:
        print(f"| {arm} | {recall[arm] / n:.2f} | {chars[arm] // n:,} |")


if __name__ == "__main__":
    main(sys.argv[1])
