"""The public page's routes and Save to Silica (ADR-0143)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PAGE = "https://pages.silica-agent.dev/@marta/spacing-effect"
MD = ("---\ntitle: Spacing effect\ntags:\n- memory\n---\n"
      "Reviews spread over days. It follows from the "
      "[Forgetting curve](https://pages.silica-agent.dev/@marta/forgetting-curve) and the "
      "[Testing effect](https://pages.silica-agent.dev/@marta/testing-effect).\n\n"
      "How I use it: see Exam plan June.\n\n![hypnogram](https://pages.silica-agent.dev/@marta/assets/1-h.png)\n")


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    from silica.kernel.vault_manifest import reset_manifest_cache
    from silica.ui.web import server

    for k in ("SILICA_HOSTED", "SILICA_VIEWS", "SILICA_PUBLIC_BASE", "SILICA_PUBLIC_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    reset_manifest_cache()
    tmp_vault.note("Memory/Spacing effect.md", "Spaced reviews. [[Forgetting curve]], [[Exam plan June]].\n")
    tmp_vault.note("Memory/Forgetting curve.md", "Ebbinghaus.\n")
    tmp_vault.note("Courses/Exam plan June.md", "Private plan.\n")
    server._reset_session()
    yield TestClient(server.app), Path(tmp_path)
    reset_manifest_cache()


def test_the_view_reports_the_choice_and_what_publish_would_send(client):
    tc, _ = client
    v = tc.get("/public").json()
    assert v["available"] and v["handle"] == "marta" and not v["page"] and v["out"] == []
    v = tc.post("/public/folder", json={"folder": "Memory", "on": True}).json()
    assert v["folders"] == ["Memory"] and v["out"] == ["Forgetting curve", "Spacing effect"]
    assert {"words": "Exam plan June", "in": "Spacing effect"} in v["plain"]
    v = tc.post("/public/note", json={"path": "Memory/Forgetting curve.md", "on": False}).json()
    assert v["notes"]["Memory/Forgetting curve.md"] == "struck"
    assert any(p["why"] == "left out of Memory" for p in v["private"])
    assert tc.post("/public/folder", json={"folder": "sources", "on": True}).status_code == 400


def test_publish_turns_the_page_on_and_writes_it(client):
    tc, tmp = client
    tc.post("/public/folder", json={"folder": "Memory", "on": True})
    r = tc.post("/public/publish").json()
    assert r["page"] and r["result"]["notes"] == 2 and r["published"] == 2
    live = tmp / "public" / "marta" / "live"
    assert (live / "spacing-effect.html").is_file()
    assert "Private plan" not in (live / "spacing-effect.html").read_text(encoding="utf-8")
    off = tc.post("/public/page", json={"on": False}).json()
    assert not off["page"] and not live.exists()


def test_the_cover_is_the_graph_of_what_goes_out_and_nothing_private(client):
    """The view's cover (ADR-0146 point 4): the notes that go out, the links between them, laid
    out once; a private note it links to is neither a mark nor a line."""
    tc, _ = client
    assert tc.get("/public/cover").json() == {"n": [], "e": []}
    tc.post("/public/folder", json={"folder": "Memory", "on": True})
    c = tc.get("/public/cover").json()
    assert sorted(n[3] for n in c["n"]) == ["Forgetting curve", "Spacing effect"]
    assert all(0 <= n[0] <= 1 and 0 <= n[1] <= 1 for n in c["n"])
    assert len(c["e"]) == 1 and sorted(c["n"][i][3] for i in c["e"][0]) == ["Forgetting curve", "Spacing effect"]
    assert "Exam plan June" not in str(c)
    # the same notes and links draw the same picture
    assert tc.get("/public/cover").json() == c


def test_a_post_from_another_origin_is_refused(client):
    tc, _ = client
    r = tc.post("/public/folder", json={"folder": "Memory", "on": True},
                headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


def test_hosted_without_the_view_has_no_public_routes_but_can_save(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    assert tc.get("/public").status_code == 404
    assert tc.post("/public/publish").status_code == 404
    # saving stays: the preview answers (here, a refusal of the address), not 404
    assert tc.get("/vault/save/preview", params={"url": "https://elsewhere.test/@x/y"}).status_code == 400


def test_hosted_without_a_handle_offers_no_page(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.delenv("SILICA_PUBLIC_HANDLE")
    assert tc.get("/public").json()["available"] is False
    assert tc.post("/public/publish").status_code == 400


@pytest.fixture
def fetched(monkeypatch):
    """The page's .md as the web reader would hand it back; records the URLs."""
    import httpx

    from silica.sources import web_fetch

    seen = []

    def fake(url, headers=None, *, types=(), cap=0):
        seen.append(url)
        return httpx.Response(200, text=MD, headers={"content-type": "text/markdown"}), url

    monkeypatch.setattr(web_fetch, "_fetch", fake)
    return seen


