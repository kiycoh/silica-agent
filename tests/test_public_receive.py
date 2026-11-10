"""A hosted account receives a linked computer's wiki (ADR-0157 points 10 and 11): the notes go into
a folder of its vault and its own renderer publishes them. The package comes from a client."""
from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_public_site import PNG

LINK = "0123456789abcdef"
NOTES = {
    "Spacing effect.md": "Reviews spread over days. It follows from the [[Forgetting curve]].\n\n"
                         "![[hypnogram.png]]\n",
    "Forgetting curve.md": "Ebbinghaus measured it.\n",
    "Deep/Interleaving.md": "Mixing topics. Back to [[Spacing effect]].\n",
}
ANSWERS = {"Spacing effect.md": {"answer": "open", "license": "CC BY", "credit": "Ebbinghaus 1885"}}


def package(notes: dict | None = None, assets: dict | None = None, extra: dict | None = None,
            **wiki) -> bytes:
    """The zip a linked computer sends (Task 11): wiki.json, notes/ and assets/."""
    meta = {"link": "ffffffffffffffff", "folder": "Memoria", "slug": "memoria", "title": "Memoria",
            "line": "How remembering works.", "license": "CC BY 4.0", "visibility": "public",
            "answers": ANSWERS}
    meta.update(wiki)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("wiki.json", json.dumps(meta))
        for rel, text in (NOTES if notes is None else notes).items():
            z.writestr("notes/" + rel, text)
        for name, blob in ({"hypnogram.png": PNG} if assets is None else assets).items():
            z.writestr("assets/" + name, blob)
        for name, blob in (extra or {}).items():
            z.writestr(name, blob)
    return out.getvalue()


@pytest.fixture
def host(tmp_vault, tmp_path, monkeypatch):
    """A hosted account with its handle and address, a note of its own, and no embedder."""
    from silica.kernel.base.manifest import reset_manifest_cache
    from silica.ui.web import drop, server
    from silica.ui.web.session import SESSION

    for k in ("SILICA_HOST_DIR", "SILICA_VAULT_MAX_NOTES", "SILICA_VAULT_MAX_MB", "SILICA_DEMO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://wikis.test")
    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    monkeypatch.setattr(drop, "embed_imported", lambda folders: None)
    reset_manifest_cache()
    tmp_vault.note("Courses/Exam plan.md", "Mine alone.\n")
    SESSION.reset()
    yield TestClient(server.app), Path(tmp_path)
    reset_manifest_cache()


