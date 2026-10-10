# tests/test_explore_panel.py
"""The explore view as the deck draws it: five gates over what the port added.

The Explore artboard in `docs/design/silica-deck/` drew five things the app did
not have. Each of them is a place where the same fact can end up stated twice --
a mode list in a rail AND a toolbar, a renderer in a HUD AND a header, a fit
button in two corners, a note's context in a drawer AND a column -- and a second
copy is the one failure none of them announces: it just goes stale.

So these tests pin the "once", plus the two facts that are only true if the
wiring exists at all: the frame answers with the renderer it built, and the
column is fed by the click.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "silica" / "ui" / "web" / "static"
INDEX = WEB / "index.html"
from tests.webassets import app_css, app_js
WORK_JS = WEB / "work.js"
FRAME_HTML = WEB / "graph-frame.html"
FRAME_CSS = WEB / "graph-frame.css"
FRAME_JS = WEB / "graph-frame.js"


def _block(text: str, open_tag: str, close_tag: str) -> str:
    start = text.index(open_tag)
    return text[start:text.index(close_tag, start)]


# --- 1. the Node panel -------------------------------------------------------

def _pure_block() -> str:
    """The projection half of work.js, which is pure and runs outside a browser."""
    src = WORK_JS.read_text()
    m = re.search(r"// --- projection:begin.*?// --- projection:end", src, re.S)
    assert m, "projection markers not found in work.js"
    return m.group(0)


def _project_node(tmp_path, node: dict, ctx) -> dict:
    script = tmp_path / "node.js"
    script.write_text(
        _pure_block()
        + "\nconst a = JSON.parse(process.argv[2]);"
        + "\nconsole.log(JSON.stringify(projectNode(a[0], a[1])));\n"
    )
    out = subprocess.run(["node", str(script), json.dumps([node, ctx])],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_the_node_panel_states_the_click_before_the_round_trip(tmp_path):
    """The head facts arrive with the click and the sections one fetch later. A
    panel that waits for /context shows the PREVIOUS node while the next one is
    loading, which is the one moment it is guaranteed to be wrong."""
    d = _project_node(tmp_path, {"path": "Concetti/ML.md", "name": "ML",
                                 "links": 14, "state": "hub", "area": "cluster · dati"}, None)
    assert d["title"] == "ML" and d["path"] == "Concetti/ML.md"
    assert d["loading"] is True
    # the count is kept apart from the words because it is painted apart
    assert d["count"] == "14 links"
    assert d["meta"] == ["hub", "cluster · dati"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_the_default_state_is_not_a_word_worth_printing(tmp_path):
    """hub and orphan are readings; "note" is every other note in the vault."""
    d = _project_node(tmp_path, {"path": "a.md", "links": 1, "state": "note"}, {})
    assert d["count"] == "1 link" and d["meta"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_the_two_offers_are_two_sections(tmp_path):
    """/context returns one `suggested` list carrying two different offers: a
    ghost is a link you already wrote and never filled, a note is a relative the
    vault computed and you never linked. One list would put "write it" and
    "link" under one heading, which is two verbs the reader has to sort."""
    ctx = {
        "title": "ML",
        "related": {"outgoing": [{"name": "Clustering", "path": "C/Clustering.md"}],
                    "backlinks": [{"name": "Corso", "path": "Corso.md"}]},
        "suggested": [
            {"name": "Gradient descent", "path": "", "kind": "ghost", "why": "never written"},
            {"name": "Rete", "path": "R.md", "kind": "note", "why": "2 hops away", "score": 0.81},
        ],
    }
    d = _project_node(tmp_path, {"path": "ML.md"}, ctx)
    assert [r["name"] for r in d["missing"]] == ["Gradient descent"]
    assert [r["name"] for r in d["similar"]] == ["Rete"]
    assert d["similar"][0]["score"] == 0.81   # the meter draws a real number
    assert len(d["out"]) == 1 and len(d["from"]) == 1
    assert d["loading"] is False


def test_the_score_the_meter_draws_is_a_field_and_not_a_parsed_sentence():
    """`why` says "score 0.81" in words. Reading the number back out of a display
    string is how a sentence becomes an API: reword it and the bar goes blank."""
    src = (ROOT / "silica" / "ui" / "web" / "server.py").read_text()
    suggested = _block(src, "def _suggested(", "@app.get")
    assert '"score": float(' in suggested
    # and the panel never reads a number back out of the sentence. It draws no
    # meter any more (ADR-0075): on the note the change was measured on, eight
    # scores between 0.02 and 0.03 drew eight bars at the 6% floor, "weak"
    # said about the vault's best candidates.
    assert "match(" not in _block(WORK_JS.read_text(), "function projectNode", "projection:end")
    assert "wk-mtr" not in WORK_JS.read_text()


def test_pointing_at_a_node_fills_the_column_on_every_view():
    """The tab used to decide: explore got the column, every other view got the
    drawer's context mode. That WAS the duplicate. One payload, two renderers,
    already drifted apart -- the drawer drew snippets the column dropped, the
    column drew degree the drawer dropped. One reader now, so the tab decides
    nothing and openContext is gone from the file."""
    app = app_js()
    routing = _block(app, 'if (e.data.type === "silica-open-context")', "});")
    assert "activeTab" not in routing, "the view still decides where a node lands"
    assert "showNode(e.data)" in routing
    assert "openContext" not in app, "the drawer's context opener survived"
    # the panel hears one event, and app.js is the only place that fires it
    assert app.count('new CustomEvent("silica:node"') == 1
    assert 'document.addEventListener("silica:node"' in WORK_JS.read_text()


def test_the_drawer_keeps_no_second_reading_of_the_context():
    """The sidebar reads a note two ways: what it SAYS (the body) and what this
    session changed (the diff). Concepts and neighbours belong to `work`, once --
    which is a third segment of this same sidebar now rather than a panel beside
    it, and that makes the duplicate cheaper to reintroduce, not dearer: the two
    readings are one click apart. This is the test that fails if an edit brings
    the pair back by habit."""
    app, html, css = app_js(), INDEX.read_text(), app_css()
    for gone in ("renderContext", "openContext", "cxSection", "cxCloud", "cxList"):
        assert gone not in app, f"{gone} is still in app.js"
    assert "note-context" not in app and "note-context" not in html
    # Comments are exempt on purpose: the stylesheet keeps one line saying WHY the
    # metrics view's `bridge` button stopped being a `.cx-do`, which is the kind
    # of note a removal is supposed to leave behind. Rules are not exempt.
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert ".cx-" not in rules, "the context mode's CSS outlived its markup"
    # ...and the mode control carries the two readings that are left, beside the
    # node. Three segments, and `context` is not one of them. `work` is not one
    # of them either: that segment drew the run on chat and the node on explore,
    # one name over two subjects, and the run left for the rail.
    # ADR-0059 then removed the segment itself: no mode control at all.
    drawer = html[html.index('<aside id="note-panel"'):]
    assert "data-mode=" not in drawer[:drawer.index("</aside>")]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_the_projection_carries_what_only_the_drawer_used_to_show(tmp_path):
    """Three fields /context has always returned and this column always threw
    away: the key snippets (what the note SAYS, which is what makes the second
    click worth spending), the frontmatter `related:` list (not the same edge as
    a body wikilink), and the hint that says WHY similar came back thin."""
    ctx = {
        "title": "ML",
        "snippets": [{"heading": "Setup", "text": "A model is fit on..."}],
        "related": {"frontmatter": [{"name": "Corso", "path": "Corso.md"}],
                    "outgoing": [], "backlinks": []},
        "concepts": [], "suggested": [],
        "hint": "no embedding index",
    }
    d = _project_node(tmp_path, {"path": "ML.md"}, ctx)
    assert [s["heading"] for s in d["says"]] == ["Setup"]
    assert [r["name"] for r in d["related"]] == ["Corso"]
    assert d["hint"] == "no embedding index"


def test_the_pane_draws_the_note_on_every_view():
    """The node scope was gated on Explore and Metrics, so a note read from
    Chat, Learn or Calendar could not show its place at all (ADR-0075). The
    note open in the drawer is the subject now, whichever view opened it; the
    three graph selections keep their gate."""
    branch = _block(WORK_JS.read_text(), "function renderCtx() {", 'scope.textContent = "";')
    assert "if (node) { renderNode(); return; }" in branch
    assert "view === \"metrics\") && node" not in branch
    for sel in ("compare", "concepts", "bridge"):
        assert f'view === "graph" && {sel}' in branch


def test_a_click_off_a_row_drops_the_node_the_way_a_background_click_does():
    """The graph clears its selection when you click the background. Rows had no
    equivalent, so pointing at one was a one-way door: the column became that
    note and the report behind it was unreachable without leaving the view."""
    app = app_js()
    for pane in ("#metrics-body", "#shape-pane"):
        handler = _block(app, '$("' + pane + '").addEventListener("click"', "\n});")
        assert "announceNode(null, null)" in handler, pane + " cannot deselect"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_a_declared_relation_that_is_also_a_link_is_not_stated_twice(tmp_path):
    """Measured on the dev vault the day this section shipped: 7 of 7 frontmatter
    `related:` entries were ALSO under links out or linked from. Listing all of
    them put those rows on the panel twice, which is the duplicate this whole
    change exists to remove, rebuilt one heading lower. What survives is the half
    nothing else states: a relation the author declared and never linked."""
    ctx = {
        "related": {
            "frontmatter": [{"name": "Linked", "path": "L.md"},
                            {"name": "Back", "path": "B.md"},
                            {"name": "Only declared", "path": "D.md"},
                            {"name": "Nowhere", "path": ""}],
            "outgoing": [{"name": "Linked", "path": "L.md"}],
            "backlinks": [{"name": "Back", "path": "B.md"}],
        },
        "concepts": [], "suggested": [], "snippets": [],
    }
    d = _project_node(tmp_path, {"path": "A.md"}, ctx)
    assert [r["name"] for r in d["related"]] == ["Only declared", "Nowhere"]


def test_a_hub_does_not_unroll_its_whole_neighbourhood():
    """A note with sixty backlinks was sixty rows the moment this column took
    the drawer's lists over, because the fold lived in cxList and cxList is what
    left. Five rows say what kind of neighbourhood a note sits in; <details>
    owns the tail, because the browser already owns that toggle and it survives
    a re-render by not existing across one."""
    work = WORK_JS.read_text()
    assert "const NODE_VISIBLE = 5;" in work
    sec = _block(work, "function nodeSection(", "function renderNode(")
    assert "NODE_VISIBLE" in sec and 'el("details"' in sec


def test_a_suggested_row_says_which_folder_it_lives_in():
    """Every other section prints folderOf(path), because two notes can share a
    name and two rows both reading "Cell" are a coin flip rather than a list.
    Similar rows print the machine that found them instead, which left this as
    the one section where the flip survived the move out of the drawer."""
    row = _block(WORK_JS.read_text(), "function neighbourRow(r, d) {", "return row;")
    # ...and since ADR-0075 only where it is news: 9 of 13 rows on one note
    # printed the note's own folder, 73 characters of mono that said nothing.
    assert "fold && fold !== folderOf(d.path)" in row


def test_one_click_opens_the_note_and_there_is_no_second_one():
    """One click used to mean "what is this" and two "read it". The first click
    opens the note now (ADR-0075), so the frame sends one message per click and
    the parent has nothing listening for a second."""
    frame = FRAME_JS.read_text()
    sel = _block(frame, "function selectNode(", 'document.getElementById("drawer-title")')
    assert '"silica-open-context"' in sel
    assert '"silica-open-note"' not in sel and "event.detail" not in sel
    assert "setTimeout" not in sel, "a timer would hold the first click back"
    assert "silica-open-note" not in app_js()


def test_a_pointed_note_opens_and_only_what_has_no_text_raises_the_pane():
    """The click that fills the pane still opens the drawer, or it reads as a
    broken graph. What it opens on is the opener's call (ADR-0075): a note on
    its text or its details, through openNote; an unresolved link or a paper
    with no leaf on node mode. The pane's own listener raises nothing, since it
    cannot tell the two apart."""
    work, app = WORK_JS.read_text(), app_js()
    listener = _block(work, 'document.addEventListener("silica:node"', "});")
    assert "openNodeDrawer" not in listener
    show = _block(app, "async function showNode(target) {", "window.showNode = showNode;")
    assert "openNote(path, null, target); return;" in show
    assert show.index("openNote(path, null, target)") < show.index("window.openNodeDrawer()")
    assert show.count("window.openNodeDrawer()") == 3      # missing paper, rim paper, ghost
    assert "window.openNodeDrawer = " in app


def test_the_two_suggestion_prompts_are_written_once():
    """The drawer and the panel offer the same two rows over the same payload.
    A prompt written twice is two turns that drift the first time one of them is
    reworded -- the rule the metrics view's bulk prompts already follow."""
    app, work = app_js(), WORK_JS.read_text()
    for fn in ("function writeGhostPrompt(", "function linkNotesPrompt(",
               "function ghostWritePrompt("):
        assert app.count(fn) == 1, f"{fn} is not defined exactly once"
    # the panel calls them; it does not carry its own copy of the sentences
    assert "window.writeGhostPrompt" in work and "window.linkNotesPrompt" in work
    assert "Write the note" not in work and "belong linked" not in work
    # each shared sentence, once. (The metrics view's per-row write prompt is a
    # third, different sentence for a different row and is not one of these.)
    for sentence in ("already links to.", "It is already linked from", "belong linked"):
        assert app.count(sentence) == 1, f"{sentence!r} is written more than once"


