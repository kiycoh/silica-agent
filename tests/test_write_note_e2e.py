# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0155 end to end: the GUI served for real on a seeded vault, driven in
headless Chrome with real keys and a real right click. Opt-in like the calendar's:
SILICA_E2E=1. A code read has missed every layout defect of this directory so far."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_calendar_e2e import CHROME, Page, _free_port

pytestmark = [
    pytest.mark.skipif(not os.environ.get("SILICA_E2E"), reason="opt-in: SILICA_E2E=1 starts a browser"),
    pytest.mark.skipif(CHROME is None, reason="needs Chrome or Chromium"),
]

BODY = "A source sentence that the note will quote later on.\n\nThe second paragraph of the source.\n"


@pytest.fixture
def app(tmp_vault):
    import uvicorn

    from silica.config import CONFIG
    from silica.ui.web import server

    tmp_vault.note("Topic/Source.md", BODY)
    tmp_vault.note("Topic/Other.md", "Another note in the folder.\n")
    server._reset_session()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    end = time.monotonic() + 15
    while not srv.started and time.monotonic() < end:
        time.sleep(0.05)
    assert srv.started, "the GUI did not start"
    profile = tempfile.mkdtemp(prefix="silica-e2e-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                               "--window-size=1440,900", "--no-first-run", "--no-default-browser-check",
                               "--disable-extensions", "--mute-audio", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        marker = Path(profile) / "DevToolsActivePort"
        end = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < end:
            time.sleep(0.05)
        devtools = int(marker.read_text().split()[0])
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{devtools}/json/list", timeout=10))
        page = Page(next(t for t in targets if t["type"] == "page")["webSocketDebuggerUrl"])
        page.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        page.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
        page.wait("document.readyState === 'complete' && !!document.querySelector('#tree .tree-note')", 30, "the tree")
        time.sleep(0.4)
        yield page, Path(CONFIG.vault_path)
        assert not page.exceptions, page.exceptions
    finally:
        chrome.terminate()
        srv.should_exit = True
        shutil.rmtree(profile, ignore_errors=True)


def _press_enter(p: Page) -> None:
    d = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"}
    p.send("Input.dispatchKeyEvent", {"type": "keyDown", **d})
    p.send("Input.dispatchKeyEvent", {"type": "keyUp", **d})


def _right_click(p: Page, x: float, y: float) -> None:
    p.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    p.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "right", "buttons": 2, "clickCount": 1})
    p.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "right", "buttons": 0, "clickCount": 1})
    time.sleep(0.25)


def test_a_new_note_is_born_with_its_first_text_and_holds_only_that(app):
    p, vault = app
    p.click("document.querySelector('#write-note')")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    before = sorted(x.name for x in vault.rglob("*.md"))
    p.js("document.querySelector('.tab[data-tab=\"metrics\"]')?.click()")
    time.sleep(0.5)
    assert sorted(x.name for x in vault.rglob("*.md")) == before, "a note left empty leaves no trace"

    p.click("document.querySelector('#write-note')")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.type("By hand")
    _press_enter(p)
    p.wait("!centreNote.fresh", 5, "the file")
    p.wait("centreNote.editor.view.hasFocus", 5, "the caret in the text")
    p.type("One line.")
    p.wait("document.querySelector('#nv-foot .nv-st').textContent.startsWith('Saved')", 5, "the server's answer")
    assert (vault / "By hand.md").read_text(encoding="utf-8") == "One line."


def test_a_conflict_waits_for_the_persons_choice(app):
    p, vault = app
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    p.js("centreNote.editor.view.focus(); centreNote.editor.view.dispatch({selection: {anchor: 0}})")
    note = vault / "Topic" / "Source.md"
    note.write_text(BODY + "\nAn agent wrote this.\n", encoding="utf-8")
    p.type("Mine. ")
    p.wait("centreNote.conflict", 5, "the 409")
    p.type("More. ")
    time.sleep(1.2)
    assert "Mine." not in note.read_text(encoding="utf-8"), "typing is not a choice"
    p.js("[...document.querySelectorAll('#nv-foot .act')].find((b) => b.textContent === 'Keep mine').click()")
    p.wait("!centreNote.conflict && document.querySelector('#nv-foot .nv-st').textContent.startsWith('Saved')", 5, "the write")
    assert "Mine. More." in note.read_text(encoding="utf-8")


