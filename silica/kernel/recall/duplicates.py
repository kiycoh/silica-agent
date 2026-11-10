# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Near-duplicate notes: the cosine and title scan over the embedding index, and
the dedup work items a list of pairs becomes, pair by pair for /dedup and
family by family for /curate.

Below the tools because /dedup seeds its ledger from the same scan the tool runs,
and the golden probe holds both as the production decision.
"""
from __future__ import annotations

from typing import Any

from silica.config import CONFIG
from silica.driver import DRIVER
from silica.kernel.base.layout import in_folder
from silica.kernel.base.vectors import cosine
from silica.kernel.workqueue import DedupJob, WorkItem
from silica.kernel.write.contested import merge_rank


def scan_pairs(folder: str = "") -> tuple[list[dict], str | None]:
    """Cosine + title scan for near-duplicate note pairs — vectors only, no body
    reads. Returns (pairs, error); each pair is {source, target, score, full_score,
    title_score}. Shared by silica_dedup (sync) and the /dedup ledger seed.

    A pair is admitted when body similarity is borderline (tau_low < score <
    tau_high) OR titles are strongly similar (title_score >= tau_title) — the
    latter catches "ROS" / "JSON in ROS 2" where bodies diverge but titles are
    clearly related.
    """
    import numpy as np

    from silica.kernel.recall.embed import get_store

    store = get_store()
    if len(store) == 0:
        return [], "Embedding index empty — run /embed first."

    τ_high = CONFIG.sim_threshold_high
    τ_low = CONFIG.sim_threshold_low
    τ_title = CONFIG.sim_title_threshold

    scope = [p for p in store.paths() if in_folder(p, folder)]
    # One blocked matmul for the whole scope, not one matvec per note: the scan is
    # N searches over an N-row index, so the per-note shape was quadratic in vault
    # size. Same top-k (see cosine_top_k_batch); notes without a usable vector are
    # simply absent from the result, as the `if not vec: continue` did.
    neighbours = store.cosine_top_k_batch(scope, k=CONFIG.dedup_scan_k)
    seen_pairs: set[tuple[str, str]] = set()
    pairs: list[dict] = []

    _title_cache: dict[str, Any] = {}

    def _title_vec(path: str):
        """Title vector as a float64 array, or None when the note has none.

        ponytail: memo, because the title gate below re-converted the same stored
        list on every candidate pair — 10 conversions per distinct vector, measured
        — and the conversion, not the dot product, was the cost (158 us -> 6.6 us
        per pair). float64 specifically: `cosine` asks for float64, so asarray on
        an already-float64 array is a no-op and the score stays bit-identical; a
        float32 cache would be re-copied on every call and is slower than no cache
        at all. Drop this if the title vectors ever arrive as arrays.
        """
        if path not in _title_cache:
            raw = store.get_title_vec(path)
            _title_cache[path] = np.asarray(raw, dtype=np.float64) if raw else None
        return _title_cache[path]

    for p in scope:
        for match in neighbours.get(p, ()):
            score = match.get("score", 0.0)
            other = match.get("path", "")
            if not other or not in_folder(other, folder):
                continue

            # Title-level similarity gate: catches pairs whose bodies diverge
            # but whose titles share a strong semantic relationship.
            title_vec_p = _title_vec(p)
            title_vec_o = _title_vec(other)
            title_score = (
                cosine(title_vec_p, title_vec_o)
                # `is not None`, not truthiness: a numpy array raises on bool().
                if title_vec_p is not None and title_vec_o is not None
                else 0.0
            )

            in_full_window = τ_low < score < τ_high
            in_title_gate = title_score >= τ_title
            # continue (not break): candidates are score-descending; a match above
            # τ_high arrives before borderline ones — break would kill the loop early.
            if not in_full_window and not in_title_gate:
                continue

            key = tuple(sorted((p, other)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            pairs.append({
                "source": p,
                "target": other,
                "score": max(score, title_score),
                "full_score": score,
                "title_score": title_score,
            })

    return pairs, None


def _read(path: str) -> str | None:
    """The note's body, None when it cannot be read: what an unreadable side
    means is each command's own call."""
    try:
        return DRIVER.read_note(path).content or ""
    except Exception:
        return None


def _stale_line(path: str) -> str:
    """The line a stale code-doc note's text starts with where the judge reads
    it, "" for a fresh one (spec-stale-triggers §4): this read bypasses the read
    gate's banner. Served from the read-only peek, so it never pays the walk.
    Annotate only: the model decides, nothing blocks."""
    try:
        from silica.kernel.code import codedocs

        lvl = codedocs.peek_level(codedocs.peek(CONFIG.vault_path), path)
    except Exception:
        return ""  # the guard is an aid; the read succeeds without it
    return f"[stale] {lvl}: verify against the source before reusing claims\n\n" if lvl else ""


def _loser_text(path: str, body: str) -> str:
    """`body` as the judge reads it. An empty note gets no line: a prefix with
    nothing under it is noise, not a warning."""
    return _stale_line(path) + body if body else body


