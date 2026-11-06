# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What one request may hold: the endpoint a local provider answers on, the
window and output cap it reports, and how characters are counted as tokens.

Below `model/llm`, which clamps every request with it, so the transport and
the role resolver above it read the same numbers; the kernel reads them here too.
"""
from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlsplit

# Before anything here reads the environment: config is what loads `.env`, and
# OLLAMA_HOST set there must reach the read at import below.
from silica.config import CONFIG
from silica.provider_table import PROVIDERS

logger = logging.getLogger(__name__)

# What new text will cost is counted at 2 chars a token, so the estimate does
# not come in under the request. chars // 3 was assumed to overestimate and does
# not: Qwen3.5 tokenizes the test vault's converted lecture slides at 2.05 to
# 2.41 chars a token, 26 of 30 notes under 3 (2026-10-01), and a request judged
# to fit overflowed a 32k window by 820 tokens. Text under 2 chars a token (CJK,
# minified code) can still overflow; llama.cpp's 400 then names the real count
# and the loop resends once (loop.py). clamp_max_tokens counts input the same way.
TEXT_CHARS_PER_TOKEN = 2
# A collapsed read is credited at chars // 3: under what it frees on that dense
# text, so the estimate after a collapse stays high; over by a twelfth on English
# prose (~4 chars a token), which the 2-char count on the added side absorbs.
FREED_CHARS_PER_TOKEN = 3
# A figure for a size nobody clamps on: the meter with no tokenizer, a provider
# that reported no usage, the distiller's output headroom. English prose is ~4.
ROUGH_CHARS_PER_TOKEN = 4


def _ollama_base_url() -> str:
    """Ollama's endpoint, honouring its own OLLAMA_HOST convention.

    OLLAMA_HOST is what `ollama` itself reads, so a user who already points the
    CLI at a remote box or a non-default port expects silica to follow. Accepts
    every shape ollama does: `host`, `host:port`, `http://host:port`. Port
    defaults to 11434, matching ollama's client.
    """
    raw = (os.getenv("OLLAMA_HOST") or "").strip()
    if not raw:
        return PROVIDERS["ollama"].base_url
    try:
        u = urlsplit(raw if "://" in raw else f"http://{raw}")
        return f"{u.scheme}://{u.hostname}:{u.port or 11434}/v1"
    except ValueError:
        # Falling back beats crashing every `silica` import on a typo'd env var;
        # the endpoint check then reports localhost unreachable.
        logger.warning("OLLAMA_HOST=%r is not a valid host — using %s", raw, PROVIDERS["ollama"].base_url)
        return PROVIDERS["ollama"].base_url


# Ollama loads a model with a 4096-token window unless told otherwise, and it
# drops whatever does not fit in silence: HTTP 200, no warning (measured — a
# 6645-token prompt came back with prompt_eval_count=2051, the tool definitions
# gone, and the model answered in prose instead of calling a tool). The chat
# toolset alone is ~8k tokens, so the default window cannot hold a single turn.
# Pinned per request rather than left to the server's OLLAMA_CONTEXT_LENGTH,
# which is invisible from inside silica: a user who never set it got garbage.
_OLLAMA_DEFAULT_NUM_CTX = 16384


def ollama_num_ctx() -> int:
    """Context window silica pins on every Ollama request.

    Costs VRAM — the KV cache scales with it — so OLLAMA_NUM_CTX raises or lowers
    it. model_limits caps the effective value at the model's trained maximum.
    """
    raw = (os.getenv("OLLAMA_NUM_CTX") or "").strip()
    if not raw:
        return _OLLAMA_DEFAULT_NUM_CTX
    try:
        return max(int(raw), 1024)
    except ValueError:
        logger.warning("OLLAMA_NUM_CTX=%r is not an integer — using %d",
                       raw, _OLLAMA_DEFAULT_NUM_CTX)
        return _OLLAMA_DEFAULT_NUM_CTX


# Frozen at import, as every local base URL has been: the doctor, the wizard and
# the chat must name the same box for the life of the process.
_OLLAMA_BASE = _ollama_base_url()


def base_url(name: str) -> str:
    """Where `name`'s OpenAI-compatible API answers; "" for a provider without
    a fixed endpoint (`custom`, anything unlisted)."""
    if name == "ollama":
        return _OLLAMA_BASE
    row = PROVIDERS.get(name)
    return row.base_url if row else ""


# TTL memo, not lru_cache: limits move while silica runs (LM Studio reload
# with a different context window), and a stale window mis-clamps every
# request until restart. The TTL also un-sticks the (0,0) an unreachable
# endpoint used to pin for the whole process. Price: one metadata call per
# provider+model per window.
_MODEL_LIMITS_TTL_S = 600.0
_model_limits_memo: dict[tuple[str, str], tuple[float, tuple[int, int]]] = {}


def model_limits(provider: str, model: str) -> tuple[int, int]:
    """(context_window, max_output_tokens) as reported by the live provider,
    memoized for _MODEL_LIMITS_TTL_S per (provider, model).

    lmstudio   → GET {base}/api/v0/models: `loaded_context_length` (the window
                 the model is loaded with RIGHT NOW, often below its max) with
                 `max_context_length` as fallback. No output cap.
    ollama     → the window silica pins per request (ollama_num_ctx), capped at
                 the trained max from POST {base}/api/show
                 `model_info["<arch>.context_length"]`. Neither the runtime's
                 4096 default nor a Modelfile `num_ctx` can widen or narrow this:
                 request options win over both. No output cap.
    openrouter → GET /api/v1/models: `context_length` plus the top provider's
                 `max_completion_tokens` (often far below the window — e.g.
                 qwen3-8b: 131k ctx, 8k out).

    (0, 0) means unknown/unreachable: callers keep their static defaults
    (and the TTL retries the probe instead of pinning the failure).
    """
    now = time.monotonic()
    hit = _model_limits_memo.get((provider, model))
    if hit is not None and now < hit[0]:
        return hit[1]
    value = _model_limits_fetch(provider, model)
    _model_limits_memo[(provider, model)] = (now + _MODEL_LIMITS_TTL_S, value)
    return value


def _llama_props_window(base_url: str) -> int:
    """llama-server's /props: the window one request gets. With a unified KV
    (`kv_unified = 'true'` in b10053's load log, 4 slots) every slot reports
    the whole cache, which concurrent requests share. Raises on a server that
    has no /props (any other OpenAI-compatible one): the caller's (0, 0)."""
    import httpx

    base = base_url.rstrip("/").removesuffix("/v1")
    props = httpx.get(f"{base}/props", timeout=5.0).json()
    return int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)


