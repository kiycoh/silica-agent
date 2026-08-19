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

# The edge colours ride along so each legend swatch and the edges it stands for
# cannot drift apart: they were two literals of the same hex until one moved.
from silica.kernel.recall.graph_export import (
    _EDGE_COLOR_AMBIGUOUS,
    _EDGE_COLOR_AMBIGUOUS_PAPER,
    _EDGE_COLOR_EXTRACTED,
    _EDGE_COLOR_EXTRACTED_PAPER,
    _EDGE_COLOR_SIMILAR,
    _EDGE_COLOR_SIMILAR_PAPER,
    Community,
    Zone,
)

logger = logging.getLogger(__name__)

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
                "This is a packaging bug. Reinstall silica or re-vendor the assets (pinned "
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
    collapse, no JS); notes become
    <button type=button class="tree-note" data-id=ID>NAME</button>.

    A button and not a div: the tree is the primary route into a note, and as a
    click-only div every one of a vault's notes reported to the accessibility
    tree as `generic` and sat outside the tab order. Nothing else changes —
    the class, the data-id and the delegated `closest('.tree-note')` handlers
    are the same, and the CSS resets the button back to a row.
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
                f'<button type="button" class="tree-note" '
                f'data-id="{html.escape(nid, quote=True)}">'
                f"{html.escape(leaf)}</button>"
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

    lib_js is the vendored renderer bundle, embedded inline (offline-capable;
    export_graph always supplies it via _vendored_lib_js).
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
        f'<label class="filter-row" style="margin-top:4px" title="Embedding k-NN: notes pulled together by semantic similarity">'
        f'<input type="checkbox" id="cb-similar" checked onchange="updateEdgeFilter()">'
        f'<div class="dot-edge" style="--c:{_EDGE_COLOR_SIMILAR};--cp:{_EDGE_COLOR_SIMILAR_PAPER}"></div>Similar'
        f'<span class="ct">{n_similar}</span>'
        f'</label>'
    ) if n_similar else ""

    discourse_badge = (
        f'<div style="font-size:11px;color:var(--ash);letter-spacing:.04em;margin-bottom:6px" '
        f'title="Shape of the wikilink graph: how much of the vault sits in the largest connected '
        f'component and how evenly the clusters split it.">'
        f'discourse: <span style="color:var(--warn);font-weight:600">{html.escape(discourse)}</span></div>'
        if discourse else ""
    )

    # The gap list used to live here, under Edge types. It is a vault-level
    # worklist, not a key to what the canvas is painting, and in a legend it read
    # as neither. It now sits on the vault-level surface that already measured it
    # -- the Structural gaps card in metrics -- where each row carries the
    # bridging action. The amber GAP overlay and its checkbox stay: that IS a key.
    legend_items = "".join(
        f'<div class="legend-item" data-community="{c.id}" data-size="{c.size}" onclick="filterCommunity({c.id})">'
        f'<span class="dot" style="--c:{c.color};--cp:{c.color_paper or c.color}"></span>{html.escape(c.label)} '
        f'<span class="ct">{c.size}</span>'
        f'</div>\n'
        # Biggest first: the legend is read top-down, and the clusters that
        # carry the vault are the ones worth seeing without scrolling.
        for c in sorted(communities, key=lambda c: (-c.size, c.id))
    )

    comm_labels_json = json.dumps(
        {c.id: c.label for c in communities}, ensure_ascii=False
    ).replace("</", "<\\/")

    zones_json = json.dumps(
        [{"id": z.id, "label": z.label, "color": z.color,
          "color_paper": z.color_paper or z.color, "size": z.size} for z in zones],
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
        f'so a note can sit inside the region of one zone while its colour says it belongs to a '
        f'community that crosses the boundary. In 3D the zones are names only; the hulls are '
        f'drawn on the 2D canvas.">Semantic zones</div>'
        f'<label class="filter-row" title="Draw a hull and a name around each semantic zone. Note '
        f'colour does not change: it always means community, so the two partitions can be read '
        f'against each other in one frame.">'
        f'<input type="checkbox" id="cb-zones" onchange="updateZoneFilter()">'
        f'<span class="dot" style="--c:{zones[0].color if zones else "#565a77"};'
        f'--cp:{(zones[0].color_paper or zones[0].color) if zones else "#6b6f8c"};opacity:.5"></span>Show zones'
        f'<span class="ct">{len(zones)}</span>'
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

    # Two display settings, baked in at render time rather than read from the
    # frame: /graph regenerates the whole document per request anyway, so there
    # is nothing here for a live toggle to be live *against*.
    from silica.config import CONFIG

    particles_js = "true" if CONFIG.graph_particles else "false"
    shading_js = "true" if CONFIG.graph_shading else "false"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <!-- Which palette, decided before anything paints. Two callers, two answers:
       embedded in the app the parent puts ?theme= on the src, because the app's
       own preference (auto/dark/light) has already been resolved there and the
       two surfaces must not disagree across an iframe boundary; opened straight
       off disk there is no parent, so the OS answers. The reload is for that
       second case only — the app never reaches it, since changing the theme
       there rebuilds this document anyway. -->
  <script>
    (function () {{
      var q = new URLSearchParams(location.search).get("theme");
      var mq = window.matchMedia("(prefers-color-scheme: light)");
      var pinned = q === "light" || q === "dark";
      document.documentElement.dataset.theme =
        pinned ? q : (mq.matches ? "light" : "dark");
      if (!pinned) mq.addEventListener("change", function () {{ location.reload(); }});
    }})();
  </script>
  <script>{lib_js}</script>
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
      color-scheme:dark;
      --void:#0D0917;--slate:#120E21;--slate-2:#1F243A;
      --line:#292F45;--line-2:#3B4662;
      --frost:#EBEFF8;--text:#BAC4D8;--ash:#8E99B0;--ash-dim:#838DA7;
      --accent:#35C6E8;--violet:#5B4BD6;--warn:#E0A93B;
      --sans:"Lexend",system-ui,sans-serif;
      --lift:0 10px 28px -12px rgba(0,0,0,.7);
      /* HUD and focus bar float over the canvas, so their fill is the floor at
         near-opacity, not a surface step: whatever the graph paints has to stop
         at their edge. --g-* are the canvas's own neutrals, declared here so
         the legend swatches and the renderer read one source. */
      --hud-fill:rgba(10,13,20,.92);
      --g-ghost:#484867;--g-fallback:#565a77;
    }}
    /* Light: same ramp as the app shell, same warm paper, same reasoning — see
       static/app.css. Kept in sync by hand because this file must stay
       self-contained for file:// export; the app's block is the original. */
    :root[data-theme="light"]{{
      color-scheme:light;
      --void:#EFEAE0;--slate:#F5F1E9;--slate-2:#E9E2D4;
      --line:#D9D1C0;--line-2:#C6BCA8;
      --frost:#1A1815;--text:#3A362F;--ash:#5B554B;--ash-dim:#615B4F;
      --accent:#096275;--violet:#4B3BC0;--warn:#7A5305;
      --lift:0 8px 20px -12px rgba(58,44,20,.34);
      --hud-fill:rgba(245,241,233,.95);
      --g-ghost:#C9C4D6;--g-fallback:#6B6F8C;
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
    .ct{{color:var(--ash-dim);font-size:11px;margin-left:auto}}
    /* Swatches carry both values and CSS picks the live one. The alternative
       was a script rewriting inline styles after the theme resolves, i.e. a
       second copy of the palette that can disagree with the canvas.
       :where() on the theme prefix is load-bearing, not style: an unwrapped
       :root[data-theme=…] .dot scores three class-level selectors and beats
       .dot.ghost, which paints the ghost swatch --ash-dim on paper and nothing
       like what the canvas draws. Zeroed, the theme rule sits at .dot's own
       weight and wins on order, while the two state swatches below still win
       on specificity — which is the cascade this actually wants. */
    .dot,.dot-edge{{background:var(--c,var(--ash-dim))}}
    :where(:root[data-theme="light"]) .dot,
    :where(:root[data-theme="light"]) .dot-edge{{background:var(--cp,var(--c,var(--ash-dim)))}}
    .dot.ghost{{background:var(--g-ghost)}}
    .dot.all{{background:var(--g-fallback)}}
    /* Focus banner — top-right, tucked left of whatever owns that corner: the
       HUD (216px) when the drawer is shut, the drawer's edge when it is open,
       so the open note's title reads directly above it. A filtered graph that
       does not say it is filtered is lying about the vault. */
    #focus-bar{{position:absolute;top:10px;right:236px;z-index:5;display:none;
                max-width:min(420px,calc(100% - 250px));padding:6px 10px;
                font-size:11px;color:var(--ash);letter-spacing:.04em;
                background:var(--hud-fill);border:1px solid var(--line-2);
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
    /* Note names in 3D. Below the zone names, which name a whole region and must
       win where the two overlap. The halo is the same trick: the canvas behind a
       label is whatever colour the cluster happens to be. */
    #node-labels{{position:absolute;inset:0;z-index:3;pointer-events:none;overflow:hidden}}
    .node-label{{position:absolute;top:0;left:0;display:none;white-space:nowrap;
                 font-size:11px;color:var(--frost);
                 text-shadow:0 0 6px var(--void),0 0 3px var(--void),0 0 1px var(--void)}}
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
          background:var(--hud-fill);border:1px solid var(--line-2)}}
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
    #file-tree .tree-note{{width:calc(100% - 12px)}}
    /* button reset: the row is a <button> so it is reachable and announces as
       one, and everything below puts it back to looking like a row */
    .tree-note{{display:block;text-align:left;font:inherit;background:none;box-sizing:border-box;
               color:var(--ash);cursor:pointer;padding:2px 6px;border:0;border-left:2px solid transparent;
               white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .tree-note:focus-visible{{outline:1px solid var(--accent);outline-offset:-1px}}
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
    /* 3D hover. `.float-tooltip-kap` is the bundle's own element — the class
       comes from its float-tooltip dependency, NOT the `.graph-tooltip` the
       docs mention, which is why this is pinned to the vendored version and
       why it shouts: the bundle ships its own stylesheet. What it ships is
       #eee sans-serif on a 60% black 3px-rounded box, three decisions this app
       does not make anywhere else. Squared off, moved onto the raised plane
       with its hairline, and typeset like the rest of the chrome.
       `transform` is deliberately NOT overridden: the bundle rewrites it every
       frame with a computed percentage that keeps the tooltip on screen near
       the edges, and a fixed translate would throw that away. */
    .float-tooltip-kap{{background:var(--slate-2)!important;
                    border:1px solid var(--line-2)!important;
                    border-radius:0!important;padding:7px 10px!important;
                    font-family:var(--sans)!important;font-size:12px!important;
                    color:var(--frost)!important;box-shadow:var(--lift)!important}}
    .g3d-tip{{display:flex;flex-direction:column;gap:3px;text-align:left}}
    .g3d-tip b{{font-weight:600;color:var(--frost)}}
    /* the one place uppercase is allowed: a chrome micro-label, never prose */
    .g3d-tip i{{font-style:normal;font-size:10px;color:var(--ash-dim);
                text-transform:uppercase;letter-spacing:.14em}}
    /* The second partition gets its own line and the word ZONE, and nothing
       else: dimming it to rank it below the community was measured at 3.23:1
       on --slate-2, under the 4.5:1 a 10px label owes. The prefix ranks it. */
  </style>
</head>
<body>

<div id="sidebar">
  <h1>&#11041; {title}</h1>

  <!-- Every number states its own rule, not its name: a count whose denominator
       you cannot name is a number you cannot act on. -->
  <div class="stat-grid">
    <div class="stat" title="Files in the graph. Unresolved link targets are not files and are counted under Unresolved instead."><div class="val">{n_notes}</div><div class="lbl">Notes</div></div>
    <div class="stat" title="Wikilinks whose target file exists, counted once per direction between a pair of notes, not once per occurrence in the text."><div class="val">{n_extracted}</div><div class="lbl">Links</div></div>
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
  <!-- Note names in 3D ride the same DOM path as the zone names above, for the
       same reason: one positioning routine serves both renderers, and there is
       no THREE to build a sprite with. 2D keeps painting its labels on canvas —
       there the zoom IS the level of detail, and the canvas draw is cheaper. -->
  <div id="node-labels"></div>
  <div id="focus-bar"></div>
  <div id="hud">
    <!-- Renderer first: it is the only control that changes what every other
         row in this panel means (the rings below are a 2D-only channel), so it
         reads before them, not after. -->
    <div>
      <div class="section-title" style="margin-bottom:6px">Renderer</div>
      <div class="seg" id="mode-toggle">
        <button type="button" data-mode="3d" onclick="setMode('3d')">3D</button>
        <button type="button" data-mode="2d" onclick="setMode('2d')">2D</button>
      </div>
    </div>

    <div>
      <div class="section-title" style="margin-bottom:8px">Edge types</div>
      <label class="filter-row" title="A [[wikilink]] whose target file exists.">
        <input type="checkbox" id="cb-extracted" checked onchange="updateEdgeFilter()">
        <div class="dot-edge" style="--c:{_EDGE_COLOR_EXTRACTED};--cp:{_EDGE_COLOR_EXTRACTED_PAPER}"></div>
        Resolved
        <span class="ct">{n_extracted}</span>
      </label>
      <label class="filter-row" style="margin-top:4px" title="A [[wikilink]] pointing at a name no file carries: the link is written, the note is not.">
        <input type="checkbox" id="cb-ambiguous" onchange="updateEdgeFilter()">
        <div class="dot-edge" style="--c:{_EDGE_COLOR_AMBIGUOUS};--cp:{_EDGE_COLOR_AMBIGUOUS_PAPER}"></div>
        Unresolved
        <span class="ct">{n_ambiguous}</span>
      </label>
      <label class="filter-row" style="margin-top:4px" title="Well-formed areas with no links between them: a bridge could go here">
        <input type="checkbox" id="cb-gaps" checked onchange="updateEdgeFilter()">
        <div class="dot-edge" style="--c:{_EDGE_COLOR_GAP};--cp:{_EDGE_COLOR_GAP_PAPER}"></div>
        Structural gaps
        <span class="ct">{n_gaps}</span>
      </label>
      {similar_row}
    </div>

    <!-- The semantic partition sits directly under the edge types because it is
         read against them: the zones are Louvain over the SIMILAR edges listed
         one row up, and the structural Communities further down are Louvain over
         the resolved ones. Each grouping now touches the edge set it came from. -->
    {zone_section}

    <!-- Colour already carries the community, so node STATE rides a second
         channel: a ring. Only the two states that had no marking at all get
         one; ghost keeps the rendering it already had, and the swatch here
         shows what each actually looks like on the canvas. 2D only — in 3D a
         ring means rebuilding every node's geometry, and size already
         separates hubs there. -->
    <div id="state-legend">
      <div class="section-title" style="margin-bottom:6px" title="What each node IS, on a channel the community colour is not using.">Node state</div>
      <div class="filter-row" title="Betweenness in the top tenth of the notes that have any: the crossings the vault routes through.">
        <span class="ring" style="border-color:var(--accent)"></span>Hub<span class="ct" id="st-hub"></span>
      </div>
      <div class="filter-row" title="The note exists and no resolved wikilink points at it. Reachable from the file tree, unreachable from the vault.">
        <span class="ring" style="border-color:var(--ash-dim)"></span>Orphan<span class="ct" id="st-orphan"></span>
      </div>
      <div class="filter-row" title="Something links here and no file carries the name. Already unlit and undersized in the view, so it takes no ring.">
        <span class="dot ghost" style="border-radius:50%"></span>Ghost<span class="ct" id="st-ghost"></span>
      </div>
    </div>

    <div>
      <div class="section-title" style="margin-bottom:6px;display:flex;align-items:center;justify-content:space-between"
           title="Louvain over the resolved wikilinks: the structural partition, the vault as you linked it. Not the semantic zones: the two groupings are independent and share no colour.">
        Communities
        <span id="sort-communities" style="color:var(--ash);cursor:pointer;font-size:11px;letter-spacing:0;text-transform:none"
              onclick="toggleCommunitySort()" title="sort by size">size &#8595;</span>
      </div>
      {discourse_badge}
      <div id="legend-box">
{legend_items}      <div class="legend-item active" id="legend-all" onclick="filterCommunity(-2)">
          <span class="dot all"></span>Show all
        </div>
      </div>
    </div>

    <div>
      <div class="section-title" style="display:flex;align-items:center;justify-content:space-between">
        Forces
        <span style="color:var(--ash);cursor:pointer;font-size:11px;letter-spacing:0;text-transform:none"
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
// Only the label is indexed. There was a ZONE_COLOR map beside it, read by
// nodeColor alone; now that the notes keep their community colour it has no
// reader left — the hulls and the zone names take z.color straight off ZONES.
const ZONE_LABEL = {{}};
ZONES.forEach(z => {{ ZONE_LABEL[z.id] = z.label; }});
// The zone's hue for the live floor. Both rides on the zone (see Zone in
// graph_export) because the phase shift that keeps zone i off community i is
// declared there, and recomputing it here would be a second place to break it.
const zoneColor = z => (LIGHT && z.color_paper) || z.color;

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
  // 3D links live in the merged LineSegments: one buffer rewrite against the
  // full link digest (9k material rebuilds) the accessor re-pass would cost.
  // With PARTICLES on the lib still owns the photon carriers, so those keep
  // the re-pass beside the merge.
  if (is2D() || PARTICLES) Graph.linkColor(Graph.linkColor());
  if (!is2D()) repaintLinkSeg();
  wake(120);
}}

