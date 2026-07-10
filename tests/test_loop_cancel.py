"""Tests for cancel_token integration in run_agent (silica/agent/loop.py).

LLM calls are patched so we never touch real providers.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from silica.agent.loop import run_agent


def _fake_resp(*, tool_calls=None, text="done"):
    """Minimal LLM response stub."""
    resp = SimpleNamespace(
        assistant_message={"role": "assistant", "content": text},
        tool_calls=tool_calls or [],
        text=text,
        reasoning=None,
    )
    return resp


def test_cancel_before_first_iteration():
    """Token already set → loop exits immediately before the first LLM call."""
    token = threading.Event()
    token.set()

    call_count = 0

    def fake_call_llm(*a, **k):
        nonlocal call_count
        call_count += 1
        return _fake_resp()

    with patch("silica.agent.loop.call_llm", fake_call_llm):
        result = run_agent(
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            cancel_token=token,
        )

    assert result == "(silica: cancelled)"
    assert call_count == 0


def test_cancel_after_first_iteration():
    """Token is set after the first LLM call completes; second call must not happen."""
    token = threading.Event()

    # Without tool calls the loop returns immediately after the first response,
    # so we need a tool call on iteration 1 to force a second LLM call.
    iter_count = [0]

    def fake_call_llm2(*a, **k):
        iter_count[0] += 1
        if iter_count[0] == 1:
            # First LLM call: return a (fake) tool call so loop continues
            tc = SimpleNamespace(
                name="unknown_tool_that_will_error",
                args={},
                id="c1",
            )
            resp = SimpleNamespace(
                assistant_message={"role": "assistant", "content": ""},
                tool_calls=[tc],
                text="",
                reasoning=None,
            )
            token.set()   # set token AFTER first LLM call returns
            return resp
        # Second LLM call should never happen
        return _fake_resp()

    with patch("silica.agent.loop.call_llm", fake_call_llm2):
        result = run_agent(
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            cancel_token=token,
        )

    assert result == "(silica: cancelled)"
    assert iter_count[0] == 1   # only one LLM call was made


def test_no_cancel_token_runs_normally():
    """When cancel_token is None, the loop runs to completion as before."""
    def fake_call_llm(*a, **k):
        return _fake_resp(text="hello")

    with patch("silica.agent.loop.call_llm", fake_call_llm):
        result = run_agent(
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            cancel_token=None,
        )

    assert result == "hello"


def test_interrupt_does_not_join_inflight_llm_call():
    """Ctrl+C during an in-flight LLM call must return control WITHOUT waiting on
    the (uncancellable, sync) worker. Regression guard: a `with ThreadPoolExecutor`
    would call shutdown(wait=True) in __exit__ and re-block the loop after the KI."""
    import concurrent.futures as cf

    shutdown_waits: list[bool] = []

    class SpyExecutor(cf.ThreadPoolExecutor):
        def shutdown(self, wait=True, **kwargs):  # record intent, never actually block
            shutdown_waits.append(wait)
            return super().shutdown(wait=False)

    def fake_call_llm(*a, **k):
        raise KeyboardInterrupt  # surfaces from _future.result() like a real Ctrl+C

    with patch("silica.agent.loop._cf.ThreadPoolExecutor", SpyExecutor), \
         patch("silica.agent.loop.call_llm", fake_call_llm):
        with pytest.raises(KeyboardInterrupt):
            run_agent(messages=[{"role": "user", "content": "hi"}], model="test")

    assert shutdown_waits, "LLM pool was never shut down"
    assert all(w is False for w in shutdown_waits), f"blocking shutdown used: {shutdown_waits}"


def test_bus_receives_events_during_run():
    """run_agent publishes agent/* events to BUS even with no callback."""
    import silica.agent.bus as bus_mod
    received = []
    bus_mod.BUS.subscribe("agent/*", received.append)

    def fake_call_llm(*a, **k):
        return _fake_resp(text="hi")

    with patch("silica.agent.loop.call_llm", fake_call_llm):
        run_agent(
            messages=[{"role": "user", "content": "hi"}],
            model="test",
        )

    # At minimum ThinkingStartEvent and ThinkingEndEvent should arrive.
    from silica.agent.events import ThinkingStartEvent, ThinkingEndEvent
    types = [type(e) for e in received]
    assert ThinkingStartEvent in types
    assert ThinkingEndEvent in types