def test_a_right_click_on_a_selection_opens_the_list_and_elsewhere_the_browsers(app):
    p, _ = app
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    p.js("window.__cm = []; document.addEventListener('contextmenu', (e) => window.__cm.push(e.defaultPrevented))")
    p.js("centreNote.editor.view.focus(); centreNote.editor.view.dispatch({selection: {anchor: 2, head: 8}})")
    time.sleep(0.2)
    x, y = p.js("(() => { const q = centreNote.editor.view.coordsAtPos(4); return [q.left + 1, (q.top + q.bottom) / 2]; })()")
    _right_click(p, x, y)
    assert p.js("[...document.querySelectorAll('#tree-menu button')].map((b) => b.textContent)") == [
        "copy", "explain this", "quiz me on this", "ask…", "check against the vault"]
    assert p.js("window.__cm.at(-1)") is True
    assert p.js("(() => { const r = centreNote.editor.view.state.selection.main; return r.to - r.from; })()") == 6
    p.js("closeTreeMenu()")
    p.js("centreNote.editor.view.dispatch({selection: {anchor: 2}})")
    _right_click(p, x, y)
    assert p.js("!document.querySelector('#tree-menu')") and p.js("window.__cm.at(-1)") is False


def test_another_view_lets_the_note_go_and_back_brings_it_back(app):
    p, _ = app
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    p.js("document.querySelector('.tab[data-tab=\"calendar\"]').click()")
    p.wait("centreNote.path === ''", 5, "the let-go")
    p.js("history.back()")
    p.wait("centreNote.path === 'Topic/Source.md' && activeTab === 'note'", 8, "Back")


def test_back_to_a_bare_write_is_a_new_note_and_not_a_dead_field(app):
    p, vault = app
    p.click("document.querySelector('#write-note')")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.js("document.querySelector('.tab[data-tab=\"calendar\"]').click()")
    p.wait("activeTab === 'calendar'", 5, "the other view")
    p.js("history.back()")
    p.wait("activeTab === 'note' && centreNote.fresh", 8, "a new note")
    p.js("centreNote.editor.focus()")
    p.type("Typed after Back.")
    p.wait("!centreNote.fresh && document.querySelector('#nv-foot .nv-st').textContent.startsWith('Saved')", 6, "the file")
    assert (vault / "Topic" / "Untitled.md").exists() or (vault / "Untitled.md").exists()


def test_a_refused_title_does_not_take_the_text_with_it(app):
    p, vault = app
    p.js("writeNote({ folder: 'Topic' })")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.type("Other")
    _press_enter(p)
    p.wait("!document.querySelector('#nv-msg').hidden", 5, "the refusal")
    p.js("window.__posts = 0; const f = window.fetch; window.fetch = (u, o) => { if (u === '/note/new') window.__posts++; return f(u, o); }")
    p.js("centreNote.editor.focus()")
    p.type("A paragraph worth keeping.")
    time.sleep(1.2)
    assert p.js("window.__posts") == 0, "a refused name is asked once, not once per keystroke"
    assert "another name" in p.js("document.querySelector('#nv-foot .nv-st').textContent")
    p.js("document.querySelector('.tab[data-tab=\"calendar\"]').click()")
    p.wait("!centreNote.fresh", 5, "the note under another name")
    time.sleep(0.5)
    assert (vault / "Topic" / "Untitled.md").read_text(encoding="utf-8") == "A paragraph worth keeping."


def test_a_conflict_survives_every_way_out(app):
    p, vault = app
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    p.js("centreNote.editor.view.focus(); centreNote.editor.view.dispatch({selection: {anchor: 0}})")
    (vault / "Topic" / "Source.md").write_text(BODY + "\nAn agent wrote this.\n", encoding="utf-8")
    p.type("Mine. ")
    p.wait("centreNote.conflict", 5, "the 409")
    for out in ("document.querySelector('.tab[data-tab=\"calendar\"]').click()", "writeNote()",
                "openCentre('Topic/Other.md')", "document.querySelector('#nv-todrawer').click()"):
        p.js(out)
        time.sleep(0.7)
        p.wait("centreNote.path === 'Topic/Source.md' && centreNote.conflict", 5, "the note kept")
        assert "Mine." in p.js("centreNote.editor.getText()"), out
        if p.js("activeTab") != "note":
            p.js("history.back()")
            p.wait("activeTab === 'note'", 5, "Back")
            assert "Mine." in p.js("centreNote.editor.getText()"), "Back reopened the disk's bytes"
    p.js("[...document.querySelectorAll('#nv-foot .act')].find((b) => b.textContent === 'Keep mine').click()")
    p.wait("!centreNote.conflict && document.querySelector('#nv-foot .nv-st').textContent.startsWith('Saved')", 5, "the write")
    assert "Mine." in (vault / "Topic" / "Source.md").read_text(encoding="utf-8")


