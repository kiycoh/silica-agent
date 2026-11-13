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


def _typed(p, text: str) -> None:
    """One key's text at a time, as a person types: the list asks its sources on each."""
    for ch in text:
        p.type(ch)
        time.sleep(0.02)


LISTED = "[...document.querySelectorAll('.cm-tooltip-autocomplete li')].map((li) => li.textContent)"


def _listed(p, label: str) -> None:
    p.wait(f"{LISTED}.some((t) => t.startsWith({label!r}))", 5, f"{label} in the list")
    time.sleep(0.2)  # CodeMirror ignores an accept within 75 ms of the list opening


def _closed(p) -> None:
    p.wait("!document.querySelector('.cm-tooltip-autocomplete')", 5, "the list closed")


def _new_line(p, vault, text: str = "Start.\n") -> None:
    (vault / "Topic" / "Other.md").write_text(text, encoding="utf-8")
    p.js("openCentre('Topic/Other.md')")
    p.wait("!!document.querySelector('#nv-edit .cm-content .cm-line')", 8, "the editor")
    p.click("document.querySelector('#nv-edit .cm-content .cm-line')")
    _key(p, "End", "End", 35)
    _press_enter(p)


def test_a_name_in_math_completes_as_before_and_enter_takes_it(app):  # noqa: F811
    """The stock completion keys are put back by hand (defaultKeymap off, ADR-0159 point
    15), so each list that took Enter before is driven here: a name in math, the second
    entry by the arrow, a note after `[[`."""
    p, vault = app
    _new_line(p, vault)
    _typed(p, "$acqu")
    _listed(p, "acqua")
    _press_enter(p)
    p.wait("centreNote.editor.getText().includes('$\\\\ce{H2O}')", 5, "the formula in the word's place")
    _closed(p)
    _typed(p, " + acqu")
    _listed(p, "acqua ossigenata")
    _key(p, "ArrowDown", "ArrowDown", 40)
    _press_enter(p)
    p.wait("centreNote.editor.getText().includes('$\\\\ce{H2O} + \\\\ce{H2O2}')", 5, "the second entry")
    _typed(p, "$ see [[Sour")
    _listed(p, "Source")
    _press_enter(p)
    p.wait("centreNote.editor.getText().includes('see [[Source]]')", 5, "the note's link")
    _closed(p)


def test_formula_and_molecule_open_the_whole_list(app):  # noqa: F811
    p, vault = app
    _new_line(p, vault)
    # A blank line above: right under a line of text the editor reads `$$` as that
    # paragraph going on, and Formula opens no list there (found 2026-11-13, not fixed here).
    _press_enter(p)
    p.click("document.querySelector('#nv-format [data-fmt=\"formula\"]')")
    p.wait(f"{LISTED}.length > 20", 5, "the formulas and molecules")
    _key(p, "Escape", "Escape", 27)
    _closed(p)
    _key(p, "End", "End", 35, mods=2)  # out of the formula block, to a line of its own
    _press_enter(p)
    _press_enter(p)
    p.click("document.querySelector('#nv-format [data-fmt=\"molecule\"]')")
    _listed(p, "water")
    _press_enter(p)
    p.wait("centreNote.editor.getText().includes('```smiles\\nO\\n```')", 5, "water's SMILES in the fence")


def test_a_tab_right_after_a_letter_takes_the_name_while_the_list_refreshes(app):  # noqa: F811
    """After a key the list has no selection until its sources answer, about 100 ms: a Tab
    sent then left the editor, and in a list item indented it (2026-11-13)."""
    p, vault = app
    _new_line(p, vault, "Start.\n\n- two\n")
    p.click("[...document.querySelectorAll('#nv-edit .cm-line')].find((l) => l.textContent.includes('two'))")
    _key(p, "End", "End", 35)
    _typed(p, " caffein")
    _listed(p, "caffeine")
    p.type("e")
    _key(p, "Tab", "Tab", 9)
    p.wait("centreNote.editor.getText().includes('\\n- two $\\\\ce{C8H10N4O2}$')", 5, "the formula, the item where it was")
    assert p.js("centreNote.editor.view.hasFocus"), "the caret stays in the note"
    _typed(p, " acqua")
    _listed(p, "acqua ossigenata")
    _key(p, "ArrowDown", "ArrowDown", 40)
    _key(p, "Tab", "Tab", 9)
    p.wait("centreNote.editor.getText().includes('$ $\\\\ce{H2O2}$')", 5, "the entry the arrow chose")
    _key(p, "Tab", "Tab", 9)  # no list: Tab is the list item's again
    p.wait("centreNote.editor.getText().includes('\\n  - two')", 5, "the item indented")


