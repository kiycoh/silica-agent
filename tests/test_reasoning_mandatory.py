"""`reasoning=False` is a request, not a guarantee: some endpoints refuse it.

Measured 2026-08-24 on openrouter/stealth/ox-alpha:
`{"error":{"message":"Reasoning is mandatory for this endpoint and cannot be
disabled.","code":400}}`. BadRequestError is not transient, so every caller that
asks for the knob (kernel/forms.py, kernel/residue.py, capabilities/dedup.py)
lost its whole lane on that model — form sniffing swallows the exception at
debug level and silently falls back, so nothing on screen said why.
"""
from __future__ import annotations

from unittest.mock import patch

import litellm

from silica.agent.llm import call_llm
from tests.llm_mocks import litellm_mock_response

_REFUSAL = ('OpenrouterException - {"error":{"message":"Reasoning is mandatory '
            'for this endpoint and cannot be disabled.","code":400}}')


def _refuses_the_knob(**kwargs):
    """An endpoint that 400s the knob and answers fine without it."""
    if "reasoning" in (kwargs.get("extra_body") or {}):
        raise litellm.BadRequestError(
            message=_REFUSAL, model=kwargs["model"], llm_provider="openrouter")
    return litellm_mock_response("ok")


def test_refused_knob_falls_back_to_thinking_on():
    from silica.agent import llm as llm_mod

    llm_mod._REASONING_MANDATORY.discard("openrouter/test/model")
    with patch("litellm.completion", side_effect=_refuses_the_knob) as completion:
        res = call_llm(model="openrouter/test/model",
                       messages=[{"role": "user", "content": "hi"}], reasoning=False)

    assert res.text == "ok"
    assert completion.call_count == 2, "one rejected call, then one without the knob"
    assert "reasoning" not in (completion.call_args.kwargs.get("extra_body") or {})


def test_refusal_is_remembered_so_it_is_paid_once():
    from silica.agent import llm as llm_mod

    llm_mod._REASONING_MANDATORY.discard("openrouter/test/model")
    with patch("litellm.completion", side_effect=_refuses_the_knob):
        call_llm(model="openrouter/test/model",
                 messages=[{"role": "user", "content": "hi"}], reasoning=False)
    with patch("litellm.completion", side_effect=_refuses_the_knob) as second:
        call_llm(model="openrouter/test/model",
                 messages=[{"role": "user", "content": "hi"}], reasoning=False)

    assert second.call_count == 1, "the second call must not re-pay the 400"


def test_other_bad_requests_still_raise():
    """Only the reasoning refusal is absorbed — a wrong model id must surface."""
    import pytest

    from silica.agent import llm as llm_mod

    llm_mod._REASONING_MANDATORY.discard("openrouter/test/model")

    def _bad_model(**kwargs):
        raise litellm.BadRequestError(message="model not found",
                                      model=kwargs["model"], llm_provider="openrouter")

    with patch("litellm.completion", side_effect=_bad_model), \
         pytest.raises(litellm.BadRequestError):
        call_llm(model="openrouter/test/model",
                 messages=[{"role": "user", "content": "hi"}], reasoning=False)


def test_custom_endpoint_gets_the_llama_cpp_thinking_switch():
    """The openrouter knob never reached a local server: Learn's item call on
    llama-server Qwen3.5-9B thought for 65 s and answered "" (2026-10-06)."""
    from silica.agent import llm as llm_mod

    llm_mod._REASONING_MANDATORY.discard("custom/qwen-local")
    with patch("litellm.completion", return_value=litellm_mock_response("ok")) as completion:
        call_llm(model="custom/qwen-local", messages=[{"role": "user", "content": "hi"}], reasoning=False)
    body = completion.call_args.kwargs.get("extra_body") or {}
    assert body.get("chat_template_kwargs") == {"enable_thinking": False}

    with patch("litellm.completion", return_value=litellm_mock_response("ok")) as completion:
        call_llm(model="custom/qwen-local", messages=[{"role": "user", "content": "hi"}])
    assert "chat_template_kwargs" not in (completion.call_args.kwargs.get("extra_body") or {})


def test_custom_host_that_rejects_the_switch_is_retried_without():
    from silica.agent import llm as llm_mod

    llm_mod._REASONING_MANDATORY.discard("custom/strict-host")

    def _strict(**kwargs):
        if "chat_template_kwargs" in (kwargs.get("extra_body") or {}):
            raise litellm.BadRequestError(message="Unrecognized request argument supplied: chat_template_kwargs",
                                          model=kwargs["model"], llm_provider="openai")
        return litellm_mock_response("ok")

    with patch("litellm.completion", side_effect=_strict) as completion:
        res = call_llm(model="custom/strict-host", messages=[{"role": "user", "content": "hi"}], reasoning=False)
    assert res.text == "ok" and completion.call_count == 2
    with patch("litellm.completion", side_effect=_strict) as second:
        call_llm(model="custom/strict-host", messages=[{"role": "user", "content": "hi"}], reasoning=False)
    assert second.call_count == 1, "the refusal is paid once per process"
