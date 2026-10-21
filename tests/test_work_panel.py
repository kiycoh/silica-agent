# tests/test_work_panel.py
"""The work panel projects the narration stream, and the projection is the seam.

`/narration/sse` has been written and served since the narration shipped, and
until now nothing in the GUI read it: the panel is a projection of a file that
already exists. These tests pin the two things that can silently rot — the
fold that attaches an llm call to the thought that ran it, and the verb table
that has to stay in step with `silica/ui/renderer.py`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web"
WORK_JS = WEB / "static" / "work.js"
INDEX = WEB / "static" / "index.html"

from tests.webassets import app_css, app_js

# The shape is the real one: silica/agent/narration.py writes these records and
# `read_beats` hands them back verbatim. A thought span WRAPS the llm call that
# produced it, both close on the same tick, and neither carries the other as a
# parent — which is exactly why the fold below cannot key on `parent`.
TRACE = [
    {"seq": 1, "ts": 100.0, "kind": "session", "status": "done", "id": None,
     "summary": "gui session, vault test", "payload": {"driver": "gui"}},
    {"seq": 2, "ts": 100.0, "kind": "turn", "status": "done", "id": None,
     "summary": "user: cerca utilitarismo",
     "payload": {"message": {"role": "user", "content": "cerca utilitarismo"}}},
    {"seq": 3, "ts": 100.0, "kind": "thought", "status": "running", "id": "th-3",
     "summary": "thinking", "payload": {}},
    {"seq": 4, "ts": 100.0, "kind": "call", "status": "running", "id": "c-1",
     "summary": "llm deepseek", "payload": {"model": "deepseek-v4-flash"}},
    {"seq": 5, "ts": 104.0, "kind": "call", "status": "done", "id": "c-1",
     "summary": "llm deepseek 10600 to 64 tok",
     "payload": {"model": "deepseek-v4-flash", "prompt_tokens": 10600,
                 "completion_tokens": 64, "cached_tokens": 0}},
    {"seq": 6, "ts": 104.0, "kind": "thought", "status": "done", "id": "th-3",
     "summary": "Cerchiamo note che menzionano utilitarismo.",
     "payload": {"text": "Cerchiamo note che menzionano utilitarismo.", "duration_s": 3.991}},
    {"seq": 7, "ts": 104.0, "kind": "tool", "status": "running", "id": "t-1",
     "summary": "silica_search_context",
     "payload": {"name": "silica_search_context", "args": {"query": "utilitarismo"}}},
    {"seq": 8, "ts": 104.02, "kind": "tool", "status": "done", "id": "t-1",
     "summary": "silica_search_context done",
     "payload": {"name": "silica_search_context", "result": "{}"}},
    {"seq": 9, "ts": 104.02, "kind": "tool", "status": "running", "id": "t-2",
     "summary": "silica_read_note",
     "payload": {"name": "silica_read_note", "args": {"name": "Morale"}}},
    {"seq": 10, "ts": 104.05, "kind": "tool", "status": "done", "id": "t-2",
     "summary": "silica_read_note done", "payload": {"name": "silica_read_note"}},
    {"seq": 11, "ts": 104.05, "kind": "tool", "status": "running", "id": "t-3",
     "summary": "silica_patch_note",
     "payload": {"name": "silica_patch_note", "args": {"name": "Filosofia/Morale.md"}}},
    {"seq": 12, "ts": 104.2, "kind": "tool", "status": "done", "id": "t-3",
     "summary": "silica_patch_note done", "payload": {"name": "silica_patch_note"}},
]


def _pure_block() -> str:
    """The projection half of work.js: pure, so it runs outside a browser."""
    src = WORK_JS.read_text()
    m = re.search(r"// --- projection:begin.*?// --- projection:end", src, re.S)
    assert m, "projection markers not found in work.js"
    block = m.group(0)
    for banned in ("document.", "window.", "fetch(", "EventSource"):
        assert banned not in block, f"the projection block reaches for {banned}"
    return block


def _project(tmp_path, beats: list[dict]) -> dict:
    """Run projectRun() from work.js under node, on these beats."""
    script = tmp_path / "project.js"
    script.write_text(
        _pure_block()
        + "\nconsole.log(JSON.stringify(projectRun(JSON.parse(process.argv[2]))));\n"
    )
    out = subprocess.run(["node", str(script), json.dumps(beats)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_a_call_folds_into_the_thought_that_ran_it(tmp_path):
    """One row, not two. The call and the thought open and close as one span in
    the log, so printing both prints the same 3.99 seconds twice."""
    run = _project(tmp_path, TRACE)
    thoughts = [r for r in run["rows"] if r["kind"] == "think"]
    assert len(thoughts) == 1
    assert thoughts[0]["text"] == "Cerchiamo note che menzionano utilitarismo."
    assert "10,600" in thoughts[0]["sub"] and "64" in thoughts[0]["sub"]
    assert thoughts[0]["dur"] == "4.0s"
    # and the call is not also a row of its own
    assert not [r for r in run["rows"] if r["kind"] == "call"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_tools_carry_the_verb_and_the_target_the_renderer_would_print(tmp_path):
    run = _project(tmp_path, TRACE)
    tools = [r for r in run["rows"] if r["kind"] == "tool"]
    assert [(t["verb"], t["target"]) for t in tools] == [
        ("search inside", "utilitarismo"),  # search_context, not silica_search
        ("read", "Morale"),
        ("patch note", "Filosofia/Morale.md"),
    ]
    assert tools[0]["dur"] == "0.0s"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_a_write_tool_becomes_a_write_and_a_read_becomes_a_source(tmp_path):
    """The panel's two lists are derived, not separately reported: whether a
    tool changed the vault is the same table `_TOOL_EFFECT` keys on."""
    run = _project(tmp_path, TRACE)
    assert [w["target"] for w in run["writes"]] == ["Filosofia/Morale.md"]
    assert "Morale" in [s["name"] for s in run["sources"]]
    assert "Filosofia/Morale.md" not in [s["name"] for s in run["sources"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_an_unclosed_span_reads_as_running(tmp_path):
    """The live case: the panel renders mid-run, so the last beat is an open
    span and the projection has to say so rather than waiting for a close."""
    run = _project(tmp_path, TRACE[:7])
    assert run["running"] is True
    assert run["rows"][-1]["running"] is True
    done = _project(tmp_path, TRACE)
    assert done["running"] is False
    assert not [r for r in done["rows"] if r["running"]]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_the_user_turn_titles_the_run(tmp_path):
    run = _project(tmp_path, TRACE)
    assert run["title"] == "cerca utilitarismo"


def test_the_verb_table_cannot_drift_from_the_renderer():
    """work.js carries its own copy of the verb table, because the narration
    payload holds the RAW tool name and the browser has no renderer. A copy that
    nothing checks is a copy that goes stale the next time a tool is added."""
    from silica.ui.renderer import _TOOL_DESC

    src = WORK_JS.read_text()
    m = re.search(r"^const TOOL_VERBS = \{(.*?)^\};", src, re.S | re.M)
    assert m, "TOOL_VERBS not found in work.js"
    js_full = {name: (verb, key or None) for name, verb, key in
               re.findall(r'"(\w+)":\s*\["([^"]*)",\s*(?:"([^"]*)"|null)\]', m.group(1))}
    js = js_full
    assert js, "no entries parsed out of TOOL_VERBS"
    missing = sorted(set(_TOOL_DESC) - set(js))
    assert not missing, f"work.js TOOL_VERBS is missing {missing}"
    extra = sorted(set(js) - set(_TOOL_DESC))
    assert not extra, f"work.js TOOL_VERBS invents {extra}"
    for name, (verb, key) in _TOOL_DESC.items():
        assert js_full[name] == (verb, key), f"{name}: {js_full[name]} != {(verb, key)}"


def test_the_panel_is_wired_into_the_page():
    """The pane is part of the shell, not a widget bolted onto chat: the markup,
    the stylesheet and the script all have to be there."""
    html = INDEX.read_text()
    assert 'id="node-pane"' in html
    assert '/static/work.js' in html
    # loaded after every cut of app.js: it calls openNote() (app-notes.js) and
    # send() (app-chat.js), and the last cut is the one that has to precede it.
    import re

    cuts = [m.end() for m in re.finditer(r"/static/app-[\w-]+\.js", html)]
    assert cuts, "index.html loads no app-*.js"
    assert max(cuts) < html.index("/static/work.js")
    assert "/narration/sse" in WORK_JS.read_text()


def test_the_run_and_the_node_are_two_panes_at_two_edges():
    """One name used to cover both. The `work` segment drew the RUN on chat and
    the NODE on explore, so the label was wrong half the time and the run was
    unreadable whenever you were reading a note. The run became a rail
    compartment (left, always there) and left the rail with ADR-0122, for the
    transcript's ledger line; the drawer kept the node and took its name.

    The rules that once kept a second right-edge aside out of the drawer's way
    have to stay gone, not merely unused."""
    html, css, js = INDEX.read_text(), app_css(), WORK_JS.read_text()
    drawer = html[html.index('<aside id="note-panel"'):]
    for pane in ("note-edit", "note-diff", "node-pane"):
        assert f'id="{pane}"' in drawer, f"{pane} is not a pane of the drawer"
    assert 'id="work-body"' not in drawer, "the run is back in the right drawer"
    # no segment names the modes since ADR-0059: each is entered the way its
    # subject is reached, and the `changes` chip leads back from the diff
    assert "data-mode=" not in drawer[:drawer.index("</aside>")]
    # the run's pane was a compartment of the left drawer, "This chat", until
    # ADR-0122: the transcript's ledger line says its steps and reads where the
    # answer is (ADR-0117 point 3), and its state is the Chats mark
    rail = html[html.index('<aside id="sidebar">'):html.index('<section id="view-chat"')]
    assert 'id="work-body"' not in rail and 'id="side-chat"' not in rail
    assert "markRun(run.running);" in js
    # one surface at this edge, so nothing hides one half of it from the other
    assert '<aside id="work"' not in html
    # comments stripped: the rules are named in the prose that explains why they
    # are gone, and a rationale that quotes what it removed is the point of it
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for dead in ("body.note-open #work", "body.work-open", "body.work-fold"):
        assert dead not in rules, f"{dead} still arbitrates an edge with one panel"
    assert "work-open" not in js, "work.js still owns whether the sidebar is open"


def test_the_scope_chip_rides_the_shared_header_and_nothing_renames_it():
    """The pane used to rewrite its own header to "Node", which is what the note
    drawer's `context` mode said under a different word. The name is the active
    segment now and the scope is a chip beside it, in the sidebar's ONE header --
    above the panes, so it is stated once whichever pane is showing."""
    html, js = INDEX.read_text(), WORK_JS.read_text()
    head = html[html.index('<aside id="note-panel"'):html.index('id="note-diff"')]
    assert 'id="node-scope"' in head and 'id="node-state"' in head
    # the run's own dot rode "This chat"; it is the Chats mark since ADR-0122
    assert 'id="work-state"' not in html
    # nothing writes over the segment's name: the two readings set the chip
    assert "work-title" not in js and "wk-title" not in html
    # "node" is not among them: the pane's own head names what was pointed at,
    # and the chip earns its place on the readings that are not a node, a
    # bridge pointed at on the graph (ADR-0045) and its kin. The report was one
    # until ADR-0084 retired it with its only entry.
    assert sorted(re.findall(r'scope\.textContent = "(\w*)"', js)) == ["", "", "bridge", "compare", "concepts"]


def test_the_toggle_restores_a_mode_rather_than_picking_one():
    """A segment can go dead between two opens -- `diff` needs this session to
    have touched the file, `node` needs something to have been pointed at -- so
    reopening on the remembered mode blindly lands on an empty pane. The ladder
    degrades. Since ADR-0084 it has no empty rung: the toggle exists only once
    the drawer has held something, which is exactly what the ladder restores."""
    app = app_js()
    fn = app[app.index("function reopenDrawer() {"):]
    fn = fn[:fn.index("\n}")]
    assert 'drawerMode === "diff" && changedPaths.has(path)' in fn
    assert 'drawerMode === "node" && nodePicked' in fn
    assert "openNodePane();\n  // Nothing" not in fn and not fn.rstrip().endswith("openNodePane();")
    # the toggle's visibility is the ladder's own precondition
    assert "toggle.hidden = open || !(lastViewedPath || nodePicked);" in app
    assert 'document.body.classList.toggle("drawer-recall", !toggle.hidden);' in app
    # and the flag that gates `node` is set by the PICK, not by the opener the
    # toggle shares with it -- or a node nobody chose would summon the toggle
    assert "window.openNodeDrawer = () => { nodePicked = true; openNodePane(); };" in app
    assert "nodePicked = true" not in app[app.index("function openNodePane() {"):
                                          app.index("window.openNodeDrawer")]
    # a vault switch leaves nothing to reopen
    forget = app[app.index("function forgetDrawer() {"):]
    assert "lastViewedPath = null;" in forget[:forget.index("\n}")]
    assert "forgetDrawer();" in app


def test_the_drawer_glyph_is_on_the_button_and_nowhere_else():
    """The window carries exactly one control for the right drawer, in the
    view's corner since ADR-0084, it wears the drawer's glyph, and the drawer
    does not wear it again.

    Two things were wrong before. It briefly opened the rail's Work compartment
    — a header button for something that already has a <summary> and an icon in
    the folded rail. And the open drawer repeated the same glyph in its own
    header: a sign saying "this is the drawer", addressed to someone already
    reading the drawer. On the button it names a surface you cannot see; inside
    it names nothing."""
    html = INDEX.read_text()
    assert 'id="work-toggle"' not in html, "the compartment button is back in the header"
    toggle = html[html.index('id="drawer-toggle"'):]
    toggle = toggle[:toggle.index("</button>")]

    assert re.search(r"<rect[^>]*>", toggle), "the button lost the drawer glyph"
    drawer = html[html.index('<aside id="note-panel"'):html.index('id="note-diff"')]
    assert "np-ico" not in re.sub(r"<!--.*?-->", "", drawer, flags=re.S), \
        "the open drawer states its own name again"
    assert "np-ico" not in app_css()
    # and it is the ONE control: #note-last was an accent-blue document glyph
    # beside it opening the same sidebar on a different mode. Markup only —
    # index.html keeps a line saying what stood there and why it does not.
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert "note-last" not in markup
    assert "note-last" not in app_js()


def test_a_click_that_moves_the_sidebar_never_also_closes_it():
    """The outside-click rule closes the sidebar, and it used to key on "is the
    panel open", read live on the way UP. With three modes that is wrong twice:
    a metrics or shape row POINTS at a note, raising the sidebar on `work` from
    its own listener, and the delegated handler then closed what the click had
    just asked for -- whether the click opened the sidebar or swung it off a note.
    The sample is taken in the capture pass and compared, so the rule is "did this
    click leave it alone" rather than "was it open"."""
    app = app_js()
    assert '{ drawerBefore = drawerNow(); openBefore = openSeq; }, true)' in app, "the sample is not captured"
    # openSeq too: a row opens its note after a fetch (ADR-0075), so the drawer
    # has not moved when the rule runs, and the click would close it
    assert "drawerBefore && drawerBefore === drawerNow() && openBefore === openSeq" in app, \
        "the close still keys on a live read"
    # the sample is registered before the handler that reads it
    assert app.index("drawerBefore = drawerNow()") < app.index("drawerBefore === drawerNow()")


