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

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

# Captured at package import (silica/__init__.py), before any third-party
# load_dotenv can blur an exported pin into a .env value. Re-exported here
# because this is where every caller expects to find it.
from silica import SHELL_ENV, VAULT_PINNED  # noqa: E402,F401

# The one ambient .env. Layered *under* the real environment (override=False),
# so a key the shell exported stays a deliberate per-invocation pin. It is the
# user's own file and follows them between folders; an installed silica has no
# .env beside its package, so without it every setting evaporated the moment you
# ran `silica` outside a checkout.
#
# There was a second layer above this one: find_dotenv(usecwd=True), the .env
# found by walking UP from the working directory. Removed, because it has no
# provenance. It is not "silica's project file" — it is the .env of whatever
# repository the shell happens to sit in, and it could repoint the model, the
# endpoints, the vault and the API keys of a user who only cd'd there. The
# *_SERVE_CMD keys used to carry an exception for exactly that reason (it was
# arbitrary command execution); the exception is gone with the layer that needed
# it. Settings that belong to one directory are that directory's job to export —
# `set -a; source .env; set +a` — which is explicit and outranks this file.
USER_ENV = Path.home() / ".silica" / ".env"


def in_config_dir(path: str | Path) -> bool:
    """True when `path` resolves inside ~/.silica, Silica's own state: the .env
    with every API key, the narration of every chat, the runs. A vault opened
    at $HOME contains that folder, and containment in the vault does not
    exclude it: on 2026-10-11 silica_read served `.silica/.env` whole and
    silica_query_table a JSON under it. Read tools that take a model-named path
    ask this after their vault check."""
    return Path(path).expanduser().resolve().is_relative_to(USER_ENV.parent.resolve())


def load_user_env(path: Path) -> None:
    """Layer `path` under the shell, above anything a foreign loader injected.

    override=False alone was not enough: litellm/__init__.py calls
    load_dotenv() at import and find_dotenv walks up from the venv to the
    repo checkout, so `<repo>/.env` reached os.environ BEFORE this file
    whenever a caller imported litellm (the CLI does) ahead of silica.config.
    Measured 2026-09-02: the REPL ran on a retired model pinned only in the
    checkout's .env while a plain `import silica.config` said the right one.
    SHELL_ENV (captured at package import, before any third-party code) is
    what separates a deliberate export from that injection: an exported key
    still wins, a key that appeared behind silica's back loses to the user's
    own file.
    """
    import silica as _pkg  # SHELL_ENV read at call time: tests replace it

    for key, value in (dotenv_values(path) if path.exists() else {}).items():
        if value is None:
            continue
        if key == "SILICA_VAULT":
            # The vault is a per-launch fact: silica curates the folder it is
            # launched in, and only an export pins one (VAULT_PINNED). Read
            # from this file, the same key served every entry point that never
            # ran the CLI bootstrap (benches, probes, `python -c`) a vault the
            # user was not in: measured 2026-09-02, an evidence dump ran
            # against the wrong embed store. Ignored since 2026-09-03. An
            # export already does what the warning asks for, so it stays quiet.
            if os.environ.get("SILICA_VAULT", "").strip():
                continue
            logging.getLogger(__name__).warning(
                "SILICA_VAULT in %s is ignored: silica curates the folder it is "
                "launched in. Delete the line, or export SILICA_VAULT to pin a vault.",
                path)
            continue
        if key in _pkg.SHELL_ENV and key in os.environ:
            continue
        os.environ[key] = value


load_user_env(USER_ENV)

# Silica's ledger of the SILICA_* keys it owns, derived from the two sources
# above rather than snapshotted out of os.environ — a snapshot would be right
# only if nothing had polluted os.environ yet, which makes its correctness a
# question about import order.
#
# Anything under the prefix that is in os.environ but not here did not come from
# a source silica reads. load_dotenv() is a function third parties call at their
# own import, and litellm calls it. With override=False it cannot change a key
# silica already set, but it can ADD one silica deliberately left unset, taken
# from the .env of whatever directory the process happens to sit in — which
# would restore the removed layer, one os.getenv call site at a time.
_SILICA_KEYS = {k for k in SHELL_ENV if k.startswith("SILICA_")}
if USER_ENV.exists():
    _SILICA_KEYS |= {
        k for k in dotenv_values(USER_ENV) if k and k.startswith("SILICA_")
    }


def drop_foreign_env() -> list[str]:
    """Take back out the SILICA_* keys that appeared behind silica's back.

    Called immediately after every `import litellm`; idempotent, and a plain
    dict scan, so a call site that runs often costs nothing measurable.
    """
    foreign = sorted(
        k for k in os.environ if k.startswith("SILICA_") and k not in _SILICA_KEYS
    )
    for key in foreign:
        del os.environ[key]
    return foreign


def claim_env(updates: dict[str, str]) -> None:
    """Make settings live in this process and record them as silica's own.

    The settings panel and the wizard both write a saved key into os.environ so
    the running session sees it. Without the record the next drop_foreign_env()
    would read those keys as injected and take them straight back out.
    """
    os.environ.update(updates)
    _SILICA_KEYS.update(k for k in updates if k.startswith("SILICA_"))


