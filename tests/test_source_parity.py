"""ADR-0120: the sources NotebookLM takes that Silica did not, read with
nothing installed on the host: PPTX from its own XML, DOCX in the limited
child on the server, images through the chat model where no OCR is, the
server's 16 MB part ceiling, DuckDB's memory there, and the recorders' audio."""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.sources import convert as conv
from tests.doc_factory import PNG_1X1, docx_bytes, pptx_bytes


@pytest.fixture
def model(monkeypatch):
    """The chat model, as the list of what it was sent; it answers a reading."""
    from silica.agent import vision

    sent: list[tuple[list[dict], int]] = []

    def ask(messages, max_tokens=700):
        sent.append((messages, max_tokens))
        return json.dumps({"kind": "photo", "description": "a whiteboard", "text": "dS >= 0"})

    monkeypatch.setattr(vision, "ask_chat_model", ask)
    return sent


@pytest.fixture
def no_mineru(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(conv.shutil, "which", lambda name: None if name == "mineru" else real(name))


def _put(name: str, data: bytes) -> str:
    (Path(CONFIG.vault_path) / name).write_bytes(data)
    return name


def _body(notes: list[str]) -> str:
    return "\n".join((Path(CONFIG.vault_path) / n).read_text(encoding="utf-8") for n in notes)


def _refuse_model(monkeypatch):
    from silica.agent import vision

    def ask(messages, max_tokens=700):
        raise RuntimeError("this model takes no images")

    monkeypatch.setattr(vision, "ask_chat_model", ask)


# --- PPTX ----------------------------------------------------------------------

def test_a_deck_reads_in_presentation_order_with_its_bullets_tables_and_notes(tmp_vault, monkeypatch):
    """Order is presentation.xml's, not the file names'; the slide number
    placeholder and a blank slide leave nothing; neither mineru nor an office
    suite runs (git does, for the vault's root, which is not the deck's)."""
    real = subprocess.run

    def run(cmd, *a, **k):
        if Path(str(cmd[0])).name in ("mineru", "soffice", "libreoffice"):
            pytest.fail(f"a deck needs no {cmd[0]}")
        return real(cmd, *a, **k)

    monkeypatch.setattr(conv.subprocess, "run", run)
    _put("deck.pptx", pptx_bytes([
        {"title": "First", "bullets": [(0, "heat flows"), (1, "from hot to cold")],
         "notes": "Say the second law here.", "footer": "Pag 99"},
        {"title": "Second", "text": ["A free box"], "table": [["T", "S"], ["300", "1|2"]]},
        {},
    ], swap_order=True))

    body = _body(conv.convert("deck.pptx"))

    assert body.index("## Second") < body.index("## First")
    assert "- heat flows\n  - from hot to cold" in body
    assert "Speaker notes:\n\nSay the second law here." in body
    assert "| T | S |\n|---|---|\n| 300 | 1\\|2 |" in body
    assert "A free box" in body
    assert "Pag 99" not in body and "## Slide 3" not in body


def test_a_title_drawn_as_a_text_box_heads_its_slide(tmp_vault):
    _put("free.pptx", pptx_bytes([{"text": ["the body of the slide"], "big": "Second law"}]))

    body = _body(conv.convert("free.pptx"))

    assert "## Second law" in body and "## Slide 1" not in body
    assert body.count("Second law") == 1


def test_a_slide_with_only_a_picture_is_read_by_the_chat_model(tmp_vault, model):
    _put("lecture.pptx", pptx_bytes([{"title": "Intro", "bullets": [(0, "x")]}, {"picture": 0.5}]))

    body = _body(conv.convert("lecture.pptx"))

    assert "> [slide 2, read by" in body and "dS >= 0" in body
    assert "![[lecture-image2.png]]" in body
    (messages, budget), = model
    assert budget == conv._PAGE_MAX_TOKENS
    assert messages[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (Path(CONFIG.vault_path) / "Inbox/Images/lecture-image2.png").is_file()


def test_a_picture_on_every_slide_and_a_tiny_one_are_left_out(tmp_vault, model):
    """A logo the template repeats, and an icon under 1% of the slide."""
    _put("brand.pptx", pptx_bytes([{"title": "A", "logo": True}, {"title": "B", "logo": True},
                                   {"title": "C", "picture": 0.001}]))

    body = _body(conv.convert("brand.pptx"))

    assert "![[" not in body and not model
    assert not (Path(CONFIG.vault_path) / "Inbox/Images").exists()


def test_a_deck_of_pictures_no_model_reads_is_refused(tmp_vault, monkeypatch):
    _refuse_model(monkeypatch)
    _put("photos.pptx", pptx_bytes([{"picture": 0.5}, {"picture": 0.6}]))

    with pytest.raises(ValueError, match="no text in photos.pptx"):
        conv.convert("photos.pptx")


def test_a_file_named_pptx_that_is_not_one_is_refused(tmp_vault):
    _put("fake.pptx", b"not a zip")

    with pytest.raises(ValueError, match="not a readable PPTX"):
        conv.convert("fake.pptx")


# --- DOCX ----------------------------------------------------------------------

def test_on_the_server_a_docx_is_read_in_the_limited_child(tmp_vault, monkeypatch):
    from silica.sources import pdf_extract

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setattr(pdf_extract, "docx_html_here", lambda p: pytest.fail("read in the tenant's process"))
    _put("essay.docx", docx_bytes([("h1", "Entropy"), ("p", "Heat flows from hot to cold.")]))

    body = _body(conv.convert("essay.docx"))

    assert "# Entropy" in body and "Heat flows from hot to cold." in body


def test_on_the_server_a_docx_part_over_16_mb_is_refused_before_mammoth_opens_it(tmp_vault, monkeypatch):
    from silica.sources import pdf_extract

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setattr(pdf_extract, "docx_html", lambda p: pytest.fail("mammoth was handed the file"))
    path = Path(CONFIG.vault_path) / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b" " * (17 * 1024 * 1024))

    with pytest.raises(ValueError, match="declares more than 16 MB"):
        conv.convert("bomb.docx")


def test_the_child_names_a_docx_it_cannot_read(tmp_path):
    from silica.sources import pdf_extract

    bad = tmp_path / "x.docx"
    bad.write_bytes(b"PK\x03\x04 truncated")

    with pytest.raises(pdf_extract.PdfUnreadable, match="too large or malformed"):
        pdf_extract.docx_html(bad)


# --- images without OCR ---------------------------------------------------------

def test_without_mineru_an_image_is_the_chat_models_reading(tmp_vault, model, no_mineru):
    _put("board.png", PNG_1X1)

    body = _body(conv.convert("board.png"))

    assert "source: image-reading" in body
    assert "![[board.png]]" in body and "dS >= 0" in body
    assert model[0][1] == conv._PAGE_MAX_TOKENS


def test_without_mineru_a_tiff_is_refused_with_the_resave(tmp_vault, no_mineru):
    _put("scan.tif", b"II*\x00")

    with pytest.raises(ValueError, match="re-save it as PNG or JPEG"):
        conv.convert("scan.tif")


def test_without_mineru_an_image_no_model_reads_is_named_for_the_student(tmp_vault, no_mineru, monkeypatch):
    from silica.sources.pdf_extract import PdfUnreadable

    _refuse_model(monkeypatch)
    _put("board.png", PNG_1X1)

    with pytest.raises(PdfUnreadable, match="This image could not be read"):
        conv.convert("board.png")


# --- the server's ceilings --------------------------------------------------------

def test_on_the_server_an_odf_part_over_16_mb_is_refused(tmp_vault, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    path = Path(CONFIG.vault_path) / "big.odt"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.xml", b" " * (17 * 1024 * 1024))

    with pytest.raises(ValueError, match="16 MB"):
        conv.convert("big.odt")
    assert conv._member_cap() == 16 * 1024 * 1024
    monkeypatch.delenv("SILICA_HOSTED")
    assert conv._member_cap() == 64 * 1024 * 1024


def test_on_the_server_an_rtf_past_the_ceiling_is_refused(tmp_vault, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    _put("long.rtf", b"{\\rtf1 " + b"x" * (16 * 1024 * 1024) + b"}")

    with pytest.raises(ValueError, match="over the 16 MB"):
        conv.convert("long.rtf")


def test_on_the_server_duckdb_keeps_to_256_mb_and_two_threads(tmp_path, monkeypatch):
    from silica.tools.tabular import _connect

    monkeypatch.setenv("SILICA_HOSTED", "1")
    con = _connect(tmp_path)
    try:
        assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
        limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    finally:
        con.close()
    assert limit.startswith("244.1") and "MiB" in limit  # 256 MB in DuckDB's binary units


# --- the recorders' audio -----------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_recorders_formats_open_through_the_demuxer_whitelist(tmp_path):
    """Each new extension is made by ffmpeg itself, then read back through
    `_FFMPEG_FORMATS`: a demuxer missing from the list refuses its file."""
    made = []
    for ext, codec in ((".aiff", []), (".au", []), (".3gp", ["-c:a", "aac"]),
                       (".amr", ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb"])):
        assert ext in conv.AUDIO_EXTS
        src = tmp_path / f"tone{ext}"
        made_it = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                                  "-i", "sine=duration=1", *codec, str(src)], capture_output=True)
        if made_it.returncode != 0:
            continue  # this build has no encoder for it; the decoder is what Silica needs
        (work := tmp_path / ext.strip(".")).mkdir()
        wav = conv._media_to_audio(src, work)
        assert wav.is_file() and wav.stat().st_size > 1000
        made.append(ext)
    assert {".aiff", ".au"} <= set(made)
