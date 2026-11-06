# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault's numbers: the metrics view, the shape of the map, the usage limits and the brief."""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter

from silica.config import CONFIG
from silica.kernel.report import brief, vault_shape
from silica.kernel.text import note_parts
from silica.ui.web.failures import explain as _failure

logger = logging.getLogger(__name__)

router = APIRouter()


# How many rows of an uncapped list the metrics view receives. The report caps
# its own ranked lists at top_k; orphans and dangling are exhaustive, so they get
# cut here — and the true length always rides along in `totals`, so a cut list
# can never read as "this is all of them".
#
# 12, not 60: at 60 the orphans and dangling cards ran to 60 rows each and the
# dashboard became two long lists with charts above them (8.5k px on a 686-note
# vault). A card samples; GRAPH_REPORT.md is where the full list lives.
_METRICS_ROWS = 12


# How many stored readings the trend band draws. The series gains a line only
# when a count actually moves, so 60 is years of daily use on a vault that keeps
# changing -- and past that a sparkline 80px wide is drawing more points than it
# has pixels, which is a smear, not a trend.
_HISTORY_POINTS = 60


def _metrics_history(vault: str | None) -> list[dict]:
    """The stored readings the trend band draws, oldest last.

    Best-effort like the delta beside it: a vault that is read-only or unbound
    still gets its metrics, and losing the trend costs a band, not the view.
    """
    if not vault:
        return []
    try:
        from silica.kernel.report.history import read_history

        return read_history(vault)[-_HISTORY_POINTS:]
    except Exception as exc:
        logger.debug("metrics: history unreadable (%s)", exc)
        return []


def _lean_limit() -> int:
    """The character count under which a note is called lean.

    Read from the linter rather than restated here: the pane prints it beside
    every row, and a copy would keep saying 600 for a year after the limit moved.
    """
    try:
        from silica.kernel.write.ofm import LIMITS

        return int(LIMITS["lean_chars"])
    except Exception as exc:
        logger.debug("metrics: lean limit unreadable (%s)", exc)
        return 0  # the pane drops the comparison rather than inventing a bound


@router.get("/vault_version")
def vault_version():
    """A digest the explore surfaces poll to learn the vault moved under them.

    Deliberately NOT the BUS/SSE the turn stream uses: that bus is in-process,
    so it carries this browser's own agent and nothing else — and the writes
    this exists for are the ones from OUTSIDE it (Obsidian, `silica nucleate`
    in a terminal, a second window). A poll is the only signal that sees them
    without a resident watcher, which the charter rules out anyway.
    """
    from silica.kernel.recall.sync import vault_version as _version

    return {"version": _version()}