# One spelling of true for every setting silica reads. Kept as a name because
# hand-written call sites drifted apart once: one of them accepted "yes" while
# SILICA_VERBOSE=yes did not work.
_TRUE_WORDS = ("true", "1", "t")


def env_flag(key: str, default: bool) -> bool:
    """Read a boolean setting from the environment.

    True is spelled "true"/"1"/"t" case-insensitively; anything else the user
    writes (including "no", "off" and typos) reads as False, which is what
    every hand-written call site here already did.
    """
    return os.getenv(key, "true" if default else "false").lower() in _TRUE_WORDS


def hosted() -> bool:
    """Hosted mode (ADR-0086 point 2): one variable, set only by the tenant's
    unit. Not an env_flag: "true" or "t" in a user's .env must not switch a
    laptop into it. Read per call: the flag is the process environment's."""
    return os.environ.get("SILICA_HOSTED", "").strip() == "1"


# Provider prefixes that map a `prefix/model` string to an endpoint and get
# auto-prefixed onto a bare model. Single source for the three checks below
# (provider, distill_escalation_provider, ensure_prefix). "custom" routes to
# SILICA_PROVIDER_BASE_URL/_API_KEY; the rest to PROVIDER_PRESETS in
# agent.providers (kept a subset of this set — see test_providers).
PROVIDER_PREFIXES = frozenset({
    "openrouter", "lmstudio", "ollama", "gemini",
    "openai", "groq", "deepseek", "mistral", "xai", "custom",
})


def ensure_prefix(model: str, provider: str | None) -> str:
    """`provider/model`, but only for a provider whose prefix routes somewhere.

    litellm resolves the endpoint from the prefix alone, so a prefix outside
    PROVIDER_PREFIXES does not name an endpoint — it just makes a bare id that
    would have worked unroutable. Those keep the id as written.

    Module-level because agent.providers.get_provider re-derives the same prefix
    from the same fields: two copies of this rule drifted apart once already
    (the copy without the guard emitted `vllm/qwen`).
    """
    if model and provider and provider in PROVIDER_PREFIXES:
        if not model.startswith(f"{provider}/"):
            return f"{provider}/{model}"
    return model

# Hosted providers in fallback order: (API key env var, model ids best first).
# One table, three readers — `model_from_env` takes the head of the first entry
# whose key is exported, the wizard offers the whole list as a pick-list, and
# agent.providers derives its PROVIDER_PRESETS hosted rows from it. Lives here
# rather than beside PROVIDER_PRESETS because config is imported on every path,
# including the ones that must not pay for the openai SDK.
# The wizard validates this list against each provider's live /models before
# offering it (wizard._live_hosted_models), so stale ids stop being offered.
# ponytail: the static heads still back model_from_env, which must stay
# HTTP-free on config load — a head can rot for env-derived defaults; refresh
# it when a provider retires a model.
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


DEFAULT_EMBEDDING_MODEL = "embeddinggemma-2"

