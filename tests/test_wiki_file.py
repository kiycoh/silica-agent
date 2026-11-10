"""A wiki file (ADR-0157 point 5): the wiki's pages, notes and data in one zip, built
without touching what is published."""
# ruff: noqa: F811  the marta fixture is imported, so every test that takes it reads as a redefinition
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from tests.test_public_site import marta  # noqa: F401


@pytest.fixture
def build_file(marta):
    """`public_site.build_file`, with each zip's folder removed after the test: the caller owns it.
    The folder sits under ~/.silica/tmp, which conftest's _isolate_silica_home moves into pytest's
    own tmp dirs, so even a failed test leaves nothing in the developer's home or /tmp."""
    from silica.kernel import public_site

    made: list[Path] = []

    def run(slug: str) -> tuple[Path, dict]:
        path, out = public_site.build_file(slug)
        made.append(path.parent)
        return path, out

    yield run
    for folder in made:
        shutil.rmtree(folder)


def _names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def _read(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def test_a_wiki_file_holds_pages_data_and_notes(build_file):
    path, out = build_file("memory")
    names = _names(path)
    assert {"index.html", "memory/index.html", "memory/vault.json", "memory/notes.json",
            "memory/wiki-data.js", "_s/pages.js", "_s/pages.css"} <= names
    for slug in ("spacing-effect", "forgetting-curve", "borrowed"):
        assert {f"memory/{slug}.html", f"memory/{slug}.md"} <= names
    assert "memory/working-memory.html" not in names and "memory/diary.html" not in names
    assert any(n.startswith("memory/assets/") for n in names)
    assert out["notes"] == 3 and out["bytes"] == path.stat().st_size
    # on disk below ~/.silica, never in /tmp, which is RAM on a machine with tmpfs
    from silica.kernel.base.home import silica_tmp_dir
    assert path.parent.parent == silica_tmp_dir() and list(path.parent.iterdir()) == [path]
    data = _read(path, "memory/wiki-data.js")
    prefix = "window.SILICA_WIKI="
    assert data.startswith(prefix) and data.endswith(";")
    wiki = json.loads(data[len(prefix):-1])
    assert set(wiki) == {"vault.json", "notes.json"}
    assert wiki["vault.json"] == json.loads(_read(path, "memory/vault.json"))
    assert wiki["notes.json"] == json.loads(_read(path, "memory/notes.json"))
    assert wiki["vault.json"]["handle"] == "marta" and wiki["vault.json"]["slug"] == "memory"
    # the room reads the data before pages.js runs, and every link stays inside the file
    note = _read(path, "memory/spacing-effect.html")
    assert note.index('src="wiki-data.js"') < note.index('src="../_s/pages.js"')
    assert 'href="forgetting-curve.html"' in note
    top = _read(path, "index.html")
    assert 'url=memory/index.html"' in top and 'href="memory/index.html"' in top
    # from disk the room asks for each note by its file's name
    room = 'data-room="vault.json" data-here="" data-vaults="../vaults.json" data-ext=".html"'
    assert room in _read(path, "memory/index.html")


def test_mermaid_goes_in_only_when_a_note_draws_a_diagram(build_file, tmp_vault):
    path, _ = build_file("memory")
    assert "_s/mermaid.min.js" not in _names(path)
    tmp_vault.note("Memory/Forgetting curve.md",
                   "Ebbinghaus measured it.\n\n```mermaid\nflowchart LR\n  A --> B\n```\n")
    path, _ = build_file("memory")
    assert "_s/mermaid.min.js" in _names(path)


def _published(marta: Path) -> dict[str, bytes]:
    root = marta / "public" / "marta"
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_an_off_or_public_wiki_saves_without_touching_its_publication(marta, build_file, monkeypatch):
    from silica.kernel.publish import find, set_visibility
    from silica.kernel.public_site import build

    # where a publication lives and moves between live/ and paused/: a computer's are the person's
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test/")

    set_visibility("memory", "off")
    before = (_published(marta), find("memory")["published"])
    _, off = build_file("memory")
    assert (_published(marta), find("memory")["published"]) == before
    assert not (marta / "public" / "marta").exists()

    set_visibility("memory", "public")
    build("memory")
    before = (_published(marta), find("memory")["published"])
    assert any(k.startswith("live/memory/") for k in before[0]) and "state.json" in before[0]
    _, public = build_file("memory")
    assert (_published(marta), find("memory")["published"]) == before

    set_visibility("memory", "off")  # its last publication waits in paused/
    before = (_published(marta), find("memory")["published"])
    assert any(k.startswith("paused/memory/") for k in before[0])
    _, paused = build_file("memory")
    assert (_published(marta), find("memory")["published"]) == before
    # the selection is the wiki's, whatever its visibility
    assert off["notes"] == public["notes"] == paused["notes"] == 3


def test_the_file_says_it_was_shared_as_a_file(build_file):
    path, _ = build_file("memory")
    css = _read(path, "_s/pages.css")
    assert ".extract { display: none; }" in css
    pages = [n for n in _names(path) if n.endswith(".html")]
    assert len(pages) == 5  # the top index, the wiki's page and its three notes
    for name in pages:
        page = _read(path, name)
        assert '<p class="extract">Extract the whole zip first, then open index.html.</p>' in page, name
        assert "/privacy" not in page and "/terms" not in page, name
        if name != "index.html":
            assert "A wiki @marta shared as a file." in page, name
            assert "chose to publish" not in page, name


OPEN_A_FILE = ("Using Silica? Keep it in your vault from Wikis → My wikis → Open a file, "
               "and choose the zip this wiki came in.")


def test_a_file_tells_a_silica_reader_to_open_it_and_a_published_wiki_keeps_its_save(marta, build_file):
    from silica.kernel.public_site import build

    path, _ = build_file("memory")
    page = _read(path, "memory/index.html")
    assert OPEN_A_FILE in page and "Save 3 notes to Silica" not in page
    build("memory")
    page = (marta / "public" / "marta" / "live" / "memory" / "index.html").read_text(encoding="utf-8")
    assert "Save 3 notes to Silica" in page and OPEN_A_FILE not in page


def test_the_file_name_is_title_by_handle(build_file):
    from silica.kernel.publish import update_vault

    _, out = build_file("memory")
    assert out["name"] == "Memoria by marta.zip"
    update_vault("memory", {"title": "Mind/Brain: notes?"})
    _, out = build_file("memory")
    assert out["name"] == "Mind Brain notes by marta.zip"


def test_a_wiki_past_the_cap_does_not_become_a_file(marta, monkeypatch):
    from silica.kernel.base.home import silica_tmp_dir
    from silica.kernel import public_site

    monkeypatch.setattr(public_site, "WIKI_FILE_MAX", 1000)
    with pytest.raises(ValueError, match="a wiki file may hold"):
        public_site.build_file("memory")
    assert not list(silica_tmp_dir().iterdir())  # nothing of the refused build stays behind


def test_a_wiki_file_stops_before_it_copies_the_image_that_passes_the_cap(marta, tmp_vault, monkeypatch):
    from silica.kernel.base.home import silica_tmp_dir
    from silica.kernel import public_site

    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600_000
    for name in ("album-1.png", "album-2.png"):
        (marta / "vault" / name).write_bytes(big)
    tmp_vault.note("Memory/Album.md", "Two photos.\n\n![[album-1.png]]\n\n![[album-2.png]]\n")
    read: list[int] = []
    strip = public_site.strip_location
    monkeypatch.setattr(public_site, "strip_location", lambda data: read.append(len(data)) or strip(data))
    monkeypatch.setattr(public_site, "WIKI_FILE_MAX", 1_000_000)
    with pytest.raises(ValueError, match="a wiki file may hold"):
        public_site.build_file("memory")
    # the first photo fits and is copied; the second would pass the cap, so it is never even read
    assert read.count(len(big)) == 1 and sum(read) <= 1_000_000
    assert not list(silica_tmp_dir().iterdir())
