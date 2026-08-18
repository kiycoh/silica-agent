# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""W2 gate — does same-hub tiering change the judge's pair, and does it ever
displace a real duplicate? (survey-provenance spec §10)

Throwaway instrument, NOT product code. Zero-LLM and zero-API: stored vectors,
the cached cluster context, the real `related_notes` facade, and the real
`route_concept` / `prefer_same_hub_candidate` functions — a replica of the
routing logic here could drift from what it measures, so nothing is replicated.

W2 runs INSIDE the defer branch and only reorders which candidate the judge
reads. Two consequences the report states rather than measures:

  * Judge-call count is STRUCTURALLY invariant (the routing decision is taken
    on related[0] before W2 is consulted). Reporting "judge calls unchanged"
    as a finding would be the vacuous-metric trap; the counts are printed as a
    wiring check, not as evidence the lever is safe.
  * Mechanical patch/keep routing is likewise untouched by construction.

So the two questions that decide the gate are:

  1. FIRE RATE — how often does a same-hub candidate actually replace a
     cross-hub top? Near zero means the lever is inert and should be closed
     like the others rather than shipped dark.
  2. DISPLACEMENT — when a note's top candidate IS a title twin (same concept,
     different surface), does W2 ever push that twin out of the judge's slot in
     favour of a same-hub distractor? Every such case is a leaked duplicate:
     the judge would adjudicate the wrong pair, answer "distinct", and the real
     twin is never seen again. This is the duplicate-recall floor, and it must
     be 0.

     The twin signal is `title_key` equality plus the `near_titles` fuzzy band
     — the same machinery validate.py's C3 gate already uses to catch "same
     concept, different surface". probe_dedup's hand-labeled TRUE_DUPS list is
     also resolved and reported, but on a drifted vault it resolves to zero
     pairs and would make the arm silently vacuous, so it is never the floor.

  uv run python -m evals.probe_w2_hub_tiering --vault ~/Documents/Obsidian/test
  uv run python -m evals.probe_w2_hub_tiering --vault <v> --limit 120 --out w2.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _ctx_for(vault: Path) -> dict:
    """The cached Louvain cluster context, exactly what COLLISION reads."""
    import orjson

    from silica.kernel.recall.paths import index_dir_for

    p = index_dir_for(str(vault)) / "clusters_ctx.json"
    if not p.exists():
        return {}
    try:
        return (orjson.loads(p.read_bytes()) or {}).get("ctx") or {}
    except Exception:
        return {}


# --- The REVERTED W2 implementation, kept verbatim ---------------------------
# Production no longer carries these (the gate below closed the lever on
# 2026-08-18). They live here so the verdict stays reproducible: re-running
# this probe against another corpus re-measures the same candidate code.


def _hub_key(ref: str | None) -> str:
    """Normalize a hub reference (name or path, with or without .md) to a
    comparable key: last path segment, extension off, case-folded."""
    if not ref:
        return ""
    return ref.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md").strip().lower()


def prefer_same_hub_candidate(related: list, *, hub, vault_ctx: dict, tau_low: float):
    """Among defer-band candidates, prefer one sharing the incoming concept's
    hub linkage over a purely semantic cross-hub top."""
    top = related[0]
    hk = _hub_key(hub)
    if not hk or not vault_ctx:
        return top

    def _same_hub(c) -> bool:
        if _hub_key(c.path) == hk:
            return True
        return _hub_key(vault_ctx.get(c.path.removesuffix(".md"), {}).get("hub")) == hk

    if _same_hub(top):
        return top
    for c in related[1:]:
        if c.embed_score is None or c.embed_score <= tau_low:
            continue
        if _same_hub(c):
            return c
    return top


def _sample(keys: list[str], limit: int | None) -> list[str]:
    """Deterministic even-stride subsample: spans the whole vault instead of one
    alphabetical corner. Returns everything when `limit` covers the corpus."""
    if not limit or limit >= len(keys):
        return keys
    step = len(keys) / limit
    return [keys[int(i * step)] for i in range(limit)]