# A cosine threshold belongs to an embedder, not to the vault (ADR-0078). On the
# founder's 681-note vault (2026-10-07) any two notes sit at a median cosine of
# 0.653 under EmbeddingGemma 2 and 0.298 under Qwen3-Embedding-0.6B, so the 0.85
# auto-merge bar set on Qwen would have called 55% of the notes a duplicate of
# their nearest neighbour instead of 9%. Each value below cuts the share of its
# own distribution that the shipped default cut under Qwen3-Embedding-0.6B
# (quantile-mapped; script and numbers in docs/evaluation/embeddinggemma-2-2026-10-07.md).
# Keyed by a substring of SILICA_EMBEDDING_MODEL; an id that matches nothing
# keeps the Qwen-era defaults, and an exported value always wins. `dim` is the
# width the doctor expects back, which is how it tells a server still loading
# Qwen apart from the model the thresholds were set for. HIGH and LOW were then
# gated on ADR-0030's risk-coverage probe, 60 synthetic duplicates per embedder
# (docs/evaluation/collision-gate-per-embedder-2026-10-08.md): Gemma 2's held,
# jina's LOW moved.
EMBEDDER_DEFAULTS: dict[str, dict[str, str]] = {
    "embeddinggemma-2": {
        "dim": "768",
        # The model card's retrieval prompts; the document side has no title
        # slot here because _note_text already leads with the title.
        "SILICA_EMBEDDING_QUERY_PREFIX": "task: search result | query: ",
        "SILICA_EMBEDDING_DOCUMENT_PREFIX": "title: none | text: ",
        "SILICA_SIM_THRESHOLD_HIGH": "0.911",
        "SILICA_SIM_THRESHOLD_LOW": "0.844",
        "SILICA_SIM_TITLE_THRESHOLD": "0.876",
        "SILICA_SIM_THRESHOLD_THEME": "0.708",
        "SILICA_AUTOLINK_MIN_SIM": "0.654",
        "SILICA_EPISODIC_RECALL_FLOOR": "0.724",
    },
    # CC-BY-NC, so a personal setting and never the default (ADR-0078). Its
    # cosines sit BELOW Qwen's (all-pairs median 0.178 vs 0.298), so the Qwen
    # bars were too strict on it: AUTOLINK at 0.30 kept 9% of pairs, not 49%.
    # Same quantile map on the same 681 notes, 2026-10-08. The episodic floor
    # stays 0.5: the mapped value is 0.520 [0.470, 0.597] over 200 draws. LOW is
    # the gate's, not the map's: the mapped 0.647 merged and leaked nothing but
    # sent 442 of 738 concepts to the judge; 0.71 still makes no error and sends 282.
    "jina-embeddings-v5-text-small": {
        "dim": "1024",
        "SILICA_EMBEDDING_QUERY_PREFIX": "Query: ",
        "SILICA_EMBEDDING_DOCUMENT_PREFIX": "Document: ",
        "SILICA_SIM_THRESHOLD_HIGH": "0.814",
        "SILICA_SIM_THRESHOLD_LOW": "0.71",
        "SILICA_SIM_TITLE_THRESHOLD": "0.709",
        "SILICA_SIM_THRESHOLD_THEME": "0.208",
        "SILICA_AUTOLINK_MIN_SIM": "0.180",
        "SILICA_EPISODIC_RECALL_FLOOR": "0.5",
    },
    # Perplexity's 4B through OpenRouter, the hosted index since 2026-10-23: an
    # API, so the 2-core host spends no CPU on it. No role prompts, the model
    # card says to embed text as it is. Its cosines sit below Qwen's (note to
    # nearest note median 0.670 vs 0.731), and a model id with no row runs
    # Qwen's bars: TITLE 0.80 would have kept 5% of titles, not 69%. Same
    # quantile map on the same 681 notes; the episodic floor is Gemma 2's 0.724
    # carried by the share of true matches it keeps (89.7% on 300 student
    # questions), which lets 2.6% of off-topic notes through against Gemma's 5.6%.
    # HIGH and LOW passed ADR-0030's gate on the same 738 concepts as the other
    # rows: no false merge, 0 of 60 duplicates leak (Gemma 2: 6), 408 go to the
    # judge (Gemma 2: 457); LOW stays 0.011 under the lowest duplicate's 0.651
    # (docs/evaluation/pplx-embed-4b-calibration-2026-10-23.md). Keyed so the
    # 0.6b, never calibrated, does not match.
    "pplx-embed-v1-4b": {
        "dim": "2560",
        "SILICA_EMBEDDING_QUERY_PREFIX": "",
        "SILICA_EMBEDDING_DOCUMENT_PREFIX": "",
        "SILICA_SIM_THRESHOLD_HIGH": "0.785",
        "SILICA_SIM_THRESHOLD_LOW": "0.640",
        "SILICA_SIM_TITLE_THRESHOLD": "0.508",
        "SILICA_SIM_THRESHOLD_THEME": "0.189",
        "SILICA_AUTOLINK_MIN_SIM": "0.204",
        "SILICA_EPISODIC_RECALL_FLOOR": "0.52",
    },
}