// --- the canvas's own palette ----------------------------------------------
// CSS tokens stop at the edge of the canvas: a WebGL material and a 2D
// fillStyle both need a literal, and both are set on a hot path where reading
// getComputedStyle per node is not an option. So the two sets live here, picked
// once at load. Every value is the light twin of the one beside it, chosen
// against the floor it lands on rather than by inverting a channel.
const LIGHT = document.documentElement.dataset.theme === "light";
const GP = LIGHT ? {{
  dim: '#DCD5C7',          // a node filtered out of focus: toward the paper
  ghost: '#C9C4D6',        // unresolved link — the faintest thing the floor holds
  fallback: '#6B6F8C',     // no community
  label: '#1A1815', ghostLabel: '#615B4F',
  linkDim: '#E5DFD2',
  bg: '#EFEAE0', bgHex: 0xEFEAE0,
  ringHub: '#096275', ringOrphan: '#615B4F',
}} : {{
  dim: '#1d192f',
  ghost: '#484867',        // unlit, never black
  fallback: '#565a77',
  label: '#EBEFF8', ghostLabel: '#838DA7',
  linkDim: '#141221',
  bg: '#0D0917', bgHex: 0x0D0917,
  ringHub: '#35C6E8', ringOrphan: '#838DA7',
}};

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
  if (n._dim) return GP.dim;
  if (n.type === 'ghost') return GP.ghost;
  // The node's colour is the STRUCTURAL community, always, whatever else is on
  // screen. It used to hand the channel over to the semantic partition while
  // the zone layer was up, which read as a bug and was one: ADR-0023 says the
  // two partitions "coesistono e non si sostituiscono mai" and "non condividono
  // ne' chiave colore ne' spazio di id", and a channel that swaps between them
  // is a substitution by definition. The semantic layer owns the hulls and the
  // names; it does not get to repaint the notes.
  const c = n.color || {{}};
  return (LIGHT ? c.background_paper || c.background : c.background) || GP.fallback;
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
const STATE_RING = {{ hub: GP.ringHub, orphan: GP.ringOrphan }};

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
// --- When the layout is allowed to stop ------------------------------------
// A tick COUNT is the wrong gate and was measurably too small. d3 decays alpha
// by a fixed 2.28% per tick regardless of graph size, so it reaches the 0.001
// that d3 itself calls converged at tick 300, always. The old budget
// (100 + min(200, N/10)) gave 155 ticks on a 550-note vault: alpha 0.028, still
// 28x above convergence. The view froze on a layout that was genuinely half
// unfolded — not a perception problem, an arithmetic one.
//
// So hand the gate to the physics. Both bundles already check it and neither
// had it switched on (d3AlphaMin defaults to 0 = disabled):
//
//   ++cntTicks > cooldownTicks || now - startTickTime > cooldownTime
//     || d3AlphaMin > 0 && forceLayout.alpha() < d3AlphaMin   ->  onEngineStop
//
// cooldownTicks and cooldownTime both go to Infinity on purpose. Alpha decays
// deterministically, so it ALWAYS converges; a tick or wall-clock ceiling can
// no longer protect against anything, it can only re-introduce the early cut
// through a second door. (cooldownTime's 15s default would have done exactly
// that on the slower renderer.)
const ALPHA_MIN = 0.001;      // d3's own convergence point, reached at tick ~300

