# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit
import httpx
import openai
from pydantic import BaseModel

from silica.agent.compaction import TEXT_CHARS_PER_TOKEN
from silica.agent.llm import LLMResponse
from silica.config import DEFAULT_EMBEDDING_MODEL, HOSTED_PROVIDERS, ensure_prefix
from silica.metrics import emit, ms_since, number, outcome_of

logger = logging.getLogger(__name__)

_OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"


def _ollama_base_url() -> str:
    """Ollama's endpoint, honouring its own OLLAMA_HOST convention.

    OLLAMA_HOST is what `ollama` itself reads, so a user who already points the
    CLI at a remote box or a non-default port expects silica to follow. Accepts
    every shape ollama does: `host`, `host:port`, `http://host:port`. Port
    defaults to 11434, matching ollama's client.
    """
    raw = (os.getenv("OLLAMA_HOST") or "").strip()
    if not raw:
        return _OLLAMA_DEFAULT_URL
    try:
        u = urlsplit(raw if "://" in raw else f"http://{raw}")
        return f"{u.scheme}://{u.hostname}:{u.port or 11434}/v1"
    except ValueError:
        # Falling back beats crashing every `silica` import on a typo'd env var;
        # the endpoint check then reports localhost unreachable.
        logger.warning("OLLAMA_HOST=%r is not a valid host — using %s", raw, _OLLAMA_DEFAULT_URL)
        return _OLLAMA_DEFAULT_URL


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


# Hosted, OpenAI-compatible base URLs. litellm resolves the same prefixes
# natively for the interactive loop; the presets below serve the
# constrained-decoding/distiller path. Gemini goes through its OpenAI-compatible
# endpoint; both loops read the same GEMINI_API_KEY. "custom" (any other
# OpenAI-compatible URL) has no static row — its endpoint comes from
# config.provider_base_url/_api_key (see get_provider).
_HOSTED_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
}

PROVIDER_PRESETS = {
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio"
    },
    "ollama": {
        "base_url": _ollama_base_url(),
        "api_key": "ollama"  # Ollama ignores it; the OpenAI SDK demands non-empty.
    },
    # Hosted rows derive their key env var from config.HOSTED_PROVIDERS — one
    # table, so the two can't drift (they used to be kept in sync by hand).
    **{
        name: {"base_url": _HOSTED_BASE_URLS[name], "api_key_env": key_env}
        for name, (key_env, _models) in HOSTED_PROVIDERS.items()
    },
}


SILICA_CLI_OPEN = "<silica-cli>"
SILICA_CLI_CLOSE = "</silica-cli>"


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
    import time

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
    base = base_url.rstrip("/").removesuffix("/v1")
    props = httpx.get(f"{base}/props", timeout=5.0).json()
    return int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)


def _model_limits_fetch(provider: str, model: str) -> tuple[int, int]:
    try:
        if provider == "ollama":
            base = PROVIDER_PRESETS["ollama"]["base_url"].removesuffix("/v1")
            wanted = model.removeprefix("ollama/")
            info = httpx.post(f"{base}/api/show", json={"model": wanted}, timeout=5.0).json()
            mi = info.get("model_info") or {}
            arch = mi.get("general.architecture", "")
            trained = int(mi.get(f"{arch}.context_length") or next(
                (v for k, v in mi.items() if k.endswith(".context_length")), 0) or 0)
            if not trained:
                return (0, 0)  # model not pulled / unknown arch → caller's default
            return min(ollama_num_ctx(), trained), 0
        if provider == "lmstudio":
            base = PROVIDER_PRESETS["lmstudio"]["base_url"].removesuffix("/v1")
            data = httpx.get(f"{base}/api/v0/models", timeout=5.0).json()["data"]
            wanted = model.removeprefix("lmstudio/")
            entry = next(m for m in data if m["id"] == wanted)
            window = entry.get("loaded_context_length") or entry.get("max_context_length") or 0
            return int(window), 0
        if provider == "custom":
            from silica.config import CONFIG
            return _llama_props_window(CONFIG.provider_base_url or ""), 0
        if provider == "openrouter":
            data = httpx.get("https://openrouter.ai/api/v1/models", timeout=5.0).json()["data"]
            wanted = model.removeprefix("openrouter/")
            entry = next(m for m in data if m["id"] == wanted)
            out_cap = (entry.get("top_provider") or {}).get("max_completion_tokens") or 0
            return int(entry.get("context_length") or 0), int(out_cap)
    except Exception as e:
        logger.debug("model_limits(%s, %s) unavailable: %s", provider, model, e)
    return (0, 0)