def test_the_preview_reads_and_writes_nothing(client, fetched):
    tc, tmp = client
    p = tc.get("/vault/save/preview", params={"url": PAGE}).json()
    assert fetched == [PAGE + ".md"]
    assert p["title"] == "Spacing effect" and p["author"] == "@marta" and p["folder"] == "Saved/@marta"
    assert [l["to"] for l in p["links"]] == ["web", "web"] and p["images"] == 1
    assert "content" not in p
    assert not (tmp / "vault" / "Saved").exists()


def test_save_writes_a_snapshot_crediting_the_author_and_links_what_it_can(client, fetched, tmp_vault):
    tc, tmp = client
    tmp_vault.note("Saved/@marta/Forgetting curve.md",
                   "---\nauthor: '@marta'\nsource: https://pages.silica-agent.dev/@marta/forgetting-curve\n---\nEbb.\n")
    path = tc.post("/vault/save", json={"url": PAGE}).json()["path"]
    assert path == "Saved/@marta/Spacing effect.md"
    text = (tmp / "vault" / path).read_text(encoding="utf-8")
    assert "author: '@marta'" in text and f"source: {PAGE}" in text and "form: clip" in text
    assert "[[Saved/@marta/Forgetting curve|Forgetting curve]]" in text
    assert "[Testing effect](https://pages.silica-agent.dev/@marta/testing-effect)" in text
    assert "see Exam plan June." in text
    # a second save is a second copy, never a rewrite of the first
    again = tc.post("/vault/save", json={"url": PAGE}).json()["path"]
    assert again == "Saved/@marta/Spacing effect 2.md"
    # and the drawer names the author over the copy
    raw = tc.get("/note/raw", params={"path": path}).json()
    assert raw["saved"]["author"] == "@marta" and raw["saved"]["source"] == PAGE


@pytest.mark.parametrize("url", [
    "http://pages.silica-agent.dev/@marta/spacing-effect",          # not https
    "https://evil.test/@marta/spacing-effect",                 # not a listed host
    "https://pages.silica-agent.dev/@marta/spacing-effect?x=1",      # a query
    "https://pages.silica-agent.dev/notes/spacing-effect",           # not a page address
    "https://pages.silica-agent.dev@evil.test/@marta/x",             # a userinfo trick
])
def test_save_refuses_anything_but_a_public_page_address(client, fetched, url):
    tc, _ = client
    assert tc.post("/vault/save", json={"url": url}).status_code == 400
    assert fetched == []


def test_a_page_that_moved_to_another_host_is_not_saved(client, monkeypatch):
    import httpx

    from silica.sources import web_fetch

    monkeypatch.setattr(web_fetch, "_fetch", lambda url, headers=None, *, types=(), cap=0:
                        (httpx.Response(200, text=MD), "https://evil.test/x.md"))
    tc, tmp = client
    assert tc.post("/vault/save", json={"url": PAGE}).status_code == 400
    assert not (tmp / "vault" / "Saved").exists()


def test_a_hostile_page_cannot_pick_the_folder_or_the_credit(client, monkeypatch):
    import httpx

    from silica.sources import web_fetch

    hostile = ("---\ntitle: Spacing effect\nauthor: \"../../x\"\nsource: https://evil.test/@marta/y\n---\nBody.\n")
    monkeypatch.setattr(web_fetch, "_fetch", lambda url, headers=None, *, types=(), cap=0:
                        (httpx.Response(200, text=hostile), url))
    tc, tmp = client
    path = tc.post("/vault/save", json={"url": PAGE}).json()["path"]
    assert path == "Saved/@marta/Spacing effect.md"
    text = (tmp / "vault" / path).read_text(encoding="utf-8")
    assert "author: '@marta'" in text and f"source: {PAGE}" in text and "evil.test" not in text


def test_a_copy_credits_its_original_only_when_the_original_is_that_authors_page(client, monkeypatch):
    import httpx

    from silica.sources import web_fetch

    copy = ("---\ntitle: Borrowed\nauthor: '@luca'\nsource: https://pages.silica-agent.dev/@luca/borrowed\n---\nLuca's.\n")
    monkeypatch.setattr(web_fetch, "_fetch", lambda url, headers=None, *, types=(), cap=0:
                        (httpx.Response(200, text=copy), url))
    tc, tmp = client
    path = tc.post("/vault/save", json={"url": "https://pages.silica-agent.dev/@marta/borrowed"}).json()["path"]
    assert path == "Saved/@luca/Borrowed.md"
    assert "source: https://pages.silica-agent.dev/@luca/borrowed" in (tmp / "vault" / path).read_text(encoding="utf-8")


def test_hosted_without_an_address_offers_no_page(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.delenv("SILICA_PUBLIC_BASE", raising=False)
    assert tc.get("/public").json()["available"] is False
    assert tc.post("/public/publish").status_code == 400
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test")
    assert tc.get("/public").json()["available"] is True
