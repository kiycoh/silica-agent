# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
import httpx
import openai
from pydantic import BaseModel

from silica.model import llm
from silica.model.llm import LLMResponse, openrouter_routing
from silica.config import ensure_prefix
from silica.metrics import emit, ms_since, number, outcome_of

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class Provider:
    """The non-interactive LLM lane: distiller, capability workers, sub-agents.

    One `call_llm(messages, ...) -> LLMResponse`, routed through the same
    `silica.model.llm.call_llm` the interactive loop uses, so both lanes share
    one retry policy, one wire boundary, one max-token clamp and one set of
    per-provider quirks (ollama's num_ctx, lmstudio's api_base, openrouter's
    routing pin). This used to be a second stack on the raw openai SDK, which
    meant those quirks had to be fixed twice — and one of them, Ollama's silent
    4096-token truncation, was only ever fixed on one side.

    `model` carries its provider prefix (litellm resolves the endpoint from it);
    `api_key` is passed only when the role overrides what litellm would resolve.
    `openrouter_pin` and `sdk_retries` are what a lane asks of its transport
    (get_provider), so the lane's caller never has to say them.
    """

    model: str
    api_key: str = ""
    openrouter_pin: str | None = None
    sdk_retries: int | None = None

    def call_llm(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        response_schema: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning: bool | None = None,
        cancel: "threading.Event | None" = None,
    ) -> LLMResponse:
        """`cancel` marks the call abandoned: a caller that bounds this with its
        own wall-clock deadline sets it when that deadline fires, so retry_transient
        stops rescheduling instead of issuing requests nobody is waiting for."""
        # Structured decoding defaults to greedy: this path exists to extract a
        # fixed shape, and sampling at the provider default (0.8 on Ollama, 1.0
        # on OpenAI) buys variety nobody wants in an extraction. Ollama's
        # structured-outputs doc calls for temperature 0 explicitly. Pass an
        # explicit temperature to override.
        if temperature is None and response_schema is not None:
            temperature = 0.0
        return llm.call_llm(
            model=self.model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            response_format=response_schema,
            temperature=temperature,
            openrouter_provider=self.openrouter_pin,
            api_key=self.api_key or None,
            # Forwarded, not defaulted: a hybrid model bills its thinking
            # against max_tokens, and a structured worker call that cannot
            # switch it off gets its JSON cut at the budget (the dedup judge,
            # 2026-08-23). Which workers turn it off is their call, not the
            # lane's: the distiller's output is prose the trace may improve.
            reasoning=reasoning,
            cancel=cancel,
            sdk_retries=self.sdk_retries,
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
        # OpenRouter routes an embedding model like a chat model, so the
        # pilot's zero-retention pin for the index lives where the chat's does
        # (OPENROUTER_PROVIDER_BY_MODEL). The empty list keeps the chat's
        # provider ORDER off it: an LLM host is not an embedding provider.
        self._pin = openrouter_routing("", model)

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
        on_openrouter = "openrouter.ai" in (self.query_base_url if own else self.base_url)
        started, outcome, usage = time.monotonic(), "ok", None
        try:
            response = client.embeddings.create(model=self.model, input=texts,
                                                extra_body=self._pin if on_openrouter else None)
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
            # A local embedder costs the host's cores (the tokens, and how long
            # a query waits behind a batch); a metered one also bills, and
            # OpenRouter says how much in usage.cost, as on the rerank line.
            emit("embed", lane="query" if query else "document", inputs=len(texts),
                 tokens=number(getattr(usage, "prompt_tokens", None)),
                 usd=number(getattr(usage, "cost", None)),
                 ms=ms_since(started), outcome=outcome)
        # The API guarantees ordering matches the input list
        return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]


