# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The pre-registered gate of docs/superpowers/specs/2026-07-25-code-why-tree-design.md.

Throwaway instrument, NOT product code: the file tools below exist only to give
arm A the same working-tree reach a human has (grep, glob, read). They are
registered in TOOLS by import, and nothing in the product imports this module.

The thesis under test: the code lane helps on the WHY, not on the code. Every
eval in evals/ measures recall and QA over notes; none has ever measured whether
a bound rationale helps on a coding question. This does.

  A: grep + glob + read over the real working tree, docs/ included.
  B: the same three, plus silica_code_why and silica_recall.

docs/ stays visible to arm A even though it is gitignored — if Silica wins only
because the specs were hidden from A, the win is fake. The gate vault is the one
thing A cannot see (it IS arm B's channel), and both arms share the identical
file tools so B's delta can only come from the two extra tools.

Runs on this repo, which is the BEST case and not the average case: here the
knowledge outside the tree is maximal. A result here does not generalize to a
young repo where everything that matters is still in the code.

  uv run python -m evals.probe_code_why --backfill
  uv run python -m evals.probe_code_why --model openrouter/... --json bench/code_why.json
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

import silica.tools.codedocs_tool  # noqa: F401  — registers silica_code_why
import silica.tools.graph  # noqa: F401  — registers silica_recall
from silica.kernel import codedocs, gitstate, templates
from silica.tools import TOOLS, tool

# --- Pre-registered gate ------------------------------------------------------
# Declared before any arm ran. n=15 has power only for a large effect, which is
# exactly what the thesis predicts: on WHY, arm A cannot know, so it should sit
# near zero. If it does not move at n=15 the effect is at best small.

WHY_PASS = 3        # B - A on WHY, of 7
WHY_KILL = 1        # B - A <= this kills the lane
LOOKUP_TOL = 1      # |LOOKUP delta| above this voids the run as confounded
LOOKUP_CONFOUND = 2

# --- Secondary gate: retrieval effort ----------------------------------------
# Declared before the re-run, NOT before the first run: run 1 already showed
# 9.57 vs 6.86 mean tool calls on WHY. So this is a REPLICATION test of an
# effect seen once, not a discovery, and it is stated that way on purpose — a
# threshold fitted to the data it then confirms proves nothing.
#
# It is secondary and it cannot rescue a KILL on accuracy: the primary verdict
# stands on its own. What it can do is say whether the lane pays for itself in
# effort when accuracy ties, which is the only place run 1 showed any signal.

EFF_WIN_MIN = 5       # B strictly cheaper on >= 5 of the 7 WHY questions
EFF_MEDIAN_MIN = 0.20  # and the median per-question reduction is >= 20%

MAX_ITERATIONS = 12         # declared cost control, below the product default
GATE_VAULT = ".silica/code-why-gate"

# Both arms get these three. Arm B adds the code lane on top.
_FILE_TOOLS = ("probe_grep", "probe_glob", "probe_read")
_ARM_A_TOOLS = _FILE_TOOLS
_ARM_B_TOOLS = _FILE_TOOLS + ("silica_code_why", "silica_recall")

_SYSTEM = (
    "You are answering questions about the silica-agent repository. Investigate "
    "with your tools before answering — never answer from prior knowledge of "
    "this repository. Answer concisely, naming the files, symbols, numbers or "
    "verdicts the question asks for. If your tools do not supply the answer, say "
    "you do not have that information; never invent a rationale, a measurement, "
    "or a decision."
)

# Written from the MEMORY.md index and `git log`, never from the bodies of the
# backfilled notes: a question sourced from the note that is supposed to answer
# it measures the gate against itself. LOOKUP is the control, not the target.
QUESTIONS: list[dict] = [
    # --- LOOKUP (n=8): answerable from the current tree -----------------------
    {"id": "L1", "stratum": "LOOKUP",
     "q": "Which module holds the doc-to-source staleness verdict, and which "
          "function decides whether a change is structural or cosmetic?",
     "gold": "silica/kernel/codedocs.py; classify_change (aggregated per note by "
             "note_verdict, driven by stale_docs)."},
    {"id": "L2", "stratum": "LOOKUP",
     "q": "Where is commit_derived defined and who calls it?",
     "gold": "Defined in silica/agent/commit.py; called by the /wiki pipeline in "
             "silica/capabilities/codewiki.py."},
    {"id": "L3", "stratum": "LOOKUP",
     "q": "Where is the RRF fusion of the relatedness legs implemented?",
     "gold": "_rrf_fuse in silica/kernel/relatedness.py."},
    {"id": "L4", "stratum": "LOOKUP",
     "q": "Which environment variable sets Ollama's context window, and in which "
          "module is it read?",
     "gold": "OLLAMA_NUM_CTX, read in silica/agent/providers.py."},
    {"id": "L5", "stratum": "LOOKUP",
     "q": "Which function is the single choke point every code-lane consumer "
          "goes through to get its repo root?",
     "gold": "repo_root_for in silica/kernel/paths.py."},
    {"id": "L6", "stratum": "LOOKUP",
     "q": "Which module drives the /organize state machine?",
     "gold": "silica/router/organize_fsm.py."},
    {"id": "L7", "stratum": "LOOKUP",
     "q": "How does gitstate.latest_shas avoid one subprocess per path?",
     "gold": "One `git log --format=... --name-only` walk limited to the paths, "
             "newest-first: the first mention of a path is its latest commit, and "
             "the walk stops early once every path resolves. Per-path spawning "
             "would be a fork storm because wiki notes list every member file."},
    {"id": "L8", "stratum": "LOOKUP",
     "q": "Where is the list of tools excluded from the interactive chat toolset?",
     "gold": "_CHAT_EXCLUDED in silica/agent/constraints.py."},

    # --- WHY (n=7): rationale and closed directions ---------------------------
    # Rewritten after runs 1 and 2 both put arm A at 6/7, which made the +3 PASS
    # arithmetically unreachable: the old WHY answers sat in greppable markdown
    # under docs/. Every question below is sourced from a memory file that lives
    # OUTSIDE the working tree, and `fingerprints` is the mechanical proof —
    # leak_check() fails the run if all of a question's terms co-occur in any one
    # file arm A can read. Golds transcribed from the note bodies, questions from
    # the MEMORY.md index, so no question comes from the note that answers it.
    {"id": "W1", "stratum": "WHY", "fingerprints": ["Zagaran"],
     "q": "Why is the project published on PyPI as `silica-agent` rather than "
          "`silica`?",
     "gold": "Because `silica` is already taken on PyPI (Zagaran Inc., v0.0.3, "
             "2021). Only the distribution name changed: the import package and "
             "the `silica` command are unchanged."},
    {"id": "W2", "stratum": "WHY", "fingerprints": ["corpus-dependence"],
     "q": "Was IDF weighting adopted in the CORRELATE edge metric, and why?",
     "gold": "No, IDF in the metric was rejected by measurement: the raw top-30 "
             "stem counts are already sparse and clean, and IDF adds a "
             "corpus-dependence that breaks local refresh. The metric stayed "
             "Jaccard over top-30 raw-count stems at tau = 0.25."},
    {"id": "W3", "stratum": "WHY", "fingerprints": ["size-pathological"],
     "q": "What is wrong with the gap term in the E(vault) energy function?",
     "gold": "It is size-pathological: gap_score = size_a*size_b/(1+inter) scales "
             "with cluster size, so resolving an orphan by linking it INTO a "
             "cluster raises E instead of lowering it. E rewards fragmentation "
             "over growth. At unit weights the gap term also swamps cohesion."},
    {"id": "W4", "stratum": "WHY", "fingerprints": ["T7 rev2"],
     "q": "Did the episodic key schema (F1a) fix the knowledge-update key drift, "
          "and what did its acceptance gate say?",
     "gold": "No. The T7 rev2 formal gate was missed 1 of 6: the capture ceiling "
             "held and multi-session containment improved (one first-ever N/N "
             "row, group explosion tamed), but every knowledge-update row stayed "
             "at 1/2 because deep per-entity subkeys put the update on a "
             "different key than the original fact. The verdict was that KU "
             "chains need identity at capture time, not a deeper schema."},
    {"id": "W5", "stratum": "WHY", "fingerprints": ["category error"],
     "q": "Why was the `contradicts` relation of the contested bi-temporal spec "
          "not implemented?",
     "gold": "It is a category error: the reliability tiers rank notes, and an "
             "incoming claim is not a note (reliability_tier on a raw excerpt "
             "returns HUMAN). An implementable variant exists — suppress the "
             "contest only when the target strictly outranks the incoming — but "
             "it is a product decision needing its own gate."},
    {"id": "W6", "stratum": "WHY", "fingerprints": ["speedup demo"],
     "q": "In the read-time assembly session, why was re-launching arm 3 as a "
          "speedup demonstration the wrong move?",
     "gold": "Arm 3's dominant cost is the ingest phase, which the harness "
             "speedup (a thread pool over the questions) does not touch. The "
             "broader lesson: the slowness was bugs, not a cost of doing "
             "business — the answer/judge path had no working timeout and hung, "
             "and the harness ran every question serially."},
    {"id": "W7", "stratum": "WHY", "fingerprints": ["third prompt exhortation"],
     "q": "Can the distiller's episodic key discipline be fixed with prompt "
          "instructions?",
     "gold": "No. Three prompt attempts, three misses: the T7 acceptance gate was "
             "missed, the depth cap was violated outright, and the prompt's "
             "literal example was echoed verbatim into a store. Key discipline is "
             "not prompt-reachable on this distiller model; the fix has to be "
             "mechanical, at capture time in code."},
]


# --- Working-tree tools (both arms) -------------------------------------------
# ponytail: python walk, not `grep -r`. One dependency fewer, and the exclusion
# set is the load-bearing part (the gate vault must be unreachable), which is
# easier to state in code than to keep correct across grep's --exclude-dir flags.

_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".import_linter_cache", "bench", "dist",
    "build", ".silica",          # <- the gate vault lives here; A must not see it
}
# Vendored third-party repositories (4 GB, ~4000 markdown files). Excluded from
# BOTH arms and from the backfill: they hold other projects' knowledge, so they
# would slow every search without carrying a single silica rationale. Symmetric,
# so it cannot advantage either arm.
#
# The harness files are excluded for a harder reason: they carry the gold
# answers as literals, and the first smoke run had arm A grep this very file and
# answer "based on the content of evals/probe_code_why.py". A measurement
# instrument left inside the search space measures itself. This is the same
# vacuity the spec's two backfill guards were written against — they covered the
# questions' provenance but not the golds' reachability.
_EXCLUDED_PREFIXES = (
    "docs/repos",
    "evals/probe_code_why.py",
    "evals/test_probe_code_why.py",
)
_MAX_MATCHES = 80
_MAX_READ_LINES = 300
_MAX_GLOB = 200


