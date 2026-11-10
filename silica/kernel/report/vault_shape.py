# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Read models over a vault report: what the metrics view and the map draw, shaped here so a
route only ships them."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from silica.config import CONFIG
from silica.kernel.text import note_parts


# Degree-distribution buckets. Doubling widths, not equal ones: a wikilink graph
# is heavy-tailed, so linear bins put ~everything in the first two and stretch a
# hundred empty bins under the hubs. The first three degrees stay their own bin
# because 0 (isolated), 1 (a leaf) and 2 mean different things about a note.
_DEGREE_BINS = ((0, 0), (1, 1), (2, 2), (3, 4), (5, 8), (9, 16), (17, 32), (33, 64), (65, None))


def degree_histogram(degree_map: dict[str, int]) -> list[dict]:
    """Bucket every note's resolved-link degree. Trailing empty buckets are
    dropped so the axis ends where the vault does; interior empties stay, since
    a hole in the middle of the distribution is itself the reading."""
    out = []
    for lo, hi in _DEGREE_BINS:
        n = sum(1 for d in degree_map.values() if d >= lo and (hi is None or d <= hi))
        label = str(lo) if hi == lo else (f"{lo}+" if hi is None else f"{lo}-{hi}")
        out.append({"label": label, "count": n, "lo": lo})
    while len(out) > 1 and out[-1]["count"] == 0:
        out.pop()
    return out


def area_of(report) -> dict[str, int]:
    """Note id -> area id, indexed under both keyspaces the callers arrive in.

    `clusters[].members` are graph ids, which carry `.md`; the store keyspace
    drops it, and store-derived rows arrive in that spelling. Indexing both
    spellings once is cheaper than making every caller remember which one it
    holds, and a lookup that silently missed would not raise, it would report
    an area as clean.
    """
    out: dict[str, int] = {}
    for c in report.clusters:
        if c.size <= 1:
            continue  # a singleton is its own area; see inter_cluster's cut
        for m in c.members:
            out[m] = c.cluster_id
            if m.endswith(".md"):
                out[m[:-3]] = c.cluster_id
            else:
                out[m + ".md"] = c.cluster_id
    return out


def signal_areas(report) -> dict[str, dict[str, int]]:
    """How many notes each worklist signal puts in each area.

    Counted over the report's FULL lists, never the twelve rows `/metrics` ships:
    a lens coloured from the slice would paint the top-12's areas hot and every
    other area clean, which is a confident statement about the wrong population.
    That is the same trap `dangling_hist` exists to avoid for the tail reading.

    `dangling` and `gaps` are absent by construction and not by omission: a
    dangling target has no note to place, and a gap is already a fact about a
    PAIR of areas, so tallying either into one area would invent a location.
    """
    area = area_of(report)
    lists = {
        "lean": report.lean_notes,
        "orphans": report.orphans,
        "attention": [a.path for a in report.attention_candidates],
        "deficits": [d.path for d in report.integration_deficits],
        "contested": [c.path for c in report.contested],
        "drift": [d.note for d in report.source_drift],
    }
    out: dict[str, dict[str, int]] = {}
    for key, paths in lists.items():
        tally: dict[str, int] = {}
        for p in paths:
            cid = area.get(p)
            if cid is None:
                continue  # a note outside every multi-note area has no column
            tally[str(cid)] = tally.get(str(cid), 0) + 1
        if tally:
            out[key] = tally
    return out


def area_matrix(report) -> dict | None:
    """The area x area coupling grid, in the shape `/shape` already ships.

    Same payload shape on purpose: one client renderer draws both, so the two
    surfaces cannot drift into disagreeing about what a coupling is. The
    diagonal carries intra-area linked pairs, which is cohesion's numerator, and
    the renderer prints cohesion there instead.

    None rather than an empty grid below two areas: a 1x1 matrix is a cell
    saying nothing, and None is what lets the card say so in words.
    """
    if not report.inter_cluster:
        return None  # analytics did not run; see VaultReport.inter_cluster
    areas = [c for c in report.clusters if c.size > 1]
    if len(areas) < 2:
        return None
    areas.sort(key=lambda c: (-c.size, c.cluster_id))
    ids = [c.cluster_id for c in areas]
    short = lambda nid: note_parts.clean_name(nid or "")  # noqa: E731  one name with /shape: no folder, no .md
    cell = report.inter_cluster
    at = lambda a, b: cell.get(f"{min(a, b)}|{max(a, b)}", 0)  # noqa: E731
    # The colours too, though the gaps card paints none: the row is /shape's
    # row, and an area's colour is a function of its id in both producers
    # (detect_communities), so shipping it keeps the two payloads one shape.
    from silica.kernel.graph.palette import community_color

    return {
        "areas": [
            {"id": c.cluster_id, "label": short(c.hub) or f"#{c.cluster_id}",
             "path": c.hub or "", "size": c.size, "cohesion": c.cohesion,
             "intra": at(c.cluster_id, c.cluster_id),
             "color": community_color(c.cluster_id),
             "color_paper": community_color(c.cluster_id, on_paper=True)}
            for c in areas
        ],
        "matrix": [[at(a, b) for b in ids] for a in ids],
    }


