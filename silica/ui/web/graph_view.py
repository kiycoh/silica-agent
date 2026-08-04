# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Graph viewer — the force-graph HTML emitter for the vault wikilink graph.

Split out of `silica.kernel.recall.graph_export` (which keeps the deterministic *data*
role: build_graph_data / detect_communities). This module owns only the viewer:
it turns nodes/edges/communities into a fully self-contained HTML file.

TWO renderers ship in every document: `3d-force-graph` (WebGL) and `force-graph`
(2D canvas), switched at runtime from the HUD. Not `numDimensions(2)` on the 3D
bundle — the point of 2D is readable node text, which canvas gives and a
flattened WebGL scene does not. They are kapsule siblings and share nearly the
whole chainable API, so `buildGraph()` is one builder with four branches.

Both bundles are *vendored* (silica/ui/web/static/, pinned to
3d-force-graph@1.80.0 and force-graph@1.51.2) and inlined into every emitted
file — the artifact opens offline, with no network at render time. `render_html`
keeps an empty-`lib_js` CDN fallback for direct/test callers, but `export_graph`
(the production path) always inlines the vendored bundles and raises loudly if
either asset is missing.
"""
from __future__ import annotations

import html
import importlib.resources
import json
import logging
from pathlib import Path

from silica.kernel.recall.graph_export import Community, Zone

logger = logging.getLogger(__name__)

_VIS_JS_URLS = (
    "https://cdn.jsdelivr.net/npm/3d-force-graph@1.80.0/dist/3d-force-graph.min.js",
    "https://cdn.jsdelivr.net/npm/force-graph@1.51.2/dist/force-graph.min.js",
)

# Both renderers, in load order: WebGL first (the default mode), canvas second.
_VENDORED_BUNDLES = ("3d-force-graph.min.js", "force-graph.min.js")


def _vendored_lib_js() -> str:
    """Read the vendored renderer bundles shipped under ui/web/static/.

    Returns both concatenated (they are independent UMD modules exporting
    `ForceGraph3D` and `ForceGraph`), so callers keep one `lib_js` string.

    Raises a clear RuntimeError if either asset is absent (a packaging bug). We
    do NOT fall back to render_html's empty-lib_js CDN <script src>: that would
    silently reintroduce the network dependency this split removed and hide the
    bug. Keep the trust-boundary failure loud.
    """
    out = []
    for name in _VENDORED_BUNDLES:
        res = importlib.resources.files("silica.ui.web") / "static" / name
        if not res.is_file():
            raise RuntimeError(
                f"graph_export: vendored {name} is missing from silica/ui/web/static/ "
                "— packaging bug. Reinstall silica or re-vendor the assets (pinned "
                "3d-force-graph@1.80.0, force-graph@1.51.2)."
            )
        out.append(res.read_text(encoding="utf-8"))
    return ";\n".join(out)


def _vendored_font_face() -> str:
    """@font-face rule with the Lexend woff2 inlined as a data: URI, so the
    exported HTML stays fully self-contained (it is opened from file:// too).
    Cosmetic asset: if missing, degrade to the system-ui fallback, not a raise."""
    import base64

    res = importlib.resources.files("silica.ui.web") / "static" / "lexend-latin.woff2"
    if not res.is_file():
        return ""
    b64 = base64.b64encode(res.read_bytes()).decode("ascii")
    return (
        '@font-face{font-family:"Lexend";'
        f'src:url("data:font/woff2;base64,{b64}") format("woff2");'
        "font-weight:100 900;font-style:normal;font-display:swap}"
    )


def render_tree(nodes: list[dict]) -> str:
    """Build a collapsible <details> file tree from real note paths.

    Pure: nodes -> HTML. Folders become nested <details>/<summary> (native
    collapse, no JS); notes become <div class="tree-note" data-id=ID>NAME</div>.
    Ghost nodes (type == "ghost" or empty path) are unresolved links, not files,
    so they are skipped. Folders sort before notes at each level; both groups
    sort case-insensitively.
    """
    root: dict = {}
    for n in nodes:
        if n.get("type") == "ghost":
            continue
        path = n.get("path") or ""
        if not path:
            continue
        *folders, leaf = path.split("/")
        cur = root
        for f in folders:
            cur = cur.setdefault(f, {})
        cur.setdefault("__notes__", []).append((leaf, n.get("id", path)))

    def emit(tree: dict, depth: int) -> str:
        out = []
        for name in sorted((k for k in tree if k != "__notes__"), key=str.lower):
            attr = " open" if depth == 0 else ""
            out.append(f"<details{attr}><summary>{html.escape(name)}</summary>")
            out.append(emit(tree[name], depth + 1))
            out.append("</details>")
        for leaf, nid in sorted(tree.get("__notes__", []), key=lambda x: x[0].lower()):
            out.append(
                f'<div class="tree-note" data-id="{html.escape(nid, quote=True)}">'
                f"{html.escape(leaf)}</div>"
            )
        return "".join(out)

    return f'<div id="file-tree">{emit(root, 0)}</div>'


def render_html(
    nodes: list[dict],
    edges: list[dict],
    communities: "list[Community]" = (),  # type: ignore[assignment]
    title: str = "Vault Graph",
    lib_js: str = "",
    discourse: str = "",
    zones: "list[Zone]" = (),  # type: ignore[assignment]
) -> str:
    """Produce a fully self-contained 3d-force-graph HTML string.

    Pass lib_js to embed the bundle inline (truly offline-capable).
    If omitted, CDN link is used as a fallback.
    communities is a list of Community objects; legend is built from it.
    zones is the semantic partition (node["sgroup"]); it draws the zone layer,
    which is off until asked for and is a different grouping from communities.
    """
    nodes_json = json.dumps(nodes, ensure_ascii=False).replace("</", "<\\/")
    edges_json = json.dumps(edges, ensure_ascii=False).replace("</", "<\\/")

    n_notes      = sum(1 for n in nodes if n.get("type") != "ghost")
    n_ghost      = sum(1 for n in nodes if n.get("type") == "ghost")
    n_extracted  = sum(1 for e in edges if e.get("type") == "EXTRACTED")
    n_ambiguous  = sum(1 for e in edges if e.get("type") == "AMBIGUOUS")
    n_gaps       = sum(1 for e in edges if e.get("type") == "GAP")
    n_similar    = sum(1 for e in edges if e.get("type") == "SIMILAR")
    n_communities = len(communities)
    # Semantic-map edges: only surface the row when present (the links view
    # have none, so the row would just read 0 and confuse).
    similar_row = (
        f'<label class="filter-row" style="margin-top:4px" title="Embedding k-NN — notes pulled together by semantic similarity">'
        f'<input type="checkbox" id="cb-similar" checked onchange="updateEdgeFilter()">'
        f'<div class="dot-edge" style="background:#00a5e1"></div>Similar'
        f'<span style="color:#565a77;font-size:11px;margin-left:auto">{n_similar}</span>'
        f'</label>'
    ) if n_similar else ""

    discourse_badge = (
        f'<div style="font-size:11px;color:#8a8da6;letter-spacing:.04em;margin-bottom:6px" '
        f'title="Shape of the wikilink graph: how much of the vault sits in the largest connected '
        f'component and how evenly the clusters split it.">'
        f'discourse: <span style="color:#c9a227;font-weight:600">{html.escape(discourse)}</span></div>'
        if discourse else ""
    )

    # The gap list used to live here, under Edge types. It is a vault-level
    # worklist, not a key to what the canvas is painting, and in a legend it read
    # as neither. It now sits on the vault-level surface that already measured it
    # -- the Structural gaps card in metrics -- where each row carries the
    # bridging action. The amber GAP overlay and its checkbox stay: that IS a key.
    legend_items = "".join(
        f'<div class="legend-item" data-community="{c.id}" data-size="{c.size}" onclick="filterCommunity({c.id})">'
        f'<span class="dot" style="background:{c.color}"></span>{html.escape(c.label)} '
        f'<span style="color:#5a6372;font-size:11px;margin-left:auto">{c.size}</span>'
        f'</div>\n'
        # Biggest first: the legend is read top-down, and the clusters that
        # carry the vault are the ones worth seeing without scrolling.
        for c in sorted(communities, key=lambda c: (-c.size, c.id))
    )

    comm_labels_json = json.dumps(
        {c.id: c.label for c in communities}, ensure_ascii=False
    ).replace("</", "<\\/")

    zones_json = json.dumps(
        [{"id": z.id, "label": z.label, "color": z.color, "size": z.size} for z in zones],
        ensure_ascii=False,
    ).replace("</", "<\\/")

    # The zone layer only exists where the vault has vectors, so the panel does
    # too — an empty "Semantic zones" checkbox would promise a layer that has
    # nothing behind it. The tooltip states the rule the layer obeys, because a
    # second grouping over the same notes is unreadable without one.
    zone_section = (
        f'<div id="zone-panel">'
        f'<div class="section-title" style="margin-bottom:6px" '
        f'title="Louvain over the embedding k-NN, not over your wikilinks: notes grouped by what '
        f'they are about. A different partition from Communities above — the two are not nested, '
        f'so a note can be coloured by one zone and sit inside the region of another, pulled '
        f'there by wikilinks the embeddings do not see. In 3D the regions are colours and labels '
        f'only; the hulls are drawn on the 2D canvas.">Semantic zones</div>'
        f'<label class="filter-row" title="Colour the notes by their semantic zone and draw a '
        f'hull around each. While this is on, colour means zone, not community.">'
        f'<input type="checkbox" id="cb-zones" onchange="updateZoneFilter()">'
        f'<span class="dot" style="background:{zones[0].color if zones else "#565a77"};'
        f'opacity:.5"></span>Show zones'
        f'<span style="color:#565a77;font-size:11px;margin-left:auto">{len(zones)}</span>'
        f'</label>'
        f'<label class="filter-row" style="margin-top:4px" title="Untick to leave the zones alone '
        f'in the frame — the macro read of the vault, with the individual notes and their edges '
        f'out of the way.">'
        f'<input type="checkbox" id="cb-zone-nodes" checked onchange="updateZoneFilter()">'
        f'Notes'
        f'</label>'
        f'</div>'
    ) if zones else ""

    tree_html = render_tree(nodes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  {f'<script>{lib_js}</script>' if lib_js
    else "".join(f'<script src="{u}"></script>' for u in _VIS_JS_URLS)}
  <style>
    /* Palette mirrors the app shell: crystal substrate travelling blue to
       violet the way the mascot is shaded, iced cyan accent, amber for
       caution. Community hues stay data-driven. Type: Lexend, inlined
       as a data: URI because this file must stay self-contained for file:// use.
       Keep these tokens in sync with static/app.css — this block had already
       drifted a generation behind it (--ash-dim was still #757F99, which fails
       4.5:1 on --slate-2). */
    {_vendored_font_face()}
    :root{{
      --void:#0D0917;--slate:#120E21;--slate-2:#1F243A;
      --line:#292F45;--line-2:#3B4662;
      --frost:#EBEFF8;--text:#BAC4D8;--ash:#8E99B0;--ash-dim:#838DA7;
      --accent:#35C6E8;--violet:#5B4BD6;--warn:#E0A93B;
      --sans:"Lexend",system-ui,sans-serif;
    }}
    *{{box-sizing:border-box;margin:0;padding:0;border-radius:0}}
    html{{scrollbar-width:thin;scrollbar-color:var(--line-2) transparent}}
    ::-webkit-scrollbar{{width:8px;height:8px}}
    ::-webkit-scrollbar-track{{background:transparent}}
    ::-webkit-scrollbar-thumb{{background:var(--line-2);border:2px solid var(--void)}}
    ::-webkit-scrollbar-thumb:hover{{background:var(--ash-dim)}}
    body{{display:flex;height:100vh;font-family:var(--sans);font-weight:400;
          background:var(--void);color:var(--frost);overflow:hidden;-webkit-font-smoothing:antialiased}}
    #sidebar{{width:240px;flex-shrink:0;background:var(--slate);border-right:1px solid var(--line);
              display:flex;flex-direction:column;padding:14px 12px;gap:14px;overflow-y:auto}}
    #sidebar h1{{font-size:14px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--frost)}}
    body.embedded #sidebar{{display:none}}
    .stat-grid{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line)}}
    .stat{{background:var(--slate-2);padding:9px;text-align:center}}
    .stat .val{{font-size:24px;font-weight:600;letter-spacing:-.02em;color:var(--frost)}}
    .stat .val.warn{{color:var(--warn)}}
    .stat .lbl{{font-size:10px;color:var(--ash-dim);margin-top:2px;letter-spacing:.08em;text-transform:uppercase}}
    #search{{width:100%;padding:8px 10px;background:var(--slate-2);border:1px solid var(--line-2);
             color:var(--frost);font-family:var(--sans);font-size:13px;outline:none}}
    #search:focus{{border-color:var(--frost)}}
    .section-title{{font-size:10px;color:var(--ash-dim);text-transform:uppercase;letter-spacing:.18em}}
    .filter-row{{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--ash);cursor:pointer;
                 padding:3px 0;user-select:none}}
    .filter-row input{{cursor:pointer;accent-color:var(--accent)}}
    .dot-edge{{width:24px;height:3px;flex-shrink:0}}
    #sort-communities:hover{{color:var(--frost)}}
    #legend-box{{display:flex;flex-direction:column;gap:2px;max-height:200px;overflow-y:auto}}
    .legend-item{{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ash);cursor:pointer;
                  padding:3px 6px}}
    .legend-item:hover{{background:var(--slate-2);color:var(--frost)}}
    .legend-item.active{{background:var(--slate-2);outline:1px solid var(--frost);color:var(--frost)}}
    .dot{{width:9px;height:9px;flex-shrink:0}}
    /* Node-state legend: the ring is the swatch, so the legend looks like what
       drawNode paints. border-radius must be restated — the reset zeroes it. */
    .ring{{width:9px;height:9px;flex-shrink:0;border:1.5px solid;border-radius:50%}}
    .ct{{color:#565a77;font-size:11px;margin-left:auto}}
    /* Focus banner — top-right, tucked left of whatever owns that corner: the
       HUD (216px) when the drawer is shut, the drawer's edge when it is open,
       so the open note's title reads directly above it. A filtered graph that
       does not say it is filtered is lying about the vault. */
    #focus-bar{{position:absolute;top:10px;right:236px;z-index:5;display:none;
                max-width:min(420px,calc(100% - 250px));padding:6px 10px;
                font-size:11px;color:var(--ash);letter-spacing:.04em;
                background:rgba(10,13,20,.92);border:1px solid var(--line-2);
                white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    #focus-bar b{{color:var(--frost);font-weight:600}}
    #focus-bar .esc{{color:var(--ash-dim)}}
    body.host-drawer-open #focus-bar{{right:calc(var(--drawer-w,0px) + 10px)}}
    /* Zone names: over the canvas, under the HUD, never in the way of a click.
       The halo is what keeps a mid-lightness hue readable over both a dark
       background and the translucent hull it sits on. */
    #zone-labels{{position:absolute;inset:0;z-index:4;pointer-events:none;overflow:hidden}}
    .zone-label{{position:absolute;top:0;left:0;display:none;white-space:nowrap;
                 font-size:13px;font-weight:600;letter-spacing:.06em;
                 text-shadow:0 0 10px var(--void),0 0 4px var(--void),0 0 2px var(--void)}}
    .btn{{padding:8px 10px;background:var(--slate-2);border:1px solid var(--line-2);
           color:var(--ash);font-family:var(--sans);font-size:12px;cursor:pointer;text-align:center;
           text-transform:uppercase;letter-spacing:.06em}}
    .btn:hover{{border-color:var(--frost);color:var(--frost)}}
    /* min-width:0 + overflow:hidden: the WebGL canvas must never force the flex
       item wider than the viewport (it pushes the absolute HUD off-screen) */
    #graph-wrap{{flex:1;min-width:0;position:relative;overflow:hidden}}
    #graph{{width:100%;height:100%}}
    /* HUD — floating legend/filter panel anchored to the graph itself */
    #hud{{position:absolute;top:10px;right:10px;z-index:5;width:216px;max-height:calc(100% - 20px);
          display:flex;flex-direction:column;gap:12px;padding:12px;overflow-y:auto;
          background:rgba(10,13,20,.92);border:1px solid var(--line-2)}}
    /* The embedding page's note drawer overlays this frame's right edge, where
       the HUD lives, and the drawer is translucent — so the legend showed
       through the note you were reading. The frame cannot see that drawer, so
       the parent tells it. */
    body.host-drawer-open #hud{{display:none}}
    #drawer{{width:260px;flex-shrink:0;background:var(--slate);border-left:1px solid var(--line);
             padding:18px 16px;overflow-y:auto;display:none;flex-direction:column;gap:12px}}
    #drawer.open{{display:flex}}
    #drawer-title{{font-size:15px;font-weight:600;color:var(--frost);word-break:break-word}}
    #drawer-path{{font-size:11px;color:var(--ash-dim);word-break:break-all}}
    #drawer-meta{{font-size:12px;color:var(--ash)}}
    .drawer-section{{display:flex;flex-direction:column;gap:4px}}
    .drawer-label{{font-size:10px;color:var(--ash-dim);text-transform:uppercase;letter-spacing:.18em}}
    .drawer-val{{font-size:13px;color:var(--frost)}}
    .tag{{display:inline-block;padding:2px 7px;background:var(--slate-2);border:1px solid var(--line);
           font-size:11px;color:var(--ash);margin:2px}}
    #close-drawer{{align-self:flex-end;cursor:pointer;color:var(--ash-dim);font-size:18px;line-height:1}}
    #close-drawer:hover{{color:var(--frost)}}
    #search-results{{display:none;flex-direction:column;gap:1px;max-height:260px;overflow-y:auto;
                     margin-top:6px;border:1px solid var(--line);background:var(--slate-2)}}
    #search-results.open{{display:flex}}
    #search-count{{font-size:10px;color:var(--ash-dim);letter-spacing:.04em;padding:6px 8px 2px}}
    .result-item{{display:flex;flex-direction:column;gap:1px;padding:6px 8px;cursor:pointer;border-left:2px solid transparent}}
    .result-item:hover,.result-item.sel{{background:var(--slate);border-left-color:var(--accent)}}
    .result-name{{font-size:12px;color:var(--frost);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .result-sub{{font-size:10px;color:var(--ash-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .result-sub em{{color:var(--frost);font-style:normal}}
    #file-tree{{display:flex;flex-direction:column;max-height:260px;overflow-y:auto;font-size:12px}}
    #file-tree summary{{cursor:pointer;color:var(--ash);padding:2px 0;user-select:none;
                        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    #file-tree summary:hover{{color:var(--frost)}}
    #file-tree details details,#file-tree .tree-note{{margin-left:12px}}
    .tree-note{{color:var(--ash);cursor:pointer;padding:2px 6px;border-left:2px solid transparent;
               white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .tree-note:hover{{background:var(--slate-2);border-left-color:var(--accent);color:var(--frost)}}
    .force-row{{display:flex;justify-content:space-between;align-items:center;font-size:12px;
                color:var(--ash);margin-top:6px}}
    .force-row .fv{{color:var(--ash-dim);font-size:11px}}
    .force-slider{{width:100%;accent-color:var(--accent);cursor:pointer;margin-top:2px}}
    /* renderer switch — two halves of one control, so it reads as "one of these
       two", not as two independent buttons */
    .seg{{display:flex;border:1px solid var(--line-2);background:var(--slate-2)}}
    .seg button{{flex:1;padding:6px 4px;background:none;border:none;cursor:pointer;
                 font-family:var(--sans);font-size:11px;letter-spacing:.06em;
                 text-transform:uppercase;color:var(--ash-dim)}}
    .seg button:hover{{color:var(--frost)}}
    .seg button.active{{background:var(--line);color:var(--frost)}}
  </style>
</head>
<body>

<div id="sidebar">
  <h1>&#11041; {title}</h1>

  <!-- Every number states its own rule, not its name: a count whose denominator
       you cannot name is a number you cannot act on. -->
  <div class="stat-grid">
    <div class="stat" title="Files in the graph. Unresolved link targets are not files and are counted under Unresolved instead."><div class="val">{n_notes}</div><div class="lbl">Notes</div></div>
    <div class="stat" title="Wikilinks whose target file exists, counted once per direction between a pair of notes — not once per occurrence in the text."><div class="val">{n_extracted}</div><div class="lbl">Links</div></div>
    <div class="stat" title="Louvain communities over the resolved wikilinks. Derived from the structure, never declared by you."><div class="val">{n_communities}</div><div class="lbl">Clusters</div></div>
    <div class="stat" title="Distinct link targets with no file behind them, counted once per name however many notes point at it."><div class="val">{n_ghost}</div><div class="lbl">Unresolved</div></div>
  </div>

  <input id="search" type="text" placeholder="Search notes, paths, #tags&#8230;"
         oninput="onSearch(this.value)" onkeydown="onSearchKey(event)" autocomplete="off">
  <div id="search-results"></div>

  <div>
    <div class="section-title" style="margin-bottom:6px">Files</div>
    {tree_html}
  </div>
</div>

<div id="graph-wrap">
  <div id="graph"></div>
  <!-- Zone names ride the DOM, not the canvas and not a three sprite: one
       positioning path serves both renderers, and the vendored 3d bundle
       exposes no THREE to build a sprite with anyway. -->
  <div id="zone-labels"></div>
  <div id="focus-bar"></div>
  <div id="hud">
    <div>
      <div class="section-title" style="margin-bottom:8px">Edge types</div>
      <label class="filter-row" title="A [[wikilink]] whose target file exists.">
        <input type="checkbox" id="cb-extracted" checked onchange="updateEdgeFilter()">
        <div class="dot-edge" style="background:#8a8da6"></div>
        Resolved
        <span style="color:#565a77;font-size:11px;margin-left:auto">{n_extracted}</span>
      </label>
      <label class="filter-row" style="margin-top:4px" title="A [[wikilink]] pointing at a name no file carries — the link is written, the note is not.">
        <input type="checkbox" id="cb-ambiguous" onchange="updateEdgeFilter()">
        <div class="dot-edge" style="background:#e2544f"></div>
        Unresolved
        <span style="color:#565a77;font-size:11px;margin-left:auto">{n_ambiguous}</span>
      </label>
      <label class="filter-row" style="margin-top:4px" title="Well-formed areas with no links between them — a bridge could go here">
        <input type="checkbox" id="cb-gaps" checked onchange="updateEdgeFilter()">
        <div class="dot-edge" style="background:#c9a227"></div>
        Structural gaps
        <span style="color:#565a77;font-size:11px;margin-left:auto">{n_gaps}</span>
      </label>
      {similar_row}
    </div>

    <!-- Colour already carries the community, so node STATE rides a second
         channel: a ring. Only the two states that had no marking at all get
         one; ghost keeps the rendering it already had, and the swatch here
         shows what each actually looks like on the canvas. 2D only — in 3D a
         ring means rebuilding every node's geometry, and size already
         separates hubs there. -->
    <div id="state-legend">
      <div class="section-title" style="margin-bottom:6px" title="What each node IS, on a channel the community colour is not using.">Node state</div>
      <div class="filter-row" title="Betweenness in the top tenth of the notes that have any — the crossings the vault routes through.">
        <span class="ring" style="border-color:#35C6E8"></span>Hub<span class="ct" id="st-hub"></span>
      </div>
      <div class="filter-row" title="The note exists and no resolved wikilink points at it. Reachable from the file tree, unreachable from the vault.">
        <span class="ring" style="border-color:#838DA7"></span>Orphan<span class="ct" id="st-orphan"></span>
      </div>
      <div class="filter-row" title="Something links here and no file carries the name. Already unlit and undersized in the view, so it takes no ring.">
        <span class="dot" style="background:#484867;border-radius:50%"></span>Ghost<span class="ct" id="st-ghost"></span>
      </div>
    </div>

    <div>
      <div class="section-title" style="margin-bottom:6px;display:flex;align-items:center;justify-content:space-between"
           title="Louvain over the resolved wikilinks — the structural partition, the vault as you linked it. Not the semantic zones: the two groupings are independent and share no colour.">
        Communities
        <span id="sort-communities" style="color:#8a8da6;cursor:pointer;font-size:11px;letter-spacing:0;text-transform:none"
              onclick="toggleCommunitySort()" title="sort by size">size &#8595;</span>
      </div>
      {discourse_badge}
      <div id="legend-box">
{legend_items}      <div class="legend-item active" id="legend-all" onclick="filterCommunity(-2)">
          <span class="dot" style="background:#565a77"></span>Show all
        </div>
      </div>
    </div>

    {zone_section}

    <div>
      <div class="section-title" style="margin-bottom:6px">Renderer</div>
      <div class="seg" id="mode-toggle">
        <button type="button" data-mode="3d" onclick="setMode('3d')">3D</button>
        <button type="button" data-mode="2d" onclick="setMode('2d')">2D</button>
      </div>
    </div>

    <div>
      <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
        Forces
        <span style="color:#8a8da6;cursor:pointer;font-size:11px;letter-spacing:0;text-transform:none"
              onclick="resetForces()" title="back to auto-scaled defaults">reset</span>
      </div>
      <div class="force-row">Repel<span class="fv" id="fv-repel">1.0&times;</span></div>
      <input type="range" class="force-slider" id="sl-repel" min="-0.7" max="0.7" step="0.01" value="0" oninput="onForceSlider()">
      <div class="force-row">Link distance<span class="fv" id="fv-dist">1.0&times;</span></div>
      <input type="range" class="force-slider" id="sl-dist" min="-0.7" max="0.7" step="0.01" value="0" oninput="onForceSlider()">
      <div class="force-row">Center<span class="fv" id="fv-center">1.00</span></div>
      <input type="range" class="force-slider" id="sl-center" min="0" max="1" step="0.05" value="1" oninput="onForceSlider()">
    </div>

    <div style="display:flex;gap:6px">
      <div class="btn" style="flex:1" onclick="fitGraph()">&#8862; Fit graph</div>
      <div class="btn" title="rebuild from the vault (e.g. after editing notes outside silica)"
           onclick="location.reload()">&#8635;</div>
    </div>
  </div>
</div>

<div id="drawer">
  <span id="close-drawer" onclick="closeDrawer()">&#10005;</span>
  <div id="drawer-title">&#8212;</div>
  <div id="drawer-path"></div>
  <div id="drawer-meta"></div>
  <div class="drawer-section">
    <div class="drawer-label">Out-links</div>
    <div id="drawer-out" class="drawer-val">&#8212;</div>
  </div>
  <div class="drawer-section">
    <div class="drawer-label">Backlinks</div>
    <div id="drawer-in" class="drawer-val">&#8212;</div>
  </div>
  <div id="drawer-tags-section" class="drawer-section" style="display:none">
    <div class="drawer-label">Tags</div>
    <div id="drawer-tags"></div>
  </div>
</div>

<script>
// Embedded in the web app's iframe: the app's own sidebar (stats/search/tree)
// replaces the internal one; only the graph + HUD legend remain.
if (window.parent !== window) document.body.classList.add("embedded");

const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const COMM_LABELS = {comm_labels_json};
// The semantic partition: node.sgroup -> zone. Disjoint from node.group in every
// way that matters — different edges, different ids, different colours.
const ZONES = {zones_json};
const ZONE_COLOR = {{}};
ZONES.forEach(z => {{ ZONE_COLOR[z.id] = z.color; }});

const outDeg = {{}}, inDeg = {{}};
RAW_EDGES.forEach(e => {{
  outDeg[e.from] = (outDeg[e.from] || 0) + 1;
  inDeg[e.to]   = (inDeg[e.to]   || 0) + 1;
}});

const NODE_BY_ID = {{}};
RAW_NODES.forEach(n => {{ NODE_BY_ID[n.id] = n; }});

const neighbors = {{}};
RAW_EDGES.forEach(e => {{
  (neighbors[e.from] = neighbors[e.from] || new Set()).add(e.to);
  (neighbors[e.to]   = neighbors[e.to]   || new Set()).add(e.from);
}});

let focusIds = [];  // the focused set; [] = nothing focused
const NO_NEIGHBOURS = new Set();

// Highlight a SET of nodes and their 1-hop neighbours; dim everything else.
// A set, not one id: the context drawer lights every note carrying a concept,
// and a single-node focus is just the one-element case.
//
// Split in two on purpose: computeFocus only writes the _dim flags onto the
// shared node/link objects, applyFocus also repaints. Those objects outlive the
// renderer instance, so a fresh one picks the flags up in its first graphData()
// digest — which is what lets a rebuild compute and skip the repaint. The
// repaint is not free (see refreshPaint).
function computeFocus(ids) {{
  focusIds = (ids == null ? [] : [].concat(ids)).filter(id => NODE_BY_ID[id]);
  const on = new Set(focusIds);
  // Lit = the focused nodes plus their 1-hop neighbours; an edge stays lit when
  // EITHER endpoint is focused (neighbour-to-neighbour edges dim, as before).
  const lit = new Set(focusIds);
  focusIds.forEach(id => (neighbors[id] || NO_NEIGHBOURS).forEach(nb => lit.add(nb)));
  RAW_NODES.forEach(n => {{ n._dim = on.size > 0 && !lit.has(n.id); }});
  RAW_EDGES.forEach(e => {{ e._dim = on.size > 0 && !on.has(e.from) && !on.has(e.to); }});
  updateFocusBar();
}}

// --- The banner: say out loud that you are not looking at the whole vault ---
// Two mechanisms hide notes — the community filter and the focus dim — and
// neither used to announce itself. Both land here, so one line covers both, and
// it names the way out (Esc) next to the reason it is needed.
function updateFocusBar() {{
  const bar = document.getElementById("focus-bar");
  const parts = [];
  // Notes off is the loudest filter of the three: with the zones off too it
  // empties the frame outright, and an empty frame that says nothing reads as a
  // broken view rather than a chosen one.
  if (!showNotes) parts.push(showZones ? "<b>zones only</b>" : "<b>notes hidden</b>");
  if (activeCommunity !== -2) {{
    const label = COMM_LABELS[activeCommunity] || ("cluster " + activeCommunity);
    const n = RAW_NODES.filter(x => x.group === activeCommunity).length;
    parts.push("cluster <b>" + escHtml(label) + "</b> · " + n + " notes");
  }}
  if (focusIds.length) {{
    const lit = new Set(focusIds);
    focusIds.forEach(id => (neighbors[id] || NO_NEIGHBOURS).forEach(nb => lit.add(nb)));
    const nearby = Math.max(0, lit.size - focusIds.length);
    // One note focused: its name is already on screen above this bar, in the
    // drawer header. Repeating it here is noise, so only a set says what it is.
    const head = focusIds.length === 1 ? "" : "<b>" + focusIds.length + " notes</b> ";
    parts.push(head + "+ " + nearby + " neighbour" + (nearby === 1 ? "" : "s"));
  }}
  bar.innerHTML = parts.length
    ? parts.join(" · ") + ' <span class="esc">· Esc to clear</span>' : "";
  bar.style.display = parts.length ? "block" : "none";
}}

const escHtml = s => String(s).replace(/[&<>]/g, c => (
  {{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));

function applyFocus(ids) {{ computeFocus(ids); refreshPaint(); }}

function fitGraph() {{ Graph.zoomToFit(400, 40); wake(600); }}

// Undim AND reframe — the background click means "show me everything again".
function clearFocus() {{
  applyFocus(null);
  Graph.zoomToFit(600, 40);
  wake(700);   // the refit is a camera tween; it needs frames to run
}}

// Re-pass the colour accessors so the renderer repaints without touching the
// simulation. 2D also needs nodeCanvasObject re-passed — the canvas draw reads
// _dim itself, and force-graph caches nothing per node between frames, so a
// plain redraw suffices there; the re-pass is what schedules it.
//
// Cheap in 2D only. force-graph declares nodeColor/linkColor with
// `triggerUpdate:false`, so the re-pass just raises needsRedraw. 3d-force-graph
// does NOT: it lists nodeColor/nodeVisibility/linkColor/linkVisibility among the
// props that re-run the whole node and link digest, so every re-pass rebuilds
// the material of every node and every link. Never call this when a rebuild is
// about to happen anyway.
function refreshPaint() {{
  Graph.nodeColor(Graph.nodeColor());
  Graph.linkColor(Graph.linkColor());
  wake(120);
}}

let activeCommunity = -2;
let showExtracted = true;
let showAmbiguous = false;
let showGaps = true;
let showSimilar = true;
let showZones = false;   // the semantic layer is asked for, never assumed
let showNotes = true;    // the macro read: zones alone in the frame

// --- Node color = its community color, flat -------------------------------
// One hue per community: every node in a community shares the exact color,
// hub or leaf. Degree is shown by size, never by washing the hue out.
function nodeColor(n) {{
  // ponytail: solid darken-to-background dim; switch to rgba() only if visual
  // verification shows 3d-force-graph honours per-node alpha.
  // Neutrals are blue-violet, never gray: the mascot has no gray facet, only
  // unlit ones. Each value below holds the luminance of the gray it replaces.
  if (n._dim) return '#1d192f';
  if (n.type === 'ghost') return '#484867';   // unlit, never black
  // Colour is ONE channel and the vault has two partitions, so it carries one
  // at a time and the HUD says which: with the zone layer on it is the semantic
  // one, otherwise the structural community. Never a blend of the two.
  if (showZones && n.sgroup >= 0) return ZONE_COLOR[n.sgroup] || '#565a77';
  return (n.color && n.color.background) || '#565a77';
}}

// --- Node state = a ring, on a channel colour is not already using ----------
// Nothing new is computed here: betweenness rides on every node already (it is
// what sizes them) and the in-degree comes from the edge list. The states are
// the ones the vault report already names, so the view and the report agree.
//
// Orphan matches graph_report's definition exactly — in-degree zero over
// RESOLVED wikilinks only. Counting SIMILAR would erase the state entirely
// (k-NN gives almost every note an in-edge), which is the opposite of the
// question "is anything in the vault pointing here".
const linkInDeg = {{}};
RAW_EDGES.forEach(e => {{
  if (e.type === "EXTRACTED") linkInDeg[e.to] = (linkInDeg[e.to] || 0) + 1;
}});

// Hub = top decile of the nodes that have any betweenness at all. A fixed
// threshold would call half a dense vault a hub and none of a sparse one;
// the decile asks the same question of both.
const _bets = RAW_NODES.map(n => n.betweenness || 0).filter(b => b > 0).sort((a, b) => a - b);
const HUB_MIN = _bets.length ? _bets[Math.floor(_bets.length * 0.9)] : Infinity;

// Priority is deliberate: a crossing with no backlinks is worth reading as a
// hub, not as an orphan.
function nodeState(n) {{
  if (n.type === "ghost") return "ghost";
  if ((n.betweenness || 0) >= HUB_MIN) return "hub";
  if (!linkInDeg[n.id]) return "orphan";
  return "note";
}}

// Only the states with NO channel of their own get a ring. "note" is the
// default, so ringing it would say nothing; ghost already has three markers
// (its own unlit colour, a smaller radius, a dimmer label) and there are 468 of
// them in a 682-note vault — a fourth marker on the most numerous state turns
// the whole view into an alarm about links you have not written yet.
const STATE_RING = {{ hub: "#35C6E8", orphan: "#838DA7" }};

// Counts never change (the states come from the exported data), so this runs
// once at load, not per repaint.
function syncStateLegend() {{
  const c = {{ hub: 0, orphan: 0, ghost: 0, note: 0 }};
  RAW_NODES.forEach(n => c[nodeState(n)]++);
  Object.keys(c).forEach(k => {{
    const el = document.getElementById("st-" + k);
    if (el) el.textContent = c[k];
  }});
}}
syncStateLegend();

// --- Density-aware forces ---------------------------------------------------
// The lib's d3 defaults (charge -60 in 3D, link distance 30) collapse dense
// graphs into a hairball: equilibrium spacing must grow with avg degree or
// neighborhoods overlap. sqrt keeps sparse graphs (k<=2) exactly as before
// (scale=1) and opens dense ones up to 4x. Sliders multiply on top of this
// baseline, so the auto-scaling stays authoritative as the vault grows.
const AVG_DEG = RAW_NODES.length ? 2 * RAW_EDGES.length / RAW_NODES.length : 0;
const FORCE_SCALE = Math.min(4, Math.max(1, Math.sqrt(AVG_DEG / 2)));
// Same auto-scaled baseline in both modes; only the per-mode constants differ.
// 2D has one dimension fewer to disperse into, so at the same charge the plane
// packs tighter than the sphere: repulsion and rest length are opened up until
// x1 reads the same in both. (Tuned by eye — that IS what "looks right" means.)
const CHARGE_2D_K = 1.8, DIST_2D_K = 1.5;
const baseCharge = () => -60 * FORCE_SCALE * FORCE_SCALE * (is2D() ? CHARGE_2D_K : 1);
const baseDist   = () => 30 * FORCE_SCALE * (is2D() ? DIST_2D_K : 1);
// Fixed 100 ticks never let a big graph unfold; scale the tick budget with size.
//
// Most of that budget is spent before the first paint. Both libs advance the
// layout exactly one tick per RENDERED frame, so settle time is
// tick-budget / frame-rate: the slower renderer takes proportionally longer to
// reach the same layout, and on a real GPU the 2D canvas (1147 arcs + labels +
// 4566 strokes per frame, all CPU) is the slower one by a wide margin. That is
// the whole reason 2D feels sluggish to settle next to 3D; the initial node
// positions have nothing to do with it (measured: a 2D layout started from
// scratch and one started from the collapsed 3D projection both settled in
// 3650ms, because both burn the same fixed tick count).
//
// warmupTicks runs the same tick() in a plain loop with nothing painted. Same
// total ticks, same forces, same result — measured bit-identical at 1412 mean
// radius / 307.8 mean edge length — reached 2.2x sooner, and the last stretch
// still animates so the unfolding is not lost.
const TOTAL_TICKS = 100 + Math.min(200, Math.round(RAW_NODES.length / 10));
const WARMUP_TICKS = Math.round(TOTAL_TICKS * 0.7);   // unpainted, blocking
const COOLDOWN_TICKS = TOTAL_TICKS - WARMUP_TICKS;    // painted, animated

// --- 2D labels: node radius + zoom LOD --------------------------------------
// Base size is whatever the smallest real note got (16 today, or 16+40*b once
// betweenness sizing runs); anything above it is a node that stands out, and
// those are the ones worth a label before you have zoomed in.
const BASE_SIZE = RAW_NODES.reduce(
  (m, n) => n.type === "ghost" ? m : Math.min(m, n.size || 16), Infinity);
const NODE_REL_SIZE = 1.5;  // r = sqrt(val) * this — 16 => 6px, a 56 hub => 11px
const nodeRadius = n => Math.sqrt(Math.max(0, n.size || 16)) * NODE_REL_SIZE;

// Circle + text. Below ~0.6 zoom only dots (at that scale the text is a smear
// and the labels outnumber the pixels); 0.6-1.5 the standouts; above 1.5 all of
// them. Font size divides by the zoom so text keeps a constant SCREEN size.
function drawNode(n, ctx, scale) {{
  const r = nodeRadius(n);
  ctx.beginPath();
  ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
  ctx.fillStyle = nodeColor(n);
  ctx.fill();
  if (n._dim || scale < 0.6) return;                       // dimmed: dot only
  // The ring is detail, so it lives behind the same zoom gate as the labels.
  // Drawing it at every scale flooded the zoomed-out view: rings outnumbered
  // the pixels between them and the whole graph read as one alarm.
  // It sits OUTSIDE the disc so the community hue keeps its full area, and both
  // offset and width divide by the zoom to stay constant on screen.
  const ring = STATE_RING[nodeState(n)];
  if (ring) {{
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + 2 / scale, 0, 2 * Math.PI);
    ctx.lineWidth = 1.5 / scale;
    ctx.strokeStyle = ring;
    ctx.stroke();
  }}
  if (scale < 1.5 && (n.size || 16) <= BASE_SIZE) return;  // mid zoom: standouts
  ctx.font = (11 / scale) + 'px Lexend, system-ui, sans-serif';
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = n.type === "ghost" ? "#838DA7" : "#EBEFF8";  // --ash-dim / --frost
  ctx.fillText(n.label, n.x, n.y + r + 2 / scale);
}}

// Hit area follows the circle, not the label: clicking the text of a dense
// cluster would otherwise pick whichever node's label happened to be on top.
function paintNodeArea(n, color, ctx) {{
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(n.x, n.y, nodeRadius(n) + 2, 0, 2 * Math.PI);
  ctx.fill();
}}

// --- the renderer, either dimension ----------------------------------------
// /graph regenerates the whole document per request (cooccurrence refresh + kNN
// + Louvain), so the switch must never reload: it destroys the instance and
// rebuilds from the RAW_NODES/RAW_EDGES already in the page. The two libs are
// kapsule siblings — one builder, four branches (fly-to, sphere detail, link
// width, labels), everything else shared.
const MODE_KEY = "silica-graph-mode";
let mode = "3d";
try {{ if (localStorage.getItem(MODE_KEY) === "2d") mode = "2d"; }} catch (e) {{}}
const is2D = () => mode === "2d";

let Graph = null;
let fitPending = false;  // one-shot zoomToFit after a rebuild

// --- render budget: stop paying 60fps for a picture that stopped moving -----
// Neither bundle idles on its own here. 3d-force-graph's _animationCycle is
// unconditional: tickFrame + render + requestAnimationFrame, every frame,
// forever. force-graph DOES have autoPauseRedraw (default on), but its wake
// condition includes `links.some(l => l.__photons.length)` — so the five GAP
// particle links hold the whole canvas awake, repainting all 553 nodes at 60Hz
// to move ten dots. On a settled graph that is the single largest cost in this
// view, and it is pure waste: nothing on screen changes.
//
// So: full rate while the layout settles, the camera tweens or the pointer is
// over the graph; IDLE_FPS when only the gap particles still move; nothing when
// even those are off. The failure mode is chosen — a missed wake signal leaves
// the loop running, i.e. exactly today's behaviour, never a frozen view.
const IDLE_FPS = 20;
const WAKE_MS = 1200;   // trackball inertia keeps the camera moving after pointerup
let awakeUntil = 0, simRunning = true, idleTick = null, sleepTimer = null;

const gapsMoving = () =>
  showGaps && RAW_EDGES.some(e => e.type === "GAP" && !e._dim && !e._hidden);

function renderBudget() {{
  if (!Graph) return;
  clearInterval(idleTick); idleTick = null;
  if (simRunning || performance.now() < awakeUntil) {{ Graph.resumeAnimation(); return; }}
  Graph.pauseAnimation();
  // resumeAnimation() runs one cycle synchronously and re-arms rAF;
  // pauseAnimation() cancels the re-arm. Net effect: exactly one frame.
  if (gapsMoving()) idleTick = setInterval(
    () => {{ Graph.resumeAnimation(); Graph.pauseAnimation(); }}, 1000 / IDLE_FPS);
}}

// Keep rendering at full rate for `ms`. Every mutation and every camera tween
// calls this; the tail covers control inertia and tween duration.
function wake(ms = WAKE_MS) {{
  awakeUntil = Math.max(awakeUntil, performance.now() + ms);
  clearTimeout(sleepTimer);
  sleepTimer = setTimeout(renderBudget, awakeUntil - performance.now() + 30);
  renderBudget();
}}

function buildGraph() {{
  const el = document.getElementById("graph");
  if (Graph) {{
    // Loud on failure: a swallowed teardown leaks the WebGL context, and the
    // browser caps them at ~16 — the graph would go black after enough mode
    // switches with nothing in the console to say why.
    try {{ Graph._destructor(); }} catch (e) {{ console.warn("graph teardown failed", e); }}
    el.innerHTML = "";
  }}
  const G = is2D() ? new ForceGraph(el) : new ForceGraph3D(el);
  // Before graphData, not after: the layout consumes warmupTicks when the data
  // lands, so a later call in the chain would be read one rebuild too late.
  G.warmupTicks(WARMUP_TICKS)
    .backgroundColor("#0D0917")   // --void
    .graphData({{ nodes: RAW_NODES, links: RAW_EDGES }})
    .linkSource("from").linkTarget("to")
    .nodeVal("size")
    .nodeColor(nodeColor)
    .linkColor(l => l._dim ? '#141221' : ((l.color && l.color.color) || "#8a8da6"))
    // Structural gaps have no dash in WebGL — mark them by motion instead: amber
    // particles stream along the absent bridge. Only for GAP links, and they stop
    // when the link is dimmed (node focus) so focus mode stays quiet. Same
    // binding in both libs, so the gaps read the same either way.
    .linkDirectionalParticles(l => l.type === "GAP" && !l._dim ? 2 : 0)
    .linkDirectionalParticleColor(() => "{_EDGE_COLOR_GAP}")
    .linkDirectionalParticleWidth(2)
    .cooldownTicks(COOLDOWN_TICKS)
    .nodeVisibility(n => !n._hidden)
    .linkVisibility(l => !l._hidden)
    .onNodeClick(node => {{ selectNode(node); applyFocus(node.id); }})
    .onBackgroundClick(() => {{ closeDrawer(); clearFocus(); }})
    .onEngineStop(() => {{
      simRunning = false;
      if (fitPending) {{ fitPending = false; G.zoomToFit(400, 40); wake(600); }}
      else renderBudget();
    }});

  if (is2D()) {{
    // Width 0 is invisible on canvas (GL draws a 1px line for it, 2D draws
    // nothing), and the labels ARE the point here, so pay for them.
    G.linkWidth(1)
      .nodeRelSize(NODE_REL_SIZE)
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint(paintNodeArea)
      // Pre, not Post: a zone is the ground the notes stand on. 2D only — the
      // 3D bundle hands out no THREE, so there the zones are colour and name.
      .onRenderFramePre(drawZones);
  }} else {{
    // Perf on big vaults (1200+ notes): linkWidth>0 makes every edge a cylinder
    // mesh and arrows add a cone per edge — thousands of meshes. Width 0 ⇒ cheap
    // GL lines; no arrows; fewer sphere segments; finite cooldown so the sim
    // settles and stops reflowing instead of re-laying-out every frame.
    G.linkWidth(0).nodeResolution(6).nodeLabel("label");
  }}
  return G;
}}

// Switching preserves everything that is not the camera: edge filters, the
// community filter, the focused set and the search box all live outside the
// instance. The camera cannot be preserved — there is no sane mapping from a 3D
// camera to a 2D pan/zoom — so it refits once the new layout settles.
function setMode(m) {{
  if (m === mode && Graph) return;
  const rebuild = Graph !== null;   // a switch always refits; a first build may not
  mode = m;
  try {{ localStorage.setItem(MODE_KEY, m); }} catch (e) {{}}
  document.querySelectorAll("#mode-toggle button")
    .forEach(b => b.classList.toggle("active", b.dataset.mode === m));
  // The rings are a canvas draw. Leaving their legend up in 3D would promise a
  // channel that mode does not paint.
  document.getElementById("state-legend").style.display = is2D() ? "" : "none";
  fitPending = fitPending || rebuild;
  // Flags BEFORE the build, repaint never: the new instance reads _hidden/_dim
  // in its first graphData() digest. Recomputing them afterwards used to cost
  // four more full digests to arrive at the state the build already had.
  computeFilters();
  computeFocus(focusIds);
  simRunning = true;
  Graph = buildGraph();
  applyForces(false);      // sim restarts at full alpha — no reheat needed
  renderBudget();
}}

// Slider multipliers persist across sessions; the baseline is never persisted
// (recomputed from the current graph each load).
const FORCES_KEY = "silica-graph-forces";
let forceMul = {{ repel: 1, dist: 1, center: 1 }};
try {{
  Object.assign(forceMul, JSON.parse(localStorage.getItem(FORCES_KEY)) || {{}});
}} catch (e) {{ /* corrupt or blocked storage -> auto defaults */ }}

function applyForces(reheat) {{
  // distanceMax bounds both over-dispersion and per-tick cost on big graphs.
  Graph.d3Force("charge").strength(baseCharge() * forceMul.repel)
    .distanceMax(600 * FORCE_SCALE);
  Graph.d3Force("link").distance(baseDist() * forceMul.dist);
  // Center capped at 1: d3 forceCenter shifts positions directly, >1 oscillates.
  Graph.d3Force("center").strength(Math.min(1, forceMul.center));
  if (reheat) {{ Graph.d3ReheatSimulation(); simRunning = true; renderBudget(); }}
}}

// Log-scale track for the multiplier sliders: x1 sits mid-track and the
// useful 0.2-1 range gets half the travel instead of a sliver.
const fromSlider = v => Math.pow(10, +v);
const toSlider = m => Math.log10(m);

function syncForceUI() {{
  document.getElementById("sl-repel").value = toSlider(forceMul.repel);
  document.getElementById("sl-dist").value = toSlider(forceMul.dist);
  document.getElementById("sl-center").value = forceMul.center;
  document.getElementById("fv-repel").textContent = forceMul.repel.toFixed(1) + "\\u00d7";
  document.getElementById("fv-dist").textContent = forceMul.dist.toFixed(1) + "\\u00d7";
  document.getElementById("fv-center").textContent = (+forceMul.center).toFixed(2);
}}

function onForceSlider() {{
  forceMul.repel = fromSlider(document.getElementById("sl-repel").value);
  forceMul.dist = fromSlider(document.getElementById("sl-dist").value);
  forceMul.center = +document.getElementById("sl-center").value;
  try {{ localStorage.setItem(FORCES_KEY, JSON.stringify(forceMul)); }} catch (e) {{}}
  syncForceUI();
  applyForces(true);
}}

function resetForces() {{
  forceMul = {{ repel: 1, dist: 1, center: 1 }};
  try {{ localStorage.removeItem(FORCES_KEY); }} catch (e) {{}}
  syncForceUI();
  applyForces(true);
}}

syncForceUI();
// First build: the mode comes from localStorage (3D on a fresh profile), and
// setMode owns the whole bring-up — instance, forces, filters, focus. A 2D
// first paint has no default camera worth keeping, so it refits; 3D keeps the
// lib's own initial framing.
fitPending = is2D();
setMode(mode);

// Same split as computeFocus/applyFocus: flags on the shared objects here,
// repaint only when there is no rebuild coming to read them for free.
function computeFilters() {{
  RAW_NODES.forEach(n => {{
    n._hidden = !showNotes || (activeCommunity !== -2 && n.group !== activeCommunity);
  }});
  RAW_EDGES.forEach(e => {{
    // Notes off takes the edges with it: an edge between two invisible nodes is
    // a line to nowhere, and the macro read is exactly the one that cannot
    // afford 2718 of them.
    e._hidden = !showNotes ||
                (e.type === "EXTRACTED" && !showExtracted) ||
                (e.type === "AMBIGUOUS" && !showAmbiguous) ||
                (e.type === "GAP" && !showGaps) ||
                (e.type === "SIMILAR" && !showSimilar);
  }});
}}

function applyFilters() {{
  computeFilters();
  // Re-pass the current accessor to force a visibility refresh without resetting the physics layout
  Graph.nodeVisibility(Graph.nodeVisibility());
  Graph.linkVisibility(Graph.linkVisibility());
  wake(120);   // and re-evaluate the idle tick: gaps may have just been toggled
}}

function updateEdgeFilter() {{
  showExtracted = document.getElementById("cb-extracted").checked;
  showAmbiguous = document.getElementById("cb-ambiguous").checked;
  showGaps = document.getElementById("cb-gaps").checked;
  const cbSim = document.getElementById("cb-similar");
  if (cbSim) showSimilar = cbSim.checked;
  applyFilters();
}}

// --- The semantic zone layer ------------------------------------------------
// Hull + name around the members of each k-NN Louvain cluster. Not a second
// view and not a second graph: the same frame, one more layer, so the two
// partitions are read against each other instead of one after the other.
//
// The k-NN FORCES are not toggled here and never were — d3's link force reads
// every link in graphData, and linkVisibility is a render-time accessor, so the
// SIMILAR edges pull whether or not they are drawn. That decoupling is what
// makes a hull honest: on a pure-wikilink layout the members of a semantic
// cluster sit all over the frame and its hull would be a lie about space.
// The region is a CORRIDOR OF CONSTANT WIDTH along the zone's minimum spanning
// tree, not a hull around it.
// A zone has to LOOK like one region — Louvain hands back a partition, and a
// grouping drawn as scattered islands reads as several. But a CONVEX hull buys
// that continuity by claiming the members are contiguous in SPACE, and they are
// not: the layout is driven by the wikilinks too, so a semantic zone is
// routinely scattered and its hull swallows whatever lies between the pieces.
//
// Measured on the 682-note vault, one settled layout, counting foreign notes
// that fall inside a zone's drawn region:
//
//   convex hull            675   1.88 regions over the average note   max 6
//   plain discs r=0.7·d    110   1.16                                 max 3   NOT continuous
//   MST corridor r=0.35·d  119   1.17                                 max 3   continuous
//
// So continuity is nearly free as long as the corridors are the SHORTEST set
// that joins the members — which is what a minimum spanning tree is. Striping
// every intra-zone k-NN edge instead was measured too: 2406 edges, many of them
// long sweeps across the frame, 300 foreign notes at a third of this width.
//
// What overlap survives is the real thing (spec §3): wikilink pull dragging a
// note into another zone's neighbourhood. The gaps stay gaps, which is the
// information the tessellation was rejected to keep.
//
// Width follows the layout, never a constant: baseDist() IS the link force's
// rest length, and it landed within 15% of this vault's measured median
// nearest-neighbour distance (84 vs 73). So the zones breathe with the vault's
// density and with the Link-distance slider instead of going blobby or grainy.
// Two widths, because one does not read: at a single width wide enough to fuse
// neighbouring branches the corridors claim the whole neighbourhood, and at one
// narrow enough to be honest the zone reads as a tangle of tubes rather than a
// territory. So the members get a BULB and the tree gets an ISTHMUS, and the
// isthmus only shows where the bulbs do not already touch.
const ZONE_BULB = () => 0.70 * baseDist() * forceMul.dist;   // fuses at typical spacing
const ZONE_LINK = () => 0.22 * baseDist() * forceMul.dist;   // half-width of a bridge
const ZONE_ALPHA = 0.13;   // a backdrop; the notes stay the figure

// Prim, O(m²) — the textbook array form, not the naive rescan: with the tree
// scanned per candidate it is O(m³) and cost 7.9ms a frame on the largest zone
// here, against 1.0ms for all 18 zones this way.
// ponytail: recomputed every frame, no cache. It is a millisecond and the
// positions move for most of the time a zone is on screen; memoize on a
// settled-layout flag only if a vault ten times this size ever complains.
function zoneMST(ms) {{
  if (ms.length < 2) return [];
  const n = ms.length, used = new Array(n).fill(false),
        best = new Array(n).fill(Infinity), from = new Array(n).fill(0), out = [];
  used[0] = true;
  for (let j = 1; j < n; j++) best[j] = (ms[0].x - ms[j].x) ** 2 + (ms[0].y - ms[j].y) ** 2;
  for (let k = 1; k < n; k++) {{
    let b = -1;
    for (let j = 0; j < n; j++) if (!used[j] && (b < 0 || best[j] < best[b])) b = j;
    used[b] = true;
    out.push([ms[from[b]], ms[b]]);
    for (let j = 0; j < n; j++) if (!used[j]) {{
      const d = (ms[b].x - ms[j].x) ** 2 + (ms[b].y - ms[j].y) ** 2;
      if (d < best[j]) {{ best[j] = d; from[j] = b; }}
    }}
  }}
  return out;
}}

// Members per zone, positions only — recomputed per frame because the layout
// is still moving for most of the time a zone is on screen.
function zoneMembers() {{
  const by = {{}};
  RAW_NODES.forEach(n => {{
    if (!(n.sgroup >= 0) || typeof n.x !== "number") return;
    (by[n.sgroup] = by[n.sgroup] || []).push(n);
  }});
  return by;
}}

// 2D only: onRenderFramePre draws under the nodes.
//
// ONE fill per zone, never a fill plus a stroke: the two widths would have to be
// two passes, and two passes double-composite where they meet — a darker lozenge
// ringing every member, an accidental density map. So the isthmus goes into the
// SAME path as the bulbs, as a quad rather than a stroked segment, and nonzero
// winding unions the lot. Everything inside one fill() composites exactly once,
// however much of it overlaps, which is what lets dense areas fuse flat.
//
// The quad is wound consistently (left normal, so its handedness does not follow
// the edge's direction) and the arcs are drawn anticlockwise to match: mixed
// winding would subtract the overlaps and punch holes where a bridge meets a bulb.
function drawZones(ctx) {{
  if (!showZones || !ZONES.length) return;
  const by = zoneMembers();
  const R = ZONE_BULB(), w = ZONE_LINK();
  ZONES.forEach(z => {{
    const members = by[z.id];
    if (!members) return;
    ctx.save();
    ctx.globalAlpha = ZONE_ALPHA;
    ctx.fillStyle = z.color;
    ctx.beginPath();
    members.forEach(n => {{
      ctx.moveTo(n.x + R, n.y);
      ctx.arc(n.x, n.y, R, 0, 2 * Math.PI, true);
    }});
    zoneMST(members).forEach(([a, b]) => {{
      const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len * w, ny = dx / len * w;
      ctx.moveTo(a.x + nx, a.y + ny);
      ctx.lineTo(b.x + nx, b.y + ny);
      ctx.lineTo(b.x - nx, b.y - ny);
      ctx.lineTo(a.x - nx, a.y - ny);
      ctx.closePath();
    }});
    ctx.fill();
    ctx.restore();
  }});
}}

const zoneEls = {{}};
function buildZoneLabels() {{
  const layer = document.getElementById("zone-labels");
  if (!layer || !ZONES.length) return;
  layer.innerHTML = ZONES.map(z =>
    '<div class="zone-label" data-id="' + z.id + '" style="color:' + z.color + '">' +
    escHtml(z.label) + '</div>').join("");
  layer.querySelectorAll(".zone-label").forEach(el => {{ zoneEls[el.dataset.id] = el; }});
}}

// In 3D a centroid behind the camera still projects to a point on screen — a
// mirrored one. A zone name planted over the wrong cluster is worse than no
// name, so ask which side of the camera it is on first. Plain arithmetic on
// camera.position and the controls' target: the bundle exposes no THREE.
function inFrontOfCamera(c) {{
  if (is2D()) return true;
  const cam = Graph.camera && Graph.camera();
  const ctr = Graph.controls && Graph.controls();
  if (!cam || !cam.position || !ctr || !ctr.target) return true;
  const p = cam.position, t = ctr.target;
  return (c.x - p.x) * (t.x - p.x) + (c.y - p.y) * (t.y - p.y) + (c.z - p.z) * (t.z - p.z) > 0;
}}

function positionZoneLabels() {{
  const by = zoneMembers();
  ZONES.forEach(z => {{
    const el = zoneEls[z.id];
    if (!el) return;
    const members = by[z.id];
    if (!showZones || !members) {{ el.style.display = "none"; return; }}
    let x = 0, y = 0, zc = 0;
    members.forEach(n => {{ x += n.x; y += n.y; zc += n.z || 0; }});
    x /= members.length; y /= members.length; zc /= members.length;
    // Snap to the member nearest the centroid. A scattered zone's centroid sits
    // in the hole between its pieces — now that the region no longer fills that
    // hole, a name parked there labels empty space, or worse, someone else's.
    // O(n) per zone per frame, n <= 92 here.
    let c = null, best = Infinity;
    members.forEach(n => {{
      const d = (n.x - x) ** 2 + (n.y - y) ** 2 + ((n.z || 0) - zc) ** 2;
      if (d < best) {{ best = d; c = {{ x: n.x, y: n.y, z: n.z || 0 }}; }}
    }});
    if (!c || !inFrontOfCamera(c)) {{ el.style.display = "none"; return; }}
    const s = Graph.graph2ScreenCoords(c.x, c.y, c.z);
    el.style.display = "block";
    el.style.transform =
      "translate(" + s.x + "px," + s.y + "px) translate(-50%,-50%)";
  }});
}}

// Its own rAF, not the render budget's: a camera orbit moves the labels without
// the simulation running, and this loop only writes transforms on a handful of
// divs — nothing is rendered. It exists only while the layer is on.
let zoneRaf = null;
function syncZoneLoop() {{
  if (showZones && zoneRaf === null) {{
    const step = () => {{ positionZoneLabels(); zoneRaf = requestAnimationFrame(step); }};
    zoneRaf = requestAnimationFrame(step);
  }} else if (!showZones && zoneRaf !== null) {{
    cancelAnimationFrame(zoneRaf);
    zoneRaf = null;
    positionZoneLabels();   // one last pass to hide them
  }}
}}

function updateZoneFilter() {{
  const cbZones = document.getElementById("cb-zones");
  const cbNodes = document.getElementById("cb-zone-nodes");
  if (cbZones) showZones = cbZones.checked;
  if (cbNodes) showNotes = cbNodes.checked;
  applyFilters();
  refreshPaint();   // the colour channel just changed which partition it carries
  syncZoneLoop();
  updateFocusBar();
}}

buildZoneLabels();

function filterCommunity(cid) {{
  activeCommunity = cid;
  document.querySelectorAll(".legend-item").forEach(el => el.classList.remove("active"));
  const el = cid === -2
    ? document.getElementById("legend-all")
    : document.querySelector(`[data-community="${{cid}}"]`);
  if (el) el.classList.add("active");
  applyFilters();
  updateFocusBar();
  if (cid !== -2) {{
    Graph.zoomToFit(400, 50, n => n.group === cid); // isolate: fit camera to the filtered set
    wake(600);
  }}
}}

// --- Communities legend: sort by size, toggling ascending <-> descending ----
let communitySortAsc = true;
function toggleCommunitySort() {{
  const box = document.getElementById("legend-box");
  const allItem = document.getElementById("legend-all");
  const items = Array.from(box.querySelectorAll(".legend-item[data-community]"));
  items.sort((a, b) => (+a.dataset.size - +b.dataset.size) * (communitySortAsc ? 1 : -1));
  items.forEach(el => box.insertBefore(el, allItem));
  document.getElementById("sort-communities").textContent = communitySortAsc ? "size ↑" : "size ↓";
  communitySortAsc = !communitySortAsc;
}}

// --- Search → ranked results → fly-to-focus -------------------------------
// Search by what people actually remember: title first, then path, then
// #tags, then the cluster they were browsing. Choosing a result flies the
// camera to the node and selects it — the graph answers "where is it", not
// just "is it somewhere in this cloud".
let results = [], selIdx = -1;

function scoreNode(n, q) {{
  if (n.type === 'ghost') return 0;
  const label = (n.label || '').toLowerCase();
  if (label === q)            return 5;
  if (label.startsWith(q))    return 4;
  if (label.includes(q))      return 3;
  if ((n.path || '').toLowerCase().includes(q)) return 2;
  if ((n.tags || []).some(t => t.toLowerCase().includes(q))) return 2;
  const cl = COMM_LABELS[n.group];
  if (cl && cl.toLowerCase().includes(q)) return 1;
  return 0;
}}

function renderResults(q) {{
  const box = document.getElementById("search-results");
  if (!q) {{ box.className = ""; box.innerHTML = ""; results = []; selIdx = -1; return; }}
  results = RAW_NODES
    .map(n => [scoreNode(n, q), n])
    .filter(p => p[0] > 0)
    .sort((a, b) => b[0] - a[0] || a[1].label.localeCompare(b[1].label))
    .slice(0, 12)
    .map(p => p[1]);
  selIdx = results.length ? 0 : -1;

  const esc = s => String(s).replace(/[&<>]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));
  const sub = n => {{
    const cl = COMM_LABELS[n.group];
    return cl ? '<em>' + esc(cl) + '</em>' : esc(n.path || n.type);
  }};
  box.innerHTML =
    '<div id="search-count">' + (results.length || 'no') +
      ' result' + (results.length === 1 ? '' : 's') + '</div>' +
    results.map((n, i) =>
      '<div class="result-item' + (i === selIdx ? ' sel' : '') +
        '" onclick="chooseResult(' + i + ')">' +
        '<span class="result-name">' + esc(n.label) + '</span>' +
        '<span class="result-sub">' + sub(n) + '</span>' +
      '</div>').join("");
  box.className = "open";
}}

// Shared selection path for tree clicks and search results: open the note view
// and fly the camera. Task 3 adds neighbour dimming here.
function chooseNode(node) {{
  if (!node) return;
  selectNode(node);
  focusNode(node);
  applyFocus(node.id);
}}

function chooseResult(i) {{
  const n = results[i];
  if (!n) return;
  selIdx = i;
  chooseNode(n);
}}

function moveSel(d) {{
  if (!results.length) return;
  selIdx = (selIdx + d + results.length) % results.length;
  document.querySelectorAll("#search-results .result-item")
    .forEach((el, i) => el.classList.toggle("sel", i === selIdx));
}}

function onSearch(q) {{ renderResults(q.trim().toLowerCase()); }}

function onSearchKey(e) {{
  if (e.key === "Enter")          {{ e.preventDefault(); chooseResult(selIdx); }}
  else if (e.key === "ArrowDown") {{ e.preventDefault(); moveSel(1); }}
  else if (e.key === "ArrowUp")   {{ e.preventDefault(); moveSel(-1); }}
  else if (e.key === "Escape")    {{ document.getElementById("search").value = ""; renderResults(""); }}
}}

// Fly to a node: a camera move in 3D, a pan + zoom in 2D. Coords (node.x/y/z)
// exist once the layout has run (cooldownTicks); before that they default to 0
// and the view simply recentres — harmless.
function focusNode(node) {{
  wake(1100);   // 900ms tween, either mode — it needs frames to actually fly
  if (is2D()) {{
    Graph.centerAt(node.x || 0, node.y || 0, 900);
    Graph.zoom(2.5, 900);
    return;
  }}
  const r = Math.hypot(node.x || 0, node.y || 0, node.z || 0) || 1;
  const k = 1 + 90 * 3 / r;
  Graph.cameraPosition(
    {{ x: (node.x || 0) * k, y: (node.y || 0) * k, z: (node.z || 0) * k }},
    node, 900
  );
}}

function selectNode(node) {{
  // Embedded in the web-UI iframe: hand off to the parent's drawer instead of
  // opening this internal metadata drawer (avoids two stacked drawers). A graph
  // click means "what is this, and what is around it", so it opens the parent's
  // CONTEXT mode, not the reader. Ghost nodes ride the same message: they have
  // no path, and context is the only mode that can say anything about them.
  if (window.parent !== window) {{
    window.parent.postMessage({{
      type:  "silica-open-context",
      path:  node.path || "",
      name:  node.label || "",
      ghost: node.type === "ghost",
    }}, "*");
    return;
  }}
  document.getElementById("drawer-title").textContent = node.label;
  document.getElementById("drawer-path").textContent  = node.path || "(ghost node)";
  const commText = (Number.isInteger(node.group) && node.group >= 0 && COMM_LABELS[node.group])
    ? ` · ${{COMM_LABELS[node.group]}}` : "";
  const betwText = node.betweenness ? ` · betweenness ${{node.betweenness}}` : "";
  document.getElementById("drawer-meta").textContent = `${{node.type}}${{commText}}${{betwText}}`;
  document.getElementById("drawer-out").textContent = outDeg[node.id] || 0;
  document.getElementById("drawer-in").textContent  = inDeg[node.id]  || 0;

  const tagsSection = document.getElementById("drawer-tags-section");
  const tags = node.tags || [];
  if (tags.length) {{
    document.getElementById("drawer-tags").innerHTML =
      tags.map(t => `<span class="tag">#${{t}}</span>`).join("");
    tagsSection.style.display = "flex";
  }} else {{
    tagsSection.style.display = "none";
  }}

  document.getElementById("drawer").classList.add("open");
}}

// (Direct clicks in the view get the same dim-non-neighbours treatment as
// tree/search picks, but skip focusNode's fly — the user is already looking at
// this spot, recentring would just be jarring. Bound in buildGraph, so a mode
// switch rebinds them.)

// The embedding page (chat + note-panel) tells us which note is open
// elsewhere — e.g. a link followed inside the note panel itself — so the
// graph mirrors it. Dim only, no camera move (same reasoning as above).
window.addEventListener("message", e => {{
  if (e.data && e.data.type === "silica-focus-path") {{
    applyFocus(NODE_BY_ID[e.data.path] ? e.data.path : null);
  }}
  // Same, for a SET of notes: the context drawer's concept cloud lights every
  // note carrying the clicked concept at once.
  if (e.data && e.data.type === "silica-focus-paths") {{
    applyFocus(e.data.paths || []);
  }}
  // The explore toolbar's note search asks us to *locate* a note: fly the
  // camera to it and dim to its neighbourhood, without opening the drawer
  // (selectNode would) — the user is searching the cloud, not inspecting yet.
  if (e.data && e.data.type === "silica-goto-path") {{
    const n = NODE_BY_ID[e.data.path];
    if (n) {{ focusNode(n); applyFocus(n.id); }}
  }}
  // The note drawer covers this frame's right edge, which is where the HUD is.
  // Its width comes with the message because the drawer is resizable — the
  // focus bar parks against its edge, not against a constant.
  if (e.data && e.data.type === "silica-host-drawer") {{
    document.body.classList.toggle("host-drawer-open", !!e.data.open);
    document.body.style.setProperty("--drawer-w", (e.data.width || 0) + "px");
  }}
}});

function closeDrawer() {{
  document.getElementById("drawer").classList.remove("open");
}}

document.getElementById("file-tree").addEventListener("click", e => {{
  const leaf = e.target.closest(".tree-note");
  if (leaf) chooseNode(NODE_BY_ID[leaf.dataset.id]);
}});

// --- Esc: back to the whole vault -------------------------------------------
// Undone in the order it was applied — focus first, then the community filter —
// so one press never throws away two decisions at once. The search box owns its
// own Escape (it clears the query), so it is skipped here.
document.addEventListener("keydown", e => {{
  if (e.key !== "Escape") return;
  if (e.target && e.target.id === "search") return;
  if (focusIds.length) {{ closeDrawer(); clearFocus(); }}
  else if (activeCommunity !== -2) {{ filterCommunity(-2); fitGraph(); }}
  // Last rung, because it is the one the banner promises Esc will undo: the
  // zone layer stays on, only the notes come back.
  else if (!showNotes) {{
    const cb = document.getElementById("cb-zone-nodes");
    if (cb) {{ cb.checked = true; updateZoneFilter(); }}
  }}
}});

// The loop sleeps between interactions, so anything the renderer itself
// services on a frame — the hover raycast, the cursor, drag, wheel zoom,
// control inertia — has to wake it first. Capture phase because force-graph
// reads its hover target at click time and only a rendered frame refreshes it:
// on touch there is no pointermove before the tap.
//
// The same events also cancel the pending auto-fit. The layout settles about a
// second and a half after the view opens, and until now the fit fired then
// regardless: if you had already grabbed the graph and moved somewhere, it
// yanked the camera back. Taking hold of the view means you have chosen your
// framing, so the fit stands down. Hovering is not taking hold, so pointermove
// only wakes the loop.
["pointerdown", "pointermove", "wheel", "touchstart"].forEach(t =>
  document.getElementById("graph-wrap").addEventListener(t, e => {{
    wake();
    if (e.type !== "pointermove") fitPending = false;
  }}, {{ capture: true, passive: true }}));
</script>
</body>
</html>"""


_EDGE_COLOR_GAP = "#c9a227"  # dim amber — "a bridge could go here, and doesn't"


def _gap_edges(nodes: list[dict], edges: list[dict], top_k: int = 5) -> list[dict]:
    """Top structural gaps as overlay edges between two area hubs.

    Reads: 'these two well-formed areas should probably connect, and don't.'
    Reuses graph_export.structural_gaps so the overlay agrees with the /graph
    report's Structural Gaps section node-for-node. Only the keys 3d-force-graph
    actually honours: from/to (linkSource/linkTarget), color.color (linkColor),
    and type (visibility toggle + particle accessor). The lib draws these as
    amber directional-particle links — WebGL has no dashed line, so motion, not
    a dash pattern, is what sets a gap apart. score rides along for the title map.
    """
    from silica.kernel.recall.graph_export import structural_gaps

    return [
        {
            "id":    f"gap{i}",
            "from":  hub_a,
            "to":    hub_b,
            "type":  "GAP",
            "color": {"color": _EDGE_COLOR_GAP},
            "score": score,
        }
        for i, (ca, cb, hub_a, hub_b, ie, score, _dens) in enumerate(
            structural_gaps(nodes, edges, top_k=top_k)
        )
    ]


def export_graph(
    output_path: str,
    folder: str = "",
    title: str = "Vault Graph",
    knn_k: int = 6,
) -> dict:
    """Build and write the unified vault-graph HTML to output_path.

    One build, two edge layers on a shared force layout:
      - the wikilink graph (EXTRACTED/AMBIGUOUS) — the explicit structure;
      - the embedding k-NN overlay (SIMILAR) — meaning-space proximity.
    Communities are Louvain on the WIKILINKS; the SIMILAR layer is a toggleable
    HUD overlay whose forces pull link-orphans (e.g. book extracts with no
    wikilinks) next to their semantic neighbours instead of leaving them
    floating. Structural-gap particles ride the wikilink layer.

    Reads the vendored JS first (fail fast on a packaging bug) and always inlines
    it, so the emitted file is self-contained/offline. Returns dict with keys:
    success, path, nodes, edges (wikilinks), similar (k-NN), communities,
    unresolved, gaps.
    """
    from silica.kernel.recall.graph_export import (
        build_graph_data,
        canvas_metrics,
        detect_communities,
        detect_semantic_partition,
        discourse_shape,
        knn_edges,
    )

    lib_js = _vendored_lib_js()  # fail fast before the graph build
    nodes, edges = build_graph_data(folder=folder)   # wikilink edges (the structure)
    sim = knn_edges(nodes, k=knn_k)                   # embedding k-NN overlay
    communities = detect_communities(nodes, edges)   # Louvain on the wikilinks
    # ...and the second partition, on the same nodes: Louvain on the k-NN. It
    # writes node["sgroup"] only, so the structural colours above stand.
    zones = detect_semantic_partition(nodes, sim)

    # Betweenness → node size (bottleneck nodes swell) + discourse-shape badge,
    # from one shared nx build over the wikilinks. Base size 16 for ordinary nodes.
    bet, giant = canvas_metrics(nodes, edges)
    if bet:
        for n in nodes:
            if n.get("type") != "ghost":
                b = round(bet.get(n["id"], 0.0), 4)
                n["betweenness"] = b
                n["size"] = round(16 + 40 * b, 2)
    discourse = discourse_shape(
        sum(1 for n in nodes if n.get("type") != "ghost"),
        giant, [c.size for c in communities],
    )

    # Gap particles ride the wikilink layer (they answer a linking question).
    gaps = _gap_edges(nodes, edges)
    html_out = render_html(
        nodes, edges + sim + gaps, communities, title=title, lib_js=lib_js,
        discourse=discourse, zones=zones,
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_out, encoding="utf-8")

    n_notes       = sum(1 for n in nodes if n.get("type") != "ghost")
    n_ghost       = sum(1 for n in nodes if n.get("type") == "ghost")
    n_links       = sum(1 for e in edges if e.get("type") == "EXTRACTED")
    n_similar     = len(sim)
    n_communities = len(communities)

    logger.info(
        "graph_export: wrote %s — %d notes, %d links, %d similar, %d clusters, "
        "%d zones, %d unresolved",
        out, n_notes, n_links, n_similar, n_communities, len(zones), n_ghost,
    )
    return {
        "success":     True,
        "path":        str(out.resolve()),
        "nodes":       n_notes,
        "edges":       n_links,
        "similar":     n_similar,
        # Two counts, two names: `communities` is the structural partition,
        # `zones` the semantic one. Never summed, never swapped (ADR-0023).
        "communities": n_communities,
        "zones":       len(zones),
        "unresolved":  n_ghost,
        "gaps":        len(gaps),
    }
