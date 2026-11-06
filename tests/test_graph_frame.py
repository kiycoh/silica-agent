"""The graph frame is three static files, filled by graph_view (ADR-0026).

What these hold: the skeleton and the Python agree on every slot in both
directions, the JS is a file node can parse, the data island cannot be ended by
what it carries, and a missing file is a packaging error with a name.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from string import Template

import pytest

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"


def _island(html: str) -> str:
    start = html.index('<script id="graph-data"')
    start = html.index(">", start) + 1
    return html[start:html.index("</script>", start)]


def test_every_slot_is_filled_and_every_value_is_read():
    """substitute() already raises on a slot with no value; this is the other
    direction, a value the skeleton stopped asking for, which nothing else
    would ever report."""
    import silica.ui.web.graph_view as gv

    slots = set(Template(gv._asset("graph-frame.html")).get_identifiers())
    ctx = gv._frame_context([], [], [], "t", "", "", [])
    assert slots == set(ctx), {"unfilled": slots - set(ctx), "unread": set(ctx) - slots}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to parse graph-frame.js")
def test_the_frame_code_is_a_file_node_can_parse():
    """The point of the split: the JS is no longer inside an f-string where a
    brace slip is a runtime error in a browser and nothing else."""
    out = subprocess.run(["node", "--check", str(STATIC / "graph-frame.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_the_frame_reads_its_data_from_the_island_and_nowhere_else():
    import silica.ui.web.graph_view as gv

    html = gv.render_html([{"id": "a.md", "label": "a", "type": "note", "group": 0, "path": "a.md"}],
                          [], lib_js="// x")
    js = gv._asset("graph-frame.js")
    assert 'JSON.parse(document.getElementById("graph-data").textContent)' in js
    for sym in ("RAW_NODES", "RAW_EDGES", "COMM_LABELS", "COMM_HUBS", "ATLAS_BY", "SUBS", "ZONES",
                "PARTICLES", "SHADING"):
        assert f"const {sym} = DATA." in js, f"{sym} is not read off the island"
    data = json.loads(_island(html))
    assert data["nodes"][0]["id"] == "a.md"
    assert set(data) == {"nodes", "edges", "comm_labels", "comm_hubs", "atlas", "subs", "zones",
                         "covers", "particles", "shading", "colors"}


def test_the_island_cannot_be_ended_by_what_it_carries():
    """`</script>` is the obvious one. `<!--<script>` is the other: it moves the
    HTML parser into the double-escaped state, where the real closing tag no
    longer closes, which is why every `<` goes and not only `</`."""
    import silica.ui.web.graph_view as gv

    label = "x</script><!--<script>alert(1)//"
    html = gv.render_html([{"id": "n", "label": label, "type": "note", "group": 0, "path": label + ".md"}],
                          [], title="t", lib_js="// x")
    island = _island(html)
    assert "<" not in island
    assert json.loads(island)["nodes"][0]["label"] == label
    assert html.count("</script>") == 5   # theme resolver, lib, worker's lib, island, frame: no sixth


def test_a_non_finite_score_fails_on_the_server_with_a_name():
    """NaN was a working JS literal and would be a JSON.parse syntax error and a
    blank frame; the ValueError here is what /graph shows instead."""
    import silica.ui.web.graph_view as gv

    edges = [{"id": "e", "from": "a", "to": "b", "type": "SIMILAR", "score": float("nan")}]
    with pytest.raises(ValueError, match="JSON compliant"):
        gv.render_html([], edges, lib_js="// x")


def test_a_missing_frame_file_is_a_packaging_error_with_a_name():
    import silica.ui.web.graph_view as gv

    with pytest.raises(RuntimeError, match="nope.css"):
        gv._asset("nope.css")
    for name in gv._FRAME_ASSETS:
        assert gv._asset(name)


def test_the_document_references_nothing_by_url():
    """Opened from file:// there is no server: everything the page needs is in
    the page. The bundles and the face were already inlined; the frame's own
    three files must not be the exception."""
    import silica.ui.web.graph_view as gv

    html = gv.render_html([], [], lib_js="// x")
    assert "<link" not in html and "<script src=" not in html
    assert "/static/" not in html


# --- the edge encoding: two channels, and each says what it is --------------
# Seven edge kinds on the hue channel alone is more than a 1px antialiased line
# can carry, which is how "between zones" (#E8559E, hue 330) and "unresolved"
# (#e2544f, hue 2) came to be the same colour on screen: 32 degrees apart at
# identical saturation and near-identical luminance. The fix splits the load in
# two - solid means you wrote it, broken or moving means the vault inferred it -
# and takes the recolour layer off the hue channel entirely.

def _edge_colors(paper: bool) -> dict[str, str]:
    """Every edge colour the canvas can draw, by constant name.

    Particle colours are excluded on purpose: they are blended against the
    floor they land on (graph_view's comment on _EDGE_COLOR_SIMILAR_PARTICLE),
    so their luminance is chosen to hide, not to separate.
    """
    from silica.kernel.graph import graph_export as ge
    import silica.ui.web.graph_view as gv

    out = {}
    for mod in (ge, gv):
        for name, val in vars(mod).items():
            if not name.startswith("_EDGE_COLOR_") or "PARTICLE" in name:
                continue
            if name.endswith("_PAPER") is paper:
                out[name] = val
    return out


def _luma(hex_color: str) -> float:
    """Rec.709 weights on the sRGB values as stored, no gamma linearisation.

    The question here is "does this line read as brighter than that one on the
    same floor", which is a question about the numbers the GPU is handed. A CIE
    round trip would move every value by the same curve and change no verdict.
    """
    n = int(hex_color.lstrip("#"), 16)
    r, g, b = (n >> 16) & 255, (n >> 8) & 255, n & 255
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def test_a_broken_line_is_a_link_that_is_not_there():
    """The second channel is only worth having if the panel and the canvas agree
    on it. ABSENT is the canvas's list; the three legend rows that carry .dashed
    are the panel's. A full swatch over a broken line is a legend that lies about
    the picture under it.

    SIMILAR is the row this pins hardest. It is inferred, so a "solid is what you
    wrote" rule would dash it, and it must not be dashed: it is a relation that
    holds rather than one that is missing, and at 2901 of 4246 visible edges it
    is the ground the other layers are read against.
    """
    import silica.ui.web.graph_view as gv

    js = gv._asset("graph-frame.js")
    line = next(ln for ln in js.splitlines() if ln.startswith("const ABSENT"))
    assert set(json.loads(line[line.index("["):line.rindex("]") + 1])) == {
        "GAP", "PROPOSED", "COUPLED"}

    # Rendered and not read off the skeleton: three of the seven rows are in
    # graph-frame.html and four are built in graph_view, and a check that saw
    # only one of the two halves would pass while the other half lied.
    kinds = ["EXTRACTED", "AMBIGUOUS", "GAP", "SIMILAR", "PROPOSED", "COUPLED"]
    edges = [{"from": "a.md", "to": "b.md", "type": k} for k in kinds]
    # A citation is in the paper's own references: a relation that holds.
    edges.append({"from": "a.md", "to": "b.md", "type": "CITES"})
    nodes = [{"id": f"{c}.md", "label": c, "type": "paper", "group": 0, "path": f"{c}.md"}
             for c in "ab"]
    html = gv.render_html(nodes, edges, lib_js="// x")

    for cb, dashed in (("cb-extracted", False), ("cb-ambiguous", False),
                       ("cb-similar", False), ("cb-cites", False),
                       ("cb-gaps", True), ("cb-proposed", True),
                       ("cb-coupled", True)):
        row = html[html.index(f'id="{cb}"'):]
        row = row[:row.index("</label>")]
        assert ('dot-edge dashed' in row) is dashed, f"{cb} swatch disagrees with the canvas"


def test_both_renderers_break_the_inferred_line():
    """3D has no dash at all, but every link there is already merged into one
    LineSegments, and LineSegments IS the dash primitive - it draws disjoint
    vertex pairs. So the buffer carries a per-edge segment count rather than a
    flat two vertices, and that count comes from a period in WORLD units: a long
    link gets more dashes, never longer ones, which is the only version of the
    pattern that reads the same on every edge in the picture.

    2D prints a screen instead (ADR-0146, the tests below); the canvas keeps
    the dash only for the frame with no WebGL2, which has no screen to print.
    """
    import silica.ui.web.graph_view as gv

    js = gv._asset("graph-frame.js")
    assert ".linkLineDash(" in js, "the canvas fallback draws the absent layers solid"
    assert "DASH_PERIOD" in js and "DASH_CAP" in js, (
        "the 3D dash is a world-unit period with a bounded per-edge count")
    assert "DASH_SEGS" not in js, (
        "a fixed segment count makes the dash a FRACTION of the link, so a long "
        "edge draws long strokes with furrows cut in them")


# --- 2D prints every link in a spot ink (ADR-0146 point 5) -------------------
# The paint and the packer are run in node, not grepped: the claim is about the
# colours and the instances the GPU is handed, and a string pin would pass on a
# formula that computes the wrong ink.

_NODE = shutil.which("node")
needs_node = pytest.mark.skipif(_NODE is None, reason="needs node to run graph-frame.js")
_KINDS = ("EXTRACTED", "AMBIGUOUS", "SIMILAR", "CITES", "GAP", "PROPOSED", "COUPLED", "COVERS")
_WRITTEN = ("EXTRACTED", "AMBIGUOUS", "SIMILAR", "CITES")


def _frame_js() -> str:
    return (STATIC / "graph-frame.js").read_text()


def _tokens(light: bool) -> dict[str, str]:
    """The frame's own CSS, which is what GP reads at run time."""
    import re

    css = (STATIC / "graph-frame.css").read_text()
    head = ':root[data-theme="light"]{' if light else ":root{"
    block = css[css.index(head):]
    return dict(re.findall(r"(--[\w-]+):\s*([^;}]+)", block[:block.index("}")]))


def _edge(kind: str) -> dict:
    """An edge as the server sends it: colour pair and opacity from the
    exporters' own constants (CITES carries none and takes the fallback)."""
    from silica.kernel.graph import graph_export as ge
    import silica.ui.web.graph_view as gv

    pair = {"EXTRACTED": (ge._EDGE_COLOR_EXTRACTED, ge._EDGE_COLOR_EXTRACTED_PAPER, 0.72),
            "AMBIGUOUS": (ge._EDGE_COLOR_AMBIGUOUS, ge._EDGE_COLOR_AMBIGUOUS_PAPER, 0.55),
            "SIMILAR": (ge._EDGE_COLOR_SIMILAR, ge._EDGE_COLOR_SIMILAR_PAPER, 0.35),
            "GAP": (gv._EDGE_COLOR_GAP, gv._EDGE_COLOR_GAP_PAPER, 0.95),
            "PROPOSED": (gv._EDGE_COLOR_PROPOSED, gv._EDGE_COLOR_PROPOSED_PAPER, 0.75),
            "COUPLED": (gv._EDGE_COLOR_COUPLED, gv._EDGE_COLOR_COUPLED_PAPER, 0.75),
            "COVERS": (None, None, 1.0)}.get(kind)
    e = {"type": kind, "from": "a", "to": "b"}
    if pair:
        e["color"] = {"color": pair[0], "paper": pair[1], "opacity": pair[2]}
    return e


def _run_frame(body: str, *, light: bool, mode: str = "2d") -> object:
    """The frame's edge paint (LINK_ALPHA_2D through linkPaint) and its 2D
    packer, under the globals they read, then `body`, whose last line prints."""
    from silica.kernel.graph import graph_export as ge

    js = _frame_js()
    paint = js[js.index("const LINK_ALPHA_2D"):js.index("// --- 3D: every link in ONE LineSegments")]
    stride = next(ln for ln in js.splitlines() if ln.startswith("const GL_STRIDE"))
    pack = js[js.index("function glRGBA("):js.index("// The canvas the lib's pre-paint layers draw on")]
    tok = _tokens(light)
    gp = {"bg": tok["--void"], "linkDim": tok["--g-link-dim"], "accent": tok["--accent"]}
    stubs = (
        f"const DATA = {{colors: {{extracted: {json.dumps(ge._EDGE_COLOR_EXTRACTED)},"
        f" extracted_paper: {json.dumps(ge._EDGE_COLOR_EXTRACTED_PAPER)}}}}};\n"
        f"const LIGHT = {json.dumps(light)}, GP = {json.dumps(gp)}, AB = null;\n"
        f"const is2D = () => {json.dumps(mode == '2d')};\n"
        "const FADE_STEPS = 16, PARTICLES = false, libOwnsLink = () => false;\n"
        "let GLLinks = null, ALL_EDGES = [], ATLAS = false, LINK_SEL = null;\n"
        "const atlasOn = () => ATLAS;\nlet quietNow = () => false;\n")
    out = subprocess.run([_NODE, "-e", stubs + paint + "\n" + stride + "\n" + pack + "\n" + body],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_PACK = """
GLLinks = {inst: new ArrayBuffer(8192), index: new Map([["a", 0], ["b", 1]]), rgba: new Map(),
           gl: {ARRAY_BUFFER: 0, bindBuffer() {}, bufferSubData() {}}};
packGLLinks();
const f = new Float32Array(GLLinks.inst), u = new Uint8Array(GLLinks.inst), n = GL_STRIDE / 4, out = [];
for (let i = 0; i < GLLinks.count; i++)
  out.push({rgb: Array.from(u.slice(i * GL_STRIDE + 8, i * GL_STRIDE + 11)), cover: u[i * GL_STRIDE + 11],
            w: f[i * n + 3], side: f[i * n + 4], ink: Math.round(f[i * n + 5] * 100) / 100});
console.log(JSON.stringify({out, mat: GLLinks.mat, over: GLLinks.over, paint: ALL_EDGES.map(linkPaint),
                            ink: SCREEN_INK, matAlpha: ATLAS_MAT}));
"""


def _rgba(paint: str) -> tuple[float, ...]:
    """A fully dimmed link paints the dim token itself, a hex: opaque."""
    if paint.startswith("#"):
        n = int(paint[1:], 16)
        return ((n >> 16) & 255, (n >> 8) & 255, n & 255, 1)
    return tuple(float(v) for v in paint[paint.index("(") + 1:paint.index(")")].split(","))


def _mix(hex_color: str, floor: str, k: float) -> tuple[int, int, int]:
    a, b = int(hex_color.lstrip("#"), 16), int(floor.lstrip("#"), 16)
    return tuple(_round(((b >> s) & 255) + (((a >> s) & 255) - ((b >> s) & 255)) * k) for s in (16, 8, 0))


def _round(v: float) -> int:
    """Math.round: half up. Python's round() is half to even and disagrees
    on exactly the halves a 0.5 fade lands on."""
    import math

    return math.floor(v + 0.5)


def _paint_all(edges: list[dict], *, light: bool, mode: str = "2d") -> list[str]:
    return _run_frame(f"console.log(JSON.stringify({json.dumps(edges)}.map(linkPaint)))",
                      light=light, mode=mode)


@needs_node
@pytest.mark.parametrize("light", [False, True])
def test_no_link_ink_in_2d_is_translucent(light):
    """Every kind, lit, fading, dimmed, and as a context line under the rim:
    one opaque ink, so two links crossing can never darken (invariant 8 by
    construction, which is why the MAX-blended union buffer is gone)."""
    edges = []
    for kind in _KINDS:
        for extra in ({}, {"_fade": 0.3}, {"_fade": 1}, {"_ctx": True}):
            edges.append({**_edge(kind), **extra})
    for paint, e in zip(_paint_all(edges, light=light), edges):
        assert _rgba(paint)[3] == 1, (e, paint)


@needs_node
@pytest.mark.parametrize("light", [False, True])
def test_a_written_link_is_todays_colour_mixed_onto_the_floor(light):
    """It looks as it does now: today's colour at today's 2D alpha (the data
    alpha times 0.55), mixed once onto the theme's floor. The ADR's two
    examples are .396 for a wikilink and .1925 for k-NN; CITES carries no
    colour and takes the wikilink's at the default .6."""
    floor = _tokens(light)["--void"]
    kinds = ("EXTRACTED", "SIMILAR", "AMBIGUOUS", "CITES")
    paints = _paint_all([_edge(k) for k in kinds], light=light)
    for kind, paint in zip(kinds, paints):
        e = _edge(kind)
        c = e.get("color") or {"color": _edge("EXTRACTED")["color"]["color"],
                               "paper": _edge("EXTRACTED")["color"]["paper"], "opacity": 0.6}
        want = _mix(c["paper"] if light else c["color"], floor, c["opacity"] * 0.55)
        assert _rgba(paint)[:3] == want, (kind, paint, want)
    assert round(0.72 * 0.55, 4) == 0.396 and round(0.35 * 0.55, 4) == 0.1925


@needs_node
@pytest.mark.parametrize("light", [False, True])
def test_a_band_prints_its_own_hue_and_the_screen_is_its_density(light):
    """Kind stays the hue (invariant 4): the band's ink is the kind's colour
    whole, and certainty is how many cells of the screen it inks. COVERS is
    the accent, read from the theme."""
    tok = _tokens(light)
    kinds = ("GAP", "PROPOSED", "COUPLED", "COVERS")
    for kind, paint in zip(kinds, _paint_all([_edge(k) for k in kinds], light=light)):
        c = _edge(kind).get("color")
        hue = tok["--accent"] if kind == "COVERS" else (c["paper"] if light else c["color"])
        assert _rgba(paint)[:3] == _mix(hue, hue, 1), (kind, paint)


@needs_node
def test_proposed_prints_through_the_4x4_and_a_question_through_the_8x8():
    """The landing's tiers: a proposal (a shared concept; COVERS, which
    Silica drew) is the 4x4 screen, a question (an association only) the 8x8
    ghost. Wikilinks and SIMILAR are never screened (invariants 8 and 1), and
    every written link is 1 px whatever its state, so width means one thing."""
    out = _run_frame("console.log(JSON.stringify({screen: SCREEN_2D, ink: SCREEN_INK,"
                     f" w: {json.dumps([_edge(k) for k in _KINDS])}"
                     ".flatMap(l => [width2D(l), width2D({...l, _dim: true, _fade: 1})])}))",
                     light=False)
    side = {k: v[0] for k, v in out["screen"].items()}
    assert side == {"PROPOSED": 4, "COVERS": 4, "COUPLED": 8, "GAP": 8}
    assert not set(_WRITTEN) & set(side)
    widths = dict(zip(_KINDS, zip(out["w"][::2], out["w"][1::2])))
    for kind in _WRITTEN:
        assert widths[kind] == (1, 1), kind
    # every band 3 px, one size (the owner, 2026-11-01)
    assert all(widths[k] == (3, 3) for k in ("PROPOSED", "COVERS", "COUPLED", "GAP"))
    # the approved draft's graphite shares; paper keeps the landing's
    assert out["ink"] == {"4": 0.42, "8": 0.14}
    assert _run_frame("console.log(JSON.stringify(SCREEN_INK))", light=True) == {"4": 0.55, "8": 0.16}


@needs_node
@pytest.mark.parametrize("light", [False, True])
def test_the_layer_draws_every_band_last_cased_by_a_pixel_of_floor(light):
    """The instances the GPU gets, in order: the written links at 1 px and
    unscreened, then per band a casing in the floor 2 px wider and the band
    in its screen, gap under coupled under proposed. A band out of focus (or
    outside a comparison) keeps its screen and its width in the dim ink, but
    goes first and uncased: it is not what is being read, and drawn last with
    its casing it cut every lit link of the focus that crossed it."""
    kinds = ["PROPOSED", "EXTRACTED", "GAP", "SIMILAR", "COUPLED", "CITES", "AMBIGUOUS", "COVERS"]
    edges = [_edge(k) for k in kinds] + [{**_edge("COUPLED"), "_fade": 1, "_dim": True}]
    got = _run_frame(f"ALL_EDGES = {json.dumps(edges)};" + _PACK, light=light)
    out, ink = got["out"], got["ink"]
    assert got["over"] is False and got["mat"] == 0, "atlas off: one pass, no mat"
    rgb = lambda paint: [int(v) for v in _rgba(paint)[:3]]
    by = dict(zip(kinds, got["paint"]))
    dim = list(_mix(_tokens(light)["--g-link-dim"], "#000000", 1))
    full = {"rgb": dim, "cover": 255, "w": 3, "side": 8, "ink": round(ink["8"], 2)}
    assert out[0] == full, "the dimmed band, first"
    written = out[1:5]
    assert all(i["side"] == 0 and i["w"] == 1 for i in written)
    assert sorted(i["rgb"] for i in written) == sorted(rgb(by[k]) for k in _WRITTEN)
    floor = list(_mix(_tokens(light)["--void"], _tokens(light)["--void"], 1))
    bands = out[5:]
    want = [("GAP", 8, 3), ("COUPLED", 8, 3), ("PROPOSED", 4, 3), ("COVERS", 4, 3)]
    assert len(bands) == 2 * len(want), "a band is drawn after every written link"
    for (kind, side, w), case, band in zip(want, bands[::2], bands[1::2]):
        assert case == {"rgb": floor, "cover": 255, "w": w + 2, "side": 0, "ink": 0}, kind
        assert band == {"rgb": rgb(by[kind]), "cover": 255, "w": w, "side": side,
                        "ink": round(ink[str(side)], 2)}, kind


# Atlas cells measured on alex_second_brain (2026-11-01): the ground a focused
# note's links cross in the default 2D view.
_CELLS = {False: [(51, 46, 75), (45, 47, 75), (73, 46, 75)], True: [(204, 194, 215), (220, 205, 214)]}


def _lum(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _overprint(inst: dict, ground, floor) -> list[float]:
    """GL_INK_FS with the atlas on, laid over `ground` at full strength: the
    buffer holds the spot ink less the floor it was mixed onto, at its
    coverage, and the composite is ONE, ONE_MINUS_SRC_ALPHA."""
    c = inst["cover"] / 255
    return [max(0.0, s - f * (1 - c)) + g * (1 - c) for s, f, g in zip(inst["rgb"], floor, ground)]


@needs_node
@pytest.mark.parametrize("light", [False, True])
def test_with_the_atlas_on_the_mat_prints_under_the_read_links(light):
    """The owner, 2026-11-01: with the atlas on the links should show too,
    less opaque. Every visible link the atlas used to hide prints in a mat,
    composited once at ATLAS_MAT; the links being read (the focus, the
    hover) and every live band print over it at full strength. The mat is the
    structure as it stands at rest whatever is in focus: in the dim ink it
    darkened every cell, and the focused links over it fell to luma 80 against
    86 (measured, alex_second_brain). So a dimmed link keeps its own ink and
    cover there, and a band in the mat goes first and uncased."""
    edges = [{**_edge("EXTRACTED"), "_q": 1, "_fade": 1, "_dim": True}, {**_edge("SIMILAR"), "_q": 1},
             _edge("EXTRACTED"), {**_edge("PROPOSED"), "_q": 1},
             {**_edge("COUPLED"), "_q": 1, "_fade": 1, "_dim": True}]
    got = _run_frame(f"ATLAS = true; quietNow = l => !!l._q; ALL_EDGES = {json.dumps(edges)};" + _PACK,
                     light=light)
    out, mat = got["out"], got["mat"]
    assert got["over"] is True
    assert 0.2 <= got["matAlpha"] <= 0.6, "the mat is translucent and still reads"
    assert mat == 3, out
    assert out[0]["side"] == 8 and out[0]["w"] == 3 and out[0]["cover"] == 255, "the dimmed band, uncased"
    assert all(i["cover"] < 255 for i in out[1:3]), "a written link in the mat keeps its coverage"
    lit = [int(v) for v in _rgba(got["paint"][2])[:3]]
    assert lit in [i["rgb"] for i in out[1:3]], "the dimmed wikilink prints its own ink in the mat"
    hue = _paint_all([_edge("COUPLED")], light=light)[0]
    assert out[0]["rgb"] == [int(v) for v in _rgba(hue)[:3]], "the band in the mat keeps its hue"
    top = out[mat:]
    assert len(top) == 3 and top[0]["side"] == 0 and top[0]["cover"] < 255, "the read link"
    assert top[1]["w"] == 5 and top[1]["side"] == 0 and top[2]["side"] == 4, "the live band, cased, last"
    js = _frame_js()
    draw = js[js.index("function drawGLLinks("):js.index("// --- 3D: the same crystal")]
    assert "[0, L.mat, ATLAS_MAT], [L.mat, L.count - L.mat, 1]" in draw


@needs_node
@pytest.mark.parametrize("light", [False, True])
def test_over_a_cell_a_read_link_is_as_visible_as_before_the_reshade(light):
    """The first build pre-mixed every ink onto the floor, so over an atlas
    cell a focused wikilink fell from luma 86 to 65 on graphite (cell 49).
    Overprinted, it is the old translucent line again over any cell, and
    still exactly the spot ink over the floor."""
    tok = _tokens(light)
    floor = _mix(tok["--void"], tok["--void"], 1)
    got = _run_frame(f"ATLAS = true; ALL_EDGES = {json.dumps([_edge(k) for k in _WRITTEN])};" + _PACK,
                     light=light)
    assert got["mat"] == 0
    for kind, inst in zip(_WRITTEN, got["out"]):
        c = _edge(kind).get("color") or {"color": _edge("EXTRACTED")["color"]["color"],
                                         "paper": _edge("EXTRACTED")["color"]["paper"], "opacity": 0.6}
        hexc, a = (c["paper"] if light else c["color"]), c["opacity"] * 0.55
        n = int(hexc.lstrip("#"), 16)
        assert abs(inst["cover"] - a * 255) <= 0.5, kind
        assert max(abs(x - y) for x, y in zip(_overprint(inst, floor, floor), inst["rgb"])) <= 0.5, (
            "over the floor the overprint is the spot ink", kind)
        for cell in _CELLS[light]:
            before = [g + (((n >> s) & 255) - g) * a for g, s in zip(cell, (16, 8, 0))]
            now = _overprint(inst, cell, floor)
            assert max(abs(x - y) for x, y in zip(now, before)) <= 1.5, (kind, cell, now, before)
            assert abs(_lum(now) - _lum(cell)) >= abs(_lum(before) - _lum(cell)) - 1, (kind, cell)


def test_crossings_never_stack_and_the_atlas_off_prints_as_built():
    """The links go into a buffer with blending off, so where two cross the
    last one wins and nothing darkens; the buffer is laid over the ground
    once per pass. With the atlas off nothing goes through the buffer: the
    spot ink is printed opaque, as the first build did."""
    js = _frame_js()
    draw = js[js.index("function drawGLLinks("):js.index("// --- 3D: the same crystal")]
    inbuf = draw[draw.index("for (const [from, n, share]"):]
    assert inbuf.index("gl.bindFramebuffer(gl.FRAMEBUFFER, L.fbo);") < inbuf.index("draw(from, n);")
    assert inbuf.index("gl.disable(gl.BLEND);") < inbuf.index("draw(from, n);")
    assert "gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);" in inbuf
    assert "gl.blendEquation(gl.MAX);" not in js
    assert "if (!L.over) {" in draw, "atlas off: straight to the canvas, no buffer"
    build = js[js.index("function buildGLLinks("):js.index("function buildGLAtlas(")]
    fs = js[js.index("const GL_INK_FS = `"):]
    fs = fs[:fs.index("`;")]
    assert "float c = over > .5 ? vColor.a : 1.;" in fs
    assert "o = vec4(vColor.rgb - ground * (1. - c), c);" in fs
    assert "GL_INK_FS" in build


@needs_node
def test_3d_paints_as_it_did():
    """Invariant 9: the 3D buffer carries per-vertex RGBA, and none of the
    spot inks reach it. Today's formula, at full weight and halfway dimmed."""
    edges = [_edge(k) for k in _KINDS if k != "COVERS"]
    edges += [{**e, "_fade": 0.5} for e in edges]
    dim = int(_tokens(False)["--g-link-dim"].lstrip("#"), 16)
    for e, paint in zip(edges, _paint_all(edges, light=False, mode="3d")):
        c = e.get("color") or {"color": _edge("EXTRACTED")["color"]["color"], "opacity": 0.6}
        n, a = int(c["color"].lstrip("#"), 16), c["opacity"]
        t = round(e.get("_fade", 0) * 16) / 16
        ch = [_round(((n >> s) & 255) + (((dim >> s) & 255) - ((n >> s) & 255)) * t) for s in (16, 8, 0)]
        assert paint == "rgba({},{},{},{:.3f})".format(*ch, a + (1 - a) * t), (e["type"], paint)


def test_the_link_shader_prints_a_screen_in_device_pixels_with_hard_edges():
    """The screen is thresholded in the fragment shader against gl_FragCoord,
    so it never scales with zoom; the matrix is the landing's 8x8 Bayer (the
    4x4 is its even cells). Every fragment is ink or nothing: no coverage
    ramp, alpha 1. The atlas keeps its own antialiased lines."""
    import re

    js = _frame_js()
    build = js[js.index("function buildGLLinks("):js.index("function buildGLAtlas(")]
    fs_name = re.search(r"prog = glProgram\(gl, GL_LINK_VS, (\w+)\)", build).group(1)
    fs = js[js.index(f"const {fs_name} = `"):]
    fs = fs[:fs.index("`;")]
    assert "gl_FragCoord" in fs and "discard" in fs
    assert "vColor.a * clamp(" not in fs, "a coverage ramp is a translucent edge"
    assert "o = vec4(vColor.rgb - ground * (1. - c), c);" in fs
    table = [int(v) for v in re.search(r"int\[64\]\(([^)]*)\)", fs).group(1).split(",")]

    def bayer(n):
        if n == 1:
            return [[0]]
        b = bayer(n // 2)
        return [[4 * b[y % (n // 2)][x % (n // 2)] + [[0, 2], [3, 1]][y * 2 // n][x * 2 // n]
                 for x in range(n)] for y in range(n)]

    assert table == [v for row in bayer(8) for v in row]
    assert "GL_LINK_FS" in js[js.index("function buildGLAtlas("):], "the atlas's lines keep their ramp"


@needs_node
def test_in_2d_the_key_prints_the_band_the_canvas_prints():
    """Invariant 12: the swatch of an absent row is, in 2D, the same cells of
    the same screen at the same share the shader inks, in both themes; the
    rows still carry .dashed for 3D, which keeps the dash."""
    import re
    from urllib.parse import unquote

    js, css = _frame_js(), (STATIC / "graph-frame.css").read_text()
    table = [int(v) for v in re.search(r"int\[64\]\(([^)]*)\)", js).group(1).split(",")]
    cell = {8: lambda x, y: table[y * 8 + x] / 64, 4: lambda x, y: table[y * 16 + x * 2] / 16}
    assert "document.body.classList.toggle(\"mode-2d\", is2D());" in js
    for light in (False, True):
        ink = _run_frame("console.log(JSON.stringify(SCREEN_INK))", light=light)
        for row, side in (("#cb-proposed+.dot-edge", 4), ("#cb-coupled+.dot-edge", 8),
                          ("#cb-gaps+.dot-edge", 8), ("#covers-row>.dot-edge", 4)):
            theme = ':where(:root[data-theme="light"]) ' if light else ""
            rule = css[css.index(theme + ".mode-2d " + row):]
            mask = unquote(rule[rule.index("mask:url("):rule.index("}")])
            got = set(map(tuple, (map(int, m) for m in re.findall(r"M(\d+) (\d+)h1", mask))))
            want = {(x, y) for y in range(side) for x in range(side) if ink[str(side)] > cell[side](x, y)}
            assert got == want, (light, row)
            assert f"width='{side}' height='{side}'" in mask and f"0 0/{side}px {side}px" in mask


def test_with_the_layer_up_the_lib_draws_no_link_line():
    """All of a link's line is the layer's, so the bands can be drawn last and
    nothing the lib strokes crosses them. The lib keeps what it adds to a
    line: the citation's arrowhead, a written requires' chevrons (ADR-0145)
    and the photons, which ride only a link the lib counts as visible."""
    js = _frame_js()
    assert '(!GLLinks || l.type === "CITES" || l.requires || libOwnsLink(l)))' in js
    assert '.linkCanvasObjectMode(l => GLLinks ? "replace" : l.type === "CITES" || l.requires ? "after" : null)' in js
    arrow = js[js.index("function drawCiteArrow("):]
    assert arrow.index('if (l.type !== "CITES") return;') < arrow.index("ctx.fill();")
    assert "G.linkWidth(1)" in js, "the canvas fallback draws every link at 1 px"


def test_the_3d_mark_carries_the_rings_2d_draws():
    """The legend's NODE rows promise four readings in either renderer, and two
    of them - the state ring and load-bearing - used to exist in 2D only. They
    ride the one per-node channel this view has: the bundle shares a material
    per COLOUR and a geometry per size, so the value has to arrive on the object
    itself, immediately before its draw.
    """
    import silica.ui.web.graph_view as gv

    js = gv._asset("graph-frame.js")
    assert "onBeforeRender = nodeRingStep" in js, (
        "the ring colour is per NODE, and onBeforeRender is the only hook that "
        "runs per object while the material and the geometry are shared")
    assert "uSilState" in js and "uSilCut" in js, (
        "both rings, not one: a note can be a hub and load-bearing at once, "
        "which is why 2D draws them as two circles and not one")
    assert "RING_MIN_PX" in js, (
        "under a floor there is no note left under 6.5px of ring - 2D spends "
        "its zoom gate on the same problem")
    assert "n._dim" in js[js.index("function nodeUniforms"):js.index("function nodeRingStep")], (
        "a dimmed note is a dot in 2D; a ring on it would outrank the focus")