def _repo_root() -> Path:
    root = gitstate.find_repo_root(Path(__file__))
    if root is None:
        raise SystemExit("probe_code_why must run inside the silica-agent git repo")
    return root


def _excluded(rel: str) -> bool:
    return (not _EXCLUDED_DIRS.isdisjoint(rel.split("/"))
            or rel.startswith(_EXCLUDED_PREFIXES))


def _walk(start: Path, root: Path):
    for dirpath, dirnames, filenames in os.walk(start):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if not _excluded((here / d).relative_to(root).as_posix())]
        for name in filenames:
            # files too, not just dirs: the exclusion set names two FILES (the
            # harness carries the gold answers) and pruning dirnames alone let
            # them straight through
            if not _excluded((here / name).relative_to(root).as_posix()):
                yield here / name


def _safe(root: Path, rel: str) -> Path | None:
    """Resolve a model-supplied path under the repo root, or None. Reuses the
    binding validator: same trust boundary, same rules."""
    if not rel:
        return root
    ok, err = codedocs.validate_documents([rel], root)
    if err or not ok:
        return None
    return None if _excluded(ok[0]) else root / ok[0]


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python regular expression to search for")
    path: str = Field(default="", description="Repo-relative file or directory to "
                                              "search under; '' searches the whole repo")