@router.get("/shape")
def shape():
    """Three read-only views over ONE graph build: containment, area coupling,
    and a derived reading order.

    They share an endpoint because they share the expensive part — build_graph_data
    plus community detection — and splitting them into three routes would pay it
    three times for surfaces a reader flips between.
    """
    from silica.kernel.graph.graph_export import build_graph_data, detect_communities

    try:
        nodes, edges = build_graph_data(folder="")
        # Assigns node["group"] in place, which group_of reads back below; the
        # return is read only for each area's colour, which Areas paints its
        # discs with so a disc and the graph's dots agree (ADR-0060).
        communities = detect_communities(nodes, edges)
    except Exception as exc:
        logger.warning("shape: graph build failed (%s)", exc)
        return _failure(exc)
    real = [n for n in nodes if n.get("type") != "ghost"]
    group_of = {n["id"]: n.get("group", -1) for n in real}
    label_of = {n["id"]: n.get("label") or n["id"].rsplit("/", 1)[-1] for n in real}

    # One tally with the report and the gap/bridge ranking (area_tally): linked
    # pairs counted once, hubs by degree with ties to the larger id. This loop
    # used to live here a third time and picked hubs by file order.
    from silica.kernel.graph.graph_export import area_hubs, area_tally

    t = area_tally(real, edges)
    sizes, intra, inter, deg, hub_of = dict(t.sizes), t.intra, t.inter, t.deg, area_hubs(t)
    adj: dict[str, set[str]] = {}
    for a, b in t.pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    # Multi-note communities only, biggest first. A singleton is its own
    # community: on the matrix it is a row and column of zeroes with a perfect
    # diagonal, which is 65 rows of noise around the 26 that carry the vault.
    ids = sorted((g for g, s in sizes.items() if s > 1), key=lambda g: (-sizes[g], g))

    def cohesion(g: int) -> float:
        s = sizes[g]
        possible = s * (s - 1) / 2
        return round(intra.get(g, 0) / possible, 4) if possible else 0.0

    colour = {c.id: (c.color, c.color_paper) for c in communities}
    areas = [
        {"id": g, "label": label_of.get(hub_of.get(g, ""), f"#{g}"), "path": hub_of.get(g, ""),
         "size": sizes[g], "cohesion": cohesion(g), "intra": intra.get(g, 0),
         "color": colour.get(g, ("", ""))[0], "color_paper": colour.get(g, ("", ""))[1]}
        for g in ids
    ]
    matrix = [[(intra.get(a, 0) if a == b else inter.get((min(a, b), max(a, b)), 0))
               for b in ids] for a in ids]

    return {
        "areas": areas,
        "matrix": matrix,
        # Every real note, for the containment view. Three fields, not the whole
        # node: the treemap needs a path, an area and a weight, and shipping the
        # colour/font/title the canvas uses would triple the payload for nothing.
        "notes": [{"path": n["id"], "size": n.get("size") or 1, "area": group_of.get(n["id"], -1)}
                  for n in real],
        "reading": vault_shape.shape_reading(adj, deg, areas, label_of),
        "totals": {"notes": len(real), "areas": len(ids),
                   "singletons": sum(1 for s in sizes.values() if s <= 1)},
    }


def _report_head(report, payload: dict, elapsed_s: float, started) -> dict:
    """The run head of the Report panel, plus the reading it can only get from a store.

    Everything else the metrics view shows is derived from THIS report. The one
    thing it cannot derive is what moved, so this is where the report is filed
    and the last different one is handed back.

    Best-effort by construction: a vault that is read-only, unbound or on a full
    disk still gets its metrics. Losing the delta costs a section of one panel;
    failing the call costs the whole view.
    """
    import datetime as _dt

    from silica.kernel.report.history import record_report, signals_of

    areas = len([c for c in report.clusters if c.size > 1])
    # compute_report memoises per vault epoch, so a second open of the tab costs
    # microseconds and "0.0s" in the run head would read as "the audit is free"
    # rather than "you are looking at the one from two minutes ago". The duration
    # is stated only when this call actually computed it; when it did not, the
    # head's own timestamp is the honest answer and the panel drops the figure.
    #
    # Compared against the instant before the call rather than against a
    # tolerance: `generated_at` is stamped inside compute_report, so a fresh
    # report's is strictly after `started` and a memo hit's is strictly before.
    # No threshold to tune and no wrong answer on a fast vault.
    fresh = True
    try:
        made = _dt.datetime.fromisoformat(report.generated_at)
        fresh = made >= started
    except (TypeError, ValueError):
        pass  # an unparseable stamp is not a reason to drop the duration
    head = {
        "at": report.generated_at,
        "elapsed_s": round(elapsed_s, 2) if fresh else None,
        "notes": (report.totals or {}).get("notes", 0),
        "depth": payload["depth"],
        "signals": {},
        "previous": None,
        "since": None,
    }
    try:
        signals = signals_of(report.totals or {}, areas)
        head["signals"] = signals
        if CONFIG.vault_path:
            prev = record_report(CONFIG.vault_path, signals)
            if prev:
                head["previous"] = prev.get("signals")
                head["since"] = prev.get("at")
    except Exception as exc:
        logger.debug("metrics: report history skipped (%s)", exc)
    return head


