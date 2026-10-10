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


def test_look_is_loaded_on_demand_not_offered_every_turn():
    from silica.agent.constraints import CHAT_CORE, loadable
    assert "silica_look" in loadable() and "silica_look" not in CHAT_CORE


def test_an_image_counts_as_a_figure_not_as_its_base64():
    from silica.agent.loop import _content_chars
    parts = [{"type": "text", "text": "cosa mostra?"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 300_000}}]
    assert _content_chars(parts) < 5_000
    assert _content_chars("ciao") == 4