def test_a_failed_save_keeps_the_text_after_the_view_is_left(app):
    p, vault = app
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    p.js("centreNote.editor.view.focus(); centreNote.editor.view.dispatch({selection: {anchor: 0}})")
    p.send("Network.enable")
    p.send("Network.emulateNetworkConditions", {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})
    p.type("Typed offline. ")
    p.wait("centreNote.phase === 'retry'", 5, "the failed save")
    p.js("document.querySelector('.tab[data-tab=\"calendar\"]').click()")
    time.sleep(0.5)
    assert p.js("centreNote.path") == "Topic/Source.md", "the note stays held"
    p.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    p.wait("centreNote.path === ''", 8, "the retry, then the let-go")
    assert "Typed offline." in (vault / "Topic" / "Source.md").read_text(encoding="utf-8")


def _press_format(p: Page, fmt: str) -> None:
    p.js(f"document.querySelector('#nv-format [data-fmt={fmt}]').dispatchEvent("
         "new MouseEvent('mousedown', {bubbles: true, cancelable: true}))")


@pytest.mark.parametrize("fmt,want", [("bullets", "- typed"), ("numbers", "1. typed"), ("tasks", "- [ ] typed"),
                                      ("h2", "## typed"), ("quote", "> typed")])
def test_text_typed_after_a_line_control_lands_after_the_marker(app, fmt, want):
    p, _ = app
    p.js("writeNote({ folder: 'Topic' })")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.js("centreNote.editor.focus()")
    _press_format(p, fmt)
    p.type("typed")
    assert p.js("centreNote.editor.getText()") == want


def test_italic_on_a_bold_word_adds_to_it_and_takes_only_its_own(app):
    p, _ = app
    p.js("writeNote({ folder: 'Topic' })")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.js("centreNote.editor.focus()")
    p.type("one **two** three")
    p.js("centreNote.editor.view.dispatch({selection: {anchor: 6, head: 9}})")
    _press_format(p, "italic")
    assert "***two***" in p.js("centreNote.editor.getText()")
    _press_format(p, "italic")
    assert p.js("centreNote.editor.getText()") == "one **two** three"


def test_a_quoted_comma_stays_inside_its_chip(app):
    p, vault = app
    (vault / "Topic" / "Other.md").write_text('---\ntags: [alpha, beta, "gamma, delta"]\n---\n\nBody.\n', encoding="utf-8")
    p.js("openCentre('Topic/Other.md')")
    p.wait("centreNote.path === 'Topic/Other.md'", 8, "the note at the centre")
    chips = p.js("[...document.querySelectorAll('#nv-props .nv-chip')].map((c) => c.firstChild.textContent)")
    assert chips == ["alpha", "beta", "gamma, delta"]


def test_a_match_in_a_drawn_block_is_shown_when_reached(app):
    p, vault = app
    (vault / "Topic" / "Other.md").write_text(
        "---\nowner: ada@example\n---\n\nBody.\n\n| a | b |\n|---|---|\n| tablecellword | x |\n", encoding="utf-8")
    p.js("openCentre('Topic/Other.md')")
    p.wait("centreNote.path === 'Topic/Other.md'", 8, "the note at the centre")
    p.js("findInNote(centreNote)")
    p.wait("!!document.querySelector('#nv-edit .nv-find input')", 5, "the find bar")
    for word in ("ada@example", "tablecellword"):
        p.js(f"(() => {{ const i = document.querySelector('#nv-edit .nv-find input'); i.value = '{word}'; "
             "i.dispatchEvent(new Event('input')); i.focus(); })()")
        _press_enter(p)
        time.sleep(0.4)
        assert p.js("(() => { const v = centreNote.editor.view; return !!v.coordsAtPos(v.state.selection.main.from); })()"), word
        assert p.js("[...document.querySelectorAll('#nv-edit .cm-searchMatch')].some((m) => m.getBoundingClientRect().width > 0)"), word