// warmupTicks runs the same tick() in a plain loop with nothing painted, and
// consumes the SAME alpha schedule — the bundle's warmup loop carries the
// d3AlphaMin check too. So warmup and the animated tail split one 300-tick
// budget, and the split is what you actually watch.
//
// Split per renderer, because the two pay completely different prices for a
// painted tick: both libs advance the layout exactly one tick per RENDERED
// frame, and the 2D canvas (553 arcs + labels + 4566 strokes per frame, all
// CPU) runs that at roughly 19 ticks/s against WebGL's 60. An even split makes
// 2D take three times as long to settle for the identical layout. Weighting the
// warmup by the renderer buys back a comparable settle time in both.
const WARMUP_TICKS = () => is2D() ? 240 : 150;

// --- Layout cache: pay the 300 ticks once, not once per load ----------------
// The honest gate above costs about twice the ticks of the wrong one. Cached
// positions hand that back from the second load on: seed x/y/z from the last
// settled layout and raise the decay so the sim only has to rerelax, ~66 ticks,
// which the warmup then swallows whole. The graph opens already settled.
//
// One slot per renderer, holding its own fingerprint — a slot per fingerprint
// would accumulate a dead entry for every force-slider position ever dragged.
// The fingerprint covers the node set AND the force multipliers: a layout
// settled under different forces is not this layout.
const LAYOUT_KEY = "silica-graph-layout";
const FAST_DECAY = 0.1;   // alpha 1 -> ALPHA_MIN in ~66 ticks
const NODE_FP = (() => {{
  let h = RAW_NODES.length;
  for (const n of RAW_NODES)
    for (let i = 0; i < n.id.length; i++) h = (h * 31 + n.id.charCodeAt(i)) | 0;
  return h;
}})();
const layoutFp = () => NODE_FP + ":" + forceMul.repel.toFixed(3) + ":" +
  forceMul.dist.toFixed(3) + ":" + forceMul.center.toFixed(2);

