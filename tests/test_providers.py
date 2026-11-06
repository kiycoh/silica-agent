from __future__ import annotations

import unittest

import httpx
import openai
import pytest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel

from tests.config_factory import make_config
from silica.model.providers import get_provider, Provider
from silica.config import PROVIDER_PREFIXES
from silica.model.llm import LLMResponse


def DummyConfig(provider: str, model: str):
    return make_config(provider=provider, model=model)


class SchemaModel(BaseModel):
    key: str
    value: int


class TestProviders(unittest.TestCase):
    def test_get_provider_presets(self):
        # The model keeps (or regains) its provider prefix: litellm resolves the
        # endpoint from it, so a bare id would route nowhere.
        config_lm = DummyConfig("lmstudio", "my-model")
        provider_lm = get_provider(config_lm)
        self.assertIsInstance(provider_lm, Provider)
        self.assertEqual(provider_lm.model, "lmstudio/my-model")

        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            config_or = DummyConfig("openrouter", "or-model")
            provider_or = get_provider(config_or)
            self.assertIsInstance(provider_or, Provider)
            self.assertEqual(provider_or.model, "openrouter/or-model")

    def test_get_provider_custom_uses_config_endpoint(self):
        provider = get_provider(make_config(
            provider="custom", model="custom/my-model",
            provider_base_url="http://localhost:8000/v1", provider_api_key="sk-local"))
        self.assertIsInstance(provider, Provider)
        # `custom/` is the prefix llm.call_llm turns into openai/ + api_base
        # from provider_base_url, so it must survive.
        self.assertEqual(provider.model, "custom/my-model")

    def test_get_provider_leaves_unknown_prefix_off(self):
        """A provider outside PROVIDER_PREFIXES must not be prefixed onto the model.

        config._ensure_prefix guards on that set on purpose: litellm resolves an
        endpoint only from a prefix it knows, so pinning e.g. `vllm/` in front of
        the id turns a bare model into a BadRequestError. get_provider re-derives
        the same prefix and must apply the same guard.
        """
        config = make_config(
            provider="lmstudio", model="lmstudio/qwen3-8b",
            worker_provider="vllm",           # not in PROVIDER_PREFIXES
            worker_model="qwen3-4b",          # so config leaves it bare
            worker_api_key=None, provider_base_url="http://box:8000/v1", provider_api_key="k")

        self.assertNotIn("vllm", PROVIDER_PREFIXES)
        provider = get_provider(config, role="worker")
        self.assertEqual(provider.model, "qwen3-4b")

    def test_get_provider_empty_model_stays_empty(self):
        """No model configured is not a model named after its provider."""
        provider = get_provider(DummyConfig("lmstudio", ""))
        self.assertEqual(provider.model, "")

    def test_get_provider_worker(self):
        def DummyWorkerConfig(provider, model, worker_provider=None, worker_model=None, worker_api_key=None):
            return make_config(provider=provider, model=model, worker_provider=worker_provider,
                               worker_model=worker_model, worker_api_key=worker_api_key)

        # 1. Fallback to router when worker not configured
        config_fallback = DummyWorkerConfig("openrouter", "or-model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-key"}):
            provider = get_provider(config_fallback, role="worker")
            self.assertEqual(provider.model, "openrouter/or-model")
            # No override: litellm resolves OPENROUTER_API_KEY itself.
            self.assertEqual(provider.api_key, "")

        # 2. Worker explicit preset (openrouter) without overrides
        config_worker_or = DummyWorkerConfig("lmstudio", "lm-model", worker_provider="openrouter", worker_model="worker-or-model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "worker-or-key"}):
            provider = get_provider(config_worker_or, role="worker")
            self.assertEqual(provider.model, "openrouter/worker-or-model")

        # 3. Worker explicit api-key override — the one case that must reach the
        # wire as an explicit credential, since litellm cannot know about it.
        config_worker_override = DummyWorkerConfig(
            "lmstudio", "lm-model",
            worker_provider="openrouter", worker_model="worker-or-model",
            worker_api_key="custom-key"
        )
        provider = get_provider(config_worker_override, role="worker")
        self.assertEqual(provider.model, "openrouter/worker-or-model")
        self.assertEqual(provider.api_key, "custom-key")


    def test_provider_forwards_every_argument_to_the_llm_layer(self):
        """The whole class is a delegation now: anything it drops is a feature
        the distiller/worker lane silently loses."""
        from silica.model import llm as llm_mod
        from silica.model.providers import Provider

        captured: dict = {}

        def fake_call_llm(**kw):
            captured.update(kw)
            return LLMResponse(text="ok", tool_calls=[],
                               assistant_message={"role": "assistant"},
                               usage={"prompt_tokens": 7})

        with patch.object(llm_mod, "call_llm", fake_call_llm):
            provider = Provider("openrouter/vendor/m", "KEY", openrouter_pin="together")
            resp = provider.call_llm(
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"type": "function"}],
                response_schema=SchemaModel,
                max_tokens=512,
            )

        self.assertEqual(captured["model"], "openrouter/vendor/m")
        self.assertEqual(captured["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(captured["tools"], [{"type": "function"}])
        self.assertEqual(captured["max_tokens"], 512)
        self.assertIs(captured["response_format"], SchemaModel)
        self.assertEqual(captured["openrouter_provider"], "together")
        self.assertEqual(captured["api_key"], "KEY")
        # Structured decoding stays greedy — sampling buys variety nobody wants
        # in an extraction.
        self.assertEqual(captured["temperature"], 0.0)
        self.assertEqual(resp.text, "ok")
        self.assertEqual(resp.usage, {"prompt_tokens": 7})

    def test_provider_sends_no_api_key_when_it_has_none(self):
        """An empty key must arrive as None, not "": litellm treats the empty
        string as an explicit credential and stops resolving the env var."""
        from silica.model import llm as llm_mod
        from silica.model.providers import Provider

        captured: dict = {}

        def fake_call_llm(**kw):
            captured.update(kw)
            return LLMResponse(text="", tool_calls=[],
                               assistant_message={"role": "assistant"}, usage={})

        with patch.object(llm_mod, "call_llm", fake_call_llm):
            Provider("lmstudio/m").call_llm(
                messages=[{"role": "user", "content": "hi"}])
        self.assertIsNone(captured["api_key"])

    def test_unstructured_call_sends_no_temperature(self):
        from silica.model import llm as llm_mod
        from silica.model.providers import Provider

        captured: dict = {}

        def fake_call_llm(**kw):
            captured.update(kw)
            return LLMResponse(text="", tool_calls=[],
                               assistant_message={"role": "assistant"}, usage={})

        with patch.object(llm_mod, "call_llm", fake_call_llm):
            Provider("lmstudio/m").call_llm(
                messages=[{"role": "user", "content": "hi"}])
        self.assertIsNone(captured["temperature"])


def test_presets_are_a_subset_of_known_prefixes():
    """Every preset name must be an auto-prefixable provider prefix, else its
    bare model never gets `provider/` prepended and routing silently breaks."""
    from silica.model.providers import PROVIDER_PRESETS
    from silica.config import PROVIDER_PREFIXES

    assert set(PROVIDER_PRESETS) <= PROVIDER_PREFIXES


def test_a_hosted_row_carries_everything_its_readers_need():
    """The wizard and the doctor read the row; a hosted provider missing a key
    variable, an endpoint or a model would fail there and not here."""
    from silica.provider_table import HOSTED

    assert HOSTED and all(row.key_env and row.base_url and row.models for row in HOSTED.values())


def test_call_llm_custom_routes_via_openai(monkeypatch):
    """A custom/ model reaches litellm as openai/<id> with an explicit api_base,
    since litellm has no `custom/` provider."""
    from silica.model import llm

    captured: dict = {}

    class _Msg:
        content = "ok"
        tool_calls = None
        reasoning_content = None
        reasoning = None
        thinking_blocks = None

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = None

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(llm._litellm(), "completion", fake_completion)
    monkeypatch.setattr(llm.CONFIG, "provider_base_url", "http://localhost:9999/v1")
    monkeypatch.setattr(llm.CONFIG, "provider_api_key", "sk-local")

    resp = llm.call_llm("custom/qwen3", [{"role": "user", "content": "hi"}])

    assert captured["model"] == "openai/qwen3"
    assert captured["api_base"] == "http://localhost:9999/v1"
    assert captured["api_key"] == "sk-local"
    assert resp.text == "ok"


def test_call_llm_lmstudio_routes_via_openai(monkeypatch):
    """An lmstudio/ model reaches litellm as openai/<id> pinned to the preset
    endpoint: litellm's registry has no `lmstudio` (BadRequestError), and its
    `lm_studio` dialect resolves api_base only from LM_STUDIO_API_BASE — no
    localhost default — so the generic openai/ route with the preset URL is
    the only self-contained path."""
    from silica.model import llm
    from silica.provider_table import PROVIDERS

    captured: dict = {}

    class _Msg:
        content = "ok"
        tool_calls = None
        reasoning_content = None
        reasoning = None
        thinking_blocks = None

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = None

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(llm._litellm(), "completion", fake_completion)

    resp = llm.call_llm("lmstudio/qwen3-30b", [{"role": "user", "content": "hi"}])

    assert captured["model"] == "openai/qwen3-30b"
    assert captured["api_base"] == PROVIDERS["lmstudio"].base_url
    assert captured["api_key"] == PROVIDERS["lmstudio"].fixed_key
    assert resp.text == "ok"


def test_call_llm_ollama_routes_via_ollama_chat(monkeypatch):
    """An ollama/ model reaches litellm as ollama_chat/<id> so tool calls use
    /api/chat (native) rather than /api/generate (prompt-emulated)."""
    from silica.model import limits, llm

    captured: dict = {}

    class _Msg:
        content = "ok"
        tool_calls = None
        reasoning_content = None
        reasoning = None
        thinking_blocks = None

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]
        usage = None

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(llm._litellm(), "completion", fake_completion)
    # Don't let clamp_max_tokens probe a live Ollama during the test.
    monkeypatch.setattr(limits, "model_limits", lambda p, m: (0, 0))
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    # limits.base_url is the single endpoint authority (doctor and model_limits
    # read the same one, resolved from OLLAMA_HOST at import). Patching it here
    # proves the chat path follows it instead of litellm's hardcoded localhost.
    monkeypatch.setattr(limits, "_OLLAMA_BASE", "http://gpu-box:11500/v1")

    llm.call_llm("ollama/llama3.2:3b", [{"role": "user", "content": "hi"}])

    assert captured["model"] == "ollama_chat/llama3.2:3b"
    # Ollama loads at a 4096 window by default and drops the overflow silently,
    # which strips the ~8k-token toolset out of the prompt. The pin is what keeps
    # tool calling working at all, so its absence is a regression, not a nit.
    assert captured["num_ctx"] == 16384
    # litellm reads OLLAMA_API_BASE only; without an explicit api_base the chat
    # path would ignore OLLAMA_HOST while doctor and model_limits honour it.
    assert captured["api_base"] == "http://gpu-box:11500"


def test_get_provider_ollama_avoids_the_v1_endpoint(monkeypatch):
    """/v1 cannot size the context window (it ignores num_ctx and reloads the
    model at 4096), so the distiller path must go through /api/chat instead."""
    from pydantic import BaseModel
    from silica.model import llm as llm_mod
    from silica.model import providers

    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    config = make_config(provider="ollama", model="ollama/gemma3:4b")
    provider = providers.get_provider(config)

    assert isinstance(provider, providers.Provider)
    # The `ollama/` prefix is load-bearing: llm.call_llm rewrites it to
    # ollama_chat/ + num_ctx. Stripping it would send this to /v1.
    assert provider.model == "ollama/gemma3:4b"

    captured: dict = {}

    def fake_call_llm(**kw):
        captured.update(kw)
        return llm_mod.LLMResponse(text="{}", tool_calls=[],
                                   assistant_message={"role": "assistant"}, usage={})

    monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)

    class Schema(BaseModel):
        title: str

    provider.call_llm(messages=[{"role": "user", "content": "hi"}], tools=None,
                      response_schema=Schema, max_tokens=512)

    assert captured["model"] == "ollama/gemma3:4b"
    assert captured["response_format"] is Schema  # litellm renders it as Ollama's `format`
    assert captured["temperature"] == 0.0  # extraction stays greedy, not Ollama's 0.8


