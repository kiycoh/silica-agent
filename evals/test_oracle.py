# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The oracle cache: full-payload key, empty-never-cached, bypass valve.
No network: call_llm is monkeypatched."""
from __future__ import annotations

import evals.oracle as oracle


class _Resp:
    def __init__(self, text: str):
        self.text = text


def _patch(monkeypatch, tmp_path, replies: list[str]) -> list:
    calls: list = []

    def fake(model, messages, **kw):
        calls.append((model, kw))
        return _Resp(replies[min(len(calls) - 1, len(replies) - 1)])

    monkeypatch.setattr(oracle, "_CACHE_DIR", tmp_path / "oracle")
    monkeypatch.setattr("silica.agent.llm.call_llm", fake)
    monkeypatch.setattr(oracle.time, "sleep", lambda s: None)
    return calls


def test_hit_skips_upstream(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, ["yes"])
    msgs = [{"role": "user", "content": "q"}]
    a = oracle.cached_text("m", msgs, max_tokens=8, temperature=0.0)
    b = oracle.cached_text("m", msgs, max_tokens=8, temperature=0.0)
    assert a == b == "yes"
    assert len(calls) == 1


def test_every_knob_is_in_the_key(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, ["yes"])
    msgs = [{"role": "user", "content": "q"}]
    oracle.cached_text("m", msgs, max_tokens=8)
    oracle.cached_text("m", msgs, max_tokens=9)      # knob change = new key
    oracle.cached_text("m2", msgs, max_tokens=8)     # model change = new key
    assert len(calls) == 3


def test_empty_reply_retries_and_is_never_cached(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, [""])
    assert oracle.cached_text("m", [{"role": "user", "content": "q"}]) == ""
    assert len(calls) == oracle._ATTEMPTS  # retried in-call
    assert not list((tmp_path / "oracle").rglob("*.json"))  # nothing frozen
    calls.clear()
    oracle.cached_text("m", [{"role": "user", "content": "q"}])
    assert calls  # the next call goes upstream again, not to a cached ""


def test_no_cache_env_bypasses_reads_and_refreshes(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, tmp_path, ["old", "new"])
    msgs = [{"role": "user", "content": "q"}]
    assert oracle.cached_text("m", msgs) == "old"
    monkeypatch.setenv("SILICA_EVAL_NO_CACHE", "1")
    assert oracle.cached_text("m", msgs) == "new"    # read bypassed
    monkeypatch.delenv("SILICA_EVAL_NO_CACHE")
    assert oracle.cached_text("m", msgs) == "new"    # entry refreshed in place
    assert len(calls) == 2
