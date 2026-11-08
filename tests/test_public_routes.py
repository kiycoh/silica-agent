"""The Page's routes, Save to Silica and invitations (ADR-0143, ADR-0151)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BASE = "https://wikis.silica-agent.dev/@marta/memory/"
PAGE = BASE + "spacing-effect"
MD = ("---\ntitle: Spacing effect\ntags:\n- memory\nvault: Memoria\n---\n"
      "Reviews spread over days. It follows from the "
      f"[Forgetting curve]({BASE}forgetting-curve) and the "
      f"[Testing effect]({BASE}testing-effect).\n\n"
      f"How I use it: see Exam plan June.\n\n![hypnogram]({BASE}assets/1-h.png)\n")


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


def test_the_view_reports_each_vault_and_what_publish_would_send(client):
    tc, _ = client
    v = tc.get("/public").json()
    assert v["available"] and v["handle"] == "marta" and v["vaults"] == [] and v["max"] == 20
    assert "Memory" in v["folders"] and v["private"] == "hosted"
    v = tc.post("/public/vault", json={"folder": "Memory"}).json()
    assert v["added"] == "memory" and "Memory" not in v["folders"]
    m = v["vaults"][0]
    assert (m["slug"], m["visibility"], m["out"]) == ("memory", "off", 2)
    assert m["top"][0] == "Spacing effect" or m["top"][0] == "Forgetting curve"
    v = tc.post("/public/note", json={"path": "Memory/Forgetting curve.md", "on": False}).json()
    assert v["notes"]["Memory/Forgetting curve.md"] == "struck" and v["vaults"][0]["struck"] == 1
    assert tc.post("/public/vault", json={"folder": "sources"}).status_code == 400
    v = tc.post("/public/vault/memory", json={"title": "Memoria", "line": "How it works", "license": "CC BY 4.0"}).json()
    assert (v["vaults"][0]["title"], v["vaults"][0]["license"]) == ("Memoria", "CC BY 4.0")
    assert tc.post("/public/vault/memory", json={"license": "mine"}).status_code == 400
    assert tc.post("/public/vault/memory", json={"visibility": "private"}).status_code == 400


def test_publish_turns_the_vault_public_and_writes_it_and_off_takes_it_down(client):
    tc, tmp = client
    tc.post("/public/vault", json={"folder": "Memory"})
    r = tc.post("/public/publish", json={"vault": "memory"}).json()
    assert r["vaults"][0]["visibility"] == "public" and r["result"]["notes"] == 2
    assert r["vaults"][0]["published"]
    live = tmp / "public" / "marta" / "live"
    assert (live / "memory" / "spacing-effect.html").is_file() and (live / "index.html").is_file()
    assert "Private plan" not in (live / "memory" / "spacing-effect.html").read_text(encoding="utf-8")
    off = tc.post("/public/vault/memory", json={"visibility": "off"}).json()
    assert off["vaults"][0]["visibility"] == "off"
    assert not (live / "memory").exists() and not (live / "index.html").exists()
    # an Off vault leaves the Page; one still on it refuses
    assert tc.post("/public/vault/memory/remove").json()["vaults"] == []


def test_listed_reaches_the_published_manifest_at_once(client):
    import json

    tc, tmp = client
    tc.post("/public/vault", json={"folder": "Memory"})
    tc.post("/public/vault/memory", json={"license": "CC BY 4.0"})
    tc.post("/public/publish", json={"vault": "memory"})
    tc.post("/public/vault/memory", json={"listed": True})
    m = json.loads((tmp / "public" / "marta" / "live" / "memory" / "vault.json").read_text(encoding="utf-8"))
    assert m["listed"] is True


def test_the_view_says_what_the_host_said_of_each_wiki(client, monkeypatch):
    """`listed` is the author's request; what became of it is the host's (ADR-0153 point 7)."""
    import json

    tc, tmp = client
    tc.post("/public/vault", json={"folder": "Memory"})
    assert tc.get("/public").json()["vaults"][0]["review"] == {}
    host = tmp / "host"
    host.mkdir()
    (host / "handle.json").write_text('{"handle": "marta"}', encoding="utf-8")
    monkeypatch.setenv("SILICA_HOST_DIR", str(host))
    assert tc.get("/public").json()["vaults"][0]["review"] == {}
    said = {"state": "no", "since": "2026-11-06", "rule": "Rule 2: Name your sources.", "seen": "Copied text."}
    (host / "wikis.json").write_text(json.dumps({"memory": said, "other": {"state": "in"}}), encoding="utf-8")
    assert tc.get("/public").json()["vaults"][0]["review"] == said
    (host / "wikis.json").write_text("half a file", encoding="utf-8")
    assert tc.get("/public").json()["vaults"][0]["review"] == {}


def test_a_blocked_wiki_says_why_and_a_request_does_not_reach_its_manifest(client):
    import json

    tc, tmp = client
    tc.post("/public/vault", json={"folder": "Memory"})
    assert tc.get("/public").json()["vaults"][0]["blocked"] == []
    tc.post("/public/publish", json={"vault": "memory"})
    v = tc.post("/public/vault/memory", json={"listed": True}).json()["vaults"][0]
    assert "license" in v["blocked"][0]
    m = json.loads((tmp / "public" / "marta" / "live" / "memory" / "vault.json").read_text(encoding="utf-8"))
    # the manifest is public: the reasons are the author's, and one names a note with personal data
    assert m["listed"] is False and m["blocked"] is True
    tc.post("/public/vault/memory", json={"license": "CC BY 4.0"})
    tc.post("/public/publish", json={"vault": "memory"})
    assert tc.get("/public").json()["vaults"][0]["blocked"] == []


def test_the_cover_is_the_graph_of_what_goes_out_and_nothing_private(client):
    """The view's cover (ADR-0146 point 4): the notes that go out, the links between them, laid
    out once; a private note it links to is neither a mark nor a line."""
    tc, _ = client
    assert tc.get("/public/cover", params={"vault": "memory"}).json() == {"n": [], "e": []}
    tc.post("/public/vault", json={"folder": "Memory"})
    c = tc.get("/public/cover", params={"vault": "memory"}).json()
    assert sorted(n[4] for n in c["n"]) == ["Forgetting curve", "Spacing effect"]
    assert all(0 <= n[0] <= 1 and 0 <= n[1] <= 1 for n in c["n"])
    assert c["e"] == [[0, 1]] and len(c["areas"]) == 1
    assert "Exam plan June" not in str(c)
    # the same notes and links draw the same picture
    assert tc.get("/public/cover", params={"vault": "memory"}).json() == c


def test_a_post_from_another_origin_is_refused(client):
    tc, _ = client
    r = tc.post("/public/vault", json={"folder": "Memory"}, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


def test_hosted_without_the_view_has_no_page_routes_but_can_save(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    assert tc.get("/public").status_code == 404
    assert tc.post("/public/publish", json={"vault": "memory"}).status_code == 404
    # saving stays: the preview answers (here, a refusal of the address), not 404
    assert tc.get("/vault/save/preview", params={"url": "https://elsewhere.test/@x/y/z"}).status_code == 400
    assert tc.get("/vault/invitations").status_code == 200


def test_hosted_without_a_handle_offers_no_page(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.delenv("SILICA_PUBLIC_HANDLE")
    assert tc.get("/public").json()["available"] is False
    assert tc.post("/public/publish", json={"vault": "memory"}).status_code == 400


def _hosted_claims(monkeypatch, tmp):
    """A hosted account with no handle, on a host that takes claims."""
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test")
    monkeypatch.delenv("SILICA_PUBLIC_HANDLE")
    monkeypatch.setenv("SILICA_HOST_DIR", str(tmp / "host"))
    monkeypatch.setenv("HOME", str(tmp / "home"))
    (tmp / "host").mkdir()
    return tmp / "host" / "handle.json", tmp / "home" / ".silica" / "handle-request.json"


def test_a_hosted_account_asks_for_a_handle_and_the_host_answers(client, monkeypatch):
    import json

    tc, tmp = client
    answer, request = _hosted_claims(monkeypatch, tmp)
    v = tc.get("/public").json()
    assert v["available"] is False and v["ask_handle"]["ask"] is True and "admin" in v["ask_handle"]["reserved"]
    for bad in ("admin", "a--b", "-x", "Ab_c", "x" * 31):
        assert tc.post("/public/handle", json={"handle": bad}).status_code == 400, bad
    assert not request.exists()
    v = tc.post("/public/handle", json={"handle": "@Sara"}).json()
    assert json.loads(request.read_text()) == {"handle": "sara"} and v["ask_handle"]["pending"] == "sara"
    # the host's pass: taken, the request answered and removed
    request.unlink()
    answer.write_text(json.dumps({"asked": "sara", "refused": "taken"}))
    assert tc.get("/public").json()["ask_handle"]["refused"] == {"handle": "sara", "why": "taken"}
    # granted, with no restart: the file is read before the unit's env
    answer.write_text(json.dumps({"handle": "sara-k"}))
    v = tc.get("/public").json()
    assert v["available"] is True and v["handle"] == "sara-k" and v["ask_handle"] == {"ask": False}
    assert v["url"] == "https://pages.test/@sara-k"
    assert tc.post("/public/handle", json={"handle": "other"}).status_code == 400


def test_a_host_that_takes_no_claims_keeps_giving_handles_itself(client, monkeypatch):
    tc, tmp = client
    _hosted_claims(monkeypatch, tmp)
    monkeypatch.delenv("SILICA_HOST_DIR")
    assert tc.get("/public").json()["ask_handle"] == {"ask": False}
    assert tc.post("/public/handle", json={"handle": "sara"}).status_code == 400
    assert not (tmp / "home" / ".silica" / "handle-request.json").exists()


def test_a_laptop_takes_its_handle_into_the_vault_unless_it_already_published(client, monkeypatch):
    from silica.kernel import publish

    tc, tmp = client
    monkeypatch.delenv("SILICA_PUBLIC_HANDLE")
    ask = tc.get("/public").json()["ask_handle"]
    assert ask["ask"] is True and ask["suggest"] == publish.slugify((tmp / "vault").resolve().name)
    v = tc.post("/public/handle", json={"handle": "marta-v"}).json()
    assert v["handle"] == "marta-v" and v["ask_handle"] == {"ask": False}
    assert publish._block((tmp / "vault"))["handle"] == "marta-v"
    # a laptop that already has a page under its folder's name is not asked
    from silica.kernel.vault_manifest import unset_block_key

    unset_block_key((tmp / "vault"), publish.PUBLIC_KEY, "handle")
    (tmp / "public" / publish.handle((tmp / "vault"))).mkdir(parents=True)
    assert tc.get("/public").json()["ask_handle"] == {"ask": False}


def test_the_view_counts_against_the_account_s_tier(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    assert tc.get("/public").json()["max"] == 3
    monkeypatch.setenv("SILICA_VIEWS", "all")
    assert tc.get("/public").json()["max"] == 20


def test_a_free_account_loses_private_and_its_private_vault_turns_off(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    tc.post("/public/vault", json={"folder": "Memory"})
    v = tc.post("/public/vault/memory", json={"visibility": "private", "readers": ["sara"]}).json()
    assert v["vaults"][0]["visibility"] == "private" and v["vaults"][0]["readers"] == ["sara"]
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    v = tc.get("/public").json()
    assert v["private"] == "paid" and v["turned_off"] == ["memory"]
    assert v["vaults"][0]["visibility"] == "off" and v["vaults"][0]["downgraded"]


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
    assert p["title"] == "Spacing effect" and p["author"] == "@marta" and p["folder"] == "Saved/@marta/Memoria"
    assert [l["to"] for l in p["links"]] == ["web", "web"] and p["images"] == 1
    assert "content" not in p
    assert not (tmp / "vault" / "Saved").exists()


def test_save_writes_a_snapshot_crediting_the_author_and_links_what_it_can(client, fetched, tmp_vault):
    tc, tmp = client
    tmp_vault.note("Saved/@marta/Memoria/Forgetting curve.md",
                   f"---\nauthor: '@marta'\nsource: {BASE}forgetting-curve\n---\nEbb.\n")
    path = tc.post("/vault/save", json={"url": PAGE}).json()["path"]
    assert path == "Saved/@marta/Memoria/Spacing effect.md"
    text = (tmp / "vault" / path).read_text(encoding="utf-8")
    assert "author: '@marta'" in text and f"source: {PAGE}" in text and "form: clip" in text
    assert "[[Saved/@marta/Memoria/Forgetting curve|Forgetting curve]]" in text
    assert f"[Testing effect]({BASE}testing-effect)" in text
    assert "see Exam plan June." in text
    # a second save is a second copy, never a rewrite of the first
    again = tc.post("/vault/save", json={"url": PAGE}).json()["path"]
    assert again == "Saved/@marta/Memoria/Spacing effect 2.md"
    # and the drawer names the author over the copy
    raw = tc.get("/note/raw", params={"path": path}).json()
    assert raw["saved"]["author"] == "@marta" and raw["saved"]["source"] == PAGE


@pytest.mark.parametrize("url", [
    "http://wikis.silica-agent.dev/@marta/memory/spacing-effect",     # not https
    "https://evil.test/@marta/memory/spacing-effect",                 # not a listed host
    "https://wikis.silica-agent.dev/@marta/memory/spacing-effect?x=1",  # a query
    "https://wikis.silica-agent.dev/notes/spacing-effect",            # not a page address
    "https://wikis.silica-agent.dev/@marta/spacing-effect",           # ADR-0143's shape, gone
    "https://wikis.silica-agent.dev@evil.test/@marta/memory/x",       # a userinfo trick
    "https://wikis.silica-agent.dev:8443/@marta/memory/",             # another port
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

    orig = "https://wikis.silica-agent.dev/@luca/notes/borrowed"
    copy = f"---\ntitle: Borrowed\nauthor: '@luca'\nsource: {orig}\nvault: Memoria\n---\nLuca's.\n"
    monkeypatch.setattr(web_fetch, "_fetch", lambda url, headers=None, *, types=(), cap=0:
                        (httpx.Response(200, text=copy), url))
    tc, tmp = client
    path = tc.post("/vault/save", json={"url": BASE + "borrowed"}).json()["path"]
    assert path == "Saved/@luca/Borrowed.md"
    assert f"source: {orig}" in (tmp / "vault" / path).read_text(encoding="utf-8")


def test_hosted_without_an_address_offers_no_page(client, monkeypatch):
    tc, _ = client
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.delenv("SILICA_PUBLIC_BASE", raising=False)
    assert tc.get("/public").json()["available"] is False
    assert tc.post("/public/publish", json={"vault": "memory"}).status_code == 400
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test")
    assert tc.get("/public").json()["available"] is True


def _vault_files():
    import json

    manifest = {"v": 1, "handle": "marta", "slug": "memory", "title": "Memoria", "url": BASE,
                "license": "CC BY 4.0", "count": {"notes": 3, "links": 1, "drawn": 2, "areas": 1},
                "areas": [["Spacing effect", 2]],
                "n": [[0, 0, 0, 1, "Spacing effect", "spacing-effect"], [1, 1, 0, 1, "Forgetting curve", "forgetting-curve"]],
                "e": [[0, 1]], "unlinked": [["Diary", "diary"]]}
    notes = {"spacing-effect": MD,
             "forgetting-curve": "---\ntitle: Forgetting curve\nvault: Memoria\n---\nEbbinghaus.\n",
             "diary": "---\ntitle: Diary\nvault: Memoria\n---\nAlone.\n"}
    return json.dumps(manifest), json.dumps(notes)


@pytest.fixture
def vault_fetched(monkeypatch):
    import httpx

    from silica.sources import web_fetch

    seen = []
    m, n = _vault_files()

    def fake(url, headers=None, *, types=(), cap=0):
        seen.append(url)
        body = m if url.endswith("vault.json") else n
        return httpx.Response(200, text=body, headers={"content-type": "application/json"}), url

    monkeypatch.setattr(web_fetch, "_fetch", fake)
    return seen


def test_a_vault_saves_the_areas_picked_as_one_revertible_run(client, vault_fetched):
    from silica.kernel.write.undo_journal import get_undo_journal

    tc, tmp = client
    p = tc.get("/vault/save/preview", params={"url": BASE, "areas": "0"}).json()
    assert p["kind"] == "vault" and p["notes"] == 2 and p["of"] == 3 and p["folder"] == "Saved/@marta/Memoria"
    assert vault_fetched == [BASE + "vault.json", BASE + "notes.json"]
    assert not (tmp / "vault" / "Saved").exists()
    r = tc.post("/vault/save", json={"url": BASE, "areas": [0]}).json()
    assert r["notes"] == 2 and r["folder"] == "Saved/@marta/Memoria"
    folder = tmp / "vault" / "Saved/@marta/Memoria"
    assert sorted(p.name for p in folder.iterdir()) == ["Forgetting curve.md", "Spacing effect.md"]
    text = (folder / "Spacing effect.md").read_text(encoding="utf-8")
    # a link to a note saved in the same write opens the copy; one to a note left out stays a web link
    assert "[[Saved/@marta/Memoria/Forgetting curve|Forgetting curve]]" in text
    assert f"[Testing effect]({BASE}testing-effect)" in text
    assert f"source: {PAGE}" in text and "author: '@marta'" in text and "form: clip" in text
    runs = get_undo_journal()._conn().execute("SELECT COUNT(*) FROM inverses WHERE run_id = ?", (r["run"],)).fetchone()[0]
    assert runs == 2


def test_a_later_save_adds_to_the_same_folder_and_leaves_what_is_there(client, vault_fetched):
    """Saving one area again used to write its notes a second time with no warning, in a dated
    folder (2026-11-06): a reader who saves in pieces now has one folder and no double."""
    tc, tmp = client
    tc.post("/vault/save", json={"url": BASE, "notes": ["forgetting-curve"]})
    folder = tmp / "vault" / "Saved/@marta/Memoria"
    kept = folder / "Forgetting curve.md"
    kept.write_text(kept.read_text(encoding="utf-8") + "\nMy own margin note.\n", encoding="utf-8")
    p = tc.get("/vault/save/preview", params={"url": BASE}).json()
    assert (p["notes"], p["already"], p["of"], p["folder"]) == (2, 1, 3, "Saved/@marta/Memoria")
    r = tc.post("/vault/save", json={"url": BASE}).json()
    assert r["notes"] == 2 and r["folder"] == "Saved/@marta/Memoria"
    assert sorted(f.name for f in folder.iterdir()) == ["Diary.md", "Forgetting curve.md", "Spacing effect.md"]
    assert "My own margin note." in kept.read_text(encoding="utf-8")
    # a link to the note that stayed opens that copy, as one saved in the same write would
    assert "[[Saved/@marta/Memoria/Forgetting curve|Forgetting curve]]" in (folder / "Spacing effect.md").read_text(encoding="utf-8")
    p = tc.get("/vault/save/preview", params={"url": BASE}).json()
    assert (p["notes"], p["already"]) == (0, 3)
    assert "saved already" in tc.post("/vault/save", json={"url": BASE}).json()["detail"]


def test_a_new_note_never_lands_on_a_file_the_folder_already_holds(client, vault_fetched):
    tc, tmp = client
    folder = tmp / "vault" / "Saved/@marta/Memoria"
    folder.mkdir(parents=True)
    (folder / "Diary.md").write_text("My own diary, no source.\n", encoding="utf-8")
    tc.post("/vault/save", json={"url": BASE, "notes": ["diary"]})
    assert (folder / "Diary.md").read_text(encoding="utf-8") == "My own diary, no source.\n"
    assert "Alone." in (folder / "Diary 2.md").read_text(encoding="utf-8")


def test_a_reader_saves_whole_areas_and_single_notes_in_one_address(client, vault_fetched):
    tc, tmp = client
    p = tc.get("/vault/save/preview", params={"url": BASE, "areas": "0", "notes": "diary"}).json()
    assert (p["notes"], p["of"]) == (3, 3)
    p = tc.get("/vault/save/preview", params={"url": BASE, "notes": "forgetting-curve,no-such-note"}).json()
    assert p["notes"] == 1
    r = tc.post("/vault/save", json={"url": BASE, "notes": ["diary"]}).json()
    assert r["notes"] == 1
    assert [f.name for f in (tmp / "vault" / "Saved/@marta/Memoria").iterdir()] == ["Diary.md"]


def test_the_area_a_page_sends_beside_notes_takes_nothing_so_an_old_silica_offers_nothing(client, vault_fetched):
    """A Silica before `notes` reads `areas` alone: -2 must show it no note, not the whole vault,
    and must not get in the way of one that reads both (amendment of 2026-11-06, Open 1)."""
    tc, _ = client
    assert tc.get("/vault/save/preview", params={"url": BASE, "areas": "-2"}).json()["notes"] == 0
    both = tc.get("/vault/save/preview", params={"url": BASE, "areas": "-2", "notes": "diary"}).json()
    assert both["notes"] == 1


def test_a_link_in_a_table_row_escapes_its_bar_and_one_in_prose_does_not():
    from silica.sources.saved_page import _MD_LINK, _wikilink

    def write(text):
        return _MD_LINK.sub(lambda m: _wikilink(m, "Saved/@a/W/Hub", m.group(1)), text)

    assert write("| [hub](https://x/y) | a |") == "| [[Saved/@a/W/Hub\\|hub]] | a |"
    assert write("  | a | [hub](https://x/y) |") == "  | a | [[Saved/@a/W/Hub\\|hub]] |"
    assert write("one\nsee [hub](https://x/y) | not a row") == "one\nsee [[Saved/@a/W/Hub|hub]] | not a row"


def test_a_vault_save_past_the_room_left_is_refused_naming_the_shortfall(client, vault_fetched, monkeypatch):
    tc, tmp = client
    monkeypatch.setenv("SILICA_VAULT_MAX_NOTES", "4")
    p = tc.get("/vault/save/preview", params={"url": BASE}).json()
    assert "notes over the 4" in p["room"]["short"]
    r = tc.post("/vault/save", json={"url": BASE})
    assert r.status_code == 400 and "no room" in r.json()["detail"]
    assert not (tmp / "vault" / "Saved").exists()


def _invite(tmp: Path, author="sara", slug="tesi", stamp="h1"):
    m, n = _vault_files()
    d = tmp / "invitations" / author / slug
    (d / "assets").mkdir(parents=True)
    (d / "vault.json").write_text(m.replace('"handle": "marta"', '"handle": "luca"'), encoding="utf-8")
    (d / "notes.json").write_text(n, encoding="utf-8")
    (d / "assets" / "1-h.png").write_bytes(b"png")
    (d / ".hash").write_text(stamp, encoding="utf-8")
    return d


def test_an_invitation_is_listed_saved_as_a_private_copy_and_then_gone(client, monkeypatch):
    tc, tmp = client
    monkeypatch.setenv("SILICA_INVITATIONS_DIR", str(tmp / "invitations"))
    monkeypatch.setenv("HOME", str(tmp / "home"))
    _invite(tmp)
    inv = tc.get("/vault/invitations").json()["invitations"]
    assert [(i["author"], i["vault"], i["title"], i["notes"]) for i in inv] == [("sara", "tesi", "Memoria", 3)]
    p = tc.get("/vault/invitations/preview", params={"author": "sara", "vault": "tesi"}).json()
    # the author is the folder the host wrote, not the manifest's claim
    assert p["author"] == "@sara" and p["folder"] == "Saved/@sara/Memoria" and p["notes"] == 3
    r = tc.post("/vault/invitations/save", json={"author": "sara", "vault": "tesi"}).json()
    folder = tmp / "vault" / r["folder"]
    text = (folder / "Spacing effect.md").read_text(encoding="utf-8")
    assert "shared: private" in text and "author: '@sara'" in text
    # a Private vault's addresses are never served: a link to a note not saved is its words
    assert "Testing effect" in text and "testing-effect" not in text
    assert (folder / "assets" / "1-h.png").read_bytes() == b"png"
    assert tc.get("/vault/invitations").json()["invitations"] == []
    # republished by the author: offered again
    (tmp / "invitations" / "sara" / "tesi" / ".hash").write_text("h2", encoding="utf-8")
    assert len(tc.get("/vault/invitations").json()["invitations"]) == 1
    assert tc.post("/vault/invitations/decline", json={"author": "sara", "vault": "tesi"}).json() == {"invitations": []}
    assert tc.post("/vault/invitations/save", json={"author": "../x", "vault": "tesi"}).status_code == 400


def test_a_saved_private_copy_has_no_globe(client, monkeypatch):
    tc, tmp = client
    monkeypatch.setenv("SILICA_INVITATIONS_DIR", str(tmp / "invitations"))
    monkeypatch.setenv("HOME", str(tmp / "home"))
    _invite(tmp)
    r = tc.post("/vault/invitations/save", json={"author": "sara", "vault": "tesi"}).json()
    (tmp / "vault" / "Saved").mkdir(exist_ok=True)
    tc.post("/public/vault", json={"folder": "Saved"})
    v = tc.get("/public").json()
    assert not any(p.startswith(r["folder"]) for p in v["notes"])
    assert tc.post("/public/note", json={"path": r["first"], "on": True}).status_code == 400


def test_the_upgrade_window_has_its_three_doors_and_a_page_reads_the_plan():
    """ADR-0140, amendment of 2026-11-06: the fourth wiki, Private and a month spent
    open one window, and its button goes to the landing's Plans section."""
    from tests.webassets import app_js

    js = app_js()
    assert js.count("upgradeOpen(") == 4 and "upgradeAtLimit(ev, said)" in js      # its definition, three doors
    assert 'ev.limit !== "month"' in js
    html = (Path(__file__).parents[1] / "silica/ui/web/static/index.html").read_text(encoding="utf-8")
    assert 'href="https://silica-agent.dev/#plans"' in html and ">Check it out<" in html


