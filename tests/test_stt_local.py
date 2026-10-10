# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The in-process transcriber behind the dictation button (ADR-0049).

The model itself is never loaded here: `_load` is replaced, so what is pinned
is the seam: the WAV the browser sends becomes the array faster-whisper
expects, and the language setting is spelled the way it wants.
"""
from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from silica.agent import stt


def _wav(samples: list[int], rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()


class _Seg:
    def __init__(self, text):
        self.text = text


class _Model:
    def __init__(self):
        self.seen = {}

    def transcribe(self, audio, **kw):
        self.seen = {"audio": audio, **kw}
        return iter([_Seg(" il margine"), _Seg(" geometrico ")]), None


@pytest.fixture
def model(monkeypatch):
    m = _Model()
    monkeypatch.setattr(stt, "_load", lambda name: m)
    return m


def test_the_browser_clip_reaches_the_model_as_float_samples(model):
    text = stt.transcribe_local(_wav([0, 16384, -32768]), lang="it", model="small")
    assert text == "il margine geometrico"
    audio = model.seen["audio"]
    assert audio.dtype == np.float32
    assert audio.tolist() == [0.0, 0.5, -1.0]
    assert model.seen["language"] == "it"
    # silence is where whisper invents subtitles credits; the VAD drops it first
    assert model.seen["vad_filter"] is True


@pytest.mark.parametrize("lang", ["auto", "", "  "])
def test_auto_language_asks_the_model_to_detect(model, lang):
    stt.transcribe_local(_wav([0, 0]), lang=lang, model="small")
    assert model.seen["language"] is None


def test_a_clip_that_is_not_16k_mono_is_refused_rather_than_misread(model):
    with pytest.raises(ValueError, match="16 kHz mono"):
        stt.transcribe_local(_wav([0, 0], rate=44100), lang="it", model="small")
    with pytest.raises(ValueError, match="16 kHz mono"):
        stt.transcribe_local(_wav([0, 0, 0, 0], channels=2), lang="it", model="small")


def test_bytes_that_are_not_a_wav_are_refused(model):
    with pytest.raises(ValueError, match="not a WAV"):
        stt.transcribe_local(b"RIFFfake", lang="it", model="small")


def test_the_model_setting_falls_back_to_the_shipped_size():
    class Cfg:
        stt_local_model = ""

    assert stt.local_stt_model(Cfg()) == stt.LOCAL_STT_MODEL
    Cfg.stt_local_model = "large-v3-turbo"
    assert stt.local_stt_model(Cfg()) == "large-v3-turbo"


def test_speech_lang_prefers_the_fixed_dictation_language(monkeypatch):
    class Cfg:
        stt_lang = "it"
        vault_path = ""

    assert stt.speech_lang(Cfg()) == "it"


def test_speech_lang_maps_the_vault_language_to_a_bcp47_tag(monkeypatch):
    from silica.onboarding import checks

    class Cfg:
        stt_lang = "auto"
        vault_path = "/some/vault"

    monkeypatch.setattr(checks, "reply_language_for", lambda v: "Italian")
    assert stt.speech_lang(Cfg()) == "it"
    monkeypatch.setattr(checks, "reply_language_for", lambda v: "de")
    assert stt.speech_lang(Cfg()) == "de"
    # an unknown name is no tag at all: the browser's own default beats a guess
    monkeypatch.setattr(checks, "reply_language_for", lambda v: "klingon")
    assert stt.speech_lang(Cfg()) == ""
