# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The dedup judge never sees the cosine (ADR-0079 decision 3)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from silica.config import SilicaConfig


def test_neither_judge_prompt_carries_the_closeness_score():
    """ADR-0030 measured a closeness score in front of the judge pushing it to
    merge; both the single and the family prompt leave it out."""
    from silica.capabilities.dedup import _decide_dedup, _decide_dedup_batch

    prompts = []

    class _Provider:
        def call_llm(self, *, messages, **kw):
            prompts.append(messages[0]["content"])
            return SimpleNamespace(text='{"verdict": "distinct", "rationale": "r"}')

    with patch("silica.agent.providers.get_provider", return_value=_Provider()):
        _decide_dedup(SilicaConfig(), concept="C", excerpt="e", candidate_name="N", candidate_body="b",
                      score=0.937, full_score=0.937, title_score=0.951)
        _decide_dedup_batch(SilicaConfig(), concepts=[{"concept": "C", "excerpt": "e", "score": 0.937,
                                                       "full_score": 0.937, "title_score": 0.951}],
                            candidate_name="N", candidate_body="b")
    for prompt in prompts:
        assert "0.937" not in prompt and "0.951" not in prompt
        assert "CLOSENESS SCORE" not in prompt
