"""Every file in the vault is a rail row on the laptop (ADR-0129, amended
2026-11-08): what Silica holds of it rides on the row, read off the passage
index's own stamps; the drawer opens it by its path; "OCR this" queues one
conversion whose note lands beside it. The server's rail stays its notes and
their raws."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from silica.config import CONFIG
from tests.doc_factory import pdf_bytes


@pytest.fixture
def client(tmp_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    pdf_extract._CACHE.clear()
    SESSION.reset()
    return TestClient(server.app)


def _library() -> Path:
    vault = Path(CONFIG.vault_path)
    lib = vault / "lib"
    (lib / "_foto").mkdir(parents=True)
    (lib / "book.pdf").write_bytes(pdf_bytes(["The Book of Enoch, first chapter, the watchers"]))
    (lib / "scan.pdf").write_bytes(pdf_bytes(["", ""]))
    (lib / "_foto" / "p1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (lib / "bible.epub").write_bytes(b"PK\x03\x04")
    (lib / "README.md").write_text("# Library\n\nThe books.\n", encoding="utf-8")
    (vault / ".hidden").mkdir()
    (vault / ".hidden" / "secret.pdf").write_bytes(pdf_bytes(["hidden"]))
    return vault


def _row(tree: str, rel: str) -> str:
    m = re.search(rf'<a class="tree-file"[^>]*data-file="{re.escape(rel)}".*?</a>', tree)
    assert m, f"no row for {rel}"
    return m.group(0)


def test_every_file_is_a_row_with_what_silica_holds_of_it(client):
    from silica.kernel.recall.engine import engine

    _library()
    before = client.get("/vault_info").json()["tree"]
    # the index has not read the PDF yet: neither read nor not read
    assert 'data-state="unindexed"' in _row(before, "lib/book.pdf") and "not indexed" in _row(before, "lib/book.pdf")

    engine().build_index(rebuild=True)
    tree = client.get("/vault_info").json()["tree"]

    book, scan, photo, epub = (_row(tree, r) for r in ("lib/book.pdf", "lib/scan.pdf", "lib/_foto/p1.png", "lib/bible.epub"))
    assert 'data-state="read"' in book and "tree-mark" not in book and "data-act" not in book
    assert 'data-state="unread"' in scan and 'data-act="ocr"' in scan and ">not read<" in scan
    assert 'data-state="unread"' in photo and 'data-act="ocr"' in photo
    assert 'data-state="unread"' in epub and 'data-act="convert"' in epub
    assert 'href="/file?path=lib%2Fbook.pdf"' in book
    # a dot-folder is Silica's or the system's, never a row
    assert "secret.pdf" not in tree
    # the folder's number counts every row; its one note, which delete removes, rides apart
    assert '<summary draggable="true">lib<span class="tree-n" data-notes="1">5</span>' in tree


def test_the_server_rail_lists_no_file_by_its_path(client, monkeypatch):
    _library()
    monkeypatch.setenv("SILICA_HOSTED", "1")

    tree = client.get("/vault_info").json()["tree"]

    assert "data-file=" not in tree and "README" in tree


def test_a_file_opens_by_its_path_a_pdf_and_a_picture_inline(client):
    _library()

    pdf = client.get("/file", params={"path": "lib/book.pdf"})
    photo = client.get("/file", params={"path": "lib/_foto/p1.png"})
    epub = client.get("/file", params={"path": "lib/bible.epub"})
    asked = client.get("/file", params={"path": "lib/book.pdf", "download": 1})

    assert pdf.status_code == 200 and pdf.headers["content-disposition"].startswith("inline")
    assert "sandbox" in pdf.headers["content-security-policy"]
    assert photo.headers["content-disposition"].startswith("inline")
    assert epub.headers["content-disposition"].startswith("attachment")
    assert asked.headers["content-disposition"].startswith("attachment")


def test_a_file_outside_the_vault_under_a_dot_folder_or_on_the_server_is_not_found(client, monkeypatch, tmp_path):
    _library()
    (tmp_path / "outside.pdf").write_bytes(pdf_bytes(["x"]))

    for path in ("../outside.pdf", str(tmp_path / "outside.pdf"), ".hidden/secret.pdf", "lib", "lib/gone.pdf", ""):
        assert client.get("/file", params={"path": path}).status_code == 404, path
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert client.get("/file", params={"path": "lib/book.pdf"}).status_code == 404
    assert client.post("/file/ocr", json={"path": "lib/scan.pdf"}).status_code == 404


def test_ocr_this_converts_the_file_into_a_note_beside_it(client):
    from silica.ui.web import jobs

    vault = _library()

    r = client.post("/file/ocr", json={"path": "lib/book.pdf"})
    assert r.status_code == 200 and r.json()["source"]["path"] == "lib/book.pdf"
    assert jobs.drain()

    assert jobs.read(r.json()["id"])["state"] == "done"
    # beside the file, as an import's, and not in the inbox: the row is then a raw of its note
    assert (vault / "lib" / "book.md").is_file()
    assert not (vault / "Inbox" / "book.md").exists()
    assert client.post("/file/ocr", json={"path": "../x.pdf"}).status_code == 404


def test_a_pdfs_chapters_come_from_its_outline_and_not_on_the_server(client, monkeypatch):
    vault = Path(CONFIG.vault_path)
    (vault / "lib").mkdir()
    (vault / "lib/book.pdf").write_bytes(pdf_bytes([f"page {i}" for i in range(1, 31)],
                                                   toc=[(1, "Genesis", 1), (1, "Exodus", 15)]))

    got = client.get("/file/chapters", params={"path": "lib/book.pdf"}).json()

    assert got["source"] == "outline" and [s["title"] for s in got["steps"]] == ["Genesis", "Exodus"]
    assert client.get("/file/chapters", params={"path": "../x.pdf"}).status_code == 404
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert client.get("/file/chapters", params={"path": "lib/book.pdf"}).status_code == 404


def test_studying_a_pdf_writes_its_chapters_beside_it_and_memorize_plans_over_them(client):
    """ADR-0156 point 7: the ladder's steps as notes beside the book, ADR-0129's
    keys on each, the book then a raw of its notes, and Memorize's folder scope
    finding them as material with items built from them."""
    import yaml

    from silica.kernel.learn import pick, plans
    from silica.kernel.recall.engine import engine

    vault = Path(CONFIG.vault_path)
    (vault / "lib").mkdir()
    genesis = [f"In the beginning the watchers kept the gate number {i}. The scribe wrote it down." for i in range(10)]
    exodus = [f"The tablets were carried across the river on day {i}. The elders counted them." for i in range(10)]
    (vault / "lib/book.pdf").write_bytes(pdf_bytes(genesis + exodus, toc=[(1, "Genesis", 1), (1, "Exodus", 11)]))
    (vault / "lib/scan.pdf").write_bytes(pdf_bytes(["", ""]))
    engine().build_index(rebuild=True)

    r = client.post("/file/study", json={"path": "lib/book.pdf"})

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["folder"] == "lib/book" and out["parent"] == "lib/book.md" and len(out["parts"]) == 2
    first = (vault / out["parts"][0]).read_text(encoding="utf-8")
    fm = yaml.safe_load(first.split("---\n")[1])
    assert fm["source_file"] == "lib/book.pdf" and (fm["source_page"], fm["source_page_end"]) == (1, 10)
    assert fm["segment_title"] == "Genesis" and "watchers kept the gate number 3" in first
    assert yaml.safe_load((vault / "lib/book.md").read_text(encoding="utf-8").split("---\n")[1])["document"] is True
    # the book is now the raw of its notes, and no file row of its own
    tree = client.get("/vault_info").json()["tree"]
    assert 'data-raw="lib/book.pdf"' in tree and 'data-file="lib/book.pdf"' not in tree
    scope = plans.resolve_scope("folder", "lib/book/")
    assert len(scope["paths"]) == 2
    assert pick.session(4, scope=scope["paths"])["items"]
    # studied once: the folder answers, nothing is written twice
    again = client.post("/file/study", json={"path": "lib/book.pdf"}).json()
    assert again["written"] == 0 and len(again["parts"]) == 2
    assert client.post("/file/study", json={"path": "lib/scan.pdf"}).status_code == 400