def test_one_lighting_rule_serves_the_concept_pills():
    """The drawer's cloud and the panel's pills used to light the same notes
    through one function, which is what kept them from drifting. The cloud is
    gone and the rule outlives it: lightConcept stays exported and stays the
    only place that resolves a term and focuses whatever carries it, so a second
    concept surface cannot quietly grow its own."""
    app = app_js()
    assert app.count("async function lightConcept(") == 1
    assert "window.lightConcept = lightConcept;" in app
    # the selection is a set now: every pill is re-lit against it on each change
    assert 'querySelectorAll(".wk-pill")' in app and 'classList.toggle("lit"' in app
    assert ".wk-pill.lit {" in app_css()


# --- 2. the tabs (ADR-0058) -------------------------------------------------

def _modes(app: str) -> list[tuple[str, str]]:
    return re.findall(r'\["([a-z]+)", "[A-Za-z]+", "[^"]*", (\d)\]', _block(app, "const LAYOUT_MODES = [", "\n];"))


def test_the_tabs_are_the_only_place_the_surfaces_are_named():
    """Seven mode names written twice is two controls that eventually disagree
    about what this view can do, and the copy is always the one that is wrong.
    They were a toolbar, a list in the left rail, then a closed Layout fold a
    reader learned of by opening it; they are the view's navigation, so they are
    tabs at its top, and LAYOUT_MODES is still the only declaration.

    The label's CASE is not pinned: it is display copy. What must not drift is
    the set of mode ids, which is what every other file switches on, and their
    groups, which the row draws as space."""
    html, app = INDEX.read_text(), app_js()
    view = _block(html, '<section id="view-graph"', "</section>")
    assert 'id="surface-tabs"' in view
    for gone in ('id="legend"', 'id="lg-layout"', 'id="layout-modes"', 'id="lg-areas"', 'id="lg-surface"'):
        assert gone not in html, f"{gone} is back"
    modes = _modes(app)
    # ADR-0064: Bridges took the name Areas and the matrix with it, so the
    # mode id that names one part and its neighbours is "areas"
    assert [m for m, _ in modes] == ["graph", "map", "path", "folders", "areas", "read"], modes
    assert [g for _, g in modes] == ["0", "0", "0", "1", "1", "1"], "pictures of the links, then readings of the shape"
    # ADR-0066: the Library tab is pushed wherever Zotero answers, read-only
    # where this vault does not hold the link, and always in the third group.
    lib = app[app.index('LAYOUT_MODES.push(["library", "Library"'):]
    lib = lib[:lib.index("]);") + 3]
    assert "bring papers in, file the inbox, write groups" in lib and "read only here" in lib
    assert lib.endswith(", 2]);")
    # nothing in the markup declares a mode, so no second control can fall out
    # of step with the list
    assert "data-gmode" not in html
    builder = _block(app, "function buildSurfaceTabs() {", "\n}")
    assert '" stab-gap"' in builder
    assert "syncSurfaceTabs();" in _block(app, "function setGraphMode(", "\n}")