def _ollama_limits(model: str) -> tuple[int, int]:
    import httpx

    base = base_url("ollama").removesuffix("/v1")
    wanted = model.removeprefix("ollama/")
    info = httpx.post(f"{base}/api/show", json={"model": wanted}, timeout=5.0).json()
    mi = info.get("model_info") or {}
    arch = mi.get("general.architecture", "")
    trained = int(mi.get(f"{arch}.context_length") or next(
        (v for k, v in mi.items() if k.endswith(".context_length")), 0) or 0)
    if not trained:
        return (0, 0)  # model not pulled / unknown arch → caller's default
    return min(ollama_num_ctx(), trained), 0


def _lmstudio_limits(model: str) -> tuple[int, int]:
    import httpx

    base = base_url("lmstudio").removesuffix("/v1")
    data = httpx.get(f"{base}/api/v0/models", timeout=5.0).json()["data"]
    wanted = model.removeprefix("lmstudio/")
    entry = next(m for m in data if m["id"] == wanted)
    window = entry.get("loaded_context_length") or entry.get("max_context_length") or 0
    return int(window), 0


def _custom_limits(model: str) -> tuple[int, int]:
    return _llama_props_window(CONFIG.provider_base_url or ""), 0


def _openrouter_limits(model: str) -> tuple[int, int]:
    import httpx

    data = httpx.get(f"{base_url('openrouter')}/models", timeout=5.0).json()["data"]
    wanted = model.removeprefix("openrouter/")
    entry = next(m for m in data if m["id"] == wanted)
    out_cap = (entry.get("top_provider") or {}).get("max_completion_tokens") or 0
    return int(entry.get("context_length") or 0), int(out_cap)


# The providers whose limits can be asked for, each in its own dialect. A
# provider without a row keeps the caller's static defaults.
_LIMIT_PROBES = {"ollama": _ollama_limits, "lmstudio": _lmstudio_limits,
                 "custom": _custom_limits, "openrouter": _openrouter_limits}


def _model_limits_fetch(provider: str, model: str) -> tuple[int, int]:
    probe = _LIMIT_PROBES.get(provider)
    if probe is None:
        return (0, 0)
    try:
        return probe(model)
    except Exception as e:
        logger.debug("model_limits(%s, %s) unavailable: %s", provider, model, e)
    return (0, 0)


def default_max_tokens() -> int:
    """The output budget a call gets when it names none.

    shortcut: 32768 keeps the OpenRouter pool wide: cheap endpoints advertise
    smaller output caps and get dropped above this. 256k measured bad, 32768
    measured good; in-between never A/B'd. Override via MAX_TOKENS; sweep the
    in-between once real runs finish at this ceiling (finish_reason=length on
    distill).
    """
    return int(os.getenv("MAX_TOKENS", "32768"))


def distiller_timeout() -> float:
    """The wall-clock seconds one distiller or outline call gets (run_with_deadline)."""
    return float(os.getenv("DISTILLER_TIMEOUT", "300"))


def clamp_max_tokens(provider: str, model: str, requested: int | None, input_chars: int = 0) -> int:
    """Output-token budget for a request: the caller's ask (or the MAX_TOKENS
    default), never above the provider's live max_completion_tokens, never
    above the window space left after the input.

    Providers validate input + max_tokens <= context window upfront; without
    this clamp a default above either limit makes them reject the request
    (e.g. claude-sonnet-5 on OpenRouter: 128k output cap, 262144 window).
    input_chars is the serialized request size, counted at TEXT_CHARS_PER_TOKEN
    so the output budget errs small.
    """
    want = requested if requested is not None else default_max_tokens()
    window, out_cap = model_limits(provider, model)
    if out_cap:
        want = min(want, out_cap)
    if window:
        # floor 1024 keeps the request well-formed when input nearly
        # fills the window; compaction is the real defense at that point.
        want = min(want, max(window - input_chars // TEXT_CHARS_PER_TOKEN, 1024))
    return want
