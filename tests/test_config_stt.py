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
    "SILICA_STT_LOCAL_MODEL", "SILICA_STT_BROWSER",
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
    # the browser lane can send audio to the vendor, so it is never on unasked
    assert cfg.stt_browser == "off"
    assert cfg.stt_local_model == ""


def test_auto_language_is_sent_on_the_convert_lane(clean_env, monkeypatch, tmp_path):
    """/convert used to omit the field for "auto", and whisper-server then
    transcribed as English (its default, server.cpp:113): an Italian lecture
    came back translated. Both lanes now send "auto" the way dictation does."""
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
    assert seen["data"]["language"] == "auto"

    monkeypatch.setattr(CONFIG, "stt_lang", "it")
    conv._asr_via_endpoint(wav)
    assert seen["data"]["language"] == "it"


def test_a_server_that_rejects_auto_is_asked_again_without_it(clean_env, monkeypatch, tmp_path):
    """whisper-server needs "auto" named, but OpenRouter answers 400 to it on
    every model tried (2026-10-13) and detects the language when the field is
    absent. A fixed code is never dropped: that 400 is a real error."""
    from silica.config import CONFIG
    import silica.sources.convert as conv

    calls: list[dict] = []

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, '{"text": "ciao"}'

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        calls.append(dict(data or {}))
        return R(400 if "language" in (data or {}) else 200)

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")

    monkeypatch.setattr(CONFIG, "stt_lang", "auto")
    assert conv._asr_via_endpoint(audio) == "ciao"
    assert calls[0]["language"] == "auto" and "language" not in calls[1]

    calls.clear()
    monkeypatch.setattr(CONFIG, "stt_lang", "it")
    with pytest.raises(ValueError, match="returned 400"):
        conv._asr_via_endpoint(audio)
    assert len(calls) == 1
