"""model_limits() — dynamic context window resolved from the live provider.

LM Studio exposes /api/v0/models with `loaded_context_length` (the window the
model is loaded with RIGHT NOW — can be far below the model's max) and
`max_context_length`. OpenRouter exposes /api/v1/models with `context_length`
and `top_provider.max_completion_tokens`. (0, 0) means unknown → callers keep
their static defaults.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from silica.agent.providers import _model_limits_memo, model_limits


@pytest.fixture(autouse=True)
def _clear_cache():
    _model_limits_memo.clear()
    yield
    _model_limits_memo.clear()


def _http_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


LMSTUDIO_MODELS = {
    "data": [
        {
            "id": "qwen3-8b",
            "type": "llm",
            "max_context_length": 40960,
            "loaded_context_length": 8192,
        },
        # Not loaded → no loaded_context_length key
        {"id": "other-model", "type": "llm", "max_context_length": 4096},
    ]
}

OPENROUTER_MODELS = {
    "data": [
        {
            "id": "qwen/qwen3-8b",
            "context_length": 131072,
            "top_provider": {"context_length": 131072, "max_completion_tokens": 8192},
        },
        # top_provider can be null in the OpenRouter catalog
        {"id": "foo/bar", "context_length": 8192, "top_provider": None},
    ]
}


class TestLMStudio:
    @patch("silica.agent.providers.httpx.get")
    def test_loaded_context_wins_over_max(self, mock_get):
        mock_get.return_value = _http_response(LMSTUDIO_MODELS)
        assert model_limits("lmstudio", "qwen3-8b") == (8192, 0)

    @patch("silica.agent.providers.httpx.get")
    def test_falls_back_to_max_context_when_not_loaded(self, mock_get):
        mock_get.return_value = _http_response(LMSTUDIO_MODELS)
        assert model_limits("lmstudio", "other-model") == (4096, 0)

    @patch("silica.agent.providers.httpx.get")
    def test_strips_lmstudio_prefix(self, mock_get):
        mock_get.return_value = _http_response(LMSTUDIO_MODELS)
        assert model_limits("lmstudio", "lmstudio/qwen3-8b") == (8192, 0)

    @patch("silica.agent.providers.httpx.get")
    def test_queries_rest_api_not_openai_compat(self, mock_get):
        mock_get.return_value = _http_response(LMSTUDIO_MODELS)
        model_limits("lmstudio", "qwen3-8b")
        url = mock_get.call_args.args[0]
        assert url.endswith("/api/v0/models")


OLLAMA_WIDE = {  # trained max well above anything silica would pin
    "parameters": "stop \"<|eot_id|>\"",
    "model_info": {"general.architecture": "llama", "llama.context_length": 131072},
}

OLLAMA_NARROW = {  # trained max below the pinned window → the model is the ceiling
    "parameters": "",
    "model_info": {"general.architecture": "qwen2", "qwen2.context_length": 8192},
}


class TestOllama:
    """Silica pins num_ctx on every request, so the window is what it asks for,
    capped by the model's trained maximum. The runtime's 4096 default and any
    Modelfile num_ctx are both overridden by request options, so neither is read."""

    @pytest.fixture(autouse=True)
    def _no_ambient_pin(self, monkeypatch):
        # A developer with OLLAMA_NUM_CTX exported must not shift these assertions.
        monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)

    @patch("silica.agent.providers.httpx.post")
    def test_pinned_window_wins_below_trained_max(self, mock_post):
        mock_post.return_value = _http_response(OLLAMA_WIDE)
        assert model_limits("ollama", "llama3.1") == (16384, 0)

    @patch("silica.agent.providers.httpx.post")
    def test_trained_max_caps_the_pin(self, mock_post):
        mock_post.return_value = _http_response(OLLAMA_NARROW)
        assert model_limits("ollama", "qwen2.5") == (8192, 0)

    @patch("silica.agent.providers.httpx.post")
    def test_env_override_is_honoured(self, mock_post, monkeypatch):
        monkeypatch.setenv("OLLAMA_NUM_CTX", "65536")
        mock_post.return_value = _http_response(OLLAMA_WIDE)
        assert model_limits("ollama", "llama3.1") == (65536, 0)

    @patch("silica.agent.providers.httpx.post")
    def test_strips_ollama_prefix_and_posts_show(self, mock_post):
        mock_post.return_value = _http_response(OLLAMA_WIDE)
        model_limits("ollama", "ollama/llama3.1")
        assert mock_post.call_args.args[0].endswith("/api/show")
        assert mock_post.call_args.kwargs["json"] == {"model": "llama3.1"}

    @patch("silica.agent.providers.httpx.post")
    def test_missing_model_info_returns_zero(self, mock_post):
        mock_post.return_value = _http_response({"parameters": ""})
        assert model_limits("ollama", "ghost") == (0, 0)


class TestOpenRouter:
    @patch("silica.agent.providers.httpx.get")
    def test_context_window_and_output_cap(self, mock_get):
        mock_get.return_value = _http_response(OPENROUTER_MODELS)
        assert model_limits("openrouter", "qwen/qwen3-8b") == (131072, 8192)

    @patch("silica.agent.providers.httpx.get")
    def test_strips_openrouter_prefix(self, mock_get):
        mock_get.return_value = _http_response(OPENROUTER_MODELS)
        assert model_limits("openrouter", "openrouter/qwen/qwen3-8b") == (131072, 8192)

    @patch("silica.agent.providers.httpx.get")
    def test_null_top_provider(self, mock_get):
        mock_get.return_value = _http_response(OPENROUTER_MODELS)
        assert model_limits("openrouter", "foo/bar") == (8192, 0)


class TestFallbacks:
    @patch("silica.agent.providers.httpx.get")
    def test_unknown_model_returns_zero(self, mock_get):
        mock_get.return_value = _http_response(LMSTUDIO_MODELS)
        assert model_limits("lmstudio", "not-in-catalog") == (0, 0)

    @patch("silica.agent.providers.httpx.get")
    def test_http_error_returns_zero(self, mock_get):
        mock_get.side_effect = ConnectionError("refused")
        assert model_limits("lmstudio", "qwen3-8b") == (0, 0)

    def test_unknown_provider_returns_zero(self):
        assert model_limits("some-future-preset", "m") == (0, 0)

    @patch("silica.agent.providers.httpx.get")
    def test_result_is_cached_per_process(self, mock_get):
        mock_get.return_value = _http_response(LMSTUDIO_MODELS)
        model_limits("lmstudio", "qwen3-8b")
        model_limits("lmstudio", "qwen3-8b")
        assert mock_get.call_count == 1


class TestCliContextBudget:
    """_resolve_context_budget() sizes the REPL meter to the real window."""

    @patch("silica.agent.providers.model_limits", return_value=(131072, 0))
    def test_sets_budget_from_provider(self, _limits, monkeypatch):
        from silica.cli import _resolve_context_budget
        from silica.config import CONFIG

        monkeypatch.delenv("SILICA_MAX_CONTEXT", raising=False)
        monkeypatch.setattr(CONFIG, "model", "qwen/qwen3-8b")
        monkeypatch.setattr(CONFIG, "max_context_tokens", 60000)
        _resolve_context_budget()
        assert CONFIG.max_context_tokens == 131072

    @patch("silica.agent.providers.model_limits", return_value=(131072, 0))
    def test_env_pin_wins_for_local_provider(self, _limits, monkeypatch):
        from silica.cli import _resolve_context_budget
        from silica.config import CONFIG

        monkeypatch.setenv("SILICA_MAX_CONTEXT", "42000")
        monkeypatch.setattr(CONFIG, "model", "lmstudio/qwen3-8b")
        monkeypatch.setattr(CONFIG, "_provider", "lmstudio")
        monkeypatch.setattr(CONFIG, "max_context_tokens", 42000)
        _resolve_context_budget()
        assert CONFIG.max_context_tokens == 42000

    @patch("silica.agent.providers.model_limits", return_value=(1048576, 0))
    def test_env_pin_ignored_for_openrouter(self, _limits, monkeypatch):
        # Hosted providers report their own window — the pin (meant for local
        # models) must not mask it. Regression: mimo-v2.5 (1M) showed 250k.
        from silica.cli import _resolve_context_budget
        from silica.config import CONFIG

        monkeypatch.setenv("SILICA_MAX_CONTEXT", "250000")
        monkeypatch.setattr(CONFIG, "model", "openrouter/xiaomi/mimo-v2.5")
        monkeypatch.setattr(CONFIG, "_provider", "openrouter")
        monkeypatch.setattr(CONFIG, "max_context_tokens", 250000)
        _resolve_context_budget()
        assert CONFIG.max_context_tokens == 1048576

    @patch("silica.agent.providers.model_limits", return_value=(0, 0))
    def test_unknown_window_keeps_default(self, _limits, monkeypatch):
        from silica.cli import _resolve_context_budget
        from silica.config import CONFIG

        monkeypatch.delenv("SILICA_MAX_CONTEXT", raising=False)
        monkeypatch.setattr(CONFIG, "model", "qwen/qwen3-8b")
        monkeypatch.setattr(CONFIG, "max_context_tokens", 60000)
        _resolve_context_budget()
        assert CONFIG.max_context_tokens == 60000