def test_a_name_in_prose_offers_its_inline_formula_and_only_tab_takes_it(app):  # noqa: F811
    """The owner's wish (2026-11-13): part of a molecule's name in a sentence is offered
    as its formula. Prose is typed fast, so Enter stays a newline: "acqua" must never
    become H2O by a key meant for the next line."""
    p, vault = app
    _new_line(p, vault)
    _typed(p, "acq")
    time.sleep(0.4)
    assert not p.js(LISTED), "three letters are a word, not yet a name"
    _typed(p, "ua")
    _listed(p, "acqua")
    _press_enter(p)
    _closed(p)
    assert p.js("centreNote.editor.getText()") == "Start.\nacqua\n\n"  # the word as typed, then the new line
    _typed(p, "Ho versato dell'acido solforico")
    _listed(p, "acido solforico")
    _key(p, "Tab", "Tab", 9)
    p.wait("centreNote.editor.getText().includes(\"dell'$\\\\ce{H2SO4}$\\n\")", 5, "the formula, after the elision")
    _closed(p)
    _typed(p, " e caffeine")
    _listed(p, "caffeine")
    _key(p, "Escape", "Escape", 27)
    _closed(p)
    _typed(p, ", water")
    _listed(p, "water")
    _typed(p, " f")  # typing on: no name goes on with " f"
    _closed(p)
    _typed(p, "; pitagora")
    _listed(p, "teorema di Pitagora")
    _key(p, "Tab", "Tab", 9)
    p.wait("centreNote.editor.getText().includes('$a^2 + b^2 = c^2$\\n')", 5, "the formula, as inline math")
    _typed(p, " [[acqua")
    time.sleep(0.4)
    assert not p.js(LISTED), "a link's name is a note's, not a molecule's"
    _saved(p)
    assert (vault / "Topic" / "Other.md").read_text(encoding="utf-8") == (
        "Start.\nacqua\nHo versato dell'$\\ce{H2SO4}$ e caffeine, water f; $a^2 + b^2 = c^2$ [[acqua\n")


UNDER_A_HEADING = ("## Prices paid for the fruit in the second quarter\n| Item | Price | Note |\n| --- | ---: | --- |\n"
                   "| Apple | $5 | plain |\n| Pear | 3 | a \\| b |\n\nAfter.\n")
# The words the grid's bar would cover: every text node of the note outside the grid
# whose glyphs meet the bar's box, and the head of the view or the drawer, which is text too.
COVERED = """(() => {
  const bar = document.querySelector('%(ed)s .cm-grid-bar').getBoundingClientRect(), hit = [];
  for (const host of [document.querySelector('%(ed)s'), document.querySelector('%(head)s')]) {
    const walk = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    for (let t = walk.nextNode(); t; t = walk.nextNode()) {
      if (!t.textContent.trim() || t.parentElement.closest('.cm-grid')) continue;
      const r = document.createRange();
      r.selectNodeContents(t);
      if ([...r.getClientRects()].some((b) => b.bottom > bar.top && b.top < bar.bottom && b.right > bar.left && b.left < bar.right))
        hit.push(t.textContent);
    }
    for (const f of host.querySelectorAll('textarea, input')) {  // a title's words are its value, no text node
      const b = f.getBoundingClientRect();
      if (f.value && b.bottom > bar.top && b.top < bar.bottom && b.right > bar.left && b.left < bar.right) hit.push(f.value);
    }
  }
  return hit;
})()"""