def test_the_line_under_the_tabs_is_a_key_on_every_surface_but_the_graph():
    """Each surface states what its marks mean where reading starts, in one
    line. The Graph's key is its panel; its line is scope and compare, which
    choose what you look at."""
    app = app_js()
    modes = {m for m, _ in _modes(app)}
    keys = re.findall(r'^  ([a-z]+): "', _block(app, "const SURFACE_KEYS = {", "\n};"), re.M)
    assert sorted(keys) == sorted((modes - {"graph"}) | {"library"}), keys
    line = _block(app, "function buildSurfaceLine() {", "\n}")
    assert "buildScopePicker()" in line and "buildComparePicker()" in line


def test_the_right_column_holds_find_scope_compare_over_the_key():
    """ADR-0072: the search, scope and compare are one column at the key's
    width and inset, and the key in the frame starts under it. The bar keeps
    out of the column's band wherever the column shows; the pane surfaces hide
    it and take the full width."""
    app, css = app_js(), app_css()
    side = _block(INDEX.read_text(), '<div id="graph-side">', '<div id="graph-loading"')
    assert 'id="node-search"' in side and 'id="graph-picks"' in side
    assert '$("#graph-side").hidden = isShape || isLibrary;' in app
    assert '$("#graph-bar").classList.toggle("by-side", !(isShape || isLibrary));' in app
    # the pickers land in the column, not on the line under the tabs
    line = _block(app, "function buildSurfaceLine()", "\n}\n")
    assert "picks.appendChild(buildScopePicker());" in line
    assert "box.appendChild(buildScopePicker());" not in line
    # padding, not `right`: on the path the bar is in flow
    assert "#graph-bar.by-side {\n  padding-right: 240px;" in css
    # the frame is told the column's height and starts its key under it
    assert 'type: "silica-host-side"' in app
    frame_js, frame_css = FRAME_JS.read_text(), FRAME_CSS.read_text()
    assert '"silica-host-side"' in frame_js and "var(--side-h,0px)" in frame_css
    pane = css[css.index("#shape-pane {"):]
    pane = pane[:pane.index("}")]
    assert "240px" not in pane and "with-bar" not in css


