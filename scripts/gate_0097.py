#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0097's gate, counted from the narration: is a named note's path enough?

    uv run python scripts/gate_0097.py [--first 50] [--vault PATH] [DIR_OR_FILE ...]

Default source is ~/.silica/narration. On the pilot every tenant has its own,
so pass them all: /srv/silica/*/.silica/narration.

The rule is the ADR's "Gate (point 2)", per model:

- counted: a user turn whose stored text carries a <named-notes> block naming
  at least one resolved path. The block is read, not the [[...]] in the text:
  it is what resolution produced when the turn ran, and resolving the links
  again against a vault that has since moved on would count other notes.
- excluded: a turn that writes, moves, flags or deletes a named note. There
  the note is the turn's object, and editing it needs no read to be right.
- set aside, and printed as their own number: a turn with no final answer,
  still running or stopped by an error or a cancel, since "before the final
  answer" has nothing to measure against.
- pass: every named path read by a successful silica_read_note before the
  final answer.
- lenient pass: also credits a path whose text reached the model through any
  other tool (a search snippet, silica_read, a recall excerpt), because the
  fallback the gate decides on (inlining the note) only adds what that missed.

The first --first counted turns per model, by time, as the ADR says. A model
is named by the turn's first LLM call; a turn that made none is "?".
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from silica.agent.mentions import CLOSE, OPEN, resolve

READ = "silica_read_note"
# The tools that change a note, and the arguments that name it. The commit
# lane's own `write` beats are read too (payload.touched), for writes that
# reach the vault through a pipeline rather than one of these calls.
WRITES = {
    "silica_write_note": ("path",),
    "silica_patch_note": ("name",),
    "silica_flag_note": ("name",),
    "silica_move": ("ref", "to"),
    "silica_delete": ("ref",),
}
_BLOCK = re.compile(re.escape(OPEN) + r"(.*?)" + re.escape(CLOSE), re.S)
_NAMED = re.compile(r"^Notes the user named[^:]*: (.+)$", re.M)
# Eight words in a row shared with the note: long enough that two notes do not
# share it by chance, short enough to survive inside a 300-char search snippet.
SHINGLE = 8
# Frontmatter is left out of the note's words: recall and search serve the body
# without it, and in a short note it made the only n-gram one no result holds.
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def _text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, list):  # an image turn's parts
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return str(c or "")


def named_paths(text: str) -> list[str]:
    m = _BLOCK.search(text)
    hit = _NAMED.search(m.group(1)) if m else None
    return [p.strip() for p in hit.group(1).split(" · ") if p.strip()] if hit else []


def turns(path: Path):
    """Every user turn in one session file, with the calls it made."""
    vault, cur = "", None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            b = json.loads(line)
        except ValueError:
            continue  # a torn tail or a corrupt line carries no turn
        kind, p = b.get("kind"), b.get("payload") or {}
        msg = p.get("message") if kind == "turn" else None
        if kind == "session":
            vault = p.get("vault") or ""
        elif isinstance(msg, dict) and msg.get("role") == "user":
            if cur:
                yield cur
            cur = {"id": f"{b.get('sid')}#{b.get('seq')}", "ts": b.get("ts") or 0, "vault": vault,
                   "named": named_paths(_text(msg)), "model": None, "calls": {},
                   "touched": [], "answer": None}
        elif cur is None:
            continue
        elif kind == "call" and cur["model"] is None and p.get("model"):
            cur["model"] = p["model"]
        elif isinstance(msg, dict) and msg.get("role") == "assistant" and not msg.get("tool_calls"):
            # The last one wins: an announce-and-stop preamble is narrated the
            # same way and is followed by the real answer (loop.py).
            cur["answer"] = b.get("seq")
        elif kind == "tool" and b.get("id"):
            c = cur["calls"].setdefault(b["id"], {"name": p.get("name"), "args": {}})
            if b.get("status") == "running":
                c["args"] = p.get("args") or {}
            else:
                c.update(status=b.get("status"), result=p.get("result"), end=b.get("seq"))
        elif kind == "write":
            cur["touched"] += [t[0] for t in p.get("touched") or [] if t]
    if cur:
        yield cur


@lru_cache(maxsize=None)
def vault_notes(vault: str) -> dict[str, str]:
    """{vault-relative path: stem} of every note, or {} when the vault is not here."""
    root = Path(vault)
    if not vault or not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.stem for p in root.rglob("*.md")
            if not any(part.startswith(".") for part in p.relative_to(root).parts)}


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


@lru_cache(maxsize=None)
def _shingles(vault: str, path: str) -> tuple[int, frozenset] | None:
    """The note body's word n-grams, n = SHINGLE or the whole body when
    shorter; None when the note is not on this disk."""
    try:
        w = _words(_FRONTMATTER.sub("", (Path(vault) / path).read_text(encoding="utf-8")))
    except OSError:
        return None
    n = min(SHINGLE, len(w))
    return n, frozenset(tuple(w[i:i + n]) for i in range(len(w) - n + 1)) if n else frozenset()


def _flat(result) -> str:
    """A tool result as the text the model read: a JSON result's strings, unescaped."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result

    def walk(x) -> str:
        if isinstance(x, dict):
            x = list(x.values())
        return "\n".join(map(walk, x)) if isinstance(x, list) else str(x)
    return walk(result)


def carries(path: str, vault: str, result) -> bool:
    """Whether `result` holds text of the note at `path`. With the vault gone,
    the path itself is the only evidence left."""
    text = _flat(result)
    sh = _shingles(vault, path)
    if sh is None:
        return path in text or path.removesuffix(".md") in text
    n, grams = sh
    t = _words(text)
    hit = bool(n) and any(tuple(t[i:i + n]) in grams for i in range(len(t) - n + 1))
    # A body under eight words ("# Empty") is a few common words: it counts
    # only where the result also names the note, as recall and search do.
    return hit and (n == SHINGLE or path.removesuffix(".md") in text)


def _ok(c: dict, answer) -> bool:
    if c.get("status") != "done" or c.get("end") is None or answer is None or c["end"] > answer:
        return False
    try:
        return not (isinstance(r := json.loads(c["result"]), dict) and "error" in r)
    except (TypeError, ValueError):
        return True  # plain text is a note, not an error (loop._is_tool_failure)


def judge(t: dict) -> dict | None:
    """None when the turn is not counted; else its strict and lenient verdict."""
    named = t["named"]
    if not named:
        return None
    titles = vault_notes(t["vault"]) or {p: Path(p).stem for p in named}

    def names(arg, path):
        ref = arg.split("#")[0].strip("[] ").removeprefix("./").lstrip("/") if isinstance(arg, str) else ""
        return bool(ref) and resolve(ref, titles) == [path]

    for c in t["calls"].values():
        for key in WRITES.get(c["name"], ()):
            if any(names(c["args"].get(key), p) for p in named):
                return {"skip": "writes"}
    if set(t["touched"]) & set(named):
        return {"skip": "writes"}
    if t["answer"] is None:
        return {"skip": "unanswered"}
    reads = [c for c in t["calls"].values() if c["name"] == READ]
    missed, lenient_missed = [], []
    for p in named:
        if any(_ok(c, t["answer"]) and (names(c["args"].get("name"), p)
                                        or carries(p, t["vault"], c.get("result"))) for c in reads):
            continue
        via = [c["name"] for c in t["calls"].values()
               if _ok(c, t["answer"]) and carries(p, t["vault"], c.get("result"))]
        tried = any(names(c["args"].get("name"), p) for c in reads)
        why = f"via {via[0]}" if via else "read failed" if tried else "not read"
        missed.append((p, why))
        if not via:
            lenient_missed.append(p)
    return {"skip": None, "strict": not missed, "lenient": not lenient_missed, "missed": missed}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def count(sources: list[Path], first: int = 50, vault: str = "") -> dict[str, dict]:
    files = [f for s in sources for f in (sorted(s.glob("*.jsonl")) if s.is_dir() else [s])]
    by_model: dict[str, list] = defaultdict(list)
    skipped: dict[str, dict[str, int]] = defaultdict(lambda: {"writes": 0, "unanswered": 0})
    for t in sorted((t for f in files for t in turns(f)), key=lambda t: t["ts"]):
        if vault and Path(t["vault"]).resolve() != Path(vault).resolve():
            continue
        model = t["model"] or "?"
        v = judge(t)
        if v is None or len(by_model[model]) >= first:
            continue
        if v["skip"]:
            skipped[model][v["skip"]] += 1
            continue
        by_model[model].append((t, v))
    out = {}
    for model, rows in by_model.items():
        out[model] = {"n": len(rows), **skipped[model],
                      "strict": sum(v["strict"] for _, v in rows),
                      "lenient": sum(v["lenient"] for _, v in rows),
                      "failures": [(t["id"], len(t["named"]), v["missed"]) for t, v in rows if not v["strict"]]}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sources", nargs="*", type=Path, default=[Path.home() / ".silica" / "narration"])
    ap.add_argument("--first", type=int, default=50, help="counted turns per model (the ADR's 50)")
    ap.add_argument("--vault", default="", help="only sessions on this vault")
    a = ap.parse_args(argv)
    res = count(a.sources, a.first, a.vault)
    if not res:
        print("no counted turn: no user turn carries a resolved <named-notes> block")
    for model, r in res.items():
        n = r["n"]
        print(f"{model}: n={n} (excluded: {r['writes']} change a named note, "
              f"{r['unanswered']} have no final answer)")
        for key in ("strict", "lenient"):
            k = r[key]
            lo, hi = wilson(k, n)
            # 45 of 50 is 90%: a --first above 50 is held to the same rate.
            verdict = "open, n<50" if n < 50 else "holds" if k >= 0.9 * n else "fails"
            print(f"  {key:8s}{k}/{n}  95% CI {lo:.3f}-{hi:.3f}  gate 45/50: {verdict}")
        for tid, total, missed in r["failures"]:
            print(f"  FAIL {tid}  read {total - len(missed)}/{total}  "
                  + "; ".join(f"{p} ({why})" for p, why in missed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
