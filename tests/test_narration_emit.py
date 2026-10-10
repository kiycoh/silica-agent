# SPDX-License-Identifier: AGPL-3.0-or-later
"""The emit-site grafts: each seam gets the smallest test that fails
without it (call_llm wrapper, consume spans, commit_ops write beats,
the GUI turn round-trip)."""
from __future__ import annotations

import pytest

import silica.agent.narration as narr


@pytest.fixture()
def narrator(_fresh_narrator, monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.vault_path", "/v/demo", raising=False)
    _fresh_narrator.ensure_session(driver="tui")
    return _fresh_narrator


def _beats(narrator):
    return list(narr.read_beats(narr.narration_dir() / f"{narrator.sid}.jsonl"))


def test_call_llm_wrapper_narrates_the_envelope(narrator, monkeypatch):
    import silica.agent.llm as llm

    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: llm.LLMResponse(
        text="hi", usage={"prompt_tokens": 900, "completion_tokens": 40}))
    llm.call_llm("test/model", [{"role": "user", "content": "q"}])
    calls = [b for b in _beats(narrator) if b["kind"] == "call"]
    assert [b["status"] for b in calls] == ["running", "done"]
    assert calls[1]["payload"]["prompt_tokens"] == 900
    assert calls[0]["id"] == calls[1]["id"]


def test_call_llm_failure_closes_the_span_failed(narrator, monkeypatch):
    import silica.agent.llm as llm

    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(llm, "_call_llm", boom)
    with pytest.raises(RuntimeError):
        llm.call_llm("test/model", [])
    calls = [b for b in _beats(narrator) if b["kind"] == "call"]
    assert calls[-1]["status"] == "failed"
    assert "provider down" in calls[-1]["payload"]["error"]


def test_consume_wraps_items_in_attributed_subagent_spans(narrator):
    from silica.agent.subagent import consume
    from silica.kernel.workqueue import WorkItem, WorkQueue

    class FakeAgent:
        def handle(self, item):
            narr.NARRATOR.narrate("write", "committed", "inner write")
            return {"status": "done"}

    wq = WorkQueue()
    item = WorkItem(kind="dedup", target_path="Concepts/X.md", id="it1")
    wq.enqueue(item)
    wq.close()
    consume(wq, FakeAgent(), parent_span="run-r9", run_id="r9")
    beats = _beats(narrator)
    sub = [b for b in beats if b["kind"] == "subagent"]
    assert [b["status"] for b in sub] == ["running", "done"]
    assert sub[0]["parent"] == "run-r9" and sub[0]["run"] == "r9"
    inner = next(b for b in beats if b["summary"] == "inner write")
    assert inner["parent"] == "it1"          # attributed to the subagent span
    work = [b for b in beats if b["kind"] == "work"]
    assert work[-1]["status"] == "done" and work[-1]["id"] == "wk-it1"


def test_consume_cancelled_item_narrates_cancelled(narrator):
    from silica.agent.subagent import consume
    from silica.kernel.workqueue import WorkItem, WorkQueue

    wq = WorkQueue()
    item = WorkItem(kind="refine", target_path="A.md", id="it2")
    item.cancel_token.set()
    wq.enqueue(item)
    wq.close()
    consume(wq, object())
    sub = [b for b in _beats(narrator) if b["kind"] == "subagent"]
    assert sub[-1]["status"] == "cancelled"
    assert sub[-1]["payload"]["phase"] == "pre_handle"


def test_commit_ops_narrates_proposed_then_committed(narrator, monkeypatch):
    import silica.agent.commit as commit
    import silica.tools.composed as composed
    import silica.tools.wrapped as wrapped
    from silica.kernel.write.ops import Op, OpType

    op = Op(op=OpType.patch, heading="X", source_basename="s.md", path="Concepts/X.md",
            snippet="body")
    monkeypatch.setattr(composed, "silica_validate_ops", lambda *a, **k: {"validated_count": 1})
    monkeypatch.setattr(commit, "load_ops", lambda _p: [op])
    monkeypatch.setattr(wrapped, "silica_snapshot", lambda *a, **k: {"txn_id": "t" * 12, "inverses": []})
    monkeypatch.setattr(composed, "silica_bulk_write", lambda *a, **k: {"successful": 1, "total": 1})
    monkeypatch.setattr(composed, "silica_lint", lambda *a, **k: {"success": True})
    res = commit.commit_ops([op])
    assert res["status"] == "committed"
    writes = [b for b in _beats(narrator) if b["kind"] == "write"]
    assert [b["status"] for b in writes] == ["proposed", "committed"]
    assert writes[0]["payload"]["touched"] == [["Concepts/X.md", "patch", ""]]
    assert writes[0]["id"] == writes[1]["id"]


