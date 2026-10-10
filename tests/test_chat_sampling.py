# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The custom/ route sends the model card's sampling when the caller sets none (ADR-0080)."""
from __future__ import annotations

from unittest.mock import patch

from silica.agent.llm import call_llm
from tests.llm_mocks import litellm_mock_response


def _sent(**call_kwargs) -> dict:
    with patch("litellm.completion", return_value=litellm_mock_response("ok")) as completion:
        call_llm(model="custom/qwen3.5-9b", messages=[{"role": "user", "content": "hi"}], **call_kwargs)
    return completion.call_args.kwargs


def test_card_sampling_per_mode_and_a_caller_value_wins():
    off = _sent(reasoning=False)
    assert (off["temperature"], off["top_p"]) == (0.7, 0.8)
    # top_k rides in the body, beside the thinking switch, which it must not replace.
    assert off["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20}

    on = _sent()
    assert (on["temperature"], on["top_p"], on["extra_body"]["top_k"]) == (1.0, 0.95, 20)

    pinned = _sent(temperature=0.0)  # what providers.py sends on a schema call
    assert pinned["temperature"] == 0.0
    assert "top_p" not in pinned and "top_k" not in (pinned.get("extra_body") or {})