# --- the check before Publish (ADR-0153) ---------------------------------------

def _with_a_book(tmp_vault, tc, root: Path) -> None:
    from silica.kernel.write.provenance import append_record

    tmp_vault.note("sources/Book.md", '---\nsource_file: "/home/u/Book.pdf"\n---\nA chapter of a book.\n')
    append_record("Book.md", "x", "r1", ["Memory/Forgetting curve"], vault_path=str(root / "vault"))
    tc.post("/public/vault", json={"folder": "Memory"})


def test_the_check_reads_a_wiki_and_refuses_an_unknown_one(client, tmp_vault):
    tc, root = client
    tc.post("/public/vault", json={"folder": "Memory"})
    assert tc.get("/public/check", params={"vault": "memory"}).json()["ask"] is False
    assert tc.get("/public/check", params={"vault": "nope"}).status_code == 400


def test_an_answer_through_the_route_changes_what_goes_out(client, tmp_vault):
    tc, root = client
    _with_a_book(tmp_vault, tc, root)
    first = tc.get("/public/check", params={"vault": "memory"}).json()
    assert first["ask"] is True and first["count"]["notes"] == 1
    said = tc.post("/public/check/answer", json={"vault": "memory", "key": "Book.pdf", "answer": "mine"}).json()
    assert said["ask"] is False and said["count"]["notes"] == 2


