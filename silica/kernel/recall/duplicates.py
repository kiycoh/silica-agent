# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Near-duplicate notes: the cosine and title scan over the embedding index, and
the dedup work items a list of pairs becomes.

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


def pairs_to_items(pairs: list[dict]) -> list[WorkItem]:
    """Build dedup WorkItems from {source, target, score} dicts — the single
    place bodies are read and the larger/smaller split is decided. Optional
    full_score/title_score telemetry is propagated into context when present.
    """
    items: list[WorkItem] = []
    for pair in pairs:
        source = pair.get("source")
        target = pair.get("target")
        score = pair.get("score", 0.0)
        if not source or not target:
            continue
        try:
            body_src = DRIVER.read_note(source).content or ""
            body_tgt = DRIVER.read_note(target).content or ""
        except Exception:
            continue  # a pair with an unreadable side cannot be judged: left for the next run

        # The more reliable note is the merge target; the other is the source of
        # new info. Reliability first, length only to break a tie within a tier.
        if merge_rank(body_tgt) >= merge_rank(body_src):
            larger, smaller, smaller_body = target, source, body_src
        else:
            larger, smaller, smaller_body = source, target, body_tgt

        job = DedupJob.between_notes(loser=smaller, winner=larger, loser_body=smaller_body, score=score)
        reason = f"dedup score={score:.3f}"
        if "full_score" in pair and "title_score" in pair:
            job.full_score = pair["full_score"]
            job.title_score = pair["title_score"]
            reason += f" (full={pair['full_score']:.3f} title={pair['title_score']:.3f})"

        items.append(WorkItem(kind="dedup", target_path=larger, context=job, reason=reason))
    return items
