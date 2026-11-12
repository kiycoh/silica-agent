# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0159, 0160 and 0161 in the browser: the GUI served for real on a seeded vault,
driven in headless Chrome with real keys and a real pointer. Opt-in like ADR-0155's:
SILICA_E2E=1. A code read has missed every layout defect of this directory so far."""
from __future__ import annotations

import os
import time

import pytest

from tests.test_calendar_e2e import CHROME
from tests.test_write_note_e2e import _press_enter, app  # noqa: F401  (the fixture)

# The fixture comes with its import, its module's marks do not.
pytestmark = [
    pytest.mark.skipif(not os.environ.get("SILICA_E2E"), reason="opt-in: SILICA_E2E=1 starts a browser"),
    pytest.mark.skipif(CHROME is None, reason="needs Chrome or Chromium"),
]

TABLE = "Before.\n\n|   Name    | Note |\n|-----------|:----:|\n| **water** | [[Topic/Source\\|src]] |\n| salt      | b \\| c |\n\nAfter.\n"


def _key(p, key: str, code: str, vk: int, mods: int = 0) -> None:
    d = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mods}
    p.send("Input.dispatchKeyEvent", {"type": "keyDown", **d})
    p.send("Input.dispatchKeyEvent", {"type": "keyUp", **d})


def _cell(r: int, c: int) -> str:
    return f"document.querySelector('#nv-edit .cm-grid [data-r=\"{r}\"][data-c=\"{c}\"]')"


def _saved(p) -> None:
    p.wait("document.querySelector('#nv-foot .nv-st').textContent.startsWith('Saved')", 8, "the server's answer")


def test_a_cell_edit_writes_its_cell_and_tab_and_enter_move_through_the_grid(app):  # noqa: F811
    p, vault = app
    (vault / "Topic" / "Other.md").write_text(TABLE, encoding="utf-8")
    p.js("openCentre('Topic/Other.md')")
    p.wait("!!document.querySelector('#nv-edit .cm-grid td .note-link')", 8, "the grid, drawn by the server")
    p.click(_cell(1, 0))
    p.wait("document.activeElement.classList.contains('cm-grid-input')", 5, "the cell's field")
    assert p.js("document.activeElement.value") == "**water**"
    p.type(" ice | x")
    _key(p, "Tab", "Tab", 9)
    p.wait("document.activeElement.closest('td')?.dataset.c === '1'", 5, "the next cell")
    assert p.js("document.activeElement.value") == "[[Topic/Source|src]]", "a cell shows its pipes bare"
    _saved(p)
    text = (vault / "Topic" / "Other.md").read_text(encoding="utf-8")
    # its own cell's bytes and no others: the head keeps its padding, the pipe goes in escaped
    assert text == TABLE.replace("| **water** |", "| **water** ice \\| x |")
    p.click(_cell(2, 1))
    _press_enter(p)
    p.wait("document.activeElement.closest('td')?.dataset.r === '3'", 5, "a new row's cell")
    p.type("new")
    _key(p, "Tab", "Tab", 9)  # past the last cell the caret goes on into the text
    p.wait("centreNote.editor.view.hasFocus", 5, "the caret after the table")
    _saved(p)
    assert (vault / "Topic" / "Other.md").read_text(encoding="utf-8").endswith("| salt      | b \\| c |\n|  | new |\n\nAfter.\n")


def test_a_column_rewrites_the_table_compactly_and_edit_as_text_shows_it(app):  # noqa: F811
    p, vault = app
    (vault / "Topic" / "Other.md").write_text(TABLE, encoding="utf-8")
    p.js("openCentre('Topic/Other.md')")
    p.wait("!!document.querySelector('#nv-edit .cm-grid td .note-link')", 8, "the grid")
    p.click(_cell(1, 0))
    p.js("document.querySelector('#nv-edit .cm-grid-bar [data-act=\"col-add\"]').dispatchEvent("
         "new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0}))")
    p.wait("document.activeElement.closest('td')?.dataset.c === '1'", 5, "the new column's cell")
    assert p.js("centreNote.editor.getText()") == ("Before.\n\n| Name |  | Note |\n| --- | --- | :---: |\n"
                                                    "| **water** |  | [[Topic/Source\\|src]] |\n| salt |  | b \\| c |\n\nAfter.\n")
    _key(p, "z", "KeyZ", 90, mods=2)  # nothing typed in the cell: the note's undo
    p.wait("centreNote.editor.getText().includes('|   Name    |')", 5, "the table as it was")
    p.js("document.querySelector('#nv-edit .cm-grid-bar [data-act=\"text\"]').dispatchEvent("
         "new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0}))")
    p.wait("document.querySelectorAll('#nv-edit .cm-line.cm-table').length === 4", 5, "the table's lines as source")


def test_a_wikilink_shows_its_note_on_a_pause_and_not_before(app):  # noqa: F811
    p, vault = app
    (vault / "Topic" / "Other.md").write_text("See [[Source]] and [[Missing note]].\n", encoding="utf-8")
    p.js("openNote('Topic/Other.md')")
    p.wait("!!document.querySelector('#note-edit a.cm-link-live[data-target=\"Source\"]')", 8, "the live link")
    time.sleep(0.6)  # the drawer slides in: measured mid-slide, the link is past the window's edge
    x, y = p.center("document.querySelector('#note-edit a.cm-link-live[data-target=\"Source\"]')")
    p.mouse("mouseMoved", x, y)
    time.sleep(0.15)
    assert not p.js("!!document.querySelector('.hv-pop')"), "not before the pause"
    p.wait("!!document.querySelector('.hv-pop')", 3, "the preview")
    assert p.js("document.querySelector('.hv-pop .hv-title').textContent") == "Source"
    assert "source sentence" in p.js("document.querySelector('.hv-pop .hv-body').textContent")
    p.mouse("mouseMoved", x, y + 200)
    p.wait("!document.querySelector('.hv-pop')", 3, "the preview gone with the pointer")
    x, y = p.center("document.querySelector('#note-edit .note-link.broken')")
    p.mouse("mouseMoved", x, y)
    time.sleep(0.8)
    assert not p.js("!!document.querySelector('.hv-pop')"), "an unresolved link shows nothing"


def test_a_pasted_address_takes_its_pages_title(app, monkeypatch):  # noqa: F811
    p, _ = app
    monkeypatch.setattr("silica.sources.web_fetch.link_title", lambda url: "A [page] title")
    p.js("writeNote({ folder: 'Topic' })")
    p.wait("document.activeElement.id === 'nv-title'", 5, "the title's caret")
    p.js("centreNote.editor.focus()")
    p.type("See ")
    p.js("(() => { const v = centreNote.editor.view, dt = new DataTransfer(); dt.setData('text/plain', 'https://example.org/a_b');"
         " v.contentDOM.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })); })()")
    assert p.js("centreNote.editor.getText()") == "See [https://example.org/a\\_b](https://example.org/a_b)"
    p.wait("centreNote.editor.getText().includes('title')", 5, "the title")
    assert p.js("centreNote.editor.getText()") == "See [A \\[page\\] title](https://example.org/a_b)"