@pytest.mark.parametrize(
    "env, expected",
    [
        (None, "http://localhost:11434/v1"),
        ("", "http://localhost:11434/v1"),
        ("remote-box", "http://remote-box:11434/v1"),           # bare host -> ollama's default port
        ("remote-box:9999", "http://remote-box:9999/v1"),
        ("http://10.0.0.5:11434", "http://10.0.0.5:11434/v1"),
        ("https://ollama.internal:443", "https://ollama.internal:443/v1"),
        ("http://10.0.0.5:11434/", "http://10.0.0.5:11434/v1"),  # trailing slash
        ("host:not-a-port", "http://localhost:11434/v1"),        # garbage degrades, never raises
    ],
)
def test_ollama_base_url_honours_ollama_host(env, expected, monkeypatch):
    from silica.model.limits import _ollama_base_url

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    if env is not None:
        monkeypatch.setenv("OLLAMA_HOST", env)
    assert _ollama_base_url() == expected


@pytest.fixture
def fresh_warn_state():
    """warn_down_once dedups per process; each test needs a clean slate."""
    from silica.model import providers

    providers._warned_down.clear()
    yield
    providers._warned_down.clear()


@patch("silica.model.providers.openai.OpenAI")
def test_embedder_on_openrouter_sends_the_models_own_pin(mock_openai_cls, monkeypatch):
    """The pilot's zero-retention pin for the index lives in
    OPENROUTER_PROVIDER_BY_MODEL like the chat's; the chat's provider ORDER is
    not an embedding provider and must not follow. A local server gets nothing."""
    from silica.model.providers import OpenAIEmbedder
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "openrouter_provider_by_model",
                        '{"perplexity/pplx-embed-v1-4b": {"zdr": true}}')
    monkeypatch.setattr(CONFIG, "openrouter_provider", "deepinfra")
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    item = MagicMock(index=0, embedding=[0.1, 0.2])
    mock_client.embeddings.create.return_value = MagicMock(data=[item], usage=MagicMock(prompt_tokens=3, cost=1e-7))

    OpenAIEmbedder("https://openrouter.ai/api/v1", "k", "perplexity/pplx-embed-v1-4b").embed(["t"])
    assert mock_client.embeddings.create.call_args.kwargs["extra_body"] == {"provider": {"zdr": True}}

    OpenAIEmbedder("https://openrouter.ai/api/v1", "k", "baai/bge-m3").embed(["t"])
    assert mock_client.embeddings.create.call_args.kwargs["extra_body"] is None

    OpenAIEmbedder("http://127.0.0.1:1234/v1", "k", "perplexity/pplx-embed-v1-4b").embed(["t"])
    assert mock_client.embeddings.create.call_args.kwargs["extra_body"] is None


