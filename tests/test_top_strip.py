# tests/test_top_strip.py
"""The deck's substrate move: identity and counts are stated once.

The rail used to carry the vault's size as a 2x2 board while the metrics view
carried four of the same numbers again, and three boot-diagnostic cards held
roughly 420 of the rail's 1000 pixels from launch for the whole session. Both
are now in the top strip -- the counts because they are true on every view, the
notices behind a chip because configuration is a thing you open, not a thing
that sits on top of the vault you came to read.

These tests pin the "once", which is the only part that can silently come back:
a second copy of a count is added the day someone wants it closer to hand.
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
INDEX = WEB / "index.html"
from tests.webassets import app_css, app_js
WORK_JS = WEB / "work.js"


def _block(html: str, open_tag: str, close_tag: str) -> str:
    start = html.index(open_tag)
    return html[start:html.index(close_tag, start)]


def _foot(html: str) -> str:
    return _block(html, '<div id="side-foot">', "</div>")


def test_the_vault_states_its_size_on_the_landing_and_not_in_the_chrome():
    """The strip carried the counts until the audit (P16): three numbers that
    cannot change during a session, beside every conversation. The landing
    and the metrics view keep them; the chrome keeps identity and health, in
    the drawer's foot since the strip went (ADR-0084)."""
    html = INDEX.read_text()
    rail = _block(html, '<aside id="sidebar">', "</aside>")

    assert 'id="vault-chip"' in _foot(html) and 'id="top-vname"' in _foot(html)
    assert 'id="top-counts"' not in html, "the counts are back in the chrome"
    # the rail's old board, by every name it had
    for dead in ("stat-grid", "stat-notes", "stat-links", "stat-clusters",
                 "stat-unresolved"):
        assert dead not in html, f"the rail's {dead} is back"
    assert 'id="vh-facts"' in _block(html, '<section id="view-chat"', "</section>")
    assert 'id="top-counts"' not in rail


def test_the_area_count_has_one_rule():
    """The strip and the chat landing both state 'N areas'. A cluster count made
    entirely of singletons names nothing, so the count is gated -- and the gate
    stated twice is two numbers that can disagree about the same vault."""
    src = app_js()
    assert src.count("function areaCount(") == 1
    # the raw expression lives only inside that function
    assert src.count("(data.topics || []).length ?") == 1
    assert src.count("= areaCount(data)") == 1  # the landing; the strip states no size (P16)


def test_the_notices_are_behind_the_chip_not_camped_in_the_rail():
    html = INDEX.read_text()
    rail = _block(html, '<aside id="sidebar">', "</aside>")
    assert 'id="vault-chip"' in _foot(html)
    assert 'id="boot-notices"' not in rail
    panel = _block(html, 'id="vault-panel"', 'id="settings-btn"')
    assert 'id="boot-notices"' in panel
    # ...and the broken-link count is in the same panel: one place for what
    # needs answering (audit P16)
    assert 'id="top-broken"' in panel
    # the chip carries the count as words and goes quiet at zero
    assert '$("#top-notices").textContent = n ?' in app_js()


def test_the_rail_holds_what_only_the_rail_can():
    """Pinned, this session's writes, the files, the chats, the run: compartments
    none of them stated anywhere else in the app. Layout and Areas stood here too
    and no longer do -- both are readings of ONE view, and a control for the
    picture belongs against the picture (#legend, inside #view-graph)."""
    rail = _block(INDEX.read_text(), '<aside id="sidebar">', "</aside>")
    # Three compartments by scope, never hidden (audit P17), widest first since
    # ADR-0072: the vault, the chats, this chat. The filter heads the rail,
    # above all three, because it narrows all three; pinned is a group of the
    # vault list rather than a compartment of its own.
    secs = re.findall(r'<details class="side-section" id="(side-\w+)"', rail)
    assert secs == ["side-vault", "side-chats", "side-chat"], secs
    assert rail.index('id="side-search"') < rail.index('id="side-vault"')
    vault = rail[rail.index('id="side-vault"'):rail.index('id="side-chats"')]
    assert 'id="pinned"' in vault and 'id="side-search"' not in vault
    assert "hidden" not in re.search(r'<details class="side-section" id="side-chat"[^>]*>', rail).group(0)
    # ...and the two that left are readings of the explore view: the surfaces
    # are its tabs, and the areas are the Graph panel's one list (ADR-0058)
    view = _block(INDEX.read_text(), '<section id="view-graph"', "</section>")
    assert 'id="surface-tabs"' in view and 'id="areas"' not in rail


def test_the_strip_is_gone_and_the_views_are_rows_of_the_drawer():
    """ADR-0084: no bar runs above the three surfaces any more. The views are
    rows of the drawer under New chat, as Claude's drawer lists its places; chat
    has no row, because a chat is a row of Chats. The view showing wears the
    --sel box the open note and the current chat wear."""
    html = INDEX.read_text()
    assert "<header>" not in html[html.index("<body>"):html.index("<main>")], "the top strip is back"
    rail = _block(html, '<aside id="sidebar">', "</aside>")
    views = _block(rail, '<nav id="views"', "</nav>")
    assert re.findall(r'data-tab="(\w+)"', views) == ["graph", "learn", "calendar", "metrics"]
    assert rail.index('id="side-search"') < rail.index('id="new-chat"') < rail.index('<nav id="views"') \
        < rail.index('id="side-vault"')
    assert 'data-tab="chat"' not in html
    css = app_css()
    active = css[css.index("#views .tab.active {"):]
    assert active[:active.index("}")].count("background: var(--sel);") == 1
    # the view showing is the current page, said out loud, on every row and icon
    assert 'b.setAttribute("aria-current", "page")' in app_js()


def test_a_pin_is_scoped_to_the_vault_it_was_made_in():
    """Switching vaults must not carry one vault's pins into another, so the
    storage key carries the path and a /vault switch re-reads it."""
    src = app_js()
    assert 'return "pinned:" + ($("#top-vname").title || "");' in src
    assert "if (path && path !== was) loadPins();" in src


def test_the_strip_and_the_panel_say_the_same_word():
    """Both are painted from one projectRun() call and one computed word. Two
    derivations would let the strip read idle while the panel beside it fills."""
    src = WORK_JS.read_text()
    assert src.count('run.running ? "live"') == 1
    assert "runState.textContent = state;" in src
    assert "paintRunLine(run);" in src  # the run line at the composer (P1)


def test_no_two_functions_in_app_js_share_a_name():
    """A second `function renderAreas(...)` in the same file is not an error in
    JavaScript: the later declaration is hoisted over the earlier one and every
    call in the file silently reaches the wrong body. That is how the rail's
    area spectrum shipped invisible for one commit -- it collided with the shape
    view's area-coupling matrix, and nothing in the console said so."""
    src = app_js()
    names = re.findall(r"^(?:async )?function ([A-Za-z_$][\w$]*)\(", src, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"declared twice at top level in app.js: {dupes}"