@tool(GrepArgs, cls="composed")
def probe_grep(pattern: str, path: str = "") -> dict:
    """Search the repository working tree for a regular expression. Returns
    `file:line: text` matches. Use this first to locate anything by content."""
    root = _repo_root()
    target = _safe(root, path)
    if target is None:
        return {"status": "error", "message": f"path not in the repo: {path!r}"}
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"status": "error", "message": f"bad regex: {e}"}
    files = [target] if target.is_file() else _walk(target, root)
    hits: list[str] = []
    total = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                total += 1
                if len(hits) < _MAX_MATCHES:
                    hits.append(f"{f.relative_to(root).as_posix()}:{i}: {line.strip()[:200]}")
    return {"status": "ok", "matches": hits, "residue": max(0, total - len(hits))}


class GlobArgs(BaseModel):
    pattern: str = Field(description="Glob over repo-relative paths, e.g. 'silica/kernel/*.py'")


@tool(GlobArgs, cls="composed")
def probe_glob(pattern: str) -> dict:
    """List repository files whose repo-relative path matches a glob."""
    root = _repo_root()
    found = sorted(p.relative_to(root).as_posix() for p in _walk(root, root))
    hits = [p for p in found if fnmatch.fnmatch(p, pattern)]
    return {"status": "ok", "paths": hits[:_MAX_GLOB],
            "residue": max(0, len(hits) - _MAX_GLOB)}


