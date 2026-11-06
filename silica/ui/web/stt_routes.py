# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The composer's voice: the state of the transcriber, and one clip transcribed."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, File, HTTPException, UploadFile

from silica.config import CONFIG
from silica.ui.web.failures import explain as _failure

router = APIRouter()


# 180 s of 16 kHz mono 16-bit PCM is 5.76 MB, Learn's take ceiling (app-learn.js);
# the cap is generous enough for that and still refuses a body that was never a clip.
_STT_MAX_BYTES = 8 * 1024 * 1024


@router.get("/stt")
def stt_status(warm: bool = True):
    """Whether dictation can work, and which lane will do it, asked before the
    browser requests the mic.

    A probe, not a config flag: the dictation endpoint falls back to
    stt_base_url, which has a default, so "configured" and
    "listening" are different questions and only the second one is useful. Shares
    ensure_local_servers' readiness check, which knows that llama.cpp-family
    servers answer 503 while they load and that an open port therefore lies.

    `browser` and `lang` ride along for the page's last lane, the Web Speech API,
    which only runs when neither transcriber here answers, and for Learn's read
    aloud, which asks with `warm=0` (ADR-0049).
    """
    from silica.agent import stt as local
    from silica.config import hosted
    from silica.onboarding.serve import ready

    url, key = local.dictation_endpoint(CONFIG)
    base = {"url": url, "browser": CONFIG.stt_browser, "lang": local.speech_lang(CONFIG),
            "hosted": hosted()}
    if url and ready(url):
        return {**base, "ok": True, "via": "endpoint", "detail": ""}
    if local.has_local_stt():
        model = local.local_stt_model(CONFIG)
        cold = not local.loaded(model)
        if warm:
            local.warm(model)
        return {**base, "ok": True, "via": "local", "model": model, "cold": cold, "detail": ""}
    if base["hosted"]:
        # A tenant can set neither the key nor the extra, and a loopback URL
        # names nothing it can reach, so the sentence names neither.
        why = "the transcriber is not answering" if url else "this server does not transcribe dictation"
        return {**base, "ok": False, "via": "", "detail": why}
    why = f"nothing is answering at {url}" if url else f"{key} is empty"
    return {**base, "ok": False, "via": "", "detail": why + " and the [stt] extra is not installed"}


async def _stt_via_endpoint(url: str, clip: bytes) -> tuple[str, str]:
    """One clip through the configured /audio/transcriptions endpoint:
    `(text, "")`, or `("", the sentence the user reads)` so the route can try
    the in-process lane before it reports anything."""
    from starlette.concurrency import run_in_threadpool

    from silica.agent.stt import post_transcription, transcribed
    from silica.metrics import outcome_of

    form = {"model": CONFIG.stt_model, "response_format": "json"}
    if CONFIG.stt_lang:
        form["language"] = CONFIG.stt_lang
    started = time.monotonic()
    try:
        resp = await run_in_threadpool(post_transcription, url, clip, name="clip.wav", mime="audio/wav",
                                       form=form, api_key=CONFIG.stt_api_key, timeout=120.0)
    except Exception as exc:
        transcribed("dictation", started, outcome_of(exc))
        return "", f"transcription endpoint unreachable: {exc}"
    if resp.status_code != 200:
        transcribed("dictation", started, str(resp.status_code))
        return "", f"transcription failed ({resp.status_code}): {resp.text[:200]}"
    try:
        payload = resp.json()
        text = (payload.get("text") or "").strip()
    except Exception as exc:
        transcribed("dictation", started, "malformed")
        return "", f"transcription endpoint returned no JSON: {exc}"
    transcribed("dictation", started, "ok" if text else "empty", payload)
    return text, ""


@router.post("/stt")
async def stt(audio: UploadFile = File(...)):
    """Transcribe one recorded clip: the endpoint when it answers, else in process.

    The browser sends 16 kHz mono WAV. MediaRecorder can only produce webm/opus,
    and whisper.cpp's server reads WAV unless it was built with ffmpeg, so the
    conversion happens in app-boot.js, where it costs no dependency on either side.

    Any endpoint failure falls through to the [stt] extra when it is installed:
    the default URL points at a server most installs never start, and a refused
    connection costs nothing. The reranker gave up its own in-process fallback
    (ADR-0073, 1.4 GB in every process); this lane's RAM is not measured yet.
    """
    from starlette.concurrency import run_in_threadpool

    from silica.agent import stt as local

    clip = await audio.read()
    if not clip:
        raise HTTPException(400, "empty recording")
    if len(clip) > _STT_MAX_BYTES:
        raise HTTPException(413, f"recording over {_STT_MAX_BYTES // (1024 * 1024)} MB")
    failed = ""
    url, _key = local.dictation_endpoint(CONFIG)
    if url:
        text, failed = await _stt_via_endpoint(url, clip)
        if not failed:
            return {"text": text, "via": "endpoint"}
    if not local.has_local_stt():
        if failed:
            from silica.config import hosted

            if hosted():
                # A spent allowance reaches dictation as OpenRouter's 403, whose
                # body is no sentence for a student: explain() names the layer
                # and its reset (ADR-0102 point 6). It reads the key, so off the loop.
                failed = (await asyncio.to_thread(_failure, failed))["error"]
            raise HTTPException(502, failed)
        raise HTTPException(503, "no transcription endpoint configured and the [stt] extra is not installed")
    try:
        # A thread, not the event loop: a CPU transcription of a long take runs
        # for seconds, and every other request (the narration stream included)
        # would stall behind it.
        text = await run_in_threadpool(
            local.transcribe_local, clip, lang=CONFIG.stt_lang, model=local.local_stt_model(CONFIG),
        )
    except Exception as exc:
        reasons = [r for r in (failed, f"in-process transcription failed: {exc}") if r]
        raise HTTPException(502, "; ".join(reasons)) from exc
    return {"text": text, "via": "local"}