def send(tc, data: bytes, link: str | None = LINK):
    return tc.post("/public/receive", content=data, headers={} if link is None else {"X-Silica-Link": link})


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` with its bytes, Silica's own folders left out."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not any(s.startswith(".") for s in p.relative_to(root).parts)}


def _leftovers(tmp: Path) -> list[str]:
    return [p.name for p in tmp.iterdir() if p.name.startswith(".receive")]


def test_a_new_folder_is_written_marked_and_published_with_the_host_s_renderer(host):
    from silica.kernel import publish

    tc, tmp = host
    r = send(tc, package())
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"slug", "url", "notes", "published_at"}
    assert (out["slug"], out["url"], out["notes"]) == ("memoria", "https://wikis.test/@marta/memoria/", 3)
    vault = tmp / "vault"
    assert (vault / "Memoria" / "Deep" / "Interleaving.md").read_text(encoding="utf-8") == NOTES["Deep/Interleaving.md"]
    assert (vault / "Memoria" / "assets" / "hypnogram.png").read_bytes() == PNG
    # the gateway's link marks it, never the package's word
    row = publish.find("memoria")
    assert row["from_link"] == LINK and row["visibility"] == "public" and row["license"] == "CC BY 4.0"
    assert (row["title"], row["line"]) == ("Memoria", "How remembering works.")
    assert row["answers"] == ANSWERS
    live = tmp / "public" / "marta" / "live" / "memoria"
    page = (live / "spacing-effect.html").read_text(encoding="utf-8")
    # the index saw the new notes before the build: the link is a link, not words
    assert 'class="note-link"' in page and "forgetting-curve" in page
    # the computer's check let the image out, and the author's answer is the note's credit
    assert any(p.suffix == ".png" for p in (live / "assets").iterdir())
    assert "Ebbinghaus 1885" in page and "Image not published" not in page
    m = json.loads((live / "vault.json").read_text(encoding="utf-8"))
    assert m["from"] == "computer"
    # the review prints these as the computer's answers, its author's word (ADR-0157 point 11)
    assert {"answer": "open", "license": "CC BY", "credit": "Ebbinghaus 1885", "notes": 1,
            "by": "computer"} in m["rights"]["sources"]
    assert not list(live.parent.rglob("*.md.html"))
    assert _leftovers(tmp) == []
    v = tc.get("/public").json()["vaults"][0]
    assert v["computer"] is True and "from_link" not in v


def test_the_computer_s_readers_and_listing_request_are_the_row_s_and_the_review_still_decides(host):
    from silica.kernel import publish

    tc, tmp = host
    r = send(tc, package(readers=["sara", "marta", "luca"], listed=True))
    assert r.status_code == 200, r.text
    row = publish.find("memoria")
    # the account's own handle is no reader of its wiki, as for a hosted author's list
    assert (row["readers"], row["listed"]) == (["luca", "sara"], True)
    m = json.loads((tmp / "public" / "marta" / "live" / "memoria" / "vault.json").read_text(encoding="utf-8"))
    assert m["listed"] is True
    assert publish.host_reviews().get("memoria") is None  # asked, not in: the host's review reads it next
    # the computer owns the request: a package that does not carry it leaves the row's as it is
    assert send(tc, package(readers=[])).status_code == 200
    assert (publish.find("memoria")["readers"], publish.find("memoria")["listed"]) == ([], True)
    assert send(tc, package(listed=False)).status_code == 200
    assert publish.find("memoria")["listed"] is False


def test_a_wiki_the_computer_turned_off_leaves_its_address_and_keeps_its_notes(host):
    from silica.kernel import publish

    tc, tmp = host
    assert send(tc, package()).status_code == 200
    root = tmp / "public" / "marta"
    assert (root / "live" / "memoria" / "index.html").is_file()
    r = send(tc, package(notes={"Spacing effect.md": "Rewritten, then turned off.\n"}, assets={}, visibility="off"))
    assert r.status_code == 200, r.text
    assert r.json() == {"slug": "memoria", "url": "", "notes": 0, "published_at": ""}
    assert publish.find("memoria")["visibility"] == "off" and publish.find("memoria")["from_link"] == LINK
    assert not (root / "live" / "memoria").exists() and (root / "paused" / "memoria" / "index.html").is_file()
    assert (tmp / "vault" / "Memoria" / "Spacing effect.md").read_text(encoding="utf-8") == "Rewritten, then turned off.\n"
    # a later Publish from the computer puts it back on its address
    assert send(tc, package()).status_code == 200
    assert (root / "live" / "memoria" / "index.html").is_file()


def test_the_computer_s_own_folder_is_replaced_whole_and_the_journal_takes_it_back(host):
    from silica.kernel import publish
    from silica.kernel.write.gate import revert_run
    from silica.kernel.write.undo_journal import get_undo_journal

    tc, tmp = host
    vault = tmp / "vault"
    assert send(tc, package()).status_code == 200
    # an edit made here, and a title: the next Publish from the computer overwrites both
    (vault / "Memoria" / "Added here.md").write_text("typed on the host\n", encoding="utf-8")
    assert tc.post("/public/vault/memoria", json={"title": "Renamed here"}).status_code == 200
    assert publish.find("memoria")["from_link"] == LINK
    notes = {"Spacing effect.md": "Rewritten on the computer.\n", "New.md": "A new note.\n"}
    r = send(tc, package(notes=notes, assets={}, title="Memoria 2"))
    assert r.status_code == 200, r.text
    assert r.json()["notes"] == 2
    assert sorted(_tree(vault / "Memoria")) == ["New.md", "Spacing effect.md"]
    assert publish.find("memoria")["title"] == "Memoria 2"
    assert (vault / "Courses" / "Exam plan.md").read_text(encoding="utf-8") == "Mine alone.\n"
    assert not (tmp / "public" / "marta" / "live" / "memoria" / "forgetting-curve.html").exists()
    assert _leftovers(tmp) == []
    run = get_undo_journal().last_active_run(vault=str(vault))
    revert_run(run)
    # the notes come back, the old images do not: the journal holds notes, as for a save
    assert sorted(_tree(vault / "Memoria")) == ["Added here.md", "Deep/Interleaving.md",
                                                "Forgetting curve.md", "Spacing effect.md"]
    assert (vault / "Memoria" / "Spacing effect.md").read_text(encoding="utf-8") == NOTES["Spacing effect.md"]


@pytest.mark.parametrize("row", ["none", "unmarked"])
def test_a_folder_of_the_person_s_own_is_never_replaced(host, row):
    from silica.kernel import publish

    tc, tmp = host
    vault = tmp / "vault"
    (vault / "Memoria").mkdir()
    (vault / "Memoria" / "Mine.md").write_text("my own notes\n", encoding="utf-8")
    if row == "unmarked":
        publish.add_vault("Memoria")
    before = _tree(vault)  # vault.yaml with it, so its rows are compared too
    r = send(tc, package())
    assert r.status_code == 409
    assert r.json()["detail"] == ("A folder named Memoria already holds notes of yours: "
                                  "rename one of them to publish this wiki.")
    assert _tree(vault) == before and _leftovers(tmp) == []


def test_a_folder_another_link_of_this_account_sent_is_replaced_and_marked_by_the_last(host):
    """A link lasts 90 days: the same computer linked again has a new id, and its folder is still its own."""
    from silica.kernel import publish

    tc, tmp = host
    assert send(tc, package(), link="fedcba9876543210").status_code == 200
    assert publish.find("memoria")["from_link"] == "fedcba9876543210"
    r = send(tc, package(notes={"New.md": "After the new link.\n"}, answers={}, assets={}))
    assert r.status_code == 200, r.text
    assert sorted(_tree(tmp / "vault" / "Memoria")) == ["New.md"]
    assert publish.find("memoria")["from_link"] == LINK


@pytest.mark.parametrize("link", [None, "", "not-a-link", "0123456789ABCDEF"])
def test_without_the_gateway_s_link_nothing_is_received(host, link):
    tc, tmp = host
    before = _tree(tmp / "vault")
    r = send(tc, package(), link=link)
    assert r.status_code == 401
    assert _tree(tmp / "vault") == before


def test_the_route_is_the_hosted_service_s_alone(host, monkeypatch):
    tc, tmp = host
    monkeypatch.delenv("SILICA_HOSTED")
    assert send(tc, package()).status_code == 404
    assert not (tmp / "vault" / "Memoria").exists()


def test_a_wiki_past_100_mb_is_refused_as_sent_and_as_unpacked(host, monkeypatch):
    from silica.sources import saved_page

    tc, tmp = host
    assert saved_page.WIKI_FILE_MAX == 100_000_000
    monkeypatch.setattr(saved_page, "WIKI_FILE_MAX", 20_000)
    before = _tree(tmp / "vault")
    big = package(assets={"noise.png": os.urandom(30_000)})  # random: no zip shrinks it under the cap
    assert len(big) > 20_000
    r = send(tc, big)
    assert r.status_code == 413 and "MB" in r.json()["detail"]
    # small as sent, past the cap once unpacked: counted while it is written
    bomb = package(notes={"Spacing effect.md": "a" * 30_000})
    assert len(bomb) < 20_000
    r = send(tc, bomb)
    assert r.status_code == 400 and "MB" in r.json()["detail"]
    assert _tree(tmp / "vault") == before and _leftovers(tmp) == []


def test_past_the_tier_s_wikis_or_its_private_a_new_wiki_is_refused(host, monkeypatch):
    from silica.kernel import publish

    tc, tmp = host
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    r = send(tc, package(visibility="private"))
    assert r.status_code == 400 and "Private" in r.json()["detail"]
    for name in ("A", "B", "C"):
        (tmp / "vault" / name).mkdir()
        publish.add_vault(name)
    before = _tree(tmp / "vault")
    r = send(tc, package())
    assert r.status_code == 400 and r.json()["detail"] == "you can publish 3 wikis at most"
    assert _tree(tmp / "vault") == before and len(publish.vaults()) == 3


def test_past_the_account_s_room_nothing_is_written(host, monkeypatch):
    tc, tmp = host
    monkeypatch.setenv("SILICA_VAULT_MAX_NOTES", "3")
    r = send(tc, package())
    assert r.status_code == 400 and "notes over the 3" in r.json()["detail"]
    assert not (tmp / "vault" / "Memoria").exists() and _leftovers(tmp) == []


def _symlink_package() -> bytes:
    out = io.BytesIO(package())
    with zipfile.ZipFile(out, "a") as z:
        info = zipfile.ZipInfo("notes/Link.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    return out.getvalue()


@pytest.mark.parametrize("data, said", [
    (package(extra={"notes/../../evil.md": "x"}), "leaves its folder"),
    (package(extra={"/etc/evil.md": "x"}), "leaves its folder"),
    (package(extra={"notes\\evil.md": "x"}), "leaves its folder"),
    (_symlink_package(), "a link"),
    (package(extra={"notes/Page.html": "<script>alert(1)</script>"}), "a page"),
    (package(extra={"index.html": "<p>hi</p>"}), "a page"),
    (package(extra={"notes/run.sh": "rm -rf /"}), "neither a note nor an image"),
    (package(extra={"assets/x.svg": "<svg/>"}), "neither a note nor an image"),
    (package(extra={"notes/.hidden/x.md": "x"}), "neither a note nor an image"),
    (package(extra={"notes/A.md": "a", "notes/A.md/B.md": "b"}), "both as a file and as a folder"),
    (package(extra={"notes/" + "/".join(["x" * 250] * 20) + ".md": "x"}), "longer than a file system takes"),
    (package(extra={"notes/" + "y" * 300 + ".md": "x"}), "longer than a file system takes"),
    (package(folder=7), "wiki.json"),
    (package(answers=["a"]), "wiki.json"),
    (package(answers={"Spacing effect.md": {"answer": "stolen"}}), "wiki.json"),
    (package(answers={"notes/Spacing effect.md": ANSWERS["Spacing effect.md"]}), "a note it did not send"),
    (package(visibility="hidden"), "wiki.json"),
    (package(readers="sara"), "wiki.json"),
    (package(readers=["Not A Handle"]), "wiki.json"),
    (package(readers=["x" * 31]), "wiki.json"),
    (package(readers=[f"r{i}" for i in range(101)]), "wiki.json"),
    (package(listed="yes"), "wiki.json"),
    (package(listed=None), "wiki.json"),
    (package(title=None), "wiki.json"),
    (package(license="mine"), "license"),
    (package(folder="../Elsewhere"), "cannot be"),
    (package(folder="a/b"), "cannot be"),
    (package(folder=".silica"), "cannot be"),
    (package(folder="Inbox"), "cannot be"),
    (package(folder=""), "cannot be"),
    (b"not a zip", "zip"),
], ids=["slip", "absolute", "backslash", "symlink", "html note", "html page", "script", "svg",
        "dot folder", "file and folder", "long path", "long name", "folder type", "answers type", "answer word", "answer key", "visibility", "readers type", "reader word", "reader long", "readers many",
        "listed type", "listed null", "title type", "license",
        "folder slip", "folder path", "dot folder name", "silica folder", "no folder", "not a zip"])
def test_a_hostile_package_is_refused_with_a_sentence_and_nothing_written(host, data, said):
    tc, tmp = host
    before = _tree(tmp / "vault")
    r = send(tc, data)
    assert r.status_code == 400, r.text
    assert said in r.json()["detail"]
    assert _tree(tmp / "vault") == before and _leftovers(tmp) == []
    assert not (tmp / "public" / "marta" / "live").exists()


def test_a_package_without_wiki_json_or_with_a_note_that_is_not_text_is_refused(host):
    tc, tmp = host
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("notes/A.md", "a")
    r = send(tc, out.getvalue())
    assert r.status_code == 400 and "wiki.json" in r.json()["detail"]
    r = send(tc, package(notes={"A.md": b"\x00\xff binary"}, answers={}))
    assert r.status_code == 400 and "not text" in r.json()["detail"]
    assert not (tmp / "vault" / "Memoria").exists() and _leftovers(tmp) == []


def test_a_busy_account_asks_for_the_publish_again_and_never_reads_as_a_taken_folder(host, monkeypatch):
    from silica.ui.web.session import SESSION

    tc, tmp = host
    monkeypatch.setattr(SESSION, "busy", True)
    r = send(tc, package())
    assert r.status_code == 503 and "publish again" in r.json()["detail"]
    assert not (tmp / "vault" / "Memoria").exists()


def test_the_sweep_leaves_an_old_folder_a_failed_swap_could_not_put_back(host):
    """A `-old` folder is one the swap moved aside and could not move back: maybe the only copy."""
    import time

    tc, tmp = host
    kept, stale = tmp / ".receive-abc-old", tmp / ".receive-def"
    for d in (kept, stale):
        d.mkdir()
        (d / "Note.md").write_text("the only copy\n", encoding="utf-8")
        os.utime(d, (time.time() - 7200,) * 2)
    assert send(tc, package()).status_code == 200
    assert (kept / "Note.md").read_text(encoding="utf-8") == "the only copy\n"
    assert not stale.exists()


def test_a_computer_s_remote_image_goes_out_as_a_link_and_this_account_is_asked_nothing(host):
    from silica.kernel import rights
    from silica.ui.web.public_routes import _thumbnail

    tc, tmp = host
    url = "https://example.org/pic.png"
    # this account answered the site and holds a copy: a wiki of its own would copy the image in
    rights._keep(url, PNG, "image/png", tmp / "vault")
    rights.save_answer("site:example.org", "mine")
    notes = {**NOTES, "Spacing effect.md": NOTES["Spacing effect.md"] + f"\n![a figure]({url})\n"}
    assert send(tc, package(notes=notes)).status_code == 200
    live = tmp / "public" / "marta" / "live" / "memoria"
    page = (live / "spacing-effect.html").read_text(encoding="utf-8")
    assert f'href="{url}"' in page and "Image from example.org" in page
    assert "Image not published" not in page
    assert [p.name for p in (live / "assets").iterdir()] == ["1-hypnogram.png"]
    md = (live / "spacing-effect.md").read_text(encoding="utf-8")
    assert f"[Image from example.org]({url})" in md
    sheet = rights.sheet(rights.read_wiki("memoria"), rights.answers(), _thumbnail)
    assert not [g for g in sheet["groups"] if g["kind"] == "site"]


def test_a_swap_that_cannot_put_the_old_folder_back_says_where_it_waits(host, monkeypatch, caplog):
    import pathlib

    tc, tmp = host
    assert send(tc, package()).status_code == 200
    real = pathlib.Path.rename

    def rename(self, target):
        # the vault's folder moves aside; nothing moves back in from beside the vault
        if self.name.startswith(".receive-"):
            raise OSError(28, "No space left on device")
        return real(self, target)

    monkeypatch.setattr(pathlib.Path, "rename", rename)
    r = send(tc, package(notes={"New.md": "new\n"}, answers={}))
    assert r.status_code == 500 and "could not write" in r.json()["detail"]
    old = [p for p in tmp.iterdir() if p.name.endswith("-old")]
    assert len(old) == 1 and (old[0] / "Forgetting curve.md").is_file()
    assert "could not be put back" in caplog.text and old[0].name in caplog.text
