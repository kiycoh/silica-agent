"""Open a file (ADR-0157 point 7): a wiki file becomes an invitation any Silica can save,
read for its data and images and never for its pages. The zip comes from anyone."""
# ruff: noqa: F811  the marta fixture is imported, so every test that takes it reads as a redefinition
from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_public_site import marta  # noqa: F401


@pytest.fixture
def homes(marta, monkeypatch):
    """Every folder Open a file and an invitation touch, in the test's tmp dir: saving and
    declining write under Path.home(), which conftest does not move."""
    monkeypatch.setenv("HOME", str(marta / "home"))
    monkeypatch.setenv("SILICA_INVITATIONS_DIR", str(marta / "invitations"))
    monkeypatch.setenv("SILICA_FILE_INVITATIONS_DIR", str(marta / "file-invitations"))
    return marta


@pytest.fixture
def wiki_zip(homes) -> bytes:
    """Task 1's wiki file of marta's Memoria, as the bytes a reader would open."""
    from silica.ui.web import public_site

    path, _ = public_site.build_file("memory")
    try:
        return path.read_bytes()
    finally:
        shutil.rmtree(path.parent)


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _rezip(data: bytes, extra: dict[str, bytes] | None = None, drop=(), change: dict | None = None) -> bytes:
    """The wiki file with entries added, left out or rewritten, as a crafted zip would be."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            if info.filename in drop:
                continue
            blob = (change or {}).get(info.filename, src.read(info))
            z.writestr(info.filename, blob)
        for name, blob in (extra or {}).items():
            z.writestr(name, blob)
    return out.getvalue()


def _manifest(data: bytes, **over) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        m = json.loads(z.read("memory/vault.json"))
    m.update(over)
    return _rezip(data, change={"memory/vault.json": json.dumps(m).encode()})


def test_a_wiki_file_opens_as_an_invitation_from_a_file(wiki_zip, homes):
    from silica.sources import saved_page

    out = saved_page.open_file(wiki_zip)
    assert out == {"author": "marta", "vault": "memory", "title": "Memoria", "notes": 3}
    d = homes / "file-invitations" / "marta" / "memory"
    # its data and its images, never a page, a script or a .md
    assert _files(d) == [".from-file", ".hash", "assets/1-hypnogram.png", "notes.json", "vault.json"]
    assert (d / ".from-file").read_bytes() == b""
    assert (d / ".hash").read_text() == hashlib.sha256((d / "notes.json").read_bytes()).hexdigest()
    inv = saved_page.invitations()
    assert [(i["author"], i["vault"], i["title"], i["notes"], i["file"]) for i in inv] == [
        ("marta", "memory", "Memoria", 3, True)]
    p = saved_page.prepare_invitation("marta", "memory")
    assert p["author"] == "@marta" and p["folder"] == "Saved/@marta/Memoria" and p["notes"] == 3
    assert "files" not in p


def test_a_saved_file_lands_in_saved_and_its_invitation_goes(wiki_zip, homes):
    from silica.sources import saved_page

    saved_page.open_file(wiki_zip)
    r = saved_page.save_invitation("marta", "memory")
    folder = homes / "vault" / r["folder"]
    assert r["folder"] == "Saved/@marta/Memoria" and r["notes"] == 3
    assert "author: '@marta'" in (folder / "Spacing effect.md").read_text(encoding="utf-8")
    assert (folder / "assets" / "1-hypnogram.png").is_file()
    assert saved_page.invitations() == []
    assert not (homes / "file-invitations" / "marta" / "memory").exists()
    # No done mark: it is keyed author.slug with the file's hash, so the same file opened
    # again would be hidden from the person who just chose to open it.
    assert not (homes / "home" / ".silica" / "invitations-done").exists()
    saved_page.open_file(wiki_zip)
    assert [i["vault"] for i in saved_page.invitations()] == ["memory"]


def test_pages_scripts_and_what_is_not_an_image_are_never_written(wiki_zip, homes):
    from silica.sources import saved_page

    crafted = _rezip(wiki_zip, extra={
        "memory/assets/drawing.svg": b"<svg onload='alert(1)'/>",
        "memory/assets/page.html": b"<script>alert(1)</script>",
        "memory/assets/run.js": b"alert(1)",
        "memory/assets/notes.txt": b"x",
        "memory/assets/deeper/photo.png": b"png",
        "memory/assets/.hidden.png": b"png",
        "memory/evil.js": b"alert(1)",
        "memory/photo.png": b"png",
        "elsewhere/vault.json.png": b"png",
    })
    saved_page.open_file(crafted)
    assert _files(homes / "file-invitations") == [
        "marta/memory/.from-file", "marta/memory/.hash", "marta/memory/assets/1-hypnogram.png",
        "marta/memory/notes.json", "marta/memory/vault.json"]


def _symlink_zip(data: bytes) -> bytes:
    out = io.BytesIO(_rezip(data))
    with zipfile.ZipFile(out, "a") as z:
        link = zipfile.ZipInfo("memory/assets/2-link.png")
        link.external_attr = 0o120777 << 16  # S_IFLNK: unpacked by a naive tool, it points anywhere
        z.writestr(link, "/etc/passwd")
    return out.getvalue()


@pytest.mark.parametrize("craft, said", [
    (lambda d: _rezip(d, extra={"../evil.txt": b"x"}), "leaves its folder"),
    (lambda d: _rezip(d, extra={"memory/assets/../../../x.png": b"x"}), "leaves its folder"),
    (lambda d: _rezip(d, extra={"/etc/cron.d/x": b"x"}), "leaves its folder"),
    (lambda d: _rezip(d, extra={"memory\\assets\\x.png": b"x"}), "leaves its folder"),
    (_symlink_zip, "a link"),
    (lambda d: _manifest(d, handle="Marta!"), "author"),
    (lambda d: _manifest(d, handle=""), "author"),
    (lambda d: _manifest(d, handle=["marta"]), "author"),
    (lambda d: _manifest(d, slug="../memory"), "author"),
    (lambda d: _rezip(d, drop=("memory/vault.json",)), "not a wiki file"),
    (lambda d: _rezip(d, change={"memory/notes.json": b"not json"}), "could not be read"),
    (lambda d: _rezip(d, change={"memory/vault.json": b"[1, 2]"}), "could not be read"),
    (lambda d: _rezip(d, extra={"other/vault.json": b"{}"}), "not a wiki file"),
    (lambda d: b"PK\x03\x04 not really a zip", "not a zip"),
])
def test_a_crafted_file_is_refused_and_nothing_is_written(wiki_zip, homes, craft, said):
    from silica.sources import saved_page

    with pytest.raises(ValueError, match=said):
        saved_page.open_file(craft(wiki_zip))
    assert _files(homes / "file-invitations") == []
    assert saved_page.invitations() == []


def test_a_file_past_the_cap_is_refused_and_leaves_nothing(wiki_zip, homes, monkeypatch):
    from silica.sources import saved_page

    # a bomb: two megabytes of zeros that pack into a few kilobytes
    bomb = _rezip(wiki_zip, extra={"memory/assets/9-bomb.png": b"\x00" * 2_000_000})
    assert len(bomb) - len(wiki_zip) < 50_000
    monkeypatch.setattr(saved_page, "WIKI_FILE_MAX", 1_000_000)
    with pytest.raises(ValueError, match="1 MB"):
        saved_page.open_file(bomb)
    assert _files(homes / "file-invitations") == []
    # the upload itself, before a byte of it is unpacked
    monkeypatch.setattr(saved_page, "WIKI_FILE_MAX", len(wiki_zip) - 1)
    with pytest.raises(ValueError, match="MB"):
        saved_page.open_file(wiki_zip)
    assert _files(homes / "file-invitations") == []


def test_a_file_of_more_entries_than_a_wiki_holds_is_refused_before_it_is_parsed(wiki_zip, homes, monkeypatch):
    from silica.sources import saved_page

    monkeypatch.setattr(saved_page, "_ENTRIES_MAX", 10)
    monkeypatch.setattr("zipfile.ZipFile", lambda *a, **k: pytest.fail("parsed before the count"))
    with pytest.raises(ValueError, match="more than 10 entries"):
        saved_page.open_file(wiki_zip)
    assert _files(homes / "file-invitations") == []


def test_the_cap_is_one_number_for_the_file_built_and_the_file_opened():
    from silica.sources import saved_page
    from silica.ui.web import public_site

    assert saved_page.WIKI_FILE_MAX == public_site.WIKI_FILE_MAX == 100_000_000


def _relay(homes: Path, author="marta", slug="memory") -> Path:
    d = homes / "invitations" / author / slug
    d.mkdir(parents=True)
    (d / "vault.json").write_text(json.dumps({"title": "Memoria by the relay", "count": {"notes": 1}}))
    (d / "notes.json").write_text(json.dumps({"spacing-effect": "---\ntitle: Relay\n---\nfrom the relay\n"}))
    (d / ".hash").write_text("relay-1")
    return d


def test_a_relay_invitation_of_the_same_author_and_slug_wins(wiki_zip, homes):
    from silica.sources import saved_page

    _relay(homes)
    with pytest.raises(ValueError, match="already"):
        saved_page.open_file(wiki_zip)
    assert _files(homes / "file-invitations") == []
    assert [(i["title"], i["file"]) for i in saved_page.invitations()] == [("Memoria by the relay", False)]


def test_a_relay_invitation_that_arrives_after_the_file_is_the_one_offered(wiki_zip, homes):
    from silica.sources import saved_page

    saved_page.open_file(wiki_zip)
    _relay(homes)
    assert [(i["title"], i["file"]) for i in saved_page.invitations()] == [("Memoria by the relay", False)]
    assert saved_page.prepare_invitation("marta", "memory")["of"] == 0  # the relay's manifest, not the file's
    saved_page.decline_invitation("marta", "memory")
    # declining the relay's marks it done and leaves the file's folder, which still never shows
    assert (homes / "file-invitations" / "marta" / "memory" / "vault.json").is_file()
    assert saved_page.invitations() == []


def test_opening_the_same_file_twice_replaces_the_first_and_a_refused_one_leaves_it(wiki_zip, homes, monkeypatch):
    from silica.sources import saved_page

    saved_page.open_file(wiki_zip)
    with zipfile.ZipFile(io.BytesIO(wiki_zip)) as z:
        notes = json.loads(z.read("memory/notes.json"))
    notes.pop("borrowed")
    second = _rezip(wiki_zip, change={"memory/notes.json": json.dumps(notes).encode()},
                    drop=("memory/assets/1-hypnogram.png",))
    assert saved_page.open_file(second)["notes"] == 2
    d = homes / "file-invitations" / "marta" / "memory"
    assert set(json.loads((d / "notes.json").read_text())) == {"spacing-effect", "forgetting-curve"}
    assert not (d / "assets" / "1-hypnogram.png").exists()
    assert len(saved_page.invitations()) == 1
    # a third that is refused leaves the second as it was
    monkeypatch.setattr(saved_page, "WIKI_FILE_MAX", 2000)
    with pytest.raises(ValueError):
        saved_page.open_file(_rezip(wiki_zip, extra={"memory/assets/9-big.png": b"\x00" * 5000}))
    assert _files(homes / "file-invitations") == [
        "marta/memory/.from-file", "marta/memory/.hash", "marta/memory/notes.json", "marta/memory/vault.json"]


def test_an_open_killed_mid_write_is_swept_by_the_next_after_an_hour(wiki_zip, homes):
    import os
    import time

    from silica.sources import saved_page

    root = homes / "file-invitations"
    for name, age in ((".open-dead", 3700), (".open-live", 60)):
        (root / name / "assets").mkdir(parents=True)
        os.utime(root / name, (time.time() - age, time.time() - age))
    saved_page.open_file(wiki_zip)
    assert not (root / ".open-dead").exists() and (root / ".open-live").is_dir()


def test_declining_a_file_invitation_removes_its_folder(wiki_zip, homes):
    from silica.sources import saved_page

    saved_page.open_file(wiki_zip)
    saved_page.decline_invitation("marta", "memory")
    assert not (homes / "file-invitations" / "marta" / "memory").exists()
    assert saved_page.invitations() == []
    assert not (homes / "home" / ".silica" / "invitations-done").exists()
    with pytest.raises(ValueError):
        saved_page.decline_invitation("marta", "memory")


# --- the route and the screen ---------------------------------------------------

@pytest.fixture
def tc(homes, monkeypatch):
    from silica.ui.web import server

    for k in ("SILICA_HOSTED", "SILICA_VIEWS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    server._reset_session()
    return TestClient(server.app)


def test_the_route_takes_the_zip_as_the_body(tc, wiki_zip, homes, monkeypatch):
    from silica.sources import saved_page

    r = tc.post("/vault/invitations/file", content=wiki_zip, headers={"Content-Type": "application/zip"})
    assert r.status_code == 200
    d = r.json()
    assert d["opened"] == {"author": "marta", "vault": "memory", "title": "Memoria", "notes": 3}
    assert [(i["vault"], i["file"]) for i in d["invitations"]] == [("memory", True)]
    p = tc.get("/vault/invitations/preview", params={"author": "marta", "vault": "memory"}).json()
    assert p["folder"] == "Saved/@marta/Memoria"
    s = tc.post("/vault/invitations/save", json={"author": "marta", "vault": "memory"})
    assert s.status_code == 200 and s.json()["folder"] == "Saved/@marta/Memoria"
    assert tc.get("/vault/invitations").json()["invitations"] == []

    r = tc.post("/vault/invitations/file", content=b"not a zip")
    assert r.status_code == 400 and "zip" in r.json()["detail"]
    monkeypatch.setattr(saved_page, "WIKI_FILE_MAX", 1000)
    r = tc.post("/vault/invitations/file", content=wiki_zip)
    assert r.status_code == 413 and "100 MB" not in r.json()["detail"] and "MB" in r.json()["detail"]
    # without a length the body is counted as it arrives
    r = tc.post("/vault/invitations/file", content=iter([wiki_zip[:800], wiki_zip[800:]]))
    assert r.status_code == 413
    assert _files(homes / "file-invitations") == []
    r = tc.post("/vault/invitations/file", content=wiki_zip, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_screen_opens_a_file_from_shared_with_you_and_from_a_drop_on_my_wikis():
    from tests.webassets import app_js

    js = app_js()
    box = js[js.index("function invitesBox("):js.index("function savedStripEl(")]
    assert '"Open a file…"' in box and 'accept = ".zip' in box
    assert '" · from a file"' in box
    assert '"No invitations. A wiki someone sent you as a file opens here, or drop the zip on this view."' in box
    assert "Silica reads a file's notes and images, never its pages." in box
    # the box shows with no invitation too: its button is how a file comes in
    assert "box.hidden" not in box
    assert "from a file: the file says it is by ${p.author}, and Silica cannot check that" in js
    assert '$("#view-public").addEventListener("drop"' in js
    assert '"/vault/invitations/file"' in js
