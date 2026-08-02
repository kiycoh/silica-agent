# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Silica configuration — model, vault, provider settings.

Configuration is loaded from (in order of precedence):
  1. Environment variables (SILICA_MODEL, SILICA_VAULT, etc.)
  2. .env file in the project root
  3. Hardcoded defaults

The config module is imported early and provides a singleton CONFIG object
that the rest of the codebase reads from.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import find_dotenv, load_dotenv

# Captured at package import (silica/__init__.py), before any third-party
# load_dotenv can blur an exported pin into a .env value. Re-exported here
# because this is where every caller expects to find it.
from silica import VAULT_PINNED  # noqa: E402,F401

# .env layering, first value wins per key (override=False): the project's own
# .env, found from the working directory upwards, then the user-level
# ~/.silica/.env the wizard writes when there is no project file. An installed
# silica has no .env beside its package, so before the user-level file existed
# every setting evaporated the moment you ran `silica` outside a checkout.
USER_ENV = Path.home() / ".silica" / ".env"
for _dotenv_path in (find_dotenv(usecwd=True), USER_ENV):
    if _dotenv_path:
        load_dotenv(_dotenv_path, override=False)


# Provider prefixes that map a `prefix/model` string to an endpoint and get
# auto-prefixed onto a bare model. Single source for the three checks below
# (provider, distill_escalation_provider, _ensure_prefix). "custom" routes to
# SILICA_PROVIDER_BASE_URL/_API_KEY; the rest to PROVIDER_PRESETS in
# agent.providers (kept a subset of this set — see test_providers).
PROVIDER_PREFIXES = frozenset({
    "openrouter", "lmstudio", "ollama", "gemini",
    "openai", "groq", "deepseek", "mistral", "xai", "custom",
})

# Hosted providers in fallback order: (API key env var, model ids best first).
# Two readers, one table — `model_from_env` takes the head of the first entry
# whose key is exported, the wizard offers the whole list as a pick-list. Lives
# here rather than beside PROVIDER_PRESETS because config is imported on every
# path, including the ones that must not pay for the openai SDK.
# ponytail: a hand-kept list goes stale as vendors ship — that is the ceiling,
# and the wizard's `other` entry is the escape hatch. Upgrade path if it rots:
# fetch /models from the provider instead of hardcoding.
HOSTED_PROVIDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "openrouter": ("OPENROUTER_API_KEY", (
        "openrouter/deepseek/deepseek-v4-flash",
        "openrouter/anthropic/claude-sonnet-5",
        "openrouter/google/gemini-3.5-flash",
        "openrouter/mistralai/mistral-small-2603",
    )),
    "gemini": ("GEMINI_API_KEY", ("gemini/gemini-2.5-flash",)),
    "openai": ("OPENAI_API_KEY", ("openai/gpt-4o",)),
    "groq": ("GROQ_API_KEY", ("groq/llama-3.3-70b-versatile",)),
    "deepseek": ("DEEPSEEK_API_KEY", ("deepseek/deepseek-chat",)),
    "mistral": ("MISTRAL_API_KEY", ("mistral/mistral-large-latest",)),
    "xai": ("XAI_API_KEY", ("xai/grok-2-latest",)),
}


def model_from_env() -> tuple[str, str]:
    """Resolve the chat model: (model id, source env var).

    SILICA_MODEL wins. Failing that, the first hosted provider whose API key is
    already exported answers for it — a user who has OPENROUTER_API_KEY in their
    shell should not have to name a model before silica will run. Returns
    ("", "") when nothing answers, which keeps the fail-fast for a bare install.

    Env keys only: probing a local endpoint from here would put an HTTP call on
    every config load. SILICA_PROVIDER set means the user pinned an endpoint
    (custom, ollama, ...) and a hosted guess would contradict it, so the chain
    stands down.
    """
    explicit = os.getenv("SILICA_MODEL", "").strip()
    if explicit:
        return explicit, "SILICA_MODEL"
    if os.getenv("SILICA_PROVIDER"):
        return "", ""
    for key_env, models in HOSTED_PROVIDERS.values():
        if os.getenv(key_env):
            return models[0], key_env
    return "", ""