// True when the positions were seeded, i.e. the sim only has to rerelax.
function loadLayout() {{
  let saved = null;
  try {{ saved = JSON.parse(localStorage.getItem(LAYOUT_KEY + "-" + mode)); }} catch (e) {{}}
  if (!saved || saved.fp !== layoutFp() || !saved.pos) return false;
  // Positions are keyed by id, not by index: a vault that gained a note between
  // two loads has a different fingerprint anyway, but by-index would silently
  // scatter every node past the insertion point if that ever stopped holding.
  let hit = 0;
  RAW_NODES.forEach(n => {{
    const p = saved.pos[n.id];
    if (!p) return;
    n.x = p[0]; n.y = p[1]; n.z = p[2];
    hit++;
  }});
  return hit === RAW_NODES.length;
}}

function saveLayout() {{
  const pos = {{}};
  // Rounded to whole graph units: the layout is thousands of units across, the
  // decimals are noise, and they triple the size of what goes into storage.
  RAW_NODES.forEach(n => {{
    pos[n.id] = [Math.round(n.x || 0), Math.round(n.y || 0), Math.round(n.z || 0)];
  }});
  try {{
    localStorage.setItem(LAYOUT_KEY + "-" + mode,
                         JSON.stringify({{ fp: layoutFp(), pos: pos }}));
  }} catch (e) {{ /* quota or blocked storage -> next load just re-settles */ }}
}}

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
  ctx.fillStyle = n.type === "ghost" ? GP.ghostLabel : GP.label;
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

// --- Edge weight: what you wrote outranks what was inferred -----------------
// Every edge already declares its own opacity and width (graph_export), and
// both were dead: the 3D branch drew every link at the bundle's flat 0.2 and
// the 2D branch at a flat width of 1. So the two layers that matter most —
// 1341 wikilinks you wrote and 2718 similarities a model guessed — arrived on
// screen at identical weight, separated by hue alone.
//
// The alpha lives in the colour string because that is the only per-link seam
// the 3D bundle has: linkOpacity is global, but it MULTIPLIES the alpha it
// parses out of an rgba(), so setting it to 1 makes the per-edge alpha the
// final word. 2D reads the same string straight into strokeStyle.
const LINK_ALPHA_2D = 0.55;   // see the comment on its use below
const EDGE_FALLBACK = LIGHT ? "{_EDGE_COLOR_EXTRACTED_PAPER}" : "{_EDGE_COLOR_EXTRACTED}";

// Memoised because 2D calls this for every visible link on every frame, and
// building the string costs ~1ms per frame across 4.6k edges — measured, not
// assumed. An edge's colour depends on nothing but its own (fixed) data, its
// dim state and the renderer, so those two are the whole cache key.
function linkPaint(l) {{
  const key = (l._dim ? "d" : "") + (is2D() ? "2" : "3");
  if (l.__paintKey === key) return l.__paint;
  let out;
  if (l._dim) out = GP.linkDim;
  else {{
    const c = l.color || {{}};
    const hex = (LIGHT ? c.paper || c.color : c.color) || EDGE_FALLBACK;
    const n = parseInt(hex.slice(1), 16);
    // 2D stacks every edge on one flat plane with no depth to thin the far ones
    // out, so the same alphas that read as structure in 3D read as a mat there.
    // One scale factor rather than a second table: the RANK is what is worth
    // preserving between the two views, not the absolute values.
    const a = (c.opacity == null ? 0.6 : c.opacity) * (is2D() ? LINK_ALPHA_2D : 1);
    out = "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," +
          (n & 255) + "," + a.toFixed(3) + ")";
  }}
  l.__paintKey = key; l.__paint = out;
  return out;
}}

// --- 3D: every link in ONE LineSegments -------------------------------------
// The bundle builds a separate THREE.Line per link: one draw call and one
// per-tick geometry write each, which on this vault is ~9k of either and
// ~36ms a frame before a single sphere is drawn — measured, and the reason
// the 3D view lagged. So the lib's lines are never shown (linkVisibility
// false) and never positioned (linkPositionUpdate returns true), and this
// layer draws every visible link as one LineSegments: one object, one draw
// call, two vertices per link, RGBA per vertex so the per-edge alpha rank
// survives the merge. Fog applies to the shared material like it did to the
// per-link ones.
//
// No THREE global exists (same constraint as faceteNodes below), and no
// LineSegments instance exists to steal a constructor from. A Line instance
// does — and the renderer branches on the isLineSegments FLAG, not the class,
// so a Line wearing the flag renders as GL_LINES. The donor is the ONE line
// the lib is allowed to build: its visibility accessor admits RAW_EDGES[0]
// only, and an accessor that returns false makes the lib skip creating the
// object outright (verified, not assumed) — so the whole per-link fleet, its
// 9k materials included, is simply never born. The donor itself stays
// degenerate at the origin (linkPositionUpdate never lets it be positioned)
// and draws nothing.
//
// Particles are the one carve-out. A photon group is only created for a link
// whose line object exists (verified: admit the link, photons appear; skip
// it, they never do), so the links that may carry photons — GAP always,
// SIMILAR for the drift — stay lib-owned whenever PARTICLES is on: real
// lines, lib-positioned, excluded from the merge. PARTICLES off (the
// default) merges everything behind the one donor.
const libOwnsLink = l => PARTICLES && (l.type === "GAP" || l.type === "SIMILAR");
let LinkSeg = null;   // {{ obj, pos, col, colorCls, edges }}

// The same conversion Color.setStyle applies (sRGB into the working space),
// without setStyle's per-call "alpha will be ignored" console warning.
function segRGBA(Color, s) {{
  let r, g, b, a = 1;
  if (s[0] === "#") {{
    const n = parseInt(s.slice(1), 16);
    r = (n >> 16) & 255; g = (n >> 8) & 255; b = n & 255;
  }} else {{
    const p = s.match(/rgba?\(([^)]+)\)/)[1].split(",").map(Number);
    r = p[0]; g = p[1]; b = p[2];
    if (p.length > 3) a = p[3];
  }}
  const c = new Color().setRGB(r / 255, g / 255, b / 255, "srgb");
  return [c.r, c.g, c.b, a];
}}

