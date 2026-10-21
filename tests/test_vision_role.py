"""ADR-0077 amendment (2026-10-21): images go to a vision model of their own
when one is set, so the chat model may take text only."""
from __future__ import annotations

import types

QWEN = "openrouter/qwen/qwen3.7-flash-20260727"
V4 = "openrouter/deepseek/deepseek-v4-flash-0731"


def _cfg(**over):
    base = dict(provider="openrouter", model=V4, worker_provider="", worker_model="",
                worker_api_key=None, vision_provider="", vision_model="")
    base.update(over)
    return types.SimpleNamespace(**base)


def test_vision_role_falls_back_to_the_chat_model():
    from silica.agent.providers import get_provider

    assert get_provider(_cfg(), role="vision").model == get_provider(_cfg(), role="router").model


def test_a_vision_model_set_alone_rides_the_chat_provider():
    # Falling back to the router for want of a provider would send the pixels
    # to the text-only chat model this role exists to avoid.
    from silica.agent.providers import get_provider

    assert get_provider(_cfg(vision_model="qwen/qwen3.7-flash-20260727"), role="vision").model == QWEN


def test_config_prefixes_the_vision_model_with_the_chat_provider(monkeypatch):
    monkeypatch.setenv("SILICA_PROVIDER", "openrouter")
    monkeypatch.setenv("SILICA_VISION_MODEL", "qwen/qwen3.7-flash-20260727")
    monkeypatch.delenv("SILICA_VISION_PROVIDER", raising=False)
    from silica.config import SilicaConfig

    assert SilicaConfig().vision_model == QWEN


def test_separate_only_when_the_vision_model_is_another(monkeypatch):
    from silica import config as cfg_mod
    from silica.agent import vision

    monkeypatch.setattr(cfg_mod.CONFIG, "model", V4)
    monkeypatch.setattr(cfg_mod.CONFIG, "vision_model", None)
    assert not vision.separate()
    monkeypatch.setattr(cfg_mod.CONFIG, "vision_model", V4)
    assert not vision.separate()
    monkeypatch.setattr(cfg_mod.CONFIG, "vision_model", QWEN)
    assert vision.separate()


def test_an_attached_image_becomes_the_vision_models_words(tmp_vault, monkeypatch, tmp_path):
    from silica import config as cfg_mod
    from silica.agent import vision

    monkeypatch.setattr(cfg_mod.CONFIG, "vision_model", QWEN)
    sent = []

    def ask(messages, max_tokens=700):
        sent.append(messages)
        return '{"kind": "chart", "description": "Una curva che sale fino a 3.2.", "text": ""}'

    monkeypatch.setattr(vision, "ask_chat_model", ask)
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG fake")
    out = vision.read_attached([img], "a che valore arriva la curva?")
    assert sent[0][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out == f"\n\n[image {img}, read by qwen3.7-flash-20260727] chart: Una curva che sale fino a 3.2."


def test_a_failed_reading_keeps_the_question_and_points_to_look(tmp_vault, tmp_path):
    # conftest's offline ask_chat_model raises, as a refused endpoint would.
    from silica.agent import vision

    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG fake")
    assert vision.read_attached([img], "cosa mostra?") == f"\n\n[image {img}: not read; silica_look can look at it]"
