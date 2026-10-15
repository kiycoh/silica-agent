"""A page without text, and text that is not letters (ADR-0087 points 3 and 4,
and the amendment of 2026-10-13: such a page is read by the chat model).

Every PDF here is built by tests/doc_factory.py: an image page is a gray
picture with no text layer under it, as a scan is, and a garbled page draws
real glyphs whose ToUnicode map sends them into the private use area.
"""
from __future__ import annotations

import json
import logging
import struct
import zlib
from pathlib import Path

import pytest

from tests.doc_factory import gray_lines, pdf_bytes

TEXT = "A page of real lecture text about entropy and heat engines."
READING = {"kind": "slide", "description": "A Carnot cycle drawn as a loop.",
           "text": "Carnot cycle\nQ1 - Q2 = W"}


@pytest.fixture
def pdfium_vault(tmp_vault, monkeypatch):
    from silica.config import CONFIG
    from silica.sources import pdf_extract

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    monkeypatch.setattr(CONFIG, "model", "openrouter/z-ai/glm-5.3-flash")
    monkeypatch.delenv("SILICA_PDF_VISION_PAGES", raising=False)
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    pdf_extract._CACHE.clear()
    return tmp_vault


def _reader(monkeypatch, reply: str | None = None, fail: bool = False) -> list:
    """Patch the chat model; returns the messages each call received."""
    from silica.agent import vision

    calls: list = []

    def ask(messages, max_tokens=700):
        calls.append(messages)
        if fail:
            raise RuntimeError("404 No endpoints found that support image input")
        return reply if reply is not None else json.dumps(READING)

    monkeypatch.setattr(vision, "ask_chat_model", ask)
    return calls


def _write(vault, name: str, data: bytes) -> Path:
    path = Path(vault.note(f"Inbox/{name}"))
    path.write_bytes(data)
    return path


def _note(rel: str) -> str:
    from silica.config import CONFIG

    return (Path(CONFIG.vault_path) / rel).read_text(encoding="utf-8")