@patch("silica.model.providers.openai.OpenAI")
def test_embedder_counts_what_a_metered_index_costs(mock_openai_cls, monkeypatch):
    from silica.model import providers

    events = []
    monkeypatch.setattr(providers, "emit", lambda name, **kw: events.append((name, kw)))
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    item = MagicMock(index=0, embedding=[0.1])
    mock_client.embeddings.create.return_value = MagicMock(data=[item], usage=MagicMock(prompt_tokens=3, cost=2.5e-7))

    providers.OpenAIEmbedder("http://x/v1", "k", "m").embed(["t"])
    assert events[0][0] == "embed" and events[0][1]["usd"] == 2.5e-7 and events[0][1]["tokens"] == 3


@patch("silica.model.providers.openai.OpenAI")
def test_embedder_down_warns_once_and_still_raises(mock_openai_cls, caplog, fresh_warn_state):
    """A down embedder degrades recall silently — the warning is what says so."""
    from silica.model.providers import OpenAIEmbedder

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.embeddings.create.side_effect = ConnectionError("connection refused")

    embedder = OpenAIEmbedder(base_url="http://localhost:1234/v1", api_key="k", model="m")
    with caplog.at_level("WARNING", logger="silica.model.providers"):
        for _ in range(2):
            # Callers keep their own fail-open guards: the exception must survive.
            with pytest.raises(ConnectionError):
                embedder.embed(["text"])

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "embedder unreachable at http://localhost:1234/v1" in warnings[0].getMessage()