def shape_reading(adj: dict, deg: dict, areas: list[dict], label_of: dict, stops: int = 24) -> dict[str, Any]:
    """A reading order over the vault, derived rather than authored.

    Areas biggest first, and inside one the hub then its best-connected
    neighbours, breadth-first. Not a ranking of what matters: it is the order
    that keeps each next note adjacent to something already read, which is the
    only property a reading path can actually promise from link structure.

    Capped, because a path with 795 stops is the file tree with extra words.
    """
    out: list[dict] = []
    seen: set[str] = set()
    # Every area's hub, reserved. Hubs link to each other, so without this the
    # second area's hub gets picked up as a neighbour of the first, and then its
    # own section is skipped as already-seen — silently dropping the area, and
    # the biggest ones first, since those are exactly the well-connected hubs.
    hubs = {a["path"] for a in areas if a["path"]}
    covered = 0
    for a in areas:
        hub = a["path"]
        if not hub:
            continue
        if len(out) + 1 > stops:
            break
        covered += 1
        seen.add(hub)
        out.append({"path": hub, "label": label_of.get(hub, hub), "area": a["label"],
                    "why": f"hub of {a['label']} — {a['size']} notes, the densest point of the area"})
        # Two neighbours per area: enough to show what the hub opens onto,
        # few enough that the biggest area cannot eat the whole path.
        near = sorted((n for n in adj.get(hub, ()) if n not in seen and n not in hubs),
                      key=lambda n: -deg.get(n, 0))[:2]
        for n in near:
            if len(out) >= stops:
                break
            seen.add(n)
            out.append({"path": n, "label": label_of.get(n, n), "area": a["label"],
                        "why": f"linked from the hub, {deg.get(n, 0)} links of its own"})
    return {"stops": out, "areas_covered": covered, "areas_total": len(areas)}


def write_sessions(report) -> dict | None:
    """The days claims were written, crossed against the areas that received them.

    Not a chronology of the vault: the vault's clock is per-claim (`valid_from`
    stamps), and only a nucleated note carries one. So this measures what WROTE
    the vault, not when the vault's subjects happened.

    A session x area matrix, not a time axis. Measured on a real vault the dates
    collapse onto 9 days inside a 2-month window with one straggler two years
    back; on a linear date axis that straggler takes 90% of the width and the
    nine days that hold 99% of the work land in a smear. The matrix drops the
    duration and keeps what varies -- which areas recur across sessions.

    Areas are the multi-note communities only. A singleton is its own community,
    so counting them would make every session look perfectly focused by
    construction. Areas never written into are counted rather than listed: the
    reading here is coverage, and 19 empty columns bury the 7 carrying the work.

    A stem that resolves to more than one note (the vault has forked pairs
    sharing a subpath under `write_dir`) is attributed to no area and counted as
    ambiguous, because guessing one of the two would silently move a mark into
    the wrong column.
    """
    from silica.kernel.calendar.timeline import timeline

    vault = Path(CONFIG.vault_path or "").expanduser()
    if not vault.is_dir():
        return None
    rows = timeline(vault, limit=10**6)["rows"]
    if not rows:
        return None

    areas = [c for c in report.clusters if c.size > 1]
    # Stem -> the areas claiming it. A set, so a genuine fork shows up as >1
    # rather than as whichever member the iteration happened to reach last.
    by_stem: dict[str, set[int]] = {}
    for c in areas:
        for m in c.members:
            by_stem.setdefault(m.rsplit("/", 1)[-1].removesuffix(".md"), set()).add(c.cluster_id)
    hub_path = {c.cluster_id: (c.hub or "") for c in areas}

    days: dict[str, dict[str, int]] = {}
    touched: dict[int, int] = {}
    ambiguous = unplaced = 0
    for date, _label, stem in rows:
        hit = by_stem.get(stem)
        if not hit:
            unplaced += 1
            continue
        if len(hit) > 1:
            ambiguous += 1
            continue
        cid = next(iter(hit))
        cells = days.setdefault(date, {})
        cells[str(cid)] = cells.get(str(cid), 0) + 1
        touched[cid] = touched.get(cid, 0) + 1

    if not days:
        return None
    # Busiest area first: the columns are read left to right, and the areas the
    # writing actually lands in are the ones worth seeing without scrolling.
    ordered = sorted(touched.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "areas": [
            {"id": str(cid), "label": (hub_path.get(cid, "") or f"#{cid}").rsplit("/", 1)[-1]
             .removesuffix(".md"), "path": hub_path.get(cid, ""), "total": n}
            for cid, n in ordered
        ],
        "days": [
            {"date": d, "notes": sum(cells.values()), "cells": cells}
            for d, cells in sorted(days.items())
        ],
        "areas_total": len(areas),
        "untouched": len(areas) - len(touched),
        # The notes with no claim clock at all. Named, never dropped: on a vault
        # written mostly by hand this is the overwhelming majority, and a matrix
        # that omits it reads as "the whole vault, over 9 days".
        "undated": max((report.totals or {}).get("notes", 0) - len(rows), 0),
        "ambiguous": ambiguous,
        "unplaced": unplaced,
    }
