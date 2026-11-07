# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the graph says beyond the links a person wrote: the proposal layers, and the readings
stamped on each node.

Deterministic reports over the graph data, so they sit with it and any surface can ask. The
frame that draws them is `silica.tools.graph_view`; none of this knows it exists, except the
hues, which ride on each edge because the frame paints what it is handed.
"""
from __future__ import annotations

import logging
from pathlib import Path

from silica.kernel.base.noteshape import NATIVE_TYPES

logger = logging.getLogger(__name__)

_EDGE_COLOR_GAP = "#E0A93B"  # --warn — "a bridge could go here, and doesn't"
_EDGE_COLOR_GAP_PAPER = "#7A4F03"              # --warn, on paper

# Two more proposal layers, and one reading. All three are off by default: the
# frame opens on what you WROTE, and every layer that is a claim has to be asked
# for. Amber was not reused for the note-level proposals even though a gap and a
# structural link make the same kind of claim, because the legend is read by
# swatch before it is read by word, and two amber rows a line apart say "these
# are the same thing at two widths" - which is exactly the mistake a reader
# would then make about the 5 area bridges and the 12 note pairs.
#
# V1 (structural): green, the one hue the frame was not using. "The shape of
# what you already wrote predicts this link."
_EDGE_COLOR_PROPOSED = "#3FBF8F"
_EDGE_COLOR_PROPOSED_PAPER = "#0E6B4C"
# V3 (coupled): violet. A different KIND of claim - provenance, not topology -
# so it gets a hue rather than a weight.
_EDGE_COLOR_COUPLED = "#B06BD9"
_EDGE_COLOR_COUPLED_PAPER = "#5B2E86"


def gap_edges(nodes: list[dict], edges: list[dict], top_k: int = 5) -> list[dict]:
    """Top structural gaps as overlay edges between two area hubs.

    Reads: 'these two well-formed areas should probably connect, and don't.'
    Reuses graph_export.structural_gaps so the overlay agrees with the /graph
    report's Structural Gaps section node-for-node. Only the keys 3d-force-graph
    actually honours: from/to (linkSource/linkTarget), color.color (linkColor),
    and type (visibility toggle + particle accessor). The lib draws these as
    amber directional-particle links — WebGL has no dashed line, so motion, not
    a dash pattern, is what sets a gap apart. score rides along for the title map.
    """
    from silica.kernel.graph.graph_export import structural_gaps

    return [
        {
            "id":    f"gap{i}",
            "from":  hub_a,
            "to":    hub_b,
            "type":  "GAP",
            # Five of them against four thousand others: a gap can afford to be
            # the most opaque and the widest thing on screen, because there is
            # never enough of it to crowd anything.
            "color": {"color": _EDGE_COLOR_GAP,
                      "paper": _EDGE_COLOR_GAP_PAPER, "opacity": 0.95},
            "width": 2.0,
            "score": score,
        }
        for i, (ca, cb, hub_a, hub_b, ie, score, _dens) in enumerate(
            structural_gaps(nodes, edges, top_k=top_k)
        )
    ]


def proposed_edges(G_und, nodes: list[dict], top_k: int = 12) -> list[dict]:
    """Top Adamic-Adar pairs (V1) as overlay edges: "these two share
    neighbours and do not link".

    Corroborated only, which is the whole reason the layer is honest: a pair
    also has to share at least one CONCEPT in the co-occurrence index. As a
    fusion leg V1 was killed (0 wins against 417 losses on the golden pairs,
    ADR-0027); corroborated, it is the same rule the curator accepts an
    autolink under, so the layer and the curator cannot disagree about what
    is worth proposing.
    """
    from silica.kernel.graph.signals import structural_links

    ranked = structural_links(G_und, top_k=top_k * 4)
    return _corroborate(
        [(u, v, sc) for u, v, sc, _cm in ranked], nodes, top_k,
        kind="PROPOSED", color=_EDGE_COLOR_PROPOSED, paper=_EDGE_COLOR_PROPOSED_PAPER,
    )


def coupled_edges(G_und, nodes: list[dict], top_k: int = 12) -> list[dict]:
    """Top coupled pairs (V3) as overlay edges: "written from the same source,
    or in the same run, and never linked".

    Same corroboration rule and the same assembly of transactions the report
    uses, so the layer cannot claim a pair the report does not carry.
    """
    from silica.config import CONFIG
    from silica.kernel.graph.signals import coupling_adjacency
    from silica.kernel.report.cowrite import coupling_transactions

    ids = {n["id"] for n in nodes if n.get("type") != "ghost"}
    transactions, _dropped = coupling_transactions(
        str(CONFIG.vault_path or ""), _sources_of(nodes), ids,
    )
    adj = coupling_adjacency(transactions)
    pairs: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str]] = set()
    for a, row in adj.items():
        for b, w in row.items():
            key = (min(a, b), max(a, b))
            if key in seen or a not in ids or b not in ids or G_und.has_edge(a, b):
                continue
            seen.add(key)
            pairs.append((key[0], key[1], w))
    pairs.sort(key=lambda r: (-r[2], r[0], r[1]))
    return _corroborate(
        pairs[:top_k * 4], nodes, top_k,
        kind="COUPLED", color=_EDGE_COLOR_COUPLED, paper=_EDGE_COLOR_COUPLED_PAPER,
    )


def _sources_of(nodes: list[dict]) -> dict[str, list[str]]:
    """{node id: frontmatter `sources:`}, read straight off the files.

    compute_report harvests this during its body scan; the export has no such
    scan, and a 709-note frontmatter read measured 0.01 s, so it does its own
    rather than dragging the report in for one field.
    """
    import re

    from silica.config import CONFIG

    root = Path(str(CONFIG.vault_path or ""))
    if not root.is_dir():
        return {}
    out: dict[str, list[str]] = {}
    for n in nodes:
        if n.get("type") == "ghost":
            continue
        try:
            head = (root / n["id"]).read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            continue  # a note the graph knows and the disk no longer has
        m = re.search(r"^sources:\s*(.*?)(?=^\S|\Z)", head, re.M | re.S)
        if not m:
            continue
        vals = [v.strip().strip("\"'[]") for v in re.findall(r"^\s*-\s*(.+)$", m.group(1), re.M)]
        inline = m.group(1).strip()
        if not vals and inline:
            vals = [v.strip().strip("\"'[]") for v in inline.split(",")]
        vals = [v for v in vals if v]
        if vals:
            out[n["id"]] = vals
    return out


def _corroborate(
    pairs: list[tuple[str, str, float]], nodes: list[dict], top_k: int,
    *, kind: str, color: str, paper: str,
) -> list[dict]:
    """Keep only the pairs that also share a concept, as overlay edges.

    The co-occurrence index is what corroborates; without one the layer is
    EMPTY rather than uncorroborated, because an uncorroborated proposal is
    the thing the gates refuted. The store read itself belongs to
    graph_export, which already owns the frame's other label lookups.
    """
    from silica.kernel.graph.graph_export import shared_concepts

    shared_of = shared_concepts([(u, v) for u, v, _s in pairs])
    out: list[dict] = []
    for i, (u, v, score) in enumerate(pairs):
        labels = shared_of.get((u, v))
        if not labels:
            continue
        out.append({
            "id": f"{kind.lower()}{i}",
            "from": u,
            "to": v,
            "type": kind,
            "color": {"color": color, "paper": paper, "opacity": 0.75},
            "width": 1.2,
            "score": round(score, 4),
            "shared": labels,
        })
        if len(out) >= top_k:
            break
    return out


def stamp_load_bearing(nodes: list[dict], G_und, bet: dict[str, float]) -> None:
    """Coreness, cut-vertex status and surprise onto the nodes themselves (V4).

    On the nodes and not as a layer, because these are facts ABOUT a note the
    way betweenness already is: the frame decides what to do with them (a ring,
    a hover line), and a note carries its own reading wherever it is drawn.
    `strands` is what a cut vertex costs, which is the number the curator's veto
    is really about and the only one that says whether a hold is worth honouring.

    Best-effort: a graph nx cannot read leaves every node exactly as it was.
    """
    from silica.kernel.graph.signals import load_bearing
    from silica.kernel.report.structure import cut_component_sizes

    try:
        core, articulation, surprise = load_bearing(
            G_und, betweenness=bet, degree=dict(G_und.degree()),
        )
        strands = cut_component_sizes(G_und, articulation)
    except Exception as exc:
        logger.warning("graph overlays: load-bearing signals skipped (%s)", exc)
        return
    for n in nodes:
        if n.get("type") == "ghost":
            continue
        nid = n["id"]
        n["coreness"] = core.get(nid, 0)
        n["surprise"] = surprise.get(nid, 0.0)
        if nid in articulation:
            n["cut"] = True
            n["strands"] = strands.get(nid, 0)


def size_by_links(nodes: list[dict], G_und) -> None:
    """A note's size is how many notes it links with (ADR-0152), counted on the
    graph betweenness reads: resolved wikilinks, each neighbour once. A note
    Silica runs on counts for nothing, as its tooltip says.

    A drawn disc's area is proportional to `size`, so the area grows with the
    links and the radius with their root: 51 links draws 2.9 times the radius of
    2. Betweenness drew radii from 6.0 to 7.65 on Cultura (2026-11-05), which
    said nothing and said it twice with the hub ring. `links` rides along: the
    frame's Key names and Clusters read it, and they must agree with the size.
    """
    native = {n["id"] for n in nodes if n.get("type") in NATIVE_TYPES}
    for n in nodes:
        if n.get("type") == "ghost" or n["id"] in native:
            continue
        n["links"] = len(set(G_und[n["id"]]) - {n["id"]} - native) if n["id"] in G_und else 0
        n["size"] = round(16 + 3.5 * n["links"], 2)