def _png_header(png: bytes) -> tuple[int, int, int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    length, tag = struct.unpack(">I4s", png[8:16])
    assert (length, tag) == (13, b"IHDR")
    width, height, depth, color = struct.unpack(">IIBB", png[16:26])
    return width, height, depth, color


# --- the PNG the child writes -------------------------------------------------

def test_the_png_encoder_writes_a_png_a_decoder_reads():
    from silica.sources.pdf_extract import png_gray

    rows = b"\x00\x00\x80\xff" + b"\x00\x10\x20\x30"  # two rows of three pixels
    png = png_gray(rows, 3, 2)

    assert _png_header(png) == (3, 2, 8, 0)  # 8-bit grayscale
    chunks, at = {}, 8
    while at < len(png):
        length, tag = struct.unpack(">I4s", png[at:at + 8])
        body = png[at + 8:at + 8 + length]
        assert struct.unpack(">I", png[at + 8 + length:at + 12 + length])[0] == zlib.crc32(tag + body)
        chunks[tag] = body
        at += 12 + length
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    assert zlib.decompress(chunks[b"IDAT"]) == rows


def test_a_page_without_text_is_rendered_gray_and_at_most_1600_pixels(tmp_path):
    from silica.sources import pdf_extract

    f = tmp_path / "scan.pdf"
    f.write_bytes(pdf_bytes([TEXT, ""], images={1: gray_lines()}))

    data = pdf_extract.extract(str(f), vision_pages=60)

    assert data["no_text"] == [1] and list(data["images"]) == [1]
    width, height, depth, color = _png_header(data["images"][1])
    assert max(width, height) == pdf_extract.LONG_SIDE and (depth, color) == (8, 0)


# --- point 3 and the amendment: read, or named --------------------------------

def test_every_page_without_text_and_no_model_gets_point_3s_sentence(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    _reader(monkeypatch, fail=True)
    _write(pdfium_vault, "scan.pdf", pdf_bytes(["", ""], images={0: gray_lines(), 1: gray_lines()}))

    with pytest.raises(pdf_extract.PdfUnreadable) as caught:
        convert("Inbox/scan.pdf")

    assert str(caught.value).startswith(pdf_extract.NO_TEXT) and caught.value.outcome == "no_text"
    assert not (Path(pdfium_vault.note("Inbox/x")).parent / "scan.md").exists()


def test_on_the_server_the_sentence_names_no_tool(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    monkeypatch.setenv("SILICA_HOSTED", "1")
    _reader(monkeypatch, fail=True)
    _write(pdfium_vault, "scan.pdf", pdf_bytes(["", ""], images={0: gray_lines(), 1: gray_lines()}))

    with pytest.raises(pdf_extract.PdfUnreadable) as caught:
        convert("Inbox/scan.pdf")

    assert str(caught.value) == pdf_extract.NO_TEXT
    assert "mineru" not in str(caught.value).lower() and "pip" not in str(caught.value)


def test_a_mixed_pdf_names_the_page_not_read_in_the_note(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    _reader(monkeypatch, fail=True)
    src = _write(pdfium_vault, "mixed.pdf", pdf_bytes([TEXT, "", TEXT.replace("entropy", "work")],
                                                      images={1: gray_lines()}))

    body = _note(convert("Inbox/mixed.pdf")[0])

    marker = "> [page 2: no selectable text, not read]"
    assert body.index("about entropy") < body.index(marker) < body.index("about work")
    pages, unread = pdf_extract.report(src)
    assert pages == {"pages": 3, "pages_without_text": 1, "pages_read": 0}
    assert unread == "page 2 has no selectable text and was not read"


def test_a_reading_lands_at_its_page_under_the_models_label(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    calls = _reader(monkeypatch)
    src = _write(pdfium_vault, "mixed.pdf", pdf_bytes([TEXT, "", TEXT.replace("entropy", "work")],
                                                      images={1: gray_lines()}))

    body = _note(convert("Inbox/mixed.pdf")[0])

    label = "> [page 2, read by glm-5.3-flash] slide: A Carnot cycle drawn as a loop."
    assert body.index("about entropy") < body.index(label) < body.index("about work")
    assert "> Carnot cycle\n> Q1 - Q2 = W" in body
    assert "source: image-reading" not in body  # the rest is the document's own text
    (part,) = [p for p in calls[0][0]["content"] if p["type"] == "image_url"]
    assert part["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert pdf_extract.report(src) == ({"pages": 3, "pages_without_text": 1, "pages_read": 1}, "")


def test_a_pdf_read_wholly_by_the_model_says_so_in_its_provenance(pdfium_vault, monkeypatch):
    from silica.sources.convert import convert

    _reader(monkeypatch)
    _write(pdfium_vault, "deck.pdf", pdf_bytes(["", ""], images={0: gray_lines(), 1: gray_lines()}))

    body = _note(convert("Inbox/deck.pdf")[0])

    assert "source: image-reading" in body.split("\n---\n")[0]
    assert "[page 1, read by" in body and "[page 2, read by" in body


def test_a_reply_that_is_not_a_reading_leaves_the_page_named(pdfium_vault, monkeypatch):
    from silica.sources.convert import convert

    _reader(monkeypatch, reply="I cannot see any image here.")
    _write(pdfium_vault, "mixed.pdf", pdf_bytes([TEXT, ""], images={1: gray_lines()}))

    assert "> [page 2: no selectable text, not read]" in _note(convert("Inbox/mixed.pdf")[0])


def test_a_model_that_refuses_the_first_page_is_not_asked_the_rest(pdfium_vault, monkeypatch):
    from silica.sources.convert import convert

    calls = _reader(monkeypatch, fail=True)
    _write(pdfium_vault, "deck.pdf", pdf_bytes([TEXT, "", "", ""],
                                               images={1: gray_lines(), 2: gray_lines(), 3: gray_lines()}))

    body = _note(convert("Inbox/deck.pdf")[0])

    assert len(calls) == 1
    assert all(f"> [page {n}: no selectable text, not read]" in body for n in (2, 3, 4))


def test_the_page_cap_reads_the_first_pages_and_names_the_rest(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    monkeypatch.setenv("SILICA_PDF_VISION_PAGES", "2")
    calls = _reader(monkeypatch)
    src = _write(pdfium_vault, "deck.pdf", pdf_bytes(["", "", "", "", TEXT],
                                                     images={i: gray_lines() for i in range(4)}))

    body = _note(convert("Inbox/deck.pdf")[0])

    assert len(calls) == 2
    assert "[page 1, read by" in body and "[page 2, read by" in body
    assert pdf_extract.report(src) == ({"pages": 5, "pages_without_text": 4, "pages_read": 2},
                                       "pages 3 and 4 have no selectable text and were not read")


def test_zero_turns_the_reading_off(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    monkeypatch.setenv("SILICA_PDF_VISION_PAGES", "0")
    calls = _reader(monkeypatch)
    src = _write(pdfium_vault, "mixed.pdf", pdf_bytes([TEXT, ""], images={1: gray_lines()}))

    body = _note(convert("Inbox/mixed.pdf")[0])

    assert calls == [] and "> [page 2: no selectable text, not read]" in body
    assert pdf_extract.cached(src)["render"] == []


def test_the_image_budget_names_the_pages_it_cannot_carry(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    monkeypatch.setattr(pdf_extract, "_IMAGE_BYTES", 1)
    calls = _reader(monkeypatch)
    src = _write(pdfium_vault, "mixed.pdf", pdf_bytes([TEXT, ""], images={1: gray_lines()}))

    convert("Inbox/mixed.pdf")

    assert calls == [] and pdf_extract.report(src)[1] == "page 2 has no selectable text and was not read"


def test_a_blank_page_is_neither_read_nor_named(pdfium_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    calls = _reader(monkeypatch)
    src = _write(pdfium_vault, "blank.pdf", pdf_bytes([TEXT, ""]))

    body = _note(convert("Inbox/blank.pdf")[0])

    assert calls == [] and "page 2" not in body
    assert pdf_extract.report(src) == ({"pages": 2, "pages_without_text": 1, "pages_read": 0}, "")


def test_the_server_child_sends_the_rendered_page_back(tmp_path):
    from silica.sources import pdf_extract

    f = tmp_path / "mixed.pdf"
    f.write_bytes(pdf_bytes([TEXT, ""], images={1: gray_lines()}))

    data = pdf_extract._read_in_child(f, 60)

    assert data["render"] == [1] and data["images"][1][:4] == b"\x89PNG"
    assert data["images"][1] == pdf_extract.extract(str(f), vision_pages=60)["images"][1]


def test_a_picture_pdfium_will_not_draw_is_named_not_passed_over(tmp_path):
    """A 1 GiB image on a page without text: PDFium draws it as nothing in a
    tenth of a second (2026-10-13). White, but not blank: the page is not sent
    and the parent names it, and the text before it stands."""
    from silica.sources import pdf_extract

    side = 32768
    z = zlib.compressobj(9)
    stream = b"".join(z.compress(bytes(side * 1024)) for _ in range(side // 1024)) + z.flush()
    f = tmp_path / "bomb.pdf"
    f.write_bytes(pdf_bytes([TEXT, ""], images={1: (side, side, stream)}))

    data = pdf_extract._read_in_child(f, 60)

    assert data["pages"][0] == [[TEXT, 11.0]]
    assert data["render"] == [1] and data["images"] == {}


def test_a_child_cut_short_while_rendering_keeps_its_text():
    """The text line is written and flushed before any page is drawn: a kill
    in the render costs the pages, which are then named, not the document."""
    from silica.sources import pdf_extract

    text = {"pages": [[[TEXT, 11.0]], []], "toc": [], "meta": {}, "n_pages": 2, "hidden": {},
            "no_text": [1], "function_words": 0.3, "render": [1]}
    torn = bytearray(json.dumps(text).encode() + b'\n[1, "iVBORw0KGgoAAAANSUhE')

    data = pdf_extract._parse_child(torn)

    assert data["pages"][0] == [[TEXT, 11.0]] and data["images"] == {}
    with pytest.raises(pdf_extract.PdfUnreadable, match="too large or malformed"):
        pdf_extract._parse_child(bytearray(json.dumps(text).encode()[:40]))


def test_the_stdout_cap_holds_the_text_and_the_image_budget():
    from silica.sources import pdf_extract

    assert pdf_extract._OUT_BYTES >= pdf_extract._TEXT_OUT_BYTES + pdf_extract._IMAGE_BYTES * 4 // 3


# --- point 4: garbage ----------------------------------------------------------

def test_bad_characters_are_stripped_and_a_page_left_without_text_is_named(pdfium_vault, monkeypatch):
    from silica.sources.convert import convert

    _reader(monkeypatch, fail=True)
    _write(pdfium_vault, "fonts.pdf", pdf_bytes([TEXT, TEXT, TEXT, "Glyphs whose font maps to no letters."],
                                                garbled={3}))

    body = _note(convert("Inbox/fonts.pdf")[0])

    assert not any(0xE000 <= ord(ch) <= 0xF8FF for ch in body)
    assert "> [page 4: no selectable text, not read]" in body


@pytest.mark.parametrize("hosted", [False, True])
def test_garbage_on_the_median_page_refuses_the_document(pdfium_vault, monkeypatch, hosted):
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    if hosted:
        monkeypatch.setenv("SILICA_HOSTED", "1")
    _write(pdfium_vault, "fonts.pdf", pdf_bytes([TEXT, TEXT, TEXT], garbled={0, 1}))

    with pytest.raises(pdf_extract.PdfUnreadable) as caught:
        convert("Inbox/fonts.pdf")

    assert str(caught.value) == pdf_extract.GARBAGE and caught.value.outcome == "garbage"
    assert "fonts do not map to letters" in pdf_extract.GARBAGE


def test_few_function_words_only_warn(pdfium_vault, caplog):
    from silica.sources.convert import convert

    terse = "Entropia\nTermodinamica\nCarnot Kelvin Clausius\nRendimento massimo"
    _write(pdfium_vault, "slides.pdf", pdf_bytes([terse, terse, terse]))

    with caplog.at_level(logging.WARNING, logger="silica.sources.pdf_extract"):
        body = _note(convert("Inbox/slides.pdf")[0])

    assert "Termodinamica" in body
    assert any("function words" in r.getMessage() for r in caplog.records)


def test_an_image_part_does_not_count_as_its_base64_in_the_output_budget():
    """A 120 KB page PNG is 160k characters of data URL; read as text it left
    no window, and the reply was cut to the clamp's 1024 floor (2026-10-13:
    completion_tokens 1024 at max_tokens 2500 and at 6000)."""
    from silica.agent import llm

    data = "data:image/png;base64," + "A" * 160_000
    with_image = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data}}, {"type": "text", "text": "read it"}]}]
    text_only = [{"role": "user", "content": [{"type": "text", "text": "read it"}]}]
    chars = llm._request_chars(with_image)
    assert chars < 20_000  # an image is a few thousand tokens, not 80k characters of text
    assert chars > llm._request_chars(text_only)  # but it is not free either


def test_a_page_is_asked_for_all_its_words_with_room_to_answer(pdfium_vault, monkeypatch):
    """The figure prompt gave back a page's figure or table and dropped its
    prose (30 of 533 words, 2026-10-13), and 700 tokens cut a dense page short."""
    from silica.agent import vision
    from silica.kernel.recall import image_readings
    from silica.sources import convert as conv

    seen: list = []

    def ask(messages, max_tokens=700):
        seen.append(max_tokens)
        (text,) = [p["text"] for p in messages[0]["content"] if p["type"] == "text"]
        seen.append(text)
        return json.dumps(READING)

    monkeypatch.setattr(vision, "ask_chat_model", ask)
    _write(pdfium_vault, "mixed.pdf", pdf_bytes([TEXT, ""], images={1: gray_lines()}))
    conv.convert("Inbox/mixed.pdf")
    assert seen[0] == conv._PAGE_MAX_TOKENS > 700
    assert "every word" in seen[1] and seen[1] != image_readings._PROMPT.format(language="English")


# --- a page in a math font (amendment of 2026-10-14) --------------------------
# PDFium keeps a formula slide's words and loses its formulas: Greek letters and
# operators come out as private-use characters, or as ASCII with the layout gone.

FORMULA = "alpha = (K + lambda I)^-1 y, what the text layer kept of it"


def _deck() -> bytes:
    return pdf_bytes([TEXT, "Kernel ridge\n" + FORMULA], toc=[(1, "Kernel ridge", 2)], math={1})


def test_a_math_font_page_is_transcribed_in_place_of_its_text(pdfium_vault, monkeypatch):
    from silica.agent import vision
    from silica.kernel.recall import image_readings
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    prompts: list = []

    def ask(messages, max_tokens=700):
        prompts.extend(p["text"] for p in messages[0]["content"] if p["type"] == "text")
        return "```markdown\nKernel ridge\n\n$$\\hat\\alpha = (K + \\lambda I)^{-1} y$$\n```"

    monkeypatch.setattr(vision, "ask_chat_model", ask)
    src = _write(pdfium_vault, "deck.pdf", _deck())

    body = _note(convert("Inbox/deck.pdf")[0])

    assert prompts == [image_readings.FORMULA_PAGE_PROMPT]  # page 1 has no math font: not sent
    assert "about entropy" in body
    # The heading stays, since the note is cut at headings; the rest is the transcription.
    assert body.index("\n# Kernel ridge\n") < body.index("> [page 2, read by glm-5.3-flash] transcription")
    assert "> $$\\hat\\alpha = (K + \\lambda I)^{-1} y$$" in body and "```" not in body
    assert FORMULA not in body
    # ADR-0100's pages_read counts pages without text; a formula page is not one.
    assert pdf_extract.report(src) == ({"pages": 2, "pages_without_text": 0, "pages_read": 0}, "")


def test_a_math_font_page_the_model_does_not_read_keeps_its_text(pdfium_vault, monkeypatch):
    from silica.sources.convert import convert

    _reader(monkeypatch, fail=True)
    _write(pdfium_vault, "deck.pdf", _deck())

    body = _note(convert("Inbox/deck.pdf")[0])

    assert FORMULA in body and "not read" not in body and "read by" not in body


def test_pages_without_text_are_rendered_before_formula_pages(pdfium_vault, monkeypatch):
    """Under the cap a page without text goes first: it has nothing else to
    stand for it, and a formula page left out keeps its text."""
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    monkeypatch.setenv("SILICA_PDF_VISION_PAGES", "1")
    _reader(monkeypatch)
    src = _write(pdfium_vault, "deck.pdf", pdf_bytes(["Kernel ridge\n" + FORMULA, ""], math={0},
                                                     images={1: gray_lines()}))

    body = _note(convert("Inbox/deck.pdf")[0])

    assert pdf_extract.cached(src)["render"] == [1]
    assert FORMULA in body and "[page 2, read by" in body


def test_the_server_child_finds_the_math_font_page(tmp_path):
    from silica.sources import pdf_extract

    f = tmp_path / "deck.pdf"
    f.write_bytes(_deck())

    data = pdf_extract._read_in_child(f, 60)

    assert data["math"] == [1] and data["render"] == [1] and data["images"][1][:4] == b"\x89PNG"
