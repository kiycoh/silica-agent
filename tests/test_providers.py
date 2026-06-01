from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel

from silica.agent.providers import get_provider, OpenAICompatibleProvider
from silica.agent.llm import LLMResponse


class DummyConfig:
    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model


class SchemaModel(BaseModel):
    key: str
    value: int


class TestProviders(unittest.TestCase):
    def test_get_provider_presets(self):
        # Test default preset (lmstudio)
        config_lm = DummyConfig("lmstudio", "my-model")
        provider_lm = get_provider(config_lm)
        self.assertIsInstance(provider_lm, OpenAICompatibleProvider)
        self.assertEqual(provider_lm.model, "my-model")

        # Test openrouter preset
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            config_or = DummyConfig("openrouter", "or-model")
            provider_or = get_provider(config_or)
            self.assertIsInstance(provider_or, OpenAICompatibleProvider)
            self.assertEqual(provider_or.model, "or-model")

    def test_get_provider_worker(self):
        class DummyWorkerConfig:
            def __init__(self, provider, model, worker_provider=None, worker_model=None, worker_base_url=None, worker_api_key=None):
                self.provider = provider
                self.model = model
                self.worker_provider = worker_provider
                self.worker_model = worker_model
                self.worker_base_url = worker_base_url
                self.worker_api_key = worker_api_key

        # 1. Fallback to router when worker not configured
        config_fallback = DummyWorkerConfig("openrouter", "or-model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-key"}):
            provider = get_provider(config_fallback, role="worker")
            self.assertEqual(provider.model, "or-model")
            self.assertIn("openrouter.ai", str(provider.client.base_url))

        # 2. Worker explicit preset (openrouter) without overrides
        config_worker_or = DummyWorkerConfig("lmstudio", "lm-model", worker_provider="openrouter", worker_model="worker-or-model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "worker-or-key"}):
            provider = get_provider(config_worker_or, role="worker")
            self.assertEqual(provider.model, "worker-or-model")
            self.assertIn("openrouter.ai", str(provider.client.base_url))

        # 3. Worker explicit overrides
        config_worker_override = DummyWorkerConfig(
            "lmstudio", "lm-model",
            worker_provider="openrouter", worker_model="worker-or-model",
            worker_base_url="http://custom-worker:5000/v1", worker_api_key="custom-key"
        )
        provider = get_provider(config_worker_override, role="worker")
        self.assertEqual(provider.model, "worker-or-model")
        self.assertEqual(str(provider.client.base_url), "http://custom-worker:5000/v1/")
        self.assertEqual(provider.client.api_key, "custom-key")


    @patch("openai.OpenAI")
    def test_call_llm_structured_success(self, mock_openai_cls):
        # Setup mock client
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # Mock beta.chat.completions.parse
        mock_parsed_response = MagicMock()
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_parsed_obj = SchemaModel(key="test", value=123)
        
        mock_message.content = '{"key": "test", "value": 123}'
        mock_message.parsed = mock_parsed_obj
        mock_message.tool_calls = None
        mock_choice.message = mock_message
        mock_parsed_response.choices = [mock_choice]
        mock_parsed_response.usage = {"prompt_tokens": 10}
        
        mock_client.beta.chat.completions.parse.return_value = mock_parsed_response

        # Execute
        provider = OpenAICompatibleProvider(base_url="http://dummy", api_key="dummy", model="test-model")
        response = provider.call_llm(
            messages=[{"role": "user", "content": "hi"}],
            response_schema=SchemaModel
        )

        # Assertions
        mock_client.beta.chat.completions.parse.assert_called_once()
        self.assertIsInstance(response, LLMResponse)
        self.assertEqual(response.text, '{"key": "test", "value": 123}')

    @patch("openai.OpenAI")
    def test_call_llm_structured_fallback(self, mock_openai_cls):
        # Setup mock client
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # beta.chat.completions.parse raises an exception (not supported, e.g., older server)
        mock_client.beta.chat.completions.parse.side_effect = Exception("Not supported")

        # Non-structured fallback path now streams.
        mock_delta = MagicMock()
        mock_delta.content = '{"key": "fallback", "value": 456}'
        mock_delta.tool_calls = None
        mock_chunk_choice = MagicMock()
        mock_chunk_choice.finish_reason = "stop"
        mock_chunk_choice.delta = mock_delta
        mock_chunk = MagicMock()
        mock_chunk.choices = [mock_chunk_choice]

        mock_client.chat.completions.create.return_value = [mock_chunk]

        # Execute
        provider = OpenAICompatibleProvider(base_url="http://dummy", api_key="dummy", model="test-model")
        response = provider.call_llm(
            messages=[{"role": "user", "content": "hi"}],
            response_schema=SchemaModel
        )

        # Assertions
        mock_client.beta.chat.completions.parse.assert_called_once()
        mock_client.chat.completions.create.assert_called_once()
        self.assertIsInstance(response, LLMResponse)
        self.assertEqual(response.text, '{"key": "fallback", "value": 456}')

    @patch("openai.OpenAI")
    @patch("time.sleep", return_value=None)
    def test_call_llm_retries_on_timeout(self, mock_sleep, mock_openai_cls):
        import openai
        # Setup mock client
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # First two calls raise APITimeoutError, third call succeeds.
        # Non-structured path now streams: successful response is an iterable of chunks.
        mock_delta = MagicMock()
        mock_delta.content = "Success after retries"
        mock_delta.tool_calls = None
        mock_chunk_choice = MagicMock()
        mock_chunk_choice.finish_reason = "stop"
        mock_chunk_choice.delta = mock_delta
        mock_chunk = MagicMock()
        mock_chunk.choices = [mock_chunk_choice]
        success_stream = [mock_chunk]

        # Set up side effect to fail twice then succeed
        mock_client.chat.completions.create.side_effect = [
            openai.APITimeoutError(request=MagicMock()),
            openai.APIConnectionError(request=MagicMock(), message="Connection issue"),
            success_stream,
        ]

        provider = OpenAICompatibleProvider(base_url="http://dummy", api_key="dummy", model="test-model")
        response = provider.call_llm(
            messages=[{"role": "user", "content": "hi"}]
        )

        self.assertEqual(mock_client.chat.completions.create.call_count, 3)
        self.assertEqual(response.text, "Success after retries")
        self.assertEqual(mock_sleep.call_count, 2)