class ReadArgs(BaseModel):
    path: str = Field(description="Repo-relative file path to read")
    start: int = Field(default=1, description="1-indexed first line to return")


@tool(ReadArgs, cls="composed")
def probe_read(path: str, start: int = 1) -> dict:
    """Read a slice of a repository file, with line numbers."""
    root = _repo_root()
    target = _safe(root, path)
    if target is None or not target.is_file():
        return {"status": "error", "message": f"not a readable repo file: {path!r}"}
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    first = max(1, start)
    chunk = lines[first - 1:first - 1 + _MAX_READ_LINES]
    return {"status": "ok", "path": path, "start": first, "total_lines": len(lines),
            "text": "\n".join(f"{first + i}\t{ln}" for i, ln in enumerate(chunk))}


# --- Backfill -----------------------------------------------------------------
# Mechanical, not selective: every source file that names an existing repo path
# gets bound to that path. No selection driven by the questions — the PPR phase-0
# precedent (a kill gate that passed and was vacuous) is why this rule is stated
# rather than assumed.

_PATH_RE = re.compile(r"(?<![\w/.-])((?:silica|evals|tests|docs|scripts)/[\w./-]+)")
_TRAILING = ".,;:)]}'\"`"


def extract_paths(text: str, root: Path) -> list[str]:
    """Repo paths named in `text` that exist in the working tree."""
    out: list[str] = []
    for raw in _PATH_RE.findall(text or ""):
        p = raw.rstrip(_TRAILING).rstrip("/")
        if p and (root / p).exists():
            out.append(p)
    return list(dict.fromkeys(out))


def _body(source: Path) -> str:
    from silica.kernel import frontmatter

    text = source.read_text(encoding="utf-8", errors="replace")
    _, _, body = frontmatter.split(text)
    return f"# {source.stem}\n\n{body.strip()}\n"


def _note_rel(src: Path, root: Path) -> str:
    """Vault-relative note path, unique per source. Mirroring the source path
    keeps two `spec.md` files in different directories from clobbering each
    other — 47 notes were silently lost to bare stems on the first run."""
    if src.is_relative_to(root):
        return src.relative_to(root).as_posix()
    return f"memory/{src.name}"


def backfill(vault: Path, root: Path, sources: list[Path]) -> dict:
    """Write one note per source file that names a repo path. Goes through the
    product's own validator and stamper, so the fixture cannot be more permissive
    than the write channel it is meant to exercise."""
    head = gitstate.head_ref(root)
    written, skipped = 0, 0
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        docs, err = codedocs.validate_documents(extract_paths(text, root), root)
        if err or not docs:
            skipped += 1
            continue
        ref = head if any((root / p).is_file() for p in docs) else None
        content = templates.ensure_system_floor(_body(src))
        note = vault / _note_rel(src, root)
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(templates.stamp_documents(content, docs, ref), encoding="utf-8")
        written += 1
    return {"written": written, "skipped_no_path": skipped, "vault": str(vault)}