def test_commit_ops_rollback_narrates_rolled_back(narrator, monkeypatch):
    import silica.agent.commit as commit
    import silica.tools.composed as composed
    import silica.tools.wrapped as wrapped
    from silica.kernel.write.ops import Op, OpType

    op = Op(op=OpType.patch, heading="X", source_basename="s.md", path="Concepts/X.md",
            snippet="body")
    monkeypatch.setattr(composed, "silica_validate_ops", lambda *a, **k: {"validated_count": 1})
    monkeypatch.setattr(commit, "load_ops", lambda _p: [op])
    monkeypatch.setattr(wrapped, "silica_snapshot", lambda *a, **k: {"txn_id": "t" * 12, "inverses": []})
    monkeypatch.setattr(composed, "silica_bulk_write", lambda *a, **k: {"error": "disk full"})
    monkeypatch.setattr(wrapped, "silica_restore", lambda *a, **k: None)
    res = commit.commit_ops([op])
    assert res["status"] == "rolled_back"
    writes = [b for b in _beats(narrator) if b["kind"] == "write"]
    assert [b["status"] for b in writes] == ["proposed", "rolled_back"]
    assert "disk full" in writes[1]["summary"]


def test_gui_turn_roundtrip_replay_and_endpoints(narrator, monkeypatch, tmp_path):
    """The GUI seam end to end: a turn lands as beats, /narration replays it,
    and a reloaded session equals what the user saw."""
    from fastapi.testclient import TestClient
    from silica.ui.web import server

    narrator.close()   # the server owns its own session
    server._reset_session()

    def fake_run_agent(messages, model, tool_progress_callback=None,
                       cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "Reply"})
        return "Reply"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    tc = TestClient(server.app)
    tc.post("/chat", json={"text": "hello narration"})

    sid = server.current_session_id
    assert sid
    body = tc.get("/narration").json()
    kinds = [b["kind"] for b in body["beats"]]
    assert body["sid"] == sid
    assert kinds[0] == "session"
    turns = [b["payload"]["message"] for b in body["beats"] if b["kind"] == "turn"]
    assert turns[0]["content"] == "hello narration"
    assert turns[-1]["content"] == "Reply"

    # from_seq is a real cursor, not a rendering
    tail = tc.get("/narration", params={"from_seq": body["beats"][-1]["seq"]}).json()
    assert tail["beats"] == []

    # reset then load: the replay is the store (no snapshot anywhere)
    tc.post("/reset")
    assert tc.post("/session/load", json={"id": sid}).status_code == 200
    assert server.messages[-1]["content"] == "Reply"


def test_gui_stop_narrates_cancel(narrator, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from silica.ui.web import server

    server.current_cancel = threading.Event()
    tc = TestClient(server.app)
    tc.post("/stop")
    assert server.current_cancel.is_set()
    beats = _beats(narrator)
    assert beats[-1]["kind"] == "cancel"
    assert beats[-1]["payload"]["driver"] == "gui"


def test_repl_sessions_resume_handlers(narrator, monkeypatch, capsys):
    """The REPL surface: /sessions lists, /resume rebuilds messages from the
    narration, and the meta dispatcher owns both."""
    from silica.cli import _handle_slash_command

    narrator.turn({"role": "user", "content": "prima domanda"})
    narrator.turn({"role": "assistant", "content": "prima risposta"})
    sid = narrator.sid
    narrator.close()

    monkeypatch.setattr("silica.cli._fresh_messages", lambda: [{"role": "system", "content": "seed"}])
    monkeypatch.setattr("silica.cli._update_context_tokens", lambda m: None)

    assert _handle_slash_command("/sessions", []) is True
    messages: list[dict] = []
    assert _handle_slash_command("/resume 1", messages) is True
    assert [m["content"] for m in messages] == ["seed", "prima domanda", "prima risposta"]
    from silica.agent.narration import NARRATOR
    assert NARRATOR.sid == sid              # appending continues the same account

    assert _handle_slash_command("/resume 99", []) is True   # graceful, no raise
    assert _handle_slash_command("/sessions prune", []) is True  # usage line, no delete


# --- observability levers (2026-09-06 audit) ---------------------------------
# The call beat is the one record every lane writes, so the trace, the wire
# request and the retries all land on it; the tool beat gains the MCP lane;
# the judges narrate their verdicts under an owner-prefixed kind.

def _fake_resp(**kw):
    import silica.agent.llm as llm
    base = dict(text="hi", usage={"prompt_tokens": 9, "completion_tokens": 4})
    base.update(kw)
    return llm.LLMResponse(**base)


def _done_call(narrator):
    return [b for b in _beats(narrator) if b["kind"] == "call" and b["status"] == "done"][-1]


def test_call_beat_carries_the_reasoning_trace(narrator, monkeypatch):
    import silica.agent.llm as llm

    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: _fake_resp(reasoning="because"))
    llm.call_llm("test/model", [{"role": "user", "content": "q"}])
    assert _done_call(narrator)["payload"]["reasoning"] == "because"


