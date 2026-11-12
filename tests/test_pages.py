# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0171: a page the chat writes into Pages/, its footer, its revert, and the
route that serves it in an origin of its own."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.kernel.write import pages

PAGE = ("<!doctype html><html><head><style>h1{color:teal}</style></head>"
        "<body><h1>Rome</h1><img src=\"../img/map.png\"><script>let n = 1 < 2;</script></body></html>")


class TestPath:
    @pytest.mark.parametrize("path", ["Pages/rome.html", "Pages/rome/index.html", "Pages/a b.html"])
    def test_a_page_lives_under_pages(self, path):
        assert pages.page_path(path) == path

    @pytest.mark.parametrize("path", ["rome.html", "Pages/rome.md", "Pages/.hidden.html", "Pages",
                                      "Notes/Pages/x.html", "Pages/../x.html"])
    def test_anything_else_is_refused(self, path):
        assert not pages.is_page(path)


class TestNetwork:
    @pytest.mark.parametrize("html", [
        '<script src="https://cdn.example/x.js"></script>',
        "<link rel=stylesheet href='//fonts.example/css'>",
        "<style>body{background:url( https://x/y.png )}</style>",
        '<style>@import "https://fonts.example/a.css";</style>',
        "<script>fetch('/api')</script>",
        "<script>new WebSocket(u)</script>",
        '<a href="https://example.org">out</a>',
        '<meta http-equiv="refresh" content="0; url=https://x">',
        # a navigation of the page's own frame carries data out in the address
        '<script>location = "/x?d=" + document.title</script>',
        "<script>top.location.href = u</script>",
        "<script>document.location.assign(u)</script>",
        "<script>window.open(u)</script>",
    ])
    def test_a_request_to_anywhere_is_named(self, html):
        assert pages.network_refs(html)

    def test_what_stays_inside_passes(self):
        html = ('<svg xmlns="http://www.w3.org/2000/svg"></svg><img src="../a.png">'
                '<img src="data:image/png;base64,AA"><a href="other.html">next</a>'
                "<p>see https://doi.org/10.1/x in the text</p><a href=\"#top\">top</a>"
                "<script>if (location == x) location.hash = '#a';</script>")
        assert pages.network_refs(html) == []


def test_the_footer_lists_the_notes_once_and_a_revision_replaces_it():
    once = pages.with_footer(PAGE, ["Rome/Republic.md", "A <b>.md"])
    assert once.index("<footer data-silica-sources") < once.index("</body>")
    assert "A &lt;b&gt;.md" in once
    twice = pages.with_footer(once, ["Rome/Empire.md"])
    assert twice.count("<footer data-silica-sources") == 1
    assert "Rome/Empire.md" in twice and "Rome/Republic.md" not in twice


# --- the tool, its card's revert, and /page -------------------------------------

def _write(path="Pages/rome.html", html=PAGE, sources=("Rome/Republic.md",)) -> dict:
    from silica.tools import TOOLS

    return json.loads(TOOLS["silica_write_page"].run(path=path, html=html, sources=list(sources)))


def _undo(path: str) -> str:
    from silica.commands import dispatch
    from silica.term.console import plain

    out = dispatch(f"/undo {path}", [])
    assert out is not None
    return plain(out.lines)


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("Rome/Republic.md", "# Republic\n\nFounded 509 BC.\n")
    return Path(CONFIG.vault_path)


def test_a_page_is_written_with_its_footer_and_taken_back_step_by_step(vault):
    out = _write()
    assert out["success"] and out["created"] and out["checkpoint_ok"]
    first = (vault / "Pages/rome.html").read_text(encoding="utf-8")
    assert "Rome/Republic.md" in first.split("<footer", 1)[1]
    assert _write(html=PAGE.replace("Rome", "Roma"))["created"] is False
    assert "Roma" in (vault / "Pages/rome.html").read_text(encoding="utf-8")
    assert "Undone: Pages/rome.html" in _undo("Pages/rome.html")
    assert (vault / "Pages/rome.html").read_text(encoding="utf-8") == first
    assert "removed (the chat created it)" in _undo("Pages/rome.html")
    assert not (vault / "Pages/rome.html").exists()
    assert "Restored" in _undo("Pages/rome.html")
    assert (vault / "Pages/rome.html").read_text(encoding="utf-8") == first


