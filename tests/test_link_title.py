# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0161: a pasted address asks for its page's title, read through /vault/fetch's own
`fetch` (no second fetcher, no model), and a failure is no title rather than an error."""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _page(html: str, ctype: str = "text/html; charset=utf-8"):
    return lambda url, *a, **k: (httpx.Response(200, headers={"content-type": ctype}, content=html.encode()), url)


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    return TestClient(server.app)


def test_the_title_is_the_pages_own(client, monkeypatch):
    seen: list[str] = []

    def fetch(url, *a, **k):
        seen.append(url)
        return _page('<html><head><meta property="og:title" content="Aspirin, the drug"><title>x</title>')(url)

    monkeypatch.setattr("silica.sources.web_fetch.fetch", fetch)
    r = client.get("/note/link-title", params={"url": "https://example.org/aspirin"})
    assert r.status_code == 200 and r.json() == {"title": "Aspirin, the drug"}
    assert seen == ["https://example.org/aspirin"]


def test_a_video_asks_oembed_and_not_the_watch_page(client, monkeypatch):
    seen: list[str] = []

    def fetch(url, *a, **k):
        seen.append(url)
        return _page('{"title": "A  talk", "type": "video"}', "application/json")(url)

    monkeypatch.setattr("silica.sources.web_fetch.fetch", fetch)
    r = client.get("/note/link-title", params={"url": "https://youtu.be/abc"})
    assert r.json() == {"title": "A talk"}
    assert seen[0].startswith("https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fabc")


def test_what_is_not_a_page_has_no_title(client, monkeypatch):
    monkeypatch.setattr("silica.sources.web_fetch.fetch", _page("plain words", "text/plain"))
    assert client.get("/note/link-title", params={"url": "https://e.org/a.txt"}).json() == {"title": ""}


def test_a_refused_address_keeps_the_link_as_pasted(client, monkeypatch):
    def fetch(url, *a, **k):
        raise ValueError("refusing non-global address 127.0.0.1")

    monkeypatch.setattr("silica.sources.web_fetch.fetch", fetch)
    assert client.get("/note/link-title", params={"url": "http://localhost:1234/"}).json() == {"title": ""}


@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "javascript:alert(1)", "https://a b.org", "https://e.org/" + "x" * 2050])
def test_only_one_web_address_is_read(client, monkeypatch, url):
    monkeypatch.setattr("silica.sources.web_fetch.fetch", lambda *a, **k: pytest.fail("fetched"))
    assert client.get("/note/link-title", params={"url": url}).status_code == 400


def test_another_site_cannot_make_this_server_fetch(client, monkeypatch):
    monkeypatch.setattr("silica.sources.web_fetch.fetch", lambda *a, **k: pytest.fail("fetched"))
    r = client.get("/note/link-title", params={"url": "https://e.org/"}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