function buildLinkSeg() {{
  const line = RAW_EDGES.length && RAW_EDGES[0].__lineObj;
  if (!line) return;               // digest not run yet; next frame retries
  const E = RAW_EDGES.length;
  const pos = new Float32Array(E * 6);
  const col = new Float32Array(E * 8);
  const Attr = line.geometry.getAttribute("position").constructor;
  const geom = new (line.geometry.constructor)();
  geom.setAttribute("position", new Attr(pos, 3));
  geom.setAttribute("color", new Attr(col, 4));
  const mat = new (line.material.constructor)({{ vertexColors: true, transparent: true }});
  const obj = new (line.constructor)(geom, mat);
  obj.isLineSegments = true;       // the renderer reads the flag, not the class
  obj.type = "LineSegments";
  obj.frustumCulled = false;       // positions churn every tick; skip bounds
  obj.raycast = () => {{}};        // the pointer belongs to the nodes
  (line.parent || Graph.scene()).add(obj);
  LinkSeg = {{ obj, pos, col, colorCls: line.material.color.constructor, edges: [] }};
  repaintLinkSeg();
}}

// Colours + the visible set — only when they change (filters, focus, dim),
// which is what refreshPaint/applyFilters call in place of the accessor
// re-pass that used to rebuild 9k materials.
function repaintLinkSeg() {{
  if (!LinkSeg) return;
  const edges = LinkSeg.edges = RAW_EDGES.filter(e => !e._hidden && !libOwnsLink(e));
  for (let i = 0; i < edges.length; i++) {{
    const c = segRGBA(LinkSeg.colorCls, linkPaint(edges[i]));
    LinkSeg.col.set(c, i * 8);
    LinkSeg.col.set(c, i * 8 + 4);
  }}
  LinkSeg.obj.geometry.setDrawRange(0, edges.length * 2);
  LinkSeg.obj.geometry.getAttribute("color").needsUpdate = true;
  writeLinkSegPositions();
}}

function writeLinkSegPositions() {{
  const {{ pos, edges, obj }} = LinkSeg;
  for (let i = 0; i < edges.length; i++) {{
    const a = NODE_BY_ID[edges[i].from], b = NODE_BY_ID[edges[i].to];
    const o = i * 6;
    pos[o]     = a.x || 0; pos[o + 1] = a.y || 0; pos[o + 2] = a.z || 0;
    pos[o + 3] = b.x || 0; pos[o + 4] = b.y || 0; pos[o + 5] = b.z || 0;
  }}
  obj.geometry.getAttribute("position").needsUpdate = true;
}}

// Per frame beside the label layers (the loop always runs in 3D): build once
// the lib's digest has produced a carrier, then follow the nodes — but only
// while they can move. A settled, unwoken graph skips the write entirely.
function linkSegStep() {{
  if (is2D()) return;
  if (!LinkSeg) {{ buildLinkSeg(); return; }}
  if (!simRunning && performance.now() >= awakeUntil) return;
  writeLinkSegPositions();
}}

// --- 3D: the same crystal the mark is cut from ------------------------------
// PARTICLES/SHADING come from the settings panel (Display). Both off is the
// bundle's own look: smooth lit spheres, no fog, still edges.
const PARTICLES = {particles_js};
const SHADING = {shading_js};
// Everything here is a bundle default undone. Out of the box the scene is lit
// by a GRAY ambient at full strength plus a white key, the spheres are smooth-
// shaded, and there is no fog — which is a fine neutral rig for a demo and the
// wrong one for this palette. A substrate built as blue-black crystal, lit flat
// by an office light, renders as plastic beads: that, not the library, is what
// made this view read as a stock 3D graph while the rest of the app did not.
//
// No THREE global exists to build with (see the DOM label layers below for the
// same constraint). It is not needed: the bundle hands out the scene, and every
// constructor this wants is reachable from an object already inside it.
// Vendored+pinned bundles are what makes that safe to lean on.
//
// Two rigs, because light is not the same scene with the background swapped.
// The community colours already dropped ~28 lightness points to survive a paper
// floor (_COMMUNITY_LIGHTNESS_ON_PAPER), and a multiplying ambient at crystal
// strength takes them the rest of the way to mud. So on paper the ambient rises
// toward white and the key comes down: the facets still split, but the light
// falling on them is a room's, not a lamp inside the stone. Fog travels with
// the floor either way — it is the same depth cue, sold into white instead of
// out of black.
const CRYSTAL = LIGHT ? {{
  ambient: 0xEFEAE0, ambientI: 1.55,
  key: 0xFFF6E4, keyI: 1.35,      // warm key, matching the paper it lands on
  fogNear: 0.55, fogFar: 2.30,    // paper fog swallows less: white on white
  fov: 42,
}} : {{
  // Ambient MULTIPLIES each node's own colour, so this is the one value that
  // can silently destroy the community channel: a saturated violet here turned
  // every community violet, and a bright one flattened the facets back into
  // spheres. Low enough that lit and unlit facets separate, unsaturated enough
  // that the hue survives into the unlit half.
  ambient: 0x9B93C6, ambientI: 1.05,
  key: 0xDCE8FF, keyI: 2.40,      // cool key, off-axis so facets split
  fogNear: 0.45, fogFar: 2.10,    // as fractions of the camera's distance
  fov: 42,                        // 50 is the wide-angle look; this is a lens
}};

// Re-facet after every wholesale material rebuild. refreshPaint triggers one on
// every focus change (see its comment), so this cannot be a one-shot. The flag
// rides on the material itself: a rebuilt material simply arrives without one,
// which makes the repeat cost a 1150-entry loop that writes nothing.
// __threeObj is the bundle's own per-node handle — 18x cheaper than walking the
// scene, which also carries ~2700 particle meshes that want none of this.
function faceteNodes() {{
  let Color = null;
  for (const n of RAW_NODES) {{
    const m = n.__threeObj && n.__threeObj.material;
    if (!m) continue;
    if (!Color) Color = m.color.constructor;
    if (m.__silica) continue;
    // A 6-segment sphere stops pretending to be round and becomes a facet
    // cluster. The geometry never changed; it just stopped being smoothed.
    m.flatShading = true;
    m.needsUpdate = true;
    m.__silica = 1;
  }}
  return Color;
}}