def clamp_max_tokens(provider: str, model: str, requested: int | None, input_chars: int = 0) -> int:
    """Output-token budget for a request: the caller's ask (or the MAX_TOKENS
    default), never above the provider's live max_completion_tokens, never
    above the window space left after the input.

    Providers validate input + max_tokens <= context window upfront; without
    this clamp a default above either limit makes them reject the request
    (e.g. claude-sonnet-5 on OpenRouter: 128k output cap, 262144 window).
    input_chars is the serialized request size, counted at
    compaction.TEXT_CHARS_PER_TOKEN so the output budget errs small: // 3 was
    assumed to overestimate and does not on converted slides (2.05 to 2.41 chars
    a token on Qwen3.5, measured 2026-10-01).
    """
    # ponytail: 32768 default keeps the OpenRouter pool wide — cheap endpoints
    # advertise smaller output caps and get dropped above this. 256k measured
    # bad, 32768 measured good; in-between never A/B'd. Override via MAX_TOKENS;
    # sweep the in-between only if real runs start finishing at this ceiling
    # (finish_reason=length on distill).
    want = requested if requested is not None else int(os.getenv("MAX_TOKENS", "32768"))
    window, out_cap = model_limits(provider, model)
    if out_cap:
        want = min(want, out_cap)
    if window:
        # floor 1024 keeps the request well-formed when input nearly
        # fills the window; compaction is the real defense at that point.
        want = min(want, max(window - input_chars // TEXT_CHARS_PER_TOKEN, 1024))
    return want


# Keys the history carries for silica's own use and the provider must never see.
# `origin` is turn provenance; `silica_reasoning` is the model's thinking, kept
# so a reopened chat can replay the trace it showed while streaming — sending it
# back would re-bill a multi-thousand-token trace on every later iteration of the
# tool loop, which is exactly why it used to be thrown away. `silica_taint`
# marks the tool message whose result tainted the conversation by a path
# (user_memory.TAINT_KEY, ADR-0090 point 8). `silica_turn_notes` is what the
# chat drew under an answer (ADR-0094, agent/turn_notes.py): the reader's
# line, never the model's.
# `silica_workspace` is the workspace a user turn ran in (ADR-0112 point 4),
# kept on the turn so a reopened chat knows its scope. `silica_reads` is the
# notes a turn read, kept on its answer so a reopened chat links it the same
# (ADR-0127).
INTERNAL_KEYS = ("origin", "silica_reasoning", "silica_taint", "silica_turn_notes", "silica_workspace",
                 "silica_files", "silica_said", "silica_reads")


def _to_wire(msg: dict) -> dict:
    """Strip internal fields and render the CLI marker for the wire.

    The OpenAI message object rejects unknown fields, so `INTERNAL_KEYS` must
    never reach the SDK. When ``origin == "cli"`` the content is wrapped in
    <silica-cli> markers so the model can tell a harness directive apart from a
    human turn. Messages carrying none of them are returned unchanged.
    """
    if not any(k in msg for k in INTERNAL_KEYS):
        return msg
    wire = {k: v for k, v in msg.items() if k not in INTERNAL_KEYS}
    if msg.get("origin") == "cli" and wire.get("content"):
        wire["content"] = f"{SILICA_CLI_OPEN}{wire['content']}{SILICA_CLI_CLOSE}"
    return wire


class Provider:
    """The non-interactive LLM lane: distiller, capability workers, sub-agents.

    One `call_llm(messages, ...) -> LLMResponse`, routed through the same
    `silica.agent.llm.call_llm` the interactive loop uses, so both lanes share
    one retry policy, one wire boundary, one max-token clamp and one set of
    per-provider quirks (ollama's num_ctx, lmstudio's api_base, openrouter's
    routing pin). This used to be a second stack on the raw openai SDK, which
    meant those quirks had to be fixed twice — and one of them, Ollama's silent
    4096-token truncation, was only ever fixed on one side.

    `model` carries its provider prefix (litellm resolves the endpoint from it);
    `api_key` is passed only when the role overrides what litellm would resolve.
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        # Diagnostic only — NOT how the call is routed. llm.call_llm derives the
        # api_base from the model prefix, so changing this steers nothing; it is
        # kept because it is the endpoint a "which box answered?" question means.
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    def call_llm(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_schema: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        openrouter_provider: str | None = None,
        temperature: float | None = None,
        reasoning: bool | None = None,
        cancel: "threading.Event | None" = None,
        sdk_retries: int | None = None,
    ) -> LLMResponse:
        """`cancel` marks the call abandoned: a caller that bounds this with its
        own wall-clock deadline sets it when that deadline fires, so retry_transient
        stops rescheduling instead of issuing requests nobody is waiting for."""
        from silica.agent.llm import call_llm  # lazy: llm.py imports this module

        # Structured decoding defaults to greedy: this path exists to extract a
        # fixed shape, and sampling at the provider default (0.8 on Ollama, 1.0
        # on OpenAI) buys variety nobody wants in an extraction. Ollama's
        # structured-outputs doc calls for temperature 0 explicitly. Pass an
        # explicit temperature to override.
        if temperature is None and response_schema is not None:
            temperature = 0.0
        return call_llm(
            model=self.model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            response_format=response_schema,
            temperature=temperature,
            openrouter_provider=openrouter_provider,
            api_key=self.api_key or None,
            # Forwarded, not defaulted: a hybrid model bills its thinking
            # against max_tokens, and a structured worker call that cannot
            # switch it off gets its JSON cut at the budget (the dedup judge,
            # 2026-08-23). Which workers turn it off is their call, not the
            # lane's: the distiller's output is prose the trace may improve.
            reasoning=reasoning,
            cancel=cancel,
            sdk_retries=sdk_retries,
        )


_warned_down: set[tuple[str, str]] = set()


# What a call to `OpenAIEmbedder.embed` can raise when the endpoint, not the
# caller, is at fault: the SDK's own family, the transport under it, and the
# socket errors a local server exposes. A fail-open guard that catches these
# and nothing else lets a programming error surface instead of reading as
# "embedder down" (silica_vaults and the recall probes catch this tuple).
EMBED_ERRORS: tuple[type[BaseException], ...] = (openai.APIError, httpx.HTTPError, OSError)


def _failure_kind(exc: Exception) -> str:
    """'down' | 'rejected' | 'failed' — what the user has to go fix.

    The distinction is whether an HTTP response came back at all: no response
    means nothing is listening (start the server), a 4xx means something IS
    listening and refused the request (wrong model name, or a base_url whose
    path is off). 5xx and non-HTTP errors stay 'failed' — the server is up and
    broken, which is neither.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return "rejected" if 400 <= status < 500 else "failed"
    # Not OSError at large: a missing weights file or a full disk raise it too,
    # and "unreachable" would be a lie there.
    if isinstance(exc, (openai.APIConnectionError, httpx.TransportError, ConnectionError)):
        return "down"
    return "failed"


# The cause a retrieval call records for a leg it ran without (ADR-0094),
# finer than `_failure_kind` where the fix differs: a timeout is a server that is
# up and slow (raise the timeout, do not start it), and a 503 is llama.cpp still
# loading its weights, which `serve.ready` already reads the same way.
_WHY = {"down": "unreachable", "rejected": "rejected", "failed": "failed"}


def why_of(exc: Exception) -> str:
    """'slow' | 'loading' | 'unreachable' | 'rejected' | 'failed'."""
    # Before the status check and before `_failure_kind`: APITimeoutError is an
    # APIConnectionError, and httpx's timeouts are transport errors, so both
    # would otherwise read as "unreachable".
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
        return "slow"
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 503:
        return "loading"
    return _WHY[_failure_kind(exc)]


def warn_down_once(role: str, where: str, exc: Exception, model: str = "") -> None:
    """Report a degraded relatedness leg once per (role, kind), then at DEBUG.

    Both legs fail open by design (embed leg abstains, rerank keeps the fused
    order), and every caller swallows the failure at DEBUG — so a server the
    user forgot to start degrades recall invisibly. This is the one line that
    says so, and says which of the two problems it is. Once per kind and not
    per call, because a batch embeds hundreds of times; keying on the kind too
    means the follow-on problem still gets its own line after the first is fixed.
    """
    kind = _failure_kind(exc)
    args: tuple
    if kind == "down":
        msg, args = "%s unreachable at %s (%s)", (role, where, exc)
    elif kind == "rejected":
        msg = "%s at %s is up but rejected model `%s` (%s)"
        args = (role, where, model, exc)
    else:
        msg, args = "%s failed at %s (%s)", (role, where, exc)
    msg += " — recall degrades; run `silica doctor`"

    if (role, kind) in _warned_down:
        logger.debug(msg, *args)
        return
    _warned_down.add((role, kind))
    logger.warning(msg, *args)


class OpenAIEmbedder:
    """Thin wrapper for the OpenAI-compatible /v1/embeddings endpoint.

    Uses the same SDK already present in the project. Suitable for any
    provider that speaks the OpenAI API (LM Studio, OpenRouter, etc.).
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 query_prefix: str = "", document_prefix: str = "",
                 read_timeout: float = 60.0, query_base_url: str = ""):
        # Mirror the LLM provider's hardening: a granular read-timeout turns a
        # frozen embedding server (e.g. a cold/contended local model) into a
        # fast failure instead of an indefinite hang, and max_retries=1 stops
        # the SDK's default 2 silent retries from stacking 60s waits. COLLISION
        # is best_effort, so a bounded failure degrades to "skip dedup" rather
        # than freezing the run. The read bound is a knob because 60 s is a GPU
        # number: on 2 ARM cores one 32-note batch takes minutes, and the index
        # build failed on batch 0 every time (SILICA_EMBEDDING_TIMEOUT).
        _timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=5.0)
        self.client = openai.OpenAI(
            base_url=base_url, api_key=api_key, timeout=_timeout, max_retries=1
        )
        self.base_url = base_url
        # A query's own server, so it never queues behind an index batch
        # (config.embedding_query_base_url). None, and not a second reference
        # to `client`, keeps a test that swaps `client` covering queries too.
        self.query_base_url = query_base_url if query_base_url not in ("", base_url) else ""
        self.query_client = openai.OpenAI(
            base_url=query_base_url, api_key=api_key, timeout=_timeout, max_retries=1
        ) if self.query_base_url else None
        self.model = model
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """Return one embedding vector per input text.

        `query=True` marks a search query, everything else is a document: the
        two get different role prefixes on asymmetric embedders (config
        embedding_query_prefix / embedding_document_prefix, empty by default).

        Vectors are normalised by most embedding models; cosine similarity is
        therefore equivalent to dot-product for those models.
        """
        if not texts:
            return []
        prefix = self.query_prefix if query else self.document_prefix
        if prefix:
            texts = [prefix + t for t in texts]
        client = self.client
        if query and self.query_client is not None:
            client = self.query_client
        own = client is not self.client
        started, outcome, usage = time.monotonic(), "ok", None
        try:
            response = client.embeddings.create(model=self.model, input=texts)
            usage = getattr(response, "usage", None)
        except Exception as e:
            outcome = outcome_of(e)
            # Warn, then re-raise unchanged: the callers' fail-open guards stay
            # in charge of what to skip, this only makes the skip visible.
            # Its own role, so a dead query server does not use up the one
            # warning the index server's failure would get.
            if own:
                warn_down_once("query embedder", self.query_base_url, e, self.model)
            else:
                warn_down_once("embedder", self.base_url, e, self.model)
            raise
        finally:
            # The hosted embedder is free and local, so its cost is the host's
            # 2 cores: the tokens, and how long a query waits behind a batch.
            emit("embed", lane="query" if query else "document", inputs=len(texts),
                 tokens=number(getattr(usage, "prompt_tokens", None)),
                 ms=ms_since(started), outcome=outcome)
        # The API guarantees ordering matches the input list
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


def get_embedder(config: Any) -> OpenAIEmbedder:
    """Return an embedder configured from SilicaConfig."""
    return OpenAIEmbedder(
        base_url=getattr(config, "embedding_base_url", "http://localhost:1234/v1"),
        api_key=getattr(config, "embedding_api_key", "lm-studio"),
        model=getattr(config, "embedding_model", DEFAULT_EMBEDDING_MODEL),
        query_prefix=getattr(config, "embedding_query_prefix", ""),
        document_prefix=getattr(config, "embedding_document_prefix", ""),
        read_timeout=getattr(config, "embedding_timeout", 60.0),
        query_base_url=getattr(config, "embedding_query_base_url", ""),
    )


def get_embedder_or_none(config: Any, label: str, *, level: str = "warning") -> OpenAIEmbedder | None:
    """Acquire the embedder, or None if unavailable (logged at `level`).

    Centralizes the 'try get_embedder, on failure log and skip the phase' guard
    each embedding-gated FSM handler repeats; callers keep their own skip action.
    """
    try:
        return get_embedder(config)
    except Exception as e:
        getattr(logger, level)("%s: embedder unavailable (%s) — skipping", label, e)
        return None


class Reranker:
    """Cross-encoder reranker over a served /rerank endpoint.

    Speaks the de-facto protocol (llama.cpp --rerank, Infinity, Jina, Cohere):
    ``POST {model, query, documents} -> {results: [{index, relevance_score}]}``.
    A cross-encoder scores query x document *jointly* — the biggest precision
    lever retrieval has after first-stage recall — so it is used to reorder an
    already-fused candidate pool, never to retrieve.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 5.0):
        self.url = base_url.rstrip("/") + "/rerank"
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.timeout = timeout
        # Why the last `scores` call abstained (`why_of`), None when it scored.
        # One call's worth: get_reranker builds a client per retrieval call.
        self.failure: str | None = None

    def scores(self, query: str, documents: list[str]) -> list[float] | None:
        """Relevance score per document in input order, or None to abstain.

        Abstains (None) on any transport or response-shape failure so the caller
        keeps its prior ordering rather than dropping candidates, and says why in
        `failure`. The short timeout keeps a slow reranker from stalling an
        interactive path.
        """
        self.failure = None
        if not query or not documents:
            return None
        started, outcome = time.monotonic(), "malformed"
        usage: dict = {}
        try:
            resp = httpx.post(
                self.url,
                json={"model": self.model, "query": query, "documents": documents},
                headers=self.headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            # OpenRouter's /rerank usage (schema read 2026-10-15); a local
            # llama.cpp or rerank_serve reply has none.
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            results = body.get("results")
            if not isinstance(results, list):
                self.failure = "failed"
                return None
            scored: list[float | None] = [None] * len(documents)
            for r in results:
                i = r.get("index")
                if isinstance(i, int) and 0 <= i < len(documents):
                    scored[i] = float(r.get("relevance_score", r.get("score", 0.0)))
            if any(s is None for s in scored):
                # A reply that skips documents used to be padded with 0.0, which
                # passed the caller's length check and reranked the pool on zeros
                # (ADR-0094): abstain, so the fused order stands and is marked.
                self.failure = "failed"
                return None
            outcome = "ok"
            return scored  # type: ignore[return-value]  # every slot was filled above
        except Exception as e:
            outcome = outcome_of(e)
            warn_down_once("reranker", self.url, e, self.model)
            self.failure = why_of(e)
            return None
        finally:
            emit("rerank", documents=len(documents), tokens=number(usage.get("total_tokens")),
                 usd=number(usage.get("cost")), ms=ms_since(started), outcome=outcome)


@lru_cache(maxsize=1)
def has_local_rerank() -> bool:
    """Whether the optional [rerank] extra is installed, i.e. whether this
    interpreter can run `silica.agent.rerank_serve`. find_spec, not import:
    importing torch costs seconds and 454 MB."""
    return importlib.util.find_spec("sentence_transformers") is not None


def get_reranker(config: Any) -> Reranker | None:
    """The served /rerank endpoint, or None (disabled).

    No in-process leg (ADR-0073): loading the cross-encoder into the calling
    process cost it 1,420 MB of host RAM, and the fallback that did so fired
    exactly when the server was slowest. The [rerank] extra runs as its own
    server (`silica.agent.rerank_serve`); while it is down or loading, this
    client abstains and the fused order stands.
    """
    base_url = getattr(config, "rerank_base_url", "")
    model = getattr(config, "rerank_model", "")
    if not (base_url and model):
        return None
    return Reranker(
        base_url=base_url, model=model, api_key=getattr(config, "rerank_api_key", ""),
        timeout=getattr(config, "rerank_timeout", 5.0),
    )


def local_rerank_model(config: Any) -> str:
    """The weights the rerank server loads: the config override, else the shipped
    LOCAL_RERANK_MODEL. One place, so `silica doctor` reports what actually runs.
    The override was declined on 2026-08-19 while one cross-encoder served every
    machine; it exists since 2026-09-27 for a personal stack on jina-reranker-v3.5,
    whose CC-BY-NC licence keeps it out of the default."""
    from silica.agent.rerank_serve import LOCAL_RERANK_MODEL

    return getattr(config, "local_rerank_model", "") or LOCAL_RERANK_MODEL


def get_provider(config: Any, role: str = "router") -> Provider:
    """Return an LLM provider for the given role.

    role="router" (default) → uses config.provider / config.model (the main model).
    role="worker"            → uses config.worker_provider / config.worker_model so
                               leashed sub-agents can run on a separate small model.
    role="escalation"        → uses config.distill_escalation_provider / _model for escalated steer retries.
    role="judge"             → uses config.judge_provider / config.judge_model (spec M7); router when unset.
    role="vision"            → uses config.vision_model, on config.vision_provider or the router's
                               provider (ADR-0077 amendment 2026-10-21); router when unset.

    When the worker role specifies an explicit worker_api_key it wins over the
    preset; the endpoint always comes from the worker_provider preset.
    """
    # `or ""` on every read: these fields are Optional on the config, and the
    # "fall back to the router role" test below is a falsiness test either way.
    provider_name: str
    model_name: str
    if role == "escalation":
        provider_name = getattr(config, "distill_escalation_provider", "") or ""
        model_name = getattr(config, "distill_escalation_model", "") or ""
        if not provider_name or not model_name:
            provider_name = getattr(config, "provider", "lmstudio") or ""
            model_name = getattr(config, "model", "") or ""
            role = "router"
    elif role == "judge":
        provider_name = getattr(config, "judge_provider", "") or ""
        model_name = getattr(config, "judge_model", "") or ""
        if not provider_name or not model_name:
            provider_name = getattr(config, "provider", "lmstudio") or ""
            model_name = getattr(config, "model", "") or ""
            role = "router"
    elif role == "vision":
        # Unlike the judge, the model alone is enough: falling back to the
        # router because the provider was left unset would send the pixels to
        # a chat model that may take text only, the one case this role exists for.
        model_name = getattr(config, "vision_model", "") or ""
        provider_name = getattr(config, "vision_provider", "") or getattr(config, "provider", "lmstudio") or ""
        if not model_name:
            provider_name = getattr(config, "provider", "lmstudio") or ""
            model_name = getattr(config, "model", "") or ""
            role = "router"
    elif role == "worker":
        provider_name = getattr(config, "worker_provider", "") or ""
        model_name = getattr(config, "worker_model", "") or ""
        if not provider_name or not model_name:
            provider_name = getattr(config, "provider", "lmstudio") or ""
            model_name = getattr(config, "model", "") or ""
            role = "router"
    else:
        provider_name = getattr(config, "provider", "lmstudio") or ""
        model_name = getattr(config, "model", "") or ""

    preset = PROVIDER_PRESETS.get(provider_name)
    if preset:
        base_url = preset["base_url"]
        api_key = preset.get("api_key", "lm-studio")
        if "api_key_env" in preset:
            api_key = os.getenv(preset["api_key_env"], "dummy-key")
    else:
        # custom / unknown provider: endpoint from config (SILICA_PROVIDER_BASE_URL
        # / _API_KEY). Falls back to the lmstudio localhost default so a bare
        # misconfig still points somewhere local rather than crashing.
        base_url = getattr(config, "provider_base_url", "") or PROVIDER_PRESETS["lmstudio"]["base_url"]
        api_key = getattr(config, "provider_api_key", "") or "dummy-key"

    # Worker role: explicit api-key override takes precedence over the preset.
    if role == "worker":
        api_key = getattr(config, "worker_api_key", None) or api_key

    # litellm resolves the endpoint from the prefix, so keep it (or restore it
    # when the user wrote a bare id). Ollama in particular MUST keep it: the
    # prefix is what routes it to /api/chat with an explicit num_ctx, and the
    # /v1 endpoint it would otherwise hit cannot size the context window and
    # truncates window-sized prompts in silence (measured: 2051 of 6645 prompt
    # tokens kept at Ollama's 4096 default, zero tool calls, HTTP 200).
    #
    # Same rule as config.ensure_prefix, guard included: the roles below read
    # the very fields it already prefixed, so re-deriving it without the guard
    # made the two disagree. Only a prefix litellm knows can resolve an
    # endpoint, so an unlisted provider (SILICA_WORKER_PROVIDER=vllm) keeps its
    # bare id — config leaves it bare for exactly that reason, and pinning
    # `vllm/` in front of it turns a routable model into a BadRequestError.
    model_name = ensure_prefix(model_name, provider_name)

    # The preset key is only worth passing when it is not what litellm would
    # resolve on its own — the worker role's explicit override. `custom` needs
    # no entry here: llm.call_llm reads provider_api_key off CONFIG directly.
    override = api_key if (role == "worker" and getattr(config, "worker_api_key", None)) else ""
    return Provider(base_url=base_url, api_key=override, model=model_name)