@router.get("/metrics")
def metrics(proposals: bool = False):
    """Everything the L1 graph report measures, as JSON for the metrics tab.

    Two depths, because the co-occurrence leg costs an order of magnitude more
    than the rest and, unlike the rest, grows with the square of the vault
    (_compute_cooccur_delta ranks every note against every other):

      default          — analytics + embeddings (~2s on a 686-note vault).
                         `depth: "structural"`.
      ?proposals=1     — adds the co-occurrence delta (autolink candidates,
                         stale links, missing hubs, integration deficits).
                         ~7s on the same vault. `depth: "full"`.

    The depth rides in the payload because E(vault) is only comparable across
    reports built at the same depth (see vault_energy): its `deficits` term is
    zero without the co-occurrence leg, and on a real vault that term dominates.
    The client labels the number rather than letting two different E's look alike.
    """
    import datetime as _datetime
    from collections import Counter

    from silica.kernel.report.graph_report import compute_report
    from silica.kernel.report.vault_energy import vault_energy

    t0, started = time.perf_counter(), _datetime.datetime.now(_datetime.timezone.utc)
    try:
        report = compute_report(
            analytics=True, with_embeddings=True, with_cooccurrence=proposals, top_k=20,
        )
    except Exception as exc:
        logger.warning("metrics: report failed (%s)", exc)
        return _failure(exc)
    elapsed_s = time.perf_counter() - t0

    e = vault_energy(report)
    short = lambda nid: note_parts.clean_name(nid or "")  # noqa: E731  one name with /shape: no folder, no .md
    # An area is named by its hub note's *name*: the full store path is a folder
    # tree, and in a table cell it wraps to three lines and says nothing extra.
    label = {c.cluster_id: (short(c.hub) or f"#{c.cluster_id}") for c in report.clusters}
    size = {c.cluster_id: c.size for c in report.clusters}

    payload = {
        "path": CONFIG.vault_path or "",
        "generated_at": report.generated_at,
        "depth": "full" if proposals else "structural",
        "totals": report.totals,
        "discourse_state": report.discourse_state,
        "energy": {
            "total": round(e.total, 2),
            # Ordered as E is composed: the one negative (bond-forming) term
            # first, then the entropic costs. `deficits` is dropped rather than
            # printed as 0.00 when the leg that measures it never ran — a zero
            # would read as "measured, came out flat". It contributes 0.0 either
            # way, so the terms still sum to `total`.
            "terms": [
                {"name": "cohesion", "value": round(e.cohesion, 2)},
                {"name": "orphans", "value": round(e.orphans, 2)},
                {"name": "dangling", "value": round(e.dangling, 2)},
                {"name": "gaps", "value": round(e.gaps, 2)},
                *([{"name": "deficits", "value": round(e.deficits, 2)}] if proposals else []),
                {"name": "contested", "value": round(e.contested, 2)},
            ],
        },
        "degree_histogram": vault_shape.degree_histogram(report.degree_map),
        "clusters": [
            {"id": c.cluster_id, "size": c.size, "hub": short(c.hub), "path": c.hub,
             "cohesion": c.cohesion}
            for c in sorted(report.clusters, key=lambda c: -c.size)
        ],
        "hubs": [
            {"label": n.label, "path": n.id, "area": label.get(n.cluster, f"#{n.cluster}"),
             "degree": n.degree, "in": n.in_degree, "out": n.out_degree,
             "betweenness": n.betweenness}
            for n in report.god_nodes
        ],
        "bridges": [
            {"source": short(b.source), "target": short(b.target),
             "source_path": b.source, "target_path": b.target,
             "links_between": b.links_between, "joined": b.joined}
            for b in report.bridges
        ],
        # The two area sizes ride along because they *are* the ranking:
        # gap_score = size_a * size_b / (1 + inter_edges). gap_density is left
        # out — on a real vault it reads 99.7-100% on every row, and a column
        # that never varies cannot explain the order it is sitting in.
        "gaps": [
            {"a": short(g.hub_a), "b": short(g.hub_b), "a_path": g.hub_a, "b_path": g.hub_b,
             "inter_edges": g.inter_edges, "size_a": size.get(g.cluster_a, 0),
             "size_b": size.get(g.cluster_b, 0)}
            for g in report.structural_gaps
        ],
        "orphans": [{"label": short(p), "path": p} for p in report.orphans[:_METRICS_ROWS]],
        # A target is a row you act on, so it carries what it needs to be judged:
        # who asks for it. Three names and a count, because at 372px of evidence
        # pane the fourth name is what starts wrapping.
        "dangling": [
            {"target": d["target"], "refs": d["refs"],
             "from": [short(src) for src in d.get("sources", [])[:3]],
             "from_more": max(0, len(d.get("sources", [])) - 3)}
            for d in report.dangling[:_METRICS_ROWS]
        ],
        # The tail, over EVERY target and not the twelve above. The shape IS the
        # reading -- a handful of targets carry most of the references and the
        # rest are asked for once each, which is the difference between "write
        # twenty notes" and "stub four hundred" -- and it cannot be seen from a
        # top-12 slice, so it is summarised here rather than shipped as rows.
        "dangling_hist": [
            {"refs": refs, "targets": n}
            for refs, n in sorted(Counter(d["refs"] for d in report.dangling).items())
        ],
        "dangling_top_refs": sum(d["refs"] for d in report.dangling[:20]),
        "contested": [
            {"label": short(c.path), "path": c.path, "refs": c.refs} for c in report.contested
        ],
        "source_drift": [
            {"label": short(d.note), "path": d.note, "source": d.source}
            for d in report.source_drift[:_METRICS_ROWS]
        ],
        "attention": [
            {"label": short(a.path), "path": a.path, "days_idle": a.days_idle,
             "degree": a.degree, "misses": a.misses, "attempts": a.attempts,
             "score": round(a.score, 2)}
            for a in report.attention_candidates
        ],
        "deficits": [
            {"label": short(d.path), "path": d.path, "concepts": d.concepts,
             "degree": d.degree, "score": round(d.score, 2)}
            for d in report.integration_deficits
        ],
        # Confirmed first — those are the merge candidates; the borderline band
        # is only "link, don't merge". Neither list is capped by the report, and
        # on a real vault they run to the hundreds, so the slice happens here.
        "duplicates": ([
            {"a": short(d.source), "b": short(d.target), "a_path": d.source,
             "b_path": d.target, "score": d.score, "confirmed": True}
            for d in report.confirmed_duplicate_pairs
        ] + [
            {"a": short(d.source), "b": short(d.target), "a_path": d.source,
             "b_path": d.target, "score": d.score, "confirmed": False}
            for d in report.duplicate_pairs
        ])[:_METRICS_ROWS],
        # Sliced like every other uncapped list: the report caps the
        # co-occurrence leg at top_k, but the import-derived candidates
        # _compute_code_signals appends are exhaustive — 13k pairs on a
        # 400-note vault, which is a 4 MB payload and a card 390,000 px tall.
        # The true count rides in `totals`, so the cut list can't read as all.
        "autolinks": [
            {"a": short(a.source), "b": short(a.target), "a_path": a.source,
             "b_path": a.target, "weight": a.weight, "shared": a.shared[:4]}
            for a in report.autolink_candidates[:_METRICS_ROWS]
        ],
        "stale_links": [
            {"a": short(s.source), "b": short(s.target), "a_path": s.source, "b_path": s.target}
            for s in report.stale_links
        ],
        "missing_hubs": [
            {"concept": h.concept, "centrality": round(h.centrality, 3)}
            for h in report.missing_hubs
        ],
        # `chars` and the limit it is under, together: a bare "412" is a number
        # the reader has to be told the meaning of, and the pane cannot say
        # "under 600" for a row without knowing what 600 is.
        "lean_notes": [
            {"label": short(p), "path": p, "chars": report.lean_chars.get(p, 0)}
            for p in report.lean_notes[:_METRICS_ROWS]
        ],
        "lean_limit": _lean_limit(),
        # V6 (spec 2026-08-22) rides the co-occurrence depth, so it is absent
        # at structural depth rather than shipped empty: an empty list would
        # read as "measured, found nothing".
        "bursting": [
            {"concept": b.concept, "z": round(b.z, 2), "recent": b.recent, "total": b.total}
            for b in report.bursting_concepts
        ],
        "temporal": (
            {
                "notes_scanned": report.temporal.notes_scanned,
                "by_tier": {str(k): v for k, v in report.temporal.by_tier.items()},
                "stamped": report.temporal.stamped,
                "superseded_notes": report.temporal.superseded_notes,
                "superseded_sections": report.temporal.superseded_sections,
                "oldest_valid_from": report.temporal.oldest_valid_from,
            }
            if report.temporal and report.temporal.notes_scanned
            else None
        ),
        "sessions": vault_shape.write_sessions(report),
        # The three readings the charts need that no single report can hold.
        # history is the only one that outlives a run: it is what turns four
        # tiles from a state into a direction.
        "history": _metrics_history(CONFIG.vault_path),
        "area_matrix": vault_shape.area_matrix(report),
        "signal_areas": vault_shape.signal_areas(report),
        "code_coverage": (
            {
                "documented": report.code_coverage.documented,
                "total": report.code_coverage.total,
                "undocumented": [
                    {"path": p, "fan_in": f}
                    for p, f in report.code_coverage.undocumented[:_METRICS_ROWS]
                ],
            }
            if report.code_coverage and report.code_coverage.total
            else None
        ),
    }
    payload["report"] = _report_head(report, payload, elapsed_s, started)
    return payload