def test_an_answer_for_what_this_wiki_does_not_show_is_refused(client, tmp_vault):
    tc, root = client
    _with_a_book(tmp_vault, tc, root)
    tc.get("/public/check", params={"vault": "memory"})
    r = tc.post("/public/check/answer", json={"vault": "memory", "key": "Other.pdf", "answer": "mine"})
    assert r.status_code == 400
    r = tc.post("/public/check/answer", json={"vault": "memory", "key": "Book.pdf", "answer": "maybe"})
    assert r.status_code == 400


def test_the_check_writes_only_from_this_origin(client, tmp_vault):
    tc, root = client
    _with_a_book(tmp_vault, tc, root)
    bad = {"Origin": "https://evil.test"}
    r = tc.post("/public/check/answer", json={"vault": "memory", "key": "Book.pdf", "answer": "mine"}, headers=bad)
    assert r.status_code == 403
    r = tc.post("/public/check/note", json={"vault": "memory", "path": "Memory/Forgetting curve.md", "on": True},
                headers=bad)
    assert r.status_code == 403


def test_a_note_is_marked_through_the_route_and_an_untouched_one_is_refused(client, tmp_vault):
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import _content_hash, get_undo_journal

    tc, root = client
    _with_a_book(tmp_vault, tc, root)
    note = "Memory/Forgetting curve.md"
    store = get_undo_journal()
    run = store.start_run(vault=str(root / "vault"))
    store.record(run, InverseOp(kind=InverseOpKind.delete_created, path=note),
                 _content_hash((root / "vault" / note).read_text(encoding="utf-8")))
    tc.get("/public/check", params={"vault": "memory"})
    r = tc.post("/public/check/note", json={"vault": "memory", "path": note, "on": True})
    assert r.status_code == 400 and "Not edited" in r.json()["detail"]
    tmp_vault.note(note, "Ebbinghaus, as I would put it.\n")
    tc.get("/public/check", params={"vault": "memory"})
    said = tc.post("/public/check/note", json={"vault": "memory", "path": note, "on": True}).json()
    assert said["count"]["notes"] == 2
    assert "rewritten: true" in (root / "vault" / note).read_text(encoding="utf-8")
    said = tc.post("/public/check/note", json={"vault": "memory", "path": note, "on": False}).json()
    assert said["count"]["notes"] == 1


