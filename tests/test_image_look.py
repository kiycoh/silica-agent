"""ADR-0077 point 3: the agent looks at the pixels when it asks."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_vault):
    import silica.config
    root = Path(silica.config.CONFIG.vault_path)
    (root / "Immagini").mkdir()
    (root / "Immagini" / "Pasted image 1.png").write_bytes(b"\x89PNG fake")
    return root


@pytest.fixture
def asked(monkeypatch):
    from silica.agent import vision
    calls = []

    def ask(messages, max_tokens=700):
        calls.append(messages)
        return "La curva arriva a 3.2."
    monkeypatch.setattr(vision, "ask_chat_model", ask)
    return calls


def test_look_sends_the_pixels_and_the_question(vault, asked):
    from silica.tools.atomic import silica_look
    out = silica_look("Pasted image 1.png", "a che valore arriva la curva?")
    assert out == "La curva arriva a 3.2."
    (parts,) = [m["content"] for m in asked[0]]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1] == {"type": "text", "text": "a che valore arriva la curva?"}


def test_look_takes_a_vault_path_too(vault, asked):
    from silica.tools.atomic import silica_look
    assert silica_look("Immagini/Pasted image 1.png", "cosa mostra?") == "La curva arriva a 3.2."


def test_look_never_opens_a_file_outside_the_vault_or_the_attachments(vault, asked, tmp_path):
    from silica.tools.atomic import silica_look
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG")
    out = silica_look(str(outside), "cosa mostra?")
    assert "not found" in out and not asked


# --- a page of a PDF (ADR-0099 amendment 2026-10-21) ---------------------------

@pytest.fixture
def lecture(vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.tools import atomic
    from tests.doc_factory import gray_lines, pdf_bytes

    pdf_extract._CACHE.clear()
    monkeypatch.setattr(atomic, "_pdf_looks", {})  # each test's turn starts uncounted
    (vault / "Inbox").mkdir(exist_ok=True)
    (vault / "Inbox" / "lecture.pdf").write_bytes(
        pdf_bytes(["Kernel ridge\nalpha = (K + lambda I)^-1 y", ""], images={1: gray_lines()}))
    return "Inbox/lecture.pdf"


def test_look_sends_the_page_of_a_pdf_it_is_asked_about(lecture, asked):
    from silica.tools.atomic import silica_look

    assert silica_look(lecture, "che formula c'è?", page=2) == "La curva arriva a 3.2."
    (parts,) = [m["content"] for m in asked[0]]
    assert parts[0] == {"type": "text", "text": "Page 2:"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert parts[2] == {"type": "text", "text": "che formula c'è?"}


def test_without_a_page_the_question_s_words_find_it(lecture, asked):
    """A turn named the right page 13 times in 23 (2026-10-21)."""
    from silica.tools.atomic import silica_look

    silica_look(lecture, "quale formula dà alpha nella kernel ridge?")

    (parts,) = [m["content"] for m in asked[0]]
    assert [p["text"] for p in parts if p["type"] == "text"] == [
        "Page 1:", "quale formula dà alpha nella kernel ridge?\n\nSay which page the answer is on."]


def test_look_asks_for_the_page_and_sends_nothing_past_the_last(lecture, asked):
    from silica.tools.atomic import silica_look

    assert "shares the question's words" in silica_look(lecture, "cosa mostra?")
    assert "past its last page" in silica_look(lecture, "cosa mostra?", page=3)
    assert not asked


def test_a_turn_looks_at_six_pdf_pages_at_most(lecture, asked):
    """26 pages looked at one after the other for a figure, 266k tokens (2026-10-21)."""
    from silica.agent import read_scope
    from silica.tools.atomic import silica_look

    token = read_scope.REQUESTS.set(("dov'è la figura?",))
    try:
        for _ in range(6):
            silica_look(lecture, "c'è la figura?", page=1)
        assert "looks at PDF pages already" in silica_look(lecture, "c'è la figura?", page=2)
        assert len(asked) == 6
        read_scope.REQUESTS.set(("dov'è la figura?", "e la formula?"))
        assert silica_look(lecture, "c'è la formula?", page=1) == "La curva arriva a 3.2."
    finally:
        read_scope.REQUESTS.reset(token)


def test_on_the_server_the_page_is_drawn_in_the_limited_child(lecture, asked, monkeypatch):
    from silica.sources import pdf_extract
    from silica.tools.atomic import silica_look

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setattr(pdf_extract, "extract", lambda *a, **k: pytest.fail("drawn in the tenant's process"))

    silica_look(lecture, "kernel ridge?")

    (parts,) = [m["content"] for m in asked[0]]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")


def test_look_is_loaded_on_demand_not_offered_every_turn():
    from silica.agent.constraints import CHAT_CORE, loadable
    assert "silica_look" in loadable() and "silica_look" not in CHAT_CORE


def test_an_image_counts_as_a_figure_not_as_its_base64():
    from silica.agent.loop import _content_chars
    parts = [{"type": "text", "text": "cosa mostra?"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 300_000}}]
    assert _content_chars(parts) < 5_000
    assert _content_chars("ciao") == 4