def get_embedder(config: Any) -> OpenAIEmbedder:
    """Return an embedder configured from SilicaConfig."""
    return OpenAIEmbedder(
        base_url=config.embedding_base_url,
        api_key=config.embedding_api_key,
        model=config.embedding_model,
        query_prefix=config.embedding_query_prefix,
        document_prefix=config.embedding_document_prefix,
        read_timeout=config.embedding_timeout,
        query_base_url=config.embedding_query_base_url,
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
    base_url = config.rerank_base_url
    model = config.rerank_model
    if not (base_url and model):
        return None
    return Reranker(
        base_url=base_url, model=model, api_key=config.rerank_api_key,
        timeout=config.rerank_timeout,
    )


def local_rerank_model(config: Any) -> str:
    """The weights the rerank server loads: the config override, else the shipped
    LOCAL_RERANK_MODEL. One place, so `silica doctor` reports what actually runs.
    The override was declined on 2026-08-19 while one cross-encoder served every
    machine; it exists since 2026-09-27 for a personal stack on jina-reranker-v3.5,
    whose CC-BY-NC licence keeps it out of the default."""
    from silica.config import LOCAL_RERANK_MODEL

    return config.local_rerank_model or LOCAL_RERANK_MODEL


# The config fields behind each role: (model, provider).
_ROLES = {
    "worker": ("worker_model", "worker_provider"),
    "judge": ("judge_model", "judge_provider"),
    "escalation": ("distill_escalation_model", "distill_escalation_provider"),
    "vision": ("vision_model", "vision_provider"),
}
# Lanes run on a role and ask their transport for something of their own (get_provider).
_LANE_ROLE = {"distiller": "worker", "outline": "worker"}


def resolve_role(role: str = "router", config: Any = None) -> tuple[str, str]:
    """(model, key) a role runs on: its own model when one is set, else the router's.

    "router"     → config.provider / config.model (the main model).
    "worker"     → leashed sub-agents, on a separate small model.
    "escalation" → escalated steer retries.
    "judge"      → the verdicts (spec M7).
    "vision"     → images (ADR-0077 amendment 2026-10-21).

    The model decides, not the provider: litellm resolves the endpoint from the
    model's prefix, so the provider field only restores a prefix the id was
    written without, and an id with neither falls to the router. `key` is "" unless the role carries its own, which only the
    worker can (SILICA_WORKER_API_KEY); the endpoint still comes from the model.

    `config` is a parameter because the capability workers run on the config
    they were handed; every other caller reads CONFIG.
    """
    if config is None:
        from silica.config import CONFIG

        config = CONFIG
    role = _LANE_ROLE.get(role, role)
    fields = _ROLES.get(role)
    # `or ""` on every read: these fields are Optional on the config, and the
    # router fallback below is a falsiness test either way.
    model = (getattr(config, fields[0]) or "") if fields else ""
    router_provider = config.provider or ""
    provider = (getattr(config, fields[1]) or "") if fields else ""
    if role == "vision":
        # The one role that inherits the chat's provider: hosted, a vision model
        # is one variable (config.vision_model), and it must not need a second.
        provider = provider or router_provider
    # An id with no prefix and no provider beside it names no endpoint, and litellm
    # would refuse the call. It always meant the router here, so a config that worked
    # keeps working. Any prefix counts, not only PROVIDER_PREFIXES: litellm resolves far
    # more of them (`together_ai/`, `groq/`) and the id runs as set.
    if not model or (not provider and "/" not in model):
        # litellm resolves the endpoint from the prefix, so keep it (or restore
        # it when the user wrote a bare id). Ollama in particular MUST keep it:
        # the prefix is what routes it to /api/chat with an explicit num_ctx, and
        # the /v1 endpoint it would otherwise hit cannot size the context window
        # and truncates window-sized prompts in silence (measured: 2051 of 6645
        # prompt tokens kept at Ollama's 4096 default, zero tool calls, HTTP 200).
        #
        # Same rule as config.ensure_prefix, guard included: only a prefix
        # litellm knows can resolve an endpoint, so an unlisted provider
        # (SILICA_WORKER_PROVIDER=vllm) keeps its bare id, and pinning `vllm/` in
        # front of it turns a routable model into a BadRequestError.
        return ensure_prefix(config.model or "", router_provider), ""
    key = (config.worker_api_key or "") if role == "worker" else ""
    return ensure_prefix(model, provider), key


def get_provider(config: Any, role: str = "router") -> Provider:
    """The LLM lane for `role` (resolve_role), or for one of the two lanes that
    run on the worker and ask its transport for more:

    "distiller" → the worker on CONFIG.openrouter_provider_distiller's pin.
    "outline"   → the same, with no client retries. Every outline call runs at
                  temperature 0, so a 500 the client retried came back as the
                  same reply, three times on Lezione 8 (llama-server,
                  2026-10-12); the stage's loop and re-ask are the retries.
    """
    return lane_on(*resolve_role(role, config), config, role)


def lane_on(model: str, key: str, config: Any, role: str) -> Provider:
    """`role`'s lane on `model`: what a lane falls back on when its own model failed."""
    pin = config.openrouter_provider_distiller if role in _LANE_ROLE else None
    return Provider(model, key, openrouter_pin=pin, sdk_retries=0 if role == "outline" else None)