def run(vault: Path, *, limit: int | None = None, verbose: bool = False) -> dict:
    from silica.config import CONFIG
    from silica.kernel.link.health import iter_notes
    from silica.kernel.recall.paths import is_inbox_path
    from silica.kernel.recall.relatedness import related_notes
    from silica.router.states.collision import _names_agree, route_concept
    from silica.kernel.text.title import NEAR_BAND, near_titles, title_key
    from evals.golden.probe_dedup import TRUE_DUPS
    from evals.golden.runner import _open_stores

    store, embed_store = _open_stores(vault)
    if embed_store is None or not len(embed_store):
        raise SystemExit("embed index absent for this vault — nothing to measure")

    tau_high = getattr(CONFIG, "sim_threshold_high", 0.85)
    tau_low = getattr(CONFIG, "sim_threshold_low", 0.75)
    ctx = _ctx_for(vault)

    all_keys = sorted(
        p.relative_to(vault).with_suffix("").as_posix() for p in iter_notes(vault)
    )
    all_keys = [
        k for k in all_keys
        if embed_store.get_vec(k) is not None and not is_inbox_path(k)
    ]
    keys = _sample(all_keys, limit)

    # Labeled twins, resolved to store keys by stem (probe_dedup's convention).
    by_stem: dict[str, str] = {}
    for k in store.paths():
        by_stem.setdefault(k.split("/")[-1], k)
    twins: dict[str, set[str]] = {}
    resolved_pairs = 0
    for a, b in TRUE_DUPS:
        ka, kb = by_stem.get(a), by_stem.get(b)
        if not ka or not kb:
            continue
        resolved_pairs += 1
        twins.setdefault(ka.removesuffix(".md"), set()).add(kb.removesuffix(".md"))
        twins.setdefault(kb.removesuffix(".md"), set()).add(ka.removesuffix(".md"))

    def _is_twin(a: str, b: str) -> str:
        """'key' (title_key-equal), 'near' (fuzzy band), or '' — the live
        same-concept-different-surface signal, keyed like the C3 gate."""
        if title_key(a) and title_key(a) == title_key(b):
            return "key"
        hits = near_titles(a, [b], band=NEAR_BAND)
        return "near" if hits else ""

    counts = {"patch": 0, "defer": 0, "keep": 0}
    evaluated = 0
    fired = 0
    no_hub = 0
    fires: list[dict] = []
    twin_slots = 0          # defer pairs whose baseline candidate IS a title twin
    twin_displaced: list[dict] = []
    labeled_twin_slots = 0
    top_already_same_hub = 0   # premise check: is the semantic top already structural?

    for k in keys:
        cands = related_notes(
            k, embed_store=embed_store, cooccur_store=store,
            k=5, exclude={k, k + ".md"},
        )
        cands = [c for c in cands if not is_inbox_path(c.path)]
        if not cands:
            continue
        best = cands[0]
        if best.embed_score is None:
            continue  # co-occurrence-only top: never auto-routed
        evaluated += 1

        is_hub = ctx.get(best.path.removesuffix(".md"), {}).get("is_hub", False)
        decision = route_concept(
            best.embed_score,
            names_agree=_names_agree(k.split("/")[-1], best.name),
            is_hub=is_hub, tau_high=tau_high, tau_low=tau_low,
        )
        counts[decision] += 1
        if decision != "defer":
            continue

        # The incoming concept's own hub linkage is the analogue of fsm.hub.
        hub = ctx.get(k, {}).get("hub") or ""
        if not hub:
            no_hub += 1
        chosen = prefer_same_hub_candidate(
            cands, hub=hub, vault_ctx=ctx, tau_low=tau_low
        )

        hk = _hub_key(hub)
        if hk and (
            _hub_key(best.path) == hk
            or _hub_key(ctx.get(best.path.removesuffix(".md"), {}).get("hub")) == hk
        ):
            top_already_same_hub += 1

        note_title = k.split("/")[-1]
        twin_kind = _is_twin(note_title, best.name)
        if twin_kind:
            twin_slots += 1
        if best.path.removesuffix(".md") in twins.get(k, set()):
            labeled_twin_slots += 1

        if chosen is best:
            continue
        fired += 1
        row = {
            "note": k,
            "hub": hub,
            "baseline": {"path": best.path, "cos": round(best.embed_score, 3),
                         "twin": twin_kind},
            "tiered": {"path": chosen.path, "cos": round(chosen.embed_score, 3),
                       "twin": _is_twin(note_title, chosen.name)},
            "cos_delta": round(chosen.embed_score - best.embed_score, 3),
        }
        fires.append(row)
        # A displacement is harmful only if the promoted pair is NOT itself a
        # twin: swapping one twin for another still puts a duplicate in front
        # of the judge.
        if twin_kind and not row["tiered"]["twin"]:
            twin_displaced.append(row)

    defer_n = counts["defer"]
    out = {
        "notes_in_vault": len(all_keys),
        "notes_sampled": len(keys),
        "sampled_fraction": round(len(keys) / len(all_keys), 4) if all_keys else 0.0,
        "pairs_evaluated": evaluated,
        "route_patch": counts["patch"],
        "route_defer": defer_n,
        "route_keep": counts["keep"],
        "judge_calls_baseline": defer_n,
        "judge_calls_w2": defer_n,  # structural, see module docstring
        "defer_top_already_same_hub": top_already_same_hub,
        "premise_rate": (
            round(top_already_same_hub / defer_n, 4) if defer_n else 0.0
        ),
        "retier_fired": fired,
        "retier_rate": round(fired / defer_n, 4) if defer_n else 0.0,
        "defer_without_hub": no_hub,
        "labeled_pairs_resolved": resolved_pairs,
        "labeled_twin_in_judge_slot": labeled_twin_slots,
        "twin_in_judge_slot": twin_slots,
        "twin_displaced": len(twin_displaced),
        "twin_displacement_rate": (
            round(len(twin_displaced) / twin_slots, 4) if twin_slots else 0.0
        ),
        "mean_cos_delta_on_fire": (
            round(sum(f["cos_delta"] for f in fires) / fired, 4) if fired else 0.0
        ),
        "examples": fires[:15],
        "displacements": twin_displaced,
    }

    if verbose:
        print(f"\nsample: {len(keys)}/{len(all_keys)} notes "
              f"({out['sampled_fraction']:.0%} of the frozen corpus)")
        print(f"routing: patch={counts['patch']} defer={defer_n} keep={counts['keep']}")
        print(f"premise: top candidate ALREADY same-hub on "
              f"{top_already_same_hub}/{defer_n} defer pairs "
              f"({out['premise_rate']:.1%}) -- nothing for W2 to re-tier there")
        print(f"W2 fired on {fired}/{defer_n} defer pairs "
              f"(rate={out['retier_rate']:.4f}), mean cos delta "
              f"{out['mean_cos_delta_on_fire']:+.4f}")
        print(f"title twins in judge slot={twin_slots} "
              f"(hand labels resolved={resolved_pairs}, in slot={labeled_twin_slots})")
        print(f"TWINS DISPLACED BY W2 = {len(twin_displaced)} "
              f"(rate={out['twin_displacement_rate']:.4f}) -- gate requires 0")
        for f in fires[:15]:
            print(f"   {f['note']}\n      was {f['baseline']['path']} "
                  f"cos={f['baseline']['cos']} twin={f['baseline']['twin'] or '-'}"
                  f"\n      now {f['tiered']['path']} "
                  f"cos={f['tiered']['cos']} twin={f['tiered']['twin'] or '-'} "
                  f"({f['cos_delta']:+.3f})")
        for d in twin_displaced:
            print(f"   !! DISPLACED TWIN {d}")
    return out


def main() -> int:
    from evals.golden.runner import resolve_vault, vault_digest

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault")
    ap.add_argument("--limit", type=int, default=None,
                    help="even-stride subsample size (default: whole vault)")
    ap.add_argument("--out")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    vault = resolve_vault(args.vault)
    digest, notes = vault_digest(vault)
    rep = run(vault, limit=args.limit, verbose=not args.quiet)
    doc = {
        "probe": "w2_hub_tiering",
        "corpus": {"path": str(vault), "digest": digest, "notes": notes},
        "report": rep,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