def test_trace_flag_records_messages_request_and_reply(narrator, monkeypatch):
    import silica.agent.llm as llm

    monkeypatch.setenv("SILICA_TRACE_LLM", "1")
    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: _fake_resp(
        assistant_message={"role": "assistant", "content": "hi", "silica_reasoning": "x"},
        request={"max_tokens": 77, "tools": ["silica_recall"]}))
    llm.call_llm("test/model", [{"role": "user", "content": "q", "origin": "gui"}])
    p = _done_call(narrator)["payload"]
    assert p["messages"] == [{"role": "user", "content": "q"}]   # wire form: origin stripped
    assert p["request"] == {"max_tokens": 77, "tools": ["silica_recall"]}
    assert p["reply"] == {"role": "assistant", "content": "hi"}  # the trace has its own key


def test_without_the_trace_flag_the_call_beat_stays_an_envelope(narrator, monkeypatch):
    import silica.agent.llm as llm

    monkeypatch.delenv("SILICA_TRACE_LLM", raising=False)
    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: _fake_resp())
    llm.call_llm("test/model", [{"role": "user", "content": "q"}])
    p = _done_call(narrator)["payload"]
    assert "messages" not in p and "reply" not in p and "request" not in p


def test_trace_flag_records_messages_on_a_failed_call_too(narrator, monkeypatch):
    import silica.agent.llm as llm

    monkeypatch.setenv("SILICA_TRACE_LLM", "1")

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(llm, "_call_llm", boom)
    with pytest.raises(RuntimeError):
        llm.call_llm("test/model", [{"role": "user", "content": "q"}])
    failed = [b for b in _beats(narrator) if b["kind"] == "call"][-1]
    assert failed["status"] == "failed"
    assert failed["payload"]["messages"] == [{"role": "user", "content": "q"}]


def test_request_record_keeps_the_wire_kwargs_but_never_the_key(monkeypatch):
    import litellm
    import silica.agent.llm as llm
    from tests.llm_mocks import litellm_mock_response

    seen = {}

    def fake(**kw):
        seen.update(kw)
        return litellm_mock_response("ok")
    monkeypatch.setattr(litellm, "completion", fake)
    tools = [{"type": "function", "function": {"name": "t1", "parameters": {}}}]
    resp = llm._call_llm("custom/m", [{"role": "user", "content": "q"}],
                         tools=tools, max_tokens=50, temperature=0)
    assert resp.request["tools"] == ["t1"]
    assert resp.request["temperature"] == 0
    assert resp.request["max_tokens"] == seen["max_tokens"]
    assert "api_key" in seen and "api_key" not in resp.request
    assert "messages" not in resp.request


def test_openrouter_asks_for_reasoning_whenever_a_session_is_open(narrator, monkeypatch):
    import litellm
    import silica.agent.llm as llm
    from silica.config import CONFIG
    from tests.llm_mocks import litellm_mock_response

    monkeypatch.setattr(CONFIG, "show_thinking", False)
    monkeypatch.setattr(CONFIG, "verbose", False)
    seen = {}

    def fake(**kw):
        seen.clear()
        seen.update(kw)
        return litellm_mock_response("ok")
    monkeypatch.setattr(litellm, "completion", fake)
    llm._call_llm("openrouter/x/y", [{"role": "user", "content": "q"}])
    assert seen.get("include_reasoning") is True
    narrator.close()
    llm._call_llm("openrouter/x/y", [{"role": "user", "content": "q"}])
    assert "include_reasoning" not in seen


