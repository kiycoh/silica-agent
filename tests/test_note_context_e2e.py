# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The three P1s of the 2026-10-25 GUI critique, end to end in headless Chrome:
the open note rides the chat turn as a chip, Explore folds its right column
when a note is open beside it and its find opens the note, and a reload keeps
the note the address names.

Opt-in like tests/test_calendar_e2e.py, whose DevTools driver this borrows:
SILICA_E2E=1 starts a browser per test. No turn reaches a model: the page's
fetch answers /chat with a finished stream and records what was sent.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.test_calendar_e2e import CHROME, Page, _free_port, pytestmark  # noqa: F401

NOTE = "Codice civile/Art. 1453 c.c. - Risolubilità del contratto per inadempimento.md"
OTHER = "Codice civile/Art. 1455 c.c. - Importanza dell'inadempimento.md"

# /chat answered at once, the body kept on window.__sent; every other call is real.
FAKE_CHAT = """
window.__sent = [];
const realFetch = window.fetch;
window.fetch = (url, opts) => {
  if (url !== "/chat") return realFetch(url, opts);
  window.__sent.push(JSON.parse(opts.body));
  const done = 'data: {"type":"done","answer":"ok","html":"<p>ok</p>"}\\n\\n';
  const body = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(done)); c.close(); } });
  return Promise.resolve(new Response(body, { headers: { "Content-Type": "text/event-stream" } }));
};
"""