def test_the_run_needs_no_boot_preference_to_survive_a_reload():
    """There was a `work-open` key and an ordering puzzle around it: the pane had
    to be reopened AFTER the boot tab (which closed the drawer) and the key read
    BEFORE the first sync (which overwrote it). Both went when the run became a
    rail compartment, and the compartment went with ADR-0122: nothing about the
    run is restored at boot, because the transcript replays it."""
    app = app_js()
    html = INDEX.read_text()
    assert "bootWantsWork" not in app and "work-open" not in app
    assert 'id="side-chat"' not in html and '"side-chat"' not in app


def test_every_list_has_a_rail_icon_and_a_segment():
    """Below the 1195 floor the lists ARE their icons (#railmini), so each pane
    has one, named by its label and its tooltip, and one button of the
    segment that shows it (ADR-0122). A pane with neither is a list nobody can
    reach once the other one is in view."""
    html = INDEX.read_text()
    rail = html[html.index('<aside id="railmini"'):html.index('<aside id="sidebar">')]
    sidebar = html[html.index('<aside id="sidebar">'):html.index('<section id="view-chat"')]
    panes = re.findall(r'<section class="side-pane" id="(side-\w+)"', sidebar)
    assert len(panes) == 2, panes  # the notes, the chats
    seg = sidebar[sidebar.index('<div id="side-tabs"'):]
    seg = seg[:seg.index("</div>")]
    for pane in panes:
        btn = re.search(rf'<button[^>]*data-sec="{pane}"[^>]*>', rail)
        assert btn, f"{pane} has no button in the folded rail"
        assert "aria-label=" in btn.group(0) and "data-tip=" in btn.group(0)
        assert f'data-pane="{pane}"' in seg, f"{pane} has no segment button"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run work.js")
def test_a_retried_call_keeps_its_first_start(tmp_path):
    """A transient retry re-narrates the call span as still running; the row's
    duration must count from the first attempt, not from the last retry."""
    beats = [
        TRACE[0], TRACE[1],
        {"seq": 3, "ts": 100.0, "kind": "call", "status": "running", "id": "c-1",
         "summary": "llm deepseek", "payload": {"model": "deepseek-v4-flash"}},
        {"seq": 4, "ts": 102.0, "kind": "call", "status": "running", "id": "c-1",
         "summary": "llm deepseek retry 1/3 in 1s: Timeout",
         "payload": {"model": "deepseek-v4-flash", "attempt": 1}},
        {"seq": 5, "ts": 104.0, "kind": "call", "status": "done", "id": "c-1",
         "summary": "llm deepseek 10 to 6 tok",
         "payload": {"model": "deepseek-v4-flash", "prompt_tokens": 10,
                     "completion_tokens": 6, "cached_tokens": 0, "retries": 1}},
    ]
    run = _project(tmp_path, beats)
    calls = [r for r in run["rows"] if r["kind"] == "call"]
    assert len(calls) == 1 and calls[0]["dur"] == "4.0s"
