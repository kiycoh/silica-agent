# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""In-process speech-to-text, the dictation button's fallback (ADR-0049).

The served transcriber (`config.stt_base_url`, a whisper-server on :1236 by
default) is something almost no install runs, so until this module the web
GUI's microphone was a button that opened an explanation of how to start one.
An optional extra found with find_spec, weights fetched on first use, the
served endpoint still preferred whenever it answers: the shape the reranker had
before ADR-0073 moved it into a server of its own.
"""
from __future__ import annotations

import importlib.util
import io
import logging
import threading
import wave
from typing import Any

logger = logging.getLogger(__name__)

# `small` and not `base`: Italian WER on MLS is 31.1% on base, 21.4% on small,
# 16.0% on medium (Radford et al. 2022, table 10, in docs/research/papers/
# 09-document-ai). Medium is three times the download for the last 5 points,
# and a learner reviews every transcript before it is graded.
LOCAL_STT_MODEL = "small"


def has_local_stt() -> bool:
    """Whether the [stt] extra is installed. find_spec, not import: the probe
    runs on every microphone click and faster-whisper pulls ctranslate2 and
    onnxruntime in with it."""
    return importlib.util.find_spec("faster_whisper") is not None


def local_stt_model(config: Any) -> str:
    """A faster-whisper size ("small", "large-v3-turbo"), a HuggingFace repo id,
    or a local CTranslate2 directory. One place, so the doctor names what runs."""
    return (getattr(config, "stt_local_model", "") or "").strip() or LOCAL_STT_MODEL


# One lock for load and download both: the warm-up started by the status probe
# and the transcription that follows it a few seconds later would otherwise
# each fetch the same 480 MB.
_lock = threading.Lock()
_models: dict[str, Any] = {}


def _load(model: str) -> Any:
    with _lock:
        if model not in _models:
            from faster_whisper import WhisperModel

            # CPU and not "auto": ctranslate2 picks CUDA whenever a device is
            # present, and then needs cuDNN 9 and cuBLAS beside it, which
            # `pip install faster-whisper` does not install (its README); the
            # default local stack also already fills a 12 GB GPU.
            # ponytail: CPU only. Measured 2026-09-27 on a 32-thread laptop:
            # 5.5 s for a 30 s Italian clip on CPU, 1.2 s on its RTX 4080
            # (cuDNN there came with torch from [rerank]), 25 s for 170 s on
            # CPU, 17 s to fetch and load once. Add SILICA_STT_LOCAL_DEVICE
            # when a 30 s clip takes over 10 s on someone's CPU.
            _models[model] = WhisperModel(model, device="cpu", compute_type="int8")
        return _models[model]


def loaded(model: str) -> bool:
    return model in _models


def warm(model: str) -> None:
    """Load (and on first use download) the model in the background, so it is
    ready by the time the speaker stops. Called by the status probe, which the
    browser sends on the click that opens the microphone."""
    if model in _models:
        return
    threading.Thread(target=_warm_quietly, args=(model,), daemon=True, name="stt-warm").start()


def _warm_quietly(model: str) -> None:
    try:
        _load(model)
    except Exception as exc:
        # Tolerated: the transcription that follows calls _load again, and that
        # failure reaches the person holding the microphone. Raising here would
        # die in a daemon thread nobody reads.
        logger.warning("stt: warming %s failed (%s)", model, exc)


def _samples(wav: bytes):
    """16 kHz mono PCM16 WAV to the float32 array faster-whisper takes as-is.

    Parsed here and not handed to faster-whisper as a file: that path decodes
    through PyAV, which resamples anything silently, and the browser's
    converter (app-boot.js toWav16k) is the one place allowed to resample.
    """
    import numpy as np

    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            shape = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a WAV clip: {exc}") from None
    if shape != (1, 2, 16000):
        raise ValueError(f"expected 16 kHz mono 16-bit WAV, got {shape[2]} Hz, "
                         f"{shape[0]} channel(s), {8 * shape[1]}-bit")
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def transcribe_local(wav: bytes, *, lang: str, model: str) -> str:
    """One recorded clip to text, in process."""
    audio = _samples(wav)
    language = lang.strip()
    segments, _info = _load(model).transcribe(
        audio,
        language=None if language in ("", "auto") else language,
        # Whisper fills silence with the subtitle credits it was trained on
        # ("Sottotitoli creati dalla comunità Amara.org"); the VAD cuts the
        # silence before the model can hear it.
        vad_filter=True,
    )
    return "".join(s.text for s in segments).strip()


def speech_lang(config: Any) -> str:
    """The BCP 47 tag the browser's speech APIs should use, "" when unknown.

    The dictation language when it is fixed, else the vault's own language:
    Learn reads a question aloud in the language the note is written in, and a
    browser left to guess picks its UI language, which reads an Italian vault
    with an English voice.
    """
    fixed = (getattr(config, "stt_lang", "") or "").strip()
    if fixed and fixed != "auto":
        return fixed
    from silica.kernel.text.language import SNOWBALL_TO_ISO
    from silica.onboarding.checks import reply_language_for

    name = (reply_language_for((getattr(config, "vault_path", "") or "").strip()) or "").strip().lower()
    if name in SNOWBALL_TO_ISO:
        return SNOWBALL_TO_ISO[name]
    return name if len(name) == 2 and name.isalpha() else ""