def default_sources(root: Path) -> list[Path]:
    """Every memory file and every doc under docs/, minus the vendored repos.
    The memory directory is outside the working tree by construction (it is the
    harness's own memory), which is why arm A cannot grep it; docs/ is inside
    and A sees all of it."""
    mem = Path.home() / ".claude" / "projects" / (
        "-" + str(root).strip("/").replace("/", "-")) / "memory"
    out = sorted(p for p in mem.glob("*.md") if p.name != "MEMORY.md") if mem.is_dir() else []
    return out + sorted(
        p for p in (root / "docs").rglob("*.md")
        if not _excluded(p.relative_to(root).as_posix()))


# --- Arms ---------------------------------------------------------------------

def leak_check(root: Path) -> list[tuple[str, str]]:
    """(qid, file) for every WHY question whose fingerprints all co-occur in one
    file arm A can read.

    A WHY question answerable by grep is not a WHY question. Runs 1 and 2 both
    put arm A at 6/7 because the answers sat in markdown under docs/, which made
    the +3 PASS arithmetically unreachable before arm B asked anything. This is
    that check made mechanical, so the stratum cannot quietly rot back into it.
    """
    fps = [(q["id"], [f.casefold() for f in q["fingerprints"]])
           for q in QUESTIONS if q.get("fingerprints")]
    out: list[tuple[str, str]] = []
    for f in _walk(root, root):
        try:
            text = f.read_text(encoding="utf-8", errors="replace").casefold()
        except (OSError, ValueError):
            continue
        rel = f.relative_to(root).as_posix()
        out += [(qid, rel) for qid, terms in fps if all(t in text for t in terms)]
    return out


def assert_arms_live() -> None:
    """Every named tool must actually be registered.

    run_agent's constraint filter is `name for name in constraints.tools if name
    in TOOLS` — a name whose module was never imported is dropped in silence. It
    was: the first smoke run had arm B reaching for probe_grep only, and arm B
    WAS arm A. Same class as _shared.assert_lexical_live — an A/B must never
    silently compare baseline against baseline.
    """
    missing = [n for n in set(_ARM_A_TOOLS + _ARM_B_TOOLS) if n not in TOOLS]
    if missing:
        raise SystemExit(f"arm tools not registered: {missing} — the arms would be "
                         f"identical and the gate would measure nothing.")


def ask(model: str, question: str, tools: tuple[str, ...]) -> dict:
    from silica.agent import loop as loop_mod
    from silica.agent.constraints import AgentConstraints
    from silica.agent.events import ToolCompleteEvent

    events: list[ToolCompleteEvent] = []

    def _collect(evt) -> None:
        if isinstance(evt, ToolCompleteEvent):
            events.append(evt)

    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": question}]
    err = None
    try:
        # temperature=0: a single-run A/B must measure the lever, not sampling.
        response = loop_mod.run_agent(
            messages, model, tool_progress_callback=_collect, temperature=0.0,
            constraints=AgentConstraints(tools=tools, max_iterations=MAX_ITERATIONS))
    except Exception as e:
        response, err = "", f"{type(e).__name__}: {e}"
    # Budget exhaustion is part of the effort story, not a hidden accuracy
    # penalty: arm A burned its whole iteration budget on W5 in run 1 and still
    # answered wrong. Recorded so the two gates can be read against each other.
    exhausted = response == "(silica: maximum iterations reached)"
    return {"response": (response or "").strip(),
            "tools_used": [e.name for e in events],
            "iterations": len({e.iteration for e in events}) + 1,
            "budget_exhausted": exhausted, "error": err}


def _verdict(why_delta: int, lookup_delta: int) -> str:
    if abs(lookup_delta) >= LOOKUP_CONFOUND:
        return "CONFOUNDED"          # B merely has more context, or the judge is skewed
    if why_delta >= WHY_PASS and abs(lookup_delta) <= LOOKUP_TOL:
        return "PASS"
    if why_delta <= WHY_KILL:
        return "KILL"
    return "GREY"                    # +2: does not decide, needs larger n


