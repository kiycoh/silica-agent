# SPDX-License-Identifier: AGPL-3.0-or-later
"""Token meter attributes usage to the calling frame, not the LLM plumbing."""
from silica.agent import llm


def _distill(usage):
    return llm.LLMResponse(usage=usage)


def _collision(usage):
    return llm.LLMResponse(usage=usage)


def test_meter_sums_per_callsite(monkeypatch):
    monkeypatch.setattr(llm, "_METER_ON", True)
    llm._meter.clear()

    _distill({"prompt_tokens": 100, "completion_tokens": 10})
    _distill({"prompt_tokens": 50, "completion_tokens": 5})
    _collision({"prompt_tokens": 7, "completion_tokens": 3})

    by_fn = {site.split(":")[-1]: counts for site, counts in llm._meter.items()}
    assert by_fn["_distill"] == [2, 150, 15]
    assert by_fn["_collision"] == [1, 7, 3]


def test_meter_off_is_noop(monkeypatch):
    monkeypatch.setattr(llm, "_METER_ON", False)
    llm._meter.clear()
    llm.LLMResponse(usage={"prompt_tokens": 999, "completion_tokens": 999})
    assert not llm._meter