# --- 3. the renderer, on the frame's foot bar --------------------------------

def test_the_renderer_has_one_home_the_frames_foot_bar():
    """The view gives each edge one job (ADR-0058): the HUD is how to read the
    picture, the foot how it is made. The renderer was a segment in the app's
    legend AND the HUD's Renderer section, hidden embedded, with an ask/answer
    pair to keep the two in step; on the foot bar it has one home in both
    contexts, embedded and the exported graph.html. Forces opens from there."""
    app, markup, css, js = app_js(), FRAME_HTML.read_text(), FRAME_CSS.read_text(), FRAME_JS.read_text()
    foot = _block(markup, '<div id="gstat">', '<div id="hud">')
    assert 'id="mode-toggle"' in foot and 'id="forces-fold"' in foot
    hud = _block(markup, '<div id="hud">', '<div id="drawer">')
    assert "mode-toggle" not in hud and "forces-fold" not in hud and "Renderer" not in hud
    for gone in ("silica-set-renderer", "silica-renderer", "syncRenderer", "renderer-tabs",
                 "renderer-section", "silica-legend-h", "--legend-h", "syncLegendOffset"):
        assert gone not in app + markup + css + js, f"{gone} is back"


# --- 4. fit / reheat / the settle line ---------------------------------------

def test_fit_is_offered_once_and_reheat_is_gone():
    """Fit lived at the bottom of the HUD; the bar at the other corner is where
    it belongs. Two fit buttons is the same control twice. Reheat went on
    2026-10-06: auto-layout finishes the descent it only starts, and a force
    slider still makes the same perturbation."""
    markup, js = FRAME_HTML.read_text(), FRAME_JS.read_text()
    assert 'id="gstat"' in markup
    assert (markup + js).count('onclick="fitGraph()"') == 1
    assert "reheat()" not in markup and "function reheat()" not in js
    assert "Fit graph" not in markup, "the HUD's fit row is back"