def test_a_kept_image_is_served_by_its_name_and_nothing_else_is(client):
    tc, _ = client
    assert tc.get("/public/check/image", params={"name": "../vault.yaml"}).status_code == 404
    assert tc.get("/public/check/image", params={"name": "0" * 24 + ".png"}).status_code == 404


def test_a_note_outside_the_wiki_cannot_be_marked(client, tmp_vault):
    tc, root = client
    _with_a_book(tmp_vault, tc, root)
    tc.get("/public/check", params={"vault": "memory"})
    for path in ("Courses/Exam plan June.md", "Memory/Spacing effect.md", "../vault.yaml"):
        r = tc.post("/public/check/note", json={"vault": "memory", "path": path, "on": True})
        assert r.status_code == 400
        r = tc.post("/public/check/note", json={"vault": "memory", "path": path, "on": False})
        assert r.status_code == 400


# --- Save as a file (ADR-0157 point 5) ------------------------------------------

def test_save_as_a_file_downloads_the_zip(client):
    import io
    import zipfile
    from urllib.parse import quote

    from silica.kernel.recall.paths import silica_tmp_dir

    tc, _ = client
    tc.post("/public/vault", json={"folder": "Memory"})
    tc.post("/public/vault/memory", json={"title": "Memoria è"})
    r = tc.get("/public/vault/memory/file")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''" + quote("Memoria è by marta.zip")
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "index.html" in names and "memory/wiki-data.js" in names
    # the zip was streamed from its own folder, which goes once it is sent
    assert not list(silica_tmp_dir().glob("wiki-file-*"))
    # a file publishes nothing
    v = tc.get("/public").json()["vaults"][0]
    assert v["visibility"] == "off" and not v["published"]
    assert tc.get("/public/vault/nowhere/file").status_code == 400
    assert tc.get("/public/vault/memory/file", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_on_a_computer_the_file_lands_in_downloads(client, monkeypatch):
    import os
    import time
    import zipfile

    from silica.kernel.recall.paths import silica_tmp_dir
    from silica.ui.web import public_routes

    tc, tmp = client
    dl = tmp / "Downloads"
    dl.mkdir()
    monkeypatch.setattr(public_routes, "_downloads", lambda: dl)
    # a build killed by a signal leaves its folder; the next build clears what is past an hour
    old, fresh = silica_tmp_dir() / "wiki-file-old", silica_tmp_dir() / "wiki-file-fresh"
    old.mkdir()
    fresh.mkdir()
    hours_ago = time.time() - 7200
    os.utime(old, (hours_ago, hours_ago))
    tc.post("/public/vault", json={"folder": "Memory"})
    tc.post("/public/vault/memory", json={"title": "Memoria"})
    first = tc.post("/public/vault/memory/file").json()
    assert first == {"path": str(dl / "Memoria by marta.zip"), "name": "Memoria by marta.zip", "folder": "Downloads",
                     "notes": 2, "bytes": (dl / "Memoria by marta.zip").stat().st_size}
    assert "memory/wiki-data.js" in zipfile.ZipFile(first["path"]).namelist()
    # a second save never overwrites the first
    second = tc.post("/public/vault/memory/file").json()
    assert second["name"] == "Memoria by marta (2).zip" and Path(second["path"]).is_file()
    assert Path(first["path"]).stat().st_size == first["bytes"]
    assert sorted(p.name for p in silica_tmp_dir().glob("wiki-file-*")) == ["wiki-file-fresh"]
    v = tc.get("/public").json()["vaults"][0]
    assert v["visibility"] == "off" and not v["published"]
    # the line names the folder the file is in, as the person's desktop names it
    other = tmp / "Scaricati"
    other.mkdir()
    monkeypatch.setattr(public_routes, "_downloads", lambda: other)
    assert tc.post("/public/vault/memory/file").json()["folder"] == "Scaricati"
    monkeypatch.setattr(public_routes, "_downloads", lambda: dl)
    assert tc.post("/public/vault/nowhere/file").status_code == 400
    assert tc.post("/public/vault/memory/file", headers={"Origin": "https://evil.test"}).status_code == 403
    # the hosted service has no Downloads of the person's: its browser downloads the GET
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    assert tc.post("/public/vault/memory/file").status_code == 404
    assert tc.post("/public/reveal", json={"path": first["path"]}).status_code == 404
    assert tc.get("/public/vault/memory/file").headers["content-type"] == "application/zip"
    assert sorted(p.name for p in dl.iterdir()) == ["Memoria by marta (2).zip", "Memoria by marta.zip"]


def test_reveal_refuses_a_path_outside_downloads(client, monkeypatch):
    from silica.ui.web import public_routes

    tc, tmp = client
    dl = tmp / "Downloads"
    dl.mkdir()
    (dl / "a.zip").write_bytes(b"zip")
    (tmp / "elsewhere.zip").write_bytes(b"zip")
    (dl / "out").symlink_to(tmp)
    opened = []
    monkeypatch.setattr(public_routes, "_downloads", lambda: dl)
    monkeypatch.setattr(public_routes, "_open_folder", opened.append)
    # a path under a file names that file as its folder, and the opener would launch it
    for path in (tmp / "elsewhere.zip", dl / ".." / "elsewhere.zip", dl / "out" / "elsewhere.zip", dl, "a.zip", "",
                 dl / "a.zip" / "x", dl / "gone.zip"):
        assert tc.post("/public/reveal", json={"path": str(path)}).status_code == 400, path
    assert opened == []
    assert tc.post("/public/reveal", json={"path": str(dl / "a.zip")}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert tc.post("/public/reveal", json={"path": str(dl / "a.zip")}).status_code == 200
    assert opened == [dl.resolve()]


def test_downloads_is_the_desktop_s_answer_unless_that_is_the_home_folder(tmp_path, monkeypatch):
    """xdg-user-dir answers $HOME when no Downloads is set: saving there and saying
    "in Downloads" would be false, so the home folder is no answer."""
    from silica.ui.web import public_routes

    home, bin_ = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_.mkdir()
    xdg = bin_ / "xdg-user-dir"
    xdg.write_text('#!/bin/sh\necho "$ANSWER"\n', encoding="utf-8")
    xdg.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    monkeypatch.setenv("ANSWER", str(home))
    assert public_routes._downloads() == home / "Downloads" and (home / "Downloads").is_dir()
    (home / "Scaricati").mkdir()
    monkeypatch.setenv("ANSWER", str(home / "Scaricati"))
    assert public_routes._downloads() == home / "Scaricati"
    monkeypatch.setenv("PATH", "/nonexistent")
    assert public_routes._downloads() == home / "Downloads"


def test_a_file_leaves_out_what_publish_leaves_out_until_it_is_answered(client, tmp_vault, monkeypatch):
    """The check runs for a file as for a Publish (ADR-0157 point 5): a note made from a file nobody
    answered for stays out of both, and the answer brings it into both."""
    import zipfile

    from silica.ui.web import public_routes

    tc, root = client
    dl = root / "Downloads"
    dl.mkdir()
    monkeypatch.setattr(public_routes, "_downloads", lambda: dl)
    _with_a_book(tmp_vault, tc, root)
    check = tc.get("/public/check", params={"vault": "memory"}).json()
    held = tc.post("/public/vault/memory/file").json()
    assert check["ask"] is True and held["notes"] == check["count"]["notes"] == 1
    assert "memory/forgetting-curve.html" not in zipfile.ZipFile(held["path"]).namelist()
    assert tc.post("/public/publish", json={"vault": "memory"}).json()["result"]["notes"] == 1
    tc.post("/public/check/answer", json={"vault": "memory", "key": "Book.pdf", "answer": "mine"})
    out = tc.post("/public/vault/memory/file").json()
    assert out["notes"] == 2 and "memory/forgetting-curve.html" in zipfile.ZipFile(out["path"]).namelist()
    assert tc.post("/public/publish", json={"vault": "memory"}).json()["result"]["notes"] == 2
