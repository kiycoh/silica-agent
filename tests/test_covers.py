"""Covers for the Library (ADR-0163): a PDF's first page drawn small, an EPUB's
declared picture, kept in the index namespace, and served by the file routes'
own rules: by path on the laptop, through the note on the server."""
from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from silica.config import CONFIG
from silica.sources import covers, pdf_extract
from tests.doc_factory import pdf_bytes

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _png_size(data: bytes) -> tuple[int, int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, colour


def _epub(cover: bytes | None = None, *, epub3: bool = True, href: str = "images/cover.jpg",
          meta_by_href: bool = False) -> bytes:
    """A package that declares its cover the EPUB 3 way (`properties`) or the
    EPUB 2 way (`<meta name="cover">` naming an item)."""
    item = (f'<item id="cov" href="{href}" media-type="image/jpeg"'
            + (' properties="cover-image"' if epub3 else "") + "/>")
    meta = "" if epub3 else f'<meta name="cover" content="{href if meta_by_href else "cov"}"/>'
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title>{meta}</metadata>'
           f'<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>{item}</manifest>'
           '<spine><itemref idref="c1"/></spine></package>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", "<html><body><p>text</p></body></html>")
        if cover is not None:
            z.writestr(f"OEBPS/{href}", cover)
    return buf.getvalue()


@pytest.fixture
def lib(tmp_vault):
    vault = Path(CONFIG.vault_path)
    (vault / "lib").mkdir()
    return vault / "lib"


# --- what a cover is -----------------------------------------------------------

def test_a_pdfs_cover_is_its_first_page_in_colour_320_pixels_tall(lib):
    book = lib / "book.pdf"
    book.write_bytes(pdf_bytes(["The first page", "The second page"]))

    path, media = covers.cover(book)

    assert media == "image/png"
    width, height, colour = _png_size(path.read_bytes())
    assert height == pdf_extract.COVER_HEIGHT and 0 < width < height and colour == 2  # RGB
    assert path.is_relative_to(covers.covers_dir())


def test_a_cover_is_drawn_once_and_again_when_the_file_changes(lib, monkeypatch):
    book = lib / "book.pdf"
    book.write_bytes(pdf_bytes(["one"]))
    first, _ = covers.cover(book)

    monkeypatch.setattr(pdf_extract, "cover_png", lambda path: pytest.fail("drawn twice"))
    assert covers.cover(book)[0] == first

    book.write_bytes(pdf_bytes(["two", "pages now"]))
    monkeypatch.setattr(pdf_extract, "cover_png", lambda path: b"\x89PNG\r\n\x1a\nnew")
    second, _ = covers.cover(book)
    assert second != first and not first.exists() and second.read_bytes().endswith(b"new")


def test_a_file_pdfium_cannot_draw_has_no_cover_and_is_not_opened_again(lib, monkeypatch):
    fake = lib / "fake.pdf"
    fake.write_text("<html>not a pdf</html>", encoding="utf-8")
    assert covers.cover(fake) is None
    monkeypatch.setattr(pdf_extract, "cover_png", lambda path: pytest.fail("opened again"))
    assert covers.cover(fake) is None


@pytest.mark.parametrize("epub", [
    _epub(JPEG), _epub(JPEG, epub3=False), _epub(JPEG, epub3=False, meta_by_href=True),
])
def test_an_epubs_cover_is_the_picture_its_package_declares(lib, epub):
    book = lib / "book.epub"
    book.write_bytes(epub)
    path, media = covers.cover(book)
    assert media == "image/jpeg" and path.read_bytes() == JPEG


def test_a_png_cover_by_its_bytes_whatever_the_manifest_says(lib):
    book = lib / "book.epub"
    book.write_bytes(_epub(PNG))  # the manifest says image/jpeg
    assert covers.cover(book)[1] == "image/png"


@pytest.mark.parametrize("epub", [
    _epub(None),                                          # declares a member it lacks
    _epub(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", href="cover.svg"),
    _epub(b"\xff\xd8\xff" + b"\x00" * (covers.EPUB_COVER_BYTES + 1)),
    b"PK\x03\x04 not a zip",
])
def test_no_picture_an_svg_an_oversized_one_or_a_broken_book_is_no_cover(lib, epub):
    book = lib / "book.epub"
    book.write_bytes(epub)
    assert covers.cover(book) is None


def test_a_book_that_declares_no_cover_and_any_other_file_have_none(lib):
    (lib / "plain.epub").write_bytes(_epub(None).replace(b' properties="cover-image"', b""))
    (lib / "essay.docx").write_bytes(b"PK\x03\x04")
    assert covers.cover(lib / "plain.epub") is None and covers.cover(lib / "essay.docx") is None


# --- the routes keep the file routes' rules --------------------------------------

@pytest.fixture
def client(lib):
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    pdf_extract._CACHE.clear()
    SESSION.reset()
    (lib / "book.pdf").write_bytes(pdf_bytes(["A book"]))
    (lib / "book.md").write_text('---\nsource_file: "lib/book.pdf"\n---\n\nThe text.\n', encoding="utf-8")
    (lib / "loose.md").write_text("no raw\n", encoding="utf-8")
    return TestClient(server.app)


def test_a_rail_rows_cover_by_its_path_on_the_laptop_only(client, monkeypatch):
    r = client.get("/file/cover", params={"path": "lib/book.pdf"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    for path in ("lib/loose.md", "../outside.pdf", "lib/gone.pdf", ""):
        assert client.get("/file/cover", params={"path": path}).status_code == 404, path
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert client.get("/file/cover", params={"path": "lib/book.pdf"}).status_code == 404


def test_on_the_server_a_cover_is_reached_through_its_note_and_drawn_in_the_child(client, monkeypatch):
    """The real child: an uploaded PDF is never opened in the tenant's process
    (ADR-0087 point 2), its cover included."""
    monkeypatch.setenv("SILICA_HOSTED", "1")
    opened = []
    monkeypatch.setattr("pypdfium2.PdfDocument", lambda *a, **k: opened.append(a) or pytest.fail("in-process"))

    r = client.get("/raw/cover", params={"path": "lib/book.md"})

    assert r.status_code == 200 and _png_size(r.content)[1] == pdf_extract.COVER_HEIGHT and opened == []
    assert client.get("/raw/cover", params={"path": "lib/loose.md"}).status_code == 404
    assert client.get("/raw/cover", params={"path": "lib/book.pdf"}).status_code == 404  # a file is no note
