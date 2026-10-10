# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Key pools and fallback models (ADR-0063): a call outlives one key and one
endpoint, and a setup with one key and no fallbacks calls exactly as before.

groq/ and deepseek/ ids on purpose: model_limits probes nothing for them, so
no test here reaches the network."""
from __future__ import annotations

from unittest.mock import patch

import litellm
import pytest

from silica.agent import llm as llm_mod
from silica.agent.llm import call_llm
from tests.llm_mocks import litellm_mock_response

MSGS = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from silica.config import CONFIG

    llm_mod._benched.clear()
    llm_mod._held.clear()
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)   # retry backoff, not under test
    monkeypatch.setattr(CONFIG, "fallback_models", "")
    for n in ("", "_2", "_3"):
        monkeypatch.delenv(f"GROQ_API_KEY{n}", raising=False)
    yield
    llm_mod._benched.clear()
    llm_mod._held.clear()


def _err(cls, model):
    return cls(message="nope", llm_provider=model.split("/")[0], model=model)


def test_one_key_and_no_fallbacks_call_as_before(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    with patch("litellm.completion", return_value=litellm_mock_response("ok")) as c:
        assert call_llm("groq/a", MSGS).text == "ok"
    assert c.call_count == 1
    assert "api_key" not in c.call_args.kwargs   # litellm still reads the variable itself


def test_key_pool_reads_numbered_siblings_to_the_first_gap(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY_2", "k2")
    monkeypatch.setenv("GROQ_API_KEY_4", "k4")
    assert llm_mod.key_pool("groq/a") == ["k1", "k2"]
    assert llm_mod.key_pool("lmstudio/a") == []


def test_a_rate_limited_key_hands_over_without_a_backoff(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY_2", "k2")
    slept = []
    monkeypatch.setattr(llm_mod.time, "sleep", slept.append)

    def endpoint(**kw):
        if kw["api_key"] == "k1":
            raise _err(litellm.RateLimitError, kw["model"])
        return litellm_mock_response("ok")

    with patch("litellm.completion", side_effect=endpoint) as c:
        assert call_llm("groq/a", MSGS).text == "ok"
        assert [x.kwargs["api_key"] for x in c.call_args_list] == ["k1", "k2"]
        assert slept == []
        call_llm("groq/a", MSGS)   # k1 sits out its minute: the next call starts at k2
        assert c.call_args.kwargs["api_key"] == "k2" and c.call_count == 3


def test_a_pool_of_refused_keys_ends_in_the_error(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k1")
    monkeypatch.setenv("GROQ_API_KEY_2", "k2")
    with patch("litellm.completion", side_effect=lambda **kw: (_ for _ in ()).throw(
            _err(litellm.AuthenticationError, kw["model"]))) as c, \
            pytest.raises(litellm.AuthenticationError):
        call_llm("groq/a", MSGS)
    assert c.call_count == 2
    assert set(llm_mod._benched) == {"k1", "k2"}


def test_a_down_endpoint_falls_back_and_stays_behind(monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "fallback_models", "deepseek/c, groq/a")

    def endpoint(**kw):
        if kw["model"] == "groq/a":
            raise _err(litellm.ServiceUnavailableError, kw["model"])
        return litellm_mock_response(kw["model"])

    with patch("litellm.completion", side_effect=endpoint) as c:
        assert call_llm("groq/a", MSGS).text == "deepseek/c"
        assert [x.kwargs["model"] for x in c.call_args_list] == ["groq/a"] * 3 + ["deepseek/c"]
        c.reset_mock()
        assert call_llm("groq/a", MSGS).text == "deepseek/c"
        assert c.call_count == 1   # held: the dead endpoint is not paid again for 5 minutes


def test_a_bad_request_is_the_requests_fault_and_does_not_fall_back(monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "fallback_models", "deepseek/c")
    with patch("litellm.completion", side_effect=lambda **kw: (_ for _ in ()).throw(
            _err(litellm.BadRequestError, kw["model"]))) as c, \
            pytest.raises(litellm.BadRequestError):
        call_llm("groq/a", MSGS)
    assert [x.kwargs["model"] for x in c.call_args_list] == ["groq/a"]


def test_the_callers_key_stays_with_the_callers_model(monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "fallback_models", "deepseek/c")

    def endpoint(**kw):
        if kw["model"] == "groq/a":
            raise _err(litellm.AuthenticationError, kw["model"])
        return litellm_mock_response("ok")

    with patch("litellm.completion", side_effect=endpoint) as c:
        call_llm("groq/a", MSGS, api_key="worker-key")
    first, second = c.call_args_list
    assert first.kwargs["api_key"] == "worker-key"
    assert second.kwargs.get("api_key") != "worker-key"
