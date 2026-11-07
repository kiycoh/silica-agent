# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""My Wikis when the answers hold notes back, end to end in headless Chrome.

Opt-in like tests/test_calendar_e2e.py, whose DevTools driver this borrows. The view counts what
a Publish writes, so a wiki whose every note waits on an answer counts none: the press that opens
the check is the one way to those answers, and it has to stay open at every width.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tests.test_calendar_e2e import CHROME, Page, _free_port, pytestmark  # noqa: F401

GO = "document.querySelector('#public-body .pp-go')"
# Whether the cover's canvas holds more than its ground: two inks, so a printed cover has both.
INKED = """(() => {
  const c = document.querySelector('.pp-cover-box canvas');
  if (!c || !c.width) return false;
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  return new Set(Array.from({length: d.length / 4}, (_, i) => d[i * 4])).size > 1;
})()"""


@pytest.fixture
def app(tmp_vault, tmp_path, monkeypatch):
    import uvicorn

    import silica.cli  # noqa: F401  # fills the tool registry the graph export runs through
    from silica.kernel.base.manifest import reset_manifest_cache
    from silica.kernel.publish import add_vault
    from silica.kernel.write.provenance import append_record
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    for k in ("SILICA_HOSTED", "SILICA_VIEWS", "SILICA_PUBLIC_BASE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    reset_manifest_cache()
    tmp_vault.note("sources/Book.md", '---\nsource_file: "/x/Book.pdf"\n---\nThe book.\n')
    tmp_vault.note("Stats/Bayes.md", "From the book. [[Variance]]\n")
    tmp_vault.note("Stats/Variance.md", "From the book. [[Bayes]]\n")
    append_record("Book.md", "Book.md", "run-1", ["Stats/Bayes", "Stats/Variance"], vault_path=str(tmp_path / "vault"))
    add_vault("Stats")
    SESSION.reset()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    end = time.monotonic() + 15
    while not srv.started and time.monotonic() < end:
        time.sleep(0.05)
    assert srv.started, "the GUI did not start"
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
        reset_manifest_cache()


def _open(page: Page, width: int) -> None:
    page.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    page.send("Page.navigate", {"url": page.base + "/#public"})
    page.wait(f"{GO} && {GO}.textContent.length > 0", 30, "My Wikis to load")


@pytest.mark.parametrize("width", [1440, 390])
def test_a_wiki_held_by_its_answers_still_opens_the_check_and_the_view_follows_them(app, width):
    _open(app, width)
    assert app.js(f"{GO}.textContent") == "Check before publishing"
    assert app.js(f"{GO}.disabled") is False
    assert "2 notes waiting for an answer" in app.js("document.querySelector('#public-body').textContent")
    app.js(f"{GO}.click()")
    app.wait("!!document.querySelector('.pre-top')", 15, "the check before Publish")
    assert app.js("fetch('/public/check/answer', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                  " body: JSON.stringify({vault: 'stats', key: 'Book.pdf', answer: 'mine'})}).then(r => r.status)") == 200
    app.js("[...document.querySelectorAll('.pre-top button, .pre-top a')].find(e => e.textContent === 'Back').click()")
    app.wait(f"{GO} && {GO}.textContent.startsWith('Turn on and publish 2 notes')", 15, "the view to follow the answer")
    app.wait(INKED, 15, "the cover to print on the shared press")