@pytest.fixture
def app(tmp_vault):
    import uvicorn

    import silica.cli  # noqa: F401  # fills the tool registry the graph export runs through
    from silica.ui.web import server

    capo = "[[Capo XIV - Della risoluzione del contratto]]"
    tmp_vault.note(NOTE, f"# Art. 1453\n\nNei contratti con prestazioni corrispettive...\n\n{capo}\n")
    tmp_vault.note(OTHER, f"# Art. 1455\n\nIl contratto non si può risolvere...\n\n{capo}\n")
    tmp_vault.note("Codice civile/Capo XIV - Della risoluzione del contratto.md",
                   "# Capo XIV\n\n- [[Art. 1453 c.c. - Risolubilità del contratto per inadempimento]]\n")
    server._reset_session()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    end = time.monotonic() + 15
    while not srv.started and time.monotonic() < end:
        time.sleep(0.05)
    assert srv.started, "the GUI did not start"

    import subprocess

    profile = tempfile.mkdtemp(prefix="silica-e2e-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                               "--window-size=1440,900", "--no-first-run", "--no-default-browser-check",
                               "--disable-extensions", "--mute-audio", "--enable-unsafe-swiftshader", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        marker = Path(profile) / "DevToolsActivePort"
        end = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < end:
            time.sleep(0.05)
        devtools = int(marker.read_text().split()[0])
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{devtools}/json/list", timeout=10))
        page = Page(next(t for t in targets if t["type"] == "page")["webSocketDebuggerUrl"])
        page.send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        page.base = f"http://127.0.0.1:{port}"
        yield page
        assert not page.exceptions, f"the page threw: {page.exceptions}"
    finally:
        if page:
            page.ws.close()
        chrome.terminate()
        chrome.wait(10)
        shutil.rmtree(profile, ignore_errors=True)
        srv.should_exit = True
        th.join(10)


def _go(page: Page, hash_: str) -> None:
    page.send("Page.navigate", {"url": page.base + "/" + hash_})
    page.wait("document.querySelectorAll('#tree a.tree-note').length >= 3", 30, "the tree to load")


def _drawer_on(page: Page, path: str) -> None:
    page.wait(f"document.querySelector('#note-panel').classList.contains('open') && lastNotePath === {path!r}",
              15, f"the drawer to hold {path}")


def _row(path: str) -> str:
    # json.dumps: OTHER's "dell'inadempimento" would end a single-quoted string
    return f"document.querySelector({json.dumps(f'#tree a.tree-note[data-id=\"{path}\"]')})"


def test_a_reload_keeps_the_note_the_address_names(app):
    for view in ("#explore/graph", "#chat"):
        _go(app, f"{view}?note={quote(NOTE)}")
        _drawer_on(app, NOTE)
        assert "?note=" in app.js("location.hash"), view
    app.js("location.reload()")
    app.wait("document.querySelectorAll('#tree a.tree-note').length >= 3", 30, "the tree after the reload")
    _drawer_on(app, NOTE)
    # A view row puts the note away; Back brings the address, and the note, back.
    app.click("document.querySelector('#views .tab[data-tab=\"calendar\"]')")
    app.wait("!document.querySelector('#note-panel').classList.contains('open')", 5, "the note to be put away")
    app.js("history.back()")
    _drawer_on(app, NOTE)


def test_the_open_note_rides_every_turn_and_its_x_skips_one(app):
    _go(app, "#chat")
    app.js(FAKE_CHAT)
    app.click(_row(NOTE))
    _drawer_on(app, NOTE)
    app.wait("!document.querySelector('#note-chip').hidden && !!document.querySelector('#note-chip .note-chip')",
             5, "the chip")

    def ask(words: str, n: int) -> dict:
        app.click("document.querySelector('#input')")
        app.type(words)
        app.key("Enter")
        app.wait(f"window.__sent.length === {n} && !streaming", 10, f"turn {n}")
        # The first send moves the landing onto the transcript in a view
        # transition, whose overlay takes every click until it ends.
        app.wait("!document.documentElement.classList.contains('vt-landing')", 5, "the landing transition")
        return app.js("window.__sent")[n - 1]

    assert ask("cosa dice questo articolo?", 1)["note"] == NOTE
    assert "Art. 1453" in app.js("[...document.querySelectorAll('.msg.user .msg-note')].pop().textContent")
    assert app.js("!!document.querySelector('#note-chip .note-chip')"), "the chip stays after a send"

    # A view transition's overlay hands its clicks to the root: never "outside".
    app.js("document.documentElement.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    _drawer_on(app, NOTE)

    app.click("document.querySelector('#note-chip .chip-x')")
    assert app.js("!!document.querySelector('#note-chip .note-chip-add')")
    assert "note" not in ask("e in generale?", 2)
    assert app.js("!!document.querySelector('#note-chip .note-chip')"), "the x skipped one message only"

    # The x belongs to the note it was pressed on: another note, then this one
    # again, and the chip is back.
    app.click("document.querySelector('#note-chip .chip-x')")
    app.click(_row(OTHER))
    _drawer_on(app, OTHER)
    app.click(_row(NOTE))
    _drawer_on(app, NOTE)
    assert app.js("!!document.querySelector('#note-chip .note-chip')")

    app.click("document.querySelector('#note-close')")
    app.wait("document.querySelector('#note-chip').hidden", 5, "the chip to leave with the note")
    assert "note" not in ask("e ora?", 3)


def test_explore_folds_its_column_beside_a_note_and_find_opens_it(app):
    _go(app, "#explore/graph")
    app.wait("!document.querySelector('#graph-frame').hidden && document.querySelector('#graph-loading').hidden",
             30, "the graph")
    folded = "document.querySelector('#view-graph').classList.contains('side-folded')"
    frame_key_shut = "document.querySelector('#graph-frame').contentDocument.body.classList.contains('key-folded')"
    assert not app.js(folded) and app.js("document.querySelector('#graph-key').hidden")

    app.click("document.querySelector('#node-search')")
    app.type("1455")
    app.wait("!document.querySelector('#node-results').hidden", 5, "the results")
    app.key("Enter")
    _drawer_on(app, OTHER)                      # the find opens the note, as a click on its node does

    app.wait(folded, 5, "the column to fold beside the note")
    assert app.js("getComputedStyle(document.querySelector('#graph-picks')).display") == "none"
    app.wait(frame_key_shut, 5, "the frame's key to fold")
    assert app.js("document.querySelector('#view-graph').clientWidth") < 760

    # The column's own controls never put the note away: it would unfold the
    # column under the pointer (the resize lands a frame later, hence the sleep).
    app.click("document.querySelector('#node-search')")
    app.click("document.querySelector('#graph-key')")
    time.sleep(0.5)
    _drawer_on(app, OTHER)
    assert app.js(folded) and app.js("document.querySelector('#view-graph').classList.contains('key-open')")
    assert app.js("document.querySelector('#graph-key').getAttribute('aria-expanded')") == "true"
    assert app.js("getComputedStyle(document.querySelector('#graph-picks')).display") != "none"
    app.wait(f"!{frame_key_shut}", 5, "the frame's key to open")

    app.click("document.querySelector('#note-close')")
    app.wait(f"!{folded}", 5, "the column to unfold")
    assert app.js("document.querySelector('#graph-key').hidden") and not app.js(frame_key_shut)


def test_a_question_sent_while_the_transcript_loads_stays_on_screen(app):
    """loadVault redrew the chat from a /messages reply that left before the
    question did, and the question vanished while its turn ran on (a real
    turn, 2026-10-25). The reply is held here until the question is drawn."""
    _go(app, "#chat")
    app.js(FAKE_CHAT)
    app.js("""(() => { const f = window.fetch;
      window.fetch = (u, o) => u === "/messages" ? new Promise((res) => { window.__release = () => res(f(u, o)); }) : f(u, o);
      window.__loading = loadVault(); return 1; })()""")
    app.click("document.querySelector('#input')")
    app.type("prima domanda")
    app.key("Enter")
    app.wait("window.__sent.length === 1 && !streaming", 10, "the turn")
    app.js("(window.__release(), window.__loading)")
    assert app.js("document.querySelectorAll('#log .msg.user').length") == 1
    assert app.js("document.querySelector('#log .msg.user .body').textContent") == "prima domanda"
