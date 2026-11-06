# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Every fact about a model provider, one row each: what the prefix is called,
where it answers, which key it reads, how litellm has to be told about it.

Data and nothing else, and at the top of the package rather than in
`silica/model`, because config needs it at import to read the model out of the
environment and config sits below the model stack. Adding a provider is a row
here, plus `.env.example`; a provider with a request quirk of its own adds one
branch in `model/llm._call_llm`. OLLAMA_HOST is not read here: this module loads
before config has read `.env` (`model/limits.base_url`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderRow:
    name: str                     # the `prefix/` of a model id, and SILICA_PROVIDER's value
    label: str                    # what the wizard and the doctor call it
    # OpenAI-compatible root. "" for `custom`, whose endpoint is the user's.
    base_url: str = ""
    key_env: str = ""             # the variable holding the key; siblings _2, _3 pool with it
    fixed_key: str = ""           # what a local server is sent: it ignores it, the SDK demands one
    # Best first: `model_from_env` takes the head, the wizard offers the list.
    # A row with models is a hosted provider. The wizard checks them against
    # the provider's live /models (wizard._live_hosted_models).
    # shortcut: the heads back model_from_env, which must stay HTTP-free, so a
    # head can rot; refresh it when a provider retires a model.
    models: tuple[str, ...] = ()
    # What litellm is told in place of `name`, for a prefix it has no route for
    # or routes badly (ollama/ is /api/generate, which fakes tools in the prompt).
    wire_prefix: str = ""
    # The server renders the conversation with the model's own Jinja chat
    # template (llama-server, LM Studio, vLLM behind `custom`): `model/llm`
    # folds the system block and hands the reasoning back for these.
    jinja_template: bool = False


# Hosted rows come first and in this order: it is the fallback order of
# `model_from_env` and the order the wizard lists them in.
PROVIDERS: dict[str, ProviderRow] = {row.name: row for row in (
    ProviderRow(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
        models=(
            "openrouter/deepseek/deepseek-v4-flash",
            "openrouter/anthropic/claude-sonnet-5",
            "openrouter/google/gemini-3.5-flash",
            "openrouter/mistralai/mistral-small-2603",
        )),
    # Through its OpenAI-compatible endpoint; both loops read the same key.
    ProviderRow("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/",
                "GEMINI_API_KEY", models=("gemini/gemini-2.5-flash",)),
    ProviderRow("openai", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY",
                models=("openai/gpt-4o",)),
    ProviderRow("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
                models=("groq/llama-3.3-70b-versatile",)),
    ProviderRow("deepseek", "DeepSeek", "https://api.deepseek.com", "DEEPSEEK_API_KEY",
                models=("deepseek/deepseek-chat",)),
    ProviderRow("mistral", "Mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY",
                models=("mistral/mistral-large-latest",)),
    ProviderRow("xai", "xAI (Grok)", "https://api.x.ai/v1", "XAI_API_KEY",
                models=("xai/grok-2-latest",)),
    # litellm's registry has no `lmstudio`, and its `lm_studio` dialect resolves
    # api_base only from LM_STUDIO_API_BASE, with no localhost default.
    ProviderRow("lmstudio", "LM Studio", "http://localhost:1234/v1", fixed_key="lm-studio",
                wire_prefix="openai", jinja_template=True),
    ProviderRow("ollama", "Ollama", "http://localhost:11434/v1", fixed_key="ollama",
                wire_prefix="ollama_chat"),
    # Any other OpenAI-compatible URL: SILICA_PROVIDER_BASE_URL/_API_KEY.
    ProviderRow("custom", "the endpoint", key_env="SILICA_PROVIDER_API_KEY",
                wire_prefix="openai", jinja_template=True),
)}

HOSTED: dict[str, ProviderRow] = {name: row for name, row in PROVIDERS.items() if row.models}