def test_the_drawers_close_button_keeps_text_the_disk_does_not_have(app):
    p, vault = app
    p.js("openNote('Topic/Source.md')")
    p.wait("drawerNote.path === 'Topic/Source.md'", 8, "the note in the drawer")
    p.js("drawerNote.editor.view.focus(); drawerNote.editor.view.dispatch({selection: {anchor: 0}})")
    p.send("Network.enable")
    p.send("Network.emulateNetworkConditions", {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})
    p.type("Typed offline. ")
    p.wait("drawerNote.phase === 'retry'", 5, "the failed save")
    p.click("document.querySelector('#note-close')")
    time.sleep(0.3)
    assert p.js("notePanel.classList.contains('open')") and p.js("drawerNote.path") == "Topic/Source.md"
    p.send("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    p.wait("drawerNote.phase === 'saved'", 8, "the retry")
    assert "Typed offline." in (vault / "Topic" / "Source.md").read_text(encoding="utf-8")


def test_a_chip_takes_its_own_bytes_and_no_others(app):
    p, vault = app
    head = 'tags: [alpha, beta, "gamma, delta", \'it\'\'s\', "q\\"x"]\nalias:\n  - first alias\n  - "second"\n'
    note = vault / "Topic" / "Other.md"
    note.write_text(f"---\n{head}---\n\nBody.\n", encoding="utf-8")
    p.js("openCentre('Topic/Other.md')")
    p.wait("centreNote.path === 'Topic/Other.md'", 8, "the note at the centre")
    chips = "[...document.querySelectorAll('#nv-props .nv-chip')].map((c) => c.firstChild.textContent)"
    assert p.js(chips) == ["alpha", "beta", "gamma, delta", "it's", 'q"x', "first alias", "second"]
    press = "[...document.querySelectorAll('#nv-props .nv-chip')].find((c) => c.firstChild.textContent === {!r}).querySelector('button')"
    p.click(f"({press.format('beta')})")
    p.wait(f"{chips}.length === 6", 3, "the form to follow")
    p.click(f"({press.format('gamma, delta')})")
    p.wait(f"{chips}.length === 5", 3, "the form to follow")
    p.click(f"({press.format('second')})")
    p.wait("document.querySelector('#nv-foot .nv-st').textContent.startsWith('Saved')", 5, "the save")
    text = note.read_text(encoding="utf-8")
    assert "tags: [alpha, 'it''s', \"q\\\"x\"]\n" in text, text
    assert "  - first alias\n" in text and "second" not in text, text


def test_ctrl_i_on_a_bold_word_is_the_bars_italic(app):
    p, _ = app
    p.js("writeNote({ folder: 'Topic' })")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.js("centreNote.editor.focus()")
    p.type("one **two** three")
    p.js("centreNote.editor.view.dispatch({selection: {anchor: 6, head: 9}})")
    d = {"key": "i", "code": "KeyI", "windowsVirtualKeyCode": 73, "modifiers": 2}
    p.send("Input.dispatchKeyEvent", {"type": "keyDown", **d})
    p.send("Input.dispatchKeyEvent", {"type": "keyUp", **d})
    assert "***two***" in p.js("centreNote.editor.getText()")


def test_the_web_entry_is_out_until_its_gate_passes(app):
    p, _ = app
    assert p.js("checkWebOpen") is False
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    assert p.js("document.querySelector('#nv-selbar [data-check]').textContent") == "check this"
    r = p.js("fetch('/note/check', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
             "body: JSON.stringify({path: 'Topic/Source.md', lines: [1, 1], web: true, claim: 'x'})}).then((r) => r.status)")
    assert r == 404


def test_a_note_whose_file_left_is_a_choice_and_not_a_wedge(app):
    p, vault = app
    p.js("openCentre('Topic/Source.md')")
    p.wait("centreNote.path === 'Topic/Source.md'", 8, "the note at the centre")
    p.js("centreNote.editor.view.focus(); centreNote.editor.view.dispatch({selection: {anchor: 0}})")
    note = vault / "Topic" / "Source.md"
    note.unlink()
    p.type("Still mine. ")
    p.wait("centreNote.phase === 'gone'", 5, "the 404")
    foot = "document.querySelector('#nv-foot .nv-st').innerText"
    assert "Write it back" in p.js(foot) and "Let it go" in p.js(foot)
    p.js("[...document.querySelectorAll('#nv-foot .act')].find((b) => b.textContent === 'Write it back').click()")
    p.wait("centreNote.phase === 'saved'", 5, "the write")
    assert note.read_text(encoding="utf-8").startswith("Still mine. ")
    note.unlink()
    p.type("Again. ")
    p.wait("centreNote.phase === 'gone'", 5, "the 404")
    p.js("[...document.querySelectorAll('#nv-foot .act')].find((b) => b.textContent === 'Let it go').click()")
    p.wait("centreNote.path === '' && activeTab !== 'note'", 5, "the way out")