@pytest.mark.parametrize("width, ed, head", [(1440, "#nv-edit", "#nv-bar"), (900, "#note-edit", "#note-panel > header")])
@pytest.mark.parametrize("text", [UNDER_A_HEADING, TABLE.replace("Before.\n\n", "")], ids=["under-a-heading", "first-line"])
def test_the_grid_bar_is_one_row_and_covers_no_word(app, width, ed, head, text):  # noqa: F811
    """At 900 the drawer is 320 px and the bar of six acts wrapped to two rows over the
    line above (2026-11-13); one row still covered a heading set right above a table."""
    p, vault = app
    p.send("Browser.setWindowBounds", {"windowId": p.send("Browser.getWindowForTarget")["windowId"],
                                       "bounds": {"width": width, "height": 900}})
    p.wait(f"innerWidth === {width}", 5, "the window's width")
    (vault / "Topic" / "Other.md").write_text(text, encoding="utf-8")
    p.js("openCentre('Topic/Other.md')" if ed == "#nv-edit" else "openNote('Topic/Other.md')")
    p.wait(f"!!document.querySelector('{ed} .cm-content .cm-line')", 8, "the editor")
    time.sleep(0.6)  # the drawer slides in
    p.click(f"[...document.querySelectorAll('{ed} .cm-line')].find((l) => l.textContent === 'After.')")
    p.wait(f"!!document.querySelector('{ed} .cm-grid td')", 8, "the grid")
    hover = p.js(f"document.querySelector('{ed} .cm-grid').getBoundingClientRect().top")
    x, y = p.center(f"document.querySelector('{ed} .cm-grid [data-r=\"2\"][data-c=\"0\"]')")  # plain words, no link
    p.mouse("mouseMoved", x, y)
    assert p.js(f"document.querySelector('{ed} .cm-grid').getBoundingClientRect().top") == hover, "a hover moves nothing"
    p.mouse("mousePressed", x, y, True)
    p.mouse("mouseReleased", x, y)
    p.wait("document.activeElement.classList.contains('cm-grid-input')", 5, "the cell's field")
    # the cell opened where it was pressed
    assert p.js(f"document.elementFromPoint({x}, {y}).closest('td') === document.activeElement.closest('td')")
    rows = p.js(f"(() => {{ const b = [...document.querySelectorAll('{ed} .cm-grid-bar button')].map((e) => e.getBoundingClientRect().top);"
                " return new Set(b.map(Math.round)).size; })()")
    assert rows == 1, "six acts on one row"
    labels = p.js(f"[...document.querySelectorAll('{ed} .cm-grid-bar button')].map((b) => [b.textContent, b.title, b.getAttribute('aria-label')])")
    assert len(labels) == 6 and all(t == a and len(a) > len(s) for s, t, a in labels), labels
    assert p.js(COVERED % {"ed": ed, "head": head}) == []


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


def test_the_preview_stays_while_the_pointer_is_on_a_link_inside_it(app):  # noqa: F811
    """The box closed the moment the pointer reached a link in it (2026-11-12): the
    pointer's arrival there read as pointing at a new link, which closed the box with
    the link in it, so nothing showed at all."""
    p, vault = app
    (vault / "Topic" / "Source.md").write_text(BODY_LINKED, encoding="utf-8")
    (vault / "Topic" / "Other.md").write_text("See [[Source]].\n", encoding="utf-8")
    p.js("openNote('Topic/Other.md')")
    p.wait("!!document.querySelector('#note-edit a.cm-link-live[data-target=\"Source\"]')", 8, "the live link")
    time.sleep(0.6)
    x, y = p.center("document.querySelector('#note-edit a.cm-link-live[data-target=\"Source\"]')")
    p.mouse("mouseMoved", x, y)
    p.wait("!!document.querySelector('.hv-pop .note-link')", 3, "the preview, a link in it")
    ix, iy = p.center("document.querySelector('.hv-pop .note-link')")
    p.mouse("mouseMoved", ix, iy)
    time.sleep(0.8)
    assert p.js("document.querySelector('.hv-pop .hv-title')?.textContent") == "Source"


BODY_LINKED = "A source sentence that names [[Other]].\n\nThe second paragraph of the source.\n"


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
