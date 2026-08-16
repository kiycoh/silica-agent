# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Verification-based residue coverage check.

Replaces the open-enumeration check ("list what is missing" over
notes[:6000]) refuted by the 2026-08-16 ROI audit: its declared residue was
100% false positives because it read 1.4-4% of the note set it judged
(docs/audits/2026-08-16-boundary-prefetch-lever.md). This core inverts the
question into the narrow, reliable one:

1. decompose the SOURCE into atomic facts once (prompt kept in parity with
   evals/factscore.py, which the product cannot import);
2. per fact, retrieve candidate evidence with the vault's own embed index
   (refreshed at every WRITE) plus lexical paragraph windowing, instead of
   stuffing a truncated note dump into the prompt;
3. judge each fact against ITS evidence in batches ("N: yes/no").

Fail-open on the declaration side: a judge failure or degraded dependency
never declares a fact missing (a false "missing" report is the disease this
module cures). Missing facts are DECLARED and routed to the deferred store;
there is no re-distill round (refuted: it re-added already-present facts at
60-170s per round).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

_JUDGE_BATCH = 25  # facts per judge call, same amortization as evals/factscore

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
    "For each numbered fact below, decide whether it is stated or directly "
    "implied by ITS OWN evidence block (notes from a knowledge vault). Judge "
    "each fact only from its evidence: a fact that is plausible, or that you "
    "know to be true from elsewhere, but that the evidence does not state, is "
    "\"no\". Do not argue for the fact on the evidence's behalf.\n\n"
    "{items}\n\n"
    "Answer with one line per fact, exactly \"N: yes\" or \"N: no\". "
    "No other text."
)

_FACT_RE = re.compile(r"^-\s+(.+)$", re.M)
_VERDICT_RE = re.compile(r"^\s*(\d+)\s*[:.)]\s*(yes|no)\b", re.M | re.I)


def _llm(prompt: str, max_tokens: int) -> str:
    from silica.agent.llm import call_llm
    from silica.config import CONFIG

    # temperature=0: factscore-judge parity. Free temperature on a
    # reasoning-class router model let the thinking eat the output budget and
    # returned empty/misnumbered replies in-run (45/45 and 92/92 judge
    # failures, run 5e88feb0).
    resp = call_llm(CONFIG.model, [{"role": "user", "content": prompt}],
                    max_tokens=max_tokens, temperature=0)
    return resp.text or ""