@patch("silica.model.providers.httpx.post")
def test_reranker_down_warns_once_and_abstains(mock_post, caplog, fresh_warn_state):
    """Rerank keeps abstaining (None), but no longer without telling anyone."""
    from silica.model.providers import Reranker

    mock_post.side_effect = ConnectionError("connection refused")
    reranker = Reranker(base_url="http://127.0.0.1:1235/v1", model="bge")

    with caplog.at_level("WARNING", logger="silica.model.providers"):
        assert reranker.scores("q", ["a"]) is None
        assert reranker.scores("q", ["b"]) is None

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "reranker unreachable at http://127.0.0.1:1235/v1/rerank" in warnings[0].getMessage()


@patch("silica.model.providers.openai.OpenAI")
def test_embedder_wrong_model_is_not_reported_as_down(mock_openai_cls, caplog, fresh_warn_state):
    """A 404 means something IS listening — telling the user to start a server
    would send them after the wrong problem."""
    from silica.model.providers import OpenAIEmbedder

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    not_found = openai.NotFoundError(
        "model not found",
        response=httpx.Response(404, request=httpx.Request("POST", "http://localhost:1234/v1")),
        body=None,
    )
    mock_client.embeddings.create.side_effect = not_found

    embedder = OpenAIEmbedder(base_url="http://localhost:1234/v1", api_key="k", model="typo-model")
    with caplog.at_level("WARNING", logger="silica.model.providers"):
        with pytest.raises(openai.NotFoundError):
            embedder.embed(["text"])

    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "is up but rejected model `typo-model`" in warning.getMessage()
    assert "unreachable" not in warning.getMessage()