def test_the_foot_is_two_corners_anchored_beside_the_dock():
    """What you press bottom-left, the simulation (Forces and its size)
    bottom-right, so the open composer has the middle. Since ADR-0072 both sit
    on the frame's bottom edge and the app caps its unfolded composer between
    them; under 912px of dock it cannot, says so (`lift`), and they ride above
    the dock, whose height the app posts because a run line, the answer card
    and a multi-row draft grow it during a turn."""
    markup, css, js = FRAME_HTML.read_text(), FRAME_CSS.read_text(), FRAME_JS.read_text()
    left = _block(markup, '<div id="gstat">', '<div id="gsim">')
    right = _block(markup, '<div id="gsim">', '<div id="hud">')
    for chip in ('id="mode-toggle"', 'onclick="fitGraph()"', 'onclick="autoLayout()"'):
        assert chip in left and chip not in right, chip
    assert 'id="forces-fold"' in right and 'id="gstat-n"' in right
    assert "body{--foot:12px}" in css and "body.embedded{--foot:16px}" in css
    assert "body.embedded.foot-lift{--foot:calc(var(--dock-h,72px) + 12px)}" in css
    assert '"silica-host-dock"' in js and 'classList.toggle("foot-lift", !!e.data.lift)' in js
    host = app_js()
    assert 'postToViews({ type: "silica-host-dock", h: dock.offsetHeight, lift: dock.offsetWidth < 912 });' in host
    # the cap reads the same 912px the lift does, on the same box
    assert "@container (min-width: 912px)" in app_css()
    assert "syncDockToViews(); //" in host  # replayed when /graph is rebuilt


def test_the_settle_count_includes_the_warmup_it_cannot_observe():
    """The bundle runs warmupTicks as a plain forceLayout.tick() loop with no
    onEngineTick call, so counting only the animated tail would under-report the
    settle by up to 240 ticks on a cold build."""
    js = FRAME_JS.read_text()
    assert ".onEngineTick(() => { simTicks++; descendTick(); })" in js
    assert 'paintStat("settled in " + gnum(warmupRun + simTicks) + " ticks");' in js
    assert "warmupRun = warm;" in js


def test_auto_layout_finishes_the_descent_a_reheat_only_starts():
    """The alpha gate freezes a layout at tick ~300 whatever the forces still
    say. Measured 2026-09-21 on the 1,195-node test vault: the next reheat still
    moved every node 100 units on average; after one auto-layout it moved 21,
    after two 13. auto-layout holds alpha instead of decaying it and lets go on
    a residual or a budget, and the budget is not optional: the median step
    plateaus at ~3 units on that vault, above any eps that means "at rest"."""
    markup, js = FRAME_HTML.read_text(), FRAME_JS.read_text()
    assert 'onclick="autoLayout()"' in markup
    body = _block(js, "function autoLayout() {", "\n}")
    assert "Graph.d3AlphaMin(0);" in body       # the drag deadlock, same cure
    assert "applyForces(true);" in body          # still the one definition of "stir it"
    assert "Graph.d3AlphaDecay(0);" in body      # the hold: alpha never cools
    tick = _block(js, "function descendTick() {", "\n}")
    assert "r < RESIDUAL_EPS || --residualTicks === 0" in tick
    assert "Graph.d3AlphaDecay(FAST_DECAY).d3AlphaMin(ALPHA_MIN);" in tick
    assert ".onEngineTick(() => { simTicks++; descendTick(); })" in js
    # the residual is the median step, not the max: one hub's Barnes-Hut noise
    # would otherwise report a settled graph as moving
    assert "return st[st.length >> 1] || 0;" in _block(js, "function residual() {", "\n}")