def _split(a: str, b: str, body_a: str, body_b: str) -> tuple[str, str, str]:
    """(merge target, loser, the loser's body). The more reliable note is the
    merge target; the other is the source of new info. Reliability first,
    length only to break a tie within a tier, where `b` wins."""
    if merge_rank(body_b) >= merge_rank(body_a):
        return b, a, body_a
    return a, b, body_b


def pairs_to_items(pairs: list[dict]) -> list[WorkItem]:
    """Build /dedup's WorkItems from {source, target, score} dicts, one per pair.
    Optional full_score/title_score telemetry is propagated into context when
    present.
    """
    items: list[WorkItem] = []
    for pair in pairs:
        source = pair.get("source")
        target = pair.get("target")
        score = pair.get("score", 0.0)
        if not source or not target:
            continue
        body_src, body_tgt = _read(source), _read(target)
        if body_src is None or body_tgt is None:
            continue  # a pair with an unreadable side cannot be judged: left for the next run

        larger, smaller, smaller_body = _split(source, target, body_src, body_tgt)
        job = DedupJob.between_notes(loser=smaller, winner=larger, loser_body=smaller_body, score=score)
        reason = f"dedup score={score:.3f}"
        if "full_score" in pair and "title_score" in pair:
            job.full_score = pair["full_score"]
            job.title_score = pair["title_score"]
            reason += f" (full={pair['full_score']:.3f} title={pair['title_score']:.3f})"

        items.append(WorkItem(kind="dedup", target_path=larger, context=job, reason=reason))
    return items


def families_to_items(pairs: list[dict]) -> list[WorkItem]:
    """Build /curate's WorkItems from {source, target, score, reason} dicts,
    collapsing duplicate *families*.

    Pairwise dedup leaves one survivor per local top-1 hub: a family {A,B,C,D,E}
    whose top-1 edges are A→B, C→B, D→E, F→E collapses to TWO notes, not one.
    So confirmed pairs (score ≥ τ_high) are union-found into connected components
    and each component funnels into its single largest note.

    Transitive closure is applied ONLY to confirmed pairs — chaining borderline
    (< τ_high) links would merge distant notes — so borderline pairs stay per-pair.
    Safety: every item still passes the ternary judge, and curate items carry no
    `target_dir`, so a "distinct" verdict is a no-op. Union-find proposes the
    merge target; the judge disposes. A false union costs a judge call, never a bad
    merge.
    """
    tau_high = CONFIG.sim_threshold_high

    _body_cache: dict[str, str] = {}

    def body(p: str) -> str:
        # An unreadable note reads as empty here, as /curate always took it:
        # the judge's own read of the merge target skips it.
        if p not in _body_cache:
            _body_cache[p] = _loser_text(p, _read(p) or "")
        return _body_cache[p]

    stem = lambda p: p.removesuffix(".md").rsplit("/", 1)[-1]  # noqa: E731

    # Connected components over confirmed pairs only (nx over a hand-rolled
    # union-find — same result, one well-known call).
    import networkx as nx

    graph = nx.Graph()
    node_score: dict[str, float] = {}
    for pair in pairs:
        if pair["score"] >= tau_high:
            graph.add_edge(pair["source"], pair["target"])
            for n in (pair["source"], pair["target"]):
                node_score[n] = max(node_score.get(n, 0.0), pair["score"])

    components = [set(c) for c in nx.connected_components(graph)]
    # node -> component-id, so the borderline pass below can ask "same component?"
    comp_of: dict[str, int] = {n: i for i, c in enumerate(components) for n in c}

    items: list[WorkItem] = []
    # 1. Each confirmed component → collapse every member into its largest note.
    for members in components:
        canonical = max(members, key=lambda p: merge_rank(body(p)))
        for m in members:
            if m == canonical:
                continue
            sc = node_score.get(m, tau_high)
            items.append(WorkItem(
                kind="dedup",
                target_path=canonical,
                context=DedupJob.between_notes(loser=m, winner=canonical, loser_body=body(m), score=sc),
                reason=f"curate dedup family → {stem(canonical)} (score={sc:.3f})",
            ))

    # 2. Borderline pairs — per-pair behaviour (more reliable note is target),
    #    skipping any pair already absorbed by a shared confirmed component.
    for pair in pairs:
        if pair["score"] >= tau_high:
            continue
        source, target = pair["source"], pair["target"]
        if comp_of.get(source, -1) == comp_of.get(target, -2):
            continue
        larger, smaller, smaller_body = _split(source, target, body(source), body(target))
        items.append(WorkItem(
            kind="dedup",
            target_path=larger,
            context=DedupJob.between_notes(loser=smaller, winner=larger, loser_body=smaller_body,
                                           score=pair["score"]),
            reason=pair.get("reason") or f"curate dedup score={pair['score']:.3f}",
        ))
    # Family members share target_path=canonical by construction → one judge
    # call per family instead of one per member.
    from silica.kernel.workqueue import batch_dedup_items
    return batch_dedup_items(items)