@router.get("/limits")
def limits():
    """How much of each usage limit is used and when it resets (ADR-0102
    point 7): `{"limits": [{layer, used, resets_at}]}`, the window first, then
    the week and the month, `used` a share from 0 to 1 and never a currency. An
    empty list off hosted, where the key has no limit or the model is not on
    OpenRouter.
    Sync, so FastAPI runs the read of the key on its thread pool."""
    from silica.model.usage_limits import status

    return {"limits": status(CONFIG.model or "")}


@router.get("/vault_brief")
def vault_brief(refresh: int = 0):
    """One sentence about what the vault holds, written by the worker model.

    `stamp` is the corpus shape the sentence was written against, so a vault
    that grew gets a new sentence and a vault that only sat there replays the
    one on disk. A failure here is never an error: the agent describes a vault
    without it, from its name and its coverage.
    """
    if not CONFIG.vault_brief:
        return {"enabled": False, "text": ""}

    from silica.kernel.graph.graph_export import build_graph_data, detect_communities

    try:
        nodes, edges = build_graph_data(folder="")
        communities = detect_communities(nodes, edges)
    except Exception as exc:
        return {"enabled": True, "text": "", **_failure(exc)}

    notes = sum(1 for n in nodes if n.get("type") != "ghost")
    topics = [c.label for c in communities if c.size > 1][:8]
    stamp = f"{notes}|" + "|".join(topics)

    cache = brief.brief_path()
    if not refresh:
        try:
            got = json.loads(cache.read_text(encoding="utf-8"))
            if got.get("stamp") == stamp and got.get("text"):
                return {"enabled": True, "text": got["text"], "cached": True}
        except Exception:
            pass  # no cache, unreadable cache, or a stamp from an older shape

    hubs = [h["name"] for h in brief.top_hubs(nodes, edges, top_n=12)]
    text = brief.write_brief(notes, topics, hubs)
    if not text:
        return {"enabled": True, "text": ""}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "text": text}), encoding="utf-8")
    except Exception:
        pass  # an uncacheable brief is still a usable brief
    return {"enabled": True, "text": text}