# --- 5. the rail's floor -----------------------------------------------------

def test_the_full_path_is_readable_without_hovering_it():
    """The foot names the vault by its last segment, which is what identifies
    it at a glance. The full path was a tooltip, and a path you can only get by
    hovering is a path you cannot read while comparing two windows. It stood at
    the rail's floor until ADR-0084 folded that line into the vault's panel,
    whose head it is, beside the switch."""
    html, app, css = INDEX.read_text(), app_js(), app_css()
    head = _block(html, '<div id="vp-head">', "</div>")
    assert 'id="vp-path"' in head and 'id="vault-switch"' in head
    assert 'id="railfoot"' not in html
    setter = _block(app, "function setVaultPath(", "\n}")
    assert '$("#vp-path")' in setter and '$("#vault-chip").hidden = !path;' in setter
    # last, and last is enough: the rail itself no longer scrolls, so the floor
    # cannot be scrolled off it. Its compartments do, inside themselves.
    foot = _block(css, "#side-foot {", "}")
    assert "margin-top: auto" in foot
    rail_css = _block(css, "#sidebar {", "\n}")
    assert "overflow: hidden" in rail_css, "the rail scrolls again"
    assert "overflow-y: auto" in _block(css, ".side-body {", "\n}")
    # clipped at the start: the end of a path is the folder that names the vault
    assert "direction: rtl;" in _block(css, "#vp-path {", "}")


def test_a_click_the_panel_answers_does_not_read_as_a_click_outside_it():
    """The outside-click rule closed the sidebar on every row that re-rendered.

    A Read-first row's own handler calls render(), which does `body.textContent
    = ""` and detaches the button the click started on. The document handler
    runs after that, and `closest("#note-panel")` on a node with no parent
    answers null for every selector -- so the click that asked the panel a
    question was read as a click away from it. isConnected has to be checked,
    and checked BEFORE the closest() calls it invalidates.
    """
    app = app_js()
    guard = _block(app, "if (drawerBefore && drawerBefore === drawerNow()", "closeNote();")
    assert "e.target.isConnected" in guard
    assert guard.index("e.target.isConnected") < guard.index('closest("#note-panel")')


def test_naming_a_neighbour_row_opens_it_in_the_view_the_drawer_is_in():
    """A row names a note, so it opens it, and openNote loads its context too:
    walking the neighbours with details on stays in details (ADR-0075). The
    second call that kept the pane behind the note in step is gone with the
    pane behind it."""
    work = WORK_JS.read_text()
    for fn in ("function nodeRow(r, opt)", "function neighbourRow(r, d) {"):
        row = _block(work, fn, "return row;")
        assert "window.openNote(r.path)" in row, fn
        assert "showNode({ path: r.path })" not in row, fn
    app = app_js()
    assert "loadContext(data.path, facts);" in _block(app, "async function openNote(", "\n}")


def test_a_lit_concept_can_be_put_out_by_the_pill_that_lit_it():
    """A set you can only swap for another set is a mode with no exit: before
    this there was no gesture at all for "drop the filter, show me the graph"."""
    light = _block(app_js(), "async function lightConcept(", "window.lightConcept")
    # membership lives in the set, not on the button: a chip in the work panel
    # drops a concept with no button of its own
    assert "litConcepts.indexOf(term)" in light and "litConcepts.splice(" in light
    assert "focusGraphNodes([])" in light


def test_the_ladder_footer_roots_path_before_it_enters_the_tab():
    """Path has no root-replay on tab-enter, the way map does.

    showTab -> setGraphMode("path") -> drawPath() reads pathRootedPath and
    nothing else, so pre-setting graphMode the way the map button does starts an
    UNROOTED draw first -- and the landing measures forty ladders, so it also
    finishes last and overwrites the rooted one. rootPath has to run before the
    tab click, not after it.
    """
    app = app_js()
    body = _block(app, "window.openLadder = (note) => {", "};")
    assert 'graphMode = "path"' not in body, "pre-setting the mode races the rooted draw"
    assert body.index("rootPath(note)") < body.index('.tab[data-tab="graph"]')
    # and the panel is the only caller: it knows its note and nothing about modes
    assert "window.openLadder" in WORK_JS.read_text()


# --- 4. areas: one part and its neighbours, over the coupling of every pair --
# ADR-0060 drew WHICH links join one part to the others; ADR-0064 folds the
# matrix that says HOW MANY into the same surface, under the drawing's index.

def test_areas_is_one_shape_view_fed_by_the_shape_load():
    """One /shape load feeds it like the other pane surfaces, both readings: a
    second endpoint would be a second build of the same graph that can
    disagree with the matrix beside it. The matrix's own surface is gone."""
    app = app_js()
    views = _block(app, "const SHAPE_VIEWS = {", "};")
    assert "areas: renderAreas" in views and "bridges" not in views
    body = _block(app, "function renderAreas(s) {", "\n}")
    assert "s.bridges" in body and "s.zone_bridges" in body
    assert "couplingMatrix(" in body and "s.matrix" in body
    assert "function renderBridges(" not in app