def decompose_facts(source: str) -> list[str] | None:
    """SOURCE -> atomic fact lines. None when decomposition failed (skip
    verification), distinct from [] (a legitimately fact-free source)."""
    # Output scales with input; ceiling keeps a book chapter's list bounded.
    budget = min(8192, max(1024, len(source) // 8))
    # One retry on an empty parse: same nondeterministic reasoning/format
    # flake as the judge (2/10 files failed decompose in run 4dabf989).
    for attempt in (1, 2):
        out = _llm(_DECOMPOSE_PROMPT.format(text=source), budget)
        facts = [f for f in (m.group(1).strip() for m in _FACT_RE.finditer(out)) if f]
        if facts:
            return facts
        logger.warning("residue: decompose parsed 0 facts (reply head: %r)%s",
                       out[:80], " — retrying once" if attempt == 1 else "")
    return None


def filter_on_theme(
    facts: list[str],
    source: str,
    *,
    embedder: Any,
    theme_tau: float,
) -> tuple[list[str], list[list[float]], int]:
    """Keep only facts thematically aligned with the source document.

    The same order parameter SALIENCE applies to concepts: a fact below
    theme_tau is content the pipeline deliberately drops, so declaring it
    missing would contradict the product's own contract (narrative filler,
    front matter). Returns (kept_facts, their_vectors, off_theme_count);
    tau <= 0 or a missing theme vector keeps everything (fail-open).
    """
    vecs = embedder.embed(facts)
    if theme_tau <= 0:
        return facts, vecs, 0
    from silica.kernel.recall.embed import _cosine, document_theme_vector
    theme = document_theme_vector(embedder, source)
    if not theme:
        return facts, vecs, 0
    kept: list[str] = []
    kept_vecs: list[list[float]] = []
    for f, v in zip(facts, vecs):
        if _cosine(v, theme) >= theme_tau:
            kept.append(f)
            kept_vecs.append(v)
    return kept, kept_vecs, len(facts) - len(kept)


def _best_paragraphs(body: str, fact: str, n: int = 3) -> list[str]:
    """The n paragraphs of ``body`` sharing the most content words with
    ``fact`` — lexical windowing so a 30KB aggregate note contributes a
    focused excerpt instead of blowing up the judge prompt."""
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    if len(paras) <= n:
        return paras or ([body] if body else [])
    words = {w for w in re.findall(r"[a-zà-ú]{4,}", fact.lower())}
    scored = sorted(
        ((len(words & set(re.findall(r"[a-zà-ú]{4,}", p.lower()))), i, p)
         for i, p in enumerate(paras)),
        key=lambda t: (-t[0], t[1]),
    )
    keep = sorted(scored[:n], key=lambda t: t[1])  # document order back
    return [p for _, _, p in keep]


def gather_evidence(
    facts: list[str],
    *,
    embedder: Any,
    store: Any,
    read_body: Callable[[str], str],
    k: int = 2,
    paras: int = 3,
    vecs: list[list[float]] | None = None,
) -> list[str] | None:
    """Per-fact evidence text: top-k notes by cosine, windowed paragraphs.

    One batched embed call for all facts, skipped when ``vecs`` carries the
    vectors filter_on_theme already computed. None on embed failure
    (verification skipped upstream). Runs on the caller's thread by design:
    reads are snapshotted here so the judge calls can run in the background
    without racing autolink/backlink note edits.
    """
    if vecs is None:
        try:
            vecs = embedder.embed(facts)
        except Exception as _e:
            logger.debug("residue: fact embed failed (%s) — verification skipped", _e)
            return None
    if len(vecs) != len(facts):
        return None

    body_cache: dict[str, str] = {}

    def _body(path: str) -> str:
        if path not in body_cache:
            text = ""
            for cand in (path, f"{path}.md"):
                try:
                    text = read_body(cand) or ""
                except Exception:
                    text = ""
                if text:
                    break
            body_cache[path] = text
        return body_cache[path]

    evidence: list[str] = []
    for fact, vec in zip(facts, vecs):
        blocks: list[str] = []
        try:
            hits = store.cosine_top_k(vec, k=k)
        except Exception as _se:
            logger.debug("residue: evidence search failed (%s)", _se)
            hits = []
        for h in hits:
            body = _body(h["path"])
            if not body:
                continue
            excerpt = "\n".join(_best_paragraphs(body, fact, n=paras))
            blocks.append(f"[[{h['name']}]]\n{excerpt}")
        evidence.append("\n\n".join(blocks) or "(no notes found)")
    return evidence


def judge_covered(facts: list[str], evidence: list[str]) -> list[bool | None]:
    """Per-fact verdicts: True = stated in its evidence. None = judge failure
    (skipped/garbled line), excluded from any declaration by the caller."""
    verdicts: list[bool | None] = []
    for start in range(0, len(facts), _JUDGE_BATCH):
        batch = facts[start:start + _JUDGE_BATCH]
        ev = evidence[start:start + _JUDGE_BATCH]
        items = "\n\n".join(
            f"FACT {i + 1}: {f}\nEVIDENCE {i + 1}:\n{e}"
            for i, (f, e) in enumerate(zip(batch, ev))
        )
        by_idx: dict[int, bool] = {}
        # One retry on a zero-parse reply: the failure is nondeterministic
        # (reasoning/format flake), so a second deterministic attempt usually
        # lands; more than one re-pays the call for a systemic problem.
        for attempt in (1, 2):
            try:
                # 4096 out: verdict lines are tiny but a reasoning model's
                # thinking shares the budget.
                out = _llm(_JUDGE_PROMPT.format(items=items), 4096)
            except Exception as _e:
                logger.warning("residue: judge call failed (%s)", _e)
                break
            by_idx = {int(m.group(1)): m.group(2).lower() == "yes"
                      for m in _VERDICT_RE.finditer(out)}
            if by_idx:
                break
            logger.warning("residue: judge reply parsed 0/%d verdicts%s",
                           len(batch), " — retrying once" if attempt == 1 else "")
        verdicts.extend(by_idx.get(i + 1) for i in range(len(batch)))
    return verdicts


def verify_missing(
    source: str,
    *,
    embedder: Any,
    store: Any,
    read_body: Callable[[str], str],
    facts: list[str] | None = None,
    theme_tau: float = 0.0,
) -> dict:
    """The whole pipeline: decompose (unless ``facts`` precomputed) → theme
    filter → evidence → judge. Returns {"missing", "total", "judged",
    "failures", "off_theme"} and optionally "skipped" naming a degraded
    dependency; missing is [] on every degrade (fail-open, same contract as
    the old check)."""
    empty = {"missing": [], "total": 0, "judged": 0, "failures": 0,
             "off_theme": 0}
    if embedder is None:
        return {**empty, "skipped": "no embedder"}
    if facts is None:
        facts = decompose_facts(source)
    if facts is None:
        return {**empty, "skipped": "decompose failed"}
    if not facts:
        return empty
    total = len(facts)
    vecs = None
    off_theme = 0
    if theme_tau > 0:
        try:
            facts, vecs, off_theme = filter_on_theme(
                facts, source, embedder=embedder, theme_tau=theme_tau)
        except Exception as _e:
            logger.debug("residue: theme filter failed (%s) — verification skipped", _e)
            return {**empty, "total": total, "skipped": "evidence failed"}
        if not facts:
            return {**empty, "total": total, "off_theme": off_theme}
    evidence = gather_evidence(facts, embedder=embedder, store=store,
                               read_body=read_body, vecs=vecs)
    if evidence is None:
        return {**empty, "total": total, "off_theme": off_theme,
                "skipped": "evidence failed"}
    verdicts = judge_covered(facts, evidence)
    missing = [f for f, v in zip(facts, verdicts) if v is False]
    judged = sum(1 for v in verdicts if v is not None)
    return {"missing": missing, "total": total, "judged": judged,
            "failures": verdicts.count(None), "off_theme": off_theme}
