"""The speech-to-text config family.

`SILICA_ASR_*` and `SILICA_STT_*` used to be two independent families pointing
at the same OpenAI-compatible `/audio/transcriptions` shape — one for /convert's
media lane, one for the GUI dictation button. They are one family now. The ASR
spelling stays readable forever so an existing .env keeps working.
"""
from __future__ import annotations

import pytest

from silica.config import SilicaConfig

_KEYS = (
    "SILICA_STT_BASE_URL", "SILICA_ASR_BASE_URL",
    "SILICA_STT_MODEL", "SILICA_ASR_MODEL",
    "SILICA_STT_LANG", "SILICA_ASR_LANG",
    "SILICA_STT_PROVIDER", "SILICA_ASR_PROVIDER",
    "SILICA_STT_WHISPERCPP_BIN", "SILICA_ASR_WHISPERCPP_BIN",
    "SILICA_STT_WHISPERCPP_MODEL", "SILICA_ASR_WHISPERCPP_MODEL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """config.py loads ~/.silica/.env at import, so the developer's own pins
    reach default_factory unless they are cleared here."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("field,legacy,value", [
    ("stt_base_url", "SILICA_ASR_BASE_URL", "http://127.0.0.1:8080"),
    ("stt_model", "SILICA_ASR_MODEL", "large-v3"),
    ("stt_lang", "SILICA_ASR_LANG", "it"),
    ("stt_provider", "SILICA_ASR_PROVIDER", "whispercpp"),
    ("stt_whispercpp_bin", "SILICA_ASR_WHISPERCPP_BIN", "/usr/bin/whisper-cli"),
    ("stt_whispercpp_model", "SILICA_ASR_WHISPERCPP_MODEL", "models/ggml-base.bin"),
])
def test_legacy_asr_keys_still_configure_the_stt_family(clean_env, monkeypatch,
                                                        field, legacy, value):
    monkeypatch.setenv(legacy, value)
    assert getattr(SilicaConfig(), field) == value


def test_the_stt_spelling_wins_when_both_are_set(clean_env, monkeypatch):
    monkeypatch.setenv("SILICA_ASR_BASE_URL", "http://legacy:8080")
    monkeypatch.setenv("SILICA_STT_BASE_URL", "http://current:1236/v1")
    assert SilicaConfig().stt_base_url == "http://current:1236/v1"


def test_defaults_when_nothing_is_set(clean_env):
    cfg = SilicaConfig()
    assert cfg.stt_base_url == "http://localhost:1236/v1"
    assert cfg.stt_provider == "endpoint"
    assert cfg.stt_lang == "auto"


def test_auto_language_is_omitted_on_the_convert_lane(clean_env, monkeypatch, tmp_path):
    """The two lanes spell "let the server detect" differently: dictation sends
    language=auto, /convert omits the field. One config value, both behaviours
    preserved."""
    from silica.config import CONFIG
    import silica.sources.convert as conv

    seen: dict = {}

    class R:
        status_code, text = 200, "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi\n"

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        seen.update(data=dict(data or {}))
        return R()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(CONFIG, "stt_lang", "auto")
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    conv._asr_via_endpoint(wav)
    assert "language" not in seen["data"]

    monkeypatch.setattr(CONFIG, "stt_lang", "it")
    conv._asr_via_endpoint(wav)
    assert seen["data"]["language"] == "it"