def test_an_old_bridges_link_lands_on_areas():
    """#explore/bridges was the surface's address from ADR-0045 to ADR-0064: a
    bookmark of it opens the surface it became, not the Graph."""
    parse = _block(app_js(), "function parseHash() {", "\n}")
    assert '"bridges"' in parse and '"areas"' in parse


def test_the_centre_is_the_scope_and_defaults_to_the_rarest_link():
    """Decision 1: one state across Graph and Areas, so the centre is read
    from the scope and written back to it; with no scope, the source part of
    the row ADR-0045 ranks first."""
    body = _block(app_js(), "function renderAreas(s) {", "\n}")
    assert "graphScope.kind === kind" in body and "setGraphScope(scopeOf(id))" in body
    assert "rows[0].a.part_id" in body


def test_a_coupling_cell_opens_its_pair():
    """Decision 4: a cell (A, B) on the centre opens the other end's group in
    place; off it, it recentres on A with the B spoke open."""
    app = app_js()
    grid = _block(app, "function couplingMatrix(areas, matrix,", "\n}")
    assert "onPair(a.id, b.id)" in grid
    body = _block(app, "function renderAreas(s) {", "\n}")
    assert "onPair: openPair" in body and "openBridges(x, y, bridgePartition)" in body
    opener = _block(app, "function openBridges(x, y,", "\n}")
    assert "bridgeSpoke = y" in opener and "setGraphScope(" in opener and 'setGraphMode("areas")' in opener
    assert "bridgePair" not in app, "the flat list's pair filter is gone with the list"


def test_the_matrix_holds_only_the_parts_that_link():
    """ADR-0064: a part with no link to another is a row and a column of empty
    cells (8 of 22 areas on the test vault); the drawing already says so by
    drawing its disc alone, so the grid leaves it out. Zones have no matrix on
    the payload: theirs is summed from the rows, whose links_between is the
    same tally the area matrix reads."""
    body = _block(app_js(), "function renderAreas(s) {", "\n}")
    assert ".some((v, j) => j !== i && v)" in body
    assert "r.links_between" in body


def test_the_folded_matrix_names_its_parts_by_colour_and_one_renderer_draws_it():
    """Two renderers over one payload is two places to fix a contrast bug: the
    column's grid is couplingMatrix with swatch heads, not a second grid."""
    grid = _block(app_js(), "function couplingMatrix(areas, matrix,", "\n}")
    assert "swatch" in grid and "onPart" in grid
    assert "c.dataset.i = a.id" in grid and "c.dataset.j = b.id" in grid
    css = app_css()
    assert ".smx.fit {" in css and ".smx-sw {" in css


def test_the_index_is_a_layer_over_the_matrix():
    """ADR-0064: a group opens in place and the list grows down over the
    matrix, which never moves; where the list covers it, the glass blur keeps
    the rows readable. The column reserves the closed index of the busiest
    centre, so a recentre does not move the matrix either."""
    css = _block(app_css(), "/* bridges: one part and its neighbours", "/* the sentence that carries a link")
    layer = _block(css, ".ego-index {", "}")
    assert "position: absolute" in layer and "backdrop-filter: var(--glass-blur)" in layer
    assert ".ego-reserve {" in css
    body = _block(app_js(), "function renderAreas(s) {", "\n}")
    assert '"--slots"' in body


def test_the_gestures_go_where_the_decision_sends_them():
    """Decision 4: a line fills the work panel (renderBridge), a neighbour
    becomes the centre, a dot opens its note, "show in graph" scopes the Graph."""
    body = _block(app_js(), "function renderAreas(s) {", "\n}")
    assert 'new CustomEvent("silica:bridge"' in body
    assert "pickEgo(id)" in body
    assert "openNote(end.path)" in body
    assert 'setGraphMode("graph")' in body
    # every mark's click stops at the mark, or the pane's own handler clears the
    # work panel the line has just filled
    assert body.count("ev.stopPropagation()") >= 5


def test_nothing_on_areas_is_told_apart_by_width_or_dashes():
    """Decision 3 and ADR-0058 Decision 11: the count is a label and the part a
    colour; a frontmatter link is fainter, because dashed means absent."""
    body = _block(app_js(), "function renderAreas(s) {", "\n}")
    assert "stroke-width" not in body and "r: RD" in body
    css = _block(app_css(), "/* bridges: one part and its neighbours", "/* the sentence that carries a link")
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "dasharray" not in rules
    widths = set(re.findall(r"\.ego-ln-ink \{[^}]*stroke-width: ([\d.]+)", rules))
    assert widths == {"1.5"}
    for sel in (".ego-ln:hover .ego-ln-ink,\n.ego-ln.on .ego-ln-ink {", ".ego-ln.fm .ego-ln-ink {", ".ego-ln.off .ego-ln-ink {"):
        assert "stroke-width" not in _block(rules, sel, "}")