function styleScene() {{
  if (!SHADING || is2D() || !Graph || !Graph.scene) return;
  const sc = Graph.scene();
  const Color = faceteNodes();
  if (!Color) return;
  // Per-light flags, not one scene-level one. The bundle populates its scene
  // lazily: at construction and immediately after graphData it holds only the
  // background mesh, and the lights and the node Group appear some frames
  // later. A single "already styled" mark set the moment the nodes showed up
  // could therefore land in a window where the lights had not, and then said
  // done forever — which is exactly what it did. Flagging each light drops the
  // ordering assumption: one that arrives late, or gets replaced, is styled on
  // the next frame instead of never. Direct children only, so this is four
  // entries rather than a walk over ~3900 meshes.
  for (const o of sc.children) {{
    if (!o.isLight || o.__silica) continue;
    if (o.type === "AmbientLight") {{
      o.color = new Color(CRYSTAL.ambient); o.intensity = CRYSTAL.ambientI;
    }} else if (o.type === "DirectionalLight") {{
      o.color = new Color(CRYSTAL.key); o.intensity = CRYSTAL.keyI;
      o.position.set(0.55, 1, 0.75);
    }} else continue;
    o.__silica = 1;
  }}
  if (sc.__silicaLit) return;
  // Linear fog, not exponential. Exponential density is anchored to world units,
  // and the camera here travels three orders of magnitude between a fitted vault
  // and a single note: one density either does nothing up close or swallows the
  // whole graph the moment you pull back — which is exactly what it did. Linear
  // near/far ride the camera instead, so the depth cue reads the same at every
  // zoom. No Fog constructor is reachable (no instance exists to borrow one
  // from), but the renderer reads the shape, not the class.
  sc.fog = {{ isFog: true, isFogExp2: false, name: "",
             color: new Color(GP.bgHex), near: 1, far: 2 }};
  const cam = Graph.camera();
  cam.fov = CRYSTAL.fov;
  cam.updateProjectionMatrix();
  sc.__silicaLit = 1;
}}

