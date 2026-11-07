# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one way a run's ambient state crosses a thread (agent/run_context.py)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from silica.agent import read_scope
from silica.agent.run_context import RunContext
from silica.kernel.write import gate
from silica.model import llm
from silica.obs import narration as narr


def _on_a_thread(fn):
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn()))
    t.start()
    t.join()
    return box["r"]


def _ambient():
    return {"requests": read_scope.REQUESTS.get(), "meter": llm._SCOPE.get(), "undo": gate._current_undo_run.get(),
            "run": narr.current_run(), "parent": narr.current_parent()}


def test_a_bare_thread_has_none_of_it():
    token = read_scope.REQUESTS.set(("the question",))
    try:
        assert _on_a_thread(_ambient)["requests"] == ()
    finally:
        read_scope.REQUESTS.reset(token)


def test_a_helper_carries_everything_ambient():
    counter = llm.new_meter()
    token = read_scope.REQUESTS.set(("the question",))
    try:
        with llm.meter_scope(counter), gate.in_run(undo="u1"):
            seen = _on_a_thread(RunContext.here().spawn(_ambient))
    finally:
        read_scope.REQUESTS.reset(token)
    assert seen["requests"] == ("the question",) and seen["meter"] is counter and seen["undo"] == "u1"


def test_a_workers_context_holds_the_run_and_nothing_of_the_turn():
    """A worker's read is not the turn's: it must not be narrowed to the turn's
    workspace, spend a read-scope decision or taint the turn's memory gate."""
    token = read_scope.REQUESTS.set(("the question",))
    try:
        seen = _on_a_thread(RunContext.of_run(run="r1", parent="run-r1", undo="u1").spawn(_ambient))
    finally:
        read_scope.REQUESTS.reset(token)
    assert seen == {"requests": (), "meter": None, "undo": "u1", "run": "r1", "parent": "run-r1"}


def test_one_spawned_function_runs_on_every_thread_of_a_pool_at_once():
    """A Context can be entered by one thread at a time, so each call needs its own."""
    barrier = threading.Barrier(3, timeout=10)
    work = RunContext.here().spawn(lambda n: (barrier.wait(), n)[1])
    with ThreadPoolExecutor(3) as pool:
        assert sorted(pool.map(work, range(3))) == [0, 1, 2]


def test_a_model_call_in_a_loop_under_a_tool_is_the_tools_child(_fresh_narrator, monkeypatch):
    """`parent` is the innermost open span (narration spec §3). The loop runs each
    model call on a thread of its own, so a tool that runs a loop of its own, as
    silica_web_answer does, left its `call` beats at the root beside the `thought`
    beats that carried the tool."""
    from silica.agent.constraints import AgentConstraints
    from silica.agent.loop import run_agent
    from silica.model.llm import LLMResponse

    monkeypatch.setattr("silica.model.llm._call_llm", lambda *a, **k: LLMResponse(
        text="done", assistant_message={"role": "assistant", "content": "done"}, finish_reason="stop"))
    monkeypatch.setattr("silica.model.llm.fallback_chain", lambda model: [model])
    sid = _fresh_narrator.ensure_session(driver="test")
    _fresh_narrator.span_open("tool", "outer", "silica_web_answer", {}, attach=True)
    run_agent([{"role": "user", "content": "q"}], "local/m", constraints=AgentConstraints(tools=(), interactive=True))
    _fresh_narrator.span_close("tool", "outer", "done", "done")
    calls = [b for b in narr.read_beats(narr.narration_dir() / f"{sid}.jsonl") if b["kind"] == "call"]
    assert calls and {b["parent"] for b in calls} == {"outer"}