def embedder_defaults(model: str | None = None) -> dict[str, str]:
    """The EMBEDDER_DEFAULTS row for `model` (the configured embedder when None), or {}."""
    model = (model if model is not None else os.getenv("SILICA_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)).lower()
    return next((row for key, row in EMBEDDER_DEFAULTS.items() if key in model), {})


def embedder_env(key: str, default: str) -> str:
    """os.getenv, falling back to the configured embedder's own default before
    the Qwen-era one. An exported empty string is a value (it clears a prefix)."""
    value = os.getenv(key)
    return value if value is not None else embedder_defaults().get(key, default)


# The sampling a local chat model was post-trained with, sent on the `custom/`
# route when the caller sets no temperature (ADR-0080). Until 2026-10-08 that
# route sent none, so llama-server's own defaults ran, not the model card's.
# Keyed by a substring of the model id, first match wins; the inner key is
# whether the model thinks on this call. Read 2026-10-08 from the cards:
# Qwen3.5 4B/9B/27B "general tasks" rows (the 397B card says 0.6 for thinking,
# the small cards keep 0.6 for precise coding only); Gemma 4 from the official
# generation_config.json, one set for both modes. presence_penalty is left out:
# the cards ask 1.5 and warn it can mix languages, and the vaults are Italian.
CHAT_SAMPLING_DEFAULTS: dict[str, dict[bool, dict[str, float]]] = {
    "qwen3.5": {
        True: {"temperature": 1.0, "top_p": 0.95, "top_k": 20},
        False: {"temperature": 0.7, "top_p": 0.8, "top_k": 20},
    },
    "gemma-4": {
        True: {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
        False: {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
    },
}


def chat_sampling(model: str, *, thinking: bool) -> dict[str, float]:
    """The card's sampling for `model` in this mode, or {} for a model not in
    CHAT_SAMPLING_DEFAULTS. `gemma4` (Ollama's spelling) folds onto `gemma-4`."""
    model = model.lower().replace("gemma4", "gemma-4")
    row = next((r for key, r in CHAT_SAMPLING_DEFAULTS.items() if key in model), {})
    return dict(row.get(thinking, {}))


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

    # The same routing, per model: a JSON object from model id (with or without
    # the openrouter/ prefix) to OpenRouter's `provider` object, sent as written,
    # e.g. {"qwen/qwen3.7-flash-20260727": {"order": ["alibaba/us-east-1"],
    # "allow_fallbacks": false}, "z-ai/glm-5.3-flash": {"zdr": true}}. A model
    # it names ignores OPENROUTER_PROVIDER, which follows every model, fallbacks
    # included: the pilot's Alibaba pin sent its GLM fallback to an endpoint GLM
    # does not have (2026-10-13).
    openrouter_provider_by_model: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_PROVIDER_BY_MODEL", "")
    )

    # Models tried in order when a call's own model fails for a reason the
    # endpoint owns: down, overloaded, rate-limited past its retries, a key it
    # refuses, a model it does not have (agent/llm.py, ADR-0063).
    # Comma-separated litellm ids, e.g. "openrouter/deepseek/deepseek-v4-flash,
    # lmstudio/qwen3.5-9b"; empty keeps every call on its own model, as before.
    fallback_models: str = field(
        default_factory=lambda: os.getenv("SILICA_FALLBACK_MODELS", "")
    )

    # Distiller-only upstream-provider pin. Lets the constrained-decoding path
    # (kernel.prep_delegation.run_distiller) route to a different OpenRouter
    # provider than the interactive loop and the other workers. Falls back to
    # OPENROUTER_PROVIDER when unset, so a single pin still covers everything.
    openrouter_provider_distiller: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_PROVIDER_DISTILLER", "")
        or os.getenv("OPENROUTER_PROVIDER", "")
    )

    # Typed-decision model (agent/decide.py): TypeSafe Jev through OpenRouter's
    # Decisions endpoint, in front of the form sniff. Empty = off, and the
    # sniff runs its LLM call as before. Pin a dated id (a reply names
    # "typesafe/jev-1.13-20260917"), never "~typesafe/jev-latest": the
    # acceptance threshold in kernel/forms.py was measured against one model.
    decision_model: str = field(
        default_factory=lambda: os.getenv("SILICA_DECISION_MODEL", "")
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
    # Which DELEGATE input document-shaped sources get: "outline" (the model
    # reads the source whole and names the ideas, kernel/outline.py) or
    # "keyphrase" (recon + payload windows). Measured 2026-09-02 on two SVM
    # lectures: outline 100% section provenance, 37 typed edges, 7 calls;
    # keyphrase 0%, 0, and 53 min of wall clock. Memory forms (clip,
    # promotion) stay on keyphrase whatever this says (outline.lane_for).
    nucleate_lane: str = field(
        default_factory=lambda: os.getenv("SILICA_NUCLEATE_LANE", "outline")
    )
    # Worker provider preset name; falls back to "lmstudio" when unset.
    worker_provider: str | None = field(
        default_factory=lambda: os.getenv("SILICA_WORKER_PROVIDER", None)
    )
    # Explicit API-key override for the worker model (endpoint comes from the preset).
    worker_api_key: str | None = field(
        default_factory=lambda: os.getenv("SILICA_WORKER_API_KEY", None)
    )

    # --- Judge model (epistemic-state spec M7) ---
    # The verdicts (residue coverage in kernel/residue.py, dedup in
    # capabilities/dedup.py) on a model other than the writer, so a verdict is
    # not the writer grading itself. Unset: every judge keeps the role it had
    # (residue the router, dedup the worker), byte-identical behaviour.
    judge_model: str | None = field(
        default_factory=lambda: os.getenv("SILICA_JUDGE_MODEL", None)
    )
    judge_provider: str | None = field(
        default_factory=lambda: os.getenv("SILICA_JUDGE_PROVIDER", None)
    )

    # --- Vision model (ADR-0077 amendment 2026-10-21) ---
    # Where images go when the chat model cannot see: DeepSeek V4 Flash 0731
    # won the hosted chat A/B of 2026-10-21 and takes text only. Unset, images
    # go to the chat model as ADR-0077 decided. The provider defaults to the
    # chat's (see get_provider), so the hosted case is one variable.
    vision_model: str | None = field(
        default_factory=lambda: os.getenv("SILICA_VISION_MODEL", None)
    )
    vision_provider: str | None = field(
        default_factory=lambda: os.getenv("SILICA_VISION_PROVIDER", None)
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
        default_factory=lambda: env_flag("SILICA_CAPTURE_SESSIONS", False)
    )

    # User memory (ADR-0076): the two files the chat seed shows and only an
    # explicit ask writes. Default on, unlike capture above: nothing lands in
    # them unless the user asked, so on costs nothing until they do. Off pauses
    # (no snapshot, no write) and keeps the files; the Settings editor and its
    # reset still work, because erasing must not depend on this switch.
    user_memory: bool = field(
        default_factory=lambda: env_flag("SILICA_MEMORY", True)
    )

    # Episodic memory lane (kernel/recall/episodic.py, ADR-0057): TTL in LIVED
    # days (days the lane captured or delivered a fact) since a chain head was
    # last captured or used, 0 = never expire; and the distinct-day count at
    # which a key becomes a nucleation candidate in the digest. The second knob
    # counted run ids until 2026-10-05: its old env name still reads, so a value
    # set then keeps working.
    episodic_ttl_days: int = field(
        default_factory=lambda: int(os.getenv("SILICA_EPISODIC_TTL_DAYS", "90"))
    )
    episodic_nucleation_days: int = field(
        default_factory=lambda: int(str(os.getenv("SILICA_EPISODIC_NUCLEATION_DAYS")
                                        or os.getenv("SILICA_EPISODIC_NUCLEATION_RUNS", "3")))
    )
    # Relevance floor on the episodic embed leg (cosine). Without one, top-k
    # over `score > 0` ships the whole store on every query: measured on a
    # 11-fact store, "pasta recipe with tomatoes" recalled the same 10
    # AI-history facts as an on-topic query, ~520 tokens of noise per recall.
    # Calibration knob: 0.5 separates Qwen3-Embedding's off-topic ceiling (0.464
    # over 5 unrelated queries) from its true matches (0.598, 0.833). A
    # different embedder shifts the whole band: under EmbeddingGemma 2 every
    # off-topic query clears 0.5, hence its own row in EMBEDDER_DEFAULTS.
    # 0 = off (pre-floor behavior).
    episodic_recall_floor: float = field(
        default_factory=lambda: float(embedder_env("SILICA_EPISODIC_RECALL_FLOOR", "0.5"))
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
    # "pdfium" (default, BSD/Apache, in the base install — one ~3 MB wheel, no
    # torch and no JVM, reads the PDF outline for headings, but NO OCR), "mineru"
    # (best fidelity and the only OCR path; `pip install 'mineru[full]'`,
    # several GB of torch+CUDA+vLLM plus model downloads), "docling" (MIT but its PDF
    # pipeline hard-imports docling-ibm-models, so torch is unavoidable), or
    # "opendataloader" (Apache-2.0, strong on complex tables and multi-column
    # reading order, needs a JVM). Non-PDF formats (DOCX/EPUB/FB2) have their own
    # in-process readers — the providers only take PDFs. All three non-default
    # providers are binaries the user installs; none is a dependency of Silica,
    # and the doctor's converters row reports whether mineru is on PATH. The
    # pre-2026-08-31 value "pymupdf" is accepted as an alias of the default
    # (convert.resolve_pdf_provider). An unmet provider errors clearly.
    pdf_provider: str = field(
        default_factory=lambda: os.getenv("SILICA_PDF_PROVIDER", "pdfium")
    )

    # OCR languages for PDF conversion, comma-separated (split at point of use).
    # Only docling consumes it: mineru 3.x has no latin-script language option
    # (its default `ch` models cover latin), opendataloader only OCRs in its
    # generative `hybrid` mode, which we never enable, and pdfium has no OCR at
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
        default_factory=lambda: env_flag("SILICA_VERBOSE", False)
    )

    # Shows the model's reasoning blocks (runtime toggle with /thinking)
    show_thinking: bool = field(
        default_factory=lambda: env_flag("SILICA_SHOW_THINKING", True)
    )

    # Runtime session state — updated by cli.py after each agent turn
    context_tokens: int = 0

    # Startup banner art (True → wordmark, False → plain one-liner)
    show_banner: bool = field(
        default_factory=lambda: env_flag("SILICA_SHOW_BANNER", True)
    )

    # Graph viewer: the drifting particles on the GAP and SIMILAR edges (both
    # renderers), and the crystal rig in 3D — flat shading, the two lights, the
    # camera-following fog. Off in either case is the bundle's own default, and
    # off costs nothing: particles alone hold the canvas awake at IDLE_FPS
    # forever, which on a settled graph is the largest standing cost in that view.
    graph_particles: bool = field(
        default_factory=lambda: env_flag("SILICA_GRAPH_PARTICLES", True)
    )
    graph_shading: bool = field(
        default_factory=lambda: env_flag("SILICA_GRAPH_SHADING", True)
    )

    # A sentence the worker model writes about what the vault holds, which
    # silica_vaults hands the agent when it picks among vaults. It was the chat
    # landing's second line until the greeting took the landing (ADR-0048,
    # amendment 2026-10-22). It costs a call, so it can be switched off.
    vault_brief: bool = field(
        default_factory=lambda: env_flag("SILICA_VAULT_BRIEF", True)
    )

    # What the new chat calls you, "Hi <name>." in the web UI's greeting. Asked
    # for rather than derived: the address an account signs in with is not a
    # name, and a name guessed from one greets somebody else. Empty greets no
    # one by name.
    user_name: str = field(default_factory=lambda: os.getenv("SILICA_USER_NAME", "").strip())

    # Web UI palette: "auto" follows the OS, "dark" and "light" pin it. Only the
    # browser can answer "auto", so the server ships the preference and a script
    # in <head> resolves it to a concrete data-theme before first paint. The
    # terminal UI has its own palette and does not read this.
    theme: str = field(default_factory=lambda: os.getenv("SILICA_THEME", "auto").lower())

    # Embedding model — used by silica/kernel/recall/embed.py (Phase 3)
    # Default targets a local llama-server (`llama-server -m ... --embedding`) or
    # LM Studio, whichever answers at the URL below — both speak the same
    # OpenAI-compatible /v1/embeddings shape, and a single-model server ignores
    # the `model` field anyway. The id is cosmetic when llama-server is what's
    # actually listening, but it IS the store's identity: the embed signature
    # hashes it, so changing it re-embeds every note. It also picks the row of
    # EMBEDDER_DEFAULTS that sets the prefixes and every cosine threshold below.
    # EmbeddingGemma 2 (Apache-2.0, 2026-10-06) and not Qwen3-Embedding-0.6B
    # since 2026-10-07 (ADR-0078): higher nDCG@10 on both Italian retrieval sets
    # measured, and 310 MB at Q8_0 against 640 MB. Its image and audio encoders
    # are not why: both lost to their own text (ADR-0078). It needs a llama.cpp
    # built after 2026-10-06 (PR #30054).
    # Qwen keeps its old thresholds: "text-embedding-qwen3-embedding-0.6b"
    # (LM Studio's id) matches no row. Other alternatives: "text-embedding-3-small"
    # (OpenAI), "nomic-embed-text" (Ollama).
    embedding_model: str = field(
        default_factory=lambda: os.getenv("SILICA_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    )

    # Text prepended to every query / every document before embedding, for
    # asymmetric embedders trained with role prompts (jina-embeddings-v5
    # retrieval: "Query: " / "Document: "). The configured embedder's own pair
    # from EMBEDDER_DEFAULTS, else empty: Qwen3-Embedding ran without its query
    # instruction because adding it lowered LoCoMo recall
    # (docs/evaluation/qwen3-query-instruction-2026-09-27.md). The document
    # prefix is part of the store identity (embed.embedder_identity), so
    # changing it re-embeds every note like a model swap does.
    embedding_query_prefix: str = field(
        default_factory=lambda: embedder_env("SILICA_EMBEDDING_QUERY_PREFIX", "")
    )
    embedding_document_prefix: str = field(
        default_factory=lambda: embedder_env("SILICA_EMBEDDING_DOCUMENT_PREFIX", "")
    )

    # Base URL for the embeddings endpoint — a local llama-server or LM Studio
    # instance by default.
    embedding_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_EMBEDDING_BASE_URL", "http://localhost:1234/v1")
    )
    # Where search queries are embedded, when not at embedding_base_url (empty).
    # llama-server queues requests first in, first out, so a query sent during
    # a 64-input index batch waits for the rest of it. Measured 2026-10-13,
    # EmbeddingGemma 2 Q8_0 on 2 threads of 4 pinned cores, batches of 180 s:
    # a query took 0.17 s on an idle server, p50 130 s and p95 161 s behind
    # batches, and p95 0.49 s on a second 1-thread server beside the busy one.
    # Same weights, so the vectors and the store do not change.
    embedding_query_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_EMBEDDING_QUERY_BASE_URL", "")
    )

    # API key for embeddings endpoint (local runtimes ignore it; any non-empty
    # value satisfies the OpenAI SDK)
    embedding_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_EMBEDDING_API_KEY", "lm-studio")
    )

    # Seconds one embeddings call may take to answer. 60 fits a GPU; a CPU
    # embedder needs the whole batch's time (OpenAIEmbedder says why). `or`
    # and not a getenv default: compose hands over "" when the name is unset.
    embedding_timeout: float = field(
        default_factory=lambda: float(os.getenv("SILICA_EMBEDDING_TIMEOUT") or 60)
    )

    # Cross-encoder reranker: the precision pass over the fused candidate pool.
    # Neither LM Studio nor Ollama can serve one (it scores a [query, document]
    # pair jointly, not an embedding), so the default here is a local llama-server
    # started with --reranking. The [rerank] extra serves its own on this URL
    # (agent/rerank_serve.py, ADR-0073), which the wizard points it at. Down,
    # loading or empty, the pass is skipped and the pool's order stands.
    rerank_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_RERANK_BASE_URL", "http://localhost:1235/v1")
    )
    rerank_model: str = field(
        default_factory=lambda: os.getenv("SILICA_RERANK_MODEL", "bge-reranker-v2-m3-Q8_0")
    )
    rerank_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_RERANK_API_KEY", "lm-studio")
    )
    # Seconds one rerank call may take. 5 fits a GPU; a CPU scorer needs more
    # (26 s for 15 passages, ADR-0054) or it abstains on every call. The server
    # sheds a request still queued at half this (rerank_serve._Served.wait).
    rerank_timeout: float = field(
        default_factory=lambda: float(os.getenv("SILICA_RERANK_TIMEOUT") or 5)
    )
    # The weights the [rerank] server loads: a HuggingFace repo id or a local
    # directory. Empty = rerank_serve.LOCAL_RERANK_MODEL, the one the product
    # ships. Exists for personal stacks whose best reranker cannot be the default
    # (jina-reranker v3.5 is CC-BY-NC, see rerank_serve._LISTWISE_RERANKERS).
    local_rerank_model: str = field(
        default_factory=lambda: os.getenv("SILICA_LOCAL_RERANK_MODEL", "")
    )

    # Speech-to-text: one endpoint for both lanes — the web GUI's dictation
    # button and /convert's media transcription (audio/video -> inbox note),
    # unless stt_dictation_base_url below gives dictation its own.
    # Any OpenAI-compatible /audio/transcriptions server (whisper.cpp's
    # whisper-server, faster-whisper-server, OpenAI itself). Next port after
    # embeddings (1234) and rerank (1235); SILICA_STT_SERVE_CMD starts it.
    # Empty turns the dictation button off outright.
    # The SILICA_ASR_* keys are the pre-merge spelling of this family and are
    # still honoured, so an existing .env keeps working; SILICA_STT_* wins.
    stt_base_url: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_BASE_URL", "")
        or os.getenv("SILICA_ASR_BASE_URL", "")
        or "http://localhost:1236/v1"
    )
    # Dictation's own endpoint. whisper-server transcribes one request at a time
    # (one mutex around inference, examples/server/server.cpp), so on a server
    # shared with /convert a 30 s clip waits behind a whole lecture. Unset means
    # stt_base_url, the one endpoint both lanes shared until 2026-10-13. Empty
    # means no endpoint for dictation: the in-process and browser lanes remain.
    # None and not a copy of stt_base_url, so the fallback follows that value
    # when it changes after start; read it through agent.stt.dictation_endpoint.
    stt_dictation_base_url: str | None = field(
        default_factory=lambda: os.environ.get("SILICA_STT_DICTATION_BASE_URL")
    )
    # Cosmetic against whisper.cpp, which serves whatever model it was started
    # with; a hosted endpoint needs the real id.
    stt_model: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_MODEL", "")
        or os.getenv("SILICA_ASR_MODEL", "")
        or "whisper-1"
    )
    # whisper-server assumes English when a request names no language, so a vault
    # dictated in any other one comes back translated instead of transcribed.
    # "auto" asks it to detect; a fixed code ("it", "en") is steadier on short
    # clips. Dictation and /convert both send it through. whisper.cpp detects
    # once, on the first 30 s of the file, and keeps that language to the end,
    # so a lecture that opens on silence or chatter can come out in the wrong
    # one: a vault that only ever records one language should fix the code.
    stt_lang: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_LANG", "")
        or os.getenv("SILICA_ASR_LANG", "")
        or "auto"
    )
    stt_api_key: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_API_KEY", "lm-studio")
    )

    # "endpoint" (the server above) or "whispercpp" (spawn the CLI locally).
    # /convert honours this; dictation always goes to the endpoint, since the
    # browser is already talking to the server.
    stt_provider: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_PROVIDER", "")
        or os.getenv("SILICA_ASR_PROVIDER", "")
        or "endpoint"
    )
    # Path to (or name on PATH of) the whisper.cpp CLI, for stt_provider=whispercpp.
    # Upstream renamed `main` to `whisper-cli` in 2024; both names are tried.
    stt_whispercpp_bin: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_WHISPERCPP_BIN", "")
        or os.getenv("SILICA_ASR_WHISPERCPP_BIN", "")
    )
    # whisper.cpp needs an explicit model file (`-m`); there is no default it can
    # find on its own, so this is required for that provider.
    stt_whispercpp_model: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_WHISPERCPP_MODEL", "")
        or os.getenv("SILICA_ASR_WHISPERCPP_MODEL", "")
    )
    # Weights of the in-process transcriber (agent/stt.py, the [stt] extra) that
    # dictation falls back to when the endpoint above does not answer: a
    # faster-whisper size, a HuggingFace repo id or a local CTranslate2
    # directory. Empty = agent.stt.LOCAL_STT_MODEL.
    stt_local_model: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_LOCAL_MODEL", "")
    )
    # The browser's own recognizer (Web Speech API), the last dictation lane when
    # neither transcriber answers. Off by default because Chrome's recognizer
    # sends the audio to Google unless asked not to: "on-device" allows only
    # recognition that runs inside the browser, "cloud" allows the vendor's
    # service too. It also decides which voices may read Learn aloud, since
    # Chrome's "Google" voices are the same kind of network service.
    stt_browser: str = field(
        default_factory=lambda: os.getenv("SILICA_STT_BROWSER", "off")
    )

    # COLLISION routing thresholds on the cosine of the cosine-best vault note
    # (ADR-0030). Measured 2026-08-23 as a risk-coverage curve (677 human-kept
    # notes as distinct, 60 synthetic duplicates): the high bar at 0.85 auto-merged
    # one pair, which the vault itself holds twice (a true duplicate), and 0.88
    # none; the low bar is where the leaks were. At 0.75, 28% of the duplicates
    # scored under it against their own source and were written as new notes; at
    # 0.70 it is 10% for 1.65x the judge calls, at 0.66 3% for 2.1x. 0.70 is the
    # 2%-risk point of the curve; the judge on the 0.66-0.75 band was measured at
    # 0.85 accuracy with the prompt of ADR-0030, so what it is handed it decides.
    # A repo .env that pins SILICA_SIM_THRESHOLD_LOW keeps its own value.
    # Every number in this comment, and the defaults of TITLE, THEME and
    # AUTOLINK below, is Qwen3-Embedding's; EMBEDDER_DEFAULTS replaces them
    # for an embedder whose cosines sit elsewhere (ADR-0078).
    sim_threshold_high: float = field(
        default_factory=lambda: float(embedder_env("SILICA_SIM_THRESHOLD_HIGH", "0.85"))
    )
    sim_threshold_low: float = field(
        default_factory=lambda: float(embedder_env("SILICA_SIM_THRESHOLD_LOW", "0.70"))
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
        default_factory=lambda: float(embedder_env("SILICA_SIM_TITLE_THRESHOLD", "0.80"))
    )

    # Language for the co-occurrence graph stemmer + stopwords (kernel/cooccurrence.py).
    # "auto" (default) detects the vault language from its own text at build time
    # and freezes it into the index; set an explicit Snowball language to override.
    cooccurrence_lang: str = field(
        default_factory=lambda: os.getenv("SILICA_COOCCURRENCE_LANG", "auto")
    )

    # BM25 tf term in the co-occurrence ranking leg (docs/specs/cooccur-scoring.md).
    # On by default since 2026-08-23 (ADR-0029). Gate 2026-07-25: +4.02pp recall@10,
    # p=0.0015. Re-measured 2026-08-23 on the 709-note vault: +5.82pp (0.8233 ->
    # 0.8815), cooccur alone 0.51 -> 0.86, which is what lets the two legs fuse
    # unweighted. No decision surface reads this seam any more: COLLISION routes
    # on the cosine-best note since ADR-0030 (the fused winner was the duplicate
    # 87% of the time against 98% for the cosine-best one) and autolink's gate is
    # embed-only. Answer-side (LoCoMo) not re-run: retrieval lift has failed to
    # reach answers before in this project, so SILICA_COOCCUR_BM25=0 is the kill
    # switch. k1/b stay module constants in relatedness.py: the sweep the spec
    # pre-declared ran 2026-08-23 (ADR-0030) and its held-out best is +0.9pp,
    # under the gate.
    cooccur_bm25: bool = field(
        default_factory=lambda: env_flag("SILICA_COOCCUR_BM25", True)
    )

    # Invocation-time index sweep (kernel/recall/sync.py): detect out-of-band
    # note edits/creates/deletes before the indexes are read. Off = you own
    # index freshness via explicit /embed, /cooccur, /lexical (eval harnesses
    # that need byte-identical retrieval across runs set this off).
    index_sweep: bool = field(
        default_factory=lambda: env_flag("SILICA_INDEX_SWEEP", True)
    )

    # Salience gate (Phase 2.05): concept kept only if cosine(concept, doc_centroid) >= threshold
    sim_threshold_theme: float = field(
        default_factory=lambda: float(embedder_env("SILICA_SIM_THRESHOLD_THEME", "0.35"))
    )

    # AUTOLINK relevance gate: a title is a linking candidate only if its note
    # vector is within this cosine of the note being linked. 0 disables it (the
    # whole title index is a candidate, which is what shipped before).
    #
    # 0.30 is a knee, not an optimum. Swept 0.00-0.85 against 678 hand-authored
    # notes' own wikilinks as ground truth: F1 peaks at 0.40 (0.800 vs 0.758 at
    # 0.00), but that ranks a link the author simply never made as an error.
    # Judged on the noise it exists to stop (a psychology note linked from an ML
    # lecture), 0.30 removes 7 of 11 such links for 2 lost real ones; every step
    # above costs ~9 real links per further noise link removed.
    autolink_min_sim: float = field(
        default_factory=lambda: float(embedder_env("SILICA_AUTOLINK_MIN_SIM", "0.30"))
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
        self.model = ensure_prefix(self.model, self._provider)
        self.worker_model = ensure_prefix(self.worker_model, self.worker_provider)
        self.judge_model = ensure_prefix(self.judge_model, self.judge_provider)
        self.vision_model = ensure_prefix(self.vision_model, self.vision_provider or self.provider)
        self.distill_escalation_model = ensure_prefix(
            self.distill_escalation_model, self._distill_escalation_provider)


CONFIG = SilicaConfig()