def test_a_transient_retry_narrates_on_the_open_call_span(narrator, monkeypatch):
    import litellm
    import silica.agent.llm as llm
    from tests.llm_mocks import litellm_mock_response

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    n = {"calls": 0}

    def flaky(**kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise litellm.Timeout(message="slow", model="m", llm_provider="test")
        return litellm_mock_response("ok")
    monkeypatch.setattr(litellm, "completion", flaky)
    llm.call_llm("test/model", [{"role": "user", "content": "q"}])
    calls = [b for b in _beats(narrator) if b["kind"] == "call"]
    assert [b["status"] for b in calls] == ["running", "running", "done"]
    assert len({b["id"] for b in calls}) == 1
    retry = calls[1]["payload"]
    assert retry["attempt"] == 1 and "slow" in retry["error"] and retry["delay_s"] >= 0
    assert "retry 1/" in calls[1]["summary"]
    assert calls[2]["payload"]["retries"] == 1


def test_mcp_tool_call_is_a_tool_span_that_parents_its_inner_beats(narrator):
    from silica.ui.mcp import run_tool_narrated

    class T:
        name = "silica_recall"

        def run(self, **kw):
            narr.NARRATOR.narrate("call", "done", "inner")   # what call_llm would write
            return '{"hits": 1}'

    assert run_tool_narrated(T(), {"query": "x"}) == '{"hits": 1}'
    tools = [b for b in _beats(narrator) if b["kind"] == "tool"]
    assert [b["status"] for b in tools] == ["running", "done"]
    assert tools[0]["payload"] == {"name": "silica_recall", "args": {"query": "x"}}
    assert tools[1]["payload"]["result"] == '{"hits": 1}'
    inner = [b for b in _beats(narrator) if b["summary"] == "inner"][0]
    assert inner["parent"] == tools[0]["id"]


def test_mcp_tool_exception_closes_the_span_failed(narrator):
    from silica.ui.mcp import run_tool_narrated

    class T:
        name = "silica_recall"

        def run(self, **kw):
            raise RuntimeError("registry broke")

    with pytest.raises(RuntimeError):
        run_tool_narrated(T(), {})
    last = [b for b in _beats(narrator) if b["kind"] == "tool"][-1]
    assert last["status"] == "failed" and "registry broke" in last["payload"]["error"]


def test_run_mcp_opens_a_narration_session(_fresh_narrator, monkeypatch):
    pytest.importorskip("mcp")
    import signal
    import anyio
    import silica.ui.mcp as mcp

    monkeypatch.setattr("silica.config.CONFIG.vault_path", "/v/demo", raising=False)
    monkeypatch.setattr(anyio, "run", lambda fn: None)
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(mcp, "make_server", lambda a, e: object())
    assert mcp.run_mcp() == 0
    beats = _beats(_fresh_narrator)
    assert beats and beats[0]["kind"] == "session"
    assert beats[0]["payload"]["driver"] == "mcp"


def test_dedup_judge_verdict_is_a_beat_with_the_raw_reply(narrator, monkeypatch):
    from types import SimpleNamespace
    from silica.capabilities import dedup

    class P:
        def call_llm(self, **kw):
            return SimpleNamespace(text='{"verdict": "duplicate", "rationale": "same thing"}')
    monkeypatch.setattr("silica.agent.providers.get_provider", lambda cfg, role="worker": P())
    d = dedup._decide_dedup(SimpleNamespace(judge_model=None), concept="A", excerpt="e",
                            candidate_name="B", candidate_body="b")
    assert d.verdict == "duplicate"
    j = [b for b in _beats(narrator) if b["kind"] == "dedup/judge"][-1]
    assert j["status"] == "done"
    assert j["payload"]["verdict"] == "duplicate" and j["payload"]["judged"] is True
    assert j["payload"]["concept"] == "A" and j["payload"]["candidate"] == "B"
    assert '"duplicate"' in j["payload"]["raw"]


def test_dedup_parse_failure_is_a_failed_judge_beat(narrator, monkeypatch):
    from types import SimpleNamespace
    from silica.capabilities import dedup

    class P:
        def call_llm(self, **kw):
            return SimpleNamespace(text="not json at all")
    monkeypatch.setattr("silica.agent.providers.get_provider", lambda cfg, role="worker": P())
    d = dedup._decide_dedup(SimpleNamespace(judge_model=None), concept="A", excerpt="e",
                            candidate_name="B", candidate_body="b")
    assert d.judged is False
    j = [b for b in _beats(narrator) if b["kind"] == "dedup/judge"][-1]
    assert j["status"] == "failed" and j["payload"]["judged"] is False
    assert j["payload"]["raw"] == "not json at all"
