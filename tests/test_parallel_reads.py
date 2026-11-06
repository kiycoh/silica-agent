# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A block of two or more reads runs at once (ADR-0150), and everything the
loop does with their results still happens in call order: the events, the
history, the taint gate's attribution."""
from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from silica.agent import loop
from silica.obs.events import ToolCompleteEvent, ToolStartEvent
from silica.tools import TOOLS, Tool
from silica.tools import user_memory as memory_gate


class _Args(BaseModel):
    pass


def _resp(tool_calls=None, text="done"):
    return SimpleNamespace(assistant_message={"role": "assistant", "content": text},
                           tool_calls=tool_calls or [], text=text, reasoning=None, usage={})


def _run(names, register, *, callback=None):
    calls = [0]
    tcs = [SimpleNamespace(name=n, args={}, id=f"c{i}") for i, n in enumerate(names)]

    def fake_call_llm(model, messages, tools=None, cancel=None, **kw):
        calls[0] += 1
        return _resp(tool_calls=tcs) if calls[0] == 1 else _resp(text="done")

    messages = [{"role": "user", "content": "hi"}]
    with patch("silica.agent.loop.call_llm", fake_call_llm):
        loop.run_agent(messages=messages, model="m", tool_progress_callback=callback,
                       cancel_token=threading.Event())
    return [m for m in messages if m.get("role") == "tool"]


@pytest.fixture
def register(monkeypatch):
    def reg(name, fn, read=True):
        monkeypatch.setitem(TOOLS, name, Tool(fn, name, "doc", _Args, "atomic"))
        if read:
            monkeypatch.setattr(loop, "PARALLEL_READS", loop.PARALLEL_READS | {name})
    return reg


def test_the_reads_of_one_block_run_at_once(register):
    both = threading.Barrier(2, timeout=5)  # serial calls would break it

    def a():
        both.wait()
        return "a"

    def b():
        both.wait()
        return "b"

    register("read_a", a)
    register("read_b", b)
    out = _run(["read_a", "read_b"], register)
    assert [m["content"] for m in out] == ["a", "b"]


def test_results_events_and_history_keep_the_call_order(register):
    def slow():
        time.sleep(0.3)
        return "slow"

    register("read_slow", slow)
    register("read_fast", lambda: "fast")
    seen = []
    out = _run(["read_slow", "read_fast"], register,
               callback=lambda e: seen.append((type(e).__name__, e.name, getattr(e, "duration_s", None)))
               if isinstance(e, (ToolStartEvent, ToolCompleteEvent)) else None)
    assert [m["tool_call_id"] for m in out] == ["c0", "c1"]
    assert [(k, n) for k, n, _ in seen] == [
        ("ToolStartEvent", "read_slow"), ("ToolCompleteEvent", "read_slow"),
        ("ToolStartEvent", "read_fast"), ("ToolCompleteEvent", "read_fast")]
    # each call reports its own run time, not the time it waited in line
    durations = {n: d for k, n, d in seen if k == "ToolCompleteEvent"}
    assert durations["read_slow"] >= 0.25 and durations["read_fast"] < 0.2


def _overlapped(names, register, write_name=None):
    spans = {}

    def make(name):
        def fn():
            t0 = time.perf_counter()
            time.sleep(0.15)
            spans[name] = (t0, time.perf_counter())
            return name
        return fn

    for n in names:
        register(n, make(n), read=n != write_name)
    _run(names, register)
    (s1, e1), (s2, e2) = spans[names[0]], spans[names[1]]
    return s2 < e1 and s1 < e2


def test_a_block_holding_a_write_stays_serial(register):
    assert not _overlapped(["read_x", "write_y"], register, write_name="write_y")


def test_two_recalls_in_one_block_stay_serial(register, monkeypatch):
    assert loop._prefetch_reads(
        [SimpleNamespace(name="silica_recall", args={}, id=i) for i in "ab"],
        {"silica_recall": TOOLS["silica_recall"]}, None) == {}


def test_a_single_read_is_not_handed_to_a_pool(register):
    register("read_one", lambda: "1")
    assert loop._prefetch_reads([SimpleNamespace(name="read_one", args={}, id="c")],
                                {"read_one": TOOLS["read_one"]}, None) == {}


def test_a_read_sees_the_turns_context_variables(register):
    var = contextvars.ContextVar("scope_probe", default="unset")
    var.set("workspace")
    seen = []
    register("read_p", lambda: seen.append(var.get()) or "p")
    register("read_q", lambda: seen.append(var.get()) or "q")
    _run(["read_p", "read_q"], register)
    assert seen == ["workspace", "workspace"]


def test_each_call_is_credited_with_the_paths_it_returned(register, monkeypatch):
    def p():
        memory_gate.returned_notes(["Mail/one.md"])
        return "p"

    def q():
        time.sleep(0.1)  # finishes after p: on a shared list p would carry q's path
        memory_gate.returned_notes(["Mail/two.md"])
        return "q"

    register("read_p2", p)
    register("read_q2", q)
    credited = []
    real = memory_gate.after_tool

    def spy(turn, name):
        credited.append((name, list(turn.returned)))
        return real(turn, name)

    monkeypatch.setattr(memory_gate, "after_tool", spy)
    _run(["read_p2", "read_q2"], register)
    assert credited == [("read_p2", ["Mail/one.md"]), ("read_q2", ["Mail/two.md"])]


def test_a_read_that_raises_is_reported_as_the_serial_path_reports_it(register):
    def boom():
        raise RuntimeError("disk gone")

    # Tool.run turns an exception into a JSON error, as it does serially.
    register("read_boom", boom)
    register("read_ok", lambda: "ok")
    out = _run(["read_boom", "read_ok"], register)
    assert "disk gone" in out[0]["content"] and out[1]["content"] == "ok"


def test_a_read_stop_came_before_never_runs(register):
    ran = []
    register("read_late", lambda: ran.append(1) or "x")
    token = threading.Event()
    token.set()
    result, raised, seconds, paths = loop._run_read(TOOLS["read_late"], SimpleNamespace(name="read_late", args={}), token)
    assert ran == [] and "stopped before it ran" in result and paths == []