def efficiency(rows: list[dict], why_delta: int) -> dict:
    """Paired tool-call effort on the WHY stratum, with a sign test.

    Voided when arm B lost accuracy: answering faster and worse is not a win,
    so the effort number must never be readable in isolation.
    """
    from scipy.stats import binomtest

    sel = [r for r in rows if r["stratum"] == "WHY"]
    a = [len(r["A"]["tools_used"]) for r in sel]
    b = [len(r["B"]["tools_used"]) for r in sel]
    wins = sum(1 for x, y in zip(a, b) if y < x)
    losses = sum(1 for x, y in zip(a, b) if y > x)
    disc = wins + losses
    reductions = sorted((x - y) / x for x, y in zip(a, b) if x)
    median = reductions[len(reductions) // 2] if reductions else 0.0
    if len(reductions) % 2 == 0 and reductions:
        median = (reductions[len(reductions) // 2 - 1] + median) / 2

    if why_delta < 0:
        verdict = "VOID"             # cheaper but less correct is not cheaper
    elif wins >= EFF_WIN_MIN and median >= EFF_MEDIAN_MIN:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    return {
        "mean_calls": {"A": round(sum(a) / len(a), 2) if a else None,
                       "B": round(sum(b) / len(b), 2) if b else None},
        "max_calls": {"A": max(a, default=0), "B": max(b, default=0)},
        "budget_exhausted": {"A": sum(1 for r in sel if r["A"]["budget_exhausted"]),
                             "B": sum(1 for r in sel if r["B"]["budget_exhausted"])},
        "b_cheaper_on": wins, "b_costlier_on": losses, "n": len(sel),
        "median_reduction": round(median, 4),
        "sign_test_p": round(binomtest(wins, disc, 0.5).pvalue, 4) if disc else 1.0,
        "thresholds": {"win_min": EFF_WIN_MIN, "median_min": EFF_MEDIAN_MIN},
        "verdict": verdict,
    }


def run(model: str, judge_model: str, vault: Path, root: Path) -> dict:
    from evals._shared import provenance, warn_unpinned_provider
    from evals.longmemeval.runner import judge
    from evals.paired_stats import paired

    from silica.config import CONFIG

    assert_arms_live()
    leaks = leak_check(root)
    if leaks:
        raise SystemExit(
            f"WHY answers are greppable in the working tree: {leaks} — arm A can "
            f"read them, so the gate cannot pass. Rewrite those questions.")
    # CONFIG.openrouter_provider (env OPENROUTER_PROVIDER), the same source the
    # locomo and longmemeval runners read. factscore reads SILICA_OPENROUTER_
    # PROVIDER, which nothing ever sets, so its warning never fires.
    pin = CONFIG.openrouter_provider or None
    warn_unpinned_provider(model, pin)
    warn_unpinned_provider(judge_model, pin)

    rows: list[dict] = []
    for spec in QUESTIONS:               # same question order in both arms
        row = {"question_id": spec["id"], "stratum": spec["stratum"],
               "question": spec["q"], "gold": spec["gold"]}
        for arm, tools in (("A", _ARM_A_TOOLS), ("B", _ARM_B_TOOLS)):
            got = ask(model, spec["q"], tools)
            verdict = judge(judge_model, "multi-session", spec["q"], spec["gold"],
                            got["response"])
            row[arm] = {**got, "correct": verdict}
            print(f"  {spec['id']} {spec['stratum']:6s} arm {arm}: "
                  f"{'ok ' if verdict else 'no ' if verdict is not None else '?? '}"
                  f"({len(got['tools_used'])} tool calls)")
        rows.append(row)

    strata: dict[str, dict] = {}
    for stratum in ("LOOKUP", "WHY"):
        sel = [r for r in rows if r["stratum"] == stratum]
        a = sum(1 for r in sel if r["A"]["correct"])
        b = sum(1 for r in sel if r["B"]["correct"])
        ungraded = sum(1 for r in sel
                       if r["A"]["correct"] is None or r["B"]["correct"] is None)
        strata[stratum] = {"n": len(sel), "A": a, "B": b, "delta": b - a,
                           "ungraded": ungraded}

    verdict = _verdict(strata["WHY"]["delta"], strata["LOOKUP"]["delta"])
    # paired() is A-minus-B by argument order, so arm B goes in the first slot to
    # keep every delta in this report B-minus-A.
    def _doc(arm):
        return {"questions": [{"question_id": r["question_id"],
                               "correct": r[arm]["correct"], "abstention": False}
                              for r in rows]}

    return {
        "provenance": provenance(__file__),
        "config": {"model": model, "judge_model": judge_model,
                   "provider_pin": pin,
                   "vault": str(vault), "repo_root": str(root),
                   "max_iterations": MAX_ITERATIONS,
                   "arm_a_tools": list(_ARM_A_TOOLS), "arm_b_tools": list(_ARM_B_TOOLS)},
        "gate": {"why_pass": WHY_PASS, "why_kill": WHY_KILL,
                 "lookup_tolerance": LOOKUP_TOL, "lookup_confound": LOOKUP_CONFOUND},
        "strata": strata,
        "paired_overall": paired(_doc("B"), _doc("A")),
        "verdict": verdict,
        "efficiency": efficiency(rows, strata["WHY"]["delta"]),
        "questions": rows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.probe_code_why")
    ap.add_argument("--backfill", action="store_true",
                    help="build the gate vault from memories + docs, then exit")
    ap.add_argument("--vault", default=None, help=f"gate vault (default {GATE_VAULT})")
    ap.add_argument("--model", default=os.getenv("SILICA_MODEL", ""),
                    help="answering model for both arms")
    ap.add_argument("--judge-model", default=None, help="defaults to --model")
    ap.add_argument("--json", default="bench/code_why.json")
    args = ap.parse_args(argv)

    root = _repo_root()
    vault = Path(args.vault) if args.vault else root / GATE_VAULT

    if args.backfill:
        res = backfill(vault, root, default_sources(root))
        print(json.dumps(res, indent=2))
        return 0

    if not args.model:
        ap.error("--model is required (or set SILICA_MODEL)")
    # rglob: notes mirror their source path, so nothing lands at the vault root
    if not vault.is_dir() or not any(vault.rglob("*.md")):
        ap.error(f"gate vault {vault} is empty — run --backfill first")
    # The gate is void if arm A can read arm B's channel.
    rel = vault.resolve().relative_to(root) if vault.resolve().is_relative_to(root) else None
    if rel is not None and _EXCLUDED_DIRS.isdisjoint(rel.as_posix().split("/")):
        ap.error(f"gate vault {rel} is inside the working tree arm A can walk — "
                 f"put it under one of {sorted(_EXCLUDED_DIRS)}")

    import silica.driver
    from silica.config import CONFIG
    from silica.kernel import paths

    CONFIG.vault_path = str(vault)
    silica.driver._driver = None
    paths.clear_repo_root_cache()
    try:
        res = run(args.model, args.judge_model or args.model, vault, root)
    finally:
        silica.driver._driver = None

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    s = res["strata"]
    print(f"\nLOOKUP  A {s['LOOKUP']['A']}/{s['LOOKUP']['n']}  "
          f"B {s['LOOKUP']['B']}/{s['LOOKUP']['n']}  delta {s['LOOKUP']['delta']:+}")
    print(f"WHY     A {s['WHY']['A']}/{s['WHY']['n']}  "
          f"B {s['WHY']['B']}/{s['WHY']['n']}  delta {s['WHY']['delta']:+}")
    e = res["efficiency"]
    print(f"EFFORT  A {e['mean_calls']['A']} calls  B {e['mean_calls']['B']} calls  "
          f"B cheaper on {e['b_cheaper_on']}/{e['n']}  "
          f"median {-e['median_reduction']:+.0%} calls  p={e['sign_test_p']}")
    print(f"VERDICT {res['verdict']}   EFFICIENCY {e['verdict']}\nwritten → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