@patch("silica.model.providers.httpx.post")
def test_reranker_down_then_wrong_model_each_warn_once(mock_post, caplog, fresh_warn_state):
    """Dedup keys on the kind, so the second problem still surfaces once the
    first is fixed — otherwise starting the server would silence the typo."""
    from silica.model.providers import Reranker

    reranker = Reranker(base_url="http://127.0.0.1:1235/v1", model="typo-model")
    rejected = MagicMock()
    rejected.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404",
        request=httpx.Request("POST", reranker.url),
        response=httpx.Response(404, request=httpx.Request("POST", reranker.url)),
    )

    with caplog.at_level("WARNING", logger="silica.model.providers"):
        mock_post.side_effect = httpx.ConnectError("connection refused")
        assert reranker.scores("q", ["a"]) is None
        assert reranker.scores("q", ["a"]) is None  # same kind: deduped
        mock_post.side_effect = None                # server comes up, model still wrong
        mock_post.return_value = rejected
        assert reranker.scores("q", ["a"]) is None
        assert reranker.scores("q", ["a"]) is None  # same kind: deduped

    down, wrong_model = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "reranker unreachable at" in down
    assert "is up but rejected model `typo-model`" in wrong_model


class ProviderReasoningForwardingTests(unittest.TestCase):
    def test_reasoning_flag_reaches_call_llm(self):
        """A structured-output worker call must be able to switch a hybrid
        model's thinking off: the trace bills against max_tokens, and at 2048
        the dedup judge came back finish=length with its JSON cut mid-string
        (2026-08-23 run, deepseek-v4-flash). The knob exists on llm.call_llm;
        the Provider lane silently dropped it."""
        from silica.model import llm as llm_mod
        from silica.model.llm import LLMResponse
        from silica.model.providers import Provider

        captured: dict = {}

        def fake_call_llm(**kw):
            captured.update(kw)
            return LLMResponse(text="{}", tool_calls=[],
                               assistant_message={"role": "assistant"}, usage={})

        with patch.object(llm_mod, "call_llm", fake_call_llm):
            Provider("openrouter/v/m").call_llm(
                messages=[{"role": "user", "content": "judge"}],
                reasoning=False,
            )
        self.assertIs(captured["reasoning"], False)

    def test_reasoning_defaults_to_provider_choice(self):
        """Unset ⇒ None on the wire, so every other worker keeps its current behaviour."""
        from silica.model import llm as llm_mod
        from silica.model.llm import LLMResponse
        from silica.model.providers import Provider

        captured: dict = {}

        def fake_call_llm(**kw):
            captured.update(kw)
            return LLMResponse(text="", tool_calls=[],
                               assistant_message={"role": "assistant"}, usage={})

        with patch.object(llm_mod, "call_llm", fake_call_llm):
            Provider("openrouter/v/m").call_llm(
                messages=[{"role": "user", "content": "x"}],
            )
        self.assertIsNone(captured["reasoning"])