def test_a_page_with_no_note_behind_it_or_a_request_out_is_not_written(vault):
    assert "built from notes" in _write(sources=("Nowhere.md",))["error"]
    assert "reaches nothing outside" in _write(html='<img src="https://x/y.png">')["error"]
    assert "Refused path" in _write(path="rome.html")["error"]
    assert not (vault / "Pages").exists()


def test_a_source_that_is_no_note_is_left_out_and_said(vault):
    out = _write(sources=("Rome/Republic.md", "Ghost"))
    assert "Ghost" in out["warning"]
    assert "Ghost" not in (vault / "Pages/rome.html").read_text(encoding="utf-8")


def test_a_page_is_no_note_of_the_index(vault):
    from silica.kernel.recall.resolve import note_resolver

    _write()
    assert note_resolver()("Pages/rome.html") is None


def test_the_page_command_asks_the_model_to_read_then_write(vault):
    from tests.run_command import expansion, refusal

    asked = expansion('/page "a timeline of Rome"')
    assert "a timeline of Rome" in asked and "silica_write_page" in asked
    assert refusal("/page").startswith("Usage: /page")
    # An apostrophe is a letter in what the page is about, not an open quote.
    assert "Make a page: a site on Caravaggio's light\n" in expansion("/page a site on Caravaggio's light")


# --- the route --------------------------------------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(vault):
    from silica.ui.web import server

    return TestClient(server.app)


def test_the_page_is_served_in_an_origin_of_its_own(client, vault):
    from silica.ui.web import render

    _write()
    r = client.get("/page/Pages/rome.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert csp == render.page_headers("testserver")["Content-Security-Policy"]
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp
    assert "default-src 'none'" in csp
    # Its images from the route's own paths only: under 'self' a hand-written
    # page's <img> reached any route of the app, /vault_brief?refresh=1 among
    # them, which is a model call on the person's key per request.
    assert "img-src data: testserver/page/;" in csp and "'self'" not in csp


@pytest.mark.parametrize("script", ["app-notes.js", "app-zotero.js"])
def test_the_app_takes_messages_only_from_its_own_origin(script):
    """A page in the drawer holds `parent` and can post to it: a hand-written one
    opened a note in place of itself with `silica-open-line` (2026-11-13). Its
    origin is opaque, so its messages arrive from "null"; the graph frame's from
    the app's own."""
    import re

    from silica.ui.web.server import STATIC_DIR

    js = (STATIC_DIR / script).read_text(encoding="utf-8")
    listeners = re.findall(r'window\.addEventListener\("message", \(e\) => \{\n(.*)\n', js)
    assert listeners and all("e.origin !== location.origin" in first for first in listeners)


def test_a_page_navigating_its_frame_cannot_make_the_app_call_the_model(client, monkeypatch):
    """frame-src 'self' lets a page navigate its own frame to any route of the app:
    `location = "/vault_brief?refresh=1"` from a hand-written page was a worker-model
    call (2026-11-13). Its navigation comes from an opaque origin, so cross-site."""
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_brief", False)
    assert client.get("/vault_brief?refresh=1", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.get("/vault_brief", headers={"sec-fetch-site": "same-origin"}).json() == {"enabled": False, "text": ""}


def test_a_host_header_that_is_no_host_puts_nothing_in_the_page_policy():
    from silica.ui.web import render

    csp = render.page_headers("127.0.0.1:8785; script-src *")["Content-Security-Policy"]
    assert "img-src data:;" in csp and "script-src *" not in csp
    assert "img-src data: localhost:8785/page/;" in render.page_headers("LocalHost:8785")["Content-Security-Policy"]


def test_its_relative_image_is_the_vaults_and_nothing_else_is_served(client, vault):
    from silica.ui.web import render

    (vault / "img").mkdir()
    (vault / "img/map.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (vault / "Pages").mkdir()
    (vault / "Pages/raw.html").write_text("<p>x</p>", encoding="utf-8")
    (vault / "secret.html").write_text("<p>no</p>", encoding="utf-8")
    img = client.get("/page/img/map.png")
    assert img.status_code == 200
    assert img.headers["content-security-policy"] == render.ASSET_HEADERS["Content-Security-Policy"]
    assert client.get("/page/Pages/raw.html").status_code == 200
    for path in ("secret.html", "Rome/Republic.md", "Pages/missing.html", "Pages/../secret.html"):
        assert client.get(f"/page/{path}").status_code == 404, path


def test_a_demo_serves_no_page(client, vault, monkeypatch):
    _write()
    monkeypatch.setenv("SILICA_DEMO", "1")
    assert client.get("/page/Pages/rome.html").status_code == 404