@dataclass
class SilicaConfig:
    """Runtime configuration singleton."""

    # LLM provider — litellm model string. SILICA_MODEL, else derived from
    # whichever provider key is already exported (see model_from_env), else
    # empty: the REPL then points the user to `silica init` rather than assume
    # a hosted model whose API key was never mentioned.
    # Examples: "openrouter/anthropic/claude-sonnet-4-20250514", "qwen3-30b"
    model: str = field(
        default_factory=lambda: model_from_env()[0]
    )

    # Provider preset name (derived from model prefix by default, or overridden)
    _provider: str | None = field(
        default_factory=lambda: os.getenv("SILICA_PROVIDER", None)
    )

    # Custom OpenAI-compatible endpoint (provider="custom"): base URL + key.
    # Covers any server speaking the OpenAI API without a dedicated preset —
    # vLLM, llama.cpp, LocalAI, Jan, or a hosted vendor we don't preset.
    provider_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_PROVIDER_BASE_URL", "")
    )
    provider_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_PROVIDER_API_KEY", "")
    )

    # OpenRouter upstream-provider routing (agent/llm.py). Comma-separated
    # provider names (e.g. "DeepInfra,Together") pinned as the routing `order`
    # for openrouter/* models; unset → OpenRouter's default auto-routing (as now).
    openrouter_provider: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_PROVIDER", "")
    )

    # Distiller-only upstream-provider pin. Lets the constrained-decoding path
    # (kernel.prep_delegation.run_distiller) route to a different OpenRouter
    # provider than the interactive loop and the other workers. Falls back to
    # OPENROUTER_PROVIDER when unset, so a single pin still covers everything.
    openrouter_provider_distiller: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_PROVIDER_DISTILLER")
        or os.getenv("OPENROUTER_PROVIDER", "")
    )

    @property
    def provider(self) -> str:
        if self._provider is not None:
            return self._provider
        if self.model and "/" in self.model:
            prefix = self.model.split("/", 1)[0]
            if prefix in PROVIDER_PREFIXES:
                return prefix
        return "lmstudio"

    @provider.setter
    def provider(self, val: str) -> None:
        self._provider = val

    @property
    def distill_escalation_provider(self) -> str | None:
        """Escalation provider: explicit env wins, else derived from the model
        prefix (same rule as the main model), else lmstudio for a bare name,
        else None (get_provider then degrades the role to router)."""
        if self._distill_escalation_provider is not None:
            return self._distill_escalation_provider
        m = self.distill_escalation_model
        if not m:
            return None
        if "/" in m:
            prefix = m.split("/", 1)[0]
            if prefix in PROVIDER_PREFIXES:
                return prefix
        return "lmstudio"

    # --- Sub-agent worker model (leashed sub-agents run on a separate, smaller model) ---
    # The router (agent loop) uses `model`/`provider` above; sub-agents (dedup, refiner)
    # use these worker_* fields so they can run concurrently on a small local model.
    worker_model: str | None = field(
        default_factory=lambda: os.getenv("SILICA_WORKER_MODEL", None)
    )
    # Worker provider preset name; falls back to "lmstudio" when unset.
    worker_provider: str | None = field(
        default_factory=lambda: os.getenv("SILICA_WORKER_PROVIDER", None)
    )
    # Explicit API-key override for the worker model (endpoint comes from the preset).
    worker_api_key: str | None = field(
        default_factory=lambda: os.getenv("SILICA_WORKER_API_KEY", None)
    )

    # --- Distiller escalation model (Tier 2 cascade) ---
    # A VALIDATE rejection escalates the steer retry to this model instead of
    # re-steering the worker (UCCI-style cascade). Unset: escalation falls back
    # to the router model. Opt-out: set it equal to the worker model.
    distill_escalation_model: str | None = field(
        default_factory=lambda: os.getenv("SILICA_DISTILL_ESCALATION_MODEL", None)
    )
    _distill_escalation_provider: str | None = field(
        default_factory=lambda: os.getenv("SILICA_DISTILL_ESCALATION_PROVIDER", None)
    )


    subagent_max_concurrent: int = field(
        default_factory=lambda: int(os.getenv("SILICA_SUBAGENT_MAX_CONCURRENT", "3"))
    )
    # Global ceiling on concurrent worker-model LLM calls (the one true
    # concurrency budget; see ADR-0004). Sized to the worker backend
    # (API rate limit or local GPU slots).
    worker_max_concurrent: int = field(
        default_factory=lambda: int(os.getenv("SILICA_WORKER_MAX_CONCURRENT", "4"))
    )

    # Distiller prefetch width for /ingest (Tier 1 speed): how many chunk
    # distillations may be in flight at once. 1 = fully sequential. Default is 3
    # since the 2026-07-18 k=1-vs-k=3 staleness A/B (bench/kway_diff.py): a
    # lookahead chunk's staler ledger_digest diverged from a k=1 baseline no more
    # than a second k=1 run did (title agreement k1/k3 0.355 >= k1/k1 0.303) —
    # the staleness effect sits inside the pipeline's own run-to-run noise.
    distill_concurrency: int = field(
        default_factory=lambda: int(os.getenv("SILICA_DISTILL_CONCURRENCY", "3"))
    )

    # Tier 2 novelty gate (SAGE-style): a concept whose top vault candidate
    # scores at or above this cosine leaves the payload BEFORE chunking and
    # goes to the dedup-judge lane (deferred store + concurrent ternary judge).
    # 0 = gate off. Flip the default to 0.93 only after the bench A/B passes
    # (see docs spec 2026-07-18-ingest-tier2-cost-design).
    novelty_tau: float = field(
        default_factory=lambda: float(os.getenv("SILICA_NOVELTY_TAU", "0"))
    )

    # Vault path — used by the fs backend and for context.
    vault_path: str = field(
        default_factory=lambda: os.getenv("SILICA_VAULT", "")
    )

    # Obsidian vault display name (prompt fallback when no vault path is set).
    vault_name: str = field(
        default_factory=lambda: os.getenv("SILICA_VAULT_NAME", "")
    )

    # Personal-memory vault — the second recall lane (ADR-0019). Read-only at
    # query time: its (embed, cooccur) stores join the RRF fusion; writes never
    # route here. Empty ⇒ the default user vault (~/.silica/vault). When it
    # resolves to the SAME path as the active vault the lane abstains and
    # behavior is bit-identical to single-vault.
    memory_vault: str = field(
        default_factory=lambda: os.getenv("SILICA_MEMORY_VAULT", "")
    )

    # Capture of Silica's OWN sessions (capture.py), default off: opting in
    # deposits each conversation in the WAL, from which /nucleate distills
    # facts into the episodic store. Machine memory never becomes a note by
    # itself — promotion is the only path into the vault. /incognito turns it
    # off for the running session without touching this.
    capture_sessions: bool = field(
        default_factory=lambda: os.getenv("SILICA_CAPTURE_SESSIONS", "False").lower() in ("true", "1", "t")
    )

    # Episodic memory lane (kernel/episodic.py): wall-clock TTL in days from a
    # fact chain's last_seen (0 = never expire), and the distinct-run count at
    # which a key becomes a nucleation candidate in the digest.
    episodic_ttl_days: int = field(
        default_factory=lambda: int(os.getenv("SILICA_EPISODIC_TTL_DAYS", "90"))
    )
    episodic_nucleation_runs: int = field(
        default_factory=lambda: int(os.getenv("SILICA_EPISODIC_NUCLEATION_RUNS", "3"))
    )
    # Canonical-keys matcher cascade (fase 2): capture-time embed-snap
    # threshold on KEY embeddings, 0 = off. Probe-gated on LoCoMo
    # (bench/locomo_embed_identity_gates.md, tau window ~0.80-0.85); a nonzero
    # default requires the harness A/B to promote it.
    episodic_embed_snap_tau: float = field(
        default_factory=lambda: float(os.getenv("SILICA_EPISODIC_EMBED_SNAP_TAU", "0"))
    )
    # Relevance floor on the episodic embed leg (cosine). Without one, top-k
    # over `score > 0` ships the whole store on every query: measured on a
    # 11-fact store, "pasta recipe with tomatoes" recalled the same 10
    # AI-history facts as an on-topic query, ~520 tokens of noise per recall.
    # Calibration knob: 0.5 separates this embedder's off-topic ceiling (0.464
    # over 5 unrelated queries) from its true matches (0.598, 0.833). A
    # different embedder shifts the whole band — re-measure before trusting it.
    # 0 = off (pre-floor behavior).
    episodic_recall_floor: float = field(
        default_factory=lambda: float(os.getenv("SILICA_EPISODIC_RECALL_FLOOR", "0.5"))
    )

    # Driver backend: "fs" (default, filesystem-native, headless) or "ws" (the
    # Obsidian bridge plugin over a loopback WebSocket, PROTOCOL.md — installed
    # live by `silica connect`, never set here).
    backend: str = field(
        default_factory=lambda: os.getenv("SILICA_BACKEND", "fs")
    )

    # Obsidian WebSocket bridge (backend="ws"): port `silica connect` binds (0 →
    # OS picks a free one) and the shared token (empty → minted on first connect,
    # written to <vault>/.obsidian/silica-bridge.json). The wire contract is
    # PROTOCOL.md in github.com/kiycoh/obsidian-silica — change both sides together.
    ws_port: int = field(
        default_factory=lambda: int(os.getenv("SILICA_WS_PORT", "0"))
    )
    ws_token: str = field(
        default_factory=lambda: os.getenv("SILICA_WS_TOKEN", "")
    )

    # Inbox folder inside the vault — used to archive and blacklist staging files.
    inbox_dir: str = field(
        default_factory=lambda: os.getenv("SILICA_INBOX_DIR", "Inbox")
    )

    # PDF→Markdown converter (ADR-0011 provider seam):
    # "pymupdf" (default, AGPL, in the base install — 60 MB, no torch and no JVM,
    # reads the PDF outline for headings, but NO OCR), "mineru" (best fidelity and
    # the only OCR path; 3.8 GB of torch+CUDA plus model downloads, via the
    # `silica-agent[pdf]` extra), "docling" (MIT but its PDF pipeline hard-imports
    # docling-ibm-models, so torch is unavoidable), or "opendataloader"
    # (Apache-2.0, strong on complex tables and multi-column reading order, needs
    # a JVM). Non-PDF formats (DOCX/EPUB/…) always use pymupdf — the others only
    # take PDFs. An unmet provider errors clearly.
    pdf_provider: str = field(
        default_factory=lambda: os.getenv("SILICA_PDF_PROVIDER", "pymupdf")
    )

    # OCR languages for PDF conversion, comma-separated (split at point of use).
    # Only docling consumes it: mineru 3.x has no latin-script language option
    # (its default `ch` models cover latin), opendataloader only OCRs in its
    # generative `hybrid` mode, which we never enable, and pymupdf has no OCR at
    # all. Default keeps docling's European coverage and adds Italian; all
    # latin-script languages share one EasyOCR model, so the list is cheap.
    # Language detection can't replace this: for a scanned PDF there is no text
    # to detect from until OCR runs.
    pdf_ocr_lang: str = field(
        default_factory=lambda: os.getenv("SILICA_PDF_OCR_LANG", "en,it,fr,de,es")
    )

    # Tavily API key: the /web-search backstop when DuckDuckGo challenges us.
    # Empty is fine — DuckDuckGo is the primary lane and needs no key.
    tavily_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_TAVILY_API_KEY", "")
        or os.getenv("TAVILY_API_KEY", "")
    )

    # Maximum context tokens before the agent warns.
    max_context_tokens: int = field(
        default_factory=lambda: int(os.getenv("SILICA_MAX_CONTEXT", "60000"))
    )

    # Tool progress display level (REPL-runtime, cycled with /verbose)
    # off     — total silence, only the final response
    # new     — shows the tool name only when it changes
    # all     — every tool call with an args preview (default)
    # verbose — full args, truncated result, duration
    tool_progress: Literal["off", "new", "all", "verbose"] = field(
        default_factory=lambda: os.getenv("SILICA_TOOL_PROGRESS", "all")  # type: ignore
    )

    # Debug logging to stderr (--verbose / -v CLI flag, not cycled)
    debug_logging: bool = field(
        default_factory=lambda: os.getenv("SILICA_VERBOSE", "False").lower() in ("true", "1", "t")
    )

    # Shows the model's reasoning blocks (runtime toggle with /thinking)
    show_thinking: bool = field(
        default_factory=lambda: os.getenv("SILICA_SHOW_THINKING", "True").lower() in ("true", "1", "t")
    )

    # Runtime session state — updated by cli.py after each agent turn
    context_tokens: int = 0

    # Startup banner art (True → wordmark, False → plain one-liner)
    show_banner: bool = field(
        default_factory=lambda: os.getenv("SILICA_SHOW_BANNER", "True").lower() in ("true", "1", "t")
    )

    # Embedding model — used by silica/kernel/recall/embed.py (Phase 3)
    # Default targets a local llama-server (`llama-server -m ... --embedding`) or
    # LM Studio, whichever answers at the URL below — both speak the same
    # OpenAI-compatible /v1/embeddings shape, and a single-model server ignores
    # the `model` field anyway. "text-embedding-qwen3-embedding-4b" is LM
    # Studio's id for qwen3-embedding-4b; the id is cosmetic when llama-server is
    # what's actually listening. Example alternatives: "qwen3-embedding-8b",
    # "text-embedding-3-small" (OpenAI), "nomic-embed-text" (Ollama).
    embedding_model: str = field(
        default_factory=lambda: os.getenv(
            "SILICA_EMBEDDING_MODEL", "text-embedding-qwen3-embedding-4b"
        )
    )

    # Base URL for the embeddings endpoint — a local llama-server or LM Studio
    # instance by default.
    embedding_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_EMBEDDING_BASE_URL", "http://localhost:1234/v1")
    )

    # API key for embeddings endpoint (local runtimes ignore it; any non-empty
    # value satisfies the OpenAI SDK)
    embedding_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_EMBEDDING_API_KEY", "lm-studio")
    )

    # Cross-encoder reranker: the precision pass over the fused candidate pool.
    # Neither LM Studio nor Ollama can serve one (it scores a [query, document]
    # pair jointly, not an embedding), so the default here is a local llama-server
    # started with --reranking. get_reranker (agent/providers.py) tries it first
    # and falls back automatically, per call, to the in-process cross-encoder
    # from `pip install silica-agent[rerank]` when it's down — set both empty to
    # skip straight to that path, or leave the extra uninstalled too to disable
    # reranking outright (a no-op that preserves the pool's order).
    rerank_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_RERANK_BASE_URL", "http://localhost:1235/v1")
    )
    rerank_model: str = field(
        default_factory=lambda: os.getenv("SILICA_RERANK_MODEL", "bge-reranker-v2-m3-Q8_0")
    )
    rerank_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_RERANK_API_KEY", "lm-studio")
    )

    # Cosine similarity thresholds for dedup routing (Phase 5)
    # score >= sim_threshold_high → strong duplicate → patch existing note
    # score <= sim_threshold_low  → clearly new concept → write new note
    # between the two → ambiguous → deferred store
    sim_threshold_high: float = field(
        default_factory=lambda: float(os.getenv("SILICA_SIM_THRESHOLD_HIGH", "0.85"))
    )
    sim_threshold_low: float = field(
        default_factory=lambda: float(os.getenv("SILICA_SIM_THRESHOLD_LOW", "0.75"))
    )

    # Number of candidates to retrieve per note during dedup scan.
    # Higher values increase recall at negligible BLAS cost (search is a single
    # matrix-vector product). k=1 misses borderline secondary matches when the
    # primary match lands above τ_high and is discarded.
    dedup_scan_k: int = field(
        default_factory=lambda: int(os.getenv("SILICA_DEDUP_SCAN_K", "5"))
    )

    # Minimum title-only cosine similarity to promote a pair into the dedup
    # borderline window, regardless of the full-note score.
    # Set higher than sim_threshold_low (0.75) to avoid spurious matches between
    # generically related titles (e.g. "Python" / "Python async").
    sim_title_threshold: float = field(
        default_factory=lambda: float(os.getenv("SILICA_SIM_TITLE_THRESHOLD", "0.80"))
    )

    # Language for the co-occurrence graph stemmer + stopwords (kernel/cooccurrence.py).
    # "auto" (default) detects the vault language from its own text at build time
    # and freezes it into the index; set an explicit Snowball language to override.
    cooccurrence_lang: str = field(
        default_factory=lambda: os.getenv("SILICA_COOCCURRENCE_LANG", "auto")
    )

    # BM25 tf term in the co-occurrence ranking leg (docs/spec-cooccur-scoring.md).
    # Off by default: the probe gate (+4.02pp recall@10, +0.055 mrr, p=0.0015) covers
    # retrieval only, and the same seam feeds autolink, dedup, /map and collision
    # routing. Fase 2 (those surfaces) and fase 3 (answer-side) promote it, not this
    # flag. k1/b stay untuned module constants in relatedness.py, by spec section 8.
    cooccur_bm25: bool = field(
        default_factory=lambda: os.getenv("SILICA_COOCCUR_BM25", "False").lower() in ("true", "1", "t")
    )

    # Invocation-time index sweep (kernel/recall/sync.py): detect out-of-band
    # note edits/creates/deletes before the indexes are read. Off = you own
    # index freshness via explicit /embed, /cooccur, /lexical (eval harnesses
    # that need byte-identical retrieval across runs set this off).
    index_sweep: bool = field(
        default_factory=lambda: os.getenv("SILICA_INDEX_SWEEP", "True").lower() in ("true", "1", "t")
    )

    # Salience gate (Phase 2.05): concept kept only if cosine(concept, doc_centroid) >= threshold
    sim_threshold_theme: float = field(
        default_factory=lambda: float(os.getenv("SILICA_SIM_THRESHOLD_THEME", "0.35"))
    )

    domain: str | None = field(
        default_factory=lambda: os.getenv("SILICA_DOMAIN") or None
    )

    # Mindmap (/map): radial map rooted on one note. Node cap is "breathing room"
    # (readable map, not a hairball); latent_k = neighbours asked of the
    # relatedness facade; hops = wikilink BFS depth from the root.
    mindmap_max_nodes: int = field(
        default_factory=lambda: int(os.getenv("SILICA_MINDMAP_MAX_NODES", "35"))
    )
    mindmap_latent_k: int = field(
        default_factory=lambda: int(os.getenv("SILICA_MINDMAP_LATENT_K", "10"))
    )
    mindmap_hops: int = field(
        default_factory=lambda: int(os.getenv("SILICA_MINDMAP_HOPS", "2"))
    )

    # Git commit safety net for docs/ writes. "off" (default) → never commit;
    # "auto" → after each write batch, commit the touched docs/ paths with a
    # structured message. Additive to the undo journal (ADR-0002), never a
    # replacement. Only takes effect when the vault sits inside a git repo.
    git_commit: Literal["off", "auto"] = field(
        default_factory=lambda: os.getenv("SILICA_GIT_COMMIT", "off")  # type: ignore
    )

    @property
    def verbose(self) -> bool:
        return self.debug_logging

    @verbose.setter
    def verbose(self, v: bool) -> None:
        self.debug_logging = v

    def __post_init__(self):
        def _ensure_prefix(model: str | None, provider: str | None) -> str | None:
            if model and provider and not model.startswith(f"{provider}/"):
                if provider in PROVIDER_PREFIXES:
                    return f"{provider}/{model}"
            return model

        self.model = _ensure_prefix(self.model, self._provider) or self.model
        self.worker_model = _ensure_prefix(self.worker_model, self.worker_provider) or self.worker_model
        self.distill_escalation_model = _ensure_prefix(self.distill_escalation_model, self._distill_escalation_provider) or self.distill_escalation_model


CONFIG = SilicaConfig()