def test_jinja_routes_get_one_leading_system_message(monkeypatch):
    """Qwen3.5's chat template raises on a system message past index 0: on the
    custom/ and lmstudio/ routes the leading block folds into one message and a
    mid-run system note becomes a user turn; hosted routes are sent untouched."""
    from silica.model import llm

    captured = {}

    def fake_completion(**kwargs):
        captured["messages"] = kwargs["messages"]
        raise RuntimeError("stop")

    monkeypatch.setattr(llm._litellm(), "completion", fake_completion)
    convo = [
        {"role": "system", "content": "base"},
        {"role": "system", "content": "today"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "silica_reasoning": "look it up",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "silica_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "note body"},
        {"role": "system", "content": "budget note"},
    ]
    for model in ("custom/qwen3.5-9b", "lmstudio/qwen/qwen3.5-9b"):
        try:
            llm._call_llm(model, list(convo))
        except Exception:
            pass
        sent = captured["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "user"]
        assert sent[0]["content"] == "base\n\ntoday"
        assert sent[2]["reasoning_content"] == "look it up"
        assert "silica_reasoning" not in sent[2]
        assert sent[4]["content"] == "budget note"
    try:
        llm._call_llm("openai/gpt-4o", list(convo))
    except Exception:
        pass
    sent = captured["messages"]
    assert [m["role"] for m in sent] == ["system", "system", "user", "assistant", "tool", "system"]
    assert "reasoning_content" not in sent[3]


def test_embedder_read_timeout_comes_from_config(monkeypatch):
    # A CPU embedder needs minutes for one 32-note batch, so the 60 s read
    # guard must be a knob: measured 2026-10-02 on 2 ARM cores, /embed of 22
    # notes timed out twice on batch 0 and indexed nothing.
    from silica.model.providers import OpenAIEmbedder, get_embedder
    from silica.config import SilicaConfig

    assert OpenAIEmbedder("http://x/v1", "k", "m").client.timeout.read == 60.0
    monkeypatch.setenv("SILICA_EMBEDDING_TIMEOUT", "1800")
    assert get_embedder(SilicaConfig()).client.timeout.read == 1800.0
    # compose passes `${SILICA_EMBEDDING_TIMEOUT:-}`: unset there is "" here.
    monkeypatch.setenv("SILICA_EMBEDDING_TIMEOUT", "")
    assert get_embedder(SilicaConfig()).client.timeout.read == 60.0


def test_a_role_runs_on_its_own_model_without_a_provider_beside_it():
    """A worker or judge model alone used to be ignored by get_provider and obeyed
    by run_worker, residue and grade, so one setting ran two models: Settings
    writes SILICA_WORKER_MODEL alone and says it is used."""
    from silica.model.providers import resolve_role

    cfg = make_config(provider="lmstudio", model="chat", worker_api_key="wk",
                      worker_model="openrouter/w", worker_provider=None,
                      judge_model="openrouter/j", judge_provider=None)
    assert resolve_role("worker", cfg) == ("openrouter/w", "wk")
    assert get_provider(cfg, role="judge").model == "openrouter/j"
    assert resolve_role("escalation", cfg) == ("lmstudio/chat", "")


def test_a_bare_role_model_with_no_provider_stays_on_the_router():
    """It names no endpoint, so litellm would refuse the call: before the role
    resolver this config ran on the router, and it must not start failing."""
    cfg = make_config(provider="openrouter", model="openrouter/chat", worker_api_key="",
                      worker_model="qwen3", worker_provider=None,
                      judge_model="qwen3", judge_provider=None)
    assert get_provider(cfg, role="worker").model == "openrouter/chat"
    assert get_provider(cfg, role="judge").model == "openrouter/chat"
    cfg.worker_provider = "vllm"  # a named provider keeps the id, as it always did
    assert get_provider(cfg, role="worker").model == "qwen3"
    # A prefix this tree does not list still routes: litellm knows it, silica need not.
    cfg.worker_provider, cfg.worker_model = None, "together_ai/llama"
    assert get_provider(cfg, role="worker").model == "together_ai/llama"


def test_the_worker_key_stays_with_the_worker_model():
    from silica.model.providers import resolve_role

    cfg = make_config(provider="openrouter", model="chat", worker_api_key="wk", worker_model="")
    assert resolve_role("worker", cfg) == ("openrouter/chat", "")