// Per frame, beside the label layers: the fog slab has to follow the camera or
// it is just a fixed band the graph flies through.
function fogStep() {{
  if (!SHADING || is2D() || !Graph || !Graph.scene) return;
  const sc = Graph.scene();
  if (!sc.fog) return;
  const p = Graph.camera().position;
  const d = Math.hypot(p.x, p.y, p.z);
  sc.fog.near = d * CRYSTAL.fogNear;
  sc.fog.far = d * CRYSTAL.fogFar;
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
// over the graph; IDLE_FPS when only particles still move; nothing when even
// those are off. The failure mode is chosen — a missed wake signal leaves the
// loop running, i.e. exactly today's behaviour, never a frozen view.
const IDLE_FPS = 20;
const WAKE_MS = 1200;   // trackball inertia keeps the camera moving after pointerup
let awakeUntil = 0, simRunning = true, idleTick = null, sleepTimer = null;

// Both particle layers keep the canvas awake, so both have to be asked. The
// similarity layer is on by default, which means the idle tick now effectively
// always runs — that is the price of the drift, and it is the reason the drift
// rides the 20fps budget instead of the full frame rate.
const particlesMoving = () => PARTICLES && RAW_EDGES.some(e =>
  !e._dim && !e._hidden &&
  ((e.type === "GAP" && showGaps) || (e.type === "SIMILAR" && showSimilar)));

function renderBudget() {{
  if (!Graph) return;
  clearInterval(idleTick); idleTick = null;
  if (simRunning || performance.now() < awakeUntil) {{ Graph.resumeAnimation(); return; }}
  Graph.pauseAnimation();
  // resumeAnimation() runs one cycle synchronously and re-arms rAF;
  // pauseAnimation() cancels the re-arm. Net effect: exactly one frame.
  if (particlesMoving()) idleTick = setInterval(
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
    LinkSeg = null;   // died with the scene; the next 3D build remakes it
  }}
  const G = is2D() ? new ForceGraph(el) : new ForceGraph3D(el);
  // Seeded positions and the decay that goes with them, decided before the data
  // lands: on a cache hit the whole rerelaxation fits inside the warmup, so the
  // graph appears already settled instead of unfolding a layout you have
  // watched unfold before.
  const seeded = loadLayout();
  G.warmupTicks(WARMUP_TICKS())
    .d3AlphaMin(ALPHA_MIN)
    .d3AlphaDecay(seeded ? FAST_DECAY : 0.0228)
    .cooldownTicks(Infinity)   // alpha is the gate; see ALPHA_MIN
    .cooldownTime(Infinity)
    .backgroundColor(GP.bg)
    .linkSource("from").linkTarget("to")
    .nodeVal("size")
    .nodeColor(nodeColor)
    .linkColor(linkPaint)
    // Structural gaps have no dash in WebGL — mark them by motion instead: amber
    // particles stream along the absent bridge. The similarity layer gets the
    // same treatment at a fraction of the weight: one particle instead of two,
    // pre-dimmed almost into the background, and six times the speed, so two
    // thousand of them read as a faint drift through the semantic neighbourhood
    // rather than as two thousand moving dots. Both stop when the link is dimmed
    // or filtered out, so focus mode stays quiet and unticking Similar takes its
    // meshes with it. Same binding in both libs, so the layers read the same
    // either way.
    .linkDirectionalParticles(l => (!PARTICLES || l._dim || l._hidden) ? 0
      : l.type === "GAP" ? 2 : l.type === "SIMILAR" ? 1 : 0)
    .linkDirectionalParticleSpeed(l => l.type === "SIMILAR" ? 0.06 : 0.01)
    .linkDirectionalParticleColor(l => l.type === "SIMILAR"
      ? (LIGHT ? "{_EDGE_COLOR_SIMILAR_PARTICLE_PAPER}" : "{_EDGE_COLOR_SIMILAR_PARTICLE}")
      : (LIGHT ? "{_EDGE_COLOR_GAP_PAPER}" : "{_EDGE_COLOR_GAP}"))
    .linkDirectionalParticleWidth(l => l.type === "SIMILAR" ? 1.5 : 2)
    .nodeVisibility(n => !n._hidden)
    .linkVisibility(l => !l._hidden)
    .onNodeClick(node => {{ selectNode(node); applyFocus(node.id); }})
    .onBackgroundClick(() => {{ closeDrawer(); clearFocus(); }})
    // The alpha gate and a drag deadlock each other. Both bundles reheat a drag
    // the d3 way — d3AlphaTarget(0.3).resetCountdown() on every drag event — but
    // alpha only climbs toward that target INSIDE tick(), and tick() is exactly
    // what the gate refuses to run while alpha still sits at the settled value
    // it stopped on. So the engine stops again on the same frame, forever. The
    // grabbed node still follows the pointer (both libs write its position
    // directly), which is why the symptom is one node moving through a graph
    // that has stopped answering. Lift the gate for the drag and let alpha do
    // what it was going to do; dragend puts the gate back, alphaTarget returns
    // to 0, and the layout settles into onEngineStop as usual.
    .onNodeDrag(() => {{
      if (simRunning) return;
      simRunning = true;
      G.d3AlphaMin(0);
      renderBudget();   // a still pointer stops waking the loop mid-drag
    }})
    .onNodeDragEnd(() => G.d3AlphaMin(ALPHA_MIN))
    .onEngineStop(() => {{
      simRunning = false;
      saveLayout();
      measureGraphRadius();   // the label thresholds are a fraction of it
      if (fitPending) {{ fitPending = false; G.zoomToFit(400, 40); wake(600); }}
      else renderBudget();
    }});

  if (is2D()) {{
    // Width 0 is invisible on canvas (GL draws a 1px line for it, 2D draws
    // nothing), and the labels ARE the point here, so pay for them. Width is
    // per-edge because on canvas it is free, and it is the second half of the
    // rank the alpha starts: a wikilink is both stronger and thicker than the
    // similarity that a model proposed beside it.
    G.linkWidth(l => l.width || 1)
      .nodeRelSize(NODE_REL_SIZE)
      .nodeCanvasObject(drawNode)
      .nodePointerAreaPaint(paintNodeArea)
      // The picking canvas repaints on a ~800ms debounce, and its link pass
      // strokes every edge at width+4px — 40-80ms on this vault, landing as a
      // visible hitch once or twice a second through any pan. Nothing hovers
      // or clicks a link here (no linkLabel, no onLinkHover), so the pass
      // buys nothing: paint no link areas at all. Node picking keeps its own
      // painter above. Measured: worst 2D frame 78ms -> 9ms.
      .linkPointerAreaPaint(() => {{}})
      // Pre, not Post: a zone is the ground the notes stand on. 2D only — the
      // 3D bundle hands out no THREE, so there the zones are colour and name.
      .onRenderFramePre(drawZones);
  }} else {{
    // Perf on big vaults (1200+ notes): the bundle gives every link its own
    // THREE.Line — a draw call and a per-tick buffer write each. The links are
    // drawn merged instead (see LinkSeg above), and the visibility accessor
    // admits exactly one lib line into existence: the constructor donor. A
    // falsy accessor result skips object creation entirely, so the other 9k
    // Lines and their materials are never built at all. linkWidth 0 keeps the
    // donor a cheap Line rather than a cylinder mesh; fewer sphere segments.
    G.linkWidth(0).nodeResolution(6)
      .linkVisibility(l => l === RAW_EDGES[0] || (libOwnsLink(l) && !l._hidden))
      // "handled" for merged links and for the donor, which stays degenerate;
      // lib-owned particle carriers keep the default update and really move.
      .linkPositionUpdate((o, coords, l) => !libOwnsLink(l))
      // 1 so the per-edge alpha in linkPaint is the final opacity rather than
      // being scaled by a second global. The bundle's default 0.2 is what put
      // every link at the same weight in the first place.
      .linkOpacity(1)
      // The bundle's own onboarding line, bottom centre of every scene it has
      // ever rendered. It is the single most recognisable thing about the
      // library, it teaches three mouse bindings nobody needed taught, and it
      // is not written in this app's voice.
      .showNavInfo(false)
      // 0.75 is the default, and at 0.75 every node shows through every other
      // one: the cluster reads as gas. Solid nodes plus the fog below carry the
      // depth instead, which is the reading that was wanted all along.
      .nodeOpacity(0.96)
      // The default tooltip is the bare label in the library's own black
      // rounded box. This one is a Silica compartment (see .float-tooltip-kap)
      // and it answers the question the hover is actually asking at a distance
      // the note labels do not reach: which cluster is this, and is it special.
      //
      // The community comes first because that is what the node's colour means,
      // always. The zone is APPENDED when its layer is up, never substituted:
      // in 3D there are no hulls to carry it (onRenderFramePre is 2D-only), so
      // without this the semantic layer would be floating names and nothing an
      // individual note could be checked against.
      // The zone gets its OWN line rather than joining the first: community
      // labels already contain " · " inside themselves, so appending to them
      // produced "etica · sistemi · zone: etica · morale", one run with no seam
      // where the second partition starts.
      .nodeLabel(n => {{
        const bits = [];
        if (COMM_LABELS[n.group]) bits.push(COMM_LABELS[n.group]);
        const st = nodeState(n);
        if (st !== "note") bits.push(st);
        const zone = (showZones && n.sgroup >= 0) ? ZONE_LABEL[n.sgroup] : null;
        return '<div class="g3d-tip"><b>' + escHtml(n.label) + '</b>' +
          (bits.length ? '<i>' + escHtml(bits.join(" · ")) + '</i>' : '') +
          (zone ? '<i>zone ' + escHtml(zone) + '</i>' : '') + '</div>';
      }});
  }}
  // Forces before the data, and the data last of all. The warmup loop runs the
  // moment graphData lands, so anything set afterwards shapes only the animated
  // tail: with the tuned forces arriving late, the bulk of the layout was being
  // built by d3's untouched defaults and then nudged at low alpha. Widening the
  // warmup made that worse, which is how it surfaced.
  applyForces(false, G);
  G.graphData({{ nodes: RAW_NODES, links: RAW_EDGES }});
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
  Graph = buildGraph();    // owns the forces now: they must precede graphData
  styleScene();            // first frame already lit; a no-op if 2D
  syncZoneLoop();          // the note-name layer is 3D-only, so the mode owns it
  renderBudget();
}}

// Slider multipliers persist across sessions; the baseline is never persisted
// (recomputed from the current graph each load).
const FORCES_KEY = "silica-graph-forces";
let forceMul = {{ repel: 1, dist: 1, center: 1 }};
try {{
  Object.assign(forceMul, JSON.parse(localStorage.getItem(FORCES_KEY)) || {{}});
}} catch (e) {{ /* corrupt or blocked storage -> auto defaults */ }}

// G is passed explicitly during a build, where the instance is not yet the
// global one: the forces have to be in place before graphData runs the warmup.
function applyForces(reheat, G) {{
  G = G || Graph;
  // distanceMax bounds both over-dispersion and per-tick cost on big graphs.
  G.d3Force("charge").strength(baseCharge() * forceMul.repel)
    .distanceMax(600 * FORCE_SCALE);
  G.d3Force("link").distance(baseDist() * forceMul.dist);
  // Center capped at 1: d3 forceCenter shifts positions directly, >1 oscillates.
  G.d3Force("center").strength(Math.min(1, forceMul.center));
  // A slider moves an already-settled layout, so the reheat is a perturbation,
  // not a cold start: the fast decay is the right budget for it, and without it
  // dragging a slider would cost the full 300-tick schedule every time.
  if (reheat) {{
    G.d3AlphaDecay(FAST_DECAY).d3ReheatSimulation();
    simRunning = true;
    renderBudget();
  }}
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
  // 3D link visibility lives in the merged buffer; the accessor re-pass stays
  // for 2D and, with PARTICLES on, for the lib-owned photon carriers.
  if (is2D() || PARTICLES) Graph.linkVisibility(Graph.linkVisibility());
  if (!is2D()) repaintLinkSeg();
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
    ctx.fillStyle = zoneColor(z);
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
    '<div class="zone-label" data-id="' + z.id + '" style="color:' + zoneColor(z) + '">' +
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

// --- Note names in 3D -------------------------------------------------------
// 2D paints its labels on the canvas and gates them on the ZOOM: dots below
// 0.6, the standouts up to 1.5, everything above it. 3D has no zoom scalar, so
// the same idea rides the only distance it does have — camera to node. Move in,
// names appear; the reading is identical, the quantity is not a guess.
//
// DOM, like the zone names above, and for the same two reasons: the bundle
// hands out no THREE to build a sprite with, and one positioning routine is
// enough. The cost is bounded by a fixed pool of divs rather than by the vault:
// 553 absolutely-positioned elements written every frame is a real bill, and
// past the first few dozen the names overlap into a smear anyway.
const LABEL_POOL = 60;
const labelEls = [];
function buildNodeLabels() {{
  const layer = document.getElementById("node-labels");
  if (!layer || labelEls.length) return;
  for (let i = 0; i < LABEL_POOL; i++) {{
    const el = document.createElement("div");
    el.className = "node-label";
    layer.appendChild(el);
    labelEls.push(el);
  }}
}}

// The thresholds are in graph units and have to scale with the layout, which is
// thousands of units across on a big vault and hundreds on a small one. Mean
// distance from the centroid is that scale, recomputed whenever the layout
// settles rather than per frame.
let GRAPH_R = 0;
function measureGraphRadius() {{
  let x = 0, y = 0, z = 0, n = 0;
  RAW_NODES.forEach(p => {{ if (p.x != null) {{ x += p.x; y += p.y; z += p.z || 0; n++; }} }});
  if (!n) return;
  x /= n; y /= n; z /= n;
  let sum = 0;
  RAW_NODES.forEach(p => {{
    if (p.x == null) return;
    sum += Math.hypot(p.x - x, p.y - y, (p.z || 0) - z);
  }});
  GRAPH_R = sum / n;
}}

function positionNodeLabels() {{
  if (!labelEls.length) return;
  const cam = !is2D() && Graph.camera && Graph.camera();
  // 2D draws its own labels on the canvas; showing these there would double
  // every name. Same for notes-off, where there is nothing to name.
  if (!cam || !cam.position || !showNotes || !GRAPH_R) {{
    labelEls.forEach(el => {{ el.style.display = "none"; }});
    return;
  }}
  const far = 3.0 * GRAPH_R;    // beyond this, no name at all
  const near = 1.2 * GRAPH_R;   // inside this, every note gets one
  const p = cam.position;
  const cand = [];
  for (const n of RAW_NODES) {{
    if (n._hidden || n._dim || n.x == null) continue;
    const d = Math.hypot(n.x - p.x, n.y - p.y, (n.z || 0) - p.z);
    if (d > far) continue;
    // Between near and far only the standouts, exactly the rule 2D applies
    // between zoom 0.6 and 1.5. Ghosts are names nothing carries; they are
    // already the most numerous thing in the frame and stay unnamed.
    if (d > near && ((n.size || 16) <= BASE_SIZE || n.type === "ghost")) continue;
    if (!inFrontOfCamera(n)) continue;
    cand.push([d, n]);
  }}
  // Nearest first, then the pool cuts the tail: when more notes qualify than
  // there are slots, the ones you flew towards are the ones that get named.
  cand.sort((a, b) => a[0] - b[0]);
  const shown = Math.min(cand.length, LABEL_POOL);
  for (let i = 0; i < shown; i++) {{
    const d = cand[i][0], n = cand[i][1];
    const el = labelEls[i];
    const s = Graph.graph2ScreenCoords(n.x, n.y, n.z || 0);
    if (el._id !== n.id) {{ el.textContent = n.label; el._id = n.id; }}
    el.style.display = "block";
    // Fade with distance so names arrive instead of popping. Floored, because a
    // label at 5% is a smudge, not a word.
    el.style.opacity = Math.max(0.35, Math.min(1, 1.15 - d / far));
    el.style.color = n.type === "ghost" ? GP.ghostLabel : GP.label;
    el.style.transform =
      "translate(" + s.x + "px," + s.y + "px) translate(-50%,6px)";
  }}
  for (let i = shown; i < labelEls.length; i++) labelEls[i].style.display = "none";
}}

// Its own rAF, not the render budget's: a camera orbit moves the labels without
// the simulation running, and this loop only writes transforms on a bounded set
// of divs — nothing is rendered. It exists only while a layer that needs it is
// on. One loop drives both layers, so a 3D graph with zones up pays for one.
let zoneRaf = null;
const labelsWanted = () => showZones || !is2D();
function syncZoneLoop() {{
  if (labelsWanted() && zoneRaf === null) {{
    const step = () => {{
      positionZoneLabels();
      positionNodeLabels();
      // All 3D-only and all cheap. styleScene is here rather than wired to
      // each rebuild site because the rebuilds are the bundle's, not ours, and
      // land whenever it decides: a frame is the one moment we know they have.
      // linkSegStep rides the same fact — the link carriers it builds from
      // appear whenever the digest does.
      linkSegStep();
      styleScene();
      fogStep();
      zoneRaf = requestAnimationFrame(step);
    }};
    zoneRaf = requestAnimationFrame(step);
  }} else if (!labelsWanted() && zoneRaf !== null) {{
    cancelAnimationFrame(zoneRaf);
    zoneRaf = null;
    positionZoneLabels();   // one last pass to hide them
    positionNodeLabels();
  }}
}}

function updateZoneFilter() {{
  const cbZones = document.getElementById("cb-zones");
  const cbNodes = document.getElementById("cb-zone-nodes");
  if (cbZones) showZones = cbZones.checked;
  if (cbNodes) showNotes = cbNodes.checked;
  // No refreshPaint: toggling zones no longer touches a single node's colour,
  // and in 3D that call rebuilds the material of every node and every link (see
  // its comment). applyFilters already re-passes the visibility accessors and
  // wakes the renderer, which is all the hull layer needs to appear.
  applyFilters();
  syncZoneLoop();
  updateFocusBar();
}}

buildZoneLabels();
buildNodeLabels();

// First build: the mode comes from localStorage (3D on a fresh profile), and
// setMode owns the whole bring-up — instance, forces, filters, focus. A 2D
// first paint has no default camera worth keeping, so it refits; 3D keeps the
// lib's own initial framing.
//
// It runs down here, after both label layers exist, because setMode starts the
// label loop: the loop's own state is declared above in this section, and a
// bring-up from further up the file would read it before it is initialised.
fitPending = is2D();
setMode(mode);

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


_EDGE_COLOR_GAP = "#E0A93B"  # --warn — "a bridge could go here, and doesn't"

# The similarity particles, pre-dimmed rather than made transparent. There are
# five gap links and roughly two thousand similar ones, so the same particle
# treatment would drown the frame; the effect has to survive at a fortieth of
# the weight. Alpha is not available to do that dimming: 3d-force-graph builds
# each photon as a mesh whose material takes a THREE.Color, which discards the
# alpha channel outright. So the colour is blended against the void here, once,
# and both renderers get an opaque colour that already looks faint.
#
# It is derived UPWARD from the line, not downward. The line is _EDGE_COLOR_SIMILAR
# at opacity 0.35, which over --void lands at about #08405E; a particle at or
# below that is a moving dot you cannot see. This sits a step above it — the same
# azure blended at 0.65 — which is the whole budget the effect gets: enough that
# the drift registers, not enough that two thousand of them become the subject.
_EDGE_COLOR_SIMILAR_PARTICLE = "#056E9A"  # one step up from the line's apparent #08405E

# The paper pair. Same derivation, opposite direction: a photon that cannot use
# alpha has to be blended against the floor it will sit on, and on paper that
# blend goes toward white. The similar particle is _EDGE_COLOR_SIMILAR_PAPER
# blended one step DOWN from its line's apparent value, because on a light floor
# a faint dot is a dark one — the reverse of the crystal case above and the
# reason this is a second constant rather than the same value reused.
_EDGE_COLOR_GAP_PAPER = "#7A5305"              # --warn, on paper
_EDGE_COLOR_SIMILAR_PARTICLE_PAPER = "#5CA8C4"


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