def test_the_index_is_the_keyboard_way_through():
    """Decision 6: grouped by neighbour in ring order; the arrows walk it, and
    the selection is painted in place so the focus survives it."""
    body = _block(app_js(), "function renderAreas(s) {", "\n}")
    assert '"ArrowDown"' in body and "next.focus()" in body
    assert "aria-expanded" in body
    pick = _block(body, "function pickLink(m) {", "\n  }")
    assert "paint()" in pick and "drawShape()" not in pick


def test_no_two_classic_scripts_declare_one_top_level_name():
    """index.html loads its scripts as classic scripts, which share ONE global
    scope: a second top-level `const plural` (Bridges, 2026-10-06, against
    work.js's) is a SyntaxError that drops the whole later file, and the work
    panel went blank with no error anywhere but the console."""
    html = INDEX.read_text()
    files = [f for f in re.findall(r'<script(?![^>]*type="module")[^>]*src="/static/([\w./-]+\.js)"', html)
             if "vendor" not in f]
    owner: dict[str, str] = {}
    clashes = []
    for f in files:
        text = (WEB / f).read_text()
        # `= ` after the name, so a GLSL `const float x` inside a shader string
        # is not read as a declaration of `float`
        for name in re.findall(r"^(?:const|let|class)\s+(\w+)\s*[=({]", text, re.M):
            if name in owner and owner[name] != f:
                clashes.append((name, owner[name], f))
            owner.setdefault(name, f)
    assert len(files) > 5 and not clashes, clashes


def test_a_pane_surface_writes_itself_into_the_url_like_the_graph_does():
    """P7: the surface is state. setGraphMode synced the URL on its last line,
    after the pane views (folders, areas, bridges, read, path) had already
    returned, so #explore/areas never appeared and Back walked past four of
    the seven surfaces. Measured in the browser 2026-09-22: setGraphMode("read")
    left the hash at #explore/areas. The sync has to come before that return."""
    body = _block(app_js(), "function setGraphMode(m) {", "\n}")
    assert body.index("syncUrl(false)") < body.index("if (isShape || isPath || isLibrary) {"), \
        "the URL sync sits after the pane views' early return"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_details_links_name_every_neighbour_once_with_its_relations_as_tags(tmp_path):
    """ADR-0075, on the shape of the note it was measured on: seven sections
    listed Forward chaining under Links out AND Linked from, Backtracking under
    Reading order AND Links out. One row per note now, the relations as tags,
    linked first, the notes behind the bridge first inside their group."""
    ctx = {
        "title": "BC", "path": "K/BC.md",
        "related": {"frontmatter": [],
                    "outgoing": [{"name": "F", "path": "K/F.md"}, {"name": "Base", "path": "K/Base.md"},
                                 {"name": "BT", "path": "K/BT.md"}],
                    "backlinks": [{"name": "F", "path": "K/F.md"}, {"name": "CP", "path": "K/CP.md"}]},
        "suggested": [
            {"name": "RD", "path": "K/RD.md", "kind": "note", "why": "3 hops away · score 0.03"},
            {"name": "L23", "path": "ML/L23.md", "kind": "note", "why": "unreachable · score 0.02"},
            {"name": "Ghost", "path": "", "kind": "ghost", "why": "linked from here, never written"},
        ],
        "ladder": {"root": "K/BC.md", "further": 0, "rungs": [
            {"depth": -1, "notes": [{"name": "Psi", "path": "P/Psi.md"}], "hidden": 0},
            {"depth": 0, "notes": [{"name": "BC", "path": "K/BC.md"}], "hidden": 0},
            {"depth": 1, "notes": [{"name": "BT", "path": "K/BT.md"}, {"name": "RD", "path": "K/RD.md"}],
             "hidden": 0}]},
        "structure": {"articulation": True, "strands": 3, "coreness": 1,
                      "behind": ["K/F.md", "K/CP.md", "K/RD.md"]},
    }
    rows = _project_node(tmp_path, {"path": "K/BC.md"}, ctx)["neighbours"]
    names = [r["name"] for r in rows]
    assert names == ["F", "CP", "Base", "BT", "RD", "Psi", "L23", "Ghost"]
    assert len(set(names)) == len(names)            # the note itself is not its neighbour
    tags = {r["name"]: r["tags"] for r in rows}
    assert tags["F"] == ["both", "behind"]
    assert tags["BT"] == ["out", "+1"]
    assert tags["RD"] == ["+1", "similar", "behind"]
    assert tags["Psi"] == ["-1"] and tags["Ghost"] == ["unwritten"]
    assert [r["name"] for r in rows if r["unreachable"]] == ["L23"]
